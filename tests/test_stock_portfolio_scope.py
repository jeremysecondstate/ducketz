from __future__ import annotations

from copy import deepcopy

import pytest

from ml.stock_trader.contracts import STOCK_TRADER_SYMBOLS
from ml.stock_trader.state import capture_portfolio_state


NOW = "2026-10-13T16:30:00+00:00"
FOREIGN_SYMBOL = "ZZTEST"


def working_order(identity="buy-1", *, symbol=FOREIGN_SYMBOL, instruction="BUY"):
    return {
        "orderId": identity,
        "status": "WORKING",
        "orderType": "LIMIT",
        "orderStrategyType": "SINGLE",
        "complexOrderStrategyType": "NONE",
        "quantity": 4.0,
        "filledQuantity": 1.0,
        "remainingQuantity": 3.0,
        "price": 100.0,
        "childOrderStrategies": [],
        "orderLegCollection": [{
            "instruction": instruction,
            "quantity": 4.0,
            "instrument": {"symbol": symbol, "assetType": "EQUITY"},
        }],
    }


class FakeBroker:
    """Raw broker responses, without network access or order submission methods."""

    def __init__(self, orders=()):
        self.calls = []
        self.orders = list(orders)

    def get_account(self):
        self.calls.append("account")
        return {"securitiesAccount": {
            "currentBalances": {
                "liquidationValue": 100_000.0,
                "cashAvailableForTrading": 25_000.0,
                "availableFundsNonMarginableTrade": 24_000.0,
                "buyingPowerNonMarginableTrade": 26_000.0,
                "settledCash": 23_000.0,
                "cashBalance": 24_500.0,
            },
            "positions": [{
                "instrument": {"symbol": FOREIGN_SYMBOL, "assetType": "EQUITY"},
                "longQuantity": 10.0,
                "shortQuantity": 0.0,
                "quantity": 10.0,
                "settledLongQuantity": 10.0,
                "settledShortQuantity": 0.0,
                "settledQuantity": 10.0,
                "marketPrice": 100.0,
                "marketValue": 1_000.0,
                "costBasis": 900.0,
                "averagePrice": 90.0,
                "longOpenProfitLoss": 100.0,
                "currentDayProfitLoss": 10.0,
            }],
        }}

    def get_open_orders(self):
        self.calls.append("orders")
        return deepcopy(self.orders)

    def get_equity_quotes(self, symbols):
        self.calls.append(("quotes", symbols))
        return {symbol: {"bidPrice": 99.0, "askPrice": 101.0,
                         "lastPrice": 100.0, "totalVolume": 1_000.0}
                for symbol in symbols}


def capture(broker, *, parallel=False, **kwargs):
    return capture_portfolio_state(broker, observed_at=NOW, parallel=parallel, **kwargs)


@pytest.mark.parametrize("parallel", [False, True])
def test_explicit_universe_captures_foreign_symbol_and_preserves_account_reserves(parallel):
    assert FOREIGN_SYMBOL not in STOCK_TRADER_SYMBOLS
    broker = FakeBroker([
        working_order(),
        working_order("other-buy", symbol="OTHER"),
        working_order("sell-1", instruction="SELL"),
    ])
    portfolio = capture(broker, symbols=[FOREIGN_SYMBOL], literal_cash_only=True, parallel=parallel)

    assert len(broker.calls) == 3
    assert all(broker.calls.count(call) == 1 for call in
               ["account", "orders", ("quotes", (FOREIGN_SYMBOL,))])
    assert portfolio.held_shares == {FOREIGN_SYMBOL: 10.0}
    assert portfolio.symbol_exposure == {FOREIGN_SYMBOL: 1_000.0}
    assert set(portfolio.quotes) == {FOREIGN_SYMBOL}
    assert portfolio.pending_buy_shares == {FOREIGN_SYMBOL: 3.0}
    assert portfolio.pending_sell_shares == {FOREIGN_SYMBOL: 3.0}
    # Both buys reserve cash, including the order outside the requested universe.
    assert portfolio.available_cash == 22_400.0
    assert portfolio.working_order_count == 3
    assert portfolio.broker_working_orders is None


def test_default_universe_and_legacy_cash_capacity_are_preserved():
    broker = FakeBroker([working_order()])
    portfolio = capture(broker)

    assert broker.calls[-1] == ("quotes", STOCK_TRADER_SYMBOLS)
    assert set(portfolio.held_shares) == set(STOCK_TRADER_SYMBOLS)
    assert set(portfolio.quotes) == set(STOCK_TRADER_SYMBOLS)
    assert FOREIGN_SYMBOL not in portfolio.held_shares
    assert portfolio.gross_exposure == 1_000.0
    assert portfolio.available_cash == 23_700.0
    assert portfolio.broker_working_orders is None


@pytest.mark.parametrize("symbols", [
    [], (), "AAPL", b"AAPL", {"AAPL"}, ["aapl"], ["AAPL", "AAPL"],
    [" AAPL"], ["AAPL "], ["AAPL/US"], ["AAPL", None],
])
def test_invalid_universe_fails_before_any_broker_read(symbols):
    broker = FakeBroker()
    with pytest.raises(ValueError, match="Symbols must"):
        capture(broker, symbols=symbols)
    assert broker.calls == []


def test_opt_in_order_evidence_keeps_account_wide_real_identities():
    broker = FakeBroker([working_order(101), working_order("other-2", symbol="OTHER")])
    portfolio = capture(broker, symbols=(FOREIGN_SYMBOL,), include_order_identities=True)

    assert portfolio.broker_working_orders is not None
    assert [row["order_id"] for row in portfolio.broker_working_orders] == ["101", "other-2"]
    assert [row["symbol"] for row in portfolio.broker_working_orders] == [FOREIGN_SYMBOL, "OTHER"]
    assert all(row["remaining_quantity"] == 3.0 for row in portfolio.broker_working_orders)
    assert all(row["filled_quantity"] == 1.0 for row in portfolio.broker_working_orders)
    assert all(row["reserved_cash"] == 300.0 for row in portfolio.broker_working_orders)
    assert capture(FakeBroker(), include_order_identities=True).broker_working_orders == ()


@pytest.mark.parametrize("orders", [
    [working_order(None)],
    [working_order("duplicate"), working_order("duplicate", symbol="OTHER")],
    [{**working_order(), "status": "UNKNOWN"}],
])
def test_opt_in_order_evidence_rejects_missing_duplicate_or_incomplete_ids(orders):
    with pytest.raises(ValueError):
        capture(FakeBroker(orders), include_order_identities=True)


def test_order_ids_bind_fingerprint_only_when_evidence_is_requested():
    first = [working_order("first")]
    second = [working_order("second")]

    assert capture(FakeBroker(first)).source_fingerprint == capture(FakeBroker(second)).source_fingerprint
    with_ids_first = capture(FakeBroker(first), include_order_identities=True)
    with_ids_second = capture(FakeBroker(second), include_order_identities=True)
    assert with_ids_first.source_fingerprint != with_ids_second.source_fingerprint
    assert with_ids_first.source_fingerprint != capture(FakeBroker(first)).source_fingerprint
