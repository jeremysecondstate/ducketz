from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from app.models.market_data import MarketQuote
from datafetching.ids import ID_COLUMN, add_readable_id, without_internal_identity_columns
from datafetching.layout import safe_token

QUOTE_LIQUIDITY_CALCULATION = "quote-liquidity"
QUOTE_LIQUIDITY_CALCULATION_VERSION = "1.0.0"
QUOTE_LIQUIDITY_SCHEMA_VERSION = "quote-liquidity-v1"
QUOTE_LIQUIDITY_QUALITY_POLICY_VERSION = "schwab-quote-quality-v1"
QUOTE_LIQUIDITY_MAX_STALENESS_SECONDS = 5 * 60
QUOTE_LIQUIDITY_MAX_CLOCK_SKEW_SECONDS = 5.0
QUOTE_LIQUIDITY_EXCHANGE_CALENDAR = "XNYS"

QUOTE_LIQUIDITY_COLUMNS = (
    "id",
    "symbol",
    "source",
    "quote_event_at",
    "fetched_at",
    "available_at",
    "calculation",
    "calculation_version",
    "schema_version",
    "quality_policy_version",
    "bid",
    "ask",
    "mid",
    "relative_bid_ask_spread",
    "quote_staleness_seconds",
    "quote_quality_pass",
)
_QUOTE_LIQUIDITY_KEY = ("symbol", "available_at")
_TEMPORAL_COLUMNS = ("quote_event_at", "fetched_at", "available_at")


class QuoteLiquidityQualityError(ValueError):
    """Raised when a quote cannot support a causal liquidity observation."""

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


def quote_liquidity_freshness_expected_at(value: object) -> bool:
    """Return whether the generic Schwab quote feed should be live at ``value``."""

    available_at = _utc_timestamp(value, field="fetched_at")
    try:
        import exchange_calendars as xcals
    except ImportError as exc:
        raise RuntimeError(
            "exchange-calendars is required for quote-liquidity session timing"
        ) from exc

    session_label = available_at.tz_convert("UTC").tz_localize(None).normalize()
    calendar = xcals.get_calendar(
        QUOTE_LIQUIDITY_EXCHANGE_CALENDAR,
        start=session_label - pd.Timedelta(days=14),
        end=session_label + pd.Timedelta(days=14),
    )
    local_receipt = available_at.tz_convert(calendar.tz)
    local_session_label = local_receipt.tz_localize(None).normalize()
    if local_session_label not in calendar.sessions:
        return False

    local_midnight = local_session_label.tz_localize(calendar.tz)
    regular_open = pd.Timestamp(
        calendar.session_open(local_session_label)
    ).tz_convert(calendar.tz)
    regular_close = pd.Timestamp(
        calendar.session_close(local_session_label)
    ).tz_convert(calendar.tz)
    standard_close = local_midnight + pd.Timedelta(hours=16)

    # Schwab's generic extended session is 07:00-20:00 ET on full market
    # days. It does not run on exchange holidays or scheduled early-close days.
    if regular_close < standard_close:
        return bool(regular_open <= local_receipt < regular_close)
    extended_open = local_midnight + pd.Timedelta(hours=7)
    extended_close = local_midnight + pd.Timedelta(hours=20)
    return bool(extended_open <= local_receipt < extended_close)


