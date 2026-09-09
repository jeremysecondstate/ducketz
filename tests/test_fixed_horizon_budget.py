"""Pure fixed-budget candidate checks; no provider, model fit, or broker calls."""
from dataclasses import replace

import pandas as pd
import pytest

from ml.independent_stock_targets import STOCK_TARGET_CONTRACT_VERSION, stock_target_windows
from ml.stock_trader.contracts import ActivationIntent, PortfolioState, PredictionSignal, QuoteState, StockTraderPolicy
from ml.stock_trader.fixed_horizon_budget import fixed_budget_forecast_readiness, size_fixed_horizon_budget


NOW = "2026-09-08T11:01:00Z"
ACTIVATION = ActivationIntent(True, "ACTIVE", "fixture active", "fixture", "hash")


def signal(group="1h", probability=.7, *, hour=4, symbol="COST"):
    spec = next(spec for spec in stock_target_windows(pd.Timestamp("2026-09-08").date())
                if spec["model_group"] == group and spec["execution_eligible"]
                and spec["target_window_start"].tz_convert("America/Los_Angeles").hour == hour)
    start = spec["target_window_start"]
    return PredictionSignal(symbol, group, f"fixture-{symbol}-{group}", "2026-09-08T08:00:00Z",
        start.isoformat(), spec["target_window_end"].isoformat(), (start + pd.Timedelta(minutes=10)).isoformat(),
        "2026-09-08T09:00:00Z", probability, .001, {group: probability}, "qualified-stock-forecast", "fixture",
        "f" * 64, target_definition_version=STOCK_TARGET_CONTRACT_VERSION, target_price_source_contract="xnas-itch-archive-v1")


def portfolio(symbol="COST", *, ask=100., bid=100.):
    quote = QuoteState(symbol, bid, ask, ask, ask, 1000., NOW)
    return PortfolioState(NOW, 100000., 100000., 0., 0., {}, {}, {}, {}, 0, {symbol: quote}, "snapshot", "broker"), quote


def size(prediction=None, **kwargs):
    prediction = prediction or signal()
    state, quote = portfolio(prediction.symbol)
    return size_fixed_horizon_budget(prediction, kwargs.pop("portfolio", state), kwargs.pop("quote", quote),
        kwargs.pop("activation", ACTIVATION), forecast_promoted=kwargs.pop("forecast_promoted", True),
        ledger_ready=kwargs.pop("ledger_ready", True), has_active_allocation=kwargs.pop("has_active_allocation", False),
        decided_at=kwargs.pop("decided_at", NOW), **kwargs)


@pytest.mark.parametrize("probability,utilization", [(.54, .08), (.55, .1), (.6, .2), (.7, .4), (.75, .5), (1., .5)])
def test_confidence_fraction_is_explicit_conservative_policy(probability, utilization):
    result = size(signal(probability=probability))
    assert result.status == "ELIGIBLE"
    assert result.confidence_budget_fraction == utilization
    assert result.horizon_notional_ceiling == 1500.
    assert result.confidence_notional_budget == 1500 * utilization
    assert result.quantity * result.limit_price <= result.available_notional_budget
    assert result.forecast_probability == probability
    audit = result.to_dict()
    assert not {"expected_net_return", "expected_net_dollars", "trade_probability", "model_fingerprint"}.intersection(audit)
    assert result.combined_batch_accounting_required is True


def test_horizon_weights_and_existing_order_cap_are_preserved():
    results = [size(signal(group)) for group in ("1h", "4h", "1d", "1w")]
    assert [item.horizon_weight for item in results] == [1, 2, 3, 4]
    assert [item.horizon_notional_ceiling for item in results] == [1500., 3000., 4500., 5000.]
    assert [item.quantity for item in results] == [6, 12, 18, 20]
    assert sum(item.order_notional for item in results) <= 15000.


@pytest.mark.parametrize("probability", [0., .3, .46, .5, .539999])
def test_qualified_bearish_or_neutral_is_ready_without_an_entry_or_short(probability):
    prediction = signal(probability=probability)
    readiness = fixed_budget_forecast_readiness(prediction, forecast_promoted=True)
    assert readiness["status"] == "READY_WITH_NO_ENTRY_SIGNAL"
    assert readiness["entry_signal"] is False
    state, _ = portfolio()
    state = replace(state, held_shares={"COST": 900.})
    result = size(prediction, portfolio=state)
    assert result.quantity == 0
    assert result.limit_price is None
    assert result.reason_code == "NO_BULLISH_ENTRY_SIGNAL"


