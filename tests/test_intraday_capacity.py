import pytest
from ml.intraday.capacity import clip_to_capacity
from ml.intraday.prediction import ContractError


def decide(**changes):
    values = dict(symbol="AAPL", side="BUY", desired_quantity=4, limit_price="50",
                  available_cash="100", available_shares=3, capacity_evidence_id="snapshot",
                  observed_at="2026-10-12T17:15:00Z", calculated_at="2026-10-12T17:15:01Z")
    return clip_to_capacity(**(values | changes))


def test_take_smaller_possible_trade_and_preserve_original():
    result = decide()
    assert (result.desired_quantity, result.adjusted_quantity) == (4, 2)
    assert result.disposition == "smaller_trade_possible"
    assert decide(desired_quantity=2, available_cash="50").adjusted_quantity == 1


def test_full_trade_has_no_buffer_minimum_or_extra_scaling():
    assert decide(available_cash="200").adjusted_quantity == 4
    assert decide(available_cash="10000").adjusted_quantity == 4
    assert decide(desired_quantity=1, limit_price="0.25", available_cash="0.25").adjusted_quantity == 1


def test_sell_uses_account_shares_without_horizon_gate():
    result = decide(side="SELL", available_cash="0")
    assert (result.desired_quantity, result.adjusted_quantity) == (4, 3)
    assert decide(side="SELL", available_shares=10).adjusted_quantity == 4


@pytest.mark.parametrize("side,changes,reason", [
    ("BUY", {"available_cash":"49.99"}, "no_purchasing_capacity"),
    ("SELL", {"available_shares":0}, "no_eligible_uncommitted_shares"),
    ("BUY", {"desired_quantity":0}, "sizing_produced_zero")])
def test_zero_is_distinct_and_never_increased(side, changes, reason):
    result = decide(side=side, **changes)
    assert result.adjusted_quantity == 0 and result.disposition == reason


def test_decimal_whole_share_capacity_has_no_binary_roundoff():
    assert decide(limit_price="0.10", available_cash="0.30").adjusted_quantity == 3


@pytest.mark.parametrize("changes", [{"desired_quantity":True}, {"desired_quantity":-1},
    {"available_shares":1.5}, {"available_cash":"NaN"}, {"limit_price":"0"},
    {"side":"SELL_SHORT"}, {"observed_at":"2026-10-12T18:00:00Z"}])
def test_invalid_or_future_capacity_is_not_zero(changes):
    with pytest.raises(ContractError):
        decide(**changes)
