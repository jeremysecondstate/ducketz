from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

STOCKS_ROOT = "stocks"
POOLS_ROOT = "pools"
DEFAULT_POOL = "macro"

_TIMEFRAME_ALIASES = {
    "second": "1s",
    "minute": "1m",
    "hour": "1h",
    "daily": "1d",
    "day": "1d",
    "weekly": "1w",
    "week": "1w",
    "monthly": "1mo",
    "month": "1mo",
}


def canonical_timeframe(
    source: str,
    timeframe: str | None,
    metadata: Mapping[str, Any] | None = None,
) -> str:
    """Return a compact timeframe folder name such as 1m, 1h, 1d, or 1w."""
    extra = metadata or {}
    frequency_type = str(extra.get("provider_frequency_type") or "").strip().lower()
    frequency_value = _positive_int(extra.get("provider_frequency"))
    if frequency_type and frequency_value:
        suffix = {
            "minute": "m",
            "daily": "d",
            "weekly": "w",
            "monthly": "mo",
        }.get(frequency_type)
        if suffix:
            return f"{frequency_value}{suffix}"

    for key in ("output_frequency", "source_frequency"):
        configured = str(extra.get(key) or "").strip().lower()
        if configured:
            return _normalize_timeframe(configured)

    configured = str(timeframe or "").strip().lower()
    if configured:
        normalized = _normalize_timeframe(configured)
        if _looks_canonical(normalized):
            return normalized

    inferred = infer_timeframe_from_text(configured)
    return inferred or "unknown"


def stock_data_folder(
    root: Path,
    *,
    symbol: str,
    category: str,
    source: str,
    scope: str,
    dataset_key: str = "",
    timeframe: str = "",
) -> Path:
    """Build a symbol-first stock folder for quotes, bars, corporate data, or errors."""
    stock_root = root / STOCKS_ROOT / safe_token(symbol.upper().replace("/", "-"))
    clean_source = safe_token(source)
    clean_scope = safe_token(scope)
    clean_category = safe_token(category)

    if scope == "errors":
        return stock_root / "errors" / clean_source / clean_category
    if category == "bars":
        return stock_root / "bars" / safe_token(timeframe or "unknown") / clean_source / clean_scope
    if category == "quotes":
        return stock_root / "quotes" / clean_source / clean_scope
    if category == "corporate":
        return stock_root / "corporate" / safe_token(dataset_key or "general") / clean_source / clean_scope
    return stock_root / clean_category / safe_token(dataset_key or "general") / clean_source / clean_scope


def pool_data_folder(
    root: Path,
    *,
    pool: str,
    symbol: str,
    category: str,
    source: str,
    scope: str,
    dataset_key: str = "",
) -> Path:
    """Build a folder for shared context that must not be attributed to one stock."""
    pool_root = (
        root
        / POOLS_ROOT
        / safe_token(pool or DEFAULT_POOL)
        / safe_token(symbol.upper().replace("/", "-"))
    )
    if scope == "errors":
        return pool_root / "errors" / safe_token(source) / safe_token(category)
    return (
        pool_root
        / safe_token(dataset_key or category or "general")
        / safe_token(source)
        / safe_token(scope)
    )


def infer_timeframe_from_text(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""

    explicit = re.findall(r"(?<![a-z0-9])(\d+)(mo|[smhdw])(?![a-z0-9])", text)
    if explicit:
        amount, unit = explicit[-1]
        return f"{int(amount)}{unit}"

    minute = re.search(r"minute[_-](\d+)", text)
    if minute:
        return f"{int(minute.group(1))}m"
    for word, canonical in (
        ("monthly", "1mo"),
        ("weekly", "1w"),
        ("daily", "1d"),
        ("hourly", "1h"),
    ):
        if word in text:
            return canonical
    return ""


def safe_token(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value).strip())
    return cleaned.strip("._-") or "unknown"


def _normalize_timeframe(value: str) -> str:
    text = value.strip().lower().replace(" ", "")
    if text in _TIMEFRAME_ALIASES:
        return _TIMEFRAME_ALIASES[text]
    text = text.replace("minutes", "m").replace("minute", "m")
    text = text.replace("hours", "h").replace("hour", "h")
    text = text.replace("seconds", "s").replace("second", "s")
    text = text.replace("days", "d").replace("day", "d")
    text = text.replace("weeks", "w").replace("week", "w")
    text = text.replace("months", "mo").replace("month", "mo")
    return text


def _looks_canonical(value: str) -> bool:
    return re.fullmatch(r"\d+(?:mo|[smhdw])", value) is not None


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
