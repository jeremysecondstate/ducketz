from dataclasses import replace

import pandas as pd
import pytest

from ml.stock_trader.contracts import ActivationIntent, EnrichmentOutput, PortfolioState, PredictionSignal, QuoteState, StockTraderPolicy
from ml.stock_trader.independent_engine import build_independent_trade_decisions

NOW = "2026-09-08T11:01:00Z"


class Model:
    def predict(self, features):
        return EnrichmentOutput("test", "v1", "fingerprint", .9, 1., .02, .01, .9, 0., .03, 60.)


def inputs(symbols=("AAPL",)):
    portfolio = PortfolioState(
        NOW, 100_000., 100_000., 0., 0., {}, {}, {}, {}, 0,
        {s: QuoteState(s, 100., 100., 100., 100., 1000., NOW) for s in symbols},
        "snapshot", "account-fingerprint",
    )
    signals = {}
    for symbol in symbols:
        for horizon, hours in (("1h", 1), ("4h", 4), ("1d", 13), ("1w", 109)):
            signals[symbol, horizon] = PredictionSignal(
                symbol, horizon, symbol + horizon, "2026-09-04T20:00:00Z",
                "2026-09-08T11:00:00Z", (pd.Timestamp("2026-09-08T11:00:00Z") + pd.Timedelta(hours=hours)).isoformat(),
                "2026-09-08T11:05:00Z", "2026-09-08T10:00:00Z", .7, .002,
                {horizon: .7}, "model", "version", "source", "PRE", "independent-stock-targets-v1",
            )
    return signals, portfolio


def build(monkeypatch, signals, portfolio, **kwargs):
    # Isolate joint sizing from qualification. Separate integration tests verify
    # that real, hourly-only model artifacts cannot reach this sizing path.
    monkeypatch.setattr("ml.stock_trader.model.require_enrichment_signal_support", lambda *_: None)
    return build_independent_trade_decisions(
        signals, portfolio, Model(), ActivationIntent(True, "ACTIVE", "enabled", "control", "hash"),
        active_allocations=kwargs.pop("active_allocations", frozenset()),
        ledger_ready=kwargs.pop("ledger_ready", True), decided_at=NOW, time_in_force="AM", **kwargs,
    )


def test_separate_caps_increase_without_multiplying_symbol_budget(monkeypatch):
    signals, portfolio = inputs()
    decisions = build(monkeypatch, signals, portfolio)
    quantities = {d.prediction["primary_horizon"]: d.quantity for d in decisions}
    assert quantities == {"1h": 15, "4h": 30, "1d": 45, "1w": 50}
    assert sum(d.quantity * d.limit_price for d in decisions) <= 15_000


def test_four_horizons_share_cash_symbol_and_order_caps(monkeypatch):
    signals, portfolio = inputs(("AAPL", "COST", "NVDA", "MU", "AMZN", "GOOG", "SNDK"))
    portfolio = replace(portfolio, available_cash=4200., symbol_exposure={"AAPL": 14_900.})
    decisions = build(monkeypatch, signals, portfolio)
    selected = [d for d in decisions if d.quantity]
    assert len(selected) <= 6
    assert sum(d.quantity * d.limit_price for d in selected) <= 4200 * .95
    assert sum(d.quantity * d.limit_price for d in selected if d.symbol == "AAPL") <= 100.


def test_existing_weekly_allocation_blocks_only_its_new_entry(monkeypatch):
    signals, portfolio = inputs()
    decisions = build(monkeypatch, signals, portfolio, active_allocations=frozenset({("AAPL", "1w")}))
    weekly = next(d for d in decisions if d.prediction["primary_horizon"] == "1w")
    assert weekly.quantity == 0
    assert weekly.decision_reason_code == "HORIZON_ALLOCATION_ALREADY_ACTIVE"
    assert sum(d.quantity > 0 for d in decisions) == 3


def test_bearish_hourly_cannot_sell_weekly_or_manual_shares(monkeypatch):
    signals, portfolio = inputs()
    signals = {("AAPL", "1h"): replace(signals["AAPL", "1h"], calibrated_probability=.3)}
    portfolio = replace(portfolio, held_shares={"AAPL": 500.})
    decisions = build(monkeypatch, signals, portfolio, active_allocations=frozenset({("AAPL", "1w")}))
    assert decisions[0].quantity == 0
    assert decisions[0].decision_reason_code == "NO_OWNED_HORIZON_SHARES"


def test_unreconciled_inventory_prevents_all_entries(monkeypatch):
    signals, portfolio = inputs()
    assert all(d.quantity == 0 and d.decision_reason_code == "HORIZON_LEDGER_UNRECONCILED"
               for d in build(monkeypatch, signals, portfolio, ledger_ready=False))


def test_expired_entry_and_large_actual_limit_fail_feasibility(monkeypatch):
    signals, portfolio = inputs()
    signals = {k: replace(v, actionable_until=NOW) for k, v in signals.items()}
    assert all(d.quantity == 0 for d in build(monkeypatch, signals, portfolio))


def test_exit_batch_precedes_new_entries_under_one_cap(monkeypatch):
    signals, portfolio = inputs()
    portfolio = replace(portfolio, held_shares={"AAPL": 5.})
    planned = build(monkeypatch, signals, portfolio)
    exit_decision = replace(planned[0], action="SELL", quantity=5)
    decisions = build(monkeypatch, signals, portfolio, policy=StockTraderPolicy(maximum_orders_per_wake=1), exit_decisions=(exit_decision,))
    assert decisions[0].action == "SELL"
    assert decisions[0].quantity == 5
    assert all(d.quantity == 0 for d in decisions[1:])
