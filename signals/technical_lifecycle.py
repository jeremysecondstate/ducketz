from __future__ import annotations

from pathlib import Path
from typing import Mapping

import numpy as np
import pandas as pd

from datafetching.calculated_features import write_immutable_feature_partition

CALCULATION_NAME = "technical-lifecycle"
CALCULATION_VERSION = "1.0.0"
SCHEMA_VERSION = "technical-lifecycle-v1"
PROVIDER_POLICY_VERSION = "databento-canonical-only-v1"

TECHNICAL_LIFECYCLE_COLUMNS = (
    "symbol",
    "timestamp",
    "constituent_available_at",
    "calculated_at",
    "available_at",
    "calculation",
    "calculation_version",
    "schema_version",
    "provider_policy_version",
    "technical_consensus_score",
    "technical_consensus_change_5d",
    "long_term_technical_score",
    "technical_term_spread",
    "timing_score",
    "constituent_complete",
)
TECHNICAL_LIFECYCLE_NATURAL_KEY = (
    "symbol",
    "timestamp",
    "available_at",
    "calculation_version",
    "provider_policy_version",
)


def calculate_technical_lifecycle_snapshot(
    technical_frames: Mapping[tuple[str, str], pd.DataFrame],
    *,
    symbol: str,
    calculated_at: object | None = None,
) -> pd.DataFrame:
    """Create one forward-going canonical-provider technical snapshot."""

    completed = _utc_timestamp(
        calculated_at if calculated_at is not None else pd.Timestamp.now(tz="UTC")
    )
    try:
        source = technical_frames[("databento", "1d")].copy()
    except KeyError as exc:
        raise ValueError(
            "Technical lifecycle requires canonical Databento daily market regime"
        ) from exc
    required = {"timestamp", "technical_score", "bar_end_timestamp", "bar_complete"}
    missing = sorted(required.difference(source.columns))
    if missing:
        raise ValueError(
            "Technical lifecycle source is missing columns: " + ", ".join(missing)
        )
    source["timestamp"] = pd.to_datetime(
        source["timestamp"], utc=True, errors="coerce"
    )
    source["bar_end_timestamp"] = pd.to_datetime(
        source["bar_end_timestamp"], utc=True, errors="coerce"
    )
    source["technical_score"] = pd.to_numeric(
        source["technical_score"], errors="coerce"
    )
    if "available_at" in source:
        source["_source_available_at"] = pd.to_datetime(
            source["available_at"], utc=True, errors="coerce"
        )
    else:
        source["_source_available_at"] = (
            source["bar_end_timestamp"] + pd.Timedelta(minutes=5)
        )
    if source["_source_available_at"].lt(source["bar_end_timestamp"]).any():
        raise ValueError(
            "Technical lifecycle source availability precedes a completed bar"
        )
    source = (
        source.loc[
            source["bar_complete"].fillna(False).astype(bool)
            & source["_source_available_at"].le(completed)
        ]
        .dropna(
            subset=[
                "timestamp",
                "bar_end_timestamp",
                "technical_score",
                "_source_available_at",
            ]
        )
        .sort_values("timestamp")
        .drop_duplicates("timestamp", keep="last")
        .reset_index(drop=True)
    )
    if len(source) < 20:
        raise ValueError("Technical lifecycle requires at least 20 daily values")

    score = source["technical_score"]
    long_term = score.rolling(20, min_periods=20).mean()
    change = score.diff(5)
    spread = score - long_term
    timing = (
        50.0
        + change.fillna(0.0).clip(-20.0, 20.0) * 1.5
        + spread.fillna(0.0).clip(-20.0, 20.0) * 1.0
    ).clip(0.0, 100.0)
    index = source.index[-1]
    constituent_available = source.tail(20)["_source_available_at"].max()
    return pd.DataFrame(
        [
            {
                "symbol": str(symbol).strip().upper(),
                "timestamp": source.loc[index, "timestamp"],
                "constituent_available_at": constituent_available,
                "calculated_at": completed,
                "available_at": max(constituent_available, completed),
                "calculation": CALCULATION_NAME,
                "calculation_version": CALCULATION_VERSION,
                "schema_version": SCHEMA_VERSION,
                "provider_policy_version": PROVIDER_POLICY_VERSION,
                "technical_consensus_score": float(score.loc[index]),
                "technical_consensus_change_5d": _finite_or_none(
                    change.loc[index]
                ),
                "long_term_technical_score": _finite_or_none(
                    long_term.loc[index]
                ),
                "technical_term_spread": _finite_or_none(spread.loc[index]),
                "timing_score": _finite_or_none(timing.loc[index]),
                "constituent_complete": True,
            }
        ],
        columns=TECHNICAL_LIFECYCLE_COLUMNS,
    )


def persist_technical_lifecycle(
    output_root: Path,
    frame: pd.DataFrame,
) -> Path:
    path = (
        Path(output_root)
        / CALCULATION_NAME
        / "consensus"
        / "daily.parquet"
    )
    if path.is_file() and not frame.empty:
        existing = pd.read_parquet(path).drop(columns=["id"], errors="ignore")
        stable_key = [
            "symbol",
            "timestamp",
            "calculation_version",
            "schema_version",
            "provider_policy_version",
        ]
        compare_columns = [
            "constituent_available_at",
            "technical_consensus_score",
            "technical_consensus_change_5d",
            "long_term_technical_score",
            "technical_term_spread",
            "timing_score",
            "constituent_complete",
        ]
        keep: list[bool] = []
        for _, incoming in frame.iterrows():
            matches = existing.copy()
            for column in stable_key:
                matches = matches.loc[
                    matches[column].astype(str).eq(str(incoming[column]))
                ]
            if matches.empty:
                keep.append(True)
                continue
            latest = matches.sort_values("available_at").iloc[-1]
            unchanged = all(
                _equal_value(latest.get(column), incoming.get(column))
                for column in compare_columns
            )
            keep.append(not unchanged)
        frame = frame.loc[keep].reset_index(drop=True)
        if frame.empty:
            return path
    return write_immutable_feature_partition(
        path,
        frame,
        columns=TECHNICAL_LIFECYCLE_COLUMNS,
        natural_key=TECHNICAL_LIFECYCLE_NATURAL_KEY,
    )


def _utc_timestamp(value: object) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise ValueError("Expected a valid calculation timestamp")
    return pd.Timestamp(timestamp)


def _finite_or_none(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _equal_value(left: object, right: object) -> bool:
    if pd.isna(left) and pd.isna(right):
        return True
    if isinstance(left, pd.Timestamp) or isinstance(right, pd.Timestamp):
        return _utc_timestamp(left) == _utc_timestamp(right)
    try:
        return bool(np.isclose(float(left), float(right), equal_nan=True))
    except (TypeError, ValueError):
        return left == right
