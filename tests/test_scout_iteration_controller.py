"""Private fake-process regressions for the Scout logical-run controller."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import psutil
import pytest

from tools import scout_iteration_controller as controller
from tools.scout_release import PROTOCOL, atomic_json


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 26, 15, 0, tzinfo=timezone.utc)
        self.elapsed = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.elapsed += seconds
        self.now += timedelta(seconds=seconds)


def selection(tmp_path, name):
    return dict(release_root=str(tmp_path / name), manifest_sha256=name * 64,
                source_root=str(tmp_path / name / "source"), recipe_path=str(tmp_path / (name + ".json")),
                recipe_file_sha256="f" * 64, symbols=[f"SYM{n}" for n in range(11)],
                lifecycle_protocol=PROTOCOL)


class FakeWorker:
    def __init__(self, harness, command, **kwargs):
        self.h = harness
        self.request = json.loads(Path(command[-1]).read_bytes())
        self.root = Path(self.request["generation_root"])
        self.pid = 10000 + len(harness.workers)
        self.create_time = float(self.pid)
        self.job_name = "fake-job-" + str(self.pid)
        self.returncode = None
        self.resumed = False
        self.ticks = 0
        self.children = harness.mode == "orphan"
        assert kwargs["start_suspended"] is True
        assert kwargs["env"]["PYTHONPATH"] == ""
        assert kwargs["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
        assert not any(not w.closed for w in harness.workers), "overlapping generations"
        self.closed = False
        self.terminated = False
        harness.workers.append(self)
        harness.event("created", self)

    def header(self):
        return {k: self.request[k] for k in ("session_id", "generation", "token", "release")}

    def resume(self):
        assert not self.resumed
        self.resumed = True
        self.h.event("resume", self)

    def poll(self):
        if self.returncode is not None or not self.resumed:
            return self.returncode
        self.ticks += 1
        if self.h.mode == "startup_timeout":
            return None
        if self.ticks == 1:
            atomic_json(self.root / "phase.json", {**self.header(), "phase": "ready"})
            self.h.event("running", self)
            return None
        if self.ticks == 2:
            atomic_json(self.root / "phase.json", {**self.header(), "phase": "draining"})
            self.h.event("draining", self)
            return None
        if self.h.mode == "drain_timeout":
            return None
        stop = self.h.owner.stop_reason()
        result = {**self.header(), "status": "STOPPED" if stop else "PLANNED_RESTART"}
        if stop:
            result["stop_reason"] = stop
        self.returncode = 0 if stop else controller.PLANNED_EXIT
        checkpoint = {**self.header(), "schema": "scout-iteration-checkpoint-v1",
                      "session_id": self.request["session_id"], "owned_workers_drained": True,
                      "boundary_cycle_id": "cycle-" + str(self.request["generation"]),
                      "boundary_kind": "full_symbol_attempt_with_durable_deferrals",
                      "symbols": self.request["release"]["symbols"],
                      "cycles": [{"cycle_id": "cycle-" + str(self.request["generation"]), "status": "completed"}],
                      "pending_queue": [{"forecast_id": "pending", "status": "pending"}],
                      "completed_queue": [{"forecast_id": "complete", "status": "executed"}],
                      "broker_retry": [["broker", "2026-10-26T15:03:00+00:00"]]}
        if self.h.mode == "crash":
            result.update(status="FAILED", error="precise fake failure")
            self.returncode = 9
        elif self.h.mode == "unexplained_stop":
            result.update(status="STOPPED", stop_reason=None)
            self.returncode = 0
        elif self.h.mode == "stale_result":
            result["token"] = "stale"
        elif self.h.mode == "bad_checkpoint":
            checkpoint["schema"] = "unsupported-version"
        elif self.h.mode == "stale_checkpoint":
            checkpoint["generation"] += 1
        elif self.h.mode == "wrong_release":
            result["release"] = selection(self.h.root, "wrong")
        atomic_json(self.root / "result.json", result)
        if self.h.mode != "partial_cycle":
            atomic_json(self.root / "checkpoint.json", checkpoint)
        self.h.event("exited", self)
        return self.returncode

    def job_empty(self):
        return self.returncode is not None and not self.children

    def terminate(self, timeout):
        self.terminated = True
        self.returncode = 70
        self.children = False
        self.h.event("terminated", self)

    def close(self, timeout):
        if not self.job_empty():
            self.terminate(timeout)
        self.closed = True
        self.h.event("closed", self)


class Harness:
    def __init__(self, tmp_path, monkeypatch, *, mode="normal", stop_stage=None, generations=3,
                 update=False, end_stage=None):
        self.root, self.clock = tmp_path, Clock()
        self.mode, self.stop_stage, self.end_stage = mode, stop_stage, end_stage
        self.generations, self.update = generations, update
        self.workers, self.events, self.selection_calls = [], [], 0
        self.current = selection(tmp_path, "a")
        self.locked = False
        self.owner = controller.Controller(selector=tmp_path / "selector.json", repository=tmp_path,
            datastore=tmp_path / "private", wake_at=self.clock(), deadline=self.clock() + timedelta(hours=1),
            clock=self.clock, sleep=self.sleep, worker_factory=lambda *a, **kw: FakeWorker(self, *a, **kw),
            select=self.select, startup_timeout=1, drain_timeout=1, backoff_seconds=0.25)
        monkeypatch.setattr(controller.time, "monotonic", lambda: self.clock.elapsed)

    def select(self, _):
        self.selection_calls += 1
        if self.selection_calls > 1:
            self.event("selected")
            if self.mode == "bad_release" and self.selection_calls > 2:
                raise ValueError("Exact selected manifest failed fake validation")
        return self.current

    @contextmanager
    def locks(self, *_):
        if self.locked:
            raise RuntimeError("duplicate launcher")
        self.locked = True
        try:
            yield
        finally:
            self.locked = False

    def event(self, stage, worker=None):
        self.events.append((stage, self.owner.state["generation"]))
        if self.stop_stage == stage:
            self.owner.request_stop()
        if self.end_stage == stage:
            self.clock.now = controller.instant(self.owner.state["deadline"])
        if self.update and stage == "running" and self.owner.state["generation"] == 1:
            self.current = selection(self.root, "b")

    def sleep(self, seconds):
        if self.owner.state["status"] == "BETWEEN_ITERATIONS":
            self.event("backoff")
            if self.owner.state["generation"] >= self.generations:
                self.owner.request_stop()
        if self.owner.state["status"] == "WAITING":
            self.event("waiting")
        self.clock.advance(seconds)

    def run(self):
        return self.owner.run(lock_factory=self.locks)


def test_one_activation_rotates_every_complete_iteration_even_unchanged(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    assert h.run() == 0
    assert len(h.workers) == 3
    assert [w.request["generation"] for w in h.workers] == [1, 2, 3]
    assert len({w.request["token"] for w in h.workers}) == 3
    assert len({w.request["session_id"] for w in h.workers}) == 1
    assert len({w.request["started_at"] for w in h.workers}) == 1
    assert len({w.request["deadline"] for w in h.workers}) == 1
    assert all(w.closed and not w.terminated for w in h.workers)
    assert h.selection_calls == 4
    assert h.owner.state["stop_reason"] == "manual_stop"
    assert h.workers[0].request["previous_checkpoint"] is None
    for previous, successor in zip(h.workers, h.workers[1:]):
        assert successor.request["previous_checkpoint"] == json.loads((previous.root / "checkpoint.json").read_bytes())


def test_release_selected_during_a_iteration_loads_b_next_generation(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, update=True)
    assert h.run() == 0
    assert [Path(w.request["release"]["release_root"]).name for w in h.workers] == ["a", "b", "b"]
    assert h.workers[0].request["release"] != h.workers[1].request["release"]


@pytest.mark.parametrize("stage", ["selected", "created", "resume", "running", "draining", "exited", "closed", "backoff"])
def test_manual_stop_wins_each_generation_stage(tmp_path, monkeypatch, stage):
    h = Harness(tmp_path, monkeypatch, stop_stage=stage)
    assert h.run() == 0
    assert len(h.workers) <= 1
    assert h.owner.stop_path.exists()
    assert h.owner.state["status"] == "STOPPED"
    assert h.owner.state["stop_reason"] == "manual_stop"
    assert all(w.closed for w in h.workers)
    if stage == "created":
        assert not h.workers[0].resumed


@pytest.mark.parametrize("stage", ["selected", "created", "resume", "running", "draining", "exited", "closed", "backoff"])
def test_session_end_wins_each_generation_stage(tmp_path, monkeypatch, stage):
    h = Harness(tmp_path, monkeypatch, end_stage=stage)
    assert h.run() == 0
    assert len(h.workers) <= 1
    assert h.owner.state["stop_reason"] == "session_end"
    assert not h.owner.stop_path.exists()
    assert all(w.closed for w in h.workers)


def test_stop_before_run_is_not_cleared_and_never_creates_worker(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    h.owner.request_stop()
    assert h.run() == 0
    assert not h.workers
    assert h.owner.stop_path.exists()
    assert h.owner.state["stop_reason"] == "manual_stop"


def test_stop_while_waiting_does_not_shift_to_later_session(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, stop_stage="waiting")
    h.owner.state["wake_at"] = (h.clock() + timedelta(minutes=5)).isoformat()
    deadline = h.owner.state["deadline"]
    assert h.run() == 0
    assert not h.workers
    assert h.owner.state["deadline"] == deadline
    assert h.owner.state["stop_reason"] == "manual_stop"


@pytest.mark.parametrize("mode, error_type, message", [
    ("startup_timeout", TimeoutError, "startup timed out"),
    ("drain_timeout", TimeoutError, "drain timed out"),
    ("crash", RuntimeError, "precise fake failure"),
    ("orphan", RuntimeError, "owned child workers"),
    ("unexplained_stop", RuntimeError, "did not complete a planned boundary"),
    ("stale_result", ValueError, "token"),
    ("bad_checkpoint", ValueError, "Unsupported Scout checkpoint"),
    ("stale_checkpoint", ValueError, "generation"),
    ("wrong_release", ValueError, "loaded release differs"),
    ("partial_cycle", FileNotFoundError, "checkpoint.json"),
    ("bad_release", ValueError, "Exact selected manifest"),
])
def test_unsafe_handoff_is_bounded_truthful_and_never_respawned(tmp_path, monkeypatch, mode, error_type, message):
    h = Harness(tmp_path, monkeypatch, mode=mode)
    with pytest.raises(error_type, match=message):
        h.run()
    assert len(h.workers) == 1
    assert h.owner.state["status"] == "FAILED"
    assert h.owner.state["automatic_retry"] is False
    assert message in h.owner.state["error"]
    assert h.clock.elapsed < 10
    assert all(w.closed for w in h.workers)
    assert (h.workers[0].root / "request.json").exists()


def test_duplicate_launch_lock_never_calls_worker_factory(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    h.locked = True
    with pytest.raises(RuntimeError, match="duplicate launcher"):
        h.run()
    assert not h.workers
    assert not h.owner.active_path.exists()


def test_still_alive_prior_controller_prevents_new_run(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    atomic_json(h.owner.active_path, {"schema": controller.STATE_SCHEMA,
        "controller_pid": os.getpid(), "controller_create_time": psutil.Process().create_time()})
    with pytest.raises(RuntimeError, match="Another Scout controller"):
        h.run()
    assert not h.workers


def test_dead_controller_with_remaining_child_tree_prevents_new_run(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    atomic_json(h.owner.active_path, {"schema": controller.STATE_SCHEMA,
        "controller_pid": 99999999, "controller_create_time": 0,
        "worker": {"pid": 99999998, "create_time": 0, "job_name": "old-tree"}})
    monkeypatch.setattr(controller, "alive", lambda *_: False)
    monkeypatch.setattr(controller, "inspect_job", lambda _: True)
    with pytest.raises(RuntimeError, match="worker tree has not exited"):
        h.run()
    assert not h.workers


def lifecycle_fixture(tmp_path, monkeypatch):
    now = datetime(2026, 10, 26, 15, tzinfo=timezone.utc)
    request = {"session_id": "run", "generation": 2, "token": "token", "release": {"id": "r"},
               "started_at": now.isoformat(), "wake_at": now.isoformat(),
               "deadline": (now + timedelta(hours=1)).isoformat(), "previous_checkpoint": {},
               "drain_timeout_seconds": 1, "generation_root": str(tmp_path),
               "stop_path": str(tmp_path / "stop"), "active_path": str(tmp_path / "active.json"),
               "controller_pid": os.getpid(), "controller_create_time": psutil.Process().create_time()}
    state = {**request, "status": "RUNNING", "worker": {"pid": os.getpid(), "create_time": psutil.Process().create_time(), "job_name": "fixture-job"}}
    own_pid, own_birth = os.getpid(), psutil.Process().create_time()
    monkeypatch.setattr(controller, "alive", lambda pid, birth: pid == own_pid and birth == own_birth)
    monkeypatch.setattr(controller, "process_in_job", lambda pid, job: pid == own_pid and job == "fixture-job")
    atomic_json(request["active_path"], state)
    return controller.WorkerLifecycle(request, clock=lambda: now), state


@pytest.mark.parametrize("mutation", ["token", "generation", "session_id", "pid", "birth", "controller_dead", "stopping", "failed", "manual_stop", "deadline", "unreadable"])
def test_worker_effect_fence_rejects_stale_or_stopped_authority(tmp_path, monkeypatch, mutation):
    lifecycle, state = lifecycle_fixture(tmp_path, monkeypatch)
    assert lifecycle.stop_reason() is None
    if mutation in {"token", "generation", "session_id"}:
        state[mutation] = "changed"
    elif mutation == "pid":
        state["worker"]["pid"] += 1
    elif mutation == "birth":
        state["worker"]["create_time"] += 1
    elif mutation == "controller_dead":
        monkeypatch.setattr(controller, "alive", lambda *_: False)
    elif mutation == "stopping":
        state.update(status="STOPPING", stop_reason="manual_stop")
    elif mutation == "failed":
        state.update(status="FAILED", stop_reason="unexpected_failure")
    elif mutation == "manual_stop":
        Path(lifecycle.request["stop_path"]).touch()
    elif mutation == "deadline":
        lifecycle.clock = lambda: controller.instant(lifecycle.deadline)
    if mutation == "unreadable":
        Path(lifecycle.request["active_path"]).write_text("partial")
    else:
        atomic_json(lifecycle.request["active_path"], state)
    assert lifecycle.stop_reason() is not None
    with pytest.raises(InterruptedError, match="no effect authority"):
        lifecycle.ready()
    with pytest.raises(InterruptedError, match="boundary interrupted"):
        lifecycle.checkpoint({"schema": "scout-iteration-checkpoint-v1"})
    assert not (tmp_path / "checkpoint.json").exists()


def test_actual_atomic_installer_selection_is_loaded_on_next_fake_generation(tmp_path, monkeypatch):
    from tools import scout_release as release
    h = Harness(tmp_path, monkeypatch)
    installations = {}
    for name in ("a", "b"):
        root = tmp_path / ("installed-" + name)
        source = root / "source"
        source.mkdir(parents=True)
        (source / "identity.py").write_text("RELEASE = '" + name + "'\n", encoding="utf-8")
        recipe = root / "recipe.json"
        atomic_json(recipe, {"pc": "Scout", "symbols": [f"SYM{i}" for i in range(11)]})
        atomic_json(root / "manifest.json", {"source_root": str(source), "lifecycle_protocol": PROTOCOL,
            "files": {"identity.py": release.sha256((source / "identity.py").read_bytes())},
            "recipe_path": str(recipe), "recipe_file_sha256": release.sha256(recipe.read_bytes())})
        installations[name] = root, release.sha256((root / "manifest.json").read_bytes())
    release.select_release(h.owner.selector, *installations["a"])
    h.owner.select = release.resolve_selection
    original_event = h.event

    def install_during_iteration(stage, worker=None):
        original_event(stage, worker)
        if stage == "running" and h.owner.state["generation"] == 1:
            before = h.owner.selector.read_bytes()
            release.select_release(h.owner.selector, *installations["b"],
                                   expected_selection_sha256=release.sha256(before))

    h.event = install_during_iteration
    assert h.run() == 0
    assert [Path(w.request["release"]["release_root"]).name for w in h.workers] == ["installed-a", "installed-b", "installed-b"]
    assert release.validate_release(*installations["a"])["manifest_sha256"] == installations["a"][1]


def test_bad_worker_release_fails_before_any_application_import(tmp_path, monkeypatch):
    lifecycle, state = lifecycle_fixture(tmp_path, monkeypatch)
    lifecycle.request["release"] = lifecycle.release = controller.release_identity(selection(tmp_path, "a"))
    state["release"] = lifecycle.release
    atomic_json(lifecycle.request["active_path"], state)
    request_path = tmp_path / "request.json"
    atomic_json(request_path, lifecycle.request)
    monkeypatch.setattr(controller, "WorkerLifecycle", lambda _: lifecycle)
    imported = []
    monkeypatch.setattr(controller, "import_release", lambda _: imported.append(True))

    def invalid(*_):
        raise ValueError("Exact manifest invalid before import")

    monkeypatch.setattr(controller, "validate_release", invalid)
    with pytest.raises(ValueError, match="Exact manifest invalid"):
        controller.worker_main(request_path)
    assert imported == []
    result = json.loads((tmp_path / "result.json").read_bytes())
    assert result["status"] == "FAILED"
    assert result["error"] == "Exact manifest invalid before import"


def test_stop_after_import_before_application_start_prevents_effects(tmp_path, monkeypatch):
    from types import SimpleNamespace
    lifecycle, state = lifecycle_fixture(tmp_path, monkeypatch)
    selected = selection(tmp_path, "a")
    lifecycle.request["release"] = lifecycle.release = controller.release_identity(selected)
    state["release"] = lifecycle.release
    atomic_json(lifecycle.request["active_path"], state)
    request_path = tmp_path / "request.json"
    atomic_json(request_path, lifecycle.request)
    monkeypatch.setattr(controller, "WorkerLifecycle", lambda _: lifecycle)
    monkeypatch.setattr(controller, "validate_release", lambda *_: selected)
    effects = []

    def imported(_):
        Path(lifecycle.request["stop_path"]).touch()
        return SimpleNamespace(run_manual=lambda **_: effects.append(True)), None, None

    monkeypatch.setattr(controller, "import_release", imported)
    with pytest.raises(InterruptedError, match="authority revoked before startup"):
        controller.worker_main(request_path)
    assert effects == []
    assert json.loads((tmp_path / "result.json").read_bytes())["error_type"] == "InterruptedError"


@pytest.mark.parametrize("mutation", ["token", "generation", "release", "bad_json", "missing_state", "worker_birth"])
def test_revoked_generation_cannot_regain_authority_after_state_repairs(tmp_path, monkeypatch, mutation):
    lifecycle, original = lifecycle_fixture(tmp_path, monkeypatch)
    path = Path(lifecycle.request["active_path"])
    before = path.read_bytes()
    state = json.loads(before)
    if mutation == "token":
        state["token"] = "stale"
    elif mutation == "generation":
        state["generation"] += 1
    elif mutation == "release":
        state["release"] = {"another": "release"}
    elif mutation == "worker_birth":
        state["worker"]["create_time"] += 1
    if mutation == "bad_json":
        path.write_text("incomplete-state")
    elif mutation == "missing_state":
        path.unlink()
    else:
        atomic_json(path, state)
    assert lifecycle.stop_reason() == "controller_failure"
    path.write_bytes(before)
    assert lifecycle.stop_reason() == "controller_failure"
    with pytest.raises(InterruptedError, match="no effect authority"):
        lifecycle.ready()
    with pytest.raises(InterruptedError, match="boundary interrupted"):
        lifecycle.checkpoint({"schema": "scout-iteration-checkpoint-v1"})


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_transient_authority_read_sharing_is_bounded_without_false_revocation(tmp_path, monkeypatch, winerror):
    from tools import scout_release as release
    lifecycle, _ = lifecycle_fixture(tmp_path, monkeypatch)
    active = Path(lifecycle.request["active_path"])
    original_read = release._read_once
    reads, elapsed = [], [0.0]
    monkeypatch.setattr(release.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(release.time, "sleep", lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))

    def sharing_once(path):
        path = Path(path)
        if path == active:
            reads.append(path)
            if len(reads) == 1:
                error = PermissionError("transient active-state read sharing")
                error.winerror = winerror
                raise error
        return original_read(path)

    monkeypatch.setattr(release, "_read_once", sharing_once)
    assert lifecycle.stop_reason() is None
    assert len(reads) == 2
    assert elapsed[0] > 0
    assert lifecycle.stop_reason() is None


@pytest.mark.parametrize("authority", ["manual_stop", "session_end"])
def test_stop_or_deadline_during_authority_read_retry_prevents_effects(tmp_path, monkeypatch, authority):
    from tools import scout_release as release
    lifecycle, _ = lifecycle_fixture(tmp_path, monkeypatch)
    active = Path(lifecycle.request["active_path"])
    original_read = release._read_once
    reads, elapsed = [], [0.0]
    monkeypatch.setattr(release.time, "monotonic", lambda: elapsed[0])

    def pause(seconds):
        elapsed[0] += seconds
        if authority == "manual_stop":
            Path(lifecycle.request["stop_path"]).touch()
        else:
            lifecycle.clock = lambda: controller.instant(lifecycle.deadline)

    def sharing_once(path):
        path = Path(path)
        if path == active:
            reads.append(path)
            if len(reads) == 1:
                error = PermissionError("active-state read sharing while stop arrives")
                error.winerror = 32
                raise error
        return original_read(path)

    monkeypatch.setattr(release.time, "sleep", pause)
    monkeypatch.setattr(release, "_read_once", sharing_once)
    assert lifecycle.stop_reason() == authority
    assert lifecycle.stop_detail == {"reason": authority, "stage": "after_authority_read"}
    with pytest.raises(InterruptedError, match="no effect authority"):
        lifecycle.ready()
    assert not (tmp_path / "phase.json").exists()


def test_permanent_authority_read_failure_is_latched_even_after_reader_recovers(tmp_path, monkeypatch):
    from tools import scout_release as release
    lifecycle, _ = lifecycle_fixture(tmp_path, monkeypatch)
    active = Path(lifecycle.request["active_path"])
    original_read = release._read_once
    elapsed, reads = [0.0], []
    monkeypatch.setattr(release.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(release.time, "sleep", lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))

    def held(path):
        path = Path(path)
        if path == active:
            reads.append(path)
            error = PermissionError("precise permanent authority-state sharing failure")
            error.winerror = 32
            raise error
        return original_read(path)

    monkeypatch.setattr(release, "_read_once", held)
    assert lifecycle.stop_reason() == "controller_failure"
    assert 0 < elapsed[0] <= 1.01
    assert 2 <= len(reads) <= 102
    expected_detail = {"reason": "controller_failure", "stage": "authority_read",
                       "error_type": "PermissionError", "error": "precise permanent authority-state sharing failure",
                       "winerror": 32}
    assert lifecycle.stop_detail == expected_detail
    monkeypatch.setattr(release, "_read_once", original_read)
    assert lifecycle.stop_reason() == "controller_failure"
    assert lifecycle.stop_detail == expected_detail
    with pytest.raises(InterruptedError, match="boundary interrupted"):
        lifecycle.checkpoint({"schema": "scout-iteration-checkpoint-v1"})


def test_worker_initial_authority_failure_is_failed_not_a_normal_stop(tmp_path, monkeypatch):
    lifecycle, _ = lifecycle_fixture(tmp_path, monkeypatch)
    request_path = tmp_path / "request.json"
    atomic_json(request_path, lifecycle.request)
    monkeypatch.setattr(controller, "WorkerLifecycle", lambda _: lifecycle)
    monkeypatch.setattr(controller, "alive", lambda *_: False)

    def forbidden(*_):
        raise AssertionError("No imports or release work after revoked initial authority")

    monkeypatch.setattr(controller, "validate_release", forbidden)
    monkeypatch.setattr(controller, "import_release", forbidden)
    assert controller.worker_main(request_path) == 1
    result = json.loads((tmp_path / "result.json").read_bytes())
    assert result["status"] == "FAILED" and result["stop_reason"] == "controller_failure"
    assert result["authority_failure"] == {"reason": "controller_failure", "stage": "controller_identity"}


@pytest.mark.parametrize("winerror", [None, 13])
def test_nonsharing_authority_read_failure_is_immediate_and_latched(tmp_path, monkeypatch, winerror):
    from tools import scout_release as release
    lifecycle, _ = lifecycle_fixture(tmp_path, monkeypatch)
    active = Path(lifecycle.request["active_path"])
    original_read = release._read_once
    reads, sleeps = [], []
    monkeypatch.setattr(release.time, "sleep", sleeps.append)

    def failed(path):
        path = Path(path)
        if path == active:
            reads.append(path)
            error = PermissionError("nonsharing authority-read rejection")
            if winerror is not None:
                error.winerror = winerror
            raise error
        return original_read(path)

    monkeypatch.setattr(release, "_read_once", failed)
    assert lifecycle.stop_reason() == "controller_failure"
    assert reads == [active] and sleeps == []
    assert lifecycle.stop_detail["winerror"] == winerror
    assert lifecycle.stop_detail["error"] == "nonsharing authority-read rejection"
    monkeypatch.setattr(release, "_read_once", original_read)
    assert lifecycle.stop_reason() == "controller_failure"


def test_concurrent_healthy_reader_cannot_override_another_threads_revocation(tmp_path, monkeypatch):
    import threading
    lifecycle, _ = lifecycle_fixture(tmp_path, monkeypatch)
    active = Path(lifecycle.request["active_path"])
    real_read = controller.read_bytes
    entered, resume = threading.Event(), threading.Event()
    results = []

    def paused_read(path):
        raw = real_read(path)
        if Path(path) == active and threading.current_thread().name == "healthy-authority-reader":
            entered.set()
            assert resume.wait(2), "test failed to resume authority reader"
        return raw

    monkeypatch.setattr(controller, "read_bytes", paused_read)
    reader = threading.Thread(target=lambda: results.append(lifecycle.stop_reason()), name="healthy-authority-reader")
    reader.start()
    try:
        assert entered.wait(2), "healthy reader did not reach deterministic pause"
        assert lifecycle._revoke("controller_failure", stage="concurrent_revocation", error="exact original fault") == "controller_failure"
        assert lifecycle._revoke("manual_stop", stage="later_reason") == "controller_failure"
    finally:
        resume.set()
        reader.join(2)
    assert not reader.is_alive()
    assert results == ["controller_failure"]
    assert lifecycle.stop_detail == {"reason": "controller_failure", "stage": "concurrent_revocation", "error": "exact original fault"}
    with pytest.raises(InterruptedError, match="no effect authority"):
        lifecycle.ready()
    assert not (tmp_path / "phase.json").exists()
