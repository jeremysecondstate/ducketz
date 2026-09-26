"""Forward paper integration uses temporary files and fake public observations."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

import pandas as pd
import pytest

from ml import hyperliquid_paper_runtime as runtime_module


BASE = pd.Timestamp("2026-09-26T03:00:00+00:00").timestamp()
DATA_ID = "20260926T030005Z-1234abcd"
MODEL_ID = "20260926T025000Z-abcdef12"


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class Clock:
    def __init__(self, now=BASE + 30):
        self.now = now

    def __call__(self):
        return self.now


class FakeMarket:
    def __init__(self, clock):
        self.clock = clock
        self.price = {"perp": 100.0, "spot": 102.0}
        self.depth = 10000.0
        self.book_offset = 0
        self.omit = set()
        self.funding_rows = []
        self.funding_calls = []
        self.snapshot_calls = []

    def snapshot(self, symbols):
        self.snapshot_calls.append(tuple(symbols))
        markets = {}
        for coin in symbols:
            for kind in ("perp", "spot"):
                key = f"{kind}:{coin}"
                if key in self.omit:
                    continue
                price = self.price[kind]
                markets[key] = {
                    "coin": coin, "kind": kind, "wire_coin": coin if kind == "perp" else "@1",
                    "mark": price, "oracle": price, "funding_rate": 0.0,
                    "sz_decimals": 6, "book_time_utc": iso(self.clock() + self.book_offset),
                    "received_at_utc": iso(self.clock()),
                    "bids": [[price - 0.1, self.depth]], "asks": [[price + 0.1, self.depth]],
                }
        return {"observed_at_utc": iso(self.clock()), "markets": markets,
                "errors": {key: "unavailable" for key in self.omit}}

    def funding_history(self, coin, start_ms, end_ms):
        self.funding_calls.append((coin, start_ms, end_ms))
        return [deepcopy(row) for row in self.funding_rows if start_ms <= row["time"] <= end_ms]


def configurations(tmp_path, *, seed_mode="manual", symbols=("BTC",), **overrides):
    data = tmp_path / "data"
    markets_path = tmp_path / "markets.json"
    models_path = tmp_path / "models.json"
    paper_path = tmp_path / "paper.json"
    markets_path.write_text(json.dumps({"version": 1, "symbols": list(symbols), "interval": "15m", "output_root": str(data)}))
    models_path.write_text(json.dumps({"version": 1, "markets_config": str(markets_path), "horizons_bars": [4]}))
    paper_path.write_text(json.dumps({"version": 1, "seed_mode": seed_mode,
                                    "data_root": str(data), "model_config": str(models_path), **overrides}))
    return paper_path, data


def forecast(data, *, coin="BTC", identity="forecast-1", decision=BASE, p=0.65,
             qualified=False, price=77.0, overrides=None, record_overrides=None):
    data_id = datetime.fromtimestamp(decision + 5, timezone.utc).strftime("%Y%m%dT%H%M%SZ-1234abcd")
    model_id = datetime.fromtimestamp(decision - 600, timezone.utc).strftime("%Y%m%dT%H%M%SZ-abcdef12")
    directory = data / "_models" / coin / "15m" / "h4"
    run = directory / "runs" / model_id
    run.mkdir(parents=True, exist_ok=True)
    record = {
        "model_id": model_id, "coin": coin, "interval": "15m", "horizon_bars": 4,
        "trained_at_utc": iso(decision - 600), "eligible": qualified,
        "source_run_id": data_id, "feature_revision": "btc_shared_causal_v1",
        **(record_overrides or {}),
    }
    (run / "record.json").write_text(json.dumps(record))
    prediction = {
        "prediction_id": identity, "model_id": model_id, "coin": coin, "interval": "15m",
        "horizon_bars": 4, "data_run_id": data_id, "qualified": qualified,
        "role": "active" if qualified else "research_candidate",
        "created_at_utc": iso(decision + 5), "decision_timestamp_utc": iso(decision - 900),
        "decision_close_utc": iso(decision), "target_close_utc": iso(decision + 3600),
        "decision_price": price, "p_not_down": p, "p_down": 1-p,
        **(overrides or {}),
    }
    (directory / "latest_prediction.json").write_text(json.dumps(prediction))
    data_run = data / coin / "15m" / "runs" / data_id
    data_run.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"close_time": [pd.Timestamp(iso(decision))], "close": [price],
                  "volatility_log_return_20": [0.01]}).to_parquet(data_run / "features.parquet", index=False)
    (data_run.parent.parent / "latest.json").write_text(json.dumps({"run_id": data_id}))
    return prediction


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Paper runtime tests cannot make network requests.")
    monkeypatch.setattr("requests.post", forbidden)


@pytest.fixture
def runner(tmp_path):
    config, data = configurations(tmp_path)
    clock = Clock()
    market = FakeMarket(clock)
    instance = runtime_module.PaperRuntime(config, market=market, clock=clock)
    instance.initialize()
    yield instance, clock, market, data
    try:
        instance.ledger.close()
    except sqlite3.ProgrammingError:
        pass


@pytest.fixture
def qualified_runner(tmp_path):
    config, data = configurations(tmp_path, require_qualified_forecasts=True)
    clock = Clock()
    market = FakeMarket(clock)
    instance = runtime_module.PaperRuntime(config, market=market, clock=clock)
    instance.initialize()
    yield instance, clock, market, data
    try:
        instance.ledger.close()
    except sqlite3.ProgrammingError:
        pass


def test_fresh_forecast_fills_from_actual_spot_and_perp_books_and_preserves_roles(runner):
    instance, clock, market, data = runner
    forecast(data, p=0.65, qualified=False, price=77)
    result = instance.tick()
    fills = instance.ledger.history("fills")
    assert {row["account"] for row in fills} == {"jeremy", "clearpond"}
    assert all(row["quantity"] > 0 for row in fills)
    for fill in fills:
        expected = market.price[fill["kind"]] + 0.1
        assert fill["price"] == pytest.approx(expected)
        assert fill["price"] != 77
        details = json.loads(fill["details_json"])
        assert details["qualified"] is False
        assert runtime_module.stamp(details["book_time_utc"]) >= BASE + 5
    pooled = result["portfolio"]["pooled"]
    execution_loss = sum(row["quantity"] * (row["price"] - market.price[row["kind"]]) + row["fee"] for row in fills)
    assert pooled["equity"] == pytest.approx(30000 - execution_loss)
    assert pooled["fees"] == pytest.approx(sum(row["fee"] for row in fills))
    assert pooled["net_transfers"] == pytest.approx(0)


def test_partial_taker_buys_and_sells_use_book_vwap_and_reconcile_cash_fees_and_inventory(runner, monkeypatch):
    instance, clock, market, data = runner
    original_snapshot = market.snapshot

    def limited_multilevel_snapshot(symbols):
        observation = original_snapshot(symbols)
        for book in observation["markets"].values():
            mark = book["mark"]
            book["asks"] = [[mark + 0.1, 1], [mark + 0.2, 1]]
            book["bids"] = [[mark - 0.1, 0.5], [mark - 0.2, 0.75]]
        return observation

    monkeypatch.setattr(market, "snapshot", limited_multilevel_snapshot)
    forecast(data, p=0.65)
    opened = instance.tick()["portfolio"]
    buys = {row["account"]: row for row in instance.ledger.history("fills")}
    assert set(buys) == {"jeremy", "clearpond"}
    for account, kind, fee_rate in (("jeremy", "perp", 0.00045), ("clearpond", "spot", 0.0007)):
        fill = buys[account]
        details = json.loads(fill["details_json"])
        notional = 2 * market.price[kind] + 0.3
        fee = notional * fee_rate
        assert fill["quantity"] == 2
        assert fill["price"] == details["raw_book_vwap"] == pytest.approx(notional / 2)
        assert fill["notional"] == pytest.approx(notional)
        assert fill["fee"] == pytest.approx(fee)
        assert details["fee_rate"] == fee_rate
        assert details["extra_slippage_bps"] == 0
        assert details["levels_consumed"] == 2
        assert details["status"] == "partial" and details["unfilled_quantity"] > 0
        values = opened["accounts"][account]
        assert values["positions"][0]["quantity"] == 2
        assert values["cash"] == pytest.approx(10000 - fee - (notional if kind == "spot" else 0))
        assert values["equity"] == pytest.approx(10000 - 0.3 - fee)
        assert values["net_transfers"] == 0

    clock.now = BASE + 930
    forecast(data, identity="forecast-2", decision=BASE + 900, p=0.5)
    reduced = instance.tick()["portfolio"]
    sells = {row["account"]: row for row in instance.ledger.history("fills")
             if row["forecast_id"] == "forecast-2"}
    assert set(sells) == set(buys)
    for account, kind, fee_rate in (("jeremy", "perp", 0.00045), ("clearpond", "spot", 0.0007)):
        fill = sells[account]
        details = json.loads(fill["details_json"])
        notional = 1.25 * market.price[kind] - 0.2
        fee = notional * fee_rate
        realized = 1.25 * (notional / 1.25 - buys[account]["price"])
        assert fill["quantity"] == -1.25
        assert fill["price"] == details["raw_book_vwap"] == pytest.approx(notional / 1.25)
        assert fill["notional"] == pytest.approx(notional)
        assert fill["fee"] == pytest.approx(fee)
        assert fill["realized_pnl"] == pytest.approx(realized)
        assert details["fee_rate"] == fee_rate
        assert details["extra_slippage_bps"] == 0
        assert details["levels_consumed"] == 2
        assert details["status"] == "partial" and details["unfilled_quantity"] == -0.75
        values = reduced["accounts"][account]
        assert values["positions"][0]["quantity"] == 0.75
        assert values["positions"][0]["avg_entry"] == buys[account]["price"]
        cash_delta = (notional if kind == "spot" else realized) - fee
        assert values["cash"] == pytest.approx(opened["accounts"][account]["cash"] + cash_delta)
        assert values["fees"] == pytest.approx(buys[account]["fee"] + fee)
        assert values["equity"] == pytest.approx(opened["accounts"][account]["equity"] - 0.2 - fee)
        assert values["total_pnl"] == pytest.approx(values["equity"] - 10000)
    assert reduced["pooled"]["net_transfers"] == 0


def test_same_forecast_is_not_filled_again_after_poll_or_restart(runner):
    instance, clock, market, data = runner
    forecast(data)
    instance.tick()
    original = instance.ledger.history("fills")
    clock.now += 31
    instance.tick()
    assert instance.ledger.history("fills") == original
    instance.ledger.close()
    reopened = runtime_module.PaperRuntime(instance.config_path, market=market, clock=clock)
    reopened.initialize()
    try:
        reopened.tick()
        assert reopened.ledger.history("fills") == original
    finally:
        reopened.ledger.close()


def test_new_bearish_forecast_closes_long_books_and_opens_only_alex(runner):
    instance, clock, market, data = runner
    forecast(data)
    instance.tick()
    clock.now = BASE + 930
    forecast(data, identity="forecast-2", decision=BASE+900, p=0.35, qualified=True)
    result = instance.tick()
    positions = result["portfolio"]["pooled"]["positions"]
    assert len(positions) == 1
    assert positions[0]["account"] == "alex" and positions[0]["quantity"] < 0
    second_fills = [row for row in instance.ledger.history("fills") if row["forecast_id"] == "forecast-2"]
    assert second_fills[-1]["account"] == "alex"
    assert all(row["quantity"] < 0 for row in second_fills)


def test_stale_forecast_exits_existing_positions_using_current_books(runner):
    instance, clock, market, data = runner
    forecast(data)
    instance.tick()
    clock.now = BASE + instance.config.max_forecast_age_seconds + 1
    result = instance.tick()
    assert result["portfolio"]["pooled"]["positions"] == []
    assert "stale" in result["errors"]["BTC"]
    exits = instance.ledger.history("fills")[-2:]
    assert all(row["quantity"] < 0 for row in exits)
    assert all(row["forecast_id"] is None for row in exits)


def test_stop_loss_does_not_reopen_on_the_same_forecast(runner):
    instance, clock, market, data = runner
    forecast(data)
    instance.tick()
    clock.now += 31
    market.price = {"perp": 94.0, "spot": 95.0}
    result = instance.tick()
    assert result["portfolio"]["pooled"]["positions"] == []
    stopped_fills = list(instance.ledger.history("fills"))
    assert all(row["reason"] == "stop_loss" for row in stopped_fills[-2:])
    clock.now += 31
    market.price = {"perp": 100.0, "spot": 102.0}
    instance.tick()
    assert instance.ledger.history("fills") == stopped_fills


def test_stop_cooldown_survives_restart_and_new_forecast_before_horizon(runner):
    instance, clock, market, data = runner
    forecast(data)
    instance.tick()
    clock.now += 31
    market.price = {"perp": 94.0, "spot": 95.0}
    instance.tick()
    stopped_fills = instance.ledger.history("fills")
    instance.ledger.close()
    clock.now = BASE + 930
    market.price = {"perp": 100.0, "spot": 102.0}
    forecast(data, identity="forecast-2", decision=BASE+900)
    reopened = runtime_module.PaperRuntime(instance.config_path, market=market, clock=clock)
    reopened.initialize()
    try:
        reopened.tick()
        assert reopened.ledger.history("fills") == stopped_fills
        assert reopened.ledger.state({"perp:BTC": 100, "spot:BTC": 102})["pooled"]["positions"] == []
    finally:
        reopened.ledger.close()


def test_reductions_below_exchange_minimum_are_reported_as_unfilled_dust(runner):
    instance, clock, market, data = runner
    marks = {"perp:BTC": 100.0, "spot:BTC": 102.0}
    instance.ledger.execute_cycle("setup", clock(), marks, [
        {"account": "jeremy", "coin": "BTC", "kind": "perp", "quantity": 0.05, "price": 100},
    ])
    forecast(data)
    market.price["perp"] = 90
    result = instance.tick()
    assert result["portfolio"]["accounts"]["jeremy"]["positions"][0]["quantity"] == 0.05
    stop = [json.loads(row["details_json"]) for row in instance.ledger.history("decisions")
            if row["account"] == "jeremy"]
    assert stop and stop[0]["action"] == "skip"
    assert stop[0]["reason"] == "below_min_notional"
    assert stop[0]["execution"]["reason"] == "below_min_notional"
    checks = stop[0]["decision_checks"]
    assert checks["trigger_reason"] == "stop_loss"
    assert checks["execution_status"] == "unfilled"
    assert checks["rebalance_required"] and checks["rebalance_forced"]
    assert checks["venue_minimum_fill_notional"] == 10
    assert checks["delta_notional"] == pytest.approx(-4.5)
    assert ("jeremy", "BTC") not in instance._stops  # No stop fill was invented.


@pytest.mark.parametrize("probability", [0.35, 0.65])
def test_research_exclusion_can_be_enabled_explicitly(runner, probability):
    instance, clock, market, data = runner
    instance.config = replace(instance.config, require_qualified_forecasts=True)
    forecast(data, qualified=False, p=probability)
    instance.tick()
    assert instance.ledger.history("fills") == []
    assert instance.ledger.history("transfers") == []
    for row in instance.ledger.history("decisions"):
        details = json.loads(row["details_json"])
        assert row["reason"] == "unqualified_forecast_excluded"
        assert row["forecast_id"] is None and row["model_id"] is None
        assert details["qualified"] is None
        assert details["policy"]["rejected_forecast"]["p_not_down"] == probability


@pytest.mark.parametrize("probability", [0.35, 0.65])
def test_qualified_hold_preserves_existing_inventory_despite_research_direction(qualified_runner, probability):
    instance, clock, market, data = qualified_runner
    marks = {"perp:BTC": 100.0, "spot:BTC": 102.0}
    instance._execute("inherited", marks, orders=[
        {"account": "alex", "coin": "BTC", "kind": "perp", "quantity": -2, "price": 100},
        {"account": "jeremy", "coin": "BTC", "kind": "perp", "quantity": 2, "price": 100},
        {"account": "clearpond", "coin": "BTC", "kind": "spot", "quantity": 3, "price": 102},
    ])
    inventory, fills = instance.ledger.inventory(), instance.ledger.history("fills")
    rejected = forecast(data, qualified=False, p=probability)
    instance.tick()
    clock.now += 31
    instance.tick()
    assert instance.ledger.inventory() == inventory
    assert instance.ledger.history("fills") == fills
    assert not instance.ledger.history("transfers")
    for row in instance.ledger.history("decisions"):
        details = json.loads(row["details_json"])
        assert row["action"] == "hold" and row["reason"] == "unqualified_forecast_excluded"
        assert row["forecast_id"] is None and row["model_id"] is None
        assert all(details[key] is None for key in ("qualified", "data_run_id", "forecast_created_at_utc", "p_not_down"))
        assert details["policy"]["rejected_forecast"]["prediction_id"] == rejected["prediction_id"]
        assert details["policy"]["rejected_forecast"]["qualified"] is False
        assert details["target_notional"] == details["current_notional"]


@pytest.mark.parametrize("probability,accounts", [(0.35, {"alex"}), (0.65, {"jeremy", "clearpond"})])
def test_qualified_signal_can_allocate_in_qualified_hold_recipe(qualified_runner, probability, accounts):
    instance, _, _, data = qualified_runner
    forecast(data, qualified=True, p=probability)
    instance.tick()
    fills = instance.ledger.history("fills")
    assert {row["account"] for row in fills} == accounts
    assert all(json.loads(row["details_json"])["qualified"] is True for row in fills)
    assert all(row["forecast_id"] == "forecast-1" for row in fills)
    assert json.loads((data / "_paper" / "policy.json").read_text())["recipe_version"] == "direction-volatility-v2-qualified-hold"


def test_qualified_hold_rollover_has_no_closing_cost_and_fresh_signal_resumes(qualified_runner):
    instance, clock, market, data = qualified_runner
    forecast(data, qualified=True)
    first = instance.tick()
    inventory, fills = instance.ledger.inventory(), instance.ledger.history("fills")
    clock.now = BASE + 901
    waiting = instance.tick()
    assert instance.ledger.inventory() == inventory
    assert instance.ledger.history("fills") == fills
    assert waiting["portfolio"]["pooled"]["fees"] == first["portfolio"]["pooled"]["fees"]
    assert "stale" in waiting["errors"]["BTC"]
    assert all(row["reason"] == "qualified_forecast_unavailable"
               for row in instance.ledger.history("decisions") if row["forecast_id"] is None)
    clock.now = BASE + 906
    forecast(data, qualified=True, identity="forecast-2", decision=BASE+900, p=.35)
    resumed = instance.tick()
    assert {p["account"] for p in resumed["portfolio"]["pooled"]["positions"]} == {"alex"}
    assert all(row["forecast_id"] == "forecast-2" for row in instance.ledger.history("fills")[len(fills):])


@pytest.mark.parametrize("unavailable", [False, True])
def test_qualified_hold_stop_exit_does_not_use_rejected_or_missing_signal(qualified_runner, unavailable):
    instance, clock, market, data = qualified_runner
    marks = {"perp:BTC": 100.0, "spot:BTC": 102.0}
    instance._execute("inherited", marks, orders=[
        {"account": "jeremy", "coin": "BTC", "kind": "perp", "quantity": 2, "price": 100},
    ])
    if not unavailable:
        forecast(data, qualified=False)
        market.book_offset = -26  # Fresh book may predate an excluded publication.
    market.price["perp"] = 94
    result = instance.tick()
    assert result["portfolio"]["pooled"]["positions"] == []
    exit_fill = instance.ledger.history("fills")[-1]
    assert exit_fill["reason"] == "stop_loss" and exit_fill["quantity"] == -2
    assert exit_fill["forecast_id"] is None and exit_fill["model_id"] is None
    assert json.loads(exit_fill["details_json"])["qualified"] is None
    assert instance._stops[("jeremy", "BTC")] == clock()


@pytest.mark.parametrize("missing", [False, True])
def test_qualified_hold_unavailable_forecast_preserves_existing_inventory(qualified_runner, missing):
    instance, _, _, data = qualified_runner
    marks = {"perp:BTC": 100.0, "spot:BTC": 102.0}
    instance._execute("inherited", marks, orders=[
        {"account": "jeremy", "coin": "BTC", "kind": "perp", "quantity": 2, "price": 100},
    ])
    if not missing:
        forecast(data, qualified=True, overrides={"p_down": .9})
    inventory, fills = instance.ledger.inventory(), instance.ledger.history("fills")
    result = instance.tick()
    assert instance.ledger.inventory() == inventory
    assert instance.ledger.history("fills") == fills
    assert not instance.ledger.history("transfers")
    assert result["errors"]["BTC"]
    for row in instance.ledger.history("decisions"):
        details = json.loads(row["details_json"])
        assert row["reason"] == "qualified_forecast_unavailable"
        assert details["policy"]["reason"] == result["errors"]["BTC"]
        assert row["forecast_id"] is None and row["model_id"] is None


def test_qualified_hold_partial_stop_continues_in_cooldown_after_restart(qualified_runner):
    instance, clock, market, data = qualified_runner
    marks = {"perp:BTC": 100.0, "spot:BTC": 102.0}
    instance._execute("inherited", marks, orders=[
        {"account": "jeremy", "coin": "BTC", "kind": "perp", "quantity": .3, "price": 100},
    ])
    forecast(data, qualified=False)
    market.price["perp"], market.depth = 94, .12
    instance.tick()
    assert instance.ledger.history("fills")[-1]["reason"] == "stop_loss"
    assert instance.ledger.inventory()[0]["quantity"] == pytest.approx(.18)
    instance.ledger.close()
    clock.now += 31
    # Recovery removes the price stop, but the $18 residual is still an exit
    # despite falling below the ordinary $25 rebalance threshold.
    market.price["perp"] = 100
    reopened = runtime_module.PaperRuntime(instance.config_path, market=market, clock=clock)
    reopened.initialize()
    try:
        reopened.tick()
        reduction = reopened.ledger.history("fills")[-1]
        assert reduction["reason"] == "stop_cooldown"
        assert reduction["quantity"] == pytest.approx(-.12)
        assert reduction["forecast_id"] is None and reduction["model_id"] is None
        assert reopened.ledger.inventory()[0]["quantity"] == pytest.approx(.06)
        assert not reopened.ledger.history("transfers")
    finally:
        reopened.ledger.close()


@pytest.mark.parametrize("cap", ["account", "symbol", "pool"])
def test_qualified_hold_still_reduces_exposure_caps(qualified_runner, cap):
    instance, clock, market, data = qualified_runner
    limits = {"account_utilization": 1, "per_symbol_gross_fraction": 1, "pool_gross_fraction": 1}
    limits[{"account": "account_utilization", "symbol": "per_symbol_gross_fraction", "pool": "pool_gross_fraction"}[cap]] = .3 if cap == "account" else .1
    instance.config = replace(instance.config, **limits)
    marks = {"perp:BTC": 100.0, "spot:BTC": 102.0}
    instance._execute("inherited", marks, orders=[
        {"account": "jeremy", "coin": "BTC", "kind": "perp", "quantity": 50, "price": 100},
    ])
    forecast(data, qualified=False)
    instance.tick()
    reduction = instance.ledger.history("fills")[-1]
    assert reduction["reason"] == "risk_cap"
    assert reduction["quantity"] == pytest.approx(-20)
    assert reduction["forecast_id"] is None and reduction["model_id"] is None
    assert not instance.ledger.history("transfers")


def test_qualified_hold_rechecks_caps_after_same_research_forecast(qualified_runner):
    instance, clock, market, data = qualified_runner
    instance.config = replace(instance.config, per_symbol_gross_fraction=1, pool_gross_fraction=1)
    marks = {"perp:BTC": 100.0, "spot:BTC": 102.0}
    instance._execute("inherited", marks, orders=[
        {"account": "jeremy", "coin": "BTC", "kind": "perp", "quantity": 50, "price": 100},
    ])
    forecast(data, qualified=False)
    instance.tick()
    assert len(instance.ledger.history("fills")) == 1
    instance._execute("funding-loss", marks, funding=[
        {"funding_id": "funding-loss", "account": "jeremy", "coin": "BTC", "amount": -5000},
    ])
    clock.now += 31
    instance.tick()
    reduction = instance.ledger.history("fills")[-1]
    assert reduction["reason"] == "risk_cap" and reduction["quantity"] == pytest.approx(-10)


def test_qualified_hold_and_signal_idempotency_survive_restart(qualified_runner):
    instance, clock, market, data = qualified_runner
    forecast(data, qualified=True)
    instance.tick()
    original_fills = instance.ledger.history("fills")
    instance.ledger.close()
    clock.now += 31
    reopened = runtime_module.PaperRuntime(instance.config_path, market=market, clock=clock)
    reopened.initialize()
    try:
        reopened.tick()
        assert reopened.ledger.history("fills") == original_fills
        clock.now = BASE + 901
        reopened.tick()
        assert reopened.ledger.history("fills") == original_fills
    finally:
        reopened.ledger.close()


def test_qualified_only_missing_forecast_id_does_not_bypass_stop_checks(runner):
    instance, clock, market, data = runner
    instance.config = replace(instance.config, require_qualified_forecasts=True)
    prediction = forecast(data, qualified=True)
    instance.tick()
    clock.now += 31
    market.price = {"perp": 94.0, "spot": 95.0}
    del prediction["prediction_id"]
    path = data / "_models" / "BTC" / "15m" / "h4" / "latest_prediction.json"
    path.write_text(json.dumps(prediction))
    result = instance.tick()
    assert "prediction_id" in result["errors"]["BTC"]
    assert result["portfolio"]["pooled"]["positions"] == []
    stops = instance.ledger.history("fills")[-2:]
    assert all(row["reason"] == "stop_loss" and row["forecast_id"] is None for row in stops)


@pytest.mark.parametrize("offset", [-26, -60, 60])
def test_book_before_forecast_stale_or_future_cannot_create_fills(runner, offset):
    instance, clock, market, data = runner
    forecast(data)
    market.book_offset = offset
    instance.tick()
    assert instance.ledger.history("fills") == []
    for row in instance.ledger.history("decisions"):
        assert row["action"] == "skip"
        assert row["reason"] == ("Executable book predates the forecast." if offset < 0
                                 else "Executable book is stale or future-dated.")
        assert "decision_checks" in json.loads(row["details_json"])


def test_quote_retries_are_bounded_durable_and_fill_once_after_restart(qualified_runner, monkeypatch):
    instance, clock, market, data = qualified_runner
    forecast(data, qualified=True)
    sleeps = []
    monkeypatch.setattr(runtime_module.time, "sleep", sleeps.append)
    market.book_offset = -26
    instance.tick()
    assert len(market.snapshot_calls) == 3 and sleeps == [.1, .2]
    assert not instance.ledger.has_cycle("forecast:forecast-1")
    assert instance.ledger.has_cycle("forecast-wait:forecast-1")
    original = instance.ledger.history("decisions")
    assert len(original) == 3
    assert all(json.loads(row["details_json"])["retry_pending"] for row in original)
    instance.tick()
    assert instance.ledger.history("decisions") == original
    instance.ledger.close()
    restarted = runtime_module.PaperRuntime(instance.config_path, market=market, clock=clock)
    try:
        restarted.initialize()
        market.book_offset = 0
        restarted.tick()
        fills = restarted.ledger.history("fills")
        assert len(fills) == 2
        assert restarted.ledger.has_cycle("forecast:forecast-1")
        restarted.tick()
        assert restarted.ledger.history("fills") == fills
    finally:
        restarted.ledger.close()


def test_second_snapshot_can_fill_frozen_forecast_without_consuming_bad_quotes(qualified_runner, monkeypatch):
    instance, clock, market, data = qualified_runner
    forecast(data, qualified=True)
    original_snapshot = market.snapshot
    def snapshot(symbols):
        market.book_offset = -26 if not market.snapshot_calls else 0
        return original_snapshot(symbols)
    monkeypatch.setattr(market, "snapshot", snapshot)
    monkeypatch.setattr(runtime_module.time, "sleep", lambda _: None)
    instance.tick()
    assert len(market.snapshot_calls) == 2
    assert len(instance.ledger.history("fills")) == 2
    assert not instance.ledger.has_cycle("forecast-wait:forecast-1")
    assert all(json.loads(row["details_json"])["decision_checks"]["quote_attempt_count"] == 2
               for row in instance.ledger.history("decisions"))


def test_forecast_published_during_snapshot_waits_until_next_tick(qualified_runner, monkeypatch):
    instance, clock, market, data = qualified_runner
    forecast(data, qualified=True)
    original_snapshot = market.snapshot
    def snapshot(symbols):
        forecast(data, qualified=True, identity="forecast-2", p=.35)
        return original_snapshot(symbols)
    monkeypatch.setattr(market, "snapshot", snapshot)
    instance.tick()
    assert {row["forecast_id"] for row in instance.ledger.history("fills")} == {"forecast-1"}
    clock.now += 30
    instance.tick()
    assert {row["forecast_id"] for row in instance.ledger.history("fills")} == {"forecast-1", "forecast-2"}


def test_forecast_expiring_during_snapshot_is_not_executed(qualified_runner, monkeypatch):
    instance, clock, market, data = qualified_runner
    forecast(data, qualified=True)
    original_snapshot = market.snapshot
    def snapshot(symbols):
        clock.now = BASE + 3601
        return original_snapshot(symbols)
    monkeypatch.setattr(market, "snapshot", snapshot)
    result = instance.tick()
    assert instance.ledger.history("fills") == []
    assert "expired while fetching" in result["errors"]["BTC"]
    assert not instance.ledger.has_cycle("forecast:forecast-1")


def test_cached_seed_quote_is_never_used_for_trading(qualified_runner):
    instance, clock, market, data = qualified_runner
    market.book_offset = -26
    instance._seed_quotes = market.snapshot(("BTC",))
    market.book_offset = 0
    forecast(data, qualified=True)
    seed = instance.ledger.seed()
    instance.tick()
    assert len(instance.ledger.history("fills")) == 2
    assert len(market.snapshot_calls) == 2
    assert instance.ledger.seed() == seed


def test_legacy_all_quote_skip_is_recovered_append_only_once(qualified_runner):
    instance, clock, market, data = qualified_runner
    prediction = forecast(data, qualified=True)
    marks = {"perp:BTC": 100, "spot:BTC": 102}
    decisions = [{"account": account, "coin": "BTC", "forecast_id": prediction["prediction_id"],
                  "action": "skip", "reason": "Executable book predates the forecast."}
                 for account in runtime_module.ACCOUNTS]
    instance._execute("forecast:forecast-1", marks, decisions=decisions)
    original = instance.ledger.history("decisions")
    cycle = next(row for row in instance.ledger.history("cycles") if row["cycle_id"] == "forecast:forecast-1")
    instance.tick()
    fills = instance.ledger.history("fills")
    assert len(fills) == 2
    assert {row["cycle_id"] for row in fills} == {"forecast-retry:forecast-1"}
    assert instance.ledger.history("decisions")[:3] == original
    assert next(row for row in instance.ledger.history("cycles") if row["cycle_id"] == "forecast:forecast-1") == cycle
    instance.tick()
    assert instance.ledger.history("fills") == fills


def test_legacy_recovery_can_wait_across_restart_and_respects_stop_cooldown(qualified_runner, monkeypatch):
    instance, clock, market, data = qualified_runner
    forecast(data, qualified=True)
    marks = {"perp:BTC": 100, "spot:BTC": 102}
    instance._execute("setup", marks, orders=[{
        "account": "jeremy", "coin": "BTC", "kind": "perp", "quantity": 1, "price": 100,
    }])
    instance._execute("forecast:forecast-1", marks, decisions=[{
        "account": account, "coin": "BTC", "forecast_id": "forecast-1", "action": "skip",
        "reason": "Executable book predates the forecast.",
    } for account in runtime_module.ACCOUNTS])
    market.book_offset = -26
    monkeypatch.setattr(runtime_module.time, "sleep", lambda _: None)
    instance.tick()
    assert instance.ledger.has_cycle("forecast-wait:forecast-1")
    assert not instance.ledger.has_cycle("forecast-retry:forecast-1")
    instance.ledger.close()
    restarted = runtime_module.PaperRuntime(instance.config_path, market=market, clock=clock)
    try:
        restarted.initialize()
        market.book_offset = 0
        market.price["perp"] = 94
        restarted.tick()
        assert restarted.ledger.has_cycle("forecast-retry:forecast-1")
        assert not restarted.ledger.state({"perp:BTC": 94, "spot:BTC": 102})["accounts"]["jeremy"]["positions"]
        fills = restarted.ledger.history("fills")
        assert any(row["reason"] == "stop_loss" for row in fills)
        clock.now += 30
        restarted.tick()
        assert restarted.ledger.history("fills") == fills
    finally:
        restarted.ledger.close()


def test_new_publication_supersedes_quote_pending_forecast(qualified_runner, monkeypatch):
    instance, clock, market, data = qualified_runner
    forecast(data, qualified=True)
    market.book_offset = -26
    monkeypatch.setattr(runtime_module.time, "sleep", lambda _: None)
    instance.tick()
    market.book_offset = 0
    forecast(data, qualified=True, identity="new-publication", p=.35)
    instance.tick()
    assert {row["forecast_id"] for row in instance.ledger.history("fills")} == {"new-publication"}
    assert not instance.ledger.has_cycle("forecast:forecast-1")
    assert not instance.ledger.has_cycle("forecast-retry:forecast-1")


@pytest.mark.parametrize("mutation", ["hold", "depth", "missing", "transfer", "fill"])
def test_legacy_mixed_or_executed_cycles_are_never_recovered(qualified_runner, mutation):
    instance, clock, market, data = qualified_runner
    forecast(data, qualified=True)
    decisions = [{"account": account, "coin": "BTC", "forecast_id": "forecast-1",
                  "action": "skip", "reason": "Executable book predates the forecast."}
                 for account in runtime_module.ACCOUNTS]
    kwargs = {}
    if mutation == "hold":
        decisions[0].update(action="hold", reason="target_unchanged")
    elif mutation == "depth":
        decisions[0]["reason"] = "insufficient_depth"
    elif mutation == "missing":
        decisions.pop()
    elif mutation == "transfer":
        kwargs["transfers"] = [{"from_account": "clearpond", "to_account": "jeremy", "amount": 20}]
    else:
        kwargs["orders"] = [{"account": "jeremy", "coin": "BTC", "kind": "perp", "quantity": 1, "price": 100}]
    instance._execute("forecast:forecast-1", {"perp:BTC": 100, "spot:BTC": 102}, decisions=decisions, **kwargs)
    before = instance.ledger.history("fills")
    instance.tick()
    assert instance.ledger.history("fills") == before
    assert not instance.ledger.has_cycle("forecast-retry:forecast-1")


@pytest.mark.parametrize("overrides", [
    {"created_at_utc": iso(BASE + 31)},
    {"decision_close_utc": iso(BASE + 900)},
    {"target_close_utc": iso(BASE + 29)},
    {"p_down": 0.9}, {"qualified": "false"},
    {"data_run_id": "../outside"}, {"model_id": "../outside"},
])
def test_forecast_contract_errors_do_not_trade(runner, overrides):
    instance, clock, market, data = runner
    forecast(data, overrides=overrides)
    result = instance.tick()
    assert instance.ledger.history("fills") == []
    assert "BTC" in result["errors"]


@pytest.mark.parametrize("record_overrides", [{"coin": "ETH"}, {"interval": "1h"},
                                               {"horizon_bars": 16}, {"model_id": "wrong"}])
def test_forecast_model_record_must_match_market_horizon_and_identifier(runner, record_overrides):
    instance, clock, market, data = runner
    forecast(data, record_overrides=record_overrides)
    result = instance.tick()
    assert instance.ledger.history("fills") == []
    assert "BTC" in result["errors"]


def test_forecast_target_must_match_exact_horizon(runner):
    instance, clock, market, data = runner
    forecast(data, overrides={"target_close_utc": iso(BASE + 7200)})
    result = instance.tick()
    assert instance.ledger.history("fills") == []
    assert "BTC" in result["errors"]


def test_forecast_model_cannot_be_trained_after_forecast_was_created(runner):
    instance, clock, market, data = runner
    forecast(data, record_overrides={"trained_at_utc": iso(BASE + 10)})
    result = instance.tick()
    assert instance.ledger.history("fills") == []
    assert "BTC" in result["errors"]


def test_opened_positions_remain_watched_when_removed_from_market_universe(runner):
    instance, clock, market, data = runner
    forecast(data)
    instance.tick()
    path = instance.model_config.markets_config
    configured = json.loads(path.read_text())
    configured["symbols"] = ["ETH"]
    path.write_text(json.dumps(configured))
    clock.now += 31
    instance.tick()
    assert set(market.snapshot_calls[-1]) == {"BTC", "ETH"}


def test_quote_failure_does_not_invent_missing_held_asset_marks(runner):
    instance, clock, market, data = runner
    forecast(data)
    instance.tick()
    market.omit.add("spot:BTC")
    original = instance.ledger.history("fills")
    with pytest.raises(ValueError, match="Missing mark"):
        instance.tick()
    assert instance.ledger.history("fills") == original


def test_mirror_initialization_preserves_inventory_and_resume_never_reads_accounts_again(tmp_path, monkeypatch):
    config, data = configurations(tmp_path, seed_mode="mirror")
    clock, calls = Clock(), []
    market = FakeMarket(clock)

    def mirror(provider, symbols, *, clock):
        calls.append(tuple(symbols))
        observation = provider.snapshot(symbols)
        return {"initial_cash": {"alex": 1000, "jeremy": 2000, "clearpond": 3000},
                "initial_positions": [{"account": "jeremy", "coin": "BTC", "kind": "perp", "quantity": 2, "average_entry": 90},
                                      {"account": "clearpond", "coin": "BTC", "kind": "spot", "quantity": 3, "average_entry": 102}],
                "initial_marks": {key: row["mark"] for key, row in observation["markets"].items()},
                "now": clock(), "metadata": {"seed_mode": "mirror"}, "quotes": observation}

    monkeypatch.setattr(runtime_module, "mirror_accounts", mirror)
    first = runtime_module.PaperRuntime(config, market=market, clock=clock)
    first.initialize()
    opening = first.ledger.seed()
    assert len(opening["positions"]) == 2
    assert sum(opening["baseline_equity"].values()) == pytest.approx(6326)
    assert opening["metadata"]["seed_mode"] == "mirror"
    first.ledger.close()
    second = runtime_module.PaperRuntime(config, market=market, clock=clock)
    second.initialize()
    try:
        assert second.ledger.seed() == opening
        assert calls == [("BTC",)]
    finally:
        second.ledger.close()


def test_failed_mirror_does_not_fall_back_to_default_cash(tmp_path, monkeypatch):
    config, data = configurations(tmp_path, seed_mode="mirror")
    clock = Clock()
    monkeypatch.setattr(runtime_module, "mirror_accounts", lambda *a, **k: (_ for _ in ()).throw(ValueError("read unavailable")))
    instance = runtime_module.PaperRuntime(config, market=FakeMarket(clock), clock=clock)
    with pytest.raises(ValueError, match="read unavailable"):
        instance.initialize()
    assert not (data / "_paper" / "ledger.sqlite3").exists()


def test_empty_existing_database_is_not_treated_as_a_mirrored_seed(tmp_path):
    config, data = configurations(tmp_path, seed_mode="mirror")
    database = data / "_paper" / "ledger.sqlite3"
    database.parent.mkdir(parents=True)
    sqlite3.connect(database).close()
    clock = Clock()
    instance = runtime_module.PaperRuntime(config, market=FakeMarket(clock), clock=clock)
    with pytest.raises(ValueError, match="seed|initial|empty"):
        instance.initialize()


def test_virtual_transfer_allocates_collateral_without_changing_pool_equity(tmp_path):
    config, data = configurations(tmp_path, initial_cash={"alex": 1000, "jeremy": 1000, "clearpond": 28000})
    clock, market = Clock(), None
    market = FakeMarket(clock)
    instance = runtime_module.PaperRuntime(config, market=market, clock=clock)
    instance.initialize()
    try:
        forecast(data, p=0.35)
        result = instance.tick()
        transfers = instance.ledger.history("transfers")
        assert transfers and all(row["to_account"] == "alex" for row in transfers)
        state = result["portfolio"]
        assert state["pooled"]["net_transfers"] == pytest.approx(0)
        assert sum(account["net_transfers"] for account in state["accounts"].values()) == pytest.approx(0)
        assert state["accounts"]["alex"]["positions"][0]["quantity"] < 0
        assert state["accounts"]["alex"]["gross_exposure"] <= state["accounts"]["alex"]["equity"] * instance.config.account_utilization + 1
        fills = instance.ledger.history("fills")
        cost = sum(row["fee"] + abs(row["quantity"]) * (100-row["price"]) for row in fills)
        assert state["pooled"]["equity"] == pytest.approx(30000-cost)
    finally:
        instance.ledger.close()


@pytest.mark.parametrize("current_btc_quantity", [0.0, -2.0])
def test_unrelated_neutral_or_closing_coin_does_not_fund_other_positions(runner, current_btc_quantity):
    instance, clock, market, data = runner
    marks = {"perp:BTC": 100.0, "spot:BTC": 102.0, "perp:ETH": 100.0}
    state = instance.ledger.state(marks)
    # This is an inherited account above the paper utilization limit. Closing
    # or ignoring BTC should not fund its separate ETH exposure as a side effect.
    alex = state["accounts"]["alex"]
    alex.update(cash=1000.0, initial_equity=1000.0, equity=1000.0, free_cash=0.0,
                gross_exposure=1200.0, positions=[{
                    "account": "alex", "coin": "ETH", "kind": "perp",
                    "quantity": -12 + abs(current_btc_quantity), "market": "perp:ETH",
                    "avg_entry": 100.0,
                }])
    if current_btc_quantity:
        alex["positions"].append({"account": "alex", "coin": "BTC", "kind": "perp",
                                  "quantity": current_btc_quantity, "market": "perp:BTC", "avg_entry": 100.0})
    transfers, _, _ = instance._plan_transfers(state, "BTC", {account: 0.0 for account in runtime_module.ACCOUNTS}, marks)
    assert transfers == []


def test_visible_depth_is_shared_and_consumed_by_reductions_first(runner):
    instance, clock, market, data = runner
    marks = {"perp:BTC": 100.0, "spot:BTC": 102.0}
    instance.ledger.execute_cycle("setup", clock(), marks, [
        {"account": "alex", "coin": "BTC", "kind": "perp", "quantity": -1, "price": 100},
        {"account": "jeremy", "coin": "BTC", "kind": "perp", "quantity": 2, "price": 100},
    ])
    forecast(data, p=0.35)
    market.depth = 1.0
    instance.tick()
    fills = [row for row in instance.ledger.history("fills") if row["forecast_id"] == "forecast-1"]
    assert sum(abs(row["quantity"]) for row in fills if row["kind"] == "perp") <= 1.0
    assert fills[0]["account"] == "jeremy"


def test_drawdown_uses_one_pool_value_per_cycle_without_counting_account_rows_twice(runner):
    instance, clock, market, data = runner
    marks = {"perp:BTC": 100.0, "spot:BTC": 102.0}
    instance._execute("first", marks)
    instance._execute("fee", marks, orders=[{"account": "jeremy", "coin": "BTC", "kind": "perp", "quantity": 1,
                                             "price": 100, "fee_rate": 0.01}])
    instance._execute("same-time-mark", marks)
    clock.now += 30
    instance._execute("next-mark", marks)
    result = instance.report(instance.ledger.state(marks))
    assert result["max_drawdown_fraction"] == pytest.approx(-1 / 30000)
    assert result["fees"] == pytest.approx(1)


@pytest.mark.parametrize("offset_ms", [0, 76])
def test_funding_uses_quantity_at_settlement_and_exact_completed_price_proxy(runner, offset_ms):
    instance, clock, market, data = runner
    marks = {"perp:BTC": 100.0, "spot:BTC": 102.0}
    instance._execute("enter", marks, orders=[
        {"account": "jeremy", "coin": "BTC", "kind": "perp", "quantity": 10, "price": 100},
        {"account": "alex", "coin": "BTC", "kind": "perp", "quantity": -10, "price": 100},
    ])
    clock.now = BASE + 1800
    instance._execute("before-funding", marks, orders=[
        {"account": "jeremy", "coin": "BTC", "kind": "perp", "quantity": -4, "price": 100},
    ])
    clock.now = BASE + 3610
    instance._execute("after-funding", marks, orders=[
        {"account": "alex", "coin": "BTC", "kind": "perp", "quantity": 8, "price": 100},
    ])
    forecast(data, decision=BASE+3600, price=105)
    market.funding_rows = [{"coin": "BTC", "time": int((BASE+3600)*1000)+offset_ms,
                           "time_utc": iso(BASE+3600+offset_ms/1000), "funding_rate": 0.001}]
    instance._funding(marks, clock())
    amounts = {row["account"]: row["amount"] for row in instance.ledger.history("funding")}
    assert amounts == pytest.approx({"jeremy": -0.63, "alex": 1.05})
    assert all(json.loads(row["details_json"])["estimated"] for row in instance.ledger.history("funding"))
    clock.now += 301
    instance._funding(marks, clock())
    assert len(instance.ledger.history("funding")) == 2


def test_run_once_exports_and_closes_without_live_network(tmp_path):
    config, data = configurations(tmp_path)
    clock = Clock()
    market = FakeMarket(clock)
    forecast(data)
    instance = runtime_module.PaperRuntime(config, market=market, clock=clock)
    status = instance.run(once=True)
    assert status["status"] == "stopped"
    assert (data / "_paper" / "fills.parquet").exists()
    assert not runtime_module.read_status(data)["running"]


def test_paper_and_model_datastores_cannot_diverge(tmp_path):
    config, data = configurations(tmp_path)
    values = json.loads(config.read_text())
    values["data_root"] = str(tmp_path / "another-root")
    config.write_text(json.dumps(values))
    with pytest.raises(ValueError, match="same datastore"):
        runtime_module.PaperRuntime(config, market=FakeMarket(Clock()), clock=Clock())


@pytest.mark.parametrize("entry_band,probability,hold_reason,fill_accounts", [
    (.05, .5, "neutral_band", set()),
    (.05, .549999, "entry_deadband", set()),
    (.05, .450001, "entry_deadband", set()),
    (.05, .55, "opposite_account_direction", {"jeremy", "clearpond"}),
    (.05, .45, "opposite_account_direction", {"alex"}),
    (.04, .539999, "entry_deadband", set()),
    (.04, .54, "opposite_account_direction", {"jeremy", "clearpond"}),
    (.04, .46, "opposite_account_direction", {"alex"}),
])
def test_saved_entry_checks_explain_holds_without_changing_entries(
    qualified_runner, entry_band, probability, hold_reason, fill_accounts,
):
    instance, _, market, data = qualified_runner
    instance.config = replace(instance.config, entry_band=entry_band)
    forecast(data, qualified=True, p=probability)
    instance.tick()
    fills = instance.ledger.history("fills")
    assert {row["account"] for row in fills} == fill_accounts
    for row in instance.ledger.history("decisions"):
        decision = json.loads(row["details_json"])
        checks = decision["decision_checks"]
        assert checks["entry_probability_long"] == pytest.approx(.5 + entry_band)
        assert checks["entry_probability_short"] == pytest.approx(.5 - entry_band)
        assert checks["exit_probability_long"] == .52
        assert checks["exit_probability_short"] == .48
        assert checks["minimum_trade_notional"] == 25
        assert checks["rebalance_min_delta_fraction"] == .1
        assert checks["cooldown_until_utc"] is None
        assert checks["cooldown_remaining_seconds"] == 0
        assert checks["account_role"] == {"alex": "short_perp", "jeremy": "long_perp", "clearpond": "long_spot"}[row["account"]]
        if row["account"] not in fill_accounts:
            assert row["action"] == "hold" and row["reason"] == hold_reason
            assert decision["current_notional"] == decision["target_notional"] == 0
            assert not checks["rebalance_required"]
        else:
            assert row["action"] == "fill" and row["reason"] == "signal_rebalance"
            fill = next(fill for fill in fills if fill["account"] == row["account"])
            # The diagnostic payload stays out of order/fill records entirely.
            assert "decision_checks" not in json.loads(fill["details_json"])
            gross = 1500 * ((abs(probability-.5) - .02) / .13)
            share = {"alex": -1, "jeremy": .4, "clearpond": .6}[row["account"]]
            assert decision["target_notional"] == pytest.approx(gross * share)
            mark = market.price[fill["kind"]]
            assert abs(fill["quantity"] * mark - decision["target_notional"]) < mark * 1e-6
    assert instance.ledger.history("transfers") == []


@pytest.mark.parametrize("current,target,expected", [
    (100, 100, "target_unchanged"),
    (100, 124.999, "below_rebalance_threshold"),
    (100, 125, "signal_rebalance"),
    (900, 999.99, "below_rebalance_threshold"),
    (900, 1000, "signal_rebalance"),
])
def test_saved_rebalance_checks_match_actual_boundary_gate(qualified_runner, monkeypatch, current, target, expected):
    instance, clock, _, data = qualified_runner
    marks = {"perp:BTC": 100.0, "spot:BTC": 102.0}
    instance._execute("inherited", marks, orders=[
        {"account": "jeremy", "coin": "BTC", "kind": "perp", "quantity": current/100, "price": 100},
    ])
    # Isolate the runtime's rebalance gate from changing policy target sizes.
    monkeypatch.setattr(runtime_module, "target_notionals", lambda *args: {
        "targets": {"alex": 0.0, "jeremy": target, "clearpond": 0.0},
        "details": {"reason": "hold_with_hysteresis", "direction": "long"},
    })
    forecast(data, qualified=True)
    instance.tick()
    row = next(row for row in instance.ledger.history("decisions") if row["account"] == "jeremy")
    detail = json.loads(row["details_json"])
    checks = detail["decision_checks"]
    assert row["reason"] == expected
    assert checks["delta_notional"] == pytest.approx(target-current)
    assert checks["rebalance_threshold_notional"] == max(25, .1 * target)
    assert checks["rebalance_required"] is (expected == "signal_rebalance")
    assert not checks["rebalance_forced"]
    new_fills = [fill for fill in instance.ledger.history("fills") if fill["forecast_id"]]
    if expected == "signal_rebalance":
        assert len(new_fills) == 1
        assert new_fills[0]["quantity"] == pytest.approx((target-current)/100)
        assert new_fills[0]["reason"] == "signal_rebalance"
    else:
        assert new_fills == []
    assert instance.ledger.history("transfers") == []


@pytest.mark.parametrize("probability,policy_reason,jeremy_action,jeremy_reason", [
    (.52, "exit_band", "fill", "signal_rebalance"),
    (.54, "hold_with_hysteresis", "hold", "below_rebalance_threshold"),
])
def test_hysteresis_checks_distinguish_exit_from_small_adjustment(
    qualified_runner, probability, policy_reason, jeremy_action, jeremy_reason,
):
    instance, clock, _, data = qualified_runner
    instance._execute("inherited", {"perp:BTC": 100.0, "spot:BTC": 102.0}, orders=[
        {"account": "jeremy", "coin": "BTC", "kind": "perp", "quantity": 1, "price": 100},
    ])
    forecast(data, qualified=True, p=probability)
    instance.tick()
    decisions = {row["account"]: json.loads(row["details_json"]) for row in instance.ledger.history("decisions")}
    assert decisions["jeremy"]["decision_checks"]["policy_reason"] == policy_reason
    assert decisions["jeremy"]["action"] == jeremy_action
    assert decisions["jeremy"]["reason"] == jeremy_reason
    if probability == .52:
        assert decisions["alex"]["reason"] == decisions["clearpond"]["reason"] == "exit_band"
        assert instance.ledger.history("fills")[-1]["quantity"] == -1


def test_flat_stop_cooldown_is_explained_without_enabling_retries(qualified_runner):
    instance, clock, market, data = qualified_runner
    forecast(data, qualified=True)
    instance.tick()
    clock.now += 31
    market.price = {"perp": 94.0, "spot": 95.0}
    instance.tick()
    stopped_at = clock()
    assert not instance.ledger.inventory()
    stopped_fills = instance.ledger.history("fills")
    assert all(row["reason"] == "stop_loss" for row in stopped_fills[-2:])

    clock.now = BASE + 930
    market.price = {"perp": 100.0, "spot": 102.0}
    forecast(data, qualified=True, identity="forecast-2", decision=BASE+900)
    instance.tick()
    rows = [row for row in instance.ledger.history("decisions") if row["forecast_id"] == "forecast-2"]
    for row in rows:
        detail = json.loads(row["details_json"])
        checks = detail["decision_checks"]
        if row["account"] == "alex":
            assert row["reason"] == "opposite_account_direction"
            continue
        assert row["reason"] == "stop_cooldown" and row["action"] == "hold"
        assert detail["current_notional"] == detail["target_notional"] == 0
        assert checks["proposed_target_notional"] > 0
        assert checks["cooldown_blocks_target"]
        assert runtime_module.stamp(checks["cooldown_until_utc"]) == stopped_at + 3600
        assert checks["cooldown_remaining_seconds"] == stopped_at + 3600-clock()
        assert not checks["rebalance_required"] and not checks["rebalance_forced"]
    before = instance.ledger.history("decisions")
    clock.now += 31
    instance.tick()
    assert instance.ledger.history("decisions") == before
    assert instance.ledger.history("fills") == stopped_fills

    # Even a new fresh signal is blocked immediately before expiry.
    clock.now = stopped_at + 3599
    forecast(data, qualified=True, identity="forecast-3", decision=BASE+3600)
    instance.tick()
    assert instance.ledger.history("fills") == stopped_fills
    assert all(json.loads(row["details_json"])["decision_checks"]["cooldown_remaining_seconds"] == 1
               for row in instance.ledger.history("decisions") if row["forecast_id"] == "forecast-3" and row["account"] != "alex")
    clock.now += 1
    forecast(data, qualified=True, identity="forecast-4", decision=BASE+3600)
    instance.tick()
    new_fills = instance.ledger.history("fills")[len(stopped_fills):]
    assert {row["account"] for row in new_fills} == {"jeremy", "clearpond"}
    assert all(row["reason"] == "signal_rebalance" for row in new_fills)
    for row in instance.ledger.history("decisions"):
        if row["forecast_id"] == "forecast-4":
            checks = json.loads(row["details_json"])["decision_checks"]
            assert not checks["cooldown_blocks_target"]
            assert checks["cooldown_until_utc"] is None and checks["cooldown_remaining_seconds"] == 0


@pytest.mark.parametrize("cause", ["cash", "capacity"])
def test_hold_names_the_account_constraint_that_prevents_entry(qualified_runner, cause):
    instance, _, _, data = qualified_runner
    if cause == "cash":
        # Reserve all spot cash, with no donor able to release cash.
        instance.config = replace(instance.config, reserve_cash_fraction=1)
    else:
        instance.config = replace(instance.config, account_utilization=.001, transfer_min_amount=1e9)
    forecast(data, qualified=True)
    instance.tick()
    account = "clearpond" if cause == "cash" else "jeremy"
    row = next(row for row in instance.ledger.history("decisions") if row["account"] == account)
    checks = json.loads(row["details_json"])["decision_checks"]
    assert row["action"] == "hold"
    assert row["reason"] == ("insufficient_cash" if cause == "cash" else "account_capacity")
    assert checks["cash_limited" if cause == "cash" else "capacity_limited"]
    assert not checks["rebalance_required"]
    assert all(fill["account"] != account for fill in instance.ledger.history("fills"))
    assert instance.ledger.history("transfers") == []


@pytest.mark.parametrize("depth", [0, .000001])
def test_simulation_skip_uses_saved_execution_cause(qualified_runner, depth):
    instance, _, market, data = qualified_runner
    forecast(data, qualified=True)
    market.depth = depth
    instance.tick()
    rows = [row for row in instance.ledger.history("decisions") if row["account"] != "alex"]
    for row in rows:
        detail = json.loads(row["details_json"])
        assert row["action"] == "skip"
        assert row["reason"] == detail["execution"]["reason"]
        if depth:
            assert row["reason"] == "below_min_notional"
        else:
            assert row["reason"]  # Keep the simulator's exact validation message.
        assert detail["decision_checks"]["execution_reason"] == row["reason"]
        assert detail["decision_checks"]["execution_status"] == "unfilled"
    assert instance.ledger.history("fills") == []


def install_opening_risk_mirror(monkeypatch, *, include_long=True):
    calls = []

    def mirror(provider, symbols, *, clock):
        calls.append(tuple(symbols))
        quotes = provider.snapshot(symbols)
        positions = [{"account": "alex", "coin": "BTC", "kind": "perp", "quantity": -10,
                      "average_entry": 80, "risk_reference_price": 100, "risk_reference_source": "opening_mark"}]
        if include_long:
            positions.append({"account": "jeremy", "coin": "BTC", "kind": "perp", "quantity": 10,
                              "average_entry": 120, "risk_reference_price": 100, "risk_reference_source": "opening_mark"})
        return {"initial_cash": {a: 10000 for a in ("alex", "jeremy", "clearpond")},
                "initial_positions": positions, "initial_marks": {key: row["mark"] for key, row in quotes["markets"].items()},
                "now": clock(), "metadata": {"seed_mode": "mirror"}, "quotes": quotes}

    monkeypatch.setattr(runtime_module, "mirror_accounts", mirror)
    return calls


def test_fresh_mirror_opening_is_published_before_qualified_first_cycle_without_historical_stops(tmp_path, monkeypatch):
    config, data = configurations(tmp_path, seed_mode="mirror", symbols=("BTC", "ETH"),
                                  require_qualified_forecasts=True, entry_band=.04)
    clock = Clock()
    market = FakeMarket(clock)
    install_opening_risk_mirror(monkeypatch)
    forecast(data, coin="BTC", p=.43, qualified=True)
    forecast(data, coin="ETH", identity="eth-1", p=.455, qualified=True)
    instance = runtime_module.PaperRuntime(config, market=market, clock=clock)
    instance.initialize()
    try:
        opening = instance.ledger.latest_observation()
        assert opening["state"]["pooled"]["equity"] == 29600
        for name in ("performance.json", "_runtime/status.json"):
            published = json.loads((data / "_paper" / name).read_text())
            assert published["portfolio"]["pooled"]["equity"] == 29600
            assert published["portfolio"]["pooled"]["total_pnl"] == published["portfolio"]["pooled"]["fees"] == 0
            assert len(published["portfolio"]["pooled"]["positions"]) == 2
        assert pd.read_parquet(data / "_paper" / "fills.parquet").empty
        assert len(pd.read_parquet(data / "_paper" / "equity.parquet")) == 4
        state = instance.tick()["portfolio"]
        fills = instance.ledger.history("fills")
        assert {row["coin"] for row in fills} == {"BTC", "ETH"}
        assert all(row["reason"] == "signal_rebalance" for row in fills)
        assert all(json.loads(row["details_json"])["qualified"] is True for row in fills)
        assert instance._stops == {}
        btc = next(p for p in state["pooled"]["positions"] if p["coin"] == "BTC")
        assert btc["account"] == "alex" and btc["quantity"] < 0
        assert btc["avg_entry"] == 80 and btc["risk_reference_price"] == 100
        for row in instance.ledger.history("decisions"):
            if row["coin"] == "BTC" and row["account"] in {"alex", "jeremy"}:
                checks = json.loads(row["details_json"])["decision_checks"]
                assert checks["historical_entry_price"] == {"alex": 80, "jeremy": 120}[row["account"]]
                assert checks["risk_reference_price"] == 100 and checks["risk_reference_source"] == "opening_mark"
                assert checks["stop_return_fraction"] == 0 and checks["cooldown_remaining_seconds"] == 0
        costs = sum(row["quantity"] * (row["price"] - market.price[row["kind"]]) + row["fee"] for row in fills)
        assert costs > 0
        assert state["pooled"]["equity"] == pytest.approx(29600 - costs)
        assert state["pooled"]["total_pnl"] == pytest.approx(-costs)
        assert instance.ledger.history("cycles")[0]["cycle_id"] == "opening"
    finally:
        instance.ledger.close()


@pytest.mark.parametrize("missing", [False, True])
def test_fresh_mirror_missing_or_research_signal_holds_until_new_experiment_stop(tmp_path, monkeypatch, missing):
    config, data = configurations(tmp_path, seed_mode="mirror", require_qualified_forecasts=True)
    clock = Clock()
    market = FakeMarket(clock)
    install_opening_risk_mirror(monkeypatch, include_long=False)
    if not missing:
        forecast(data, qualified=False, p=.1)
    instance = runtime_module.PaperRuntime(config, market=market, clock=clock)
    instance.initialize()
    instance.tick()
    assert instance.ledger.history("fills") == []
    assert instance._stops == {}
    original_seed = instance.ledger.seed()
    instance.ledger.close()
    clock.now += 31
    market.price["perp"] = 103
    reopened = runtime_module.PaperRuntime(config, market=market, clock=clock)
    reopened.initialize()
    try:
        reopened.tick()
        fills = reopened.ledger.history("fills")
        assert len(fills) == 1 and fills[0]["reason"] == "stop_loss"
        assert fills[0]["quantity"] == 10 and fills[0]["forecast_id"] is None
        checks = json.loads(reopened.ledger.history("decisions")[-3]["details_json"])["decision_checks"]
        assert checks["risk_reference_price"] == 100
        assert checks["stop_return_fraction"] == pytest.approx(-.03)
        assert reopened.ledger.seed() == original_seed
        assert reopened._stops[("alex", "BTC")] == clock()
    finally:
        reopened.ledger.close()


def test_prepare_only_preserves_seed_and_stop_request_without_trading_then_resume_uses_fresh_quotes(tmp_path, monkeypatch):
    config, data = configurations(tmp_path, seed_mode="mirror", require_qualified_forecasts=True)
    clock = Clock()
    market = FakeMarket(clock)
    calls = install_opening_risk_mirror(monkeypatch, include_long=False)
    forecast(data, qualified=True, p=.43)
    instance = runtime_module.PaperRuntime(config, market=market, clock=clock)
    marker = instance.control / "stop.request"
    marker.write_text('{"reason":"intentional maintenance"}')
    result = instance.run(prepare_only=True)
    assert result["status"] == "stopped" and result["stop_reason"] == "prepare_only"
    assert result["prepare_only"] and result["lifecycle_phase"] == "opening_prepared"
    assert marker.read_text() == '{"reason":"intentional maintenance"}'
    assert result["portfolio"]["pooled"]["total_pnl"] == result["portfolio"]["pooled"]["fees"] == 0
    assert not runtime_module.read_status(data)["running"]
    assert pd.read_parquet(data / "_paper" / "fills.parquet").empty
    opening = json.loads((data / "_paper" / "opening_snapshot.json").read_text())
    # Only the caller who established maintenance clears its request.
    marker.unlink()
    clock.now += 31
    market.price["perp"] = 101
    resumed = runtime_module.PaperRuntime(config, market=market, clock=clock)
    resumed.run(once=True)
    assert calls == [("BTC",)]
    assert market.snapshot_calls == [("BTC",), ("BTC",)]
    assert json.loads((data / "_paper" / "opening_snapshot.json").read_text()) == opening
    fills = pd.read_parquet(data / "_paper" / "fills.parquet")
    assert not fills.empty and set(fills.reason) == {"signal_rebalance"}
    assert all(fills.price > 101)


def test_prepare_only_cli_is_mutually_exclusive_with_once(tmp_path):
    with pytest.raises(SystemExit) as error:
        runtime_module.main(["--prepare-only", "--once"])
    assert error.value.code == 2


@pytest.mark.parametrize("probability,inside,accounts", [
    (.51, .509999, {"jeremy", "clearpond"}),
    (.49, .490001, {"alex"}),
])
def test_shared_entry_exit_threshold_fills_at_boundary_and_exits_inside(
    tmp_path, probability, inside, accounts,
):
    config, data = configurations(tmp_path, require_qualified_forecasts=True,
                                  entry_band=.01, exit_band=.01)
    clock = Clock()
    market = FakeMarket(clock)
    instance = runtime_module.PaperRuntime(config, market=market, clock=clock)
    instance.initialize()
    try:
        forecast(data, qualified=True, p=probability)
        opened = instance.tick()["portfolio"]
        fills = instance.ledger.history("fills")
        assert {row["account"] for row in fills} == accounts
        assert all(row["notional"] >= instance.config.min_trade_notional for row in fills)
        assert {row["account"] for row in opened["pooled"]["positions"]} == accounts
        for row in instance.ledger.history("decisions"):
            detail = json.loads(row["details_json"])
            assert detail["policy"]["confidence"] == pytest.approx(.01 / .15)
            checks = detail["decision_checks"]
            assert checks["entry_probability_long"] == checks["exit_probability_long"] == .51
            assert checks["entry_probability_short"] == checks["exit_probability_short"] == .49

        # The same inclusive boundary retains an existing position as well as
        # allowing a flat account to enter; there is no hidden hysteresis gap.
        clock.now = BASE + 930
        forecast(data, qualified=True, identity="boundary-held", decision=BASE + 900, p=probability)
        held = instance.tick()["portfolio"]
        assert {row["account"] for row in held["pooled"]["positions"]} == accounts
        assert instance.ledger.history("fills") == fills

        clock.now = BASE + 1830
        forecast(data, qualified=True, identity="inside-threshold", decision=BASE + 1800, p=inside)
        closed = instance.tick()["portfolio"]
        assert closed["pooled"]["positions"] == []
        exits = [row for row in instance.ledger.history("fills")
                 if row["forecast_id"] == "inside-threshold"]
        assert {row["account"] for row in exits} == accounts
        assert all(row["reason"] == "signal_rebalance" for row in exits)
        assert instance._stops == {}
    finally:
        instance.ledger.close()


def test_shared_threshold_policy_resume_preserves_mirror_history_dedupe_and_cooldown(tmp_path, monkeypatch):
    config, data = configurations(tmp_path, seed_mode="mirror", require_qualified_forecasts=True,
                                  entry_band=.04, exit_band=.02)
    clock = Clock()
    market = FakeMarket(clock)
    mirror_calls = install_opening_risk_mirror(monkeypatch)
    first = runtime_module.PaperRuntime(config, market=market, clock=clock)
    first.initialize()
    try:
        seed = first.ledger.seed()
        forecast(data, qualified=True, p=.65)
        first.tick()
        clock.now += 31
        market.price["perp"] = 94
        first.tick()
        assert first._stops == {("jeremy", "BTC"): clock()}
        inventory = first.ledger.inventory()
        assert len(inventory) == 1 and inventory[0]["account"] == "clearpond"
        histories = {table: first.ledger.history(table) for table in (
            "initial_positions", "cycles", "fills", "decisions", "equity", "transfers", "funding", "events",
        )}
        old_policy_id = first.policy_id
        old_policy_path = data / "_paper" / "policies" / f"{old_policy_id}.json"
        old_policy_bytes = old_policy_path.read_bytes()
        stops = dict(first._stops)
    finally:
        first.ledger.close()

    values = json.loads(config.read_text())
    values.update(entry_band=.01, exit_band=.01)
    config.write_text(json.dumps(values))
    market.price["perp"] = 100
    clock.now += 31
    resumed = runtime_module.PaperRuntime(config, market=market, clock=clock)
    resumed.initialize()
    try:
        assert mirror_calls == [("BTC",)]
        assert resumed.ledger.seed() == seed
        assert resumed.ledger.inventory() == inventory
        assert resumed._stops == stops
        assert resumed.policy_id != old_policy_id
        assert old_policy_path.read_bytes() == old_policy_bytes
        assert (data / "_paper" / "policies" / f"{resumed.policy_id}.json").is_file()
        for table, rows in histories.items():
            assert resumed.ledger.history(table) == rows

        # A changed policy must not replay an already consumed forecast.
        resumed.tick()
        assert resumed.ledger.inventory() == inventory
        assert resumed.ledger.history("fills") == histories["fills"]
        assert resumed.ledger.history("decisions") == histories["decisions"]

        clock.now = BASE + 930
        forecast(data, qualified=True, identity="shared-threshold-new", decision=BASE + 900, p=.51)
        resumed.tick()
        fresh = [row for row in resumed.ledger.history("decisions")
                 if row["forecast_id"] == "shared-threshold-new"]
        assert {row["account"] for row in fresh} == {"alex", "jeremy", "clearpond"}
        for row in fresh:
            detail = json.loads(row["details_json"])
            assert detail["policy_id"] == resumed.policy_id
            checks = detail["decision_checks"]
            assert checks["entry_probability_long"] == checks["exit_probability_long"] == .51
            assert checks["entry_probability_short"] == checks["exit_probability_short"] == .49
            if row["account"] == "jeremy":
                assert row["action"] == "hold" and row["reason"] == "stop_cooldown"
                assert checks["cooldown_blocks_target"] and checks["cooldown_remaining_seconds"] > 0
        remaining = resumed.ledger.inventory()
        assert len(remaining) == 1 and remaining[0]["account"] == "clearpond"
        assert 0 < remaining[0]["quantity"] < inventory[0]["quantity"]
        for name in ("avg_entry", "risk_reference_price", "risk_reference_source"):
            assert remaining[0][name] == inventory[0][name]
        assert resumed.ledger.seed() == seed
        assert resumed._stops == stops
        for table, rows in histories.items():
            assert resumed.ledger.history(table)[:len(rows)] == rows
        assert old_policy_path.read_bytes() == old_policy_bytes
    finally:
        resumed.ledger.close()
