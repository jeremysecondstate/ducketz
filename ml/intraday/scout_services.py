"""Independent Scout 1A preparation and 1B model/trading service entrypoints.

Only the new manually activated controller can construct a trading role.
The separate data entrypoint has no broker, order or trading activation path.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path
import argparse
import hashlib
import json
import os
import threading
import time
import uuid

from .prediction import ContractError, PACIFIC, STEP, digest, encoded, instant
from .store import utcnow
from .service_handoff import ReadySpool, ServiceStore, full_snapshot
from .live_session import ExecutionQueue, _execution_store_busy
from .final_scoring import score_saved_session_outcomes
from .runtime import ModelPersistenceStopped

_METRIC_LOCK = threading.Lock()


def _data_worker_retry_detail(worker, error, attempt, *, pc, source_session, release, collector=None):
    """Bounded cause evidence only; diagnostic failure cannot stop a retry."""
    detail = {"worker": worker, "error_type": type(error).__name__, "attempt": attempt,
              "role": "data", "pc": pc, "source_session": source_session,
              "manifest_sha256": release["manifest_sha256"]}
    try:
        message = str(error)
        detail.update(message=message[:512], message_truncated=len(message) > 512)
    except Exception:
        detail["message_unavailable"] = True
    try:
        from .runtime import _symbol_exception_diagnostics
        diagnostics = _symbol_exception_diagnostics(error)
        # Keep the existing bounded locations/native categories, without the
        # optional dataset profile or any traceback locals, source or payload.
        detail["diagnostics"] = {key: diagnostics[key] for key in
            ("schema_version", "exception_chain", "chain_truncated")}
    except Exception:
        detail["diagnostics_unavailable"] = True
    if worker == "context" and collector is not None and "diagnostics" in detail:
        try:
            from .market_collection import collection_failure_diagnostics
            detail["diagnostics"]["market_collection"] = collection_failure_diagnostics(collector, error)
        except BaseException:
            pass  # Optional observations cannot replace the existing worker retry.
    return detail


def _write_retry_identity(pending, error):
    """Bounded correlation only; never duplicate private pending payloads."""
    return {"schema": "scout-native-write-retry-v1", "operation_id": uuid.uuid4().hex,
            "operation": pending["operation"], "pending_sha256": digest(pending),
            "pid": os.getpid(), "busy_count": 1,
            "sqlite_errorcode": getattr(error, "sqlite_errorcode", None),
            "sqlite_errorname": getattr(error, "sqlite_errorname", None)}


class ServiceExecutionQueue(ExecutionQueue):
    """Bounded pending forecast work; order recovery remains journal-owned."""
    def __init__(self, *args, **kwargs):
        self._broker_retry_floor = None
        self._pending_broker_wait = None
        self._broker_write_retry = None
        super().__init__(*args, **kwargs)

    def _broker_retry_receipt(self, phase, episode, **values):
        try:
            # The existing trading sink adds its actual generation/loaded release.
            self.event_sink({"event": "broker_wait_persistence_" + phase,
                             "logical_run_id": self.session_id, "role": "trading",
                             **episode, **values})
        except Exception:
            pass  # Diagnostic I/O must not retry an already committed write.

    def _persist_broker_wait(self):
        due = self._pending_broker_wait
        if due is None:
            return
        episode = self._broker_write_retry
        if episode is not None and episode["next_attempt_at"] != due:
            self._broker_write_retry = None
            self._broker_retry_receipt("superseded", episode, replacement_next_attempt_at=due)
            episode = None
        try:
            with self.store.connect() as db:
                db.execute("INSERT INTO intraday_live_retry VALUES('broker',?) ON CONFLICT(scope) "
                           "DO UPDATE SET next_attempt_at=max(next_attempt_at,excluded.next_attempt_at)", (due,))
        except Exception as exc:
            if _execution_store_busy(exc):
                try:
                    if episode is None:
                        episode = {**_write_retry_identity({"operation": "broker_retry_floor",
                            "scope": "broker", "next_attempt_at": due}, exc), "next_attempt_at": due}
                        self._broker_write_retry = episode
                        self._broker_retry_receipt("started", episode)
                    else:
                        episode["busy_count"] += 1
                except Exception:
                    pass  # Retain the original persistence exception and floor.
            raise
        # Clear pending state before diagnostics: sink failure cannot replay SQL.
        self._pending_broker_wait = None
        self._broker_write_retry = None
        if episode is not None:
            self._broker_retry_receipt("recovered", episode)

    def broker_wait(self, *, now, seconds):
        # Retain the original absolute provider floor even if its write fails.
        # Every effect and planned handoff passes broker_due before proceeding.
        due = instant(now) + timedelta(seconds=max(3.0, float(seconds)))
        self._broker_retry_floor = max(due, self._broker_retry_floor or due)
        self._pending_broker_wait = self._broker_retry_floor.isoformat()
        self._persist_broker_wait()

    def broker_due(self, now):
        self._persist_broker_wait()
        if self._broker_retry_floor is not None and instant(now) < self._broker_retry_floor:
            return False
        return super().broker_due(now)

    def defer(self, forecast_id, *, now, seconds, broker_wait=False):
        if broker_wait:
            # A failed forecast-row write cannot discard a provider retry floor.
            self.broker_wait(now=now, seconds=seconds)
        return super().defer(forecast_id, now=now, seconds=seconds, broker_wait=False)

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
    with _METRIC_LOCK, path.open("a", encoding="utf-8") as stream:
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


class _ModelFailurePersistenceStopped(ModelPersistenceStopped):
    """Authority ended while an exact model write remained pending."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__("Model failure persistence stopped: " + reason)


