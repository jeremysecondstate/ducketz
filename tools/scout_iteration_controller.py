"""Scout's authorized run controller; no trading imports in the parent process.

The installed bootstrap is pinned for a logical run. Each child validates and
imports one freshly selected immutable application release. No crash respawner.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
import uuid

import psutil

if __package__:
    from .scout_console import ConsoleStatus
    from .scout_release import PROTOCOL, atomic_json, resolve_selection, validate_release, sha256, read_bytes
    from .scout_process import ManagedWorker, inspect_job, process_in_job
else:
    from scout_console import ConsoleStatus
    from scout_release import PROTOCOL, atomic_json, resolve_selection, validate_release, sha256, read_bytes
    from scout_process import ManagedWorker, inspect_job, process_in_job

STATE_SCHEMA = "scout-logical-run-v1"
PLANNED_EXIT = 75


def instant(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Lifecycle timestamps require a timezone")
    return result.astimezone(timezone.utc)


def utcnow():
    return datetime.now(timezone.utc)


def alive(pid, birth):
    try:
        return psutil.Process(pid).create_time() == birth
    except psutil.NoSuchProcess:
        return False


def release_identity(selected):
    return {key: selected[key] for key in ("release_root", "manifest_sha256", "source_root",
            "recipe_path", "recipe_file_sha256", "symbols", "lifecycle_protocol")}


def application_locks(selected, datastore):
    # Load only the reviewed lock helper. Do not import trading modules here.
    spec = importlib.util.spec_from_file_location("scout_bootstrap_runtime_lock",
        Path(selected["source_root"]) / "datafetching/runtime_lock.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stack = ExitStack()
    try:
        for name in ("intraday-manual-launcher", "intraday-manual-session"):
            stack.enter_context(module.exclusive_runtime_lock(
                Path(datastore) / "locks" / (name + ".lock"), process_name=name))
        return stack
    except BaseException:
        stack.close()
        raise


class Controller:
    def __init__(self, *, selector, repository, datastore, wake_at, deadline,
                 clock=utcnow, sleep=time.sleep, worker_factory=ManagedWorker,
                 select=resolve_selection, startup_timeout=300, drain_timeout=120,
                 backoff_seconds=0.25, console=None):
        self.selector, self.repository, self.datastore = Path(selector), Path(repository), Path(datastore)
        self.clock, self.sleep, self.worker_factory, self.select = clock, sleep, worker_factory, select
        self.startup_timeout, self.drain_timeout = startup_timeout, drain_timeout
        self.backoff_seconds = backoff_seconds
        self.console = console
        self.run_id = "manual-scout-15m:" + uuid.uuid4().hex
        self.root = self.datastore / "intraday/scout/runs" / self.run_id.replace(":", "-")
        self.active_path = self.datastore / "intraday/scout/controller.json"
        self.stop_path = self.root / "manual.stop"
        self.worker = None
        self.state = {"schema": STATE_SCHEMA, "protocol": PROTOCOL, "session_id": self.run_id,
                      "started_at": instant(clock()).isoformat(), "wake_at": instant(wake_at).isoformat(),
                      "deadline": instant(deadline).isoformat(), "controller_pid": os.getpid(),
                      "controller_create_time": psutil.Process().create_time(), "generation": 0,
                      "token": uuid.uuid4().hex, "status": "WAITING", "stop_path": str(self.stop_path),
                      "automatic_daily_restart": False}

    def save(self, status, **extra):
        self.state.update(status=status, updated_at=instant(self.clock()).isoformat(), **extra)
        atomic_json(self.root / "run.json", self.state)
        atomic_json(self.active_path, self.state)
        self.report()

    def report(self, phase=None):
        if self.console is not None:
            try:
                self.console.update(self.state, self.root, phase=phase)
            except Exception:
                # Observability is never execution or stop authority.
                pass

    def stop_reason(self):
        if self.stop_path.exists():
            return "manual_stop"
        if instant(self.clock()) >= instant(self.state["deadline"]):
            return "session_end"
        return None

    def request_stop(self):
        # Never remove or clear this latch, including after a failed generation.
        atomic_json(self.stop_path, {"session_id": self.run_id, "reason": "manual_stop",
                                   "requested_at": instant(self.clock()).isoformat()})

    def _prior_run_exited(self):
        if not self.active_path.exists():
            return
        previous = json.loads(read_bytes(self.active_path))
        if previous.get("schema") != STATE_SCHEMA:
            raise ValueError("Unknown prior Scout controller state; preserve for review")
        if alive(previous["controller_pid"], previous["controller_create_time"]):
            raise RuntimeError("Another Scout controller still owns the run")
        child = previous.get("worker")
        if child and (alive(child["pid"], child["create_time"]) or inspect_job(child["job_name"])):
            raise RuntimeError("Prior Scout worker tree has not exited; no overlapping launch")

    def run(self, *, lock_factory=application_locks):
        selected = self.select(self.selector)
        with lock_factory(selected, self.datastore):
            self._prior_run_exited()
            # A stop arriving before run() may already have created its latch.
            self.root.mkdir(parents=True, exist_ok=True)
            self.save("WAITING")
            checkpoint = None
            try:
                while instant(self.clock()) < instant(self.state["wake_at"]):
                    self.report()
                    if reason := self.stop_reason():
                        self.save("STOPPED", stop_reason=reason)
                        return 0
                    self.sleep(min(1, (instant(self.state["wake_at"]) - instant(self.clock())).total_seconds()))
                while not (reason := self.stop_reason()):
                    # Reselect even when the last generation loaded the same release.
                    selected = self.select(self.selector)
                    if reason := self.stop_reason():
                        break
                    self.state["generation"] += 1
                    generation = self.state["generation"]
                    self.state["token"] = uuid.uuid4().hex
                    generation_root = self.root / str(generation)
                    generation_root.mkdir()
                    request = {**self.state, "active_path": str(self.active_path),
                               "repository": str(self.repository), "datastore": str(self.datastore),
                               "release": release_identity(selected), "previous_checkpoint": checkpoint,
                               "generation_root": str(generation_root), "drain_timeout_seconds": self.drain_timeout}
                    request_path = generation_root / "request.json"
                    atomic_json(request_path, request)
                    self.save("STARTING", release=request["release"], worker=None)
                    if reason := self.stop_reason():
                        break
                    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH="")
                    self.worker = self.worker_factory([sys.executable, "-B", str(Path(__file__).resolve()),
                        "--worker", str(request_path)], cwd=str(generation_root), env=environment,
                        log_path=generation_root / "worker.log", start_suspended=True)
                    self.save("STARTING", worker={"pid": self.worker.pid,
                        "create_time": self.worker.create_time, "job_name": self.worker.job_name})
                    if reason := self.stop_reason():
                        self.worker.terminate(timeout=self.drain_timeout)
                        break
                    self.worker.resume()
                    start = time.monotonic()
                    stopping_at = None
                    drain_at = None
                    ready = False
                    while self.worker.poll() is None:
                        if reason := self.stop_reason():
                            if stopping_at is None:
                                self.save("STOPPING", stop_reason=reason)
                                stopping_at = time.monotonic()
                        phase_path = generation_root / "phase.json"
                        if phase_path.exists():
                            phase = json.loads(read_bytes(phase_path))
                            self._validate_header(phase)
                            if phase["phase"] in {"ready", "iteration_execution", "draining", "checkpointed"}:
                                if not ready and stopping_at is None:
                                    self.save("RUNNING")
                                ready = True
                            if phase["phase"] in {"draining", "checkpointed"} and drain_at is None:
                                drain_at = time.monotonic()
                            self.report(phase["phase"])
                        else:
                            self.report()
                        elapsed = time.monotonic() - start
                        if not ready and elapsed >= self.startup_timeout:
                            raise TimeoutError("Scout worker startup timed out; generation not replaced")
                        for since in (stopping_at, drain_at):
                            if since is not None and time.monotonic() - since >= self.drain_timeout + 5:
                                raise TimeoutError("Scout worker drain timed out; pending work retained")
                        self.sleep(0.1)
                    exit_code = self.worker.poll()
                    atomic_json(generation_root / "exit.json", {
                        "session_id": self.run_id, "generation": generation,
                        "exit_code": exit_code, "observed_at": instant(self.clock()).isoformat()})
                    exited_at = time.monotonic()
                    while not self.worker.job_empty():
                        self.report("awaiting_worker_tree_exit")
                        if time.monotonic() - exited_at >= self.drain_timeout:
                            raise RuntimeError("Scout worker exited with owned child workers still running")
                        self.sleep(0.05)
                    self.worker.close(timeout=self.drain_timeout)
                    self.worker = None
                    if reason := self.stop_reason():
                        break
                    result = json.loads(read_bytes(generation_root / "result.json"))
                    self._validate_header(result)
                    if exit_code != PLANNED_EXIT or result.get("status") != "PLANNED_RESTART":
                        if (exit_code == 0 and result.get("status") == "STOPPED"
                                and result.get("stop_reason") in {"manual_stop", "session_end", "automatic_17_00_pacific"}):
                            self.save("STOPPED", stop_reason=result.get("stop_reason", "worker_stop"))
                            return 0
                        raise RuntimeError("Scout worker did not complete a planned boundary: " + json.dumps(result))
                    checkpoint_raw = read_bytes(generation_root / "checkpoint.json")
                    checkpoint = json.loads(checkpoint_raw)
                    self._validate_header(checkpoint)
                    if (checkpoint.get("schema") != "scout-iteration-checkpoint-v1"
                            or checkpoint.get("owned_workers_drained") is not True
                            or checkpoint.get("boundary_kind") != "full_symbol_attempt_with_durable_deferrals"
                            or not checkpoint.get("cycles") or not checkpoint.get("boundary_cycle_id")):
                        raise ValueError("Unsupported Scout checkpoint; no replacement or fallback")
                    self.save("BETWEEN_ITERATIONS", checkpoint_path=str(generation_root / "checkpoint.json"),
                              checkpoint_sha256=sha256(checkpoint_raw))
                    # A stop during the gap/backoff always wins the next launch.
                    self.sleep(self.backoff_seconds)
                self.save("STOPPED", stop_reason=reason)
                return 0
            except KeyboardInterrupt:
                self.request_stop()
                self.save("STOPPING", stop_reason="manual_stop")
                if self.worker is not None:
                    self.worker.terminate(timeout=self.drain_timeout)
                self.save("STOPPED", stop_reason="manual_stop")
                return 0
            except BaseException as exc:
                # Revoke generation authority before bounded termination.
                self.save("FAILED", stop_reason="unexpected_failure", error_type=type(exc).__name__,
                          error=str(exc), automatic_retry=False)
                raise
            finally:
                if self.worker is not None:
                    self.worker.close(timeout=self.drain_timeout)
                    self.worker = None

    def _validate_header(self, value):
        for key in ("session_id", "generation", "token"):
            if value.get(key) != self.state[key]:
                raise ValueError("Stale Scout generation receipt: " + key)
        if value.get("release") != self.state["release"]:
            raise ValueError("Scout receipt loaded release differs")


class WorkerLifecycle:
    def __init__(self, request, *, clock=utcnow):
        self.request, self.clock = request, clock
        self.session_id, self.generation = request["session_id"], request["generation"]
        self.started_at, self.wake_at, self.deadline = (request[key] for key in ("started_at", "wake_at", "deadline"))
        self.release = request["release"]
        self.previous_checkpoint = request["previous_checkpoint"]
        self.drain_timeout_seconds = request["drain_timeout_seconds"]
        self.root = Path(request["generation_root"])
        self._write_lock = threading.Lock()
        self._authority_lock = threading.Lock()
        self._latched_reason = None
        self.stop_detail = None

    def _revoke(self, reason, **details):
        with self._authority_lock:
            if self._latched_reason is None:
                self.stop_detail = {"reason": reason, **details}
                self._latched_reason = reason
            return self._latched_reason

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
            # A manual stop or deadline reached during bounded filesystem IO
            # wins before effects, even if the state read eventually succeeds.
            if Path(request["stop_path"]).exists():
                return self._revoke("manual_stop", stage="after_authority_read")
            if instant(self.clock()) >= instant(self.deadline):
                return self._revoke("session_end", stage="after_authority_read")
            for key in ("session_id", "generation", "token", "controller_pid", "controller_create_time"):
                if state.get(key) != request[key]:
                    return self._revoke("controller_failure", stage="generation_fence", field=key)
            if state.get("release") != request["release"]:
                return self._revoke("controller_failure", stage="release_fence")
            identity = state.get("worker") or {}
            # Windows virtualenv launchers can introduce an extra process. The
            # actual Python worker must belong to this generation's exact job.
            if (not alive(identity["pid"], identity["create_time"])
                    or not process_in_job(os.getpid(), identity["job_name"])
                    or state.get("status") not in {"STARTING", "RUNNING"}):
                reason = state.get("stop_reason")
                return self._revoke(reason if reason in {"manual_stop", "session_end"} else "controller_failure",
                                    stage="worker_identity_or_controller_status", controller_status=state.get("status"))
        except (OSError, ValueError, KeyError, psutil.Error) as exc:
            # Preserve the exact cause. A real revoked generation cannot regain
            # authority merely because the next read happens to look healthy.
            if Path(request["stop_path"]).exists():
                return self._revoke("manual_stop", stage="failed_authority_read")
            if instant(self.clock()) >= instant(self.deadline):
                return self._revoke("session_end", stage="failed_authority_read")
            return self._revoke("controller_failure", stage="authority_read", error_type=type(exc).__name__,
                                error=str(exc), winerror=getattr(exc, "winerror", None))
        return self._latched_reason

    def header(self):
        return {**{key: self.request[key] for key in ("session_id", "generation", "token", "release")},
                "worker_pid": os.getpid(), "worker_create_time": psutil.Process().create_time()}

    def phase(self, name, **details):
        with self._write_lock:
            atomic_json(self.root / "phase.json", {**details, **self.header(), "phase": name,
                        "recorded_at": instant(self.clock()).isoformat()})

    def ready(self):
        if reason := self.stop_reason():
            raise InterruptedError("Scout generation has no effect authority: " + reason)
        self.phase("ready")

    def checkpoint(self, payload):
        if reason := self.stop_reason():
            raise InterruptedError("Scout boundary interrupted: " + reason)
        atomic_json(self.root / "checkpoint.json", {**payload, **self.header()})
        self.phase("checkpointed")


def import_release(selected):
    source = Path(selected["source_root"])
    # A fresh child never inherits the checkout's import path or current dir.
    sys.dont_write_bytecode = True
    os.environ["PYTHONPATH"] = str(source)
    sys.path.insert(0, str(source))
    os.chdir(source)
    from app.services import schwab_token_store
    repository = Path(selected["manifest"]["original_repository"])
    schwab_token_store.PROJECT_ROOT = repository
    schwab_token_store.DEFAULT_TOKEN_CACHE_PATH = repository / "data/schwab_tokens.json"
    schwab_token_store.TOKEN_CACHE_PATH = schwab_token_store.DEFAULT_TOKEN_CACHE_PATH
    from ml.intraday import live_session, manual_launcher, live_model_recipe
    for module in (live_session, manual_launcher, live_model_recipe):
        if not Path(module.__file__).resolve().is_relative_to(source):
            raise RuntimeError("Scout module imported outside the pinned release")
    return live_session, manual_launcher, live_model_recipe


def worker_main(request_path):
    request = json.loads(Path(request_path).read_bytes())
    lifecycle = WorkerLifecycle(request)
    result_path = Path(request["generation_root"]) / "result.json"
    try:
        if reason := lifecycle.stop_reason():
            failed = reason == "controller_failure"
            atomic_json(result_path, {**lifecycle.header(), "status": "FAILED" if failed else "STOPPED",
                                     "stop_reason": reason, "authority_failure": lifecycle.stop_detail})
            return 1 if failed else 0
        selected = validate_release(request["release"]["release_root"], request["release"]["manifest_sha256"])
        if release_identity(selected) != request["release"]:
            raise ValueError("Scout worker release identity differs from controller selection")
        session, _, _ = import_release(selected)
        if reason := lifecycle.stop_reason():
            raise InterruptedError("Scout authority revoked before startup: " + reason)
        result = session.run_manual(repository=Path(request["repository"]), datastore=Path(request["datastore"]),
            profile=Path(request["repository"]) / "scratch/cross-pc/local-profile.json",
            watchlist=Path(request["repository"]) / "datafetching/watchlist.local.txt",
            config=session.ResearchConfig(publication_offset_seconds=30, symbol_workers=1, use_supervised_models=True),
            scout_admission="broker-local", model_recipe=Path(selected["recipe_path"]), lifecycle=lifecycle)
        atomic_json(result_path, {**result, **lifecycle.header(), "authority_failure": lifecycle.stop_detail})
        return PLANNED_EXIT if result["status"] == "PLANNED_RESTART" else 1 if result["status"] == "FAILED" else 0
    except BaseException as exc:
        atomic_json(result_path, {**lifecycle.header(), "status": "FAILED", "error_type": type(exc).__name__,
                                 "error": str(exc), "authority_failure": lifecycle.stop_detail})
        raise


def offline_probe(request_path):
    request = json.loads(Path(request_path).read_bytes())
    selected = validate_release(request["release_root"], request["manifest_sha256"])
    session, launcher, recipe = import_release(selected)
    recipe_value = json.loads(Path(selected["recipe_path"]).read_bytes())
    recipe.prepare_manual_models(session.ResearchConfig(), datastore=Path(request["datastore"]),
        pc="Scout", symbols=recipe_value["symbols"], recipe_path=Path(selected["recipe_path"]))
    wake = launcher.selected_wake_at(instant(request["started_at"]))
    deadline = session.automatic_stop_deadline(wake)
    atomic_json(request["output"], {"wake_at": wake.isoformat(), "deadline": deadline.isoformat(),
                                    "release": release_identity(selected), "trader_started": False})
    return 0


def probe(selected, datastore, output_root):
    output_root.mkdir(parents=True, exist_ok=True)
    unique = uuid.uuid4().hex
    path = output_root / (unique + ".request.json")
    output = output_root / (unique + ".result.json")
    atomic_json(path, {**release_identity(selected), "datastore": str(datastore),
                      "started_at": utcnow().isoformat(), "output": str(output)})
    worker = ManagedWorker([sys.executable, "-B", str(Path(__file__).resolve()), "--probe", str(path)],
        cwd=str(output_root), env=dict(os.environ, PYTHONPATH="", PYTHONDONTWRITEBYTECODE="1"),
        log_path=output_root / (unique + ".log"))
    try:
        if worker.wait(timeout=120) != 0:
            raise RuntimeError("Offline Scout recipe/calendar validation failed: " + str(path))
        worker.await_empty(timeout=10)
        return json.loads(output.read_bytes())
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
    parser.add_argument("--hold-console", action="store_true",
                        help="Keep an interactive manual console open after cleanup; never wait with redirected IO")
    args = parser.parse_args(argv)
    if args.worker:
        return worker_main(args.worker)
    if args.probe:
        return offline_probe(args.probe)
    if args.datastore is None:
        parser.error("--datastore is required")
    if args.stop:
        state = json.loads((args.datastore / "intraday/scout/controller.json").read_bytes())
        if state.get("schema") != STATE_SCHEMA:
            raise ValueError("No compatible Scout controller to stop")
        atomic_json(state["stop_path"], {"session_id": state["session_id"], "reason": "manual_stop",
                                      "requested_at": utcnow().isoformat()})
        return 0
    if args.selector is None or args.repository is None:
        parser.error("--selector and --repository are required")
    console = ConsoleStatus() if args.live else None
    controller = None
    try:
        if console:
            console.write("STARTING | Validating the selected release and saved recipe/calendar (up to 2 minutes).")
            console.write("Press Ctrl+C to stop. Heartbeats show controller activity, not completed trades.")
        selected = resolve_selection(args.selector)
        prepared = probe(selected, args.datastore, args.repository / "scratch/scout-iteration-probes")
        if args.check:
            print(json.dumps(prepared, sort_keys=True))
            return 0
        controller = Controller(selector=args.selector, repository=args.repository, datastore=args.datastore,
                                wake_at=prepared["wake_at"], deadline=prepared["deadline"], console=console)
        console.write("Session deadline: " + console.local_time(controller.state["deadline"]))
        console.write("Run records: " + str(controller.root))
        def signal_stop(*_):
            # Keep console writes outside the signal handler (TextIO is not reentrant).
            # The loop observes this durable latch and reports the stopping state.
            controller.request_stop()
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            for sig in previous:
                signal.signal(sig, signal_stop)
            result = controller.run()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        # run() has completed its finally block and released worker/lock ownership.
        console.write("STOPPED | " + str(controller.state.get("stop_reason", "run ended"))
                      + " | Owned worker cleanup complete. No automatic restart.")
        return result
    except KeyboardInterrupt:
        if console:
            console.write("INTERRUPTED | Ctrl+C received. No replacement will be started.")
        return 130
    except Exception as exc:
        if console is None:
            raise
        console.write(f"FAILED | {type(exc).__name__}: {exc}")
        if controller is not None:
            console.write("Inspect run records and the worker log: " + str(controller.root))
        console.write("No automatic retry. Cleanup errors, if any, are shown above.")
        return 1
    finally:
        if console and args.hold_console:
            console.hold()


if __name__ == "__main__":
    raise SystemExit(main())
