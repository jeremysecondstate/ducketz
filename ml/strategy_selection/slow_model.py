from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import joblib

from ml.artifacts import file_checksum, utc_timestamp
from ml.strategy_selection.contracts import (
    STRATEGY_MODEL_POLICY_VERSION,
    StrategyModel,
)


SLOW_STRATEGY_MODEL_VERSION = "multi-horizon-strategy-profit-slow-model-v2"
SLOW_STRATEGY_RECEIPT_VERSION = (
    "multi-horizon-strategy-profit-slow-model-receipt-v2"
)
SLOW_STRATEGY_POINTER_VERSION = (
    "multi-horizon-strategy-profit-slow-model-pointer-v2"
)
CANONICAL_PROFIT_HORIZONS = ("1h", "4h", "1d", "1w")
HORIZON_MODEL_ALIASES = {
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
    "1w": "1w",
    "1w-d1": "1d",
    "1w-d2": "1d",
    "1w-d3": "1d",
    "1w-d4": "1d",
    "1w-d5": "1d",
}


@dataclass(frozen=True)
class PromotedStrategyModel:
    requested_horizon: str
    canonical_horizon: str
    model: StrategyModel
    report: Mapping[str, object]
    authority_files: tuple[Path, ...]


def canonical_profit_horizon(horizon: str) -> str | None:
    return HORIZON_MODEL_ALIASES.get(str(horizon).strip().lower())


def strategy_model_promotion_gate(
    evaluation: Mapping[str, object],
    *,
    minimum_assessment_decisions: int = 63,
) -> dict[str, object]:
    """Apply a predeclared assessment-only quality gate.

    The constant training-base-rate comparator is intentionally difficult to
    beat by accident.  A promoted model may tie within floating-point residue,
    but it may not be worse on either proper scoring rule.  Calibration error
    is also bounded independently.
    """

    try:
        calibrated = _mapping(evaluation["calibrated_model"])
        baseline = _mapping(evaluation["base_rate_model"])
        brier = _finite(calibrated["brier_score"])
        baseline_brier = _finite(baseline["brier_score"])
        log_loss = _finite(calibrated["log_loss"])
        baseline_log_loss = _finite(baseline["log_loss"])
        calibration_error = _finite(
            calibrated["expected_calibration_error_10_bin"]
        )
        assessment_decisions = int(evaluation["assessment_decisions"])
    except (KeyError, TypeError, ValueError) as exc:
        return {
            "status": "REJECTED",
            "reason": f"INCOMPLETE_ASSESSMENT_METRICS:{type(exc).__name__}",
        }
    required_assessment = int(minimum_assessment_decisions)
    if required_assessment <= 0:
        raise ValueError("minimum_assessment_decisions must be positive")
    checks = {
        "assessment_decisions_meets_minimum": (
            assessment_decisions >= required_assessment
        ),
        "brier_not_worse_than_training_base_rate": (
            brier <= baseline_brier + 1e-12
        ),
        "log_loss_not_worse_than_training_base_rate": (
            log_loss <= baseline_log_loss + 1e-12
        ),
        "expected_calibration_error_at_most_0_12": calibration_error <= 0.12,
    }
    return {
        "status": "PROMOTED" if all(checks.values()) else "REJECTED",
        "checks": checks,
        "calibrated_brier_score": brier,
        "base_rate_brier_score": baseline_brier,
        "calibrated_log_loss": log_loss,
        "base_rate_log_loss": baseline_log_loss,
        "expected_calibration_error_10_bin": calibration_error,
        "assessment_decisions": assessment_decisions,
        "minimum_assessment_decisions": required_assessment,
        "assessment_used_for_promotion_only": True,
        "assessment_used_for_training": False,
        "assessment_used_for_calibration": False,
    }


