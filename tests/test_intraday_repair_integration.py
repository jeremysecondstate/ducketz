"""Offline integration of frozen policies, causal inputs and terminal gaps."""
import json
from dataclasses import replace
from datetime import timedelta

import pytest

from ml.intraday.prediction import ContractError, digest
from ml.intraday.runtime import ResearchConfig, with_local_data_policy, SourceIntervalEmpty
from ml.intraday.technical_sizing import indicator_snapshot
from tests.test_intraday_technical_sizing import bars, fixture
from tests.test_intraday_selected_models import composed_runner, write_config, REF


def test_snapshot_records_preserve_precision_and_nullable_values():
    import pandas as pd
    import numpy as np
    from ml.intraday.runtime import _jsonable
    price=123.12345678912345
    frame=pd.DataFrame({"price":[price],"nullable":[pd.NA],"time":[pd.NaT],"nonfinite":[np.float64("nan")]})
    result=_jsonable(frame.to_dict("records"))
    assert result==[{"price":price,"nullable":None,"time":None,"nonfinite":None}]
    assert json.loads(json.dumps(result,allow_nan=False))==result


def test_new_market_columns_refit_retained_specs_without_inheriting_qualification(tmp_path, composed_runner):
    path=tmp_path/'model-supervision/active.json'
    write_config(path)
    value=json.loads(path.read_text())
    entry=value['symbols']['AAPL']
    entry.update(feature_names=['x','market_old','market_old__available'],market_input_version='older-context')
    path.write_text(json.dumps(value))
    runner,history,seen=composed_runner(config=ResearchConfig(market_inputs_enabled=True))
    result=runner.run_cycle(history,reference_at=REF,mode='live')
    saved=result['forecasts']['AAPL']
    assert 'not_yet_assessed' in saved['model_qualification_context']
    assert seen[0]['names']==['nb_smooth'] and seen[0]['rows']==3
    _,snapshot=runner.store.inputs(result['cycle_id'],'AAPL')
    assert snapshot['feature_names'][0]=='x' and 'market_old' not in snapshot['feature_names']
    assert len(snapshot['X'][0])==len(snapshot['feature_names'])
    assert snapshot['market_inputs']['training_rows']==3
    assert set(snapshot['market_inputs']['training_nonmissing_rows_by_feature'].values())=={0}


@pytest.mark.parametrize('features', [['other','market_old','market_old__available'],['x','nonmarket','nonmarket__available']])
def test_market_migration_does_not_hide_price_or_untyped_feature_changes(tmp_path, composed_runner,features):
    path=tmp_path/'model-supervision/active.json';write_config(path)
    value=json.loads(path.read_text());value['symbols']['AAPL']['feature_names']=features
    path.write_text(json.dumps(value))
    runner,history,seen=composed_runner(config=ResearchConfig(market_inputs_enabled=True))
    result=runner.run_cycle(history,reference_at=REF,mode='live')
    assert not result['forecasts'] and not seen


def test_private_binding_is_explicit_and_keeps_statistical_parameters(tmp_path):
    config = ResearchConfig(lookback_days=37, synthetic_policy="exclude",symbol_workers=2)
    assert with_local_data_policy(config,tmp_path) == config
    root=tmp_path/"intraday";root.mkdir()
    (root/"data-policy.json").write_text(json.dumps({"schema_version":"intraday-data-policy-v1",
        "source_dataset":"EQUS.MINI","population_contract":"intraday-verified-source-observations-v1",
        "market_inputs_enabled":True}))
    updated=with_local_data_policy(config,tmp_path)
    assert updated.lookback_days==37 and updated.synthetic_policy=="exclude" and updated.symbol_workers==2
    assert updated.market_inputs_enabled


def test_sizing_filters_frozen_exclusion_before_indicator_warmup():
    frame=bars()
    frame.loc[2,"synthetic"]=True
    bundle=fixture(frame)
    _,_,config,_,snapshot,frame=bundle
    snapshot["dataset_scope"]={"synthetic_policy":"exclude","lookback_days":None}
    result=indicator_snapshot(frame,symbol=config.symbol,reference_at=snapshot["inference"]["reference_at"],
        data_cutoff=snapshot["cutoff"],indicator_specs=config.indicators,window_policy=config.window,
        forecast_snapshot=snapshot,forecast_snapshot_hash=digest(snapshot)).payload()
    assert result["status"]=="ready"
    assert len(result["candles"])==31
    assert result["provenance"]["excluded_policy_rows"]==1
    assert result["provenance"]["synthetic_rows"]==0


def test_sizing_does_not_substitute_reference_when_frozen_policy_excludes_it():
    frame=bars();frame.loc[len(frame)-1,"synthetic"]=True
    bundle=fixture(frame)
    _,_,config,_,snapshot,frame=bundle
    snapshot["dataset_scope"]={"synthetic_policy":"exclude","lookback_days":None}
    result=indicator_snapshot(frame,symbol=config.symbol,reference_at=snapshot["inference"]["reference_at"],
        data_cutoff=snapshot["cutoff"],indicator_specs=config.indicators,window_policy=config.window,
        forecast_snapshot=snapshot,forecast_snapshot_hash=digest(snapshot)).payload()
    assert result["status"]=="unavailable" and result["reason"]=="original_completed_reference_unavailable"


def test_scoring_rejects_new_population_for_old_frozen_forecast(tmp_path):
    # Dedicated store tests cover immutable identity first; here deliberately
    # isolate the new population mismatch before persisting any outcome.
    from ml.intraday.store import ShadowStore
    from tests.test_intraday_store import forecast as make_forecast, candle
    store=ShadowStore(tmp_path/"shadow.sqlite3")
    saved=store.save_forecast(make_forecast())
    with pytest.raises(ContractError,match="population contract"):
        store.observe(saved,as_of="2026-10-08T17:46:00Z",
                      candle=candle(population_contract="intraday-verified-source-observations-v1"))
    assert not store.revisions(saved["forecast_id"])


def test_final_source_empty_completes_collection_without_inventing_a_candle(tmp_path,monkeypatch):
    from ml.intraday import candle_collection
    from ml.intraday.store import ShadowStore
    from ml.intraday.prediction import instant
    from types import SimpleNamespace
    now=instant("2026-10-17T00:00:30Z")
    class Empty:
        def __init__(self,**kwargs):
            pass
        def bind_cycle(self,*args):
            pass
        def get(self,*args):
            raise SourceIntervalEmpty({"status":"unavailable","reason":"source_empty","final":True,
                                       "evidence":{"covered_requests":["receipt-a","receipt-b"]}})
    monkeypatch.setattr(candle_collection,"LiveHistories",Empty)
    store=ShadowStore(tmp_path/"shadow.sqlite3")
    collector=candle_collection.CandleCollector(store,histories={},provider=SimpleNamespace(dataset="EQUS.MINI"),
        journal=None,symbols=("AAA",),session_id="manual-fixture",started_at=now-timedelta(hours=1),
        close_at=now-timedelta(seconds=30),clock=lambda:now,
        population_contract="intraday-verified-source-observations-v1")
    result=collector.pass_once()
    assert result["final_complete"] and not store.observed_candles()
    with store.connect() as db:
        assert db.execute("SELECT status FROM intraday_candle_collection").fetchone()[0]=="source_empty"

