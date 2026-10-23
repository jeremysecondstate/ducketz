"""Human-started local 15m session. No scheduler or autostart.

The default command only reports readiness. --live is explicit manual operating
intent. Forecast/model work and serialized broker steps run independently, with
durable forecast identities, retry times and original targets.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import threading
import time
import uuid

from datafetching.runtime_lock import exclusive_runtime_lock
from .prediction import ContractError, PACIFIC, STEP, digest, encoded, instant
from .runtime import ResearchConfig, ShadowRunner, LiveHistories, SourceIntervalEmpty, local_symbols, recorded_symbol_failures, with_local_data_policy
from .store import ShadowStore, utcnow
from .candle_collection import collection_loop, market_close_at, _storage_failure_evidence

VERSION = "manual-local-15m-session-v2"


def latest_completed_reference(now, *, publication_offset_seconds):
    offset = float(publication_offset_seconds)
    if not 0 <= offset < 900:
        raise ContractError("Publication offset must lie within one quarter-hour")
    adjusted = instant(now) - timedelta(seconds=offset)
    boundary = adjusted.replace(minute=adjusted.minute // 15 * 15, second=0, microsecond=0)
    return boundary - STEP


def eligible_reference(reference):
    from .data import equity_sessions
    reference = instant(reference)
    boundary = (reference + STEP).astimezone(PACIFIC)
    return ((4,0) < (boundary.hour,boundary.minute) < (16,45)
            and any(session.contains(reference) and session.contains(reference + 2*STEP)
                    for session in equity_sessions(reference, reference + 3*STEP)))


class ExecutionQueue:
    def __init__(self, store, session_id, actor="Atlas"):
        self.store, self.session_id, self.actor = store, session_id, actor
        with store.connect() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS intraday_live_queue(
                forecast_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,symbol TEXT NOT NULL,
                issued_at TEXT NOT NULL,next_attempt_at TEXT NOT NULL,status TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS intraday_live_retry(
                scope TEXT PRIMARY KEY,next_attempt_at TEXT NOT NULL);
            """)

    def enqueue(self, forecast):
        if forecast["mode"] != "live":
            raise ContractError("Only newly issued live forecasts enter manual execution")
        if forecast.get("execution_blocked_reason") or instant(forecast["issued_at"]) >= instant(forecast["target_close"]):
            self.store.event(forecast["cycle_id"], "forecast_execution_blocked",
                {"forecast_id":forecast["forecast_id"], "reason":forecast.get("execution_blocked_reason") or "issue_after_target",
                 "forecast_preserved":True}, symbol=forecast["symbol"])
            return
        with self.store.connect() as db:
            cycle = db.execute("SELECT payload FROM cycles WHERE id=?", (forecast["cycle_id"],)).fetchone()
            if cycle is not None and json.loads(cycle[0]).get("experiment") != self.session_id:
                raise ContractError("Forecast belongs to another manual session")
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT session_id,symbol,issued_at FROM intraday_live_queue WHERE forecast_id=?",
                             (forecast["forecast_id"],)).fetchone()
            value = (self.session_id,forecast["symbol"],instant(forecast["issued_at"]).isoformat())
            if row is not None and row != value:
                raise ContractError("Forecast already belongs to another manual session")
            inserted = db.execute("INSERT OR IGNORE INTO intraday_live_queue VALUES(?,?,?,?,?,'pending')",
                       (forecast["forecast_id"],*value,value[2])).rowcount
        if inserted:
            print(json.dumps({"event":"forecast_published","symbol":forecast["symbol"],
                "issued_at":forecast["issued_at"],"reference_at":forecast["reference_at"],
                "target_close":forecast["target_close"],"probability_not_down":forecast["probability_not_down"],
                "successful_models":forecast["successful_count"],"configured_models":forecast["configured_count"],
                "ensemble_status":forecast["ensemble_status"]}),flush=True)

    def due(self, now):
        current = instant(now).isoformat()
        if not self.broker_due(now):
            return []
        with self.store.connect() as db:
            # Newer forecasts supersede unsent queued work for the same symbol.
            rows = db.execute("""SELECT q.forecast_id FROM intraday_live_queue q JOIN forecasts f ON f.id=q.forecast_id
                WHERE q.session_id=? AND q.status='pending' AND q.next_attempt_at<=?
                AND NOT EXISTS(SELECT 1 FROM intraday_live_queue n JOIN forecasts nf ON nf.id=n.forecast_id
                    WHERE n.session_id=q.session_id AND n.symbol=q.symbol
                    AND (nf.reference_at>f.reference_at OR (nf.reference_at=f.reference_at AND n.issued_at>q.issued_at)))
                ORDER BY f.reference_at,q.symbol""",
                              (self.session_id,current)).fetchall()
        ids = {row[0] for row in rows}
        return self.store.forecasts(pc=self.actor, forecast_ids=ids)

    def broker_due(self, now):
        with self.store.connect() as db:
            retry = db.execute("SELECT next_attempt_at FROM intraday_live_retry WHERE scope='broker'").fetchone()
        return retry is None or instant(retry[0]) <= instant(now)

    def broker_wait(self, *, now, seconds):
        due = (instant(now)+timedelta(seconds=max(3.0,float(seconds)))).isoformat()
        with self.store.connect() as db:
            db.execute("INSERT INTO intraday_live_retry VALUES('broker',?) ON CONFLICT(scope) DO UPDATE SET next_attempt_at=max(next_attempt_at,excluded.next_attempt_at)", (due,))

    def defer(self, forecast_id, *, now, seconds, broker_wait=False):
        due = (instant(now) + timedelta(seconds=max(3.0,float(seconds)))).isoformat()
        with self.store.connect() as db:
            db.execute("UPDATE intraday_live_queue SET next_attempt_at=? WHERE forecast_id=? AND session_id=?",
                       (due,forecast_id,self.session_id))
        if broker_wait:
            self.broker_wait(now=now,seconds=seconds)

    def complete(self, forecast_id, status):
        with self.store.connect() as db:
            db.execute("UPDATE intraday_live_queue SET status=? WHERE forecast_id=? AND session_id=?",
                       (status,forecast_id,self.session_id))


