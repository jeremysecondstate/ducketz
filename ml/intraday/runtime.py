"""Explicitly invoked research/shadow runner; no order or production worker hooks."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import time
import uuid

import numpy as np
import pandas as pd
import psutil

from .prediction import ContractError, PACIFIC, STEP, TARGET_VERSION, UnresolvedPolicy, digest, instant
from .store import ShadowStore, utcnow


@dataclass(frozen=True)
class ResearchConfig:
    models: tuple = ("logistic", "gaussian_nb", "knn", "lda", "random_forest", "extra_trees",
                     "hist_gradient_boosting", "gradient_boosting", "adaboost", "xgboost")
    lookback_days: int | None = None
    symbol_workers: int = 1
    publication_offset_seconds: float = 30.0
    synthetic_policy: str = "include"
    source_dataset: str = "EQUS.MINI"
    retraining_minutes: int = 15

    def validate(self):
        if self.retraining_minutes != 15:
            raise ContractError("Primary benchmark fully retrains every 15 minutes")
        if type(self.symbol_workers) is not int or self.symbol_workers < 1:
            raise ContractError("Explicit positive symbol worker count required")
        if not 0 <= self.publication_offset_seconds < 900:
            raise ContractError("Offset must be within one quarter-hour")
        if self.synthetic_policy not in {"include", "exclude"}:
            raise ContractError("Synthetic training inclusion must be explicit")
        if self.lookback_days is not None and (type(self.lookback_days) is not int or self.lookback_days <= 0):
            raise ContractError("Historical window must be positive or all available")
        return self


def local_symbols(profile_path, watchlist_path, datastore):
    """Read each PC's existing bindings. Never replace them with a document list."""
    profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
    binding = json.loads((Path(datastore) / "state/account-gameplan/config.json").read_text(encoding="utf-8"))
    watch = tuple(x.strip() for x in Path(watchlist_path).read_text(encoding="utf-8").splitlines()
                  if x.strip() and not x.lstrip().startswith("#"))
    machine = profile["machine"]
    symbols = tuple(binding["participants"][machine])
    if (binding["machine_id"] != machine or len(set(watch)) != len(watch) or
            set(watch) != set(symbols) or set(profile["symbols"]) != set(symbols)):
        raise ContractError("Local profile, watchlist and existing ownership disagree")
    return profile["actor"], symbols


def _frame(history):
    return history.frame if hasattr(history, "frame") else history


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray, pd.Index)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def resource_snapshot():
    process = psutil.Process()
    cpu = process.cpu_times()
    # Names and counts only; no account state, worker arguments or private logs.
    processes = [p.info["name"] for p in psutil.process_iter(["name"]) if p.info["name"]]
    gameplan_workers=0
    for worker in psutil.process_iter(["name","cmdline"]):
        try:
            command=" ".join(worker.info.get("cmdline") or [])
            if "python" in (worker.info.get("name") or "").lower() and any(
                marker in command for marker in ("ml.stock_trader.independent_session","ml.stock_trader.independent_runtime")):
                gameplan_workers+=1
        except (psutil.AccessDenied,psutil.NoSuchProcess):
            continue
    return dict(process_rss_bytes=process.memory_info().rss, process_cpu_seconds=cpu.user + cpu.system,
                system_available_memory_bytes=psutil.virtual_memory().available,
                system_cpu_percent=psutil.cpu_percent(interval=None), python_process_count=sum("python" in n.lower() for n in processes),
                process_count=len(processes),gameplan_worker_processes_observed=gameplan_workers)


