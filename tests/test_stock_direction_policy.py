import pytest

from ml.stock_direction_policy import stock_direction, stock_action, saved_stock_direction


@pytest.mark.parametrize("probability,label", [
    (0.0, "BEARISH"), (.46, "BEARISH"), (.460001, "BEARISH"),
    (.4999, "BEARISH"), (.49990000000000007, "NO_EDGE"),
    (.499999, "NO_EDGE"), (.5, "NO_EDGE"), (.500001, "NO_EDGE"),
    (.5000999999999999, "NO_EDGE"), (.5001, "BULLISH"),
    ("0.50009999999999999999999999999", "NO_EDGE"),
    ("0.49990000000000000000000000001", "NO_EDGE"),
    (.5287, "BULLISH"), (.539999, "BULLISH"), (.54, "BULLISH"), (1.0, "BULLISH"),
])
def test_user_selected_stock_direction_boundaries(probability, label):
    assert stock_direction(probability) == label
    assert stock_action(probability) == {"BULLISH": "BUY", "BEARISH": "SELL", "NO_EDGE": "HOLD"}[label]


@pytest.mark.parametrize("probability,label", [(.499999, "BEARISH"), (.5, "NO_EDGE"), (.500001, "BULLISH")])
def test_documented_historical_strict_50_policy_is_not_rewritten(probability, label):
    assert saved_stock_direction(probability, "stock-direction-50-v2") == label


@pytest.mark.parametrize("probability", [float("nan"), float("inf"), -.01, 1.01])
def test_direction_requires_an_actual_probability(probability):
    with pytest.raises(ValueError):
        stock_direction(probability)


def test_main_policy_apis_and_recorded_identifiers_survive_scout_port():
    from ml.stock_direction_policy import (
        STOCK_DIRECTION_POLICY_VERSION, autonomous_horizon_sales,
        saved_direction_policy_summary, stock_direction_for_policy,
    )
    assert STOCK_DIRECTION_POLICY_VERSION == "stock-direction-5001-4999-v3"
    for version in ("stock-direction-50-01-v3", "stock-direction-5001-4999-v3"):
        assert stock_direction_for_policy("0.5000999999999999999999", version) == "NO_EDGE"
        assert stock_direction_for_policy("0.4999000000000000000001", version) == "NO_EDGE"
        assert stock_direction_for_policy("0.5001", version) == "BULLISH"
        assert stock_direction_for_policy("0.4999", version) == "BEARISH"
    assert stock_direction_for_policy("0.5000000000000000000001", "stock-direction-50-v2") == "BULLISH"
    assert stock_direction_for_policy("0.4999999999999999999999", None) == "BEARISH"
    with pytest.raises(ValueError, match="Unsupported saved"):
        stock_direction_for_policy(.6, "unrecorded-name")
    rows = [{"direction_policy_version": "stock-direction-50-01-v3"},
            {"direction_policy_version": "stock-direction-5001-4999-v3"},
            {"direction_policy_version": "stock-direction-50-v2"}, {},
            {"direction_policy_version": float("nan")}]
    result = saved_direction_policy_summary(rows)
    assert result["unrecorded_direction_policy_forecasts"] == 2
    assert result["direction_policy_versions"] == ["stock-direction-50-01-v3", "stock-direction-50-v2", "stock-direction-5001-4999-v3"]
    assert result["direction_policy_cutoffs"]["stock-direction-50-v2"]["boundaries"] == "exclusive"
    assert result["direction_policy_cutoffs"]["stock-direction-50-01-v3"] == result["direction_policy_cutoffs"]["stock-direction-5001-4999-v3"]
    assert "direction_policy_version" not in rows[3]
    assert autonomous_horizon_sales("1h", "2026-10-19")
    assert autonomous_horizon_sales("4h", "2026-10-19")
    assert not autonomous_horizon_sales("1h", "2026-10-18")
    assert not autonomous_horizon_sales("1d", "2026-10-19")
