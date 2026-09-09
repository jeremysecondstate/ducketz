from __future__ import annotations

import copy
import traceback
from pathlib import Path

import databento as db
import databento_dbn as dbn
import pandas as pd
import pytest

from datafetching import databento_xnas_replay as replay


START = pd.Timestamp("2026-09-08T11:00:00Z")
END = pd.Timestamp("2026-09-09T00:00:00Z")
NOW = pd.Timestamp("2026-09-09T02:00:00Z")


def _mapping(symbol="AAPL", *, output="7", instrument_id=7,
             stype_in=dbn.SType.RAW_SYMBOL, stype_out=dbn.SType.INSTRUMENT_ID,
             start_ts=2**64 - 1, end_ts=2**64 - 1):
    return dbn.SymbolMappingMsg(0, instrument_id, NOW.value, stype_in, symbol,
                               stype_out, output, start_ts, end_ts)


def _bar(stamp=None, *, instrument_id=7, rtype=dbn.RType.OHLCV_1M):
    return dbn.OHLCVMsg(rtype, 2, instrument_id,
                       START.value + 60 * 10**9 if stamp is None else stamp,
                       100, 110, 90, 105, 10)


class FakeClient:
    def __init__(self, *, scenario="success", **kwargs):
        self.kwargs, self.scenario = kwargs, scenario
        self.stopped = self.terminated = False
        self.stopped_after_interval = []

    def add_callback(self, callback, exception_callback):
        self.callback, self.callback_error = callback, exception_callback

    def add_stream(self, stream, exception_callback):
        self.stream, self.stream_error = stream, exception_callback

    def add_reconnect_callback(self, callback, exception_callback):
        self.reconnect = callback

    def subscribe(self, **request):
        self.request = request
        if self.scenario == "subscribe_error":
            raise ValueError("fake-key must not be exposed")
        return 0

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def terminate(self):
        self.terminated = True

    def is_connected(self):
        return False

    def block_for_close(self, timeout):
        schema = self.request["schema"]
        meta = dict(dataset="XNAS.ITCH", start=pd.Timestamp(self.request["start"]).value,
                    stype_in=None, stype_out=dbn.SType.INSTRUMENT_ID, schema=None)
        if self.scenario == "wrong_start":
            meta["start"] += 10**9
        elif self.scenario == "wrong_dataset":
            meta["dataset"] = "EQUS.MINI"
        elif self.scenario == "wrong_metadata_schema":
            meta["schema"] = dbn.Schema.TRADES
        elif self.scenario == "wrong_metadata_symbol":
            meta["symbols"] = ["MSFT"]
        elif self.scenario == "unresolved":
            meta["not_found"] = ["AAPL"]
        elif self.scenario == "historical_end":
            meta["end"] = END.value
        self.stream.write(dbn.Metadata(**meta).encode())
        ack = dbn.SystemMsg(NOW.value, f"Subscription request 0 for {schema} data succeeded",
                            dbn.SystemCode.SUBSCRIPTION_ACK)
        completion = dbn.SystemMsg(NOW.value, f"Finished {schema} replay", dbn.SystemCode.REPLAY_COMPLETED)
        records = [ack, _mapping(), _bar(), completion]
        if self.scenario == "missing_completion":
            records.pop()
        elif self.scenario == "wrong_completion":
            records[-1] = dbn.SystemMsg(NOW.value, "Finished trades replay", dbn.SystemCode.REPLAY_COMPLETED)
        elif self.scenario == "wrong_ack_id":
            records[0] = dbn.SystemMsg(NOW.value, f"Subscription request 9 for {schema} data succeeded",
                                        dbn.SystemCode.SUBSCRIPTION_ACK)
        elif self.scenario == "error_after_ack":
            records = [ack, dbn.ErrorMsg(NOW.value, "Invalid start time")]
        elif self.scenario == "error_after_completion":
            records.append(dbn.ErrorMsg(NOW.value, "Records skipped"))
        elif self.scenario == "gateway_key_echo":
            records = [ack, dbn.ErrorMsg(NOW.value, "Rejected key fake-key")]
        elif self.scenario == "missing_mapping":
            records.pop(1)
        elif self.scenario == "wrong_symbol":
            records[1] = _mapping("MSFT")
        elif self.scenario == "wrong_mapping_type":
            records[1] = _mapping(stype_in=dbn.SType.PARENT)
        elif self.scenario == "wrong_mapping_id":
            records[1] = _mapping(output="8")
        elif self.scenario == "expired_mapping":
            records[1] = _mapping(start_ts=START.value, end_ts=START.value + 1)
        elif self.scenario == "raw_mapping":
            records[1] = _mapping(output="AAPL", stype_out=dbn.SType.RAW_SYMBOL)
        elif self.scenario == "duplicate_completion":
            records.append(completion)
        elif self.scenario == "duplicate_bar":
            records.insert(3, _bar())
        elif self.scenario == "wrong_rtype":
            records[2] = _bar(rtype=dbn.RType.OHLCV_1H)
        elif self.scenario == "unmapped_id":
            records[2] = _bar(instrument_id=8)
        elif self.scenario == "before_start":
            records[2] = _bar(START.value - 1)
        elif self.scenario == "post_window":
            records.insert(3, _bar(END.value))
        elif self.scenario == "only_post_window":
            records[2] = _bar(END.value)
        elif self.scenario == "empty":
            records.pop(2)
        elif self.scenario == "early_completion":
            records = [ack, completion, _mapping(), _bar()]
        elif self.scenario == "data_before_ack":
            records = [_mapping(), _bar(), ack, completion]
        elif self.scenario == "slow_warning":
            records.insert(3, dbn.SystemMsg(NOW.value, "Slow reader", dbn.SystemCode.SLOW_READER_WARNING))
        elif self.scenario in ("interval_markers", "interval_without_completion", "interval_without_data"):
            marker = dbn.SystemMsg(START.value + 2 * 60 * 10**9,
                                   "End of interval for ohlcv-1m", dbn.SystemCode.END_OF_INTERVAL)
            records = [ack, marker, _mapping(), _bar(), marker, completion]
            if self.scenario == "interval_without_completion":
                records.pop()
            elif self.scenario == "interval_without_data":
                records.pop(3)
        elif self.scenario == "reconnect":
            self.reconnect(NOW, NOW)
        elif self.scenario == "callback_exception":
            self.callback_error(ValueError("fake-key must not be exposed"))
        for record in records:
            try:
                self.stream.write(bytes(record))
            except Exception as exc:
                self.stream_error(exc)
            try:
                self.callback(record)
            except Exception as exc:
                self.callback_error(exc)
            if isinstance(record, dbn.SystemMsg) and record.code == dbn.SystemCode.END_OF_INTERVAL:
                self.stopped_after_interval.append(self.stopped)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(replay, "_utc_now", lambda: NOW)


