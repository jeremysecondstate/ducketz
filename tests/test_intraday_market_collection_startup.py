"""Native startup queue contention; no provider, broker, model or process effects."""
from dataclasses import asdict
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from filelock import FileLock, Timeout, _api, _error
import pytest

from ml.intraday import market_collection as M, live_session
from ml.intraday.store import ShadowStore
from tests.test_intraday_live_session import (
    scout_manual_fixture, isolate_minute_acquisition_in_execution_fixtures,
)


@pytest.fixture
def fast_native_lock(monkeypatch):
    """Exercise native enter/Timeout identities without thirty seconds of sleep."""
    original = FileLock.__init__
    requested = []
    def initialize(self, path, *args, **kwargs):
        requested.append(kwargs.get("timeout"))
        kwargs["timeout"] = 0
        return original(self, path, *args, **kwargs)
    monkeypatch.setattr(FileLock, "__init__", initialize)
    return requested


def construct(root, *, recovery=None, collector_type=M.QuarterHourMarketCollector):
    return collector_type(root, symbols=["TEST"], requests=[], adapters={},
                         startup_recovery=recovery)


def observations(error):
    detail = M.queue_initialization_failure_diagnostics(error)
    assert detail["schema_version"] == "intraday-market-queue-initialization-v1"
    return detail, detail["collection"]["queue_lock_timing"]["recent"]


def test_actual_native_initialization_timeout_recovers_with_same_queue(tmp_path, fast_native_lock):
    holder = FileLock(str(tmp_path / "queue.lock"), timeout=0)
    holder.acquire()
    pauses, authority_checks = [], []
    def stopped():
        authority_checks.append(holder.is_locked)
        return False
    def pause(seconds):
        pauses.append(seconds)
        assert holder.is_locked
        holder.release()
    try:
        collector = construct(tmp_path, recovery=M.QueueInitializationRecovery(stopped, pause))
    finally:
        if holder.is_locked:
            holder.release()
    recent = collector.queue_lock_timing.snapshot()["recent"]
    assert [r["outcome"] for r in recent] == ["acquire_error", "completed"]
    assert recent[0]["failure_active_local"][0]["state"] == "waiting"
    assert all(r["phase"] == "initialize" for r in recent)
    assert pauses == [1.0] and authority_checks == [True, True, False, False, False]
    assert not collector.state_path.exists()  # No invented queue tasks or provider work.
    assert 10 in fast_native_lock  # The production lock constructor retains its ten-second budget.


def test_exhausted_recovery_reraises_final_native_timeout_and_keeps_all_attempts(
        tmp_path, fast_native_lock):
    pauses = []
    with FileLock(str(tmp_path / "queue.lock"), timeout=0):
        with pytest.raises(Timeout) as caught:
            construct(tmp_path, recovery=M.QueueInitializationRecovery(lambda: False, pauses.append))
    error = caught.value
    assert type(error) is _api.Timeout and error.lock_file == str(tmp_path / "queue.lock")
    detail, recent = observations(error)
    assert detail["attempts"] == detail["maximum_attempts"] == 3
    assert detail["recovery_enabled"] is True and pauses == [1.0, 1.0]
    assert [r["outcome"] for r in recent] == ["acquire_error"] * 3
    assert all(r["failure_capture_stage"] == "native_acquisition_error_before_pool_drain" for r in recent)
    assert str(tmp_path) not in json.dumps(detail) and "queue.lock" not in json.dumps(detail)
    detail["collection"]["queue_lock_timing"]["recent"][0]["failure_active_local"].clear()
    assert observations(error)[1][0]["failure_active_local"]  # Detached on every read.


def test_default_constructor_does_not_add_any_retry(tmp_path, fast_native_lock):
    with FileLock(str(tmp_path / "queue.lock"), timeout=0):
        with pytest.raises(Timeout) as caught:
            construct(tmp_path)
    detail, recent = observations(caught.value)
    assert detail["attempts"] == detail["maximum_attempts"] == 1
    assert detail["recovery_enabled"] is False and len(recent) == 1


@pytest.mark.parametrize("boundary", ["before_attempt", "before_pause", "during_pause", "after_success"])
def test_stop_or_deadline_is_checked_at_every_initialization_boundary(tmp_path, fast_native_lock, boundary):
    checks, pauses = [], []
    held = boundary in {"before_pause", "during_pause"}
    holder = FileLock(str(tmp_path / "queue.lock"), timeout=0)
    if held:
        holder.acquire()
    def stopped():
        checks.append(True)
        return (boundary == "before_attempt" or
                boundary in {"before_pause", "after_success"} and len(checks) >= 2 or
                boundary == "during_pause" and bool(pauses))
    try:
        with pytest.raises(InterruptedError, match="Manual session stopped") as caught:
            construct(tmp_path, recovery=M.QueueInitializationRecovery(stopped, pauses.append))
    finally:
        if holder.is_locked:
            holder.release()
    detail, recent = observations(caught.value)
    assert detail["attempts"] == (0 if boundary == "before_attempt" else 1)
    assert pauses == ([1.0] if boundary == "during_pause" else [])
    assert len(recent) == (0 if boundary == "before_attempt" else 1)
    assert not (tmp_path / "queue.json").exists()