def _session_cycles(store, session_id):
    # Recovery identity lives in the existing cycle payload, not a lost Python
    # cursor. Never adopt another human session's pending work for execution.
    with store.connect() as db:
        rows = db.execute("SELECT id,payload FROM cycles ORDER BY rowid").fetchall()
    return [(identity, value) for identity, raw in rows
            if (value := json.loads(raw)).get("mode") == "live"
            and value.get("pc") in {"Atlas", "Scout"} and value.get("experiment") == session_id]


def _pending_cycle_passes(store, session_id, now):
    result = []
    for identity, cycle in _session_cycles(store, session_id):
        if cycle.get("status") == "completed":
            continue
        published = store.forecast_symbols(identity)
        missing = [symbol for symbol in cycle["symbols"] if symbol not in published]
        failures = dict(cycle.get("failures", {}))
        # The failure event is durable even if a crash preceded the cycle-map
        # update. Latest events override that possibly older summary.
        failures.update(recorded_symbol_failures(store, identity))
        due = [s for s in missing if (
            cycle.get("status") == "running" and s not in failures
        ) or (failures.get(s, {}).get("retryable") is True
              and instant(failures[s]["retry_at"]) <= instant(now))]
        if due:
            result.append((identity, cycle, due))
    return sorted(result, key=lambda item: (
        item[1].get("last_recovery_attempt_at", item[1].get("completed_at", item[1]["started_at"])),
        item[1]["reference_at"]))


