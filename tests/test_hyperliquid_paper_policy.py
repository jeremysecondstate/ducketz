from dataclasses import replace
import json
from pathlib import Path

import pytest

from ml.hyperliquid_paper_policy import (
    DEFAULT_MODEL_CONFIG_PATH, DEFAULT_PAPER_CONFIG_PATH, PaperConfig,
    load_config, should_rebalance, target_notionals,
)


def write_config(tmp_path, **values):
    path = tmp_path / "paper.json"
    path.write_text(json.dumps({"version": 1, **values}), encoding="utf-8")
    return path


def plan(probability=0.65, sigma=0.01, equity=30000.0, current=None, other_gross=0.0, config=None):
    return target_notionals(probability, sigma, equity, current or {}, other_gross, config or PaperConfig())


def test_checked_in_policy_is_paper_mirror_and_qualified_only_without_account_reads():
    config = load_config(DEFAULT_PAPER_CONFIG_PATH)
    assert config.mode == "paper"
    assert config.seed_mode == "mirror"
    assert config.model_config == DEFAULT_MODEL_CONFIG_PATH
    assert config.require_qualified_forecasts is True
    assert config.initial_cash == {"alex": 10000, "jeremy": 10000, "clearpond": 10000}
    assert config.paper_root == config.data_root / "_paper"
    assert config == PaperConfig(require_qualified_forecasts=True)


def test_minimal_config_does_not_read_a_missing_model_file_and_resolves_relative_paths(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    path = write_config(tmp_path, model_config="models.json", data_root="data")
    monkeypatch.chdir(elsewhere)
    config = load_config(path)
    assert config.model_config == tmp_path / "models.json"
    assert config.data_root == tmp_path / "data"


@pytest.mark.parametrize("mode", ["live", "LIVE", "testnet", "paper_live", None, True])
def test_live_mode_is_explicitly_unavailable(tmp_path, mode):
    with pytest.raises(ValueError, match="paper.*live"):
        load_config(write_config(tmp_path, mode=mode))


@pytest.mark.parametrize("seed_mode", ["random", "fallback", "", None, True, []])
def test_unknown_seed_mode_cannot_silently_choose_manual_cash(seed_mode):
    with pytest.raises(ValueError, match="seed_mode"):
        PaperConfig(seed_mode=seed_mode)


def test_manual_seed_requires_explicit_config_and_copies_input_cash():
    cash = {"alex": 0, "jeremy": 3000, "clearpond": 7000}
    config = PaperConfig(seed_mode="manual", initial_cash=cash)
    cash["alex"] = 999
    assert config.initial_cash["alex"] == 0
    assert config.seed_mode == "manual"


@pytest.mark.parametrize("cash", [
    {}, {"alex": 100}, {"alex": 1, "jeremy": 1, "clearpond": 1, "other": 1},
    {"alex": 0, "jeremy": 0, "clearpond": 0},
    {"alex": -1, "jeremy": 100, "clearpond": 100},
    {"alex": float("nan"), "jeremy": 100, "clearpond": 100},
    {"alex": True, "jeremy": 100, "clearpond": 100},
])
def test_invalid_seed_cash_is_rejected(cash):
    with pytest.raises(ValueError):
        PaperConfig(initial_cash=cash)


@pytest.mark.parametrize("text", [
    '{"version":1,"mode":"paper","mode":"live"}',
    '{"version":1,"poll_seconds":NaN}',
    '{"version":1,"poll_seconds":Infinity}',
    '{"version":true}', '{"version":2}', '{"version":1,"orders_enabled":true}', '[]',
])
def test_json_contract_rejects_duplicates_nonfinite_unknown_keys_and_versions(tmp_path, text):
    path = tmp_path / "paper.json"
    path.write_text(text)
    with pytest.raises(ValueError):
        load_config(path)


@pytest.mark.parametrize("field", [
    "poll_seconds", "volatility_budget_fraction", "sigma_floor", "min_trade_notional",
    "max_forecast_age_seconds", "max_model_age_seconds", "max_quote_age_seconds", "transfer_min_amount",
])
@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, "1"])
def test_config_requires_finite_positive_settings(field, value):
    with pytest.raises(ValueError):
        PaperConfig(**{field: value})


