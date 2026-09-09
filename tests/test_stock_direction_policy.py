import pytest

from ml.stock_direction_policy import stock_direction


@pytest.mark.parametrize("probability,label", [
    (0.0, "BEARISH"), (.46, "BEARISH"), (.460001, "NO_EDGE"),
    (.5287, "NO_EDGE"), (.539999, "NO_EDGE"), (.54, "BULLISH"), (1.0, "BULLISH"),
])
def test_user_selected_stock_direction_boundaries(probability, label):
    assert stock_direction(probability) == label


@pytest.mark.parametrize("probability", [float("nan"), float("inf"), -.01, 1.01])
def test_direction_requires_an_actual_probability(probability):
    with pytest.raises(ValueError):
        stock_direction(probability)
