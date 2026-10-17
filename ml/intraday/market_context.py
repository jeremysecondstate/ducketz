"""Versioned as-of market context from immutable, already-collected receipts.

No provider client, filesystem mutation, fitting, or historical reconstruction
belongs here. A quarter's label is not evidence of availability. Every selected
snapshot must have actually arrived by that decision's cutoff; row publication
and event clocks are checked independently. Missing numeric values are encoded
as zero *plus a missing indicator*, never as an invented market observation.
"""
from __future__ import annotations

from collections import defaultdict
from bisect import bisect_right
from copy import deepcopy
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import date, timedelta
import hashlib
import json
import math
import re
from statistics import median, pstdev
from typing import Iterable, Mapping

import numpy as np
import pandas as pd

from .prediction import ContractError


SNAPSHOT_VERSION = "intraday-market-snapshot-v1"
LEGACY_FEATURE_VERSION = "intraday-causal-market-context-v1"
FEATURE_VERSION = "intraday-causal-market-context-v2"
PROJECTION_VERSION = "intraday-native-context-projection-v1"
FEATURE_POLICY = "actual_receipt_and_row_clocks_asof_explicit_missingness_no_historical_backfill_v1"
SOURCE_KINDS = ("stock_quote", "stock_trades", "stock_bars", "options",
                "fundamentals", "events", "cross_asset", "opra_options", "technical", "macro")
FUTURES_ROOTS = ("ES", "NQ", "RTY", "CL", "GC", "ZN", "6E")
_OPTION_FIELDS = ("log_contract_count", "iv_median", "delta_median", "gamma_median",
                  "theta_median", "vega_median", "put_call_volume_log_ratio")
_FIELDS = {
    "stock_quote": ("relative_spread", "depth_imbalance", "last_mid_return"),
    "stock_trades": ("log_observation_count", "log_observed_volume", "price_return"),
    "stock_bars": ("log_observation_count", "log_observed_volume", "price_return"),
    "options": _OPTION_FIELDS,
    "fundamentals": ("earnings_yield", "debt_equity", "profit_margin", "revenue_growth", "log_market_cap",
        "dcf_value", "dcf_upside_fraction", "analyst_target_consensus", "analyst_target_high",
        "analyst_target_low", "analyst_target_median", "analyst_strong_buy", "analyst_buy",
        "analyst_hold", "analyst_sell", "analyst_strong_sell", "analyst_count", "analyst_bullish_fraction",
        "analyst_eps_estimate", "analyst_revenue_estimate"),
    "events": ("log_observed_event_count", "next_earnings_days", "split_events_past30d",
               "log_observed_news_count", "log_observed_press_release_count"),
    "cross_asset": (*tuple(f"{root}_return" for root in FUTURES_ROOTS),
        "commodity_quote_return_median", "commodity_quote_return_dispersion", "commodity_log_instrument_count"),
    "opra_options": _OPTION_FIELDS,
    "technical": ("sma", "ema", "wma", "dema", "tema", "rsi", "adx", "williams", "standard_deviation"),
    "macro": ("gdp_value", "cpi_value", "unemployment_rate", "federal_funds_rate"),
}
# Age limits are part of this feature version, not silently inherited from a
# current provider/config default. Old statements may be newly received, but
# an old quote is not made current by receiving it now.
_MAX_RECEIPT_AGE = {kind: 1800 for kind in SOURCE_KINDS}
_MAX_RECEIPT_AGE.update(fundamentals=400 * 86400, events=7 * 86400, macro=400 * 86400)
_MARKET_KINDS = set(SOURCE_KINDS) - {"fundamentals", "events", "macro"}
_SUCCESS = {"COMPLETE", "EMPTY"}
_MAX_ROWS = 100_000
_BAR_SECONDS = {"ohlcv-1s": 1, "ohlcv-1m": 60, "ohlcv-1h": 3600, "ohlcv-1d": 86400,
                "historical-chart-5min": 300, "historical-chart-15min": 900,
                "CHART_EQUITY": 60, "CHART_FUTURES": 60}
VALUE_FEATURE_NAMES = tuple(
    f"ctx_{kind}_{field}"
    for kind in SOURCE_KINDS
    for field in (*_FIELDS[kind], "source_age_seconds", "receipt_age_seconds")
)
CONTEXT_FEATURE_NAMES = (
    *VALUE_FEATURE_NAMES,
    *(name + "_missing" for name in VALUE_FEATURE_NAMES),
    *(f"ctx_{kind}_source_available" for kind in SOURCE_KINDS),
    *(f"ctx_{kind}_source_partial" for kind in SOURCE_KINDS),
)


def instant(value):
    """Preserve provider nanoseconds when enforcing strict as-of cutoffs."""
    try:
        stamp = pd.Timestamp(value)
        if pd.isna(stamp) or stamp.tzinfo is None:
            raise ValueError("Timezone-aware timestamp required")
        return stamp.tz_convert("UTC")
    except (ValueError, TypeError, OverflowError) as exc:
        raise ContractError("Timezone-aware timestamp required") from exc


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def snapshot_digest(snapshot: Mapping) -> str:
    """Match the collector's canonical immutable receipt hash."""
    from .market_collection import snapshot_digest as digest
    return digest(snapshot)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _selectors(source_ids):
    selectors = {}
    for kind, value in dict(source_ids or {}).items():
        ids = [value] if isinstance(value, str) else value
        if kind not in SOURCE_KINDS or not isinstance(ids, (list, tuple)) or not ids:
            raise ContractError("Market context selectors require known kinds and nonempty source ID lists")
        if any(not isinstance(source, str) or not source for source in ids) or len(ids) != len(set(ids)):
            raise ContractError("Market context source IDs must be nonempty and unique")
        selectors[kind] = tuple(sorted(ids))
    return selectors


def _slots(selectors):
    return tuple((kind, source_id, f"ctx_{kind}" +
                  ("_s" + hashlib.sha256(source_id.encode()).hexdigest()[:12] if len(selectors.get(kind, ())) > 1 else ""))
                 for kind in SOURCE_KINDS for source_id in selectors.get(kind, (None,)))


