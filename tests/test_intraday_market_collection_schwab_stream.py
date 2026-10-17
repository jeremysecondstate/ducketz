from datetime import datetime, timezone, timedelta
import json
import hashlib

import pytest

from ml.intraday.market_collection import CollectionFailure, canonical
from ml.intraday.market_collection_schwab_stream import capture, normalize, stream_requests, SchwabStreamCollector

NOW = datetime.now(timezone.utc)
MILLIS = int(NOW.timestamp()*1000)
OPTION = "SDGR  261120C00030000"


class Response:
    def raise_for_status(self):
        pass

    def json(self):
        return {"accounts": [{"private": "MUST_NOT_PERSIST"}], "streamerInfo": [{
            "schwabClientCorrelId": "correlation", "schwabClientCustomerId": "customer",
            "schwabClientChannel": "channel", "schwabClientFunctionId": "function",
            "streamerSocketUrl": "wss://streamer-api.schwab.com/ws"}]}


class Session:
    def _headers(self):
        return {"Authorization": "Bearer NEVER_PERSIST"}


class Socket:
    def __init__(self, *, scenario="normal"):
        self.sent, self.pending, self.scenario, self.closed = [], [], scenario, False

    def send(self, message):
        request = json.loads(message)["requests"][0]
        self.sent.append(request)
        service = request["service"]
        code = 0 if self.scenario != "denied" or service == "ADMIN" else 1
        response = {"service": service, "command": request["command"],
            "requestid": request["requestid"], "content": {"code": code,
            "msg": "not authorized NEVER_PERSIST" if code else "OK"}}
        if self.scenario == "mismatch":
            response["requestid"] = "999"
        if self.scenario == "boolean_ack":
            response["content"]["code"] = False
        if service != "ADMIN":
            if self.scenario == "account":
                self.pending.append({"data": [{"service": "ACCT_ACTIVITY", "content": [{"key": "PRIVATE_ACCOUNT_NEVER_WRITE"}]}]})
            if self.scenario == "futures":
                content = ([{"key": "/ES", "1": MILLIS-1000, "2": 1, "3": 2, "4": .5, "5": 1.5, "6": 10}]
                    if service == "CHART_FUTURES" else [{"key": "/ES", "10": MILLIS, "34": "/ESZ26", "1": 10, "2": 11}])
                self.pending.append({"data": [{"service": service, "timestamp": MILLIS, "content": content}]})
            else:
                self.pending.append({"data": [{"service": service, "timestamp": MILLIS,
                    "content": [{"key": "SDGR", "1": 10, "2": 10.1, "34": MILLIS,
                        "4": 100, "5": 200}]}]})
        self.pending.append({"response": [response]})
        if self.scenario == "duplicate_ack":
            self.pending.append({"response": [response]})

    def recv(self):
        if self.pending:
            return json.dumps(self.pending.pop(0))
        raise TimeoutError()

    def close(self):
        self.closed = True


def run_capture(tmp_path, *, scenario="normal", subscriptions=None, capture_seconds=.1, timeout_seconds=2):
    socket = Socket(scenario=scenario)
    calls = []
    clock_value = [0]
    def clock():
        clock_value[0] += .01
        return clock_value[0]
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()
    result = capture(Session(), subscriptions or {"LEVELONE_EQUITIES": {"SDGR": "SDGR"}},
        tmp_path/"stream.jsonl", capture_seconds=capture_seconds, timeout_seconds=timeout_seconds,
        http_get=get, socket_factory=lambda *a, **kw: socket, monotonic=clock)
    return result, socket, calls


def test_market_only_bootstrap_validated_acks_native_provenance_and_no_secret_write(tmp_path):
    (rows, evidence), socket, calls = run_capture(tmp_path)
    assert len(calls) == 1 and calls[0][0].endswith("/trader/v1/userPreference")
    assert {r["service"] for r in socket.sent} == {"ADMIN", "LEVELONE_EQUITIES"}
    assert rows["LEVELONE_EQUITIES"][0]["bid"] == 10
    assert rows["LEVELONE_EQUITIES"][0]["quote_timestamp"]
    assert evidence["mode"] == "current_stream_snapshot" and evidence["completed"]
    assert evidence["service_results"]["LEVELONE_EQUITIES"]["acknowledged_symbols"] == 1
    native = (tmp_path/"stream.jsonl").read_bytes()
    assert evidence["raw_sha256"] == hashlib.sha256(native).hexdigest()
    assert b"NEVER_PERSIST" not in native
    assert "MUST_NOT_PERSIST" not in (tmp_path/"stream.bootstrap.json").read_text()
    assert socket.closed


def test_entitlement_failure_is_explicit_and_not_returned_as_success(tmp_path):
    (rows, evidence), _, _ = run_capture(tmp_path, scenario="denied")
    assert evidence["service_results"]["LEVELONE_EQUITIES"]["status"] == "NOT_AUTHORIZED"
    assert "NEVER_PERSIST" not in json.dumps(evidence)
    assert b"NEVER_PERSIST" not in (tmp_path/"stream.jsonl").read_bytes()


