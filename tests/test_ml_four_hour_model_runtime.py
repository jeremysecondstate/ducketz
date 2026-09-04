from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from ml.horizons import horizon_specification
from ml.model_features import ModelFeatureSet, resolve_model_feature_set
from ml.model_runtime import (
    DEFAULT_PARTITION_CONFIGS,
    ModelPartitionConfig,
    fit_or_reuse_model,
    partition_model_rows,
)
from ml.parquet_contracts import frame_with_readable_id
from ml.preprocessing import preprocessing_policy
from ml.runtime_pipeline import (
    MINIMUM_LIVE_DECISIONS,
    _closed_lockbox_view,
    _evaluation_frame,
)


_COST = 0.001
_PARTITIONS = ModelPartitionConfig(
    minimum_train_clusters=4,
    calibration_clusters=2,
    assessment_clusters=2,
    lockbox_clusters=2,
)


def test_intraday_defaults_fit_bounded_minute_history_without_weakening_daily() -> None:
    expected_one_hour = ModelPartitionConfig(160, 40, 40, 80)
    expected_four_hour = ModelPartitionConfig(128, 32, 32, 64)
    assert DEFAULT_PARTITION_CONFIGS["1h"] == expected_one_hour
    assert DEFAULT_PARTITION_CONFIGS["4h"] == expected_four_hour
    assert DEFAULT_PARTITION_CONFIGS["1d"] == ModelPartitionConfig(
        252,
        63,
        63,
        126,
    )
    assert MINIMUM_LIVE_DECISIONS["4h"] == 60

    for horizon, window, cluster_count in (
        ("1h", pd.Timedelta(hours=1), 343),
        # Mirrors the 308 completed target starts observed in the bounded
        # production four-checkpoint materialization.
        ("4h", pd.Timedelta(hours=3), 308),
    ):
        samples = _overlapping_samples(cluster_count=cluster_count).copy()
        samples["horizon"] = horizon
        samples["id"] = [
            f"GOOG|{horizon}|{_iso_z(value)}"
            for value in samples["decision_timestamp"]
        ]
        samples["target_window_end"] = samples["target_window_start"] + window

        partitions = partition_model_rows(
            samples,
            config=DEFAULT_PARTITION_CONFIGS[horizon],
        )

        expected = DEFAULT_PARTITION_CONFIGS[horizon]
        assert (
            partitions.train["target_window_start"].nunique()
            >= expected.minimum_train_clusters
        )
        assert (
            partitions.calibration["target_window_start"].nunique()
            == expected.calibration_clusters
        )
        assert (
            partitions.assessment["target_window_start"].nunique()
            == expected.assessment_clusters
        )
        assert partitions.lockbox_cluster_count == expected.lockbox_clusters


def test_four_hour_overlaps_are_purged_across_every_partition_boundary() -> None:
    samples = _overlapping_samples(cluster_count=22)
    starts = pd.DatetimeIndex(samples["target_window_start"])

    partitions = partition_model_rows(samples, config=_PARTITIONS)

    assert tuple(partitions.train["target_window_start"]) == tuple(starts[:4])
    assert tuple(partitions.calibration["target_window_start"]) == tuple(
        starts[8:10]
    )
    assert tuple(partitions.assessment["target_window_start"]) == tuple(
        starts[14:16]
    )
    assert partitions.lockbox_start == starts[20]
    assert partitions.lockbox_end == starts[21]
    assert partitions.lockbox_row_count == 2
    assert partitions.lockbox_cluster_count == 2
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


def test_four_hour_partition_fails_clearly_when_purging_exhausts_training() -> None:
    with pytest.raises(
        ValueError,
        match="purging left fewer training clusters than required",
    ):
        partition_model_rows(
            _overlapping_samples(cluster_count=21),
            config=_PARTITIONS,
        )


def test_partition_purging_follows_window_geometry_not_horizon_name() -> None:
    samples = _overlapping_samples(cluster_count=22).copy()
    samples["horizon"] = "1h"
    samples["id"] = [
        f"GOOG|1h|{_iso_z(value)}"
        for value in samples["decision_timestamp"]
    ]
    partitions = partition_model_rows(samples, config=_PARTITIONS)
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