def _fields(kind, version):
    extra = {
        "stock_quote": ("book_depth_imbalance", "log_book_depth_levels"),
        "options": ("relative_spread_median", "top_depth_imbalance_median", "book_depth_imbalance_median"),
        "opra_options": ("relative_spread_median", "top_depth_imbalance_median", "book_depth_imbalance_median"),
        "fundamentals": ("log_float_shares", "log_outstanding_shares", "free_float_fraction",
            "eps", "diluted_eps", "cash_assets_fraction", "signed_log_operating_cash_flow",
            "signed_log_free_cash_flow", "signed_log_capital_expenditure",
            "operating_cash_flow_growth", "free_cash_flow_growth", "overall_rating_score",
            "dcf_rating_score", "roe_rating_score", "roa_rating_score", "debt_equity_rating_score",
            "pe_rating_score", "pb_rating_score", "target_month_average", "target_month_count",
            "target_quarter_average", "target_quarter_count", "target_year_average", "target_year_count",
            "target_all_time_average", "target_all_time_count", "log_observed_filing_count"),
    }
    return (*_FIELDS[kind], *(extra.get(kind, ()) if version == FEATURE_VERSION else ()))


def _names(selectors, version=FEATURE_VERSION):
    slots = _slots(selectors)
    names = tuple(f"{prefix}_{field}" for kind, _, prefix in slots
                  for field in (*_fields(kind, version), "source_age_seconds", "receipt_age_seconds"))
    return (names, (*names, *(name + "_missing" for name in names),
                    *(f"{prefix}_source_available" for _, _, prefix in slots),
                    *(f"{prefix}_source_partial" for _, _, prefix in slots)))


VALUE_FEATURE_NAMES, CONTEXT_FEATURE_NAMES = _names({})


def market_context_contract(source_ids: Mapping | None = None, *, version=FEATURE_VERSION) -> dict:
    """Serializable exact contract to freeze alongside a PC-local recipe."""
    if version not in {LEGACY_FEATURE_VERSION, FEATURE_VERSION}:
        raise ContractError("Unsupported market context feature version")
    selectors = _selectors(source_ids)
    return {"version": version, "snapshot_version": SNAPSHOT_VERSION,
            "feature_names": list(_names(selectors, version)[1]),
            "source_ids": {k: list(v) for k, v in sorted(selectors.items())},
            "timing_policy": FEATURE_POLICY, "max_receipt_age_seconds": dict(_MAX_RECEIPT_AGE),
            "max_market_event_age_seconds": 1800,
            "source_selection": "independent_frozen_source_blocks_else_unique_source_only",
            "historical_cutoff": "explicit_reference_receipt_before_target_open",
            "observation_window": "bounded_requests_exact_interval_current_rest_or_stream_asof_receipt",
            "provider_delayed_policy": "unavailable_for_current_context",
            "native_projection_version": PROJECTION_VERSION,
            "analyst_estimate_selection": "nearest_nonpast_fiscal_period_per_source_conflicts_missing",
            "technical_contract": {"timeframe": "15min", "period_length": 10, "completion": "explicit_bar_end"},
            "macro_timing": "period_date_is_not_publication_actual_receipt_required",
            "commodity_quote_contract": "configured_native_symbols_latest_positive_price_over_previous_close_equal_weight_median_population_stddev",
            "partial_policy": ("unavailable" if version == LEGACY_FEATURE_VERSION else
                "eligible_observed_records_only_keep_partial_scope_and_row_exclusions"), "option_iv_unit": "decimal",
            "missing_encoding": "zero_plus_per_value_missing_indicator"}


def validate_market_context_contract(contract: Mapping) -> dict:
    if (not isinstance(contract, Mapping) or contract != market_context_contract(
            contract.get("source_ids"), version=contract.get("version"))):
        raise ContractError("Saved market context contract differs from the exact supported contract")
    return deepcopy(dict(contract))


def _resolved_contract(source_ids, contract):
    value = (market_context_contract(source_ids) if contract is None
             else validate_market_context_contract(contract))
    if _selectors(value["source_ids"]) != _selectors(source_ids):
        raise ContractError("Market context selectors differ from the frozen contract")
    return value


@dataclass(frozen=True)
class MarketContext:
    symbol: str
    cutoff: str
    feature_names: tuple[str, ...]
    values: tuple[float, ...]
    source_evidence: tuple[dict, ...]
    snapshot_sha256: str
    source_ids: tuple[tuple[str, tuple[str, ...]], ...]
    feature_version: str = FEATURE_VERSION

    @property
    def features(self):
        return dict(zip(self.feature_names, self.values))

    @property
    def snapshot(self):
        return {"symbol": self.symbol, "cutoff": self.cutoff,
                "feature_version": self.feature_version, "feature_policy": FEATURE_POLICY,
                "feature_names": list(self.feature_names), "features": list(self.values),
                "source_ids": {k: list(v) for k, v in self.source_ids}, "sources": deepcopy(list(self.source_evidence)),
                "missing_encoding": "zero_placeholder_plus_per_value_missing_indicator"}


def _number(row, *names, nonnegative=False, positive=False):
    for name in names:
        value = row.get(name)
        if value is None or isinstance(value, bool):
            continue
        try:
            value = float(value)
        except (ValueError, TypeError, OverflowError):
            continue
        if math.isfinite(value) and not (nonnegative and value < 0) and not (positive and value <= 0):
            return value
    return None


def _row_clock(row, cutoff, *, kind, receipt):
    """Return the true event/publication clock, or an explicit exclusion reason."""
    try:
        event = instant(row["event_at"])
        available = instant(row.get("available_at", receipt.isoformat()))
        if event > cutoff or available > cutoff:
            return None, "future_row_clock"
        for name in ("receipt_at", "quote_timestamp", "trade_timestamp", "underlying_quote_timestamp",
                     "bar_end_at", "bar_end_timestamp"):
            if row.get(name) is not None and instant(row[name]) > cutoff:
                return None, "future_row_clock"
        if kind in {"fundamentals", "events", "macro"}:
            published = instant(row["published_at"])
            if published > cutoff:
                return None, "future_publication"
            if row.get("publication_time_basis") == "receipt_only" and published < receipt:
                return None, "backdated_unknown_publication"
        if kind in _MARKET_KINDS and (cutoff - event).total_seconds() > 1800:
            return None, "stale_market_event"
    except (KeyError, ContractError):
        return None, "missing_or_invalid_row_clock"
    return event, None


