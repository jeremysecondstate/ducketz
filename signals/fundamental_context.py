from __future__ import annotations

import numpy as np
import pandas as pd


def attach_lifecycle_fundamentals(
    technical_daily: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """As-of join quarterly fundamentals and derive event-to-event changes."""
    output = technical_daily.copy()
    empty_columns = {
        "fundamental_effective_from": pd.NaT,
        "fundamental_period_end_date": pd.NaT,
        "fundamental_score": np.nan,
        "fundamental_confidence": np.nan,
        "fundamental_label": None,
        "fundamental_change_1q": np.nan,
        "fundamental_acceleration": np.nan,
        "fundamental_fiscal_period": None,
    }
    if fundamentals.empty or not {
        "effective_from",
        "fundamental_score",
        "fundamental_confidence",
    }.issubset(fundamentals.columns):
        return _empty(output, empty_columns)

    right = fundamentals.copy()
    right["effective_from"] = pd.to_datetime(
        right["effective_from"], utc=True, errors="coerce"
    ).astype("datetime64[ns, UTC]")
    right["fundamental_score"] = pd.to_numeric(
        right["fundamental_score"], errors="coerce"
    )
    right["fundamental_confidence"] = pd.to_numeric(
        right["fundamental_confidence"], errors="coerce"
    )
    right = (
        right.dropna(subset=["effective_from", "fundamental_score"])
        .sort_values("effective_from")
        .drop_duplicates("effective_from", keep="last")
        .reset_index(drop=True)
    )
    if right.empty:
        return _empty(output, empty_columns)

    right["fundamental_change_1q"] = right["fundamental_score"].diff()
    right["fundamental_acceleration"] = right["fundamental_change_1q"].diff()
    selected = [
        "effective_from",
        "period_end_date",
        "fundamental_score",
        "fundamental_confidence",
        "fundamental_label",
        "fundamental_change_1q",
        "fundamental_acceleration",
        "fiscal_period",
    ]
    selected = [column for column in selected if column in right.columns]
    output["timestamp"] = pd.to_datetime(
        output["timestamp"], utc=True, errors="coerce"
    ).astype("datetime64[ns, UTC]")
    joined = pd.merge_asof(
        output.sort_values("timestamp"),
        right[selected].sort_values("effective_from"),
        left_on="timestamp",
        right_on="effective_from",
        direction="backward",
        allow_exact_matches=True,
    )
    return joined.rename(
        columns={
            "effective_from": "fundamental_effective_from",
            "period_end_date": "fundamental_period_end_date",
            "fiscal_period": "fundamental_fiscal_period",
        }
    ).reset_index(drop=True)


def _empty(frame: pd.DataFrame, columns: dict[str, object]) -> pd.DataFrame:
    output = frame.copy()
    for column, default in columns.items():
        output[column] = default
    return output
