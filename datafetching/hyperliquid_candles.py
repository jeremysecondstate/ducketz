"""Public Hyperliquid OHLCV reads and a canonical, closed-candle table.

No wallet, private endpoint, or signing code is used here. ``candleSnapshot``
exposes only the latest 5,000 candles, so an older local archive must be retained
when merging a refresh. ``close_time`` is the *exclusive* candle boundary in UTC
(the exchange normally supplies ``T`` one millisecond before that boundary).
"""

from __future__ import annotations

import math
import time
from decimal import Decimal, InvalidOperation
from typing import Any

import numpy as np
import pandas as pd
import requests

DEFAULT_INFO_URL = "https://api.hyperliquid.xyz/info"
INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}
CANDLE_COLUMNS = (
    "timestamp", "close_time", "symbol", "interval", "open", "high", "low",
    "close", "volume", "trade_count",
)
_PRICE_COLUMNS = ("open", "high", "low", "close")
_MAX_ATTEMPTS = 4
_TIMEOUT_SECONDS = (5, 30)


class CandleDataError(ValueError):
    """The exchange response or local OHLCV table is malformed."""


class CandleFetchError(RuntimeError):
    """A bounded public candle request failed."""


def _interval_ms(interval: str) -> int:
    try:
        return INTERVAL_MS[interval]
    except KeyError as exc:
        raise CandleDataError(f"Unsupported fixed candle interval: {interval!r}") from exc


def _integer(value: Any, field: str) -> int:
    if isinstance(value, (bool, np.bool_)):
        raise CandleDataError(f"{field} must be a nonnegative integer")
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number != number.to_integral_value():
            raise ValueError
        integer = int(number)
        if integer > np.iinfo(np.int64).max:
            raise ValueError
        return integer
    except (ValueError, InvalidOperation, OverflowError) as exc:
        raise CandleDataError(f"{field} must be a nonnegative int64 integer") from exc


def _number(value: Any, field: str) -> float:
    if isinstance(value, (bool, np.bool_)):
        raise CandleDataError(f"{field} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CandleDataError(f"{field} must be a finite number") from exc
    if not math.isfinite(number):
        raise CandleDataError(f"{field} must be a finite number")
    return number


def _retry_delay(attempt: int, response: requests.Response | None = None) -> float:
    delay = float(2 ** attempt)
    if response is not None:
        try:
            supplied = float(response.headers.get("Retry-After", ""))
            if math.isfinite(supplied) and supplied >= 0:
                delay = supplied
        except (TypeError, ValueError):
            pass
    return min(delay, 10.0)


def fetch_candles(
    *, coin: str = "BTC", interval: str = "15m", start_ms: int, end_ms: int,
    info_url: str = DEFAULT_INFO_URL, session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    """Fetch one public snapshot; do not imply pagination recovers older history.

    At most four attempts are made. Connection/timeout failures, HTTP 429, and
    server errors retry with a bounded delay. Caller-supplied sessions remain
    open. Authentication is neither required nor added.
    """
    _interval_ms(interval)
    if not isinstance(coin, str) or not coin.strip():
        raise CandleDataError("coin must be a nonempty exchange symbol")
    start_ms = _integer(start_ms, "start_ms")
    end_ms = _integer(end_ms, "end_ms")
    if end_ms < start_ms:
        raise CandleDataError("end_ms must be greater than or equal to start_ms")
    payload = {"type": "candleSnapshot", "req": {
        "coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms,
    }}
    client = session if session is not None else requests.Session()
    try:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = client.post(info_url, json=payload, timeout=_TIMEOUT_SECONDS)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == _MAX_ATTEMPTS - 1:
                    raise CandleFetchError("Public candle request failed after 4 attempts") from exc
                time.sleep(_retry_delay(attempt))
                continue
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt < _MAX_ATTEMPTS - 1:
                    delay = _retry_delay(attempt, response)
                    response.close()
                    time.sleep(delay)
                    continue
            try:
                response.raise_for_status()
                rows = response.json()
            except requests.HTTPError as exc:
                raise CandleFetchError(
                    f"Public candle request returned HTTP {response.status_code}"
                ) from exc
            except ValueError as exc:
                raise CandleDataError("Public candle response is not JSON") from exc
            finally:
                response.close()
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise CandleDataError("Public candle response must be a list of candle objects")
            return rows
    finally:
        if session is None:
            client.close()
    raise CandleFetchError("Public candle request exhausted its retry budget")


def _empty_candles() -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": pd.Series(dtype="datetime64[ns, UTC]"),
        "close_time": pd.Series(dtype="datetime64[ns, UTC]"),
        "symbol": pd.Series(dtype="object"), "interval": pd.Series(dtype="object"),
        **{column: pd.Series(dtype="float64") for column in (*_PRICE_COLUMNS, "volume")},
        "trade_count": pd.Series(dtype="int64"),
    })


