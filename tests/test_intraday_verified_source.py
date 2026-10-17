"""Offline contract repairs: sparse source data is not market-wide no trading."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from ml.intraday import data, supervisor
from ml.intraday.coverage import (POPULATION_CONTRACT, aggregate_source_minutes,
    independently_verified_no_trade, load_data_policy, population_frame, verified_source_observation)
from ml.intraday.prediction import ContractError, STEP
from tests.test_intraday_data import candles, response, bounds, independent_absence


def native_rows(indices=(0, 7, 14)):
    frame = candles(15, start="2026-05-11T14:00:00Z", minutes=True).iloc[list(indices)]
    return tuple({"opened_at": row.opened_at.isoformat(), **{k: float(getattr(row, k))
                 for k in ("open", "high", "low", "close", "volume")}} for row in frame.itertuples())


def attempt(number, rows=(), **kwargs):
    # Real MINI responses have no independent no-trade proof, even when empty.
    return replace(response(number, rows=rows, wait=number, **kwargs), no_trade_scope_evidence=None)


def resolve(attempts):
    return data.resolve_minute_candle(attempts, session=bounds(), previous=None,
                                     population_contract=POPULATION_CONTRACT)


def test_repeated_empty_mini_never_fabricates_a_market_price():
    prior = candles(2).iloc[-1].to_dict()
    attempts = [attempt(1), attempt(2)]
    assert data.resolve_interval(attempts, session=bounds(), previous=prior)["candle"] is None
    first = resolve(attempts[:1])
    assert first["status"] == "retry" and first["reason"] == "source_observations_unstable"
    final = resolve(attempts)
    assert final["reason"] == "source_empty" and final["final"] is True and final["candle"] is None


def test_stable_sparse_uses_only_real_native_extrema_volume_and_endpoints():
    rows = native_rows((2, 7, 11))
    result = resolve([attempt(1, rows), attempt(2, rows)])
    candle = result["candle"]
    assert result["status"] == "sparse_observed"
    assert (candle["observed_constituents"], candle["source_empty_constituents"]) == (3, 12)
    assert candle["open"] == rows[0]["open"] and candle["close"] == rows[-1]["close"]
    assert candle["volume"] == sum(row["volume"] for row in rows)
    assert candle["high"] == max(row["high"] for row in rows)
    assert candle["low"] == min(row["low"] for row in rows)
    assert not candle["synthetic"] and not candle["no_trade"]
    assert candle["native_first_opened_at"] == rows[0]["opened_at"]
    assert verified_source_observation(candle) and data.completed_15m_observation(candle)


def test_stability_requires_latest_successful_payload_not_any_older_match():
    first, changed = native_rows((0,)), native_rows((0, 7))
    assert resolve([attempt(1, first), attempt(2, changed), attempt(3, first)])["status"] == "retry"
    assert resolve([attempt(1, first), attempt(2, changed), attempt(3, first), attempt(4, first)])["status"] == "sparse_observed"


@pytest.mark.parametrize("damage", ["too_soon", "same_id", "uncovered", "retry_after"])
def test_sparse_receipt_stability_is_not_bypassed(damage):
    rows = native_rows()
    first, second = attempt(1, rows), attempt(2, rows)
    if damage == "too_soon": second = replace(second,request_started_at=first.request_ended_at)
    if damage == "same_id": second = replace(second,attempt_id=first.attempt_id)
    if damage == "uncovered": first = replace(first,coverage_open=None,coverage_close=None)
    if damage == "retry_after": first = replace(first,retry_after_seconds=60)
    assert resolve([first, second])["status"] == "retry"


def test_full_native_quarter_needs_no_sparse_stability_wait():
    assert resolve([attempt(1,native_rows(tuple(range(15))))])["status"] == "observed"


@pytest.mark.parametrize("marker,value", [("synthetic",True),("no_trade",True),
    ("synthetic",0),("synthetic",None),("synthetic","False"),
    ("synthetic_open",True),("synthetic_close",True),("synthetic_constituents",1),("is_synthetic",True)])
def test_native_response_cannot_erase_synthetic_or_unknown_markers(marker,value):
    rows=tuple(dict(row,**{marker:value}) for row in native_rows())
    with pytest.raises(ContractError,match="Native source observations"):
        resolve([attempt(1,rows),attempt(2,rows)])


def test_explicit_observed_native_markers_remain_valid():
    rows=tuple(dict(row,synthetic=False,no_trade=False,synthetic_open=False,
                    synthetic_close=False,synthetic_constituents=0) for row in native_rows())
    assert resolve([attempt(1,rows),attempt(2,rows)])["status"]=="sparse_observed"


def test_direct_aggregation_cannot_strip_synthetic_constituent_metadata():
    minutes=candles(15,minutes=True).assign(synthetic_constituents=1)
    with pytest.raises(ContractError,match="Native source observations"):
        aggregate_source_minutes(minutes,sessions=[bounds()],source_coverage=[{
            "opened_at":"2026-05-11T13:30:00Z","closed_at":"2026-05-11T13:45:00Z","basis":"fixture"}])


def test_covered_source_empty_quarters_count_without_becoming_candles():
    minute = candles(60, minutes=True).drop(index=list(range(15,30))+[35])
    frame, report = aggregate_source_minutes(minute,sessions=[bounds()],source_coverage=[{
        "opened_at":"2026-05-11T13:30:00Z","closed_at":"2026-05-11T14:30:00Z","basis":"saved-test-range"}])
    assert len(frame)==3 and report["covered_intervals"]==4 and report["empty_intervals"]==1
    assert report["sparse_intervals"]==1 and not frame.synthetic.any()
    assert not frame.opened_at.eq(pd.Timestamp("2026-05-11T13:45:00Z")).any()
    partial, partial_report = aggregate_source_minutes(minute,sessions=[bounds()],source_coverage=[{
        "opened_at":"2026-05-11T13:31:00Z","closed_at":"2026-05-11T14:29:00Z","basis":"partial-test-range"}])
    assert len(partial)==1 and partial_report["uncovered_observed_intervals"]==2


@pytest.mark.parametrize("damage", ["synthetic", "missing_proof", "wrong_count", "integer_bool", "bad_ohlc"])
def test_proof_cannot_admit_tampered_or_legacy_rows(damage):
    candle = resolve([attempt(1,native_rows()),attempt(2,native_rows())])["candle"]
    if damage=="synthetic": candle["synthetic"]=True
    if damage=="missing_proof": candle["evidence_json"]="{}"
    if damage=="wrong_count": candle["source_empty_constituents"]=0
    if damage=="integer_bool": candle["synthetic"]=0
    if damage=="bad_ohlc": candle["high"]=1
    assert not verified_source_observation(candle)


def native_history_fixture(tmp_path):
    minute = candles(630, start="2026-05-11T13:30:00Z", minutes=True)
    normalized = minute.rename(columns={"opened_at":"timestamp"})[["timestamp","open","high","low","close","volume"]]
    folder=tmp_path/"stocks/AAPL/bars/1m/databento"
    (folder/"normalized").mkdir(parents=True)
    (folder/"raw").mkdir()
    canonical=folder/"normalized/AAPL_fixture_ohlcv-1m_1m.parquet"
    rawpath=folder/"raw/AAPL_fixture_ohlcv-1m_1m_raw.parquet"
    raw=normalized.rename(columns={"timestamp":"ts_event"}).assign(provider_dataset="EQUS.MINI",
        source_schema="ohlcv-1m",symbol="AAPL",price_basis="unadjusted_market_scale",publisher_id=95,instrument_id=38,
        range_start="2026-05-11T13:30:00Z",range_end="2026-05-12T00:00:00Z")
    normalized.to_parquet(canonical,index=False); raw.to_parquet(rawpath,index=False)
    return canonical,rawpath


def test_native_objects_preserve_previous_revision_and_causal_training(tmp_path,monkeypatch):
    canonical,rawpath=native_history_fixture(tmp_path)
    history=data.load_local_history(tmp_path,"AAPL",population_contract=POPULATION_CONTRACT)
    obj=history.manifest["native_objects"][0]
    saved=Path(obj["path"])
    before=saved.read_bytes()
    assert hashlib.sha256(before).hexdigest()==obj["sha256"]
    assert pd.read_parquet(saved).instrument_id.eq(38).all()
    # Change both current files consistently; prior input object remains immutable.
    for path in (canonical,rawpath):
        frame=pd.read_parquet(path); frame["volume"]+=1; frame.to_parquet(path,index=False)
    revised=data.load_local_history(tmp_path,"AAPL",population_contract=POPULATION_CONTRACT)
    assert revised.manifest["native_objects"][0]["sha256"]!=obj["sha256"]
    assert saved.read_bytes()==before
    snapshot=supervisor.training_snapshot(history,cutoff="2026-05-12T00:00:00Z",synthetic_policy="include")
    assert snapshot.manifest["population_contract"]==POPULATION_CONTRACT
    assert snapshot.manifest["synthetic_labels_included"]==0
    assert (snapshot.label_available_at<=pd.Timestamp("2026-05-12T00:00:00Z")).all()
    from ml.intraday import market_inputs, feature_support
    original_augment=market_inputs.augment_snapshot
    def with_extra_contract(*args,**kwargs):
        result=original_augment(*args,**kwargs)
        result["manifest"]["feature_contract"]["additional_contract"]="preserved"
        return result
    monkeypatch.setattr(market_inputs,"augment_snapshot",with_extra_contract)
    augmented=supervisor.attach_market_inputs(snapshot,tmp_path,"AAPL",cutoff="2026-05-12T00:00:00Z")
    assert augmented.X.shape[1]>snapshot.X.shape[1]
    assert np.array_equal(augmented.X[:,:snapshot.X.shape[1]],snapshot.X)
    assert not augmented.X[:,snapshot.X.shape[1]:].any()  # absence flagged, no historical backfill
    assert augmented.manifest["feature_contract"]["market_inputs"]
    assert augmented.manifest["feature_contract"]["additional_contract"]=="preserved"
    assert augmented.manifest["feature_contract"]["market_support"]==feature_support.VERSION
    evidence=augmented.manifest["market_input_runtime_provenance"]["modules"][feature_support.__name__]
    assert evidence["status"]=="captured" and evidence["loaded_functions_match_disk"] is True


@pytest.mark.parametrize("damage", ["normalized_revision", "missing_ranges", "source_identity"])
def test_native_history_rejects_unproven_lineage(tmp_path,damage):
    canonical,rawpath=native_history_fixture(tmp_path)
    if damage=="normalized_revision":
        frame=pd.read_parquet(canonical); frame.loc[0,"volume"]+=1; frame.to_parquet(canonical,index=False)
    else:
        frame=pd.read_parquet(rawpath)
        if damage=="missing_ranges": frame=frame.drop(columns="range_start")
        else: frame.loc[0,"provider_dataset"]=None
        frame.to_parquet(rawpath,index=False)
    with pytest.raises(ContractError): data.load_local_history(tmp_path,"AAPL",population_contract=POPULATION_CONTRACT)


@pytest.mark.parametrize("which",["raw","canonical"])
@pytest.mark.parametrize("marker,value",[("synthetic",True),("no_trade",True),("synthetic",0),("is_synthetic",True)])
def test_history_cannot_relabel_marked_rows_as_observed(tmp_path,which,marker,value):
    canonical,rawpath=native_history_fixture(tmp_path)
    path=rawpath if which=="raw" else canonical
    frame=pd.read_parquet(path); frame[marker]=value; frame.to_parquet(path,index=False)
    with pytest.raises(ContractError,match="Native source observations"):
        data.load_local_history(tmp_path,"AAPL",population_contract=POPULATION_CONTRACT)


@pytest.mark.parametrize("which",["normalized","native"])
def test_archive_cannot_relabel_marked_rows_as_observed(tmp_path,monkeypatch,which):
    from tests.test_intraday_data import archive_partition
    frame=candles(45,minutes=True).rename(columns={"opened_at":"ts_event"})
    frame=frame[["ts_event","symbol","open","high","low","close","volume"]]
    if which=="normalized": frame["synthetic"]=True
    directory,natives=archive_partition(tmp_path,monkeypatch,frame=frame)
    if which=="native": natives[directory/"provider.dbn.zst"]["synthetic"]=True
    with pytest.raises(ContractError,match="Native source observations"):
        data.load_local_history(tmp_path,"AAPL",source_dataset="XNAS.ITCH",population_contract=POPULATION_CONTRACT)


def test_legacy_synthetic_does_not_enter_new_features_or_labels(tmp_path):
    native_history_fixture(tmp_path)
    history=data.load_local_history(tmp_path,"AAPL",population_contract=POPULATION_CONTRACT)
    original=history.frame.copy(deep=True)
    legacy=history.frame.iloc[10].copy()
    legacy["synthetic"]=True; legacy["population_contract"]=None; legacy["evidence_json"]="{}"
    mixed=pd.concat([history.frame,pd.DataFrame([legacy])],ignore_index=True)
    admitted=population_frame(mixed,POPULATION_CONTRACT)
    pd.testing.assert_frame_equal(admitted,original,check_dtype=False)
    ref=original.opened_at.iloc[-4]
    baseline=data.build_dataset(original,cutoff=ref+STEP,reference_at=ref,population_contract=POPULATION_CONTRACT)
    actual=data.build_dataset(mixed,cutoff=ref+STEP,reference_at=ref,population_contract=POPULATION_CONTRACT)
    assert np.array_equal(actual.X,baseline.X) and np.array_equal(actual.y,baseline.y)
    assert actual.manifest["unverified_or_other_population_candles_excluded"]==1


def test_filtered_historical_native_window_cannot_claim_complete_edge_quarters(tmp_path):
    native_history_fixture(tmp_path)
    history=data.load_local_history(tmp_path,"AAPL",population_contract=POPULATION_CONTRACT,
        start="2026-05-11T13:32:00Z",end="2026-05-11T14:08:00Z")
    assert history.frame.opened_at.tolist()==[pd.Timestamp("2026-05-11T13:45:00Z")]
    assert history.manifest["aggregation"]["uncovered_observed_intervals"]==2


def test_stopped_recovery_never_reads_journal_or_provider():
    class Forbidden:
        def __getattr__(self,_): pytest.fail("Stopped recovery touched a provider or journal")
    with pytest.raises(InterruptedError):
        data.recover_databento_candle_step(Forbidden(),symbol="AAPL",opened_at="2026-05-11T14:00:00Z",
            journal=Forbidden(),session=bounds(),previous=None,stopped=lambda:True)
    with pytest.raises(InterruptedError):
        data.fetch_databento_interval_attempts(Forbidden(),symbols=["AAPL"],opened_at="2026-05-11T14:00:00Z",
            attempt_id="stopped",stopped=lambda:True)


def test_policy_is_explicit_and_preserves_additional_bindings(tmp_path):
    assert load_data_policy(tmp_path)["population_contract"] is None
    folder=tmp_path/"intraday";folder.mkdir()
    policy={"schema_version":"intraday-data-policy-v1","population_contract":POPULATION_CONTRACT,
            "market_inputs_enabled":True,"future_binding":"retained"}
    (folder/"data-policy.json").write_text(json.dumps(policy))
    assert load_data_policy(tmp_path)==policy
    policy["market_inputs_enabled"]=1
    (folder/"data-policy.json").write_text(json.dumps(policy))
    with pytest.raises(ContractError): load_data_policy(tmp_path)