@pytest.mark.parametrize("kwargs,code", [
    ({"forecast_promoted": False}, "FORECAST_NOT_PROMOTED"),
    ({"ledger_ready": False}, "HORIZON_LEDGER_UNRECONCILED"),
    ({"has_active_allocation": True}, "HORIZON_ALLOCATION_ALREADY_ACTIVE"),
    ({"activation": replace(ACTIVATION, active=False)}, "TRADER_INACTIVE"),
    ({"decided_at": "2026-09-08T10:59:00Z"}, "ENTRY_WINDOW_CLOSED"),
    ({"decided_at": "2026-09-08T11:05:00Z"}, "ENTRY_WINDOW_CLOSED"),
])
def test_existing_entry_authority_and_window_checks(kwargs, code):
    result = size(**kwargs)
    assert result.quantity == 0
    assert result.reason_code == code


def test_policy_does_not_veto_using_another_horizons_forecast():
    prediction = replace(signal(), horizon_probabilities={"1h": .7, "4h": .1, "1d": .1, "1w": .1})
    assert size(prediction).quantity == 6


def test_exact_overnight_target_is_retained():
    prediction = signal("4h", hour=16)
    state, quote = portfolio()
    timestamp = "2026-09-08T23:01:00Z"
    quote = replace(quote, observed_at=timestamp)
    result = size(prediction, quote=quote, decided_at=timestamp)
    assert result.quantity > 0
    assert result.target_window_end == "2026-09-09T14:00:00+00:00"
    invalid = replace(prediction, target_window_end="2026-09-09T03:00:00+00:00")
    assert size(invalid, quote=quote, decided_at=timestamp).reason_code == "FORECAST_EXACT_TARGET_WINDOW_INVALID"


@pytest.mark.parametrize("field,value", [("available_cash", 150.), ("gross_exposure", 129850.)])
def test_single_candidate_cash_and_gross_cap(field, value):
    state, _ = portfolio()
    result = size(portfolio=replace(state, **{field: value}))
    assert result.quantity == 1
    assert result.available_notional_budget <= 150.


def test_symbol_cap_includes_existing_and_pending_buys():
    state, _ = portfolio()
    state = replace(state, symbol_exposure={"COST": 14600.}, pending_buy_shares={"COST": 3.})
    result = size(portfolio=state)
    assert result.quantity == 1
    assert result.available_notional_budget == 100.


def test_limit_uses_rounded_ask_and_whole_share_affordability():
    state, quote = portfolio(ask=100.123, bid=100.)
    result = size(portfolio=state, quote=quote)
    assert result.limit_price == 100.13
    assert result.quantity == 5
    assert result.quantity * result.limit_price <= result.available_notional_budget


@pytest.mark.parametrize("ask,bid,code", [(101., 100., "STOCK_SPREAD_TOO_WIDE"), (99., 100., "USABLE_QUOTE_UNAVAILABLE"), (float("nan"), 100., "USABLE_QUOTE_UNAVAILABLE")])
def test_invalid_or_wide_quotes_do_not_make_orders(ask, bid, code):
    state, quote = portfolio(ask=ask, bid=bid)
    result = size(portfolio=state, quote=quote)
    assert result.quantity == 0
    assert result.reason_code == code


def test_missing_model_is_irrelevant_but_research_forecast_cannot_be_relabelled():
    assert size().quantity == 6
    assert size(forecast_promoted=False).quantity == 0
    assert fixed_budget_forecast_readiness(signal(), forecast_promoted="PROMOTED")["status"] == "NOT_READY"
    assert size(replace(signal(), target_definition_version="legacy-hourly")).quantity == 0


def test_existing_minimum_order_and_limit_offset_caps_still_apply():
    state, quote = portfolio(ask=10.001, bid=10.)
    result = size(portfolio=state, quote=quote, policy=StockTraderPolicy(maximum_limit_offset_bps=0.))
    assert result.reason_code == "LIMIT_PRICE_OFFSET_EXCEEDS_CAP"
    assert size(policy=StockTraderPolicy(minimum_order_notional=1000.)).reason_code == "FIXED_BUDGET_TOO_SMALL"
