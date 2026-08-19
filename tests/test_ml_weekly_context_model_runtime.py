from __future__ import annotations

import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import pytest

from datafetching.ids import is_opaque_identifier
from ml.calibration import IdentityCalibrator
from ml.horizons import (
    WEEKLY_HORIZON_ORDER,
    horizon_specifications_for_profile,
)
from ml.model_features import ModelFeatureSet, resolve_model_feature_set
from ml.model_runtime import (
    DEFAULT_PARTITION_CONFIGS,
    ModelPartitionConfig,
    RuntimeModel,
    fit_or_reuse_model,
    partition_model_rows,
)
from ml.parquet_contracts import EVALUATION_SCHEMA, PREDICTION_SCHEMA, empty_frame
from ml.rolling_materialization import RollingMaterialization, RouteMaterialization
from ml.runtime_pipeline import (
    MINIMUM_LIVE_DECISIONS,
    _intelligence_frame,
    _live_candidates,
    _prediction_frame,
)


_SYMBOLS = ("GOOG", "MU", "NVDA")
_ROUND_TRIP_COST = 0.001


def test_weekly_context_uses_daily_default_partitions_and_disjoint_clusters(
) -> None:
    expected = ModelPartitionConfig(252, 63, 63, 126)
    assert all(
        DEFAULT_PARTITION_CONFIGS[horizon] == expected
        for horizon in WEEKLY_HORIZON_ORDER
    )
    assert all(
        MINIMUM_LIVE_DECISIONS[horizon] == 30
        for horizon in WEEKLY_HORIZON_ORDER
    )
    assert DEFAULT_PARTITION_CONFIGS["1w"] == DEFAULT_PARTITION_CONFIGS["1d"]

    samples = _daily_cluster_samples(cluster_count=504, symbols=_SYMBOLS)
    target_starts = pd.Index(
        samples["target_window_start"].drop_duplicates().sort_values()
    )

    partitions = partition_model_rows(samples, config=expected)

    assert len(partitions.train) == 252 * len(_SYMBOLS)
    assert len(partitions.calibration) == 63 * len(_SYMBOLS)
    assert len(partitions.assessment) == 63 * len(_SYMBOLS)
    assert partitions.lockbox_row_count == 126 * len(_SYMBOLS)
    assert partitions.lockbox_cluster_count == 126
    assert partitions.train["target_window_start"].nunique() == 252
    assert partitions.calibration["target_window_start"].nunique() == 63
    assert partitions.assessment["target_window_start"].nunique() == 63

    assert partitions.calibration["target_window_start"].min() == target_starts[252]
    assert partitions.assessment["target_window_start"].min() == target_starts[315]
    assert partitions.lockbox_start == target_starts[378]
    assert partitions.lockbox_end == target_starts[-1]
    assert (
        partitions.train["target_window_end"].max()
        < partitions.calibration["target_window_start"].min()
    )
    assert (
        partitions.calibration["target_window_end"].max()
        < partitions.assessment["target_window_start"].min()
    )
    assert (
        partitions.assessment["target_window_end"].max()
        < partitions.lockbox_start
    )

    train_ids = set(partitions.train["id"])
    calibration_ids = set(partitions.calibration["id"])
    assessment_ids = set(partitions.assessment["id"])
    lockbox_ids = set(
        samples.loc[
            samples["target_window_start"].ge(partitions.lockbox_start),
            "id",
        ]
    )
    assert train_ids.isdisjoint(calibration_ids)
    assert train_ids.isdisjoint(assessment_ids)
    assert train_ids.isdisjoint(lockbox_ids)
    assert calibration_ids.isdisjoint(assessment_ids)
    assert calibration_ids.isdisjoint(lockbox_ids)
    assert assessment_ids.isdisjoint(lockbox_ids)


