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
    use_supervised_models: bool = False

    def validate(self):
        if self.retraining_minutes != 15:
            raise ContractError("Primary benchmark fully retrains every 15 minutes")
        if type(self.use_supervised_models) is not bool:
            raise ContractError("Supervised model adoption must be an explicit boolean")
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
    """Validate research membership without creating or activating trading state.

    Only the installed local profile and its exact watchlist bind research.
    Other trader configuration is outside this runtime.
    """
    profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
    watch = tuple(x.strip() for x in Path(watchlist_path).read_text(encoding="utf-8").splitlines()
                  if x.strip() and not x.lstrip().startswith("#"))
    machine = profile["machine"]
    symbols = tuple(profile["symbols"])
    if (profile.get("actor") not in {"Atlas", "Scout"} or
            {"Atlas": "pc-original", "Scout": "pc-new"}.get(profile.get("actor")) != machine or
            not symbols or len(set(symbols)) != len(symbols) or
            not watch or len(set(watch)) != len(watch) or set(watch) != set(symbols)):
        raise ContractError("Local profile and watchlist research membership disagree")
    pinned_watchlist = profile.get("symbol_profile_path")
    if not pinned_watchlist or Path(pinned_watchlist).resolve() != Path(watchlist_path).resolve():
        raise ContractError("Research requires the installed profile's exact local watchlist")
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
    """Own process and aggregate host metrics without inspecting other traders."""
    process = psutil.Process()
    cpu = process.cpu_times()
    return dict(process_rss_bytes=process.memory_info().rss, process_cpu_seconds=cpu.user + cpu.system,
                system_available_memory_bytes=psutil.virtual_memory().available,
                system_cpu_percent=psutil.cpu_percent(interval=None))

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


def recorded_symbol_failures(store, cycle_id):
    """Latest immutable failure events also survive a missing cycle summary."""
    with store.connect() as db:
        rows=db.execute("SELECT symbol,payload FROM events WHERE cycle_id=? AND kind='symbol_failure' ORDER BY id",
                        (cycle_id,)).fetchall()
    return {symbol:json.loads(payload) for symbol,payload in rows}


def _dataset_scope(value):
    if not isinstance(value, dict) or not {"synthetic_policy", "lookback_days"} <= value.keys():
        raise ContractError("Frozen dataset scope is invalid")
    scope = {"synthetic_policy": value.get("synthetic_policy"), "lookback_days": value.get("lookback_days")}
    if (not isinstance(scope["synthetic_policy"], str) or scope["synthetic_policy"] not in {"include", "exclude"} or
            scope["lookback_days"] is not None and
            (type(scope["lookback_days"]) is not int or scope["lookback_days"] <= 0)):
        raise ContractError("Frozen dataset scope is invalid")
    return scope


def _frozen_supervised_policy(saved, *, symbols):
    policy = saved.get("supervised_model_policy")
    if not isinstance(policy, dict):
        raise ContractError("Supervised cycle is missing its frozen model policy")
    evidence = {key: value for key, value in policy.items() if key != "sha256"}
    if digest(evidence) != policy.get("sha256"):
        raise ContractError("Frozen supervised model policy hash changed")
    if "dataset_scope_by_symbol" not in policy:
        # A pre-scope cycle already used its original ResearchConfig population.
        # Its unfinished symbols must use that same population, including when
        # active legacy policies now derive a different scope for new cycles.
        configuration = saved.get("configuration")
        if not isinstance(configuration, dict):
            raise ContractError("Legacy supervised cycle is missing its original dataset configuration")
        scope = _dataset_scope(configuration)
        return {**policy, "dataset_scope_by_symbol": {symbol: dict(scope) for symbol in symbols}}
    if not isinstance(policy["dataset_scope_by_symbol"], dict):
        raise ContractError("Frozen supervised cycle dataset scopes are invalid")
    for symbol in symbols:
        if symbol not in policy["dataset_scope_by_symbol"]:
            raise ContractError("Frozen supervised cycle is missing a symbol dataset scope")
        _dataset_scope(policy["dataset_scope_by_symbol"][symbol])
    return policy


