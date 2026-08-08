from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pandas as pd

from ml.artifacts import utc_timestamp


def load_point_in_time_rate_observations(
    datastore_root: Path,
) -> tuple[pd.DataFrame | None, tuple[Path, ...]]:
    """Load causal FRED rate releases without inventing a fallback value."""

    root = Path(datastore_root).resolve()
    paths = tuple(
        sorted(
            (
                root
                / "pools"
                / "macro"
                / "features"
                / "release-context"
                / "fred"
            ).glob("*.parquet")
        )
    )
    if not paths:
        return None, ()
    frames = [pd.read_parquet(path) for path in paths]
    combined = pd.concat(frames, ignore_index=True, sort=False)
    required = {"fed_funds_available_at", "macro__fed_funds_level"}
    if not required.issubset(combined.columns):
        return None, paths
    output = pd.DataFrame(
        {
            "available_at": pd.to_datetime(
                combined["fed_funds_available_at"], utc=True, errors="coerce"
            ),
            # FRED FEDFUNDS is quoted in percentage points.
            "risk_free_rate": pd.to_numeric(
                combined["macro__fed_funds_level"], errors="coerce"
            )
            / 100.0,
        }
    ).dropna()
    output = output.loc[output["risk_free_rate"].between(-0.20, 1.0)]
    output = output.sort_values("available_at").drop_duplicates(
        "available_at", keep="last"
    )
    return (output.reset_index(drop=True) if not output.empty else None), paths


def rate_coverage_report(
    observations: pd.DataFrame | None,
    *,
    target_snapshot_fors: Sequence[object],
    source_backward_minutes: int = 5,
) -> dict[str, object]:
    """Prove a strictly prior rate exists at every planned source boundary."""

    targets = sorted({utc_timestamp(value) for value in target_snapshot_fors})
    if observations is None or observations.empty:
        covered: list[pd.Timestamp] = []
        available = pd.Series(dtype="datetime64[ns, UTC]")
    else:
        available = pd.to_datetime(
            observations.get("available_at"), utc=True, errors="coerce"
        ).dropna().sort_values()
        covered = [
            target
            for target in targets
            if available.lt(
                target - pd.Timedelta(minutes=source_backward_minutes)
            ).any()
        ]
    missing = sorted(set(targets).difference(covered))
    return {
        "status": "PASS" if targets and not missing else "NOT_PROVEN",
        "target_count": len(targets),
        "covered_target_count": len(covered),
        "rate_observation_count": len(available),
        "source_backward_minutes": source_backward_minutes,
        "first_rate_available_at": (
            pd.Timestamp(available.iloc[0]).isoformat() if len(available) else None
        ),
        "last_rate_available_at": (
            pd.Timestamp(available.iloc[-1]).isoformat() if len(available) else None
        ),
        "missing_targets": [value.isoformat() for value in missing],
    }


__all__ = ["load_point_in_time_rate_observations", "rate_coverage_report"]
