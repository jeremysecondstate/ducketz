from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import queue
import threading
import time

import pytest

from ml.intraday.market_input_schwab_stream import (
    ADDITIONAL_SOURCE_FIELDS, SchwabMarketStream, normalize,
)

UTC = timezone.utc
NOW = datetime(2026, 10, 16, 15, 1, 2, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import requests
    import websocket
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline stream tests cannot call providers")
    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr(websocket, "create_connection", forbidden)


def ms(stamp):
    return int(stamp.timestamp() * 1000)


def quote(stamp=NOW, **extra):
    return {"key": "AAPL", "1": 100, "2": 102, "3": 101,
            "4": 20, "5": 10, "8": 1000, "34": ms(stamp), **extra}


def stream(tmp_path, *, service="LEVELONE_EQUITIES", scope=None, clock=None, **kwargs):
    value = SchwabMarketStream(tmp_path, ["AAPL"], clock=clock or (lambda: NOW), **kwargs)
    value._started_at = NOW - timedelta(seconds=5)
    value._status = "connected"
    value._connection = "test-connection"
    value._scopes[service] = scope or {"AAPL": "AAPL"}
    value._acks[service] = {"status": "ok", "keys": list(value._scopes[service]), "received_at": (NOW - timedelta(seconds=2)).isoformat()}
    return value


def message(service, *rows):
    return {"data": [{"service": service, "content": list(rows)}]}


def picked(value, source="schwab_stream_quote"):
    return next(r for r in value.snapshot() if r["source"] == source)


def test_raw_is_durable_before_projection_and_sealed_bytes_do_not_change(tmp_path):
    value = stream(tmp_path)
    value.ingest(message("LEVELONE_EQUITIES", quote()), received_at=NOW)
    result = picked(value)
    segment = result["payload"]["raw_segments"][0]
    path = Path(segment["path"])
    original = path.read_bytes()
    assert hashlib.sha256(original).hexdigest() == segment["sha256"]
    assert json.loads(original)["payload"]["data"][0]["content"][0]["1"] == 100
    assert result["received_at"] == NOW.isoformat()
    value.ingest(message("LEVELONE_EQUITIES", quote(NOW + timedelta(seconds=1))), received_at=NOW + timedelta(seconds=1))
    value.close()
    assert path.read_bytes() == original


def test_callback_runs_before_projection(tmp_path):
    seen = []
    value = stream(tmp_path, persist_raw=lambda p, m: seen.append((p, m, dict(value._rows))) or {"id": "test"})
    value.ingest(message("LEVELONE_EQUITIES", quote()), received_at=NOW)
    assert seen[0][2] == {}
    assert seen[0][1]["received_at"] == NOW.isoformat()
    assert picked(value)["features"]["last"] == 101


@pytest.mark.parametrize("marker", ["synthetic", "is_synthetic", "no_trade", "is_no_trade", "source_empty", "is_source_empty"])
def test_marked_native_rows_remain_raw_but_never_become_features(tmp_path, marker):
    value = stream(tmp_path)
    value.ingest(message("LEVELONE_EQUITIES", quote(**{marker: True})), received_at=NOW)
    assert picked(value)["status"] == "unavailable"
    raw = b"".join(p.read_bytes() for p in (tmp_path / "schwab-stream").rglob("*.jsonl"))
    assert marker.encode() in raw
    assert normalize("LEVELONE_EQUITIES", quote(**{marker: False}), received_at=NOW, scope={"AAPL": "AAPL"}) is not None


def test_delta_fields_are_not_carried_and_receipt_is_not_now(tmp_path):
    now = [NOW]
    value = stream(tmp_path, clock=lambda: now[0])
    value.ingest(message("LEVELONE_EQUITIES", quote()), received_at=NOW)
    later = NOW + timedelta(seconds=1)
    value.ingest(message("LEVELONE_EQUITIES", {"key": "AAPL", "3": 104, "34": ms(later)}), received_at=later)
    now[0] += timedelta(minutes=5)
    result = picked(value)
    assert result["received_at"] == later.isoformat()
    assert result["features"]["last"] == 104
    assert result["features"]["spread_relative"] is None


def test_later_ack_and_disconnect_are_not_backdated(tmp_path):
    now = [NOW + timedelta(seconds=10)]
    value = stream(tmp_path, clock=lambda: now[0])
    value.ingest(message("LEVELONE_EQUITIES", quote()), received_at=NOW)
    ack_time = NOW + timedelta(seconds=2)
    value._acks["LEVELONE_EQUITIES"]["received_at"] = ack_time.isoformat()
    assert picked(value)["received_at"] == ack_time.isoformat()
    value._status = "reconnecting"
    result = picked(value)
    assert result["status"] == "unavailable"
    assert result["received_at"] == now[0].isoformat()


def test_same_event_and_receipt_conflict_masks_latest_instrument(tmp_path):
    now = [NOW]
    value = stream(tmp_path, clock=lambda: now[0])
    value.ingest(message("LEVELONE_EQUITIES", quote(), quote(**{"3": 105})), received_at=NOW)
    assert picked(value)["status"] == "unavailable"
    now[0] += timedelta(seconds=1)
    value.ingest(message("LEVELONE_EQUITIES", quote(now[0])), received_at=now[0])
    assert picked(value)["status"] == "ok"


def test_delayed_future_clock_and_invalid_numeric_fields_are_unavailable():
    scope = {"AAPL": "AAPL"}
    assert normalize("LEVELONE_EQUITIES", quote(delayed=True), received_at=NOW, scope=scope) is None
    assert normalize("LEVELONE_EQUITIES", quote(**{"35": ms(NOW + timedelta(seconds=1))}), received_at=NOW, scope=scope) is None
    row = normalize("LEVELONE_EQUITIES", quote(**{"1": True, "2": float("inf")}), received_at=NOW, scope=scope)
    assert row["bid"] is row["ask"] is None


def test_completed_native_chart_only_and_invalid_ohlc_volume(tmp_path):
    start = NOW.replace(second=0) - timedelta(minutes=1)
    row = {"key": "AAPL", "2": 100, "3": 102, "4": 99, "5": 101, "6": 10, "7": ms(start)}
    value = stream(tmp_path, service="CHART_EQUITY")
    value.ingest(message("CHART_EQUITY", row), received_at=NOW)
    result = picked(value, "schwab_stream_bars")
    assert result["features"]["observed_bars"] == 1
    assert result["bar_end_at"] == (start + timedelta(minutes=1)).isoformat()
    assert normalize("CHART_EQUITY", row, received_at=start + timedelta(seconds=30), scope={"AAPL": "AAPL"}) is None
    for change in ({"6": 0}, {"4": 103}, {"2": -1}):
        assert normalize("CHART_EQUITY", row | change, received_at=NOW, scope={"AAPL": "AAPL"}) is None


def test_books_keep_service_scope_and_options_are_observed_subset(tmp_path):
    key = "AAPL  261023C00100000"
    value = stream(tmp_path, service="OPTIONS_BOOK", scope={key: "AAPL"})
    value._scope_counts = {"AAPL": {"requested": 1500, "submitted": 1}}
    row = {"key": key, "1": ms(NOW), "2": [{"0": 2, "1": 10}, {"0": 1.5, "1": 20}], "3": [{"0": 2.2, "1": 5}]}
    value.ingest(message("OPTIONS_BOOK", row), received_at=NOW)
    result = picked(value, "schwab_stream_options_book")
    assert result["features"]["bid_depth"] == 30
    assert result["features"]["contracts"] == 1
    assert result["features"]["requested_contracts"] == 1500
    assert result["features"]["acknowledged_contracts"] == 1
    assert "subset" in result["payload"]["numeric_scope"]


def test_futures_root_mapping_cannot_backdate_and_explicit_contract_is_native(tmp_path):
    value = stream(tmp_path, service="CHART_FUTURES", scope={"/ES": None}, futures=["/ES"])
    start = NOW - timedelta(minutes=1, seconds=2)
    chart = {"key": "/ES", "1": ms(start), "2": 100, "3": 102, "4": 99, "5": 101, "6": 5}
    value.ingest(message("CHART_FUTURES", chart), received_at=NOW)
    assert picked(value, "schwab_future_es_bars")["status"] == "unavailable"
    direct = stream(tmp_path / "direct", service="CHART_FUTURES", scope={"/ESZ26": None}, futures=["/ESZ26"])
    direct.ingest(message("CHART_FUTURES", chart | {"key": "/ESZ26"}), received_at=NOW)
    assert picked(direct, "schwab_future_es_bars")["status"] == "ok"


def test_late_futures_mapping_does_not_roll_back_and_equal_clock_conflict_is_ambiguous(tmp_path):
    value = stream(tmp_path, service="LEVELONE_FUTURES", scope={"/ES": None}, futures=["/ES"])
    def future(event, contract):
        return {"key": "/ES", "1": 100, "2": 102, "3": 101, "10": ms(event), "34": contract}
    value.ingest(message("LEVELONE_FUTURES", future(NOW, "/ESZ26")), received_at=NOW)
    value.ingest(message("LEVELONE_FUTURES", future(NOW - timedelta(seconds=2), "/ESU26")), received_at=NOW + timedelta(seconds=1))
    assert value._future_ids["/ES"][0] == "/ESZ26"
    value.ingest(message("LEVELONE_FUTURES", future(NOW, "/ESH27")), received_at=NOW)
    assert value._future_ids["/ES"][0] is None
    value._scopes["CHART_FUTURES"] = {"/ES": None}
    value._acks["CHART_FUTURES"] = {"status": "ok", "keys": ["/ES"]}
    chart = {"key": "/ES", "1": ms(NOW), "2": 100, "3": 102, "4": 99, "5": 101, "6": 5}
    value.ingest(message("CHART_FUTURES", chart), received_at=NOW + timedelta(minutes=1))
    assert not any(r["service"] == "CHART_FUTURES" for r in value._rows.values())
    value.ingest(message("LEVELONE_FUTURES", future(NOW + timedelta(seconds=1), "/ESH27")), received_at=NOW + timedelta(seconds=1))
    assert value._future_ids["/ES"][0] == "/ESH27"


def test_ack_mismatch_does_not_authenticate_and_secrets_are_not_written(tmp_path):
    value = stream(tmp_path)
    value._pending["1"] = ("ADMIN", "LOGIN", [])
    ack = {"response": [{"requestid": "1", "service": "LEVELONE_EQUITIES", "command": "SUBS", "content": {"code": 0, "msg": "LOGIN secret-should-not-persist"}}]}
    with pytest.raises(ValueError, match="scope mismatch"):
        value.ingest(ack, received_at=NOW)
    assert "1" in value._pending
    value.close()
    assert b"secret-should-not-persist" not in b"".join(p.read_bytes() for p in tmp_path.rglob("*.jsonl"))


def test_capacity_followup_never_upgrades_original_rejection(tmp_path):
    value = stream(tmp_path)
    value._pending["2"] = ("LEVELONE_EQUITIES", "SUBS", ["AAPL"])
    value._acks["LEVELONE_EQUITIES"] = {"status": "pending", "keys": [], "codes": []}
    def ack(code):
        return {"response": [{"requestid": "2", "service": "LEVELONE_EQUITIES", "command": "SUBS", "content": {"code": code}}]}
    value.ingest(ack(19), received_at=NOW)
    value.ingest(ack(0), received_at=NOW)
    assert value._acks["LEVELONE_EQUITIES"]["status"] == "capacity_blocked"
    assert value._acks["LEVELONE_EQUITIES"]["keys"] == []


def test_account_stream_is_never_persisted(tmp_path):
    value = stream(tmp_path)
    with pytest.raises(ValueError, match="account"):
        value.ingest(message("ACCT_ACTIVITY", {"secret_account": "private"}), received_at=NOW)
    assert not list(tmp_path.rglob("*.jsonl"))


def test_parse_failure_is_retained_after_removing_active_secrets(tmp_path):
    value = stream(tmp_path)
    value._sensitive.add("ACTIVE_TEST_TOKEN")
    value._socket = type("Socket", (), {"recv": lambda self: "broken ACTIVE_TEST_TOKEN {"})()
    with pytest.raises(ValueError, match="JSON"):
        value._receive()
    value._socket = None
    value.close()
    raw = b"".join(p.read_bytes() for p in tmp_path.rglob("*.jsonl"))
    assert b"parse_failure" in raw and b"ACTIVE_TEST_TOKEN" not in raw


class Session:
    def _headers(self):
        return {"Authorization": "Bearer TEST_ONLY_TOKEN"}


class Response:
    def __init__(self, payload):
        self.payload = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self.payload


def test_full_chain_response_is_saved_before_projection_and_marker_rejected(tmp_path):
    payload = {"symbol": "AAPL", "status": "SUCCESS", "is_synthetic": True,
               "extra_raw_field": "must survive", "callExpDateMap": {}, "putExpDateMap": {}}
    value = stream(tmp_path, http_get=lambda *a, **k: Response(payload))
    value._session = Session()
    assert value._refresh_one_chain() is False
    value.close()
    raw = b"".join(p.read_bytes() for p in tmp_path.rglob("*.jsonl"))
    assert b"must survive" in raw and "AAPL" not in value._chains


def test_scope_is_bounded_and_spreads_underlyings(tmp_path):
    value = SchwabMarketStream(tmp_path, ["AAPL", "NVDA"], max_option_contracts=3, clock=lambda: NOW)
    value._completed_chains = {"AAPL": ["a1", "a2", "a3"], "NVDA": ["n1", "n2", "n3"]}
    selected = value._option_scope()
    book = value._option_scope("OPTIONS_BOOK")
    assert len(selected) == 2 and len(book) == 1 and set(selected.values()) == {"AAPL", "NVDA"}
    assert sum(c["requested"] for c in value._scope_counts.values()) == 6
    assert sum(c["submitted"] for c in value._scope_counts.values()) == 3


def test_combined_service_budget_reserves_nonoption_memberships(tmp_path):
    symbols = ["AAPL", "AMZN", "GOOG", "MU", "NVDA", "SNDK", "COST", "CROX", "PATH", "TWST", "IONQ"]
    value = SchwabMarketStream(tmp_path, symbols, ["/ES", "/NQ", "/RTY", "/CL", "/GC"], clock=lambda: NOW)
    value._completed_chains = {symbol: [f"{symbol}{i}" for i in range(300)] for symbol in symbols}
    quote_scope = value._option_scope()
    book_scope = value._option_scope("OPTIONS_BOOK")
    assert len(quote_scope) == len(book_scope) == 223
    assert len(quote_scope) + len(book_scope) + 4 * len(symbols) + 2 * len(value.futures) == 500
    assert set(quote_scope.values()) == set(book_scope.values()) == set(symbols)
    assert sum(c["requested"] for c in value._scope_counts.values()) == 3300


def test_chain_timeout_completes_round_without_partial_resubscription(tmp_path):
    now = [NOW]
    calls, subscriptions = [], []
    def http(url, **kwargs):
        symbol = kwargs["params"]["symbol"]
        calls.append(symbol)
        assert kwargs["timeout"] == 10
        if symbol == "NVDA":
            raise TimeoutError("bounded test timeout")
        key = symbol + " 261016C00100000"
        return Response({"symbol": symbol, "status": "SUCCESS", "callExpDateMap": {"date": {"100": [{"symbol": key}]}}, "putExpDateMap": {}})
    value = SchwabMarketStream(tmp_path, ["AAPL", "NVDA", "GOOG"], clock=lambda: now[0], http_get=http)
    value._session = Session()
    value._subscribe = lambda service, scope: subscriptions.append((service, dict(scope))) or value._scopes.update({service: scope})
    generation = -1
    for expected in ["AAPL", "NVDA", "GOOG"]:
        value._refresh_one_chain()
        assert calls[-1] == expected
        generation = value._sync_option_subscriptions(generation)
        if expected != "GOOG":
            assert not subscriptions and value._chain_generation == 0
    assert len(subscriptions) == 2 and generation == 1
    assert value._chain_attempts["NVDA"]["status"] == "TimeoutError"
    assert value._chain_attempts["GOOG"]["status"] == "ok"
    assert value._refresh_one_chain() is False and len(calls) == 3
    assert value._sync_option_subscriptions(generation) == generation and len(subscriptions) == 2
    now[0] += timedelta(minutes=15)
    value._refresh_one_chain()
    assert value._sync_option_subscriptions(generation) == generation
    value._refresh_one_chain()
    value._refresh_one_chain()
    assert value._chain_generation == 2 and calls == ["AAPL", "NVDA", "GOOG"] * 2
    # Unchanged complete scopes do not need a new SUBS even after a refresh.
    assert value._sync_option_subscriptions(generation) == 2 and len(subscriptions) == 2
    value.close()


def test_capacity_fallback_is_bounded_retains_scope_and_rotates_underlyings(tmp_path):
    now = [NOW]
    value = SchwabMarketStream(tmp_path, ["AAPL", "NVDA", "GOOG"], clock=lambda: now[0])
    value._completed_chains = {symbol: [f"{symbol}{i}" for i in range(100)] for symbol in value.symbols}
    seen = []
    def subscribe(service, scope):
        seen.append(dict(scope))
        value._scopes[service] = scope
        ok = len(scope) == 1
        value._acks[service] = {"status": "ok" if ok else "capacity_blocked", "codes": [0 if ok else 19], "keys": list(scope) if ok else []}
    value._subscribe = subscribe
    value._subscribe_options("OPTIONS_BOOK", value._option_scope("OPTIONS_BOOK"))
    assert [len(s) for s in seen] == [244, 3, 1]
    assert value._capacity_limits["OPTIONS_BOOK"] == 1
    assert sum(c["requested"] for c in value._scope_counts.values()) == 300
    assert sum(c["submitted_by_service"]["OPTIONS_BOOK"] for c in value._scope_counts.values()) == 1
    first_underlying = next(iter(seen[-1].values()))
    attempts = value._subscription_attempts["OPTIONS_BOOK"]
    assert [a["codes"] for a in attempts] == [[19], [19], [0]]
    now[0] += timedelta(minutes=15)
    next_scope = value._option_scope("OPTIONS_BOOK")
    assert len(next_scope) == 1 and next(iter(next_scope.values())) != first_underlying
    value._subscribe_options("OPTIONS_BOOK", next_scope)
    assert len(seen) == 4  # No repeated full rejected scope on a later round.
    value.close()
    saved = [json.loads(line) for p in tmp_path.rglob("*.jsonl") for line in p.read_text().splitlines()]
    assert [len(item["payload"]["native_scope"]) for item in saved] == [244, 3, 1, 1]


def test_capacity_reduction_stops_on_other_denial_and_on_manual_stop(tmp_path):
    for stopped, status, code in [(False, "denied", 3), (True, "capacity_blocked", 19)]:
        value = SchwabMarketStream(tmp_path / str(stopped), ["AAPL"])
        seen = []
        def subscribe(service, scope):
            seen.append(scope)
            value._acks[service] = {"status": status, "codes": [code], "keys": []}
            if stopped:
                value._stop.set()
        value._subscribe = subscribe
        value._subscribe_options("OPTIONS_BOOK", {"a": "AAPL", "b": "AAPL"})
        assert len(seen) == 1
        value.close()


def test_fallback_book_features_disclose_observed_subset_and_unserved_symbols(tmp_path):
    value = SchwabMarketStream(tmp_path, ["AAPL", "NVDA"], clock=lambda: NOW)
    value._status = "connected"
    value._completed_chains = {"AAPL": ["a1", "a2"], "NVDA": ["n1", "n2"]}
    def subscribe(service, scope):
        value._scopes[service] = scope
        ok = len(scope) == 1
        value._acks[service] = {"status": "ok" if ok else "capacity_blocked", "codes": [0 if ok else 19],
                               "keys": list(scope) if ok else [], "received_at": NOW.isoformat()}
    value._subscribe = subscribe
    value._subscribe_options("OPTIONS_BOOK", value._option_scope("OPTIONS_BOOK"))
    key, symbol = next(iter(value._scopes["OPTIONS_BOOK"].items()))
    value.ingest(message("OPTIONS_BOOK", {"key": key, "1": ms(NOW), "2": [{"0": 2, "1": 10}], "3": [{"0": 3, "1": 5}]}), received_at=NOW)
    records = [row for row in value.snapshot() if row["source"] == "schwab_stream_options_book"]
    observed = next(row for row in records if row["symbol"] == symbol)
    unavailable = next(row for row in records if row["symbol"] != symbol)
    assert observed["status"] == "ok" and observed["features"]["contracts"] == 1
    assert observed["features"]["requested_contracts"] == 2 and observed["features"]["acknowledged_contracts"] == 1
    assert unavailable["status"] == "unavailable" and unavailable["features"] == {}
    assert unavailable["payload"]["option_scope"]["submitted_by_service"]["OPTIONS_BOOK"] == 0
    assert len(observed["payload"]["option_subscription_attempts"]) == 3
    value.close()


class FakeSocket:
    def __init__(self):
        self.queue = queue.Queue()
        self.closed = False
        self.sent = []
    def send(self, raw):
        request = json.loads(raw)["requests"][0]
        self.sent.append(request)
        self.queue.put(json.dumps({"response": [{"requestid": request["requestid"], "service": request["service"], "command": request["command"], "content": {"code": 0}}]}))
    def recv(self):
        if self.closed:
            raise ConnectionError("closed")
        try:
            return self.queue.get(timeout=.01)
        except queue.Empty:
            raise TimeoutError()
    def close(self):
        self.closed = True


def test_start_is_asynchronous_single_owner_and_manual_close_stays_stopped(tmp_path):
    boot_wait = threading.Event()
    entered = threading.Event()
    sockets = []
    def http(url, **kwargs):
        if url.endswith("userPreference"):
            entered.set()
            boot_wait.wait(2)
            return Response({"streamerInfo": [{"schwabClientCorrelId": "cid", "schwabClientCustomerId": "customer", "schwabClientChannel": "channel", "schwabClientFunctionId": "function", "streamerSocketUrl": "wss://streamer.schwab.com/ws"}]})
        return Response({"symbol": "AAPL", "status": "SUCCESS", "callExpDateMap": {}, "putExpDateMap": {}})
    def factory(*args, **kwargs):
        socket = FakeSocket()
        sockets.append(socket)
        return socket
    value = SchwabMarketStream(tmp_path, ["AAPL"], session_factory=Session, http_get=http, socket_factory=factory, reconnect_seconds=.01)
    assert value.start()["owner"] == "new"
    assert entered.wait(1)
    assert value.start()["owner"] == "existing"
    assert sockets == []  # start returned while bootstrap remained blocked.
    boot_wait.set()
    deadline = time.monotonic() + 2
    while value._status != "connected" and time.monotonic() < deadline:
        time.sleep(.01)
    assert value._status == "connected" and len(sockets) == 1
    value.close()
    assert sockets[0].closed
    assert value.start()["status"] == "closed"
    assert not value._thread.is_alive()
    assert not value._chain_thread.is_alive()
    assert set(ADDITIONAL_SOURCE_FIELDS).issuperset({"schwab_stream_nyse_book", "schwab_future_rty_bars"})


def test_stop_while_ack_pending_prevents_next_subscription(tmp_path):
    subscribed = threading.Event()
    class WaitingSocket(FakeSocket):
        def send(self, raw):
            request = json.loads(raw)["requests"][0]
            if request["service"] == "ADMIN":
                return super().send(raw)
            self.sent.append(request)
            subscribed.set()
    socket = WaitingSocket()
    payload = {"streamerInfo": [{"schwabClientCorrelId": "cid", "schwabClientCustomerId": "customer", "schwabClientChannel": "channel", "schwabClientFunctionId": "function", "streamerSocketUrl": "wss://streamer.schwab.com/ws"}]}
    value = SchwabMarketStream(tmp_path, ["AAPL"], session_factory=Session,
        http_get=lambda *a, **k: Response(payload), socket_factory=lambda *a, **k: socket)
    value.start()
    assert subscribed.wait(2)
    value.close()
    assert [r["service"] for r in socket.sent] == ["ADMIN", "LEVELONE_EQUITIES"]
    assert not value._thread.is_alive()


def test_reconnect_records_gap_and_never_replays_old_prices(tmp_path):
    sockets = []
    class DisconnectOnce(FakeSocket):
        def recv(self):
            if self.queue.empty() and any(r["service"] == "NASDAQ_BOOK" for r in self.sent):
                raise ConnectionError("deliberate test disconnect")
            return super().recv()
    def factory(*args, **kwargs):
        sock = DisconnectOnce() if not sockets else FakeSocket()
        sockets.append(sock)
        return sock
    def http(url, **kwargs):
        if url.endswith("userPreference"):
            return Response({"streamerInfo": [{"schwabClientCorrelId": "cid", "schwabClientCustomerId": "customer", "schwabClientChannel": "channel", "schwabClientFunctionId": "function", "streamerSocketUrl": "wss://streamer.schwab.com/ws"}]})
        return Response({"symbol": "AAPL", "status": "SUCCESS", "callExpDateMap": {}, "putExpDateMap": {}})
    value = SchwabMarketStream(tmp_path, ["AAPL"], session_factory=Session, http_get=http,
                               socket_factory=factory, reconnect_seconds=.01)
    value.start()
    deadline = time.monotonic() + 2
    while (len(sockets) < 2 or value._status != "connected") and time.monotonic() < deadline:
        time.sleep(.01)
    value.close()
    assert len(sockets) == 2
    assert all(s.closed for s in sockets)
    assert len(value._gaps) == 2
    assert all(g["replayed"] is False for g in value._gaps)
    assert picked(value)["status"] == "unavailable"


def test_snapshot_ack_evidence_is_frozen(tmp_path):
    value = stream(tmp_path)
    result = picked(value)
    value._acks["LEVELONE_EQUITIES"]["keys"].append("NVDA")
    assert result["payload"]["acknowledgement"]["keys"] == ["AAPL"]
