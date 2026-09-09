from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from datafetching.fred_alfred_readiness import read_verified_macro_evidence
from datafetching.ids import add_readable_id
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from ml.artifacts import file_checksum, input_inventory, utc_timestamp
from ml.calibration import IdentityCalibrator, fit_probability_calibrator
from ml.contracts import FeatureSet
from ml.current_publication import read_current_publication, resolve_current_output
from ml.feature_registry import DEFAULT_FEATURE_REGISTRY
from ml.horizons import horizon_specifications_for_profile
from ml.model_features import model_matrix_for_feature_set
from ml.model_runtime import TARGET_COLUMN
from ml.models.registry import ModelSpec, build_estimator


MACRO_ABLATION_VERSION = "macro-rolling-origin-ablation-v1"
MACRO_ABLATION_RECEIPT_VERSION = "macro-rolling-origin-ablation-receipt-v1"
MACRO_PRODUCTION_PROFILE = "loop-a-all-bsgp-active-v3"
MACRO_ABLATION_HORIZONS = ("1d", "1w")


@dataclass(frozen=True)
class MacroAblationResult:
    directory: Path
    report_path: Path
    predictions_path: Path
    receipt_path: Path
    report: Mapping[str, object]


def run_macro_ablation(
    datastore_root: Path,
    *,
    created_at: object | None = None,
    minimum_train_clusters: int = 252,
    calibration_clusters: int = 63,
    test_clusters: int = 63,
) -> MacroAblationResult:
    """Run expanding-window baseline-versus-macro diagnostics after activation."""

    for label, value in (
        ("minimum_train_clusters", minimum_train_clusters),
        ("calibration_clusters", calibration_clusters),
        ("test_clusters", test_clusters),
    ):
        if int(value) < 1:
            raise ValueError(f"{label} must be positive")
    root = Path(datastore_root).resolve()
    publication = read_current_publication(root)
    configuration = publication.manifest.get("configuration")
    if not isinstance(configuration, Mapping) or configuration.get(
        "feature_profile"
    ) != MACRO_PRODUCTION_PROFILE:
        raise ValueError(
            "Macro ablation requires an activated production v3 Loop B publication"
        )
    samples_path = resolve_current_output(root, "samples.parquet")
    samples = pd.read_parquet(samples_path)
    readiness = read_verified_macro_evidence(root).readiness
    specifications = horizon_specifications_for_profile(
        MACRO_PRODUCTION_PROFILE,
        horizons=MACRO_ABLATION_HORIZONS,
    )
    prediction_frames: list[pd.DataFrame] = []
    horizon_reports: dict[str, object] = {}
    for horizon in MACRO_ABLATION_HORIZONS:
        full_set = DEFAULT_FEATURE_REGISTRY.feature_set(
            specifications[horizon].feature_set,
            require_active=True,
            horizon=horizon,
        )
        baseline_set = FeatureSet(
            name=f"{full_set.name}-without-macro-diagnostic",
            features=tuple(
                feature
                for feature in full_set.features
                if feature.source_family != "macro"
            ),
            version=f"{full_set.version}-diagnostic-baseline",
            applicable_horizons=(horizon,),
        )
        horizon_samples = samples.loc[
            samples["horizon"].astype(str).eq(horizon)
        ].copy()
        missing = sorted(
            {
                "id",
                "symbol",
                "decision_timestamp",
                "target_window_start",
                "target_window_end",
                "label_status",
                TARGET_COLUMN,
                *full_set.names,
            }.difference(horizon_samples.columns)
        )
        if missing:
            raise ValueError(
                f"Ablation samples for {horizon} are missing: "
                + ", ".join(missing)
            )
        folds = _rolling_origin_folds(
            horizon_samples,
            minimum_train_clusters=int(minimum_train_clusters),
            calibration_clusters=int(calibration_clusters),
            test_clusters=int(test_clusters),
        )
        if not folds:
            raise ValueError(f"No causal rolling-origin folds are available for {horizon}")
        horizon_predictions: list[pd.DataFrame] = []
        for fold_index, (train, calibration, test) in enumerate(folds, start=1):
            for variant, feature_set in (
                ("baseline_without_macro", baseline_set),
                ("production_with_macro", full_set),
            ):
                predicted = _fit_predict_fold(
                    train,
                    calibration,
                    test,
                    horizon=horizon,
                    fold_index=fold_index,
                    variant=variant,
                    feature_set=feature_set,
                )
                horizon_predictions.append(predicted)
                prediction_frames.append(predicted)
        combined = pd.concat(horizon_predictions, ignore_index=True, sort=False)
        by_variant = {
            variant: _metrics(group)
            for variant, group in combined.groupby("variant", sort=True)
        }
        baseline_metrics = by_variant["baseline_without_macro"]
        macro_metrics = by_variant["production_with_macro"]
        horizon_reports[horizon] = {
            "fold_count": len(folds),
            "first_test_decision": pd.to_datetime(
                combined["decision_timestamp"], utc=True
            ).min().isoformat(),
            "last_test_decision": pd.to_datetime(
                combined["decision_timestamp"], utc=True
            ).max().isoformat(),
            "variants": by_variant,
            "macro_minus_baseline": {
                metric: float(macro_metrics[metric] - baseline_metrics[metric])
                for metric in (
                    "log_loss",
                    "brier_score",
                    "calibration_gap",
                    "accuracy_at_0_5",
                    "action_rate_at_0_5",
                    "action_precision_at_0_5",
                    "action_recall_at_0_5",
                )
            },
        }

    predictions = add_readable_id(
        pd.concat(prediction_frames, ignore_index=True, sort=False),
        key_columns=(
            "horizon",
            "fold",
            "variant",
            "symbol",
            "decision_timestamp",
        ),
    )
    created = utc_timestamp(created_at)
    report = {
        "schema_version": MACRO_ABLATION_VERSION,
        "created_at": created.isoformat(),
        "status": "COMPLETE_DIAGNOSTIC_ONLY",
        "activation_gate": False,
        "activation_was_delayed": False,
        "correctness_failures_block_activation": True,
        "feature_profile": MACRO_PRODUCTION_PROFILE,
        "readiness_run_path": readiness.directory.relative_to(root).as_posix(),
        "readiness_receipt_checksum_sha256": readiness.receipt_checksum_sha256,
        "readiness_lookahead_violation_count": int(
            readiness.coverage["lookahead_violation_count"]
        ),
        "current_revised_history_used": False,
        "fold_policy": {
            "kind": "expanding-training-fixed-calibration-forward-test",
            "minimum_train_clusters": int(minimum_train_clusters),
            "calibration_clusters": int(calibration_clusters),
            "test_clusters": int(test_clusters),
            "target_window_purging": True,
            "assessment_used_for_training_or_calibration": False,
        },
        "source_files": input_inventory(
            (samples_path, readiness.report_path, readiness.receipt_path),
            relative_to=root,
        ),
        "horizons": horizon_reports,
    }
    return _publish(root, created=created, report=report, predictions=predictions)