def _row_identity(row, *, kind, event):
    if kind in {"options", "opra_options"}:
        identity = row.get("contract_symbol") or row.get("instrument_id")
        if identity is None:
            return None
        if row.get("trade_id") is not None:
            return (str(identity), event.isoformat(), str(row["trade_id"]))
        return (str(identity), event.isoformat(), _sequence(row))
    if kind == "stock_trades":
        # Distinct trades can share an event timestamp and identical economics.
        # Without a provider ID/sequence it is unsafe to deduplicate or sum them.
        identity = row.get("trade_id", row.get("sequence", row.get("id")))
        return None if identity is None else (str(identity), event.isoformat())
    if kind in {"fundamentals", "events", "macro"}:
        identity = row.get("id") or row.get("event_id") or row.get("endpoint")
        if identity is None:
            return (_hash(row), event.isoformat())
        return (str(identity), str(row.get("period_end", row.get("date", row.get("event_date", "")))), event.isoformat())
    return (str(row.get("symbol", "")), event.isoformat(), _sequence(row))


def _sequence(row):
    raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
    value = row.get("sequence", raw.get("sequence"))
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return -1


def _rows(snapshot, *, symbol, cutoff, kind):
    records = snapshot.get("records", [])
    if not isinstance(records, list) or len(records) > _MAX_ROWS:
        raise ContractError("Market context snapshot records exceed the bounded list contract")
    selected, excluded = {}, defaultdict(int)
    receipt = instant(snapshot["receipt_at"])
    delivery = snapshot.get("evidence") or {}
    delayed_delivery = delivery.get("is_delayed") is True or delivery.get("realtime") is False
    for source in records:
        if not isinstance(source, Mapping):
            excluded["invalid_row"] += 1
            continue
        row = dict(source)
        # Normalization must not hide explicit source provenance. Absence of a
        # marker is not a no-trade attestation; present markers must be Boolean
        # false in both the projection and retained native record.
        marker_fields = ("synthetic", "is_synthetic", "no_trade", "is_no_trade",
                         "source_empty", "is_source_empty")
        native = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
        if any(field in value and value[field] is not False
               for value in (row, native) for field in marker_fields):
            excluded["synthetic_or_unverified_observation_marker"] += 1
            continue
        bar_observation = (kind == "stock_bars" or
            (kind in {"cross_asset", "opra_options"} and snapshot.get("schema") in _BAR_SECONDS))
        trade_observation = kind == "stock_trades" or (kind in {"opra_options", "cross_asset"} and snapshot.get("schema") == "trades")
        volume_field = "volume" if bar_observation or "size" not in row else "size"
        if (bar_observation or trade_observation) and _number(row, volume_field, positive=True) is None:
            excluded["missing_or_nonpositive_observed_volume"] += 1
            continue
        if bar_observation:
            op, hi, lo, cl = (_number(row, field, positive=True) for field in ("open", "high", "low", "close"))
            if None in (op, hi, lo, cl) or not lo <= min(op, cl) <= max(op, cl) <= hi:
                excluded["invalid_observed_ohlc"] += 1
                continue
        if delayed_delivery or row.get("is_delayed") is True or row.get("realtime") is False:
            excluded["provider_delayed"] += 1
            continue
        row_symbol = row.get("underlying_symbol") if kind in {"options", "opra_options"} else row.get("symbol")
        if kind != "cross_asset" and row_symbol not in ({symbol, "*"} if kind in {"events", "macro"} else {symbol}):
            excluded["wrong_or_missing_row_symbol"] += 1
            continue
        event, reason = _row_clock(row, cutoff, kind=kind, receipt=receipt)
        if reason:
            excluded[reason] += 1
            continue
        if kind == "technical":
            try:
                if (row.get("timeframe") != "15min" or row.get("period_length") != 10
                        or instant(row["bar_end_at"]) != event + timedelta(minutes=15)):
                    raise ContractError("Wrong technical bar contract")
            except (KeyError, ContractError):
                excluded["invalid_technical_bar_contract"] += 1
                continue
        if row.get("observed_trade_count") is not None:
            try:
                count = row["observed_trade_count"]
                first, last = instant(row["first_event_at"]), instant(row["last_event_at"])
                if ((snapshot.get("evidence") or {}).get("projection_version") != PROJECTION_VERSION
                        or isinstance(count, bool) or not isinstance(count, int) or count < 1
                        or not instant(snapshot["requested_start"]) <= first <= last < instant(snapshot["requested_end"])
                        or last != event or last > cutoff):
                    raise ContractError("Invalid native trade projection")
            except (KeyError, ContractError):
                excluded["invalid_native_trade_projection"] += 1
                continue
        if kind == "fundamentals":
            period = str(row.get("period_end", row.get("date", "")))[:10]
            if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", period):
                try:
                    if date.fromisoformat(period) > cutoff.date():
                        excluded["future_financial_period"] += 1
                        continue
                except ValueError:
                    excluded["invalid_financial_period"] += 1
                    continue
        opra_incremental = (kind == "opra_options" and
                            (snapshot.get("schema") == "trades" or str(snapshot.get("schema", "")).startswith("ohlcv-")))
        if kind in {"stock_trades", "stock_bars", "cross_asset"} or opra_incremental:
            try:
                start, end = instant(snapshot["requested_start"]), instant(snapshot["requested_end"])
                mode = (snapshot.get("evidence") or {}).get("mode")
                current_rest = ((kind in {"stock_trades", "cross_asset"} and mode == "current_rest_snapshot")
                    or (kind in {"stock_trades", "stock_bars", "cross_asset"} and mode == "current_stream_snapshot"))
                sampled_quote = snapshot.get("schema") in {"bbo-1m", "bbo-1s", "cbbo-1m", "cbbo-1s"}
                in_window = start <= event < end
                if sampled_quote:
                    # BBO sampling clocks name the end of their preceding
                    # sampling interval. An end-boundary quote is causal once
                    # received and must not be dropped as a future tick.
                    sample_start, sample_end = instant(row["interval_start"]), instant(row["interval_end"])
                    duration = timedelta(seconds=60 if snapshot["schema"].endswith("1m") else 1)
                    in_window = (start <= sample_start < sample_end <= end and sample_end == event
                                 and sample_end - sample_start == duration)
                # REST snapshots observe the fetch instant, which normally
                # follows the quarter just closed. They are causal at receipt,
                # but must never be represented as that quarter's full tape.
                if not current_rest and not in_window:
                    excluded["outside_requested_interval"] += 1
                    continue
                # OHLC event time is its opening. A provider row at an old
                # opening can still expose an unfinished bar's future close.
                duration = _BAR_SECONDS.get(snapshot.get("schema"))
                bar_end = row.get("bar_end_at", row.get("bar_end_timestamp"))
                is_bar = (kind == "stock_bars" or (opra_incremental and snapshot.get("schema") != "trades")
                          or (kind == "cross_asset" and snapshot.get("schema") in _BAR_SECONDS))
                if is_bar and not duration and not bar_end:
                    excluded["unknown_source_bar_duration"] += 1
                    continue
                finish = instant(bar_end) if bar_end else event + timedelta(seconds=duration or 0)
                if is_bar and finish > (cutoff if mode == "current_stream_snapshot" else min(cutoff, end)):
                    excluded["unfinished_source_bar"] += 1
                    continue
            except (KeyError, ContractError):
                excluded["invalid_request_interval"] += 1
                continue
        if kind in {"options", "opra_options"} and row.get("expiration_at"):
            try:
                if instant(row["expiration_at"]) <= cutoff:
                    excluded["expired_option"] += 1
                    continue
            except ContractError:
                excluded["invalid_expiration"] += 1
                continue
        identity = _row_identity(row, kind=kind, event=event)
        if identity is None:
            excluded["missing_observation_identity"] += 1
            continue
        previous = selected.get(identity)
        if previous is not None and _json(previous[1]) != _json(row):
            raise ContractError("Conflicting market observations share one source identity")
        if previous is not None:
            excluded["identical_duplicate"] += 1
        selected[identity] = (event, row)
    return sorted(selected.values(), key=lambda x: (x[0], _sequence(x[1]), _json(x[1]))), dict(sorted(excluded.items()))


