"""Approved starter arithmetic and causal evidence; no performance simulations."""
from dataclasses import asdict, replace
from decimal import Decimal, ROUND_UP, localcontext
import json

import numpy as np
import pandas as pd
import pytest

from ml.intraday.prediction import ContractError, STEP, TARGET_VERSION, digest
from ml.intraday.starter_sizing import QuoteObservation, independent_reference, size_forecast, starter_policy
from ml.intraday.technical_sizing import AvailableInput


def fixture(*, symbol="AAPL", probability=.72, count=40, direction=1, zero_volume=False, flat=False):
    steps = np.arange(count, dtype=float)
    close = np.full(count, 100.) if flat else 100. * (1 + direction * .005) ** steps
    previous = np.r_[close[0], close[:-1]]
    opened = pd.date_range("2026-10-08T08:00:00Z", periods=count, freq="15min")
    frame = pd.DataFrame({"symbol": symbol, "opened_at": opened,
        "available_at": opened + pd.Timedelta(minutes=15, seconds=2), "availability_kind": "actual",
        "open": previous, "high": np.maximum(previous, close) + (0 if flat else 1),
        "low": np.minimum(previous, close) - (0 if flat else 1), "close": close,
        "volume": np.zeros(count) if zero_volume else 100 + steps,
        "source_id": "databento:EQUS.MINI:ohlcv-1m", "source_dataset": "EQUS.MINI",
        "session_id": "2026-10-08", "session_open": "2026-10-08T08:00:00+00:00",
        "session_close": "2026-10-09T00:00:00+00:00", "synthetic": False, "no_trade": False,
        "synthetic_open": False, "synthetic_close": False, "observed_constituents": 15,
        "synthetic_constituents": 0, "evidence_json": '{"verified_source":"fixture-only"}'})
    ref = opened[-1]
    cutoff = ref + pd.Timedelta(minutes=15, seconds=3)
    snapshot = {"cutoff": cutoff.isoformat(), "candles": json.loads(frame.to_json(orient="records", date_format="iso")),
        "inference": {"symbol": symbol, "reference_at": ref.isoformat(), "reference_open": float(previous[-1]),
            "source": "databento:EQUS.MINI:ohlcv-1m", "source_dataset": "EQUS.MINI"},
        "manifest": {"fixture_only": True}}
    forecast = {"symbol": symbol, "reference_at": ref.isoformat(), "reference_open": str(previous[-1]),
        "data_through": (ref + STEP).isoformat(), "target_open": (ref + 2 * STEP).isoformat(),
        "target_close": (ref + 3 * STEP).isoformat(), "issued_at": cutoff.isoformat(),
        "training_cutoff": cutoff.isoformat(), "target_version": TARGET_VERSION, "probability_not_down": probability,
        "source_id": snapshot["inference"]["source"], "source_dataset": "EQUS.MINI", "snapshot_hash": digest(snapshot)}
    forecast["forecast_id"] = digest(forecast)
    equity = AvailableInput(100_000., cutoff.isoformat(), "fixture-liquidation-equity", {"snapshot_id": "account-fixture"})
    quote = QuoteObservation(99., 101., cutoff.isoformat(), cutoff.isoformat(), "fixture-fresh-quote",
                             {"quote_id": "quote-fixture", "no_actual_provider": True})
    return forecast, snapshot, equity, quote


def run(bundle, **overrides):
    forecast, snapshot, equity, quote = bundle
    args = dict(forecast_snapshot=snapshot, account_equity=equity, quote=quote,
                calculated_at=forecast["issued_at"], maximum_quote_age_seconds=60.)
    args.update(overrides)
    return size_forecast(forecast, **args)


