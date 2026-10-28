"""Independent Scout 1A preparation and 1B model/trading service entrypoints.

Only the new manually activated controller can construct a trading role.
The separate data entrypoint has no broker, order or trading activation path.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path
import argparse
import hashlib
import json
import os
import threading
import time

from .prediction import ContractError, STEP, digest, encoded, instant
from .store import utcnow
from .service_handoff import ReadySpool, ServiceStore, full_snapshot
from .live_session import ExecutionQueue


class ServiceExecutionQueue(ExecutionQueue):
    """Bounded pending forecast work; order recovery remains journal-owned."""
    def retire(self, now):
        with self.store.connect() as db:
            rows = db.execute("SELECT q.forecast_id,q.symbol,f.reference_at,f.issued_at,f.payload FROM intraday_live_queue q "
                "JOIN forecasts f ON f.id=q.forecast_id WHERE q.session_id=? AND f.pc=? AND q.status='pending' "
                "ORDER BY f.reference_at DESC,f.issued_at DESC", (self.session_id, self.pc)).fetchall()
        seen, retired = set(), []
        for key, symbol, reference, issued, raw in rows:
            forecast = json.loads(raw)
            state = ("expired" if instant(forecast["target_close"]) <= instant(now)
                     else "superseded" if symbol in seen else None)
            seen.add(symbol)
            if state:
                retired.append((state, key, self.session_id))
        if retired:
            with self.store.connect() as db:
                db.executemany("UPDATE intraday_live_queue SET status=? WHERE forecast_id=? AND session_id=? AND status='pending'", retired)

    def enqueue(self, forecast):
        super().enqueue(forecast)
        self.retire(forecast["issued_at"])

    def due(self, now):
        self.retire(now)
        return super().due(now)


def metric(root, role, phase, **values):
    path = Path(root) / (role + "-metrics.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(encoded({"recorded_at": utcnow(), "role": role, "phase": phase, **values}) + "\n")


def _configuration(repository, datastore, release, *, workers):
    from .runtime import ResearchConfig, local_symbols
    from .live_model_recipe import prepare_manual_models
    repository, datastore = Path(repository), Path(datastore)
    pc, symbols = local_symbols(repository / "scratch/cross-pc/local-profile.json",
                                repository / "datafetching/watchlist.local.txt", datastore)
    if pc != "Scout":
        raise ContractError("Scout independent services require Scout ownership")
    recipe_path = Path(release["recipe_path"])
    recipe_hash = hashlib.sha256(recipe_path.read_bytes()).hexdigest()
    if recipe_hash != release["recipe_file_sha256"] or list(symbols) != release["symbols"]:
        raise ContractError("Service release recipe or symbol identity changed")
    config, runtime = prepare_manual_models(ResearchConfig(symbol_workers=workers,
        publication_offset_seconds=30, use_supervised_models=True), datastore=datastore,
        pc=pc, symbols=symbols, recipe_path=recipe_path)
    # Worker concurrency is an execution resource limit, outside the frozen
    # scientific model/preprocessing/calibration/population contract.
    return pc, symbols, config, runtime, recipe_hash, digest(json.loads(recipe_path.read_bytes()))


class PreparedHistories:
    """Nonblocking exact-reference handoff; missing acquisition never waits here."""
    def __init__(self, spool, store, reference, *, clock=utcnow):
        self.spool, self.store, self.reference, self.clock = spool, store, instant(reference), clock
        self.cycle_id = None
        self.missing = set(spool.symbols)

    def bind_cycle(self, cycle_id):
        self.cycle_id = cycle_id
        self.store.ready_loader = self.load

    def load(self, cycle_id, symbol):
        from .data import AcquisitionPending
        import sqlite3
        if cycle_id != self.cycle_id or symbol not in self.spool.symbols:
            raise ContractError("Ready input request exceeds the latched model cycle")
        try:
            ready = self.spool.ready(symbol, self.reference, now=self.clock())
            if ready is None:
                raise AcquisitionPending("system_1a_snapshot_pending", retry_after_seconds=3)
            envelope = ready["envelope"]
            if digest(envelope) != ready["execution_input_hash"]:
                raise ContractError("Ready execution projection changed")
            snapshot = full_snapshot(ready["snapshot_hash"], envelope)
            if (digest(snapshot["feature_names"]) != ready["feature_order_hash"]
                    or digest(snapshot.get("source_manifest", {})) != ready["provenance_hash"]
                    or instant(snapshot["cutoff"]) != instant(ready["training_cutoff"])):
                raise ContractError("Ready scientific metadata changed")
            self.store.attach(cycle_id, symbol, envelope)
            self.missing.discard(symbol)
            metric(self.spool.root, "model", "queue_wait", symbol=symbol,
                   reference_at=self.reference.isoformat(), ready_id=ready["ready_id"],
                   seconds=max(0, (instant(self.clock()) - instant(ready["published_at"])).total_seconds()))
            return ready["snapshot_hash"], snapshot
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            raise AcquisitionPending("system_1a_handoff_busy", retry_after_seconds=1) from exc

    def get(self, symbol):
        from .data import AcquisitionPending
        if symbol not in self.spool.symbols:
            raise ContractError("Model work exceeds Scout symbol scope")
        raise AcquisitionPending("system_1a_snapshot_pending", retry_after_seconds=3)


def _model_checkpoint(store, lifecycle, result, symbols):
    from .iteration_boundary import inspect_cycle
    inspection = inspect_cycle(store, result["cycle_id"], session_id=lifecycle.session_id, symbols=symbols)
    return {"schema": "scout-independent-model-checkpoint-v1",
            "boundary_kind": "full_symbol_attempt_with_durable_deferrals",
            "boundary_cycle_id": result["cycle_id"], "symbols": list(symbols),
            "started_at": lifecycle.started_at, "wake_at": lifecycle.wake_at,
            "deadline": lifecycle.deadline, "owned_workers_drained": True,
            "inspection": inspection}


def run_model_service(*, repository, datastore, release, lifecycle, clock=utcnow, sleep=time.sleep):
    from .runtime import ShadowRunner
    from .live_session import ExecutionQueue, latest_completed_reference, eligible_reference, _session_cycles
    from .iteration_boundary import inspect_cycle
    pc, symbols, config, runtime, recipe_hash, recipe_identity = _configuration(repository, datastore, release, workers=2)
    service_root = Path(datastore) / "intraday/services"
    store = ServiceStore(Path(datastore) / "intraday/shadow.sqlite3", object_root=service_root)
    store.run_binding = {"logical_run_id": lifecycle.session_id,
                         "original_execution_deadline": lifecycle.deadline}
    spool = ReadySpool(service_root, pc=pc, symbols=symbols, recipe_hash=recipe_hash,
                       recipe_identity=recipe_identity, clock=clock)
    previous = lifecycle.previous_checkpoint
    if previous:
        checked = inspect_cycle(store, previous["boundary_cycle_id"], session_id=lifecycle.session_id, symbols=symbols)
        if checked != previous.get("inspection"):
            raise ContractError("Model checkpoint scientific evidence changed")
    complete = threading.Event()
    stopped = lambda: complete.is_set() or lifecycle.stop_reason() is not None
    def pause(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end and not stopped():
            sleep(min(.2, max(0, end-time.monotonic())))
    queue = ServiceExecutionQueue(store, lifecycle.session_id, pc=pc)
    runner = ShadowRunner(store=store, pc=pc, symbols=symbols, config=config, stopped=stopped,
                          clock=clock, **runtime)
    completed = []
    def on_iteration(result):
        completed.append(result)
        complete.set()
    lifecycle.ready()
    begun = time.perf_counter()
    while not stopped():
        reference = latest_completed_reference(clock(), publication_offset_seconds=config.publication_offset_seconds)
        if not eligible_reference(reference):
            pause(.2)
            continue
        saved = next((cycle for _, cycle in _session_cycles(store, lifecycle.session_id, pc=pc)
                      if instant(cycle["reference_at"]) == reference), None)
        retry_symbols = None
        if saved is not None:
            # No historical live backlog. Original failures and expired inputs
            # remain evidence; only missing due inputs for the newest quarter
            # can become a current training opportunity.
            retry_symbols = [symbol for symbol, error in saved.get("failures", {}).items()
                             if error.get("retryable") and instant(error["retry_at"]) <= instant(clock())]
            if saved.get("status") == "completed" or not retry_symbols:
                pause(.2)
                continue
        began = time.perf_counter()
        result = runner.run_cycle(PreparedHistories(spool, store, reference, clock=clock),
            reference_at=reference, mode="live", experiment=lifecycle.session_id,
            on_published=queue.enqueue, retry_symbols=retry_symbols,
            acquisition={"kind": "independent_verified_ready_snapshot"})
        metric(service_root, "model", "model_work", seconds=time.perf_counter()-began,
               cycle_id=result["cycle_id"], reference_at=reference.isoformat())
        on_iteration(result)
    if lifecycle.stop_reason() is not None:
        return {"status": "STOPPED", "stop_reason": lifecycle.stop_reason()}
    if len(completed) != 1:
        raise ContractError("Model worker exited without one returned full-scope iteration")
    result = completed[0]
    # run_cycle's executor context has drained before this callback returns.
    lifecycle.checkpoint(_model_checkpoint(store, lifecycle, result, symbols))
    metric(service_root, "model", "full_scope_iteration", seconds=time.perf_counter()-begun,
           cycle_id=result["cycle_id"], status=result["status"],
           deadline_misses=sum(bool(f.get("issue_after_target")) for f in result["forecasts"].values()))
    return {"status": "PLANNED_RESTART", "cycle_id": result["cycle_id"], "cycle_status": result["status"]}


def run_trading_service(*, repository, datastore, release, lifecycle):
    from .live_session import run_manual
    from .runtime import ResearchConfig
    return run_manual(repository=Path(repository), datastore=Path(datastore),
        profile=Path(repository) / "scratch/cross-pc/local-profile.json",
        watchlist=Path(repository) / "datafetching/watchlist.local.txt",
        config=ResearchConfig(use_supervised_models=True), scout_admission="broker-local",
        model_recipe=Path(release["recipe_path"]), lifecycle=lifecycle, independent_services=True)


def run_data_service(*, repository, datastore, release, clock=utcnow, sleep=time.sleep,
                     stopped=lambda: False):
    """One separately authorized data day; never observes the trader stop latch."""
    from datafetching.runtime_lock import exclusive_runtime_lock
    from dotenv import load_dotenv
    from .data import AcquisitionJournal, load_local_history
    from .runtime import ShadowRunner
    from .live_session import (automatic_stop_deadline, manual_wake_at, eligible_reference,
        latest_completed_reference, prepare_minute_provider, prepare_market_collection, scoring_loop)
    from .live_model_recipe import native_interval_symbols_for_runtime
    from .minute_feed import QuarterHourMinuteFeed
    from tools.scout_release import atomic_json
    pc, symbols, config, runtime, recipe_hash, recipe_identity = _configuration(repository, datastore, release, workers=2)
    root = Path(datastore) / "intraday/services"
    spool = ReadySpool(root, pc=pc, symbols=symbols, recipe_hash=recipe_hash,
                       recipe_identity=recipe_identity, clock=clock)
    wake = manual_wake_at(clock())
    deadline = automatic_stop_deadline(wake)
    session = "scout-data-" + wake.date().isoformat() + "-" + recipe_hash[:16]
    halt = threading.Event()
    # 1A owns the latched data day, including the final native window after
    # 17:00. Trading's fixed deadline never closes this acquisition lifecycle.
    stop = lambda: stopped() or halt.is_set()
    def pause(seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            if stop():
                raise InterruptedError("Independent data service day or data stop ended")
            sleep(min(.2, max(0, until-time.monotonic())))
    store = ServiceStore(root / "preparation.sqlite3", object_root=root)
    # Candle/outcome evidence remains in the existing scientific store, while
    # heavy preparation and handoff transactions use their own databases.
    evidence = ServiceStore(Path(datastore) / "intraday/shadow.sqlite3", object_root=root)
    workers, errors = [], []
    with exclusive_runtime_lock(Path(datastore) / "locks/scout-data-service.lock", process_name="scout-data-service"):
        def status(state):
            atomic_json(root / "data-status.json", {"schema": "scout-data-service-v1", "status": state,
                "pc": pc, "pid": os.getpid(), "source_session": session, "symbols": list(symbols),
                "wake_at": wake.isoformat(), "deadline": deadline.isoformat(), "recipe_hash": recipe_hash,
                "release": {k:release[k] for k in ("release_root", "manifest_sha256")},
                "heartbeat_at": instant(clock()).isoformat(), "errors": errors})
        status("STARTING")
        while instant(clock()) < wake and not stop():
            pause(1)
        if stop():
            status("STOPPED")
            return {"status": "STOPPED", "source_session": session}
        load_dotenv(Path(repository) / ".env")
        histories = {}
        native = native_interval_symbols_for_runtime(runtime)
        for symbol in symbols:
            try:
                if symbol in native:
                    from .native_interval import load_native_interval_history
                    histories[symbol] = load_native_interval_history(datastore, symbol,
                        source_dataset=config.source_dataset, pc=pc,
                        recovery_contract=getattr(runtime["dataset_builder"], "historical_recovery_contract", None))
                else:
                    histories[symbol] = load_local_history(datastore, symbol, source_dataset=config.source_dataset)
            except Exception as exc:
                spool.record("history_unavailable", {"symbol": symbol, "error_type": type(exc).__name__})
        provider = prepare_minute_provider(runtime, datastore=datastore, pc=pc,
            symbols=symbols, source_dataset=config.source_dataset, clock=clock)
        journal = AcquisitionJournal(Path(datastore) / "intraday/acquisition.sqlite3")
        feed = QuarterHourMinuteFeed(provider=provider, store=evidence, journal=journal,
            histories=histories, pc=pc, symbols=symbols, session_id=session, clock=clock,
            publication_offset_seconds=config.publication_offset_seconds, native_interval_symbols=native,
            reference_fill_policy=getattr(runtime["dataset_builder"], "reference_fill_policy", None))
        collector = prepare_market_collection(runtime, datastore=datastore, pc=pc, symbols=symbols,
                                              clock=clock, stopped=stop, pause=pause)
        def background(name, action):
            def work():
                attempts = 0
                while not stop():
                    try:
                        action()
                        if name == "acquisition":
                            halt.set()  # Feed proves this one data day is complete.
                        return
                    except InterruptedError:
                        if stop():
                            return
                    except Exception as exc:
                        attempts += 1
                        detail = {"worker": name, "error_type": type(exc).__name__, "attempt": attempts}
                        errors.append(detail)
                        del errors[:-32]
                        try:
                            spool.record("data_worker_retry", detail)
                        except Exception:
                            pass
                    try:
                        pause(min(60, 2 ** min(attempts, 5)))
                    except InterruptedError:
                        return
            worker = threading.Thread(target=work, name="scout-1a-"+name, daemon=True)
            workers.append(worker)
            worker.start()
        background("acquisition", lambda: feed.run(stopped=stop, pause=pause))
        if collector is not None:
            background("context", lambda: collector.run(stopped=stop, pause=pause,
                on_retry=lambda detail: spool.record("context_retry", detail)))
        background("scoring", lambda: scoring_loop(evidence, histories=histories, provider=provider,
            journal=journal, session_id=session, stopped=stop, clock=clock, pause=pause, pc=pc,
            history_source=feed.scoring_history_source))
        runner = ShadowRunner(store=store, pc=pc, symbols=symbols, config=config, clock=clock,
                              stopped=stop, **runtime)
        pending, completed, retry = {}, set(), {}
        cursor = 0
        reference = None
        try:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="scout-1a-prepare") as pool:
                while not stop():
                    newest = latest_completed_reference(clock(), publication_offset_seconds=config.publication_offset_seconds)
                    if newest != reference:
                        reference = newest
                        completed.clear()
                        retry.clear()
                    for symbol, (ref, future, queued) in list(pending.items()):
                        if not future.done():
                            continue
                        del pending[symbol]
                        try:
                            key, _ = future.result()
                            identity = digest({"pc": pc, "symbol": symbol, "reference": ref.isoformat(),
                                               "recipe_hash": recipe_hash, "source_session": session})
                            envelope = store.envelope(identity, symbol)[1]
                            spool.publish(symbol=symbol, reference_at=ref, envelope=envelope, source_session=session)
                            metric(root, "data", "preparation", symbol=symbol, reference_at=ref.isoformat(),
                                   seconds=time.perf_counter()-queued, snapshot_hash=key,
                                   deadline_miss=instant(clock()) >= ref + 3 * STEP)
                            if ref == reference:
                                completed.add(symbol)
                        except Exception as exc:
                            retry[symbol] = instant(clock()) + timedelta(seconds=3)
                            spool.record("preparation_deferred", {"symbol": symbol, "reference_at": ref.isoformat(),
                                "error_type": type(exc).__name__, "reason": getattr(exc, "reason", None)})
                    if eligible_reference(reference) and instant(clock()) < reference + 3 * STEP:
                        # At most two in-flight preparations; scan the complete
                        # scope every turn. Missing acquisition frees its slot.
                        fair_scope = symbols[cursor:] + symbols[:cursor]
                        for symbol in fair_scope:
                            if len(pending) >= 2:
                                break
                            if symbol in pending or symbol in completed or retry.get(symbol, instant(clock())) > instant(clock()):
                                continue
                            if spool.ready(symbol, reference, now=clock()):
                                completed.add(symbol)
                                continue
                            identity = digest({"pc": pc, "symbol": symbol, "reference": reference.isoformat(),
                                               "recipe_hash": recipe_hash, "source_session": session})
                            incoming = feed.history_source(reference)
                            incoming.bind_cycle(identity)
                            queued = time.perf_counter()
                            future = pool.submit(runner._symbol, identity, symbol, lambda s=symbol,h=incoming: h.get(s),
                                reference, instant(clock()), "live", None, None, queued, prepare_only=True)
                            pending[symbol] = (reference, future, queued)
                            cursor = (symbols.index(symbol) + 1) % len(symbols)
                    status("RUNNING")
                    pause(.2)
        except (KeyboardInterrupt, InterruptedError):
            halt.set()
        finally:
            halt.set()
            for worker in workers:
                worker.join(timeout=120)
            if any(worker.is_alive() for worker in workers):
                errors.append({"worker": "drain", "error_type": "TimeoutError"})
            if getattr(feed, "closed", False) and not stopped():
                # The final native candle may arrive after trading's deadline.
                # Score retained targets without starting acquisition or models.
                from .live_session import score_saved_session_outcomes
                from .prediction import PACIFIC
                with evidence.connect() as db:
                    cycles = [json.loads(raw) for (raw,) in db.execute("SELECT payload FROM cycles")]
                sessions = {cycle["experiment"] for cycle in cycles
                            if cycle.get("pc") == pc and cycle.get("mode") == "live"
                            and instant(cycle["reference_at"]).astimezone(PACIFIC).date() == wake.astimezone(PACIFIC).date()}
                for trading_session in sessions:
                    try:
                        result = score_saved_session_outcomes(evidence, session_id=trading_session,
                            actor=pc, symbols=symbols, as_of=clock(), stopped=stopped)
                        spool.record("final_saved_outcomes", result)
                    except Exception as exc:
                        spool.record("final_scoring_deferred", {"session_id": trading_session,
                            "error_type": type(exc).__name__, "original_targets_retained": True})
            status("FAILED" if errors else "STOPPED")
        return {"status": "FAILED" if errors else "STOPPED", "source_session": session}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--datastore", type=Path, required=True)
    parser.add_argument("--selector", type=Path, required=True)
    parser.add_argument("--data-live", action="store_true", help="Run separately authorized data collection only")
    args = parser.parse_args(argv)
    from tools.scout_release import resolve_selection
    release = resolve_selection(args.selector, protocol="scout-independent-v1")
    if not args.data_live:
        print(encoded({"status": "INSTALLED", "release": release["manifest_sha256"], "trading_started": False}))
        return 0
    result = run_data_service(repository=args.repository, datastore=args.datastore, release=release)
    print(encoded(result))
    return 1 if result["status"] == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