def _quote(rows):
    for _, row in reversed(rows):
        bid, ask = _number(row, "bid", positive=True), _number(row, "ask", positive=True)
        if bid is None or ask is None or ask < bid:
            continue
        mid = (bid + ask) / 2
        result = {"relative_spread": (ask - bid) / mid}
        bs, az = _number(row, "bid_size", nonnegative=True), _number(row, "ask_size", nonnegative=True)
        if bs is not None and az is not None and bs + az > 0:
            result["depth_imbalance"] = (bs - az) / (bs + az)
        last = _number(row, "last", positive=True)
        if last is not None:
            result["last_mid_return"] = last / mid - 1
        depth_bid, depth_ask = (_number(row, side + "_depth_volume", nonnegative=True) for side in ("bid", "ask"))
        if depth_bid is not None and depth_ask is not None and depth_bid + depth_ask > 0:
            result["book_depth_imbalance"] = (depth_bid - depth_ask) / (depth_bid + depth_ask)
        bid_levels, ask_levels = (_number(row, side + "_depth_levels", positive=True) for side in ("bid", "ask"))
        if bid_levels is not None and ask_levels is not None:
            result["log_book_depth_levels"] = math.log1p(bid_levels + ask_levels)
        return result
    return {}


def _prices(rows, *, trades):
    observations = []
    for event, row in rows:
        if trades:
            if row.get("observed_trade_count") is not None:
                op, cl = _number(row, "first_price", positive=True), _number(row, "last_price", positive=True)
                count = row["observed_trade_count"]
            else:
                op = cl = _number(row, "price", "last", positive=True)
                count = 1
            volume = _number(row, "size" if "size" in row else "volume", positive=True)
        else:
            count = 1
            op, cl = _number(row, "open", positive=True), _number(row, "close", positive=True)
            volume = _number(row, "volume", positive=True)
            hi, lo = _number(row, "high", positive=True), _number(row, "low", positive=True)
            if None in (op, cl, hi, lo) or not lo <= min(op, cl) <= max(op, cl) <= hi:
                continue
        if None not in (op, cl, volume):
            observations.append((event, op, cl, volume, count))
    if not observations:
        return {}
    return {"log_observation_count": math.log1p(sum(x[4] for x in observations)),
            "log_observed_volume": math.log1p(sum(x[3] for x in observations)),
            "price_return": observations[-1][2] / observations[0][1] - 1}


def _options(rows, *, incremental_volume=False, cumulative_stream=False):
    # One latest causal observation per contract; repeated quote updates must
    # not inflate cross-sectional option counts or cumulative daily volume.
    latest = {}
    for event, row in rows:
        latest[str(row.get("contract_symbol", row.get("instrument_id")))] = (event, row)
    result = {}
    if not latest:
        return result
    result["log_contract_count"] = math.log1p(len(latest))
    for source, target in (("implied_volatility", "iv_median"), ("delta", "delta_median"),
                           ("gamma", "gamma_median"), ("theta", "theta_median"), ("vega", "vega_median")):
        values = [_number(row, source, nonnegative=source in {"implied_volatility", "gamma", "vega"})
                  for _, row in latest.values()
                  if source != "implied_volatility" or row.get("implied_volatility_unit", "decimal") == "decimal"]
        values = [value for value in values if value is not None and (source != "delta" or abs(value) <= 1)]
        if values:
            result[target] = median(values)
    volumes = {"PUT": [], "CALL": []}
    # Schwab chain volume is cumulative per contract; OPRA trade size and
    # bounded bar volume are incremental observations within the source window.
    for _, row in (rows if incremental_volume else latest.values()):
        side = str(row.get("option_type", "")).upper()
        if cumulative_stream:
            side = {"P": "PUT", "C": "CALL"}.get(side, side)
        volume = _number(row, *(('day_volume', 'volume') if cumulative_stream else ('volume',)), nonnegative=True)
        if side in volumes and volume is not None:
            volumes[side].append(volume)
    if all(volumes.values()):
        result["put_call_volume_log_ratio"] = math.log1p(sum(volumes["PUT"])) - math.log1p(sum(volumes["CALL"]))
    # The exact selected source's observed contracts only. A partial book is
    # neither the full chain nor an option-market-wide liquidity estimate.
    quote_values = [_quote([item]) for item in latest.values()]
    for source, target in (("relative_spread", "relative_spread_median"),
                           ("depth_imbalance", "top_depth_imbalance_median"),
                           ("book_depth_imbalance", "book_depth_imbalance_median")):
        values = [value[source] for value in quote_values if source in value]
        if values:
            result[target] = median(values)
    return result


