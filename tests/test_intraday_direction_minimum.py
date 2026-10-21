"""Valid directions retain a whole-share opportunity within the existing scale."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pandas as pd
import pytest

from ml.intraday import starter_sizing
from ml.intraday.capacity import clip_to_capacity
from ml.intraday.intentions import IntentionStore
from ml.intraday.prediction import ContractError, digest
from ml.intraday.store import ShadowStore
from test_intraday_starter_sizing import fixture as sizing_fixture
from tests.test_intraday_store import forecast as forecast_fixture


def low_score_bundle(probability=.4999, *, equity=7000.):
    # A rising market opposes SELL sizing; a falling market opposes BUY sizing.
    # Synthetic equity105/0.015 and side price105 give exactly one reference share.
    bundle = sizing_fixture(probability=probability, direction=1 if probability < .5 else -1)
    return (*bundle[:2], replace(bundle[2], value=equity), replace(bundle[3], bid=105., ask=105.))


def calculate(bundle, **changes):
    forecast, snapshot, equity, quote = bundle
    values = dict(forecast_snapshot=snapshot, account_equity=equity, quote=quote,
                  calculated_at=forecast["issued_at"], maximum_quote_age_seconds=60.)
    return starter_sizing.size_forecast(forecast, **(values | changes))


@pytest.mark.parametrize("probability,side", [(.5001, "BUY"), (.4999, "SELL"), (.4978, "SELL")])
def test_valid_direction_keeps_one_share_when_the_independent_scale_affords_it(probability, side):
    bundle = low_score_bundle(probability)
    original = deepcopy(bundle)
    result = calculate(bundle).payload()
    assert result["status"] == "sized" and result["side"] == side
    assert result["desired_quantity"] == 1
    assert Decimal(result["symbol_reference"]["shares"]) == 1
    assert Decimal(result["explanation"]["raw_quantity"]) < Decimal(".5")
    policy = result["direction_quantity_policy"]
    assert policy["original_ta_desired_quantity"] == 0
    assert result["desired_quantity"] == result["explanation"]["desired_quantity"] == 1
    assert result["explanation"]["ta_rounded_quantity"] == 0
    assert result["explanation"]["ta_rounded_at_or_above_reference"] is False
    assert result["explanation"]["rounded_at_or_above_reference"] is True
    assert policy["applied"] is True and policy["effective_desired_quantity"] == 1
    assert policy["version"] == starter_sizing.DIRECTION_QUANTITY_POLICY_VERSION
    assert policy["authority"] == "human_direction_2026_10_19"
    assert policy["actual_capacity_evaluated"] is False and result["adjusted_quantity"] is None
    assert result["orders_enabled"] is False
    assert result["starter_policy_version"] == "intraday-independent-direction-ta-v2"
    assert result["initial_configuration_authority"] == "human_approved_2026_10_12"
    probability_term = next(t for t in result["term_contributions"] if t["feature"] == "probability_not_down")
    assert probability_term["value"] == probability
    assert bundle == original
    for field in ("forecast_id", "reference_at", "target_open", "target_close"):
        assert result[field] == bundle[0][field]


@pytest.mark.parametrize("probability", [.5001, .4999])
@pytest.mark.parametrize("equity", [6999., 100.])
def test_genuine_subshare_reference_still_rounds_to_zero(probability, equity):
    result = calculate(low_score_bundle(probability, equity=equity)).payload()
    assert Decimal(result["symbol_reference"]["shares"]) < 1
    assert result["desired_quantity"] == 0
    assert result["direction_quantity_policy"]["applied"] is False
    assert result["direction_quantity_policy"]["original_ta_desired_quantity"] == 0


@pytest.mark.parametrize("probability", [.5, .50005, .49995])
def test_hold_never_becomes_an_order(probability):
    result = calculate(low_score_bundle(probability)).payload()
    assert result["side"] == "HOLD" and result["status"] == "unavailable"
    assert result["desired_quantity"] is None
    assert result["reason"] == "neutral_probability_no_direction"
    assert result["direction_quantity_policy"]["applied"] is False


@pytest.mark.parametrize("probability", [.5001, .4999])
def test_minimum_still_obeys_actual_zero_and_smaller_capacity(probability):
    bundle = low_score_bundle(probability)
    result = calculate(bundle).payload()
    values = dict(symbol=bundle[0]["symbol"], side=result["side"], desired_quantity=result["desired_quantity"],
                  limit_price="105", available_cash="0", available_shares=0,
                  capacity_evidence_id="synthetic-own-snapshot", observed_at=bundle[0]["issued_at"],
                  calculated_at=bundle[0]["issued_at"])
    blocked = clip_to_capacity(**values)
    assert blocked.desired_quantity == 1 and blocked.adjusted_quantity == 0
    assert blocked.disposition == ("no_purchasing_capacity" if result["side"] == "BUY"
                                   else "no_eligible_uncommitted_shares")
    allowed = clip_to_capacity(**(values | dict(available_cash="105", available_shares=11)))
    assert allowed.adjusted_quantity == 1 and allowed.disposition == "full_intention_possible"
    assert result["desired_quantity"] == 1 and result["adjusted_quantity"] is None


@pytest.mark.parametrize("probability", [.72, .2])
def test_already_positive_technical_size_and_evidence_stay_unchanged(monkeypatch, probability):
    bundle = sizing_fixture(probability=probability)
    result = calculate(bundle).payload()
    with monkeypatch.context() as patch:
        patch.setattr(starter_sizing, "_apply_direction_minimum", lambda payload: payload)
        pure_ta = calculate(bundle).payload()
    assert result["desired_quantity"] == pure_ta["desired_quantity"] > 0
    assert result["explanation"] == pure_ta["explanation"]
    assert result["direction_quantity_policy"]["applied"] is False
    assert {key: value for key, value in result.items() if key != "direction_quantity_policy"} == pure_ta


def test_missing_context_and_expired_low_score_are_not_promoted():
    bundle = low_score_bundle()
    unavailable = calculate(bundle, quote=None).payload()
    assert unavailable["desired_quantity"] is None and unavailable["status"] == "unavailable"
    assert unavailable["direction_quantity_policy"]["applied"] is False
    later = (pd.Timestamp(bundle[0]["target_close"]) + pd.Timedelta(seconds=1)).isoformat()
    quote = replace(bundle[3], observed_at=later, available_at=later)
    expired = calculate(bundle, calculated_at=later, quote=quote).payload()
    assert expired["calculated_after_target"] is True and expired["desired_quantity"] == 0
    assert expired["direction_quantity_policy"]["applied"] is False
    assert expired["orders_enabled"] is False
    assert expired["target_close"] == bundle[0]["target_close"]


def test_saved_zero_intention_is_immutable_after_the_new_policy(tmp_path, monkeypatch):
    bundle = low_score_bundle()
    original, snapshot, equity, quote = bundle
    store = ShadowStore(tmp_path / "synthetic-own-history.sqlite3")
    cycle = "synthetic-original-zero"
    frozen_id, frozen = store.freeze_inputs(cycle, original["symbol"], snapshot)
    payload = forecast_fixture(pc="Atlas", cycle_id=cycle, mode="live")
    payload.update({key: value for key, value in original.items() if key != "forecast_id"})
    payload.update(snapshot_hash=frozen_id, information_available_at=original["issued_at"],
                   models=[dict(name=name, status="success", probability_not_down=original["probability_not_down"])
                           for name in ("a", "b")])
    issued = store.save_forecast(payload)
    values = dict(forecast_snapshot=frozen, account_equity=equity, quote=quote,
                  calculated_at=issued["issued_at"], maximum_quote_age_seconds=60.)
    intentions = IntentionStore(store)
    with monkeypatch.context() as patch:
        patch.setattr(starter_sizing, "VERSION", "intraday-independent-initial-ta-v1")
        patch.setattr(starter_sizing, "_apply_direction_minimum", lambda value: value)
        old_sizing = starter_sizing.size_forecast(issued, **values)
        old = intentions.save_sizing(issued, old_sizing)
    assert old["desired_quantity"] == 0
    original_hash = digest(old)
    new = starter_sizing.size_forecast(issued, **values)
    assert new.desired_quantity == 1
    assert intentions.get(issued["forecast_id"]) == old
    with pytest.raises(ContractError, match="immutable"):
        intentions.save_sizing(issued, new)
    assert digest(intentions.get(issued["forecast_id"])) == original_hash
    assert store.forecast(cycle, original["symbol"]) == issued
