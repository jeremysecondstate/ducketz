from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np

from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.artifacts import create_timestamp_directory, file_checksum, utc_timestamp, verify_manifest, write_manifest
from ml.stock_trader.contracts import canonical_sha256, finite
from ml.stock_trader.model import (
    ENRICHMENT_FEATURE_NAMES,
    ENRICHMENT_MODEL_POINTER_VERSION,
    ENRICHMENT_MODEL_SCHEMA_VERSION,
    model_from_payload,
)


ENRICHMENT_TRAINING_RECEIPT_VERSION = "stock-trader-enrichment-training-receipt-v1"


def fit_enrichment_model_payload(
    decision_outcome_pairs: Sequence[Mapping[str, object]],
    *,
    trained_at: object,
    minimum_rows: int = 40,
    ridge_penalty: float = 5.0,
    model_name: str = "stock-trader-multihead-ridge",
    model_version: str | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    """Fit all sizing/execution heads from paired hourly counterfactual outcomes."""

    rows = [_training_row(pair) for pair in decision_outcome_pairs]
    usable = [row for row in rows if row is not None]
    if len(usable) < minimum_rows:
        raise ValueError(
            f"Stock trader enrichment training needs {minimum_rows} mature rows; "
            f"only {len(usable)} were usable."
        )
    if ridge_penalty < 0.0:
        raise ValueError("ridge_penalty cannot be negative")
    timestamp = utc_timestamp(trained_at)
    x = np.asarray([row["features"] for row in usable], dtype=float)
    means = x.mean(axis=0)
    scales = x.std(axis=0)
    scales = np.where(scales > 1e-9, scales, 1.0)
    z = (x - means) / scales
    aligned_net = np.asarray([row["aligned_net"] for row in usable], dtype=float)
    aligned_raw = np.asarray([row["aligned_raw"] for row in usable], dtype=float)
    profitable = (aligned_net > 0.0).astype(float)
    magnitude_scale = max(float(np.quantile(np.abs(aligned_net), 0.90)), 0.005)
    allocation = np.clip(np.maximum(aligned_net, 0.0) / magnitude_scale, 0.01, 0.99)
    urgency = np.clip(np.abs(aligned_net) / magnitude_scale, 0.01, 0.99)
    adverse = np.maximum(0.0, -aligned_raw)
    spreads = x[:, ENRICHMENT_FEATURE_NAMES.index("relative_spread")]
    limit_offset_bps = np.maximum(0.01, spreads * 10_000.0 * urgency * 0.5)
    protective_distance = np.clip(0.005 + 1.25 * adverse, 0.0025, 0.15)
    holding_minutes = np.full(len(usable), 60.0)
    target_by_head = {
        "trade_probability": _logit(np.clip(0.05 + 0.90 * profitable, 0.01, 0.99)),
        "allocation_fraction": _logit(allocation),
        "expected_net_return": aligned_net,
        "adverse_return": _inverse_softplus(np.maximum(adverse, 1e-8)),
        "execution_urgency": _logit(urgency),
        "limit_offset_bps": _inverse_softplus(limit_offset_bps),
        "protective_distance_pct": _inverse_softplus(protective_distance),
        "expected_holding_minutes": _inverse_softplus(holding_minutes),
    }
    links = {
        "trade_probability": "sigmoid",
        "allocation_fraction": "sigmoid",
        "expected_net_return": "identity",
        "adverse_return": "softplus",
        "execution_urgency": "sigmoid",
        "limit_offset_bps": "softplus",
        "protective_distance_pct": "softplus",
        "expected_holding_minutes": "softplus",
    }
    heads: dict[str, object] = {}
    for name, target in target_by_head.items():
        intercept, coefficients = _ridge(z, target, penalty=ridge_penalty)
        heads[name] = {
            "link": links[name],
            "intercept": intercept,
            "coefficients": coefficients,
        }
    version = model_version or timestamp.strftime("%Y%m%dT%H%M%S.%fZ")
    payload: dict[str, object] = {
        "schema_version": ENRICHMENT_MODEL_SCHEMA_VERSION,
        "model_name": model_name,
        "model_version": version,
        "trained_at": timestamp.isoformat(),
        "feature_names": list(ENRICHMENT_FEATURE_NAMES),
        "feature_means": means.tolist(),
        "feature_scales": scales.tolist(),
        "heads": heads,
        "training": {
            "row_count": len(usable),
            "minimum_rows": minimum_rows,
            "ridge_penalty": ridge_penalty,
            "outcome_definition": "direction_aligned_forward_raw_return_minus_round_trip_cost",
            "sizing_target": "positive_outcome_magnitude_scaled_to_unit_allocation",
            "selection_scope": "eligible_and_abstained_decisions_with_mature_counterfactuals",
        },
    }
    payload["model_fingerprint"] = canonical_sha256(payload)
    model_from_payload(payload)
    report = {
        "status": "MODEL_FIT",
        "trained_at": timestamp.isoformat(),
        "row_count": len(usable),
        "excluded_row_count": len(rows) - len(usable),
        "positive_outcome_rate": float(profitable.mean()),
        "mean_direction_aligned_net_return": float(aligned_net.mean()),
        "median_direction_aligned_net_return": float(np.median(aligned_net)),
        "magnitude_scale": magnitude_scale,
        "model_fingerprint": payload["model_fingerprint"],
    }
    return payload, report


def train_and_publish_enrichment_model(
    datastore_root: Path,
    *,
    trained_at: object | None = None,
    minimum_rows: int = 40,
    ridge_penalty: float = 5.0,
) -> Path:
    root = Path(datastore_root).resolve()
    pairs, source_files = load_verified_audit_pairs(root)
    timestamp = utc_timestamp(trained_at)
    payload, report = fit_enrichment_model_payload(
        pairs,
        trained_at=timestamp,
        minimum_rows=minimum_rows,
        ridge_penalty=ridge_penalty,
    )
    run = create_timestamp_directory(
        root / "ml" / "stock-trader-model-runs", timestamp=timestamp
    )
    model_path = run / "model.json"
    report_path = run / "training-report.json"
    _write_json_atomic(model_path, payload)
    _write_json_atomic(report_path, report)
    manifest_path = write_manifest(
        run,
        run_timestamp=timestamp,
        input_files=source_files,
        output_files=(model_path.name, report_path.name),
        model_name=str(payload["model_name"]),
        feature_columns=ENRICHMENT_FEATURE_NAMES,
        target_column="direction_aligned_net_return",
        configuration={
            "model_version": payload["model_version"],
            "model_fingerprint": payload["model_fingerprint"],
            "minimum_rows": minimum_rows,
            "ridge_penalty": ridge_penalty,
            "automatic_activation_allowed": False,
        },
        datastore_root=root,
    )
    receipt_path = run / "receipt.json"
    receipt = {
        "schema_version": ENRICHMENT_TRAINING_RECEIPT_VERSION,
        "run_path": run.relative_to(root).as_posix(),
        "trained_at": timestamp.isoformat(),
        "model_fingerprint": payload["model_fingerprint"],
        "manifest_sha256": file_checksum(manifest_path),
        "model_sha256": file_checksum(model_path),
        "training_report_sha256": file_checksum(report_path),
    }
    _write_json_atomic(receipt_path, receipt)
    pointer_path = root / "ml" / "stock-trader-model-latest" / "run.json"
    _write_json_atomic(
        pointer_path,
        {
            "schema_version": ENRICHMENT_MODEL_POINTER_VERSION,
            "run_path": receipt["run_path"],
            "trained_at": timestamp.isoformat(),
            "model_fingerprint": payload["model_fingerprint"],
            "manifest_sha256": receipt["manifest_sha256"],
            "model_sha256": receipt["model_sha256"],
            "receipt_sha256": file_checksum(receipt_path),
        },
    )
    return run


def load_verified_audit_pairs(
    datastore_root: Path,
) -> tuple[list[dict[str, object]], tuple[Path, ...]]:
    root = Path(datastore_root).resolve()
    audits_root = root / "ml" / "stock-trader-weekly-audits"
    if not audits_root.is_dir():
        return [], ()
    pairs_by_id: dict[str, dict[str, object]] = {}
    sources: list[Path] = []
    for run in sorted(path for path in audits_root.iterdir() if path.is_dir()):
        verify_manifest(run)
        audit_path = run / "audit.json"
        receipt_path = run / "receipt.json"
        receipt = _read_object(receipt_path, "stock trader audit receipt")
        if receipt.get("audit_sha256") != file_checksum(audit_path):
            raise ValueError(f"Stock trader audit receipt differs: {run}")
        audit = _read_object(audit_path, "stock trader audit")
        raw_pairs = audit.get("decision_outcome_pairs")
        if not isinstance(raw_pairs, list):
            raise ValueError(f"Stock trader audit has no decision_outcome_pairs: {run}")
        for pair in raw_pairs:
            if isinstance(pair, Mapping) and pair.get("decision_id"):
                pairs_by_id[str(pair["decision_id"])] = dict(pair)
        sources.extend((audit_path, run / "manifest.json", receipt_path))
    return list(pairs_by_id.values()), tuple(dict.fromkeys(sources))


def _training_row(pair: Mapping[str, object]) -> dict[str, object] | None:
    reality = pair.get("market_reality")
    model = pair.get("model")
    if not isinstance(reality, Mapping) or reality.get("status") != "EVALUATED":
        return None
    if not isinstance(model, Mapping) or not isinstance(model.get("feature_values"), Mapping):
        return None
    feature_values = model["feature_values"]
    features: list[float] = []
    for name in ENRICHMENT_FEATURE_NAMES:
        value = finite(feature_values.get(name))
        if value is None:
            return None
        features.append(value)
    aligned_net = finite(reality.get("direction_aligned_net_return"))
    aligned_raw = finite(reality.get("direction_aligned_raw_return"))
    if aligned_net is None or aligned_raw is None:
        return None
    return {"features": features, "aligned_net": aligned_net, "aligned_raw": aligned_raw}


def _ridge(
    features: np.ndarray, target: np.ndarray, *, penalty: float
) -> tuple[float, list[float]]:
    design = np.column_stack((np.ones(len(features)), features))
    regularization = np.eye(design.shape[1]) * penalty
    regularization[0, 0] = 0.0
    coefficients = np.linalg.solve(
        design.T @ design + regularization,
        design.T @ target,
    )
    return float(coefficients[0]), coefficients[1:].astype(float).tolist()


def _logit(values: np.ndarray) -> np.ndarray:
    clipped = np.clip(values, 1e-6, 1.0 - 1e-6)
    return np.log(clipped / (1.0 - clipped))


def _inverse_softplus(values: np.ndarray) -> np.ndarray:
    clipped = np.maximum(values, 1e-12)
    output = clipped.copy()
    ordinary = clipped <= 30.0
    output[ordinary] = np.log(np.expm1(clipped[ordinary]))
    return output


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


def _read_object(path: Path, label: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to read {label}: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} is not an object: {path}")
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fit and publish the stock-trader multi-head enrichment model."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--root-dir", type=Path)
    group.add_argument("--datastore-target", choices=sorted(DATASTORE_TARGETS))
    parser.add_argument("--minimum-rows", type=int, default=40)
    parser.add_argument("--ridge-penalty", type=float, default=5.0)
    parser.add_argument("--trained-at")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = resolve_datastore_dir(
            root_dir=args.root_dir, target=args.datastore_target
        )
        with exclusive_runtime_lock(
            root / "locks" / "stock-trader-training.lock",
            process_name="stock-trader-training",
        ):
            run = train_and_publish_enrichment_model(
                root,
                trained_at=args.trained_at,
                minimum_rows=args.minimum_rows,
                ridge_penalty=args.ridge_penalty,
            )
    except Exception as exc:
        print(json.dumps({"status": "FAILED", "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps({"status": "MODEL_PUBLISHED", "run_directory": str(run)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "fit_enrichment_model_payload",
    "load_verified_audit_pairs",
    "main",
    "train_and_publish_enrichment_model",
]
