from __future__ import annotations

from datetime import datetime
from typing import Any

import requests
import time

from app.config import HyperliquidAccountConfig, hyperliquid_accounts, hyperliquid_info_url
from app.models.portfolio import CashBalance, Holding, PortfolioSnapshot


ZERO_EPSILON = 0.00000001
CASH_SYMBOLS = {"USDC", "USD"}


class HyperliquidInfoClient:
    def __init__(self, info_url: str | None = None, timeout_seconds: int = 30) -> None:
        self.info_url = (info_url or hyperliquid_info_url()).strip()
        self.timeout_seconds = timeout_seconds

    def post_info(self, payload: dict[str, Any]) -> Any:
        last_error: Exception | None = None

        for attempt in range(3):
            try:
                response = requests.post(
                    self.info_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=self.timeout_seconds,
                )

                if response.status_code in {502, 503, 504} and attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue

                response.raise_for_status()
                return response.json()
            except requests.RequestException as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise

        raise RuntimeError(f"Hyperliquid info request failed: {last_error}")


def sync_hyperliquid_portfolios() -> list[PortfolioSnapshot]:
    client = HyperliquidInfoClient()
    return [
        sync_hyperliquid_portfolio(account, client)
        for account in hyperliquid_accounts()
    ]


def sync_hyperliquid_portfolio(
    account: HyperliquidAccountConfig,
    client: HyperliquidInfoClient | None = None,
) -> PortfolioSnapshot:
    wallet_address = _normalize_wallet_address(account.wallet_address)
    client = client or HyperliquidInfoClient()

    clearinghouse_state = client.post_info({"type": "clearinghouseState", "user": wallet_address})
    spot_state = client.post_info({"type": "spotClearinghouseState", "user": wallet_address})
    all_mids = client.post_info({"type": "allMids"})
    spot_meta_and_asset_ctxs = client.post_info({"type": "spotMetaAndAssetCtxs"})

    if not isinstance(clearinghouse_state, dict):
        raise RuntimeError("Hyperliquid clearinghouseState returned an unexpected response.")
    if not isinstance(spot_state, dict):
        raise RuntimeError("Hyperliquid spotClearinghouseState returned an unexpected response.")
    if not isinstance(all_mids, dict):
        raise RuntimeError("Hyperliquid allMids returned an unexpected response.")
    if not isinstance(spot_meta_and_asset_ctxs, list):
        raise RuntimeError("Hyperliquid spotMetaAndAssetCtxs returned an unexpected response.")

    perp_holdings = _perp_holdings(clearinghouse_state)
    spot_cash, spot_holdings = _spot_balances(spot_state, all_mids, spot_meta_and_asset_ctxs)

    perp_account_value = _perp_account_value(clearinghouse_state)
    perp_notional = round(sum(holding.value for holding in perp_holdings), 2)
    perp_cash_value = round(perp_account_value - perp_notional, 2)

    cash = list(spot_cash)
    if abs(perp_cash_value) > 0.005:
        cash.append(
            CashBalance(
                symbol="USDC",
                amount=perp_cash_value,
                value=perp_cash_value,
                source="hyperliquid",
                bucket="Perps",
            )
        )

    return PortfolioSnapshot(
        source="hyperliquid",
        account_label=account.label,
        cash=cash,
        holdings=[*perp_holdings, *spot_holdings],
        synced_at=datetime.now(),
        status=f"{account.label} synced {wallet_address[:6]}...{wallet_address[-4:]}",
    )


def _perp_holdings(clearinghouse_state: dict[str, Any]) -> list[Holding]:
    holdings: list[Holding] = []

    for row in _dict_rows(clearinghouse_state.get("assetPositions")):
        position = row.get("position") if isinstance(row.get("position"), dict) else row

        coin = str(position.get("coin") or "").strip().upper()
        signed_size = _to_float(position.get("szi") or position.get("size")) or 0.0

        if not coin or abs(signed_size) <= ZERO_EPSILON:
            continue

        quantity = abs(signed_size)
        value = abs(_to_float(position.get("positionValue")) or 0.0)
        price = _first_number(position, ("markPx", "oraclePx", "midPx", "entryPx"))

        if price is None:
            price = value / quantity if quantity > ZERO_EPSILON else 0.0

        side_suffix = "PERP" if signed_size > 0 else "PERP-SHORT"

        holdings.append(
            Holding(
                symbol=f"{coin}-PERP" if signed_size > 0 else f"{coin}-PERP-SHORT",
                quantity=round(quantity, 8),
                price=round(price, 8),
                value=round(value, 2),
                source="hyperliquid",
                bucket="Perps",
                unrealized_pnl=_to_float(position.get("unrealizedPnl")),
                day_pnl=None,
            )
        )

    return holdings