def _rolling_origin_folds(
    samples: pd.DataFrame,
    *,
    minimum_train_clusters: int,
    calibration_clusters: int,
    test_clusters: int,
) -> list[tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]]:
    eligible = samples.loc[samples["label_status"].eq("COMPLETE")].copy()
    for column in ("decision_timestamp", "target_window_start", "target_window_end"):
        eligible[column] = pd.to_datetime(eligible[column], utc=True, errors="coerce")
    eligible[TARGET_COLUMN] = pd.to_numeric(
        eligible[TARGET_COLUMN], errors="coerce"
    )
    eligible = eligible.dropna(
        subset=(
            "decision_timestamp",
            "target_window_start",
            "target_window_end",
            TARGET_COLUMN,
        )
    )
    clusters = pd.Index(
        eligible["target_window_start"].drop_duplicates().sort_values()
    )
    first_test = minimum_train_clusters + calibration_clusters
    folds: list[tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]] = []
    for offset in range(first_test, len(clusters), test_clusters):
        test_values = clusters[offset : offset + test_clusters]
        if len(test_values) == 0:
            continue
        test_start = pd.Timestamp(test_values[0])
        prior = eligible.loc[eligible["target_window_end"].lt(test_start)].copy()
        prior_clusters = pd.Index(
            prior["target_window_start"].drop_duplicates().sort_values()
        )
        if len(prior_clusters) < minimum_train_clusters + calibration_clusters:
            continue
        calibration_values = prior_clusters[-calibration_clusters:]
        calibration_start = pd.Timestamp(calibration_values[0])
        train = prior.loc[
            prior["target_window_start"].lt(calibration_start)
            & prior["target_window_end"].lt(calibration_start)
        ].copy()
        calibration = prior.loc[
            prior["target_window_start"].isin(calibration_values)
        ].copy()
        test = eligible.loc[eligible["target_window_start"].isin(test_values)].copy()
        if (
            train["target_window_start"].nunique() < minimum_train_clusters
            or calibration["target_window_start"].nunique() < calibration_clusters
            or train[TARGET_COLUMN].nunique() != 2
            or calibration[TARGET_COLUMN].nunique() != 2
            or test.empty
        ):
            continue
        order = ["target_window_start", "symbol", "id"]
        folds.append(
            tuple(
                frame.sort_values(order, kind="stable").reset_index(drop=True)
                for frame in (train, calibration, test)
            )
        )
    return folds


