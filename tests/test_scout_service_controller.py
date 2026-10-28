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
        "lifecycle_protocol": services.PROTOCOL}


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
            return None
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
        return self.harness.mode != "orphan" or self.closed

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
            return selection(tmp_path, "a" if self.selections < 3 else "b")
        self.owner = services.ServiceController(selector=tmp_path / "independent.json", repository=tmp_path,
            datastore=tmp_path, wake_at=self.clock(), deadline=self.clock() + timedelta(seconds=2),
            clock=self.clock, sleep=self.clock.sleep, monotonic=lambda: self.clock.elapsed,
            worker_factory=lambda command, **kwargs: FakeWorker(self, command, **kwargs), select=select,
            drain_timeout=0.3, startup_timeout=1)

    def run(self):
        return self.owner.run(lock_factory=lambda *_: nullcontext())


def test_model_rotation_keeps_one_trading_process_and_original_run_identity(tmp_path):
    h = Harness(tmp_path)
    assert h.run() == 0
    traders = [w for w in h.workers if w.role == "trading"]
    models = [w for w in h.workers if w.role == "model"]
    assert len(traders) == 1 and len(models) > 1
    assert {w.request["session_id"] for w in h.workers} == {h.owner.run_id}
    assert {w.request["deadline"] for w in h.workers} == {h.owner.state["deadline"]}
    assert {w.request["started_at"] for w in h.workers} == {h.owner.state["started_at"]}
    assert models[1].request["previous_checkpoint"]["boundary_cycle_id"] == "cycle-1"
    assert traders[0].request["release"] != models[-1].request["release"]
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
    assert h.run() == 0
    assert h.recovery_ticks > 10
    assert len(h.workers) == 2
    assert "private recipe" in h.owner.state["roles"]["model"]["error"]


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