def _request():
    return {"dataset": "XNAS.ITCH", "schema": "ohlcv-1m", "symbols": ["AAPL"],
            "stype_in": "raw_symbol", "start": START, "end": END}


def _capture(tmp_path: Path, *, scenario="success", **overrides):
    clients = []

    def factory(**kwargs):
        client = FakeClient(scenario=scenario, **kwargs)
        clients.append(client)
        return client

    arguments = dict(api_key="fake-key", schema="ohlcv-1m", symbols=["AAPL"],
                     start=START, end=END, raw_path=tmp_path / "native.dbn",
                     max_bytes=100000, client_factory=factory)
    arguments.update(overrides)
    return replay.capture_replay(**arguments), clients


@pytest.mark.parametrize("scenario", ["success", "raw_mapping"])
def test_complete_native_capture_and_independent_validation(tmp_path, scenario):
    delivery, clients = _capture(tmp_path, scenario=scenario)
    assert int(dbn.RType.OHLCV_1M) == 33
    assert delivery["data_record_count"] == 1
    assert delivery["record_count"] == 4
    assert delivery["raw_data_record_count"] == 1
    assert delivery["out_of_window_record_count"] == 0
    assert (tmp_path / "native.dbn").read_bytes().startswith(b"DBN")
    assert clients[0].kwargs["compression"] is db.Compression.ZSTD
    assert clients[0].kwargs["slow_reader_behavior"] == "warn"
    assert clients[0].kwargs["reconnect_policy"] == "none"
    assert clients[0].request["start"] == START.isoformat()
    assert clients[0].request["stype_in"] == "raw_symbol"
    assert clients[0].request["dataset"] == "XNAS.ITCH"
    assert clients[0].stopped and clients[0].terminated
    replay.validate_replay_delivery(delivery, _request(), tmp_path / "native.dbn")


