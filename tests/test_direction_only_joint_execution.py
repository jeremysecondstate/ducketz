"""Current accepted instructions execute at live capacity without projected fills."""
from copy import deepcopy
from dataclasses import replace
import json

import pandas as pd
import pytest

from test_independent_stock_runtime import environment, _decisions
from test_gameplan_direction_runtime import prepare
from ml.gameplan_execution_policy import current_gameplan_policy_metadata, direction_only_rows
from ml.stock_trader import gameplan_execution as execution
from ml.stock_trader import independent_runtime as runtime
from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY


DAY = "2026-10-20"


def forecast(*, symbol="AAPL", horizon="1h", start="11:00", end="12:00", probability=.5001,
             eligible=True, identity=None, **extra):
    return dict(id=identity or f"{DAY}:{symbol}:{horizon}@{start}", symbol=symbol, model_group=horizon,
        action_date=DAY, direction="NO_EDGE", calibrated_probability=probability, raw_probability=.4723,
        target_window_start=f"{DAY}T{start}:00Z", target_window_end=f"{DAY}T{end}:00Z",
        frozen_at=f"{DAY}T06:00:00Z", decision_timestamp=f"{DAY}T06:00:00Z",
        target_contract_version="independent-stock-targets-v1",
        target_price_source_contract="observed-multi-source-stock-v2", target_role="EXECUTION",
        model_status="RESEARCH_NO_TARGET_HISTORY", symbol_fitted_target_rows=0,
        symbol_route_fitted_target_rows=0, execution_eligible=eligible, **extra)


def plan(*rows):
    frame, ledger = direction_only_rows(pd.DataFrame(rows or [forecast()]), action_date=DAY)
    return {**current_gameplan_policy_metadata(DAY), "action_date":DAY,
        "as_of":f"{DAY}T06:00:00Z", "plan_sha256":"a"*64,
        "forecasts":frame.astype(object).where(frame.notna(),None).to_dict("records"), "ledger":ledger}


@pytest.mark.parametrize("probability,expected", [(.5001,"BUY"), (.4999,"SELL"),
    (.50010000001,"BUY"), (.49989999999,"SELL"), (.5,"HOLD"), (.4999999,"HOLD")])
def test_original_probability_and_target_have_no_history_or_projected_quantity_gate(probability, expected):
    saved = plan(forecast(probability=probability))
    before = deepcopy(saved)
    signals = execution.accepted_direction_only_signals(saved, as_of=f"{DAY}T11:37:00Z", source_fingerprint="saved")
    signal = signals["AAPL","1h"]
    assert signal.calibrated_probability == probability
    assert signal.horizon_probabilities == {"1h":probability}
    assert signal.planned_quantity is None and signal.catchup_components == ()
    assert signal.prediction_id == saved["forecasts"][0]["id"]
    assert signal.target_window_start == f"{DAY}T11:00:00+00:00"
    assert signal.target_window_end == f"{DAY}T12:00:00+00:00"
    assert signal.target_definition_version == "independent-stock-targets-v1"
    assert signal.target_price_source_contract == "observed-multi-source-stock-v2"
    assert saved["forecasts"][0]["direction_based_action"] == expected
    assert saved == before


def test_latest_due_instruction_per_horizon_preserves_hold_and_longer_target():
    saved = plan(forecast(start="11:00",end="15:00",probability=.7),
                 forecast(start="12:00",end="13:00",probability=.5),
                 forecast(start="13:00",end="14:00",probability=.4),
                 forecast(horizon="4h",end="15:00",probability=.4999),
                 forecast(symbol="MSFT",probability=.8,eligible=False))
    signals = execution.accepted_direction_only_signals(saved, as_of=f"{DAY}T12:15:00Z", source_fingerprint="saved")
    assert set(signals) == {("AAPL","1h"),("AAPL","4h")}
    assert signals["AAPL","1h"].calibrated_probability == .5
    assert signals["AAPL","1h"].prediction_id.endswith("@12:00")
    assert signals["AAPL","4h"].target_window_start == f"{DAY}T11:00:00+00:00"
    assert signals["AAPL","4h"].actionable_until == f"{DAY}T15:00:00+00:00"
    # A newer expired slot must not restore the older overlapping target.
    assert ("AAPL","1h") not in execution.accepted_direction_only_signals(
        saved, as_of=f"{DAY}T14:30:00Z", source_fingerprint="saved")


@pytest.mark.parametrize("as_of", ["2026-10-20T10:59:59Z", "2026-10-21T00:00:00Z", "2026-10-21T11:01:00Z"])
def test_current_recipe_respects_action_date_and_existing_session_hours(as_of):
    saved = plan(forecast(horizon="1w",end="23:59"))
    assert execution.accepted_direction_only_signals(saved, as_of=as_of, source_fingerprint="saved") == {}


def test_current_recipe_keeps_inclusive_peer_policy_identity_and_original_row():
    saved = plan(forecast(probability=.4999))
    for item in [saved, saved["ledger"], *saved["forecasts"]]:
        item["direction_policy_version"] = "stock-direction-5001-4999-v3"
    before = deepcopy(saved)
    signal = execution.accepted_direction_only_signals(saved, as_of=f"{DAY}T11:01:00Z", source_fingerprint="saved")["AAPL","1h"]
    assert signal.calibrated_probability == .4999
    assert saved == before


