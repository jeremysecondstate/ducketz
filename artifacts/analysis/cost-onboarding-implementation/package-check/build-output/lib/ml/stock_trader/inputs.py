from __future__ import annotations

from pathlib import Path
from typing import Mapping

import pandas as pd

from ml.artifacts import file_checksum
from ml.current_publication import read_current_publication
from ml.parquet_contracts import PREDICTION_SCHEMA
from ml.stock_trader.contracts import (
    PredictionSignal,
    STOCK_TRADER_SYMBOLS,
    canonical_sha256,
    finite,
    utc,
)
from ml.stock_trader.session import (
    STOCK_TARGET_HORIZONS,
    checkpoint_session_for_target,
    normalize_stock_target_horizon,
)


PRIMARY_STOCK_HORIZON = "1h"
PRIMARY_STOCK_HORIZONS: tuple[str, ...] = STOCK_TARGET_HORIZONS
CONTEXT_HORIZONS: tuple[str, ...] = ("1h", "4h", "1d", "1w")


def load_current_prediction_signals(
    datastore_root: Path,
    *,
    as_of: object,
    target_horizon: object | None = None,
) -> tuple[dict[str, PredictionSignal], tuple[Path, ...]]:
    """Load the newest actionable LIVE Loop B route for each trader symbol."""

    root = Path(datastore_root).resolve()
    timestamp = utc(as_of)
    clean_target_horizon = normalize_stock_target_horizon(target_horizon)
    primary_horizons = (
        PRIMARY_STOCK_HORIZONS
        if clean_target_horizon is None
        else (clean_target_horizon,)
    )
    publication = read_current_publication(root)
    path = publication.run_directory / "predictions.parquet"
    if not path.is_file():
        raise ValueError("Current Loop B publication has no predictions.parquet")
    frame = pd.read_parquet(path)
    missing = sorted(set(PREDICTION_SCHEMA.names).difference(frame.columns))
    if missing:
        raise ValueError("Loop B predictions are missing columns: " + ", ".join(missing))
    eligible = _actionable_live_predictions(frame, as_of=timestamp)
    source_files = tuple(
        path_value
        for path_value in (
            path,
            publication.run_directory / "manifest.json",
            publication.run_directory / "publication.json",
            root / "ml" / "latest" / "run.json",
        )
        if path_value.is_file()
    )
    source_fingerprint = canonical_sha256(
        {
            "run": publication.run_directory.relative_to(root).as_posix(),
            "files": {source.name: file_checksum(source) for source in source_files},
        }
    )
    signals: dict[str, PredictionSignal] = {}
    for symbol in STOCK_TRADER_SYMBOLS:
        rows = eligible.loc[eligible["symbol"].eq(symbol)]
        primary = rows.loc[rows["horizon"].isin(primary_horizons)].copy()
        if primary.empty:
            continue
        primary["__horizon_priority"] = primary["horizon"].map(
            {horizon: index for index, horizon in enumerate(primary_horizons)}
        )
        primary_row = primary.sort_values(
            [
                "target_window_start",
                "__horizon_priority",
                "decision_timestamp",
                "prediction_created_at",
            ],
            ascending=[True, True, False, False],
            kind="mergesort",
        ).iloc[0]
        horizon_probabilities: dict[str, float] = {}
        for horizon, horizon_rows in rows.groupby("horizon", sort=False):
            latest = horizon_rows.sort_values(
                ["decision_timestamp", "prediction_created_at"], kind="mergesort"
            ).iloc[-1]
            probability = finite(latest.get("calibrated_probability"))
            if probability is not None:
                horizon_probabilities[str(horizon)] = probability
        probability = finite(primary_row.get("calibrated_probability"))
        cost = finite(primary_row.get("assumed_round_trip_cost"), default=0.0)
        if probability is None or not 0.0 <= probability <= 1.0 or cost is None:
            continue
        signals[symbol] = PredictionSignal(
            symbol=symbol,
            primary_horizon=str(primary_row["horizon"]),
            prediction_id=str(primary_row["id"]),
            decision_timestamp=_iso(primary_row["decision_timestamp"]),
            target_window_start=_iso(primary_row["target_window_start"]),
            target_window_end=_iso(primary_row["target_window_end"]),
            actionable_until=_iso(primary_row["actionable_until"]),
            prediction_created_at=_iso(primary_row["prediction_created_at"]),
            calibrated_probability=probability,
            assumed_round_trip_cost=max(0.0, cost),
            horizon_probabilities=horizon_probabilities,
            model_name=str(primary_row.get("model_name") or ""),
            model_version=str(primary_row.get("model_version") or ""),
            source_fingerprint=source_fingerprint,
            checkpoint_session=checkpoint_session_for_target(
                primary_row["target_window_start"]
            ),
            target_definition_version=str(
                primary_row.get("target_definition_version") or ""
            ),
        )
    return signals, source_files


def _actionable_live_predictions(frame: pd.DataFrame, *, as_of: pd.Timestamp) -> pd.DataFrame:
    data = frame.copy()
    for column in (
        "decision_timestamp",
        "information_available_at",
        "prediction_created_at",
        "target_window_start",
        "target_window_end",
        "actionable_until",
    ):
        data[column] = pd.to_datetime(data[column], utc=True, errors="coerce")
    data["symbol"] = data["symbol"].astype("string").str.upper()
    data["horizon"] = data["horizon"].astype("string")
    probability = pd.to_numeric(data["calibrated_probability"], errors="coerce")
    mask = (
        data["symbol"].isin(STOCK_TRADER_SYMBOLS)
        & data["horizon"].isin(CONTEXT_HORIZONS)
        & data["prediction_mode"].astype("string").str.upper().eq("LIVE")
        & data["prediction_status"].astype("string").str.upper().eq("CREATED")
        & data["information_available_at"].le(as_of)
        & data["prediction_created_at"].le(as_of)
        & data["decision_timestamp"].le(as_of)
        & data["actionable_until"].gt(as_of)
        & probability.between(0.0, 1.0, inclusive="both")
    )
    return data.loc[mask].copy()


def _iso(value: object) -> str:
    return utc(value).isoformat()


__all__ = [
    "CONTEXT_HORIZONS",
    "PRIMARY_STOCK_HORIZON",
    "PRIMARY_STOCK_HORIZONS",
    "load_current_prediction_signals",
]