def regime_measurements(snapshot):
    """Causal trailing observations; bins belong only to retrospective reporting."""
    rows=snapshot.get("inference",{}).get("snapshot",{}).get("candles",[])[-21:]
    if len(rows)<21:
        return {}
    close=np.asarray([float(r["close"]) for r in rows])
    changes=np.abs(np.diff(close))
    highs=np.asarray([float(r["high"]) for r in rows[1:]])
    lows=np.asarray([float(r["low"]) for r in rows[1:]])
    tr=np.maximum.reduce([highs-lows,np.abs(highs-close[:-1]),np.abs(lows-close[:-1])])
    span=float(highs.max()-lows.min())
    feature_map=dict(zip(snapshot["feature_names"],snapshot["inference_X"][0]))
    return {"volatility":feature_map.get("volatility20_observed"),
            "trend_strength":float(abs(close[-1]-close[0])/changes.sum()) if changes.sum() else 0.0,
            "choppiness":100*math.log10(float(tr.sum())/span)/math.log10(20) if span>0 and tr.sum()>0 else 100.0,
            "definition":"trailing20_observed_steps_efficiency_and_choppiness_v1"}


class ShadowRunner:
    def __init__(self, *, store, pc, symbols, config=None, artifact_root=None, clock=utcnow,
                 trainer=None, dataset_builder=None):
        self.store = store
        self.pc = pc
        self.symbols = tuple(symbols)
        if not self.symbols or len(set(self.symbols)) != len(self.symbols):
            raise ContractError("A nonempty unique local symbol scope is required")
        self.config = (config or ResearchConfig()).validate()
        self.artifact_root = Path(artifact_root) if artifact_root else self.store.path.parent / "models"
        self.clock = clock
        self.trainer = trainer
        self.dataset_builder = dataset_builder

    def run_cycle(self, histories, *, reference_at, mode="shadow", cutoff=None, experiment="primary",
                  acquisition=None, intent_by_symbol=None, on_published=None):
        """Publish each complete symbol independently. Failure/restart never reissues.

        A research invocation uses explicit worker count, no deadline cancellation,
        and no prior-model fallback. The primary runner always fits fresh models.
        Historical replay uses a separate simulated information clock and persists
        actual computation/recording time. It cannot masquerade as a live issue.
        """
        reference = instant(reference_at)
        if reference.minute % 15 or reference.second or reference.microsecond:
            raise ContractError("Reference must be a quarter-hour opening")
        if mode not in {"shadow", "historical_replay"}:
            raise ContractError("Explicit shadow or historical_replay mode required")
        started = instant(self.clock())
        scheduled = reference + STEP + timedelta(seconds=self.config.publication_offset_seconds)
        training_cutoff = (instant(cutoff) if cutoff else started if mode == "shadow" else
                           scheduled+timedelta(seconds=float((acquisition or {}).get("seconds",0))))
        if training_cutoff < reference + STEP or (mode == "shadow" and training_cutoff > started):
            raise ContractError("Training cutoff cannot precede completion or be in the future")
        configuration = _jsonable(asdict(self.config))
        configuration_hash = digest(configuration)
        cycle_id = digest(dict(pc=self.pc,reference_at=reference.isoformat(),configuration_hash=configuration_hash,
                               mode=mode,experiment=experiment))
        initial = dict(pc=self.pc,symbols=list(self.symbols),reference_at=reference.isoformat(),mode=mode,
                       experiment=experiment,configuration_hash=configuration_hash,configuration=configuration,
                       started_at=started.isoformat(),training_cutoff=training_cutoff.isoformat(),
                       candle_close=(reference+STEP).isoformat(),scheduled_acquisition=scheduled.isoformat(),
                       acquisition=acquisition or {},status="running",full_retrain=True,
                       scheduling_policy="explicit_research_workers_no_timeout_no_fallback",
                       overrun_policy="record_overrun_no_automatic_live_execution")
        saved = self.store.begin_cycle(cycle_id,initial)
        existing = {symbol: self.store.forecast(cycle_id, symbol) for symbol in self.symbols}
        reused = {symbol: forecast for symbol, forecast in existing.items() if forecast is not None}
        if saved.get("status") == "completed":
            if len(reused) != len(self.symbols):
                raise ContractError("Completed cycle is missing immutable forecasts")
            self.store.event(cycle_id,"completed_cycle_reused",{"reused_symbols":list(reused),
                             "retrained_symbols":0,"original_measurements_preserved":True})
            return {"cycle_id":cycle_id,**saved,"forecasts":reused,"recovered_existing":True}
        if hasattr(histories,"bind_cycle"):
            histories.bind_cycle(cycle_id)
        training_cutoff = instant(saved["training_cutoff"])
        start_perf = time.perf_counter()
        before = resource_snapshot()
        results, failures = {}, {}
        with ThreadPoolExecutor(max_workers=self.config.symbol_workers,thread_name_prefix="intraday-research") as pool:
            futures = {pool.submit(self._symbol, cycle_id, symbol, lambda s=symbol: histories.get(s), reference,
                        training_cutoff, mode, (intent_by_symbol or {}).get(symbol), on_published, start_perf): symbol for symbol in self.symbols}
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    results[symbol] = future.result()
                except Exception as exc:
                    error = {"type":type(exc).__name__,"message":str(exc),"recorded_at":instant(self.clock()).isoformat()}
                    failures[symbol] = error
                    self.store.event(cycle_id,"symbol_failure",error,symbol=symbol)
        elapsed = time.perf_counter()-start_perf
        after = resource_snapshot()
        end = instant(self.clock())
        acquisition_seconds=float((acquisition or {}).get("seconds",0.0))
        total_seconds=elapsed+acquisition_seconds
        attempt = dict(compute_persist_seconds=elapsed,acquisition_seconds=acquisition_seconds,
                       total_cycle_seconds=total_seconds,completed_at=end.isoformat(),
                       reused_symbols=list(reused),retrained_symbols=[s for s in results if s not in reused],
                       failed_symbols=list(failures),resources_before=before,resources_after=after,
                       process_cpu_seconds=after["process_cpu_seconds"]-before["process_cpu_seconds"])
        self.store.event(cycle_id,"cycle_attempt_measured",attempt)
        cumulative_total = total_seconds + float(saved.get("total_cycle_seconds",0))
        summary = dict(status="completed" if not failures else "partial_failure",completed_at=end.isoformat(),
                       compute_persist_seconds=elapsed+float(saved.get("compute_persist_seconds",0)),
                       acquisition_seconds=acquisition_seconds+float(saved.get("acquisition_seconds",0)),
                       total_cycle_seconds=cumulative_total,successful_symbols=len(results),failed_symbols=len(failures),
                       symbols_published=list(results),failures=failures,resources_before=before,resources_after=after,
                       process_cpu_seconds=attempt["process_cpu_seconds"]+float(saved.get("process_cpu_seconds",0)),
                       measurement_scope="cumulative_attempt_work",attempt_count=int(saved.get("attempt_count",0))+1,
                       latest_attempt=attempt,resources_initial=saved.get("resources_initial",before),
                       exceeds_15m=cumulative_total>=900,overrun_seconds=max(0,cumulative_total-900))
        self.store.update_cycle(cycle_id,summary)
        return {"cycle_id":cycle_id,**summary,"forecasts":results}

    def _symbol(self,cycle_id,symbol,history,reference,cutoff,mode,intent,on_published,cycle_started_perf):
        existing=self.store.forecast(cycle_id,symbol)
        if existing:
            return existing
        local_boundary=(reference+STEP).astimezone(PACIFIC)
        edge=(local_boundary.hour,local_boundary.minute)
        if edge <= (4,0) or edge >= (16,45):
            if callable(history):
                history=history()
            if history is not None:
                frame=_frame(history)
                final=frame.loc[pd.to_datetime(frame.available_at,utc=True).le(pd.Timestamp(instant(self.clock()) if mode=="shadow" else cutoff))]
                self.store.freeze_inputs(cycle_id,symbol,{"status":"session_edge_policy_unresolved","reference_at":reference.isoformat(),
                    "next_session_target":None,"candles":json.loads(final.to_json(orient="records",date_format="iso"))})
            raise UnresolvedPolicy("04:00 opening and final16:45 cycle/submission contracts remain unresolved; inputs preserved")
        begun=time.perf_counter()
        frozen=self.store.inputs(cycle_id,symbol)
        if frozen is None:
            if callable(history):
                history=history()
            if history is None:
                raise ContractError("No history provided for configured local symbol")
            begun=time.perf_counter()
            if mode=="shadow":
                cutoff=instant(self.clock())
            from .data import build_dataset
            builder=self.dataset_builder or build_dataset
            dataset=builder(_frame(history),cutoff=cutoff,reference_at=reference,
                            lookback_days=self.config.lookback_days,synthetic_policy=self.config.synthetic_policy)
            inference=_jsonable(dataset.inference)
            if mode=="shadow" and inference.get("availability_kind")!="actual":
                raise ContractError("Current shadow cycle requires actually observed reference availability")
            snapshot={"X":_jsonable(np.asarray(dataset.X)),"y":_jsonable(np.asarray(dataset.y)),
                      "inference_X":_jsonable(np.asarray(dataset.inference_X)),"inference":inference,
                      "feature_names":list(dataset.feature_names),"label_available_at":_jsonable(dataset.label_available_at),
                      "reference_at":_jsonable(dataset.reference_at),"target_close_at":_jsonable(dataset.target_close_at),
                      "manifest":_jsonable(dataset.manifest),"cutoff":cutoff.isoformat(),
                      "source_manifest":_jsonable(getattr(history,"manifest",{}))}
            # Retain the actual observations used, rather than only transformed features.
            frame=_frame(history)
            available=pd.to_datetime(frame.available_at,utc=True)
            used=frame.loc[available.le(pd.Timestamp(cutoff)) & pd.to_datetime(frame.opened_at,utc=True).le(pd.Timestamp(reference))]
            snapshot["candles"]=json.loads(used.to_json(orient="records",date_format="iso"))
            frozen=self.store.freeze_inputs(cycle_id,symbol,snapshot)
        snapshot_hash,snapshot=frozen
        prep_seconds=time.perf_counter()-begun
        inf=snapshot["inference"]
        if instant(inf["reference_at"]) != reference:
            raise ContractError("Inference snapshot changed cycle reference")
        availability_basis="observed" if inf.get("availability_kind")=="actual" else "assumed_historical"
        if mode=="shadow" and availability_basis!="observed":
            raise ContractError("Current shadow cycle requires actually observed reference availability")
        from .models import fit_ensemble
        trainer=self.trainer or fit_ensemble
        attempt=uuid.uuid4().hex
        training_start=instant(self.clock())
        self.store.event(cycle_id,"training_started",{"attempt":attempt,"snapshot_hash":snapshot_hash,
                         "training_cutoff":snapshot["cutoff"],"validated_data_ready":training_start.isoformat()},symbol=symbol)
        fitted=trainer(np.asarray(snapshot["X"],dtype=float),np.asarray(snapshot["y"]),
                       np.asarray(snapshot["inference_X"],dtype=float),config=self.config.models,
                       target_version=TARGET_VERSION,training_cutoff=snapshot["cutoff"],
                       label_available_at=snapshot["label_available_at"],feature_names=snapshot["feature_names"],
                       artifact_dir=self.artifact_root,run_id=f"{cycle_id[:16]}-{symbol}-{attempt}",
                       reference_at=snapshot["reference_at"],target_close_at=snapshot["target_close_at"],
                       dataset_metadata=snapshot["manifest"])
        finished=instant(self.clock())
        self.store.event(cycle_id,"models_completed",{"attempt":attempt,"result":fitted},symbol=symbol)
        if not fitted["successful_count"]:
            raise ContractError("All enabled models failed; see persisted individual model failures")
        measured=time.perf_counter()-begun
        issued=finished if mode=="shadow" else cutoff+timedelta(seconds=time.perf_counter()-cycle_started_perf)
        payload={"pc":self.pc,"symbol":symbol,"cycle_id":cycle_id,"reference_at":reference.isoformat(),
                 "reference_open":str(inf["reference_open"]),"data_through":(reference+STEP).isoformat(),
                 "target_open":(reference+2*STEP).isoformat(),"target_close":(reference+3*STEP).isoformat(),
                 "information_available_at":inf["information_available_at"],"training_cutoff":snapshot["cutoff"],
                 "issued_at":issued.isoformat(),"recorded_at":finished.isoformat(),"target_version":TARGET_VERSION,
                 "mode":mode,"availability_basis":availability_basis,"snapshot_hash":snapshot_hash,
                 "source_dataset":inf.get("source_dataset"),"source_id":inf.get("source"),
                 "models":fitted["outputs"],"probability_not_down":fitted["probability_not_down"],
                 "configured_count":fitted["configured_count"],"successful_count":fitted["successful_count"],
                 "ensemble_status":"full" if fitted["complete"] else "incomplete",
                 "preprocessing_seconds":prep_seconds,"training_seconds":fitted["training_seconds"],
                 "inference_seconds":fitted["inference_seconds"],"symbol_seconds_before_persistence":measured,
                 "training_started_at":training_start.isoformat(),"training_completed_at":finished.isoformat(),
                 "issue_after_target":issued>=reference+3*STEP,"dataset_manifest":snapshot["manifest"],
                 "synthetic_policy":self.config.synthetic_policy,"features":dict(zip(snapshot["feature_names"],snapshot["inference_X"][0])),
                 "regime_measurements":regime_measurements(snapshot)}
        persistence_start=time.perf_counter()
        forecast=self.store.save_forecast(payload,intent=intent)
        self.store.event(cycle_id,"forecast_persisted",{"forecast_id":forecast["forecast_id"],
                         "persisted_at":instant(self.clock()).isoformat(),"persistence_seconds":time.perf_counter()-persistence_start},symbol=symbol)
        if on_published:
            on_published(forecast)
        return forecast

    def score_available(self,histories,*,as_of,mode=None):
        results=[]
        for forecast in self.store.forecasts(pc=self.pc):
            if mode is not None and forecast["mode"]!=mode:
                continue
            if instant(forecast["issued_at"])>instant(as_of):
                continue
            revisions=self.store.revisions(forecast["forecast_id"])
            if revisions and instant(revisions[-1]["observed_at"])>instant(as_of):
                # A retrospective experiment cannot overwrite later recorded
                # evidence or expose that future outcome at its earlier cutoff.
                continue
            history=histories.get(forecast["symbol"])
            candle=None
            if history is not None:
                frame=_frame(history)
                if forecast["mode"]=="shadow":
                    frame=frame.loc[frame.availability_kind.eq("actual")]
                match=frame.loc[(pd.to_datetime(frame.opened_at,utc=True)==pd.Timestamp(forecast["target_open"])) &
                                (pd.to_datetime(frame.available_at,utc=True)<=pd.Timestamp(instant(as_of)))]
                if len(match):
                    match=match.sort_values("available_at")
                    candle=_jsonable(match.iloc[-1].to_dict())
            results.append(self.store.observe(forecast,as_of=as_of,candle=candle))
        return results


