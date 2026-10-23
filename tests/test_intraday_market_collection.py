from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import threading

import pandas as pd
import pytest

from ml.intraday.market_collection import (
    CollectionResult, QuarterHourMarketCollector, SourceRequest, canonical,
    collection_window_open, instant, load_snapshots, snapshot_digest,
)
from ml.intraday.market_collection_adapters import DatabentoAdapter, FmpAdapter, SchwabAdapter, default_requests


pytestmark = pytest.mark.usefixtures("offline_databento_sdk")


START = datetime(2026, 10, 16, 19, 45, tzinfo=timezone.utc)
END = START + timedelta(minutes=15)
NOW = END + timedelta(seconds=10)


def source(symbol="SDGR", **kwargs):
    values = dict(provider="fmp", source_kind="stock_quote", symbol=symbol,
                  dataset="stable", schema="quote", endpoint="quote", params={"symbol": symbol})
    values.update(kwargs)
    return SourceRequest(**values)


def quote(symbol="SDGR", **kwargs):
    return {"symbol": symbol, "event_at": END.isoformat(), "bid": 10, "ask": 11, **kwargs}


def collector(tmp_path, adapter, *, requests=None, clock=None, **kwargs):
    return QuarterHourMarketCollector(tmp_path, symbols=["SDGR", "GLOB"],
        requests=requests or [source()], adapters={"fmp": adapter},
        clock=clock or (lambda: NOW), **kwargs)


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_queue_publication_retries_same_receipt_without_provider_replay(tmp_path, monkeypatch, winerror):
    import ml.intraday.market_collection as module
    calls, attempts, sleeps = [], [], []
    col = collector(tmp_path, lambda *args: calls.append(args) or CollectionResult([quote()]))
    col.enqueue(START)
    previous = col.state_path.read_bytes()
    replace_path = Path.replace

    def transient(source_path, destination):
        assert destination == col.state_path
        attempts.append(source_path)
        assert len(json.loads(source_path.read_bytes())["tasks"]) == 1
        if len(attempts) <= 2:
            assert destination.read_bytes() == previous
            exc = PermissionError("fixture reader holds destination")
            exc.winerror = winerror
            raise exc
        return replace_path(source_path, destination)

    monkeypatch.setattr(Path, "replace", transient)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    result = col.step()
    assert len(result) == len(calls) == 1
    assert result[0]["status"] == "COMPLETE"
    saved = next(iter(json.loads(col.state_path.read_bytes())["tasks"].values()))
    assert saved["attempts"] == 1 and len(saved["snapshots"]) == 1
    assert len(attempts) == 3 and len(set(attempts)) == 1
    assert sleeps == [0.05, 0.1]
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("winerror,expected_attempts", [(5, 5), (32, 5), (33, 5), (None, 1), (2, 1)])
def test_queue_publication_failure_preserves_destination_and_bounded_retry(
        tmp_path, monkeypatch, winerror, expected_attempts):
    import ml.intraday.market_collection as module
    target = tmp_path / "queue.json"
    target.write_bytes(b'{"generation":"old"}')
    attempts, sleeps = [], []
    error = PermissionError("fixture access denied")
    error.winerror = winerror

    def denied(source_path, destination):
        attempts.append(source_path)
        assert destination.read_bytes() == b'{"generation":"old"}'
        assert json.loads(source_path.read_bytes()) == {"generation": "new"}
        raise error

    monkeypatch.setattr(Path, "replace", denied)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    with pytest.raises(PermissionError) as caught:
        module._atomic(target, {"generation": "new"})
    assert caught.value is error
    assert len(attempts) == expected_attempts and len(set(attempts)) == 1
    assert sum(sleeps) <= 0.75
    assert target.read_bytes() == b'{"generation":"old"}'
    assert not list(tmp_path.glob("*.tmp"))


