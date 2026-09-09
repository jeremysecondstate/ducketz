from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from ml.sequence_encoder.contracts import SEQUENCE_FEATURE_COLUMNS, SequenceEncoderConfig


@dataclass(frozen=True)
class RobustSequenceScaler:
    median: tuple[float, ...]
    scale: tuple[float, ...]
    feature_columns: tuple[str, ...] = SEQUENCE_FEATURE_COLUMNS

    @classmethod
    def fit(
        cls,
        states: pd.DataFrame,
        *,
        feature_columns: Sequence[str] = SEQUENCE_FEATURE_COLUMNS,
    ) -> "RobustSequenceScaler":
        columns = tuple(feature_columns)
        missing = sorted(set(columns).difference(states.columns))
        if missing:
            raise ValueError("Sequence states are missing: " + ", ".join(missing))
        matrix = states.loc[:, columns].apply(
            pd.to_numeric, errors="coerce"
        ).to_numpy(dtype=float, copy=True)
        matrix[~np.isfinite(matrix)] = np.nan
        with np.errstate(all="ignore"):
            median = np.nanmedian(matrix, axis=0)
            lower = np.nanquantile(matrix, 0.25, axis=0)
            upper = np.nanquantile(matrix, 0.75, axis=0)
        median = np.where(np.isfinite(median), median, 0.0)
        scale = upper - lower
        scale = np.where(np.isfinite(scale) & (scale > 1.0e-12), scale, 1.0)
        return cls(
            median=tuple(float(value) for value in median),
            scale=tuple(float(value) for value in scale),
            feature_columns=columns,
        )

    def transform(self, states: pd.DataFrame) -> np.ndarray:
        matrix = states.loc[:, self.feature_columns].apply(
            pd.to_numeric, errors="coerce"
        ).to_numpy(dtype=float)
        missing = ~np.isfinite(matrix)
        median = np.asarray(self.median, dtype=float)
        scale = np.asarray(self.scale, dtype=float)
        values = np.where(missing, median, matrix)
        values = np.clip((values - median) / scale, -12.0, 12.0)
        return np.concatenate([values, missing.astype(float)], axis=1).astype(
            np.float32
        )

    def semantic_contract(self) -> dict[str, object]:
        return {
            "feature_columns": list(self.feature_columns),
            "median": list(self.median),
            "scale": list(self.scale),
            "missingness_encoding": "value-plus-explicit-mask-v1",
            "clip_range": [-12.0, 12.0],
        }

    @classmethod
    def from_contract(cls, value: Mapping[str, object]) -> "RobustSequenceScaler":
        return cls(
            median=tuple(float(item) for item in value["median"]),  # type: ignore[index]
            scale=tuple(float(item) for item in value["scale"]),  # type: ignore[index]
            feature_columns=tuple(str(item) for item in value["feature_columns"]),  # type: ignore[index]
        )


@dataclass(frozen=True)
class SequencePartitions:
    train: pd.DataFrame
    calibration: pd.DataFrame
    assessment: pd.DataFrame
    purged_rows: int
    train_clusters: int
    calibration_clusters: int
    assessment_clusters: int


@dataclass(frozen=True)
class WindowedExamples:
    windows: np.ndarray
    symbol_ids: np.ndarray
    horizon_ids: np.ndarray
    direction_targets: np.ndarray
    return_targets: np.ndarray
    sample_weights: np.ndarray
    metadata: pd.DataFrame

    def __len__(self) -> int:
        return int(self.windows.shape[0])


