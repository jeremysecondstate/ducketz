"""Bounded, explicitly synthetic closing references for account planning.

Sparse venue OHLCV can leave a completed session without a recent candle.
This policy carries a verified same-session close forward for at most fifteen
minutes. Missing candles are *assumed* to mean no trades; they do not establish
that fact. The original observation time remains visible. Native prices and
the observed history used for model targets and price-band samples are never
modified here.
"""
from __future__ import annotations

from typing import Any
import re

import exchange_calendars as xcals
import numpy as np
import pandas as pd

from ml.independent_stock_targets import STOCK_TARGET_BOUNDARY_TOLERANCE, STOCK_TIMEZONE
from ml.stock_target_prices import independent_price_identity, stock_price_dataset


PLANNING_REFERENCE_COMPLETION_CONTRACT = "bounded-planning-reference-completion-v1"
_MINUTE = pd.Timedelta(minutes=1)
_HARD_MAX_GAP_MINUTES = 15


def _aware_timestamp(value: Any, label: str) -> pd.Timestamp:
    result = pd.Timestamp(value)
    if pd.isna(result) or result.tzinfo is None:
        raise ValueError(f"{label} requires a timezone-aware timestamp")
    return result.tz_convert("UTC")


def _source_identity(prices: pd.DataFrame, forecasts: pd.DataFrame) -> tuple[str, str]:
    source = prices.attrs.get("stock_price_source", {})
    contract, dataset = source.get("source_contract"), source.get("dataset")
    if not contract or stock_price_dataset(contract) != dataset:
        raise ValueError("Planning reference completion requires a verified stock_price_source identity")
    try:
        matching = all(independent_price_identity(row) == (contract, dataset) for row in forecasts.to_dict("records"))
    except RuntimeError as exc:
        raise ValueError("Planning reference forecast source identity is inconsistent") from exc
    if not matching:
        raise ValueError("Planning reference completion cannot mix forecast and observation price sources")
    if "provider_dataset" in prices and set(prices.provider_dataset.dropna().astype(str)) != {dataset}:
        raise ValueError("Planning reference observations contain a different dataset")
    return str(contract), str(dataset)


def _native_minutes(prices: pd.DataFrame) -> pd.DataFrame:
    required = {"symbol", "timestamp", "open", "close"}
    if not required.issubset(prices.columns):
        raise ValueError(f"Planning reference observations are missing columns: {sorted(required - set(prices.columns))}")
    if "is_synthetic" in prices and prices.is_synthetic.fillna(False).ne(False).any():
        raise ValueError("Planning reference completion requires native observations, not prior synthetic bars")
    fields = [field for field in ("open", "high", "low", "close", "volume") if field in prices]
    bars = prices.loc[:, ["symbol", "timestamp", *fields]].copy()
    if bars.symbol.isna().any():
        raise ValueError("Planning reference observations have an invalid symbol")
    bars["symbol"] = bars.symbol.astype(str).str.strip().str.upper()
    if bars.symbol.eq("").any():
        raise ValueError("Planning reference observations have an invalid symbol")
    # Native archive timestamps are aware; reject naive input rather than
    # silently interpreting local wall clocks as UTC.
    if len(bars) and not isinstance(bars.timestamp.dtype, pd.DatetimeTZDtype):
        if any(pd.isna(value) or pd.Timestamp(value).tzinfo is None for value in bars.timestamp):
            raise ValueError("Planning reference observations require timezone-aware minute timestamps")
    bars["timestamp"] = pd.to_datetime(bars.timestamp, utc=True, errors="coerce")
    if bars.timestamp.isna().any() or bars.timestamp.ne(bars.timestamp.dt.floor("min")).any():
        raise ValueError("Planning reference observations have invalid minute timestamps")
    for field in fields:
        if bars[field].map(lambda value: isinstance(value, (bool, np.bool_))).any():
            raise ValueError(f"Planning reference observations have invalid {field}")
        bars[field] = pd.to_numeric(bars[field], errors="coerce")
        if not np.isfinite(bars[field]).all():
            raise ValueError(f"Planning reference observations have invalid {field}")
        if field == "volume":
            if bars[field].lt(0).any() or bars[field].mod(1).ne(0).any():
                raise ValueError("Planning reference observations have invalid volume")
        elif bars[field].le(0).any():
            raise ValueError(f"Planning reference observations have invalid {field}")
    if "high" in bars and bars.high.lt(bars[["open", "close"]].max(axis=1)).any():
        raise ValueError("Planning reference observations have invalid OHLC ranges")
    if "low" in bars and bars.low.gt(bars[["open", "close"]].min(axis=1)).any():
        raise ValueError("Planning reference observations have invalid OHLC ranges")
    bars = bars.drop_duplicates(["symbol", "timestamp", *fields])
    if bars.duplicated(["symbol", "timestamp"]).any():
        raise ValueError("Planning reference observations contain conflicting minutes")
    return bars.sort_values(["symbol", "timestamp"], kind="stable")


