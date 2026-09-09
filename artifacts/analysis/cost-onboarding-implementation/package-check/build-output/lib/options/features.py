from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from datafetching.layout import safe_token

OPTION_FEATURE_VERSION = "1.2.0"
OPTION_FEATURE_SCHEMA_VERSION = "option-surface-v2"
OPTION_SELECTION_POLICY_VERSION = "schwab-tenor-delta-selection-v1"
OPTION_SURFACE_QUALITY_POLICY_VERSION = "schwab-option-surface-quality-v1"
OPTION_REALIZED_VOLATILITY_POLICY_VERSION = "databento-1d-rv20-v1"
OPTION_MAX_QUOTE_STALENESS_SECONDS = 15 * 60
OPTION_MIN_QUOTE_COVERAGE = 0.90
OPTION_MIN_QUOTE_TIME_COVERAGE = 0.95
OPTION_MIN_IV_COVERAGE = 0.80
OPTION_MIN_GREEKS_COVERAGE = 0.80
OPTION_MIN_OPEN_INTEREST_COVERAGE = 0.80
_NUMERIC_COLUMNS = (
    "strike",
    "underlying_price",
    "bid",
    "ask",
    "mark",
    "volume",
    "open_interest",
    "implied_volatility",
    "delta",
    "gamma",
    "theta",
    "vega",
    "intrinsic_value",
    "relative_bid_ask_spread",
    "quote_staleness_seconds",
    "days_to_expiration",
    "interest_rate",
    "dividend_yield",
)


@dataclass(frozen=True)
class RealizedVolatilityEvidence:
    value: float | None
    source_provider: str = "databento"
    source_timeframe: str = "1d"
    source_calculation: str = "market-regime"
    source_calculation_version: str = ""
    source_available_at: pd.Timestamp | None = None
    source_observation_count: int = 0
    source_file: str = ""
    price_adjustment_status: str = ""
    split_event_count: int | None = None
    policy_version: str = OPTION_REALIZED_VOLATILITY_POLICY_VERSION