def normalize_candles(
    rows: list[dict[str, Any]], *, coin: str, interval: str, as_of_ms: int,
) -> pd.DataFrame:
    """Validate, sort and deduplicate rows, excluding candles still in progress.

    An opening time ``t`` is eligible exactly when ``t + interval <= as_of_ms``.
    Gaps remain gaps; prices, volumes, and missing candles are never fabricated.
    The last occurrence of a duplicate timestamp wins.
    """
    duration = _interval_ms(interval)
    as_of_ms = _integer(as_of_ms, "as_of_ms")
    normalized = []
    for index, row in enumerate(rows):
        try:
            if not isinstance(row, dict):
                raise CandleDataError("candle must be an object")
            if row.get("s") != coin or row.get("i") != interval:
                raise CandleDataError(f"candle identity does not match {coin}/{interval}")
            opened = _integer(row["t"], "t")
            closed = _integer(row["T"], "T")
            if opened % duration:
                raise CandleDataError("t is not aligned with its candle interval")
            if closed not in (opened + duration - 1, opened + duration):
                raise CandleDataError("T does not match the candle interval boundary")
            prices = {name: _number(row[key], key) for name, key in
                      zip(_PRICE_COLUMNS, ("o", "h", "l", "c"))}
            if any(value <= 0 for value in prices.values()):
                raise CandleDataError("OHLC prices must be strictly positive")
            if not (prices["low"] <= min(prices["open"], prices["close"])
                    and prices["high"] >= max(prices["open"], prices["close"])):
                raise CandleDataError("OHLC high/low envelope is invalid")
            volume = _number(row["v"], "v")
            if volume < 0:
                raise CandleDataError("volume must be nonnegative")
            count = _integer(row["n"], "n")
            if opened + duration > as_of_ms:
                continue
            normalized.append({
                "timestamp": opened, "close_time": opened + duration,
                "symbol": coin, "interval": interval, **prices,
                "volume": volume, "trade_count": count,
            })
        except KeyError as exc:
            raise CandleDataError(f"Candle row {index} is missing field {exc.args[0]!r}") from exc
        except CandleDataError as exc:
            raise CandleDataError(f"Candle row {index}: {exc}") from exc
    if not normalized:
        return _empty_candles()
    frame = pd.DataFrame(normalized, columns=CANDLE_COLUMNS)
    for column in ("timestamp", "close_time"):
        try:
            frame[column] = pd.to_datetime(frame[column], unit="ms", utc=True).astype(
                "datetime64[ns, UTC]"
            )
        except (ValueError, OverflowError) as exc:
            raise CandleDataError(f"{column} is outside the supported UTC date range") from exc
    frame["trade_count"] = frame["trade_count"].astype("int64")
    return frame.drop_duplicates("timestamp", keep="last").sort_values(
        "timestamp", kind="stable"
    ).reset_index(drop=True)


def merge_candles(existing: pd.DataFrame, incoming: pd.DataFrame) -> pd.DataFrame:
    """Keep the full archive, replacing overlapping candles with fetched values."""
    frames = []
    for frame in (existing, incoming):
        if frame.empty:
            continue
        missing = set(CANDLE_COLUMNS).difference(frame.columns)
        if missing:
            raise CandleDataError(f"Candle table is missing columns: {sorted(missing)}")
        frames.append(frame.loc[:, list(CANDLE_COLUMNS)])
    if not frames:
        return _empty_candles()
    combined = pd.concat(frames, ignore_index=True)
    if combined["symbol"].nunique(dropna=False) != 1 or combined["interval"].nunique(dropna=False) != 1:
        raise CandleDataError("Cannot merge different symbols or candle intervals")
    if combined["timestamp"].isna().any():
        raise CandleDataError("Cannot merge candles with missing timestamps")
    return combined.drop_duplicates("timestamp", keep="last").sort_values(
        "timestamp", kind="stable"
    ).reset_index(drop=True)


def summarize_candles(frame: pd.DataFrame, interval: str) -> dict[str, Any]:
    """Report coverage and missing intervals without filling or blocking on gaps."""
    duration = pd.Timedelta(milliseconds=_interval_ms(interval))
    stamps = frame["timestamp"].sort_values().drop_duplicates()
    deltas = stamps.diff().dropna()
    gaps = deltas[deltas > duration]
    return {
        "rows": len(frame),
        "start_time": stamps.iloc[0].isoformat() if len(stamps) else None,
        "end_time": stamps.iloc[-1].isoformat() if len(stamps) else None,
        "latest_close_time": frame["close_time"].max().isoformat() if len(frame) else None,
        "gap_count": int(len(gaps)),
        "missing_candles": int(sum(int(delta // duration) - 1 for delta in gaps)),
        "duplicate_timestamps": int(frame["timestamp"].duplicated().sum()),
    }