class _ModelServiceStore(ServiceStore):
    """Retry exact model evidence writes; never replay model work."""
    def __init__(self, *args, persistence_stop_reason, persistence_pause,
                 persistence_diagnostic_binding=None, **kwargs):
        self._persistence_stop_reason = persistence_stop_reason
        self._persistence_pause = persistence_pause
        self._persistence_diagnostic_binding = dict(persistence_diagnostic_binding or {})
        super().__init__(*args, **kwargs)

    def _failure_retry_receipt(self, phase, episode, pending):
        try:
            metric(self.object_root, "model", "failure_persistence_" + phase,
                   **self._persistence_diagnostic_binding, **episode,
                   cycle_id=pending["cycle_id"], symbol=pending.get("symbol"),
                   original_recorded_at=pending.get("recorded_at"))
        except Exception:
            pass  # Diagnostic I/O must not replay or mask the original write.

    def _failure_write(self, action, pending):
        episode = None
        while True:
            try:
                result = action()
            except Exception as exc:
                if not _execution_store_busy(exc):
                    raise
                try:
                    if episode is None:
                        episode = _write_retry_identity(pending, exc)
                        self._failure_retry_receipt("started", episode, pending)
                    else:
                        episode["busy_count"] += 1
                except Exception:
                    pass  # Diagnostics cannot change native busy retry behavior.
                reason = self._persistence_stop_reason()
                if reason is None:
                    self._persistence_pause(.2)
                    reason = self._persistence_stop_reason()
                if reason is not None:
                    try:
                        from .runtime import _symbol_exception_diagnostics
                        metric(self.object_root, "model", "failure_persistence_stopped",
                               logical_run_id=(self.run_binding or {}).get("logical_run_id"),
                               cycle_id=pending["cycle_id"], symbol=pending.get("symbol"),
                               stop_reason=reason, pending_write=pending,
                               diagnostics=_symbol_exception_diagnostics(exc))
                    except Exception:
                        pass  # Diagnostics cannot mask the authority-ended original write.
                    raise _ModelFailurePersistenceStopped(reason) from exc
            else:
                # The action, including its transaction exit, already succeeded.
                if episode is not None:
                    self._failure_retry_receipt("recovered", episode, pending)
                return result

    def begin_cycle(self, cycle_id, payload):
        # Admission precedes all symbol work. Retain this attempt's original
        # reference, configuration and row clock through native contention.
        frozen = json.loads(encoded(payload))
        return self._failure_write(lambda: super(_ModelServiceStore, self).begin_cycle(cycle_id, frozen),
            {"operation":"model_cycle_begin", "cycle_id":cycle_id, "payload":frozen})

    def event(self, cycle_id, kind, payload, *, symbol=None, recorded_at=None):
        failure_progress = kind == "cycle_progress" and "failures" in payload
        if kind not in ("symbol_failure", "cycle_started", "cycle_attempt_measured", "forecast_persisted", "models_completed") and not failure_progress:
            return super().event(cycle_id, kind, payload, symbol=symbol, recorded_at=recorded_at)
        # Freeze the original payload and row clock before the first attempt.
        frozen = json.loads(encoded(payload))
        recorded = instant(recorded_at or utcnow()).isoformat()
        # Admission and failure updates commit their row before this separate
        # event write. Retry the event here without replaying the committed row
        # operation or changing the original event clock. Post-work attempt
        # evidence likewise retains completed computation and publications.
        operation = {"symbol_failure": "symbol_failure_event",
                     "cycle_started": "model_cycle_started_event",
                     "cycle_attempt_measured": "model_cycle_attempt_event",
                     "forecast_persisted": "model_forecast_persisted_event",
                      "models_completed": "model_models_completed_event",
                     "cycle_progress": "model_failure_progress_event"}[kind]
        pending = {"operation":operation, "cycle_id":cycle_id, "symbol":symbol,
                   "kind":kind, "recorded_at":recorded, "payload":frozen}
        return self._failure_write(lambda: super(_ModelServiceStore, self).event(
            cycle_id, kind, frozen, symbol=symbol, recorded_at=recorded), pending)

    def update_cycle(self, cycle_id, changes):
        if "failures" not in changes:
            return super().update_cycle(cycle_id, changes)
        frozen = json.loads(encoded(changes))
        return self._failure_write(lambda: super(_ModelServiceStore, self).update_cycle(cycle_id, frozen),
            {"operation":"model_failure_cycle_update", "cycle_id":cycle_id, "changes":frozen})


