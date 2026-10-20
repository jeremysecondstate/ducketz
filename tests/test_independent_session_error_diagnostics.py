"""Offline unexpected-failure evidence; fake runner never contacts a broker."""
from contextlib import ExitStack
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.stock_trader import independent_session as worker


@pytest.fixture
def session(tmp_path, monkeypatch):
    now = [pd.Timestamp("2026-10-20T20:06:00Z")]
    active = [True]
    monkeypatch.setattr(worker, "_has_inventory", lambda _: True)
    monkeypatch.setattr(worker, "read_gameplan_stock_activation_intent", lambda _: SimpleNamespace(active=active[0]))
    monkeypatch.setattr("ml.account_gameplan.config.load_account_config", lambda _: None)
    monkeypatch.setattr("ml.account_gameplan.config.assert_coordinator", lambda *_: None)
    def run(runner, **options):
        return worker.run_independent_stock_session(
            tmp_path, execute=False, clock=lambda: now[0], runner=runner,
            sleep=options.pop("sleep", lambda _: pytest.fail("Unexpected wait")),
            reporter=lambda _: None, sizing_policy=worker.GAMEPLAN_SIZING_POLICY, **options)
    return SimpleNamespace(run=run, now=now, active=active, root=tmp_path,
                           status=tmp_path / "state/independent-stock-trader/session-status.json")


def test_unexpected_chained_permission_error_preserves_safe_evidence_and_fatal_behavior(session):
    secret = "PRIVATE_TOKEN_AND_ACCOUNT_12345"
    original = PermissionError(13, secret, "C:/private/" + secret + ".json")
    original.winerror = 5
    def runner(*args, **kwargs):
        assert kwargs["execute"] is False
        try:
            raise original
        except PermissionError as error:
            raise RuntimeError(secret) from error
    with pytest.raises(RuntimeError) as caught:
        session.run(runner)
    assert caught.value.__cause__ is original
    status = json.loads(session.status.read_text())
    assert status["status"] == "FAILED"
    assert status["failed_cycles"] == status["consecutive_failures"] == 1
    assert status["calls"] == status["orders_submitted"] == 0
    assert status["last_cycle"] == {"status": "UNHANDLED_WORKER_ERROR", "error_type": "RuntimeError"}
    diagnostic = status["worker_error_diagnostics"]
    assert [item["error_type"] for item in diagnostic["chain"]] == ["RuntimeError", "PermissionError"]
    assert diagnostic["chain"][1]["errno"] == 13
    assert diagnostic["chain"][1]["winerror"] == 5
    assert diagnostic["chain"][0]["frames"][-1]["function"] == "runner"
    assert any(frame["file"] == "ml/stock_trader/independent_session.py" for frame in diagnostic["chain"][0]["frames"])
    raw = json.dumps(diagnostic)
    assert secret not in raw and "C:/private" not in raw and str(session.root) not in raw
    assert "filename" not in raw and "message" not in raw and "args" not in raw
    assert not diagnostic["chain_cycle"] and not diagnostic["chain_truncated"]
    assert not (session.root / "locks/independent-stock-session.lock").exists()
    assert not (session.root / worker.LEDGER_RELATIVE_PATH).exists()


@pytest.mark.parametrize("field,value", [("errno", True), ("winerror", 2 ** 40), ("errno", "private")])
def test_os_code_values_are_bounded_integers(field, value):
    error = RuntimeError("PRIVATE_MESSAGE")
    setattr(error, field, value)
    assert field not in worker._worker_error_diagnostics(error)["chain"][0]


def test_external_traceback_filename_and_runtime_values_are_not_serialized(tmp_path):
    filename = str(tmp_path / "PRIVATE_ACCOUNT_TOKEN.py")
    namespace = {}
    exec(compile("def external_failure():\n    secret = 'PRIVATE_LOCAL_VALUE'\n    raise OSError(13, secret, 'PRIVATE_FILENAME')\n", filename, "exec"), namespace)
    try:
        namespace["external_failure"]()
    except OSError as error:
        diagnostic = worker._worker_error_diagnostics(error)
    frame = diagnostic["chain"][0]["frames"][-1]
    assert frame == {"file": "<external>", "function": "external_failure", "line": 3}
    assert "PRIVATE" not in json.dumps(diagnostic) and str(tmp_path) not in json.dumps(diagnostic)


