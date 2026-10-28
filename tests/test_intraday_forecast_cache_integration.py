"""Offline forecast-cache reports preserve source validation and recovery results."""
import copy
import hashlib
import importlib.util
from pathlib import Path
import socket
import sqlite3

import pytest

# Portable publication may precede consumer adoption. Report that gap before
# importing local-only consumer fixtures; full Atlas checks exercise every case.
_REQUIRED = ("ml.intraday.forecast_header_cache", "ml.intraday.intentions",
             "ml.intraday.prediction", "ml.intraday.store")
def _module_available(name):
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False

if (not all(_module_available(name) for name in _REQUIRED)
        or not Path(__file__).with_name("test_intraday_durable_forecast_batch.py").is_file()):
    pytest.skip("Full forecast-cache consumer fixture unavailable on this base", allow_module_level=True)

from ml.intraday import forecast_header_cache as cache_module
from ml.intraday import intentions as intentions_module
from ml.intraday import prediction as prediction_module
from ml.intraday import store as store_module
if (not hasattr(intentions_module, "_RecoveryTimings")
        or not hasattr(intentions_module._RecoveryTimings, "forecast_cache_read")
        or not hasattr(store_module, "ShadowStore")
        or not hasattr(store_module.ShadowStore, "_revision_forecast_headers")):
    pytest.skip("Forecast-cache consumer diagnostics not installed on this base", allow_module_level=True)
DurableForecastHeaders = cache_module.DurableForecastHeaders
_RecoveryTimings = intentions_module._RecoveryTimings
ContractError = prediction_module.ContractError
ShadowStore = store_module.ShadowStore
from test_intraday_durable_forecast_batch import populate, headers, decoded


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Forecast diagnostic integration must remain offline")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(DurableForecastHeaders, "_protect_secret",
                        staticmethod(lambda raw, **kwargs: raw))


def read(store, timings=None, *, budget=.25, unpin=False):
    with store._execution_forecast_lock, store.connect() as db:
        db.execute("BEGIN")
        if unpin:
            store._connection_source_identities.pop(id(db), None)
        return store._revision_forecast_headers(db, ["f.pc=?"], ["Atlas"],
            read_timings=timings, cache_io_budget_seconds=budget)


def stats(timings):
    return timings.snapshot()["read_details"]["forecast_cache"]


def report(reason="complete", *, count=4, returned=4, code=None):
    return dict(schema_version="forecast-cache-load-outcome-v1", reason=reason,
        requested_count=count, expected_count=count, validated_count=returned,
        returned_count=returned, discarded_count=0, rejected_count=0,
        sqlite_errorcode=code)


def test_cold_complete_report_and_warm_no_read_preserve_headers(tmp_path, monkeypatch):
    store, _ = populate(tmp_path / "science.sqlite3")
    expected = headers(store)
    cold = ShadowStore(store.path)
    reads = decoded(cold, monkeypatch)
    timings = _RecoveryTimings()
    assert read(cold, timings) == expected and reads == []
    got = stats(timings)
    assert got["outcomes"] == {"complete": 1}
    assert got["requested_count"] == got["returned_count"] == 4
    monkeypatch.setattr(DurableForecastHeaders, "load_many",
                        lambda *a, **kw: pytest.fail("Warm inventory attempted disk cache"))
    warm = _RecoveryTimings()
    assert read(cold, warm) == expected and stats(warm)["attempts"] == 0
    # Returned snapshots cannot mutate either accumulator.
    got["outcomes"]["complete"] = 90
    assert stats(timings)["outcomes"] == {"complete": 1}
    assert timings.snapshot()["read_details"]["sizing_cache"]["attempts"] == 0


@pytest.mark.parametrize("case,reason", [("missing_file", "missing_file"),
    ("missing_key", "missing_key"), ("malformed", "sqlite_error"),
    ("disabled", "budget_disabled")])
def test_optional_failures_report_and_revalidate_same_originals(tmp_path, monkeypatch, case, reason):
    store, _ = populate(tmp_path / "science.sqlite3")
    expected = headers(store)
    helper = DurableForecastHeaders(store.path)
    if case == "missing_file":
        helper.path.unlink()
    elif case == "missing_key":
        helper.key_path.unlink()
    elif case == "malformed":
        helper.path.write_bytes(b"synthetic malformed optional cache")
    source_hash = hashlib.sha256(store.path.read_bytes()).digest()
    cold = ShadowStore(store.path)
    reads = decoded(cold, monkeypatch)
    timings = _RecoveryTimings()
    assert read(cold, timings, budget=0 if case == "disabled" else .25) == expected
    assert len(reads) == 4 and stats(timings)["outcomes"] == {reason: 1}
    assert stats(timings)["returned_count"] == 0
    assert hashlib.sha256(store.path.read_bytes()).digest() == source_hash