def calculate_quote_liquidity(
    quote: MarketQuote,
    *,
    max_staleness_seconds: float = QUOTE_LIQUIDITY_MAX_STALENESS_SECONDS,
    max_clock_skew_seconds: float = QUOTE_LIQUIDITY_MAX_CLOCK_SKEW_SECONDS,
) -> pd.DataFrame:
    symbol = str(quote.symbol or "").strip().upper()
    if not symbol:
        raise QuoteLiquidityQualityError("Quote-liquidity calculation requires a symbol")
    if str(quote.source or "").strip().lower() != "schwab":
        raise QuoteLiquidityQualityError("Quote-liquidity calculation requires a Schwab quote")
    if max_staleness_seconds < 0:
        raise ValueError("max_staleness_seconds must not be negative")
    if max_clock_skew_seconds < 0:
        raise ValueError("max_clock_skew_seconds must not be negative")

    fetched_at = _utc_timestamp(quote.fetched_at, field="fetched_at")
    quote_event_at = _utc_timestamp(
        quote.quote_event_at,
        field="quote_event_at",
        required=True,
    )
    clock_skew_seconds = (quote_event_at - fetched_at).total_seconds()
    if clock_skew_seconds > max_clock_skew_seconds:
        raise QuoteLiquidityQualityError(
            "Schwab quote event time exceeds local receipt by "
            f"{clock_skew_seconds:.3f}s (maximum allowed clock skew is "
            f"{max_clock_skew_seconds:.3f}s): quote_event_at="
            f"{quote_event_at.isoformat()}, fetched_at={fetched_at.isoformat()}",
            reason="clock_skew",
        )

    # Provider and local timestamps come from independent clocks. When their
    # small disagreement is within policy, use the later time as availability
    # so the persisted observation never appears available before its event.
    available_at = max(fetched_at, quote_event_at)

    staleness_seconds = (available_at - quote_event_at).total_seconds()
    if (
        staleness_seconds > max_staleness_seconds
        and quote_liquidity_freshness_expected_at(available_at)
    ):
        raise QuoteLiquidityQualityError(
            "Schwab quote is stale: "
            f"{staleness_seconds:.3f}s exceeds {max_staleness_seconds:.3f}s",
            reason="stale",
        )

    bid = _finite_number(quote.bid, field="bid")
    ask = _finite_number(quote.ask, field="ask")
    if bid <= 0 or ask <= 0:
        raise QuoteLiquidityQualityError("Schwab quote bid and ask must both be positive")
    if ask < bid:
        raise QuoteLiquidityQualityError("Schwab quote is crossed")
    if ask == bid:
        raise QuoteLiquidityQualityError("Schwab quote is locked")

    mid = (bid + ask) / 2.0
    if not math.isfinite(mid) or mid <= 0:
        raise QuoteLiquidityQualityError("Schwab quote has a non-positive midpoint")

    row = {
        "symbol": symbol,
        "source": "schwab",
        "quote_event_at": quote_event_at,
        "fetched_at": fetched_at,
        "available_at": available_at,
        "calculation": QUOTE_LIQUIDITY_CALCULATION,
        "calculation_version": QUOTE_LIQUIDITY_CALCULATION_VERSION,
        "schema_version": QUOTE_LIQUIDITY_SCHEMA_VERSION,
        "quality_policy_version": QUOTE_LIQUIDITY_QUALITY_POLICY_VERSION,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "relative_bid_ask_spread": (ask - bid) / mid,
        "quote_staleness_seconds": staleness_seconds,
        "quote_quality_pass": staleness_seconds <= max_staleness_seconds,
    }
    return pd.DataFrame([row])


def persist_quote_liquidity(
    datastore_root: Path,
    quote: MarketQuote,
    *,
    max_staleness_seconds: float = QUOTE_LIQUIDITY_MAX_STALENESS_SECONDS,
    max_clock_skew_seconds: float = QUOTE_LIQUIDITY_MAX_CLOCK_SKEW_SECONDS,
) -> Path:
    frame = calculate_quote_liquidity(
        quote,
        max_staleness_seconds=max_staleness_seconds,
        max_clock_skew_seconds=max_clock_skew_seconds,
    )
    available_at = pd.Timestamp(frame["available_at"].iloc[0])
    month = available_at.strftime("%Y-%m")
    path = (
        Path(datastore_root)
        / "stocks"
        / safe_token(str(frame["symbol"].iloc[0]))
        / "quotes"
        / "features"
        / QUOTE_LIQUIDITY_CALCULATION
        / "schwab"
        / f"{month}.parquet"
    )
    return write_quote_liquidity_parquet(path, frame)