def publish_slow_strategy_authority(
    datastore_root: Path,
    *,
    run_directory: Path,
    models: Mapping[str, StrategyModel],
    reports: Mapping[str, Mapping[str, object]],
    published_at: object,
    output_files: Sequence[Path],
) -> tuple[Path, Path, Path]:
    root = Path(datastore_root).resolve()
    run = Path(run_directory).resolve()
    if root not in run.parents:
        raise ValueError("Slow Strategy run escapes the datastore root")
    model_horizons = set(models)
    canonical_horizons = set(CANONICAL_PROFIT_HORIZONS)
    if not model_horizons or not model_horizons.issubset(canonical_horizons):
        raise ValueError(
            "Slow Strategy authority requires at least one canonical promoted model"
        )
    if not model_horizons.issubset(set(reports)):
        raise ValueError("Every promoted Strategy model requires a report")
    model_inventory: dict[str, object] = {}
    for horizon in CANONICAL_PROFIT_HORIZONS:
        if horizon not in models:
            continue
        model = models[horizon]
        report = reports[horizon]
        gate = _mapping(report.get("promotion_gate"))
        if gate.get("status") != "PROMOTED":
            raise ValueError(f"Slow Strategy {horizon} model did not pass promotion")
        artifact = model.artifact_directory.resolve()
        if root not in artifact.parents:
            raise ValueError("Slow Strategy model artifact escapes the datastore")
        artifact_manifest = artifact / "manifest.json"
        if not artifact_manifest.is_file():
            raise ValueError("Slow Strategy model artifact has no manifest")
        payload = _read_json(artifact_manifest)
        model_info = _mapping(payload.get("model_file"))
        model_path = artifact / str(model_info.get("path", ""))
        _verify_file(model_path, model_info)
        model_inventory[horizon] = {
            "artifact_path": artifact.relative_to(root).as_posix(),
            "artifact_manifest_checksum_sha256": file_checksum(
                artifact_manifest
            ),
            "model_file_checksum_sha256": file_checksum(model_path),
            "model_policy_version": payload.get("model_policy_version"),
            "training_data_fingerprint_sha256": payload.get(
                "training_data_fingerprint_sha256"
            ),
            "promotion_gate": dict(gate),
        }
    outputs = []
    for value in output_files:
        path = Path(value).resolve()
        if run not in path.parents:
            raise ValueError("Slow Strategy output escapes its run directory")
        outputs.append(
            {
                "path": path.relative_to(run).as_posix(),
                "size": path.stat().st_size,
                "checksum_sha256": file_checksum(path),
            }
        )
    created = utc_timestamp(published_at)
    manifest_path = run / "manifest.json"
    manifest = {
        "schema_version": SLOW_STRATEGY_MODEL_VERSION,
        "published_at": created.isoformat(),
        "model_policy_version": STRATEGY_MODEL_POLICY_VERSION,
        "canonical_horizons": list(CANONICAL_PROFIT_HORIZONS),
        "published_horizons": [
            horizon for horizon in CANONICAL_PROFIT_HORIZONS if horizon in models
        ],
        "horizon_aliases": dict(HORIZON_MODEL_ALIASES),
        "models": model_inventory,
        "outputs": outputs,
        "orders_enabled": False,
        "research_only": True,
    }
    _write_json_atomic(manifest_path, manifest)
    receipt_path = run / "receipt.json"
    receipt = {
        "schema_version": SLOW_STRATEGY_RECEIPT_VERSION,
        "published_at": created.isoformat(),
        "run_path": run.relative_to(root).as_posix(),
        "manifest_checksum_sha256": file_checksum(manifest_path),
        "models": {
            horizon: _mapping(model_inventory[horizon]).get(
                "model_file_checksum_sha256"
            )
            for horizon in CANONICAL_PROFIT_HORIZONS
            if horizon in model_inventory
        },
        "orders_placed": 0,
    }
    _write_json_atomic(receipt_path, receipt)
    pointer = root / "ml" / "strategy-profit-training-latest" / "run.json"
    _write_json_atomic(
        pointer,
        {
            "schema_version": SLOW_STRATEGY_POINTER_VERSION,
            "current": {
                "run_path": run.relative_to(root).as_posix(),
                "published_at": created.isoformat(),
                "receipt_checksum_sha256": file_checksum(receipt_path),
            },
        },
    )
    return manifest_path, receipt_path, pointer