def calculate_option_snapshot_features(
    contracts: pd.DataFrame,
    *,
    realized_volatility: float | None = None,
    realized_volatility_evidence: RealizedVolatilityEvidence | None = None,
) -> pd.DataFrame:
    if contracts.empty:
        raise ValueError("Option snapshot features require at least one contract.")

    frame = contracts.copy()
    for column in _NUMERIC_COLUMNS:
        if column not in frame.columns:
            frame[column] = np.nan
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    for column in (
        "snapshot_for",
        "decision_timestamp",
        "fetched_at",
        "available_at",
        "quote_cutoff_at",
        "quote_timestamp",
        "underlying_quote_timestamp",
    ):
        if column in frame.columns:
            frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce")

    snapshot_for = _single_timestamp(
        frame,
        "snapshot_for",
        fallback="decision_timestamp",
    )
    available_at = _single_timestamp(frame, "available_at", fallback="fetched_at")
    quote_cutoff_at = _single_timestamp(
        frame,
        "quote_cutoff_at",
        fallback="available_at",
    )
    if available_at < quote_cutoff_at:
        raise ValueError("Option surface availability cannot precede its quote cutoff")

    quote_timestamp = (
        frame["quote_timestamp"]
        if "quote_timestamp" in frame.columns
        else pd.Series(pd.NaT, index=frame.index, dtype="datetime64[ns, UTC]")
    )
    underlying_quote_timestamp = _single_optional_timestamp(
        frame,
        "underlying_quote_timestamp",
    )
    quote_after_cutoff = quote_timestamp.notna() & quote_timestamp.gt(quote_cutoff_at)
    underlying_after_cutoff = (
        underlying_quote_timestamp is not None
        and underlying_quote_timestamp > quote_cutoff_at
    )
    crossed = (
        frame["bid"].notna()
        & frame["ask"].notna()
        & frame["ask"].lt(frame["bid"])
    )
    locked = (
        frame["bid"].notna()
        & frame["ask"].notna()
        & frame["ask"].eq(frame["bid"])
    )
    quote_mid = (frame["bid"] + frame["ask"]) / 2.0
    nonpositive_mid = quote_mid.notna() & quote_mid.le(0)
    valid_quote = (
        frame["bid"].notna()
        & frame["ask"].notna()
        & quote_mid.gt(0)
        & ~crossed
        & ~locked
        & ~quote_after_cutoff
    )
    causal = frame.loc[
        quote_timestamp.notna() & ~quote_after_cutoff
    ].copy()

    evidence = realized_volatility_evidence or RealizedVolatilityEvidence(
        value=realized_volatility
    )
    effective_realized_volatility = (
        realized_volatility
        if realized_volatility is not None
        else evidence.value
    )

    underlying_price = _series_first(causal["underlying_price"])
    pairs = _paired_contracts(causal)
    atm_pair = _atm_pair(pairs, underlying_price)
    atm_call = atm_pair.get("call") if atm_pair else None
    atm_put = atm_pair.get("put") if atm_pair else None
    atm_mid = _sum_values(_row_value(atm_call, "mark"), _row_value(atm_put, "mark"))
    atm_ask = _sum_values(_row_value(atm_call, "ask"), _row_value(atm_put, "ask"))
    atm_iv = _mean_finite(
        [_row_value(atm_call, "implied_volatility"), _row_value(atm_put, "implied_volatility")]
    )
    atm_dte = _coalesce(
        _row_value(atm_call, "days_to_expiration"),
        _row_value(atm_put, "days_to_expiration"),
    )
    atm_implied_move = _safe_divide(atm_ask, underlying_price)
    realized_expected_absolute_move = _expected_absolute_move(
        effective_realized_volatility,
        atm_dte,
    )

    expiry_atm_iv = _atm_iv_by_expiration(causal, underlying_price)
    front_iv = expiry_atm_iv[0][1] if expiry_atm_iv else None
    back_iv = expiry_atm_iv[1][1] if len(expiry_atm_iv) > 1 else None
    wing = _delta_wing_features(causal)
    parity = _put_call_parity_features(pairs, underlying_price)
    open_interest = causal["open_interest"].fillna(0.0).clip(lower=0.0)
    total_oi = float(open_interest.sum())
    total_volume = float(causal["volume"].fillna(0.0).clip(lower=0.0).sum())
    hhi = float(((open_interest / total_oi) ** 2).sum()) if total_oi > 0 else None
    intrinsic_violation = (
        causal["mark"].notna()
        & causal["intrinsic_value"].notna()
        & causal["mark"].lt(causal["intrinsic_value"] - 0.01)
    )
    calls = causal.loc[causal["call_put"].eq("CALL")]
    puts = causal.loc[causal["call_put"].eq("PUT")]

    quote_coverage = float(valid_quote.mean())
    quote_time_coverage = float(quote_timestamp.notna().mean())
    iv_coverage = float(causal["implied_volatility"].notna().mean())
    greeks_coverage = float(
        causal[["delta", "gamma", "theta", "vega"]].notna().all(axis=1).mean()
    )
    open_interest_coverage = float(causal["open_interest"].notna().mean())
    max_quote_staleness_seconds = _max_finite(causal["quote_staleness_seconds"])
    quote_cutoff_pass = (
        not bool(quote_after_cutoff.any())
        and underlying_quote_timestamp is not None
        and not underlying_after_cutoff
    )
    surface_quality_pass = bool(
        quote_cutoff_pass
        and quote_coverage >= OPTION_MIN_QUOTE_COVERAGE
        and quote_time_coverage >= OPTION_MIN_QUOTE_TIME_COVERAGE
        and iv_coverage >= OPTION_MIN_IV_COVERAGE
        and greeks_coverage >= OPTION_MIN_GREEKS_COVERAGE
        and open_interest_coverage >= OPTION_MIN_OPEN_INTEREST_COVERAGE
        and max_quote_staleness_seconds is not None
        and max_quote_staleness_seconds <= OPTION_MAX_QUOTE_STALENESS_SECONDS
        and not bool(crossed.any())
        and not bool(locked.any())
        and not bool(nonpositive_mid.any())
        and not bool(intrinsic_violation.any())
    )

    row = {
        "symbol": str(frame["symbol"].iloc[-1]).strip().upper(),
        "source": "schwab",
        "snapshot_for": snapshot_for,
        "decision_timestamp": snapshot_for,
        "decision_bar_timestamp": pd.to_datetime(frame["decision_bar_timestamp"].iloc[-1], utc=True),
        "decision_provider": str(frame["decision_provider"].iloc[-1]),
        "decision_timeframe": str(frame["decision_timeframe"].iloc[-1]),
        "quote_cutoff_at": quote_cutoff_at,
        "underlying_quote_timestamp": underlying_quote_timestamp,
        "fetched_at": available_at,
        "available_at": available_at,
        "decision_lag_seconds": _series_first(frame["decision_lag_seconds"]),
        "underlying_price": underlying_price,
        "contract_count": len(frame),
        "expiration_count": int(frame["expiration_date"].nunique(dropna=True)),
        "relative_bid_ask_spread": _median(frame["relative_bid_ask_spread"]),
        "atm_relative_bid_ask_spread": _mean_finite(
            [_row_value(atm_call, "relative_bid_ask_spread"), _row_value(atm_put, "relative_bid_ask_spread")]
        ),
        "atm_strike": _coalesce(_row_value(atm_call, "strike"), _row_value(atm_put, "strike")),
        "atm_days_to_expiration": atm_dte,
        "atm_straddle_mid": atm_mid,
        "atm_straddle_ask": atm_ask,
        "atm_straddle_implied_move": atm_implied_move,
        "realized_expected_absolute_move_atm_horizon": realized_expected_absolute_move,
        "atm_straddle_move_excess": _difference(
            atm_implied_move,
            realized_expected_absolute_move,
        ),
        "atm_straddle_move_richness": _safe_divide(
            atm_implied_move,
            realized_expected_absolute_move,
        ),
        "atm_implied_volatility": atm_iv,
        "realized_volatility_20d": effective_realized_volatility,
        "iv_minus_realized_volatility": _difference(
            atm_iv,
            effective_realized_volatility,
        ),
        "front_atm_implied_volatility": front_iv,
        "back_atm_implied_volatility": back_iv,
        "front_iv_minus_back_iv": _difference(front_iv, back_iv),
        "put_25d_implied_volatility": wing["put_iv"],
        "call_25d_implied_volatility": wing["call_iv"],
        "put_25d_iv_minus_call_25d_iv": _difference(wing["put_iv"], wing["call_iv"]),
        "smile_curvature": _difference(_mean_finite([wing["put_iv"], wing["call_iv"]]), wing["atm_iv"]),
        "open_interest_concentration": hhi,
        "volume_to_open_interest": _safe_divide(total_volume, total_oi),
        "call_put_volume_ratio": _safe_divide(_total(calls, "volume"), _total(puts, "volume")),
        "call_put_open_interest_ratio": _safe_divide(
            _total(calls, "open_interest"),
            _total(puts, "open_interest"),
        ),
        "put_call_parity_residual": parity["median_residual"],
        "atm_put_call_parity_residual": parity["atm_residual"],
        "intrinsic_value_violation": bool(intrinsic_violation.any()),
        "intrinsic_value_violation_rate": float(intrinsic_violation.mean()),
        "quote_coverage": quote_coverage,
        "quote_time_coverage": quote_time_coverage,
        "iv_coverage": iv_coverage,
        "greeks_coverage": greeks_coverage,
        "open_interest_coverage": open_interest_coverage,
        "quote_staleness_seconds": _median(causal["quote_staleness_seconds"]),
        "max_quote_staleness_seconds": max_quote_staleness_seconds,
        "quote_after_cutoff_count": int(quote_after_cutoff.sum()),
        "underlying_quote_after_cutoff": bool(underlying_after_cutoff),
        "crossed_quote_count": int(crossed.sum()),
        "locked_quote_count": int(locked.sum()),
        "nonpositive_mid_count": int(nonpositive_mid.sum()),
        "quote_cutoff_pass": quote_cutoff_pass,
        "surface_quality_pass": surface_quality_pass,
        "surface_quality_policy_version": OPTION_SURFACE_QUALITY_POLICY_VERSION,
        "selection_policy_version": OPTION_SELECTION_POLICY_VERSION,
        "realized_volatility_policy_version": evidence.policy_version,
        "realized_volatility_source_provider": evidence.source_provider,
        "realized_volatility_source_timeframe": evidence.source_timeframe,
        "realized_volatility_source_calculation": evidence.source_calculation,
        "realized_volatility_source_calculation_version": (
            evidence.source_calculation_version
        ),
        "realized_volatility_source_available_at": evidence.source_available_at,
        "realized_volatility_source_observation_count": (
            evidence.source_observation_count
        ),
        "realized_volatility_source_file": evidence.source_file,
        "realized_volatility_price_adjustment_status": (
            evidence.price_adjustment_status
        ),
        "realized_volatility_split_event_count": evidence.split_event_count,
        "calculation": "option-quality",
        "calculation_version": OPTION_FEATURE_VERSION,
        "schema_version": OPTION_FEATURE_SCHEMA_VERSION,
    }
    return pd.DataFrame([row])