class LiveHistories:
    """Read-only existing Databento access; original DATASTORE files stay intact."""
    def __init__(self,*,histories,provider,store,journal,reference_at,clock=utcnow,sleep=time.sleep):
        self.histories=histories
        self.provider=provider
        self.store=store
        self.journal=journal
        self.reference=instant(reference_at)
        self.clock=clock
        self.sleep=sleep
        self.cycle_id=None
        from .data import History
        for symbol,history in tuple(histories.items()):
            if history.manifest.get("source_dataset")!=provider.dataset:
                raise ContractError("Existing provider binding differs from selected single historical source")
            saved=store.observed_candles(symbol)
            if saved:
                if any(c.get("source_dataset")!=provider.dataset for c in saved):
                    raise ContractError("Stored observed candles have another source dataset")
                addition=pd.DataFrame(saved)
                for column in ("opened_at","available_at"):
                    addition[column]=pd.to_datetime(addition[column],utc=True)
                merged=pd.concat([_frame(history),addition],ignore_index=True).sort_values("available_at").drop_duplicates("opened_at",keep="last").sort_values("opened_at").reset_index(drop=True)
                merged.attrs["intraday_history"]=history.manifest
                histories[symbol]=History(merged,history.manifest)

    def bind_cycle(self,cycle_id):
        self.cycle_id=cycle_id

    def get(self,symbol):
        from .data import History,equity_sessions,recover_databento_candle
        history=self.histories[symbol]
        frame=_frame(history)
        sessions=equity_sessions(self.reference,self.reference+STEP)
        session=next((s for s in sessions if s.contains(self.reference)),None)
        if session is None:
            raise ContractError("Requested interval is outside a genuine eligible session")
        before=frame.loc[pd.to_datetime(frame.opened_at,utc=True)<pd.Timestamp(self.reference)]
        previous=_jsonable(before.iloc[-1].to_dict()) if len(before) else None
        begin=instant(self.clock());tick=time.perf_counter()
        result=recover_databento_candle(self.provider,symbol=symbol,opened_at=self.reference,
            journal=self.journal,session=session,previous=previous,sleep=self.sleep,clock=self.clock)
        if result.get("candle") is None:
            raise ContractError("Exact reference unavailable: "+str(result.get("reason",result.get("status"))))
        candle=result["candle"]
        self.store.record_candle(candle)
        addition=pd.DataFrame([candle])
        for column in ("opened_at","available_at"):
            addition[column]=pd.to_datetime(addition[column],utc=True)
        merged=pd.concat([frame,addition],ignore_index=True).sort_values("available_at").drop_duplicates("opened_at",keep="last").sort_values("opened_at").reset_index(drop=True)
        merged.attrs["intraday_history"]=history.manifest
        updated=History(merged,{**history.manifest,"current_reference_availability":"actual","source_dataset":self.provider.dataset})
        self.histories[symbol]=updated
        self.store.event(self.cycle_id,"acquisition_completed",{"request_started_at":begin.isoformat(),
            "validated_data_ready":instant(self.clock()).isoformat(),"first_confirmed_availability":candle["available_at"],
            "seconds":time.perf_counter()-tick,"status":result["status"],"reference_at":self.reference.isoformat()},symbol=symbol)
        return updated


