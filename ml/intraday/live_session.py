"""Human-started Atlas or Scout 15m session. No scheduler or Gameplan activation.

The default command only reports readiness. --live is explicit manual operating
intent. Forecast/model work and serialized broker steps run independently, with
durable forecast identities, retry times and original targets.
"""
import argparse
from contextlib import nullcontext
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import threading
import time
import uuid

import psutil

from datafetching.runtime_lock import exclusive_runtime_lock
from .prediction import ContractError, PACIFIC, STEP, digest, encoded, instant, number
from .runtime import (ResearchConfig, ShadowRunner, LiveHistories, local_symbols,
                      recorded_symbol_failures, _symbol_exception_diagnostics)
from .store import ShadowStore, utcnow

VERSION = "manual-atlas-15m-session-v1"
SCOUT_VERSION = "manual-scout-15m-session-v1"


def automatic_stop_deadline(started):
    """Latch 5pm on this manual session's Pacific date, including across DST."""
    return instant(started).astimezone(PACIFIC).replace(
        hour=17, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def manual_wake_at(started):
    """One manually selected operating day; never roll a finished session forward."""
    from .data import equity_sessions
    started = instant(started)
    local = started.astimezone(PACIFIC)
    # Preserve the established after-5pm Scout immediate-stop behavior.
    if local.hour >= 17:
        return local.replace(hour=4, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    for session in equity_sessions(started, started + timedelta(days=14)):
        opened = instant(session.opened_at)
        day = opened.astimezone(PACIFIC)
        if day.date() >= local.date():
            wake = day.replace(hour=4, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
            return max(wake, opened)
    raise ContractError("No upcoming exchange session available for manual startup")


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


def prepare_market_collection(model_runtime, *, datastore, pc, symbols, clock=utcnow):
    """Latch data-only sources when this PC's private recipe explicitly opts in."""
    contract = getattr(model_runtime.get("dataset_builder"), "market_context_contract", None)
    if contract is None:
        return None
    from .market_context import validate_market_context_contract
    from .market_collection import load_local_collection_config, collector_from_config, SourceRequest
    contract = validate_market_context_contract(contract)
    config = load_local_collection_config(datastore, pc, symbols)
    expected_hash = getattr(model_runtime["dataset_builder"], "market_collection_configuration_sha256", None)
    if config.get("configuration_sha256") != expected_hash:
        raise ContractError("Market collection configuration differs from the frozen local recipe")
    available = {}
    for payload in config["requests"]:
        request = SourceRequest(**payload)
        available.setdefault(request.source_kind, set()).add(request.source_id)
    for kind, selection in contract["source_ids"].items():
        selected = {selection} if isinstance(selection, str) else set(selection)
        if not selected.issubset(available.get(kind, set())):
            raise ContractError("Frozen model context sources differ from this PC's collection configuration")
    return collector_from_config(config, datastore=datastore, pc=pc, clock=clock)


def prepare_minute_provider(model_runtime, *, datastore, pc, symbols, source_dataset, clock=utcnow):
    """Keep old recipes bound to their source; v4 obtains current native Live bars."""
    if getattr(model_runtime.get("dataset_builder"), "market_context_contract", None) is not None:
        from .live_minute_provider import LiveMinuteReplayProvider
        provider = LiveMinuteReplayProvider(datastore=datastore, pc=pc, symbols=symbols, clock=clock)
    else:
        from app.services.databento_market_data import DatabentoMarketDataProvider
        provider = DatabentoMarketDataProvider()
    if provider.dataset != source_dataset:
        raise ContractError("Provider binding differs from selected single source")
    from .minute_persistence import PersistedMinuteProvider
    return PersistedMinuteProvider(provider, datastore=datastore, pc=pc, symbols=symbols, clock=clock)


class ExecutionQueue:
    def __init__(self, store, session_id, *, pc="Atlas", event_sink=None):
        if pc not in {"Atlas", "Scout"}:
            raise ContractError("Explicit Atlas or Scout execution queue required")
        self.store, self.session_id, self.pc = store, session_id, pc
        self.event_sink = event_sink or (lambda value: print(json.dumps(value),flush=True))
        with store.connect() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS intraday_live_queue(
                forecast_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,symbol TEXT NOT NULL,
                issued_at TEXT NOT NULL,next_attempt_at TEXT NOT NULL,status TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS intraday_live_retry(
                scope TEXT PRIMARY KEY,next_attempt_at TEXT NOT NULL);
            """)

    def enqueue(self, forecast):
        if forecast["pc"] != self.pc:
            raise ContractError("Forecast belongs to another participant")
        if forecast["mode"] != "live":
            raise ContractError("Only newly issued live forecasts enter manual execution")
        if forecast.get("execution_blocked_reason") or instant(forecast["issued_at"]) >= instant(forecast["target_close"]):
            self.store.event(forecast["cycle_id"], "forecast_execution_blocked",
                {"forecast_id":forecast["forecast_id"], "reason":forecast.get("execution_blocked_reason") or "issue_after_target",
                 "forecast_preserved":True}, symbol=forecast["symbol"])
            return
        with self.store.connect() as db:
            cycle = db.execute("SELECT payload FROM cycles WHERE id=?", (forecast["cycle_id"],)).fetchone()
            if cycle is not None:
                source = json.loads(cycle[0])
                if source.get("experiment") != self.session_id:
                    raise ContractError("Forecast belongs to another manual session")
                if source.get("pc") != self.pc:
                    raise ContractError("Cycle belongs to another participant")
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT session_id,symbol,issued_at FROM intraday_live_queue WHERE forecast_id=?",
                             (forecast["forecast_id"],)).fetchone()
            value = (self.session_id,forecast["symbol"],instant(forecast["issued_at"]).isoformat())
            if row is not None and row != value:
                raise ContractError("Forecast already belongs to another manual session")
            inserted = db.execute("INSERT OR IGNORE INTO intraday_live_queue VALUES(?,?,?,?,?,'pending')",
                       (forecast["forecast_id"],*value,value[2])).rowcount
        if inserted:
            self.event_sink({"event":"forecast_published","symbol":forecast["symbol"],
                "forecast_id":forecast["forecast_id"],
                "issued_at":forecast["issued_at"],"reference_at":forecast["reference_at"],
                "target_close":forecast["target_close"],"probability_not_down":forecast["probability_not_down"],
                "successful_models":forecast["successful_count"],"configured_models":forecast["configured_count"],
                "ensemble_status":forecast["ensemble_status"]})

    def due(self, now):
        current = instant(now).isoformat()
        if not self.broker_due(now):
            return []
        with self.store.connect() as db:
            # Newer forecasts supersede unsent queued work for the same symbol.
            rows = db.execute("""SELECT q.forecast_id FROM intraday_live_queue q JOIN forecasts f ON f.id=q.forecast_id
                WHERE q.session_id=? AND f.pc=? AND q.status='pending' AND q.next_attempt_at<=?
                AND NOT EXISTS(SELECT 1 FROM intraday_live_queue n JOIN forecasts nf ON nf.id=n.forecast_id
                    WHERE n.session_id=q.session_id AND nf.pc=f.pc AND n.symbol=q.symbol
                    AND (nf.reference_at>f.reference_at OR (nf.reference_at=f.reference_at AND n.issued_at>q.issued_at)))
                ORDER BY f.reference_at,q.symbol""",
                              (self.session_id,self.pc,current)).fetchall()
        ids = {row[0] for row in rows}
        return [f for f in self.store.forecasts(pc=self.pc) if f["forecast_id"] in ids]

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
            changed = db.execute("""UPDATE intraday_live_queue SET next_attempt_at=? WHERE forecast_id=? AND session_id=?
                AND forecast_id IN (SELECT id FROM forecasts WHERE pc=?)""",
                       (due,forecast_id,self.session_id,self.pc)).rowcount
        if broker_wait and changed:
            self.broker_wait(now=now,seconds=seconds)

    def complete(self, forecast_id, status):
        with self.store.connect() as db:
            db.execute("""UPDATE intraday_live_queue SET status=? WHERE forecast_id=? AND session_id=?
                AND forecast_id IN (SELECT id FROM forecasts WHERE pc=?)""",
                       (status,forecast_id,self.session_id,self.pc))


def _session_cycles(store, session_id, *, pc="Atlas"):
    # Recovery identity lives in the existing cycle payload, not a lost Python
    # cursor. Never adopt another human session's pending work for execution.
    with store.connect() as db:
        rows = db.execute("SELECT id,payload FROM cycles ORDER BY rowid").fetchall()
    return [(identity, value) for identity, raw in rows
            if (value := json.loads(raw)).get("mode") == "live"
            and value.get("pc") == pc and value.get("experiment") == session_id]


def _pending_cycle_passes(store, session_id, now, *, pc="Atlas"):
    result = []
    for identity, cycle in _session_cycles(store, session_id, pc=pc):
        if cycle.get("status") == "completed":
            continue
        missing = [symbol for symbol in cycle["symbols"] if store.forecast(identity, symbol) is None]
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
                     clock=utcnow, pause=time.sleep, history_source=None, on_iteration=None, recovery_first=False):
    """New quarters first; durable acquisition retries get bounded fair passes.

    Only missing data prerequisites retry automatically. Already issued model
    outputs remain immutable, and older recovered outputs never enter execution.
    No missed-quarter forecast or older fitted model is invented.
    """
    pc = getattr(queue, "pc", "Atlas")
    saved = _session_cycles(runner.store, queue.session_id, pc=pc)
    last = max((instant(c["reference_at"]) for _, c in saved), default=None)
    recovery_turn = recovery_first
    while not stopped():
        newest = latest_completed_reference(clock(), publication_offset_seconds=runner.config.publication_offset_seconds)
        retry_symbols = None
        fresh_due = last is None or newest > last
        # One due recovery pass follows each fresh cycle, even when that
        # cycle overran another quarter. Otherwise continuous overruns would
        # starve saved acquisition failures despite their inputs arriving.
        pending = (_pending_cycle_passes(runner.store, queue.session_id, clock(), pc=pc)
                   if recovery_turn or not fresh_due else [])
        recovery_turn = False
        if pending:
            _, cycle, retry_symbols = pending[0]
            reference = instant(cycle["reference_at"])
        elif fresh_due:
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
            recovery_turn = True
        else:
            pause(1); continue
        incoming = (history_source(reference) if history_source is not None else
                    LiveHistories(histories=histories,provider=provider,store=runner.store,journal=journal,
                                  reference_at=reference,clock=clock,sleep=pause,bounded=True,stopped=stopped))
        result = runner.run_cycle(incoming,reference_at=reference,mode="live",experiment=queue.session_id,
                         on_published=queue.enqueue,acquisition={"kind":"existing_databento_exact_interval"},
                         retry_symbols=retry_symbols,allow_execution=reference == newest)
        if result and result.get("cycle_id"):
            runner.store.update_cycle(result["cycle_id"], {"last_recovery_attempt_at":instant(clock()).isoformat()})
        configured = tuple(getattr(runner, "symbols", ()))
        full_scope = retry_symbols is None or (
            bool(configured) and len(retry_symbols) == len(configured)
            and len(set(retry_symbols)) == len(configured)
            and set(retry_symbols) == set(configured))
        reused = isinstance(result, dict) and result.get("recovered_existing") is True
        if on_iteration is not None and full_scope and not reused:
            on_iteration(result)
            return  # Full returned attempt; no next-quarter overlap during handoff.
        pause(1)


def _outcome_already_observed(forecast, candle, revisions, *, as_of, verified_candle_id=None):
    """Skip only complete identical evidence; optional ID is reader-verified."""
    if not revisions:
        return False
    previous = revisions[-1]
    try:
        if (instant(previous['observed_at']) > instant(as_of)
                or instant(candle['available_at']) > instant(as_of)):
            return False
        price = str(candle['close'])
        derived = isinstance(forecast.get('reference_provenance', {}).get('forward_fill_contract'), str)
        evidence = {'forecast_id': forecast['forecast_id'],
            'status': 'synthetic' if candle.get('synthetic') or derived else 'scored',
            'target_not_down': number(candle['close'], positive=True) >= number(forecast['reference_open'], positive=True),
            'reference_to_target_return': str(number(price) / number(forecast['reference_open']) - 1),
            'return_basis': 'reference_open_to_exact_target_close_not_trade_pnl',
            'target_close_price': price, 'candle_id': verified_candle_id if verified_candle_id is not None else digest(candle),
            'unavailable_reason': None, 'availability_basis': candle.get('availability_kind')}
        prior_evidence = {key: value for key, value in previous.items() if key not in {'observed_at', 'revision_id'}}
        return (prior_evidence == evidence
                and previous.get('revision_id') == digest(evidence) == digest(prior_evidence))
    except (KeyError, TypeError, ValueError, ArithmeticError, ContractError):
        return False


def scoring_loop(store, *, histories, provider, journal, session_id, stopped,
                 clock=utcnow, pause=time.sleep, pc="Atlas", history_source=None):
    """Fetch exact due outcomes independently, including after the last entry.

    Acquisition retry evidence is durable. This never submits an order, changes
    an issued prediction or writes corrections into its original input snapshot.
    Local copies of the history mapping keep scoring out of the training writer.
    """
    from .data import completed_15m_observation, synthetic_continuity_verified
    from .native_interval import candle_matches_forecast
    from .store import ScoringForecastReader
    from .scoring_candles import ScoringCandleReader
    if pc not in {"Atlas", "Scout"}:
        raise ContractError("Explicit Atlas or Scout scoring participant required")
    audited = set()
    prior_notices = getattr(store, "scoring_audit_notices", None)
    notified = prior_notices(session_id) if callable(prior_notices) else set()
    forecasts = ScoringForecastReader(store, pc=pc)
    session_forecasts = ScoringForecastReader(store, pc=pc, session_id=session_id)
    candles = ScoringCandleReader(store, stopped=stopped)

    def drain_retained_targets():
        """Score this session's saved targets before historical acquisition."""
        if stopped():
            return
        as_of = clock()
        current = [forecast for forecast in session_forecasts
            if forecast["mode"] == "live" and instant(forecast["target_close"]) <= instant(as_of)]
        targets = {(forecast["symbol"], instant(forecast["target_open"])) for forecast in current}
        ready = {}
        for selection in candles.iter_targets(targets):
            if stopped():
                return
            candle = selection['candle']
            if (selection['actual'] and selection['continuity_verified'] and selection['completed']
                    and instant(candle['available_at']) <= instant(as_of)):
                ready.setdefault((candle['symbol'], instant(candle['opened_at'])), []).append(selection)
        for forecast in current:
            if stopped():
                return
            observed = next((row for row in reversed(ready.get(
                (forecast['symbol'], instant(forecast['target_open'])), []))
                if all(not forecast.get(field) or row['candle'].get(field) == forecast[field]
                       for field in ('source_dataset', 'source_id'))
                and candles.matches(row, forecast)), None)
            if observed is None:
                continue
            revisions = store.revisions(forecast['forecast_id'])
            if _outcome_already_observed(forecast, observed['candle'], revisions, as_of=as_of,
                                        verified_candle_id=observed['candle_id']):
                continue
            original_candle = candles.original(observed)
            original_forecast = session_forecasts.original(forecast)
            if stopped():
                return
            store.observe(original_forecast, as_of=as_of, candle=original_candle, skip_newer=True)

    while not stopped():
        drain_retained_targets()
        if stopped():
            return
        observed_candidates = {}
        for selection in candles:
            if stopped():
                return
            candle = selection['candle']
            if not selection['actual']:
                continue
            if not selection['continuity_verified']:
                key = selection['candle_id']
                if key not in audited:
                    candles.original(selection)
                    if stopped():
                        return
                    # Revalidate the original in every worker generation;
                    # only an identical session diagnostic may be omitted.
                    notice = (candle["symbol"], key)
                    if notice not in notified:
                        store.event(session_id,"unverified_synthetic_outcome_requires_audit",
                                    {"candle_hash":key,"immutable_original_retained":True},symbol=candle["symbol"])
                        notified.add(notice)
                    audited.add(key)
                continue
            if not selection['completed'] or instant(candle["available_at"]) > instant(clock()):
                continue
            observed_candidates.setdefault((candle["symbol"],instant(candle["opened_at"])), []).append(selection)
        for forecast in forecasts:
            if stopped():
                return
            if forecast["mode"] != "live" or instant(forecast["target_close"]) > instant(clock()):
                continue
            revisions = store.revisions(forecast["forecast_id"])
            if (revisions and revisions[-1]["status"] == "synthetic"
                    and "outcome:" + revisions[-1]["revision_id"] not in audited):
                audited.add("outcome:" + revisions[-1]["revision_id"])
                forecasts.original(forecast)
                if stopped():
                    return
                store.invalidate_unverified_outcome(forecast["forecast_id"], as_of=clock())
                revisions = store.revisions(forecast["forecast_id"])
            candidates = observed_candidates.get((forecast["symbol"],instant(forecast["target_open"])), [])
            observed = next((row for row in reversed(candidates)
                if all(not forecast.get(field) or row['candle'].get(field) == forecast[field]
                       for field in ("source_dataset", "source_id"))
                and candles.matches(row, forecast)), None)
            if observed is not None and instant(observed['candle']["available_at"]) <= instant(clock()):
                if not _outcome_already_observed(forecast, observed['candle'], revisions, as_of=clock(),
                                                verified_candle_id=observed['candle_id']):
                    original_candle = candles.original(observed)
                    original_forecast = forecasts.original(forecast)
                    if stopped():
                        return
                    store.observe(original_forecast,as_of=clock(),candle=original_candle,skip_newer=True)
                continue
            if revisions and revisions[-1]["status"] in {"scored", "synthetic", "unscorable"}:
                continue
            symbol = forecast["symbol"]
            if symbol not in histories:
                continue
            drain_retained_targets()
            if stopped():
                return
            incoming = (history_source(forecast["target_open"]) if history_source is not None else
                        LiveHistories(histories=dict(histories),provider=provider,store=store,journal=journal,
                                      reference_at=forecast["target_open"],clock=clock,sleep=pause,bounded=True,stopped=stopped))
            incoming.bind_cycle("score:" + forecast["forecast_id"])
            try:
                history = incoming.get(symbol)
                match = history.frame.loc[history.frame.opened_at.eq(instant(forecast["target_open"]))]
                match = match.loc[[completed_15m_observation(row, from_frame=True) and synthetic_continuity_verified(row)
                    and candle_matches_forecast(row, forecast)
                    and instant(row["available_at"]) <= instant(clock())
                    and row.get("availability_kind") == "actual"
                    and all(not forecast.get(field) or row.get(field) == forecast[field]
                            for field in ("source_dataset", "source_id"))
                    for row in match.to_dict("records")]]
                if match.empty:
                    raise ContractError("Exact target interval unavailable after acquisition")
                match = match.sort_values("available_at")
                from .runtime import _jsonable
                original_forecast = forecasts.original(forecast)
                if stopped():
                    return
                store.observe(original_forecast,as_of=clock(),candle=_jsonable(match.iloc[-1].to_dict()),skip_newer=True)
            except InterruptedError:
                if stopped():
                    return
                raise
            except Exception as exc:
                if stopped():
                    return
                store.event(session_id,"target_scoring_retry",{"forecast_id":forecast["forecast_id"],
                    "error_type":type(exc).__name__,"message":str(exc)},symbol=symbol)
            finally:
                drain_retained_targets()
        pause(3)


def score_saved_session_outcomes(store, *, session_id, actor, symbols, as_of, stopped=lambda: False):
    """Finalize persisted own outcomes using unchanged-original revision proofs."""
    from .final_scoring import score_saved_session_outcomes as finalize
    return finalize(store, session_id=session_id, actor=actor, symbols=symbols,
                    as_of=as_of, stopped=stopped)

def _scout_startup_proof(executor, session_id, *, clock):
    """Require the authenticated executor's bound result, never a local bypass."""
    from .scout_runtime_gate import readiness_gate
    proof = executor.preflight()
    if (not isinstance(proof, Mapping) or proof.get("session_id") != session_id
            or "readiness_receipt" not in proof or "authority_binding" not in proof):
        raise ContractError("Scout startup requires authenticated, protocol-compatible authority bound to this participant and session")
    verified = readiness_gate(proof["readiness_receipt"], proof["authority_binding"], now=clock())
    return {**{key:verified[key] for key in ("ready", "authenticated", "protocol_compatible", "participant_id", "stream")},
            "protocol":verified["protocol_version"], "readiness_expires_at":verified["expires_at"],
            "writers":{name:{key:writer[key] for key in ("ready", "compatibility_state", "proof_sha256")}
                       for name,writer in verified["writers"].items()}}


def _market_context_failure_diagnostics(collector, error):
    """Optional collector observations cannot replace the original worker error."""
    diagnostics = _symbol_exception_diagnostics(error)
    try:
        from .market_collection import collection_failure_diagnostics
        diagnostics["market_collection"] = collection_failure_diagnostics(collector, error)
    except BaseException:
        pass
    return diagnostics


def run_manual(*, repository, datastore, profile, watchlist, config, clock=utcnow, sleep=time.sleep,
               scout_admission="broker-local", model_recipe=None, lifecycle=None):
    from .data import AcquisitionJournal, load_local_history
    from .exact_status import retry_delay
    from app.services.schwab import SchwabSession
    from app.services.databento_market_data import DatabentoMarketDataProvider
    from dotenv import load_dotenv
    root, repository = Path(datastore),Path(repository)
    pc,symbols = local_symbols(profile,watchlist,root)
    if pc not in {"Atlas", "Scout"}:
        raise ContractError("Manual execution requires an explicitly bound Atlas or Scout participant")
    if scout_admission not in {"broker-local", "atlas-service"}:
        raise ContractError("Unknown Scout admission mode")
    if lifecycle is not None and (pc != "Scout" or scout_admission != "broker-local"):
        raise ContractError("Iteration lifecycle is exclusively Scout broker-local")
    config.validate()
    if config.source_dataset != "EQUS.MINI":
        raise ContractError("Existing single-source live provider binding required")
    if pc == "Atlas":
        from .account_execution import IntradayAccountExecutor
    started = instant(lifecycle.started_at) if lifecycle is not None else instant(clock())
    wake_at = instant(lifecycle.wake_at) if lifecycle is not None else manual_wake_at(started)
    session_id = lifecycle.session_id if lifecycle is not None else "manual-" + pc.lower() + "-15m:" + uuid.uuid4().hex
    process_create_time = psutil.Process(os.getpid()).create_time()
    stopped = threading.Event()
    automatic_stop_at = (instant(lifecycle.deadline) if lifecycle is not None else
                         automatic_stop_deadline(max(started, wake_at)) if pc == "Scout" else None)
    lifecycle_stop = [None]
    planned_restart = [False]
    iteration_ready = threading.Event()
    boundary_cycle = [None]
    def check_lifecycle():
        if lifecycle is not None:
            reason = lifecycle.stop_reason()
            if reason is not None:
                lifecycle_stop[0] = reason
                if reason == "session_end" and automatic_stop_requested_at[0] is None:
                    automatic_stop_requested_at[0] = instant(clock()).isoformat()
        return lifecycle_stop[0]
    automatic_stop_requested_at = [None]
    stop_lock = threading.Lock()
    def should_stop():
        if check_lifecycle() is not None:
            return True
        if stopped.is_set() or automatic_stop_requested_at[0] is not None:
            return True
        if automatic_stop_at is not None:
            now = instant(clock())
            if now >= automatic_stop_at:
                with stop_lock:
                    if not stopped.is_set():
                        automatic_stop_requested_at[0] = now.isoformat()
                return True
        return False
    startup_in_progress = [True]
    manual_stop_requested = [False]
    def execution_should_stop():
        requested = should_stop()
        if requested and startup_in_progress[0] and automatic_stop_requested_at[0] is not None:
            raise InterruptedError("Scout manual session reached its 5pm Pacific stop")
        return requested
    worker_errors = []
    def stop_reason():
        return (check_lifecycle() or ("worker_failure" if worker_errors else "manual_stop" if manual_stop_requested[0]
                else "automatic_17_00_pacific" if automatic_stop_requested_at[0] is not None
                else "planned_iteration_restart" if planned_restart[0] else "manual_stop"))
    store = ShadowStore(root / "intraday/shadow.sqlite3")
    status_path = root / "intraday/manual-session.json"
    session_root = root / "intraday/sessions" / session_id.replace(":", "-")
    session_status = session_root / "status.json"
    log_path = session_root / "events.jsonl"
    evidence_paths = {"status":str(status_path), "session_status":str(session_status),
                      "log":str(log_path), "forecasts":str(store.path),
                      "acquisition":str(root / "intraday/acquisition.sqlite3")}
    log_lock = threading.Lock()
    previous_status = [None]
    def emit(value):
        event = {"recorded_at":instant(clock()).isoformat(),"session_id":session_id,"pc":pc,
                 **({"generation": lifecycle.generation, "loaded_release": lifecycle.release} if lifecycle is not None else {}), **value}
        with log_lock:
            with log_path.open("a",encoding="utf-8") as log:
                log.write(encoded(event) + "\n")
        print(json.dumps(event),flush=True)
    queue = ExecutionQueue(store,session_id,pc=pc,event_sink=emit)
    def status(state, **extra):
        value = dict(schema_version=VERSION if pc == "Atlas" else SCOUT_VERSION,session_id=session_id,pc=pc,
                     pid=os.getpid(),process_create_time=process_create_time,started_at=started.isoformat(),
                     heartbeat_at=instant(clock()).isoformat(),status=state,manual_start=True,
                     wake_at=wake_at.isoformat(),
                      evidence_paths=evidence_paths,scout_admission=scout_admission if pc == "Scout" else None,
                      automatic_stop_at=automatic_stop_at.isoformat() if automatic_stop_at is not None else None,**extra)
        if lifecycle is not None:
            value.update(generation=lifecycle.generation, loaded_release=lifecycle.release,
                         logical_run_id=session_id, application_managed_restarts=True)
        destinations = [session_status, status_path]
        if lifecycle is not None:
            destinations.append(session_root / ("generation-%06d.json" % lifecycle.generation))
        for destination in destinations:
            if lifecycle is not None:
                # Windows readers can briefly deny replacement. Retry only the
                # native sharing errors, with a fixed publication deadline.
                from tools.scout_release import atomic_json
                atomic_json(destination, value)
            else:
                temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
                temporary.write_text(encoded(value),encoding="utf-8")
                os.replace(temporary,destination)
        if previous_status[0] != state:
            emit({"event":"session_status","status":state,**extra})
            previous_status[0] = state
    def manual_authority():
        if should_stop():
            return False
        try:
            value = json.loads(status_path.read_text(encoding="utf-8"))
            return (value["session_id"] == session_id and value["pc"] == pc
                    and value["pid"] == os.getpid() and value["process_create_time"] == process_create_time
                    and value["status"] == "RUNNING" and not stopped.is_set())
        except (OSError,ValueError,KeyError):
            return False
    def pause_with_stop(seconds, stop):
        end = time.monotonic() + max(0,float(seconds))
        while time.monotonic() < end:
            if stop():
                raise InterruptedError("Manual session stopped; acquisition recovery retained")
            sleep(min(1,max(0,end-time.monotonic())))
    def pause(seconds):
        pause_with_stop(seconds, should_stop)
    def data_pause(seconds):
        # Preserve the latched day's final native window, published after5pm.
        # Session end revokes orders immediately, while the original latched
        # final native window may publish during the bounded data-only drain.
        pause_with_stop(seconds, lambda: stopped.is_set() or check_lifecycle() not in {None, "session_end"})
    # The controller retains this native lock across every worker and gap.
    # A lifecycle worker still checks its controller/generation on every effect.
    session_lock = (nullcontext() if lifecycle is not None else
        exclusive_runtime_lock(root / "locks/intraday-manual-session.lock",process_name="intraday-manual-session"))
    with session_lock:
        session_root.mkdir(parents=True,exist_ok=lifecycle is not None and lifecycle.generation > 1)
        status("STARTING")
        startup_stage = "wake"
        try:
            waiting = instant(clock()) < wake_at
            if waiting:
                print(f"{pc} 15m waiting until {wake_at.astimezone(PACIFIC).isoformat()} America/Los_Angeles. "
                      "Ctrl-C cancels this manual start; broker/data workers have not started.", flush=True)
            while instant(clock()) < wake_at:
                if should_stop():
                    raise InterruptedError("Scout manual session reached its 5pm Pacific stop")
                status("WAITING")
                sleep(min(60, max(0, (wake_at - instant(clock())).total_seconds())))
            if should_stop():
                raise InterruptedError("Scout manual session reached its 5pm Pacific stop")
            if waiting:
                status("STARTING")
            # Read overnight-installed recipes at wake, not
            # when the operator first launches the sleeping process.
            from .live_model_recipe import prepare_manual_models, native_interval_symbols_for_runtime
            startup_stage = "model_recipe"
            config, model_runtime = prepare_manual_models(config, datastore=root, pc=pc, symbols=symbols,
                **({"recipe_path": model_recipe} if model_recipe is not None else {}))
            native_interval_symbols = native_interval_symbols_for_runtime(model_runtime)
            if lifecycle is not None:
                from .iteration_boundary import validate_resume
                validate_resume(store, lifecycle=lifecycle, symbols=symbols, config=config)
                if should_stop():
                    raise InterruptedError("Logical trading run authority ended before broker startup")
            startup_stage = "local_configuration"
            load_dotenv(repository / ".env")
            if pc == "Scout":
                if scout_admission == "broker-local":
                    from .scout_local_execution import ScoutLocalAccountExecutor
                    executor_type = ScoutLocalAccountExecutor
                else:
                    from .scout_execution import ScoutAccountExecutor
                    executor_type = ScoutAccountExecutor
                startup_stage = "broker_session"
                broker = SchwabSession()
                startup_stage = "local_executor"
                executor = executor_type(repository=repository,profile=profile,datastore=root,store=store,
                    broker=broker,local_symbols=symbols,session_started_at=started,session_id=session_id,
                    clock=clock,manual_authority=manual_authority,stopped=execution_should_stop,
                    **({"resume_session": lifecycle.generation > 1} if lifecycle is not None else {}))
                startup_stage = "broker_preflight"
                if scout_admission == "broker-local":
                    startup = executor.preflight()
                    if (not isinstance(startup,Mapping) or startup.get("session_id") != session_id
                            or startup.get("mode") != "scout-local-broker-account" or startup.get("ready") is not True):
                        raise ContractError("Scout local broker-account preflight did not pass")
                else:
                    startup = _scout_startup_proof(executor,session_id,clock=clock)
                executor_paths = getattr(executor,"evidence_paths",{})
                if not isinstance(executor_paths,Mapping) or any(not isinstance(key,str) or not isinstance(value,str)
                        for key,value in executor_paths.items()):
                    raise ContractError("Scout executor evidence paths must be a string mapping")
                evidence_paths.update({"executor_" + key:value for key,value in executor_paths.items()})
                emit({"event":"authority_ready",**startup})
            startup_in_progress[0] = False
            startup_stage = "historical_inputs"
            histories = {}
            for symbol in symbols:
                if should_stop():
                    raise InterruptedError("Scout manual session reached its 5pm Pacific stop")
                try:
                    if symbol in native_interval_symbols:
                        from .native_interval import load_native_interval_history
                        histories[symbol] = load_native_interval_history(root,symbol,source_dataset=config.source_dataset,
                            pc=pc, recovery_contract=getattr(model_runtime.get("dataset_builder"), "historical_recovery_contract", None))
                    else:
                        histories[symbol] = load_local_history(root,symbol,source_dataset=config.source_dataset)
                except Exception as exc:
                    store.event(session_id,"history_unavailable",{"error_type":type(exc).__name__,"message":str(exc)},symbol=symbol)
            if should_stop():
                raise InterruptedError("Scout manual session reached its 5pm Pacific stop")
            from .minute_feed import QuarterHourMinuteFeed
            startup_stage = "minute_provider"
            provider = prepare_minute_provider(model_runtime, datastore=root, pc=pc,
                symbols=symbols, source_dataset=config.source_dataset, clock=clock)
            startup_stage = "market_collection"
            market_collector = prepare_market_collection(model_runtime, datastore=root,
                                                        pc=pc, symbols=symbols, clock=clock)
            startup_stage = "minute_feed"
            journal = AcquisitionJournal(root / "intraday/acquisition.sqlite3")
            minute_feed = QuarterHourMinuteFeed(provider=provider,store=store,journal=journal,histories=histories,
                pc=pc,symbols=symbols,session_id=session_id,clock=clock,
                publication_offset_seconds=config.publication_offset_seconds,
                native_interval_symbols=native_interval_symbols,
                reference_fill_policy=getattr(model_runtime.get("dataset_builder"), "reference_fill_policy", None),
                on_closed=lambda result: emit({"event":"minute_data_day_complete",**result}))
            startup_stage = "forecast_runner"
            runner = ShadowRunner(store=store,pc=pc,symbols=symbols,config=config,clock=clock,
                                  stopped=should_stop,**model_runtime)
            if pc == "Atlas":
                startup_stage = "local_executor"
                executor = IntradayAccountExecutor(datastore=root,store=store,broker=SchwabSession(),local_symbols=symbols,
                    session_started_at=started,clock=clock,manual_authority=manual_authority,stopped=should_stop,
                    actor=pc,diagnostic_session_id=session_id)
        except BaseException as exc:
            scheduled_stop = (isinstance(exc,InterruptedError) and
                              (automatic_stop_requested_at[0] is not None or
                               check_lifecycle() in {"manual_stop", "session_end"}))
            diagnostics = _symbol_exception_diagnostics(exc)
            stopped.set()
            status("STOPPED" if isinstance(exc,KeyboardInterrupt) or scheduled_stop else "FAILED",
                   stop_reason=stop_reason() if scheduled_stop else "manual_stop" if isinstance(exc,KeyboardInterrupt) else "startup_failure",
                   error_type=type(exc).__name__,error_message=str(exc),broker_orders_automatically_cancelled=False,
                   startup_stage=startup_stage,diagnostics=diagnostics)
            store.event(session_id,"manual_startup_stopped",{"error_type":type(exc).__name__,"message":str(exc),
                        "startup_stage":startup_stage,"diagnostics":diagnostics})
            if isinstance(exc,KeyboardInterrupt) or scheduled_stop:
                return {"session_id":session_id,"status":"STOPPED", "stop_reason": stop_reason()}
            raise
        if lifecycle is not None:
            if should_stop():
                status("STOPPED", stop_reason=stop_reason())
                return {"session_id": session_id, "status": "STOPPED", "stop_reason": stop_reason()}
            lifecycle.ready()
            if should_stop():
                status("STOPPED", stop_reason=stop_reason())
                return {"session_id": session_id, "status": "STOPPED", "stop_reason": stop_reason()}
        status("RUNNING")
        def iteration_finished(result):
            from .iteration_boundary import inspect_cycle
            if not isinstance(result, dict) or not result.get("cycle_id"):
                raise ContractError("Iteration returned without its durable cycle identity")
            inspection = inspect_cycle(store, result["cycle_id"], session_id=session_id, symbols=symbols)
            boundary_cycle[0] = result["cycle_id"]
            lifecycle.phase("iteration_execution", cycle_id=result["cycle_id"], cycle_status=inspection["status"])
            iteration_ready.set()
        def forecast_work():
            try:
                forecasting_loop(runner,histories=histories,provider=provider,journal=journal,queue=queue,
                                 stopped=should_stop,clock=clock,pause=pause,
                                 history_source=minute_feed.history_source,
                                 **({"on_iteration": iteration_finished, "recovery_first": (
                                     lifecycle.previous_checkpoint["recovery_first"] if lifecycle.generation > 1 else False)}
                                    if lifecycle is not None else {}))
            except BaseException as exc:
                store.event(session_id,"forecast_worker_stopped",{"error_type":type(exc).__name__,"message":str(exc)})
                if not (isinstance(exc,InterruptedError) and should_stop()):
                    worker_errors.append({"worker":"forecast","error_type":type(exc).__name__,"message":str(exc)})
                    stopped.set()
        worker = threading.Thread(target=forecast_work,name="intraday-manual-forecast",daemon=True)
        def score_work():
            try:
                scoring_loop(store,histories=histories,provider=provider,journal=journal,session_id=session_id,
                             stopped=should_stop,clock=clock,pause=pause,pc=pc,
                             history_source=minute_feed.scoring_history_source)
            except InterruptedError:
                pass
            except BaseException as exc:
                store.event(session_id,"scoring_worker_stopped",{"error_type":type(exc).__name__,"message":str(exc)})
                worker_errors.append({"worker":"scoring","error_type":type(exc).__name__,"message":str(exc)})
                stopped.set()
        scorer = threading.Thread(target=score_work,name="intraday-manual-scoring",daemon=True)
        def data_work():
            try:
                minute_feed.run(stopped=stopped.is_set,pause=data_pause)
            except InterruptedError:
                pass
            except BaseException as exc:
                store.event(session_id,"minute_data_worker_stopped",{"error_type":type(exc).__name__,"message":str(exc)})
                worker_errors.append({"worker":"minute_data","error_type":type(exc).__name__,"message":str(exc)})
                stopped.set()
        data_worker = threading.Thread(target=data_work,name="intraday-manual-minute-data",daemon=True)
        def context_work():
            if market_collector is None:
                return
            try:
                market_collector.run(stopped=should_stop, pause=pause,
                    on_retry=lambda detail: store.event(session_id, "market_context_queue_retry", detail),
                    on_recovery=lambda detail: store.event(session_id, "market_context_queue_recovered", detail))
            except InterruptedError:
                pass
            except BaseException as exc:
                # Provider adapters retain safe categories in their private
                # snapshots; authenticated exception URLs must not reach logs.
                diagnostics = _market_context_failure_diagnostics(market_collector, exc)
                store.event(session_id, "market_context_worker_stopped", {
                    "error_type":type(exc).__name__, "diagnostics":diagnostics})
                worker_errors.append({"worker":"market_context", "error_type":type(exc).__name__,
                    "message":"Inspect private collection status", "diagnostics":diagnostics})
                stopped.set()
        context_worker = (threading.Thread(target=context_work,name="intraday-manual-market-context",daemon=True)
                          if market_collector is not None else None)
        worker.start()
        scorer.start()
        data_worker.start()
        if context_worker is not None:
            context_worker.start()
        print(f"{pc} 15m manual session running. New predictions can submit real orders. Ctrl-C stops this worker; resting orders retain their broker identities.",flush=True)
        if automatic_stop_at is not None:
            print("Automatic graceful stop: 5:00pm America/Los_Angeles; active work drains and resting orders retain their identities.",flush=True)
        print(f"Session status: {status_path}\nSession log: {log_path}",flush=True)
        next_recovery = instant(clock())
        try:
            while not should_stop():
                status("RUNNING")
                for forecast in queue.due(clock()):
                    # A prior symbol in this snapshot may have just received a
                    # longer provider wait. No later symbol bypasses that wait.
                    if should_stop() or not queue.broker_due(clock()):
                        break
                    try:
                        result = executor.step(forecast)
                        store.event(session_id,"execution_step",result,symbol=forecast["symbol"])
                        emit({"event":"execution_disposition","symbol":forecast["symbol"],
                            "forecast_id":forecast["forecast_id"],
                            **{key:result.get(key) for key in ("side","desired_quantity","adjusted_quantity",
                                "disposition","submission_status","submitted_quantity","filled_quantity")}})
                        disposition = result["disposition"]
                        if disposition == "previous_intention_reconciliation" or result["evidence"].get("proven_unsent_retry_eligible"):
                            queue.defer(forecast["forecast_id"],now=clock(),seconds=result["evidence"].get("retry_after_seconds") or 3,broker_wait=True)
                        elif disposition == "submission_recovery_required":
                            # UNKNOWN is never requeued for submission. Status
                            # recovery and other symbols still honor its wait.
                            queue.broker_wait(now=clock(),seconds=result["evidence"].get("retry_after_seconds") or 3)
                            queue.complete(forecast["forecast_id"],disposition)
                        elif result["kind"] == "sizing_unavailable":
                            queue.complete(forecast["forecast_id"],"sizing_unavailable")
                        else:
                            queue.complete(forecast["forecast_id"],disposition)
                    except Exception as exc:
                        delay = retry_delay(exc,clock())
                        queue.defer(forecast["forecast_id"],now=clock(),seconds=delay,broker_wait=True)
                        store.event(session_id,"execution_retry",{"forecast_id":forecast["forecast_id"],
                            "error_type":type(exc).__name__,"message":str(exc),"retry_after_seconds":delay,
                            "diagnostics":_symbol_exception_diagnostics(exc)},symbol=forecast["symbol"])
                        break
                if not should_stop() and instant(clock()) >= next_recovery and queue.broker_due(clock()):
                    try:
                        executor.recover()
                        delay = 3
                    except Exception as exc:
                        delay = retry_delay(exc,clock())
                        queue.broker_wait(now=clock(),seconds=delay)
                        store.event(session_id,"order_recovery_retry",{"error_type":type(exc).__name__,
                            "message":str(exc),"retry_after_seconds":delay,
                            "diagnostics":_symbol_exception_diagnostics(exc)})
                    next_recovery = instant(clock()) + timedelta(seconds=delay)
                if (lifecycle is not None and iteration_ready.is_set() and not should_stop()
                        and not queue.due(clock())):
                    # All currently eligible steps finished; any wait is already
                    # durable in the queue/broker-retry tables. Resting/UNKNOWN
                    # orders retain their broker identities and reservations.
                    planned_restart[0] = True
                    lifecycle.phase("draining", cycle_id=boundary_cycle[0])
                    stopped.set()
                    break
                if not should_stop():
                    sleep(1)
        except KeyboardInterrupt:
            manual_stop_requested[0] = True
            stopped.set()
        except BaseException as exc:
            worker_errors.append({"worker":"execution","error_type":type(exc).__name__,"message":str(exc)})
            raise
        finally:
            if automatic_stop_requested_at[0] is None or worker_errors:
                stopped.set()
            if automatic_stop_requested_at[0] is not None:
                emit({"event":"automatic_stop_requested","stop_reason":"automatic_17_00_pacific",
                      "automatic_stop_at":automatic_stop_at.isoformat(),
                      "requested_at":automatic_stop_requested_at[0]})
            status("STOPPING",broker_orders_automatically_cancelled=False,
                   stop_reason=stop_reason())
            print("15m order authority stopped. Waiting for active computation to finish; pending broker identities are retained.",flush=True)
            drain_until = (time.monotonic() + float(lifecycle.drain_timeout_seconds)
                           if lifecycle is not None else None)
            while (worker.is_alive() or scorer.is_alive() or data_worker.is_alive()
                   or context_worker is not None and context_worker.is_alive()):
                if lifecycle is not None:
                    check_lifecycle()
                    if time.monotonic() >= drain_until:
                        worker_errors.append({"worker": "drain", "error_type": "TimeoutError",
                                              "message": "Owned workers did not exit before bounded drain deadline"})
                        break
                try:
                    worker.join(timeout=1)
                    scorer.join(timeout=1)
                    data_worker.join(timeout=1)
                    if context_worker is not None:
                        context_worker.join(timeout=1)
                    status("STOPPING",broker_orders_automatically_cancelled=False,
                           stop_reason=stop_reason())
                except KeyboardInterrupt:
                    manual_stop_requested[0] = True
                    stopped.set()
                    emit({"event":"manual_stop_requested","during":"worker_drain"})
            stopped.set()
            if lifecycle is not None and not worker_errors:
                # Close the original journal only after its users drained. The
                # next generation opens the same native identity and audit.
                owned_journal = getattr(executor, "journal", None)
                if owned_journal is not None:
                    try:
                        owned_journal.close()
                    except BaseException as exc:
                        worker_errors.append({"worker": "journal_close", "error_type": type(exc).__name__, "message": str(exc)})
            if (lifecycle is not None and planned_restart[0] and not worker_errors
                    and check_lifecycle() is None and not manual_stop_requested[0]
                    and automatic_stop_requested_at[0] is None):
                try:
                    from .iteration_boundary import capture_checkpoint
                    checkpoint = capture_checkpoint(store, lifecycle=lifecycle, symbols=symbols,
                                                    boundary_cycle_id=boundary_cycle[0])
                    lifecycle.checkpoint(checkpoint)
                except BaseException as exc:
                    worker_errors.append({"worker": "checkpoint", "error_type": type(exc).__name__, "message": str(exc)})
            final_state = ("FAILED" if worker_errors or stop_reason() == "controller_failure" else
                           "PLANNED_RESTART" if planned_restart[0] and stop_reason() == "planned_iteration_restart"
                           else "STOPPED")
            status(final_state,broker_orders_automatically_cancelled=False,
                   stop_reason=stop_reason(),worker_errors=worker_errors)
            store.event(session_id,"manual_session_stopped",{"pending_order_identity_preserved":True,
                        "stop_reason":stop_reason(),"worker_errors":worker_errors})
        return {"session_id":session_id,"status":final_state, "stop_reason":stop_reason(),
                **({"generation": lifecycle.generation} if lifecycle is not None else {})}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository",type=Path,default=Path(__file__).resolve().parents[2])
    parser.add_argument("--datastore",type=Path,default=Path("C:/DATASTORE"))
    parser.add_argument("--profile",type=Path)
    parser.add_argument("--watchlist",type=Path)
    parser.add_argument("--live",action="store_true")
    parser.add_argument("--scout-admission",choices=("broker-local","atlas-service"),default="broker-local",
        help="Scout defaults to local broker-account execution; atlas-service explicitly selects distributed admission")
    parser.add_argument("--publication-offset-seconds",type=float,default=30)
    parser.add_argument("--symbol-workers",type=int,default=1)
    parser.add_argument("--lookback-days",type=int)
    parser.add_argument("--model-recipe",type=Path,
        help="Explicit local manual model recipe; missing or incompatible selections fail")
    args = parser.parse_args(argv)
    profile = args.profile or args.repository / "scratch/cross-pc/local-profile.json"
    watchlist = args.watchlist or args.repository / "datafetching/watchlist.local.txt"
    if not args.live:
        scout_profile = profile.is_file() and json.loads(profile.read_text(encoding="utf-8")).get("actor") == "Scout"
        if scout_profile and args.scout_admission == "broker-local":
            from .scout_prestart import run_local_checks
            report = run_local_checks(args.repository,args.datastore)
        else:
            from .readiness import inspect_readiness
            report = inspect_readiness(repository=args.repository,datastore=args.datastore,
                profile=profile,watchlist=watchlist)
        print(json.dumps(report,sort_keys=True))
        if scout_profile and args.scout_admission == "broker-local":
            return 0 if report.get("ready") and report.get("local_checks_passed") else 2
        return 0 if report.get("manual_live_start_ready") else 2
    result = run_manual(repository=args.repository,datastore=args.datastore,profile=profile,watchlist=watchlist,
        scout_admission=args.scout_admission,model_recipe=args.model_recipe,
        config=ResearchConfig(publication_offset_seconds=args.publication_offset_seconds,
            symbol_workers=args.symbol_workers,lookback_days=args.lookback_days,use_supervised_models=True))
    print(json.dumps(result,sort_keys=True))
    return 1 if result["status"] == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
