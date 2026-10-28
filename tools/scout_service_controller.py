"""Scout 1B process controller. System 1A is never owned or started here.

Only --live starts a new manually authorized logical trading run. The trading
process remains responsive while a separate model process completes full-scope
iterations. Planned model replacement keeps the same run, day and deadline;
terminal role failures are recorded without an automatic crash respawn.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys
import time
import uuid

import psutil

if __package__:
    from .scout_release import atomic_json, read_bytes, resolve_selection, validate_release, sha256
    from .scout_process import ManagedWorker, inspect_job, process_in_job
    from .scout_iteration_controller import (WorkerLifecycle, alive, application_locks,
        import_release, instant, release_identity, utcnow)
else:
    from scout_release import atomic_json, read_bytes, resolve_selection, validate_release, sha256
    from scout_process import ManagedWorker, inspect_job, process_in_job
    from scout_iteration_controller import (WorkerLifecycle, alive, application_locks,
        import_release, instant, release_identity, utcnow)

PROTOCOL = "scout-independent-v1"
STATE_SCHEMA = "scout-independent-logical-run-v1"
CHECKPOINT_SCHEMA = "scout-independent-model-checkpoint-v1"
PLANNED_EXIT = 75


def selected_release(selector):
    return resolve_selection(selector, protocol=PROTOCOL)


class ServiceLifecycle(WorkerLifecycle):
    """Role-specific authority. A revoked role can never regain send authority."""

    def __init__(self, request, *, clock=utcnow):
        super().__init__(request, clock=clock)
        self.role = request["role"]
        if self.role not in {"model", "trading"}:
            raise ValueError("Unknown independent Scout process role")

    def header(self):
        return {**super().header(), "role": self.role}

    def stop_reason(self):
        if self._latched_reason is not None:
            return self._latched_reason
        request = self.request
        try:
            if Path(request["stop_path"]).exists():
                return self._revoke("manual_stop", stage="stop_latch")
            if instant(self.clock()) >= instant(self.deadline):
                return self._revoke("session_end", stage="deadline")
            if not alive(request["controller_pid"], request["controller_create_time"]):
                return self._revoke("controller_failure", stage="controller_identity")
            state = json.loads(read_bytes(request["active_path"]))
            if Path(request["stop_path"]).exists():
                return self._revoke("manual_stop", stage="after_authority_read")
            if instant(self.clock()) >= instant(self.deadline):
                return self._revoke("session_end", stage="after_authority_read")
            for key in ("session_id", "controller_pid", "controller_create_time", "deadline"):
                if state.get(key) != request[key]:
                    return self._revoke("controller_failure", stage="run_fence", field=key)
            role = state["roles"][self.role]
            for key in ("generation", "token", "release"):
                if role.get(key) != request[key]:
                    return self._revoke("controller_failure", stage="role_fence", field=key)
            worker = role.get("worker") or {}
            if (state.get("schema") != STATE_SCHEMA or state.get("protocol") != PROTOCOL
                    or state.get("status") not in {"STARTING", "RUNNING", "DEGRADED"}
                    or role.get("status") not in {"STARTING", "RUNNING"}
                    or not alive(worker["pid"], worker["create_time"])
                    or not process_in_job(os.getpid(), worker["job_name"])):
                reason = state.get("stop_reason")
                return self._revoke(reason if reason in {"manual_stop", "session_end"}
                                    else "controller_failure", stage="role_identity_or_status")
        except (OSError, ValueError, KeyError, psutil.Error) as exc:
            if Path(request["stop_path"]).exists():
                return self._revoke("manual_stop", stage="failed_authority_read")
            if instant(self.clock()) >= instant(self.deadline):
                return self._revoke("session_end", stage="failed_authority_read")
            return self._revoke("controller_failure", stage="authority_read",
                error_type=type(exc).__name__, error=str(exc), winerror=getattr(exc, "winerror", None))
        return self._latched_reason


class ServiceController:
    def __init__(self, *, selector, repository, datastore, wake_at, deadline,
                 clock=utcnow, sleep=time.sleep, worker_factory=ManagedWorker,
                 select=selected_release, startup_timeout=300, drain_timeout=120,
                 monotonic=time.monotonic):
        self.selector, self.repository, self.datastore = Path(selector), Path(repository), Path(datastore)
        self.clock, self.sleep, self.select, self.worker_factory = clock, sleep, select, worker_factory
        self.startup_timeout, self.drain_timeout, self.monotonic = startup_timeout, drain_timeout, monotonic
        self.run_id = "manual-scout-15m:" + uuid.uuid4().hex
        self.root = self.datastore / "intraday/scout/runs" / self.run_id.replace(":", "-")
        # Preserve the existing stop entrypoint and cross-topology ownership.
        self.active_path = self.datastore / "intraday/scout/controller.json"
        self.stop_path = self.root / "manual.stop"
        self.workers = {}
        self.runtime = {}
        self.recipe_hash = None
        self._reported_status = None
        self.state = {"schema": STATE_SCHEMA, "protocol": PROTOCOL, "session_id": self.run_id,
            "started_at": instant(clock()).isoformat(), "wake_at": instant(wake_at).isoformat(),
            "deadline": instant(deadline).isoformat(), "controller_pid": os.getpid(),
            "controller_create_time": psutil.Process().create_time(), "roles": {},
            "stop_path": str(self.stop_path), "status": "WAITING", "automatic_daily_restart": False,
            "system_1a_owned": False}

    def save(self, status=None, **extra):
        self.state.update(updated_at=instant(self.clock()).isoformat(), **extra)
        if status is not None:
            self.state["status"] = status
        atomic_json(self.root / "run.json", self.state)
        atomic_json(self.active_path, self.state)
        if self._reported_status != self.state["status"]:
            print("Scout 1B " + self.state["status"] + "; run " + self.run_id, flush=True)
            self._reported_status = self.state["status"]

    def stop_reason(self):
        if self.stop_path.exists():
            return "manual_stop"
        if instant(self.clock()) >= instant(self.state["deadline"]):
            return "session_end"
        return None

    def request_stop(self):
        atomic_json(self.stop_path, {"session_id": self.run_id, "reason": "manual_stop",
                                   "requested_at": instant(self.clock()).isoformat()})

    def _prior_run_exited(self):
        if not self.active_path.exists():
            return
        prior = json.loads(read_bytes(self.active_path))
        if prior.get("schema") not in {STATE_SCHEMA, "scout-logical-run-v1"}:
            raise ValueError("Unknown prior Scout controller state; preserve for review")
        if alive(prior["controller_pid"], prior["controller_create_time"]):
            raise RuntimeError("Another Scout controller still owns the run")
        workers = [prior.get("worker")] + [r.get("worker") for r in prior.get("roles", {}).values()]
        for worker in filter(None, workers):
            if alive(worker["pid"], worker["create_time"]) or inspect_job(worker["job_name"]):
                raise RuntimeError("Prior Scout process tree has not exited; no overlapping launch")

    def _start_role(self, role, selected, checkpoint=None):
        if self.stop_reason():
            return
        if selected.get("lifecycle_protocol") != PROTOCOL:
            raise ValueError("Independent Scout role requires the independent controller protocol")
        if self.recipe_hash is None:
            self.recipe_hash = selected["recipe_file_sha256"]
        if selected["recipe_file_sha256"] != self.recipe_hash:
            raise ValueError("Model replacement changed the logical run's private recipe")
        prior = self.state["roles"].get(role, {})
        generation = prior.get("generation", 0) + 1
        role_root = self.root / role / str(generation)
        role_root.mkdir(parents=True)
        role_state = {"generation": generation, "token": uuid.uuid4().hex,
                      "release": release_identity(selected), "status": "STARTING", "worker": None}
        request = {**{k: self.state[k] for k in ("session_id", "started_at", "wake_at", "deadline",
                   "controller_pid", "controller_create_time", "stop_path")}, **role_state,
                   "role": role, "active_path": str(self.active_path), "repository": str(self.repository),
                   "datastore": str(self.datastore), "generation_root": str(role_root),
                   "previous_checkpoint": checkpoint, "drain_timeout_seconds": self.drain_timeout}
        request_path = role_root / "request.json"
        atomic_json(request_path, request)
        self.state["roles"][role] = role_state
        self.save()
        if self.stop_reason():
            return
        worker = self.worker_factory([sys.executable, "-B", str(Path(__file__).resolve()),
            "--worker", str(request_path)], cwd=str(role_root),
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=""),
            log_path=role_root / (role + ".log"), start_suspended=True)
        self.workers[role] = worker
        self.runtime[role] = {"root": role_root, "started": self.monotonic(), "ready": False, "draining": None}
        role_state["worker"] = {"pid": worker.pid, "create_time": worker.create_time, "job_name": worker.job_name}
        self.save()
        if self.stop_reason():
            worker.terminate(timeout=self.drain_timeout)
            return
        worker.resume()

    def _validate_header(self, role, receipt):
        expected = self.state["roles"][role]
        if receipt.get("session_id") != self.run_id or receipt.get("role") != role:
            raise ValueError("Scout role receipt changed its logical run or role")
        for key in ("generation", "token", "release"):
            if receipt.get(key) != expected[key]:
                raise ValueError("Stale Scout role receipt: " + key)

    def _checkpoint(self, role, root):
        raw = read_bytes(root / "checkpoint.json")
        checkpoint = json.loads(raw)
        self._validate_header(role, checkpoint)
        if (checkpoint.get("schema") != CHECKPOINT_SCHEMA
                or checkpoint.get("owned_workers_drained") is not True
                or checkpoint.get("boundary_kind") != "full_symbol_attempt_with_durable_deferrals"
                or not checkpoint.get("boundary_cycle_id")
                or checkpoint.get("symbols") != self.state["roles"][role]["release"]["symbols"]):
            raise ValueError("Incomplete model iteration cannot authorize replacement")
        for key in ("started_at", "wake_at", "deadline"):
            if instant(checkpoint[key]) != instant(self.state[key]):
                raise ValueError("Model checkpoint changed original session authority: " + key)
        self.state["roles"][role].update(checkpoint_path=str(root / "checkpoint.json"),
                                         checkpoint_sha256=sha256(raw))
        return checkpoint

    def _finished(self, role, exit_code):
        worker, runtime = self.workers[role], self.runtime[role]
        # Root process exit alone is not a replacement boundary.
        empty_since = runtime.setdefault("root_exited", self.monotonic())
        if not worker.job_empty():
            if self.monotonic() - empty_since >= self.drain_timeout:
                raise RuntimeError("Scout role exited with owned descendants still running")
            return
        worker.close(timeout=self.drain_timeout)
        del self.workers[role]
        atomic_json(runtime["root"] / "exit.json", {"role": role, "exit_code": exit_code,
            "session_id": self.run_id, "generation": self.state["roles"][role]["generation"],
            "observed_at": instant(self.clock()).isoformat()})
        if self.stop_reason():
            self.state["roles"][role]["status"] = "STOPPED"
            self.save()
            return
        result = json.loads(read_bytes(runtime["root"] / "result.json"))
        self._validate_header(role, result)
        if role == "model" and exit_code == PLANNED_EXIT and result.get("status") == "PLANNED_RESTART":
            checkpoint = self._checkpoint(role, runtime["root"])
            self.state["roles"][role]["status"] = "BETWEEN_ITERATIONS"
            self.save()
            # Same original day/deadline; a stop in this gap wins startup.
            selected = self.select(self.selector)
            self._start_role("model", selected, checkpoint)
        else:
            self.state["roles"][role].update(status="FAILED", result=result, automatic_retry=False)
            self.save("DEGRADED")

    def _poll_role(self, role):
        worker, runtime = self.workers[role], self.runtime[role]
        code = worker.poll()
        if code is not None:
            self._finished(role, code)
            return
        phase_path = runtime["root"] / "phase.json"
        if phase_path.exists():
            phase = json.loads(read_bytes(phase_path))
            self._validate_header(role, phase)
            if phase["phase"] in {"ready", "iteration_execution", "draining", "checkpointed"}:
                runtime["ready"] = True
                self.state["roles"][role]["status"] = "RUNNING"
            if phase["phase"] in {"draining", "checkpointed"} and runtime["draining"] is None:
                runtime["draining"] = self.monotonic()
        if not runtime["ready"] and self.monotonic() - runtime["started"] >= self.startup_timeout:
            raise TimeoutError("Scout " + role + " startup timed out; no automatic respawn")
        if runtime["draining"] is not None and self.monotonic() - runtime["draining"] >= self.drain_timeout + 5:
            raise TimeoutError("Scout " + role + " drain timed out; pending identities retained")

    def run(self, *, lock_factory=application_locks):
        selected = self.select(self.selector)
        with lock_factory(selected, self.datastore):
            self._prior_run_exited()
            self.root.mkdir(parents=True, exist_ok=True)
            self.save("WAITING")
            try:
                while instant(self.clock()) < instant(self.state["wake_at"]) and not self.stop_reason():
                    self.sleep(min(1, (instant(self.state["wake_at"]) - instant(self.clock())).total_seconds()))
                if not self.stop_reason():
                    self.save("STARTING")
                    self._start_role("trading", selected)
                    self._start_role("model", selected)
                while not (reason := self.stop_reason()):
                    for role in tuple(self.workers):
                        try:
                            self._poll_role(role)
                        except Exception as exc:
                            # Revoke this role before bounded termination. Other
                            # role's valid work, especially order recovery, lives.
                            self.state["roles"][role].update(status="FAILED", automatic_retry=False,
                                error_type=type(exc).__name__, error=str(exc))
                            self.save("DEGRADED")
                            if role in self.workers:
                                self.workers[role].close(timeout=self.drain_timeout)
                                del self.workers[role]
                    if not self.workers:
                        self.save("FAILED", stop_reason="all_roles_failed", automatic_retry=False)
                        return 1
                    if self.state["status"] != "DEGRADED":
                        self.save("RUNNING" if all(r["ready"] for r in self.runtime.values()) else "STARTING")
                    self.sleep(0.1)
                self.save("STOPPING", stop_reason=reason)
            except KeyboardInterrupt:
                self.request_stop()
                reason = "manual_stop"
                self.save("STOPPING", stop_reason=reason)
            except BaseException as exc:
                self.save("FAILED", stop_reason="controller_failure", error_type=type(exc).__name__,
                          error=str(exc), automatic_retry=False)
                raise
            finally:
                # All fences are revoked before waiting for any busy model.
                until = self.monotonic() + self.drain_timeout
                while self.workers and self.monotonic() < until:
                    for role, worker in tuple(self.workers.items()):
                        if worker.poll() is not None and worker.job_empty():
                            worker.close(timeout=max(0, until - self.monotonic()))
                            del self.workers[role]
                    if self.workers:
                        self.sleep(0.1)
                for role, worker in tuple(self.workers.items()):
                    worker.close(timeout=self.drain_timeout)
                    del self.workers[role]
            self.save("STOPPED", stop_reason=reason)
            return 0


def worker_main(request_path):
    request = json.loads(read_bytes(request_path))
    lifecycle = ServiceLifecycle(request)
    result_path = Path(request["generation_root"]) / "result.json"
    try:
        if reason := lifecycle.stop_reason():
            atomic_json(result_path, {**lifecycle.header(), "status": "STOPPED", "stop_reason": reason,
                                     "authority_failure": lifecycle.stop_detail})
            return 1 if reason == "controller_failure" else 0
        selected = validate_release(request["release"]["release_root"], request["release"]["manifest_sha256"],
                                    protocol=PROTOCOL)
        if release_identity(selected) != request["release"]:
            raise ValueError("Independent role loaded release differs from pinned request")
        import_release(selected)
        from ml.intraday import scout_services
        if not Path(scout_services.__file__).resolve().is_relative_to(Path(selected["source_root"])):
            raise RuntimeError("Independent service imported outside the pinned release")
        if reason := lifecycle.stop_reason():
            raise InterruptedError("Scout authority revoked before service startup: " + reason)
        if lifecycle.role == "model" and os.name == "nt":
            # Scheduling priority affects resource fairness, never recipe fields.
            psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        entrypoint = scout_services.run_model_service if lifecycle.role == "model" else scout_services.run_trading_service
        result = entrypoint(repository=Path(request["repository"]), datastore=Path(request["datastore"]),
                            release=selected, lifecycle=lifecycle)
        atomic_json(result_path, {**result, **lifecycle.header(), "authority_failure": lifecycle.stop_detail})
        return PLANNED_EXIT if result["status"] == "PLANNED_RESTART" else 1 if result["status"] == "FAILED" else 0
    except BaseException as exc:
        atomic_json(result_path, {**lifecycle.header(), "status": "FAILED", "error_type": type(exc).__name__,
                                 "error": str(exc), "authority_failure": lifecycle.stop_detail})
        raise


def probe_release(selected, datastore, started_at):
    """Offline recipe/calendar validation. Never construct a broker or provider."""
    session, launcher, recipe = import_release(selected)
    saved = json.loads(read_bytes(selected["recipe_path"]))
    recipe.prepare_manual_models(session.ResearchConfig(), datastore=Path(datastore), pc="Scout",
        symbols=saved["symbols"], recipe_path=Path(selected["recipe_path"]))
    wake = launcher.selected_wake_at(instant(started_at))
    return {"wake_at": wake.isoformat(), "deadline": session.automatic_stop_deadline(wake).isoformat(),
            "release": release_identity(selected), "trader_started": False, "system_1a_started": False}


def probe(selected, datastore, output_root):
    """Keep application imports out of the long-lived controller parent."""
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    identity = uuid.uuid4().hex
    request_path, result_path = output_root / (identity + ".request.json"), output_root / (identity + ".result.json")
    atomic_json(request_path, {**release_identity(selected), "datastore": str(datastore),
                              "started_at": utcnow().isoformat(), "output": str(result_path)})
    worker = ManagedWorker([sys.executable, "-B", str(Path(__file__).resolve()), "--probe", str(request_path)],
        cwd=str(output_root), env=dict(os.environ, PYTHONPATH="", PYTHONDONTWRITEBYTECODE="1"),
        log_path=output_root / (identity + ".log"))
    try:
        if worker.wait(timeout=120) != 0:
            raise RuntimeError("Offline Scout recipe/calendar probe failed: " + str(request_path))
        worker.await_empty(timeout=10)
        return json.loads(read_bytes(result_path))
    finally:
        worker.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--stop", action="store_true")
    mode.add_argument("--worker", type=Path)
    mode.add_argument("--probe", type=Path)
    parser.add_argument("--selector", type=Path)
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--datastore", type=Path)
    parser.add_argument("--hold-console", action="store_true")
    args = parser.parse_args(argv)
    if args.worker:
        return worker_main(args.worker)
    if args.probe:
        request = json.loads(read_bytes(args.probe))
        selected = validate_release(request["release_root"], request["manifest_sha256"], protocol=PROTOCOL)
        atomic_json(request["output"], probe_release(selected, request["datastore"], request["started_at"]))
        return 0
    if args.datastore is None:
        parser.error("--datastore is required")
    if args.stop:
        state = json.loads(read_bytes(args.datastore / "intraday/scout/controller.json"))
        if state.get("schema") not in {STATE_SCHEMA, "scout-logical-run-v1"}:
            raise ValueError("No compatible Scout logical run to stop")
        atomic_json(state["stop_path"], {"session_id": state["session_id"], "reason": "manual_stop",
                                       "requested_at": utcnow().isoformat()})
        return 0
    if args.selector is None or args.repository is None:
        parser.error("--selector and --repository are required")
    selected = selected_release(args.selector)
    prepared = probe(selected, args.datastore, args.repository / "scratch/scout-independent-probes")
    if args.check:
        print(json.dumps(prepared, sort_keys=True))
        return 0
    owner = ServiceController(selector=args.selector, repository=args.repository, datastore=args.datastore,
        wake_at=prepared["wake_at"], deadline=prepared["deadline"])
    signal.signal(signal.SIGINT, lambda *_: owner.request_stop())
    signal.signal(signal.SIGTERM, lambda *_: owner.request_stop())
    try:
        return owner.run()
    finally:
        if args.hold_console and sys.stdin.isatty():
            input("Scout 15m run ended. Press Enter to close.")


if __name__ == "__main__":
    raise SystemExit(main())
