import copy
import hashlib
import json
from pathlib import Path
import sqlite3

import pandas as pd
import pytest

from ml.intraday.coverage import POPULATION_CONTRACT as PRIMARY_CONTRACT
from ml.intraday.data import SessionBounds, validate_candles
from ml.intraday.prediction import ContractError
from ml.intraday.recovered_population import (
    POPULATION_CONTRACT, SOURCE_ID, SOURCE_DATASET,
    wrap_primary, recovery_candle, verified_recovery_candle,
    save_recovery_candle, load_recovery_candles,
)
from ml.market_gap_evidence import assess_interval_gap

START = pd.Timestamp("2026-10-12T14:00:00Z")
END = START+pd.Timedelta(minutes=15)
NOW = END+pd.Timedelta(seconds=30)
SESSION = SessionBounds("2026-10-12", "2026-10-12T08:00:00Z", "2026-10-13T00:00:00Z")


def primary():
    observation = {"opened_at":START.isoformat(),"available_at":(START+pd.Timedelta(minutes=1)).isoformat(),
        "source_id":"databento:EQUS.MINI:ohlcv-1m","synthetic":False,
        "open":100.,"high":102.,"low":99.,"close":101.,"volume":20.}
    evidence={"aggregation":PRIMARY_CONTRACT,"source_id":observation["source_id"],"source_dataset":"EQUS.MINI",
        "source_coverage":{"opened_at":START.isoformat(),"closed_at":END.isoformat(),"basis":"recorded_request"},
        "observations":[observation]}
    return {"symbol":"TEST","opened_at":START,"available_at":END,"availability_kind":"actual",
        "open":100.,"high":102.,"low":99.,"close":101.,"volume":20.,
        "source_id":observation["source_id"],"source_dataset":"EQUS.MINI","population_contract":PRIMARY_CONTRACT,
        "session_id":SESSION.session_id,"session_open":SESSION.opened_at,"session_close":SESSION.closed_at,
        "interval_seconds":900,"observed_constituents":1,"source_empty_constituents":14,"synthetic_constituents":0,
        "synthetic":False,"no_trade":False,"synthetic_open":False,"synthetic_close":False,
        "native_first_opened_at":START.isoformat(),"native_last_opened_at":START.isoformat(),"evidence_json":json.dumps(evidence)}


def bar(at=START, price=100, volume=10, **kw):
    return {"timestamp":at.isoformat(),"open":price,"high":price+1,"low":price-1,"close":price,"volume":volume,**kw}


def response(provider, records=(), received=NOW):
    return {"provider":provider,"source":provider+":native","dataset":provider+".NATIVE","symbol":"TEST",
        "receipt_id":provider+"-receipt","payload_sha256":("a" if provider=="schwab" else "b")*64,
        "status":"ok","response_complete":True,"request_start":START.isoformat(),"request_end":END.isoformat(),
        "received_at":received.isoformat(),"native_interval_seconds":300 if provider=="fmp" else 60,"records":list(records),
        "price_context":{"before":{"timestamp":START.isoformat(),"received_at":received.isoformat(),"price":100},
                         "after":{"timestamp":END.isoformat(),"received_at":received.isoformat(),"price":100}}}


def assessment(schwab=(), fmp=()):
    return assess_interval_gap("TEST",START,END,as_of=NOW,
        evidence=[response("databento"),response("schwab",schwab),response("fmp",fmp)],
        anchor={"timestamp":START.isoformat(),"received_at":START.isoformat(),"price":100,"receipt_id":"anchor",
            "source":"databento:native","payload_sha256":"c"*64,"kind":"observed_trade_close","is_synthetic":False})


def rehash(proof):
    proof["assessment_sha256"]=hashlib.sha256(json.dumps({k:v for k,v in proof.items() if k!="assessment_sha256"},sort_keys=True,separators=(",",":"),default=str).encode()).hexdigest()
    return proof


def test_primary_wrapper_preserves_native_prices_times_and_full_original_proof():
    original=primary();wrapped=wrap_primary(original)
    assert wrapped["source_id"]==SOURCE_ID and wrapped["source_dataset"]==SOURCE_DATASET
    assert wrapped["population_contract"]==POPULATION_CONTRACT
    assert wrapped["native_source_dataset"]=="EQUS.MINI" and wrapped["observation_kind"]=="primary_observed"
    for key in ("open","high","low","close","volume"):assert wrapped[key]==original[key]
    assert json.loads(wrapped["evidence_json"])["native_primary"]["evidence_json"]==original["evidence_json"]
    assert verified_recovery_candle(wrapped)
    assert wrap_primary(wrapped)==wrapped
    assert validate_candles(pd.DataFrame([wrapped])).shape[0]==1


