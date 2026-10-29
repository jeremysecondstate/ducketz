"""Context failure observations stay bounded and cannot alter existing1A retry."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import socket
import sqlite3

from filelock import FileLock, Timeout, _api, _error
import pytest

from ml.intraday import live_session, market_collection as collection, scout_services
from tests.test_scout_services import data_case


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Context diagnostic fixture reached the network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket.socket, "sendto", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def collector_case(tmp_path):
    now = datetime(2026, 10, 19, 19, 0, 10, tzinfo=timezone.utc)
    calls = []
    def adapter(*args):
        calls.append(1)
        raise AssertionError("Contended queue must not call its provider")
    request = collection.SourceRequest(provider="fmp", source_kind="stock_quote", symbol="DOCU",
        dataset="stable", schema="quote", endpoint="quote", params={"symbol": "DOCU"})
    col = collection.QuarterHourMarketCollector(tmp_path / "private-collector", symbols=["DOCU"],
        requests=[request], adapters={"fmp": adapter}, clock=lambda: now, retry_seconds=.02)
    col.lock.timeout = .01
    col.enqueue(now - timedelta(minutes=15, seconds=10))
    return col, calls


def detail(error, *, worker="context", collector=None):
    return scout_services._data_worker_retry_detail(worker, error, 1, pc="Scout",
        source_session="offline-context", release={"manifest_sha256": "offline-manifest"},
        collector=collector)


@pytest.mark.parametrize("duplicate", [False, True])
def test_native_timeout_identity_is_retained_without_changing_error_or_queue(collector_case, monkeypatch, duplicate):
    col, calls = collector_case
    before = col.state_path.read_bytes()
    if duplicate:
        spec = importlib.util.spec_from_file_location("_offline_context_duplicate_timeout", _error.__file__)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setattr(_api, "Timeout", module.Timeout)
    with FileLock(col.lock.lock_file, timeout=0):
        with pytest.raises(TimeoutError) as caught:
            col.step(max_requests=1)
        error = caught.value
        trace, args, attributes = error.__traceback__, error.args, dict(error.__dict__)
        saved = detail(error, collector=col)
        observed = saved["diagnostics"]["market_collection"]
        assert observed == collection.collection_failure_diagnostics(col, error)
        assert observed["collector_phase"] == "step_queue_lock_acquire"
        assert observed["handler_timeout_matches_error"] is (not duplicate)
        assert observed["handler_timeout_matches_native_api"] is (not duplicate)
        assert observed["error_matches_native_api_timeout"] is True
        assert observed["queue_lock_path_matches"] is True
        assert len(observed["collector_run_code_sha256"]) == 64
        assert len(observed["collector_step_code_sha256"]) == 64
        assert error.__traceback__ is trace and error.args == args and error.__dict__ == attributes
        assert saved["error_type"] == "Timeout" and saved["message"] == str(error)
        assert saved["diagnostics"]["schema_version"] == "intraday-symbol-exception-v1"
    assert col.state_path.read_bytes() == before and calls == []
    assert str(col.root) not in json.dumps(observed) and "queue.lock" not in json.dumps(observed)


@pytest.mark.parametrize("failure", [OSError("private diagnostic"), KeyboardInterrupt("private diagnostic"),
                                     SystemExit("private diagnostic")])
def test_optional_collection_failure_cannot_mask_original_generic_detail(collector_case, monkeypatch, failure):
    col, _ = collector_case
    error = RuntimeError("original context operation")
    baseline = detail(error)
    def broken(*args):
        raise failure
    monkeypatch.setattr(collection, "collection_failure_diagnostics", broken)
    assert detail(error, collector=col) == baseline
    assert error.args == ("original context operation",)
    assert "private diagnostic" not in json.dumps(baseline)


@pytest.mark.parametrize("worker", ["acquisition", "scoring", "other"])
def test_non_context_workers_keep_original_generic_schema(collector_case, monkeypatch, worker):
    col, _ = collector_case
    def forbidden(*args):
        raise AssertionError("Non-context worker inspected a collector")
    monkeypatch.setattr(collection, "collection_failure_diagnostics", forbidden)
    saved = detail(RuntimeError("original worker"), worker=worker, collector=col)
    assert set(saved["diagnostics"]) == {"schema_version", "exception_chain", "chain_truncated"}
    assert saved["worker"] == worker and saved["attempt"] == 1


def test_missing_generic_diagnostics_preserve_original_unavailable_record(collector_case, monkeypatch):
    from ml.intraday import runtime
    col, _ = collector_case
    def unavailable(*args):
        raise ValueError("generic observer unavailable")
    monkeypatch.setattr(runtime, "_symbol_exception_diagnostics", unavailable)
    saved = detail(RuntimeError("original operation"), collector=col)
    assert saved["diagnostics_unavailable"] is True and "diagnostics" not in saved
    assert saved["message"] == "original operation"


def test_actual_context_outer_retry_receives_its_own_collector(tmp_path, monkeypatch):
    case = data_case(tmp_path, monkeypatch)
    seen = []
    original = RuntimeError("context operation failed")
    class Collector:
        attempts = 0
        def run(self, *, stopped, pause, **kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise original
            while not stopped():
                pause(.002)
    col = Collector()
    monkeypatch.setattr(live_session, "prepare_market_collection", lambda *args, **kwargs: col)
    def observe(collector, error):
        seen.append((collector, error))
        return {"schema_version": "offline-collection-detail", "observer_error": False}
    monkeypatch.setattr(collection, "collection_failure_diagnostics", observe)
    result = scout_services.run_data_service(repository=tmp_path, datastore=tmp_path,
        release=case.release, clock=lambda: case.now[0], sleep=case.sleep)
    path = tmp_path / "intraday/services/handoff.sqlite3"
    with sqlite3.connect(path) as db:
        rows = [json.loads(raw) for raw, in db.execute(
            "SELECT payload FROM recovery WHERE kind='data_worker_retry'")]
    assert len(rows) == 1
    saved = rows[0]
    assert saved["diagnostics"]["market_collection"] == {
        "schema_version": "offline-collection-detail", "observer_error": False}
    assert seen == [(col, original)]
    assert saved["message"] == "context operation failed" and saved["error_type"] == "RuntimeError"
    assert saved["worker"] == "context" and saved["attempt"] == 1
    assert saved["role"] == "data" and saved["pc"] == "Scout"
    assert saved["manifest_sha256"] == case.release["manifest_sha256"]
    assert saved["source_session"] == result["source_session"]
    status = json.loads((path.parent / "data-status.json").read_bytes())
    assert status["errors"] == rows and status["deadline"] == case.deadline.isoformat()
    assert set(case.stats.published) == set(case.symbols)
    assert result["status"] == "FAILED"  # Existing error history remains visible.
    assert not (tmp_path / "intraday/scout/controller.json").exists()