def load_promoted_strategy_model(
    datastore_root: Path,
    *,
    horizon: str,
) -> PromotedStrategyModel | None:
    requested = str(horizon).strip().lower()
    canonical = canonical_profit_horizon(requested)
    if canonical is None:
        return None
    root = Path(datastore_root).resolve()
    pointer = root / "ml" / "strategy-profit-training-latest" / "run.json"
    if not pointer.is_file():
        return None
    try:
        pointer_payload = _read_json(pointer)
        if pointer_payload.get("schema_version") != SLOW_STRATEGY_POINTER_VERSION:
            return None
        current = _mapping(pointer_payload["current"])
        run = (root / str(current["run_path"])).resolve()
        if root not in run.parents:
            return None
        manifest_path = run / "manifest.json"
        receipt_path = run / "receipt.json"
        if file_checksum(receipt_path) != current.get(
            "receipt_checksum_sha256"
        ):
            return None
        manifest = _read_json(manifest_path)
        receipt = _read_json(receipt_path)
        if (
            manifest.get("schema_version") != SLOW_STRATEGY_MODEL_VERSION
            or receipt.get("schema_version") != SLOW_STRATEGY_RECEIPT_VERSION
            or receipt.get("manifest_checksum_sha256")
            != file_checksum(manifest_path)
            or manifest.get("model_policy_version")
            != STRATEGY_MODEL_POLICY_VERSION
        ):
            return None
        model_record = _mapping(_mapping(manifest["models"])[canonical])
        gate = _mapping(model_record.get("promotion_gate"))
        if gate.get("status") != "PROMOTED":
            return None
        artifact = (root / str(model_record["artifact_path"])).resolve()
        if root not in artifact.parents:
            return None
        artifact_manifest_path = artifact / "manifest.json"
        if file_checksum(artifact_manifest_path) != model_record.get(
            "artifact_manifest_checksum_sha256"
        ):
            return None
        artifact_manifest = _read_json(artifact_manifest_path)
        if (
            artifact_manifest.get("model_policy_version")
            != STRATEGY_MODEL_POLICY_VERSION
            or artifact_manifest.get("horizon") != canonical
            or artifact_manifest.get("effective_calibration_method") != "platt"
        ):
            return None
        model_info = _mapping(artifact_manifest["model_file"])
        model_path = artifact / str(model_info["path"])
        _verify_file(model_path, model_info)
        if file_checksum(model_path) != model_record.get(
            "model_file_checksum_sha256"
        ):
            return None
        bundle = joblib.load(model_path)
        if not isinstance(bundle, dict):
            return None
        report_path = run / f"{canonical}-model-report.json"
        report = _read_json(report_path)
        model = StrategyModel(
            horizon=canonical,
            estimator=bundle["estimator"],
            return_estimator=bundle["return_estimator"],
            calibrator=bundle["calibrator"],
            numeric_features=tuple(artifact_manifest["numeric_features"]),
            categorical_features=tuple(
                artifact_manifest["categorical_features"]
            ),
            artifact_directory=artifact,
            offline_evaluation=_mapping(
                artifact_manifest.get("offline_evaluation")
            ),
            probability_model_family=str(
                artifact_manifest.get(
                    "selected_probability_model_family", "hist-gradient"
                )
            ),
            reused=True,
        )
        return PromotedStrategyModel(
            requested_horizon=requested,
            canonical_horizon=canonical,
            model=model,
            report=report,
            authority_files=(
                pointer,
                manifest_path,
                receipt_path,
                report_path,
                artifact_manifest_path,
                model_path,
            ),
        )
    except (
        json.JSONDecodeError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
    ):
        return None


def _verify_file(path: Path, metadata: Mapping[str, object]) -> None:
    if (
        not path.is_file()
        or path.stat().st_size != int(metadata.get("size", -1))
        or file_checksum(path) != metadata.get("checksum_sha256")
    ):
        raise ValueError(f"Slow Strategy artifact verification failed: {path}")


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("Expected a mapping")
    return value


def _finite(value: object) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Expected a finite metric")
    return number


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


__all__ = [
    "CANONICAL_PROFIT_HORIZONS",
    "HORIZON_MODEL_ALIASES",
    "PromotedStrategyModel",
    "canonical_profit_horizon",
    "load_promoted_strategy_model",
    "publish_slow_strategy_authority",
    "strategy_model_promotion_gate",
]
