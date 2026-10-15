"""Own the real-data five-minute Paper workflow and its three existing workers.

Data publication, exact-snapshot fitting, and Paper forecast validation enforce
the per-candle order. Paper's quote/risk polling continues during model fitting.
This entry point has no Powder or live-order execution path.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import threading
import time

from filelock import FileLock, Timeout

from ml.hyperliquid_data_loop import _atomic_json, _utc
from ml.hyperliquid_model_config import load_config as load_models
from ml.hyperliquid_paper_policy import DEFAULT_PAPER_CONFIG_PATH, load_config as load_paper


def configuration(config_path):
    paper = load_paper(config_path)
    models = load_models(paper.model_config)
    markets = models.load_markets()
    if markets.output_root != paper.data_root:
        raise ValueError("Paper, model and market workers must use the same datastore.")
    if markets.interval != "5m" or models.horizons_bars != (1,):
        raise ValueError("The Paper loop requires 5m candles and a one-candle horizon.")
    if not models.retrain_before_predict:
        raise ValueError("The Paper loop requires retrain_before_predict=true.")
    return paper, models, markets


def control_dir(root):
    return Path(root).resolve() / "_paper_loop"


def worker_locks(root, markets):
    root = Path(root)
    return [root / "_coordinator" / ".coordinator.lock",
            root / "_models" / "_runtime" / ".runtime.lock",
            root / "_paper" / "_runtime" / ".paper.lock",
            *(root / coin / markets.interval / ".loop.lock" for coin in markets.symbols)]


def _preflight(root, markets):
    # Existing worker locks remain authoritative. Never adopt/stop an unrelated
    # running owner or silently run only some of this workflow's stages.
    for path in worker_locks(root, markets):
        path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(path), timeout=0):
            pass
    markers = [Path(root) / "_coordinator" / "stop.request",
               Path(root) / "_models" / "_runtime" / "stop.request",
               Path(root) / "_paper" / "_runtime" / "stop.request",
               *(Path(root) / coin / markets.interval / "stop.request" for coin in markets.symbols)]
    if any(path.exists() for path in markers):
        raise ValueError("An existing worker stop request is pending; resolve it before starting the Paper loop.")


def read_status(root):
    directory = control_dir(root)
    path = directory / "status.json"
    result = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"status": "not_started"}
    result["running"] = False
    if directory.exists():
        try:
            with FileLock(str(directory / ".loop.lock"), timeout=0):
                pass
        except Timeout:
            result["running"] = True
    if not result["running"] and result["status"] in {"starting", "running", "degraded", "stopping"}:
        result["status"] = "not_running"
    result["stop_requested"] = (directory / "stop.request").exists()
    result["components"] = {}
    for name, relative in (("data", "_coordinator/coordinator_status.json"),
                           ("models", "_models/_runtime/status.json"),
                           ("paper", "_paper/_runtime/status.json")):
        component = Path(root) / relative
        try:
            result["components"][name] = json.loads(component.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            result["components"][name] = {"status": "unavailable", "error": str(exc)}
    return result


def request_stop(root):
    directory = control_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(directory / ".loop.lock"), timeout=0)
    try:
        with lock:
            return {"status": "already_stopped"}
    except Timeout:
        _atomic_json(directory / "stop.request", {"requested_at_utc": _utc(time.time())})
        return {"status": "stop_requested"}


def _worker_functions(config_path, models, stop_event, once):
    # Import only when starting; --status and --stop do not instantiate workers,
    # initialize a ledger, load credentials, fetch data, or fit a model.
    from ml.hyperliquid_coordinator import Coordinator
    from ml.hyperliquid_model_runtime import ModelRuntime
    from ml.hyperliquid_paper_runtime import PaperRuntime
    from app.config import hyperliquid_info_url

    def paper():
        runtime = PaperRuntime(config_path)
        runtime._stopped = stop_event
        return runtime.run(once=once)

    return {
        "data": lambda: Coordinator(models.markets_config, default_info_url=hyperliquid_info_url()).run(
            max_cycles=1 if once else None, stop_event=stop_event),
        "models": lambda: ModelRuntime(load_paper(config_path).model_config).run(
            once=once, stop_event=stop_event),
        "paper": paper,
    }


def run(config_path=DEFAULT_PAPER_CONFIG_PATH, *, once=False, stop_event=None,
        worker_factory=_worker_functions, clock=time.time):
    paper, models, markets = configuration(config_path)
    directory = control_dir(paper.data_root)
    directory.mkdir(parents=True, exist_ok=True)
    stopped = threading.Event() if stop_event is None else stop_event
    marker = directory / "stop.request"
    state = {"version": 1, "mode": "paper", "status": "starting", "pid": os.getpid(),
             "started_at_utc": _utc(clock()), "interval": "5m", "horizon_bars": 1,
             "retrain_before_predict": True, "once": once, "workers": {}, "last_error": None}

    def persist():
        state["updated_at_utc"] = _utc(clock())
        _atomic_json(directory / "status.json", state)

    with FileLock(str(directory / ".loop.lock"), timeout=0):
        _preflight(paper.data_root, markets)
        # This explicit start supersedes this launcher's old stop marker only.
        marker.unlink(missing_ok=True)
        workers = worker_factory(Path(config_path).resolve(), models, stopped, once)
        persist()
        try:
            if once:
                # A bounded operational pass has the same dependencies, but no
                # concurrent waiting and no Paper action after an upstream failure.
                for name, worker in workers.items():
                    if stopped.is_set() or marker.exists():
                        break
                    state["workers"][name] = {"status": "running"}
                    persist()
                    result = worker()
                    state["workers"][name] = {"status": result.get("status"), "result": result}
                    if result.get("status") == "failed" or result.get("last_error"):
                        raise RuntimeError(f"{name} did not complete its bounded pass.")
                state["status"] = "stopped"
            else:
                # Model fitting and data fetching use different workers so a fit
                # cannot suspend Paper risk checks or incoming candle retention.
                with ThreadPoolExecutor(max_workers=3, thread_name_prefix="hyperliquid-paper-loop") as executor:
                    jobs = {name: executor.submit(worker) for name, worker in workers.items()}
                    state["workers"] = {name: {"status": "running"} for name in workers}
                    state["status"] = "running"
                    persist()
                    try:
                        while not stopped.is_set() and not marker.exists():
                            for name, job in jobs.items():
                                if job.done():
                                    result = job.result()
                                    state["workers"][name] = {"status": result.get("status"), "result": result}
                                    # A component stop is a workflow stop. Do not
                                    # resurrect manual stop requests automatically.
                                    if result.get("status") == "failed" or result.get("last_error"):
                                        raise RuntimeError(f"{name} worker failed; see component status.")
                                    state["stop_reason"] = f"{name}_stopped"
                                    stopped.set()
                                    break
                            persist()
                            stopped.wait(1.0)
                    finally:
                        stopped.set()
                        state["status"] = "stopping"
                        persist()
                        # All workers finish their current atomic operation, then
                        # close their own ledgers/locks before ownership is released.
                        for name, job in jobs.items():
                            try:
                                result = job.result()
                                state["workers"][name] = {"status": result.get("status"), "result": result}
                                if result.get("status") == "failed" or result.get("last_error"):
                                    state["last_error"] = f"{name} worker failed; see component status."
                            except Exception as exc:
                                state["workers"][name] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
                                state["last_error"] = state["workers"][name]["error"]
                state["status"] = "failed" if state["last_error"] else "stopped"
        except Exception as exc:
            state.update(status="failed", last_error=f"{type(exc).__name__}: {exc}")
        finally:
            stopped.set()
            persist()
            marker.unlink(missing_ok=True)
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_PAPER_CONFIG_PATH)
    controls = parser.add_mutually_exclusive_group()
    controls.add_argument("--once", action="store_true")
    controls.add_argument("--status", action="store_true")
    controls.add_argument("--stop", action="store_true")
    args = parser.parse_args(argv)
    stopped = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    try:
        root = load_paper(args.config).data_root
        result = (read_status(root) if args.status else request_stop(root) if args.stop else
                  run(args.config, once=args.once, stop_event=stopped))
    except (OSError, ValueError, Timeout) as exc:
        result = {"status": "failed", "last_error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, indent=2))
    return 1 if result.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