def _spot_balances(
    spot_state: dict[str, Any],
    all_mids: dict[str, Any],
    spot_meta_and_asset_ctxs: list[Any],
) -> tuple[list[CashBalance], list[Holding]]:
    cash: list[CashBalance] = []
    holdings: list[Holding] = []

    for balance in _dict_rows(spot_state.get("balances")):
        symbol = str(balance.get("coin") or balance.get("token") or "").strip().upper()
        quantity = _first_number(balance, ("total", "balance", "amount")) or 0.0

        if not symbol or quantity <= ZERO_EPSILON:
            continue

        value = _spot_value(symbol, quantity, balance, all_mids, spot_meta_and_asset_ctxs)

        if symbol in CASH_SYMBOLS:
            cash.append(
                CashBalance(
                    symbol=symbol,
                    amount=round(quantity, 8),
                    value=round(value, 2),
                    source="hyperliquid",
                    bucket="Spot",
                )
            )
            continue

        price = value / quantity

        holdings.append(
            Holding(
                symbol=f"{symbol}-SPOT",
                quantity=round(quantity, 8),
                price=round(price, 8),
                value=round(value, 2),
                source="hyperliquid",
                bucket="Spot",
            )
        )

    return cash, holdings


def _spot_value(
    symbol: str,
    quantity: float,
    balance: dict[str, Any],
    all_mids: dict[str, Any],
    spot_meta_and_asset_ctxs: list[Any],
) -> float:
    direct_value = _first_number(balance, ("usdValue", "usdcValue", "currentValue", "marketValue", "value"))
    if direct_value is not None:
        return direct_value

    if symbol in CASH_SYMBOLS:
        return quantity

    price = _spot_price(symbol, balance, all_mids, spot_meta_and_asset_ctxs)
    if price is None:
        available_keys = ", ".join(sorted(str(key) for key in all_mids.keys())[:20])
        raise RuntimeError(
            f"Could not price Hyperliquid spot balance: {symbol}. "
            f"First allMids keys: {available_keys}"
        )

    return quantity * price


def _spot_price(
    symbol: str,
    balance: dict[str, Any],
    all_mids: dict[str, Any],
    spot_meta_and_asset_ctxs: list[Any],
) -> float | None:
    direct_price = _to_float(all_mids.get(symbol))
    if direct_price is not None:
        return direct_price

    token_index = _spot_token_index(balance)
    candidate_keys = _spot_mid_keys(symbol, token_index, spot_meta_and_asset_ctxs)

    for key in candidate_keys:
        price = _to_float(all_mids.get(key))
        if price is not None:
            return price

    return None


def _spot_token_index(balance: dict[str, Any]) -> int | None:
    value = balance.get("token")
    if isinstance(value, int):
        return value

    if isinstance(value, str) and value.isdigit():
        return int(value)

    value = balance.get("tokenIndex")
    if isinstance(value, int):
        return value

    if isinstance(value, str) and value.isdigit():
        return int(value)

    return None


def _spot_mid_keys(
    symbol: str,
    token_index: int | None,
    spot_meta_and_asset_ctxs: list[Any],
) -> list[str]:
    keys = [symbol]

    if token_index is not None:
        keys.append(f"@{token_index}")

    universe = _spot_universe(spot_meta_and_asset_ctxs)
    for index, asset in enumerate(universe):
        if not isinstance(asset, dict):
            continue

        name = str(asset.get("name") or asset.get("coin") or "").strip().upper()
        tokens = asset.get("tokens")

        if name == symbol:
            keys.extend([str(asset.get("name")), f"@{index}"])

        if isinstance(tokens, list) and token_index is not None and token_index in tokens:
            keys.extend([str(asset.get("name")), f"@{index}"])

    return _dedupe_strings([key for key in keys if key])


def _spot_universe(spot_meta_and_asset_ctxs: list[Any]) -> list[Any]:
    if not spot_meta_and_asset_ctxs:
        return []

    meta = spot_meta_and_asset_ctxs[0]
    if not isinstance(meta, dict):
        return []

    universe = meta.get("universe")
    return universe if isinstance(universe, list) else []


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []

    for value in values:
        if value not in result:
            result.append(value)

    return result


def _perp_account_value(clearinghouse_state: dict[str, Any]) -> float:
    margin_summary = clearinghouse_state.get("marginSummary")
    cross_margin_summary = clearinghouse_state.get("crossMarginSummary")

    for summary in (margin_summary, cross_margin_summary):
        if not isinstance(summary, dict):
            continue

        account_value = _to_float(summary.get("accountValue"))
        if account_value is not None:
            return account_value

    return 0.0


def _normalize_wallet_address(address: str) -> str:
    normalized = address.strip()

    if not normalized.startswith("0x") or len(normalized) != 42:
        raise ValueError(
            "Hyperliquid sync expects a 42-character 0x wallet address. "
            "Use the master/sub-account wallet address, not an API wallet."
        )

    return normalized


def _dict_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []

    return [row for row in value if isinstance(row, dict)]


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