def _fundamentals(rows, *, cutoff, version=LEGACY_FEATURE_VERSION, schema=None):
    # Latest actually known value for each metric. This never reassigns current
    # financials to their old fiscal period as an availability timestamp.
    result = {}
    candidates = defaultdict(list)
    ordered = sorted(rows, key=lambda item: (str(item[1].get("period_end", item[1].get("date", ""))), item[0]))
    for event, row in ordered:
        direct = {"earnings_yield": _number(row, "earnings_yield", "earningsYield"),
                  "debt_equity": _number(row, "debt_equity", "debtToEquity", "debtEquityRatio"),
                  "profit_margin": _number(row, "profit_margin", "netProfitMargin", "netProfitMarginTTM"),
                  "revenue_growth": _number(row, "revenue_growth", "revenueGrowth")}
        pe = _number(row, "pe_ratio", "pe", "priceToEarningsRatio")
        if direct["earnings_yield"] is None and pe not in (None, 0):
            direct["earnings_yield"] = 1 / pe
        cap = _number(row, "market_cap", "marketCap", "mktCap", nonnegative=True)
        if cap is not None:
            direct["log_market_cap"] = math.log1p(cap)
        for name in _FIELDS["fundamentals"][5:]:
            if name in {"analyst_eps_estimate", "analyst_revenue_estimate"}:
                continue
            direct[name] = _number(row, name, nonnegative=name not in {"dcf_value", "dcf_upside_fraction", "analyst_eps_estimate"})
        if direct["analyst_bullish_fraction"] is not None and direct["analyst_bullish_fraction"] > 1:
            direct["analyst_bullish_fraction"] = None
        result.update({k: v for k, v in direct.items() if v is not None})
        if version == FEATURE_VERSION:
            # Source-specific same-record fields. Never join a current balance
            # sheet to a different income period to manufacture a ratio.
            extra = {}
            shares = _number(row, "float_shares", "floatShares", positive=True)
            outstanding = _number(row, "outstanding_shares", "outstandingShares", positive=True)
            if shares is not None:
                extra["log_float_shares"] = math.log1p(shares)
            if outstanding is not None:
                extra["log_outstanding_shares"] = math.log1p(outstanding)
            if shares is not None and outstanding is not None and shares <= outstanding:
                extra["free_float_fraction"] = shares / outstanding
            aliases = {
                "eps": ("eps",), "diluted_eps": ("eps_diluted", "epsDiluted", "dilutedEPS"),
                "revenue_growth": ("growth_revenue", "growthRevenue"),
                "operating_cash_flow_growth": ("growth_operating_cash_flow", "growthOperatingCashFlow"),
                "free_cash_flow_growth": ("growth_free_cash_flow", "growthFreeCashFlow"),
            }
            for name, keys in aliases.items():
                value = _number(row, *keys)
                if value is not None:
                    extra[name] = value
            for name, numerator, denominator in (
                ("profit_margin", ("net_income", "netIncome"), ("revenue",)),
                ("debt_equity", ("total_debt", "totalDebt"), ("total_stockholders_equity", "totalStockholdersEquity")),
                ("cash_assets_fraction", ("cash_and_cash_equivalents", "cashAndCashEquivalents"), ("total_assets", "totalAssets")),
            ):
                top = _number(row, *numerator)
                bottom = _number(row, *denominator, positive=name != "debt_equity")
                if top is not None and bottom not in (None, 0):
                    value = top / bottom
                    if name != "cash_assets_fraction" or 0 <= value <= 1:
                        extra[name] = value
            for name, keys in (
                ("operating_cash_flow", ("operating_cash_flow", "operatingCashFlow")),
                ("free_cash_flow", ("free_cash_flow", "freeCashFlow")),
                ("capital_expenditure", ("capital_expenditure", "capitalExpenditure")),
            ):
                value = _number(row, *keys)
                if value is not None:
                    extra["signed_log_" + name] = math.copysign(math.log1p(abs(value)), value)
            for name, keys in (
                ("overall", ("overall_score", "overallScore")),
                ("dcf", ("discounted_cash_flow_score", "discountedCashFlowScore")),
                ("roe", ("return_on_equity_score", "returnOnEquityScore")),
                ("roa", ("return_on_assets_score", "returnOnAssetsScore")),
                ("debt_equity", ("debt_to_equity_score", "debtToEquityScore")),
                ("pe", ("price_to_earnings_score", "priceToEarningsScore")),
                ("pb", ("price_to_book_score", "priceToBookScore")),
            ):
                value = _number(row, *keys, nonnegative=True)
                if value is not None:
                    extra[name + "_rating_score"] = value
            for period, native, snake in (("month", "lastMonth", "last_month"),
                    ("quarter", "lastQuarter", "last_quarter"), ("year", "lastYear", "last_year"),
                    ("all_time", "allTime", "all_time")):
                average = _number(row, native + "AvgPriceTarget", snake + "_avg_price_target", positive=True)
                count = _number(row, native + "Count", snake + "_count", nonnegative=True)
                if count is not None and not count.is_integer():
                    count = None
                if count is not None:
                    extra["target_" + period + "_count"] = count
                if average is not None and count != 0:
                    extra["target_" + period + "_average"] = average
            key = (str(row.get("period_end", row.get("date", ""))), event)
            for name, value in {**{k:v for k,v in direct.items() if v is not None}, **extra}.items():
                candidates[name].append((key, value))
    if version == FEATURE_VERSION:
        result = {}
        for name, values in candidates.items():
            latest = max(key for key, _ in values)
            observed = {value for key, value in values if key == latest}
            # A conflicting latest revision is unavailable. An older value or
            # deterministic row ordering cannot resolve provider disagreement.
            if len(observed) == 1:
                result[name] = observed.pop()
    estimates = []
    for event, row in rows:
        try:
            period = date.fromisoformat(str(row["analyst_estimate_period_end"])[:10])
        except (KeyError, ValueError):
            continue
        if period >= cutoff.date():
            estimates.append((period, event, row))
    if estimates:
        period = min(item[0] for item in estimates)
        latest = max(item[1] for item in estimates if item[0] == period)
        selected = [item[2] for item in estimates if item[:2] == (period, latest)]
        for field in ("analyst_eps_estimate", "analyst_revenue_estimate"):
            values = {_number(row, field, nonnegative=field == "analyst_revenue_estimate") for row in selected}
            values.discard(None)
            if len(values) == 1:
                result[field] = values.pop()
    if version == FEATURE_VERSION and schema == "sec_filings_search_symbol" and rows:
        result["log_observed_filing_count"] = math.log1p(len(rows))
    return result