@pytest.mark.parametrize("damage", ["duplicate", "backwards", "missing_time", "different_date"])
def test_malformed_current_execution_identity_or_window_is_rejected(damage):
    saved = plan(forecast())
    row = saved["forecasts"][0]
    if damage == "duplicate":
        saved["forecasts"].append(deepcopy(row))
    elif damage == "backwards":
        row["target_window_end"] = row["target_window_start"]
    elif damage == "missing_time":
        row["target_window_start"] = None
    else:
        row["action_date"] = "2026-10-19"
    with pytest.raises(ValueError, match="trading IDs|holding windows|action date"):
        execution.accepted_direction_only_signals(saved, as_of=f"{DAY}T11:01:00Z", source_fingerprint="saved")


def test_accepted_current_loader_bypasses_legacy_event_projection(tmp_path, monkeypatch):
    saved = plan(forecast())
    run = tmp_path / "ml/joint-capital-accepted/saved"
    monkeypatch.setattr("ml.joint_capital_adoption.read_accepted_joint_plan", lambda *_: (saved,{},run))
    monkeypatch.setattr(execution,"_assert_execution_deployment",lambda *a,**k:None)
    monkeypatch.setattr("ml.stock_trader.catchup.catchup_signals",lambda *a,**k:pytest.fail("Current directions have no projected events"))
    signals,sources = execution.load_execution_signals(tmp_path,as_of=f"{DAY}T11:01:00Z")
    assert signals["AAPL","1h"].planned_quantity is None
    assert sources == (run/"accepted-plan.json",run/"joint-plan.json")


def test_legacy_accepted_loader_derives_fresh_intent_from_original_probability(tmp_path, monkeypatch):
    saved = {"action_date":DAY,"ledger":{"events":[]}, "forecasts":[forecast(probability=.48)]}
    original = deepcopy(saved)
    run = tmp_path / "ml/joint-capital-accepted/legacy"
    run.mkdir(parents=True)
    for name in ("accepted-plan.json", "joint-plan.json"):
        (run / name).write_text(json.dumps(saved))
    monkeypatch.setattr("ml.joint_capital_adoption.read_accepted_joint_plan", lambda *_: (saved,{},run))
    monkeypatch.setattr(execution,"_assert_execution_deployment",lambda *a,**k:None)
    monkeypatch.setattr("ml.stock_trader.catchup.catchup_signals",
        lambda *a,**k:pytest.fail("Fresh orders must not use projected quantities or synthetic probabilities"))
    signals,_ = execution.load_execution_signals(tmp_path,as_of=f"{DAY}T11:01:00Z")
    signal = signals["AAPL", "1h"]
    assert signal.calibrated_probability == .48 and signal.planned_quantity is None
    assert signal.catchup_components == () and signal.prediction_id == original["forecasts"][0]["id"]
    assert saved == original


@pytest.mark.parametrize("probability,side", [(.5001,"BUY"),(.4999,"SELL")])
def test_synthetic_runtime_refreshes_empty_ledger_plan_and_uses_live_capacity(environment,monkeypatch,probability,side):
    env=environment
    prepare(env,monkeypatch,probability=probability)
    env.now=pd.Timestamp(f"{DAY}T11:01:00Z")
    saved=plan(forecast(probability=probability))
    run=env.root/"ml/joint-capital-accepted/saved"
    run.mkdir(parents=True)
    for name in ("accepted-plan.json","joint-plan.json"):
        (run/name).write_text(json.dumps(saved))
    monkeypatch.setattr("ml.joint_capital_adoption.read_accepted_joint_plan",lambda *_:(saved,{},run))
    monkeypatch.setattr("ml.joint_capital_adoption.assert_accepted_execution",lambda *a,**k:True)
    monkeypatch.setattr("ml.stock_trader.contracts.execution_symbols",lambda *a,**k:tuple(env.held))
    original_capture=runtime.capture_portfolio_state
    def capture(*args,symbols=None,**kwargs):
        assert set(symbols) == set(env.held)
        return original_capture(*args,**kwargs)
    monkeypatch.setattr(runtime,"capture_portfolio_state",capture)
    monkeypatch.setattr(execution,"_assert_execution_deployment",lambda *a,**k:None)
    monkeypatch.setattr(runtime,"_assert_execution_deployment",lambda *a,**k:None)
    monkeypatch.setattr(runtime,"_loaded_gameplan_run",lambda *a,**k:run)
    monkeypatch.setattr(runtime,"_loaded_fallback_policy",lambda *a,**k:(None,None))
    monkeypatch.setattr(runtime,"load_current_independent_gameplan_signals",lambda root,**k:execution.load_execution_signals(root,as_of=env.now))
    monkeypatch.setattr("ml.stock_trader.catchup.catchup_signals",lambda *a,**k:pytest.fail("Current recipe must retain own original probability after capture"))
    result=runtime.run_independent_stock_trader_once(env.root,execute=True,session=env.broker,
        runtime_clock=lambda:env.now,session_managed=True,sizing_policy=GAMEPLAN_SIZING_POLICY)
    assert result.status == "ORDERS_SUBMITTED" and result.submitted_orders == 1, result.error
    payload,=env.broker.submissions
    leg,=payload["orderLegCollection"]
    assert leg["instruction"] == side and leg["quantity"] > 0
    if side == "BUY":
        assert leg["quantity"] > 1  # The tiny probability margin does not shrink the live cash budget.
    recorded=_decisions(result)
    decision,=recorded["decisions"]
    assert decision["prediction"]["calibrated_probability"] == probability
    assert decision["prediction"]["planned_quantity"] is None
    assert recorded["prediction_handoff"]["direction_only_execution"]["planned_quantities"] is False
    assert "catchup" not in recorded["prediction_handoff"]