def run_model_service(*, repository, datastore, release, lifecycle, clock=utcnow, sleep=time.sleep):
    from .runtime import ShadowRunner
    from .live_session import ExecutionQueue, latest_completed_reference, eligible_reference, _session_cycles
    from .iteration_boundary import inspect_cycle
    pc, symbols, config, runtime, recipe_hash, recipe_identity = _configuration(repository, datastore, release, workers=2)
    service_root = Path(datastore) / "intraday/services"
    store = _ModelServiceStore(Path(datastore) / "intraday/shadow.sqlite3", object_root=service_root,
        persistence_stop_reason=lifecycle.stop_reason, persistence_pause=sleep,
        persistence_diagnostic_binding={"logical_run_id": lifecycle.session_id,
            "generation": getattr(lifecycle, "generation", None),
            "manifest_sha256": getattr(lifecycle, "release", release).get("manifest_sha256")})
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
    def retry_ready(symbol, error, reference):
        if error.get("reason") != "system_1a_snapshot_pending":
            return True
        import sqlite3
        try:
            return spool.ready(symbol, reference, now=clock()) is not None
        except ContractError:
            # Let the ordinary symbol path record the corrupt input as a
            # durable failure. Readiness inspection cannot skip validation.
            return True
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            return False
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
                             if error.get("retryable") and instant(error["retry_at"]) <= instant(clock())
                             and retry_ready(symbol, error, reference)]
            if saved.get("status") == "completed" or not retry_symbols:
                pause(1)
                continue
        began = time.perf_counter()
        try:
            result = runner.run_cycle(PreparedHistories(spool, store, reference, clock=clock),
                reference_at=reference, mode="live", experiment=lifecycle.session_id,
                on_published=queue.enqueue, retry_symbols=retry_symbols,
                acquisition={"kind": "independent_verified_ready_snapshot"})
        except _ModelFailurePersistenceStopped as exc:
            # run_cycle drained its owned pool. No full-scope completion or
            # checkpoint is claimed; the metric retains the original pending write.
            return {"status":"STOPPED", "stop_reason":exc.reason,
                    "failure_persistence_pending":True}
        metric(service_root, "model", "model_work", seconds=time.perf_counter()-began,
               cycle_id=result["cycle_id"], reference_at=reference.isoformat())
        if retry_symbols is None:
            on_iteration(result)
        else:
            # A subset retry may update durable evidence, but is never another
            # full-scope attempt or an authority to replace this worker.
            metric(service_root, "model", "subset_retry", cycle_id=result["cycle_id"],
                   symbols=retry_symbols, status=result["status"])
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
    started = instant(clock())
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
    wake = manual_wake_at(started)
    deadline = automatic_stop_deadline(wake)
    data_day = wake.astimezone(PACIFIC).date()
    start_reference = max(wake, latest_completed_reference(started,
        publication_offset_seconds=config.publication_offset_seconds))
    after_close = started >= deadline
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
        recovery_spooled = []
        def record(kind, payload):
            import sqlite3
            try:
                spool.record(kind, payload)
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                # The same busy database cannot durably record its own retry.
                # Keep an independent fsynced atomic record, without waiting
                # for a consumer or hiding the original work/error identity.
                value = {"schema":"scout-data-recovery-v1", "kind":kind, "payload":payload,
                         "recorded_at":instant(clock()).isoformat(), "database_error":str(exc)}
                name = digest(value) + ".json"
                atomic_json(root / "recovery" / name, value)
                recovery_spooled.append(name)
                del recovery_spooled[:-32]
        progress = {"stage": "initializing", "history_complete": [], "history_failed": [], "history_pending": list(symbols)}
        def status(state):
            atomic_json(root / "data-status.json", {"schema": "scout-data-service-v1", "status": state,
                "pc": pc, "pid": os.getpid(), "source_session": session, "symbols": list(symbols),
                "wake_at": wake.isoformat(), "deadline": deadline.isoformat(), "recipe_hash": recipe_hash,
                "release": {k:release[k] for k in ("release_root", "manifest_sha256")},
                "heartbeat_at": instant(clock()).isoformat(), "errors": errors,
                "startup_progress": progress, "data_day": str(data_day), "after_close_recovery": after_close,
                "recovery_spooled": list(recovery_spooled)})
        status("STARTING")
        while instant(clock()) < wake and not stop():
            pause(1)
        if stop():
            status("STOPPED")
            return {"status": "STOPPED", "source_session": session}
        load_dotenv(Path(repository) / ".env")
        histories = {}
        native = native_interval_symbols_for_runtime(runtime)
        def load_history(symbol):
            begun = time.perf_counter()
            try:
                if stop():
                    raise InterruptedError("Data service stopped before history initialization")
                if symbol in native:
                    from .native_interval import load_native_interval_history
                    return load_native_interval_history(datastore, symbol,
                        source_dataset=config.source_dataset, pc=pc,
                        recovery_contract=getattr(runtime["dataset_builder"], "historical_recovery_contract", None))
                return load_local_history(datastore, symbol, source_dataset=config.source_dataset)
            finally:
                metric(root, "data", "cold_history", symbol=symbol, seconds=time.perf_counter()-begun)
        progress["stage"] = "history_loading" if not after_close else "saved_day_recovery"
        status("STARTING")
        if not after_close:
            # Keep every original history validator and population. Bound the
            # independent file reads without serializing the entire scope.
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="scout-1a-history") as pool:
                incoming = {pool.submit(load_history, symbol): symbol for symbol in symbols}
                while incoming:
                    finished, _ = wait(incoming, timeout=1, return_when=FIRST_COMPLETED)
                    for future in finished:
                        symbol = incoming.pop(future)
                        try:
                            histories[symbol] = future.result()
                            progress["history_complete"].append(symbol)
                        except Exception as exc:
                            progress["history_failed"].append(symbol)
                            record("history_unavailable", {"symbol": symbol, "error_type": type(exc).__name__})
                        progress["history_pending"].remove(symbol)
                    progress["history_active"] = [symbol for future, symbol in incoming.items() if future.running()]
                    status("STARTING")
        else:
            # No models or causal preparation remain eligible after this day's
            # deadline. The feed still validates saved originals and resumes
            # exact missing native windows; a completion label skips nothing.
            progress["history_pending"] = []
        if stop():
            status("STOPPED")
            return {"status": "STOPPED", "source_session": session}
        progress["stage"] = "native_feed_initializing"
        status("STARTING")
        provider = prepare_minute_provider(runtime, datastore=datastore, pc=pc,
            symbols=symbols, source_dataset=config.source_dataset, clock=clock)
        journal = AcquisitionJournal(Path(datastore) / "intraday/acquisition.sqlite3")
        try:
            feed = QuarterHourMinuteFeed(provider=provider, store=evidence, journal=journal,
                histories=histories, pc=pc, symbols=symbols, session_id=session, clock=clock,
                publication_offset_seconds=config.publication_offset_seconds, native_interval_symbols=native,
                start_reference=start_reference,
                reference_fill_policy=getattr(runtime["dataset_builder"], "reference_fill_policy", None),
                event_persistence_stopped=stop, event_persistence_pause=pause,
                on_event_persistence_retry=lambda phase, detail: record("minute_event_persistence_" + phase,
                    {**detail, "source_session": session, "manifest_sha256": release["manifest_sha256"], "role": "data"}))
        except InterruptedError:
            if not stop():
                raise
            # Constructor event persistence starts before any owned worker.
            status("STOPPED")
            return {"status": "STOPPED", "source_session": session}
        collector = (None if after_close else prepare_market_collection(runtime, datastore=datastore, pc=pc, symbols=symbols,
                                              clock=clock, stopped=stop, pause=pause))
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
                        detail = _data_worker_retry_detail(name, exc, attempts, pc=pc,
                            source_session=session, release=release,
                            collector=collector if name == "context" else None)
                        errors.append(detail)
                        del errors[:-32]
                        try:
                            record("data_worker_retry", detail)
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
                on_retry=lambda detail: record("context_retry", detail)))
        if not after_close:
            background("scoring", lambda: scoring_loop(evidence, histories=histories, provider=provider,
                journal=journal, session_id=session, stopped=stop, clock=clock, pause=pause, pc=pc,
                history_source=feed.scoring_history_source,
                on_persistence_retry=lambda phase, detail: record("scoring_persistence_" + phase, detail)))
        runner = ShadowRunner(store=store, pc=pc, symbols=symbols, config=config, clock=clock,
                              stopped=stop, **runtime)
        pending, completed, retry, retry_attempts = {}, set(), {}, {}
        progress["stage"] = "active"
        cursor = 0
        reference = None
        def defer(symbol, ref, exc):
            attempts = retry_attempts.get(symbol, 0) + 1 if ref == reference else 1
            delay = max(float(getattr(exc, "retry_after_seconds", 0)), min(60, 3 * 2 ** min(attempts-1, 5)))
            due = instant(clock()) + timedelta(seconds=delay)
            if ref == reference:
                retry_attempts[symbol] = attempts
                retry[symbol] = due
            record("preparation_deferred", {"symbol": symbol, "reference_at": ref.isoformat(),
                "error_type": type(exc).__name__, "reason": getattr(exc, "reason", None),
                "attempt": attempts, "retry_after_seconds": delay, "retry_at": due.isoformat()})
        try:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="scout-1a-prepare") as pool:
                while not stop():
                    newest = latest_completed_reference(clock(), publication_offset_seconds=config.publication_offset_seconds)
                    if newest != reference:
                        reference = newest
                        completed.clear()
                        retry.clear()
                        retry_attempts.clear()
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
                            defer(symbol, ref, exc)
                    if (not after_close and reference.astimezone(PACIFIC).date() == data_day
                            and eligible_reference(reference) and instant(clock()) < reference + 3 * STEP):
                        # At most two in-flight preparations; scan the complete
                        # scope every turn. Missing acquisition frees its slot.
                        fair_scope = symbols[cursor:] + symbols[:cursor]
                        for symbol in fair_scope:
                            if len(pending) >= 2:
                                break
                            if symbol in pending or symbol in completed or retry.get(symbol, instant(clock())) > instant(clock()):
                                continue
                            try:
                                if spool.ready(symbol, reference, now=clock()):
                                    completed.add(symbol)
                                    continue
                            except Exception as exc:
                                # A busy handoff is a durable symbol deferral,
                                # never a reason to stop independent acquisition.
                                defer(symbol, reference, exc)
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
                with evidence.connect() as db:
                    cycles = [json.loads(raw) for (raw,) in db.execute("SELECT payload FROM cycles")]
                sessions = {cycle["experiment"] for cycle in cycles
                            if cycle.get("pc") == pc and cycle.get("mode") == "live"
                            and instant(cycle["reference_at"]).astimezone(PACIFIC).date() == wake.astimezone(PACIFIC).date()}
                for trading_session in sessions:
                    try:
                        result = score_saved_session_outcomes(evidence, session_id=trading_session,
                            actor=pc, symbols=symbols, as_of=clock(), stopped=stopped)
                        record("final_saved_outcomes", result)
                    except Exception as exc:
                        errors.append({"worker": "final_scoring", "error_type": type(exc).__name__,
                                       "session_id": trading_session})
                        record("final_scoring_deferred", {"session_id": trading_session,
                            "error_type": type(exc).__name__, "original_targets_retained": True})
            record("data_day_outcome", {"source_session": session, "data_day": str(data_day),
                "native_feed_closed": bool(getattr(feed, "closed", False)),
                "owned_workers_drained": not any(worker.is_alive() for worker in workers),
                "pending_preparations": [{"symbol": symbol, "reference_at": ref.isoformat()}
                    for symbol, (ref, _, _) in pending.items()], "errors": list(errors)})
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