def _fit_predict_fold(
    train: pd.DataFrame,
    calibration: pd.DataFrame,
    test: pd.DataFrame,
    *,
    horizon: str,
    fold_index: int,
    variant: str,
    feature_set: FeatureSet,
) -> pd.DataFrame:
    parameters: Mapping[str, object] = (
        {
            "C": 0.3,
            "l1_ratio": 1.0,
            "solver": "liblinear",
            "max_iter": 5_000,
            "tol": 1e-5,
        }
        if horizon == "1w"
        else {}
    )
    spec = ModelSpec(
        model_name=f"macro-ablation-{variant}-{horizon}",
        family="logistic",
        feature_set=feature_set.name,
        calibration_method="platt",
        parameters=parameters,
    )
    estimator = build_estimator(spec, feature_set)
    estimator.fit(
        model_matrix_for_feature_set(train, feature_set),
        train[TARGET_COLUMN].astype(int),
    )
    calibration_raw = estimator.predict_proba(
        model_matrix_for_feature_set(calibration, feature_set)
    )[:, 1]
    if calibration[TARGET_COLUMN].nunique() == 2:
        calibrator = fit_probability_calibrator(
            "platt",
            calibration_raw,
            calibration[TARGET_COLUMN].astype(int),
            platt_regularization_c=0.1 if horizon == "1w" else 1.0,
            clip_to_observed_probability_range=horizon == "1w",
        )
    else:
        calibrator = IdentityCalibrator()
    raw = estimator.predict_proba(
        model_matrix_for_feature_set(test, feature_set)
    )[:, 1]
    probability = calibrator.predict(raw)
    return pd.DataFrame(
        {
            "horizon": horizon,
            "fold": fold_index,
            "variant": variant,
            "symbol": test["symbol"].astype(str).to_numpy(),
            "decision_timestamp": test["decision_timestamp"].to_numpy(),
            "target_window_start": test["target_window_start"].to_numpy(),
            "observed_target": test[TARGET_COLUMN].astype(int).to_numpy(),
            "raw_probability": raw,
            "calibrated_probability": probability,
        }
    )