def test_primary_legacy_unverified_synthetic_cannot_be_promoted():
    old=primary();old["synthetic"]=True
    with pytest.raises(ContractError):wrap_primary(old)
    assert not verified_recovery_candle(primary())


def test_schwab_observed_preference_keeps_one_source_and_actual_receipt():
    proof=assessment([bar(),bar(START+pd.Timedelta(minutes=14),price=102)],[bar(price=900)])
    candle=recovery_candle(proof,SESSION)
    assert candle["native_source_id"]=="schwab:native"
    assert (candle["open"],candle["high"],candle["low"],candle["close"],candle["volume"])==(100,103,99,102,20)
    assert candle["observed_constituents"]==2 and candle["source_empty_constituents"]==13
    assert candle["native_interval_seconds"]==60 and candle["available_at"]==NOW.isoformat()
    assert not candle["synthetic"] and not candle["no_trade"] and verified_recovery_candle(candle)


def test_fmp_native_five_minutes_never_expands_to_fake_minutes():
    proof=assessment(fmp=[bar(),bar(START+pd.Timedelta(minutes=10),price=102)])
    candle=recovery_candle(proof,SESSION)
    assert candle["observed_constituents"]==2 and candle["source_empty_constituents"]==1
    assert candle["native_interval_seconds"]==300 and candle["synthetic_constituents"]==0
    assert verified_recovery_candle(candle)


def test_three_source_carry_is_derived_not_verified_no_trade_or_native_observation():
    proof=assessment();candle=recovery_candle(proof,SESSION)
    assert candle["synthetic"] and not candle["no_trade"]
    assert candle["observation_kind"]=="corroborated_price_carry_estimate"
    assert candle["observed_constituents"]==0 and candle["synthetic_constituents"]==1
    assert candle["volume"]==0 and candle["available_at"]==NOW.isoformat()
    evidence=json.loads(candle["evidence_json"])
    assert evidence["market_wide_no_trade_verified"] is False
    assert verified_recovery_candle(candle)


def test_incomplete_conflicting_or_modified_assessments_remain_unavailable():
    proof=assessment();proof["status"]="incomplete"
    assert recovery_candle(proof,SESSION) is None
    proof=assessment();proof["derived_allowed"]=False;rehash(proof)
    assert recovery_candle(proof,SESSION) is None
    assert recovery_candle(assessment(),SessionBounds("other","2026-10-13T08:00:00Z","2026-10-14T00:00:00Z")) is None


@pytest.mark.parametrize("mutation",[
    {"received_at":"2026-10-12T14:20:00Z"}, {"native_interval_seconds":1},
    {"dataset":"UNKNOWN"}, {"payload_sha256":"changed"}, {"records":[bar(is_synthetic=True)]},
    {"records":[bar(is_derived=True)]}, {"records":[bar(volume=0)]}, {"records":[bar(volume=True)]},
    {"records":[bar(),bar(price=200)]}, {"records":[bar(START+pd.Timedelta(seconds=1))]},
    {"records":[bar(synthetic_constituents=1)]},
    {"records":[bar(synthetic_open=True)]}, {"records":[bar(symbol="OTHER")]},
])
def test_candidate_validation_cannot_be_bypassed_by_rehashing_assessment(mutation):
    proof=assessment(schwab=[bar()]);proof["observed_candidates"][0].update(mutation);rehash(proof)
    assert recovery_candle(proof,SESSION) is None


@pytest.mark.parametrize("key,value",[("close",999),("available_at","2026-10-12T14:14:00Z"),
    ("synthetic",True),("no_trade",True),("native_interval_seconds",300),("source_id","other"),
    ("observed_constituents",15),("session_id","changed")])
def test_modified_frozen_candle_fails_validation(key,value):
    candle=recovery_candle(assessment(schwab=[bar()]),SESSION);candle[key]=value
    assert not verified_recovery_candle(candle)


@pytest.mark.parametrize("key,value",[("synthetic",1),("no_trade",0),("synthetic_open",1),
    ("synthetic_close",1),("volume",False),("observed_constituents",False),
    ("synthetic_constituents",True),("source_empty_constituents",False)])
def test_derived_marker_and_numeric_types_cannot_be_coerced(tmp_path,key,value):
    candle=recovery_candle(assessment(),SESSION);candle[key]=value
    assert not verified_recovery_candle(candle)
    with pytest.raises(ContractError):save_recovery_candle(tmp_path,candle)


def test_identical_native_duplicate_does_not_double_count_volume():
    candle=recovery_candle(assessment(schwab=[bar(),bar()]),SESSION)
    assert candle["volume"]==10 and candle["observed_constituents"]==1 and verified_recovery_candle(candle)


def test_equal_receipt_conflicts_do_not_arbitrarily_pick_a_revision():
    proof=assessment(schwab=[bar()])
    other=copy.deepcopy(proof["observed_candidates"][0]);other["records"]=[bar(price=101)]
    proof["observed_candidates"].append(other);rehash(proof)
    assert recovery_candle(proof,SESSION) is None


