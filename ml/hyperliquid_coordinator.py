"""Manage configured Hyperliquid data workers with one shared implementation.

Run ``python -m ml.hyperliquid_coordinator``. Edit the JSON symbols list to add
or remove markets while running. ``--status`` and ``--stop`` remain available
even while that configuration file is being edited.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field, fields
import json
import math
import os
from pathlib import Path
import signal
import sys
import threading
import time
from uuid import uuid4

from filelock import FileLock, Timeout

from datafetching.hyperliquid_candles import DEFAULT_INFO_URL
from ml.hyperliquid_coordinator_config import DEFAULT_CONFIG_PATH, load_config
from ml.hyperliquid_data_loop import LoopConfig, StopRequested, _atomic_json, _utc, run_loop
from ml.hyperliquid_data_pipeline import DEFAULT_OUTPUT_ROOT, run_pipeline


def _control_dir(output_root):
    return Path(output_root).resolve() / "_coordinator"


def read_status(output_root=DEFAULT_OUTPUT_ROOT) -> dict:
    directory = _control_dir(output_root)
    path = directory / "coordinator_status.json"
    if not path.exists():
        return {"status": "not_started", "coordinator_running": False, "status_file": str(path)}
    for attempt in range(3):
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            break
        except PermissionError:
            # Windows may briefly deny opening the file while the writer
            # atomically replaces it. Only retry this transient access failure.
            if attempt == 2:
                raise
            time.sleep(0.01 * 2 ** attempt)
    try:
        with FileLock(str(directory / ".coordinator.lock"), timeout=0):
            result["coordinator_running"] = False
    except Timeout:
        result["coordinator_running"] = True
    result["stop_requested"] = (directory / "stop.request").exists()
    if not result["coordinator_running"] and result["status"] not in {"stopped", "failed"}:
        result["status"] = "not_running"
    return result


def request_stop(output_root=DEFAULT_OUTPUT_ROOT) -> dict:
    directory = _control_dir(output_root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "stop.request"
    lock = FileLock(str(directory / ".coordinator.lock"), timeout=0)
    try:
        with lock:
            path.unlink(missing_ok=True)
            return {"status": "already_stopped", "stop_file": str(path)}
    except Timeout:
        pass
    _atomic_json(path, {"requested_at_utc": _utc(time.time())})
    try:
        with lock:
            path.unlink(missing_ok=True)
            return {"status": "already_stopped", "stop_file": str(path)}
    except Timeout:
        return {"status": "stop_requested", "stop_file": str(path)}


@dataclass
class MarketWorker:
    config: LoopConfig
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    status: str = "starting"
    attempts: int = 0
    retiring: bool = False
    last_error: dict | None = None
    result: dict | None = None
    restart_at: float = 0.0


class Coordinator:
    """Own worker lifetimes; formula code, data and retry schedules stay separate."""

    def __init__(self, config_path=DEFAULT_CONFIG_PATH, *, default_info_url=DEFAULT_INFO_URL,
                 pipeline_runner=run_pipeline, loop_runner=run_loop, clock=time.time,
                 monotonic=time.perf_counter):
        self.config_path = Path(config_path).resolve()
        self.default_info_url = default_info_url
        self.config = load_config(self.config_path, default_info_url=default_info_url)
        self.pipeline_runner = pipeline_runner
        self.loop_runner = loop_runner
        self.clock = clock
        self.monotonic = monotonic
        self._gate = threading.BoundedSemaphore(self.config.max_parallel_updates)
        self._workers: dict[str, MarketWorker] = {}
        self._state_lock = threading.RLock()
        self._max_cycles = None
        self._status = "starting"
        self._reason = None
        self._config_error = None
        self._last_config_bytes = None
        self._last_event_signature = None
        self._instance_id = uuid4().hex
        self._started_at = _utc(self.clock())

    def reload(self) -> bool:
        """Apply an entire valid symbols-list edit, retaining state on bad edits."""
        try:
            content = self.config_path.read_bytes()
            if content == self._last_config_bytes:
                self._config_error = None
                return False
            candidate = load_config(self.config_path, default_info_url=self.default_info_url)
            # An editor may replace the file between the reads. Retry next tick.
            if content != self.config_path.read_bytes():
                return False
            changed = [f.name for f in fields(candidate)
                       if f.name != "symbols" and getattr(candidate, f.name) != getattr(self.config, f.name)]
            if changed:
                raise ValueError("Restart the coordinator to change: " + ", ".join(changed)
                                 + ". Only symbols may change while running.")
            applied = candidate != self.config
            self.config = candidate
            self._last_config_bytes = content
            self._config_error = None
            return applied
        except (OSError, ValueError, TypeError) as exc:
            self._config_error = {"type": type(exc).__name__, "message": str(exc)}
            return False

    def _limited_pipeline(self, worker: MarketWorker, **kwargs):
        stop_file = worker.config.dataset_dir / "stop.request"
        queued_counter = self.monotonic()

        def stopping():
            return worker.stop_event.is_set() or stop_file.exists()

        with self._state_lock:
            worker.status = "queued"
        while True:
            if stopping():
                raise StopRequested()
            if self._gate.acquire(timeout=0.1):
                break
        queue_wait_seconds = self.monotonic() - queued_counter
        try:
            if stopping():
                raise StopRequested()
            with self._state_lock:
                worker.status = "updating"
            # A queued job can cross a candle boundary. Capture the cutoff when
            # it actually gets its slot, not when its worker joined the queue.
            kwargs["as_of_ms"] = int(self.clock() * 1000)
            result = self.pipeline_runner(**kwargs)
            return {**result, "queue_wait_seconds": queue_wait_seconds}
        finally:
            self._gate.release()
            with self._state_lock:
                worker.status = "running"

    def _worker_main(self, worker: MarketWorker):
        try:
            result = self.loop_runner(
                worker.config, max_cycles=self._max_cycles, stop_event=worker.stop_event,
                pipeline_runner=lambda **kwargs: self._limited_pipeline(worker, **kwargs),
            )
            with self._state_lock:
                worker.result = result or {}
                worker.status = "completed" if worker.result.get("reason") == "max_cycles" else "stopped"
        except StopRequested:
            with self._state_lock:
                worker.result = {"status": "stopped", "reason": "stop_requested", "failures": 0}
                worker.status = "stopped"
        except Timeout as exc:
            # Another process owns the market. Never stop it or overwrite its
            # status to claim ownership. Other configured markets keep running.
            with self._state_lock:
                worker.status = "external_worker"
                worker.last_error = {"type": type(exc).__name__, "message": "Another worker owns this market's loop lock."}
                worker.restart_at = self.clock() + self.config.retry_seconds
        except Exception as exc:
            with self._state_lock:
                worker.status = "worker_error"
                worker.last_error = {"type": type(exc).__name__, "message": str(exc)}
                delay = min(self.config.max_retry_seconds,
                            self.config.retry_seconds * 2 ** min(worker.attempts - 1, 30))
                worker.restart_at = self.clock() + delay

    def _launch(self, key: str, worker: MarketWorker):
        worker.stop_event = threading.Event()
        worker.status = "starting"
        worker.last_error = None
        worker.attempts += 1
        worker.thread = threading.Thread(target=self._worker_main, args=(worker,),
                                         name=f"hyperliquid-{key}", daemon=False)
        worker.thread.start()

    def _snapshot(self) -> dict:
        markets = {}
        with self._state_lock:
            for key, worker in self._workers.items():
                alive = bool(worker.thread and worker.thread.is_alive())
                entry = {
                    "coin": worker.config.coin, "interval": worker.config.interval,
                    "status": "removing" if worker.retiring and alive else worker.status,
                    "thread_alive": alive, "attempts": worker.attempts,
                    "last_error": worker.last_error,
                    "last_result": (worker.result or {}).get("last_result"),
                    "last_cycle_timing": (worker.result or {}).get("last_cycle_timing"),
                    "dataset_dir": str(worker.config.dataset_dir),
                    "latest_pointer": str(worker.config.dataset_dir / "latest.json"),
                }
                # Only report the per-loop file as ours when an owned thread is
                # active and the recorded PID belongs to this coordinator.
                path = worker.config.dataset_dir / "loop_status.json"
                if alive and worker.status not in {"external_worker", "starting"}:
                    try:
                        details = json.loads(path.read_text(encoding="utf-8"))
                        if details.get("pid") == os.getpid():
                            entry.update({k: details.get(k) for k in (
                                "cycle_count", "successful_cycles", "failures", "next_wake_utc",
                                "last_result", "last_error", "last_finished_at_utc",
                                "last_cycle_timing",
                            )})
                            if worker.status == "running" and not worker.retiring:
                                entry["status"] = details["status"]
                    except (OSError, ValueError):
                        pass
                if worker.result:
                    entry.update({k: worker.result.get(k) for k in (
                        "cycle_count", "successful_cycles", "failures", "last_error",
                    )})
                markets[key] = entry
        return {
            "version": 1, "pid": os.getpid(), "instance_id": self._instance_id,
            "status": self._status, "reason": self._reason,
            "started_at_utc": self._started_at, "updated_at_utc": _utc(self.clock()),
            "config_path": str(self.config_path), "config_error": self._config_error,
            "symbols": list(self.config.symbols), "interval": self.config.interval,
            "max_parallel_updates": self.config.max_parallel_updates,
            "output_root": str(self.config.output_root),
            "status_file": str(self.config.control_dir / "coordinator_status.json"),
            "markets": markets,
        }

    def _persist(self, state: dict):
        directory = self.config.control_dir
        try:
            directory.mkdir(parents=True, exist_ok=True)
            _atomic_json(directory / "coordinator_status.json", state)
        except OSError as exc:
            print(f"Coordinator status write failed: {type(exc).__name__}", file=sys.stderr, flush=True)
        # Per-market loops record every data cycle; coordinator events record
        # lifecycle/config changes without appending the same state every second.
        signature = json.dumps({"status": state["status"], "config_error": state["config_error"],
                                "markets": {k: (v["status"], v["attempts"]) for k, v in state["markets"].items()}}, sort_keys=True)
        if signature != self._last_event_signature:
            try:
                with (directory / "coordinator_events.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(state, allow_nan=False) + "\n")
            except OSError as exc:
                print(f"Coordinator event write failed: {type(exc).__name__}", file=sys.stderr, flush=True)
            self._last_event_signature = signature

    def tick(self) -> dict:
        self.reload()
        desired = self.config.loop_configs()
        for key, worker in list(self._workers.items()):
            alive = bool(worker.thread and worker.thread.is_alive())
            if key not in desired:
                worker.retiring = True
                worker.stop_event.set()
                if not alive:
                    del self._workers[key]
            elif worker.retiring and not alive:
                # A quick remove/re-add waits for the previous build to finish.
                del self._workers[key]
        for key, config in desired.items():
            if key not in self._workers:
                worker = MarketWorker(config=config)
                self._workers[key] = worker
                self._launch(key, worker)
            else:
                worker = self._workers[key]
                if (not worker.retiring and not worker.thread.is_alive()
                        and worker.status in {"external_worker", "worker_error"}
                        and self._max_cycles is None and self.clock() >= worker.restart_at):
                    self._launch(key, worker)
        self._status = "running"
        state = self._snapshot()
        self._persist(state)
        return state

    def shutdown(self) -> dict:
        self._status = "stopping"
        for worker in self._workers.values():
            worker.stop_event.set()
        self._persist(self._snapshot())
        for worker in self._workers.values():
            if worker.thread:
                worker.thread.join()
        self._status = "failed" if self._reason == "worker_failure" else "stopped"
        state = self._snapshot()
        self._persist(state)
        return state

    def run(self, *, max_cycles=None, stop_event=None, poll_seconds=1.0, max_polls=None) -> dict:
        for name, value in (("max_cycles", max_cycles), ("max_polls", max_polls)):
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(f"{name} must be a positive integer or None.")
        if not math.isfinite(poll_seconds) or poll_seconds <= 0:
            raise ValueError("poll_seconds must be finite and positive.")
        self._max_cycles = max_cycles
        stopped = threading.Event() if stop_event is None else stop_event
        directory = self.config.control_dir
        directory.mkdir(parents=True, exist_ok=True)
        stop_path = directory / "stop.request"
        polls = 0
        with FileLock(str(directory / ".coordinator.lock"), timeout=0):
            try:
                while not stopped.is_set() and not stop_path.exists():
                    self.tick()
                    polls += 1
                    if stopped.is_set() or stop_path.exists():
                        self._reason = "stop_requested"
                        break
                    if max_cycles is not None and self._workers and all(
                        not worker.thread.is_alive() for worker in self._workers.values()
                    ):
                        failed = any(worker.status != "completed"
                                     or not (worker.result or {}).get("last_result")
                                     or (worker.result or {}).get("last_error")
                                     or ((worker.result or {}).get("last_result") or {}).get("lag_intervals_after_delay", 0)
                                     for worker in self._workers.values())
                        self._reason = "worker_failure" if failed else "max_cycles"
                        break
                    if max_polls is not None and polls >= max_polls:
                        self._reason = "max_polls"
                        break
                    stopped.wait(poll_seconds)
            except KeyboardInterrupt:
                self._reason = "keyboard_interrupt"
            finally:
                self._reason = self._reason or "stop_requested"
                state = self.shutdown()
                stop_path.unlink(missing_ok=True)
        return state


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--max-cycles", type=int, default=None, help="Bound the number of update attempts per symbol.")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT,
                        help="Datastore root for --status/--stop only; startup uses the JSON output_root.")
    control = parser.add_mutually_exclusive_group()
    control.add_argument("--status", action="store_true")
    control.add_argument("--stop", action="store_true")
    args = parser.parse_args(argv)
    if args.status or args.stop:
        result = read_status(args.output_root) if args.status else request_stop(args.output_root)
        print(json.dumps(result, indent=2))
        return 0
    from app.config import hyperliquid_info_url
    stopped = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    try:
        coordinator = Coordinator(args.config, default_info_url=hyperliquid_info_url())
        result = coordinator.run(max_cycles=args.max_cycles, stop_event=stopped)
    except Timeout:
        print("Another coordinator already owns this datastore's coordinator lock.", file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError) as exc:
        print(f"Coordinator failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 1 if result["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