@pytest.mark.parametrize("fault", ["schema", "scope", "json", "native_body_timeout", "other_lock"])
def test_reader_body_and_validation_errors_never_retry(tmp_path, monkeypatch, fast_native_lock, fault):
    value = {"schema_version": M.QUEUE_VERSION, "symbols": ["TEST"], "tasks": {}, "source_blocks": {}}
    if fault == "schema":
        value["schema_version"] = "unknown"
    if fault == "scope":
        value["symbols"] = ["FOREIGN"]
    (tmp_path / "queue.json").write_bytes(b"malformed" if fault == "json" else M.canonical(value))
    pauses = []
    original = M.json.loads
    expected = Timeout(str(tmp_path / ("other.lock" if fault == "other_lock" else "queue.lock")))
    if fault in {"native_body_timeout", "other_lock"}:
        def fail(payload, *args, **kwargs):
            # Only the native queue read fails; diagnostics retain their own decoder.
            if isinstance(payload, str):
                raise expected
            return original(payload, *args, **kwargs)
        monkeypatch.setattr(M.json, "loads", fail)
    with pytest.raises((ValueError, Timeout)) as caught:
        construct(tmp_path, recovery=M.QueueInitializationRecovery(lambda: False, pauses.append))
    assert pauses == []
    if fault in {"native_body_timeout", "other_lock"}:
        assert caught.value is expected
    detail, _ = observations(caught.value)
    assert detail["attempts"] == 1


@pytest.mark.parametrize("method", ["_read_queue", "_state"])
def test_custom_readers_keep_original_propagation_without_retry(tmp_path, fast_native_lock, method):
    expected, pauses = Timeout(str(tmp_path / "queue.lock")), []
    def custom(*args, **kwargs):
        raise expected
    custom_type = type("CustomCollector", (M.QuarterHourMarketCollector,), {method: custom})
    with pytest.raises(Timeout) as caught:
        construct(tmp_path, collector_type=custom_type,
                  recovery=M.QueueInitializationRecovery(lambda: False, pauses.append))
    assert caught.value is expected and pauses == []
    assert observations(caught.value)[0]["attempts"] == 1


