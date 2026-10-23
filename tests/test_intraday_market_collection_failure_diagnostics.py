"""Native private locks and duplicate-class diagnosis; no live data or network."""
import importlib.util
import json
import os
from pathlib import Path

from filelock import FileLock, Timeout, _api, _error
import pytest

from ml.intraday import market_collection as collection, live_session
from ml.intraday.store import ShadowStore
from tests.test_intraday_market_collection_lock_retry import case, task
from tests.test_intraday_live_session import (scout_manual_fixture,
    isolate_minute_acquisition_in_execution_fixtures)


def duplicate_timeout(monkeypatch):
    spec = importlib.util.spec_from_file_location("_offline_duplicate_timeout", _error.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(_api, "Timeout", module.Timeout)
    return module.Timeout


def contend_at_step(col, monkeypatch):
    holder, original = FileLock(col.lock.lock_file, timeout=0), col.enqueue
    def enqueue(*args, **kwargs):
        value = original(*args, **kwargs)
        holder.acquire()
        return value
    monkeypatch.setattr(col, "enqueue", enqueue)
    return holder


def test_native_timeout_detail_keeps_retry_and_original_exception(case):
    col, calls = case
    before = col.state_path.read_bytes()
    with FileLock(col.lock.lock_file, timeout=0):
        with pytest.raises(Timeout) as caught:
            col.step(max_requests=1)
        error = caught.value
        args, trace, attributes = error.args, error.__traceback__, dict(error.__dict__)
        detail = collection.collection_failure_diagnostics(col, error)
        assert error.args == args and error.__traceback__ is trace and error.__dict__ == attributes
        assert detail["collector_phase"] == "step_queue_lock_acquire"
        assert detail["exception_module"] == "filelock._error" and detail["exception_name"] == "Timeout"
        assert detail["handler_timeout_matches_error"] is True
        assert detail["handler_timeout_matches_native_api"] is True
        assert detail["error_matches_native_api_timeout"] is True
        assert detail["error_is_builtin_timeout"] is True and detail["queue_lock_path_matches"] is True
        assert detail["observer_error"] is False and detail["trace_truncated"] is False
        assert len(detail["collector_run_code_sha256"]) == len(detail["collector_step_code_sha256"]) == 64
        retries, pauses, stopped = [], [], [False]
        def pause(seconds):
            pauses.append(seconds)
            stopped[0] = True
        col.run(stopped=lambda: stopped[0], pause=pause, on_retry=retries.append)
    assert len(retries) == 1 and pauses == [.02] and calls == []
    assert col.state_path.read_bytes() == before
    assert str(col.root) not in json.dumps(detail) and "queue.lock" not in json.dumps(detail)


def test_duplicate_native_class_remains_distinct_without_changing_propagation(case, monkeypatch):
    col, calls = case
    before = col.state_path.read_bytes()
    duplicate = duplicate_timeout(monkeypatch)
    holder = contend_at_step(col, monkeypatch)
    try:
        with pytest.raises(duplicate) as caught:
            col.run(stopped=lambda: False, pause=lambda *_: pytest.fail("Unexpected retry"),
                    on_retry=lambda *_: pytest.fail("Unexpected retry"))
        trace = caught.value.__traceback__
        detail = collection.collection_failure_diagnostics(col, caught.value)
        assert caught.value.__traceback__ is trace
        assert detail["collector_phase"] == "step_queue_lock_acquire"
        assert detail["handler_timeout_matches_error"] is False
        assert detail["handler_timeout_matches_native_api"] is False
        assert detail["error_matches_native_api_timeout"] is True
        assert detail["error_is_builtin_timeout"] is True and detail["queue_lock_path_matches"] is True
        assert detail["exception_module"] == "other" and detail["exception_name"] == "Timeout"
    finally:
        if holder.is_locked:
            holder.release()
    assert calls == [] and col.state_path.read_bytes() == before and task(col)["attempts"] == 0


@pytest.mark.parametrize("kind", ["other_lock", "builtin", "runtime", "interrupt"])
def test_other_errors_and_private_values_are_bounded(case, kind):
    col, _ = case
    private = "PRIVATE_ACCOUNT_OR_AUTH_TOKEN"
    error = {"other_lock": Timeout(str(col.root / (private + ".lock"))),
             "builtin": TimeoutError("https://provider.invalid/?token=" + private),
             "runtime": RuntimeError(private), "interrupt": KeyboardInterrupt(private)}[kind]
    detail = collection.collection_failure_diagnostics(col, error)
    assert detail["collector_phase"] == "unavailable"
    assert detail["queue_lock_path_matches"] is (False if kind == "other_lock" else None)
    assert detail["handler_timeout_matches_error"] is (kind == "other_lock")
    assert detail["error_is_builtin_timeout"] is (kind in {"other_lock", "builtin"})
    diagnostics = live_session._market_context_failure_diagnostics(col, error)
    assert diagnostics["market_collection"] == detail
    assert all(value not in json.dumps(diagnostics) for value in (private, str(col.root), "https://"))


@pytest.mark.parametrize("kind", ["relative", "remote", "oversize", "nul"])
def test_lock_path_comparison_is_bounded_lexical_and_never_resolves(case, monkeypatch, kind):
    col, _ = case
    value = {"relative": os.path.relpath(col.lock.lock_file),
             "remote": r"\\PRIVATE_REMOTE\share\token.lock",
             "oversize": "x" * 32769, "nul": "x\x00y"}[kind]
    monkeypatch.setattr(Path, "resolve", lambda *a, **k: pytest.fail("Filesystem resolution attempted"))
    detail = collection.collection_failure_diagnostics(col, Timeout(value))
    assert detail["queue_lock_path_matches"] is (True if kind == "relative" else False if kind == "remote" else None)
    assert detail["observer_error"] is (kind in {"oversize", "nul"})
    assert "PRIVATE_REMOTE" not in json.dumps(detail)


def test_poisoned_metadata_cannot_replace_original_error():
    class Poison:
        @property
        def lock(self):
            raise KeyboardInterrupt("private lock metadata")
        @property
        def run(self):
            raise SystemExit("private observer")
    error = RuntimeError("original")
    detail = collection.collection_failure_diagnostics(Poison(), error)
    assert detail["observer_error"] is True and detail["queue_lock_path_matches"] is None
    assert detail["collector_run_code_sha256"] is None and detail["collector_step_code_sha256"] is None
    assert "private" not in json.dumps(detail) and error.args == ("original",)


@pytest.mark.parametrize("failure", [OSError("private observer"), KeyboardInterrupt("private observer")])
def test_optional_observer_preserves_original_worker_chain(case, monkeypatch, failure):
    col, _ = case
    error = Timeout(col.lock.lock_file)
    expected = live_session._symbol_exception_diagnostics(error)
    def fail(*args):
        raise failure
    monkeypatch.setattr(collection, "collection_failure_diagnostics", fail)
    assert live_session._market_context_failure_diagnostics(col, error) == expected and error.args == ()


@pytest.mark.parametrize("observer_fault", [False, True])
def test_actual_fatal_worker_path_retains_failed_status_and_safe_details(tmp_path, monkeypatch, case, observer_fault):
    kwargs, calls, executors = scout_manual_fixture(tmp_path, monkeypatch)
    col, provider_calls = case
    duplicate_timeout(monkeypatch)
    holder = contend_at_step(col, monkeypatch)
    monkeypatch.setattr(live_session, "prepare_market_collection", lambda *a, **k: col)
    if observer_fault:
        def fail(*args):
            raise KeyboardInterrupt("private observer")
        monkeypatch.setattr(collection, "collection_failure_diagnostics", fail)
    try:
        result = live_session.run_manual(**kwargs)
    finally:
        if holder.is_locked:
            holder.release()
    assert result["status"] == "FAILED" and "step" not in calls and "recover" not in calls
    assert not executors[0].kwargs["manual_authority"]()
    status = json.loads((tmp_path / "intraday/manual-session.json").read_text())
    assert status["stop_reason"] == "worker_failure" and status["broker_orders_automatically_cancelled"] is False
    failure = status["worker_errors"][0]
    assert failure["error_type"] == "Timeout" and failure["worker"] == "market_context"
    detail = failure["diagnostics"].get("market_collection")
    if observer_fault:
        assert detail is None
    else:
        assert detail["handler_timeout_matches_error"] is False
        assert detail["handler_timeout_matches_native_api"] is False and detail["queue_lock_path_matches"] is True
    store = ShadowStore(tmp_path / "intraday/shadow.sqlite3")
    with store.connect() as db:
        saved = json.loads(db.execute("SELECT payload FROM events WHERE kind='market_context_worker_stopped'").fetchone()[0])
    assert saved["diagnostics"] == failure["diagnostics"] and provider_calls == []
    assert "private observer" not in json.dumps(status)