@pytest.mark.parametrize("changes", [
    {"entry_band": 0.02}, {"exit_band": 0.05}, {"exit_band": -0.01},
    {"saturation_band": 0.05}, {"saturation_band": 0.51}, {"entry_band": float("nan")},
    {"bullish_spot_fraction": 1.1}, {"pool_gross_fraction": -0.1},
    {"per_symbol_gross_fraction": 1.1}, {"account_utilization": 1.1},
    {"account_utilization": 0}, {"reserve_cash_fraction": -0.1},
    {"rebalance_min_delta_fraction": 1.1}, {"volatility_budget_fraction": 1.1},
    {"stop_loss_fraction": 0}, {"stop_loss_fraction": 1},
    {"perp_fee_rate": -1}, {"spot_fee_rate": 1}, {"slippage_bps": 10000},
    {"require_qualified_forecasts": "false"}, {"data_root": ""}, {"model_config": None},
])
def test_config_rejects_invalid_bands_caps_and_costs(changes):
    with pytest.raises(ValueError):
        PaperConfig(**changes)


def test_long_budget_is_split_once_between_spot_and_long_perps():
    result = plan()
    assert result["targets"] == pytest.approx({"alex": 0, "jeremy": 1200, "clearpond": 1800})
    assert result["details"]["target_gross"] == pytest.approx(3000)
    assert sum(abs(value) for value in result["targets"].values()) == pytest.approx(3000)
    assert result["details"]["account_cash_constraints_applied"] is False


def test_short_budget_only_routes_to_alex():
    result = plan(probability=0.35)
    assert result["targets"] == pytest.approx({"alex": -3000, "jeremy": 0, "clearpond": 0})
    assert result["details"]["direction"] == "short"


def test_hysteresis_holds_existing_direction_inside_flat_entry_deadband():
    flat = plan(probability=0.53)
    held = plan(probability=0.53, current={"jeremy": 500})
    assert sum(flat["targets"].values()) == 0
    assert held["details"]["reason"] == "hold_with_hysteresis"
    assert held["details"]["confidence"] > 0
    assert held["targets"]["jeremy"] > 0
    assert plan(probability=0.52, current={"jeremy": 500})["details"]["direction"] == "flat"
    assert plan(probability=0.48, current={"alex": -500})["details"]["direction"] == "flat"


def test_exact_entry_threshold_is_symmetric_and_neutral_flattens():
    assert plan(probability=0.55)["targets"]["jeremy"] > 0
    assert plan(probability=0.45)["targets"]["alex"] < 0
    neutral = plan(probability=0.5, current={"alex": -100, "jeremy": 200, "clearpond": 300})
    assert neutral["targets"] == {"alex": 0, "jeremy": 0, "clearpond": 0}
    assert neutral["details"]["existing_opposing_gross_to_reduce"] == 600


def test_hedged_inherited_positions_use_matching_exposure_not_zero_net():
    result = plan(probability=0.53, current={"alex": -1000, "jeremy": 1000})
    assert result["details"]["current_coin_net"] == 0
    assert result["details"]["current_coin_gross"] == 2000
    assert result["details"]["reason"] == "hold_with_hysteresis"
    assert result["targets"]["jeremy"] > 0
    assert result["targets"]["alex"] == 0
    assert result["details"]["existing_opposing_gross_to_reduce"] == 1000


def test_opposite_signal_exits_old_direction_before_reallocation():
    old_long = {"jeremy": 1200, "clearpond": 1800}
    within_gap = plan(probability=0.47, current=old_long)
    assert within_gap["targets"] == {"alex": 0, "jeremy": 0, "clearpond": 0}
    flipped = plan(probability=0.35, current=old_long)
    assert flipped["targets"]["alex"] < 0
    assert flipped["targets"]["jeremy"] == flipped["targets"]["clearpond"] == 0