def test_post_window_raw_retained_but_not_counted_as_target_coverage(tmp_path):
    delivery, _ = _capture(tmp_path, scenario="post_window")
    assert delivery["data_record_count"] == 1
    assert delivery["raw_data_record_count"] == 2
    assert delivery["out_of_window_record_count"] == 1
    assert delivery["first_record_timestamp_ns"] == _bar().ts_event
    assert delivery["last_record_timestamp_ns"] == _bar().ts_event
    replay.validate_replay_delivery(delivery, _request(), tmp_path / "native.dbn")


def test_end_of_interval_is_retained_without_stopping_or_completing_replay(tmp_path):
    delivery, clients = _capture(tmp_path, scenario="interval_markers")
    assert delivery["interval_marker_count"] == 2
    assert delivery["record_count"] == 6
    assert delivery["data_record_count"] == 1
    assert clients[0].stopped_after_interval == [False, False]
    assert len(delivery["replay_completed"]) == 1
    assert delivery["replay_completed"][0]["code"] == 3
    store = db.DBNStore.from_file(tmp_path / "native.dbn")
    try:
        markers = [record for record in store
                   if isinstance(record, dbn.SystemMsg) and record.code == dbn.SystemCode.END_OF_INTERVAL]
    finally:
        store.reader.close()
    assert len(markers) == 2
    assert all(record.msg == "End of interval for ohlcv-1m" for record in markers)
    replay.validate_replay_delivery(delivery, _request(), tmp_path / "native.dbn")
    changed = copy.deepcopy(delivery)
    changed["interval_marker_count"] = 1
    with pytest.raises(replay.ReplayCaptureError, match="counts"):
        replay.validate_replay_delivery(changed, _request(), tmp_path / "native.dbn")
    del changed["interval_marker_count"]
    with pytest.raises(replay.ReplayCaptureError, match="counts"):
        replay.validate_replay_delivery(changed, _request(), tmp_path / "native.dbn")


@pytest.mark.parametrize("scenario", ["interval_without_completion", "interval_without_data"])
def test_end_of_interval_cannot_replace_complete_nonempty_replay(tmp_path, scenario):
    with pytest.raises(replay.ReplayCaptureError):
        _capture(tmp_path, scenario=scenario)


def test_older_delivery_without_marker_count_requires_native_zero_markers(tmp_path):
    delivery, _ = _capture(tmp_path)
    del delivery["interval_marker_count"]
    replay.validate_replay_delivery(delivery, _request(), tmp_path / "native.dbn")


def test_interval_markers_survive_archive_publication_without_becoming_prices(tmp_path, monkeypatch):
    from datafetching import xnas_replay_archive as archive

    monkeypatch.setattr(archive, "_utc_now", lambda: NOW)
    item = archive.publish_session(tmp_path, symbol="AAPL", session="2026-09-08", api_key="fake-key",
        max_bytes=100000,
        capture_fn=lambda **kwargs: replay.capture_replay(**kwargs,
            client_factory=lambda **live_kwargs: FakeClient(scenario="interval_markers", **live_kwargs)))
    verified = archive.verify_partition(item["directory"], root=tmp_path)
    assert verified["delivery"]["interval_marker_count"] == 2
    assert verified["delivery"]["data_record_count"] == 1
    assert len(verified["frame"]) == 1
    assert verified["manifest"]["normalized"]["row_count"] == 1


