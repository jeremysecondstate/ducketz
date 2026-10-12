from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import sys
import threading

import numpy as np
import pandas as pd
import pytest

from ml.intraday.prediction import ContractError
from ml.intraday.runtime import ResearchConfig, ShadowRunner
from ml.intraday.store import ShadowStore

REF = datetime(2026,10,8,17,tzinfo=timezone.utc)


def history(symbol):
    return pd.DataFrame([dict(symbol=symbol,opened_at=REF,available_at=REF+timedelta(minutes=15),
                              availability_kind="actual",open=100,high=101,low=99,close=100,volume=100,synthetic=False)])


def dataset(frame,*,cutoff,reference_at,**kwargs):
    return SimpleNamespace(X=np.array([[0],[1],[0],[1]]),y=np.array([0,1,0,1]),inference_X=np.array([[1]]),
        feature_names=("x",),label_available_at=pd.date_range(REF-timedelta(hours=2),periods=4,freq="15min"),
        reference_at=pd.date_range(REF-timedelta(hours=3),periods=4,freq="15min"),
        target_close_at=pd.date_range(REF-timedelta(hours=2,minutes=15),periods=4,freq="15min"),
        inference=dict(symbol=frame.iloc[0].symbol,reference_at=reference_at.isoformat(),reference_open=100,
                       information_available_at=(REF+timedelta(minutes=15)).isoformat(),availability_kind="actual"),manifest={"training_rows":4})


def trained(*args,config,**kwargs):
    return dict(outputs=[dict(name=name,status="success",probability_not_down=.6,version="fixture",training_seconds=0,inference_seconds=0) for name in config],
                configured_count=len(config),successful_count=len(config),complete=True,probability_not_down=.6,training_seconds=0,inference_seconds=0)


def runner(tmp_path,**kwargs):
    return ShadowRunner(store=ShadowStore(tmp_path/"shadow.sqlite3"),pc="Atlas",symbols=("AAPL","IONQ"),
        config=ResearchConfig(models=("fixture_a","fixture_b"),symbol_workers=2),
        dataset_builder=dataset,clock=lambda:(REF+timedelta(minutes=16)).isoformat(),**kwargs)


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
    assert result["total_cycle_seconds"]>=2


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
