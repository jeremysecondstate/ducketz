from __future__ import annotations

import math
import time
from datetime import datetime
from typing import Any

import requests

from app.config import (
    HyperliquidAccountConfig,
    hyperliquid_accounts,
    hyperliquid_info_url,
    hyperevm_rpc_url,
)
from app.models.portfolio import CashBalance, Holding, PortfolioSnapshot


ZERO_EPSILON = 0.00000001
CASH_SYMBOLS = {"USDC", "USD"}
HYPE_SYMBOL = "HYPE"
HYPE_CANDLE_INTERVAL = "15m"
HYPE_CANDLE_LOOKBACK_MILLISECONDS = 24 * 60 * 60 * 1000


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


class HyperEvmRpcClient:
    def __init__(self, rpc_url: str | None = None, timeout_seconds: int = 8) -> None:
        self.rpc_url = (rpc_url or hyperevm_rpc_url()).strip()
        self.timeout_seconds = timeout_seconds

    def chain_status(self) -> dict[str, object]:
        requests_by_id = {
            1: "eth_chainId",
            2: "eth_blockNumber",
            3: "eth_gasPrice",
        }
        response = requests.post(
            self.rpc_url,
            json=[
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": []}
                for request_id, method in requests_by_id.items()
            ],
            headers={"Content-Type": "application/json"},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError("HyperEVM JSON-RPC returned an unexpected response.")

        result_by_id = {
            row.get("id"): row.get("result")
            for row in payload
            if isinstance(row, dict) and row.get("error") is None
        }
        chain_id = _hex_quantity(result_by_id.get(1))
        block_number = _hex_quantity(result_by_id.get(2))
        gas_price_wei = _hex_quantity(result_by_id.get(3))
        if chain_id is None or block_number is None or gas_price_wei is None:
            raise RuntimeError("HyperEVM JSON-RPC omitted chain status fields.")
        return {
            "available": True,
            "chain_id": chain_id,
            "block_number": block_number,
            "gas_price_wei": gas_price_wei,
        }


def sync_hyperliquid_portfolios() -> list[PortfolioSnapshot]:
    client = HyperliquidInfoClient()
    all_mids = client.post_info({"type": "allMids"})
    spot_meta_and_asset_ctxs = client.post_info({"type": "spotMetaAndAssetCtxs"})

    if not isinstance(all_mids, dict):
        raise RuntimeError("Hyperliquid allMids returned an unexpected response.")
    if not isinstance(spot_meta_and_asset_ctxs, list):
        raise RuntimeError("Hyperliquid spotMetaAndAssetCtxs returned an unexpected response.")

    hype_market = _hype_market_facts(all_mids, spot_meta_and_asset_ctxs)
    market_coin = str(hype_market.get("coin") or "").strip()
    if market_coin:
        try:
            candles = _hype_candle_closes(client, market_coin)
        except Exception as exc:
            hype_market["chart_status"] = f"{type(exc).__name__}: {exc}"
        else:
            hype_market["closes_24h"] = candles
            hype_market["chart_status"] = "current" if candles else "unavailable"

    try:
        chain_status = HyperEvmRpcClient().chain_status()
    except Exception as exc:
        chain_status = {
            "available": False,
            "status": f"{type(exc).__name__}: {exc}",
        }

    return [
        _sync_hyperliquid_portfolio_with_market(
            account,
            client,
            all_mids=all_mids,
            spot_meta_and_asset_ctxs=spot_meta_and_asset_ctxs,
            hype_market=hype_market,
            chain_status=chain_status,
        )
        for account in hyperliquid_accounts()
    ]


def sync_hyperliquid_portfolio(
    account: HyperliquidAccountConfig,
    client: HyperliquidInfoClient | None = None,
) -> PortfolioSnapshot:
    wallet_address = _normalize_wallet_address(account.wallet_address)
    client = client or HyperliquidInfoClient()

    all_mids = client.post_info({"type": "allMids"})
    spot_meta_and_asset_ctxs = client.post_info({"type": "spotMetaAndAssetCtxs"})
    if not isinstance(all_mids, dict):
        raise RuntimeError("Hyperliquid allMids returned an unexpected response.")
    if not isinstance(spot_meta_and_asset_ctxs, list):
        raise RuntimeError("Hyperliquid spotMetaAndAssetCtxs returned an unexpected response.")

    return _sync_hyperliquid_portfolio_with_market(
        account,
        client,
        all_mids=all_mids,
        spot_meta_and_asset_ctxs=spot_meta_and_asset_ctxs,
        hype_market=_hype_market_facts(all_mids, spot_meta_and_asset_ctxs),
        chain_status={},
    )


def _sync_hyperliquid_portfolio_with_market(
    account: HyperliquidAccountConfig,
    client: HyperliquidInfoClient,
    *,
    all_mids: dict[str, Any],
    spot_meta_and_asset_ctxs: list[Any],
    hype_market: dict[str, object],
    chain_status: dict[str, object],
) -> PortfolioSnapshot:
    wallet_address = _normalize_wallet_address(account.wallet_address)
    clearinghouse_state = client.post_info(
        {"type": "clearinghouseState", "user": wallet_address}
    )
    spot_state = client.post_info(
        {"type": "spotClearinghouseState", "user": wallet_address}
    )

    if not isinstance(clearinghouse_state, dict):
        raise RuntimeError("Hyperliquid clearinghouseState returned an unexpected response.")
    if not isinstance(spot_state, dict):
        raise RuntimeError("Hyperliquid spotClearinghouseState returned an unexpected response.")

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

    account_facts = _hyperliquid_account_facts(clearinghouse_state)
    account_facts.update(
        {
            "spot_equity": round(
                sum(balance.value for balance in spot_cash)
                + sum(holding.value for holding in spot_holdings),
                2,
            ),
            "hype_market": dict(hype_market),
            "chain_status": dict(chain_status),
        }
    )

    return PortfolioSnapshot(
        source="hyperliquid",
        account_label=account.label,
        cash=cash,
        holdings=[*perp_holdings, *spot_holdings],
        synced_at=datetime.now(),
        status=f"{account.label} synced {wallet_address[:6]}...{wallet_address[-4:]}",
        account_facts=account_facts,
    )


def _hyperliquid_account_facts(clearinghouse_state: dict[str, Any]) -> dict[str, object]:
    margin_summary = _margin_summary(clearinghouse_state)
    positions: dict[str, dict[str, object]] = {}
    for row in _dict_rows(clearinghouse_state.get("assetPositions")):
        position = row.get("position") if isinstance(row.get("position"), dict) else row
        coin = str(position.get("coin") or "").strip().upper()
        if not coin:
            continue
        leverage = position.get("leverage")
        leverage_row = leverage if isinstance(leverage, dict) else {}
        positions[coin] = {
            "entry_price": _to_float(position.get("entryPx")),
            "liquidation_price": _to_float(position.get("liquidationPx")),
            "margin_used": _to_float(position.get("marginUsed")),
            "return_on_equity": _to_float(position.get("returnOnEquity")),
            "signed_size": _to_float(position.get("szi") or position.get("size")),
            "margin_mode": str(leverage_row.get("type") or "").strip(),
            "leverage": _to_float(leverage_row.get("value")),
        }
    return {
        "perp_equity": _perp_account_value(clearinghouse_state),
        "available": _to_float(clearinghouse_state.get("withdrawable")),
        "margin_used": _to_float(margin_summary.get("totalMarginUsed")),
        "positions": positions,
    }


def _margin_summary(clearinghouse_state: dict[str, Any]) -> dict[str, Any]:
    margin_summary = clearinghouse_state.get("marginSummary")
    if isinstance(margin_summary, dict):
        return margin_summary
    cross_margin_summary = clearinghouse_state.get("crossMarginSummary")
    return cross_margin_summary if isinstance(cross_margin_summary, dict) else {}


def _hype_market_facts(
    all_mids: dict[str, Any],
    spot_meta_and_asset_ctxs: list[Any],
) -> dict[str, object]:
    if len(spot_meta_and_asset_ctxs) < 2:
        return {"status": "unavailable"}
    meta = spot_meta_and_asset_ctxs[0]
    contexts = spot_meta_and_asset_ctxs[1]
    if not isinstance(meta, dict) or not isinstance(contexts, list):
        return {"status": "unavailable"}

    tokens = _dict_rows(meta.get("tokens"))
    hype_token = next(
        (
            token
            for token in tokens
            if str(token.get("name") or "").strip().upper() == HYPE_SYMBOL
        ),
        None,
    )
    if hype_token is None:
        return {"status": "unavailable"}

    hype_index = _to_int(hype_token.get("index"))
    if hype_index is None:
        return {"status": "unavailable"}
    usdc_index = next(
        (
            _to_int(token.get("index"))
            for token in tokens
            if str(token.get("name") or "").strip().upper() == "USDC"
        ),
        0,
    )
    universe = _dict_rows(meta.get("universe"))
    hype_pair = next(
        (
            pair
            for pair in universe
            if _pair_token_indices(pair) == (hype_index, usdc_index)
        ),
        None,
    )
    if hype_pair is None:
        return {"status": "unavailable"}

    coin = str(hype_pair.get("name") or "").strip()
    context = next(
        (
            row
            for row in _dict_rows(contexts)
            if str(row.get("coin") or "").strip() == coin
        ),
        {},
    )
    price = _first_number(context, ("midPx", "markPx"))
    if price is None:
        price = _to_float(all_mids.get(coin))
    previous_price = _to_float(context.get("prevDayPx"))
    change_percent = None
    if price is not None and previous_price is not None and previous_price > 0:
        change_percent = ((price - previous_price) / previous_price) * 100.0
    return {
        "status": "current" if price is not None else "unavailable",
        "coin": coin,
        "price": price,
        "previous_price": previous_price,
        "change_percent_24h": change_percent,
        "volume_24h": _to_float(context.get("dayNtlVlm")),
        "circulating_supply": _to_float(context.get("circulatingSupply")),
        "closes_24h": [],
        "chart_status": "unavailable",
    }


def _hype_candle_closes(
    client: HyperliquidInfoClient,
    coin: str,
    *,
    end_time_ms: int | None = None,
) -> list[float]:
    end_time = end_time_ms if end_time_ms is not None else int(time.time() * 1000)
    response = client.post_info(
        {
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": HYPE_CANDLE_INTERVAL,
                "startTime": end_time - HYPE_CANDLE_LOOKBACK_MILLISECONDS,
                "endTime": end_time,
            },
        }
    )
    if not isinstance(response, list):
        raise RuntimeError("Hyperliquid candleSnapshot returned an unexpected response.")
    closes: list[float] = []
    for candle in response:
        if not isinstance(candle, dict):
            continue
        close = _to_float(candle.get("c"))
        if close is not None and math.isfinite(close):
            closes.append(close)
    return closes


def _pair_token_indices(pair: dict[str, Any]) -> tuple[int | None, int | None]:
    values = pair.get("tokens")
    if not isinstance(values, list) or len(values) < 2:
        return None, None
    return _to_int(values[0]), _to_int(values[1])


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


def _to_int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _hex_quantity(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None