def test_approved_nine_term_recipe_is_independent_per_symbol_and_side():
    a, b = starter_policy("AAPL"), starter_policy("IONQ")
    assert a.symbol != b.symbol and a.version != b.version
    assert a.indicators == b.indicators  # Shared explicit initial periods, not shared computed values.
    assert a.window.basis == "observed_history" and a.window.max_bars is None
    assert len(a.buy.terms) == len(a.sell.terms) == 9
    assert a.buy.sum_divisor == a.sell.sum_divisor == 18
    assert a.buy.bias == .5 and 0 <= a.buy.output_min < a.buy.output_max < 1
    for buy, sell in zip(a.buy.terms[:8], a.sell.terms[:8]):
        assert buy.weight == 1 and sell.weight == -1
    assert a.buy.terms[-1].weight == a.sell.terms[-1].weight == 1
    p = {spec.name: spec.parameters for spec in a.indicators}
    assert p["ema20"] == {"span": 20, "adjust": False}
    assert p["rsi14"] == {"window": 14, "smoothing": "simple"}
    assert p["bb20"] == {"window": 20, "deviations": 2., "ddof": 0}
    assert p["stoch14"] == {"window": 14, "k_smoothing": 3, "d_smoothing": 3}


def test_decimal_equity_scale_uses_ask_buy_and_bid_sell_without_capacity():
    buy = fixture(probability=.8)
    sell = fixture(probability=.2)
    references = []
    for bundle in (buy, sell):
        reference, reason = independent_reference(bundle[0], account_equity=bundle[2], quote=bundle[3],
            calculated_at=bundle[0]["issued_at"], maximum_quote_age_seconds=60)
        assert reason is None
        references.append(reference)
        assert reference.evidence["reference_notional"] == "1500.0000"
        assert reference.evidence["stream_budget_or_set_aside"] is False
        assert reference.evidence["scheduled_gameplan_order_required"] is False
    assert references[0].side == "BUY" and references[0].evidence["price_side"] == "ask"
    assert references[1].side == "SELL" and references[1].evidence["price_side"] == "bid"
    assert float(references[0].shares) == pytest.approx(1500 / 101)
    assert float(references[1].shares) == pytest.approx(1500 / 99)


def test_falling_and_rising_symbol_ta_produce_different_real_calculated_sizes():
    rising = run(fixture(direction=1)).payload()
    falling = run(fixture(symbol="IONQ", direction=-1)).payload()
    assert rising["side"] == falling["side"] == "BUY"
    assert float(rising["score"]) > float(falling["score"])
    assert rising["desired_quantity"] > falling["desired_quantity"]
    assert rising["policy_hash"] != falling["policy_hash"]
    assert rising["indicator_snapshot"]["features"]["rsi14.value"] == 100
    assert falling["indicator_snapshot"]["features"]["rsi14.value"] == 0
    for result in (rising, falling):
        assert result["adjusted_quantity"] is None and result["capacity_status"] == "not_evaluated"
        assert result["execution_disposition"] == "pending_capacity" and result["orders_enabled"] is False
        assert len(result["term_contributions"]) == 9
        assert result["explanation"]["half_share_ties"] == "half_up"
        assert result["explanation"]["rounded_equality"] == "allow"


def test_equal_probability_holds_and_side_orientation_has_no_confidence_gate():
    half = run(fixture(probability=.5)).payload()
    assert half["side"] == "HOLD" and half["desired_quantity"] is None
    assert half["reason"] == "probability_between_action_cutoffs"
    assert half["execution_disposition"] == "hold"
    buys = run(fixture(probability=.8)).payload()
    sells = run(fixture(probability=.2)).payload()
    buy_probability = next(x for x in buys["term_contributions"] if x["feature"] == "probability_not_down")
    sell_probability = next(x for x in sells["term_contributions"] if x["feature"] == "probability_not_down")
    assert buy_probability["contribution_decimal"] == sell_probability["contribution_decimal"]
    assert float(buys["score"]) > float(sells["score"])


