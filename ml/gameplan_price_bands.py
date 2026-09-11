"""Causal, observed-price planning bands for the nightly stock review.

These empirical ranges describe historical prior-action-close to entry-clock
ratios. They are neither calibrated future confidence intervals nor order
prices. The caller supplies one verified stock-price source; this module has
no acquisition, model-training, broker, or publication side effects.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any

import exchange_calendars as xcals
import numpy as np
import pandas as pd

from ml.independent_stock_targets import STOCK_TARGET_BOUNDARY_TOLERANCE, STOCK_TIMEZONE
from ml.stock_target_prices import independent_price_identity, stock_price_dataset


PRICE_BAND_CONTRACT = "historical-entry-price-band-v1"
PLANNING_PRICE_PATH_CONTRACT = "conditional-hourly-planning-price-path-v1"
COMPLETED_PRICE_BAND_CONTRACT = "historical-entry-price-band-v2"
COMPLETED_PLANNING_PRICE_PATH_CONTRACT = "conditional-hourly-planning-price-path-v2"
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


def _reference_completion(prices, forecasts, now, enabled):
    if not isinstance(enabled, bool):
        raise ValueError("Reference forward-fill policy must be boolean")
    if not enabled:
        return None
    from ml.gameplan_price_completion import complete_planning_reference_gaps
    return complete_planning_reference_gaps(prices, forecasts, observed_at=now)


def _planning_reference(bars, boundary, symbol, day, completion):
    """Apply a separately recorded assumption only to the current anchor."""
    observed = _observation(bars, boundary, close=True)
    if observed is not None or completion is None:
        return observed
    candidate = completion["references"][f"{symbol}|{day.isoformat()}"]
    if candidate["status"] == "AVAILABLE_SYNTHETIC":
        # Keep the actual observation time; effective_at is a distinct field.
        return candidate["price"], candidate["observed_at"]
    return None


def _reference_details(completion, symbol, day):
    if completion is None:
        return {}
    reference = completion["references"][f"{symbol}|{day.isoformat()}"]
    return {"reference_is_synthetic": reference.get("is_synthetic", False),
            "reference_effective_at": reference.get("effective_at"),
            "reference_fill_count": reference["fill_count"],
            "reference_gap_minutes": reference.get("gap_minutes"),
            "reference_completion_status": reference["status"],
            "reference_completion_reason": reference["reason"]}


def build_entry_price_bands(
    prices: pd.DataFrame, forecasts: pd.DataFrame, *, observed_at: Any,
    lookback_sessions: int = 120, minimum_samples: int = 2,
    allow_reference_forward_fill: bool = False,
) -> dict:
    """Return JSON-safe rows in forecast order and per-symbol/clock evidence.

    A sample pairs the previous XNYS session's 17:00 Pacific observed close
    with the next session's observed entry open (04:00 through 16:00). Actual
    minute opens must be at/after the entry clock, and actual minute closes
    at/before 17:00, within the native five-minute tolerance. Both bars must
    have completed by ``observed_at``. No sample ends on/after the forecast's
    action date. Weekend/holiday transitions are included, never synthesized.

    The range is anchored to the forecast action date's exact preceding
    session close. The explicit v2 planning policy may carry the last actual
    close through a verified trailing gap of at most 15 minutes, recorded as
    synthetic zero-volume bars. It never fills historical samples or uses a
    broker quote. The 5th/95th empirical percentiles are pooled across session
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
    completion = _reference_completion(prices, forecasts, now, allow_reference_forward_fill)
    report = {
        "contract_version": COMPLETED_PRICE_BAND_CONTRACT if allow_reference_forward_fill else PRICE_BAND_CONTRACT,
        "observed_at": now.isoformat(),
        "price_source_contract": contract, "price_dataset": dataset,
        "price_basis": "unadjusted_market_scale", "lookback_sessions": lookback_sessions,
        "minimum_samples": minimum_samples, "lower_quantile": 0.05, "upper_quantile": 0.95,
        "description": "Historical central 90% prior-close-to-entry planning band; not a future confidence guarantee or an order price",
        "gap_policy": "Pool observed XNYS session transitions including weekends and exchange holidays",
        "reference_policy": "Exact prior XNYS session 17:00 Pacific observed close; no older-close or cross-source substitution",
        "boundary_tolerance_seconds": STOCK_TARGET_BOUNDARY_TOLERANCE.total_seconds(),
        "rows": [], "statistics": {},
    }
    if completion is not None:
        report["reference_completion"] = completion
        report["reference_policy"] = ("Exact prior XNYS session 17:00 Pacific; observed close within five minutes, "
            "or explicitly synthetic carry-forward across a verified trailing gap of at most 15 minutes; no cross-source substitution")
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
            reference = _planning_reference(symbol_bars, _clock(prior_session, 17), symbol, day, completion)
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
            stats.update(_reference_details(completion, symbol, day))
            if status == "AVAILABLE" and stats.get("reference_is_synthetic"):
                stats["reason"] += "; anchor uses a bounded synthetic zero-volume carry-forward"
            report["statistics"][key] = stats
        row.update(price_band_status=stats["status"], price_band_reason=stats["reason"],
                   trade_price_low=stats["trade_price_low"], trade_price_high=stats["trade_price_high"],
                   price_reference=stats["reference_price"], price_reference_observed_at=stats["reference_observed_at"],
                   price_reference_session=stats["reference_session"], price_band_entry_clock_local=clock,
                   price_band_sample_count=stats["sample_count"], price_band_candidate_sessions=stats["candidate_sessions"],
                   price_band_coverage=stats["coverage"], price_band_history_first_session=stats["history_first_session"],
                   price_band_history_last_session=stats["history_last_session"])
        if completion is not None:
            row.update({f"price_{key}": value for key, value in _reference_details(completion, symbol, day).items()})
        report["rows"].append(row)
    return report


