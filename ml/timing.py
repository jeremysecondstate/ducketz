from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final, Sequence

import pandas as pd

ACTIONABLE: Final = "ACTIONABLE"
TARGET_WINDOW_STARTED: Final = "TARGET_WINDOW_STARTED"
NO_ELIGIBLE_SOURCE_DATA: Final = "NO_ELIGIBLE_SOURCE_DATA"
NO_ACTIONABLE_CANDIDATE: Final = "NO_ACTIONABLE_CANDIDATE"
TARGET_TIMESTAMP_INVALID: Final = "TARGET_TIMESTAMP_INVALID"
MODEL_UNAVAILABLE: Final = "MODEL_UNAVAILABLE"

@dataclass(frozen=True)
class Actionability:
    status: str
    reason: str | None
    information_available_at: pd.Timestamp | None
    target_window_start: pd.Timestamp | None
    forecast_created_at: pd.Timestamp

    @property
    def actionable(self) -> bool:
        return self.status == ACTIONABLE


def classify_actionability(
    *,
    information_available_at: object,
    forecast_created_at: object | None,
    target_window_start: object,
) -> Actionability:
    """Apply the one shared runtime timing rule.

    A row is actionable exactly when::

        information_available_at <= forecast_created_at < target_window_start

    Equality at the target-window start is intentionally ineligible.
    """

    created = utc_timestamp(forecast_created_at)
    available = optional_utc_timestamp(information_available_at)
    target_start = optional_utc_timestamp(target_window_start)
    if available is None:
        return Actionability(
            status=NO_ACTIONABLE_CANDIDATE,
            reason="INFORMATION_TIMESTAMP_INVALID",
            information_available_at=None,
            target_window_start=target_start,
            forecast_created_at=created,
        )
    if target_start is None:
        return Actionability(
            status=TARGET_TIMESTAMP_INVALID,
            reason="TARGET_WINDOW_START_INVALID",
            information_available_at=available,
            target_window_start=None,
            forecast_created_at=created,
        )
    if target_start <= available:
        return Actionability(
            status=TARGET_TIMESTAMP_INVALID,
            reason="TARGET_WINDOW_NOT_AFTER_INFORMATION_AVAILABILITY",
            information_available_at=available,
            target_window_start=target_start,
            forecast_created_at=created,
        )
    if created < available:
        return Actionability(
            status=NO_ACTIONABLE_CANDIDATE,
            reason="INFORMATION_NOT_YET_AVAILABLE",
            information_available_at=available,
            target_window_start=target_start,
            forecast_created_at=created,
        )
    if created >= target_start:
        return Actionability(
            status=TARGET_WINDOW_STARTED,
            reason="TARGET_WINDOW_HAS_STARTED",
            information_available_at=available,
            target_window_start=target_start,
            forecast_created_at=created,
        )
    return Actionability(
        status=ACTIONABLE,
        reason=None,
        information_available_at=available,
        target_window_start=target_start,
        forecast_created_at=created,
    )


def evaluate_actionability_rows(
    frame: pd.DataFrame,
    *,
    forecast_created_at: object | None,
    information_column: str = "information_available_at",
    target_start_column: str = "target_window_start",
    group_columns: Sequence[str] = ("symbol", "horizon"),
    latest_per_group: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    required = {information_column, target_start_column, *group_columns}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(
            "Actionability input is missing columns: " + ", ".join(missing)
        )
    created = utc_timestamp(forecast_created_at)
    working = frame.copy()
    evaluations = [
        classify_actionability(
            information_available_at=available,
            forecast_created_at=created,
            target_window_start=target,
        )
        for available, target in zip(
            working[information_column],
            working[target_start_column],
            strict=True,
        )
    ]
    working["actionability_status"] = [item.status for item in evaluations]
    working["actionability_reason"] = [item.reason for item in evaluations]
    working["forecast_created_at"] = created
    working[information_column] = [
        item.information_available_at for item in evaluations
    ]
    working[target_start_column] = [
        item.target_window_start for item in evaluations
    ]

    eligible = working.loc[working["actionability_status"].eq(ACTIONABLE)].copy()
    if latest_per_group and not eligible.empty:
        order = [
            *group_columns,
            information_column,
            target_start_column,
        ]
        eligible = eligible.sort_values(order, kind="mergesort")
        selected_indices = set(
            eligible.groupby(list(group_columns), sort=False).tail(1).index
        )
        selected = eligible.loc[eligible.index.isin(selected_indices)].copy()
        superseded = (
            working["actionability_status"].eq(ACTIONABLE)
            & ~working.index.isin(selected_indices)
        )
        working.loc[superseded, "actionability_status"] = NO_ACTIONABLE_CANDIDATE
        working.loc[
            superseded, "actionability_reason"
        ] = "SUPERSEDED_BY_LATER_ACTIONABLE_DECISION"
    else:
        selected = eligible

    selected = selected.sort_values(
        [*group_columns, target_start_column],
        kind="mergesort",
    ).reset_index(drop=True)
    diagnostics = working.sort_values(
        [*group_columns, information_column, target_start_column],
        kind="mergesort",
        na_position="first",
    ).reset_index(drop=True)
    return selected, diagnostics


def optional_utc_timestamp(value: object) -> pd.Timestamp | None:
    if value is None:
        return None
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(timestamp):
        return None
    return (
        timestamp.tz_localize("UTC")
        if timestamp.tzinfo is None
        else timestamp.tz_convert("UTC")
    )


def utc_timestamp(value: object | None) -> pd.Timestamp:
    timestamp = pd.Timestamp(
        value if value is not None else datetime.now(timezone.utc)
    )
    if pd.isna(timestamp):
        raise ValueError("UTC timestamp cannot be missing")
    return (
        timestamp.tz_localize("UTC")
        if timestamp.tzinfo is None
        else timestamp.tz_convert("UTC")
    )