def _complete_source_coverage(
    source: dict, *, symbol: str, origin_start: pd.Timestamp, boundary: pd.Timestamp, now: pd.Timestamp,
) -> dict | None:
    """Use only intervals already verified by the native price-source loader."""
    verified = source.get("native_archive_partitions_verified", 0)
    if (not isinstance(verified, int) or isinstance(verified, bool) or verified <= 0
            or source.get("schema") != "ohlcv-1m" or not isinstance(source.get("partitions"), list)):
        return None
    for partition in source["partitions"]:
        if not isinstance(partition, dict) or str(partition.get("symbol", "")).upper() != symbol:
            continue
        if not partition.get("manifest_path"):
            continue
        try:
            # Historical archive requests use UTC calendar dates as well as
            # explicit aware timestamps. Do not interpret naive wall times.
            def bound(value, label):
                if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    return pd.Timestamp(value, tz="UTC")
                return _aware_timestamp(value, label)
            start = bound(partition.get("start"), "partition.start")
            end = bound(partition.get("end"), "partition.end")
            published = _aware_timestamp(partition.get("published_at"), "partition.published_at")
        except (ValueError, TypeError):
            continue
        if start <= origin_start and end >= boundary and boundary <= published <= now:
            return {"symbol": symbol, "start": start.isoformat(), "end": end.isoformat(),
                    "published_at": published.isoformat(), "manifest_path": str(partition["manifest_path"]),
                    "request_id": str(partition.get("request_id", "")), "native_partition_verified": True}
    return None


