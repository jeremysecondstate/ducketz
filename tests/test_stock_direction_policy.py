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
