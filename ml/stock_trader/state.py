from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Mapping, Protocol

from app.services.schwab_policy_inputs import normalize_schwab_policy_inputs
from ml.stock_trader.contracts import (
    PortfolioState,
    QuoteState,
    STOCK_TRADER_SYMBOLS,
    canonical_sha256,
    finite,
    utc,
)


class SchwabReadSession(Protocol):
    def get_account(self) -> Any: ...

    def get_open_orders(self) -> Any: ...

    def get_equity_quotes(self, symbols: tuple[str, ...]) -> Mapping[str, object]: ...


def capture_portfolio_state(
    session: SchwabReadSession,
    *,
    observed_at: object,
    parallel: bool = True,
) -> PortfolioState:
    """Capture one coherent pre-decision input set without serial symbol reads."""

    timestamp = utc(observed_at)
    if parallel:
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="stock-trader-state") as pool:
            account_future = pool.submit(session.get_account)
            orders_future = pool.submit(session.get_open_orders)
            quotes_future = pool.submit(session.get_equity_quotes, STOCK_TRADER_SYMBOLS)
            account_payload = account_future.result()
            orders_payload = orders_future.result()
            quotes_payload = quotes_future.result()
    else:
        account_payload = session.get_account()
        orders_payload = session.get_open_orders()
        quotes_payload = session.get_equity_quotes(STOCK_TRADER_SYMBOLS)
    normalized = normalize_schwab_policy_inputs(
        account_payload,
        orders_payload,
        observed_at=timestamp.to_pydatetime(),
    )
    account = _mapping(normalized.get("account_values"), "account_values")
    positions = _mapping(normalized.get("positions"), "positions")
    working = _mapping(normalized.get("working_orders"), "working_orders")
    if not bool(positions.get("stock_policy_row_set_complete")):
        raise ValueError("Schwab stock position rows are not complete enough for sizing")
    if str(working.get("status")) != "CURRENT":
        raise ValueError("Schwab working orders are not complete enough for sizing")
    equity = finite(account.get("liquidation_value"))
    if equity is None or equity <= 0.0:
        raise ValueError("Schwab liquidation value is unavailable or nonpositive")
    reserved_cash = finite(working.get("reserved_cash"), default=0.0) or 0.0
    preferred_cash_candidates = [
        value
        for key in (
            "cash_available_for_trading",
            "available_funds_non_marginable_trade",
            "buying_power_non_marginable_trade",
        )
        if (value := finite(account.get(key))) is not None
    ]
    cash_candidates = preferred_cash_candidates or [
        value
        for key in ("settled_cash", "cash_balance")
        if (value := finite(account.get(key))) is not None
    ]
    if not cash_candidates:
        raise ValueError("Schwab did not provide any usable stock buying-power balance")
    available_cash = max(0.0, min(cash_candidates) - max(0.0, reserved_cash))
    held_shares = {symbol: 0.0 for symbol in STOCK_TRADER_SYMBOLS}
    symbol_exposure = {symbol: 0.0 for symbol in STOCK_TRADER_SYMBOLS}
    gross_exposure = 0.0
    daily_pnl = 0.0
    raw_items = positions.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("Schwab normalized positions did not include an items list")
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            continue
        market_value = finite(raw_item.get("market_value"))
        if market_value is None:
            raise ValueError("A Schwab position had no usable current market value")
        gross_exposure += abs(market_value)
        daily_pnl += finite(raw_item.get("day_pnl"), default=0.0) or 0.0
        asset_type = str(raw_item.get("asset_type") or "").upper()
        symbol = str(raw_item.get("symbol") or "").upper()
        underlying = str(raw_item.get("underlying_symbol") or symbol).upper()
        if underlying in symbol_exposure:
            symbol_exposure[underlying] += abs(market_value)
        if asset_type in {"EQUITY", "STOCK"} and symbol in held_shares:
            quantity = finite(raw_item.get("net_quantity"), default=0.0) or 0.0
            held_shares[symbol] += max(0.0, quantity)
    pending_buys = _symbol_map(working.get("pending_buy_shares_by_symbol"))
    pending_sells = _symbol_map(working.get("pending_sell_shares_by_symbol"))
    if not isinstance(quotes_payload, Mapping):
        raise ValueError("Schwab quote response is not an object")
    quotes: dict[str, QuoteState] = {}
    for symbol in STOCK_TRADER_SYMBOLS:
        raw_quote = quotes_payload.get(symbol)
        if not isinstance(raw_quote, Mapping):
            continue
        bid = _first_number(raw_quote, "bidPrice", "bid")
        ask = _first_number(raw_quote, "askPrice", "ask")
        if bid is None or ask is None or bid <= 0.0 or ask <= 0.0 or ask < bid:
            continue
        quotes[symbol] = QuoteState(
            symbol=symbol,
            bid=bid,
            ask=ask,
            last=_first_number(raw_quote, "lastPrice", "last"),
            mark=_first_number(raw_quote, "mark", "markPrice"),
            volume=_first_number(raw_quote, "totalVolume", "volume"),
            observed_at=timestamp.isoformat(),
        )
    working_items = working.get("items")
    working_count = len(working_items) if isinstance(working_items, list) else 0
    fingerprint_payload = {
        "observed_at": timestamp.isoformat(),
        "account_equity": equity,
        "available_cash": available_cash,
        "gross_exposure": gross_exposure,
        "daily_pnl": daily_pnl,
        "held_shares": held_shares,
        "symbol_exposure": symbol_exposure,
        "pending_buy_shares": pending_buys,
        "pending_sell_shares": pending_sells,
        "working_order_count": working_count,
        "quotes": {
            symbol: {
                "bid": quote.bid,
                "ask": quote.ask,
                "last": quote.last,
                "mark": quote.mark,
                "volume": quote.volume,
            }
            for symbol, quote in quotes.items()
        },
    }
    return PortfolioState(
        observed_at=timestamp.isoformat(),
        account_equity=equity,
        available_cash=available_cash,
        gross_exposure=gross_exposure,
        daily_pnl=daily_pnl,
        held_shares=held_shares,
        symbol_exposure=symbol_exposure,
        pending_buy_shares=pending_buys,
        pending_sell_shares=pending_sells,
        working_order_count=working_count,
        quotes=quotes,
        source_fingerprint=canonical_sha256(fingerprint_payload),
    )


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Schwab normalized {label} is unavailable")
    return value


def _symbol_map(value: object) -> dict[str, float]:
    source = value if isinstance(value, Mapping) else {}
    return {
        symbol: max(0.0, finite(source.get(symbol), default=0.0) or 0.0)
        for symbol in STOCK_TRADER_SYMBOLS
    }


def _first_number(row: Mapping[str, object], *keys: str) -> float | None:
    for key in keys:
        value = finite(row.get(key))
        if value is not None:
            return value
    return None


__all__ = ["SchwabReadSession", "capture_portfolio_state"]