def forecasting_loop(runner, *, histories, provider, journal, queue, stopped,
                     clock=utcnow, pause=time.sleep, close_at=None):
    """New quarters receive a turn before one due recovery symbol.

    Only missing data prerequisites retry automatically. Already issued model
    outputs remain immutable, and older recovered outputs never enter execution.
    No missed-quarter forecast or older fitted model is invented.
    """
    saved = _session_cycles(runner.store, queue.session_id)
    last = max((instant(c["reference_at"]) for _, c in saved), default=None)
    recovery_due = False
    while not stopped():
        if close_at is not None and instant(clock()) >= instant(close_at):
            return
        newest = latest_completed_reference(clock(), publication_offset_seconds=runner.config.publication_offset_seconds)
        retry_symbols = None
        recovery_symbol = None
        # A long new-quarter pass can cross the next boundary every time.
        # Reserve one symbol's recovery turn so those boundaries cannot starve
        # already saved scientific work. The next fresh quarter follows it.
        fair = _pending_cycle_passes(runner.store, queue.session_id, clock()) if recovery_due else []
        if fair:
            _, cycle, due = fair[0]
            symbols = cycle["symbols"]
            previous = cycle.get("last_recovery_symbol")
            start = symbols.index(previous) + 1 if previous in symbols else 0
            retry_symbols = [next(symbol for symbol in symbols[start:] + symbols[:start] if symbol in due)]
            recovery_symbol = retry_symbols[0]
            reference = instant(cycle["reference_at"])
            recovery_due = False
        elif last is None or newest > last:
            reference = newest
            if last is not None and reference > last + STEP:
                runner.store.event(queue.session_id, "missed_cycle_boundaries",
                    {"previous_reference":last.isoformat(),"next_reference":reference.isoformat(),
                     "missed_count":int((reference-last)/STEP)-1,"policy":"latest_completed_no_backlog_orders"})
            last = reference
            if not eligible_reference(reference):
                runner.store.event(queue.session_id,"closed_or_unspecified_edge",
                                   {"reference_at":reference.isoformat(),"orders_submitted":0})
                pause(1); continue
        else:
            pending = _pending_cycle_passes(runner.store, queue.session_id, clock())
            if not pending:
                pause(1); continue
            _, cycle, retry_symbols = pending[0]
            reference = instant(cycle["reference_at"])
        incoming = LiveHistories(histories=histories,provider=provider,store=runner.store,journal=journal,
                                reference_at=reference,clock=clock,sleep=pause,bounded=True,
                                stopped=lambda: stopped() or (close_at is not None and instant(clock()) >= instant(close_at)),
                                reuse_saved=True,population_contract=getattr(runner.config,"population_contract",None))
        if close_at is not None and instant(clock()) >= instant(close_at):
            return
        result = runner.run_cycle(incoming,reference_at=reference,mode="live",experiment=queue.session_id,
                         on_published=queue.enqueue,acquisition={"kind":"existing_databento_exact_interval"},
                         retry_symbols=retry_symbols,allow_execution=reference == newest)
        if result and result.get("cycle_id"):
            update = {"last_recovery_attempt_at":instant(clock()).isoformat()}
            if recovery_symbol is not None:
                update["last_recovery_symbol"] = recovery_symbol
            runner.store.update_cycle(result["cycle_id"], update)
        if retry_symbols is None:
            recovery_due = True
        pause(1)