def _technical(rows):
    result = {}
    for _, row in rows:
        name = row.get("technical_name")
        if name not in _FIELDS["technical"]:
            continue
        value = _number(row, "technical_value")
        if value is None or (name in {"rsi", "adx"} and not 0 <= value <= 100):
            continue
        if name == "williams" and not -100 <= value <= 0:
            continue
        if name not in {"williams"} and value < 0:
            continue
        result[name] = value
    return result


def _macro(rows, *, cutoff):
    result = {}
    fields = {"GDP": "gdp_value", "CPI": "cpi_value", "unemploymentRate": "unemployment_rate", "federalFunds": "federal_funds_rate"}
    for _, row in sorted(rows, key=lambda item: (str(item[1].get("macro_period_date", "")), item[0])):
        try:
            period = date.fromisoformat(str(row["macro_period_date"])[:10])
        except (KeyError, ValueError):
            continue
        value, field = _number(row, "macro_value"), fields.get(row.get("macro_series"))
        if field and value is not None and period <= cutoff.date() and row.get("macro_unit"):
            result[field] = value
    return result


def _events(rows, cutoff):
    result = {"log_observed_event_count": math.log1p(len(rows))} if rows else {}
    if rows:
        for kind, field in (("stock_news", "log_observed_news_count"), ("press_release", "log_observed_press_release_count")):
            result[field] = math.log1p(sum(str(row.get("event_type", "")).lower() == kind for _, row in rows))
    earnings, splits = [], []
    for _, row in rows:
        try:
            when = instant(row["event_date"])
        except (KeyError, ContractError):
            continue
        days = (when - cutoff).total_seconds() / 86400
        event_type = str(row.get("event_type", "")).lower()
        if event_type == "earnings" and days >= 0:
            earnings.append(days)
        if event_type == "split" and -30 <= days <= 0:
            splits.append(days)
    if earnings:
        result["next_earnings_days"] = min(earnings)
    if rows:
        result["split_events_past30d"] = float(len(splits))
    return result


def _cross_asset(rows, *, schema, delivery=None):
    if schema == "commodity-quote":
        configured = (delivery or {}).get("configured_symbols")
        if not isinstance(configured, list) or not configured or any(not isinstance(s, str) or not s for s in configured):
            return {}
        latest = {row.get("symbol"): row for _, row in rows if row.get("symbol") in configured}
        returns = []
        for row in latest.values():
            raw = row.get("raw")
            if not isinstance(raw, Mapping):
                continue
            price, previous = _number(raw, "price", positive=True), _number(raw, "previousClose", positive=True)
            if price is not None and previous is not None:
                returns.append(price / previous - 1)
        if not returns:
            return {}
        result = {"commodity_quote_return_median": median(returns),
                  "commodity_log_instrument_count": math.log1p(len(returns))}
        if len(returns) >= 2:
            result["commodity_quote_return_dispersion"] = pstdev(returns)
        return result
    quote_schema = schema in {"bbo-1s", "bbo-1m", "mbp-1", "mbp-10", "tbbo", "cbbo-1s", "cbbo-1m", "cmbp-1", "LEVELONE_FUTURES"}
    if not quote_schema and schema != "trades" and schema not in _BAR_SECONDS:
        # MBO prices are order-level prices, and statistics mix settlement,
        # interest and other values. Neither proves an executable market price.
        return {}
    buckets = defaultdict(list)
    for event, row in rows:
        identity = str(row.get("symbol", ""))
        root = row.get("root_symbol")
        if root not in FUTURES_ROOTS:
            match = re.fullmatch(r"(ES|NQ|RTY|CL|GC|ZN|6E)(?:[._-].*|[FGHJKMNQUVXZ][0-9]{1,2})?", identity)
            root = match.group(1) if match else None
        if root:
            buckets[root].append((event, row))
    result = {}
    for root, observations in buckets.items():
        if len({str(row.get("symbol")) for _, row in observations}) != 1:
            # Never calculate a return across an unapproved contract roll.
            continue
        features = {} if quote_schema else _prices(observations, trades=schema == "trades")
        if quote_schema:
            mids = []
            for _, row in observations:
                bid, ask = _number(row, "bid", positive=True), _number(row, "ask", positive=True)
                if bid is not None and ask is not None and ask >= bid:
                    mids.append((bid + ask) / 2)
            if mids:
                features["price_return"] = mids[-1] / mids[0] - 1
        if "price_return" in features:
            result[f"{root}_return"] = features["price_return"]
    return result


def build_market_context(snapshots: Iterable[Mapping], *, symbol: str, cutoff,
                         source_ids: Mapping | None = None, contract=None) -> MarketContext:
    """Build a fixed feature vector using only receipts visible at ``cutoff``.

    ``source_ids`` freezes source IDs per kind; multiple sources receive distinct
    feature blocks with source-ID hash suffixes. If omitted, a kind with multiple
    visible source IDs is unavailable instead of silently selecting a provider.
    Selectors are part of the resulting fingerprint. Future receipts do not
    influence feature values, provenance, diagnostics, or that fingerprint.
    """
    return _build_resolved_context(snapshots, symbol=symbol, cutoff=cutoff,
        source_ids=source_ids, contract=_resolved_contract(source_ids, contract))