def test_exactly_touching_target_windows_are_conservatively_purged() -> None:
    feature_set = resolve_model_feature_set(
        horizon_specification("1h").feature_set,
        horizon="1h",
    )
    samples = _non_overlapping_samples(
        horizon="1h",
        cluster_count=13,
        feature_set=feature_set,
        window=pd.Timedelta(hours=1),
    )
    starts = pd.DatetimeIndex(samples["target_window_start"])

    partitions = partition_model_rows(samples, config=_PARTITIONS)

    assert tuple(partitions.train["target_window_start"]) == tuple(starts[:4])
    assert tuple(partitions.calibration["target_window_start"]) == tuple(
        starts[5:7]
    )
    assert tuple(partitions.assessment["target_window_start"]) == tuple(
        starts[8:10]
    )
    assert partitions.lockbox_cluster_values == tuple(starts[11:13])
    assert (
        partitions.train["target_window_end"].max()
        < partitions.calibration["target_window_start"].min()
    )


def test_live_predicted_targets_are_not_reclassified_into_closed_lockbox() -> None:
    samples = _overlapping_samples(cluster_count=23)
    matured_live_start = pd.Timestamp(samples.iloc[-1]["target_window_start"])

    partitions = partition_model_rows(
        samples,
        config=_PARTITIONS,
        excluded_target_starts=(matured_live_start,),
    )

    assert matured_live_start not in partitions.lockbox_cluster_values
    assert partitions.lockbox_cluster_values == tuple(
        pd.to_datetime(
            samples["target_window_start"].iloc[-3:-1],
            utc=True,
        )
    )
    assert all(
        matured_live_start not in set(frame["target_window_start"])
        for frame in (
            partitions.train,
            partitions.calibration,
            partitions.assessment,
        )
    )


def test_closed_lockbox_filter_uses_exact_complete_cluster_membership() -> None:
    samples = _overlapping_samples(cluster_count=22)
    incomplete = samples.iloc[0].copy()
    incomplete_start = (
        pd.Timestamp(samples.iloc[-2]["target_window_start"])
        + pd.Timedelta(minutes=30)
    )
    incomplete["id"] = f"GOOG|4h|{_iso_z(incomplete_start)}"
    incomplete["decision_timestamp"] = incomplete_start
    incomplete["target_window_start"] = incomplete_start
    incomplete["target_window_end"] = incomplete_start + pd.Timedelta(hours=4)
    incomplete["label_status"] = "INCOMPLETE_LABEL"
    incomplete["target_cost_adjusted_positive"] = pd.NA
    combined = pd.concat(
        [samples, incomplete.to_frame().T],
        ignore_index=True,
        sort=False,
    )
    partitions = partition_model_rows(combined, config=_PARTITIONS)

    published = _closed_lockbox_view(
        combined,
        partitions_by_horizon={"4h": partitions},
    )

    assert incomplete["id"] in set(published["id"])
    assert not set(
        combined.loc[
            combined["target_window_start"].isin(
                partitions.lockbox_cluster_values
            ),
            "id",
        ]
    ).intersection(published["id"])


