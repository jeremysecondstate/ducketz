from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Mapping, Protocol, TypeVar

from app.services.schwab_policy_inputs import normalize_schwab_policy_inputs
from app.services.schwab_retry import is_retryable_schwab_error
from ml.stock_trader.contracts import (
    PortfolioState,
    QuoteState,
    STOCK_TRADER_SYMBOLS,
    canonical_sha256,
    finite,
    utc,
)


_T = TypeVar("_T")


class SchwabReadSession(Protocol):
    def prepare_read_snapshot(self) -> str | None: ...

    def verify_read_snapshot(self, expected_identity_fingerprint: str) -> None: ...

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
    broker_identity_fingerprint: str | None = None
    prepare = getattr(session, "prepare_read_snapshot", None)
    if callable(prepare):
        prepared_identity = _read_schwab_component("session_preflight", prepare)
        if prepared_identity is not None:
            broker_identity_fingerprint = str(prepared_identity).strip() or None
    if parallel:
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="stock-trader-state") as pool:
            account_future = pool.submit(
                _read_schwab_component, "account", session.get_account
            )
            orders_future = pool.submit(
                _read_schwab_component, "open_orders", session.get_open_orders
            )
            quotes_future = pool.submit(
                _read_schwab_component,
                "equity_quotes",
                lambda: session.get_equity_quotes(STOCK_TRADER_SYMBOLS),
            )
            payloads: dict[str, Any] = {}
            failures: list[Exception] = []
            for operation_name, future in (
                ("account", account_future),
                ("open_orders", orders_future),
                ("equity_quotes", quotes_future),
            ):
                try:
                    payloads[operation_name] = future.result()
                except Exception as exc:
                    failures.append(exc)
            if failures:
                failure = next(
                    (
                        exc
                        for exc in failures
                        if not is_retryable_schwab_error(exc)
                    ),
                    failures[0],
                )
                raise failure
            account_payload = payloads["account"]
            orders_payload = payloads["open_orders"]
            quotes_payload = payloads["equity_quotes"]
    else:
        account_payload = _read_schwab_component("account", session.get_account)
        orders_payload = _read_schwab_component("open_orders", session.get_open_orders)
        quotes_payload = _read_schwab_component(
            "equity_quotes", lambda: session.get_equity_quotes(STOCK_TRADER_SYMBOLS)
        )
    if broker_identity_fingerprint is not None:
        verify = getattr(session, "verify_read_snapshot", None)
        if not callable(verify):
            raise RuntimeError(
                "Schwab snapshot identity was captured but cannot be verified."
            )
        _read_schwab_component(
            "account_identity",
            lambda: verify(broker_identity_fingerprint),
        )
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
    (
        reserved_cash,
        pending_buys,
        pending_sells,
        stock_working_status,
    ) = _stock_working_order_effects(working)
    equity = finite(account.get("liquidation_value"))
    if equity is None or equity <= 0.0:
        raise ValueError("Schwab liquidation value is unavailable or nonpositive")
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
        "stock_working_status": stock_working_status,
        "broker_identity_fingerprint": broker_identity_fingerprint,
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
        broker_identity_fingerprint=broker_identity_fingerprint,
    )


def _read_schwab_component(
    operation_name: str, operation: Callable[[], _T]
) -> _T:
    try:
        return operation()
    except Exception as exc:
        try:
            if not getattr(exc, "stock_trader_operation", None):
                setattr(exc, "stock_trader_operation", operation_name)
        except (AttributeError, TypeError):
            pass
        raise


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


def _stock_working_order_effects(
    working: Mapping[str, object],
) -> tuple[float, dict[str, float], dict[str, float], str]:
    """Resolve stock-only pending effects without inheriting option metadata gaps.

    Schwab can omit contract multipliers from otherwise valid working option
    orders.  That prevents exact option-risk normalization, but it does not make
    the symbol, side, quantity, or reserve of a separate stock order ambiguous.
    The stock trader therefore requires every non-option working row to be
    complete while allowing option-only completeness errors to remain in their
    own lane.  Broker-reported non-marginable buying power remains the primary
    cash bound and already reflects account-wide commitments.
    """

    status = str(working.get("status") or "").upper()
    if status == "CURRENT":
        reserved = max(0.0, finite(working.get("reserved_cash"), default=0.0) or 0.0)
        return (
            reserved,
            _symbol_map(working.get("pending_buy_shares_by_symbol")),
            _symbol_map(working.get("pending_sell_shares_by_symbol")),
            "CURRENT",
        )
    if status != "INCOMPLETE":
        raise ValueError("Schwab working orders are not complete enough for stock sizing")

    raw_items = working.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("Schwab working orders did not include an items list")
    reserved_cash = 0.0
    pending_buys = {symbol: 0.0 for symbol in STOCK_TRADER_SYMBOLS}
    pending_sells = {symbol: 0.0 for symbol in STOCK_TRADER_SYMBOLS}
    for item in raw_items:
        if not isinstance(item, Mapping):
            raise ValueError("A Schwab working-order row was not structured")
        asset_type = str(item.get("asset_type") or "").upper()
        if asset_type == "OPTION":
            continue
        if asset_type not in {"EQUITY", "ETF", "STOCK"}:
            raise ValueError(
                "A non-option Schwab working order had unknown asset identity"
            )
        if str(item.get("status") or "").upper() != "CURRENT" or item.get(
            "unavailable_reasons"
        ):
            raise ValueError(
                "A Schwab stock working order is not complete enough for sizing"
            )
        symbol = str(item.get("symbol") or "").upper()
        effect = finite(item.get("pending_stock_share_effect"))
        if not symbol or effect is None:
            raise ValueError("A Schwab stock working order has no exact pending effect")
        reserved_cash += max(
            0.0, finite(item.get("reserved_cash"), default=0.0) or 0.0
        )
        if symbol in pending_buys and effect > 0.0:
            pending_buys[symbol] += effect
        elif symbol in pending_sells and effect < 0.0:
            pending_sells[symbol] += abs(effect)
    return (
        reserved_cash,
        pending_buys,
        pending_sells,
        "CURRENT_STOCK_SCOPE_OPTION_METADATA_INCOMPLETE",
    )


def _first_number(row: Mapping[str, object], *keys: str) -> float | None:
    for key in keys:
        value = finite(row.get(key))
        if value is not None:
            return value
    return None


__all__ = ["SchwabReadSession", "capture_portfolio_state"]