def test_inverse_volatility_target_has_floor_and_per_symbol_cap():
    baseline = plan(sigma=0.02)["details"]["target_gross"]
    doubled_vol = plan(sigma=0.04)["details"]["target_gross"]
    assert doubled_vol == pytest.approx(baseline / 2)
    zero_vol = plan(sigma=0)
    low_vol = plan(sigma=0.001)
    assert zero_vol["targets"] == low_vol["targets"]
    assert zero_vol["details"]["effective_horizon_sigma"] == 0.005
    assert zero_vol["details"]["target_gross"] == 4500
    assert "per_symbol_gross" in zero_vol["details"]["binding_caps"]


def test_pool_room_counts_gross_without_offsetting_other_positions():
    result = plan(other_gross=17000)
    assert result["details"]["pool_gross_cap"] == 18000
    assert result["details"]["target_gross"] == 1000
    assert sum(abs(value) for value in result["targets"].values()) + 17000 <= 18000
    assert plan(other_gross=18000)["details"]["target_gross"] == 0
    assert plan(other_gross=20000)["details"]["reason"] == "gross_capacity_exhausted"


@pytest.mark.parametrize("equity", [0, -100])
def test_nonpositive_equity_produces_exit_targets(equity):
    result = plan(equity=equity, current={"alex": -100})
    assert result["targets"] == {"alex": 0, "jeremy": 0, "clearpond": 0}
    assert result["details"]["reason"] == "nonpositive_pool_equity"


@pytest.mark.parametrize("changes", [
    {"probability": float("nan")}, {"probability": float("inf")}, {"probability": -0.1},
    {"probability": 1.1}, {"probability": True}, {"sigma": -0.1}, {"sigma": float("nan")},
    {"equity": float("inf")}, {"other_gross": -1},
    {"current": {"alex": 10}}, {"current": {"jeremy": -10}},
    {"current": {"clearpond": -10}}, {"current": {"other": 10}},
    {"current": {"alex": float("nan")}},
])
def test_invalid_inputs_never_create_targets(changes):
    with pytest.raises(ValueError):
        plan(**changes)


def test_rebalancing_ignores_small_changes_but_does_not_block_exits_or_risk_reductions():
    config = PaperConfig()
    assert not should_rebalance(1000, 1050, config)
    assert should_rebalance(1000, 1200, config)
    assert not should_rebalance(0, 24.99, config)
    assert should_rebalance(0, 25, config)
    assert should_rebalance(1, 0, config)
    assert should_rebalance(-1, 0, config)
    assert not should_rebalance(100, 95, config)
    assert should_rebalance(100, 95, config, force_reduce=True)
    assert should_rebalance(-100, -95, config, force_reduce=True)
    assert not should_rebalance(100, 105, config, force_reduce=True)
    assert not should_rebalance(100, 100, config, force_reduce=True)


def test_probability_symmetry_determinism_and_probability_saturation():
    for p in (0.55, 0.58, 0.6, 0.65, 1.0):
        bullish, bearish = plan(probability=p), plan(probability=1-p)
        assert bullish["details"]["target_gross"] == pytest.approx(bearish["details"]["target_gross"])
        assert bullish == plan(probability=p)
    assert plan(probability=0.65)["targets"] == plan(probability=1.0)["targets"]


def test_zero_gross_budget_and_alternative_bullish_splits_are_supported():
    assert plan(config=replace(PaperConfig(), pool_gross_fraction=0))["details"]["target_gross"] == 0
    all_spot = plan(config=replace(PaperConfig(), bullish_spot_fraction=1))["targets"]
    assert all_spot["jeremy"] == 0 and all_spot["clearpond"] > 0
    all_perp = plan(config=replace(PaperConfig(), bullish_spot_fraction=0))["targets"]
    assert all_perp["clearpond"] == 0 and all_perp["jeremy"] > 0
