"""Public Hyperliquid quotes and conservative visible-depth paper fills.

No account configuration, private keys, signing SDK, or exchange action endpoint
is imported. Every request is an allowlisted public ``/info`` operation.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
import math
import re
import time
from urllib.parse import urlsplit

import requests


DEFAULT_INFO_URL = "https://api.hyperliquid.xyz/info"
MAX_BOOK_AGE_SECONDS = 45.0
MAX_FUTURE_BOOK_SECONDS = 5.0
SPOT_BASES = {"BTC": "UBTC", "ETH": "UETH", "ZEC": "UZEC"}
_SYMBOL = re.compile(r"[A-Z0-9]{1,20}")
_BOOK_COIN = re.compile(r"(?:[A-Z0-9]{1,20}|@[0-9]+|PURR/USDC)")
_PUBLIC_FIELDS = {
    "metaAndAssetCtxs": {"type"},
    "spotMetaAndAssetCtxs": {"type"},
    "l2Book": {"type", "coin"},
    "fundingHistory": {"type", "coin", "startTime", "endTime"},
}


def _number(value, name: str, *, positive=False, nonnegative=False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number.")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a finite number.") from None
    if not math.isfinite(result) or (positive and result <= 0) or (nonnegative and result < 0):
        raise ValueError(f"{name} has an invalid finite numeric value.")
    return result


def _size_decimals(value) -> int:
    if type(value) is not int or not 0 <= value <= 8:
        raise ValueError("szDecimals must be an integer between zero and eight.")
    return value


def _utc(stamp: float) -> str:
    return datetime.fromtimestamp(_number(stamp, "time"), timezone.utc).isoformat()


def _timestamp(value, name: str) -> float:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a timezone-aware ISO timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.timestamp()
    except (ValueError, OverflowError):
        raise ValueError(f"{name} must be a timezone-aware ISO timestamp.") from None


def _book_time_status(book_time: float, now: float) -> str | None:
    age = now - book_time
    if age > MAX_BOOK_AGE_SECONDS:
        return "stale_book"
    if age < -MAX_FUTURE_BOOK_SECONDS:
        return "future_book"
    return None


def _levels(rows, *, side: str, dictionaries: bool) -> list[list[float]]:
    if not isinstance(rows, list):
        raise ValueError(f"{side} levels must be an array.")
    result = []
    for row in rows:
        if dictionaries:
            if not isinstance(row, dict) or "px" not in row or "sz" not in row:
                raise ValueError("Every book level requires px and sz.")
            price, quantity = row["px"], row["sz"]
        else:
            if not isinstance(row, (list, tuple)) or len(row) != 2:
                raise ValueError("Every normalized book level requires price and quantity.")
            price, quantity = row
        result.append([
            _number(price, "book price", positive=True),
            _number(quantity, "book size", positive=True),
        ])
    # A malformed order is normalized explicitly, never filled from the wrong
    # end of the book. Duplicate price levels are combined before allocation.
    by_price = {}
    for price, quantity in result:
        by_price[price] = _number(by_price.get(price, 0.0) + quantity, "combined book size", positive=True)
    return [[price, by_price[price]] for price in sorted(by_price, reverse=side == "bids")]


def _uncrossed(bids, asks):
    if bids and asks and bids[0][0] >= asks[0][0]:
        raise ValueError("Book is crossed or locked; no executable quote is available.")


class PublicPaperMarket:
    """Small public-data client; individual book requests have no shared session."""

    def __init__(self, info_url=DEFAULT_INFO_URL, *, timeout_seconds=10,
                 max_workers=4, clock=time.time, post=None):
        parsed = urlsplit(info_url)
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname
                or parsed.path.rstrip("/") != "/info" or parsed.query or parsed.fragment
                or parsed.username or parsed.password):
            raise ValueError("Use an absolute public HTTP(S) /info endpoint without credentials.")
        self.info_url = info_url
        self.timeout_seconds = _number(timeout_seconds, "timeout_seconds", positive=True)
        if type(max_workers) is not int or not 1 <= max_workers <= 4:
            raise ValueError("max_workers must be an integer between one and four.")
        self.max_workers = max_workers
        self.clock = clock
        # requests.post creates and closes its own Session for each request.
        # Caller-injected transports used with parallel workers must be safe
        # for concurrent calls; no headers/cookies/session state are mutated.
        self._post = requests.post if post is None else post

    def post_info(self, payload: dict):
        if not isinstance(payload, dict):
            raise ValueError("Public info payload must be an object.")
        operation = payload.get("type")
        if not isinstance(operation, str) or operation not in _PUBLIC_FIELDS:
            raise ValueError("This paper client only permits public market-data operations.")
        if set(payload) != _PUBLIC_FIELDS[operation]:
            raise ValueError(f"Unexpected or missing fields for public {operation} request.")
        if operation == "l2Book":
            coin = payload["coin"]
            if not isinstance(coin, str) or _BOOK_COIN.fullmatch(coin) is None:
                raise ValueError("Invalid public book coin.")
        if operation == "fundingHistory":
            if not isinstance(payload["coin"], str) or _SYMBOL.fullmatch(payload["coin"]) is None:
                raise ValueError("Funding history requires a simple perpetual coin symbol.")
            for name in ("startTime", "endTime"):
                if type(payload[name]) is not int or payload[name] < 0:
                    raise ValueError("Funding timestamps must be nonnegative integer milliseconds.")
            if payload["endTime"] < payload["startTime"]:
                raise ValueError("Funding endTime must not precede startTime.")
        for attempt in range(3):
            try:
                response = self._post(
                    self.info_url, json=dict(payload), headers={"Content-Type": "application/json"},
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                return response.json()
            except requests.RequestException as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                transient = status is None or status == 429 or status >= 500
                if not transient or attempt == 2:
                    raise
                time.sleep(0.1 * 2 ** attempt)
        raise RuntimeError("Public request retry loop exhausted.")

    def _perpetual_markets(self, symbols, payload):
        if (not isinstance(payload, list) or len(payload) != 2 or not isinstance(payload[0], dict)
                or not isinstance(payload[0].get("universe"), list) or not isinstance(payload[1], list)
                or len(payload[0]["universe"]) != len(payload[1])):
            raise ValueError("Perpetual metadata and contexts must align.")
        markets, errors = {}, {}
        entries = {}
        for metadata, context in zip(payload[0]["universe"], payload[1]):
            if isinstance(metadata, dict) and isinstance(metadata.get("name"), str):
                entries.setdefault(metadata["name"], []).append((metadata, context))
        for coin in symbols:
            key = f"perp:{coin}"
            try:
                matches = entries.get(coin, [])
                if len(matches) != 1:
                    raise ValueError("Perpetual market is missing or ambiguous.")
                metadata, context = matches[0]
                if metadata.get("isDelisted") or not isinstance(context, dict):
                    raise ValueError("Perpetual market is delisted or unavailable.")
                market = {
                    "coin": coin, "kind": "perp", "wire_coin": coin,
                    "sz_decimals": _size_decimals(metadata.get("szDecimals")),
                    "mark": _number(context.get("markPx"), "perpetual mark", positive=True),
                    "oracle": _number(context.get("oraclePx"), "perpetual oracle", positive=True),
                    "funding_rate": _number(context.get("funding"), "funding rate"),
                }
                if "maxLeverage" in metadata:
                    market["max_leverage"] = _number(metadata["maxLeverage"], "maximum leverage", positive=True)
                if "marginTableId" in metadata:
                    market["margin_table_id"] = metadata["marginTableId"]
                markets[key] = market
            except (ValueError, TypeError) as exc:
                errors[key] = str(exc)
        return markets, errors

    def _spot_markets(self, symbols, payload):
        if (not isinstance(payload, list) or len(payload) != 2 or not isinstance(payload[0], dict)
                or not isinstance(payload[0].get("tokens"), list)
                or not isinstance(payload[0].get("universe"), list) or not isinstance(payload[1], list)):
            raise ValueError("Spot metadata and asset contexts must be available.")
        metadata, contexts = payload
        tokens = {}
        for token in metadata["tokens"]:
            if not isinstance(token, dict) or type(token.get("index")) is not int:
                raise ValueError("Spot token index is missing or malformed.")
            if token["index"] in tokens:
                raise ValueError("Spot token indices are ambiguous.")
            tokens[token["index"]] = token
        markets, errors = {}, {}
        for coin in symbols:
            key = f"spot:{coin}"
            try:
                base_name = SPOT_BASES.get(coin, coin)
                matches = []
                for pair in metadata["universe"]:
                    if not isinstance(pair, dict) or not isinstance(pair.get("tokens"), list) or len(pair["tokens"]) != 2:
                        continue
                    base, quote = (tokens.get(index) for index in pair["tokens"])
                    if base and quote and base.get("name") == base_name and quote.get("name") == "USDC":
                        matches.append((pair, base, quote))
                if len(matches) != 1:
                    raise ValueError("Exact base/USDC spot market is missing or ambiguous.")
                pair, base, quote = matches[0]
                if type(pair.get("index")) is not int or pair["index"] < 0 or pair.get("isDelisted"):
                    raise ValueError("Spot pair is delisted or has no valid pair index.")
                wire_coin = "PURR/USDC" if base_name == "PURR" else f"@{pair['index']}"
                matching_contexts = [context for context in contexts if isinstance(context, dict)
                                     and context.get("coin") == wire_coin]
                if len(matching_contexts) != 1:
                    raise ValueError("Exact spot asset context is missing or ambiguous.")
                context = matching_contexts[0]
                markets[key] = {
                    "coin": coin, "kind": "spot", "wire_coin": wire_coin,
                    "base": base_name, "quote": "USDC", "base_token_index": base["index"],
                    "quote_token_index": quote["index"], "spot_pair_index": pair["index"],
                    "sz_decimals": _size_decimals(base.get("szDecimals")),
                    "mark": _number(context.get("markPx"), "spot mark", positive=True),
                    "oracle": None, "funding_rate": 0.0,
                }
            except (ValueError, TypeError) as exc:
                errors[key] = str(exc)
        return markets, errors

    def _book(self, market):
        payload = self.post_info({"type": "l2Book", "coin": market["wire_coin"]})
        received = _number(self.clock(), "received time")
        if (not isinstance(payload, dict) or payload.get("coin") != market["wire_coin"]
                or not isinstance(payload.get("levels"), list) or len(payload["levels"]) != 2):
            raise ValueError("Book identity or shape does not match the requested market.")
        stamp_ms = payload.get("time")
        if type(stamp_ms) is not int or stamp_ms < 0:
            raise ValueError("Book time must be nonnegative integer milliseconds.")
        stamp = stamp_ms / 1000
        if issue := _book_time_status(stamp, received):
            raise ValueError(issue)
        bids = _levels(payload["levels"][0], side="bids", dictionaries=True)
        asks = _levels(payload["levels"][1], side="asks", dictionaries=True)
        _uncrossed(bids, asks)
        if not bids and not asks:
            raise ValueError("Book has no visible executable levels.")
        return {**market, "bids": bids, "asks": asks, "book_time_utc": _utc(stamp),
                "received_at_utc": _utc(received)}

    def snapshot(self, symbols) -> dict:
        if not isinstance(symbols, (list, tuple)) or not symbols:
            raise ValueError("symbols must be a nonempty list or tuple.")
        if any(not isinstance(coin, str) or _SYMBOL.fullmatch(coin) is None for coin in symbols):
            raise ValueError("Use simple uppercase perpetual coin symbols.")
        if len(set(symbols)) != len(symbols):
            raise ValueError("Snapshot symbols must be distinct.")
        markets, errors = {}, {}
        for kind, operation, parser in (
            ("perp", "metaAndAssetCtxs", self._perpetual_markets),
            ("spot", "spotMetaAndAssetCtxs", self._spot_markets),
        ):
            try:
                parsed, issues = parser(symbols, self.post_info({"type": operation}))
                markets.update(parsed)
                errors.update(issues)
            except (requests.RequestException, ValueError, TypeError) as exc:
                errors.update({f"{kind}:{coin}": str(exc) for coin in symbols})
        ready = {}
        with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="hyperliquid-paper-book") as pool:
            jobs = {pool.submit(self._book, market): key for key, market in markets.items()}
            for job in as_completed(jobs):
                key = jobs[job]
                try:
                    ready[key] = job.result()
                except (requests.RequestException, ValueError, TypeError) as exc:
                    errors[key] = str(exc)
        observed = _number(self.clock(), "snapshot observation time")
        for key, market in tuple(ready.items()):
            issue = _book_time_status(_timestamp(market["book_time_utc"], "book_time_utc"), observed)
            if issue:
                errors[key] = issue
                del ready[key]
        return {"observed_at_utc": _utc(observed), "markets": dict(sorted(ready.items())),
                "errors": dict(sorted(errors.items()))}

    def funding_history(self, coin: str, start_ms: int, end_ms: int) -> list[dict]:
        payload = self.post_info({"type": "fundingHistory", "coin": coin,
                                  "startTime": start_ms, "endTime": end_ms})
        if not isinstance(payload, list):
            raise ValueError("Funding history must be an array.")
        by_time = {}
        for row in payload:
            if not isinstance(row, dict) or row.get("coin") != coin:
                raise ValueError("Funding record identity does not match the requested coin.")
            stamp = row.get("time")
            if type(stamp) is not int or not start_ms <= stamp <= end_ms:
                raise ValueError("Funding record time is outside the requested interval.")
            record = {"coin": coin, "time": stamp, "time_utc": _utc(stamp / 1000),
                      "funding_rate": _number(row.get("fundingRate"), "funding rate")}
            if "premium" in row:
                record["premium"] = _number(row["premium"], "funding premium")
            if stamp in by_time and by_time[stamp] != record:
                raise ValueError("Conflicting funding records share a settlement timestamp.")
            by_time[stamp] = record
        return [by_time[stamp] for stamp in sorted(by_time)]


def simulate_fill(market: dict, signed_quantity: float, extra_slippage_bps=2,
                  min_notional=10, *, now=None) -> dict:
    """Consume current visible depth with adverse slippage and lot-size flooring.

    Returned quantity and unfilled_quantity use the requested sign. A missing,
    stale, crossed, or below-minimum executable quote produces a zero fill; no
    historic candle or mark price is substituted for a book price.
    """
    if not isinstance(market, dict):
        raise ValueError("market must be a normalized market object.")
    requested = _number(signed_quantity, "signed_quantity")
    slippage = _number(extra_slippage_bps, "extra_slippage_bps", nonnegative=True)
    minimum = _number(min_notional, "min_notional", nonnegative=True)
    if slippage >= 10000:
        raise ValueError("extra_slippage_bps must be below 10000.")
    current = _number(time.time() if now is None else now, "execution time")
    decimals = _size_decimals(market.get("sz_decimals"))
    result = {
        "requested_quantity": requested, "rounded_quantity": 0.0, "quantity": 0.0,
        "unfilled_quantity": requested, "price": None, "notional": 0.0,
        "book_time_utc": market.get("book_time_utc"), "executed_at_utc": _utc(current),
        "coin": market.get("coin"), "kind": market.get("kind"), "wire_coin": market.get("wire_coin"),
        "extra_slippage_bps": slippage, "levels_consumed": 0, "status": "unfilled",
        "consumed_levels": [],
    }

    def no_fill(reason):
        return {**result, "reason": reason}

    if requested == 0:
        return no_fill("zero_quantity")
    try:
        stamp = _timestamp(market.get("book_time_utc"), "book_time_utc")
        bids = _levels(market.get("bids"), side="bids", dictionaries=False)
        asks = _levels(market.get("asks"), side="asks", dictionaries=False)
        _uncrossed(bids, asks)
    except ValueError as exc:
        return no_fill(str(exc))
    if issue := _book_time_status(stamp, current):
        return no_fill(issue)
    side = Decimal(1 if requested > 0 else -1)
    quantum = Decimal(1).scaleb(-decimals)
    desired = Decimal(str(abs(requested))).quantize(quantum, rounding=ROUND_DOWN)
    result["rounded_quantity"] = float(side * desired)
    if desired == 0:
        return no_fill("below_size_precision")
    remaining, filled, cost, levels = desired, Decimal(0), Decimal(0), 0
    consumed = []
    for price, available in (asks if requested > 0 else bids):
        quantity = min(remaining, Decimal(str(available))).quantize(quantum, rounding=ROUND_DOWN)
        if quantity <= 0:
            continue
        cost += quantity * Decimal(str(price))
        filled += quantity
        remaining -= quantity
        levels += 1
        consumed.append({"price": price, "quantity": float(quantity)})
        if remaining == 0:
            break
    if filled == 0:
        return no_fill("no_executable_depth")
    vwap = cost / filled
    price = vwap * (Decimal(1) + side * Decimal(str(slippage)) / Decimal(10000))
    notional = filled * price
    if notional < Decimal(str(minimum)):
        return no_fill("below_min_notional")
    signed_fill = side * filled
    return {
        **result, "quantity": _number(signed_fill, "fill quantity"),
        "price": _number(price, "fill price", positive=True),
        "notional": _number(notional, "fill notional", positive=True),
        "unfilled_quantity": float(Decimal(str(requested)) - signed_fill),
        "raw_book_vwap": _number(vwap, "book VWAP", positive=True),
        "levels_consumed": levels, "status": "filled" if remaining == 0 else "partial",
        "consumed_levels": consumed,
        "reason": None if remaining == 0 else "insufficient_visible_depth",
    }


def consume_fill(market: dict, fill: dict) -> dict:
    """Return a copy with the fill's raw book levels reserved for this cycle.

    Pass this returned market to the next same-book simulated order, including
    orders in another paper account. Neither input is mutated. Extra slippage
    changes the simulated execution price, not the raw level price to consume.
    Only actually executed fills should be consumed; rejected ledger actions
    leave liquidity untouched. A new public snapshot replaces these reserves.
    """
    if not isinstance(market, dict) or not isinstance(fill, dict):
        raise ValueError("market and fill must be objects.")
    quantity = _number(fill.get("quantity"), "fill quantity")
    result = deepcopy(market)
    if quantity == 0:
        return result
    for name in ("coin", "kind", "wire_coin", "book_time_utc"):
        if market.get(name) is None or fill.get(name) != market[name]:
            raise ValueError("Fill identity and book timestamp must match the market being consumed.")
    consumed = fill.get("consumed_levels")
    if not isinstance(consumed, list) or not consumed:
        raise ValueError("A nonzero fill must include its consumed book levels.")
    demands = {}
    total = Decimal(0)
    for level in consumed:
        if not isinstance(level, dict):
            raise ValueError("Consumed levels must state price and positive quantity.")
        price = _number(level.get("price"), "consumed price", positive=True)
        size = Decimal(str(_number(level.get("quantity"), "consumed quantity", positive=True)))
        demands[price] = demands.get(price, Decimal(0)) + size
        total += size
    if total != Decimal(str(abs(quantity))):
        raise ValueError("Consumed quantities must equal the executed fill quantity.")
    side = "asks" if quantity > 0 else "bids"
    levels = _levels(market.get(side), side=side, dictionaries=False)
    remaining = []
    for price, size in levels:
        available = Decimal(str(size))
        used = demands.pop(price, Decimal(0))
        if used > available:
            raise ValueError("The fill exceeds the remaining visible quantity at this price.")
        leftover = available - used
        if leftover > 0:
            remaining.append([price, float(leftover)])
    if demands:
        raise ValueError("A consumed level does not exist in this book snapshot.")
    result[side] = remaining
    return result
