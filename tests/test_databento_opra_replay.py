from __future__ import annotations

import copy
from pathlib import Path

import databento as db
import databento_dbn as dbn
import pandas as pd
import pytest

from datafetching import databento_opra_replay as replay


START = pd.Timestamp("2026-09-04T00:00:00Z")
END = pd.Timestamp("2026-09-05T00:00:00Z")
NOW = pd.Timestamp("2026-09-05T08:30:00Z")


def _mapping(symbol="AAPL  260904C00185000"):
    return dbn.SymbolMappingMsg(0, 7, NOW.value, dbn.SType.PARENT, "AAPL.OPT", dbn.SType.RAW_SYMBOL,
                               symbol, 2**64 - 1, 2**64 - 1)


def _bar():
    return dbn.OHLCVMsg(dbn.RType.OHLCV_1H, 30, 7, START.value + 14 * 3600 * 10**9,
                       100, 110, 90, 105, 10)


def _quote():
    # Last-sale ts_event is intentionally stale: CBBO coverage uses ts_recv.
    return dbn.CBBOMsg(dbn.RType.CBBO_1M, 30, 7, START.value - 10**9, 100, 1,
                      dbn.Side.ASK, START.value + 14 * 3600 * 10**9)


def _definition():
    return dbn.InstrumentDefMsg(
        30, 7, START.value + 10**9, START.value + 10**9, 10000000, 1,
        "AAPL  260904C00185000", "AAPL", "OPT", dbn.InstrumentClass.CALL,
        dbn.SecurityUpdateAction.ADD,
    )


class FakeClient:
    def __init__(self, *, scenario="success", **kwargs):
        self.kwargs = kwargs
        self.scenario = scenario
        self.stopped = False
        self.terminated = False

    def add_callback(self, callback, exception_callback):
        self.callback, self.callback_error = callback, exception_callback

    def add_stream(self, stream, exception_callback):
        self.stream, self.stream_error = stream, exception_callback

    def add_reconnect_callback(self, callback, exception_callback):
        self.reconnect = callback

    def subscribe(self, **request):
        self.request = request
        return 0

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def terminate(self):
        self.terminated = True

    def block_for_close(self, timeout):
        schema = self.request["schema"]
        start = pd.Timestamp(self.request["start"]).value
        if self.scenario == "wrong_start":
            start += 10**9
        metadata = dbn.Metadata("OPRA.PILLAR", start, None, dbn.SType.INSTRUMENT_ID, None)
        self.stream.write(metadata.encode())
        ack = dbn.SystemMsg(NOW.value, f"Subscription request 0 for {schema} data succeeded", dbn.SystemCode.SUBSCRIPTION_ACK)
        completion = dbn.SystemMsg(NOW.value, f"Finished {schema} replay", dbn.SystemCode.REPLAY_COMPLETED)
        data = {"ohlcv-1h": _bar, "cbbo-1m": _quote, "definition": _definition}[schema]()
        records = [ack, _mapping(), data, completion]
        if self.scenario == "missing_completion":
            records.pop()
        elif self.scenario == "wrong_completion":
            records[-1] = dbn.SystemMsg(NOW.value, "Finished trades replay", dbn.SystemCode.REPLAY_COMPLETED)
        elif self.scenario == "error_after_ack":
            records = [ack, dbn.ErrorMsg(NOW.value, "Invalid start time. Must be later or 0")]
        elif self.scenario == "error_after_completion":
            records.append(dbn.ErrorMsg(NOW.value, "Records skipped"))
        elif self.scenario == "missing_mapping":
            records.pop(1)
        elif self.scenario == "wrong_root":
            records[1] = _mapping("MSFT  260904C00185000")
        elif self.scenario == "duplicate_completion":
            records.append(completion)
        elif self.scenario == "reconnect":
            self.reconnect(NOW, NOW)
        for record in records:
            try:
                self.stream.write(bytes(record))
            except Exception as exc:
                self.stream_error(exc)
            try:
                self.callback(record)
            except Exception as exc:
                self.callback_error(exc)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(replay, "_utc_now", lambda: NOW)


def _capture(tmp_path: Path, *, scenario="success", schema="ohlcv-1h", **overrides):
    clients = []

    def factory(**kwargs):
        result = FakeClient(scenario=scenario, **kwargs)
        clients.append(result)
        return result

    arguments = dict(api_key="fake-key", schema=schema, symbols=["AAPL.OPT"],
                     start=START, end=END, raw_path=tmp_path / "native.dbn.zst",
                     max_bytes=100000, client_factory=factory)
    arguments.update(overrides)
    return replay.capture_replay(**arguments), clients