def test_queue_cleanup_failure_keeps_original_publication_error(tmp_path, monkeypatch):
    import ml.intraday.market_collection as module
    target = tmp_path / "queue.json"
    target.write_bytes(b'{"generation":"old"}')
    original = PermissionError("fixture permanent publication denial")
    cleanup = PermissionError("fixture cleanup denial")

    def denied(*args, **kwargs):
        raise original

    def cleanup_denied(*args, **kwargs):
        raise cleanup

    monkeypatch.setattr(Path, "replace", denied)
    monkeypatch.setattr(Path, "unlink", cleanup_denied)
    with pytest.raises(PermissionError) as caught:
        module._atomic(target, {"generation": "new"})
    assert caught.value is original
    assert target.read_bytes() == b'{"generation":"old"}'
    pending = list(tmp_path.glob("*.tmp"))
    assert len(pending) == 1 and json.loads(pending[0].read_bytes()) == {"generation": "new"}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file sharing behavior")
def test_queue_publication_recovers_after_native_windows_reader_closes(tmp_path):
    import ctypes
    from ctypes import wintypes
    from ml.intraday.market_collection import _atomic
    target = tmp_path / "queue.json"
    target.write_bytes(b'{"generation":"old"}')
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateFileW(str(target), 0x80000000, 1, None, 3, 0x80, None)
    assert handle != wintypes.HANDLE(-1).value
    release = threading.Timer(0.12, kernel32.CloseHandle, args=(handle,))
    release.start()
    try:
        _atomic(target, {"generation": "new"})
    finally:
        release.join()
    assert json.loads(target.read_bytes()) == {"generation": "new"}
    assert not list(tmp_path.glob("*.tmp"))


def test_durable_resume_exact_duplicates_and_conflicts(tmp_path):
    calls = []
    def adapter(*args):
        calls.append(1)
        return CollectionResult([quote(), quote(), quote(bid=9)])
    first = collector(tmp_path, adapter)
    assert first.enqueue(START) == 1
    receipt = first.step()[0]
    assert receipt["record_count"] == 2
    assert receipt["exact_duplicates_removed"] == 1
    assert receipt["snapshot_sha256"] == snapshot_digest(receipt)
    second = collector(tmp_path, adapter)
    assert second.enqueue(START) == 0
    assert second.step() == []
    assert len(calls) == 1


def test_orphan_receipt_recovers_without_repeat_provider_call(tmp_path):
    count = []
    col = collector(tmp_path, lambda *args: (count.append(1) or CollectionResult([quote()])))
    col.enqueue(START)
    first = col.step()[0]
    state = json.loads(col.state_path.read_text())
    task = next(iter(state["tasks"].values()))
    task.update(status="QUEUED", attempts=0, snapshots=[], due_at=NOW.isoformat())
    col.state_path.write_bytes(canonical(state))
    assert col.step()[0]["snapshot_id"] == first["snapshot_id"]
    assert len(count) == 1


def test_error_backoff_is_sanitized_and_retry_keeps_prior_attempt(tmp_path):
    clock = [NOW]
    calls = []
    def adapter(*args):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("timeout https://example.test?apikey=PRIVATE_SECRET")
        return CollectionResult([quote()])
    col = collector(tmp_path, adapter, clock=lambda: clock[0])
    col.enqueue(START)
    failed = col.step()[0]
    assert failed["status"] == "ERROR"
    assert col.step() == []
    clock[0] += timedelta(seconds=31)
    assert col.step()[0]["status"] == "COMPLETE"
    assert len(list((tmp_path / "snapshots").rglob("*.json"))) == 2
    assert all(b"PRIVATE_SECRET" not in p.read_bytes() for p in tmp_path.rglob("*.json"))


def test_entitlement_denial_blocks_next_quarter_calls(tmp_path):
    count = []
    def adapter(*args):
        count.append(1)
        raise RuntimeError("not entitled to this schema")
    clock = [NOW]
    col = collector(tmp_path, adapter, clock=lambda: clock[0])
    col.enqueue(START)
    assert col.step()[0]["status"] == "NOT_AUTHORIZED"
    clock[0] += timedelta(minutes=15)
    col.enqueue(END)
    assert col.step()[0]["status"] == "NOT_AUTHORIZED"
    assert len(count) == 1