@pytest.mark.parametrize("scenario", [
    "missing_completion", "wrong_completion", "wrong_ack_id", "error_after_ack",
    "error_after_completion", "missing_mapping", "wrong_symbol", "wrong_mapping_type",
    "wrong_mapping_id", "expired_mapping", "duplicate_completion", "duplicate_bar",
    "wrong_rtype", "unmapped_id", "before_start", "only_post_window", "empty",
    "early_completion", "data_before_ack", "slow_warning", "reconnect", "wrong_start",
    "wrong_dataset", "wrong_metadata_schema", "wrong_metadata_symbol", "unresolved", "historical_end",
])
def test_rejects_incomplete_or_ambiguous_delivery(tmp_path, scenario):
    with pytest.raises(replay.ReplayCaptureError):
        _capture(tmp_path, scenario=scenario)
    assert (tmp_path / "native.dbn").exists()


@pytest.mark.parametrize("scenario", ["callback_exception", "subscribe_error"])
def test_external_exception_text_is_sanitized(tmp_path, scenario):
    with pytest.raises(replay.ReplayCaptureError) as error:
        _capture(tmp_path, scenario=scenario)
    assert "ValueError" in str(error.value)
    assert "fake-key" not in str(error.value)


def test_gateway_error_redacts_key_text(tmp_path):
    with pytest.raises(replay.ReplayCaptureError) as error:
        _capture(tmp_path, scenario="gateway_key_echo")
    assert "ErrorMsg" in str(error.value)
    assert "fake-key" not in str(error.value)
    assert "Rejected key" not in str(error.value)


@pytest.mark.parametrize("message,category", [
    ("API key secret-one is not authorized for this dataset", "LIVE_ACCESS_DENIED"),
    ("User not entitled to XNAS.ITCH; secret-one", "LIVE_ACCESS_DENIED"),
    ("Authentication failed for secret-one", "LIVE_ACCESS_DENIED"),
    ("Permission denied; secret-one", "LIVE_ACCESS_DENIED"),
    ("A license is required; secret-one", "LIVE_ACCESS_DENIED"),
    ("A live data license is required to access XNAS.ITCH.", "LIVE_ACCESS_DENIED"),
    ("Missing entitlement; secret-one", "LIVE_ACCESS_DENIED"),
    ("Invalid API key secret-one", "LIVE_ACCESS_DENIED"),
    ("Account does not have a license; secret-one", "LIVE_ACCESS_DENIED"),
    ("Replay start is too old; secret-one", "REPLAY_RANGE_UNAVAILABLE"),
    ("Start time must be later than retained data; secret-one", "REPLAY_RANGE_UNAVAILABLE"),
    ("Request outside the replay window; secret-one", "REPLAY_RANGE_UNAVAILABLE"),
    ("Replay is no longer available; secret-one", "REPLAY_RANGE_UNAVAILABLE"),
    ("Connection timed out; secret-one", "LIVE_NETWORK_TIMEOUT"),
    ("Connection to private.internal:13000 timed out after 10 seconds; secret-one", "LIVE_NETWORK_TIMEOUT"),
    ("Authentication timed out after 10 seconds; secret-one", "LIVE_NETWORK_TIMEOUT"),
    ("Authentication with private.internal:13000 timed out after 10 seconds; secret-one", "LIVE_NETWORK_TIMEOUT"),
    ("Timeout while connecting; secret-one", "LIVE_NETWORK_TIMEOUT"),
    ("Timed out waiting for the gateway; secret-one", "LIVE_NETWORK_TIMEOUT"),
    ("Connection refused; secret-one", "LIVE_CONNECTION_FAILED"),
    ("Connection to private.internal:13000 failed: secret-one", "LIVE_CONNECTION_FAILED"),
    ("Unable to connect to private.internal; secret-one", "LIVE_CONNECTION_FAILED"),
    ("Getaddrinfo failed; secret-one", "LIVE_CONNECTION_FAILED"),
    ("Something unexpected with secret-one", None),
    ("Inspect authentication settings; secret-one", None),
    ("Review your license and entitlement; secret-one", None),
    ("Connection status unknown; secret-one", None),
    ("Invalid start time format; secret-one", None),
])
def test_external_provider_failure_retains_only_supported_category(tmp_path, message, category):
    class BentoError(Exception):
        pass

    def factory(**kwargs):
        raise BentoError(message)

    with pytest.raises(replay.ReplayCaptureError) as error:
        _capture(tmp_path, client_factory=factory)
    diagnostic = str(error.value)
    assert diagnostic == "Live replay failed: BentoError" + (f" [{category}]" if category else "")
    rendered = "".join(traceback.format_exception(error.value))
    assert "secret-one" not in rendered
    assert "private.internal" not in rendered