def test_four_hour_target_contract_is_model_compatibility_metadata(
    tmp_path: Path,
) -> None:
    specification = horizon_specification("4h")
    feature_set = resolve_model_feature_set(
        specification.feature_set,
        horizon="4h",
    )
    samples = _overlapping_samples(
        cluster_count=22,
        feature_set=feature_set,
    )
    partitions = partition_model_rows(samples, config=_PARTITIONS)
    source = tmp_path / "synthetic-1h-source.dat"
    source.write_bytes(b"stable synthetic hourly source")
    common = {
        "horizon": "4h",
        "feature_set_name": specification.feature_set,
        "family": "logistic",
        "calibration_method": "platt",
        "class_weight": None,
        "partitions": partitions,
        "input_files": (source,),
        "assumed_round_trip_cost": _COST,
    }

    first = fit_or_reuse_model(
        tmp_path,
        **common,
        specification=specification,
        trained_at="2026-07-30T12:00:00Z",
    )
    reused = fit_or_reuse_model(
        tmp_path,
        **common,
        specification=specification,
        trained_at="2026-07-30T12:01:00Z",
    )
    first_manifest_path = first.artifact_directory / "manifest.json"
    manifest = json.loads(first_manifest_path.read_text(encoding="utf-8"))
    assert manifest["preprocessing_policy"] == preprocessing_policy("logistic")

    stale_manifest = dict(manifest)
    stale_manifest["preprocessing_policy"] = {
        **manifest["preprocessing_policy"],
        "numeric_quantile_clipping": {
            "lower_quantile": 0.005,
            "upper_quantile": 0.995,
        },
    }
    first_manifest_path.write_text(
        json.dumps(stale_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    stale_policy = fit_or_reuse_model(
        tmp_path,
        **common,
        specification=specification,
        trained_at="2026-07-30T12:02:00Z",
    )
    changed = fit_or_reuse_model(
        tmp_path,
        **common,
        specification=replace(
            specification,
            target_definition_version=(
                "next-180-eligible-equity-minutes-four-checkpoints-test-change"
            ),
        ),
        trained_at="2026-07-30T12:03:00Z",
    )

    assert first.model_name == "logistic-4h"
    assert first.artifact_directory.parent == (
        tmp_path / "ml" / "models" / "4h" / "logistic-4h"
    )
    assert not first.reused
    assert reused.reused
    assert reused.artifact_directory == first.artifact_directory
    assert not stale_policy.reused
    assert stale_policy.artifact_directory != first.artifact_directory
    assert not changed.reused
    assert changed.artifact_directory != first.artifact_directory
    target_definition = manifest["target_definition"]
    assert target_definition["version"] == (
        "next-180-eligible-equity-minutes-four-checkpoints-v4"
    )
    assert target_definition["horizon_specification"] == specification.as_dict()
    assert target_definition["calendar_policy"] == (
        {
            "version": specification.target_calendar_policy_version,
            "definition": specification.exchange_calendar_rule,
        }
    )
    assert target_definition["target_price_source"] == {
        "provider": "databento",
        "timeframe": "1m",
        "version": specification.target_price_source_version,
        "constituent_rule": specification.target_constituent_rule,
    }
    assert target_definition["processing_delay"] == "0 days 00:05:00"
    assert target_definition["cost_convention"] == {
        "definition": specification.cost_convention,
        "assumed_round_trip_cost": _COST,
        "application": (
            "subtract_exactly_once_from_first_target_minute_open_to_"
            "final_target_minute_close_simple_return"
        ),
        "positive_class": (
            "cost_adjusted_return_strictly_greater_than_zero"
        ),
    }


def test_four_hour_contract_change_does_not_invalidate_one_hour_model(
    tmp_path: Path,
) -> None:
    one_hour_specification = horizon_specification("1h")
    one_hour_features = resolve_model_feature_set(
        one_hour_specification.feature_set,
        horizon="1h",
    )
    one_hour_partitions = partition_model_rows(
        _non_overlapping_samples(
            horizon="1h",
            cluster_count=10,
            feature_set=one_hour_features,
        ),
        config=_PARTITIONS,
    )
    one_hour_source = tmp_path / "synthetic-1h-compatibility.dat"
    one_hour_source.write_bytes(b"stable one-hour compatibility source")
    one_hour_arguments = {
        "horizon": "1h",
        "feature_set_name": one_hour_specification.feature_set,
        "family": "logistic",
        "calibration_method": "platt",
        "class_weight": None,
        "partitions": one_hour_partitions,
        "input_files": (one_hour_source,),
        "specification": one_hour_specification,
        "assumed_round_trip_cost": _COST,
    }
    first = fit_or_reuse_model(
        tmp_path,
        **one_hour_arguments,
        trained_at="2026-07-30T13:00:00Z",
    )

    four_hour_specification = replace(
        horizon_specification("4h"),
        target_definition_version=(
            "next-180-eligible-equity-minutes-four-checkpoints-isolation-test"
        ),
    )
    four_hour_features = resolve_model_feature_set(
        four_hour_specification.feature_set,
        horizon="4h",
    )
    four_hour_source = tmp_path / "synthetic-4h-isolation.dat"
    four_hour_source.write_bytes(b"stable four-hour isolation source")
    fit_or_reuse_model(
        tmp_path,
        horizon="4h",
        feature_set_name=four_hour_specification.feature_set,
        family="logistic",
        calibration_method="platt",
        class_weight=None,
        partitions=partition_model_rows(
            _overlapping_samples(
                cluster_count=22,
                feature_set=four_hour_features,
            ),
            config=_PARTITIONS,
        ),
        input_files=(four_hour_source,),
        specification=four_hour_specification,
        assumed_round_trip_cost=_COST,
        trained_at="2026-07-30T13:01:00Z",
    )

    reused = fit_or_reuse_model(
        tmp_path,
        **one_hour_arguments,
        trained_at="2026-07-30T13:02:00Z",
    )

    assert not first.reused
    assert reused.reused
    assert reused.artifact_directory == first.artifact_directory
    one_hour_manifest = json.loads(
        (first.artifact_directory / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert one_hour_manifest["target_definition"]["version"] == (
        "next-60-eligible-equity-minutes-open-close-v6"
    )
    assert one_hour_manifest["target_definition"][
        "horizon_specification"
    ] == one_hour_specification.as_dict()


def test_one_hour_target_contract_change_invalidates_only_one_hour_reuse(
    tmp_path: Path,
) -> None:
    specification = horizon_specification("1h")
    feature_set = resolve_model_feature_set(
        specification.feature_set,
        horizon="1h",
    )
    partitions = partition_model_rows(
        _non_overlapping_samples(
            horizon="1h",
            cluster_count=10,
            feature_set=feature_set,
        ),
        config=_PARTITIONS,
    )
    source = tmp_path / "synthetic-one-hour-target-source.dat"
    source.write_bytes(b"stable one-hour target source")
    common = {
        "horizon": "1h",
        "feature_set_name": specification.feature_set,
        "family": "logistic",
        "calibration_method": "platt",
        "class_weight": None,
        "partitions": partitions,
        "input_files": (source,),
        "assumed_round_trip_cost": _COST,
    }
    first = fit_or_reuse_model(
        tmp_path,
        **common,
        specification=specification,
        trained_at="2026-07-30T14:00:00Z",
    )
    changed = fit_or_reuse_model(
        tmp_path,
        **common,
        specification=replace(
            specification,
            target_definition_version=(
                "next-60-eligible-equity-minutes-open-close-test-change"
            ),
        ),
        trained_at="2026-07-30T14:01:00Z",
    )
    assert not first.reused
    assert not changed.reused
    assert changed.artifact_directory != first.artifact_directory


def test_prior_live_four_hour_prediction_reconciles_without_horizon_collision(
) -> None:
    decision = pd.Timestamp("2026-07-30T15:05:00Z")
    created = pd.Timestamp("2026-07-30T15:10:00Z")
    evaluated = pd.Timestamp("2026-07-31T20:05:00Z")
    natural_rows = (
        (
            "4h",
            pd.Timestamp("2026-07-30T16:30:00Z"),
            pd.Timestamp("2026-07-30T20:00:00Z"),
            1,
        ),
        (
            "1h",
            pd.Timestamp("2026-07-30T16:30:00Z"),
            pd.Timestamp("2026-07-30T17:30:00Z"),
            0,
        ),
    )
    prediction_rows: list[dict[str, object]] = []
    sample_rows: list[dict[str, object]] = []
    for horizon, target_start, target_end, observed in natural_rows:
        specification = horizon_specification(horizon)
        target_specification = json.dumps(
            specification.as_dict(),
            sort_keys=True,
            separators=(",", ":"),
        )
        prediction_rows.append(
            {
                "symbol": "GOOG",
                "provider": "databento",
                "horizon": horizon,
                "decision_timestamp": decision,
                "information_available_at": decision,
                "target_window_start": target_start,
                "target_window_end": target_end,
                "actionable_until": target_start,
                "prediction_created_at": created,
                "model_name": f"logistic-{horizon}",
                "model_version": "synthetic-v2",
                "prediction_mode": "LIVE",
                "prediction_status": "CREATED",
                "target_definition_version": (
                    specification.target_definition_version
                ),
                "target_specification": target_specification,
                "assumed_round_trip_cost": _COST,
                "raw_probability": 0.6,
                "calibrated_probability": 0.6,
            }
        )
        sample_rows.append(
            {
                "symbol": "GOOG",
                "horizon": horizon,
                "decision_timestamp": decision,
                "label_status": "COMPLETE",
                "target_definition_version": (
                    specification.target_definition_version
                ),
                "target_specification": target_specification,
                "target_window_start": target_start,
                "target_window_end": target_end,
                "actionable_until": target_start,
                "assumed_round_trip_cost": _COST,
                "target_cost_adjusted_positive": observed,
                "forward_raw_return": 0.01 if observed else -0.01,
                "forward_cost_adjusted_return": (
                    0.01 - _COST if observed else -0.01 - _COST
                ),
            }
        )
    predictions = frame_with_readable_id(
        pd.DataFrame(prediction_rows),
        key_columns=(
            "symbol",
            "horizon",
            "decision_timestamp",
            "prediction_created_at",
        ),
    )

    reconciled = _evaluation_frame(
        predictions,
        pd.DataFrame(sample_rows),
        evaluated_at=evaluated,
    )

    assert len(reconciled) == 2
    assert reconciled["id"].is_unique
    assert set(reconciled["horizon"]) == {"1h", "4h"}
    assert reconciled["evaluation_status"].eq("EVALUATED").all()
    assert (
        reconciled.set_index("horizon")["observed_target"].astype(int).to_dict()
        == {"1h": 0, "4h": 1}
    )
    assert reconciled.loc[
        reconciled["horizon"].eq("4h"),
        "id",
    ].str.startswith("GOOG|4h|").all()


def _overlapping_samples(
    *,
    cluster_count: int,
    feature_set: ModelFeatureSet | None = None,
) -> pd.DataFrame:
    starts = pd.date_range(
        "2026-01-05T14:30:00Z",
        periods=cluster_count,
        freq="h",
    )
    rows: list[dict[str, object]] = []
    for index, target_start in enumerate(starts):
        decision = target_start - pd.Timedelta(minutes=25)
        row: dict[str, object] = {
            "id": f"GOOG|4h|{_iso_z(decision)}",
            "symbol": "GOOG",
            "provider": "databento",
            "horizon": "4h",
            "decision_timestamp": decision,
            "information_available_at": decision,
            "target_window_start": target_start,
            "target_window_end": target_start + pd.Timedelta(hours=4),
            "actionable_until": target_start,
            "label_status": "COMPLETE",
            "target_cost_adjusted_positive": (
                "LOCKBOX_TARGET_MUST_NOT_BE_READ"
                if index >= cluster_count - _PARTITIONS.lockbox_clusters
                else index % 2
            ),
            "previous_period_direction": (index + 1) % 2,
            "assumed_round_trip_cost": _COST,
        }
        if feature_set is not None:
            row.update(_feature_values(feature_set, index=index))
        rows.append(row)
    return pd.DataFrame(rows)


def _non_overlapping_samples(
    *,
    horizon: str,
    cluster_count: int,
    feature_set: ModelFeatureSet,
    window: pd.Timedelta = pd.Timedelta(minutes=30),
) -> pd.DataFrame:
    starts = pd.date_range(
        "2026-01-05T14:30:00Z",
        periods=cluster_count,
        freq="h",
    )
    rows: list[dict[str, object]] = []
    for index, target_start in enumerate(starts):
        decision = target_start - pd.Timedelta(minutes=25)
        rows.append(
            {
                "id": f"GOOG|{horizon}|{_iso_z(decision)}",
                "symbol": "GOOG",
                "provider": "databento",
                "horizon": horizon,
                "decision_timestamp": decision,
                "information_available_at": decision,
                "target_window_start": target_start,
                "target_window_end": target_start + window,
                "actionable_until": target_start,
                "label_status": "COMPLETE",
                "target_cost_adjusted_positive": (
                    "LOCKBOX_TARGET_MUST_NOT_BE_READ"
                    if index >= cluster_count - _PARTITIONS.lockbox_clusters
                    else index % 2
                ),
                "previous_period_direction": (index + 1) % 2,
                "assumed_round_trip_cost": _COST,
                **_feature_values(feature_set, index=index),
            }
        )
    return pd.DataFrame(rows)


def _feature_values(
    feature_set: ModelFeatureSet,
    *,
    index: int,
) -> dict[str, object]:
    values: dict[str, object] = {}
    for offset, feature in enumerate(feature_set.features, start=1):
        if feature.dtype in {"category", "string"}:
            value: object = "even" if index % 2 == 0 else "odd"
        elif feature.dtype == "bool":
            value = index % 2 == 0
        else:
            value = float(index + 1) + float(offset) / 100.0
        values[feature.name] = value
    return values


def _iso_z(value: pd.Timestamp) -> str:
    return value.isoformat().replace("+00:00", "Z")