def test_unrequested_account_payload_is_rejected_before_native_write(tmp_path):
    with pytest.raises(CollectionFailure):
        run_capture(tmp_path, scenario="account")
    native = (tmp_path/"stream.jsonl").read_bytes()
    assert b"ACCT_ACTIVITY" not in native and b"PRIVATE_ACCOUNT_NEVER_WRITE" not in native


def test_full_post_ack_capture_window_is_required(tmp_path):
    with pytest.raises(CollectionFailure) as caught:
        run_capture(tmp_path, capture_seconds=.09, timeout_seconds=.1)
    assert caught.value.collection_status == "UNAVAILABLE"


def test_futures_identity_is_not_backfilled_into_earlier_chart(tmp_path):
    (rows, evidence), _, _ = run_capture(tmp_path, scenario="futures", subscriptions={
        "CHART_FUTURES": {"/ES": None}, "LEVELONE_FUTURES": {"/ES": None}})
    assert rows["CHART_FUTURES"][0]["root_symbol"] is None
    assert rows["LEVELONE_FUTURES"][0]["symbol"] == "/ESZ26"
    assert rows["LEVELONE_FUTURES"][0]["sequence"] == 1
    with pytest.raises(ValueError, match="root"):
        normalize("LEVELONE_FUTURES", {"key": "/ES", "10": MILLIS, "34": "/NQZ26"},
            receipt_at=NOW.isoformat(), scope={"/ES": None})


def test_ack_mismatch_and_account_service_fail_closed(tmp_path):
    with pytest.raises(CollectionFailure, match="Provider request failed"):
        run_capture(tmp_path, scenario="mismatch")
    with pytest.raises(ValueError, match="allowlisted"):
        run_capture(tmp_path, subscriptions={"ACCT_ACTIVITY": {"account": None}})


@pytest.mark.parametrize("scenario", ["boolean_ack", "duplicate_ack"])
def test_result_codes_and_ack_ids_are_strict_and_single_use(tmp_path, scenario):
    with pytest.raises(CollectionFailure):
        run_capture(tmp_path, scenario=scenario)


def test_capacity_followup_does_not_abort_other_services_or_satisfy_their_ack(tmp_path):
    class CapacitySocket(Socket):
        def send(self, message):
            req = json.loads(message)["requests"][0]
            self.sent.append(req)
            code = 19 if req["service"] == "LEVELONE_OPTIONS" and req["command"] == "ADD" else 0
            ack = {"service": req["service"], "command": req["command"], "requestid": req["requestid"],
                "content": {"code": code, "msg": "NEVER_PERSIST"}}
            self.pending.append({"response": [ack]})
            if code:
                self.pending.append({"response": [{**ack, "content": {"code": 24, "msg": "NEVER_PERSIST"}}]})
                self.pending.append({"response": [{**ack, "content": {"code": 0, "msg": "NEVER_PERSIST"}}]})
    socket = CapacitySocket()
    tick = [0]
    def clock():
        tick[0] += .001
        return tick[0]
    rows, evidence = capture(Session(), {"LEVELONE_OPTIONS": {f"OPTION{i}": "SDGR" for i in range(1001)},
        "LEVELONE_EQUITIES": {"SDGR": "SDGR"}}, tmp_path/"capacity.jsonl", capture_seconds=.1, timeout_seconds=2,
        http_get=lambda *a, **k: Response(), socket_factory=lambda *a, **k: socket, monotonic=clock)
    assert evidence["completed"]
    assert evidence["service_results"]["LEVELONE_OPTIONS"]["status"] == "PARTIAL"
    assert evidence["service_results"]["LEVELONE_OPTIONS"]["acknowledged_symbols"] == 250
    assert evidence["service_results"]["LEVELONE_OPTIONS"]["requested_symbols"] == 1001
    assert evidence["service_results"]["LEVELONE_OPTIONS"]["submitted_symbols"] == 500
    assert len(evidence["service_results"]["LEVELONE_OPTIONS"]["submitted_scope"]) == 500
    assert evidence["service_results"]["LEVELONE_EQUITIES"]["status"] == "COMPLETE"
    assert evidence["followup_controls"][0]["code"] == 24
    assert evidence["followup_controls"][1]["code"] == 0
    assert b"NEVER_PERSIST" not in (tmp_path/"capacity.jsonl").read_bytes()


def test_option_capacity_order_spreads_underlyings_and_rotates_quarters():
    from ml.intraday.market_collection_schwab_stream import _scope_keys
    scopes = {f"{symbol}{i:04d}": symbol for symbol in ("A", "B") for i in range(300)}
    first = _scope_keys("LEVELONE_OPTIONS", scopes, "2026-10-17T12:00:00Z")
    second = _scope_keys("LEVELONE_OPTIONS", scopes, "2026-10-17T12:15:00Z")
    assert set(first) == set(second) == set(scopes)
    assert first != second and {scopes[k] for k in first[:2]} == {"A", "B"}