def _supervised_cycle_policy(store, cycle_id, *, pc, symbols, configuration):
    """Freeze selected specs, their policy identities and populations per cycle."""
    with store.connect() as db:
        row = db.execute("SELECT payload FROM cycles WHERE id=?", (cycle_id,)).fetchone()
    if row:
        return _frozen_supervised_policy(json.loads(row[0]), symbols=symbols)
    from .data import FEATURE_NAMES
    from .model_policy import load_active_policy
    policy = {"pc": pc, "feature_names": list(FEATURE_NAMES), "models_by_symbol": {},
              "dataset_scope_by_symbol": {}, "policy_identities_by_symbol": {}}
    default_scope = _dataset_scope(configuration)
    for symbol in symbols:
        selected = load_active_policy(store.path.parent / "supervision", pc=pc, symbol=symbol,
                                      feature_names=FEATURE_NAMES)
        policy["models_by_symbol"][symbol] = [asdict(spec) for spec in selected["models"]] if selected else None
        policy["dataset_scope_by_symbol"][symbol] = _dataset_scope(selected["dataset_scope"] if selected else default_scope)
        policy["policy_identities_by_symbol"][symbol] = selected["sha256"] if selected else None
    policy["sha256"] = digest(policy)
    return policy


class ShadowRunner:
    def __init__(self, *, store, pc, symbols, config=None, artifact_root=None, clock=utcnow,
                 trainer=None, dataset_builder=None, stopped=lambda: False):
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
        self.stopped = stopped

    def run_cycle(self, histories, *, reference_at, mode="shadow", cutoff=None, experiment="primary",
                  acquisition=None, intent_by_symbol=None, on_published=None, retry_symbols=None,
                  allow_execution=True):
        """Publish each complete symbol independently. Failure/restart never reissues.

        A research invocation uses explicit worker count, no deadline cancellation,
        and no prior-model fallback. The primary runner always fits fresh models.
        Historical replay uses a separate simulated information clock and persists
        actual computation/recording time. It cannot masquerade as a live issue.
        """
        reference = instant(reference_at)
        if reference.minute % 15 or reference.second or reference.microsecond:
            raise ContractError("Reference must be a quarter-hour opening")
        if mode not in {"shadow", "live", "historical_replay"}:
            raise ContractError("Explicit shadow, live or historical_replay mode required")
        started = instant(self.clock())
        scheduled = reference + STEP + timedelta(seconds=self.config.publication_offset_seconds)
        training_cutoff = (instant(cutoff) if cutoff else started if mode in {"shadow", "live"} else
                           scheduled+timedelta(seconds=float((acquisition or {}).get("seconds",0))))
        if training_cutoff < reference + STEP or (mode in {"shadow", "live"} and training_cutoff > started):
            raise ContractError("Training cutoff cannot precede completion or be in the future")
        configuration = _jsonable(asdict(self.config))
        configuration_hash = digest(configuration)
        cycle_id = digest(dict(pc=self.pc,reference_at=reference.isoformat(),configuration_hash=configuration_hash,
                               mode=mode,experiment=experiment))
        supervised_policy = (_supervised_cycle_policy(self.store, cycle_id, pc=self.pc, symbols=self.symbols,
                                                     configuration=configuration)
                             if self.config.use_supervised_models and mode == "live" else None)
        initial = dict(pc=self.pc,symbols=list(self.symbols),reference_at=reference.isoformat(),mode=mode,
                       experiment=experiment,configuration_hash=configuration_hash,configuration=configuration,
                       supervised_model_policy=supervised_policy,
                       started_at=started.isoformat(),training_cutoff=training_cutoff.isoformat(),
                       candle_close=(reference+STEP).isoformat(),scheduled_acquisition=scheduled.isoformat(),
                       acquisition=acquisition or {},status="running",full_retrain=True,
                       scheduling_policy="explicit_research_workers_no_timeout_no_fallback",
                       overrun_policy="latest_completed_no_backlog_orders" if mode == "live" else
                                      "record_overrun_no_automatic_live_execution")
        saved = self.store.begin_cycle(cycle_id,initial)
        supervised_policy = (_frozen_supervised_policy(saved, symbols=self.symbols)
                             if supervised_policy is not None else None)
        existing = {symbol: self.store.forecast(cycle_id, symbol) for symbol in self.symbols}
        reused = {symbol: forecast for symbol, forecast in existing.items() if forecast is not None}
        selected = set(self.symbols if retry_symbols is None else retry_symbols)
        if not selected <= set(self.symbols):
            raise ContractError("Recovery changed the configured symbol scope")
        if saved.get("status") == "completed":
            if len(reused) != len(self.symbols):
                raise ContractError("Completed cycle is missing immutable forecasts")
            self.store.event(cycle_id,"completed_cycle_reused",{"reused_symbols":list(reused),
                             "retrained_symbols":0,"original_measurements_preserved":True})
            for forecast in reused.values():
                self._published(forecast, on_published)
            return {"cycle_id":cycle_id,**saved,"forecasts":reused,"recovered_existing":True}
        if hasattr(histories,"bind_cycle"):
            histories.bind_cycle(cycle_id)
        training_cutoff = instant(saved["training_cutoff"])
        start_perf = time.perf_counter()
        before = resource_snapshot()
        results = {symbol: forecast for symbol, forecast in reused.items() if symbol not in selected}
        prior_failures={**saved.get("failures", {}), **recorded_symbol_failures(self.store, cycle_id)}
        failures = {symbol: error for symbol, error in prior_failures.items()
                    if symbol not in selected and symbol not in reused}
        with ThreadPoolExecutor(max_workers=self.config.symbol_workers,thread_name_prefix="intraday-research") as pool:
            futures = {pool.submit(self._symbol, cycle_id, symbol, lambda s=symbol: histories.get(s), reference,
                        training_cutoff, mode, (intent_by_symbol or {}).get(symbol), on_published, start_perf,
                        allow_execution, supervised_policy): symbol for symbol in self.symbols if symbol in selected}
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    results[symbol] = future.result()
                except Exception as exc:
                    from .data import AcquisitionPending
                    error = {"type":type(exc).__name__,"message":str(exc),"recorded_at":instant(self.clock()).isoformat()}
                    if isinstance(exc, AcquisitionPending):
                        error.update(retryable=True, reason=exc.reason,
                                     retry_after_seconds=exc.retry_after_seconds,
                                     retry_at=(instant(error["recorded_at"])+timedelta(seconds=exc.retry_after_seconds)).isoformat())
                    failures[symbol] = error
                    self.store.event(cycle_id,"symbol_failure",error,symbol=symbol)
                    # A crash between futures must not turn a known permanent
                    # model failure into an automatic data-prerequisite retry.
                    self.store.update_cycle(cycle_id,{"failures":dict(failures),
                        "pending_recovery_symbols":[s for s,e in failures.items() if e.get("retryable")]})
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
        cumulative_total = total_seconds + float(saved.get("cumulative_attempt_seconds",
            saved.get("compute_persist_seconds",0)+saved.get("acquisition_seconds",0)))
        wall_seconds=max(0.0,(end-instant(saved["started_at"])).total_seconds())
        total_cycle_seconds=wall_seconds if mode in {"live","shadow"} else cumulative_total
        summary = dict(status="completed" if not failures and len(results)==len(self.symbols) else "partial_failure",completed_at=end.isoformat(),
                       compute_persist_seconds=elapsed+float(saved.get("compute_persist_seconds",0)),
                       acquisition_seconds=acquisition_seconds+float(saved.get("acquisition_seconds",0)),
                       total_cycle_seconds=total_cycle_seconds,cumulative_attempt_seconds=cumulative_total,
                       successful_symbols=len(results),failed_symbols=len(failures),
                       symbols_published=list(results),failures=failures,resources_before=before,resources_after=after,
                       pending_recovery_symbols=[s for s,e in failures.items() if e.get("retryable")],
                       process_cpu_seconds=attempt["process_cpu_seconds"]+float(saved.get("process_cpu_seconds",0)),
                       measurement_scope="wall_elapsed_with_cumulative_attempt_work_v1" if mode in {"live","shadow"}
                           else "cumulative_attempt_work",attempt_count=int(saved.get("attempt_count",0))+1,
                       wall_elapsed_seconds=wall_seconds,wall_exceeds_15m=wall_seconds>=900,
                       wall_overrun_seconds=max(0,wall_seconds-900),
                       latest_attempt=attempt,resources_initial=saved.get("resources_initial",before),
                       exceeds_15m=total_cycle_seconds>=900,overrun_seconds=max(0,total_cycle_seconds-900))
        self.store.update_cycle(cycle_id,summary)
        return {"cycle_id":cycle_id,**summary,"forecasts":results}

    def _symbol(self,cycle_id,symbol,history,reference,cutoff,mode,intent,on_published,cycle_started_perf,
                allow_execution=True,supervised_policy=None):
        if self.stopped():
            raise InterruptedError("Manual stop before symbol work; no model or forecast substituted")
        existing=self.store.forecast(cycle_id,symbol)
        if existing:
            self._published(existing, on_published)
            return existing
        local_boundary=(reference+STEP).astimezone(PACIFIC)
        edge=(local_boundary.hour,local_boundary.minute)
        if edge <= (4,0) or edge >= (16,45):
            if callable(history):
                history=history()
            if history is not None:
                frame=_frame(history)
                final=frame.loc[pd.to_datetime(frame.available_at,utc=True).le(pd.Timestamp(instant(self.clock()) if mode in {"shadow", "live"} else cutoff))]
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
            if mode in {"shadow", "live"}:
                cutoff=instant(self.clock())
            from .data import build_dataset
            builder=self.dataset_builder or build_dataset
            scope = (supervised_policy["dataset_scope_by_symbol"][symbol] if supervised_policy is not None
                     else _dataset_scope(asdict(self.config)))
            dataset=builder(_frame(history),cutoff=cutoff,reference_at=reference,
                            **scope)
            inference=_jsonable(dataset.inference)
            if mode in {"shadow", "live"} and inference.get("availability_kind")!="actual":
                raise ContractError("Current shadow cycle requires actually observed reference availability")
            snapshot={"X":_jsonable(np.asarray(dataset.X)),"y":_jsonable(np.asarray(dataset.y)),
                      "inference_X":_jsonable(np.asarray(dataset.inference_X)),"inference":inference,
                      "feature_names":list(dataset.feature_names),"label_available_at":_jsonable(dataset.label_available_at),
                      "reference_at":_jsonable(dataset.reference_at),"target_close_at":_jsonable(dataset.target_close_at),
                      "manifest":_jsonable(dataset.manifest),"cutoff":cutoff.isoformat(),
                      "dataset_scope": dict(scope),
                      "source_manifest":_jsonable(getattr(history,"manifest",{}))}
            if supervised_policy is not None:
                selected_specs = supervised_policy["models_by_symbol"][symbol]
                if selected_specs is not None and list(dataset.feature_names) != supervised_policy["feature_names"]:
                    raise ContractError("Selected model policy features differ from the current dataset")
                snapshot["model_selection"] = {"models": selected_specs,
                    "policy_sha256": supervised_policy["sha256"],
                    "active_policy_sha256": supervised_policy.get("policy_identities_by_symbol", {}).get(symbol),
                    "selection": "supervised" if selected_specs is not None else "configured_defaults"}
            # Retain the actual observations used, rather than only transformed features.
            frame=_frame(history)
            available=pd.to_datetime(frame.available_at,utc=True)
            used=frame.loc[available.le(pd.Timestamp(cutoff)) & pd.to_datetime(frame.opened_at,utc=True).le(pd.Timestamp(reference))]
            if dataset.manifest.get("reference_fill_policy"):
                # Carry proofs bind microsecond receipt clocks and exact prices;
                # pandas' default JSON precision would invalidate that lineage.
                from .forward_fill import _clean
                snapshot["candles"] = _clean(used.to_dict(orient="records"))
            else:
                snapshot["candles"]=json.loads(used.to_json(orient="records",date_format="iso"))
            frozen=self.store.freeze_inputs(cycle_id,symbol,snapshot)
        snapshot_hash,snapshot=frozen
        if (snapshot.get("manifest", {}).get("preparation_only")
                or snapshot.get("inference", {}).get("preparation_only")
                or snapshot.get("manifest", {}).get("executable_forecast") is False
                or snapshot.get("inference", {}).get("executable_forecast") is False):
            raise ContractError("Non-trading preparation snapshots cannot publish forecasts")
        # Immutable inputs and their actual manifest take precedence on retry,
        # including snapshots created before dataset scope was recorded.
        fallback_scope = (supervised_policy["dataset_scope_by_symbol"][symbol] if supervised_policy is not None
                          else _dataset_scope(asdict(self.config)))
        dataset_scope = _dataset_scope({**fallback_scope, **snapshot.get("dataset_scope", {}),
                                       **{key: snapshot["manifest"][key] for key in fallback_scope
                                          if key in snapshot["manifest"]}})
        prep_seconds=time.perf_counter()-begun
        inf=snapshot["inference"]
        if instant(inf["reference_at"]) != reference:
            raise ContractError("Inference snapshot changed cycle reference")
        availability_basis="observed" if inf.get("availability_kind")=="actual" else "assumed_historical"
        if mode in {"shadow", "live"} and availability_basis!="observed":
            raise ContractError("Current shadow cycle requires actually observed reference availability")
        from .models import configured_models, fit_ensemble
        if self.stopped():
            raise InterruptedError("Manual stop before training; original snapshot preserved")
        trainer=self.trainer or fit_ensemble
        selected_specs = snapshot.get("model_selection", {}).get("models")
        model_config = configured_models(selected_specs) if selected_specs is not None else self.config.models
        attempt=uuid.uuid4().hex
        training_start=instant(self.clock())
        self.store.event(cycle_id,"training_started",{"attempt":attempt,"snapshot_hash":snapshot_hash,
                         "training_cutoff":snapshot["cutoff"],"validated_data_ready":training_start.isoformat()},symbol=symbol)
        fitted=trainer(np.asarray(snapshot["X"],dtype=float),np.asarray(snapshot["y"]),
                       np.asarray(snapshot["inference_X"],dtype=float),config=model_config,
                       target_version=TARGET_VERSION,training_cutoff=snapshot["cutoff"],
                       label_available_at=snapshot["label_available_at"],feature_names=snapshot["feature_names"],
                       artifact_dir=self.artifact_root,run_id=f"{cycle_id[:16]}-{symbol}-{attempt}",
                       reference_at=snapshot["reference_at"],target_close_at=snapshot["target_close_at"],
                       dataset_metadata={**snapshot["manifest"], **dataset_scope, "dataset_scope": dataset_scope,
                           **({"model_selection": snapshot["model_selection"]} if "model_selection" in snapshot else {})})
        finished=instant(self.clock())
        self.store.event(cycle_id,"models_completed",{"attempt":attempt,"result":fitted},symbol=symbol)
        if not fitted["successful_count"]:
            raise ContractError("All enabled models failed; see persisted individual model failures")
        measured=time.perf_counter()-begun
        issued=finished if mode in {"shadow", "live"} else cutoff+timedelta(seconds=time.perf_counter()-cycle_started_perf)
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
                 "model_selection":snapshot.get("model_selection"),
                 "execution_blocked_reason":"recovered_older_cycle" if not allow_execution else
                     "issue_after_target" if issued>=reference+3*STEP else None,
                 "synthetic_policy":dataset_scope["synthetic_policy"],"dataset_scope":dataset_scope,
                 "features":dict(zip(snapshot["feature_names"],snapshot["inference_X"][0])),
                 "regime_measurements":regime_measurements(snapshot)}
        if inf.get("native_interval_contract"):
            payload.update({key:inf[key] for key in
                ("feature_version", "feature_policy", "label_provenance", "native_interval_contract")})
        if inf.get("reference_fill_policy"):
            payload["reference_fill_policy"] = inf["reference_fill_policy"]
            payload["reference_price_basis"] = inf.get("reference_price_basis", "genuine_observation")
        reference_rows=[row for row in snapshot.get("candles", [])
                        if instant(row["opened_at"]) == reference]
        if reference_rows:
            candle=reference_rows[-1]
            payload["reference_provenance"]={
                "reference_at":reference.isoformat(),
                **{field:candle.get(field) for field in (
                    "observed_constituents", "synthetic_constituents", "synthetic",
                    "no_trade", "source_id", "availability_kind")}}
            if isinstance(candle.get("forward_fill_contract"), str):
                from .forward_fill import verified_reference
                if not verified_reference(candle):
                    raise ContractError("Frozen forward-filled reference provenance is invalid")
                payload["reference_provenance"].update(
                    forward_fill_contract=candle["forward_fill_contract"],
                    price_basis="forward_filled_last_genuine_close",
                    evidence=json.loads(candle["evidence_json"]),
                    availability_basis_meaning="Actual receipt of source-empty response; prices are carried, not newly observed")
        persistence_start=time.perf_counter()
        forecast=self.store.save_forecast(payload,intent=intent)
        self.store.event(cycle_id,"forecast_persisted",{"forecast_id":forecast["forecast_id"],
                         "persisted_at":instant(self.clock()).isoformat(),"persistence_seconds":time.perf_counter()-persistence_start},symbol=symbol)
        self._published(forecast, on_published)
        return forecast

    def _published(self, forecast, callback):
        if callback is None:
            return
        try:
            callback(forecast)
        except Exception as exc:
            # Execution/intention failure cannot turn an already issued real
            # forecast into a model failure, erase it, or trigger retraining.
            self.store.event(forecast["cycle_id"], "publication_consumer_failure",
                {"forecast_id": forecast["forecast_id"], "type": type(exc).__name__,
                 "message": str(exc), "forecast_preserved": True}, symbol=forecast["symbol"])

    def score_available(self,histories,*,as_of,mode=None):
        from .data import completed_15m_observation, synthetic_continuity_verified
        from .native_interval import candle_matches_forecast
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
            if revisions and revisions[-1]["status"] == "synthetic":
                self.store.invalidate_unverified_outcome(forecast["forecast_id"], as_of=as_of)
            history=histories.get(forecast["symbol"])
            candle=None
            if history is not None:
                frame=_frame(history)
                sources={field:forecast[field] for field in ("source_dataset","source_id") if forecast.get(field)}
                for field,expected in sources.items():
                    if field not in frame:
                        frame=frame.iloc[:0]
                        break
                    frame=frame.loc[frame[field].eq(expected)]
                if sources and frame.empty:
                    # Another source's history says nothing about this forecast's
                    # outcome availability and must not revise its saved evidence.
                    continue
                if forecast["mode"] in {"shadow", "live"}:
                    frame=frame.loc[frame.availability_kind.eq("actual")]
                match=frame.loc[(pd.to_datetime(frame.opened_at,utc=True)==pd.Timestamp(forecast["target_open"])) &
                                (pd.to_datetime(frame.available_at,utc=True)<=pd.Timestamp(instant(as_of)))]
                match=match.loc[[completed_15m_observation(row, from_frame=True) and synthetic_continuity_verified(row)
                                and candle_matches_forecast(row, forecast)
                                for row in match.to_dict("records")]]
                if len(match):
                    match=match.sort_values("available_at")
                    candle=_jsonable(match.iloc[-1].to_dict())
            results.append(self.store.observe(forecast,as_of=as_of,candle=candle))
        return results


