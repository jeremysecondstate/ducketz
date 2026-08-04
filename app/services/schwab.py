from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import urlencode

import requests

from app.models.portfolio import CashBalance, Holding, PortfolioSnapshot
from app.config import SchwabConfig, schwab_config
from app.services.schwab_policy_inputs import (
    SCHWAB_TERMINAL_ORDER_STATUSES,
    normalize_schwab_policy_inputs,
)
from app.services.schwab_option_management import enrich_option_position_quotes
from app.services.schwab_token_store import (
    access_token_is_fresh,
    cached_access_token_expires_at,
    load_token_payload,
    refresh_token_is_available,
    save_token_payload,
)

TRADER_BASE_URL = "https://api.schwabapi.com/trader/v1"
MARKETDATA_BASE_URL = "https://api.schwabapi.com/marketdata/v1"
AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"

SCHWAB_GTC_ORDER_LOOKBACK_DAYS = 180
SCHWAB_ORDER_QUERY_WINDOW_DAYS = 60
SCHWAB_ORDER_QUERY_MAX_RESULTS = 3_000


class SchwabSession:
    def __init__(self, config: SchwabConfig | None = None) -> None:
        self.config = config or schwab_config()
        self.access_token: str | None = None
        self.access_token_expires_at: datetime | None = None
        self.refresh_token: str | None = None
        self.account_hash: str | None = None
        self._hydrate_from_cache()

    def build_authorization_url(self) -> tuple[str, str]:
        state = secrets.token_urlsafe(24)
        params = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "scope": "readonly",
            "state": state,
        }
        return f"{AUTH_URL}?{urlencode(params)}", state

    def exchange_authorization_code(self, authorization_code: str) -> None:
        response = requests.post(
            TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "code": authorization_code.strip(),
                "redirect_uri": self.config.redirect_uri,
            },
            auth=(self.config.client_id, self.config.client_secret),
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        self._store_token_payload(payload, previous_refresh_token=self.refresh_token)

    def ensure_access_token(self) -> None:
        if self._access_token_is_current():
            return

        self.access_token = None
        self.access_token_expires_at = None

        if self.refresh_token:
            self.refresh_access_token()
            return

        raise RuntimeError("Schwab access token is not available. Authorize Schwab first.")

    def refresh_access_token(self) -> None:
        if not self.refresh_token:
            raise RuntimeError("Schwab refresh token is not available.")

        response = requests.post(
            TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
            auth=(self.config.client_id, self.config.client_secret),
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        self._store_token_payload(payload, previous_refresh_token=self.refresh_token)

    def get_account(self) -> Any:
        account_hash = self._get_account_hash()
        response = requests.get(
            f"{TRADER_BASE_URL}/accounts/{account_hash}",
            headers=self._headers(),
            params={"fields": "positions"},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    def _get_account_hash(self) -> str:
        if self.account_hash:
            return self.account_hash

        response = requests.get(
            f"{TRADER_BASE_URL}/accounts/accountNumbers",
            headers=self._headers(),
            timeout=10,
        )
        response.raise_for_status()

        accounts = response.json()
        if not isinstance(accounts, list) or not accounts:
            raise RuntimeError("No Schwab accounts returned.")

        account_hash = accounts[0].get("hashValue")
        if not account_hash:
            raise RuntimeError("Schwab account hashValue was missing.")

        self.account_hash = str(account_hash)
        return self.account_hash

    def _headers(self) -> dict[str, str]:
        self.ensure_access_token()
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
        }

    def _hydrate_from_cache(self) -> None:
        cached_payload = load_token_payload()
        if not cached_payload:
            return

        if access_token_is_fresh(cached_payload):
            self.access_token = cached_payload.get("access_token")
            self.access_token_expires_at = cached_access_token_expires_at(cached_payload)

        if refresh_token_is_available(cached_payload):
            self.refresh_token = cached_payload.get("refresh_token")

    def _access_token_is_current(self) -> bool:
        if not self.access_token:
            return False

        if self.access_token_expires_at is None:
            return self.refresh_token is None

        return self.access_token_expires_at > datetime.now(timezone.utc)

    def _store_token_payload(self, payload: dict[str, Any], previous_refresh_token: str | None) -> None:
        cached_payload = save_token_payload(payload, previous_refresh_token)
        self.access_token = str(payload["access_token"])
        self.access_token_expires_at = cached_access_token_expires_at(cached_payload)
        self.refresh_token = str(payload.get("refresh_token") or previous_refresh_token or "")

    def get_open_orders(self) -> Any:
        now = datetime.now(timezone.utc)
        horizon_start = now - timedelta(days=SCHWAB_GTC_ORDER_LOOKBACK_DAYS)
        orders_by_key: dict[tuple[str, str], Any] = {}
        window_start = horizon_start

        while window_start < now:
            window_end = min(
                window_start + timedelta(days=SCHWAB_ORDER_QUERY_WINDOW_DAYS),
                now,
            )
            window_orders = self.get_orders(
                from_entered_time=window_start,
                to_entered_time=window_end,
                max_results=SCHWAB_ORDER_QUERY_MAX_RESULTS,
            )
            if not isinstance(window_orders, list):
                raise RuntimeError(
                    "Schwab order-history window returned a non-list payload; "
                    "current working orders are unavailable."
                )
            if len(window_orders) >= SCHWAB_ORDER_QUERY_MAX_RESULTS:
                raise RuntimeError(
                    "Schwab order-history window reached the "
                    f"{SCHWAB_ORDER_QUERY_MAX_RESULTS}-row maxResults cap for "
                    f"{window_start.isoformat()} through {window_end.isoformat()}; "
                    "the response may be truncated, so current working orders are unavailable."
                )

            for order in window_orders:
                orders_by_key[_schwab_order_dedup_key(order)] = order
            window_start = window_end

        current_orders: list[Any] = []
        for order in orders_by_key.values():
            if isinstance(order, dict):
                status = str(order.get("status") or "").strip().upper()
                if status in SCHWAB_TERMINAL_ORDER_STATUSES:
                    continue
            current_orders.append(order)
        return current_orders

    def get_recent_orders(self) -> Any:
        now = datetime.now(timezone.utc)
        return self.get_orders(
            from_entered_time=now - timedelta(days=14),
            to_entered_time=now,
        )

    def get_orders(
        self,
        *,
        from_entered_time: datetime,
        to_entered_time: datetime,
        status: str | None = None,
        max_results: int | None = None,
    ) -> Any:
        account_hash = self._get_account_hash()
        params = {
            "fromEnteredTime": from_entered_time.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "toEnteredTime": to_entered_time.astimezone(timezone.utc).isoformat(timespec="seconds"),
        }
        if status:
            params["status"] = status
        if max_results is not None:
            params["maxResults"] = max_results

        response = requests.get(
            f"{TRADER_BASE_URL}/accounts/{account_hash}/orders",
            headers=self._headers(),
            params=params,
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    def get_option_chain(self, symbol: str, strikes: int) -> Any:
        cleaned_symbol = symbol.strip().upper()
        if not cleaned_symbol:
            raise ValueError("Symbol is required for option chain.")

        response = requests.get(
            f"{MARKETDATA_BASE_URL}/chains",
            headers=self._headers(),
            params={
                "symbol": cleaned_symbol,
                "contractType": "ALL",
                "strikeCount": strikes,
                "includeUnderlyingQuote": "true",
                "strategy": "SINGLE",
            },
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    def get_equity_quotes(self, symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
        cleaned_symbols = tuple(
            dict.fromkeys(
                str(symbol).strip().upper()
                for symbol in symbols
                if str(symbol).strip()
            )
        )
        if not cleaned_symbols:
            raise ValueError("At least one stock / ETF symbol is required for quotes.")
        response = requests.get(
            f"{MARKETDATA_BASE_URL}/quotes",
            headers=self._headers(),
            params={
                "symbols": ",".join(cleaned_symbols),
                "fields": "quote",
            },
            timeout=10,
        )
        response.raise_for_status()

        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Unexpected Schwab quote response.")

        rows_by_symbol = {
            str(key).strip().upper(): row
            for key, row in payload.items()
            if isinstance(row, dict)
        }
        quotes: dict[str, dict[str, Any]] = {}
        for symbol in cleaned_symbols:
            row = rows_by_symbol.get(symbol)
            if not isinstance(row, dict):
                continue
            quote = row.get("quote")
            quotes[symbol] = quote if isinstance(quote, dict) else row
        return quotes

    def get_equity_quote(self, symbol: str) -> dict[str, Any]:
        cleaned_symbol = symbol.strip().upper()
        quotes = self.get_equity_quotes((cleaned_symbol,))
        quote = quotes.get(cleaned_symbol)
        if quote is None:
            raise RuntimeError(f"No quote returned for {cleaned_symbol}.")
        return quote

    def get_equity_mid(self, symbol: str) -> float:
        cleaned_symbol = symbol.strip().upper()
        quote = self.get_equity_quote(cleaned_symbol)

        bid = _first_number(quote, ("bidPrice", "bid"))
        ask = _first_number(quote, ("askPrice", "ask"))
        mark = _first_number(quote, ("mark", "markPrice"))
        last = _first_number(quote, ("lastPrice", "last"))

        if bid is not None and ask is not None and bid > 0 and ask > 0:
            return round((bid + ask) / 2, 2)

        if mark is not None and mark > 0:
            return round(mark, 2)

        if last is not None and last > 0:
            return round(last, 2)

        raise RuntimeError(
            f"Quote for {cleaned_symbol} did not include a usable bid/ask, mark, or last price."
        )

    def cancel_order(self, order_id: str) -> object:
        cleaned_order_id = str(order_id).strip()
        if not cleaned_order_id:
            raise ValueError("Order ID is required for cancel.")

        account_hash = self._get_account_hash()
        response = requests.delete(
            f"{TRADER_BASE_URL}/accounts/{account_hash}/orders/{cleaned_order_id}",
            headers=self._headers(),
            timeout=10,
        )
        response.raise_for_status()

        if not response.text:
            return None

        try:
            return response.json()
        except ValueError:
            return response.text

    def submit_order(self, order_payload: dict[str, Any]) -> str | None:
        account_hash = self._get_account_hash()
        response = requests.post(
            f"{TRADER_BASE_URL}/accounts/{account_hash}/orders",
            headers={**self._headers(), "Content-Type": "application/json"},
            json=order_payload,
            timeout=10,
        )
        response.raise_for_status()
        return response.headers.get("Location")


def _schwab_order_dedup_key(order: Any) -> tuple[str, str]:
    if isinstance(order, dict):
        order_id = str(order.get("orderId") or order.get("order_id") or "").strip()
        if order_id:
            return "order_id", order_id
    return "payload", json.dumps(order, sort_keys=True, separators=(",", ":"), default=str)


def sync_schwab_portfolio() -> PortfolioSnapshot:
    session = SchwabSession()
    account_payload = session.get_account()
    orders_payload: Any = None
    orders_error: str | None = None
    try:
        orders_payload = session.get_open_orders()
    except Exception as exc:
        orders_error = f"{type(exc).__name__}: {exc}"

    synced_at = datetime.now(timezone.utc)
    account_facts = normalize_schwab_policy_inputs(
        account_payload,
        orders_payload,
        observed_at=synced_at,
        orders_error=orders_error,
    )
    option_symbols = _normalized_option_symbols(account_facts)
    if option_symbols:
        try:
            option_quotes = session.get_equity_quotes(option_symbols)
        except Exception as exc:
            normalized_positions = account_facts.get("positions")
            if isinstance(normalized_positions, dict):
                normalized_positions["option_quote_status"] = "UNAVAILABLE"
                normalized_positions["option_quote_unavailable_reasons"] = [
                    f"Schwab option quote refresh failed: {type(exc).__name__}: {exc}"
                ]
        else:
            enrich_option_position_quotes(
                account_facts,
                option_quotes,
                observed_at=synced_at,
            )
    account = _securities_account(account_payload)
    account_values = account_facts["account_values"]
    if not isinstance(account_values, dict):
        raise RuntimeError("Normalized Schwab account values were unavailable.")

    holdings = [_holding_from_schwab(row) for row in _position_rows(account)]
    holdings = [holding for holding in holdings if holding is not None]

    liquidation_value = _to_float(account_values.get("liquidation_value"))
    cash_balance = _to_float(account_values.get("cash_balance"))
    cash: list[CashBalance] = []
    if cash_balance is not None:
        cash.append(
            CashBalance(
                symbol="USD",
                amount=round(cash_balance, 2),
                value=round(cash_balance, 2),
                source="schwab",
                bucket="Cash balance",
            )
        )

    order_status = ""
    working_orders = account_facts.get("working_orders")
    if isinstance(working_orders, dict) and working_orders.get("status") != "CURRENT":
        order_status = f"; working orders {str(working_orders.get('status', 'UNAVAILABLE')).lower()}"

    return PortfolioSnapshot(
        source="schwab",
        account_label="Schwab",
        cash=cash,
        holdings=holdings,
        synced_at=synced_at,
        status=f"Schwab synced {_account_label(account)}{order_status}",
        reported_total_value=liquidation_value,
        account_facts=account_facts,
    )


def _securities_account(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected Schwab account response.")

    account = payload.get("securitiesAccount") or payload
    if not isinstance(account, dict):
        raise RuntimeError("Unexpected Schwab account response; missing securitiesAccount.")

    return account


def _normalized_option_symbols(account_facts: dict[str, object]) -> tuple[str, ...]:
    positions = account_facts.get("positions")
    if not isinstance(positions, dict):
        return ()
    items = positions.get("items")
    if not isinstance(items, list):
        return ()
    return tuple(
        dict.fromkeys(
            str(row.get("symbol") or "").strip().upper()
            for row in items
            if isinstance(row, dict)
            and "OPTION" in str(row.get("asset_type") or "").upper()
            and str(row.get("symbol") or "").strip()
        )
    )


def _position_rows(account: dict[str, Any]) -> list[dict[str, Any]]:
    positions = account.get("positions") or []
    return [row for row in positions if isinstance(row, dict)] if isinstance(positions, list) else []


def _holding_from_schwab(row: dict[str, Any]) -> Holding | None:
    instrument = row.get("instrument") if isinstance(row.get("instrument"), dict) else {}

    symbol = str(instrument.get("symbol") or row.get("symbol") or "").strip().upper()
    quantity = _net_quantity(row)

    if not symbol or abs(quantity) <= 0.00000001:
        return None

    market_value = _to_float(row.get("marketValue"))
    price = _first_number(row, ("marketPrice", "lastPrice", "currentPrice", "markPrice"))

    if price is None and market_value is not None and abs(quantity) > 0.00000001:
        price = abs(market_value / quantity)

    if market_value is None:
        market_value = quantity * (price or 0.0)

    return Holding(
        symbol=symbol,
        quantity=round(quantity, 8),
        price=round(price or 0.0, 8),
        value=round(market_value, 2),
        source="schwab",
        bucket="Equity",
        unrealized_pnl=_schwab_unrealized_pnl(row),
        day_pnl=_schwab_day_pnl(row),
    )


def _net_quantity(row: dict[str, Any]) -> float:
    long_quantity = _to_float(row.get("longQuantity"))
    short_quantity = _to_float(row.get("shortQuantity"))

    if long_quantity is not None or short_quantity is not None:
        return (long_quantity or 0.0) - (short_quantity or 0.0)

    for key in ("quantity", "settledLongQuantity", "agedQuantity"):
        value = _to_float(row.get(key))
        if value is not None:
            return value

    return 0.0


def _schwab_unrealized_pnl(row: dict[str, Any]) -> float | None:
    long_pnl = _to_float(row.get("longOpenProfitLoss"))
    short_pnl = _to_float(row.get("shortOpenProfitLoss"))

    if long_pnl is not None or short_pnl is not None:
        return round((long_pnl or 0.0) + (short_pnl or 0.0), 2)

    value = _first_number(row, ("openProfitLoss", "unrealizedProfitLoss", "unrealizedPnl"))
    return round(value, 2) if value is not None else None


def _schwab_day_pnl(row: dict[str, Any]) -> float | None:
    value = _first_number(row, ("currentDayProfitLoss", "dayProfitLoss"))

    if value is not None:
        return round(value, 2)

    market_value = _to_float(row.get("marketValue"))
    day_pnl_percent = _first_number(row, ("currentDayProfitLossPercentage", "dayProfitLossPercentage"))

    if market_value is None or day_pnl_percent is None:
        return None

    return round(market_value * (day_pnl_percent / 100.0), 2)


def _first_number(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _to_float(row.get(key))
        if value is not None:
            return value

    return None


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _account_label(account: dict[str, Any]) -> str:
    account_number = str(account.get("accountNumber") or "").strip()
    if account_number:
        return "••••" + account_number[-4:]

    return "account"
