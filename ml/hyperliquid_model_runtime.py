"""Independent Hyperliquid research training, prediction and forward evaluation.

Run with --once to benchmark one pass, or without it to follow completed candle
snapshots. Model fitting runs in one background worker; prediction continues on
the previously published bundle. This module has no order-execution capability.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
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
import pandas as pd
from threadpoolctl import threadpool_limits

from ml.hyperliquid_data_loop import _atomic_json, _utc
from ml.hyperliquid_data_pipeline import DEFAULT_OUTPUT_ROOT
from ml.hyperliquid_model_config import DEFAULT_MODEL_CONFIG_PATH, load_config
from ml.hyperliquid_models import load_snapshot, predict_bundle, train_candidate
from ml.hyperliquid_model_artifacts import (
    model_dir, publish_candidate, load_predictor, record_prediction, score_matured,
)


def _runtime_dir(output_root):
    return Path(output_root).resolve() / "_models" / "_runtime"


def read_status(output_root=DEFAULT_OUTPUT_ROOT):
    directory = _runtime_dir(output_root)
    path = directory / "status.json"
    if not path.exists():
        return {"status": "not_started", "runtime_running": False, "status_file": str(path)}
    state = json.loads(path.read_text(encoding="utf-8"))
    try:
        with FileLock(str(directory / ".runtime.lock"), timeout=0):
            state["runtime_running"] = False
    except Timeout:
        state["runtime_running"] = True
    state["stop_requested"] = (directory / "stop.request").exists()
    if not state["runtime_running"] and state["status"] not in {"stopped", "failed"}:
        state["status"] = "not_running"
    return state


def request_stop(output_root=DEFAULT_OUTPUT_ROOT):
    directory = _runtime_dir(output_root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "stop.request"
    lock = FileLock(str(directory / ".runtime.lock"), timeout=0)
    try:
        with lock:
            path.unlink(missing_ok=True)
            return {"status": "already_stopped"}
    except Timeout:
        pass
    _atomic_json(path, {"requested_at_utc": _utc(time.time())})
    try:
        with lock:
            path.unlink(missing_ok=True)
            return {"status": "already_stopped"}
    except Timeout:
        return {"status": "stop_requested", "stop_file": str(path)}


class ModelRuntime:
    """One training job at a time, with no queue of obsolete snapshot jobs."""

    def __init__(self, config_path=DEFAULT_MODEL_CONFIG_PATH, *, train_fn=train_candidate,
                 predict_fn=predict_bundle, snapshot_loader=load_snapshot, clock=time.time,
                 force_train=False, timer=time.perf_counter):
        self.config_path = Path(config_path).resolve()
        self.config = load_config(self.config_path)
        self.markets = self.config.load_markets()
        self.output_root = self.markets.output_root
        self.directory = _runtime_dir(self.output_root)
        self.train_fn, self.predict_fn = train_fn, predict_fn
        self.snapshot_loader, self.clock = snapshot_loader, clock
        self.timer = timer
        self.force_train = force_train
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hyperliquid-model-fit")
        self._job = None
        self._snapshots = {}
        self._predictors = {}
        self._slots = {}
        self._attempted = set()
        self._cursor = 0
        self._once = False
        self._stopping = False
        self._started = self.clock()
        self._instance_id = uuid4().hex
        self._status = "starting"
        self._reason = None
        self._config_error = None
        self._completed_jobs = []
        self._timing_write_error = None

    def _refresh_universe(self):
        try:
            settings = load_config(self.config_path)
            if settings != self.config:
                raise ValueError("Restart the model runtime after changing model settings.")
            markets = self.config.load_markets()
            if markets.output_root != self.output_root or markets.interval != self.markets.interval:
                raise ValueError("Restart the model runtime after changing the market interval or datastore.")
            self.markets = markets
            self._config_error = None
        except (OSError, ValueError, TypeError) as exc:
            self._config_error = {"type": type(exc).__name__, "message": str(exc)}

    def _keys(self):
        return [(coin, self.markets.interval, horizon)
                for coin in self.markets.symbols for horizon in self.config.horizons_bars]

    @staticmethod
    def _key_name(key):
        return f"{key[0]}/{key[1]}/h{key[2]}"

    def _slot(self, key):
        if key not in self._slots:
            self._slots[key] = {
                "status": "waiting_for_data", "training": False, "training_attempts": 0,
                "last_error": None, "last_prediction_error": None, "last_prediction": None,
                "candidate": None, "retry_at": 0.0, "last_processed": None,
            }
        return self._slots[key]

    def _snapshot_for(self, coin, interval):
        path = self.output_root / coin / interval / "latest.json"
        pointer = json.loads(path.read_text(encoding="utf-8"))
        key = (coin, interval)
        cached = self._snapshots.get(key)
        if cached is None or cached.run_id != pointer["run_id"]:
            cached = self.snapshot_loader(self.output_root, coin, interval)
            self._snapshots[key] = cached
        return cached

    def _candidate_for(self, key):
        path = model_dir(self.output_root, *key) / "candidate.json"
        if not path.exists():
            return None
        candidate = json.loads(path.read_text(encoding="utf-8"))
        return candidate if isinstance(candidate, dict) else None

    def _is_due(self, key, snapshot, slot):
        if self._stopping or (self._once and key in self._attempted):
            return False
        if self.clock() < slot["retry_at"]:
            return False
        if self.force_train and key not in self._attempted:
            return True
        candidate = slot["candidate"]
        if not candidate:
            return True
        if candidate.get("settings") != asdict(self.config.model_settings(key[2])):
            return True
        if candidate.get("feature_revision") != snapshot.feature_revision:
            return True
        try:
            stamp = pd.Timestamp(candidate["trained_at_utc"])
            if pd.isna(stamp) or stamp.tzinfo is None:
                return True
            age = self.clock() - stamp.timestamp()
            if not math.isfinite(age) or age < 0:
                return True
        except (KeyError, ValueError, TypeError, OverflowError):
            return True
        progress = age
        if candidate.get("source_last_close_utc"):
            try:
                source_close = pd.Timestamp(candidate["source_last_close_utc"])
                if pd.isna(source_close) or source_close.tzinfo is None:
                    return True
                progress = (snapshot.features["close_time"].iloc[-1] - source_close).total_seconds()
                if not math.isfinite(progress) or progress < 0:
                    return True
            except (KeyError, ValueError, TypeError, OverflowError):
                return True
        return progress >= self.config.retrain_seconds and candidate.get("source_run_id") != snapshot.run_id

    def _train_timed(self, snapshot, settings, measurement):
        """Measure work in its worker, excluding executor and collection waits."""
        measurement["worker_started_counter"] = self.timer()
        measurement["worker_started_at_utc"] = _utc(self.clock())
        try:
            return self.train_fn(snapshot, settings)
        finally:
            measurement["worker_finished_counter"] = self.timer()
            measurement["worker_finished_at_utc"] = _utc(self.clock())

    def _record_training_event(self, event):
        # Timing telemetry must not turn an already-published model into a
        # failed fit, or hold up predictions if its journal cannot be written.
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with (self.directory / "training_events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event) + "\n")
            self._timing_write_error = None
        except OSError as exc:
            self._timing_write_error = {"type": type(exc).__name__, "message": str(exc)}
            print(f"Model timing event write failed: {type(exc).__name__}", file=sys.stderr, flush=True)

    def _collect_job(self):
        if self._job is None or not self._job["future"].done():
            return
        job, self._job = self._job, None
        key = job["key"]
        slot = self._slot(key)
        slot["training"] = False
        collected_counter = self.timer()
        elapsed = collected_counter - job["started_counter"]
        measurement = job["measurement"]
        worker_started = measurement.get("worker_started_counter")
        worker_finished = measurement.get("worker_finished_counter")
        timing = {
            "submitted_at_utc": job["submitted_at_utc"],
            "worker_started_at_utc": measurement.get("worker_started_at_utc"),
            "worker_finished_at_utc": measurement.get("worker_finished_at_utc"),
            "collected_at_utc": _utc(self.clock()),
            "worker_seconds": (worker_finished - worker_started
                               if worker_finished is not None and worker_started is not None else None),
            "scheduler_observed_seconds": elapsed,
            "queue_wait_seconds": worker_started - job["started_counter"] if worker_started is not None else None,
            "collection_wait_seconds": collected_counter - worker_finished if worker_finished is not None else None,
            "publication_seconds": None,
        }
        candidate, result, error = None, None, None
        try:
            result = job["future"].result()
        except Exception as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
        if self._stopping or key not in self._keys():
            outcome = "discarded_after_removal_or_stop"
            slot["status"] = outcome
        elif error:
            outcome = "training_failed"
        else:
            publication_started = self.timer()
            try:
                candidate = publish_candidate(
                    self.output_root, job["snapshot"], job["settings"], result, now=self.clock(),
                )
                slot["candidate"] = candidate
                slot["last_error"] = None
                slot["status"] = "trained"
                slot["last_processed"] = None
                slot["last_report"] = {
                    "eligible": result["report"]["eligible"],
                    "metrics": result["report"].get("metrics"),
                    "model_timings": result["report"].get("model_timings"),
                    "total_fit_seconds": result["report"].get("total_fit_seconds"),
                    "total_seconds": result["report"].get("total_seconds"),
                }
                self._predictors.pop(key, None)
                outcome = "promoted" if candidate["eligible"] else "research_only"
            except Exception as exc:
                outcome = "publication_failed"
                error = {"type": type(exc).__name__, "message": str(exc)}
            finally:
                timing["publication_seconds"] = self.timer() - publication_started
        if outcome in {"training_failed", "publication_failed"}:
            slot["status"] = "training_failed"
            slot["last_error"] = error
            slot["retry_at"] = self.clock() + self.config.retry_seconds
        timing["completed_at_utc"] = _utc(self.clock())
        timing["end_to_end_seconds"] = self.timer() - job["started_counter"]
        timing["operation_seconds"] = (
            timing["worker_seconds"] + timing["publication_seconds"]
            if timing["worker_seconds"] is not None and timing["publication_seconds"] is not None else None
        )
        # Retain the legacy submission-to-collection field for readers that
        # already use it; the structured timing separates actual work/waiting.
        slot["last_training_seconds"] = elapsed
        slot["last_training_timing"] = timing
        completed = {"market": self._key_name(key), "seconds": elapsed, "outcome": outcome,
                     "source_run_id": job["snapshot"].run_id, "timing": timing}
        if candidate:
            completed.update(model_id=candidate["model_id"], eligible=candidate["eligible"])
        if error:
            completed["error"] = error
        self._completed_jobs.append(completed)
        report = result.get("report", {}) if isinstance(result, dict) else {}
        self._record_training_event({
            "version": 1, "event": "training_completed", "at_utc": timing["completed_at_utc"],
            "instance_id": self._instance_id, "coin": key[0], "interval": key[1], "horizon_bars": key[2],
            **completed, "report_total_seconds": report.get("total_seconds"),
            "model_timings": report.get("model_timings"),
        })
        # Full reports live in immutable run folders; status only needs recent work.
        del self._completed_jobs[:-100]

    def _predictor_for(self, key, snapshot):
        # Cache fitted objects in memory, but recheck pointer versions and expiry.
        directory = model_dir(self.output_root, *key)
        versions = tuple((name, (directory / name).stat().st_mtime_ns if (directory / name).exists() else None)
                         for name in ("active.json", "candidate.json"))
        cached = self._predictors.get(key)
        if cached and cached[0] == versions:
            record = cached[1]
            age = self.clock() - pd.Timestamp(record["record"]["trained_at_utc"]).timestamp()
            if (0 <= age <= self.config.max_model_age_seconds
                    and record["record"].get("feature_revision") == snapshot.feature_revision):
                return record
        record = load_predictor(self.output_root, *key, now=self.clock(),
                                max_age_seconds=self.config.max_model_age_seconds,
                                expected_feature_revision=snapshot.feature_revision)
        if record:
            self._predictors[key] = (versions, record)
        else:
            self._predictors.pop(key, None)
        return record

    def _predict_and_score(self, key, snapshot, slot):
        predictor = self._predictor_for(key, snapshot)
        identity = (snapshot.run_id, predictor["record"]["model_id"] if predictor else None)
        if slot["last_processed"] == identity:
            return
        slot["forward_metrics"] = score_matured(self.output_root, snapshot, key[2], now=self.clock())
        if predictor is None:
            slot["status"] = "waiting_for_model"
            slot["last_processed"] = identity
            return
        prediction = self.predict_fn(predictor["bundle"], snapshot)
        metadata = {**predictor["record"], "role": predictor["role"]}
        saved = record_prediction(self.output_root, *key, prediction, metadata, now=self.clock())
        slot["last_prediction"] = saved
        slot["last_prediction_error"] = None
        slot["last_processed"] = identity
        slot["status"] = "predicting_with_active" if predictor["role"] == "active" else "predicting_with_research_candidate"

    def tick(self):
        self._refresh_universe()
        self._collect_job()
        keys = self._keys()
        # Discard removed markets' cached frames and fitted objects after jobs
        # finish; their on-disk artifacts and historical forecasts stay intact.
        self._snapshots = {key: value for key, value in self._snapshots.items()
                           if any(key == (k[0], k[1]) for k in keys)}
        self._predictors = {key: value for key, value in self._predictors.items() if key in keys}
        ready = {}
        for key in keys:
            slot = self._slot(key)
            try:
                snapshot = self._snapshot_for(key[0], key[1])
                if slot["status"] == "waiting_for_data":
                    slot["last_error"] = None
                ready[key] = snapshot
                slot["data_run_id"] = snapshot.run_id
                slot["candidate"] = self._candidate_for(key)
                try:
                    self._predict_and_score(key, snapshot, slot)
                except Exception as exc:
                    slot["last_prediction_error"] = {"type": type(exc).__name__, "message": str(exc)}
            except Exception as exc:
                slot["status"] = "waiting_for_data"
                slot["last_error"] = {"type": type(exc).__name__, "message": str(exc)}
                if self._once:
                    self._attempted.add(key)
        if self._job is None and not self._stopping:
            for offset in range(len(keys)):
                index = (self._cursor + offset) % len(keys)
                key = keys[index]
                if key not in ready:
                    continue
                slot = self._slot(key)
                if self._is_due(key, ready[key], slot):
                    settings = self.config.model_settings(key[2])
                    slot["training"] = True
                    slot["training_attempts"] += 1
                    self._attempted.add(key)
                    started = self.timer()
                    submitted_at = _utc(self.clock())
                    measurement = {}
                    future = self._executor.submit(self._train_timed, ready[key], settings, measurement)
                    self._job = {"key": key, "snapshot": ready[key], "settings": settings,
                                 "future": future, "started_counter": started,
                                 "submitted_at_utc": submitted_at, "measurement": measurement}
                    self._cursor = (index + 1) % len(keys)
                    break
        self._status = "running"
        state = self._snapshot()
        self._persist(state)
        return state

    def _snapshot(self):
        return {
            "version": 1, "pid": os.getpid(), "instance_id": self._instance_id,
            "status": self._status, "reason": self._reason,
            "started_at_utc": _utc(self._started), "updated_at_utc": _utc(self.clock()),
            "config_path": str(self.config_path), "config_error": self._config_error,
            "data_root": str(self.output_root), "horizons_bars": list(self.config.horizons_bars),
            "retrain_seconds": self.config.retrain_seconds, "model_threads": self.config.model_threads,
            "training_market": self._key_name(self._job["key"]) if self._job else None,
            "elapsed_seconds": self.clock() - self._started,
            "completed_jobs": self._completed_jobs,
            "timing_write_error": self._timing_write_error,
            "markets": {self._key_name(key): {name: value for name, value in self._slot(key).items()
                                             if name not in {"last_processed", "retry_at"}}
                        for key in self._keys()},
        }

    def _persist(self, state):
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            _atomic_json(self.directory / "status.json", state)
        except OSError as exc:
            print(f"Model runtime status write failed: {type(exc).__name__}", file=sys.stderr, flush=True)

    def shutdown(self):
        self._stopping = True
        self._status = "stopping"
        self._persist(self._snapshot())
        # Fitting has bounded iterations and no child-shell restarts. Let a
        # current fit finish, then discard it instead of partially publishing.
        self._executor.shutdown(wait=True, cancel_futures=True)
        self._collect_job()
        self._status = "failed" if self._reason == "cycle_failure" else "stopped"
        state = self._snapshot()
        self._persist(state)
        return state

    def run(self, *, once=False, stop_event=None, max_polls=None):
        if max_polls is not None and (type(max_polls) is not int or max_polls < 1):
            raise ValueError("max_polls must be a positive integer or None.")
        self._once = once
        stopped = threading.Event() if stop_event is None else stop_event
        self.directory.mkdir(parents=True, exist_ok=True)
        marker = self.directory / "stop.request"
        polls = 0
        try:
            with FileLock(str(self.directory / ".runtime.lock"), timeout=0):
                with threadpool_limits(limits=self.config.model_threads):
                    try:
                        while not stopped.is_set() and not marker.exists():
                            self.tick()
                            polls += 1
                            if once and self._job is None:
                                due = any(key not in self._attempted and (
                                    self._slots[key].get("candidate") is None or self.force_train)
                                    for key in self._keys())
                                if not due:
                                    failed = any(self._slot(k)["last_error"] or self._slot(k)["last_prediction_error"]
                                                 or not self._slot(k)["last_prediction"] for k in self._keys())
                                    self._reason = "cycle_failure" if failed else "once"
                                    break
                            if max_polls is not None and polls >= max_polls:
                                self._reason = "max_polls"
                                break
                            stopped.wait(min(self.config.poll_seconds, 0.1) if once else self.config.poll_seconds)
                    except KeyboardInterrupt:
                        self._reason = "keyboard_interrupt"
                    finally:
                        self._reason = self._reason or "stop_requested"
                        state = self.shutdown()
                        marker.unlink(missing_ok=True)
                return state
        except BaseException:
            self._executor.shutdown(wait=True, cancel_futures=True)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_MODEL_CONFIG_PATH)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--force-train", action="store_true", help="Train each configured symbol/horizon once even if not due.")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT, help="Datastore root for status/stop controls.")
    control = parser.add_mutually_exclusive_group()
    control.add_argument("--status", action="store_true")
    control.add_argument("--stop", action="store_true")
    args = parser.parse_args(argv)
    if args.status or args.stop:
        result = read_status(args.output_root) if args.status else request_stop(args.output_root)
    else:
        stopped = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stopped.set())
        signal.signal(signal.SIGTERM, lambda *_: stopped.set())
        try:
            result = ModelRuntime(args.config, force_train=args.force_train).run(once=args.once, stop_event=stopped)
        except Timeout:
            print("Another Hyperliquid model runtime already owns this datastore.", file=sys.stderr)
            return 2
        except (OSError, ValueError, TypeError) as exc:
            print(f"Hyperliquid model runtime failed: {exc}", file=sys.stderr)
            return 1
    print(json.dumps(result, indent=2))
    return 1 if result.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