def write_quote_liquidity_parquet(path: Path, frame: pd.DataFrame) -> Path:
    incoming = _prepare_frame(frame)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = (
        _prepare_frame(pd.read_parquet(path))
        if path.is_file()
        else pd.DataFrame(columns=QUOTE_LIQUIDITY_COLUMNS)
    )

    if incoming.duplicated(list(_QUOTE_LIQUIDITY_KEY), keep=False).any():
        raise ValueError("Quote-liquidity input contains duplicate receipt keys")
    if not existing.empty and existing.duplicated(
        list(_QUOTE_LIQUIDITY_KEY),
        keep=False,
    ).any():
        raise ValueError("Quote-liquidity history contains duplicate receipt keys")

    existing_by_key = {
        _key(row): row
        for row in existing.drop(columns=[ID_COLUMN], errors="ignore").to_dict("records")
    }
    additions: list[dict[str, Any]] = []
    for row in incoming.drop(columns=[ID_COLUMN], errors="ignore").to_dict("records"):
        key = _key(row)
        prior = existing_by_key.get(key)
        if prior is None:
            additions.append(row)
            continue
        if _canonical_record(prior) != _canonical_record(row):
            raise ValueError(
                "Quote-liquidity receipt is immutable and conflicts with an existing row: "
                + "|".join(key)
            )

    if not additions and path.is_file():
        return path

    values = pd.concat(
        [
            existing.drop(columns=[ID_COLUMN], errors="ignore"),
            pd.DataFrame(additions),
        ],
        ignore_index=True,
        sort=False,
    )
    values = _prepare_values(values).sort_values(
        list(_QUOTE_LIQUIDITY_KEY),
        kind="stable",
    )
    output = add_readable_id(values.reset_index(drop=True), key_columns=_QUOTE_LIQUIDITY_KEY)
    output = output.reindex(columns=QUOTE_LIQUIDITY_COLUMNS)
    temporary = path.with_suffix(".tmp.parquet")
    output.to_parquet(temporary, index=False)
    temporary.replace(path)
    return path


def _prepare_frame(frame: pd.DataFrame) -> pd.DataFrame:
    values = frame.drop(columns=[ID_COLUMN], errors="ignore")
    values = without_internal_identity_columns(values)
    missing = [
        column
        for column in QUOTE_LIQUIDITY_COLUMNS[1:]
        if column not in values.columns
    ]
    if missing:
        raise ValueError(
            "Quote-liquidity frame is missing required columns: " + ", ".join(missing)
        )
    values = _prepare_values(values.reindex(columns=QUOTE_LIQUIDITY_COLUMNS[1:]))
    if values[list(_QUOTE_LIQUIDITY_KEY)].isna().any(axis=None):
        raise ValueError("Quote-liquidity receipt key contains missing values")
    return add_readable_id(values, key_columns=_QUOTE_LIQUIDITY_KEY).reindex(
        columns=QUOTE_LIQUIDITY_COLUMNS
    )


def _prepare_values(frame: pd.DataFrame) -> pd.DataFrame:
    output = frame.copy()
    for column in _TEMPORAL_COLUMNS:
        output[column] = pd.to_datetime(output[column], utc=True, errors="coerce")
    for column in ("bid", "ask", "mid", "relative_bid_ask_spread", "quote_staleness_seconds"):
        output[column] = pd.to_numeric(output[column], errors="coerce")
    output["quote_quality_pass"] = output["quote_quality_pass"].astype("boolean")
    return output


def _utc_timestamp(
    value: object,
    *,
    field: str,
    required: bool = True,
) -> pd.Timestamp:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        message = f"Schwab quote is missing a valid {field}"
        if required:
            raise QuoteLiquidityQualityError(message)
        raise ValueError(message)
    return pd.Timestamp(parsed)


def _finite_number(value: object, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise QuoteLiquidityQualityError(
            f"Schwab quote is missing a numeric {field}"
        ) from exc
    if not math.isfinite(number):
        raise QuoteLiquidityQualityError(f"Schwab quote {field} is not finite")
    return number


def _key(row: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(_key_value(row.get(column)) for column in _QUOTE_LIQUIDITY_KEY)


def _key_value(value: object) -> str:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return str(value)


def _canonical_record(row: Mapping[str, Any]) -> str:
    return json.dumps(
        {
            str(key): _json_value(value)
            for key, value in sorted(row.items(), key=lambda item: str(item[0]))
            if str(key) != ID_COLUMN
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_value(value: object) -> object:
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value