@pytest.mark.parametrize("case,reason", [("unpin", "source_unpinned"),
                                        ("resolve", "identity_unavailable")])
def test_bypassed_cache_is_distinguished_and_uses_originals(tmp_path, monkeypatch, case, reason):
    store, _ = populate(tmp_path / "science.sqlite3")
    expected = headers(store)
    cold = ShadowStore(store.path)
    reads = decoded(cold, monkeypatch)
    if case == "resolve":
        resolve = Path.resolve
        def unavailable(path, *a, **kw):
            if path == cold.path:
                raise OSError("synthetic private path unavailable")
            return resolve(path, *a, **kw)
        monkeypatch.setattr(Path, "resolve", unavailable)
    monkeypatch.setattr(DurableForecastHeaders, "load_many",
                        lambda *a, **kw: pytest.fail("Unpinned source attempted disk cache"))
    timings = _RecoveryTimings()
    assert read(cold, timings, unpin=case == "unpin") == expected and len(reads) == 4
    got = stats(timings)
    assert got["outcomes"] == {reason: 1} and got["requested_count"] == 4
    assert got["returned_count"] == got["expected_count"] == 0


def test_partial_authenticated_result_revalidates_only_unread_originals(tmp_path, monkeypatch):
    store, _ = populate(tmp_path / "science.sqlite3")
    expected = headers(store)
    load = DurableForecastHeaders.load_many
    def partial(self, identity, signatures, *, diagnostics=None):
        complete = load(self, identity, signatures)
        retained = dict(list(complete.items())[:1])
        assert len(retained) == 1
        if diagnostics is not None:
            diagnostics.update(report("sqlite_interrupt", returned=1, code=sqlite3.SQLITE_INTERRUPT))
        return retained
    monkeypatch.setattr(DurableForecastHeaders, "load_many", partial)
    cold = ShadowStore(store.path)
    reads = decoded(cold, monkeypatch)
    timings = _RecoveryTimings()
    assert read(cold, timings) == expected and len(reads) == 3
    got = stats(timings)
    assert got["outcomes"] == {"sqlite_interrupt": 1}
    assert got["returned_count"] == 1 and got["sqlite_interrupt_count"] == 1


def test_reporting_failure_cannot_change_headers_or_source_errors(tmp_path, monkeypatch):
    class Failing(_RecoveryTimings):
        def forecast_cache_read(self, value):
            raise RuntimeError("synthetic diagnostic sink failure")
    store, values = populate(tmp_path / "science.sqlite3")
    expected = headers(store)
    assert read(ShadowStore(store.path), Failing()) == expected
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE forecasts SET payload='{}' WHERE id=?", (values[0]["forecast_id"],))
    with pytest.raises(ContractError, match="hash or indexed identity"):
        read(ShadowStore(store.path), Failing())


def test_no_timings_keeps_original_cache_call_signature(tmp_path, monkeypatch):
    store, _ = populate(tmp_path / "science.sqlite3")
    expected = headers(store)
    original = DurableForecastHeaders.load_many
    calls = []
    def plain(self, identity, signatures):
        calls.append(len(signatures))
        return original(self, identity, signatures)
    monkeypatch.setattr(DurableForecastHeaders, "load_many", plain)
    assert read(ShadowStore(store.path)) == expected and calls == [4]


@pytest.mark.parametrize("damage", ["schema", "reason", "count", "extra", "code"])
def test_invalid_reports_do_not_pollute_bounded_accumulator(damage):
    timings = _RecoveryTimings()
    value = report()
    value.update({"schema": {"schema_version": "unknown"}, "reason": {"reason": "PRIVATE"},
        "count": {"returned_count": -1}, "extra": {"private": "secret"},
        "code": {"sqlite_errorcode": True}}[damage])
    before = copy.deepcopy(stats(timings))
    timings.forecast_cache_read(value)
    assert stats(timings) == before and timings.read_unavailable


def test_aggregate_limits_and_extended_sqlite_code_keep_sizing_separate():
    timings = _RecoveryTimings()
    timings.forecast_cache_read(report("sqlite_error", count=2147483647, returned=0,
                                      code=sqlite3.SQLITE_BUSY | 256))
    timings.forecast_cache_read(report("sqlite_error", count=4, returned=0,
                                      code=sqlite3.SQLITE_LOCKED))
    got = stats(timings)
    assert got["requested_count"] == 2147483647
    assert got["sqlite_busy_count"] == got["sqlite_locked_count"] == 1
    assert got["outcomes"] == {"sqlite_error": 2}
    assert timings.snapshot()["read_details"]["sizing_cache"]["attempts"] == 0
