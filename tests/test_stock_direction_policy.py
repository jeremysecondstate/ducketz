import pytest

from ml.stock_direction_policy import stock_action, stock_direction, saved_stock_direction


@pytest.mark.parametrize("probability,label", [
    (0.0, "BEARISH"), (.46, "BEARISH"), (.460001, "BEARISH"),
    (.4999, "BEARISH"), (.499900001, "NO_EDGE"), (.499999, "NO_EDGE"),
    (.5, "NO_EDGE"), (.500001, "NO_EDGE"), (.500099999, "NO_EDGE"),
    (.5001, "BULLISH"),
    (.5287, "BULLISH"), (.539999, "BULLISH"), (.54, "BULLISH"), (1.0, "BULLISH"),
])
def test_user_selected_stock_direction_boundaries(probability, label):
    assert stock_direction(probability) == label
    assert stock_action(probability) == {"BEARISH": "SELL", "BULLISH": "BUY", "NO_EDGE": "HOLD"}[label]


@pytest.mark.parametrize("probability", [float("nan"), float("inf"), -.01, 1.01])
def test_direction_requires_an_actual_probability(probability):
    with pytest.raises(ValueError):
        stock_direction(probability)


@pytest.mark.parametrize("probability,label", [(.499999, "BEARISH"), (.500001, "BULLISH")])
def test_saved_prior_labels_remain_valid_but_current_execution_holds_inside_exact_cutoffs(probability, label):
    assert saved_stock_direction(probability, "stock-direction-50-v2") == label
    assert stock_action(probability) == "HOLD"
