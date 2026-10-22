"""Policy evidence and recovery regressions; fixtures never call providers or fit."""
from copy import deepcopy
import json

import pytest

from ml.intraday import direction_metadata as metadata
from ml.intraday.intentions import IntentionStore
from ml.intraday.prediction import ContractError, digest, encoded
from ml.intraday.store import ShadowStore
from ml.intraday.technical_sizing import SizingResult, evaluate_size
from ml.intraday.telemetry import export_packet, validate_packet
from tests.test_intraday_store import forecast
from tests.test_intraday_technical_sizing import fixture
from tests.test_intraday_telemetry import source_store, PRODUCED


def payload(p=.72, **changes):
    return forecast(probability_not_down=p, models=[dict(name="fixture", status="success", probability_not_down=p)],
                    configured_count=1, successful_count=1, **changes)


def initial_rows(store):
    with store.connect() as db:
        return db.execute("SELECT id,forecast_id,recorded_at,payload FROM intents ORDER BY rowid").fetchall()


def legacy(store, p=.5, side="BUY"):
    value = payload(p)
    value.update(classification="not-down" if p >= .5 else "down", execution_eligibility="unresolved_shadow_only")
    key = digest(value)
    value["forecast_id"] = key
    intention = dict(forecast_id=key, side=side, desired_quantity=None, adjusted_quantity=None,
                     disposition="sizing_policy_unresolved", submission_status="not_submitted_shadow",
                     submitted_quantity=0, filled_quantity=0, realized_pnl=None)
    with store.connect() as db:
        db.execute("INSERT INTO forecasts VALUES(?,?,?,?,?,?,?)", (key,value["pc"],value["symbol"],value["cycle_id"],value["reference_at"],value["issued_at"],encoded(value)))
        db.execute("INSERT INTO intents VALUES(?,?,?,?)", (digest(intention),key,value["issued_at"],encoded(intention)))
    return value


@pytest.mark.parametrize("p,action,scientific", [
    (.4999,"SELL","down"), (.49990000000000007,"HOLD","down"),
    (.499999,"HOLD","down"), (.5,"HOLD","not-down"),
    (.500001,"HOLD","not-down"), (.5000999999999999,"HOLD","not-down"),
    (.5001,"BUY","not-down"),
    ("0.50009999999999999999999999999","HOLD","not-down"),
    ("0.49990000000000000000000000001","HOLD","down"),
])
def test_new_forecast_and_original_intention_freeze_exact_policy(tmp_path,p,action,scientific):
    store = ShadowStore(tmp_path/"evidence.sqlite3")
    f = store.save_forecast(payload(p))
    assert f["classification"] == scientific
    expected = dict(stock_action=action, direction_policy_version="stock-direction-5001-4999-v3",
                    direction_up_threshold=.5001,direction_down_threshold=.4999)
    assert metadata.recorded_direction(f) == expected
    original = json.loads(initial_rows(store)[0][3])
    assert all(original[k] == v for k,v in expected.items())
    assert original["side"] == action
    assert store.save_forecast(payload(p)) == f
    assert len(initial_rows(store)) == 1


def test_action_uses_saved_probability_not_recomputed_mean_within_tolerance(tmp_path):
    store = ShadowStore(tmp_path/"evidence.sqlite3")
    supplied = payload(.5000999999999)
    supplied["models"][0]["probability_not_down"] = .5001
    f = store.save_forecast(supplied)
    assert f["stock_action"] == "HOLD"
    assert f["classification"] == "not-down"


@pytest.mark.parametrize("field,value", [
    ("stock_action","SELL"),("direction_policy_version","stock-direction-50-v2"),
    ("direction_up_threshold",.5),("direction_down_threshold",.5),
])
def test_new_or_existing_explicit_conflicts_fail(tmp_path,field,value):
    store = ShadowStore(tmp_path/"evidence.sqlite3")
    with pytest.raises(ContractError):
        store.save_forecast(payload(**{field:value}))
    saved = store.save_forecast(payload())
    before = initial_rows(store)
    with pytest.raises(ContractError):
        store.save_forecast({**saved,field:value})
    assert initial_rows(store) == before
    assert store.forecast("cycle","AAPL") == saved


