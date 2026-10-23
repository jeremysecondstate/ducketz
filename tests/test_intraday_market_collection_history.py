from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json

import pandas as pd
import pytest

from ml.intraday.market_collection import CollectionFailure, SourceRequest, instant
from ml.intraday.market_collection_adapters import DatabentoAdapter, default_requests
from ml.intraday.market_collection_history import (
    MODE, PARAMETERS, capture, decode_depth, effective_window, historical_request,
)
from ml.intraday.market_collection_replay import PROJECTION_VERSION

pytestmark = pytest.mark.usefixtures("offline_databento_sdk")
START, END = instant("2026-10-17T12:45:00Z"), instant("2026-10-17T13:00:00Z")
NOW = END + pd.Timedelta(seconds=10)


def old_request(schema="mbp-10"):
    return SourceRequest("databento", "cross_asset", "*", "GLBX.MDP3", schema,
        params={"symbols": ["ESZ6"], "stype_in": "raw_symbol", "projection_version": PROJECTION_VERSION})


def request(schema="mbp-10"):
    return historical_request(old_request(schema))


@pytest.fixture(autouse=True)
def offline_metadata_loop(monkeypatch):
    # Fake gateways have no file descriptors. Windows' normal asyncio loop
    # creates a loopback wakeup socket even for these offline coroutines. Use
    # a timer-capable no-I/O loop; any attempted I/O registration still fails.
    import asyncio
    import selectors
    import time
    class NoIOSelector(selectors.SelectSelector):
        def register(self, *args, **kwargs):
            raise AssertionError("Offline Historical fixture attempted real I/O")
        def select(self, timeout=None):
            if timeout: time.sleep(timeout)
            return []
    class NoIOLoop(asyncio.SelectorEventLoop):
        def __init__(self): super().__init__(NoIOSelector())
        def _make_self_pipe(self): pass
        def _close_self_pipe(self): pass
        def _write_to_self(self): pass
    def run(coroutine):
        with asyncio.Runner(loop_factory=NoIOLoop) as runner:
            return runner.run(coroutine)
    monkeypatch.setattr(asyncio, "run", run)


def availability(end="2026-10-16T23:57:00Z"):
    return {"schema": {s: {"start": "2010-01-01T00:00:00Z", "end": end} for s in ("mbp-10", "mbo")}}


class Gateway:
    def __init__(self, **overrides):
        self.results = {"get_dataset_range": availability(), "get_cost": 0.0,
            "get_billable_size": 128, "get_record_count": 2, **overrides}
        self.calls = []
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def metadata(self, method, params):
        self.calls.append((method, dict(params)))
        return self.results[method]
    async def download(self, params, path, byte_limit):
        self.calls.append(("download", dict(params)))
        path.write_bytes(b"test-native-bytes")
        return path.stat().st_size


def decode(path, req, start, end, max_records, **kwargs):
    assert path.read_bytes() == b"test-native-bytes"
    return [{"symbol": "ESZ6", "instrument_id": 100, "ts_recv": start.isoformat(),
             "ts_event": start.isoformat(), "bid_px_00": 10, "ask_px_00": 11}], 2


def run(tmp_path, gateway, **kwargs):
    return capture("not-a-real-key", request(), START, END, tmp_path / "unused.dbn",
        gateway_factory=lambda key: gateway, decoder=decode, clock=lambda: NOW, **kwargs)


def test_latest_complete_historical_quarter_is_not_relabelled_current():
    start, end, evidence = effective_window(request(), START, END, availability(), NOW)
    assert start == instant("2026-10-16T23:30:00Z")
    assert end == instant("2026-10-16T23:45:00Z")
    assert end-start == pd.Timedelta(minutes=15)
    assert evidence["requested_end"] == END.isoformat()
    assert evidence["effective_end"] != evidence["requested_end"]
    assert evidence["historical_lag_seconds"] > 0


@pytest.mark.parametrize("end", ["2026-10-17T14:01:00Z", "2026-10-17T13:00:00Z"])
def test_available_current_completed_quarter_is_used_in_full(end):
    a, b, _ = effective_window(request(), START, END, availability(end), NOW)
    assert (a, b) == (START, END)


@pytest.mark.parametrize("value", [{}, {"start": "2010-01-01", "end": "2026-10-17"}, availability("2026-09-16T00:00:00Z")])
def test_missing_schema_or_outside_rolling_month_is_unavailable(value):
    with pytest.raises(CollectionFailure, match="schema range|rolling month"):
        effective_window(request(), START, END, value, NOW)


@pytest.mark.parametrize("start,end", [(START+pd.Timedelta(seconds=1), END),
    (START, END+pd.Timedelta(seconds=1)), (START, END+pd.Timedelta(minutes=15))])
