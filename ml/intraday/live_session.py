"""Human-started Atlas or Scout 15m session. No scheduler or Gameplan activation.

The default command only reports readiness. --live is explicit manual operating
intent. Forecast/model work and serialized broker steps run independently, with
durable forecast identities, retry times and original targets.
"""
import argparse
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
from .prediction import ContractError, PACIFIC, STEP, digest, encoded, instant
from .runtime import ResearchConfig, ShadowRunner, LiveHistories, local_symbols, recorded_symbol_failures
from .store import ShadowStore, utcnow

VERSION = "manual-atlas-15m-session-v1"
SCOUT_VERSION = "manual-scout-15m-session-v1"


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
                     clock=utcnow, pause=time.sleep):
    """New quarters first; durable acquisition retries get bounded fair passes.

    Only missing data prerequisites retry automatically. Already issued model
    outputs remain immutable, and older recovered outputs never enter execution.
    No missed-quarter forecast or older fitted model is invented.
    """
    pc = getattr(queue, "pc", "Atlas")
    saved = _session_cycles(runner.store, queue.session_id, pc=pc)
    last = max((instant(c["reference_at"]) for _, c in saved), default=None)
    while not stopped():
        newest = latest_completed_reference(clock(), publication_offset_seconds=runner.config.publication_offset_seconds)
        retry_symbols = None
        if last is None or newest > last:
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
            pending = _pending_cycle_passes(runner.store, queue.session_id, clock(), pc=pc)
            if not pending:
                pause(1); continue
            _, cycle, retry_symbols = pending[0]
            reference = instant(cycle["reference_at"])
        incoming = LiveHistories(histories=histories,provider=provider,store=runner.store,journal=journal,
                                reference_at=reference,clock=clock,sleep=pause,bounded=True)
        result = runner.run_cycle(incoming,reference_at=reference,mode="live",experiment=queue.session_id,
                         on_published=queue.enqueue,acquisition={"kind":"existing_databento_exact_interval"},
                         retry_symbols=retry_symbols,allow_execution=reference == newest)
        if result and result.get("cycle_id"):
            runner.store.update_cycle(result["cycle_id"], {"last_recovery_attempt_at":instant(clock()).isoformat()})
        pause(1)


def scoring_loop(store, *, histories, provider, journal, session_id, stopped,
                 clock=utcnow, pause=time.sleep, pc="Atlas"):
    """Fetch exact due outcomes independently, including after the last entry.

    Acquisition retry evidence is durable. This never submits an order, changes
    an issued prediction or writes corrections into its original input snapshot.
    Local copies of the history mapping keep scoring out of the training writer.
    """
    from .data import synthetic_continuity_verified
    if pc not in {"Atlas", "Scout"}:
        raise ContractError("Explicit Atlas or Scout scoring participant required")
    audited = set()
    while not stopped():
        latest_observed = {}
        for candle in store.observed_candles():
            if not synthetic_continuity_verified(candle):
                key=digest(candle)
                if key not in audited:
                    store.event(session_id,"unverified_synthetic_outcome_requires_audit",
                                {"candle_hash":key,"immutable_original_retained":True},symbol=candle["symbol"])
                    audited.add(key)
                continue
            latest_observed[(candle["symbol"],candle.get("source_id"),instant(candle["opened_at"]))] = candle
        for forecast in store.forecasts(pc=pc):
            if stopped():
                return
            if forecast["mode"] != "live" or instant(forecast["target_close"]) > instant(clock()):
                continue
            revisions = store.revisions(forecast["forecast_id"])
            observed = latest_observed.get((forecast["symbol"],forecast.get("source_id"),instant(forecast["target_open"])))
            if observed is not None and instant(observed["available_at"]) <= instant(clock()):
                store.observe(forecast,as_of=clock(),candle=observed)
                continue
            if revisions and revisions[-1]["status"] in {"scored", "synthetic", "unscorable"}:
                continue
            symbol = forecast["symbol"]
            if symbol not in histories:
                continue
            incoming = LiveHistories(histories=dict(histories),provider=provider,store=store,journal=journal,
                reference_at=forecast["target_open"],clock=clock,sleep=pause,bounded=True)
            incoming.bind_cycle("score:" + forecast["forecast_id"])
            try:
                history = incoming.get(symbol)
                match = history.frame.loc[history.frame.opened_at.eq(instant(forecast["target_open"]))]
                if match.empty:
                    raise ContractError("Exact target interval unavailable after acquisition")
                from .runtime import _jsonable
                store.observe(forecast,as_of=clock(),candle=_jsonable(match.iloc[-1].to_dict()))
            except InterruptedError:
                if stopped():
                    return
                raise
            except Exception as exc:
                store.event(session_id,"target_scoring_retry",{"forecast_id":forecast["forecast_id"],
                    "error_type":type(exc).__name__,"message":str(exc)},symbol=symbol)
        pause(3)


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