def complete_planning_reference_gaps(
    prices: pd.DataFrame, forecasts: pd.DataFrame, *, observed_at: Any,
    max_gap_minutes: int = 15,
) -> dict:
    """Qualify exact prior-session closing references without editing prices.

    ``references`` is keyed by ``SYMBOL|action_date``. An actual close within
    the native five-minute tolerance is ``AVAILABLE_OBSERVED`` and gets no
    synthetic rows. A completed prior XNYS session with a close older than
    that tolerance but at most ``max_gap_minutes`` old is
    ``AVAILABLE_SYNTHETIC``. Each absent minute through 17:00 Pacific receives
    OHLC equal to the last actual close and zero *assumed* volume. Its actual
    ``observed_at`` is retained separately from its synthetic ``effective_at``.

    Missing prior-session data, longer gaps, unfinished boundaries and missing
    verified acquisition coverage through the boundary are ``UNAVAILABLE``.
    Malformed/conflicting data raises. No observations are
    fetched, no quotes are consumed and no historical training/sample frame
    is filled. The caller must already have verified the native source.
    """
    minimum = int(STOCK_TARGET_BOUNDARY_TOLERANCE / _MINUTE)
    if (not isinstance(max_gap_minutes, int) or isinstance(max_gap_minutes, bool)
            or not minimum <= max_gap_minutes <= _HARD_MAX_GAP_MINUTES):
        raise ValueError("Planning reference max_gap_minutes must be an integer between 5 and 15")
    now = _aware_timestamp(observed_at, "observed_at")
    contract, dataset = _source_identity(prices, forecasts)
    report = {
        "contract_version": PLANNING_REFERENCE_COMPLETION_CONTRACT,
        "observed_at": now.isoformat(), "price_source_contract": contract,
        "price_dataset": dataset, "max_gap_minutes": max_gap_minutes,
        "native_boundary_tolerance_seconds": STOCK_TARGET_BOUNDARY_TOLERANCE.total_seconds(),
        "policy": "Carry the exact prior XNYS session's last actual close through a completed 17:00 Pacific boundary for a bounded planning reference only",
        "synthetic_reason": "ASSUMED_NO_TRADES",
        "limitation": "Missing venue candles do not prove no trades; synthetic prices and zero volume are assumptions, not market observations",
        "historical_samples_modified": False, "native_prices_modified": False,
        "synthetic_bars": [], "references": {},
    }
    if forecasts.empty:
        return report
    required = {"symbol", "action_date"}
    if not required.issubset(forecasts.columns):
        raise ValueError(f"Planning reference forecasts are missing columns: {sorted(required - set(forecasts.columns))}")
    scopes = set()
    for row in forecasts.to_dict("records"):
        day = pd.Timestamp(row["action_date"])
        if pd.isna(day) or pd.isna(row["symbol"]) or not str(row["symbol"]).strip():
            raise ValueError("Planning reference forecasts have an invalid symbol or action date")
        scopes.add((str(row["symbol"]).strip().upper(), day.date()))
    dates = [day for _, day in scopes]
    calendar = xcals.get_calendar("XNYS", start=pd.Timestamp(min(dates)) - pd.Timedelta(days=370),
                                  end=pd.Timestamp(max(dates)) + pd.Timedelta(days=10))
    bars = _native_minutes(prices)
    by_symbol = {symbol: frame for symbol, frame in bars.groupby("symbol", sort=False)}
    for symbol, day in sorted(scopes):
        if not calendar.is_session(pd.Timestamp(day)):
            raise ValueError("Planning reference action dates must be XNYS sessions")
        session = pd.Timestamp(calendar.previous_session(pd.Timestamp(day))).date()
        boundary = pd.Timestamp(session).tz_localize(STOCK_TIMEZONE).replace(hour=17).tz_convert("UTC")
        session_start = pd.Timestamp(session).tz_localize(STOCK_TIMEZONE).replace(hour=4).tz_convert("UTC")
        reference = {
            "status": "UNAVAILABLE", "reason": "NO_PRIOR_SESSION_OBSERVATION",
            "symbol": symbol, "action_date": day.isoformat(), "session": session.isoformat(),
            "price": None, "observed_at": None, "effective_at": None,
            "boundary_at": boundary.isoformat(), "origin_bar_start": None,
            "gap_minutes": None, "max_gap_minutes": max_gap_minutes, "fill_count": 0,
            "is_synthetic": False, "source_contract": contract, "dataset": dataset,
            "completion_contract": PLANNING_REFERENCE_COMPLETION_CONTRACT, "source_coverage": None,
        }
        report["references"][f"{symbol}|{day.isoformat()}"] = reference
        if boundary > now:
            reference["reason"] = "PRIOR_SESSION_BOUNDARY_NOT_COMPLETED"
            continue
        symbol_bars = by_symbol.get(symbol, bars.iloc[:0])
        candidates = symbol_bars.loc[symbol_bars.timestamp.ge(session_start)
                                     & symbol_bars.timestamp.add(_MINUTE).le(boundary)
                                     & symbol_bars.timestamp.add(_MINUTE).le(now)]
        if candidates.empty:
            continue
        origin = candidates.iloc[-1]
        actual_time = origin.timestamp + _MINUTE
        gap = boundary - actual_time
        price = float(origin.close)
        reference.update(observed_at=actual_time.isoformat(), origin_bar_start=origin.timestamp.isoformat(),
                         gap_minutes=float(gap / _MINUTE))
        if gap <= STOCK_TARGET_BOUNDARY_TOLERANCE:
            reference.update(status="AVAILABLE_OBSERVED", reason="NATIVE_OBSERVATION_WITHIN_TOLERANCE",
                             price=price, effective_at=actual_time.isoformat())
            continue
        if gap > pd.Timedelta(minutes=max_gap_minutes):
            reference["reason"] = "GAP_EXCEEDS_MAXIMUM"
            continue
        coverage = _complete_source_coverage(prices.attrs["stock_price_source"], symbol=symbol,
                                             origin_start=origin.timestamp, boundary=boundary, now=now)
        if coverage is None:
            reference["reason"] = "UNAVAILABLE_SOURCE_COVERAGE"
            continue
        reference["source_coverage"] = coverage
        synthetic_starts = pd.date_range(origin.timestamp + _MINUTE, boundary - _MINUTE, freq="min")
        native_starts = set(symbol_bars.timestamp)
        if any(timestamp in native_starts for timestamp in synthetic_starts):
            raise ValueError("Planning reference completion would replace an existing native minute")
        for timestamp in synthetic_starts:
            report["synthetic_bars"].append({
                "symbol": symbol, "timestamp": timestamp.isoformat(), "open": price, "high": price,
                "low": price, "close": price, "volume": 0, "is_synthetic": True,
                "reason": "ASSUMED_NO_TRADES", "source_contract": PLANNING_REFERENCE_COMPLETION_CONTRACT,
                "origin_source_contract": contract, "origin_dataset": dataset,
                "origin_bar_start": origin.timestamp.isoformat(), "original_observed_at": actual_time.isoformat(),
                "source_coverage": dict(coverage),
                "session": session.isoformat(), "action_date": day.isoformat(),
            })
        reference.update(status="AVAILABLE_SYNTHETIC", reason="ASSUMED_NO_TRADES", price=price,
                         effective_at=boundary.isoformat(), is_synthetic=True, fill_count=len(synthetic_starts))
    return report