class LiveHistories:
    """Read-only existing Databento access; original DATASTORE files stay intact."""
    def __init__(self,*,histories,provider,store,journal,reference_at,clock=utcnow,sleep=time.sleep,bounded=False,
                 stopped=lambda: False):
        self.histories=histories
        self.provider=provider
        self.store=store
        self.journal=journal
        self.reference=instant(reference_at)
        self.clock=clock
        self.sleep=sleep
        self.bounded=bounded
        self.stopped=stopped
        self.cycle_id=None
        for symbol in tuple(histories):
            self._refresh_symbol(symbol)

    def _refresh_symbol(self, symbol):
        from .data import History, completed_15m_observation, synthetic_continuity_verified
        histories = self.histories
        for symbol,history in ((symbol, histories[symbol]),):
            if history.manifest.get("source_dataset")!=self.provider.dataset:
                raise ContractError("Existing provider binding differs from selected single historical source")
            saved=self.store.observed_candles(symbol)
            saved=[c for c in saved if instant(c["available_at"]) <= instant(self.clock())]
            invalid=[c for c in saved if not synthetic_continuity_verified(c)]
            saved=[c for c in saved if synthetic_continuity_verified(c) and completed_15m_observation(c)]
            original=_frame(history)
            excluded=[index for index,row in original.loc[original.synthetic.eq(True)].iterrows()
                      if not synthetic_continuity_verified(_jsonable(row.to_dict()))]
            if invalid or excluded:
                self.store.event("live-history-provenance-audit","unverified_synthetic_excluded",
                    {"saved_candle_hashes":[digest(c) for c in invalid],
                     "history_rows_excluded":len(excluded),"immutable_originals_retained":True},symbol=symbol)
                original=original.drop(index=excluded).copy()
                histories[symbol]=history=History(original,history.manifest)
            eligible=[completed_15m_observation(row, from_frame=True) and instant(row["available_at"]) <= instant(self.clock())
                      for row in original.to_dict("records")]
            original=original.loc[eligible].copy()
            original.attrs["intraday_history"]=history.manifest
            histories[symbol]=history=History(original,history.manifest)
            if saved:
                if any(c.get("source_dataset")!=self.provider.dataset for c in saved):
                    raise ContractError("Stored observed candles have another source dataset")
                addition=pd.DataFrame(saved)
                for column in ("opened_at","available_at"):
                    addition[column]=pd.to_datetime(addition[column],utc=True)
                from .native_interval import merge_native_history_frames
                merged=merge_native_history_frames(_frame(history),addition)
                merged.attrs["intraday_history"]=history.manifest
                histories[symbol]=History(merged,history.manifest)

    def bind_cycle(self,cycle_id):
        self.cycle_id=cycle_id

    def get(self,symbol):
        from .data import AcquisitionPending
        while True:
            if self.stopped():
                raise InterruptedError("Live history stopped before acquisition")
            try:
                return self._get_step(symbol)
            except AcquisitionPending as exc:
                if self.stopped():
                    raise InterruptedError("Live history stopped before retry wait") from exc
                if self.bounded:
                    raise
                self.sleep(min(4.0, exc.retry_after_seconds))

    def _get_step(self,symbol):
        from .data import (History,equity_sessions,recover_databento_candle_step,
                           resolve_minute_candle,AcquisitionPending)
        self._refresh_symbol(symbol)
        history=self.histories[symbol]
        frame=_frame(history)
        sessions=equity_sessions(self.reference,self.reference+STEP)
        session=next((s for s in sessions if s.contains(self.reference)),None)
        if session is None:
            raise ContractError("Requested interval is outside a genuine eligible session")
        requested = self.reference
        while True:
            if self.stopped():
                raise InterruptedError("Live history stopped before predecessor recovery")
            before=frame.loc[pd.to_datetime(frame.opened_at,utc=True).lt(pd.Timestamp(requested))
                             & pd.to_datetime(frame.available_at,utc=True).le(pd.Timestamp(instant(self.clock())))]
            previous=_jsonable(before.iloc[-1].to_dict()) if len(before) else None
            adjacent = (previous is not None and instant(previous["opened_at"])+STEP == requested
                        and previous.get("session_id") == session.session_id
                        and previous.get("source_dataset") == self.provider.dataset
                        and previous.get("source_id") == f"databento:{self.provider.dataset}:ohlcv-1m")
            slot=dict(symbol=symbol,source_id=f"databento:{self.provider.dataset}:ohlcv-1m",
                      interval_open=requested.isoformat(),interval_close=(requested+STEP).isoformat())
            attempts=self.journal.responses(**slot)
            # Only confirmed leading omissions require an external close. Walk
            # their missing same-session predecessors using existing evidence;
            # this call still performs at most one bounded acquisition step.
            probe=resolve_minute_candle(attempts,session=session,previous=None) if attempts and not adjacent else {}
            if (probe.get("reason") == "no_prior_known_price" and session.contains(requested-STEP)):
                requested -= STEP
                continue
            break
        begin=instant(self.clock());tick=time.perf_counter()
        kwargs=dict(symbol=symbol,opened_at=requested,journal=self.journal,session=session,
                    previous=previous,clock=self.clock,stopped=self.stopped)
        result=recover_databento_candle_step(self.provider,**kwargs)
        if result.get("candle") is None:
            if result.get("status") == "retry":
                raise AcquisitionPending(result.get("reason", "retry"), result.get("retry_after_seconds", 4))
            raise ContractError("Exact reference unavailable: "+str(result.get("reason",result.get("status"))))
        candle=result["candle"]
        self.store.record_candle(candle)
        addition=pd.DataFrame([candle])
        for column in ("opened_at","available_at"):
            addition[column]=pd.to_datetime(addition[column],utc=True)
        from .native_interval import merge_native_history_frames
        merged=merge_native_history_frames(frame,addition)
        merged.attrs["intraday_history"]=history.manifest
        updated=History(merged,{**history.manifest,"current_reference_availability":"actual","source_dataset":self.provider.dataset})
        self.histories[symbol]=updated
        self.store.event(self.cycle_id,"acquisition_completed",{"request_started_at":begin.isoformat(),
            "validated_data_ready":instant(self.clock()).isoformat(),"first_confirmed_availability":candle["available_at"],
            "seconds":time.perf_counter()-tick,"status":result["status"],"reference_at":requested.isoformat(),
            "requested_reference_at":self.reference.isoformat()},symbol=symbol)
        if requested != self.reference:
            raise AcquisitionPending("verified_predecessor_recovered", 4)
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
                                  reference_at=reference,clock=clock,sleep=sleep,stopped=runner.stopped)
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
    parser.add_argument("--source-dataset",choices=["EQUS.MINI","XNAS.ITCH"],default="EQUS.MINI",
                        help="One explicit historical source; sources are never merged")
    parser.add_argument("--symbol-workers",type=int,default=1)
    parser.add_argument("--publication-offset-seconds",type=float,default=30)
    parser.add_argument("--experiment",default="primary")
    parser.add_argument("--watch",action="store_true",help="Manually run anchored shadow cycles; never installs a schedule")
    parser.add_argument("--overrun-policy",choices=["retain_due_cycles"])
    parser.add_argument("--cycles",type=int,help="Optional bounded shadow development run")
    args=parser.parse_args(argv)
    if args.source_dataset != "EQUS.MINI" and (args.watch or args.mode in {"shadow", "live"}):
        raise ContractError("Archive source selection is historical replay only; existing shadow provider binding stays unchanged")
    from .data import load_local_history
    pc,symbols=local_symbols(args.profile,args.watchlist,args.datastore)
    config=ResearchConfig(lookback_days=args.lookback_days,symbol_workers=args.symbol_workers,source_dataset=args.source_dataset,
                          publication_offset_seconds=args.publication_offset_seconds)
    start=time.perf_counter(); request_start=utcnow()
    histories={symbol:load_local_history(args.datastore,symbol,source_dataset=config.source_dataset) for symbol in symbols}
    acquisition={"kind":"existing_local_single_source_history","request_start":request_start,"request_end":utcnow(),
                 "seconds":time.perf_counter()-start,"provider_requests":0}
    runner=ShadowRunner(store=ShadowStore(args.datastore/"intraday/shadow.sqlite3"),pc=pc,symbols=symbols,config=config)
    if args.watch or args.mode in {"shadow", "live"}:
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
    score_time=utcnow() if args.mode in {"shadow", "live"} else (instant(args.reference)+3*STEP+timedelta(seconds=config.publication_offset_seconds)).isoformat()
    scores=runner.score_available(histories,as_of=score_time,mode=args.mode)
    report={k:v for k,v in result.items() if k!="forecasts"}
    report["acquisition"]=acquisition
    report["scoring_status_counts"]={s:sum(o["status"]==s for o in scores) for s in {o["status"] for o in scores}}
    print(json.dumps(report,indent=2))
    return 1 if result["failed_symbols"] else 0


if __name__=="__main__":
    raise SystemExit(main())
