"""Independent 1B lifecycle tests using fake processes and prerecorded results."""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from tools import scout_service_controller as services
from tools.scout_release import atomic_json


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 27, 16, tzinfo=timezone.utc)
        self.elapsed = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.elapsed += seconds
        self.now += timedelta(seconds=seconds)


def selection(root, name="a"):
    return {"release_root": str(root / name), "manifest_sha256": name * 64,
        "source_root": str(root / name / "source"), "recipe_path": str(root / "private-recipe.json"),
        "recipe_file_sha256": "f" * 64, "symbols": ["SYM" + str(i) for i in range(11)],
        "lifecycle_protocol": services.PROTOCOL,
        "trading_rotation_version": services.TRADING_ROTATION_VERSION}


class FakeWorker:
    def __init__(self, harness, command, **kwargs):
        self.harness = harness
        self.request = json.loads(Path(command[-1]).read_bytes())
        self.role = self.request["role"]
        self.root = Path(self.request["generation_root"])
        self.pid = len(harness.workers) + 10000
        self.create_time = float(self.pid)
        self.job_name = "fake-service-job-" + str(self.pid)
        self.returncode, self.resumed, self.closed, self.ticks = None, False, False, 0
        self.rotation_ticks = 0
        assert kwargs["start_suspended"] is True
        assert kwargs["env"]["PYTHONPATH"] == ""
        assert kwargs["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
        assert all(w.closed for w in harness.workers if w.role == self.role)
        harness.workers.append(self)

    def header(self):
        return {key: self.request[key] for key in ("session_id", "generation", "token", "release", "role")}

    def resume(self):
        self.resumed = True

    def poll(self):
        if self.returncode is not None or not self.resumed:
            return self.returncode
        self.ticks += 1
        if self.ticks == 1:
            atomic_json(self.root / "phase.json", {**self.header(), "phase": "ready"})
            return None
        if self.harness.owner.stop_reason():
            self.returncode = 0
            return self.returncode
        if self.role == "trading":
            self.harness.recovery_ticks += 1
            rotation = self.harness.owner.state["roles"]["trading"].get("rotation_request")
            if rotation is None:
                return None
            self.rotation_ticks += 1
            if self.harness.mode == "stop_rotation":
                self.harness.owner.request_stop()
                return None
            if self.rotation_ticks < (15 if self.harness.mode == "trading_slow" else 2):
                return None
            checkpoint = {**self.header(), "schema": services.TRADING_CHECKPOINT_SCHEMA,
                "rotation_request": rotation, "boundary_kind": "model_full_symbol_attempt",
                "boundary_cycle_id": rotation["boundary_cycle_id"],
                "owned_workers_drained": True, "pending_order_identity_preserved": True,
                "symbols": self.request["release"]["symbols"],
                **{key:self.request[key] for key in ("started_at", "wake_at", "deadline")}}
            result = {**self.header(), "status": "PLANNED_RESTART"}
            self.returncode = services.PLANNED_EXIT
            if self.harness.mode == "trading_invalid":
                checkpoint["owned_workers_drained"] = False
            if self.harness.mode == "trading_wrong_sequence":
                checkpoint["rotation_request"] = {**rotation, "iteration_sequence": 99}
            if self.harness.mode == "trading_crash":
                result["status"] = "FAILED"
                self.returncode = 9
            atomic_json(self.root / "result.json", result)
            atomic_json(self.root / "checkpoint.json", checkpoint)
            return self.returncode
        mode = self.harness.mode
        if mode == "slow":
            return None
        if self.ticks < 4:
            return None
        result = {**self.header(), "status": "PLANNED_RESTART"}
        checkpoint = {**self.header(), "schema": services.CHECKPOINT_SCHEMA,
            "boundary_kind": "full_symbol_attempt_with_durable_deferrals", "owned_workers_drained": True,
            "boundary_cycle_id": "cycle-" + str(self.request["generation"]),
            "symbols": self.request["release"]["symbols"],
            **{key: self.request[key] for key in ("started_at", "wake_at", "deadline")}}
        if mode == "crash":
            result = {**self.header(), "status": "FAILED", "error": "prerecorded model failure"}
            self.returncode = 9
        else:
            self.returncode = services.PLANNED_EXIT
        if mode == "partial":
            checkpoint["symbols"] = checkpoint["symbols"][:-1]
        if mode == "wrong_day":
            checkpoint["deadline"] = "2026-10-29T00:00:00+00:00"
        if mode == "stale":
            result["token"] = "old-token"
        atomic_json(self.root / "result.json", result)
        atomic_json(self.root / "checkpoint.json", checkpoint)
        return self.returncode

    def job_empty(self):
        return not (self.harness.mode == "orphan" or
                    self.harness.mode == "trading_orphan" and self.role == "trading") or self.closed

    def close(self, timeout=120):
        self.closed = True
        self.returncode = 99 if self.returncode is None else self.returncode

    def terminate(self, timeout=120):
        self.close(timeout)


class Harness:
    def __init__(self, tmp_path, mode="planned"):
        self.clock, self.mode = Clock(), mode
        self.workers, self.recovery_ticks, self.selections = [], 0, 0
        def select(_):
            self.selections += 1
            return selection(tmp_path, "a" if mode == "unchanged" or self.selections < 3 else "b")
        self.owner = services.ServiceController(selector=tmp_path / "independent.json", repository=tmp_path,
            datastore=tmp_path, wake_at=self.clock(), deadline=self.clock() + timedelta(seconds=2),
            clock=self.clock, sleep=self.clock.sleep, monotonic=lambda: self.clock.elapsed,
            worker_factory=lambda command, **kwargs: FakeWorker(self, command, **kwargs), select=select,
            drain_timeout=0.3, startup_timeout=1)

    def run(self):
        return self.owner.run(lock_factory=lambda *_: nullcontext())


class CapturedConsole:
    def __init__(self):
        self.messages = []

    def emit(self, message):
        self.messages.append(message)

    def close(self):
        pass


@pytest.mark.parametrize("wake,zone", [("2026-10-29T11:00:00Z", "PDT"),
                                     ("2026-11-02T12:00:00Z", "PST")])
def test_early_start_reports_pacific_session_and_waits_for_both_workers(tmp_path, wake, zone):
    h = Harness(tmp_path, "slow")
    opening = services.instant(wake)
    h.clock.now = opening - timedelta(seconds=1)
    h.owner.state.update(wake_at=opening.isoformat(), deadline=(opening + timedelta(seconds=1)).isoformat())
    console = CapturedConsole()
    h.owner.console.close()
    h.owner.console = console
    starts = []
    factory = h.owner.worker_factory
    def make_worker(*args, **kwargs):
        starts.append(h.clock())
        assert h.clock() >= opening
        return factory(*args, **kwargs)
    h.owner.worker_factory = make_worker
    def advance(seconds):
        if h.clock() < opening:
            assert not h.workers
        h.clock.sleep(seconds)
    h.owner.sleep = advance
    assert h.run() == 0
    assert {w.role for w in h.workers} == {"trading", "model"}
    assert starts and all(value == opening for value in starts)
    rows = [json.loads(text) for text in console.messages if text.startswith("{")]
    waiting = next(row for row in rows if row["status"] == "WAITING")
    assert waiting["message"] == f"Waiting until 4:00 a.m. Pacific session start on {opening.date()} ({zone})."
    assert "[Scout 1B] " + waiting["message"] in console.messages
    assert waiting["wake_at"] == opening.isoformat()
    assert any("session start reached" in (row["message"] or "") for row in rows if row["status"] == "STARTING")


def test_manual_stop_while_waiting_never_starts_workers(tmp_path):
    h = Harness(tmp_path)
    h.owner.state["wake_at"] = (h.clock() + timedelta(minutes=5)).isoformat()
    h.owner.state["deadline"] = (h.clock() + timedelta(hours=13)).isoformat()
    def stop_during_wait(seconds):
        assert not h.workers
        h.clock.sleep(seconds)
        h.owner.request_stop()
    h.owner.sleep = stop_during_wait
    assert h.run() == 0
    assert not h.workers
    assert h.owner.state["stop_reason"] == "manual_stop"


def test_late_launch_and_stopped_state_do_not_claim_waiting(tmp_path):
    h = Harness(tmp_path)
    h.owner.console.close()
    h.owner.console = CapturedConsole()
    h.owner.state["wake_at"] = (h.clock() - timedelta(minutes=1)).isoformat()
    h.owner._console_status(force=True)
    row = json.loads(h.owner.console.messages[-1])
    assert "reached" in row["message"] and "Waiting until" not in row["message"]
    h.owner.state["status"] = "STOPPED"
    h.owner._console_status(force=True)
    assert json.loads(h.owner.console.messages[-1])["message"] is None


def test_full_iteration_rotates_model_and_trading_with_original_run_identity(tmp_path):
    h = Harness(tmp_path)
    assert h.run() == 0
    traders = [w for w in h.workers if w.role == "trading"]
    models = [w for w in h.workers if w.role == "model"]
    assert len(traders) > 2 and len(models) > 2
    assert {w.request["session_id"] for w in h.workers} == {h.owner.run_id}
    assert {w.request["deadline"] for w in h.workers} == {h.owner.state["deadline"]}
    assert {w.request["started_at"] for w in h.workers} == {h.owner.state["started_at"]}
    assert models[1].request["previous_checkpoint"]["boundary_cycle_id"] == "cycle-1"
    assert traders[0].request["release"] != traders[-1].request["release"]
    assert traders[1].request["previous_checkpoint"]["rotation_request"]["iteration_sequence"] == 1
    assert [w.request["previous_checkpoint"]["rotation_request"]["iteration_sequence"]
            for w in traders[1:]] == list(range(1, len(traders)))
    assert h.owner.state["trading_completed_iteration"] == len(traders) - 1
    assert h.recovery_ticks > 10
    assert h.owner.state["system_1a_owned"] is False
    assert all(w.closed for w in h.workers)


def test_slow_model_does_not_block_recovery_or_manual_stop(tmp_path):
    h = Harness(tmp_path, "slow")
    original_sleep = h.owner.sleep
    def sleep(seconds):
        original_sleep(seconds)
        if h.clock.elapsed >= 0.6:
            h.owner.request_stop()
    h.owner.sleep = sleep
    assert h.run() == 0
    assert h.recovery_ticks >= 4
    assert len([w for w in h.workers if w.role == "model"]) == 1
    assert h.owner.state["stop_reason"] == "manual_stop"
    assert h.owner.stop_path.exists()
    assert all(w.closed for w in h.workers)


@pytest.mark.parametrize("mode", ["crash", "partial", "wrong_day", "stale", "orphan"])
def test_terminal_model_failure_never_respawns_and_trading_keeps_recovering(tmp_path, mode):
    h = Harness(tmp_path, mode)
    assert h.run() == 0
    assert len([w for w in h.workers if w.role == "model"]) == 1
    assert h.recovery_ticks >= 10
    assert h.owner.state["roles"]["model"]["status"] == "FAILED"
    assert h.owner.state["roles"]["model"]["automatic_retry"] is False
    assert h.owner.state["roles"]["trading"]["generation"] == 1


def test_model_recipe_change_fails_model_only_without_replacing_trader(tmp_path):
    h = Harness(tmp_path)
    calls = [0]
    def select(_):
        calls[0] += 1
        result = selection(tmp_path)
        if calls[0] > 1:
            result["recipe_file_sha256"] = "changed"
        return result
    h.owner.select = select
    assert h.run() == 1
    assert len([w for w in h.workers if w.role == "model"]) == 1
    assert len([w for w in h.workers if w.role == "trading"]) == 1
    assert "private recipe" in h.owner.state["roles"]["model"]["error"]
    assert "private recipe" in h.owner.state["roles"]["trading"]["error"]


def lifecycle(tmp_path, monkeypatch, role="trading"):
    h = Harness(tmp_path, "slow")
    h.owner.root.mkdir(parents=True)
    h.owner.save("STARTING")
    h.owner._start_role(role, selection(tmp_path))
    worker = h.workers[-1]
    monkeypatch.setattr(services, "alive", lambda *_: True)
    monkeypatch.setattr(services, "process_in_job", lambda *_: True)
    return h, services.ServiceLifecycle(worker.request, clock=h.clock)


def test_model_token_changes_do_not_revoke_trading_role(tmp_path, monkeypatch):
    h, live = lifecycle(tmp_path, monkeypatch)
    assert live.stop_reason() is None
    h.owner.state["roles"]["model"] = {"token": "next-model", "generation": 4}
    h.owner.save("DEGRADED")
    assert live.stop_reason() is None
    h.owner.request_stop()
    assert live.stop_reason() == "manual_stop"
    h.owner.stop_path.unlink()
    assert live.stop_reason() == "manual_stop"


def test_own_generation_fence_revocation_is_permanent(tmp_path, monkeypatch):
    h, live = lifecycle(tmp_path, monkeypatch, role="model")
    assert live.stop_reason() is None
    h.owner.state["roles"]["model"]["token"] = "another-generation"
    h.owner.save()
    assert live.stop_reason() == "controller_failure"
    h.owner.state["roles"]["model"]["token"] = live.request["token"]
    h.owner.save()
    assert live.stop_reason() == "controller_failure"
    assert live.stop_detail["stage"] == "role_fence"


def test_stop_during_authority_read_wins_before_effects(tmp_path, monkeypatch):
    h, live = lifecycle(tmp_path, monkeypatch)
    read = services.read_bytes
    def stop_after_read(path):
        value = read(path)
        h.owner.request_stop()
        return value
    monkeypatch.setattr(services, "read_bytes", stop_after_read)
    assert live.stop_reason() == "manual_stop"
    assert live.stop_detail["stage"] == "after_authority_read"


def test_stop_before_first_worker_never_starts_either_role(tmp_path):
    h = Harness(tmp_path)
    h.owner.request_stop()
    assert h.run() == 0
    assert h.workers == []
    assert h.owner.state["stop_reason"] == "manual_stop"



def test_unchanged_release_still_replaces_trading_after_every_full_attempt(tmp_path):
    h = Harness(tmp_path, "unchanged")
    assert h.run() == 0
    traders = [w for w in h.workers if w.role == "trading"]
    assert len(traders) >= 3
    assert {w.request["release"]["manifest_sha256"] for w in traders} == {"a" * 64}
    assert [w.request["generation"] for w in traders] == list(range(1, len(traders) + 1))
    assert all(w.request["trading_rotation_version"] == services.TRADING_ROTATION_VERSION for w in traders)


def test_fifo_does_not_block_model_when_trading_drain_is_slow(tmp_path):
    h = Harness(tmp_path, "trading_slow")
    assert h.run() == 0
    models = [w for w in h.workers if w.role == "model"]
    traders = [w for w in h.workers if w.role == "trading"]
    assert len(models) > len(traders) >= 2
    boundaries = h.owner.state["completed_iterations"]
    assert [b["iteration_sequence"] for b in boundaries] == list(range(1, len(boundaries) + 1))
    assert len(boundaries) > h.owner.state["trading_completed_iteration"]
    assert traders[1].request["previous_checkpoint"]["rotation_request"]["iteration_sequence"] == 1
    for b in boundaries:
        path = h.owner.root / "completed-iterations" / ("%06d.json" % b["iteration_sequence"])
        assert json.loads(path.read_bytes()) == b


@pytest.mark.parametrize("mode", ["trading_invalid", "trading_wrong_sequence", "trading_crash", "trading_orphan"])
def test_terminal_trading_handoff_never_respawns_model_stays_independent(tmp_path, mode):
    h = Harness(tmp_path, mode)
    assert h.run() == 0
    assert len([w for w in h.workers if w.role == "trading"]) == 1
    assert len([w for w in h.workers if w.role == "model"]) > 2
    assert h.owner.state["roles"]["trading"]["status"] == "FAILED"
    assert h.owner.state["roles"]["trading"]["automatic_retry"] is False
    assert h.owner.state["trading_completed_iteration"] == 0


def test_stop_during_rotation_never_replaces_trading(tmp_path):
    h = Harness(tmp_path, "stop_rotation")
    assert h.run() == 0
    assert h.owner.state["stop_reason"] == "manual_stop"
    assert len([w for w in h.workers if w.role == "trading"]) == 1
    assert h.owner.state["trading_completed_iteration"] == 0


def test_missing_capability_rejected_before_trading_process(tmp_path):
    h = Harness(tmp_path)
    h.owner.root.mkdir(parents=True)
    h.owner.save("STARTING")
    selected = selection(tmp_path)
    selected.pop("trading_rotation_version")
    with pytest.raises(ValueError, match="rotation capability"):
        h.owner._start_role("trading", selected)
    assert h.workers == []


def test_trading_rotation_request_is_generation_fenced_and_stop_wins(tmp_path, monkeypatch):
    h, live = lifecycle(tmp_path, monkeypatch)
    role = h.owner.state["roles"]["trading"]
    h.owner.runtime["trading"]["ready"] = True
    role["status"] = "RUNNING"
    h.owner.state["completed_iterations"] = [{"iteration_sequence": 1, "boundary_id": "model-1:cycle-1",
        "boundary_cycle_id": "cycle-1", "model_generation": 1, "checkpoint_sha256": "c" * 64}]
    h.owner._request_trading_rotation()
    expected = role["rotation_request"]
    assert live.trading_rotation_request() == expected
    assert live.stop_reason() is None
    h.owner.request_stop()
    assert live.trading_rotation_request() is None


def test_stale_trading_rotation_request_permanently_revokes_role(tmp_path, monkeypatch):
    h, live = lifecycle(tmp_path, monkeypatch)
    role = h.owner.state["roles"]["trading"]
    role["rotation_request"] = {"schema": services.TRADING_ROTATION_VERSION,
        **live.header(), "token": "retired-token"}
    h.owner.save()
    with pytest.raises(ValueError, match="Stale Scout"):
        live.trading_rotation_request()
    assert live.stop_reason() == "controller_failure"
    role.pop("rotation_request")
    h.owner.save()
    assert live.trading_rotation_request() is None
