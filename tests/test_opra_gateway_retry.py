"""Offline contracts for persistent OPRA requests and immutable partial files."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from requests.exceptions import ReadTimeout

from datafetching import databento_opra_history as native
from ml.artifacts import file_checksum

pytestmark = pytest.mark.usefixtures("offline_databento_sdk")


def test_sdk_timeout_configuration_is_local_to_each_client(offline_databento_sdk):
    from databento.common.http import BentoHttpAPI

    first = offline_databento_sdk.Historical("SYNTHETIC_TEST_KEY")
    second = offline_databento_sdk.Historical("SYNTHETIC_TEST_KEY")
    assert BentoHttpAPI.TIMEOUT == first.metadata.TIMEOUT == second.metadata.TIMEOUT == 100
    first.metadata.TIMEOUT = 7
    native.configure_client(first)
    assert first.metadata.__dict__["TIMEOUT"] == 100
    assert first.timeseries.__dict__["TIMEOUT"] == 300
    assert "TIMEOUT" not in second.metadata.__dict__
    assert "TIMEOUT" not in second.timeseries.__dict__
    assert second.metadata.TIMEOUT == second.timeseries.TIMEOUT == BentoHttpAPI.TIMEOUT == 100
    native.configure_client(second)
    first.metadata.TIMEOUT = 8
    first.timeseries.TIMEOUT = 9
    assert second.metadata.TIMEOUT == 100 and second.timeseries.TIMEOUT == 300
    assert BentoHttpAPI.TIMEOUT == 100


@pytest.mark.parametrize("symbols", [("AMZN.OPT",), ("GOOG.OPT", "NVDA.OPT"), ()])
def test_preflight_retry_labels_bind_exact_requests_without_changing_gates(
    tmp_path, monkeypatch, capsys, symbols
):
    calls, pauses = {}, []
    monkeypatch.setattr(native.time, "sleep", pauses.append)
    # Deliberately insufficient capacity: logging must not turn this into a pass.
    monkeypatch.setattr(native.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    expected = {"dataset": "OPRA.PILLAR", "schema": "ohlcv-1h",
                "start": "2026-09-27", "end": "2026-10-03",
                "symbols": list(symbols) if symbols else "ALL_SYMBOLS",
                "stype_in": "parent" if symbols else "raw_symbol"}

    def response(name, value):
        def request(**kwargs):
            calls.setdefault(name, []).append(copy.deepcopy(kwargs))
            if len(calls[name]) == 1:
                raise ReadTimeout("SYNTHETIC_SECRET_BODY")
            return value
        return request

    client = SimpleNamespace(metadata=SimpleNamespace(
        get_billable_size=response("estimated download size", 125),
        get_record_count=response("record count", 12),
        get_cost=response("estimated cost", 0.25)))
    result = native.storage_preflight(client, datastore_root=tmp_path,
        entitlement={"entitlements": {"ohlcv-1h": {
            "entitled_start": "2026-09-27", "entitled_end": "2026-10-03"}}},
        scope=native.SyncScope(schemas=("ohlcv-1h",), start=expected["start"],
                               end=expected["end"], symbols=symbols))
    messages = capsys.readouterr().out.splitlines()
    assert len(messages) == 6 and pauses == [4.0] * 3
    for operation in calls:
        scoped = [message for message in messages if f"{operation} request=" in message]
        assert len(scoped) == 2
        assert "attempt 1 failed" in scoped[0] and "succeeded on attempt 2" in scoped[1]
        for message in scoped:
            identity, _ = json.JSONDecoder().raw_decode(message.split(" request=", 1)[1])
            assert identity == expected
        assert calls[operation] == [expected, expected]
    assert "SYNTHETIC_SECRET_BODY" not in "\n".join(messages)
    assert result["record_count"] == 12 and result["estimated_download_size_bytes"] == 125
    assert result["estimated_cost_usd"] == 0.25 and result["cost_estimates_complete"] is True
    assert result["required_free_bytes"] == 250 + native.STORAGE_RESERVE_BYTES
    assert result["capacity_pass"] is False


def test_partition_count_retry_label_uses_exact_interval(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(native.time, "sleep", lambda _: None)

    def count(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        if len(calls) == 1:
            raise ReadTimeout("synthetic timeout")
        return 1

    result = native._partition_time_segments(
        SimpleNamespace(metadata=SimpleNamespace(get_record_count=count)),
        schema="cbbo-1s", day="2026-10-02", symbols=("COST.OPT",))
    assert result == [("2026-10-02T00:00:00+00:00", "2026-10-03T00:00:00+00:00", None)]
    assert len(calls) == 2 and calls[0] == calls[1]
    message = capsys.readouterr().out.strip()
    assert "time-partition record count request=" in message
    assert json.loads(message.split(" request=", 1)[1].split(" attempt ", 1)[0]) == calls[0]


def BentoServerError(*args, **kwargs):
    from databento.common.error import BentoServerError as Error
    return Error(*args, **kwargs)


def BentoClientError(*args, **kwargs):
    from databento.common.error import BentoClientError as Error
    return Error(*args, **kwargs)


def BentoError(*args, **kwargs):
    from databento.common.error import BentoError as Error
    return Error(*args, **kwargs)


@pytest.mark.parametrize("failure_kind", ["504", "read_timeout"])
def test_metadata_default_persists_beyond_old_budgets(monkeypatch, failure_kind):
    failure = BentoServerError(504) if failure_kind == "504" else ReadTimeout("synthetic timeout")
    pauses, calls, messages = [], [], []
    monkeypatch.setattr(native.time, "sleep", pauses.append)

    def request(**kwargs):
        calls.append(kwargs)
        if len(calls) <= 80:
            raise failure
        return 17

    assert native._retry(request, kwargs={"dataset": "OPRA.PILLAR"},
                         operation="record count", reporter=messages.append) == 17
    assert len(calls) == 81
    assert pauses == [4.0] * 80
    assert all(call == {"dataset": "OPRA.PILLAR"} for call in calls)


def test_explicit_finite_override_preserves_final_cause(monkeypatch):
    pauses, calls = [], []
    failure = BentoServerError(503)
    monkeypatch.setattr(native.time, "sleep", pauses.append)

    def request(**kwargs):
        calls.append(kwargs)
        raise failure

    with pytest.raises(native.OpraSyncError) as captured:
        native._retry(request, kwargs={}, operation="finite request", maximum_attempts=2)
    assert captured.value.__cause__ is failure
    assert len(calls) == 2
    assert pauses == [4.0]


@pytest.mark.parametrize("failure_kind", [400, 401, 403, 404, 422, 429, "parser", "file", "sdk_parser"])
def test_local_permanent_and_rate_limit_errors_do_not_retry(monkeypatch, failure_kind):
    failure = (
        BentoClientError(failure_kind, message="read timed out HTTP 504")
        if isinstance(failure_kind, int) else {
            "parser": ValueError("HTTP 504 local parser failure"),
            "file": FileExistsError("existing immutable file"),
            "sdk_parser": BentoError("Error streaming response: invalid parser record"),
        }[failure_kind]
    )
    pauses, calls, callbacks = [], [], []
    monkeypatch.setattr(native.time, "sleep", pauses.append)

    def request(**kwargs):
        calls.append(kwargs)
        raise failure

    with pytest.raises(native.OpraSyncError) as captured:
        native._retry(request, kwargs={}, operation="request",
                      before_retry=lambda *args: callbacks.append(args))
    assert captured.value.__cause__ is failure
    assert len(calls) == 1
    assert not pauses and not callbacks


def test_retry_after_is_honored_without_old_pause_ceiling(monkeypatch):
    pauses, calls = [], []
    monkeypatch.setattr(native.time, "sleep", pauses.append)

    def request(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise BentoServerError(503, headers={"Retry-After": "301"})
        return "recovered"

    assert native._retry(request, kwargs={}, operation="request") == "recovered"
    assert pauses == [301.0]


@pytest.mark.parametrize("maximum_attempts", [0, -1, True, False, 1.5, "3"])
def test_invalid_attempt_override_makes_no_request(maximum_attempts):
    calls = []
    with pytest.raises(ValueError):
        native._retry(lambda **kwargs: calls.append(kwargs), kwargs={},
                      operation="request", maximum_attempts=maximum_attempts)
    assert not calls


def test_retry_callback_order_scope_and_sanitized_messages(monkeypatch):
    events, calls, messages = [], [], []
    kwargs = {"dataset": "OPRA.PILLAR", "schema": "cbbo-1m", "symbols": ["DOCU.OPT"]}
    original = copy.deepcopy(kwargs)
    failure = BentoServerError(504, message="SYNTHETIC_SECRET_BODY")
    monkeypatch.setattr(native.time, "sleep", lambda delay: events.append("sleep"))

    def request(**received):
        calls.append(copy.deepcopy(received))
        events.append("request")
        if len(calls) == 1:
            raise failure
        return "ok"

    def before_retry(exc, attempt):
        assert exc is failure and attempt == 1
        events.append("callback")

    assert native._retry(request, kwargs=kwargs, operation="scoped request",
                         before_retry=before_retry, reporter=messages.append) == "ok"
    assert events == ["request", "callback", "sleep", "request"]
    assert calls == [original, original] and kwargs == original
    assert "SYNTHETIC_SECRET_BODY" not in "\n".join(messages)


def test_local_callback_error_stops_before_sleep(monkeypatch):
    pauses, calls = [], []
    monkeypatch.setattr(native.time, "sleep", pauses.append)
    local = FileExistsError("cannot preserve partial")

    def request(**kwargs):
        calls.append(kwargs)
        raise BentoServerError(504)

    def before_retry(*args):
        raise local

    with pytest.raises(native.OpraSyncError) as captured:
        native._retry(request, kwargs={}, operation="download", before_retry=before_retry)
    assert captured.value.__cause__ is local
    assert len(calls) == 1 and not pauses


def test_keyboard_interrupt_stops_persistent_wait(monkeypatch):
    calls = []
    interruption = KeyboardInterrupt()

    def interrupt(_seconds):
        raise interruption

    def request(**kwargs):
        calls.append(kwargs)
        raise BentoServerError(504)

    monkeypatch.setattr(native.time, "sleep", interrupt)
    with pytest.raises(KeyboardInterrupt) as captured:
        native._retry(request, kwargs={}, operation="download")
    assert captured.value is interruption and len(calls) == 1


class _SyntheticDBNStore:
    def __init__(self, day):
        self.day = day

    def to_parquet(self, path, *, map_symbols):
        assert map_symbols is True
        start = pd.Timestamp(self.day, tz="UTC")
        pd.DataFrame({
            "ts_event": [start + pd.Timedelta(hours=13), start + pd.Timedelta(hours=14)],
            "publisher_id": [1, 1], "instrument_id": [12345, 12345],
            "symbol": ["SPY   260320C00600000"] * 2,
            "open": [1.0, 1.25], "high": [1.5, 1.75], "low": [0.75, 1.0],
            "close": [1.25, 1.5], "volume": [10, 12],
        }).to_parquet(path, index=False)


def test_many_stream_retries_preserve_open_partials_and_publish_only_success(tmp_path, monkeypatch):
    requests, retained_handles, pauses = [], [], []
    day = "2026-01-01"
    successful_raw = b"synthetic-complete-dbn-fixture"
    monkeypatch.setattr(native.time, "sleep", pauses.append)

    class TimeSeries:
        def get_range(self, **kwargs):
            requests.append(dict(kwargs))
            path = Path(kwargs["path"])
            attempt = len(requests)
            if attempt <= 10:
                handle = path.open("x+b")
                handle.write(f"partial-{attempt}".encode())
                handle.flush()
                retained_handles.append(handle)
                raise ReadTimeout("synthetic remote read timeout")
            assert all(not handle.closed for handle in retained_handles)
            with path.open("x+b") as handle:
                handle.write(successful_raw)
            return _SyntheticDBNStore(day)

    try:
        manifest = native._download_partition(
            SimpleNamespace(timeseries=TimeSeries()), datastore_root=tmp_path,
            entitlement={"entitlements": {"ohlcv-1h": {
                "entitled_start": day, "entitled_end": "2026-01-02"}}},
            schema="ohlcv-1h", day=day, symbols=("SPY.OPT",))
        assert len(requests) == 11 and pauses == [4.0] * 10
        attempted_paths = [Path(request["path"]) for request in requests]
        assert len(set(attempted_paths)) == 11
        assert all({k: v for k, v in request.items() if k != "path"} == manifest["request"]
                   for request in requests)
        destination = native.partition_directory(tmp_path, schema="ohlcv-1h", day=day,
                                                  symbols=("SPY.OPT",))
        verified = native.verify_partition(destination, datastore_root=tmp_path)
        assert verified["manifest"] == manifest
        assert manifest["normalized"]["row_count"] == 2
        assert (destination / "provider.dbn.zst").read_bytes() == successful_raw
        assert verified["receipt"]["raw_checksum_sha256"] == file_checksum(destination / "provider.dbn.zst")
        for index, path in enumerate(attempted_paths[:-1], start=1):
            assert path.read_bytes() == f"partial-{index}".encode()
            assert not (path.parent / "receipt.json").exists()
        assert not attempted_paths[-1].parent.exists()
    finally:
        for handle in retained_handles:
            handle.close()