@pytest.mark.parametrize("schema", ["ohlcv-1h", "cbbo-1m", "definition"])
def test_complete_native_capture_and_independent_validation(tmp_path, schema):
    delivery, clients = _capture(tmp_path, schema=schema)
    assert delivery["data_record_count"] == 1
    assert delivery["record_count"] == 4
    assert delivery["raw_format"] == "dbn"
    assert (tmp_path / "native.dbn.zst").read_bytes().startswith(b"DBN")
    assert clients[0].kwargs["compression"] is db.Compression.ZSTD
    assert clients[0].kwargs["slow_reader_behavior"] == "warn"
    assert clients[0].kwargs["reconnect_policy"] == "none"
    assert clients[0].request["start"] == START.isoformat()
    assert clients[0].stopped and clients[0].terminated
    replay.validate_replay_delivery(delivery, {"schema": schema, "symbols": ["AAPL.OPT"],
                                             "start": START, "end": END}, tmp_path / "native.dbn.zst")
    if schema == "cbbo-1m":
        assert delivery["first_record_timestamp_ns"] == _quote().ts_recv


def test_provider_acceptance_allows_explicit_start_older_than_24_hours(tmp_path):
    delivery, _ = _capture(tmp_path)
    assert NOW - START > pd.Timedelta(hours=24)
    assert delivery["completed"] is True


@pytest.mark.parametrize("scenario", ["missing_completion", "wrong_completion", "error_after_ack",
                                     "error_after_completion", "missing_mapping", "wrong_root",
                                     "duplicate_completion", "reconnect", "wrong_start"])
def test_rejects_incomplete_or_ambiguous_delivery(tmp_path, scenario):
    with pytest.raises(replay.ReplayCaptureError):
        _capture(tmp_path, scenario=scenario)
    assert (tmp_path / "native.dbn.zst").exists()


def test_byte_budget_stops_before_excess_write(tmp_path):
    with pytest.raises(replay.ReplayCaptureError, match="byte budget"):
        _capture(tmp_path, max_bytes=150)
    assert (tmp_path / "native.dbn.zst").stat().st_size <= 150


def test_existing_raw_is_preserved(tmp_path):
    path = tmp_path / "native.dbn.zst"
    path.write_bytes(b"original")
    with pytest.raises(FileExistsError):
        _capture(tmp_path)
    assert path.read_bytes() == b"original"


@pytest.mark.parametrize("overrides", [
    {"start": 0}, {"start": START - pd.Timedelta(days=2)},
    {"end": NOW + pd.Timedelta(seconds=1)}, {"end": START},
    {"symbols": ["AAPL.OPT", "MSFT.OPT"]}, {"symbols": "ALL_SYMBOLS"},
    {"schema": "trades"}, {"timeout_seconds": float("inf")}, {"max_bytes": 0},
])
def test_invalid_scope_fails_before_any_capture(tmp_path, overrides):
    with pytest.raises(replay.ReplayCaptureError):
        _capture(tmp_path, **overrides)
    assert not (tmp_path / "native.dbn.zst").exists()


def test_tampered_raw_or_claimed_counts_are_rejected(tmp_path):
    delivery, _ = _capture(tmp_path)
    request = {"schema": "ohlcv-1h", "symbols": ["AAPL.OPT"], "start": START, "end": END}
    changed = copy.deepcopy(delivery)
    changed["data_record_count"] = 2
    with pytest.raises(replay.ReplayCaptureError, match="counts"):
        replay.validate_replay_delivery(changed, request, tmp_path / "native.dbn.zst")
    with (tmp_path / "native.dbn.zst").open("ab") as stream:
        stream.write(b"bad")
    with pytest.raises(replay.ReplayCaptureError, match="checksum/size"):
        replay.validate_replay_delivery(delivery, request, tmp_path / "native.dbn.zst")
    changed = copy.deepcopy(delivery)
    changed["raw_bytes"] = (tmp_path / "native.dbn.zst").stat().st_size
    changed["raw_sha256"] = replay._checksum(tmp_path / "native.dbn.zst")
    with pytest.raises(replay.ReplayCaptureError, match="DBN verification"):
        replay.validate_replay_delivery(changed, request, tmp_path / "native.dbn.zst")


def test_target_can_clip_source_warmup_but_not_extend_coverage(tmp_path):
    delivery, _ = _capture(tmp_path)
    request = {"schema": "ohlcv-1h", "symbols": ["AAPL.OPT"],
               "start": START + pd.Timedelta(hours=1), "end": END}
    replay.validate_replay_delivery(delivery, request, tmp_path / "native.dbn.zst")
    request["end"] = END + pd.Timedelta(hours=1)
    with pytest.raises(replay.ReplayCaptureError, match="bounds"):
        replay.validate_replay_delivery(delivery, request)


def test_enum_and_string_control_codes():
    assert replay._code(dbn.SystemCode.REPLAY_COMPLETED) == 3
    assert replay._code("replay_completed") == 3
    assert replay._code("subscription_ack") == 1


def test_silent_sdk_timeout_is_failure_even_with_completion(tmp_path, monkeypatch):
    times = iter((0.0, 0.0, 301.0))
    monkeypatch.setattr(replay.time, "monotonic", lambda: next(times))
    with pytest.raises(replay.ReplayCaptureError, match="timeout"):
        _capture(tmp_path)