def watch_shadow(runner,*,histories,provider,journal,start_reference,overrun_policy,clock=utcnow,sleep=time.sleep,max_cycles=None):
    """Manual research loop, fixed quarter-hour grid and durable cursor.

    The caller explicitly selects retain_due_cycles. Late cycles keep their
    original target and never load an older model. This is not a trading schedule.
    max_cycles is a bounded development-run control, never a provider retry cap.
    """
    if overrun_policy!="retain_due_cycles":
        raise UnresolvedPolicy("Choose an explicit research overrun policy")
    if max_cycles is not None and (type(max_cycles) is not int or max_cycles<1):
        raise ContractError("Bounded shadow cycle count must be a positive integer")
    reference=instant(start_reference)
    identity=digest({"pc":runner.pc,"start":reference.isoformat(),"config":_jsonable(asdict(runner.config))})
    with runner.store.connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS research_watch(id TEXT PRIMARY KEY,next_reference TEXT)")
        db.execute("INSERT OR IGNORE INTO research_watch VALUES(?,?)",(identity,reference.isoformat()))
        reference=instant(db.execute("SELECT next_reference FROM research_watch WHERE id=?",(identity,)).fetchone()[0])
    completed=0
    while max_cycles is None or completed<max_cycles:
        scheduled=reference+STEP+timedelta(seconds=runner.config.publication_offset_seconds)
        remaining=(scheduled-instant(clock())).total_seconds()
        if remaining>0:
            sleep(min(4.0,remaining));continue
        from .data import equity_sessions
        local=(reference+STEP).astimezone(PACIFIC)
        eligible=(4,0)<(local.hour,local.minute)<=(16,45) and any(s.contains(reference) for s in equity_sessions(reference,reference+STEP))
        if eligible:
            incoming=LiveHistories(histories=histories,provider=provider,store=runner.store,journal=journal,
                                  reference_at=reference,clock=clock,sleep=sleep)
            result=runner.run_cycle(incoming,reference_at=reference,mode="shadow",experiment="manual_shadow_watch",
                                    acquisition={"kind":"existing_databento_exact_interval"})
            runner.score_available(histories,as_of=clock(),mode="shadow")
            runner.store.event(identity,"watch_cycle_completed",{"cycle_id":result["cycle_id"],"reference_at":reference.isoformat()})
        else:
            runner.store.event(identity,"closed_or_unspecified_opening_slot",{"reference_at":reference.isoformat(),"no_forecast":True})
        reference+=STEP
        with runner.store.connect() as db:
            db.execute("UPDATE research_watch SET next_reference=? WHERE id=?",(reference.isoformat(),identity))
        completed+=1
    return {"watch_id":identity,"completed_cycles":completed,"next_reference":reference.isoformat()}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datastore",required=True,type=Path)
    parser.add_argument("--profile",required=True,type=Path)
    parser.add_argument("--watchlist",required=True,type=Path)
    parser.add_argument("--reference",required=True)
    parser.add_argument("--mode",choices=["historical_replay","shadow"],default="historical_replay")
    parser.add_argument("--lookback-days",type=int)
    parser.add_argument("--symbol-workers",type=int,default=1)
    parser.add_argument("--publication-offset-seconds",type=float,default=30)
    parser.add_argument("--experiment",default="primary")
    parser.add_argument("--watch",action="store_true",help="Manually run anchored shadow cycles; never installs a schedule")
    parser.add_argument("--overrun-policy",choices=["retain_due_cycles"])
    parser.add_argument("--cycles",type=int,help="Optional bounded shadow development run")
    args=parser.parse_args(argv)
    from .data import load_local_history
    pc,symbols=local_symbols(args.profile,args.watchlist,args.datastore)
    config=ResearchConfig(lookback_days=args.lookback_days,symbol_workers=args.symbol_workers,
                          publication_offset_seconds=args.publication_offset_seconds)
    start=time.perf_counter(); request_start=utcnow()
    histories={symbol:load_local_history(args.datastore,symbol,source_dataset=config.source_dataset) for symbol in symbols}
    acquisition={"kind":"existing_local_single_source_history","request_start":request_start,"request_end":utcnow(),
                 "seconds":time.perf_counter()-start,"provider_requests":0}
    runner=ShadowRunner(store=ShadowStore(args.datastore/"intraday/shadow.sqlite3"),pc=pc,symbols=symbols,config=config)
    if args.watch or args.mode=="shadow":
        from dotenv import load_dotenv
        from app.services.databento_market_data import DatabentoMarketDataProvider
        from .data import AcquisitionJournal
        load_dotenv(Path(__file__).resolve().parents[2]/".env")
        provider=DatabentoMarketDataProvider()
        if provider.dataset!=config.source_dataset:
            raise ContractError("Existing provider binding differs from selected single historical source")
        journal=AcquisitionJournal(args.datastore/"intraday/acquisition.sqlite3")
        if args.watch:
            result=watch_shadow(runner,histories=histories,provider=provider,journal=journal,start_reference=args.reference,
                                overrun_policy=args.overrun_policy, max_cycles=args.cycles)
            print(json.dumps(result,indent=2));return 0
        incoming=LiveHistories(histories=histories,provider=provider,store=runner.store,journal=journal,reference_at=args.reference)
    else:
        incoming=histories
    result=runner.run_cycle(incoming,reference_at=args.reference,mode=args.mode,experiment=args.experiment,acquisition=acquisition)
    score_time=utcnow() if args.mode=="shadow" else (instant(args.reference)+3*STEP+timedelta(seconds=config.publication_offset_seconds)).isoformat()
    scores=runner.score_available(histories,as_of=score_time,mode=args.mode)
    report={k:v for k,v in result.items() if k!="forecasts"}
    report["acquisition"]=acquisition
    report["scoring_status_counts"]={s:sum(o["status"]==s for o in scores) for s in {o["status"] for o in scores}}
    print(json.dumps(report,indent=2))
    return 1 if result["failed_symbols"] else 0


if __name__=="__main__":
    raise SystemExit(main())