def test_store_keeps_original_derived_scientific_class_and_mode_normalization(tmp_path):
    store=ShadowStore(tmp_path/"evidence.sqlite3")
    saved=store.save_forecast(payload(classification="down",execution_eligibility="enabled"))
    assert saved["classification"] == "not-down"
    assert saved["execution_eligibility"] == "unresolved_shadow_only"
    clone={**saved,"cycle_id":"live-clone","mode":"live"}
    copied=store.save_forecast(clone)
    assert copied["execution_eligibility"] == "manual_live_checks_required"
    assert copied["classification"] == saved["classification"]


@pytest.mark.parametrize("change", [
    {"probability_not_down":.73,"models":[dict(name="fixture",status="success",probability_not_down=.73)]},
    {"target_open":"2026-10-08T17:45:00+00:00"},
])
def test_changed_actual_scientific_evidence_rejected_without_new_caller_gates(tmp_path,change):
    store=ShadowStore(tmp_path/"evidence.sqlite3")
    saved=store.save_forecast(payload())
    before=initial_rows(store)
    with pytest.raises(ContractError):store.save_forecast({**saved,**change})
    assert store.forecast("cycle","AAPL") == saved
    assert initial_rows(store) == before


@pytest.mark.parametrize("historical", [False,True])
def test_resave_preserves_exact_forecast_and_intent_bytes_under_policy_change(tmp_path,monkeypatch,historical):
    store = ShadowStore(tmp_path/"evidence.sqlite3")
    saved = legacy(store) if historical else store.save_forecast(payload(.500001))
    before = initial_rows(store)
    with store.connect() as db:
        raw_before = db.execute("SELECT payload FROM forecasts").fetchone()[0]
    monkeypatch.setattr(metadata,"current_direction",lambda p: pytest.fail("recovery must not stamp current policy"))
    assert store.save_forecast(payload(.5 if historical else .500001)) == saved
    assert store.save_forecast(saved) == saved
    assert initial_rows(store) == before
    with store.connect() as db:
        assert db.execute("SELECT payload FROM forecasts").fetchone()[0] == raw_before


def test_resave_cannot_omit_non_derived_evidence_or_change_original_intent(tmp_path):
    store = ShadowStore(tmp_path/"evidence.sqlite3")
    saved = store.save_forecast(payload(source_id="fixture-source"))
    omitted = dict(saved); omitted.pop("source_id")
    with pytest.raises(ContractError,match="immutable"):
        store.save_forecast(omitted)
    with pytest.raises(ContractError,match="intention is immutable"):
        store.save_forecast(saved,intent={"side":"SELL"})


@pytest.mark.parametrize("historical", [False,True])
def test_later_intent_event_retry_keeps_original_side_and_bytes(tmp_path,monkeypatch,historical):
    store = ShadowStore(tmp_path/"evidence.sqlite3")
    f = legacy(store) if historical else store.save_forecast(payload(.5))
    intentions = IntentionStore(store)
    kw=dict(event_key="fixture-event",recorded_at=f["issued_at"],disposition="fixture_wait",evidence={"reason":"fixture"},kind="execution_disposition")
    first = intentions.record(f,**kw)
    assert first["side"] == ("BUY" if historical else "HOLD")
    if historical:
        assert not set(metadata.FIELDS).intersection(first)
    else:
        assert metadata.recorded_direction({**first,"probability_not_down":f["probability_not_down"]}) == metadata.recorded_direction(f)
    before=initial_rows(store)
    monkeypatch.setattr(metadata,"stock_action",lambda p: "SELL")
    assert intentions.record(f,**kw) == first
    assert initial_rows(store) == before
    with pytest.raises(ContractError,match="changed evidence"):
        intentions.record(f,**{**kw,"evidence":{"reason":"changed"}})