def test_catalog_absence_and_primary_save_do_not_create_files(tmp_path):
    assert load_recovery_candles(tmp_path,"TEST")==[]
    assert save_recovery_candle(tmp_path,wrap_primary(primary())) is None
    assert list(tmp_path.iterdir())==[]


def test_recovery_catalog_retains_original_objects_and_selects_at_cutoff(tmp_path):
    first=recovery_candle(assessment(schwab=[bar()]),SESSION)
    original=save_recovery_candle(tmp_path,first)
    initial=Path(original["path"]).read_bytes()
    assert save_recovery_candle(tmp_path,first)==original
    proof=assessment(schwab=[bar(price=101)])
    later=NOW+pd.Timedelta(seconds=1)
    proof["as_of"]=later.isoformat()
    proof["observed_candidates"][0]["received_at"]=later.isoformat()
    for ref in proof["source_references"]:
        if ref["canonical_provider"]=="schwab":ref["received_at"]=later.isoformat()
    rehash(proof)
    second=recovery_candle(proof,SESSION);save_recovery_candle(tmp_path,second)
    assert Path(original["path"]).read_bytes()==initial
    assert load_recovery_candles(tmp_path,"TEST")[0]["close"]==101
    assert load_recovery_candles(tmp_path,"TEST",cutoff=NOW)[0]["close"]==100
    assert load_recovery_candles(tmp_path,"TEST",cutoff=NOW-pd.Timedelta(seconds=1))==[]
    assert load_recovery_candles(tmp_path,"OTHER")==[]
    assert load_recovery_candles(tmp_path,"TEST",start=END)==[]
    assert load_recovery_candles(tmp_path,"TEST",end=START)==[]


def test_derived_catalog_round_trip_does_not_relabel_estimate(tmp_path):
    candle=recovery_candle(assessment(),SESSION)
    save_recovery_candle(tmp_path,candle)
    saved=load_recovery_candles(tmp_path,"TEST")[0]
    assert saved["synthetic"] is True and saved["no_trade"] is False
    assert saved==candle


def test_catalog_detects_changed_object_and_missing_index(tmp_path):
    ref=save_recovery_candle(tmp_path,recovery_candle(assessment(schwab=[bar()]),SESSION))
    path=Path(ref["path"]);original=path.read_bytes();path.write_bytes(original+b" ")
    with pytest.raises(ContractError,match="checksum"):load_recovery_candles(tmp_path,"TEST")
    path.write_bytes(original)
    with sqlite3.connect(ref["catalog"]) as db:db.execute("DROP INDEX candles_symbol_time")
    with pytest.raises(ContractError,match="index"):load_recovery_candles(tmp_path,"TEST")


def test_catalog_conflicting_equal_availability_revisions_are_explicit_error(tmp_path):
    for price in (100,101):
        save_recovery_candle(tmp_path,recovery_candle(assessment(schwab=[bar(price=price)]),SESSION))
    with pytest.raises(ContractError,match="conflict"):load_recovery_candles(tmp_path,"TEST")


def test_catalog_bounds_are_enforced_before_unbounded_object_reads(tmp_path,monkeypatch):
    from ml.intraday import recovered_population as module
    save_recovery_candle(tmp_path,recovery_candle(assessment(schwab=[bar()]),SESSION))
    monkeypatch.setattr(module,"MAX_CATALOG_ROWS",0)
    with pytest.raises(ContractError,match="bounded per-symbol"):load_recovery_candles(tmp_path,"TEST")
    monkeypatch.setattr(module,"MAX_CATALOG_ROWS",100000)
    monkeypatch.setattr(module,"MAX_LOAD_BYTES",1)
    with pytest.raises(ContractError,match="bounded read"):load_recovery_candles(tmp_path,"TEST")


def test_mixed_pandas_observed_and_derived_rows_preserve_nullable_native_clocks(tmp_path):
    from ml.intraday.coverage import population_frame
    from ml.intraday.data import completed_15m_observation
    frame=pd.DataFrame([wrap_primary(primary()),recovery_candle(assessment(),SESSION)])
    for key in ("native_first_opened_at","native_last_opened_at"):
        frame[key]=pd.to_datetime(frame[key],utc=True)
    rows=frame.to_dict("records")
    assert pd.isna(rows[1]["native_first_opened_at"])
    assert all(verified_recovery_candle(row) for row in rows)
    assert all(completed_15m_observation(row,from_frame=True) for row in rows)
    assert len(population_frame(frame,POPULATION_CONTRACT))==2
    ref=save_recovery_candle(tmp_path,rows[1])
    assert json.loads(Path(ref["path"]).read_bytes())["native_first_opened_at"] is None
    rows[1]["synthetic"]=1
    assert not verified_recovery_candle(rows[1])