def run_manual(*, repository, datastore, profile, watchlist, config, clock=utcnow, sleep=time.sleep):
    from .data import AcquisitionJournal, load_local_history
    from .cancellation import retry_delay
    from app.services.schwab import SchwabSession
    from app.services.databento_market_data import DatabentoMarketDataProvider
    from dotenv import load_dotenv
    root, repository = Path(datastore),Path(repository)
    pc,symbols = local_symbols(profile,watchlist,root)
    if pc not in {"Atlas", "Scout"}:
        raise ContractError("Manual execution requires an explicitly bound Atlas or Scout participant")
    config.validate()
    if config.source_dataset != "EQUS.MINI":
        raise ContractError("Existing single-source live provider binding required")
    if pc == "Atlas":
        from .account_execution import IntradayAccountExecutor
        from .runtime_compatibility import read_gameplan_runtime_compatibility
        def compatibility():
            return read_gameplan_runtime_compatibility(root,repository,as_of=clock(),
                maximum_heartbeat_age_seconds=120,allow_verified_absence=True)
        proof = compatibility()
        if not proof["ready"]:
            raise ContractError("Live startup requires compatible loaded Gameplan or verified absence: " + ",".join(proof["reason_codes"]))
    started = instant(clock())
    session_id = "manual-" + pc.lower() + "-15m:" + uuid.uuid4().hex
    process_create_time = psutil.Process(os.getpid()).create_time()
    stopped = threading.Event()
    worker_errors = []
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
        event = {"recorded_at":instant(clock()).isoformat(),"session_id":session_id,"pc":pc,**value}
        with log_lock:
            with log_path.open("a",encoding="utf-8") as log:
                log.write(encoded(event) + "\n")
        print(json.dumps(event),flush=True)
    queue = ExecutionQueue(store,session_id,pc=pc,event_sink=emit)
    def status(state, **extra):
        value = dict(schema_version=VERSION if pc == "Atlas" else SCOUT_VERSION,session_id=session_id,pc=pc,
                     pid=os.getpid(),process_create_time=process_create_time,started_at=started.isoformat(),
                     heartbeat_at=instant(clock()).isoformat(),status=state,manual_start=True,
                     evidence_paths=evidence_paths,**extra)
        for destination in (session_status,status_path):
            temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
            temporary.write_text(encoded(value),encoding="utf-8")
            os.replace(temporary,destination)
        if previous_status[0] != state:
            emit({"event":"session_status","status":state,**extra})
            previous_status[0] = state
    def manual_authority():
        try:
            value = json.loads(status_path.read_text(encoding="utf-8"))
            return (value["session_id"] == session_id and value["pc"] == pc
                    and value["pid"] == os.getpid() and value["process_create_time"] == process_create_time
                    and value["status"] == "RUNNING" and not stopped.is_set())
        except (OSError,ValueError,KeyError):
            return False
    def pause(seconds):
        end = time.monotonic() + max(0,float(seconds))
        while time.monotonic() < end:
            if stopped.is_set():
                raise InterruptedError("Manual session stopped; acquisition recovery retained")
            sleep(min(1,max(0,end-time.monotonic())))
    with exclusive_runtime_lock(root / "locks/intraday-manual-session.lock",process_name="intraday-manual-session"):
        session_root.mkdir(parents=True,exist_ok=False)
        status("STARTING")
        try:
            load_dotenv(repository / ".env")
            if pc == "Scout":
                from .scout_execution import ScoutAccountExecutor
                executor = ScoutAccountExecutor(repository=repository,profile=profile,datastore=root,store=store,
                    broker=SchwabSession(),local_symbols=symbols,session_started_at=started,session_id=session_id,
                    clock=clock,manual_authority=manual_authority,stopped=stopped.is_set)
                startup = _scout_startup_proof(executor,session_id,clock=clock)
                executor_paths = getattr(executor,"evidence_paths",{})
                if not isinstance(executor_paths,Mapping) or any(not isinstance(key,str) or not isinstance(value,str)
                        for key,value in executor_paths.items()):
                    raise ContractError("Scout executor evidence paths must be a string mapping")
                evidence_paths.update({"executor_" + key:value for key,value in executor_paths.items()})
                emit({"event":"authority_ready",**startup})
            histories = {}
            for symbol in symbols:
                try:
                    histories[symbol] = load_local_history(root,symbol,source_dataset=config.source_dataset)
                except Exception as exc:
                    store.event(session_id,"history_unavailable",{"error_type":type(exc).__name__,"message":str(exc)},symbol=symbol)
            provider = DatabentoMarketDataProvider()
            if provider.dataset != config.source_dataset:
                raise ContractError("Provider binding differs from selected single source")
            journal = AcquisitionJournal(root / "intraday/acquisition.sqlite3")
            runner = ShadowRunner(store=store,pc=pc,symbols=symbols,config=config,clock=clock,stopped=stopped.is_set)
            if pc == "Atlas":
                executor = IntradayAccountExecutor(datastore=root,store=store,broker=SchwabSession(),local_symbols=symbols,
                    session_started_at=started,clock=clock,manual_authority=manual_authority,compatibility=compatibility,stopped=stopped.is_set)
        except BaseException as exc:
            stopped.set()
            status("STOPPED" if isinstance(exc,KeyboardInterrupt) else "FAILED",
                   stop_reason="manual_stop" if isinstance(exc,KeyboardInterrupt) else "startup_failure",
                   error_type=type(exc).__name__,error_message=str(exc),broker_orders_automatically_cancelled=False)
            store.event(session_id,"manual_startup_stopped",{"error_type":type(exc).__name__,"message":str(exc)})
            if isinstance(exc,KeyboardInterrupt):
                return {"session_id":session_id,"status":"STOPPED"}
            raise
        status("RUNNING")
        def forecast_work():
            try:
                forecasting_loop(runner,histories=histories,provider=provider,journal=journal,queue=queue,
                                 stopped=stopped.is_set,clock=clock,pause=pause)
            except BaseException as exc:
                store.event(session_id,"forecast_worker_stopped",{"error_type":type(exc).__name__,"message":str(exc)})
                if not (isinstance(exc,InterruptedError) and stopped.is_set()):
                    worker_errors.append({"worker":"forecast","error_type":type(exc).__name__,"message":str(exc)})
                stopped.set()
        worker = threading.Thread(target=forecast_work,name="intraday-manual-forecast",daemon=True)
        def score_work():
            try:
                scoring_loop(store,histories=histories,provider=provider,journal=journal,session_id=session_id,
                             stopped=stopped.is_set,clock=clock,pause=pause,pc=pc)
            except InterruptedError:
                pass
            except BaseException as exc:
                store.event(session_id,"scoring_worker_stopped",{"error_type":type(exc).__name__,"message":str(exc)})
                worker_errors.append({"worker":"scoring","error_type":type(exc).__name__,"message":str(exc)})
                stopped.set()
        scorer = threading.Thread(target=score_work,name="intraday-manual-scoring",daemon=True)
        worker.start()
        scorer.start()
        print(f"{pc} 15m manual session running. New predictions can submit real orders. Ctrl-C stops this worker; resting orders retain their broker identities.",flush=True)
        print(f"Session status: {status_path}\nSession log: {log_path}",flush=True)
        next_recovery = instant(clock())
        try:
            while not stopped.is_set():
                status("RUNNING")
                for forecast in queue.due(clock()):
                    # A prior symbol in this snapshot may have just received a
                    # longer provider wait. No later symbol bypasses that wait.
                    if not queue.broker_due(clock()):
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
                            "error_type":type(exc).__name__,"message":str(exc),"retry_after_seconds":delay},symbol=forecast["symbol"])
                        break
                if instant(clock()) >= next_recovery and queue.broker_due(clock()):
                    try:
                        executor.recover()
                        delay = 3
                    except Exception as exc:
                        delay = retry_delay(exc,clock())
                        queue.broker_wait(now=clock(),seconds=delay)
                        store.event(session_id,"order_recovery_retry",{"error_type":type(exc).__name__,
                            "message":str(exc),"retry_after_seconds":delay})
                    next_recovery = instant(clock()) + timedelta(seconds=delay)
                sleep(1)
        except KeyboardInterrupt:
            stopped.set()
        except BaseException as exc:
            worker_errors.append({"worker":"execution","error_type":type(exc).__name__,"message":str(exc)})
            raise
        finally:
            stopped.set()
            status("STOPPING",broker_orders_automatically_cancelled=False,
                   stop_reason="worker_failure" if worker_errors else "manual_stop")
            print("15m order authority stopped. Waiting for active computation to finish; pending broker identities are retained.",flush=True)
            while worker.is_alive() or scorer.is_alive():
                worker.join(timeout=1)
                scorer.join(timeout=1)
                status("STOPPING",broker_orders_automatically_cancelled=False,
                       stop_reason="worker_failure" if worker_errors else "manual_stop")
            status("FAILED" if worker_errors else "STOPPED",broker_orders_automatically_cancelled=False,
                   stop_reason="worker_failure" if worker_errors else "manual_stop",worker_errors=worker_errors)
            store.event(session_id,"manual_session_stopped",{"pending_order_identity_preserved":True,
                        "stop_reason":"worker_failure" if worker_errors else "manual_stop","worker_errors":worker_errors})
        return {"session_id":session_id,"status":"FAILED" if worker_errors else "STOPPED"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository",type=Path,default=Path(__file__).resolve().parents[2])
    parser.add_argument("--datastore",type=Path,default=Path("C:/DATASTORE"))
    parser.add_argument("--profile",type=Path)
    parser.add_argument("--watchlist",type=Path)
    parser.add_argument("--live",action="store_true")
    parser.add_argument("--publication-offset-seconds",type=float,default=30)
    parser.add_argument("--symbol-workers",type=int,default=1)
    parser.add_argument("--lookback-days",type=int)
    args = parser.parse_args(argv)
    profile = args.profile or args.repository / "scratch/cross-pc/local-profile.json"
    watchlist = args.watchlist or args.repository / "datafetching/watchlist.local.txt"
    if not args.live:
        from .readiness import inspect_readiness
        print(json.dumps(inspect_readiness(repository=args.repository,datastore=args.datastore,
            profile=profile,watchlist=watchlist),sort_keys=True))
        return 2
    result = run_manual(repository=args.repository,datastore=args.datastore,profile=profile,watchlist=watchlist,
        config=ResearchConfig(publication_offset_seconds=args.publication_offset_seconds,
            symbol_workers=args.symbol_workers,lookback_days=args.lookback_days))
    print(json.dumps(result,sort_keys=True))
    return 1 if result["status"] == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