def test_duplicate_native_timeout_identity_is_recognized_only_at_original_enter(
        tmp_path, monkeypatch, fast_native_lock):
    spec = importlib.util.spec_from_file_location("_offline_startup_timeout", _error.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(_api, "Timeout", module.Timeout)
    holder = FileLock(str(tmp_path / "queue.lock"), timeout=0)
    holder.acquire()
    pauses = []
    def pause(seconds):
        pauses.append(seconds)
        holder.release()
    try:
        collector = construct(tmp_path, recovery=M.QueueInitializationRecovery(lambda: False, pause))
    finally:
        if holder.is_locked:
            holder.release()
    assert pauses == [1.0]
    assert [r["outcome"] for r in collector.queue_lock_timing.snapshot()["recent"]] == ["acquire_error", "completed"]


def test_startup_attachment_rejects_untyped_objects_and_observer_failure_preserves_error(
        tmp_path, monkeypatch, fast_native_lock):
    arbitrary = RuntimeError("private")
    arbitrary._intraday_queue_initialization_failure = {"private": "secret"}
    assert M.queue_initialization_failure_diagnostics(arbitrary) is None
    monkeypatch.setattr(M, "collection_failure_diagnostics", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    with FileLock(str(tmp_path / "queue.lock"), timeout=0):
        with pytest.raises(Timeout) as caught:
            construct(tmp_path)
    assert M.queue_initialization_failure_diagnostics(caught.value) is None


@pytest.mark.parametrize("pc,callbacks", [("Scout", True), ("Scout", False), ("Atlas", True)])
def test_live_recipe_wires_recovery_only_for_explicit_scout_startup(tmp_path, monkeypatch, pc, callbacks):
    from ml.intraday.market_context import market_context_contract
    request = M.SourceRequest("schwab", "stock_quote", "TEST", "marketdata-v1", "quote", "quotes")
    config = {"requests": [asdict(request)], "configuration_sha256": "b" * 64}
    builder = SimpleNamespace(market_context_contract=market_context_contract({"stock_quote": [request.source_id]}),
                              market_collection_configuration_sha256="b" * 64)
    monkeypatch.setattr(M, "load_local_collection_config", lambda *a: config)
    captured, stop, pause = [], lambda: False, lambda _: None
    monkeypatch.setattr(M, "collector_from_config", lambda *a, **k: captured.append(k) or "collector")
    options = {"stopped": stop, "pause": pause} if callbacks else {}
    assert live_session.prepare_market_collection({"dataset_builder": builder}, datastore=tmp_path,
        pc=pc, symbols=["TEST"], **options) == "collector"
    if pc == "Scout" and callbacks:
        recovery = captured[0]["startup_recovery"]
        assert recovery.stopped is stop and recovery.pause is pause
    else:
        assert "startup_recovery" not in captured[0]


@pytest.mark.parametrize("recovery", [object(), {"stopped": lambda: False}])
def test_invalid_recovery_contract_is_rejected_before_initialization(tmp_path, recovery):
    with pytest.raises(ValueError, match="recovery contract"):
        construct(tmp_path, recovery=recovery)
    assert not (tmp_path / "receipt-index.sqlite3").exists()


def test_actual_scout_startup_failure_persists_constructor_observations(
        tmp_path, monkeypatch, fast_native_lock):
    kwargs, calls, executors = scout_manual_fixture(tmp_path, monkeypatch)
    queue_root = tmp_path / "market-startup-fixture"
    queue_root.mkdir()
    def prepare(*args, **options):
        assert callable(options["stopped"]) and callable(options["pause"])
        return construct(queue_root, recovery=M.QueueInitializationRecovery(
            options["stopped"], lambda _: None))
    monkeypatch.setattr(live_session, "prepare_market_collection", prepare)
    with FileLock(str(queue_root / "queue.lock"), timeout=0):
        with pytest.raises(Timeout):
            live_session.run_manual(**kwargs)
    status = json.loads((tmp_path / "intraday/manual-session.json").read_bytes())
    assert status["status"] == "FAILED" and status["startup_stage"] == "market_collection"
    assert status["stop_reason"] == "startup_failure"
    attachment = status["diagnostics"]["market_collection_initialization"]
    assert attachment["attempts"] == attachment["maximum_attempts"] == 3
    assert len(attachment["collection"]["queue_lock_timing"]["recent"]) == 3
    assert not executors[0].kwargs["manual_authority"]()
    assert "step" not in calls and "recover" not in calls
    store = ShadowStore(tmp_path / "intraday/shadow.sqlite3")
    with store.connect() as db:
        event = json.loads(db.execute("SELECT payload FROM events WHERE kind='manual_startup_stopped'").fetchone()[0])
    assert event["diagnostics"] == status["diagnostics"]


def test_unknown_native_poll_implementation_does_not_enable_retry(tmp_path, monkeypatch, fast_native_lock):
    original = FileLock._poll_until_acquired
    # A different private implementation has no validated native Timeout global.
    def unknown(self, *args, **kwargs):
        return original(self, *args, **kwargs)
    monkeypatch.setattr(FileLock, "_poll_until_acquired", unknown)
    pauses = []
    with FileLock(str(tmp_path / "queue.lock"), timeout=0):
        with pytest.raises(Timeout) as caught:
            construct(tmp_path, recovery=M.QueueInitializationRecovery(lambda: False, pauses.append))
    assert pauses == [] and observations(caught.value)[0]["attempts"] == 1


@pytest.mark.parametrize("boundary", ["manual_interrupt", "original_deadline"])
def test_actual_scout_startup_stop_preserves_pending_work_and_failure_snapshot(
        tmp_path, monkeypatch, fast_native_lock, boundary):
    kwargs, calls, executors = scout_manual_fixture(tmp_path, monkeypatch)
    current = ["2026-10-08T17:17:00Z"]
    kwargs["clock"] = lambda: current[0]
    queue_root = tmp_path / "startup-stop-fixture"
    queue_root.mkdir()
    def prepare(*args, **options):
        def pause(seconds):
            if boundary == "original_deadline":
                current[0] = "2026-10-09T00:00:01Z"  # Original 17:00 Pacific boundary.
            options["pause"](seconds)
        return construct(queue_root, recovery=M.QueueInitializationRecovery(options["stopped"], pause))
    monkeypatch.setattr(live_session, "prepare_market_collection", prepare)
    with FileLock(str(queue_root / "queue.lock"), timeout=0):
        result = live_session.run_manual(**kwargs)
    assert result["status"] == "STOPPED"
    status = json.loads((tmp_path / "intraday/manual-session.json").read_bytes())
    assert status["status"] == "STOPPED" and status["startup_stage"] == "market_collection"
    assert status["stop_reason"] == ("manual_stop" if boundary == "manual_interrupt" else "automatic_17_00_pacific")
    assert status["broker_orders_automatically_cancelled"] is False
    attachment = status["diagnostics"]["market_collection_initialization"]
    assert attachment["attempts"] == 1 and attachment["maximum_attempts"] == 3
    assert attachment["collection"]["queue_lock_timing"]["recent"][0]["outcome"] == "acquire_error"
    assert str(tmp_path) not in json.dumps(status["diagnostics"])
    assert not executors[0].kwargs["manual_authority"]()
    assert "step" not in calls and "recover" not in calls and "forecast" not in calls
    store = ShadowStore(tmp_path / "intraday/shadow.sqlite3")
    with store.connect() as db:
        event = json.loads(db.execute("SELECT payload FROM events WHERE kind='manual_startup_stopped'").fetchone()[0])
    assert event["diagnostics"] == status["diagnostics"]