def scoring_loop(store, *, histories, provider, journal, session_id, stopped,
                 clock=utcnow, pause=time.sleep, close_at=None, actor="Atlas"):
    """Fetch exact due outcomes independently, including after the last entry.

    Acquisition retry evidence is durable. This never submits an order, changes
    an issued prediction or writes corrections into its original input snapshot.
    Local copies of the history mapping keep scoring out of the training writer.
    """
    from .data import completed_15m_observation, synthetic_continuity_verified
    audited = set()
    def drain_retained_targets():
        interrupted = lambda: stopped() or (close_at is not None and instant(clock()) >= instant(close_at))
        if interrupted():
            return
        ready = store.retained_session_scoring_targets(pc=actor, session_id=session_id,
            as_of=clock(), stopped=interrupted)
        for forecast, observed in ready:
            if interrupted():
                return
            store.observe(forecast, as_of=clock(), candle=observed)
    while not stopped():
        if close_at is not None and instant(clock()) >= instant(close_at):
            return
        drain_retained_targets()
        if stopped() or (close_at is not None and instant(clock()) >= instant(close_at)):
            return
        observed_candidates = {}
        for candle in store.observed_candles():
            if not synthetic_continuity_verified(candle):
                key=digest(candle)
                if key not in audited:
                    store.event(session_id,"unverified_synthetic_outcome_requires_audit",
                                {"candle_hash":key,"immutable_original_retained":True},symbol=candle["symbol"])
                    audited.add(key)
                continue
            if not completed_15m_observation(candle) or instant(candle["available_at"]) > instant(clock()):
                continue
            observed_candidates.setdefault((candle["symbol"],instant(candle["opened_at"])), []).append(candle)
        for forecast in store.scoring_forecasts(pc=actor, observed_candidates=observed_candidates,
                as_of=clock(), stopped=lambda: stopped() or
                (close_at is not None and instant(clock()) >= instant(close_at))):
            if stopped():
                return
            if close_at is not None and instant(clock()) >= instant(close_at):
                return
            drain_retained_targets()
            if stopped() or (close_at is not None and instant(clock()) >= instant(close_at)):
                return
            if forecast["mode"] != "live" or instant(forecast["target_close"]) > instant(clock()):
                continue
            revisions = store.revisions(forecast["forecast_id"])
            candidates = observed_candidates.get((forecast["symbol"],instant(forecast["target_open"])), [])
            observed = next((row for row in reversed(candidates)
                if row.get("population_contract") == forecast.get("population_contract")
                and all(not forecast.get(field) or row.get(field) == forecast[field]
                       for field in ("source_dataset", "source_id"))), None)
            if observed is not None and instant(observed["available_at"]) <= instant(clock()):
                store.observe(forecast,as_of=clock(),candle=observed)
                continue
            if revisions and revisions[-1]["status"] in {"scored", "synthetic", "unscorable"}:
                continue
            symbol = forecast["symbol"]
            if symbol not in histories:
                continue
            incoming = LiveHistories(histories=dict(histories),provider=provider,store=store,journal=journal,
                reference_at=forecast["target_open"],clock=clock,sleep=pause,bounded=True,
                stopped=lambda: stopped() or (close_at is not None and instant(clock()) >= instant(close_at)),
                reuse_saved=True,population_contract=forecast.get("population_contract") or "legacy")
            incoming.bind_cycle("score:" + forecast["forecast_id"])
            try:
                history = incoming.get(symbol)
                match = history.frame.loc[history.frame.opened_at.eq(instant(forecast["target_open"]))]
                match = match.loc[[completed_15m_observation(row, from_frame=True) and synthetic_continuity_verified(row)
                    and (row.get("population_contract") if isinstance(row.get("population_contract"), str) else None)
                        == forecast.get("population_contract")
                    and instant(row["available_at"]) <= instant(clock())
                    and row.get("availability_kind") == "actual"
                    and all(not forecast.get(field) or row.get(field) == forecast[field]
                            for field in ("source_dataset", "source_id"))
                    for row in match.to_dict("records")]]
                if match.empty:
                    raise ContractError("Exact target interval unavailable after acquisition")
                match = match.sort_values("available_at")
                from .runtime import _jsonable
                store.observe(forecast,as_of=clock(),candle=_jsonable(match.iloc[-1].to_dict()))
            except SourceIntervalEmpty as exc:
                store.event(session_id,"target_source_empty",{"forecast_id":forecast["forecast_id"],
                    "evidence":exc.evidence,"no_market_wide_no_trade_claim":True},symbol=symbol)
                store.observe(forecast,as_of=clock(),
                    unavailable_reason="covered_source_empty; no observed outcome price or market-wide no-trade proof")
            except InterruptedError:
                if stopped() or (close_at is not None and instant(clock()) >= instant(close_at)):
                    return
                raise
            except Exception as exc:
                store.event(session_id,"target_scoring_retry",{"forecast_id":forecast["forecast_id"],
                    "error_type":type(exc).__name__,"message":str(exc)},symbol=symbol)
            finally:
                drain_retained_targets()
        pause(3)