def test_aggregate_partition_purges_overlapping_five_session_windows() -> None:
    samples = _daily_cluster_samples(
        cluster_count=22,
        symbols=("GOOG",),
        target_session_span=5,
    )
    partitions = partition_model_rows(
        samples,
        config=ModelPartitionConfig(4, 2, 2, 2),
    )

    assert partitions.train["target_window_start"].nunique() == 4
    assert partitions.calibration["target_window_start"].nunique() == 2
    assert partitions.assessment["target_window_start"].nunique() == 2
    assert (
        partitions.train["target_window_end"].max()
        < partitions.calibration["target_window_start"].min()
    )
    assert (
        partitions.calibration["target_window_end"].max()
        < partitions.assessment["target_window_start"].min()
    )
    assert (
        partitions.assessment["target_window_end"].max()
        < partitions.lockbox_start
    )


@pytest.mark.parametrize("route", WEEKLY_HORIZON_ORDER)
def test_weekly_target_contracts_are_independent_and_reload_ordered_features(
    tmp_path: Path,
    route: str,
) -> None:
    specifications = horizon_specifications_for_profile(
        "loop-a-all-v1",
        horizons=("1w",),
    )
    specification = specifications[route]
    feature_set = resolve_model_feature_set(
        specification.feature_set,
        horizon=route,
    )
    assert specification.feature_set == "loop-a-all-v1-1w"
    assert len(feature_set.names) == 132
    samples = _daily_cluster_samples(
        cluster_count=10,
        symbols=("GOOG",),
        feature_set=feature_set,
        horizon=route,
    )
    partitions = partition_model_rows(
        samples,
        config=ModelPartitionConfig(4, 2, 2, 2),
    )
    source = tmp_path / "synthetic-loop-a-input.dat"
    source.write_bytes(b"stable synthetic input")
    common = {
        "horizon": route,
        "feature_set_name": specification.feature_set,
        "family": "logistic",
        "calibration_method": "platt",
        "class_weight": None,
        "partitions": partitions,
        "input_files": (source,),
        "specification": specification,
        "assumed_round_trip_cost": _ROUND_TRIP_COST,
    }

    first = fit_or_reuse_model(
        tmp_path,
        **common,
        trained_at="2026-07-30T12:00:00Z",
    )

    assert not first.reused
    first_manifest_path = first.artifact_directory / "manifest.json"
    first_manifest = json.loads(first_manifest_path.read_text(encoding="utf-8"))
    assert first_manifest["feature_columns"] == list(feature_set.names)
    assert first_manifest["target_definition"] == {
        "version": specification.target_definition_version,
        "horizon_specification": specification.as_dict(),
        "assumed_round_trip_cost": _ROUND_TRIP_COST,
    }
    if route == "1w":
        assert first_manifest["model_parameters"] == {
            "C": 0.3,
            "l1_ratio": 1.0,
            "max_iter": 5_000,
            "solver": "liblinear",
            "tol": 1e-5,
        }
        assert first_manifest["calibration_parameters"] == {
            "clip_to_observed_probability_range": True,
            "platt_regularization_c": 0.1,
        }
        assert first_manifest["offline_evaluation"]["calibration_support"][
            "clip_to_observed_probability_range"
        ] is True
    else:
        assert first_manifest["model_parameters"] == {
            "C": 1.0,
            "max_iter": 2_000,
        }
        assert "calibration_parameters" not in first_manifest
    first_bundle = joblib.load(first.artifact_directory / "model.joblib")
    assert tuple(first_bundle["estimator"].feature_names_in_) == feature_set.names

    first_manifest.pop("target_definition")
    first_manifest_path.write_text(
        json.dumps(first_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    replacement = fit_or_reuse_model(
        tmp_path,
        **common,
        trained_at="2026-07-30T12:01:00Z",
    )

    assert not replacement.reused
    assert replacement.artifact_directory != first.artifact_directory
    replacement_manifest = json.loads(
        (replacement.artifact_directory / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert replacement_manifest["feature_columns"] == list(feature_set.names)
    assert replacement_manifest["target_definition"]["version"] == (
        specification.target_definition_version
    )

    reloaded = fit_or_reuse_model(
        tmp_path,
        **common,
        trained_at="2026-07-30T12:02:00Z",
    )

    assert reloaded.reused
    assert reloaded.artifact_directory == replacement.artifact_directory
    assert reloaded.feature_set.names == feature_set.names
    assert tuple(reloaded.estimator.feature_names_in_) == feature_set.names
    assert len(
        list(
            (
                tmp_path / "ml" / "models" / route / f"logistic-{route}"
            ).glob("*/model.joblib")
        )
    ) == 2


def test_preopen_weekly_context_candidate_produces_readable_live_prediction(
    tmp_path: Path,
) -> None:
    specification = horizon_specifications_for_profile(
        "loop-a-all-v1",
        horizons=("1w",),
    )["1w"]
    feature_set = resolve_model_feature_set(
        specification.feature_set,
        horizon="1w",
    )
    decision = pd.Timestamp("2026-07-29T20:05:00Z")
    target_start = pd.Timestamp("2026-07-30T13:30:00Z")
    target_end = pd.Timestamp("2026-07-30T20:00:00Z")
    forecast_timestamp = pd.Timestamp("2026-07-30T12:00:00Z")
    samples = pd.DataFrame(
        [
            {
                "id": f"{symbol}|1w|{_iso_z(decision)}",
                "symbol": symbol,
                "provider": "databento",
                "horizon": "1w",
                "decision_timestamp": decision,
                "information_available_at": decision,
                "target_window_start": target_start,
                "target_window_end": target_end,
                "actionable_until": target_start,
                "label_status": "INCOMPLETE_LABEL",
                "target_cost_adjusted_positive": pd.NA,
                "assumed_round_trip_cost": _ROUND_TRIP_COST,
                **_feature_values(feature_set, cluster_index=0),
            }
            for symbol in _SYMBOLS
        ]
    )
    model = RuntimeModel(
        model_name="logistic-1w",
        horizon="1w",
        feature_set=feature_set,
        estimator=_ConstantProbabilityEstimator(feature_set.names, 0.61),
        calibrator=IdentityCalibrator(),
        calibration_method="none",
        artifact_directory=tmp_path / "20260730T115500.000000Z",
        offline_evaluation={},
        reused=True,
    )

    live = _live_candidates(
        samples,
        as_of=forecast_timestamp,
        latest_per_symbol=True,
    )
    prediction = _prediction_frame(
        model,
        live,
        prediction_created_at=forecast_timestamp,
        mode="LIVE",
    )
    materialization = RollingMaterialization(
        samples=samples,
        routes=tuple(
            RouteMaterialization(
                symbol=symbol,
                horizon="1w",
                status="READY",
                samples=samples.loc[samples["symbol"].eq(symbol)].copy(),
                source_files=(),
            )
            for symbol in _SYMBOLS
        ),
        source_files=(),
        datastore_root=tmp_path,
    )
    intelligence = _intelligence_frame(
        materialization,
        samples,
        prediction,
        empty_frame(EVALUATION_SCHEMA),
        models={"1w": model},
        created_at=forecast_timestamp,
    )

    assert len(live) == len(_SYMBOLS)
    assert len(prediction) == len(_SYMBOLS)
    assert set(prediction["symbol"]) == set(_SYMBOLS)
    assert prediction["horizon"].eq("1w").all()
    assert prediction["prediction_mode"].eq("LIVE").all()
    assert prediction["prediction_status"].eq("CREATED").all()
    assert prediction["target_window_start"].eq(target_start).all()
    assert prediction["target_window_end"].eq(target_end).all()
    assert prediction["prediction_created_at"].lt(target_start).all()
    assert prediction["raw_probability"].eq(0.61).all()
    assert prediction["calibrated_probability"].eq(0.61).all()
    assert prediction["id"].str.count(r"\|").eq(3).all()
    assert prediction["id"].map(is_opaque_identifier).eq(False).all()

    assert len(intelligence) == len(_SYMBOLS)
    assert set(intelligence["symbol"]) == set(_SYMBOLS)
    assert intelligence["probability_up"].notna().all()
    assert intelligence["probability_down"].notna().all()
    assert intelligence["probability_up"].eq(0.61).all()
    assert intelligence["probability_down"].eq(0.39).all()
    assert intelligence["actionability_status"].eq(
        "FROZEN_WEEKLY_SNAPSHOT"
    ).all()
    assert intelligence["intelligence_status"].eq("PENDING_EVIDENCE").all()
    assert intelligence["model_name"].eq("logistic-1w").all()
    assert intelligence["forecast_created_at"].eq(forecast_timestamp).all()


def test_dynamic_weekly_omissions_are_current_only_with_a_coherent_bundle(
    tmp_path: Path,
) -> None:
    symbol = "GOOG"
    decision = pd.Timestamp("2026-08-19T20:05:00Z")
    created = pd.Timestamp("2026-08-19T20:12:00Z")
    windows = {
        "1w": (
            pd.Timestamp("2026-08-20T13:30:00Z"),
            pd.Timestamp("2026-08-21T20:00:00Z"),
        ),
        "1w-d1": (
            pd.Timestamp("2026-08-20T13:30:00Z"),
            pd.Timestamp("2026-08-20T20:00:00Z"),
        ),
        "1w-d2": (
            pd.Timestamp("2026-08-21T13:30:00Z"),
            pd.Timestamp("2026-08-21T20:00:00Z"),
        ),
        "1w-d3": (
            pd.Timestamp("2026-08-24T13:30:00Z"),
            pd.Timestamp("2026-08-24T20:00:00Z"),
        ),
        "1w-d4": (
            pd.Timestamp("2026-08-25T13:30:00Z"),
            pd.Timestamp("2026-08-25T20:00:00Z"),
        ),
        "1w-d5": (
            pd.Timestamp("2026-08-26T13:30:00Z"),
            pd.Timestamp("2026-08-26T20:00:00Z"),
        ),
    }
    samples = pd.DataFrame(
        [
            {
                "symbol": symbol,
                "horizon": horizon,
                "decision_timestamp": decision,
                "information_available_at": decision,
                "target_window_start": start,
                "target_window_end": end,
                "actionable_until": end,
                "target_definition_version": f"dynamic-{horizon}-v2",
            }
            for horizon, (start, end) in windows.items()
        ]
    )
    predictions = pd.DataFrame(
        [
            {
                "id": f"{symbol}|{horizon}|{_iso_z(decision)}|{_iso_z(created)}",
                "symbol": symbol,
                "provider": "databento",
                "horizon": horizon,
                "decision_timestamp": decision,
                "information_available_at": decision,
                "target_window_start": windows[horizon][0],
                "target_window_end": windows[horizon][1],
                "actionable_until": (
                    windows["1w-d1"][1]
                    if horizon == "1w"
                    else windows[horizon][1]
                ),
                "target_definition_version": f"dynamic-{horizon}-v2",
                "target_specification": "synthetic coherent weekly bundle",
                "prediction_created_at": created,
                "model_name": f"logistic-{horizon}",
                "model_version": "20260819T200000.000000Z",
                "calibration_method": "platt",
                "prediction_mode": "LIVE",
                "prediction_status": "CREATED",
                "assumed_round_trip_cost": _ROUND_TRIP_COST,
                "raw_probability": 0.55,
                "calibrated_probability": 0.55,
            }
            for horizon in ("1w", "1w-d1", "1w-d2")
        ]
    )
    materialization = RollingMaterialization(
        samples=samples,
        routes=tuple(
            RouteMaterialization(
                symbol=symbol,
                horizon=horizon,
                status="READY",
                samples=samples.loc[samples["horizon"].eq(horizon)].copy(),
                source_files=(),
            )
            for horizon in WEEKLY_HORIZON_ORDER
        ),
        source_files=(),
        datastore_root=tmp_path,
    )

    intelligence = _intelligence_frame(
        materialization,
        samples,
        predictions,
        empty_frame(EVALUATION_SCHEMA),
        models={},
        created_at=created,
    )
    omitted = intelligence.loc[
        intelligence["horizon"].isin(("1w-d3", "1w-d4", "1w-d5"))
    ]

    assert omitted["operational_status"].eq("OPERATIONALLY_CURRENT").all()
    assert omitted["intelligence_status"].eq(
        "NOT_APPLICABLE_TO_REMAINING_WEEK"
    ).all()
    assert omitted["actionability_status"].eq("NOT_ACTIONABLE").all()
    assert omitted["probability_up"].isna().all()
    assert omitted["limitations"].str.contains(
        "not part of current remaining-week snapshot", regex=False
    ).all()

    missing_bundle = _intelligence_frame(
        materialization,
        samples,
        empty_frame(PREDICTION_SCHEMA),
        empty_frame(EVALUATION_SCHEMA),
        models={},
        created_at=created,
    )
    missing_components = missing_bundle.loc[
        missing_bundle["horizon"].isin(("1w-d3", "1w-d4", "1w-d5"))
    ]
    assert missing_components["operational_status"].eq(
        "OPERATIONALLY_STALE"
    ).all()
    assert missing_components["intelligence_status"].eq(
        "NO_CURRENT_FORECAST"
    ).all()


class _ConstantProbabilityEstimator:
    def __init__(
        self,
        expected_features: tuple[str, ...],
        probability: float,
    ) -> None:
        self.expected_features = expected_features
        self.probability = probability

    def predict_proba(self, rows: pd.DataFrame) -> np.ndarray:
        assert tuple(rows.columns) == self.expected_features
        positive = np.full(len(rows), self.probability, dtype=float)
        return np.column_stack((1.0 - positive, positive))


def _daily_cluster_samples(
    *,
    cluster_count: int,
    symbols: tuple[str, ...],
    feature_set: ModelFeatureSet | None = None,
    horizon: str = "1w",
    target_session_span: int = 1,
) -> pd.DataFrame:
    sessions = pd.bdate_range(
        "2023-01-03",
        periods=cluster_count,
        tz="UTC",
    )
    rows: list[dict[str, object]] = []
    for cluster_index, session in enumerate(sessions):
        target_start = session + pd.Timedelta(hours=14, minutes=30)
        target_end = (
            session
            + pd.offsets.BusinessDay(target_session_span - 1)
            + pd.Timedelta(hours=21)
        )
        decision = target_start - pd.Timedelta(hours=18, minutes=25)
        for symbol_index, symbol in enumerate(symbols):
            row: dict[str, object] = {
                "id": f"{symbol}|{horizon}|{_iso_z(decision)}",
                "symbol": symbol,
                "provider": "databento",
                "horizon": horizon,
                "decision_timestamp": decision,
                "information_available_at": decision,
                "target_window_start": target_start,
                "target_window_end": target_end,
                "actionable_until": target_start,
                "label_status": "COMPLETE",
                "target_cost_adjusted_positive": (
                    cluster_index + symbol_index
                )
                % 2,
                "previous_period_direction": cluster_index % 2,
                "assumed_round_trip_cost": _ROUND_TRIP_COST,
            }
            if feature_set is not None:
                row.update(
                    _feature_values(
                        feature_set,
                        cluster_index=cluster_index,
                    )
                )
            rows.append(row)
    return pd.DataFrame(rows)


def _feature_values(
    feature_set: ModelFeatureSet,
    *,
    cluster_index: int,
) -> dict[str, object]:
    values: dict[str, object] = {}
    for feature_index, feature in enumerate(feature_set.features):
        if feature.dtype in {"category", "string"}:
            value: object = "even" if cluster_index % 2 == 0 else "odd"
        elif feature.dtype == "bool":
            value = cluster_index % 2 == 0
        else:
            value = (
                1.0
                + float(cluster_index)
                + float(feature_index + 1) / 1_000.0
            )
        values[feature.name] = value
    return values


def _iso_z(value: pd.Timestamp) -> str:
    return value.isoformat().replace("+00:00", "Z")