def test_chain_and_frames_are_bounded_and_cycles_marked():
    def recurse(depth):
        if depth:
            return recurse(depth - 1)
        raise OSError(13, "PRIVATE")
    try:
        recurse(20)
    except OSError as original:
        head = original
        for _ in range(10):
            next_error = RuntimeError("PRIVATE")
            next_error.__cause__ = head
            head = next_error
        diagnostic = worker._worker_error_diagnostics(head)
        assert len(diagnostic["chain"]) == 8 and diagnostic["chain_truncated"]
        frames = worker._worker_error_diagnostics(original)["chain"][0]
        assert len(frames["frames"]) == 8 and frames["frames_truncated"]
    first, second = RuntimeError("PRIVATE"), ValueError("PRIVATE")
    first.__cause__, second.__cause__ = second, first
    cycle = worker._worker_error_diagnostics(first)
    assert len(cycle["chain"]) == 2 and cycle["chain_cycle"] and not cycle["chain_truncated"]


def test_suppressed_context_is_not_followed():
    try:
        try:
            raise OSError(13, "PRIVATE")
        except OSError:
            raise RuntimeError("PRIVATE") from None
    except RuntimeError as error:
        assert len(worker._worker_error_diagnostics(error)["chain"]) == 1


def test_filename_equal_to_source_root_is_an_external_frame():
    namespace = {}
    filename = str(Path(worker.__file__).resolve().parents[2])
    try:
        exec(compile("raise OSError(13, 'PRIVATE')", filename, "exec"), namespace)
    except OSError as error:
        frame = worker._worker_error_diagnostics(error)["chain"][0]["frames"][-1]
    assert frame == {"file": "<external>", "function": "<code>", "line": 1}


def test_diagnostic_failure_preserves_original_error_and_failure_status(session, monkeypatch):
    original = PermissionError(13, "PRIVATE")
    def fail_diagnostics(_):
        raise OSError(5, "PRIVATE_DIAGNOSTIC_FAILURE")
    monkeypatch.setattr(worker, "_worker_error_diagnostics", fail_diagnostics)
    def runner(*args, **kwargs):
        raise original
    with pytest.raises(PermissionError) as caught:
        session.run(runner)
    assert caught.value is original
    status = json.loads(session.status.read_text())
    assert status["status"] == "FAILED" and status["failed_cycles"] == status["consecutive_failures"] == 1
    assert status["calls"] == status["orders_submitted"] == 0
    assert status["worker_error_diagnostics"] == {"schema_version": "gameplan-worker-error-diagnostics-v1", "unavailable": True}
    assert not (session.root / "locks/independent-stock-session.lock").exists()


@pytest.mark.parametrize("mode", ["normal", "manual_stop", "manual_interrupt"])
def test_non_failure_controls_do_not_create_diagnostics(session, mode):
    if mode == "manual_stop":
        session.active[0] = False
    def runner(*args, **kwargs):
        if mode == "manual_interrupt":
            raise KeyboardInterrupt()
        session.now[0] = pd.Timestamp("2026-10-21T00:00:00Z")
        return SimpleNamespace(submitted_orders=0, to_dict=lambda: {"status": "NO_ORDERS_SUBMITTED"})
    if mode == "manual_interrupt":
        with pytest.raises(KeyboardInterrupt):
            session.run(runner)
    else:
        result = session.run(runner)
        assert result["status"] == ("SESSION_STOPPED_TRADER_INACTIVE" if mode == "manual_stop" else "SESSION_FINISHED")
    status = json.loads(session.status.read_text())
    assert "worker_error_diagnostics" not in status
    assert status["failed_cycles"] == status["consecutive_failures"] == 0
    assert not (session.root / "locks/independent-stock-session.lock").exists()


def test_authentic_shared_lock_wait_does_not_become_failure_evidence(session, monkeypatch):
    monkeypatch.setattr(worker, "_worker_error_diagnostics", lambda _: pytest.fail("Contention is not an unexpected failure"))
    lock = session.root / "locks/stock-trader-hourly.lock"
    with ExitStack() as holder:
        holder.enter_context(worker.exclusive_runtime_lock(lock, process_name="stock-trader-hourly"))
        original = lock.read_bytes()
        def runner(*args, **kwargs):
            with worker.exclusive_runtime_lock(lock, process_name="stock-trader-hourly"):
                pytest.fail("Busy owner must prevent execution")
        def sleep(seconds):
            assert lock.read_bytes() == original and seconds == 5
            session.active[0] = False
            session.now[0] += pd.Timedelta(seconds=seconds)
        result = session.run(runner, sleep=sleep)
        assert lock.read_bytes() == original
    status = json.loads(session.status.read_text())
    assert result["status"] == "SESSION_STOPPED_TRADER_INACTIVE"
    assert status["runtime_lock_wait"]["status"] == "WAITING"
    assert status["last_cycle"] is None and "worker_error_diagnostics" not in status
    assert status["failed_cycles"] == status["consecutive_failures"] == 0