def test_late_receipt_and_future_row_unavailable_before_actual_clock(tmp_path):
    col = collector(tmp_path, lambda *args: CollectionResult([quote(event_at=(NOW + timedelta(minutes=1)).isoformat())]))
    col.enqueue(START)
    receipt = col.step()[0]
    assert load_snapshots(tmp_path, symbol="SDGR", cutoff=END) == ()
    observed = load_snapshots(tmp_path, symbol="SDGR", cutoff=NOW)
    assert len(observed) == 1  # response really arrived; invalid event stays unavailable
    from ml.intraday.market_context import build_market_context
    assert build_market_context(observed, symbol="SDGR", cutoff=NOW).features["ctx_stock_quote_source_available"] == 0
    assert len(load_snapshots(tmp_path, symbol="SDGR", cutoff=NOW + timedelta(minutes=1))) == 1
    assert receipt["available_at"] == receipt["receipt_at"]


def test_latest_error_remains_visible_to_feature_missingness(tmp_path):
    col = collector(tmp_path, lambda *args: CollectionResult([], "PARTIAL", "cap reached"))
    col.enqueue(START)
    col.step()
    assert load_snapshots(tmp_path, symbol="SDGR", cutoff=NOW)[0]["status"] == "PARTIAL"


def test_snapshot_checksum_tampering_fails_closed(tmp_path):
    col = collector(tmp_path, lambda *args: CollectionResult([quote()]))
    col.enqueue(START)
    col.step()
    path = next((tmp_path / "snapshots").rglob("*.json"))
    changed = json.loads(path.read_text())
    changed["records"][0]["bid"] = 99
    path.write_bytes(canonical(changed))
    with pytest.raises(ValueError, match="Invalid immutable"):
        load_snapshots(tmp_path, symbol="SDGR", cutoff=NOW)


def test_record_cap_partial_and_empty_is_not_marketwide_absence(tmp_path):
    col = collector(tmp_path, lambda *args: CollectionResult([quote(), quote(bid=9)]), max_records=1)
    col.enqueue(START)
    receipt = col.step()[0]
    assert receipt["status"] == "PARTIAL" and receipt["record_count"] == 1
    assert "no claim" in receipt["coverage_claim"]


def test_fresh_windows_not_starved_and_old_windows_expire(tmp_path):
    requests = [source(schema=f"quote-{i}") for i in range(4)]
    col = collector(tmp_path, lambda *args: CollectionResult([quote()]), requests=requests)
    col.enqueue(START - timedelta(days=3))
    col.enqueue(START)
    results = col.step(max_requests=4)
    assert sum(r["quarter_start"] == START.isoformat() for r in results) == 3
    assert [r["status"] for r in results].count("EXPIRED") == 1