@pytest.mark.parametrize("probability,side", [
    (0., "SELL"), (.48, "SELL"), (.4999, "SELL"),
    (np.nextafter(.4999, 1.), "HOLD"), (.5, "HOLD"),
    (np.nextafter(.5001, 0.), "HOLD"), (.5001, "BUY"), (.52, "BUY"), (1., "BUY"),
])
def test_exact_probability_cutoffs_flow_through_side_quote_and_sizing(probability, side):
    bundle = fixture(probability=float(probability))
    reference, reason = independent_reference(bundle[0], account_equity=bundle[2], quote=bundle[3],
        calculated_at=bundle[0]["issued_at"], maximum_quote_age_seconds=60)
    sized = run(bundle).payload()
    assert sized["side"] == side
    if side == "HOLD":
        assert reference is None and reason == "probability_between_action_cutoffs"
        assert sized["status"] == "unavailable" and sized["desired_quantity"] is None
    else:
        assert reason is None and reference.side == side
        assert reference.evidence["price_side"] == ("ask" if side == "BUY" else "bid")
        assert sized["status"] == "sized" and sized["desired_quantity"] > 0


def test_cash_and_eligible_shares_never_shrink_or_gate_original_desired_quantity():
    bundle = fixture()
    original = run(bundle)
    later = (pd.Timestamp(bundle[0]["issued_at"]) + pd.Timedelta(minutes=30)).isoformat()
    extras = {"cash": AvailableInput(0, later, "", {}), "eligible_shares": AvailableInput(0, later, "", {}),
              "gameplan_horizon_budget": AvailableInput(0, later, "", {})}
    unchanged = run(bundle, account_inputs=extras)
    assert unchanged.desired_quantity == original.desired_quantity > 0
    assert unchanged.payload()["account_inputs"]["cash"]["value"] == 0
    assert unchanged.adjusted_quantity is None


def test_subshare_reference_rounds_to_genuine_zero_without_one_share_floor():
    bundle = fixture()
    result = run(bundle, account_equity=replace(bundle[2], value=100.)).payload()
    assert Decimal(result["symbol_reference"]["shares"]) < 1
    assert result["status"] == "sized" and result["desired_quantity"] == 0
    assert result["adjusted_quantity"] is None and result["reference_failure_reason"] is None


@pytest.mark.parametrize("kwargs,reason", [
    ({"account_equity": None}, "account_equity_unavailable"),
    ({"quote": None}, "fresh_quote_unavailable"),
])
def test_missing_context_returns_null_with_original_forecast_and_target(kwargs, reason):
    bundle = fixture()
    result = run(bundle, **kwargs).payload()
    assert result["status"] == "unavailable" and result["desired_quantity"] is None
    assert result["reason"] == reason and result["reference_failure_reason"] == reason
    assert result["forecast_id"] == bundle[0]["forecast_id"]
    assert result["target_close"] == bundle[0]["target_close"]


def test_quote_freshness_and_actual_availability_are_distinct():
    bundle = fixture()
    at = pd.Timestamp(bundle[0]["issued_at"])
    stale = replace(bundle[3], observed_at=(at - pd.Timedelta(seconds=61)).isoformat())
    assert run(bundle, quote=stale).payload()["reason"] == "quote_stale_or_future"
    future = replace(bundle[3], available_at=(at + pd.Timedelta(seconds=1)).isoformat())
    assert run(bundle, quote=future).payload()["reason"] == "quote_unavailable_at_sizing"
    future_equity = replace(bundle[2], available_at=future.available_at)
    assert run(bundle, account_equity=future_equity).payload()["reason"] == "account_equity_unavailable_at_sizing"
    inconsistent = replace(bundle[3], available_at=(at - pd.Timedelta(seconds=1)).isoformat())
    assert run(bundle, quote=inconsistent).payload()["reason"] == "quote_observation_after_availability"
    assert run(bundle, quote=replace(bundle[3], ask=98.)).payload()["reason"] == "valid_bid_ask_unavailable"