def _metrics(frame: pd.DataFrame) -> dict[str, object]:
    target = frame["observed_target"].astype(int).to_numpy()
    probability = np.clip(
        frame["calibrated_probability"].astype(float).to_numpy(),
        1e-12,
        1.0 - 1e-12,
    )
    action = probability >= 0.5
    positive = target == 1
    true_positive = int((action & positive).sum())
    return {
        "row_count": int(len(frame)),
        "fold_count": int(frame["fold"].nunique()),
        "log_loss": float(
            -np.mean(
                target * np.log(probability)
                + (1 - target) * np.log(1.0 - probability)
            )
        ),
        "brier_score": float(np.mean((probability - target) ** 2)),
        "calibration_gap": float(abs(probability.mean() - target.mean())),
        "accuracy_at_0_5": float(np.mean(action.astype(int) == target)),
        "action_rate_at_0_5": float(action.mean()),
        "action_precision_at_0_5": (
            float(true_positive / int(action.sum())) if action.any() else 0.0
        ),
        "action_recall_at_0_5": (
            float(true_positive / int(positive.sum())) if positive.any() else 0.0
        ),
        "observed_positive_rate": float(target.mean()),
        "mean_calibrated_probability": float(probability.mean()),
    }


def _publish(
    root: Path,
    *,
    created: pd.Timestamp,
    report: Mapping[str, object],
    predictions: pd.DataFrame,
) -> MacroAblationResult:
    parent = root / "ml" / "macro-ablations"
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / created.strftime("%Y%m%dT%H%M%S.%fZ")
    suffix = 2
    while destination.exists():
        destination = parent / f"{created.strftime('%Y%m%dT%H%M%S.%fZ')}-{suffix}"
        suffix += 1
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.tmp-{os.getpid()}-",
            dir=parent,
        )
    )
    try:
        report_path = staging / "report.json"
        predictions_path = staging / "predictions.parquet"
        _write_json(report_path, report)
        predictions.to_parquet(predictions_path, index=False)
        receipt = {
            "schema_version": MACRO_ABLATION_RECEIPT_VERSION,
            "status": "COMPLETE_DIAGNOSTIC_ONLY",
            "created_at": created.isoformat(),
            "run_path": destination.relative_to(root).as_posix(),
            "report_checksum_sha256": file_checksum(report_path),
            "predictions_checksum_sha256": file_checksum(predictions_path),
            "activation_gate": False,
            "current_revised_history_used": False,
        }
        _write_json(staging / "receipt.json", receipt)
        staging.replace(destination)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    report_path = destination / "report.json"
    predictions_path = destination / "predictions.parquet"
    receipt_path = destination / "receipt.json"
    _write_json_atomic(
        root / "ml" / "macro-ablations-latest" / "run.json",
        {
            "schema_version": MACRO_ABLATION_RECEIPT_VERSION,
            "run_path": destination.relative_to(root).as_posix(),
            "created_at": created.isoformat(),
            "receipt_checksum_sha256": file_checksum(receipt_path),
        },
    )
    return MacroAblationResult(
        directory=destination,
        report_path=report_path,
        predictions_path=predictions_path,
        receipt_path=receipt_path,
        report=report,
    )


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    try:
        _write_json(temporary, payload)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run post-activation rolling-origin baseline-versus-macro diagnostics."
        )
    )
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path, default=None)
    datastore.add_argument(
        "--datastore-target",
        choices=tuple(DATASTORE_TARGETS),
        default="pc",
    )
    args = parser.parse_args(argv)
    root = resolve_datastore_dir(
        root_dir=args.datastore,
        target=None if args.datastore is not None else args.datastore_target,
    )
    result = run_macro_ablation(root)
    print(json.dumps(dict(result.report), indent=2, sort_keys=True, default=str))
    print(f"Run: {result.directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MACRO_ABLATION_HORIZONS",
    "MACRO_ABLATION_VERSION",
    "MacroAblationResult",
    "run_macro_ablation",
]