def test_no_partial_quarter_or_future_request(start, end):
    with pytest.raises(ValueError, match="decision quarter"):
        effective_window(request(), start, end, availability(), NOW)


def test_new_identity_preserves_old_scope_and_disallows_policy_weakening():
    old, new = old_request(), request()
    assert old.source_id != new.source_id
    assert new.params["symbols"] == old.params["symbols"]
    assert new.params["window_seconds"] == 900
    for key, value in (("max_cost_usd", 1.0), ("window_seconds", 60), ("symbols", ["ALL_SYMBOLS"])):
        invalid = replace(new, params={**new.params, key: value})
        with pytest.raises(ValueError):
            effective_window(invalid, START, END, availability(), NOW)


def test_old_live_depth_cannot_touch_credentials_client_or_cached_success(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABENTO_API_KEY", raising=False)
    def forbidden(*args, **kwargs): raise AssertionError("Live must never be called")
    adapter = DatabentoAdapter(tmp_path, replay=forbidden, historical=forbidden)
    for schema in ("mbp-10", "mbo"):
        result = adapter(old_request(schema), START, END, 100)
        assert result.status == "UNSUPPORTED"
        assert result.evidence["provider_called"] is False


def test_inventory_retains_legacy_identity_and_adds_separate_history(monkeypatch):
    monkeypatch.setenv("DATABENTO_EQUITIES_DATASET", "EQUS.MINI")
    monkeypatch.setenv("DATABENTO_CME_DATASET", "GLBX.MDP3")
    monkeypatch.setenv("DATABENTO_CME_SCHEMAS", "mbp-10,mbo,ohlcv-1m")
    monkeypatch.setenv("DATABENTO_CME_CONTEXT_SYMBOLS", "ES.v.0,NQ.v.0")
    monkeypatch.setenv("DATABENTO_CME_CONTRACT_SYMBOLS", "ESZ6")
    requests = default_requests(["SDGR"])
    depth = [r for r in requests if r.dataset == "GLBX.MDP3" and r.schema in {"mbp-10", "mbo"}]
    assert len(depth) == 8
    for old in (r for r in depth if not r.endpoint):
        assert old.capability == "UNSUPPORTED"
        assert old.source_id == replace(old, capability="UNPROBED").source_id
        new = next(r for r in depth if r.schema == old.schema and r.params["symbols"] == old.params["symbols"] and r.endpoint)
        assert new.source_id != old.source_id and new.capability == "UNPROBED"
    assert all(r.params.get("capture_mode") == MODE for r in depth if r.endpoint)


@pytest.mark.parametrize("quote", [.01, -1, None, "0", True, float("nan"), float("inf")])
def test_download_requires_verified_exact_zero_quote(tmp_path, quote):
    gateway = Gateway(get_cost=quote)
    with pytest.raises(CollectionFailure) as failure:
        run(tmp_path, gateway)
    assert failure.value.collection_status == "NOT_AUTHORIZED"
    assert not any(c[0] == "download" for c in gateway.calls)


@pytest.mark.parametrize("overrides", [{"get_record_count": 1_000_001}, {"get_billable_size": 256*1024*1024+1}])
def test_capacity_preflight_blocks_without_sampling_or_download(tmp_path, overrides):
    gateway = Gateway(**overrides)
    with pytest.raises(CollectionFailure) as failure:
        run(tmp_path, gateway)
    assert failure.value.collection_status == "CAPACITY_BLOCKED"
    assert failure.value.collection_evidence["window_seconds"] == 900
    assert not any(c[0] == "download" for c in gateway.calls)


def test_durable_effective_window_cache_and_native_integrity(tmp_path):
    gateway = Gateway()
    rows, evidence = run(tmp_path, gateway)
    assert evidence["completed"] and evidence["cost_quote_usd"] == 0
    assert evidence["selected_native_records"] == 2
    assert evidence["raw_bytes"] == len(b"test-native-bytes")
    download = next(c[1] for c in gateway.calls if c[0] == "download")
    assert instant(download["end"])-instant(download["start"]) == pd.Timedelta(minutes=15)
    assert int(download["limit"]) == PARAMETERS["max_native_records"]+1
    next_gateway = Gateway()
    rows2, evidence2 = run(tmp_path, next_gateway)
    assert rows2 == rows and evidence2["cache_reused"]
    assert [c[0] for c in next_gateway.calls] == ["get_dataset_range"]
    Path(evidence["raw_path"]).write_bytes(b"tampered")
    with pytest.raises(CollectionFailure):
        run(tmp_path, Gateway())


def test_download_failure_retains_partial_native_evidence_without_success_cache(tmp_path):
    class Interrupted(Gateway):
        async def download(self, params, path, byte_limit):
            path.write_bytes(b"partial")
            raise TimeoutError("private authenticated URL must not appear")
    with pytest.raises(CollectionFailure) as failure:
        run(tmp_path, Interrupted())
    evidence = failure.value.collection_evidence
    assert evidence["completed"] is False and evidence["raw_bytes"] == 7 and evidence["raw_sha256"]
    assert "private" not in str(failure.value)
    assert not list((tmp_path/"historical").glob("*.json"))


def test_total_metadata_deadline_aborts_before_download(tmp_path, monkeypatch):
    import asyncio
    from ml.intraday import market_collection_history as history
    monkeypatch.setitem(history.PARAMETERS, "timeout_seconds", .01)
    class Slow(Gateway):
        async def metadata(self, *args):
            await asyncio.sleep(.05)
            raise AssertionError("Deadline did not cancel metadata")
    with pytest.raises(CollectionFailure) as failure:
        run(tmp_path, Slow())
    assert failure.value.collection_status == "UNAVAILABLE"
    assert not list(tmp_path.rglob("*.dbn"))


def test_historical_adapter_retains_actual_older_record_clocks(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABENTO_API_KEY", "fake")
    calls = []
    def operation(key, req, start, end, path, **kwargs):
        calls.append((req, kwargs))
        return [{"symbol": "ESZ6", "instrument_id": 1, "ts_recv": "2026-10-16T23:44:59.000000001Z",
            "ts_event": "2026-10-16T23:44:58Z", "price": 10}], {"mode": MODE, "completed": True}
    def forbidden(*args, **kwargs): raise AssertionError("Live path used")
    result = DatabentoAdapter(tmp_path, replay=forbidden, historical=operation)(request("mbo"), START, END, 100)
    assert len(calls) == 1 and calls[0][1]["cache_root"] == tmp_path/"historical"
    assert result.records[0]["event_at"] == "2026-10-16T23:44:59.000000001+00:00"
    assert result.records[0]["exchange_event_at"] == "2026-10-16T23:44:58+00:00"
    assert result.records[0]["event_clock_basis"] == "ts_recv"


def native_file(path, schema, *, truncated=False, different_symbol=False):
    import databento_dbn as dbn
    start, end = instant("2026-10-16T23:30:00Z"), instant("2026-10-16T23:45:00Z")
    metadata = dbn.Metadata("GLBX.MDP3", start.value, dbn.SType.RAW_SYMBOL, dbn.SType.INSTRUMENT_ID,
        dbn.Schema(schema), symbols=["OTHER" if different_symbol else "ESZ6"], end=end.value,
        mappings=[SimpleNamespace(raw_symbol="ESZ6", intervals=[SimpleNamespace(start_date=start.date(),
            end_date=(end+pd.Timedelta(days=1)).date(), symbol="100")])])
    rows = []
    for offset in (1, 5, 899):
        clock = start.value + offset*1_000_000_000
        args = dict(publisher_id=1, instrument_id=100, ts_event=clock, ts_recv=clock,
                    price=10_000_000_000, size=1, action=dbn.Action.ADD, side=dbn.Side.BID)
        row = dbn.MBP10Msg(**args, depth=0) if schema == "mbp-10" else dbn.MBOMsg(**args, order_id=1)
        rows.append(bytes(row))
    content = metadata.encode()+b"".join(rows)
    path.write_bytes(content[:-1] if truncated else content)
    return start, end


@pytest.mark.parametrize("schema", ["mbp-10", "mbo"])
def test_real_native_dbn_complete_decode_projection_and_mapping(tmp_path, schema):
    path = tmp_path/"native.dbn"
    start, end = native_file(path, schema)
    rows, count = decode_depth(path, request(schema), start, end, 100)
    assert count == 3 and len(rows) == 2
    assert [r["projection_position"] for r in rows] == ["first", "last"]
    assert all(r["symbol"] == "ESZ6" and r["observed_native_record_count"] == 3 for r in rows)
    assert instant(rows[0]["ts_recv"]) == start+pd.Timedelta(seconds=1)
    assert instant(rows[1]["ts_recv"]) == end-pd.Timedelta(seconds=1)


@pytest.mark.parametrize("kwargs", [{"truncated": True}, {"different_symbol": True}])
def test_real_native_dbn_truncation_and_scope_mismatch_fail(tmp_path, kwargs):
    path = tmp_path/"bad.dbn"
    start, end = native_file(path, "mbp-10", **kwargs)
    with pytest.raises((Warning, ValueError)):
        decode_depth(path, request(), start, end, 100)
