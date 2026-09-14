"""Public market context shared once per portfolio sync, independent of accounts."""
from __future__ import annotations

import math
import time
from datetime import datetime

DEFAULT_MARKETS = ("HYPE", "BTC", "ETH", "ZEC")
DISPLAY_ALIASES = {"UBTC": "BTC", "UETH": "ETH", "UZEC": "ZEC"}


def display_asset(symbol: str) -> str:
    base = symbol.upper().split("/")[0].split("-SPOT")[0].split("-PERP")[0]
    return DISPLAY_ALIASES.get(base, base)


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def spot_catalog(payload) -> dict[str, dict]:
    if not isinstance(payload, list) or len(payload) < 2 or not isinstance(payload[0], dict) or not isinstance(payload[1], list):
        return {}
    tokens = {t.get("index"): t.get("name") for t in payload[0].get("tokens", []) if isinstance(t, dict)}
    contexts = {c.get("coin"): c for c in payload[1] if isinstance(c, dict)}
    result = {}
    for pair in payload[0].get("universe", []):
        if not isinstance(pair, dict) or not isinstance(pair.get("index"), int):
            continue
        indices = pair.get("tokens", [])
        if not isinstance(indices, list) or len(indices) != 2 or tokens.get(indices[1]) != "USDC":
            continue
        base = tokens.get(indices[0])
        if not base:
            continue
        coin = "PURR/USDC" if base == "PURR" else f"@{pair['index']}"
        context = contexts.get(coin, {})
        result[display_asset(base)] = {
            "coin": coin, "pair": f"{base}/USDC", "base": base,
            "mid": number(context.get("midPx")),
        }
    return result


def perpetual_contexts(payload) -> dict[str, dict]:
    if not isinstance(payload, list) or len(payload) != 2 or not isinstance(payload[0], dict):
        raise ValueError("Perpetual market metadata unavailable.")
    universe, contexts = payload[0].get("universe", []), payload[1]
    if not isinstance(universe, list) or not isinstance(contexts, list) or len(universe) != len(contexts):
        raise ValueError("Perpetual market metadata and contexts do not align.")
    result = {}
    for market, context in zip(universe, contexts):
        if not isinstance(market, dict) or market.get("isDelisted") or not isinstance(context, dict):
            continue
        coin = str(market.get("name", ""))
        mark, previous = number(context.get("markPx")), number(context.get("prevDayPx"))
        if not coin:
            continue
        result[coin] = {
            "coin": coin, "price": mark, "previous_price": previous,
            "change_percent_24h": (mark / previous - 1) * 100 if mark is not None and previous and previous > 0 else None,
            "volume_24h": number(context.get("dayNtlVlm")),
            "status": "current" if mark is not None else "unavailable",
            "sz_decimals": market.get("szDecimals"),
        }
    return result


def market_watch(client, watchlist=DEFAULT_MARKETS) -> dict:
    stamp = datetime.now().isoformat(timespec="seconds")
    try:
        catalog = perpetual_contexts(client.post_info({"type": "metaAndAssetCtxs"}))
    except Exception as exc:
        return {"markets": {coin: {"status": "unavailable", "error": type(exc).__name__} for coin in watchlist}, "market_catalog": [], "market_timestamp": stamp}
    markets = {}
    end = int(time.time() * 1000)
    for coin in tuple(dict.fromkeys(watchlist))[:24]:
        facts = dict(catalog.get(coin, {"coin": coin, "status": "unavailable"}))
        facts["timestamp"] = stamp
        if coin in catalog:
            try:
                candles = client.post_info({"type": "candleSnapshot", "req": {"coin": coin, "interval": "15m", "startTime": end - 86_400_000, "endTime": end}})
                facts["closes_24h"] = [v for row in candles if isinstance(row, dict) and (v := number(row.get("c"))) is not None]
                facts["chart_status"] = "current" if facts["closes_24h"] else "unavailable"
            except Exception:
                facts["chart_status"] = "unavailable"
        markets[coin] = facts
    return {"markets": markets, "market_catalog": sorted(catalog), "market_timestamp": stamp}