@pytest.mark.parametrize("defect",["two_references","stale_anchor","false_anchor","future_receipt","missing_diagnostic",
    "contradiction","wrong_availability","bad_anchor_hash"])
def test_rehashed_derived_assessment_must_still_satisfy_retained_eligibility(defect):
    proof=assessment()
    if defect=="two_references":proof["source_references"].pop()
    if defect=="stale_anchor":proof["anchor"]["timestamp"]=(START-pd.Timedelta(minutes=16)).isoformat()
    if defect=="false_anchor":proof["anchor_valid"]=False
    if defect=="future_receipt":proof["source_references"][0]["received_at"]=(NOW+pd.Timedelta(seconds=1)).isoformat()
    if defect=="missing_diagnostic":proof["diagnostics"].pop()
    if defect=="contradiction":proof["diagnostics"][0]["reasons"]=["observed_activity_present"]
    if defect=="wrong_availability":proof["available_at"]=END.isoformat()
    if defect=="bad_anchor_hash":proof["anchor"]["payload_sha256"]="missing"
    rehash(proof)
    assert recovery_candle(proof,SESSION) is None


@pytest.mark.parametrize("receipt_microseconds", [0, 134631])
def test_history_loader_selects_older_eligible_recovery_before_newer_revision(tmp_path, receipt_microseconds):
    from ml.intraday.data import load_local_history
    folder=tmp_path/"stocks/TEST/bars/1m/databento"
    (folder/"normalized").mkdir(parents=True);(folder/"raw").mkdir()
    native=pd.DataFrame([{"timestamp":START-pd.Timedelta(minutes=15),"open":99.,"high":100.,"low":98.,"close":99.,"volume":10.}])
    native.to_parquet(folder/"normalized/TEST_fixture_ohlcv-1m_1m.parquet",index=False)
    raw=native.rename(columns={"timestamp":"ts_event"}).assign(symbol="TEST",provider_dataset="EQUS.MINI",
        source_schema="ohlcv-1m",price_basis="unadjusted_market_scale",range_start=(START-pd.Timedelta(minutes=15)).isoformat(),range_end=END.isoformat())
    raw.to_parquet(folder/"raw/TEST_fixture_ohlcv-1m_1m_raw.parquet",index=False)
    received=NOW+pd.Timedelta(microseconds=receipt_microseconds)
    previous=assessment()["anchor"]
    initial=assess_interval_gap("TEST",START,END,as_of=received,anchor=previous,
        evidence=[response("databento",received=received),response("schwab",[bar()],received=received),response("fmp",received=received)])
    save_recovery_candle(tmp_path,recovery_candle(initial,SESSION))
    later=received+pd.Timedelta(minutes=1)
    proof=assess_interval_gap("TEST",START,END,as_of=later,anchor=previous,
        evidence=[response("databento",received=later),response("schwab",[bar(price=101)],received=later),response("fmp",received=later)])
    save_recovery_candle(tmp_path,recovery_candle(proof,SESSION))
    history=load_local_history(tmp_path,"TEST",end=END,population_contract=POPULATION_CONTRACT,recovery_cutoff=received)
    target=history.frame.loc[history.frame.opened_at.eq(START)].iloc[0]
    assert target.close==100 and target.available_at==received
    assert history.manifest["recovery_selection_cutoff"]==received.isoformat()
    assert history.frame.available_at.dt.tz is not None
    assert all(verified_recovery_candle(row) for row in history.frame.to_dict("records"))
    recent=load_local_history(tmp_path,"TEST",end=END,population_contract=POPULATION_CONTRACT,recovery_cutoff=later)
    assert recent.frame.loc[recent.frame.opened_at.eq(START)].iloc[0].close==101


@pytest.mark.parametrize("contract",[None,PRIMARY_CONTRACT,POPULATION_CONTRACT])
def test_supervisor_forwards_frozen_recovery_cutoff_only_for_new_contract(tmp_path,contract):
    from test_intraday_supervisor import run, NOW as cutoff
    if contract is not None:
        directory=tmp_path/"intraday";directory.mkdir()
        (directory/"data-policy.json").write_text(json.dumps({"schema_version":"intraday-data-policy-v1",
            "population_contract":contract,"market_inputs_enabled":False}))
    captured=[]
    def loader(root,symbol,**kwargs):
        captured.append(kwargs)
        raise RuntimeError("Stop fixture after observing loader arguments; no fitting")
    run(tmp_path,loader=loader)
    assert len(captured)==1
    if contract==POPULATION_CONTRACT:
        assert captured[0]["recovery_cutoff"]==cutoff
    else:
        assert "recovery_cutoff" not in captured[0]
