"""Current valid directions retain whole shares before real capacity clipping."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from ml.intraday import starter_sizing
from ml.intraday.capacity import clip_to_capacity
from ml.intraday.prediction import ContractError, digest
from test_intraday_starter_sizing import fixture as sizing_fixture


def low_score(probability, *, equity=7000.):
    # Opposing observed technical evidence makes the old mixed score round zero.
    bundle = sizing_fixture(probability=probability, direction=1 if probability < .5 else -1)
    return (*bundle[:2], replace(bundle[2], value=equity), replace(bundle[3], bid=105., ask=105.))


def calculate(bundle, **changes):
    forecast, snapshot, equity, quote = bundle
    values = dict(forecast_snapshot=snapshot, account_equity=equity, quote=quote,
                  calculated_at=forecast["issued_at"], maximum_quote_age_seconds=60.)
    return starter_sizing.size_forecast(forecast, **(values | changes)).payload()


@pytest.mark.parametrize("probability,side", [(.5001, "BUY"), (.4999, "SELL"), (.4978, "SELL")])
def test_low_score_cannot_suppress_valid_whole_share_direction(probability, side):
    bundle = low_score(probability)
    frozen = deepcopy(bundle)
    result = calculate(bundle)
    assert result["status"] == "sized" and result["side"] == side
    assert Decimal(result["symbol_reference"]["shares"]) == 1
    assert Decimal(result["explanation"]["raw_quantity"]) < Decimal(".5")
    assert result["desired_quantity"] == result["explanation"]["desired_quantity"] == 1
    metadata = result["direction_quantity_policy"]
    assert metadata["original_ta_desired_quantity"] == 0
    assert metadata["effective_desired_quantity"] == 1 and metadata["applied"] is True
    assert metadata["actual_capacity_evaluated"] is False
    assert metadata["version"] == "intraday-valid-direction-whole-share-v1"
    assert metadata["authority"] == "human_direction_2026_10_19"
    assert result["starter_policy_version"] == "intraday-independent-direction-ta-v2"
    assert result["adjusted_quantity"] is None and result["orders_enabled"] is False
    assert result["explanation"]["ta_rounded_quantity"] == 0
    assert result["explanation"]["rounded_at_or_above_reference"] is True
    assert result["explanation"]["ta_rounded_at_or_above_reference"] is False
    probability_term = next(t for t in result["term_contributions"] if t["feature"] == "probability_not_down")
    assert probability_term["value"] == probability
    for key in ("forecast_id", "reference_at", "target_open", "target_close"):
        assert result[key] == bundle[0][key]
    assert bundle == frozen


@pytest.mark.parametrize("probability", [.5001, .4999])
@pytest.mark.parametrize("equity", [6999., 100.])
def test_genuine_subshare_scale_remains_zero(probability, equity):
    result = calculate(low_score(probability, equity=equity))
    assert Decimal(result["symbol_reference"]["shares"]) < 1
    assert result["desired_quantity"] == 0
    assert result.get("direction_quantity_policy", {}).get("applied", False) is False


@pytest.mark.parametrize("probability", [np.nextafter(.4999, 1.), .5, np.nextafter(.5001, 0.)])
def test_strictly_interior_hold_preserves_canonical_reason(probability):
    result = calculate(low_score(float(probability)))
    assert result["side"] == "HOLD" and result["status"] == "unavailable"
    assert result["desired_quantity"] is None
    assert result["reason"] == "probability_between_action_cutoffs"
    assert result.get("direction_quantity_policy", {}).get("applied", False) is False


@pytest.mark.parametrize("probability", [.5001, .4999])
def test_whole_share_direction_still_obeys_actual_capacity(probability):
    bundle = low_score(probability)
    result = calculate(bundle)
    values = dict(symbol=bundle[0]["symbol"], side=result["side"],
                  desired_quantity=result["desired_quantity"], limit_price="105",
                  available_cash="0", available_shares=0,
                  capacity_evidence_id="synthetic-own-capacity", observed_at=bundle[0]["issued_at"],
                  calculated_at=bundle[0]["issued_at"])
    blocked = clip_to_capacity(**values)
    assert blocked.desired_quantity == 1 and blocked.adjusted_quantity == 0
    assert blocked.disposition == ("no_purchasing_capacity" if result["side"] == "BUY"
                                   else "no_eligible_uncommitted_shares")
    allowed = clip_to_capacity(**(values | dict(available_cash="105", available_shares=2)))
    assert allowed.adjusted_quantity == 1 and allowed.disposition == "full_intention_possible"
    assert result["desired_quantity"] == 1 and result["adjusted_quantity"] is None


def test_unavailable_or_elapsed_opportunity_is_not_promoted():
    bundle = low_score(.4999)
    missing = calculate(bundle, quote=None)
    assert missing["status"] == "unavailable" and missing["desired_quantity"] is None
    later = (pd.Timestamp(bundle[0]["target_close"]) + pd.Timedelta(seconds=1)).isoformat()
    quote = replace(bundle[3], observed_at=later, available_at=later)
    elapsed = calculate(bundle, calculated_at=later, quote=quote)
    assert elapsed["calculated_after_target"] is True and elapsed["desired_quantity"] == 0
    assert elapsed["orders_enabled"] is False and elapsed["target_close"] == bundle[0]["target_close"]


@pytest.mark.parametrize("probability", [.72, .2])
def test_positive_technical_quantity_and_explanation_are_preserved(monkeypatch, probability):
    bundle = sizing_fixture(probability=probability)
    result = calculate(bundle)
    with monkeypatch.context() as patch:
        patch.setattr(starter_sizing, "_apply_direction_minimum", lambda value: value, raising=False)
        technical = calculate(bundle)
    assert result["desired_quantity"] == technical["desired_quantity"] > 0
    assert result["explanation"] == technical["explanation"]
    assert {k: v for k, v in result.items() if k != "direction_quantity_policy"} == technical


def saved_policy(bundle, version, *, probability, side, cutoff):
    forecast = {k: v for k, v in bundle[0].items() if k != "forecast_id"}
    forecast.update(probability_not_down=probability, stock_action=side,
                    direction_policy_version=version, direction_up_threshold=cutoff,
                    direction_down_threshold=1. - cutoff)
    # Record the exact decimal policy boundary rather than subtraction noise.
    forecast["direction_down_threshold"] = .4999 if cutoff == .5001 else .5
    forecast["forecast_id"] = digest(forecast)
    return (forecast, *bundle[1:])


@pytest.mark.parametrize("version", ["stock-direction-5001-4999-v3", "stock-direction-50-01-v3"])
def test_original_recorded_policy_identity_is_preserved(version):
    bundle = saved_policy(low_score(.4999), version, probability=.4999, side="SELL", cutoff=.5001)
    original = deepcopy(bundle[0])
    result = calculate(bundle)
    assert result["desired_quantity"] == 1 and result["side"] == "SELL"
    assert result["direction_policy_version"] == version
    assert result["direction_up_threshold"] == .5001 and result["direction_down_threshold"] == .4999
    assert bundle[0] == original


def test_historical_direction_is_preserved_without_current_policy_relabelling():
    bundle = saved_policy(low_score(.50005), "stock-direction-50-v2",
                          probability=.50005, side="BUY", cutoff=.5)
    result = calculate(bundle)
    assert result["side"] == "BUY" and result["direction_policy_version"] == "stock-direction-50-v2"
    assert result["direction_up_threshold"] == result["direction_down_threshold"] == .5
    assert result["status"] == "unavailable" and result["desired_quantity"] is None
    assert result["direction_quantity_policy"]["applied"] is False


def test_conflicting_original_saved_direction_remains_rejected():
    bundle = saved_policy(low_score(.4999), "stock-direction-50-01-v3",
                          probability=.4999, side="BUY", cutoff=.5001)
    with pytest.raises(ContractError, match="Stock action disagrees"):
        calculate(bundle)
