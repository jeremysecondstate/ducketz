from __future__ import annotations

import numpy as np
import pandas as pd

from ml.option_pricing.policies import OPTION_PRICING_WEIGHTING_POLICY_VERSION


LIQUIDITY_COMPONENT_COLUMNS = (
    "relative_bid_ask_spread",
    "quality_spread_score",
    "quality_staleness_score",
    "quality_volume_score",
    "quality_open_interest_score",
    "quality_quote_status_score",
    "raw_observation_weight",
    "surface_normalization_factor",
    "final_row_weight",
    "quality_missingness_count",
    "weighting_policy_version",
)


def attach_liquidity_weights(frame: pd.DataFrame) -> pd.DataFrame:
    """Distribute one unit of weight within every natural target surface."""

    if frame.empty:
        output = frame.copy()
        for column in LIQUIDITY_COMPONENT_COLUMNS:
            if column not in output:
                output[column] = pd.Series(dtype="string" if column.endswith("version") else "float64")
        return output
    required = {"symbol", "target_snapshot_for", "call_put"}
    if missing := sorted(required.difference(frame.columns)):
        raise ValueError("Liquidity weights require: " + ", ".join(missing))
    output = frame.copy()
    bid = _numeric(output, "observed_bid", "bid", "source_bid")
    ask = _numeric(output, "observed_ask", "ask", "source_ask")
    midpoint = _numeric(output, "observed_mid", "midpoint", "source_mid")
    midpoint = midpoint.where(midpoint.gt(0.0), (bid + ask) / 2.0)
    relative_spread = (ask - bid).clip(lower=0.0) / midpoint
    relative_spread = relative_spread.where(np.isfinite(relative_spread))
    staleness = _numeric(
        output,
        "observed_quote_staleness_seconds",
        "source_quote_staleness_seconds",
        "quote_staleness_seconds",
    ).clip(lower=0.0)
    volume = _numeric(output, "daily_volume", "volume", "trade_volume")
    open_interest = _numeric(output, "open_interest", "openInterest")
    status = _text(output, "quote_quality_status", "quality_status")

    spread_score = (1.0 / (1.0 + 10.0 * relative_spread.clip(upper=2.0))).fillna(0.5)
    staleness_score = (1.0 / (1.0 + staleness.clip(upper=3600.0) / 60.0)).fillna(0.5)
    volume_score = (np.log1p(volume.clip(lower=0.0)) / np.log(101.0)).clip(0.1, 1.0).fillna(0.5)
    open_interest_score = (
        np.log1p(open_interest.clip(lower=0.0)) / np.log(1001.0)
    ).clip(0.1, 1.0).fillna(0.5)
    normalized_status = status.str.strip().str.upper()
    status_score = pd.Series(0.5, index=output.index, dtype=float)
    status_score.loc[normalized_status.isin({"VALID", "GOOD", "PASS", "NORMAL"})] = 1.0
    status_score.loc[normalized_status.isin({"STALE", "DEGRADED", "HALTED", "INVALID"})] = 0.1
    missingness = pd.DataFrame(
        {
            "spread": relative_spread.isna(),
            "staleness": staleness.isna(),
            "volume": volume.isna(),
            "open_interest": open_interest.isna(),
            "quality_status": normalized_status.isna() | normalized_status.eq(""),
        }
    ).sum(axis=1)
    raw = (
        spread_score
        * staleness_score
        * volume_score
        * open_interest_score
        * status_score
    ).clip(lower=1e-6, upper=1.0)
    keys = pd.DataFrame(
        {
            "symbol": output["symbol"].astype("string").str.strip().str.upper(),
            "target_snapshot_for": pd.to_datetime(
                output["target_snapshot_for"], utc=True, errors="coerce"
            ),
            "call_put": output["call_put"].astype("string").str.strip().str.upper(),
        },
        index=output.index,
    )
    if keys.isna().any(axis=None) or keys[["symbol", "call_put"]].eq("").any(axis=None):
        raise ValueError("Liquidity weights received incomplete surface keys")
    totals = raw.groupby(
        [keys["symbol"], keys["target_snapshot_for"], keys["call_put"]],
        sort=False,
        dropna=False,
    ).transform("sum")
    normalization = 1.0 / totals
    final = raw * normalization
    if not np.isfinite(final).all() or final.le(0.0).any():
        raise ValueError("Liquidity weights are non-finite or nonpositive")
    output["relative_bid_ask_spread"] = relative_spread
    output["quality_spread_score"] = spread_score
    output["quality_staleness_score"] = staleness_score
    output["quality_volume_score"] = volume_score
    output["quality_open_interest_score"] = open_interest_score
    output["quality_quote_status_score"] = status_score
    output["raw_observation_weight"] = raw
    output["surface_normalization_factor"] = normalization
    output["final_row_weight"] = final
    output["quality_missingness_count"] = missingness.astype(float)
    output["weighting_policy_version"] = OPTION_PRICING_WEIGHTING_POLICY_VERSION
    return output


def liquidity_weights(frame: pd.DataFrame) -> np.ndarray:
    return attach_liquidity_weights(frame)["final_row_weight"].to_numpy(dtype=float)


def _numeric(frame: pd.DataFrame, *columns: str) -> pd.Series:
    for column in columns:
        if column in frame:
            return pd.to_numeric(frame[column], errors="coerce")
    return pd.Series(np.nan, index=frame.index, dtype=float)


def _text(frame: pd.DataFrame, *columns: str) -> pd.Series:
    for column in columns:
        if column in frame:
            return frame[column].astype("string")
    return pd.Series(pd.NA, index=frame.index, dtype="string")


__all__ = [
    "LIQUIDITY_COMPONENT_COLUMNS",
    "attach_liquidity_weights",
    "liquidity_weights",
]
