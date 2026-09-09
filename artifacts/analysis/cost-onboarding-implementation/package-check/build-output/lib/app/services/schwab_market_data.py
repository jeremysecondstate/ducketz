from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from app.models.market_data import MarketBar, MarketQuote
from app.services.schwab import SchwabSession
from app.services.market_fetch_specs import SchwabPriceHistorySpec, schwab_price_history_specs
from app.services.schwab_retry import call_with_persistent_schwab_retry


class SchwabMarketDataProvider:
    source = "schwab"

    def __init__(self, session: SchwabSession | None = None) -> None:
        self.session = session or SchwabSession()

    def fetch_quote(self, symbol: str) -> tuple[MarketQuote, Any]:
        clean_symbol = _symbol(symbol)
        payload = self.session.get_equity_quote(clean_symbol)
        return _market_quote(clean_symbol, payload), payload

    def fetch_quotes(
        self,
        symbols: Iterable[str],
    ) -> dict[str, tuple[MarketQuote, Any]]:
        clean_symbols = tuple(dict.fromkeys(_symbol(symbol) for symbol in symbols))
        if not clean_symbols:
            raise ValueError("At least one symbol is required.")
        payloads = self.session.get_equity_quotes(clean_symbols)
        fetched_at = datetime.now(timezone.utc)
        return {
            symbol: (
                _market_quote(symbol, payload, fetched_at=fetched_at),
                payload,
            )
            for symbol in clean_symbols
            if (payload := payloads.get(symbol)) is not None
        }

    def fetch_bars(self, symbol: str, *, timeframe: str = "1d") -> tuple[list[MarketBar], Any]:
        clean_symbol = _symbol(symbol)
        request = _schwab_history_request(timeframe)
        payload = call_with_persistent_schwab_retry(
            lambda: self.session.get_price_history(clean_symbol, **request),
            operation_name=f"{clean_symbol} pricehistory {timeframe}",
        )
        return _bars_from_schwab_payload(clean_symbol, timeframe, payload), payload

    def fetch_bars_for_spec(
        self,
        symbol: str,
        spec: SchwabPriceHistorySpec,
        *,
        start_datetime: datetime | None = None,
        end_datetime: datetime | None = None,
    ) -> tuple[list[MarketBar], Any]:
        clean_symbol = _symbol(symbol)
        request: dict[str, Any] = {
            "period_type": spec.period_type,
            "period": spec.period,
            "frequency_type": spec.frequency_type,
            "frequency": spec.frequency,
            "need_extended_hours_data": spec.need_extended_hours_data,
        }
        if start_datetime is not None:
            request["start_datetime"] = start_datetime
        if end_datetime is not None:
            request["end_datetime"] = end_datetime
        payload = call_with_persistent_schwab_retry(
            lambda: self.session.get_price_history(clean_symbol, **request),
            operation_name=f"{clean_symbol} pricehistory {spec.key}",
        )
        return _bars_from_schwab_payload(clean_symbol, spec.key, payload), payload

    def fetch_all_bars(self, symbol: str) -> list[tuple[SchwabPriceHistorySpec, list[MarketBar], Any, Exception | None]]:
        results: list[tuple[SchwabPriceHistorySpec, list[MarketBar], Any, Exception | None]] = []

        for spec in schwab_price_history_specs():
            try:
                bars, raw_payload = self.fetch_bars_for_spec(symbol, spec)
                results.append((spec, bars, raw_payload, None))
            except Exception as exc:
                results.append((spec, [], None, exc))

        return results


def _schwab_history_request(timeframe: str) -> dict[str, Any]:
    if timeframe == "1d":
        return {
            "period_type": "year",
            "period": 1,
            "frequency_type": "daily",
            "frequency": 1,
            "need_extended_hours_data": False,
        }

    if timeframe == "1m":
        return {
            "period_type": "day",
            "period": 1,
            "frequency_type": "minute",
            "frequency": 1,
            "need_extended_hours_data": True,
        }

    if timeframe == "5m":
        return {
            "period_type": "day",
            "period": 5,
            "frequency_type": "minute",
            "frequency": 5,
            "need_extended_hours_data": True,
        }

    if timeframe == "30m":
        return {
            "period_type": "day",
            "period": 10,
            "frequency_type": "minute",
            "frequency": 30,
            "need_extended_hours_data": True,
        }

    raise ValueError("Unsupported Schwab timeframe. Use one of: 1d, 1m, 5m, 30m.")


def _bars_from_schwab_payload(symbol: str, timeframe: str, payload: Any) -> list[MarketBar]:
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected Schwab price-history response.")

    raw_candles = payload.get("candles") or []
    if not isinstance(raw_candles, list):
        raise RuntimeError("Unexpected Schwab price-history response: missing candles list.")

    bars: list[MarketBar] = []
    for row in raw_candles:
        if not isinstance(row, dict):
            continue

        try:
            timestamp = datetime.fromtimestamp(int(row["datetime"]) / 1000, tz=timezone.utc)
            bars.append(
                MarketBar(
                    symbol=symbol,
                    source="schwab",
                    timeframe=timeframe,
                    timestamp=timestamp,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume") or 0),
                )
            )
        except (KeyError, TypeError, ValueError, OSError):
            continue

    # Schwab can repeat a candle timestamp in one price-history response and
    # place a revised candle later in provider order. Preserve that revision
    # order while normalizing to the one-bar-per-timestamp contract.
    bars_by_timestamp = {bar.timestamp: bar for bar in bars}
    return sorted(bars_by_timestamp.values(), key=lambda bar: bar.timestamp)


def _symbol(value: str) -> str:
    cleaned = value.strip().upper()
    if not cleaned:
        raise ValueError("Symbol is required.")
    return cleaned


def _market_quote(
    symbol: str,
    payload: dict[str, Any],
    *,
    fetched_at: datetime | None = None,
) -> MarketQuote:
    return MarketQuote(
        symbol=symbol,
        source="schwab",
        fetched_at=fetched_at or datetime.now(timezone.utc),
        quote_event_at=_quote_event_at(payload),
        bid=_first_number(payload, ("bidPrice", "bid")),
        ask=_first_number(payload, ("askPrice", "ask")),
        last=_first_number(
            payload,
            ("lastPrice", "last", "regularMarketLastPrice"),
        ),
        mark=_first_number(payload, ("mark", "markPrice")),
        volume=_first_number(payload, ("totalVolume", "volume")),
    )


def _first_number(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _to_float(row.get(key))
        if value is not None:
            return value
    return None


def _to_float(value: Any) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def _quote_event_at(payload: dict[str, Any]) -> datetime | None:
    for key in (
        "quoteTimeInLong",
        "quoteTime",
        "bidTime",
        "askTime",
        "lastTradeTime",
        "tradeTimeInLong",
        "tradeTime",
    ):
        parsed = _provider_timestamp(payload.get(key))
        if parsed is not None:
            return parsed
    return None


def _provider_timestamp(value: Any) -> datetime | None:
    number = _to_float(value)
    if number is not None:
        try:
            seconds = number / 1_000.0 if abs(number) > 100_000_000_000 else number
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
