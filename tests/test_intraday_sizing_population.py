"""Sizing must honor the issued forecast's frozen synthetic population policy."""
from copy import deepcopy
from dataclasses import asdict

import pandas as pd
import pytest

from ml.intraday.prediction import ContractError, digest
from ml.intraday.starter_sizing import size_forecast, starter_policy
from ml.intraday.technical_sizing import indicator_snapshot
from test_intraday_starter_sizing import fixture


def reseal(bundle):
    forecast, snapshot, equity, quote = bundle
    forecast.pop("forecast_id", None)
    forecast["snapshot_hash"] = digest(snapshot)
    forecast["forecast_id"] = digest(forecast)
    return forecast, snapshot, equity, quote


def no_trade_gap(*, symbol="DBX", observed_after=1, policy="exclude"):
    bundle = fixture(symbol=symbol, count=57 + observed_after)
    forecast, snapshot, _, _ = bundle
    candles = snapshot["candles"]
    prior = candles[39]["close"]
    for row in candles[40:57]:
        for field in ("open", "high", "low", "close"):
            row[field] = prior
        row.update(volume=0, synthetic=True, no_trade=True, synthetic_open=True, synthetic_close=True,
            observed_constituents=0, synthetic_constituents=15,
            evidence_json='{"covered":true,"prior_price_preserved":true,"fixture_only":true}')
    if policy is not None:
        snapshot["dataset_scope"] = {"synthetic_policy": policy, "lookback_days": None}
        snapshot["manifest"]["synthetic_policy"] = policy
    return reseal(bundle)


def size(bundle):
    forecast, snapshot, equity, quote = bundle
    return size_forecast(forecast, forecast_snapshot=snapshot, account_equity=equity, quote=quote,
        calculated_at=forecast["issued_at"], maximum_quote_age_seconds=60).payload()


@pytest.mark.parametrize("symbol", ["DBX", "PYPL", "MRNA"])
@pytest.mark.parametrize("observed_after", [1, 2])
def test_observed_policy_excludes_flat_synthetic_warmup_without_substituting_stochastic(symbol, observed_after):
    bundle = no_trade_gap(symbol=symbol, observed_after=observed_after)
    original = deepcopy(bundle)
    result = size(bundle)
    assert result["status"] == "sized" and isinstance(result["desired_quantity"], int)
    assert result["adjusted_quantity"] is None
    indicator = result["indicator_snapshot"]
    observed = pd.DataFrame([row for row in bundle[1]["candles"] if not row["synthetic"]])
    low, high = observed.low.rolling(14).min(), observed.high.rolling(14).max()
    expected_k = ((observed.close - low) / (high - low)).rolling(3).mean().iloc[-1]
    assert indicator["features"]["stoch14.k"] == pytest.approx(expected_k)
    assert indicator["features"]["stoch14.k"] != .5
    assert indicator["provenance"]["excluded_synthetic_rows"] == 17
    assert indicator["provenance"]["synthetic_rows"] == 0
    assert indicator["provenance"]["used_rows"] == 40 + observed_after
    assert indicator["provenance"]["missing_policy"] == "no_filling"
    assert indicator["provenance"]["gaps_or_closures"] == 1
    assert all(not row["synthetic"] for row in indicator["candles"])
    assert indicator["input_population"] == {"synthetic_policy": "exclude", "basis": "dataset_scope"}
    assert digest(result["policy"]) == digest(asdict(starter_policy(symbol)))
    assert bundle == original


@pytest.mark.parametrize("policy", [None, "include"])
def test_legacy_or_explicit_include_policy_is_preserved_and_undefined_stochastic_stays_unavailable(policy):
    result = size(no_trade_gap(policy=policy))
    assert result["status"] == "unavailable" and result["desired_quantity"] is None
    assert result["reason"] == "sizing_feature_unavailable: stoch14.k"
    indicators = result["indicator_snapshot"]
    assert indicators["features"]["stoch14.k"] is None
    assert indicators["provenance"]["synthetic_rows"] == 17
    assert indicators["provenance"]["excluded_synthetic_rows"] == 0
    assert indicators["input_population"]["basis"] == ("legacy_unrecorded" if policy is None else "dataset_scope")


def test_legacy_saved_manifest_policy_is_used_when_snapshot_scope_was_not_recorded():
    bundle = no_trade_gap()
    bundle[1].pop("dataset_scope")
    result = size(reseal(bundle))
    assert result["status"] == "sized"
    assert result["indicator_snapshot"]["provenance"]["synthetic_policy_basis"] == "manifest"


def test_excluded_reference_cannot_shift_to_an_older_observed_bar():
    bundle = no_trade_gap()
    snapshot = bundle[1]
    snapshot["candles"][-1] = {**snapshot["candles"][-2],
        "opened_at": snapshot["candles"][-1]["opened_at"],
        "available_at": snapshot["candles"][-1]["available_at"]}
    result = size(reseal(bundle))
    assert result["status"] == "unavailable" and result["desired_quantity"] is None
    assert result["indicator_snapshot"]["reason"] == "original_observed_reference_unavailable"


@pytest.mark.parametrize("scope,manifest", [
    ({"synthetic_policy": "include"}, "exclude"), ({"synthetic_policy": "exclude"}, "include"),
    ({"synthetic_policy": "unknown"}, "exclude"), ({"synthetic_policy": None}, "exclude"),
    (["exclude"], "exclude"),
])
def test_invalid_or_conflicting_frozen_population_fails_closed(scope, manifest):
    bundle = no_trade_gap()
    bundle[1]["dataset_scope"] = scope
    bundle[1]["manifest"]["synthetic_policy"] = manifest
    with pytest.raises(ContractError, match="Frozen sizing"):
        size(reseal(bundle))


def test_flat_observed_history_remains_unavailable_without_a_neutral_fallback():
    bundle = fixture(flat=True)
    bundle[1]["dataset_scope"] = {"synthetic_policy": "exclude", "lookback_days": None}
    result = size(reseal(bundle))
    assert result["status"] == "unavailable" and result["desired_quantity"] is None
    assert result["indicator_snapshot"]["features"]["stoch14.k"] is None


def test_snapshot_validation_precedes_exclusion_and_future_data_cannot_repair_or_change_indicators():
    forecast, snapshot, _, _ = no_trade_gap()
    policy = starter_policy(forecast["symbol"])
    args = dict(symbol=policy.symbol, reference_at=forecast["reference_at"], data_cutoff=forecast["training_cutoff"],
        indicator_specs=policy.indicators, window_policy=policy.window,
        forecast_snapshot=snapshot, forecast_snapshot_hash=forecast["snapshot_hash"])
    original = indicator_snapshot(snapshot["candles"], **args)
    changed = deepcopy(snapshot["candles"])
    changed[40]["evidence_json"] = '{"changed":true}'
    with pytest.raises(ContractError, match="differ from"):
        indicator_snapshot(changed, **args)
    future = deepcopy(snapshot["candles"][-1])
    future.update(opened_at=(pd.Timestamp(forecast["reference_at"]) + pd.Timedelta(minutes=15)).isoformat(),
        available_at=(pd.Timestamp(forecast["training_cutoff"]) + pd.Timedelta(minutes=15)).isoformat(),
        high=1e8, low=-1e8, volume=-1)
    assert indicator_snapshot([*snapshot["candles"], future], **args) == original
