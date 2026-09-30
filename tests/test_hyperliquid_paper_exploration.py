"""Liberal Paper admission preserves provenance and executable constraints."""
from dataclasses import replace
import json

import pytest

from ml import hyperliquid_paper_runtime as runtime_module
from tests.test_hyperliquid_paper_runtime import BASE, Clock, FakeMarket, configurations, forecast


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Exploration tests must not make network requests.")
    monkeypatch.setattr("requests.post", forbidden)


@pytest.fixture
def exploratory(tmp_path):
    path, data = configurations(
        tmp_path, interval="5m", horizon=1, require_qualified_forecasts=False,
        entry_band=0, exit_band=0, rebalance_min_delta_fraction=0,
        min_trade_notional=10, volatility_budget_fraction=.0006,
        bullish_spot_fraction=.4, max_forecast_age_seconds=300,
    )
    clock = Clock()
    market = FakeMarket(clock)
    instance = runtime_module.PaperRuntime(path, market=market, clock=clock)
    instance.initialize()
    yield instance, clock, market, data
    instance.ledger.close()


@pytest.mark.parametrize("probability,accounts", [
    (.503, {"jeremy", "clearpond"}), (.497, {"alex"}),
])
def test_fresh_small_research_edge_can_fill_without_becoming_qualified(exploratory, probability, accounts):
    instance, _, market, data = exploratory
    publication = forecast(data, interval="5m", horizon=1, p=probability, qualified=False)
    instance.tick()
    fills = instance.ledger.history("fills")
    assert {row["account"] for row in fills} == accounts
    assert all(row["forecast_id"] == publication["prediction_id"] for row in fills)
    for row in fills:
        detail = json.loads(row["details_json"])
        retained = detail["forecast_observation"]["prediction"]
        assert detail["qualified"] is retained["qualified"] is False
        assert retained["role"] == "research_candidate"
        assert row["price"] == pytest.approx(market.price[row["kind"]] + (.1 if row["quantity"] > 0 else -.1))
        assert row["fee"] > 0
        assert row["notional"] >= 10
    # Re-observing one publication is not permission for duplicated fills.
    instance.tick()
    assert instance.ledger.history("fills") == fills


@pytest.mark.parametrize("probability", [.5, .50001, .49999])
def test_zero_or_below_venue_minimum_target_never_invents_a_fill(exploratory, probability):
    instance, _, _, data = exploratory
    forecast(data, interval="5m", horizon=1, p=probability, qualified=False)
    instance.tick()
    assert not instance.ledger.history("fills")
    assert not instance.ledger.history("transfers")
    decisions = [json.loads(row["details_json"]) for row in instance.ledger.history("decisions")]
    assert len(decisions) == 3
    assert all(row["action"] == "hold" for row in decisions)
    if probability == .5:
        assert all(row["target_notional"] == row["current_notional"] == 0 for row in decisions)
    else:
        assert any(row["target_notional"] != 0 for row in decisions)
        assert all(row["policy"]["reason"] != "entry_deadband" for row in decisions)


def test_new_research_signal_executes_small_adjustments_without_percentage_filter(exploratory):
    instance, clock, _, data = exploratory
    forecast(data, interval="5m", horizon=1, p=.55)
    instance.tick()
    prior = instance.ledger.history("fills")
    clock.now = BASE + 330
    forecast(data, interval="5m", horizon=1, decision=BASE + 300, p=.554, identity="small-adjustment")
    instance.tick()
    new = instance.ledger.history("fills")[len(prior):]
    assert {row["account"] for row in new} == {"jeremy", "clearpond"}
    assert all(row["forecast_id"] == "small-adjustment" for row in new)
    assert any(10 <= row["notional"] < 25 for row in new)
    for row in instance.ledger.history("decisions"):
        if row["forecast_id"] == "small-adjustment" and row["action"] == "fill":
            detail = json.loads(row["details_json"])
            assert abs(detail["target_notional"] - detail["current_notional"]) < .7 * abs(detail["target_notional"])
            assert detail["decision_checks"]["rebalance_threshold_notional"] == 10


def test_valid_neutral_research_can_close_but_expired_publication_only_holds(exploratory):
    instance, clock, _, data = exploratory
    forecast(data, interval="5m", horizon=1, p=.55)
    instance.tick()
    opened = instance.ledger.history("fills")
    inventory = instance.ledger.inventory()
    clock.now = BASE + 300
    expired = instance.tick()
    assert "matured" in expired["errors"]["BTC"]
    assert instance.ledger.inventory() == inventory
    assert instance.ledger.history("fills") == opened
    clock.now = BASE + 330
    forecast(data, interval="5m", horizon=1, decision=BASE + 300, p=.5, identity="valid-neutral")
    instance.tick()
    assert not instance.ledger.inventory()
    assert all(row["forecast_id"] == "valid-neutral" for row in instance.ledger.history("fills")[len(opened):])


@pytest.mark.parametrize("cap", ["account", "symbol", "pool"])
def test_missing_signal_still_reduces_exposure_in_research_mode(exploratory, cap):
    instance, _, _, _ = exploratory
    limits = {"account_utilization": 1, "per_symbol_gross_fraction": 1, "pool_gross_fraction": 1}
    limits[{"account": "account_utilization", "symbol": "per_symbol_gross_fraction", "pool": "pool_gross_fraction"}[cap]] = .3 if cap == "account" else .1
    instance.config = replace(instance.config, **limits)
    instance._execute("inherited", {"perp:BTC": 100, "spot:BTC": 102}, orders=[
        {"account": "jeremy", "coin": "BTC", "kind": "perp", "quantity": 50, "price": 100},
    ])
    instance.tick()
    reduction = instance.ledger.history("fills")[-1]
    assert reduction["reason"] == "risk_cap"
    assert reduction["quantity"] == pytest.approx(-20)
    assert reduction["forecast_id"] is reduction["model_id"] is None
    assert not instance.ledger.history("transfers")


def test_partial_stop_keeps_reducing_after_price_recovers_without_new_signal(exploratory):
    instance, clock, market, data = exploratory
    instance._execute("inherited", {"perp:BTC": 100, "spot:BTC": 102}, orders=[
        {"account": "jeremy", "coin": "BTC", "kind": "perp", "quantity": .3, "price": 100},
    ])
    forecast(data, interval="5m", horizon=1, p=.55)
    market.price["perp"], market.depth = 94, .12
    instance.tick()
    assert next(row for row in reversed(instance.ledger.history("fills"))
                if row["account"] == "jeremy")["reason"] == "stop_loss"
    clock.now += 31
    market.price["perp"] = 100
    instance.tick()
    reduction = next(row for row in reversed(instance.ledger.history("fills")) if row["account"] == "jeremy")
    assert reduction["reason"] == "stop_cooldown"
    assert reduction["quantity"] == pytest.approx(-.12)
    assert next(p for p in instance.ledger.inventory() if p["account"] == "jeremy")["quantity"] == pytest.approx(.06)


@pytest.mark.parametrize("cause", ["old_book", "empty_book", "malformed_probability"])
def test_liberal_admission_keeps_book_and_forecast_validity_constraints(exploratory, cause):
    instance, _, market, data = exploratory
    overrides = {"p_down": .99} if cause == "malformed_probability" else None
    forecast(data, interval="5m", horizon=1, p=.55, overrides=overrides)
    if cause == "old_book":
        market.book_offset = -26
    elif cause == "empty_book":
        market.depth = 0
    instance.tick()
    assert not instance.ledger.history("fills")
    assert not instance.ledger.history("transfers")