def test_explicit_cause_can_supply_category_without_rendering_secret(tmp_path):
    class BentoError(Exception):
        pass

    def factory(**kwargs):
        try:
            raise ValueError("Not entitled; secret-one")
        except ValueError as error:
            raise BentoError("The connection failed; secret-two") from error

    with pytest.raises(replay.ReplayCaptureError) as error:
        _capture(tmp_path, client_factory=factory)
    assert str(error.value) == "Live replay failed: BentoError [LIVE_ACCESS_DENIED]"
    rendered = "".join(traceback.format_exception(error.value))
    assert "secret-one" not in rendered and "secret-two" not in rendered


def test_callback_preserves_allowlisted_category(tmp_path):
    class DeniedClient(FakeClient):
        def block_for_close(self, timeout):
            self.callback_error(ValueError("Authentication failed; secret-one"))

    with pytest.raises(replay.ReplayCaptureError) as error:
        _capture(tmp_path, client_factory=DeniedClient)
    assert "ValueError [LIVE_ACCESS_DENIED]" in str(error.value)
    assert "secret-one" not in str(error.value)


@pytest.mark.parametrize("via_archive", [False, True])
def test_observed_live_license_rejection_preserves_failed_raw_without_publication(tmp_path, monkeypatch, via_archive):
    from databento.common.error import BentoError
    from datafetching import xnas_replay_archive as archive

    clients = []

    class LicenseDeniedClient(FakeClient):
        def subscribe(self, **request):
            self.request = request
            raise BentoError("A live data license is required to access XNAS.ITCH.")

    def factory(**kwargs):
        client = LicenseDeniedClient(**kwargs)
        clients.append(client)
        return client

    with pytest.raises(replay.ReplayCaptureError) as error:
        if via_archive:
            monkeypatch.setattr(archive, "_utc_now", lambda: NOW)
            archive.publish_session(tmp_path, symbol="AAPL", session="2026-09-08", api_key="fake-key",
                max_bytes=100000,
                capture_fn=lambda **kwargs: replay.capture_replay(**kwargs, client_factory=factory))
        else:
            _capture(tmp_path, client_factory=factory)
    assert str(error.value) == "Live replay failed: BentoError [LIVE_ACCESS_DENIED]"
    assert len(clients) == 1 and clients[0].terminated
    raw_files = list(tmp_path.rglob("*.dbn"))
    assert len(raw_files) == 1 and raw_files[0].stat().st_size == 0
    assert not list(tmp_path.rglob("normalized.parquet"))
    assert not list(tmp_path.rglob("manifest.json"))
    assert not list(tmp_path.rglob("receipt.json"))
    assert archive.discover_partitions(tmp_path, symbols=["AAPL"]) == []


