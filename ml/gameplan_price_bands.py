"""Causal, observed-price planning bands for the nightly stock review.

These empirical ranges describe historical prior-action-close to entry-clock
ratios. They are neither calibrated future confidence intervals nor order
prices. The caller supplies one verified stock-price source; this module has
no acquisition, model-training, broker, or publication side effects.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import exchange_calendars as xcals
import numpy as np
import pandas as pd

from ml.independent_stock_targets import STOCK_TARGET_BOUNDARY_TOLERANCE, STOCK_TIMEZONE
from ml.stock_target_prices import independent_price_identity, stock_price_dataset


PRICE_BAND_CONTRACT = "historical-entry-price-band-v1"
_MINUTE = pd.Timedelta(minutes=1)


def _aware_timestamp(value: Any, label: str) -> pd.Timestamp:
    result = pd.Timestamp(value)
    if pd.isna(result) or result.tzinfo is None:
        raise ValueError(f"{label} requires a timezone-aware timestamp")
    return result.tz_convert("UTC")


def _clock(day: date, hour: int) -> pd.Timestamp:
    return pd.Timestamp(day).tz_localize(STOCK_TIMEZONE).replace(hour=hour).tz_convert("UTC")


def _observation(bars: pd.DataFrame, boundary: pd.Timestamp, *, close: bool) -> tuple[float, str] | None:
    if bars.empty:
        return None
    timestamps = pd.DatetimeIndex(bars.timestamp)
    position = (timestamps.searchsorted(boundary - _MINUTE, side="right") - 1
                if close else timestamps.searchsorted(boundary, side="left"))
    if position < 0 or position >= len(bars):
        return None
    row = bars.iloc[position]
    observed = row.timestamp + (_MINUTE if close else pd.Timedelta(0))
    gap = boundary - observed if close else observed - boundary
    price = float(row["close" if close else "open"])
    if not pd.Timedelta(0) <= gap <= STOCK_TARGET_BOUNDARY_TOLERANCE or not np.isfinite(price) or price <= 0:
        return None
    return price, observed.isoformat()


def _source_identity(prices: pd.DataFrame, forecasts: pd.DataFrame) -> tuple[str, str]:
    report = prices.attrs.get("stock_price_source", {})
    contract, dataset = report.get("source_contract"), report.get("dataset")
    if not contract or stock_price_dataset(contract) != dataset:
        raise ValueError("Planning price bands require one verified stock_price_source identity")
    for row in forecasts.to_dict("records"):
        if independent_price_identity(row) != (contract, dataset):
            raise ValueError("Planning price bands cannot mix forecast and observation price sources")
    if "provider_dataset" in prices and set(prices.provider_dataset.dropna().astype(str)) != {dataset}:
        raise ValueError("Planning price observations contain a different dataset")
    return str(contract), str(dataset)


def build_entry_price_bands(
    prices: pd.DataFrame, forecasts: pd.DataFrame, *, observed_at: Any,
    lookback_sessions: int = 120, minimum_samples: int = 2,
) -> dict:
    """Return JSON-safe rows in forecast order and per-symbol/clock evidence.

    A sample pairs the previous XNYS session's 17:00 Pacific observed close
    with the next session's observed entry open (04:00 through 16:00). Actual
    minute opens must be at/after the entry clock, and actual minute closes
    at/before 17:00, within the native five-minute tolerance. Both bars must
    have completed by ``observed_at``. No sample ends on/after the forecast's
    action date. Weekend/holiday transitions are included, never synthesized.

    The range is anchored to the forecast action date's exact preceding
    session close; a missing close cannot be replaced with an older close or
    a broker quote. The 5th/95th empirical percentiles are pooled across session
    gap lengths and rounded outward to cents. Daily/weekly entries share the
    same 04:00 evidence, while explicit context rows receive no entry range.
    This descriptive calculation uses the observed sample whenever at least two
    session pairs exist. It has no model-training sample gate; fewer than two
    pairs cannot describe a distribution of price movement.
    """
    if (not isinstance(lookback_sessions, int) or isinstance(lookback_sessions, bool)
            or not isinstance(minimum_samples, int) or isinstance(minimum_samples, bool)
            or lookback_sessions < 2 or minimum_samples < 2 or minimum_samples > lookback_sessions):
        raise ValueError("Price-band sample settings require 2 <= minimum_samples <= lookback_sessions")
    now = _aware_timestamp(observed_at, "observed_at")
    contract, dataset = _source_identity(prices, forecasts)
    report = {
        "contract_version": PRICE_BAND_CONTRACT, "observed_at": now.isoformat(),
        "price_source_contract": contract, "price_dataset": dataset,
        "price_basis": "unadjusted_market_scale", "lookback_sessions": lookback_sessions,
        "minimum_samples": minimum_samples, "lower_quantile": 0.05, "upper_quantile": 0.95,
        "description": "Historical central 90% prior-close-to-entry planning band; not a future confidence guarantee or an order price",
        "gap_policy": "Pool observed XNYS session transitions including weekends and exchange holidays",
        "reference_policy": "Exact prior XNYS session 17:00 Pacific observed close; no older-close or cross-source substitution",
        "boundary_tolerance_seconds": STOCK_TARGET_BOUNDARY_TOLERANCE.total_seconds(),
        "rows": [], "statistics": {},
    }
    if forecasts.empty:
        return report
    required = {"symbol", "action_date", "route", "target_role", "target_window_start"}
    if not required.issubset(forecasts.columns):
        raise ValueError(f"Planning price forecasts are missing columns: {sorted(required - set(forecasts.columns))}")
    dates = [pd.Timestamp(value).date() for value in forecasts.action_date]
    calendar = xcals.get_calendar("XNYS", start=pd.Timestamp(min(dates)) - pd.Timedelta(days=max(370, lookback_sessions * 3)),
                                  end=pd.Timestamp(max(dates)) + pd.Timedelta(days=10))
    sessions = pd.DatetimeIndex(calendar.sessions)
    # The source report is retained above; only copied, observed minute data is consumed.
    bars = prices.loc[:, ["symbol", "timestamp", "open", "close"]].copy()
    bars["symbol"] = bars.symbol.astype(str).str.upper()
    bars["timestamp"] = pd.to_datetime(bars.timestamp, utc=True, errors="coerce")
    bars = bars.loc[bars.timestamp.notna() & bars.timestamp.add(_MINUTE).le(now)]
    bars = bars.drop_duplicates(["symbol", "timestamp", "open", "close"])
    if bars.duplicated(["symbol", "timestamp"]).any():
        raise ValueError("Planning price observations contain conflicting minutes")
    for field in ("open", "close"):
        bars[field] = pd.to_numeric(bars[field], errors="coerce")
    by_symbol = {symbol: frame.sort_values("timestamp", kind="stable")
                 for symbol, frame in bars.groupby("symbol", sort=False)}
    empty_bars = bars.iloc[:0]

    for forecast, day in zip(forecasts.to_dict("records"), dates):
        symbol = str(forecast["symbol"]).upper()
        row = {"forecast_id": str(forecast.get("forecast_id") or forecast.get("id", "")), "symbol": symbol,
               "route": str(forecast["route"]), "action_date": day.isoformat(),
               "price_band_status": "NOT_ENTRY", "price_band_reason": "Explicit non-entry research/outlook row",
               "trade_price_low": None, "trade_price_high": None, "price_reference": None,
               "price_reference_observed_at": None, "price_reference_session": None,
               "price_band_entry_clock_local": None, "price_band_sample_count": 0,
               "price_band_candidate_sessions": 0, "price_band_coverage": None,
               "price_band_history_first_session": None, "price_band_history_last_session": None}
        if str(forecast["target_role"]) != "EXECUTION":
            report["rows"].append(row)
            continue
        start = _aware_timestamp(forecast["target_window_start"], "target_window_start").tz_convert(STOCK_TIMEZONE)
        if (start.date() != day or start.hour not in range(4, 17) or start.minute or start.second
                or start.microsecond or start.nanosecond or not calendar.is_session(pd.Timestamp(day))):
            raise ValueError("Planning entry price bands require an exact 04:00-16:00 XNYS action-date entry clock")
        clock = f"{start.hour:02d}:00"
        key = f"{symbol}|{day.isoformat()}|{clock}"
        stats = report["statistics"].get(key)
        if stats is None:
            prior_session = pd.Timestamp(calendar.previous_session(pd.Timestamp(day))).date()
            candidates = sessions[sessions < pd.Timestamp(day)][-lookback_sessions:]
            symbol_bars = by_symbol.get(symbol, empty_bars)
            reference = _observation(symbol_bars, _clock(prior_session, 17), close=True)
            samples, missing_close, missing_entry = [], 0, 0
            for session in candidates:
                sample_day = pd.Timestamp(session).date()
                previous = pd.Timestamp(calendar.previous_session(session)).date()
                prior_close = _observation(symbol_bars, _clock(previous, 17), close=True)
                entry = _observation(symbol_bars, _clock(sample_day, start.hour), close=False)
                missing_close += int(prior_close is None)
                missing_entry += int(entry is None)
                if prior_close is None or entry is None:
                    continue
                samples.append({"session": sample_day.isoformat(), "prior_session": previous.isoformat(),
                                "ratio": entry[0] / prior_close[0], "prior_close": prior_close[0],
                                "prior_close_observed_at": prior_close[1], "entry_open": entry[0],
                                "entry_open_observed_at": entry[1],
                                "calendar_gap_days": (sample_day - previous).days})
            count = len(samples)
            ratios = np.asarray([sample["ratio"] for sample in samples], dtype=float)
            low_ratio, high_ratio = (map(float, np.quantile(ratios, [0.05, 0.95])) if count else (None, None))
            status = ("UNAVAILABLE_REFERENCE_PRICE" if reference is None else
                      "UNAVAILABLE_MINIMUM_SAMPLES" if count < minimum_samples else "AVAILABLE")
            sample_reason = ("No observed historical session pairs are available for this entry clock" if count == 0 else
                             "Only one observed historical session pair; a movement distribution needs at least two" if count == 1 else
                             f"Fewer than {minimum_samples} observed historical session pairs")
            reasons = {"UNAVAILABLE_REFERENCE_PRICE": "Missing exact prior-session 17:00 observed close within five minutes",
                       "UNAVAILABLE_MINIMUM_SAMPLES": sample_reason,
                       "AVAILABLE": "Historical central 90% planning range; execution must revalidate a current tradable quote"}
            stats = {"symbol": symbol, "action_date": day.isoformat(), "entry_clock_local": clock,
                     "status": status, "reason": reasons[status], "sample_count": count,
                     "candidate_sessions": len(candidates), "coverage": count / len(candidates) if len(candidates) else 0.0,
                     "missing_prior_close_count": missing_close, "missing_entry_open_count": missing_entry,
                     "history_first_session": samples[0]["session"] if samples else None,
                     "history_last_session": samples[-1]["session"] if samples else None,
                     "long_gap_sample_count": sum(sample["calendar_gap_days"] > 1 for sample in samples),
                     "ratio_p05": low_ratio, "ratio_p95": high_ratio,
                     "reference_price": reference[0] if reference else None,
                     "reference_observed_at": reference[1] if reference else None,
                     "reference_session": prior_session.isoformat(),
                     "trade_price_low": float(np.floor(reference[0] * low_ratio * 100) / 100) if status == "AVAILABLE" else None,
                     "trade_price_high": float(np.ceil(reference[0] * high_ratio * 100) / 100) if status == "AVAILABLE" else None,
                     "samples": samples}
            report["statistics"][key] = stats
        row.update(price_band_status=stats["status"], price_band_reason=stats["reason"],
                   trade_price_low=stats["trade_price_low"], trade_price_high=stats["trade_price_high"],
                   price_reference=stats["reference_price"], price_reference_observed_at=stats["reference_observed_at"],
                   price_reference_session=stats["reference_session"], price_band_entry_clock_local=clock,
                   price_band_sample_count=stats["sample_count"], price_band_candidate_sessions=stats["candidate_sessions"],
                   price_band_coverage=stats["coverage"], price_band_history_first_session=stats["history_first_session"],
                   price_band_history_last_session=stats["history_last_session"])
        report["rows"].append(row)
    return report