def _build_resolved_context(snapshots, *, symbol, cutoff, source_ids, contract):
    """Internal derivation after exact contract validation by the caller.

    The dataset cache retains an independent validated contract and rejects
    caller mutation on every reuse. Public callers always validate above.
    Receipt scope, clocks, hashes and row eligibility remain checked here.
    """
    cutoff = instant(cutoff)
    partial_records = contract["version"] == FEATURE_VERSION
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,19}", symbol):
        raise ContractError("Explicit equity symbol required for market context")
    selectors = _selectors(source_ids)
    grouped, seen = defaultdict(list), {}
    for snapshot in snapshots:
        if not isinstance(snapshot, Mapping) or snapshot.get("symbol") not in {symbol, "*"}:
            continue
        kind = snapshot.get("source_kind")
        if kind not in SOURCE_KINDS or (snapshot.get("symbol") == "*" and kind not in {"cross_asset", "events", "macro"}):
            continue
        try:
            receipt, available = instant(snapshot["receipt_at"]), instant(snapshot["available_at"])
        except (KeyError, ContractError):
            raise ContractError("Market snapshot needs actual receipt and availability timestamps") from None
        if receipt > cutoff or available > cutoff:
            continue
        if callable(getattr(snapshot, "load_verified", None)):
            snapshot = snapshot.load_verified()
        if available < receipt:
            raise ContractError("Market snapshot availability cannot precede actual receipt")
        if kind in selectors and snapshot.get("source_id") not in selectors[kind]:
            continue
        if snapshot.get("schema_version") != SNAPSHOT_VERSION:
            raise ContractError("Unsupported market snapshot contract")
        if not isinstance(snapshot.get("snapshot_id"), str) or not snapshot["snapshot_id"] or not snapshot.get("source_id"):
            raise ContractError("Market snapshot requires immutable source and snapshot identities")
        if snapshot.get("snapshot_sha256") != snapshot_digest(snapshot):
            raise ContractError("Market snapshot content hash mismatch")
        key = (snapshot["source_id"], snapshot["snapshot_id"])
        previous = seen.get(key)
        if previous is not None and previous != snapshot["snapshot_sha256"]:
            raise ContractError("Conflicting immutable market snapshots")
        if previous is not None:
            continue
        seen[key] = snapshot["snapshot_sha256"]
        grouped[kind].append(snapshot)
    measured, evidence, available_flags, partial_flags = {}, [], {}, {}
    slots = _slots(selectors)
    for kind, selected_id, prefix in slots:
        choices = [s for s in grouped[kind] if selected_id is None or s["source_id"] == selected_id]
        source_set = {s["source_id"] for s in choices}
        entry = {"source_kind": kind, "source_id": selected_id, "feature_prefix": prefix,
                 "status": "MISSING", "records_used": 0}
        available_flags[prefix] = partial_flags[prefix] = 0.0
        if len(source_set) > 1:
            entry.update(status="AMBIGUOUS_SOURCE", candidate_source_ids=sorted(source_set))
        elif choices:
            latest_time = max((instant(s["available_at"]), instant(s["receipt_at"])) for s in choices)
            latest = [s for s in choices if (instant(s["available_at"]), instant(s["receipt_at"])) == latest_time]
            if len({s["snapshot_sha256"] for s in latest}) > 1:
                raise ContractError("Conflicting market snapshots share the latest receipt clock")
            snapshot = latest[0]
            receipt = instant(snapshot["receipt_at"])
            age = (cutoff - receipt).total_seconds()
            status = snapshot.get("status")
            partial_flags[prefix] = float(status in {"PARTIAL", "CAPACITY_BLOCKED"})
            entry.update(source_id=snapshot["source_id"], snapshot_id=snapshot["snapshot_id"],
                         snapshot_sha256=snapshot["snapshot_sha256"], status=status,
                         receipt_at=snapshot["receipt_at"], available_at=snapshot["available_at"],
                         dataset=snapshot.get("dataset"), schema=snapshot.get("schema"))
            if partial_records:
                entry.update(coverage_complete=status == "COMPLETE",
                             records_received=len(snapshot.get("records", [])),
                             coverage_status=status,
                             coverage_evidence={key: deepcopy(value) for key, value in
                                 (snapshot.get("evidence") or {}).items() if key in {
                                     "mode", "completed", "coverage_claim", "omitted_scope_count",
                                     "configured_symbols", "service_results", "excluded_option_scopes"}})
            measured[f"{prefix}_receipt_age_seconds"] = age
            if age > _MAX_RECEIPT_AGE[kind]:
                entry["status"] = "STALE_RECEIPT"
            elif status in _SUCCESS or (partial_records and status == "PARTIAL"):
                rows, excluded = _rows(snapshot, symbol=symbol, cutoff=cutoff, kind=kind)
                entry.update(records_used=len(rows), exclusions=excluded)
                available_flags[prefix] = float(bool(rows) or status == "EMPTY")
                if rows:
                    measured[f"{prefix}_source_age_seconds"] = (cutoff - rows[-1][0]).total_seconds()
                    if kind == "stock_quote": values = _quote(rows)
                    elif kind in {"stock_trades", "stock_bars"}: values = _prices(rows, trades=kind == "stock_trades")
                    elif kind in {"options", "opra_options"}: values = _options(rows,
                        incremental_volume=kind == "opra_options" and snapshot.get("schema") in {"trades", "ohlcv-1s", "ohlcv-1m", "ohlcv-1h", "ohlcv-1d"},
                        cumulative_stream=partial_records and snapshot.get("schema") == "LEVELONE_OPTIONS")
                    elif kind == "fundamentals": values = _fundamentals(rows, cutoff=cutoff,
                        version=contract["version"], schema=snapshot.get("schema"))
                    elif kind == "events": values = _events(rows, cutoff)
                    elif kind == "technical": values = _technical(rows)
                    elif kind == "macro": values = _macro(rows, cutoff=cutoff)
                    else: values = _cross_asset(rows, schema=snapshot.get("schema"), delivery=snapshot.get("evidence"))
                    measured.update({f"{prefix}_{k}": v for k, v in values.items()})
        evidence.append(entry)
    if any(not math.isfinite(value) for value in measured.values()):
        raise ContractError("Context calculation produced nonfinite values")
    value_names, feature_names = _names(selectors, contract["version"])
    values = tuple(float(measured.get(name, 0.0)) for name in value_names)
    values += tuple(float(name not in measured) for name in value_names)
    values += tuple(available_flags[prefix] for _, _, prefix in slots)
    values += tuple(partial_flags[prefix] for _, _, prefix in slots)
    context = MarketContext(symbol, cutoff.isoformat(), feature_names, values,
                            tuple(evidence), "", tuple(sorted(selectors.items())), contract["version"])
    return replace(context, snapshot_sha256=_hash(context.snapshot))


