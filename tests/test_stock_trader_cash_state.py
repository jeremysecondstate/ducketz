from copy import deepcopy
from datetime import datetime

import pytest

from ml.stock_trader.state import capture_portfolio_state


class CashBroker:
    def __init__(self, balances=None, orders=None):
        self.balances = balances or {
            "liquidationValue": 10000, "cashBalance": 1000, "settledCash": 900,
            "cashAvailableForTrading": 5000, "availableFundsNonMarginableTrade": 6000,
            "buyingPowerNonMarginableTrade": 7000,
        }
        self.orders = orders or []
        self.calls = []

    def get_account(self):
        self.calls.append("account")
        return {"securitiesAccount": {"currentBalances": deepcopy(self.balances), "positions": []}}

    def get_open_orders(self):
        self.calls.append("orders")
        return deepcopy(self.orders)

    def get_equity_quotes(self, symbols):
        self.calls.append("quotes")
        return {symbol: {"bid": 10, "ask": 10.01} for symbol in symbols}


def capture(broker, *, literal_cash_only=True, parallel=False):
    return capture_portfolio_state(broker, observed_at="2026-09-09T11:00:00Z",
                                   literal_cash_only=literal_cash_only, parallel=parallel)


@pytest.mark.parametrize("parallel", [False, True])
def test_literal_cash_is_not_replaced_by_larger_nonmarginable_capacity(parallel):
    broker = CashBroker()
    result = capture(broker, parallel=parallel)
    assert result.available_cash == 900
    assert sorted(broker.calls) == ["account", "orders", "quotes"]
    assert capture(CashBroker(), literal_cash_only=False).available_cash == 5000


@pytest.mark.parametrize("field", ["cashBalance", "settledCash", "cashAvailableForTrading",
                                  "availableFundsNonMarginableTrade", "buyingPowerNonMarginableTrade"])
def test_each_reported_cash_or_capacity_balance_can_bound_literal_cash(field):
    broker = CashBroker()
    broker.balances[field] = 125.129
    assert capture(broker).available_cash == 125.12


def test_working_buy_cash_is_subtracted_once_without_credit_for_a_pending_sell():
    def order(side, symbol, quantity, price):
        return {
            "orderId": side, "status": "WORKING", "orderType": "LIMIT", "orderStrategyType": "SINGLE",
            "complexOrderStrategyType": "NONE",
            "quantity": quantity, "filledQuantity": 0, "remainingQuantity": quantity, "price": price,
            "orderLegCollection": [{"instruction": side, "quantity": quantity,
                                    "instrument": {"assetType": "EQUITY", "symbol": symbol}}],
        }
    broker = CashBroker(orders=[order("BUY", "AAPL", 2, 10), order("SELL", "AMZN", 1, 500)])
    result = capture(broker)
    assert result.available_cash == 880
    assert result.pending_buy_shares["AAPL"] == 2
    assert result.pending_sell_shares["AMZN"] == 1


@pytest.mark.parametrize("field", ["cashBalance", "settledCash", "cashAvailableForTrading"])
@pytest.mark.parametrize("bad", [None, float("nan"), True, "invalid"])
def test_explicit_invalid_cash_is_not_silently_replaced_with_capacity(field, bad):
    broker = CashBroker()
    broker.balances[field] = bad
    with pytest.raises(ValueError, match="invalid literal cash"):
        capture(broker)


def test_missing_literal_balances_do_not_treat_nonmarginable_capacity_as_cash():
    broker = CashBroker()
    del broker.balances["cashBalance"], broker.balances["settledCash"]
    with pytest.raises(ValueError, match="literal cash balance is unavailable"):
        capture(broker)
    assert capture(broker, literal_cash_only=False).available_cash == 5000


def test_negative_cash_balance_allows_no_borrowed_cash():
    broker = CashBroker()
    broker.balances["cashBalance"] = -100
    assert capture(broker).available_cash == 0


def test_incomplete_account_wide_reserve_cannot_be_assumed_zero(monkeypatch):
    import ml.stock_trader.state as state
    original = state.normalize_schwab_policy_inputs

    def incomplete(*args, **kwargs):
        normalized = original(*args, **kwargs)
        normalized["working_orders"].update(status="INCOMPLETE", reserved_cash=None)
        return normalized

    monkeypatch.setattr(state, "normalize_schwab_policy_inputs", incomplete)
    with pytest.raises(ValueError, match="account-wide pending cash reservations"):
        capture(CashBroker())
    assert capture(CashBroker(), literal_cash_only=False).available_cash == 5000


@pytest.mark.parametrize("field", ["quoteTime", "quoteTimeInLong"])
@pytest.mark.parametrize("stamp", ["2026-09-09T10:59:59+00:00", "2026-09-08T23:59:00+00:00"])
def test_actual_bbo_timestamp_is_preserved_even_when_quote_is_stale(field, stamp):
    broker = CashBroker()
    broker.get_equity_quotes = lambda symbols: {s: {"bid": 10, "ask": 10.01,
        field: int(datetime.fromisoformat(stamp).timestamp() * 1000)} for s in symbols}
    result = capture_portfolio_state(broker, observed_at="2026-09-09T11:00:00Z", parallel=False,
                                     use_actual_quote_timestamps=True)
    assert result.quotes["AAPL"].observed_at == stamp
    assert result.observed_at == "2026-09-09T11:00:00+00:00"


@pytest.mark.parametrize("timestamp", [None, True, float("nan"), "invalid"])
def test_missing_or_invalid_bbo_timestamp_never_uses_capture_or_last_trade_time(timestamp):
    broker = CashBroker()
    broker.get_equity_quotes = lambda symbols: {s: {"bid": 10, "ask": 10.01,
        "quoteTime": timestamp, "tradeTime": "2026-09-09T10:59:59Z"} for s in symbols}
    result = capture_portfolio_state(broker, observed_at="2026-09-09T11:00:00Z", parallel=False,
                                     use_actual_quote_timestamps=True)
    assert result.quotes == {}
    legacy = capture_portfolio_state(broker, observed_at="2026-09-09T11:00:00Z", parallel=False)
    assert legacy.quotes["AAPL"].observed_at == "2026-09-09T11:00:00+00:00"


def test_actual_quote_timestamp_participates_in_snapshot_fingerprint():
    broker = CashBroker()
    def observed(stamp):
        broker.get_equity_quotes = lambda symbols: {s: {"bid": 10, "ask": 10.01, "quoteTime": stamp} for s in symbols}
        return capture_portfolio_state(broker, observed_at="2026-09-09T11:00:00Z", parallel=False,
                                       use_actual_quote_timestamps=True)
    assert observed("2026-09-09T10:59:59Z").source_fingerprint != observed("2026-09-08T23:59:00Z").source_fingerprint
