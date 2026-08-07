from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from ml.artifacts import utc_timestamp
from ml.option_pricing.policies import OPTION_PRICING_POLICY_VERSION
from ml.option_pricing.publication import authoritative_option_pricing_runs
from ml.option_pricing.reporting import SURFACE_VERSION


OPX_VALUE_COLUMNS = (
    "causal_coverage",
    "median_normalized_residual",
    "median_predictive_standard_deviation",
    "median_model_edge_in_half_spreads",
    "positive_edge_fraction",
    "negative_edge_fraction",
    "raw_arbitrage_violation_rate",
    "constrained_arbitrage_violation_rate",
    "interval_80_coverage",
    "interval_95_coverage",
    "median_relative_bid_ask_spread",
)


def read_verified_compact_pricing_features(
    datastore_root: Path,
    *,
    available_not_after: object,
) -> tuple[pd.DataFrame, tuple[Path, ...]]:
    """Return one causal opx row per symbol/publication time.

    Publication verification happens before reading Parquet. Missing, tampered,
    late, or incompatible Pricing evidence raises instead of substituting a
    current value into the explicit shadow route.
    """

    cutoff = utc_timestamp(available_not_after)
    reachable = authoritative_option_pricing_runs(datastore_root)
    eligible = [
        (run, published)
        for run, published in reachable.items()
        if published <= cutoff
    ]
    if not eligible:
        raise FileNotFoundError(
            "No verified reachable Pricing publication was available by the causal cutoff"
        )
    run, published = max(eligible, key=lambda item: item[1])
    path = run / "pricing-surfaces.parquet"
    source = pd.read_parquet(path)
    if source.empty:
        raise FileNotFoundError("Verified Pricing publication has no compact surface rows")
    available = pd.to_datetime(source["available_at"], utc=True, errors="coerce")
    if available.isna().any() or available.gt(published).any():
        raise ValueError("Pricing surface availability disagrees with its receipt")
    source = source.loc[available.le(cutoff)].copy()
    if source.empty:
        raise FileNotFoundError("No verified Pricing surface was causal by the cutoff")
    required = {
        "symbol",
        "target_snapshot_for",
        "available_at",
        "contract_count",
        "surface_quality_pass",
        "automated_action_allowed",
        *OPX_VALUE_COLUMNS,
    }
    if missing := sorted(required.difference(source.columns)):
        raise ValueError("Compact Pricing surface is missing: " + ", ".join(missing))
    if source["automated_action_allowed"].fillna(True).astype(bool).any():
        raise ValueError("Shadow Pricing surface unexpectedly authorizes automation")
    if (
        not source["schema_version"].eq(SURFACE_VERSION).all()
        or not source["pricing_policy_version"].eq(
            OPTION_PRICING_POLICY_VERSION
        ).all()
    ):
        raise ValueError("Compact Pricing surface policy/schema is incompatible")
    source["contract_count"] = pd.to_numeric(source["contract_count"], errors="coerce")
    source["target_snapshot_for"] = pd.to_datetime(
        source["target_snapshot_for"], utc=True, errors="coerce"
    )
    source["available_at"] = pd.to_datetime(source["available_at"], utc=True)
    rows: list[dict[str, object]] = []
    for key, group in source.groupby(
        ["symbol", "target_snapshot_for", "available_at"], sort=True
    ):
        weights = pd.to_numeric(group["contract_count"], errors="coerce").fillna(0.0)
        if float(weights.sum()) <= 0:
            continue
        row: dict[str, object] = {
            "symbol": str(key[0]).strip().upper(),
            "target_snapshot_for": key[1],
            "available_at": key[2],
        }
        for column in OPX_VALUE_COLUMNS:
            values = pd.to_numeric(group[column], errors="coerce")
            valid = values.notna() & weights.gt(0)
            row[column] = (
                float(np.average(values.loc[valid], weights=weights.loc[valid]))
                if valid.any()
                else np.nan
            )
        rows.append(row)
    result = pd.DataFrame(rows)
    if result.empty:
        raise FileNotFoundError("Verified Pricing surfaces had no weighted feature rows")
    return result, (
        path,
        run / "manifest.json",
        run / "publication.json",
    )


__all__ = [
    "OPX_VALUE_COLUMNS",
    "read_verified_compact_pricing_features",
]
