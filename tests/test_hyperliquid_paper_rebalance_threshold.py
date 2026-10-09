from dataclasses import replace

import pytest

from ml.hyperliquid_paper_policy import load_config, should_rebalance


def test_exploratory_rebalance_threshold_preserves_paper_guards():
    config = load_config()

    assert config.require_qualified_forecasts is False
    assert config.entry_band == 0
    assert config.exit_band == 0
    assert config.min_trade_notional == 10
    assert config.rebalance_min_delta_fraction == pytest.approx(0.2)
    candidate = replace(config, rebalance_min_delta_fraction=0.35)
    assert config.perp_fee_rate == pytest.approx(0.00045)
    assert config.spot_fee_rate == pytest.approx(0.0007)
    assert config.stop_loss_fraction == pytest.approx(0.03)
    assert config.per_symbol_gross_fraction == pytest.approx(0.15)
    assert config.pool_gross_fraction == pytest.approx(0.6)
    assert config.account_utilization == pytest.approx(0.8)

    assert should_rebalance(130, 200, config)
    assert should_rebalance(131, 200, config)
    assert not should_rebalance(200, 200, config)
    assert should_rebalance(200, 0, config)
    assert should_rebalance(100, 95, config, force_reduce=True)
    assert not should_rebalance(0, 9.99, config)
    assert should_rebalance(0, 10, config)
    assert should_rebalance(130, 200, candidate)
    assert not should_rebalance(131, 200, candidate)
