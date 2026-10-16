from datetime import datetime, timedelta, timezone
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
import sys
import threading
import json

import numpy as np
import pandas as pd
import pytest

from ml.intraday.prediction import ContractError
from ml.intraday.runtime import ResearchConfig, ShadowRunner
from ml.intraday.store import ShadowStore

REF = datetime(2026,10,8,17,tzinfo=timezone.utc)


def research_binding(tmp_path):
    watch = tmp_path / "watchlist.local.txt"
    watch.write_text("AAA\nBBB\n")
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(dict(actor="Scout", machine="pc-new", symbols=["AAA", "BBB"],
                                       symbol_profile_path=str(watch))))
    return profile, watch


def test_research_membership_without_account_config_is_read_only(tmp_path):
    from ml.intraday.runtime import local_symbols
    profile, watch = research_binding(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert local_symbols(profile, watch, tmp_path) == ("Scout", ("AAA", "BBB"))
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("change", ["path", "symbols", "duplicate_watch", "duplicate_profile", "actor", "machine"])
def test_research_membership_rejects_unbound_or_conflicting_scope(tmp_path, change):
    from ml.intraday.runtime import local_symbols
    profile, watch = research_binding(tmp_path)
    saved = json.loads(profile.read_text())
    if change == "path": saved["symbol_profile_path"] = str(tmp_path / "other.txt")
    if change == "symbols": saved["symbols"] = ["AAA", "CCC"]
    if change == "duplicate_watch": watch.write_text("AAA\nBBB\nAAA\n")
    if change == "duplicate_profile": saved["symbols"].append("AAA")
    if change == "actor": saved["actor"] = "Other"
    if change == "machine": saved["machine"] = "pc-original"
    profile.write_text(json.dumps(saved))
    with pytest.raises(ContractError): local_symbols(profile, watch, tmp_path)


def test_other_trader_configuration_cannot_change_local_research_membership(tmp_path):
    from ml.intraday.runtime import local_symbols
    profile, watch = research_binding(tmp_path)
    binding = tmp_path / "state/account-gameplan/config.json"
    binding.parent.mkdir(parents=True)
    for content in ('invalid json', '{"machine_id":"pc-original"}'):
        binding.write_text(content)
        assert local_symbols(profile, watch, tmp_path) == ("Scout", ("AAA", "BBB"))
        assert binding.read_text() == content


@pytest.mark.parametrize("mode", [["--mode", "shadow"], ["--watch"]])
def test_archive_selection_cannot_change_shadow_provider_binding(tmp_path, mode):
    from ml.intraday.runtime import main
    with pytest.raises(ContractError, match="historical replay only"):
        main(["--datastore", str(tmp_path), "--profile", "absent", "--watchlist", "absent",
              "--reference", REF.isoformat(), "--source-dataset", "XNAS.ITCH", *mode])


def history(symbol):
    return pd.DataFrame([dict(symbol=symbol,opened_at=REF,available_at=REF+timedelta(minutes=15),
                              availability_kind="actual",interval_seconds=900,open=100,high=101,low=99,close=100,volume=100,synthetic=False)])


def dataset(frame,*,cutoff,reference_at,**kwargs):
    return SimpleNamespace(X=np.array([[0],[1],[0],[1]]),y=np.array([0,1,0,1]),inference_X=np.array([[1]]),
        feature_names=("x",),label_available_at=pd.date_range(REF-timedelta(hours=2),periods=4,freq="15min"),
        reference_at=pd.date_range(REF-timedelta(hours=3),periods=4,freq="15min"),
        target_close_at=pd.date_range(REF-timedelta(hours=2,minutes=15),periods=4,freq="15min"),
        inference=dict(symbol=frame.iloc[0].symbol,reference_at=reference_at.isoformat(),reference_open=100,
                       information_available_at=(REF+timedelta(minutes=15)).isoformat(),availability_kind="actual"),manifest={"training_rows":4})


def trained(*args,config,**kwargs):
    return dict(outputs=[dict(name=getattr(name,"name",name),status="success",probability_not_down=.6,version="fixture",training_seconds=0,inference_seconds=0) for name in config],
                configured_count=len(config),successful_count=len(config),complete=True,probability_not_down=.6,training_seconds=0,inference_seconds=0)


def runner(tmp_path,**kwargs):
    return ShadowRunner(store=ShadowStore(tmp_path/"shadow.sqlite3"),pc="Atlas",symbols=("AAPL","IONQ"),
        config=ResearchConfig(models=("fixture_a","fixture_b"),symbol_workers=2),
        dataset_builder=dataset,clock=lambda:(REF+timedelta(minutes=16)).isoformat(),**kwargs)


def test_manual_stop_skips_queued_symbol_work_with_explicit_failures(tmp_path):
    r=runner(tmp_path,trainer=lambda *a,**k:pytest.fail("training after stop"),stopped=lambda:True)
    result=r.run_cycle({},reference_at=REF,mode="live")
    assert not result["forecasts"] and len(result["failures"])==2
    assert all("Manual stop" in value["message"] for value in result["failures"].values())


def test_live_history_restore_excludes_unverified_synthetic_without_mutating_originals(tmp_path):
    from ml.intraday.data import History, IntervalResponse, SessionBounds, resolve_interval
    from ml.intraday.prediction import STEP, digest
    from ml.intraday.runtime import LiveHistories
    store = ShadowStore(tmp_path/"synthetic-audit.sqlite3")
    source = "databento:EQUS.MINI:ohlcv-1m"
    session = SessionBounds("2026-10-08", "2026-10-08T08:00:00Z", "2026-10-09T00:00:00Z")
    def row(opened_at, *, synthetic=False):
        return dict(symbol="AAPL",opened_at=opened_at,available_at=opened_at+STEP,
            availability_kind="actual",open=100,high=100 if synthetic else 101,
            low=100 if synthetic else 99,close=100,volume=0 if synthetic else 100,
            synthetic=synthetic,no_trade=synthetic,interval_seconds=900,source_id=source,source_dataset="EQUS.MINI",
            session_id=session.session_id,session_open=session.opened_at,session_close=session.closed_at,
            evidence_json='{"legacy_confirmations":2}' if synthetic else "{}")
    original = pd.DataFrame([row(REF-3*STEP),row(REF-2*STEP,synthetic=True),row(REF-STEP)])
    original.attrs["original-evidence"] = "retained"
    before = original.copy(deep=True)
    history = History(original,{"source_dataset":"EQUS.MINI","historical-evidence":"retained"})
    saved_old = row(REF-4*STEP,synthetic=True)
    saved_observed = row(REF)
    def attempt(index):
        op=REF+STEP
        return IntervalResponse(str(index),"AAPL",source,"EQUS.MINI",op.isoformat(),(op+STEP).isoformat(),
            (op+STEP+timedelta(seconds=5*index)).isoformat(),
            (op+STEP+timedelta(seconds=5*index+1)).isoformat(),"success",(),op.isoformat(),
            (op+STEP).isoformat(),"successful covered provider omission")
    valid_synthetic = resolve_interval([attempt(1),attempt(2)],session=session,
                                      previous=saved_observed)["candle"]
    old_hash=store.record_candle(saved_old)
    store.record_candle(saved_observed)
    store.record_candle(valid_synthetic)
    persisted_before=store.observed_candles("AAPL")
    histories={"AAPL":history}
    LiveHistories(histories=histories,provider=SimpleNamespace(dataset="EQUS.MINI"),store=store,
        journal=None,reference_at=REF+2*STEP,clock=lambda:(REF+timedelta(hours=1)).isoformat(),
        sleep=lambda seconds:pytest.fail("Restore must not acquire data"))
    admitted=histories["AAPL"].frame
    assert set(admitted.opened_at)=={pd.Timestamp(REF-3*STEP),pd.Timestamp(REF-STEP),
                                     pd.Timestamp(REF),pd.Timestamp(REF+STEP)}
    assert admitted.loc[admitted.opened_at.eq(REF+STEP),"synthetic"].all()
    assert not admitted.loc[admitted.opened_at.eq(REF),"synthetic"].any()
    pd.testing.assert_frame_equal(original,before)
    assert original.attrs==before.attrs and history.frame is original
    assert store.observed_candles("AAPL")==persisted_before
    with store.connect() as db:
        audits=db.execute("SELECT symbol,payload FROM events WHERE kind='unverified_synthetic_excluded'").fetchall()
        assert db.execute("SELECT count(*) FROM candles").fetchone()[0]==3
    assert len(audits)==1 and audits[0][0]=="AAPL"
    evidence=json.loads(audits[0][1])
    assert evidence["history_rows_excluded"]==1 and evidence["immutable_originals_retained"] is True
    assert evidence["saved_candle_hashes"]==[old_hash]
    assert old_hash==digest(next(c for c in persisted_before if c["synthetic"] and "synthetic_continuity" not in c))


def test_live_publication_consumer_failure_preserves_forecast_and_retries_without_refitting(tmp_path):
    calls = []
    def train(*args, **kwargs):
        calls.append(kwargs["run_id"])
        return trained(*args, **kwargs)
    r = runner(tmp_path,trainer=train)
    def broken(forecast):
        raise RuntimeError("fixture downstream execution consumer failed")
    first = r.run_cycle({s:history(s) for s in r.symbols},reference_at=REF,mode="live",on_published=broken)
    assert first["failed_symbols"] == 0 and first["successful_symbols"] == 2
    assert len(calls) == 2
    delivered = []
    second = r.run_cycle({},reference_at=REF,mode="live",on_published=delivered.append)
    assert len(calls) == 2 and len(delivered) == 2
    assert first["forecasts"] == second["forecasts"]
    assert all(f["mode"] == "live" and f["issued_at"] == (REF+timedelta(minutes=16)).isoformat() for f in delivered)
    with r.store.connect() as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='publication_consumer_failure'").fetchone()[0] == 2


def test_per_symbol_publication_does_not_wait_for_slow_model_and_restart_reuses(tmp_path):
    published=threading.Event()
    calls=[]
    def train(*args,run_id,**kwargs):
        calls.append(run_id)
        if "IONQ" in run_id:
            assert published.wait(10),"AAPL did not publish before IONQ finished"
        return trained(*args,**kwargs)
    r=runner(tmp_path,trainer=train)
    results=r.run_cycle({s:history(s) for s in r.symbols},reference_at=REF,
        on_published=lambda f: published.set() if f["symbol"]=="AAPL" else None)
    assert results["successful_symbols"]==2
    assert len(calls)==2
    repeat=r.run_cycle({s:history(s) for s in r.symbols},reference_at=REF)
    assert repeat["forecasts"]==results["forecasts"] and len(calls)==2
    assert repeat["completed_at"]==results["completed_at"]
    assert repeat["total_cycle_seconds"]==results["total_cycle_seconds"]
    assert repeat["recovered_existing"] is True


def test_model_failures_are_durable_without_fake_forecast_or_stale_fallback(tmp_path):
    def fail(*args,run_id,**kwargs):
        if "IONQ" in run_id:
            return dict(outputs=[dict(name="a",status="failed",error="fit failed")],successful_count=0)
        return trained(*args,**kwargs)
    r=runner(tmp_path,trainer=fail)
    result=r.run_cycle({s:history(s) for s in r.symbols},reference_at=REF)
    assert result["failed_symbols"]==1
    assert r.store.forecast(result["cycle_id"],"IONQ") is None
    with r.store.connect() as db:
        assert db.execute("SELECT count(*) FROM events WHERE symbol='IONQ' AND kind='models_completed'").fetchone()[0]==1
    r.trainer=trained
    resumed=r.run_cycle({s:history(s) for s in r.symbols},reference_at=REF)
    assert resumed["successful_symbols"]==2
    assert resumed["forecasts"]["AAPL"]==result["forecasts"]["AAPL"]
    assert resumed["attempt_count"]==2
    assert resumed["total_cycle_seconds"]>=result["total_cycle_seconds"]
    assert resumed["latest_attempt"]["reused_symbols"]==["AAPL"]
    assert resumed["latest_attempt"]["retrained_symbols"]==["IONQ"]


def test_three_pending_symbols_resume_full_models_without_reissuing_ready_forecast(tmp_path):
    from ml.intraday.data import AcquisitionPending
    now=[REF+timedelta(minutes=16)]
    fits=[]
    symbols=("AAPL","CROX","PATH","TWST")
    pending={"CROX","PATH","TWST"}
    class Incoming:
        def get(self,symbol):
            if symbol in pending:
                raise AcquisitionPending("unverified_prior_price_continuity",4)
            return history(symbol)
    def train(*args,run_id,**kwargs):
        fits.append(run_id)
        return trained(*args,**kwargs)
    r=ShadowRunner(store=ShadowStore(tmp_path/"resume.sqlite3"),pc="Atlas",symbols=symbols,
        config=ResearchConfig(symbol_workers=1),dataset_builder=dataset,trainer=train,clock=lambda:now[0])
    first=r.run_cycle(Incoming(),reference_at=REF,mode="live",experiment="manual")
    frozen=json.dumps(first["forecasts"]["AAPL"],sort_keys=True)
    assert set(first["pending_recovery_symbols"])=={"CROX","PATH","TWST"} and len(fits)==1
    assert all(error["retryable"] for error in first["failures"].values())
    now[0]+=timedelta(minutes=20)
    pending.clear()
    result=r.run_cycle(Incoming(),reference_at=REF,mode="live",experiment="manual",
                       retry_symbols=("CROX","PATH","TWST"))
    assert result["status"]=="completed" and result["attempt_count"]==2 and len(fits)==4
    assert json.dumps(result["forecasts"]["AAPL"],sort_keys=True)==frozen
    for symbol in ("CROX","PATH","TWST"):
        forecast=result["forecasts"][symbol]
        assert forecast["configured_count"]==forecast["successful_count"]==10
        assert forecast["issued_at"]==now[0].isoformat() and forecast["target_close"]==(REF+3*timedelta(minutes=15)).isoformat()
    assert not result["pending_recovery_symbols"]
    assert result["wall_elapsed_seconds"]==1200 and result["wall_exceeds_15m"]
    assert result["wall_overrun_seconds"]==300 and result["exceeds_15m"]
    assert result["total_cycle_seconds"]==1200 and result["overrun_seconds"]==300
    assert result["cumulative_attempt_seconds"]<900 and result["measurement_scope"]=="wall_elapsed_with_cumulative_attempt_work_v1"


def test_selective_data_recovery_does_not_automatically_retry_model_failure(tmp_path):
    from ml.intraday.data import AcquisitionPending
    fits=[]
    def train(*args,run_id,**kwargs):
        fits.append(run_id)
        if "IONQ" in run_id:
            return {"outputs":[],"successful_count":0}
        return trained(*args,**kwargs)
    r=runner(tmp_path,trainer=train)
    class Pending:
        def get(self,symbol):
            if symbol=="AAPL": raise AcquisitionPending("unavailable_coverage",4)
            return history(symbol)
    first=r.run_cycle(Pending(),reference_at=REF,mode="live")
    assert first["pending_recovery_symbols"]==["AAPL"]
    # Simulate a crash after durable failure events and before their cycle map.
    r.store.update_cycle(first["cycle_id"],{"status":"running","failures":{}})
    result=r.run_cycle({s:history(s) for s in r.symbols},reference_at=REF,mode="live",retry_symbols=["AAPL"])
    assert len(fits)==2 and result["failed_symbols"]==1 and result["failures"]["IONQ"]==first["failures"]["IONQ"]
    assert result["status"]=="partial_failure" and r.store.forecast(first["cycle_id"],"IONQ") is None
    assert not result["pending_recovery_symbols"]


@pytest.mark.parametrize("external_prior", [True,False])
def test_live_history_refreshes_late_prior_or_acquires_exact_predecessor_once_per_pass(tmp_path,monkeypatch,external_prior):
    from ml.intraday import data,runtime
    from tests.test_intraday_data import candles,_bounded_attempt
    reference=pd.Timestamp("2026-05-11T14:00:00Z")
    now=[pd.Timestamp("2026-05-11T15:00:00Z")]
    frame=candles(1)
    frame["interval_seconds"]=900
    frame["source_id"]="databento:EQUS.MINI:ohlcv-1m"
    store=ShadowStore(tmp_path/"late.sqlite3")
    journal=data.AcquisitionJournal(tmp_path/"acquisition.sqlite3")
    reads=[]
    provider=SimpleNamespace(dataset="EQUS.MINI")
    def fetch(provider,**kwargs):
        reads.append(pd.Timestamp(kwargs["opened_at"]))
        return _bounded_attempt(provider,**kwargs,empty=pd.Timestamp(kwargs["opened_at"])==reference)
    monkeypatch.setattr(data,"fetch_databento_interval_attempt",fetch)
    live=runtime.LiveHistories(histories={"AAPL":data.History(frame,{"source_dataset":"EQUS.MINI"})},
        provider=provider,store=store,journal=journal,reference_at=reference,clock=lambda:now[0],bounded=True)
    with pytest.raises(data.AcquisitionPending): live.get("AAPL")
    if external_prior:
        prior=candles(1,start=(reference-timedelta(minutes=15)).isoformat()).iloc[0].to_dict()
        prior.update(source_id="databento:EQUS.MINI:ohlcv-1m",interval_seconds=900,available_at=now[0]+timedelta(seconds=2),
                     open=77.0,high=78.0,low=76.0,close=77.0)
        store.record_candle(runtime._jsonable(prior))
    now[0]+=timedelta(seconds=4)
    if not external_prior:
        with pytest.raises(data.AcquisitionPending): live.get("AAPL")
        now[0]+=timedelta(seconds=4)
        with pytest.raises(data.AcquisitionPending,match="verified_predecessor_recovered"): live.get("AAPL")
        now[0]+=timedelta(seconds=4)
    result=live.get("AAPL")
    current=result.frame.loc[result.frame.opened_at.eq(reference)].iloc[0]
    assert current.available_at==now[0] and current.synthetic and data.synthetic_continuity_verified(current.to_dict())
    assert reads==([reference,reference] if external_prior else [reference,reference,reference-timedelta(minutes=15),reference])
    if external_prior: assert current.open==77.0
    assert frame.opened_at.iloc[0]==reference-timedelta(minutes=30)  # Original history stays intact.


def test_every_quarter_fresh_fits_and_explicit_replay_is_separate(tmp_path):
    calls=[]
    def train(*args,**kwargs):
        calls.append(kwargs["training_cutoff"])
        return trained(*args,**kwargs)
    r=runner(tmp_path,trainer=train)
    for offset in (0,15,30):
        r.run_cycle({s:history(s) for s in r.symbols},reference_at=REF+timedelta(minutes=offset),mode="historical_replay")
    assert len(calls)==6
    assert len(r.store.forecasts())==6
    assert all(f["mode"]=="historical_replay" and f["recorded_at"] for f in r.store.forecasts())
    with pytest.raises(ContractError,match="15 minutes"):
        ResearchConfig(retraining_minutes=60).validate()


def test_failed_cycle_retains_snapshot_after_restart_and_acquisition_in_total(tmp_path):
    def crashed(*a,**k):raise RuntimeError("interrupted fit")
    r=runner(tmp_path,trainer=crashed)
    result=r.run_cycle({s:history(s) for s in r.symbols},reference_at=REF)
    assert result["failed_symbols"]==2
    r.trainer=trained
    r.dataset_builder=lambda *a,**k: (_ for _ in ()).throw(AssertionError("snapshot should be reused"))
    class AlreadyAcquired:
        def get(self,symbol):
            raise AssertionError("Validated frozen inputs must not require another provider request")
    result=r.run_cycle(AlreadyAcquired(),reference_at=REF,acquisition={"seconds":2})
    assert result["successful_symbols"]==2
    assert result["cumulative_attempt_seconds"]>=2 and result["acquisition_seconds"]==2
    assert result["total_cycle_seconds"]==result["wall_elapsed_seconds"]


def test_shadow_modules_have_no_order_client_dependency():
    import ast
    for name in ("runtime.py","store.py","models.py","evaluation.py"):
        path=Path(__file__).parents[1]/"ml/intraday"/name
        tree=ast.parse(path.read_text())
        imports=[node.module or "" for node in ast.walk(tree) if isinstance(node,ast.ImportFrom)]
        imports += [alias.name for node in ast.walk(tree) if isinstance(node,ast.Import) for alias in node.names]
        assert not any(any(word in item for word in ("schwab","stock_trader","intraday.execution","intraday.book")) for item in imports)


def test_earlier_replay_scoring_does_not_backdate_or_expose_later_outcomes(tmp_path):
    r=runner(tmp_path,trainer=trained)
    r.run_cycle({s:history(s) for s in r.symbols},reference_at=REF)
    later=(REF+timedelta(hours=2)).isoformat()
    assert len(r.score_available({},as_of=later))==2
    before=[r.store.revisions(f['forecast_id']) for f in r.store.forecasts()]
    assert r.score_available({},as_of=(REF+timedelta(minutes=46)).isoformat())==[]
    assert [r.store.revisions(f['forecast_id']) for f in r.store.forecasts()]==before


@pytest.mark.parametrize("other_dataset,other_source", [
    ("XNAS.ITCH", "databento:XNAS.ITCH:ohlcv-1m"),
    ("EQUS.MINI", "other:EQUS.MINI:ohlcv-1m"),
])
def test_scoring_keeps_same_symbol_targets_separate_by_source(tmp_path,other_dataset,other_source):
    r=runner(tmp_path,trainer=trained)
    def sourced_dataset(frame,**kwargs):
        built=dataset(frame,**kwargs)
        built.inference.update(source_dataset=frame.iloc[0].source_dataset,source=frame.iloc[0].source_id)
        return built
    r.dataset_builder=sourced_dataset
    sources=[("EQUS.MINI","databento:EQUS.MINI:ohlcv-1m"),(other_dataset,other_source)]
    forecasts=[]
    outcomes=[]
    for index,(source_dataset,source_id) in enumerate(sources):
        inputs={s:history(s).assign(source_dataset=source_dataset,source_id=source_id) for s in r.symbols}
        result=r.run_cycle(inputs,reference_at=REF,experiment=f"source-{index}")
        forecasts.append(result["forecasts"])
        outcomes.append({s:frame.assign(opened_at=REF+timedelta(minutes=30),
            available_at=REF+timedelta(minutes=45),close=99 if index==0 else 101) for s,frame in inputs.items()})
    as_of=(REF+timedelta(minutes=46)).isoformat()
    first=r.score_available(outcomes[0],as_of=as_of)
    assert len(first)==len(r.symbols) and all(o["target_not_down"] is False for o in first)
    saved={s:r.store.revisions(f["forecast_id"]) for s,f in forecasts[0].items()}
    assert all(not r.store.revisions(f["forecast_id"]) for f in forecasts[1].values())
    second=r.score_available(outcomes[1],as_of=as_of)
    assert len(second)==len(r.symbols) and all(o["target_not_down"] is True for o in second)
    assert saved=={s:r.store.revisions(f["forecast_id"]) for s,f in forecasts[0].items()}
    # A mixed frame must select the matching source before choosing its latest row.
    mixed={s:pd.concat([outcomes[0][s],outcomes[1][s]],ignore_index=True) for s in r.symbols}
    all_scores=r.score_available(mixed,as_of=as_of)
    assert len(all_scores)==2*len(r.symbols)
    assert {s:r.store.revisions(f["forecast_id"]) for s,f in forecasts[0].items()}==saved
    assert all(len(r.store.revisions(f["forecast_id"]))==1 for f in forecasts[1].values())


def test_legacy_forecasts_without_source_bindings_still_score(tmp_path):
    r=runner(tmp_path,trainer=trained)
    r.run_cycle({s:history(s) for s in r.symbols},reference_at=REF)
    targets={s:history(s).assign(opened_at=REF+timedelta(minutes=30),
        available_at=REF+timedelta(minutes=45)) for s in r.symbols}
    scores=r.score_available(targets,as_of=(REF+timedelta(minutes=46)).isoformat())
    assert len(scores)==len(r.symbols) and all(o["status"]=="scored" for o in scores)


def test_manual_watch_retrains_on_fixed_grid_and_retains_cursor(tmp_path,monkeypatch):
    import ml.intraday.runtime as runtime
    clock=[REF+timedelta(minutes=15,seconds=30)]
    calls=[]
    def train(*args,**kwargs):calls.append(kwargs["training_cutoff"]);return trained(*args,**kwargs)
    r=runner(tmp_path,trainer=train)
    r.clock=lambda:clock[0].isoformat()
    class Inputs:
        def __init__(self,*,histories,**kwargs):self.histories=histories
        def get(self,symbol):return self.histories[symbol]
    monkeypatch.setattr(runtime,"LiveHistories",Inputs)
    result=runtime.watch_shadow(r,histories={s:history(s) for s in r.symbols},provider=object(),journal=object(),
        start_reference=REF,overrun_policy="retain_due_cycles",clock=r.clock,
        sleep=lambda seconds:clock.__setitem__(0,clock[0]+timedelta(seconds=seconds)),max_cycles=2)
    assert len(calls)==4
    assert sorted({f["reference_at"] for f in r.store.forecasts()})==[REF.isoformat(),(REF+timedelta(minutes=15)).isoformat()]
    assert clock[0]==REF+timedelta(minutes=30,seconds=30)
    assert result["next_reference"]==(REF+timedelta(minutes=30)).isoformat()


def test_final_1645_edge_preserves_inputs_without_invented_forecast(tmp_path):
    r=runner(tmp_path,trainer=trained)
    ref=datetime(2026,10,8,23,30,tzinfo=timezone.utc) # 16:45 Pacific completed boundary
    result=r.run_cycle({s:history(s) for s in r.symbols},reference_at=ref,mode="historical_replay")
    assert result["failed_symbols"]==2 and not r.store.forecasts()
    for symbol in r.symbols:
        _,snapshot=r.store.inputs(result["cycle_id"],symbol)
        assert snapshot["next_session_target"] is None and snapshot["candles"]


def test_resource_snapshot_does_not_inspect_other_trader_commands(monkeypatch):
    import ml.intraday.runtime as runtime

    def forbid_process_inspection(*args, **kwargs):
        pytest.fail("Independent runtime must not enumerate or inspect another process")

    def own_process(*args, **kwargs):
        assert not args and not kwargs, "Only the current process may be inspected"
        return SimpleNamespace(
            cpu_times=lambda: SimpleNamespace(user=2, system=3),
            memory_info=lambda: SimpleNamespace(rss=100),
            cmdline=forbid_process_inspection,
        )

    monkeypatch.setattr(runtime.psutil, "process_iter", forbid_process_inspection)
    monkeypatch.setattr(runtime.psutil, "pids", forbid_process_inspection)
    monkeypatch.setattr(runtime.psutil, "Process", own_process)
    monkeypatch.setattr(runtime.psutil, "virtual_memory", lambda: SimpleNamespace(available=1000))
    monkeypatch.setattr(runtime.psutil, "cpu_percent", lambda interval: 12.5)
    assert runtime.resource_snapshot() == {
        "process_rss_bytes": 100,
        "process_cpu_seconds": 5,
        "system_available_memory_bytes": 1000,
        "system_cpu_percent": 12.5,
    }


def test_research_membership_is_independent_of_account_state_and_keeps_profile_order(tmp_path, monkeypatch):
    from ml.intraday.runtime import local_symbols
    profile, watch = research_binding(tmp_path)
    binding = tmp_path / "state/account-gameplan/config.json"
    binding.parent.mkdir(parents=True)
    saved = json.loads(profile.read_text())
    saved["symbols"] = ["BBB", "AAA"]
    profile.write_text(json.dumps(saved))
    binding.write_text("invalid unrelated account json")
    original_read = Path.read_text
    def read(path, *args, **kwargs):
        assert path != binding, "Independent membership must not read Gameplan account state"
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    assert local_symbols(profile, watch, tmp_path) == ("Scout", ("BBB", "AAA"))
    assert binding.read_bytes() == b"invalid unrelated account json"



def test_live_supervised_specs_are_frozen_for_retry_and_new_cycle_adopts_latest(tmp_path, monkeypatch):
    import ml.intraday.data as data_module
    import ml.intraday.model_policy as policy_module
    from ml.intraday.models import ModelSpec
    monkeypatch.setattr(data_module, "FEATURE_NAMES", ("x",))
    selected = [ModelSpec("qualified_a", family="logistic", probability_calibration="sigmoid")]
    loads, fits = [], []
    def load(root, *, pc, symbol, feature_names):
        loads.append((root, pc, symbol, tuple(feature_names)))
        return dict(models=tuple(selected), dataset_scope=dict(synthetic_policy="exclude", lookback_days=None),
                    sha256=selected[0].name) if symbol == "AAPL" else None
    monkeypatch.setattr(policy_module, "load_active_policy", load)
    fail_ionq = [True]
    def train(*args, config, run_id, **kwargs):
        fits.append((run_id, config, kwargs["dataset_metadata"]))
        if "IONQ" in run_id and fail_ionq[0]:
            return dict(outputs=[dict(name="fixture_a", status="failed")], successful_count=0)
        names = tuple(spec.name if isinstance(spec, ModelSpec) else spec for spec in config)
        return trained(*args, config=names, **kwargs)
    r = runner(tmp_path, trainer=train)
    r.config = replace(r.config, use_supervised_models=True)
    first = r.run_cycle({s: history(s) for s in r.symbols}, reference_at=REF, mode="live")
    assert first["failed_symbols"] == 1 and len(loads) == 2
    forecast = first["forecasts"]["AAPL"]
    assert forecast["model_selection"]["models"][0]["name"] == "qualified_a"
    _, snapshot = r.store.inputs(first["cycle_id"], "AAPL")
    assert snapshot["model_selection"] == forecast["model_selection"]
    assert any(kwargs["model_selection"] == forecast["model_selection"] for _, _, kwargs in fits)
    selected[:] = [ModelSpec("qualified_b", family="random_forest", probability_calibration="sigmoid")]
    fail_ionq[0] = False
    repeated = r.run_cycle({s: history(s) for s in r.symbols}, reference_at=REF, mode="live")
    assert repeated["cycle_id"] == first["cycle_id"] and len(loads) == 2
    assert repeated["forecasts"]["AAPL"] == forecast
    assert repeated["forecasts"]["IONQ"]["model_selection"]["selection"] == "configured_defaults"
    r.clock = lambda: (REF + timedelta(minutes=31)).isoformat()
    newer = r.run_cycle({s: history(s) for s in r.symbols}, reference_at=REF + timedelta(minutes=15), mode="live")
    assert len(loads) == 4 and newer["cycle_id"] != first["cycle_id"]
    assert newer["forecasts"]["AAPL"]["model_selection"]["models"][0]["name"] == "qualified_b"
    assert newer["forecasts"]["AAPL"]["model_selection"]["policy_sha256"] != forecast["model_selection"]["policy_sha256"]
    assert all(path == tmp_path / "supervision" for path, _, _, _ in loads)



@pytest.mark.parametrize("mode", ["shadow", "historical_replay"])
def test_supervised_adoption_is_limited_to_explicit_live_mode(tmp_path, monkeypatch, mode):
    import ml.intraday.model_policy as policy_module
    monkeypatch.setattr(policy_module, "load_active_policy", lambda *a, **k: pytest.fail("research adopted live policy"))
    r = runner(tmp_path, trainer=trained)
    r.config = replace(r.config, use_supervised_models=True)
    result = r.run_cycle({s: history(s) for s in r.symbols}, reference_at=REF, mode=mode)
    assert result["successful_symbols"] == 2
    assert all(value["model_selection"] is None for value in result["forecasts"].values())



def test_invalid_supervised_policy_and_mismatched_features_never_fit_defaults(tmp_path, monkeypatch):
    import ml.intraday.model_policy as policy_module
    from ml.intraday.models import ModelSpec
    def invalid(*args, **kwargs):
        raise ContractError("Active policy evidence is invalid")
    monkeypatch.setattr(policy_module, "load_active_policy", invalid)
    r = runner(tmp_path, trainer=lambda *a, **k: pytest.fail("invalid policy triggered fitting"))
    r.config = replace(r.config, use_supervised_models=True)
    with pytest.raises(ContractError, match="policy evidence"):
        r.run_cycle({s: history(s) for s in r.symbols}, reference_at=REF, mode="live")
    monkeypatch.setattr(policy_module, "load_active_policy", lambda *a, **k: dict(
        models=(ModelSpec("qualified", family="logistic", probability_calibration="sigmoid"),),
        dataset_scope=dict(synthetic_policy="exclude", lookback_days=None), sha256="qualified"))
    result = r.run_cycle({s: history(s) for s in r.symbols}, reference_at=REF, mode="live")
    assert result["successful_symbols"] == 0 and not result["forecasts"]
    assert all("features differ" in failure["message"] for failure in result["failures"].values())



def test_supervised_cycle_detects_changed_frozen_policy(tmp_path, monkeypatch):
    import ml.intraday.model_policy as policy_module
    monkeypatch.setattr(policy_module, "load_active_policy", lambda *a, **k: None)
    r = runner(tmp_path, trainer=trained)
    r.config = replace(r.config, use_supervised_models=True)
    first = r.run_cycle({s: history(s) for s in r.symbols}, reference_at=REF, mode="live")
    with r.store.connect() as db:
        row = json.loads(db.execute("SELECT payload FROM cycles WHERE id=?", (first["cycle_id"],)).fetchone()[0])
        row["supervised_model_policy"]["models_by_symbol"]["AAPL"] = [{"name": "tampered"}]
        db.execute("UPDATE cycles SET payload=? WHERE id=?", (json.dumps(row), first["cycle_id"]))
    with pytest.raises(ContractError, match="policy hash changed"):
        r.run_cycle({}, reference_at=REF, mode="live")



def install_live_policy(tmp_path, symbol, *, feature_names=("x",), synthetic_policy="exclude", lookback_days=None,
                        legacy=False):
    from ml.intraday.model_policy import install_selection
    from ml.intraday.models import ModelSpec
    from ml.intraday.prediction import digest
    result = install_selection(tmp_path / "supervision", pc="Atlas", symbol=symbol,
        report=dict(status="promoted", selected_specs=[asdict(ModelSpec(
            "qualified-" + symbol, family="logistic", probability_calibration="sigmoid"))]),
        feature_names=feature_names, installed_at="2026-10-14T08:00Z",
        dataset_manifest=dict(synthetic_policy=synthetic_policy, lookback_days=lookback_days))
    if legacy:
        path = tmp_path / "supervision" / "active" / ("Atlas-" + symbol + ".json")
        value = json.loads(path.read_text())
        value.pop("dataset_scope")
        value.pop("sha256")
        value["sha256"] = digest(value)
        path.write_text(json.dumps(value))
        return value["sha256"]
    return result["policy_sha256"]



def population_history(symbol):
    rows = []
    for days_ago in (1, 0):
        start = REF - timedelta(days=days_ago, hours=10)
        for index in range(42):
            op = start + timedelta(minutes=15 * index)
            price = 100 + index % 7 / 10
            rows.append(dict(symbol=symbol, opened_at=op, available_at=op + timedelta(minutes=15),
                availability_kind="actual", open=price, high=price + 1, low=price - 1,
                close=price + .1, volume=100 + index, source_id="fixture:EQUS.MINI", source_dataset="EQUS.MINI",
                session_id=str(start.date()), session_open=start.isoformat(),
                session_close=(start + timedelta(hours=12)).isoformat(), synthetic=index == 30, no_trade=False))
    return pd.DataFrame(rows)



def test_live_policy_scope_drives_actual_population_and_next_cycle_adopts_new_scope(tmp_path):
    from ml.intraday.data import FEATURE_NAMES, build_dataset
    policy_id = install_live_policy(tmp_path, "AAPL", feature_names=FEATURE_NAMES, lookback_days=1)
    fits = {}
    def train(X, y, inference_X, *, config, run_id, **kwargs):
        symbol = "AAPL" if "AAPL" in run_id else "IONQ"
        fits[symbol] = (len(X), kwargs["dataset_metadata"])
        return trained(X, y, inference_X, config=tuple(getattr(spec, "name", spec) for spec in config), **kwargs)
    r = runner(tmp_path, trainer=train)
    r.dataset_builder = None
    r.config = replace(r.config, use_supervised_models=True)
    histories = {symbol: population_history(symbol) for symbol in r.symbols}
    first = r.run_cycle(histories, reference_at=REF, mode="live")
    assert first["successful_symbols"] == 2
    selected, defaults = first["forecasts"]["AAPL"], first["forecasts"]["IONQ"]
    assert selected["dataset_scope"] == dict(synthetic_policy="exclude", lookback_days=1)
    assert defaults["dataset_scope"] == dict(synthetic_policy="include", lookback_days=None)
    assert selected["synthetic_policy"] == fits["AAPL"][1]["synthetic_policy"] == "exclude"
    assert selected["model_selection"]["active_policy_sha256"] == policy_id
    assert selected["dataset_manifest"]["synthetic_labels_excluded"] > 0
    assert defaults["dataset_manifest"]["synthetic_labels_included"] > 0
    assert fits["AAPL"][0] < fits["IONQ"][0]
    expected = build_dataset(histories["AAPL"], reference_at=REF, cutoff=REF + timedelta(minutes=16),
                             synthetic_policy="exclude", lookback_days=1)
    assert fits["AAPL"][0] == len(expected.X)
    with r.store.connect() as db:
        saved = json.loads(db.execute("SELECT payload FROM cycles WHERE id=?", (first["cycle_id"],)).fetchone()[0])
    assert saved["supervised_model_policy"]["dataset_scope_by_symbol"]["AAPL"] == selected["dataset_scope"]
    assert saved["supervised_model_policy"]["policy_identities_by_symbol"]["AAPL"] == policy_id
    newer_id = install_live_policy(tmp_path, "AAPL", feature_names=FEATURE_NAMES, synthetic_policy="include")
    r.clock = lambda: (REF + timedelta(minutes=31)).isoformat()
    newer = r.run_cycle(histories, reference_at=REF + timedelta(minutes=15), mode="live")
    assert newer["successful_symbols"] == 2
    assert newer["forecasts"]["AAPL"]["dataset_scope"] == defaults["dataset_scope"]
    assert newer["forecasts"]["AAPL"]["model_selection"]["active_policy_sha256"] == newer_id != policy_id
    assert fits["AAPL"][0] == fits["IONQ"][0]



def test_live_scope_retry_keeps_frozen_inputs_and_scope_for_not_yet_acquired_symbols(tmp_path, monkeypatch):
    import ml.intraday.data as data_module
    monkeypatch.setattr(data_module, "FEATURE_NAMES", ("x",))
    original_id = install_live_policy(tmp_path, "AAPL", lookback_days=7)
    fail = [True]
    builds, fits = [], []
    def build(frame, **kwargs):
        builds.append((frame.iloc[0].symbol, kwargs["synthetic_policy"], kwargs["lookback_days"]))
        value = dataset(frame, **kwargs)
        value.manifest.update(synthetic_policy=kwargs["synthetic_policy"], lookback_days=kwargs["lookback_days"])
        return value
    def train(*args, config, run_id, **kwargs):
        fits.append(kwargs["dataset_metadata"])
        if fail[0]:
            return dict(outputs=[], successful_count=0)
        return trained(*args, config=tuple(getattr(spec, "name", spec) for spec in config), **kwargs)
    class Histories:
        def get(self, symbol):
            if symbol == "IONQ" and fail[0]:
                raise ContractError("Temporary local history fixture failure")
            return history(symbol)
    r = runner(tmp_path, trainer=train)
    r.dataset_builder = build
    r.config = replace(r.config, use_supervised_models=True, lookback_days=30)
    first = r.run_cycle(Histories(), reference_at=REF, mode="live")
    assert first["failed_symbols"] == 2 and builds == [("AAPL", "exclude", 7)]
    frozen = r.store.inputs(first["cycle_id"], "AAPL")
    assert r.store.inputs(first["cycle_id"], "IONQ") is None
    install_live_policy(tmp_path, "AAPL", synthetic_policy="include", lookback_days=14)
    install_live_policy(tmp_path, "IONQ", lookback_days=1)
    fail[0] = False
    repeated = r.run_cycle(Histories(), reference_at=REF, mode="live")
    assert repeated["successful_symbols"] == 2
    assert r.store.inputs(first["cycle_id"], "AAPL") == frozen
    assert builds == [("AAPL", "exclude", 7), ("IONQ", "include", 30)]
    assert repeated["forecasts"]["AAPL"]["model_selection"]["active_policy_sha256"] == original_id
    assert repeated["forecasts"]["IONQ"]["model_selection"]["selection"] == "configured_defaults"
    assert repeated["forecasts"]["AAPL"]["dataset_scope"] == dict(synthetic_policy="exclude", lookback_days=7)
    assert all(metadata["dataset_scope"] == dict(synthetic_policy="exclude", lookback_days=7)
               for metadata in fits if metadata.get("model_selection", {}).get("selection") == "supervised")



def test_legacy_active_policy_derives_scope_only_for_new_live_cycles(tmp_path, monkeypatch):
    import ml.intraday.data as data_module
    from ml.intraday.prediction import digest
    from ml.intraday.runtime import _supervised_cycle_policy
    monkeypatch.setattr(data_module, "FEATURE_NAMES", ("x",))
    policy_id = install_live_policy(tmp_path, "AAPL", legacy=True)
    r = runner(tmp_path, trainer=trained)
    r.config = replace(r.config, use_supervised_models=True, lookback_days=30)
    configuration = asdict(r.config)
    cycle_id = digest(dict(pc=r.pc, reference_at=REF.isoformat(), configuration_hash=digest(configuration),
                           mode="live", experiment="primary"))
    # This saved cycle predates scope-aware adoption and has not acquired IONQ.
    frozen_policy = _supervised_cycle_policy(r.store, cycle_id, pc=r.pc, symbols=r.symbols,
                                              configuration=configuration)
    frozen_policy.pop("dataset_scope_by_symbol")
    frozen_policy.pop("policy_identities_by_symbol")
    frozen_policy.pop("sha256")
    frozen_policy["sha256"] = digest(frozen_policy)
    r.store.begin_cycle(cycle_id, dict(pc=r.pc, symbols=list(r.symbols), reference_at=REF.isoformat(),
        configuration=configuration, configuration_hash=digest(configuration), mode="live", status="running",
        supervised_model_policy=frozen_policy, started_at=(REF + timedelta(minutes=16)).isoformat(),
        training_cutoff=(REF + timedelta(minutes=16)).isoformat()))
    builds = []
    def build(frame, **kwargs):
        builds.append((frame.iloc[0].symbol, kwargs["synthetic_policy"], kwargs["lookback_days"]))
        return dataset(frame, **kwargs)
    r.dataset_builder = build
    old = r.run_cycle({s: history(s) for s in r.symbols}, reference_at=REF, mode="live")
    assert old["successful_symbols"] == 2
    assert sorted(builds) == [("AAPL", "include", 30), ("IONQ", "include", 30)]
    assert old["forecasts"]["AAPL"]["synthetic_policy"] == "include"
    with r.store.connect() as db:
        persisted = json.loads(db.execute("SELECT payload FROM cycles WHERE id=?", (cycle_id,)).fetchone()[0])
    assert persisted["supervised_model_policy"] == frozen_policy
    builds.clear()
    r.clock = lambda: (REF + timedelta(minutes=31)).isoformat()
    new = r.run_cycle({s: history(s) for s in r.symbols}, reference_at=REF + timedelta(minutes=15), mode="live")
    assert new["successful_symbols"] == 2
    assert sorted(builds) == [("AAPL", "exclude", None), ("IONQ", "include", 30)]
    assert new["forecasts"]["AAPL"]["model_selection"]["active_policy_sha256"] == policy_id



def test_legacy_cycle_hash_is_checked_before_scope_derivation(tmp_path, monkeypatch):
    from ml.intraday.runtime import _frozen_supervised_policy
    from ml.intraday.prediction import digest
    policy = dict(pc="Atlas", feature_names=["x"], models_by_symbol={"AAPL": None})
    policy["sha256"] = digest(policy)
    policy["models_by_symbol"]["AAPL"] = [{"name": "changed"}]
    # Missing configuration would otherwise fail during legacy derivation.
    with pytest.raises(ContractError, match="policy hash changed"):
        _frozen_supervised_policy(dict(supervised_model_policy=policy), symbols=("AAPL",))



@pytest.mark.parametrize("scope", [None, [], {}, {"synthetic_policy": "include"},
    {"lookback_days": None}, {"synthetic_policy": [], "lookback_days": None},
    {"synthetic_policy": "unknown", "lookback_days": None},
    {"synthetic_policy": "include", "lookback_days": True},
    {"synthetic_policy": "include", "lookback_days": 0},
    {"synthetic_policy": "include", "lookback_days": 1.5}])
def test_frozen_supervised_scope_rejects_malformed_values(tmp_path, scope):
    from ml.intraday.runtime import _frozen_supervised_policy
    from ml.intraday.prediction import digest
    policy = dict(pc="Atlas", feature_names=["x"], models_by_symbol={"AAPL": None},
                  dataset_scope_by_symbol={"AAPL": scope})
    policy["sha256"] = digest(policy)
    with pytest.raises(ContractError, match="dataset scope"):
        _frozen_supervised_policy(dict(supervised_model_policy=policy), symbols=("AAPL",))



@pytest.mark.parametrize("scopes", [None, [], "invalid", {}])
def test_frozen_supervised_cycle_requires_scope_mapping_for_every_symbol(scopes):
    from ml.intraday.runtime import _frozen_supervised_policy
    from ml.intraday.prediction import digest
    policy = dict(pc="Atlas", feature_names=["x"], models_by_symbol={"AAPL": None},
                  dataset_scope_by_symbol=scopes)
    policy["sha256"] = digest(policy)
    with pytest.raises(ContractError, match="dataset scope"):
        _frozen_supervised_policy(dict(supervised_model_policy=policy), symbols=("AAPL",))