def score_saved_session_outcomes(store, *, session_id, actor, symbols, as_of, stopped=lambda: False):
    """Finalize this manual session using only already-persisted own candles.

    Keep one full forecast at a time and retain newer concurrent observations.
    Missing targets stay unresolved; verified synthetic outcomes stay distinct.
    No acquisition, model or execution clients are constructed.
    """
    from .data import completed_15m_observation, synthetic_continuity_verified
    now, membership = instant(as_of), set(symbols)
    if actor not in {"Atlas", "Scout"} or not membership:
        raise ContractError("Final scoring requires this manual session's actor and symbols")
    result = {"session_id": session_id, "scored": 0, "synthetic": 0, "unavailable": 0}
    if stopped():
        return result
    with store.connect() as db:
        cycles = db.execute("SELECT id FROM cycles WHERE pc=? AND json_extract(payload,'$.mode')='live' "
                            "AND json_extract(payload,'$.experiment')=?", (actor, session_id)).fetchall()
    latest, loaded_symbols = {}, set()
    for (cycle_id,) in cycles:
        for symbol in sorted(membership):
            if stopped():
                return result
            forecast = store.forecast(cycle_id, symbol)
            if (forecast is None or forecast["pc"] != actor or forecast["mode"] != "live"
                    or instant(forecast["target_close"]) > now):
                continue
            if symbol not in loaded_symbols:
                for candle in store.observed_candles(symbol):
                    if (instant(candle["available_at"]) <= now and completed_15m_observation(candle)
                            and synthetic_continuity_verified(candle)):
                        latest[(symbol, candle.get("source_id"), candle.get("source_dataset"),
                                candle.get("population_contract"), instant(candle["opened_at"]))] = candle
                loaded_symbols.add(symbol)
            if stopped():
                return result
            candle = latest.get((forecast["symbol"], forecast.get("source_id"), forecast.get("source_dataset"),
                                 forecast.get("population_contract"), instant(forecast["target_open"])))
            if candle is None:
                result["unavailable"] += 1
                continue
            outcome = store.observe(forecast, as_of=now, candle=candle, skip_newer=True)
            if outcome is None:
                result["newer_observation_retained"] = result.get("newer_observation_retained", 0) + 1
            else:
                result[outcome["status"]] += 1
    return result