def load_realized_volatility_evidence(
    datastore_root: Path,
    *,
    symbol: str,
    as_of: pd.Timestamp,
    lookback: int = 20,
) -> RealizedVolatilityEvidence:
    """Use only canonical Databento daily technical history available by the receipt."""
    path = (
        Path(datastore_root)
        / "stocks"
        / safe_token(symbol.strip().upper())
        / "technicals"
        / "market-regime"
        / "databento"
        / "1d.parquet"
    )
    empty = RealizedVolatilityEvidence(value=None, source_file=str(path))
    if not path.is_file():
        return empty
    try:
        combined = pd.read_parquet(path)
    except Exception:
        return empty
    if not {"timestamp", "close"}.issubset(combined.columns):
        return empty

    combined["timestamp"] = pd.to_datetime(combined["timestamp"], utc=True, errors="coerce")
    if "available_at" in combined.columns:
        available = pd.to_datetime(combined["available_at"], utc=True, errors="coerce")
    elif "bar_end_timestamp" in combined.columns:
        available = (
            pd.to_datetime(combined["bar_end_timestamp"], utc=True, errors="coerce")
            + pd.Timedelta(minutes=5)
        )
    else:
        available = combined["timestamp"] + pd.Timedelta(days=1, minutes=5)
    combined["_source_available_at"] = available
    combined["close"] = pd.to_numeric(combined["close"], errors="coerce")
    cutoff = pd.Timestamp(as_of)
    cutoff = cutoff.tz_localize("UTC") if cutoff.tzinfo is None else cutoff.tz_convert("UTC")
    eligible = (
        combined.loc[available.le(cutoff)]
        .dropna(subset=["timestamp", "close"])
        .sort_values("timestamp")
        .drop_duplicates("timestamp", keep="last")
        .tail(lookback + 1)
    )
    if len(eligible) < max(6, lookback // 2):
        return empty
    closes = eligible["close"]
    returns = (
        np.log(closes / closes.shift(1))
        .replace([np.inf, -np.inf], np.nan)
        .dropna()
    )
    if returns.empty:
        return empty
    value = float(returns.std(ddof=1) * math.sqrt(252.0))
    if not math.isfinite(value):
        return empty
    latest = eligible.iloc[-1]
    return RealizedVolatilityEvidence(
        value=value,
        source_calculation_version=_optional_text(
            latest.get("calculation_version")
        ),
        source_available_at=pd.Timestamp(latest["_source_available_at"]),
        source_observation_count=len(closes),
        source_file=str(path),
        price_adjustment_status=_optional_text(
            latest.get("price_adjustment_status")
        ),
        split_event_count=_optional_int(latest.get("split_event_count")),
    )


def _paired_contracts(frame: pd.DataFrame) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    sort_columns = ["expiration_date", "strike", "call_put"]
    if "contract_symbol" in frame.columns:
        sort_columns.append("contract_symbol")
    ordered = frame.sort_values(
        sort_columns,
        kind="stable",
    )
    for (expiration, strike), group in ordered.groupby(
        ["expiration_date", "strike"],
        dropna=True,
        sort=True,
    ):
        calls = group.loc[group["call_put"].eq("CALL")]
        puts = group.loc[group["call_put"].eq("PUT")]
        if not calls.empty and not puts.empty:
            pairs.append(
                {
                    "expiration": expiration,
                    "strike": float(strike),
                    "call": calls.iloc[0],
                    "put": puts.iloc[0],
                }
            )
    return pairs


def _atm_pair(pairs: list[dict[str, Any]], underlying: float | None) -> dict[str, Any] | None:
    if underlying is None or not pairs:
        return None
    viable = [pair for pair in pairs if (_row_value(pair["call"], "days_to_expiration") or 0) >= 1]
    return min(
        viable or pairs,
        key=lambda pair: (
            _row_value(pair["call"], "days_to_expiration") or 10**9,
            abs(pair["strike"] - underlying),
            pair["strike"],
            str(pair["expiration"]),
        ),
    )


def _atm_iv_by_expiration(frame: pd.DataFrame, underlying: float | None) -> list[tuple[float, float]]:
    if underlying is None:
        return []
    values: list[tuple[float, float]] = []
    for _expiration, group in frame.groupby("expiration_date", dropna=True):
        group = group.loc[group["implied_volatility"].notna()].copy()
        if group.empty:
            continue
        group["distance"] = (group["strike"] - underlying).abs()
        dte = _series_first(group["days_to_expiration"])
        iv = _median(group.loc[group["distance"].eq(group["distance"].min()), "implied_volatility"])
        if dte is not None and iv is not None:
            values.append((dte, iv))
    return sorted(values, key=lambda item: item[0])


def _delta_wing_features(frame: pd.DataFrame) -> dict[str, float | None]:
    expirations = frame.loc[frame["days_to_expiration"].ge(1), "days_to_expiration"].dropna()
    if expirations.empty:
        return {"put_iv": None, "call_iv": None, "atm_iv": None}
    dte = min(
        expirations.unique(),
        key=lambda value: (abs(float(value) - 30.0), float(value)),
    )
    selected = frame.loc[frame["days_to_expiration"].eq(dte)].copy()
    call = _closest_delta(selected.loc[selected["call_put"].eq("CALL")], 0.25)
    put = _closest_delta(selected.loc[selected["call_put"].eq("PUT")], -0.25)
    underlying = _series_first(selected["underlying_price"])
    atm = selected.loc[selected["implied_volatility"].notna()].copy()
    if underlying is not None and not atm.empty:
        atm["distance"] = (atm["strike"] - underlying).abs()
        atm_iv = _median(atm.loc[atm["distance"].eq(atm["distance"].min()), "implied_volatility"])
    else:
        atm_iv = None
    return {
        "put_iv": _row_value(put, "implied_volatility"),
        "call_iv": _row_value(call, "implied_volatility"),
        "atm_iv": atm_iv,
    }


def _closest_delta(frame: pd.DataFrame, target: float) -> pd.Series | None:
    usable = frame.loc[frame["delta"].notna() & frame["implied_volatility"].notna()].copy()
    return None if usable.empty else usable.loc[(usable["delta"] - target).abs().idxmin()]


def _put_call_parity_features(
    pairs: list[dict[str, Any]],
    underlying: float | None,
) -> dict[str, float | None]:
    if underlying is None:
        return {"median_residual": None, "atm_residual": None}
    residuals: list[tuple[float, float]] = []
    for pair in pairs:
        call_mark = _row_value(pair["call"], "mark")
        put_mark = _row_value(pair["put"], "mark")
        dte = _row_value(pair["call"], "days_to_expiration")
        if call_mark is None or put_mark is None or dte is None:
            continue
        rate = _row_value(pair["call"], "interest_rate") or 0.0
        dividend = _row_value(pair["call"], "dividend_yield") or 0.0
        years = max(dte, 0.0) / 365.0
        theoretical = underlying * math.exp(-dividend * years) - pair["strike"] * math.exp(-rate * years)
        residuals.append((abs(pair["strike"] - underlying), (call_mark - put_mark - theoretical) / underlying))
    if not residuals:
        return {"median_residual": None, "atm_residual": None}
    return {
        "median_residual": float(np.median([value for _distance, value in residuals])),
        "atm_residual": min(residuals, key=lambda item: item[0])[1],
    }


def _total(frame: pd.DataFrame, column: str) -> float:
    return float(frame[column].fillna(0.0).clip(lower=0.0).sum())


def _single_timestamp(
    frame: pd.DataFrame,
    column: str,
    *,
    fallback: str | None = None,
) -> pd.Timestamp:
    selected = frame[column] if column in frame.columns else pd.Series(dtype="object")
    parsed = pd.to_datetime(selected, utc=True, errors="coerce").dropna()
    if parsed.empty and fallback is not None and fallback in frame.columns:
        parsed = pd.to_datetime(
            frame[fallback],
            utc=True,
            errors="coerce",
        ).dropna()
    values = pd.Index(parsed.unique())
    if len(values) != 1:
        raise ValueError(
            f"Option snapshot requires exactly one valid {column}; found {len(values)}"
        )
    return pd.Timestamp(values[0]).tz_convert("UTC")


def _single_optional_timestamp(
    frame: pd.DataFrame,
    column: str,
) -> pd.Timestamp | None:
    if column not in frame.columns:
        return None
    parsed = pd.to_datetime(frame[column], utc=True, errors="coerce").dropna()
    values = pd.Index(parsed.unique())
    if not len(values):
        return None
    if len(values) != 1:
        raise ValueError(
            f"Option snapshot requires at most one valid {column}; found {len(values)}"
        )
    return pd.Timestamp(values[0]).tz_convert("UTC")


def _optional_int(value: object) -> int | None:
    try:
        if pd.isna(value):
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return int(number)


def _optional_text(value: object) -> str:
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value or "").strip()