def _snapshot_index(snapshots, *, symbol, source_ids):
    """Index receipt clocks once; do not repeatedly scan/hash all training history."""
    selectors, groups = _selectors(source_ids), defaultdict(list)
    for snapshot in snapshots:
        if not isinstance(snapshot, Mapping) or snapshot.get("symbol") not in {symbol, "*"}:
            continue
        kind, source = snapshot.get("source_kind"), snapshot.get("source_id")
        if kind not in SOURCE_KINDS or (kind in selectors and source not in selectors[kind]):
            continue
        if snapshot.get("symbol") == "*" and kind not in {"cross_asset", "events", "macro"}:
            continue
        try:
            receipt, available = instant(snapshot["receipt_at"]), instant(snapshot["available_at"])
        except (KeyError, ContractError):
            raise ContractError("Market snapshot needs actual receipt and availability timestamps") from None
        key = (max(receipt, available), available, receipt)
        groups[(kind, source)].append((key, snapshot))
    result = []
    for entries in groups.values():
        entries.sort(key=lambda entry: entry[0])
        result.append(([entry[0] for entry in entries], [entry[1] for entry in entries]))
    return result


def _indexed_candidates(index, cutoff):
    cutoff = instant(cutoff)
    output = []
    for clocks, snapshots in index:
        stop = bisect_right(clocks, (cutoff, cutoff, cutoff))
        if not stop:
            continue
        start = stop - 1
        while start and clocks[start - 1] == clocks[stop - 1]:
            start -= 1
        output.extend(snapshots[start:stop])
    return output


def augment_dataset(dataset, snapshots: Iterable[Mapping], *, training_decision_at: Mapping,
                    inference_cutoff, source_ids: Mapping | None = None, cache_path=None, contract=None):
    """Return a new Dataset, with per-row causal context appended explicitly.

    Historical cutoffs must be the reference's actual feature/decision receipts,
    never label_available_at (which includes the future target receipt). Missing
    context remains missing. This does not fetch, fit, rewrite saved artifacts,
    alter labels, or relax any training/calibration/support requirement.
    """
    snapshots = tuple(snapshots)
    contract = _resolved_contract(source_ids, contract)
    if any(name.startswith("ctx_") for name in dataset.feature_names):
        raise ContractError("Dataset already contains market context")
    decisions = {instant(k): instant(v) for k, v in training_decision_at.items()}
    index = _snapshot_index(snapshots, symbol=dataset.inference["symbol"], source_ids=source_ids)
    context_values = np.empty((len(dataset.training_reference_at), len(contract["feature_names"])), dtype=float)
    context_hashes, rows_with_source = [], 0
    manager = nullcontext()
    if cache_path is not None:
        from .market_context_cache import ContextCache
        manager = ContextCache(cache_path)
    with manager as cache:
        builder = cache.build if cache is not None else build_market_context
        for row_index, reference in enumerate(dataset.training_reference_at):
            reference = instant(reference)
            if reference not in decisions:
                raise ContractError("Every training reference requires its own decision/feature receipt cutoff")
            decision = decisions[reference]
            if not reference + timedelta(minutes=15) <= decision < reference + timedelta(minutes=30):
                raise ContractError("Historical context decision must follow reference close and precede target open")
            context = builder(_indexed_candidates(index, decision), symbol=dataset.inference["symbol"],
                              cutoff=decision, source_ids=source_ids, contract=contract)
            context_values[row_index] = context.values
            context_hashes.append(context.snapshot_sha256)
            rows_with_source += int(any(e.get("records_used") for e in context.source_evidence))
        inference = builder(_indexed_candidates(index, inference_cutoff), symbol=dataset.inference["symbol"],
                            cutoff=inference_cutoff, source_ids=source_ids, contract=contract)
    if instant(inference_cutoff) < instant(dataset.inference["information_available_at"]):
        raise ContractError("Inference context cutoff precedes known base feature availability")
    names = (*dataset.feature_names, *inference.feature_names)
    X = np.column_stack((dataset.X, context_values))
    inference_X = np.column_stack((dataset.inference_X, np.asarray([inference.values], dtype=float)))
    snapshot = {**deepcopy(dataset.inference["snapshot"]),
                "feature_names": list(names), "features": inference_X[0].tolist(),
                "market_context": inference.snapshot, "market_context_sha256": inference.snapshot_sha256}
    info = deepcopy(dataset.inference)
    info.update(features=inference_X[0].tolist(), snapshot=snapshot,
                snapshot_sha256=_hash(snapshot), market_context_feature_version=contract["version"],
                market_context_sha256=inference.snapshot_sha256)
    receipts = [instant(e["available_at"]) for e in inference.source_evidence if e.get("records_used")]
    if receipts:
        info["information_available_at"] = max(instant(info["information_available_at"]), *receipts).isoformat()
    manifest = deepcopy(dataset.manifest)
    manifest["market_context"] = {
        "contract": contract,
        "training_sha256": _hash(context_hashes),
        "training_rows_with_source": rows_with_source,
        "inference_sources": deepcopy(list(inference.source_evidence)),
        "inference_sha256": inference.snapshot_sha256,
    }
    return replace(dataset, X=X, inference_X=inference_X, feature_names=names, inference=info, manifest=manifest)