def test_delta_prices_are_never_carried_forward_and_scope_is_exact():
    row = normalize("LEVELONE_EQUITIES", {"key": "SDGR", "34": MILLIS, "2": 10.2},
        receipt_at=NOW.isoformat(), scope={"SDGR": "SDGR"})
    assert row["bid"] is None and row["ask"] == 10.2
    with pytest.raises(ValueError, match="unrequested"):
        normalize("LEVELONE_EQUITIES", {"key": "OTHER"}, receipt_at=NOW.isoformat(), scope={"SDGR": "SDGR"})


def test_actual_book_levels_and_option_greek_sentinels():
    scope = {OPTION: "SDGR"}
    book = normalize("OPTIONS_BOOK", {"key": OPTION, "1": MILLIS,
        "2": [{"0": 1.1, "1": 20}, {"0": 1.2, "1": 30}], "3": [{"0": 1.4, "1": 15}]},
        receipt_at=NOW.isoformat(), scope=scope)
    assert book["bid"] == 1.2 and book["bid_size"] == 30 and book["bid_depth_volume"] == 50
    assert book["contract_symbol"] == OPTION and book["underlying_symbol"] == "SDGR"
    option = normalize("LEVELONE_OPTIONS", {"key": OPTION, "38": MILLIS, "39": MILLIS-100,
        "10": 45, "28": -999, "29": .2, "8": 1000}, receipt_at=NOW.isoformat(), scope=scope)
    assert option["delta"] is None and option["gamma"] == .2 and option["implied_volatility"] == .45
    assert "volume" not in option and option["day_volume"] == 1000


def test_native_chart_fields_and_active_futures_identity():
    row = normalize("CHART_FUTURES", {"key": "/ES", "1": MILLIS, "2": 1, "3": 3,
        "4": .5, "5": 2, "6": 10}, receipt_at=NOW.isoformat(), scope={"/ES": None})
    assert [row[k] for k in ("open", "high", "low", "close", "volume")] == [1, 3, .5, 2, 10]
    assert row["bar_duration_seconds"] == 60
    row = normalize("LEVELONE_FUTURES", {"key": "/ES", "10": MILLIS, "34": "/ESZ26"},
        receipt_at=NOW.isoformat(), scope={"/ES": None})
    assert row["symbol"] == "/ESZ26" and row["root_symbol"] == "ES"


def test_one_native_capture_shared_by_all_scopes_and_survives_restart(tmp_path):
    requests = tuple(r for r in stream_requests(["SDGR", "RR"]) if r.schema == "LEVELONE_EQUITIES")
    calls = []
    def fake(session, scopes, path, **kwargs):
        calls.append(scopes)
        return {"LEVELONE_EQUITIES": [{"symbol": symbol, "event_at": NOW.isoformat()} for symbol in ["SDGR", "RR"]]}, {
            "service_results": {"LEVELONE_EQUITIES": {"status": "COMPLETE", "reason": "fixture"}}}
    collector = SchwabStreamCollector(tmp_path, requests, capture_fn=fake)
    a = collector(Session(), requests[0], NOW-timedelta(minutes=15), NOW, 100)
    b = collector(Session(), requests[1], NOW-timedelta(minutes=15), NOW, 100)
    restarted = SchwabStreamCollector(tmp_path, requests, capture_fn=lambda *a, **k: pytest.fail("must reuse receipt"))
    c = restarted(Session(), requests[0], NOW-timedelta(minutes=15), NOW, 100)
    assert len(calls) == 1 and a.records == c.records and b.records[0]["symbol"] == "RR"


def test_bad_option_scope_does_not_block_equity_and_stream_failures_back_off(tmp_path):
    requests = stream_requests(["SDGR"])
    observed = []
    def fake(session, scopes, path, **kwargs):
        observed.append(scopes)
        return {name: [] for name in scopes}, {"service_results": {
            name: {"status": "COMPLETE", "reason": "fixture"} for name in scopes}}
    def empty_chain(*args):
        return {}
    collector = SchwabStreamCollector(tmp_path, requests, capture_fn=fake, chain_fetch=empty_chain)
    equity = next(r for r in requests if r.schema == "LEVELONE_EQUITIES")
    options = next(r for r in requests if r.schema == "LEVELONE_OPTIONS")
    assert collector(Session(), equity, NOW-timedelta(minutes=15), NOW, 100).status == "COMPLETE"
    assert collector(Session(), options, NOW-timedelta(minutes=15), NOW, 100).status == "UNAVAILABLE"
    assert len(observed) == 1 and "LEVELONE_OPTIONS" not in observed[0]
    failures = []
    def failed(*args, **kwargs):
        failures.append(1)
        raise TimeoutError()
    other = SchwabStreamCollector(tmp_path/"failed", [equity], capture_fn=failed)
    for _ in range(2):
        with pytest.raises(CollectionFailure):
            other(Session(), equity, NOW-timedelta(minutes=15), NOW, 100)
    assert len(failures) == 1
