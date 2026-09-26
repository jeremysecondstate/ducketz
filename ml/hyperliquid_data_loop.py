"""Continuous, closed-candle Hyperliquid dataset updates.

Run ``python -m ml.hyperliquid_data_loop``. Use ``--status`` to inspect the
worker and ``--stop`` for a graceful stop, including a hidden background worker.
The worker uses only the existing public data pipeline; no trading is involved.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import threading
import time
from uuid import uuid4

from filelock import FileLock, Timeout
import pandas as pd

from datafetching.hyperliquid_candles import DEFAULT_INFO_URL, INTERVAL_MS
from ml.hyperliquid_data_pipeline import DEFAULT_OUTPUT_ROOT, run_pipeline


class StopRequested(Exception):
    """A coordinator cancelled an update before it started using the API."""


@dataclass(frozen=True)
class LoopConfig:
    coin: str = "BTC"
    interval: str = "15m"
    output_root: Path = DEFAULT_OUTPUT_ROOT
    info_url: str = DEFAULT_INFO_URL
    close_delay_seconds: float = 5.0
    retry_seconds: float = 15.0
    max_retry_seconds: float = 120.0
    repair_every_cycles: int = 96

    def __post_init__(self):
        coin = self.coin.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{1,20}", coin):
            raise ValueError("Use a simple perpetual coin symbol, such as BTC.")
        if self.interval not in INTERVAL_MS:
            raise ValueError("Unsupported fixed candle interval.")
        for value in (self.close_delay_seconds, self.retry_seconds, self.max_retry_seconds):
            if not math.isfinite(value):
                raise ValueError("Loop timing parameters must be finite.")
        if not 0 <= self.close_delay_seconds < INTERVAL_MS[self.interval] / 1000:
            raise ValueError("Close delay must be nonnegative and less than one candle interval.")
        if self.retry_seconds <= 0 or self.max_retry_seconds < self.retry_seconds:
            raise ValueError("Retry seconds must be positive and maximum retry must be at least the base retry.")
        if not isinstance(self.repair_every_cycles, int) or self.repair_every_cycles < 0:
            raise ValueError("Repair cadence must be a nonnegative integer; zero disables periodic reconciliation.")
        object.__setattr__(self, "coin", coin)
        object.__setattr__(self, "output_root", Path(self.output_root).resolve())

    @property
    def dataset_dir(self) -> Path:
        return self.output_root / self.coin / self.interval


def expected_close_ms(now_ms: int, interval_ms: int, close_delay_seconds: float) -> int:
    """Newest exclusive candle-close boundary whose publication delay has elapsed."""
    return math.floor((now_ms - close_delay_seconds * 1000) / interval_ms) * interval_ms


def next_wake_time(now_seconds: float, interval_ms: int, close_delay_seconds: float) -> float:
    """Next future UTC candle-close boundary plus the exchange publication delay."""
    duration = interval_ms / 1000
    return (math.floor((now_seconds - close_delay_seconds) / duration) + 1) * duration + close_delay_seconds


def _utc(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}-{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                # Windows readers can briefly deny replacement while holding
                # the previous status file open. Retry this tiny local write.
                if attempt == 5:
                    raise
                time.sleep(0.01 * 2 ** attempt)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def request_stop(config: LoopConfig) -> dict:
    config.dataset_dir.mkdir(parents=True, exist_ok=True)
    path = config.dataset_dir / "stop.request"
    lock = FileLock(str(config.dataset_dir / ".loop.lock"), timeout=0)
    try:
        with lock:
            path.unlink(missing_ok=True)
            return {"status": "already_stopped", "stop_file": str(path)}
    except Timeout:
        pass
    _atomic_json(path, {"requested_at_utc": _utc(time.time())})
    # Handle an exit between the first lock probe and writing the marker. Leave
    # no stale stop request that would accidentally stop a later explicit start.
    try:
        with lock:
            path.unlink(missing_ok=True)
            return {"status": "already_stopped", "stop_file": str(path)}
    except Timeout:
        pass
    return {"status": "stop_requested", "stop_file": str(path)}


def read_status(config: LoopConfig) -> dict:
    path = config.dataset_dir / "loop_status.json"
    if not path.exists():
        return {"status": "not_started", "status_file": str(path)}
    status = json.loads(path.read_text(encoding="utf-8"))
    # The process may have crashed since its last status write. A free lifetime
    # lock is stronger evidence than trusting a persisted PID that can be reused.
    lock = FileLock(str(config.dataset_dir / ".loop.lock"), timeout=0)
    try:
        with lock:
            status["worker_running"] = False
    except Timeout:
        status["worker_running"] = True
    status["stop_requested"] = (config.dataset_dir / "stop.request").exists()
    if not status["worker_running"] and status.get("status") not in {"stopped", "not_started"}:
        status["status"] = "not_running"
    return status


def run_loop(
    config: LoopConfig, *, max_cycles: int | None = None, stop_event=None,
    clock=time.time, wait=None, pipeline_runner=run_pipeline, monotonic=time.perf_counter,
) -> dict:
    """Run sequential updates with injectable time/network for deterministic tests.

    ``max_cycles`` counts attempts, including failed requests. ``wait(seconds)``
    may be supplied by a test and returns True when shutdown was requested.
    Startup always catches up once; normal success sleeps to the next boundary.
    """
    if max_cycles is not None and (not isinstance(max_cycles, int) or max_cycles < 1):
        raise ValueError("max_cycles must be a positive integer or None.")
    dataset_dir = config.dataset_dir
    dataset_dir.mkdir(parents=True, exist_ok=True)
    stop_event = threading.Event() if stop_event is None else stop_event
    stop_path = dataset_dir / "stop.request"
    status_path = dataset_dir / "loop_status.json"
    event_path = dataset_dir / "loop_events.jsonl"
    interval_ms = INTERVAL_MS[config.interval]
    state = {
        "version": 1, "pid": os.getpid(), "instance_id": uuid4().hex,
        "coin": config.coin, "interval": config.interval,
        "status": "starting", "reason": None, "started_at_utc": _utc(clock()),
        "cycle_count": 0, "successful_cycles": 0, "failures": 0,
        "consecutive_retries": 0, "last_result": None, "last_error": None,
        "last_cycle_timing": None,
        "next_wake_utc": None,
        "close_delay_seconds": config.close_delay_seconds,
        "retry_seconds": config.retry_seconds, "max_retry_seconds": config.max_retry_seconds,
        "repair_every_cycles": config.repair_every_cycles,
        "dataset_dir": str(dataset_dir), "status_file": str(status_path),
    }

    def stopping():
        return stop_event.is_set() or stop_path.exists()

    def persist(event: str):
        state["updated_at_utc"] = _utc(clock())
        # Logging trouble must not introduce an extra prerequisite for fetching.
        try:
            _atomic_json(status_path, state)
        except OSError as exc:
            print(f"Loop status write failed: {type(exc).__name__}", file=sys.stderr, flush=True)
        entry = {
            "event": event, "at_utc": state["updated_at_utc"],
            "instance_id": state["instance_id"], "pid": state["pid"],
            "cycle_count": state["cycle_count"], "status": state["status"],
            "last_result": state["last_result"], "last_error": state["last_error"],
            "last_cycle_timing": state["last_cycle_timing"],
            "next_wake_utc": state["next_wake_utc"], "reason": state["reason"],
        }
        try:
            with event_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, allow_nan=False) + "\n")
        except OSError as exc:
            print(f"Loop event write failed: {type(exc).__name__}", file=sys.stderr, flush=True)
        print(json.dumps(entry, allow_nan=False), flush=True)

    def default_wait(seconds):
        deadline = clock() + seconds
        while not stopping():
            remaining = deadline - clock()
            if remaining <= 0:
                return False
            # Short chunks notice stop.request quickly and handle machine sleep.
            if stop_event.wait(min(1.0, remaining)):
                return True
        return True

    wait_for = default_wait if wait is None else wait
    # The lifetime lock prevents a second loop. The pipeline retains its shorter
    # update lock so a one-shot manual refresh can still coexist between cycles.
    with FileLock(str(dataset_dir / ".loop.lock"), timeout=0):
        persist("started")
        try:
            while not stopping():
                started_counter = monotonic()
                started = clock()
                state["status"] = "updating"
                state["cycle_count"] += 1
                state["last_started_at_utc"] = _utc(started)
                state["next_wake_utc"] = None
                repair = bool(config.repair_every_cycles and state["cycle_count"] % config.repair_every_cycles == 0)
                try:
                    state["updated_at_utc"] = _utc(started)
                    _atomic_json(status_path, state)
                except OSError as exc:
                    print(f"Loop status write failed: {type(exc).__name__}", file=sys.stderr, flush=True)
                try:
                    result = pipeline_runner(
                        coin=config.coin, interval=config.interval,
                        output_root=config.output_root, info_url=config.info_url,
                        as_of_ms=int(started * 1000), refresh_history=repair,
                    )
                    finished = clock()
                    closed = pd.Timestamp(result["last_close_utc"])
                    if closed.tzinfo is None:
                        raise ValueError("Pipeline returned a candle-close timestamp without a timezone.")
                    last_close = closed.value // 1_000_000
                    expected = expected_close_ms(int(finished * 1000), interval_ms, config.close_delay_seconds)
                    behind = max(0, (expected - last_close) // interval_ms)
                    state["last_result"] = {
                        key: result[key] for key in (
                            "status", "run_id", "rows", "new_candles", "corrected_candles",
                            "repaired_candles", "last_close_utc", "gap_count",
                            "missing_candle_count", "recoverable_gap_count", "unrecoverable_gap_count",
                            "summary_path", "latest_pointer",
                        ) if key in result
                    }
                    state["last_result"].update({
                        "lag_intervals_after_delay": int(behind),
                        "expected_close_utc": _utc(expected / 1000),
                        "history_reconciliation": repair,
                    })
                    state["successful_cycles"] += 1
                    state["last_error"] = None
                    state["last_finished_at_utc"] = _utc(finished)
                    if behind:
                        state["consecutive_retries"] += 1
                        state["status"] = "waiting_for_candle"
                        delay = min(config.max_retry_seconds, config.retry_seconds * 2 ** min(state["consecutive_retries"] - 1, 30))
                        wake = finished + delay
                    else:
                        state["consecutive_retries"] = 0
                        state["status"] = "waiting"
                        wake = next_wake_time(finished, interval_ms, config.close_delay_seconds)
                    state["next_wake_utc"] = _utc(wake)
                    state["last_cycle_timing"] = {
                        "started_at_utc": _utc(started),
                        "finished_at_utc": _utc(finished),
                        # Includes waiting for the coordinator slot and the
                        # completed snapshot publication, but not this log write.
                        "total_seconds": monotonic() - started_counter,
                        "outcome": "success", "result_status": result["status"],
                        "run_id": result.get("run_id"),
                        "history_reconciliation": repair,
                        "timings_seconds": dict(result.get("timings_seconds", {})),
                    }
                    if "queue_wait_seconds" in result:
                        state["last_cycle_timing"]["queue_wait_seconds"] = result["queue_wait_seconds"]
                    persist("cycle")
                except StopRequested:
                    state["reason"] = "stop_requested"
                    break
                except Exception as exc:
                    finished = clock()
                    state["failures"] += 1
                    state["consecutive_retries"] += 1
                    state["status"] = "retrying"
                    state["last_error"] = {"type": type(exc).__name__, "message": str(exc)}
                    # A failed attempt must not repeat the previous successful
                    # snapshot or its duration as though it belongs to this cycle.
                    state["last_result"] = None
                    state["last_finished_at_utc"] = _utc(finished)
                    state["last_cycle_timing"] = {
                        "started_at_utc": _utc(started),
                        "finished_at_utc": _utc(finished),
                        "total_seconds": monotonic() - started_counter,
                        "outcome": "failed", "result_status": None,
                        "history_reconciliation": repair, "timings_seconds": {},
                    }
                    delay = min(config.max_retry_seconds, config.retry_seconds * 2 ** min(state["consecutive_retries"] - 1, 30))
                    wake = finished + delay
                    state["next_wake_utc"] = _utc(wake)
                    persist("error")
                if max_cycles is not None and state["cycle_count"] >= max_cycles:
                    state["reason"] = "max_cycles"
                    break
                if stopping() or wait_for(max(0.0, wake - clock())):
                    state["reason"] = "stop_requested"
                    break
        except KeyboardInterrupt:
            state["reason"] = "keyboard_interrupt"
        finally:
            state["status"] = "stopped"
            state["reason"] = state["reason"] or "stop_requested"
            state["next_wake_utc"] = None
            persist("stopped")
            # This marker belongs only to this dataset worker, and is consumed
            # while holding its lifetime lock. No process is killed by PID.
            stop_path.unlink(missing_ok=True)
    return state


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coin", default="BTC")
    parser.add_argument("--interval", choices=tuple(INTERVAL_MS), default="15m")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--info-url", default=None)
    parser.add_argument("--close-delay-seconds", type=float, default=5)
    parser.add_argument("--retry-seconds", type=float, default=15)
    parser.add_argument("--max-retry-seconds", type=float, default=120)
    parser.add_argument("--repair-every-cycles", type=int, default=96)
    parser.add_argument("--max-cycles", type=int, default=None)
    control = parser.add_mutually_exclusive_group()
    control.add_argument("--status", action="store_true")
    control.add_argument("--stop", action="store_true")
    args = parser.parse_args(argv)
    from app.config import hyperliquid_info_url
    config = LoopConfig(
        coin=args.coin, interval=args.interval, output_root=args.output_root,
        info_url=args.info_url or hyperliquid_info_url(), close_delay_seconds=args.close_delay_seconds,
        retry_seconds=args.retry_seconds, max_retry_seconds=args.max_retry_seconds,
        repair_every_cycles=args.repair_every_cycles,
    )
    if args.status or args.stop:
        result = read_status(config) if args.status else request_stop(config)
        print(json.dumps(result, indent=2))
        return 0
    stopped = threading.Event()

    def handle_signal(signum, frame):
        stopped.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    try:
        run_loop(config, max_cycles=args.max_cycles, stop_event=stopped)
    except Timeout:
        print(f"A {config.coin}/{config.interval} data loop already owns {config.dataset_dir / '.loop.lock'}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