def test_byte_budget_stops_before_excess_write(tmp_path):
    with pytest.raises(replay.ReplayCaptureError, match="byte budget"):
        _capture(tmp_path, max_bytes=150)
    assert (tmp_path / "native.dbn").stat().st_size <= 150


def test_short_write_is_failure(tmp_path):
    class ShortWriter:
        def write(self, data):
            return len(data) - 1

        def close(self):
            pass

    stream = replay._CappedStream(tmp_path / "short.dbn", 100)
    stream._stream.close()
    stream._stream = ShortWriter()
    with pytest.raises(replay.ReplayCaptureError, match="short write"):
        stream.write(b"abc")


def test_existing_raw_is_preserved(tmp_path):
    path = tmp_path / "native.dbn"
    path.write_bytes(b"original")
    with pytest.raises(FileExistsError):
        _capture(tmp_path)
    assert path.read_bytes() == b"original"


@pytest.mark.parametrize("overrides", [
    {"start": 0}, {"start": None}, {"start": NOW - pd.Timedelta(hours=24, nanoseconds=1)},
    {"end": NOW + pd.Timedelta(seconds=1)}, {"end": START},
    {"symbols": ["AAPL", "COST"]}, {"symbols": "ALL_SYMBOLS"},
    {"symbols": "AAPL.OPT"}, {"symbols": [42]}, {"symbols": None},
    {"schema": "trades"}, {"timeout_seconds": float("inf")}, {"timeout_seconds": True},
    {"timeout_seconds": "300"}, {"max_bytes": 0}, {"max_bytes": True},
])
def test_invalid_scope_fails_before_any_capture(tmp_path, overrides):
    with pytest.raises(replay.ReplayCaptureError):
        _capture(tmp_path, **overrides)
    assert not (tmp_path / "native.dbn").exists()


def test_exact_24_hour_source_allowed(tmp_path):
    delivery, _ = _capture(tmp_path, start=NOW - pd.Timedelta(hours=24))
    assert delivery["completed"] is True


def test_tampered_raw_or_claimed_counts_are_rejected(tmp_path):
    delivery, _ = _capture(tmp_path)
    changed = copy.deepcopy(delivery)
    changed["data_record_count"] = 2
    with pytest.raises(replay.ReplayCaptureError, match="counts"):
        replay.validate_replay_delivery(changed, _request(), tmp_path / "native.dbn")
    with (tmp_path / "native.dbn").open("ab") as stream:
        stream.write(b"bad")
    with pytest.raises(replay.ReplayCaptureError, match="checksum/size"):
        replay.validate_replay_delivery(delivery, _request(), tmp_path / "native.dbn")
    changed = copy.deepcopy(delivery)
    changed["raw_bytes"] = (tmp_path / "native.dbn").stat().st_size
    changed["raw_sha256"] = replay._checksum(tmp_path / "native.dbn")
    with pytest.raises(replay.ReplayCaptureError, match="DBN verification"):
        replay.validate_replay_delivery(changed, _request(), tmp_path / "native.dbn")


@pytest.mark.parametrize("field,delta", [("start", 1), ("start", -1), ("end", 1), ("end", -1)])
def test_request_cannot_narrow_or_expand_captured_interval(tmp_path, field, delta):
    delivery, _ = _capture(tmp_path)
    request = _request()
    request[field] += pd.Timedelta(seconds=delta)
    with pytest.raises(replay.ReplayCaptureError, match="bounds"):
        replay.validate_replay_delivery(delivery, request, tmp_path / "native.dbn")


def test_silent_sdk_timeout_is_failure_even_with_completion(tmp_path, monkeypatch):
    times = iter((0.0, 0.0, 301.0))
    monkeypatch.setattr(replay.time, "monotonic", lambda: next(times))
    with pytest.raises(replay.ReplayCaptureError, match="timeout"):
        _capture(tmp_path)