def chronological_partitions(
    labels: pd.DataFrame,
    *,
    config: SequenceEncoderConfig,
) -> dict[str, SequencePartitions]:
    """Split each horizon by target cluster and purge overlapping boundaries."""

    required = {
        "horizon",
        "target_window_start",
        "target_window_end",
        "decision_timestamp",
    }
    missing = sorted(required.difference(labels.columns))
    if missing:
        raise ValueError("Labels are missing partition columns: " + ", ".join(missing))
    output: dict[str, SequencePartitions] = {}
    for horizon in config.horizons:
        frame = labels.loc[labels["horizon"].astype("string").eq(horizon)].copy()
        if frame.empty:
            continue
        clusters = (
            frame.loc[:, ["target_window_start", "target_window_end"]]
            .drop_duplicates()
            .sort_values(["target_window_start", "target_window_end"])
            .reset_index(drop=True)
        )
        required_count = (
            config.minimum_train_clusters
            + config.calibration_clusters
            + config.assessment_clusters
        )
        if len(clusters) < required_count:
            continue
        assessment_clusters = clusters.tail(config.assessment_clusters)
        remaining = clusters.iloc[: -config.assessment_clusters]
        calibration_clusters = remaining.tail(config.calibration_clusters)
        train_clusters = remaining.iloc[: -config.calibration_clusters]
        if len(train_clusters) < config.minimum_train_clusters:
            continue
        train = _rows_for_clusters(frame, train_clusters)
        calibration = _rows_for_clusters(frame, calibration_clusters)
        assessment = _rows_for_clusters(frame, assessment_clusters)
        original_rows = len(train) + len(calibration) + len(assessment)
        embargo = pd.Timedelta(hours=config.embargo_hours)
        calibration_start = calibration["target_window_start"].min()
        assessment_start = assessment["target_window_start"].min()
        train = train.loc[
            train["target_window_end"].lt(calibration_start - embargo)
        ].copy()
        calibration = calibration.loc[
            calibration["target_window_end"].lt(assessment_start - embargo)
        ].copy()
        if train.empty or calibration.empty or assessment.empty:
            continue
        output[horizon] = SequencePartitions(
            train=train,
            calibration=calibration,
            assessment=assessment,
            purged_rows=original_rows - len(train) - len(calibration) - len(assessment),
            train_clusters=_cluster_count(train),
            calibration_clusters=_cluster_count(calibration),
            assessment_clusters=_cluster_count(assessment),
        )
    return output


def build_windowed_examples(
    states: pd.DataFrame,
    labels: pd.DataFrame,
    *,
    scaler: RobustSequenceScaler,
    config: SequenceEncoderConfig,
    symbol_vocabulary: Mapping[str, int],
    horizon_vocabulary: Mapping[str, int],
) -> WindowedExamples:
    required_state = {
        "symbol",
        "bar_timestamp",
        "information_available_at",
        *config.feature_columns,
    }
    missing_state = sorted(required_state.difference(states.columns))
    if missing_state:
        raise ValueError("States are missing: " + ", ".join(missing_state))
    required_label = {
        "symbol",
        "horizon",
        "decision_timestamp",
        "bar_end_timestamp",
        "target_cost_adjusted_positive",
        "forward_cost_adjusted_return",
        "decision_weight",
    }
    missing_label = sorted(required_label.difference(labels.columns))
    if missing_label:
        raise ValueError("Labels are missing: " + ", ".join(missing_label))

    state_frame = states.copy()
    state_frame["bar_timestamp"] = pd.to_datetime(
        state_frame["bar_timestamp"], utc=True, errors="coerce"
    )
    state_frame["information_available_at"] = pd.to_datetime(
        state_frame["information_available_at"], utc=True, errors="coerce"
    )
    state_frame = state_frame.dropna(
        subset=["symbol", "bar_timestamp", "information_available_at"]
    ).sort_values(["symbol", "bar_timestamp"])

    windows: list[np.ndarray] = []
    symbol_ids: list[int] = []
    horizon_ids: list[int] = []
    directions: list[float] = []
    returns: list[float] = []
    weights: list[float] = []
    metadata: list[dict[str, object]] = []
    by_symbol = {
        str(symbol): group.reset_index(drop=True)
        for symbol, group in state_frame.groupby("symbol", sort=False)
    }
    transformed = {
        symbol: scaler.transform(group)
        for symbol, group in by_symbol.items()
    }
    for label in labels.sort_values("decision_timestamp").itertuples(index=False):
        symbol = str(label.symbol)
        horizon = str(label.horizon)
        group = by_symbol.get(symbol)
        if group is None or horizon not in horizon_vocabulary:
            continue
        decision = _utc(label.decision_timestamp, "decision_timestamp")
        bar_end = _utc(label.bar_end_timestamp, "bar_end_timestamp")
        eligible = group["bar_timestamp"].le(bar_end) & group[
            "information_available_at"
        ].le(decision)
        positions = np.flatnonzero(eligible.to_numpy())
        if not positions.size:
            continue
        end = int(positions[-1])
        start = end - config.window_length + 1
        if start < 0:
            continue
        window_frame = group.iloc[start : end + 1]
        if window_frame["information_available_at"].max() > decision:
            raise ValueError("Sequence window contains future-available evidence")
        windows.append(transformed[symbol][start : end + 1])
        symbol_ids.append(int(symbol_vocabulary.get(symbol, 0)))
        horizon_ids.append(int(horizon_vocabulary[horizon]))
        directions.append(float(label.target_cost_adjusted_positive))
        returns.append(float(label.forward_cost_adjusted_return))
        weights.append(float(label.decision_weight))
        metadata.append(
            {
                "symbol": symbol,
                "horizon": horizon,
                "decision_timestamp": decision,
                "information_available_at": window_frame[
                    "information_available_at"
                ].max(),
                "target_window_start": _utc(
                    getattr(label, "target_window_start"), "target_window_start"
                ),
                "target_window_end": _utc(
                    getattr(label, "target_window_end"), "target_window_end"
                ),
                "sequence_window_start": window_frame["bar_timestamp"].iloc[0],
                "sequence_window_end": window_frame["bar_timestamp"].iloc[-1],
                "source_observation_count": len(window_frame),
            }
        )
    input_width = len(config.feature_columns) * 2
    if not windows:
        return WindowedExamples(
            windows=np.empty((0, config.window_length, input_width), dtype=np.float32),
            symbol_ids=np.empty(0, dtype=np.int64),
            horizon_ids=np.empty(0, dtype=np.int64),
            direction_targets=np.empty(0, dtype=np.float32),
            return_targets=np.empty(0, dtype=np.float32),
            sample_weights=np.empty(0, dtype=np.float32),
            metadata=pd.DataFrame(columns=tuple(metadata[0]) if metadata else ()),
        )
    return WindowedExamples(
        windows=np.stack(windows).astype(np.float32),
        symbol_ids=np.asarray(symbol_ids, dtype=np.int64),
        horizon_ids=np.asarray(horizon_ids, dtype=np.int64),
        direction_targets=np.asarray(directions, dtype=np.float32),
        return_targets=np.asarray(returns, dtype=np.float32),
        sample_weights=np.asarray(weights, dtype=np.float32),
        metadata=pd.DataFrame(metadata),
    )