@pytest.mark.parametrize("age", [None, True, -1, 0, float("nan")])
def test_caller_must_supply_explicit_valid_freshness_setting(age):
    with pytest.raises(ContractError, match="freshness"):
        run(fixture(), maximum_quote_age_seconds=age)


@pytest.mark.parametrize("kwargs,reason", [
    ({"zero_volume": True}, "sizing_feature_unavailable: vwap20.distance"),
    ({"flat": True}, "normalization_feature_unavailable: atr14.relative"),
    ({"count": 15}, "sizing_feature_unavailable: ema20.distance"),
])
def test_no_fake_values_for_missing_warmup_zero_atr_or_zero_volume(kwargs, reason):
    result = run(fixture(**kwargs)).payload()
    assert result["status"] == "unavailable" and result["desired_quantity"] is None
    assert result["reason"] == reason and result["adjusted_quantity"] is None
    assert result["indicator_snapshot"]["status"] == "ready"


def test_entire_original_inputs_policy_scale_and_quote_evidence_are_immutable():
    bundle = fixture()
    result = run(bundle)
    payload = result.payload()
    assert payload["indicator_snapshot"]["forecast_snapshot_hash"] == bundle[0]["snapshot_hash"]
    assert payload["reference_context"]["account_equity"] == asdict(bundle[2])
    assert payload["reference_context"]["quote"] == asdict(bundle[3])
    assert payload["initial_configuration_authority"] == "human_approved_2026_10_12"
    bundle[2].evidence["snapshot_id"] = "newer-context"
    assert result.payload()["reference_context"]["account_equity"]["evidence"]["snapshot_id"] == "account-fixture"
    bundle[1]["candles"][-1]["volume"] += 1
    with pytest.raises(ContractError, match="hash changed"):
        run(bundle)


def test_late_calculation_cannot_roll_original_reference_or_target():
    bundle = fixture()
    later = (pd.Timestamp(bundle[0]["target_close"]) + pd.Timedelta(minutes=2)).isoformat()
    quote = replace(bundle[3], observed_at=later, available_at=later)
    result = run(bundle, quote=quote, calculated_at=later).payload()
    assert result["calculated_after_target"] is True
    assert result["reference_at"] == bundle[0]["reference_at"]
    assert result["target_close"] == bundle[0]["target_close"]
    assert result["orders_enabled"] is False


def test_strong_ta_is_clipped_below_one_and_nearest_rounding_is_retained():
    bundle = fixture(probability=1.)
    for candle in bundle[1]["candles"]:
        candle["high"] = candle["close"]
    row = bundle[1]["candles"][-1]
    row["close"] *= 10
    row["high"] = row["close"]
    row["volume"] *= 3
    forecast = dict(bundle[0], snapshot_hash=digest(bundle[1]))
    forecast.pop("forecast_id")
    forecast["forecast_id"] = digest(forecast)
    result = run((forecast, bundle[1], bundle[2], bundle[3])).payload()
    assert Decimal(result["score"]) < 1
    assert result["score_clipped"] is True
    assert Decimal(result["explanation"]["raw_quantity"]) < Decimal(result["symbol_reference"]["shares"])
    assert result["desired_quantity"] == 15  # Fresh quote's 14.85... scale; no flooring substitute.


def test_independent_reference_rejects_a_rewritten_original_forecast():
    bundle = fixture()
    changed = dict(bundle[0], probability_not_down=.1)
    with pytest.raises(ContractError, match="immutable forecast"):
        independent_reference(changed, account_equity=bundle[2], quote=bundle[3],
                              calculated_at=bundle[0]["issued_at"], maximum_quote_age_seconds=60)


def test_decimal_runtime_context_cannot_change_approved_starter_result():
    bundle = fixture()
    original = run(bundle)
    with localcontext() as ambient:
        ambient.prec = 3
        ambient.rounding = ROUND_UP
        repeated = run(bundle)
    assert repeated == original
