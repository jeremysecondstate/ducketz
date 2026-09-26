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


def test_fresh_forecast_fills_from_actual_spot_and_perp_books_and_preserves_roles(runner):
    instance, clock, market, data = runner
    forecast(data, p=0.65, qualified=False, price=77)
    result = instance.tick()
    fills = instance.ledger.history("fills")
    assert {row["account"] for row in fills} == {"jeremy", "clearpond"}
    assert all(row["quantity"] > 0 for row in fills)
    for fill in fills:
        expected = (market.price[fill["kind"]] + 0.1) * 1.0002
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
    stop = [json.loads(row["details_json"]) for row in instance.ledger.history("decisions") if row["reason"] == "stop_loss"]
    assert stop and stop[0]["action"] == "skip"
    assert stop[0]["execution"]["reason"] == "below_min_notional"


def test_research_exclusion_can_be_enabled_explicitly(runner):
    instance, clock, market, data = runner
    instance.config = replace(instance.config, require_qualified_forecasts=True)
    forecast(data, qualified=False)
    instance.tick()
    assert instance.ledger.history("fills") == []


@pytest.mark.parametrize("offset", [-26, -60, 60])
def test_book_before_forecast_stale_or_future_cannot_create_fills(runner, offset):
    instance, clock, market, data = runner
    forecast(data)
    market.book_offset = offset
    instance.tick()
    assert instance.ledger.history("fills") == []


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