def test_concurrent_collectors_do_not_duplicate_one_source_window(tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def adapter(*args):
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return CollectionResult([quote()])
    a, b = collector(tmp_path, adapter), collector(tmp_path, adapter)
    a.enqueue(START)
    thread = threading.Thread(target=a.step)
    thread.start()
    assert entered.wait(2)
    assert b.step() == []
    release.set()
    thread.join(2)
    assert not thread.is_alive() and len(calls) == 1


@pytest.mark.parametrize("bad", [START + timedelta(seconds=1), END, START.replace(tzinfo=None)])
def test_rejects_unaligned_future_or_naive_windows(tmp_path, bad):
    col = collector(tmp_path, lambda *args: CollectionResult([]))
    with pytest.raises(ValueError):
        col.enqueue(bad)


def test_receipt_only_publication_never_backdated(tmp_path):
    req = source(source_kind="fundamentals")
    row = {"symbol": "SDGR", "publication_time_basis": "receipt_only", "published_at": None,
           "event_at": None, "period_end": "2025-12-31", "market_cap": 100}
    col = collector(tmp_path, lambda *args: CollectionResult([row]), requests=[req])
    col.enqueue(START)
    saved = col.step()[0]["records"][0]
    assert saved["published_at"] == saved["event_at"] == saved["receipt_at"] == NOW.isoformat()


def test_schwab_market_only_endpoints_and_option_scope():
    calls = []
    class Session:
        def get_equity_quotes(self, symbols):
            calls.append(("quotes", symbols))
            return {"SDGR": {"bidPrice": 10, "askPrice": 11, "quoteTime": int(END.timestamp()*1000)}}
        def get_option_chain_snapshot(self, symbol):
            calls.append(("chains", symbol))
            return {"callExpDateMap": {"2026-11-20:35": {"10.0": [{"symbol": "SDGR  261120C00010000",
                "quoteTimeInLong": int(END.timestamp()*1000), "delta": .5, "volatility": 40, "totalVolume": 4}]}}}
    adapter = SchwabAdapter(Session)
    q = adapter(source(provider="schwab", endpoint="quotes"), START, END, 100)
    assert q.records[0]["bid"] == 10
    result = adapter(source(provider="schwab", source_kind="options", endpoint="chains"), START, END, 100)
    assert result.records[0]["underlying_symbol"] == "SDGR"
    assert result.records[0]["contract_symbol"].startswith("SDGR ")
    assert result.records[0]["delta"] == .5
    assert result.records[0]["implied_volatility"] == .4
    assert result.records[0]["implied_volatility_unit"] == "decimal"
    assert [x[0] for x in calls] == ["quotes", "chains"]


def test_fmp_chart_timezone_filter_and_unknown_publication():
    class Provider:
        def _get_json(self, endpoint, params):
            if endpoint.startswith("historical-chart"):
                return [{"date": "2026-10-16 15:45:00", "open": 1, "close": 2},
                        {"date": "2026-10-16 16:00:00", "open": 2, "close": 3}]
            return [{"symbol": "SDGR", "acceptedDate": "2026-10-10 15:00:00", "netProfitMarginTTM": .2}]
    adapter = FmpAdapter(Provider)
    req = source(source_kind="stock_bars", endpoint="historical-chart/5min")
    result = adapter(req, START, END, 100)
    assert len(result.records) == 1 and result.records[0]["event_at"] == START.isoformat()
    result = adapter(source(source_kind="fundamentals"), START, END, 100)
    assert result.records[0]["publication_time_basis"] == "receipt_only"
    assert result.records[0]["profit_margin"] == .2


def test_databento_batches_scopes_and_reuses_durable_response(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABENTO_API_KEY", "fake-key")
    a = source(provider="databento", dataset="EQUS.MINI", schema="trades", source_kind="stock_trades",
               params={"symbols": ["SDGR"], "stype_in": "raw_symbol"})
    b = replace(a, symbol="GLOB", params={"symbols": ["GLOB"], "stype_in": "raw_symbol"})
    calls = []
    def replay(key, request, start, end, path, **kwargs):
        calls.append(request.params["symbols"])
        return ([{"symbol": s, "ts_event": START.isoformat(), "price": 10, "size": 2, "sequence": 0}
                 for s in request.params["symbols"]], {"completed": True})
    adapter = DatabentoAdapter(tmp_path, replay, [a, b])
    one, two = adapter(a, START, END, 100), adapter(b, START, END, 100)
    assert len(calls) == 1 and set(calls[0]) == {"SDGR", "GLOB"}
    assert one.records[0]["symbol"] == "SDGR" and two.records[0]["symbol"] == "GLOB"
    assert one.records[0]["trade_id"]
    restarted = DatabentoAdapter(tmp_path, replay, [a, b])
    restarted(a, START, END, 100)
    assert len(calls) == 1


def test_symbol_independent_ids_and_manifest_known_limits(monkeypatch):
    monkeypatch.setenv("DATABENTO_EQUITIES_DATASET", "EQUS.MINI")
    assert source().source_id == source("GLOB").source_id
    assert source().identity != source("GLOB").identity
    requests = default_requests(["SDGR"])
    assert any(r.schema == "computed-greeks" and r.capability == "UNSUPPORTED" for r in requests)
    assert any(r.schema == "historical-chart-1min" and r.capability == "NOT_AUTHORIZED" for r in requests)
    assert all("key" not in r.params for r in requests)


def test_calendar_automatic_loop_skips_weekend_but_allows_regular_session():
    assert not collection_window_open(datetime(2026, 10, 17, 12, tzinfo=timezone.utc))
    assert collection_window_open(datetime(2026, 10, 16, 15, tzinfo=timezone.utc))


class FakeLive:
    scenario = "complete"
    def __init__(self, **kwargs):
        self.kwargs = kwargs
    def add_callback(self, callback, exception_callback):
        self.callback, self.failed = callback, exception_callback
    def add_reconnect_callback(self, callback, exception_callback):
        self.reconnected = callback
    def add_stream(self, stream, exception_callback):
        self.stream = stream
    def subscribe(self, **request):
        self.request = request
        return 0
    def start(self): pass
    def stop(self): pass
    def terminate(self): pass
    def is_connected(self): return False
    def block_for_close(self, timeout):
        import databento_dbn as dbn
        stamp = pd.Timestamp(self.request["start"]).value
        metadata = dbn.Metadata("EQUS.MINI", stamp, None, dbn.SType.INSTRUMENT_ID, None)
        self.stream.write(metadata.encode())
        ack = dbn.SystemMsg(stamp, "Subscription request 0 for ohlcv-1m data succeeded", dbn.SystemCode.SUBSCRIPTION_ACK)
        mapping = dbn.SymbolMappingMsg(0, 7, stamp, dbn.SType.RAW_SYMBOL, "SDGR", dbn.SType.RAW_SYMBOL,
            "SDGR", 2**64-1, 2**64-1)
        bar = dbn.OHLCVMsg(dbn.RType.OHLCV_1M, 1, 7, stamp, 10000000000, 11000000000, 9000000000, 10500000000, 3)
        completion = dbn.SystemMsg(stamp, "Finished ohlcv-1m replay", dbn.SystemCode.REPLAY_COMPLETED)
        records = [ack, mapping, bar] + ([] if self.scenario == "missing_completion" else [completion])
        if self.scenario == "wrong_control":
            records[-1] = dbn.SystemMsg(stamp, "Finished trades replay", dbn.SystemCode.REPLAY_COMPLETED)
        if self.scenario == "reconnect":
            self.reconnected()
        if self.scenario == "denied":
            records = [ack, dbn.ErrorMsg(stamp, "not entitled to subscription; secret=NEVER_PERSIST")]
        if self.scenario == "rate_limited":
            records = [ack, dbn.ErrorMsg(stamp, "429 rate limit")]
        for record in records:
            self.stream.write(bytes(record))
            try:
                self.callback(record)
            except Exception as exc:
                self.failed(exc)


def test_native_replay_retains_verified_control_mapping_and_price(tmp_path):
    from ml.intraday.market_collection_replay import capture
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=1)
    start = end - timedelta(minutes=15)
    req = source(provider="databento", dataset="EQUS.MINI", schema="ohlcv-1m",
        params={"symbols": ["SDGR"], "stype_in": "raw_symbol"})
    FakeLive.scenario = "complete"
    rows, evidence = capture("fake", req, start, end, tmp_path / "raw.dbn", client_factory=FakeLive)
    assert rows[0]["symbol"] == "SDGR" and rows[0]["close"] == 10.5
    assert evidence["completed"] and evidence["selected_quarter_records"] == 1
    assert (tmp_path / "raw.dbn").read_bytes().startswith(b"DBN")


@pytest.mark.parametrize("scenario", ["missing_completion", "wrong_control", "reconnect"])
def test_native_replay_rejects_incomplete_delivery(tmp_path, scenario):
    from ml.intraday.market_collection_replay import capture
    end = datetime.now(timezone.utc) - timedelta(minutes=1)
    req = source(provider="databento", dataset="EQUS.MINI", schema="ohlcv-1m",
        params={"symbols": ["SDGR"], "stype_in": "raw_symbol"})
    FakeLive.scenario = scenario
    with pytest.raises((RuntimeError, TimeoutError)):
        capture("fake", req, end - timedelta(minutes=15), end, tmp_path / "raw.dbn", client_factory=FakeLive)


@pytest.mark.parametrize("scenario,status", [("denied", "NOT_AUTHORIZED"), ("rate_limited", "RATE_LIMITED")])
def test_native_gateway_status_is_structured_without_error_string_leak(tmp_path, scenario, status):
    from ml.intraday.market_collection_replay import capture
    from ml.intraday.market_collection import CollectionFailure, classify_error
    end = datetime.now(timezone.utc) - timedelta(minutes=1)
    req = source(provider="databento", dataset="EQUS.MINI", schema="ohlcv-1m",
        params={"symbols": ["SDGR"], "stype_in": "raw_symbol"})
    FakeLive.scenario = scenario
    with pytest.raises(CollectionFailure) as caught:
        capture("fake", req, end - timedelta(minutes=15), end, tmp_path / "raw.dbn", client_factory=FakeLive)
    assert classify_error(caught.value)[0] == status
    assert "NEVER_PERSIST" not in str(caught.value)
    assert "NEVER_PERSIST" not in json.dumps(caught.value.collection_evidence)


def test_frozen_configuration_and_source_semantics_are_bound(tmp_path, monkeypatch):
    from ml.intraday.market_collection import load_local_collection_config, configuration_digest
    monkeypatch.setenv("DATABENTO_EQUITIES_DATASET", "EQUS.MINI")
    config = load_local_collection_config(tmp_path, "scout", ["SDGR"])
    assert config["configuration_sha256"] == configuration_digest(config)
    assert source().source_id != source(endpoint="different").source_id
    assert source().source_id != source(params={"symbol": "SDGR", "period": "annual"}).source_id
    path = tmp_path / "intraday/scout/market-data/configuration.json"
    config["requests"][0]["schema"] = "changed"
    path.write_bytes(canonical(config))
    with pytest.raises(ValueError, match="checksum"):
        load_local_collection_config(tmp_path, "scout", ["SDGR"])


def test_capacity_failure_does_not_replay_the_same_window_forever(tmp_path):
    calls = []
    def adapter(*args):
        calls.append(1)
        raise RuntimeError("record budget exceeded")
    col = collector(tmp_path, adapter)
    col.enqueue(START)
    assert col.step()[0]["status"] == "CAPACITY_BLOCKED"
    assert col.step() == [] and len(calls) == 1


def test_canonical_raw_timestamps_preserve_nanoseconds_and_missing_values():
    value = json.loads(canonical({"event": pd.Timestamp("2026-10-16T19:45:00.123456789Z"), "undefined": pd.NaT}))
    assert value["event"].endswith(".123456789+00:00")
    assert value["undefined"] is None


def test_collection_clocks_and_causal_loader_preserve_one_nanosecond(tmp_path):
    from ml.intraday.market_collection import instant, snapshot_digest, load_snapshots
    assert instant("2026-10-16T20:00:00.000000001Z") > instant("2026-10-16T20:00:00Z")
    col = collector(tmp_path, lambda *a: CollectionResult([]))
    with pytest.raises(ValueError, match="exact"):
        col.enqueue(pd.Timestamp(START)+pd.Timedelta(1, "ns"))
    col.enqueue(START)
    snapshot = col.step()[0]
    receipt = pd.Timestamp(snapshot["receipt_at"])
    assert not load_snapshots(tmp_path, symbol="SDGR", cutoff=receipt-pd.Timedelta(1, "ns"))
    assert load_snapshots(tmp_path, symbol="SDGR", cutoff=receipt)


def test_subscription_deadline_is_unavailable_not_entitlement_denial():
    from ml.intraday.market_collection import classify_error
    assert classify_error(TimeoutError("Replay subscription exceeded deadline"))[0] == "UNAVAILABLE"
    assert classify_error(RuntimeError("subscription acknowledgement mismatch"))[0] == "ERROR"
    assert classify_error(RuntimeError("not authorized for this dataset"))[0] == "NOT_AUTHORIZED"
    assert classify_error(RuntimeError("FMP batch-commodity-quotes request failed with HTTP 402: not available"))[0] == "NOT_AUTHORIZED"


def test_streaming_projection_counts_real_trades_without_native_record_cap():
    from ml.intraday.market_collection_replay import project_frames, PROJECTION_VERSION
    frames = []
    for chunk in range(3):
        frame = pd.DataFrame([{"symbol": "SDGR", "instrument_id": 7,
            "ts_event": START + timedelta(seconds=chunk * 10 + i), "price": 10 + chunk + i / 100,
            "size": 2, "sequence": 0} for i in range(10)])
        frames.append(frame)
    rows, count = project_frames(frames, schema="trades", start=START, end=END, max_records=1)
    assert count == 30 and len(rows) == 1
    assert rows[0]["observed_trade_count"] == 30 and rows[0]["size"] == 60
    assert rows[0]["first_price"] == 10 and rows[0]["last_price"] == 12.09
    assert rows[0]["projection_version"] == PROJECTION_VERSION
    assert rows[0]["first_event_at"] == START
    assert rows[0]["last_event_at"] == START + timedelta(seconds=29)


def test_sampled_quote_end_boundaries_and_projection_preserve_actual_records():
    from ml.intraday.market_collection_replay import project_frames
    times = [START, START + timedelta(minutes=1), END, END + timedelta(minutes=1)]
    frame = pd.DataFrame([{"symbol": "SDGR", "instrument_id": 7, "ts_recv": t,
        "ts_event": START - timedelta(days=1), "bid_px_00": 10 + i} for i, t in enumerate(times)])
    rows, count = project_frames([frame], schema="bbo-1m", start=START, end=END, max_records=2)
    assert count == 2
    assert [r["ts_recv"] for r in rows] == [times[1], END]
    assert [r["bid_px_00"] for r in rows] == [11, 12]


def test_stock_quote_projection_keeps_latest_for_identical_event_clocks():
    from ml.intraday.market_collection_replay import project_frames
    frame = pd.DataFrame([{"symbol": "SDGR", "instrument_id": 7,
        "ts_recv": START + timedelta(seconds=1), "bid_px_00": price} for price in [10, 11]])
    rows, count = project_frames([frame], schema="mbp-1", start=START, end=END, max_records=2)
    assert count == 2 and len(rows) == 1 and rows[0]["bid_px_00"] == 11


@pytest.mark.parametrize("message,status", [
    ("Not authorized for mbp-10 schema; token=NEVER_PERSIST", "NOT_AUTHORIZED"),
    ("Invalid start time. Must be 2026-10-17T07:15:00Z or later, or 0", "UNAVAILABLE"),
    ("Live replay not supported for cmbp-1 schema", "UNSUPPORTED"),
])
def test_retained_native_error_classified_when_sdk_raises_before_callback(tmp_path, message, status):
    from ml.intraday.market_collection_replay import capture
    from ml.intraday.market_collection import CollectionFailure
    class EarlySDKFailure(FakeLive):
        def block_for_close(self, timeout):
            import databento_dbn as dbn
            stamp = pd.Timestamp(self.request["start"]).value
            self.stream.write(dbn.Metadata("EQUS.MINI", stamp, None, dbn.SType.INSTRUMENT_ID, None).encode())
            self.stream.write(bytes(dbn.ErrorMsg(stamp, message)))
            raise RuntimeError("Generic SDK exception")
    end = datetime.now(timezone.utc)-timedelta(minutes=1)
    req = source(provider="databento", dataset="EQUS.MINI", schema="ohlcv-1m",
        params={"symbols": ["SDGR"], "stype_in": "raw_symbol"})
    with pytest.raises(CollectionFailure) as caught:
        capture("fake", req, end-timedelta(minutes=15), end, tmp_path/"native.dbn", client_factory=EarlySDKFailure)
    assert caught.value.collection_status == status
    assert "NEVER_PERSIST" not in json.dumps(caught.value.collection_evidence)


def test_current_native_stream_requires_observed_window_and_never_replay_completion(tmp_path):
    from ml.intraday.market_collection_replay import capture
    from ml.intraday.market_collection import instant
    import threading
    class CurrentLive(FakeLive):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.closed = threading.Event()
        def stop(self):
            self.closed.set()
        def block_for_close(self, timeout):
            import databento_dbn as dbn
            import time
            assert "start" not in self.request
            time.sleep(.03)  # longer than requested capture: window must start after ACK
            stamp = pd.Timestamp.now(tz="UTC").value
            self.stream.write(dbn.Metadata("EQUS.MINI", 0, None, dbn.SType.INSTRUMENT_ID, None).encode())
            ack = dbn.SystemMsg(stamp, "Subscription request 0 for ohlcv-1m data succeeded", dbn.SystemCode.SUBSCRIPTION_ACK)
            self.stream.write(bytes(ack))
            self.callback(ack)
            stamp = pd.Timestamp.now(tz="UTC").value
            records = [
                dbn.SymbolMappingMsg(0, 7, stamp, dbn.SType.RAW_SYMBOL, "SDGR", dbn.SType.RAW_SYMBOL, "SDGR", 2**64-1, 2**64-1),
                dbn.OHLCVMsg(dbn.RType.OHLCV_1M, 1, 7, stamp, 10000000000, 11000000000, 9000000000, 10500000000, 3),
            ]
            for record in records:
                self.stream.write(bytes(record))
                self.callback(record)
            assert self.closed.wait(timeout)
    end = datetime.now(timezone.utc)-timedelta(minutes=1)
    req = source(provider="databento", dataset="EQUS.MINI", schema="ohlcv-1m",
        params={"symbols": ["SDGR"], "stype_in": "raw_symbol", "capture_mode": "current_stream", "capture_seconds": .02})
    rows, evidence = capture("fake", req, end-timedelta(minutes=15), end, tmp_path/"current.dbn", client_factory=CurrentLive)
    assert len(rows) == 1 and evidence["replay_completed"] == []
    assert evidence["source_request_start"] is None and evidence["mode"] == "current_stream_snapshot"
    assert evidence["actual_post_ack_observation_seconds"] >= .02
    assert instant(evidence["capture_started_at"])-instant(evidence["connection_started_at"]) >= timedelta(seconds=.03)
    assert "no complete-quarter" in evidence["coverage_claim"]
    with pytest.raises(TimeoutError, match="insufficient"):
        capture("fake", req, end-timedelta(minutes=15), end, tmp_path/"late-ack.dbn",
            timeout_seconds=.04, client_factory=CurrentLive)


def test_fmp_legacy_commodity_batch_is_disabled_before_provider_access():
    from ml.intraday.market_collection_adapters import FmpAdapter
    calls = []
    class Provider:
        def _get_json(self, endpoint, params):
            calls.append((endpoint, params))
            return [{"symbol": symbol, "price": 10, "timestamp": int(END.timestamp())}
                for symbol in ["CLUSD", "OTHER"]]
    req = source(provider="fmp", source_kind="cross_asset", symbol="*", dataset="stable",
        schema="commodity-quote", endpoint="batch-commodity-quotes",
        params={"configured_symbols": ["CLUSD", "NGUSD"]})
    result = FmpAdapter(provider_factory=Provider)(req, START, END, 100)
    assert calls == []
    assert result.records == []
    assert result.status == "UNSUPPORTED" and result.evidence["provider_called"] is False


def test_fmp_chart_requests_exchange_dates_across_utc_midnight():
    calls = []
    class Provider:
        def _get_json(self, endpoint, params):
            calls.append((endpoint, params))
            return [{"symbol": "SDGR", "date": "2026-10-16 19:45:00", "close": 10},
                    {"symbol": "SDGR", "date": "2026-10-16 20:00:00", "close": 11}]
    req = source(schema="historical-chart-5min", endpoint="historical-chart/5min", source_kind="stock_bars")
    start, end = instant("2026-10-16T23:45:00Z"), instant("2026-10-17T00:00:00Z")
    result = FmpAdapter(provider_factory=Provider)(req, start, end, 100)
    assert calls[0][1] == {"symbol": "SDGR", "from": "2026-10-16", "to": "2026-10-16"}
    assert [r["close"] for r in result.records] == [10]
    assert result.records[0]["event_at"] == "2026-10-16T23:45:00+00:00"