def build_planning_price_path(
    prices: pd.DataFrame, forecasts: pd.DataFrame, *, observed_at: Any,
    working_half_width_bps: float = 20, entry_bands: dict | None = None,
    allow_reference_forward_fill: bool = False,
) -> dict:
    """Describe conditional working prices for every 04:00-17:00 action clock.

    The center is the actual prior-session close times the observed historical
    median close-to-clock ratio. A configured +/- basis-point allowance around
    that center is a *planning assumption for conditional fills*, not a learned
    future confidence interval. Historical 5th/95th percentile stress prices
    remain separate. Prices outside the working range remain eligible for
    execution; live order prices and sizes use the current tradable quote and
    actual available cash and holdings, independent of these estimates.

    ``entry_bands`` may be the report just calculated from these same in-memory
    source prices and observation time. Its per-clock samples are reused. Any
    missing 04:00-16:00 clocks and the 17:00 closing-price clock use the supplied
    frame; this function never reloads or fetches a price dataset. A close is
    observed at minute completion, including the final 17:00 clock.
    """
    if isinstance(working_half_width_bps, bool):
        raise ValueError("Working price half-width must be finite and between 0 and 10000 basis points")
    try:
        width = float(working_half_width_bps)
    except (TypeError, ValueError) as exc:
        raise ValueError("Working price half-width must be finite and between 0 and 10000 basis points") from exc
    if not np.isfinite(width) or not 0 < width < 10000:
        raise ValueError("Working price half-width must be finite and between 0 and 10000 basis points")
    now = _aware_timestamp(observed_at, "observed_at")
    source_contract, dataset = _source_identity(prices, forecasts)
    completion = _reference_completion(prices, forecasts, now, allow_reference_forward_fill)
    existing = (build_entry_price_bands(prices, forecasts, observed_at=now,
                                      allow_reference_forward_fill=allow_reference_forward_fill)
                if entry_bands is None else entry_bands)
    expected_contract = COMPLETED_PRICE_BAND_CONTRACT if allow_reference_forward_fill else PRICE_BAND_CONTRACT
    if (existing.get("contract_version") != expected_contract
            or existing.get("price_source_contract") != source_contract
            or existing.get("price_dataset") != dataset
            or existing.get("reference_completion") != completion
            or _aware_timestamp(existing.get("observed_at"), "entry_bands.observed_at") != now):
        raise ValueError("Reused entry bands must match the exact source and observation time")
    lookback = int(existing["lookback_sessions"])
    minimum = int(existing["minimum_samples"])
    statistics = dict(existing["statistics"])
    scopes = sorted({(str(row["symbol"]).upper(), pd.Timestamp(row["action_date"]).date())
                     for row in forecasts.to_dict("records")})
    # These auxiliary clocks request descriptive price evidence only. They do
    # not create a native forecast, model qualification, or order authority.
    missing_clocks = [{"symbol": symbol, "action_date": day.isoformat(), "route": f"price-clock@{hour:02d}:00",
                       "target_role": "EXECUTION", "target_window_start": _clock(day, hour),
                       "target_price_source_contract": source_contract, "target_price_dataset": dataset}
                      for symbol, day in scopes for hour in range(4, 17)
                      if f"{symbol}|{day.isoformat()}|{hour:02d}:00" not in statistics]
    if missing_clocks:
        extra = build_entry_price_bands(prices, pd.DataFrame(missing_clocks), observed_at=now,
                                        lookback_sessions=lookback, minimum_samples=minimum,
                                        allow_reference_forward_fill=allow_reference_forward_fill)
        statistics.update(extra["statistics"])
    result = {
        "contract_version": COMPLETED_PLANNING_PRICE_PATH_CONTRACT if allow_reference_forward_fill else PLANNING_PRICE_PATH_CONTRACT,
        "observed_at": now.isoformat(),
        "price_source_contract": source_contract, "price_dataset": dataset,
        "working_half_width_bps": width,
        "method": "Observed prior-close-to-clock median, with an explicit conditional fill allowance",
        "working_range_semantics": "Estimated planning prices only; not a future confidence interval, guaranteed execution, or order-limit authority",
        "historical_range_semantics": "Historical central 90% stress range retained separately from working fill assumptions",
        "market_gap_policy": "Price and cash estimates never gate execution. Use the current tradable quote and actual available cash and holdings, including when they fall outside the estimates.",
        "lookback_sessions": lookback, "minimum_samples": minimum, "points": {},
    }
    if completion is not None:
        result["reference_completion"] = completion
    if not scopes:
        return result
    days = [day for _, day in scopes]
    calendar = xcals.get_calendar("XNYS", start=pd.Timestamp(min(days)) - pd.Timedelta(days=max(370, lookback * 3)),
                                  end=pd.Timestamp(max(days)) + pd.Timedelta(days=10))
    sessions = pd.DatetimeIndex(calendar.sessions)
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
    empty = bars.iloc[:0]
    for symbol, day in scopes:
        symbol_bars = by_symbol.get(symbol, empty)
        prior_day = pd.Timestamp(calendar.previous_session(pd.Timestamp(day))).date()
        reference = _planning_reference(symbol_bars, _clock(prior_day, 17), symbol, day, completion)
        close_samples = []
        for session in sessions[sessions < pd.Timestamp(day)][-lookback:]:
            sample_day = pd.Timestamp(session).date()
            previous = pd.Timestamp(calendar.previous_session(session)).date()
            prior_close = _observation(symbol_bars, _clock(previous, 17), close=True)
            finish = _observation(symbol_bars, _clock(sample_day, 17), close=True)
            if prior_close is not None and finish is not None:
                close_samples.append({"session": sample_day.isoformat(), "prior_session": previous.isoformat(),
                                      "ratio": finish[0] / prior_close[0], "prior_close": prior_close[0],
                                      "prior_close_observed_at": prior_close[1], "endpoint_price": finish[0],
                                      "endpoint_observed_at": finish[1], "endpoint_kind": "observed_close"})
        for hour in range(4, 18):
            clock = f"{hour:02d}:00"
            key = f"{symbol}|{day.isoformat()}|{clock}"
            stats = statistics.get(key) if hour < 17 else None
            samples = stats["samples"] if stats is not None else close_samples
            ratios = np.asarray([sample["ratio"] for sample in samples], dtype=float)
            if not np.isfinite(ratios).all() or (ratios <= 0).any():
                raise ValueError("Planning path requires finite, positive observed price ratios")
            status = ("UNAVAILABLE_REFERENCE_PRICE" if reference is None else
                      "UNAVAILABLE_MINIMUM_SAMPLES" if len(ratios) < minimum else "AVAILABLE")
            low = mid = high = historical_low = historical_high = median = None
            if status == "AVAILABLE":
                q05, median, q95 = map(float, np.quantile(ratios, [0.05, 0.5, 0.95]))
                center = Decimal(str(reference[0])) * Decimal(str(median))
                half_width = Decimal(str(width)) / Decimal(10000)
                cent = Decimal("0.01")
                low = float((center * (1 - half_width)).quantize(cent, rounding=ROUND_FLOOR))
                mid = float(center.quantize(cent, rounding=ROUND_HALF_UP))
                high = float((center * (1 + half_width)).quantize(cent, rounding=ROUND_CEILING))
                historical_low = float(np.floor(reference[0] * q05 * 100) / 100)
                historical_high = float(np.ceil(reference[0] * q95 * 100) / 100)
            reason = ("Conditional planned fill assumption around observed median" if status == "AVAILABLE" else
                      "Missing exact prior-session close" if reference is None else
                      "No observed historical session pairs" if not len(ratios) else
                      "Only one observed historical pair" if len(ratios) == 1 else
                      f"Fewer than {minimum} observed historical pairs")
            result["points"][key] = {
                "symbol": symbol, "action_date": day.isoformat(), "clock_local": clock,
                "timestamp": _clock(day, hour).isoformat(), "status": status, "reason": reason,
                "planned_price_low": low, "planned_price_mid": mid, "planned_price_high": high,
                "historical_price_low": historical_low, "historical_price_high": historical_high,
                "reference_price": reference[0] if reference else None,
                "reference_observed_at": reference[1] if reference else None,
                "reference_session": prior_day.isoformat(), "ratio_median": median,
                "sample_count": len(ratios), "endpoint_kind": "observed_close" if hour == 17 else "observed_open",
                "method": "Observed prior-close-to-clock median with conditional +/- basis-point fill allowance",
                "samples": samples,
                **_reference_details(completion, symbol, day),
            }
            if status == "AVAILABLE" and result["points"][key].get("reference_is_synthetic"):
                result["points"][key]["reason"] += "; anchor uses a bounded synthetic zero-volume carry-forward"
    return result
