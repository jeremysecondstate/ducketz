from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from fundamentals.parquet_io import load_fundamental_parquet

MAX_FUNDAMENTAL_WEIGHT = 0.30
FRESHNESS_HALF_LIFE_DAYS = 180.0


def attach_fundamental_context(
    technical_frame: pd.DataFrame,
    *,
    fundamentals_root: Path,
    period_type: str = "quarterly",
    source: str = "fmp",
) -> pd.DataFrame:
    """As-of join filing-time fundamentals onto technical rows without look-ahead."""
    output = technical_frame.copy()
    if "timestamp" not in output.columns or "technical_score" not in output.columns:
        return output

    fundamentals = load_fundamental_parquet(
        fundamentals_root,
        period_type=period_type,
        source=source,
    )
    if fundamentals.empty:
        return _empty_context(output)

    required = {"effective_from", "fundamental_score", "fundamental_confidence"}
    if not required.issubset(fundamentals.columns):
        return _empty_context(output)

    left = output.reset_index().rename(columns={"index": "__original_order"})
    left["timestamp"] = pd.to_datetime(
        left["timestamp"], utc=True, errors="coerce"
    ).astype("datetime64[ns, UTC]")
    right = fundamentals.copy()
    right["effective_from"] = pd.to_datetime(
        right["effective_from"], utc=True, errors="coerce"
    ).astype("datetime64[ns, UTC]")
    right = right.dropna(subset=["effective_from"]).sort_values("effective_from")
    right = right.drop_duplicates("effective_from", keep="last")
    if right.empty:
        return _empty_context(output)

    # Namespace the fundamental source columns before merging so pandas never rewrites
    # the technical calculation version to calculation_version_x.
    right = right.rename(
        columns={
            "period_end_date": "fundamental_period_end_date",
            "calculation_version": "fundamental_calculation_version",
            "fiscal_period": "fundamental_fiscal_period",
            "period_type": "fundamental_period_type",
        }
    )
    selected = [
        "effective_from",
        "fundamental_period_end_date",
        "fundamental_score",
        "fundamental_confidence",
        "fundamental_label",
        "fundamental_calculation_version",
        "fundamental_fiscal_period",
        "fundamental_period_type",
    ]
    selected = [column for column in selected if column in right.columns]
    joined = pd.merge_asof(
        left.sort_values("timestamp"),
        right[selected].sort_values("effective_from"),
        left_on="timestamp",
        right_on="effective_from",
        direction="backward",
        allow_exact_matches=True,
    )

    age_days = (
        joined["timestamp"] - joined["effective_from"]
    ).dt.total_seconds() / 86_400.0
    age_days = age_days.clip(lower=0.0)
    freshness = np.power(0.5, age_days / FRESHNESS_HALF_LIFE_DAYS)
    confidence = (
        pd.to_numeric(joined["fundamental_confidence"], errors="coerce")
        .clip(0.0, 100.0)
        / 100.0
    )
    effective_weight = (MAX_FUNDAMENTAL_WEIGHT * confidence * freshness).fillna(0.0)
    technical_score = pd.to_numeric(joined["technical_score"], errors="coerce")
    fundamental_score = pd.to_numeric(joined["fundamental_score"], errors="coerce")
    combined = technical_score * (1.0 - effective_weight) + fundamental_score.fillna(
        technical_score
    ) * effective_weight

    joined["fundamental_age_days"] = age_days
    joined["fundamental_freshness_factor"] = freshness
    joined["effective_fundamental_weight"] = effective_weight
    joined["combined_conviction_score"] = combined.clip(0.0, 100.0)
    joined = joined.rename(columns={"effective_from": "fundamental_effective_from"})
    return (
        joined.sort_values("__original_order")
        .drop(columns="__original_order")
        .reset_index(drop=True)
    )


def _empty_context(frame: pd.DataFrame) -> pd.DataFrame:
    output = frame.copy()
    output["fundamental_score"] = np.nan
    output["fundamental_confidence"] = np.nan
    output["fundamental_label"] = None
    output["fundamental_effective_from"] = pd.NaT
    output["fundamental_period_end_date"] = pd.NaT
    output["fundamental_fiscal_period"] = None
    output["fundamental_period_type"] = None
    output["fundamental_calculation_version"] = None
    output["fundamental_age_days"] = np.nan
    output["fundamental_freshness_factor"] = 0.0
    output["effective_fundamental_weight"] = 0.0
    output["combined_conviction_score"] = pd.to_numeric(
        output["technical_score"], errors="coerce"
    )
    return output