def test_sizing_and_new_sized_intention_copy_frozen_policy(tmp_path):
    bundle=fixture();f,indicators,config,baseline,_,_=bundle
    f.pop("forecast_id");f.update(metadata.current_direction(f["probability_not_down"]));f["forecast_id"]=digest(f)
    result=evaluate_size(f,indicators,symbol_policy=config,hourly_reference=baseline,calculated_at=f["issued_at"])
    value=result.payload()
    assert all(value[k]==f[k] for k in metadata.FIELDS)
    store=ShadowStore(tmp_path/"evidence.sqlite3");saved=store.save_forecast(payload())
    sized=dict(forecast_id=saved["forecast_id"],symbol="AAPL",side="BUY",status="sized",desired_quantity=4,
               calculated_at=saved["issued_at"],**metadata.recorded_direction(saved))
    sized_result=SizingResult(digest(sized),encoded(sized))
    intentions=IntentionStore(store);first=intentions.save_sizing(saved,sized_result)
    assert all(first[k]==saved[k] for k in metadata.FIELDS)
    before=initial_rows(store)
    assert intentions.save_sizing(saved,sized_result)==first
    assert initial_rows(store)==before


def test_legacy_saved_sizing_recovery_does_not_inject_policy(tmp_path,monkeypatch):
    store=ShadowStore(tmp_path/"evidence.sqlite3");f=legacy(store)
    intentions=IntentionStore(store)
    value=dict(forecast_id=f["forecast_id"],symbol="AAPL",side="BUY",status="sized",desired_quantity=4,
               calculated_at=f["issued_at"],direction_policy_version="stock-direction-50-v2")
    result=SizingResult(digest(value),encoded(value))
    first=intentions.save_sizing(f,result);before=initial_rows(store)
    monkeypatch.setattr(metadata,"stock_action",lambda p:"SELL")
    assert intentions.save_sizing(f,result)==first
    assert initial_rows(store)==before
    assert not set(metadata.FIELDS).intersection(first)


def test_recorded_historical_policy_is_not_replaced_by_current_bands():
    value={"probability_not_down":.500001,"stock_action":"BUY",
           "direction_policy_version":"stock-direction-50-v2",
           "direction_up_threshold":.5,"direction_down_threshold":.5}
    assert metadata.current_direction(value["probability_not_down"])["stock_action"] == "HOLD"
    assert metadata.recorded_action(value) == "BUY"


def test_telemetry_preserves_new_metadata_and_accepts_legacy_absence(tmp_path):
    store,_=source_store(tmp_path/"new.sqlite3")
    packet=export_packet(store.path,producer_pc="Atlas",source_version="fixture",produced_at=PRODUCED)
    f=packet["payload"]["forecasts"][0]
    assert metadata.recorded_direction(f)["stock_action"]=="BUY"
    for key in metadata.FIELDS:
        historical=deepcopy(packet);projected=historical["payload"]["forecasts"][0]
        projected.pop(key)
        historical["packet_id"]=digest(historical["payload"])
        with pytest.raises(ContractError,match="incomplete"):
            validate_packet(historical,as_of=PRODUCED)
    historical=deepcopy(packet)
    for key in metadata.FIELDS:historical["payload"]["forecasts"][0].pop(key)
    historical["packet_id"]=digest(historical["payload"])
    assert validate_packet(historical,as_of=PRODUCED)==historical
    for key,value in (("stock_action","SELL"),("direction_policy_version","invented-v3"),
                      ("direction_up_threshold",.5),("direction_down_threshold",.5)):
        changed=deepcopy(packet);changed["payload"]["forecasts"][0][key]=value
        changed["packet_id"]=digest(changed["payload"])
        with pytest.raises(ContractError):validate_packet(changed,as_of=PRODUCED)
