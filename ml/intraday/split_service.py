"""Atlas System 1A: independent acquisition and immutable model-input publication.

This entrypoint has no execution adapter and never reads manual trading controls.
It is owned by the existing data schedule, outside the manual trader's process
lifetime. Consumer acknowledgement is neither required nor consulted.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from datafetching.runtime_lock import exclusive_runtime_lock
from .prediction import ContractError, PACIFIC, STEP, digest, encoded, instant
from .runtime import ResearchConfig, ShadowRunner, LiveHistories, local_symbols, with_local_data_policy
from .store import ShadowStore, utcnow
from .split_handoff import ReadySnapshots

VERSION = "intraday-system-1a-service-v1"


def data_window_close(now, previous_close, *, daily_data_wake=None, data_weekdays=()):
    """Admit a later data window only at an explicitly bound local wake.

    The binding comes from this PC's existing scheduler. Retained catch-up
    never authorizes an extra overnight or weekend acquisition window.
    """
    previous = instant(previous_close)
    weekdays = tuple(data_weekdays)
    if daily_data_wake is None and not weekdays:
        return previous
    if (not isinstance(daily_data_wake, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", daily_data_wake)
            or not weekdays or len(set(weekdays)) != len(weekdays)
            or any(type(day) is not int or not 0 <= day <= 6 for day in weekdays)):
        raise ContractError("Explicit local daily data wake and weekdays must be supplied together")
    local = instant(now).astimezone(PACIFIC)
    hour, minute = (int(part) for part in daily_data_wake.split(":"))
    if (local.date() > previous.astimezone(PACIFIC).date() and local.weekday() in weekdays
            and (local.hour, local.minute) >= (hour, minute)):
        return instant(local.replace(hour=17, minute=0, second=0, microsecond=0))
    return previous


def _ensure_collection_thread(current, *, once, now, close_at, finished, start):
    """Recover a data-only collector that exits around a window rollover."""
    if once or instant(now) >= instant(close_at) and finished.is_set():
        return current
    if current is None or not current.is_alive():
        # An old window can finish just after the rollover checked is_alive.
        # Its flag cannot complete the newly admitted active collection window.
        finished.clear()
        return start()
    return current


class DataService:
    """One bounded current-reference preparation pass, injectable offline."""
    def __init__(self, *, runner, handoff, incoming, session_id, clock=utcnow, assigned_symbols=None):
        self.runner, self.handoff, self.incoming = runner, handoff, incoming
        self.session_id, self.clock = session_id, clock
        self.assigned_symbols = tuple(runner.symbols if assigned_symbols is None else assigned_symbols)
        if (not self.assigned_symbols or len(set(self.assigned_symbols)) != len(self.assigned_symbols)
                or not set(self.assigned_symbols) <= set(runner.symbols)):
            raise ContractError("Data lane changed the original local symbol ownership")

    def pass_once(self, *, reference_at=None):
        from .live_session import eligible_reference, latest_completed_reference
        reference = (instant(reference_at) if reference_at is not None else
            latest_completed_reference(self.clock(), publication_offset_seconds=self.runner.config.publication_offset_seconds))
        if not eligible_reference(reference):
            return {"status": "OUTSIDE_DEFINED_REFERENCE", "reference_at": reference.isoformat()}
        configuration_hash = digest(asdict(self.runner.config))
        experiment = "system-1a:" + self.runner.pc.lower() + ":" + reference.astimezone(PACIFIC).date().isoformat()
        cycle_id = digest(dict(pc=self.runner.pc, reference_at=reference.isoformat(),
            configuration_hash=configuration_hash, mode="live", experiment=experiment))
        ready_symbols = {row["symbol"] for row in self.handoff.ready(self.clock(), configuration_hash=configuration_hash)
                         if row["cycle_id"] == cycle_id}
        pending = [symbol for symbol in self.assigned_symbols if symbol not in ready_symbols]
        if not pending:
            return {"status": "prepared", "reference_at": reference.isoformat(), "cycle_id": cycle_id,
                    "prepared_symbols": list(self.assigned_symbols), "reused_ready_index": True}
        with self.runner.store.connect() as db:
            old = db.execute("SELECT payload FROM cycles WHERE id=?", (cycle_id,)).fetchone()
            frozen_symbols = {row[0] for row in db.execute("SELECT symbol FROM symbol_inputs WHERE cycle_id=?", (cycle_id,))}
        if old:
            cycle = json.loads(old[0])
            failures = cycle.get("failures", {})
            pending = [symbol for symbol in pending if symbol in frozen_symbols or symbol not in failures
                       or (failures[symbol].get("retryable") and
                           instant(failures[symbol]["retry_at"]) <= instant(self.clock()))]
            if not pending:
                return {"status": "WAITING_FOR_DATA", "reference_at": reference.isoformat(),
                        "cycle_id": cycle_id, "prepared_symbols": sorted(ready_symbols)}
        began = time.perf_counter()
        published = []

        def ready(cycle_id, symbol, cycle, frozen):
            try:
                record = self.handoff.publish(cycle_id, symbol, cycle, frozen)
            except sqlite3.OperationalError as exc:
                code = getattr(exc, "sqlite_errorcode", None)
                if type(code) is not int or code & 255 not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise
                from .data import AcquisitionPending
                raise AcquisitionPending("ready_publication_storage_contention", retry_after_seconds=1) from exc
            published.append(record)
            try:
                self.runner.store.event(cycle_id, "system_1a_snapshot_ready", {
                    "record_sha256": record["record_sha256"], "ready_at": record.get("ready_at"),
                    "handoff_measurements": dict(self.handoff.measurements)}, symbol=symbol)
            except Exception:
                pass  # A diagnostic write cannot revoke an immutable ready publication.

        result = self.runner.prepare_cycle(self.incoming(reference), reference_at=reference,
            mode="live", experiment=experiment, on_prepared=ready, retry_symbols=pending,
            acquisition={"kind": "independent_system_1a_exact_interval",
                         "original_target_close": (reference + 3 * STEP).isoformat()})
        ready_symbols.update(row["symbol"] for row in published)
        evidence = {"status": "prepared" if set(self.assigned_symbols) <= ready_symbols else result["status"], "reference_at": reference.isoformat(),
            "cycle_id": result["cycle_id"], "prepared_symbols": [row["symbol"] for row in published],
            "preparation_pass_seconds": time.perf_counter() - began,
            "deadline_missed": instant(self.clock()) >= reference + 3 * STEP,
            "consumer_acknowledgement_required": False}
        self.runner.store.event(self.session_id, "system_1a_preparation_pass", evidence)
        return evidence


class PreparationScheduler:
    """Bounded independent symbol preparation; no cross-quarter barrier.

    The existing full-scope policy is frozen once before lane admission. Each
    symbol has its own store and incoming-history closure; its old work cannot
    change a different symbol's reference or block that symbol's next quarter.
    """
    def __init__(self, *, runner, handoff, service_factory, session_id, clock=utcnow):
        self.runner, self.handoff, self.service_factory = runner, handoff, service_factory
        self.session_id, self.clock = session_id, clock
        self.pool = ThreadPoolExecutor(max_workers=runner.config.symbol_workers,
                                       thread_name_prefix="system-1a-preparation")
        self.services, self.running = {}, {}
        self.last_symbol = None

    def poll(self):
        completed = []
        for symbol, (reference, future) in list(self.running.items()):
            if not future.done():
                continue
            del self.running[symbol]
            try:
                completed.append(future.result())
            except Exception as exc:
                self.runner.store.event(self.session_id, "system_1a_symbol_preparation_error", {
                    "reference_at": reference.isoformat(), "error_type": type(exc).__name__,
                    "other_symbols_continue": True}, symbol=symbol)
        return completed

    def _envelope(self, reference, cycle_id, experiment):
        with self.runner.store.connect() as db:
            row = db.execute("SELECT payload FROM cycles WHERE id=?", (cycle_id,)).fetchone()
        if row is None:
            # An empty explicit subset performs only the existing policy/cycle
            # freeze. It invokes no acquisition, preparation or training. Never
            # run this on an existing cycle or overwrite its progress summary.
            self.runner.prepare_cycle({}, reference_at=reference, mode="live", experiment=experiment,
                retry_symbols=[], acquisition={"kind": "independent_system_1a_exact_interval",
                    "original_target_close": (reference + 3 * STEP).isoformat()})
            with self.runner.store.connect() as db:
                row = db.execute("SELECT payload FROM cycles WHERE id=?", (cycle_id,)).fetchone()
        return json.loads(row[0])

    def _prepare(self, symbol, reference, cycle_id, envelope):
        service = self.services[symbol]
        service.runner.store.begin_cycle(cycle_id, envelope)
        # Preserve a snapshot frozen by the earlier whole-scope installation
        # even if its publication was interrupted before this generation.
        frozen = self.runner.store.inputs(cycle_id, symbol)
        if frozen is not None:
            retained = service.runner.store.freeze_inputs(cycle_id, symbol, frozen[1])
            if retained[0] != frozen[0]:
                raise ContractError("Data lane already owns different original inputs")
        return service.pass_once(reference_at=reference)

    def pass_once(self):
        from .live_session import eligible_reference, latest_completed_reference
        completed = self.poll()
        reference = latest_completed_reference(self.clock(),
            publication_offset_seconds=self.runner.config.publication_offset_seconds)
        if not eligible_reference(reference):
            return {"status": "OUTSIDE_DEFINED_REFERENCE", "reference_at": reference.isoformat()}
        configuration_hash = digest(asdict(self.runner.config))
        experiment = "system-1a:" + self.runner.pc.lower() + ":" + reference.astimezone(PACIFIC).date().isoformat()
        cycle_id = digest(dict(pc=self.runner.pc, reference_at=reference.isoformat(),
            configuration_hash=configuration_hash, mode="live", experiment=experiment))
        ready = {row["symbol"] for row in self.handoff.ready(self.clock(), configuration_hash=configuration_hash)
                 if row["cycle_id"] == cycle_id}
        symbols = list(self.runner.symbols)
        start = symbols.index(self.last_symbol) + 1 if self.last_symbol in symbols else 0
        ordered = symbols[start:] + symbols[:start]
        envelope = None
        for symbol in ordered:
            if len(self.running) >= self.runner.config.symbol_workers:
                break  # No hidden executor backlog; one in-flight pass per symbol.
            if symbol in ready or symbol in self.running:
                continue
            if envelope is None:
                envelope = self._envelope(reference, cycle_id, experiment)
            if symbol not in self.services:
                self.services[symbol] = self.service_factory(symbol)
            self.running[symbol] = (reference, self.pool.submit(self._prepare, symbol, reference, cycle_id, envelope))
            self.last_symbol = symbol
        return {"status": "prepared" if set(symbols) <= ready else "PREPARING",
            "reference_at": reference.isoformat(), "cycle_id": cycle_id,
            "prepared_symbols": [symbol for symbol in symbols if symbol in ready],
            "in_flight_symbols": list(self.running), "completed_passes": completed,
            "consumer_acknowledgement_required": False}

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)


def _status(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run_data_service(*, repository, datastore, profile, watchlist, config,
                     once=False, clock=utcnow, pause=time.sleep,
                     daily_data_wake=None, data_weekdays=()):
    """Operate only under the existing local data-service launch authority.

    Construction is delayed until invocation. Offline verification injects the
    small DataService interface instead of calling this provider entrypoint.
    """
    from dotenv import load_dotenv
    from app.services.schwab_token_store import configure_local_repository
    from app.services.databento_market_data import DatabentoMarketDataProvider
    from .data import AcquisitionJournal
    from .live_session import _renewal_histories, latest_completed_reference
    from .candle_collection import CandleCollector, market_close_at

    repository, root = Path(repository), Path(datastore)
    actor, symbols = local_symbols(profile, watchlist, root)
    if actor != "Atlas":
        raise ContractError("This data-service installation is owned by Atlas")
    config = with_local_data_policy(config, root).validate()
    if config.source_dataset != "EQUS.MINI":
        raise ContractError("Existing Atlas single-source provider binding required")
    base = root / "intraday/system-1a"
    session_id = "data-atlas-1a:" + uuid.uuid4().hex
    started = instant(clock())
    data_close_at = market_close_at(started)
    data_window_close(started, data_close_at, daily_data_wake=daily_data_wake, data_weekdays=data_weekdays)
    status_path = base / "status.json"
    shutdown = threading.Event()  # Local process shutdown, never a trader stop.
    collection_finished = threading.Event()
    with exclusive_runtime_lock(root / "locks/intraday-system-1a.lock", process_name="intraday-system-1a"):
        load_dotenv(repository / ".env", override=False)
        configure_local_repository(repository)
        os.environ["DUCKETS_PRODUCTION_WATCHLIST"] = str(watchlist)
        store = ShadowStore(base / "preparation.sqlite3")
        collection_store = ShadowStore(base / "collection.sqlite3")
        with collection_store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS system_1a_collection_days("
                "identity TEXT PRIMARY KEY,close_at TEXT NOT NULL,configuration TEXT NOT NULL,symbols TEXT NOT NULL,"
                "status TEXT NOT NULL,last_attempt_at TEXT NOT NULL)")
        handoff = ReadySnapshots(base / "ready", actor=actor, symbols=symbols, clock=clock)
        provider = DatabentoMarketDataProvider()
        if provider.dataset != config.source_dataset:
            raise ContractError("Data provider differs from Atlas source binding")
        journal = AcquisitionJournal(base / "acquisition.sqlite3")
        session = {"session_id": session_id, "symbols": list(symbols), "started_at": started.isoformat()}
        histories = _renewal_histories(root, collection_store, session, config)
        runner = ShadowRunner(store=store, pc=actor, symbols=symbols, config=config, clock=clock,
            datastore=root, stopped=shutdown.is_set,
            selected_config_path=root / "intraday/model-supervision/active.json")
        def incoming_for_lane():
            preparation_histories, preparation_reference = None, None
            def incoming(reference):
                nonlocal preparation_histories, preparation_reference
                if preparation_reference != reference:
                    # Each lane owns this closure. A later reference in another
                    # lane cannot replace histories still used by old work.
                    preparation_reference = reference
                    preparation_histories = _renewal_histories(root, collection_store,
                        {**session, "started_at": instant(clock()).isoformat()}, config)
                preparation_histories.missing.clear()
                return LiveHistories(histories=preparation_histories, provider=provider, store=collection_store,
                    journal=journal, reference_at=reference, clock=clock, sleep=pause, bounded=True,
                    stopped=shutdown.is_set, reuse_saved=True, population_contract=config.population_contract,
                    restore_eagerly=False, datastore=root)
            return incoming
        def service_for_symbol(symbol):
            lane_runner = ShadowRunner(store=ShadowStore(base / ("preparation-" + symbol + ".sqlite3")),
                pc=actor, symbols=symbols, config=config, clock=clock, datastore=root,
                stopped=shutdown.is_set, selected_config_path=root / "intraday/model-supervision/active.json")
            return DataService(runner=lane_runner, handoff=handoff, incoming=incoming_for_lane(),
                               session_id=session_id, clock=clock, assigned_symbols=(symbol,))
        scheduler = None if once else PreparationScheduler(runner=runner, handoff=handoff,
            service_factory=service_for_symbol, session_id=session_id, clock=clock)
        service = scheduler or DataService(runner=runner, handoff=handoff, incoming=incoming_for_lane(),
                                           session_id=session_id, clock=clock)
        threads, market_collector = [], None
        context_window, candle_thread = None, None
        lifecycle = {"schema_version": VERSION, "actor": actor, "session_id": session_id,
            "started_at": started.isoformat(), "pid": os.getpid(), "symbols": list(symbols),
            "new_preparation_close_at": data_close_at.isoformat(),
            "daily_data_wake": daily_data_wake, "data_weekdays": list(data_weekdays),
            "configuration": asdict(config), "trading_authority": False,
            "manual_trader_stop_dependency": False, "consumer_acknowledgement_required": False}
        module_path = Path(__file__).resolve()
        lifecycle["loaded_source_generation"] = (module_path.parents[3].name
            if module_path.parents[2].name == "source" and len(module_path.parents[3].name) == 64 else None)

        def event(kind, evidence):
            # Acquisition diagnostics share only the small collection DB, never
            # a writer transaction containing a frozen training matrix.
            try:
                collection_store.event(session_id, kind, evidence)
            except Exception as exc:
                print(encoded({"event": "system_1a_diagnostic_unavailable", "kind": kind,
                               "error_type": type(exc).__name__, "at": instant(clock()).isoformat()}), flush=True)

        def collect_candles():
            turn = 0
            while not shutdown.is_set():
                now = instant(clock())
                try:
                    close = data_close_at
                    day = "system-1a:" + actor.lower() + ":" + close.astimezone(PACIFIC).date().isoformat() + ":collection"
                    with collection_store.connect() as db:
                        db.execute("INSERT OR IGNORE INTO system_1a_collection_days VALUES(?,?,?,?,?,?)",
                            (day, close.isoformat(), encoded(asdict(config)), encoded(list(symbols)), "pending", now.isoformat()))
                        current = db.execute("SELECT * FROM system_1a_collection_days WHERE identity=?", (day,)).fetchone()
                        old = db.execute("SELECT * FROM system_1a_collection_days WHERE identity!=? AND status='pending' "
                                         "ORDER BY last_attempt_at,close_at LIMIT 1", (day,)).fetchone()
                        selected = old if old is not None and (current[4] == "complete" or turn % 3 == 2) else current
                        db.execute("UPDATE system_1a_collection_days SET last_attempt_at=? WHERE identity=?",
                                   (now.isoformat(), selected[0]))
                    turn += 1
                    selected_config = ResearchConfig(**json.loads(selected[2])).validate()
                    selected_symbols = tuple(json.loads(selected[3]))
                    if selected_config.source_dataset != provider.dataset:
                        raise ContractError("Persisted collection source differs from its original provider")
                    selected_histories = (histories if selected[0] == day else
                        _renewal_histories(root, collection_store,
                            {"session_id": selected[0], "symbols": list(selected_symbols), "started_at": selected[1]}, selected_config))
                    collector = CandleCollector(collection_store, histories=selected_histories, provider=provider,
                        journal=journal, symbols=selected_symbols, session_id=selected[0],
                        started_at=instant(selected[1]), close_at=instant(selected[1]), clock=clock,
                        publication_offset_seconds=selected_config.publication_offset_seconds,
                        stopped=shutdown.is_set, population_contract=selected_config.population_contract,
                        pause=pause, datastore=root, continuous=True, max_steps=1)
                    collector.pass_once()
                    with collection_store.connect() as db:
                        pending = db.execute("SELECT COUNT(*) FROM intraday_candle_collection WHERE session_id=? AND status='pending'",
                                             (selected[0],)).fetchone()[0]
                        if now >= instant(selected[1]) and not pending:
                            db.execute("UPDATE system_1a_collection_days SET status='complete' WHERE identity=?", (selected[0],))
                        remaining = db.execute("SELECT COUNT(*) FROM system_1a_collection_days WHERE status='pending'").fetchone()[0]
                        if now >= data_close_at and not remaining:
                            collection_finished.set()
                            return
                except Exception as exc:
                    event("system_1a_collection_error", {"error_type": type(exc).__name__})
                shutdown.wait(1)

        def ensure_context_window():
            nonlocal market_collector, context_window
            if (once or not config.market_inputs_enabled or instant(clock()) >= data_close_at
                    or context_window == data_close_at):
                return
            context_window = data_close_at
            try:
                from .market_inputs import MarketInputCollector
                from .market_input_worker import run_market_input_worker
                market_collector = MarketInputCollector(root, symbols)
                event("system_1a_market_inputs_started", market_collector.start())
                thread = threading.Thread(target=run_market_input_worker,
                    kwargs={"collector": market_collector, "clock": clock,
                        "stopped": lambda close=context_window: shutdown.is_set() or instant(clock()) >= close,
                        "pause": pause, "reference_for": lambda now: latest_completed_reference(now,
                            publication_offset_seconds=config.publication_offset_seconds), "event": event},
                    name="system-1a-market-inputs", daemon=True)
                thread.start()
                threads.append(thread)
            except Exception as exc:
                event("system_1a_market_inputs_error", {"error_type": type(exc).__name__,
                                                       "core_preparation_continues": True})

        def start_candle_thread():
            thread = threading.Thread(target=collect_candles, name="system-1a-candles", daemon=True)
            thread.start()
            threads.append(thread)
            return thread

        try:
            if not once:
                candle_thread = start_candle_thread()
                ensure_context_window()
            while True:
                try:
                    next_close = data_window_close(clock(), data_close_at,
                        daily_data_wake=daily_data_wake, data_weekdays=data_weekdays)
                    if next_close != data_close_at:
                        original_close = data_close_at
                        if market_collector is not None:
                            market_collector.close()
                            market_collector = None
                        data_close_at = next_close
                        collection_finished.clear()
                        lifecycle["new_preparation_close_at"] = data_close_at.isoformat()
                        histories = _renewal_histories(root, collection_store,
                            {**session, "started_at": instant(clock()).isoformat()}, config)
                        event("system_1a_scheduled_data_window_resumed", {
                            "previous_close_at": original_close.isoformat(), "close_at": data_close_at.isoformat(),
                            "admitted_at": instant(clock()).isoformat(), "daily_data_wake": daily_data_wake,
                            "data_weekdays": list(data_weekdays), "retained_pending_days": True})
                        ensure_context_window()
                    candle_thread = _ensure_collection_thread(candle_thread, once=once,
                        now=clock(), close_at=data_close_at, finished=collection_finished, start=start_candle_thread)
                    if instant(clock()) < data_close_at:
                        result = service.pass_once()
                    else:
                        # The original daily data binding ends new acquisition
                        # and preparation at close. Retained exact collection
                        # continues under its original authority until complete.
                        if market_collector is not None:
                            market_collector.close()
                            market_collector = None
                        result = {"status": "FINAL_COLLECTION_CATCHUP",
                                  "final_collection_complete": collection_finished.is_set()}
                        if scheduler is not None:
                            result["completed_preparation_passes"] = scheduler.poll()
                    _status(status_path, {**lifecycle, "status": "RUNNING", "heartbeat_at": instant(clock()).isoformat(),
                                          "latest_pass": result})
                except Exception as exc:
                    failure = {"error_type": type(exc).__name__, "recorded_at": instant(clock()).isoformat()}
                    event("system_1a_preparation_error", failure)
                    _status(status_path, {**lifecycle, "status": "RECOVERING", "heartbeat_at": instant(clock()).isoformat(),
                                          "last_error": failure})
                    if once:
                        raise
                if once:
                    return result
                if (instant(clock()) >= data_close_at and collection_finished.is_set()
                        and (scheduler is None or not scheduler.running)):
                    return {"status": "COMPLETED", "final_collection_complete": True}
                pause(3)
        finally:
            shutdown.set()
            if scheduler is not None:
                scheduler.close()
            for thread in threads:
                thread.join(timeout=2)
            if market_collector is not None:
                market_collector.close()
            _status(status_path, {**lifecycle, "status": "EXITED", "heartbeat_at": instant(clock()).isoformat()})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--datastore", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--watchlist", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--publication-offset-seconds", type=float, default=30)
    parser.add_argument("--symbol-workers", type=int)
    parser.add_argument("--lookback-days", type=int)
    parser.add_argument("--daily-data-wake")
    parser.add_argument("--data-weekdays")
    args = parser.parse_args(argv)
    if (args.daily_data_wake is None) != (args.data_weekdays is None):
        parser.error("The local daily data wake and weekdays must be supplied together")
    try:
        weekdays = tuple(int(part) for part in args.data_weekdays.split(",")) if args.data_weekdays is not None else ()
    except ValueError:
        parser.error("Local data weekdays must be comma-separated integers from 0 to 6")
    from .resource_policy import manual_symbol_workers
    actor, _ = local_symbols(args.profile, args.watchlist, args.datastore)
    config = ResearchConfig(publication_offset_seconds=args.publication_offset_seconds,
        symbol_workers=manual_symbol_workers(args.datastore, actor=actor, explicit=args.symbol_workers),
        lookback_days=args.lookback_days)
    result = run_data_service(repository=args.repository, datastore=args.datastore,
        profile=args.profile, watchlist=args.watchlist, config=config, once=args.once,
        daily_data_wake=args.daily_data_wake, data_weekdays=weekdays)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