def pretraining_windows(
    states: pd.DataFrame,
    *,
    scaler: RobustSequenceScaler,
    config: SequenceEncoderConfig,
    through: object,
) -> tuple[np.ndarray, np.ndarray]:
    cutoff = _utc(through, "pretraining cutoff")
    windows: list[np.ndarray] = []
    targets: list[np.ndarray] = []
    for _, group in states.loc[
        pd.to_datetime(states["information_available_at"], utc=True, errors="coerce").le(
            cutoff
        )
    ].groupby("symbol", sort=False):
        ordered = group.sort_values("bar_timestamp").reset_index(drop=True)
        transformed = scaler.transform(ordered)
        values = transformed[:, : len(config.feature_columns)]
        for end in range(config.window_length - 1, len(ordered) - 1):
            windows.append(transformed[end - config.window_length + 1 : end + 1])
            targets.append(values[end + 1])
    input_width = len(config.feature_columns) * 2
    if not windows:
        return (
            np.empty((0, config.window_length, input_width), dtype=np.float32),
            np.empty((0, len(config.feature_columns)), dtype=np.float32),
        )
    return np.stack(windows).astype(np.float32), np.stack(targets).astype(np.float32)


def _rows_for_clusters(frame: pd.DataFrame, clusters: pd.DataFrame) -> pd.DataFrame:
    return frame.merge(
        clusters,
        on=["target_window_start", "target_window_end"],
        how="inner",
        validate="many_to_one",
    ).sort_values(["target_window_start", "decision_timestamp", "symbol"])


def _cluster_count(frame: pd.DataFrame) -> int:
    return len(
        frame.loc[:, ["target_window_start", "target_window_end"]].drop_duplicates()
    )


def _utc(value: object, label: str) -> pd.Timestamp:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"{label} must be a valid timestamp")
    return pd.Timestamp(parsed)


__all__ = [
    "RobustSequenceScaler",
    "SequencePartitions",
    "WindowedExamples",
    "build_windowed_examples",
    "chronological_partitions",
    "pretraining_windows",
]