def run_manual(*, repository, datastore, profile, watchlist, config, clock=utcnow, sleep=time.sleep):
    from .account_execution import IntradayAccountExecutor, execution_failure_evidence
    from .local_execution import load_local_binding
    from .data import AcquisitionJournal, load_local_history
    from .coverage import RECOVERY_POPULATION_CONTRACT
    from .cancellation import retry_delay
    from app.services.schwab import SchwabSession
    from app.services.databento_market_data import DatabentoMarketDataProvider
    from dotenv import load_dotenv
    root, repository = Path(datastore),Path(repository)
    config = with_local_data_policy(config, root)
    pc,symbols = local_symbols(profile,watchlist,root)
    binding = load_local_binding(root)
    if binding.actor != pc or set(binding.symbols) != set(symbols):
        raise ContractError("Independent local execution binding does not match profile")
    config.validate()
    if config.source_dataset != "EQUS.MINI":
        raise ContractError("Existing single-source live provider binding required")
    started = instant(clock())
    close_at = market_close_at(started)
    session_id = "manual-" + pc.lower() + "-15m:" + uuid.uuid4().hex
    stopped = threading.Event()
    final_collection_complete = threading.Event()
    worker_errors = []
    market_collector = None
    store = ShadowStore(root / "intraday/shadow.sqlite3")
    queue = ExecutionQueue(store,session_id,actor=pc)
    status_path = root / "intraday/manual-session.json"
    def status(state, **extra):
        value = dict(schema_version=VERSION,session_id=session_id,pid=os.getpid(),started_at=started.isoformat(),
                     heartbeat_at=instant(clock()).isoformat(),status=state,manual_start=True,
                     market_close_at=close_at.isoformat(),final_collection_complete=final_collection_complete.is_set(),**extra)
        temporary = status_path.with_name(status_path.name + "." + uuid.uuid4().hex + ".tmp")
        temporary.write_text(encoded(value),encoding="utf-8")
        os.replace(temporary,status_path)
    def manual_authority():
        try:
            value = json.loads(status_path.read_text(encoding="utf-8"))
            return (value["session_id"] == session_id and value["pid"] == os.getpid()
                    and value["status"] == "RUNNING" and not stopped.is_set() and instant(clock()) < close_at)
        except (OSError,ValueError,KeyError):
            return False
    def pause(seconds):
        end = time.monotonic() + max(0,float(seconds))
        while time.monotonic() < end:
            if stopped.is_set():
                raise InterruptedError("Manual session stopped; acquisition recovery retained")
            sleep(min(1,max(0,end-time.monotonic())))
    with exclusive_runtime_lock(root / "locks/intraday-manual-session.lock",process_name="intraday-manual-session"):
        status("STARTING")
        try:
            load_dotenv(repository / ".env")
            histories = {}
            history_load_cutoff = instant(clock())
            for symbol in symbols:
                try:
                    histories[symbol] = load_local_history(root,symbol,source_dataset=config.source_dataset,
                        **({"population_contract":config.population_contract} if config.population_contract else {}),
                        **({"recovery_cutoff":history_load_cutoff} if config.population_contract == RECOVERY_POPULATION_CONTRACT else {}))
                except Exception as exc:
                    store.event(session_id,"history_unavailable",{"error_type":type(exc).__name__,"message":str(exc)},symbol=symbol)
            provider = DatabentoMarketDataProvider()
            if provider.dataset != config.source_dataset:
                raise ContractError("Provider binding differs from selected single source")
            journal = AcquisitionJournal(root / "intraday/acquisition.sqlite3")
            runner = ShadowRunner(store=store,pc=pc,symbols=symbols,config=config,clock=clock,
                stopped=lambda: stopped.is_set() or instant(clock()) >= close_at)
            if config.market_inputs_enabled:
                try:
                    from .market_inputs import MarketInputCollector
                    market_collector = MarketInputCollector(root, symbols)
                    startup = market_collector.start()
                    store.event(session_id,"market_input_streams_started",{"status_by_dataset":startup})
                except Exception as exc:
                    store.event(session_id,"market_inputs_start_error",{"error_type":type(exc).__name__,
                        "core_forecasts_continue":True})
            executor = IntradayAccountExecutor(datastore=root,store=store,broker=SchwabSession(),local_symbols=symbols,
                session_started_at=started,clock=clock,manual_authority=manual_authority,actor=pc,stopped=stopped.is_set,
                diagnostic_session_id=session_id)
        except BaseException as exc:
            if market_collector is not None:
                market_collector.close()
            stopped.set()
            status("STOPPED" if isinstance(exc,KeyboardInterrupt) else "FAILED",
                   stop_reason="manual_stop" if isinstance(exc,KeyboardInterrupt) else "startup_failure",
                   error_type=type(exc).__name__,broker_orders_automatically_cancelled=False)
            store.event(session_id,"manual_startup_stopped",{"error_type":type(exc).__name__,"message":str(exc)})
            if isinstance(exc,KeyboardInterrupt):
                return {"session_id":session_id,"status":"STOPPED"}
            raise
        status("CLOSING" if instant(clock()) >= close_at else "RUNNING")
        def market_input_work():
            """Optional feeds run independently; missing inputs never stop core work."""
            from .market_input_worker import run_market_input_worker
            run_market_input_worker(market_collector, clock=clock, stopped=stopped.is_set, pause=sleep,
                reference_for=lambda value: latest_completed_reference(value, publication_offset_seconds=config.publication_offset_seconds),
                event=lambda kind, value: store.event(session_id, kind, value))
        input_worker = threading.Thread(target=market_input_work,name="intraday-market-inputs",daemon=True)
        def forecast_work():
            try:
                forecasting_loop(runner,histories=histories,provider=provider,journal=journal,queue=queue,
                                 stopped=stopped.is_set,clock=clock,pause=pause,close_at=close_at)
            except BaseException as exc:
                if isinstance(exc, InterruptedError) and instant(clock()) >= close_at and not stopped.is_set():
                    store.event(session_id,"forecasting_closed", {"closed_at":close_at.isoformat(),
                        "final_candle_collection_continues":True})
                    return
                store.event(session_id,"forecast_worker_stopped",{"error_type":type(exc).__name__,"message":str(exc)})
                if not (isinstance(exc,InterruptedError) and stopped.is_set()):
                    worker_errors.append({"worker":"forecast","error_type":type(exc).__name__,"message":str(exc)})
                stopped.set()
        worker = threading.Thread(target=forecast_work,name="intraday-manual-forecast",daemon=True)
        def score_work():
            try:
                scoring_loop(store,histories=histories,provider=provider,journal=journal,session_id=session_id,
                             stopped=stopped.is_set,clock=clock,pause=pause,close_at=close_at,actor=pc)
            except BaseException as exc:
                if isinstance(exc, InterruptedError) and (stopped.is_set() or instant(clock()) >= close_at):
                    return
                store.event(session_id,"scoring_worker_stopped",{"error_type":type(exc).__name__,"message":str(exc)})
                worker_errors.append({"worker":"scoring","error_type":type(exc).__name__,"message":str(exc)})
                stopped.set()
        scorer = threading.Thread(target=score_work,name="intraday-manual-scoring",daemon=True)
        def collection_completed(result):
            if stopped.is_set():
                return
            scored = score_saved_session_outcomes(store, session_id=session_id, actor=pc, symbols=symbols,
                as_of=clock(), stopped=stopped.is_set)
            store.event(session_id, "final_saved_outcomes_scored", scored)
            if stopped.is_set():
                return
            final_collection_complete.set()
            stopped.set()
        def collect_work():
            try:
                collection_loop(store, histories=histories, provider=provider, journal=journal, symbols=symbols,
                    session_id=session_id, started_at=started, close_at=close_at,
                    publication_offset_seconds=config.publication_offset_seconds,
                    population_contract=config.population_contract,
                    clock=clock, stopped=stopped.is_set, pause=pause, on_completed=collection_completed)
            except BaseException as exc:
                if isinstance(exc, InterruptedError) and stopped.is_set():
                    return
                diagnostics = _storage_failure_evidence(exc)
                failure = {"error_type":type(exc).__name__,"message":str(exc),"diagnostics":diagnostics}
                worker_errors.append({"worker":"collection",**failure})
                stopped.set()
                try:
                    store.event(session_id,"collection_worker_stopped",failure)
                except Exception as diagnostic_exc:
                    print(encoded({"event":"collection_worker_stopped", "diagnostics":diagnostics,
                        "diagnostic_failure":_storage_failure_evidence(diagnostic_exc)}),flush=True)
        collector = threading.Thread(target=collect_work,name="intraday-manual-candles",daemon=True)
        if market_collector is not None:
            input_worker.start()
        worker.start()
        scorer.start()
        collector.start()
        print(f"{pc} 15m manual session running. New predictions can submit real orders. Ctrl-C stops this worker; resting orders retain their broker identities.",flush=True)
        next_recovery = instant(clock())
        closing_announced = False
        manual_stop_requested = False
        try:
            while not stopped.is_set():
                if instant(clock()) >= close_at:
                    status("CLOSING", broker_orders_automatically_cancelled=False,
                           waiting_for_final_candles=True)
                    if not closing_announced:
                        store.event(session_id,"market_close_collection_pending", {
                            "closed_at":close_at.isoformat(), "final_reference_at":(close_at-STEP).isoformat(),
                            "symbols":list(symbols), "new_forecasts_and_orders_disabled":True})
                        closing_announced = True
                    sleep(1)
                    continue
                status("RUNNING")
                for forecast in queue.due(clock()):
                    # A prior symbol in this snapshot may have just received a
                    # longer provider wait. No later symbol bypasses that wait.
                    if instant(clock()) >= close_at or not queue.broker_due(clock()):
                        break
                    try:
                        result = executor.step(forecast)
                        store.event(session_id,"execution_step",result,symbol=forecast["symbol"])
                        print(json.dumps({"event":"execution_disposition","symbol":forecast["symbol"],
                            **{key:result.get(key) for key in ("side","desired_quantity","adjusted_quantity",
                                "disposition","submission_status","submitted_quantity","filled_quantity")}}),flush=True)
                        disposition = result["disposition"]
                        if disposition == "previous_intention_reconciliation" or result["evidence"].get("proven_unsent_retry_eligible"):
                            queue.defer(forecast["forecast_id"],now=clock(),seconds=result["evidence"].get("retry_after_seconds") or 3,broker_wait=True)
                        elif disposition == "submission_recovery_required":
                            # UNKNOWN is never requeued for submission. Status
                            # recovery and other symbols still honor its wait.
                            queue.broker_wait(now=clock(),seconds=result["evidence"].get("retry_after_seconds") or 3)
                            queue.complete(forecast["forecast_id"],disposition)
                        elif result["kind"] == "sizing_unavailable":
                            if (disposition == "quote_stale_or_future"
                                    and instant(clock()) < instant(forecast["target_close"])):
                                # No desired quantity exists yet. Retain the failed
                                # observation and retry fresh evidence for this
                                # signal; the executor still enforces its expiry.
                                queue.defer(forecast["forecast_id"],now=clock(),
                                    seconds=3,broker_wait=False)
                            else:
                                queue.complete(forecast["forecast_id"],"sizing_unavailable")
                        else:
                            queue.complete(forecast["forecast_id"],disposition)
                    except Exception as exc:
                        delay = retry_delay(exc,clock())
                        queue.defer(forecast["forecast_id"],now=clock(),seconds=delay,broker_wait=True)
                        store.event(session_id,"execution_retry",{"forecast_id":forecast["forecast_id"],
                            "error_type":type(exc).__name__,"message":str(exc),"retry_after_seconds":delay,
                            "diagnostics":execution_failure_evidence(exc)},symbol=forecast["symbol"])
                        break
                if instant(clock()) < close_at and instant(clock()) >= next_recovery and queue.broker_due(clock()):
                    try:
                        executor.recover()
                        delay = 3
                    except Exception as exc:
                        delay = retry_delay(exc,clock())
                        queue.broker_wait(now=clock(),seconds=delay)
                        store.event(session_id,"order_recovery_retry",{"error_type":type(exc).__name__,
                            "message":str(exc),"retry_after_seconds":delay,
                            "diagnostics":execution_failure_evidence(exc)})
                    next_recovery = instant(clock()) + timedelta(seconds=delay)
                sleep(1)
        except KeyboardInterrupt:
            manual_stop_requested = True
            stopped.set()
        except BaseException as exc:
            worker_errors.append({"worker":"execution","error_type":type(exc).__name__,"message":str(exc)})
            raise
        finally:
            stopped.set()
            if market_collector is not None:
                market_collector.close()
            stop_reason = ("worker_failure" if worker_errors else
                           "market_close" if final_collection_complete.is_set() and not manual_stop_requested else "manual_stop")
            status("STOPPING",broker_orders_automatically_cancelled=False,
                   stop_reason=stop_reason)
            print("15m order authority stopped. Waiting for active computation to finish; pending broker identities are retained.",flush=True)
            while worker.is_alive() or scorer.is_alive() or collector.is_alive() or input_worker.is_alive():
                worker.join(timeout=1)
                scorer.join(timeout=1)
                collector.join(timeout=1)
                if input_worker.is_alive():
                    input_worker.join(timeout=1)
                status("STOPPING",broker_orders_automatically_cancelled=False,
                       stop_reason=stop_reason)
            stop_reason = ("worker_failure" if worker_errors else
                           "market_close" if final_collection_complete.is_set() and not manual_stop_requested else "manual_stop")
            status("FAILED" if worker_errors else "STOPPED",broker_orders_automatically_cancelled=False,
                   stop_reason=stop_reason,worker_errors=worker_errors)
            store.event(session_id,"manual_session_stopped",{"pending_order_identity_preserved":True,
                        "stop_reason":stop_reason,"worker_errors":worker_errors})
        return {"session_id":session_id,"status":"FAILED" if worker_errors else "STOPPED", "stop_reason":stop_reason}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository",type=Path,default=Path(__file__).resolve().parents[2])
    parser.add_argument("--datastore",type=Path,default=Path("C:/DATASTORE"))
    parser.add_argument("--profile",type=Path)
    parser.add_argument("--watchlist",type=Path)
    parser.add_argument("--live",action="store_true")
    parser.add_argument("--publication-offset-seconds",type=float,default=30)
    parser.add_argument("--symbol-workers",type=int,default=None)
    parser.add_argument("--lookback-days",type=int)
    args = parser.parse_args(argv)
    profile = args.profile or args.repository / "scratch/cross-pc/local-profile.json"
    watchlist = args.watchlist or args.repository / "datafetching/watchlist.local.txt"
    if not args.live:
        from .readiness import inspect_readiness
        result = inspect_readiness(repository=args.repository,datastore=args.datastore,
            profile=profile,watchlist=watchlist)
        print(json.dumps(result,sort_keys=True))
        return 0 if result.get("manual_live_start_ready") else 2
    from .resource_policy import manual_symbol_workers
    actor, _ = local_symbols(profile, watchlist, args.datastore)
    workers = manual_symbol_workers(args.datastore, actor=actor, explicit=args.symbol_workers)
    result = run_manual(repository=args.repository,datastore=args.datastore,profile=profile,watchlist=watchlist,
        config=ResearchConfig(publication_offset_seconds=args.publication_offset_seconds,
            symbol_workers=workers,lookback_days=args.lookback_days))
    print(json.dumps(result,sort_keys=True))
    return 1 if result["status"] == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