def _series_first(series: pd.Series) -> float | None:
    usable = pd.to_numeric(series, errors="coerce").dropna()
    return float(usable.iloc[-1]) if not usable.empty else None


def _row_value(row: pd.Series | None, column: str) -> float | None:
    if row is None:
        return None
    try:
        value = float(row.get(column))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _mean_finite(values: list[float | None]) -> float | None:
    usable = [value for value in values if value is not None and math.isfinite(value)]
    return float(np.mean(usable)) if usable else None


def _median(series: pd.Series) -> float | None:
    usable = pd.to_numeric(series, errors="coerce").dropna()
    return float(usable.median()) if not usable.empty else None


def _max_finite(series: pd.Series) -> float | None:
    usable = pd.to_numeric(series, errors="coerce").dropna()
    return float(usable.max()) if not usable.empty else None


def _sum_values(left: float | None, right: float | None) -> float | None:
    return left + right if left is not None and right is not None else None


def _safe_divide(numerator: float | None, denominator: float | None) -> float | None:
    return None if numerator is None or denominator in {None, 0} else numerator / denominator


def _difference(left: float | None, right: float | None) -> float | None:
    return left - right if left is not None and right is not None else None


def _coalesce(left: float | None, right: float | None) -> float | None:
    return left if left is not None else right


def _expected_absolute_move(
    annualized_volatility: float | None,
    days_to_expiration: float | None,
) -> float | None:
    """Approximate E[|return|] over the option horizon from recent realized volatility."""
    if (
        annualized_volatility is None
        or days_to_expiration is None
        or not math.isfinite(annualized_volatility)
        or not math.isfinite(days_to_expiration)
        or annualized_volatility < 0
        or days_to_expiration <= 0
    ):
        return None
    years = days_to_expiration / 365.0
    return annualized_volatility * math.sqrt(2.0 / math.pi) * math.sqrt(years)
