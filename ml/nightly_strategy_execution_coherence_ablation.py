from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from ml.artifacts import file_checksum, utc_timestamp


ABLATION_SCHEMA_VERSION = "loops-nightly-strategy-execution-coherence-ablation-v1"
RECEIPT_SCHEMA_VERSION = (
    "loops-nightly-strategy-execution-coherence-proof-receipt-v1"
)
MANIFEST_SCHEMA_VERSION = (
    "loops-nightly-strategy-execution-coherence-proof-manifest-v1"
)
PREREGISTRATION_ID = "20260828-strategy-execution-coherence-v1"
PREREGISTRATION_SHA256 = (
    "449f8b7bc1e2b69e61d77f5c5796305492790186db3f8c7b444b2c789fb4fdc1"
)
ELIGIBLE_SESSION = "2026-08-28"
SOURCE_GENERATION = "20260828T220000.131790Z"
CAUSAL_INPUT_CUTOFF = pd.Timestamp("2026-08-28T22:00:00.131790Z")
EXPECTED_SOURCE_SET_SHA256 = (
    "35d0dda0f90a7eb6d7263c2dbf5121a653391e7db6fef124962d11c3f1deff44"
)
EXPECTED_GATE_SOURCE_SET_SHA256 = (
    "e3f0972a3a19868ddba24c647f63bae99911b9f898b5022ca9f81615fbedccab"
)
EXPECTED_RAW_COHORT_SHA256 = (
    "77ef0a1d8d1f73a5a726c7ff9dadeabd6919514f5d4a1c6de8f9dafa677064c7"
)
EXPECTED_EXACT_COHORT_SHA256 = (
    "7a8c19261eb6d52962c901ba839e1a7fc2926683e3dfc0896b67d0a2e53be78b"
)
EXPECTED_HANDOFF_SHA256 = (
    "a9864bc99164013dd201b320400296b9e90e1c5f0aa530ab3ba946ad57082d6d"
)
EXPECTED_HANDOFF_RELATIVE_PATH = Path(
    "logs/ducketz/system-guardian/scheduler-handoff/runs/"
    "00000085-20260829T085713642605Z.json"
)
HARNESS_RELATIVE_PATH = "ml/nightly_strategy_execution_coherence_ablation.py"
FOCUSED_TEST_RELATIVE_PATH = (
    "tests/test_nightly_strategy_execution_coherence_ablation.py"
)

SOURCE_FILES = (
    "receipt.json",
    "manifest.json",
    "training-report.json",
    "execution-haircut-report.json",
    "1d-modeled-outcomes.parquet",
    "1d-model-report.json",
    "1w-modeled-outcomes.parquet",
    "1w-model-report.json",
)
GATE_SOURCE_FILES = (
    "ml/strategy_profit_training.py",
    "ml/strategy_profit_training_runtime.py",
    "ml/strategy_selection/candidates.py",
    "ml/strategy_selection/contracts.py",
    "ml/strategy_selection/model.py",
    "tests/test_strategy_profit_training.py",
    "tests/test_ml_strategy_selection.py",
)
HORIZONS = ("1d", "1w")
EXPECTED_RAW_ROWS = {"1d": 6477, "1w": 4520}
EXPECTED_EXACT_ROWS = {"1d": 5796, "1w": 3996}
EXPECTED_RAW_POSITIVES = {"1d": 63, "1w": 188}
EXPECTED_EXACT_POSITIVES = {"1d": 46, "1w": 127}
EXPECTED_EXACT_PAYOFF_ROWS = {"1d": 5048, "1w": 3437}
EXPECTED_LOWER_BREACHES = {"1d": 3200, "1w": 2182}
EXPECTED_UPPER_BREACHES = {"1d": 0, "1w": 0}
EXPECTED_MAX_EXIT_AVAILABLE_AT = {
    "1d": pd.Timestamp("2026-02-17T21:00:00Z"),
    "1w": pd.Timestamp("2026-02-20T21:00:00Z"),
}
EXPECTED_DECISIONS = 63
EXPECTED_SYMBOLS = ("AAPL", "AMZN", "GOOG", "MU", "NVDA", "SNDK")
EXPECTED_NUMERIC_FEATURES = 189
EXPECTED_CATEGORICAL_FEATURES = 7
CANONICAL_FEE_PER_LEG = 0.65
FEE_STRESSES = (0.0, 0.65, 1.30)

_IDENTITY_COLUMNS = (
    "horizon",
    "target_window_start",
    "decision_timestamp",
    "symbol",
)
_ALIAS_EQUALITY_COLUMNS = (
    "risk_calculation_status",
    "outcome_status",
    "outcome_reason",
    "exit_available_at",
    "exit_cash_flow",
    "net_profit",
    "return_on_risk",
    "profitable",
    "max_profit",
    "max_loss",
    "entry_cash_flow",
    "entry_fees",
    "capital_required",
    "leg_count",
    "raw_profit_probability",
    "calibrated_profit_probability",
    "outcome_policy_version",
    "execution_evidence_type",
    "execution_quality_pass",
)
_NON_PROBABILITY_ALIAS_EQUALITY_COLUMNS = tuple(
    column
    for column in _ALIAS_EQUALITY_COLUMNS
    if column not in {"raw_profit_probability", "calibrated_profit_probability"}
)
_ALIAS_PROOF_COLUMNS = (
    "horizon",
    "target_window_start",
    "decision_timestamp",
    "symbol",
    "exact_construction_identity",
    "alias_count",
    "alias_candidate_keys_sha256",
    "non_probability_fields_equal",
    "raw_profit_probability_equal",
    "calibrated_profit_probability_equal",
    "maximum_raw_probability_delta",
    "maximum_calibrated_probability_delta",
)
_PROOF_COLUMNS = (
    "horizon",
    "target_window_start",
    "decision_timestamp",
    "symbol",
    "strategy_family",
    "candidate_key",
    "legs_sha256",
    "exact_construction_identity",
    "alias_count",
    "alias_candidate_keys_sha256",
    "risk_calculation_status",
    "leg_count",
    "capital_required",
    "max_loss",
    "max_profit",
    "baseline_net_profit",
    "candidate_net_profit",
    "baseline_return_on_risk",
    "candidate_return_on_risk",
    "baseline_profitable",
    "candidate_profitable",
    "raw_profit_probability",
    "calibrated_profit_probability",
    "exit_fee_allowance",
    "admissible_lower_bound",
    "admissible_upper_bound",
    "unbounded_upper",
    "baseline_lower_breach",
    "baseline_upper_breach",
    "candidate_lower_breach",
    "candidate_upper_breach",
    "projection_applied",
    "label_sign_changed",
)


@dataclass(frozen=True)
class ExecutionCoherenceAblationResult:
    status: str
    decision: str
    directory: Path
    report_path: Path
    proof_path: Path
    manifest_path: Path
    receipt_path: Path
    report: Mapping[str, object]


def run_execution_coherence_ablation(
    datastore_root: Path,
    *,
    repo_root: Path,
    created_at: object | None = None,
) -> ExecutionCoherenceAblationResult:
    """Run the sole preregistered, assessment-only execution-coherence proof."""

    root = Path(datastore_root).resolve()
    repository = Path(repo_root).resolve()
    existing = _find_existing_result(root, repo_root=repository)
    if existing is not None:
        return existing

    created = utc_timestamp(created_at)
    preregistration = _validate_preregistration(root)
    source = _validate_source_authority(root)
    gate_inventory = _validate_inventory(
        repository,
        GATE_SOURCE_FILES,
        expected_sha256=EXPECTED_GATE_SOURCE_SET_SHA256,
        label="checked-in gate source",
    )
    implementation_inventory = _validate_implementation_sources(repository)

    raw_frames: list[pd.DataFrame] = []
    model_evidence: dict[str, object] = {}
    for horizon in HORIZONS:
        frame, evidence = _load_assessment_with_scores(
            root,
            horizon=horizon,
            training_manifest=source["manifest"],
        )
        raw_frames.append(frame)
        model_evidence[horizon] = evidence
    raw = pd.concat(raw_frames, ignore_index=True)
    _validate_raw_cohort_identity(raw)

    exact, alias_evidence, alias_proof = _deduplicate_exact_constructions(raw)
    _validate_exact_cohort_identity(exact)
    if not bool(alias_evidence["all_aliases_equal"]):
        failed_gates = {
            horizon: [
                name
                for name, count in (
                    (
                        "alias_non_probability_equality",
                        alias_evidence["by_horizon"][horizon][
                            "non_probability_mismatch_groups"
                        ],
                    ),
                    (
                        "alias_raw_probability_equality",
                        alias_evidence["by_horizon"][horizon][
                            "raw_probability_mismatch_groups"
                        ],
                    ),
                    (
                        "alias_calibrated_probability_equality",
                        alias_evidence["by_horizon"][horizon][
                            "calibrated_probability_mismatch_groups"
                        ],
                    ),
                )
                if int(count) > 0
            ]
            for horizon in HORIZONS
        }
        report = {
            "schema_version": ABLATION_SCHEMA_VERSION,
            "status": "COMPLETE_SHADOW_ONLY",
            "decision": "BLOCKED",
            "created_at": created.isoformat(),
            "eligible_session": ELIGIBLE_SESSION,
            "scope": "ISOLATED_ASSESSMENT_ONLY_EXECUTION_COHERENCE_PROOF",
            "proof_kind": "TERMINAL_ALIAS_EQUALITY_ROLLBACK",
            "preregistration": preregistration,
            "source": {
                "authority_generation": SOURCE_GENERATION,
                "run_path": (
                    f"ml/strategy-profit-training-runs/{SOURCE_GENERATION}"
                ),
                "causal_input_cutoff": CAUSAL_INPUT_CUTOFF.isoformat(),
                "source_set_sha256": EXPECTED_SOURCE_SET_SHA256,
                "source_files": source["inventory"],
                "authority_pointer": source["authority_pointer"],
                "models": model_evidence,
            },
            "checked_in_gates": {
                "gate_source_set_sha256": EXPECTED_GATE_SOURCE_SET_SHA256,
                "files": gate_inventory,
            },
            "implementation": {
                "files": implementation_inventory,
                "focused_test_command": (
                    ".\\.venv\\Scripts\\python.exe -m pytest -q "
                    + FOCUSED_TEST_RELATIVE_PATH.replace("/", "\\")
                ),
            },
            "cohort": {
                "raw_rows": len(raw),
                "exact_construction_rows": len(exact),
                "raw_cohort_sha256": EXPECTED_RAW_COHORT_SHA256,
                "exact_construction_cohort_sha256": EXPECTED_EXACT_COHORT_SHA256,
                "alias_evidence": alias_evidence,
            },
            "transform": {
                "performed": False,
                "reason": "TERMINAL_PREREGISTERED_ALIAS_EQUALITY_FAILURE",
                "fit_performed": False,
                "calibration_performed": False,
                "threshold_selection_performed": False,
                "ranking_performed": False,
            },
            "failed_gates": failed_gates,
            "terminal_rollback": {
                "triggered": True,
                "condition": "alias_equality",
                "retry_allowed": False,
                "reinterpretation_allowed": False,
                "summary": (
                    "Checksum-valid promoted model inference assigns unequal raw "
                    "or calibrated probabilities to exact-construction aliases."
                ),
            },
            "safety": {
                "chronological_partitions_frozen": True,
                "assessment_only": True,
                "prediction_vintages_frozen": True,
                "model_generation_frozen": True,
                "real_lockbox_opened": False,
                "opra_archive_rescan_performed": False,
                "fit_performed": False,
                "calibration_performed": False,
                "threshold_selection_performed": False,
                "ranking_performed": False,
                "outcome_projection_performed": False,
                "account_or_portfolio_read_performed": False,
                "production_mutation": False,
                "production_candidate_mutation": False,
                "production_model_authority_mutation": False,
                "production_authority_mutation": False,
                "runtime_mutation": False,
                "ui_or_ranking_mutation": False,
                "promotion_performed": False,
                "orders_enabled": False,
                "orders_placed": 0,
            },
            "limitations": [
                "The source outcome Parquets intentionally contain null score columns; probabilities were reproduced by inference from checksum-valid promoted artifacts without fitting or calibration.",
                "The preregistered transform was not applied after the terminal alias-equality rollback condition fired.",
                "No canonical regime category exists, so no regime bins were created.",
            ],
            "next_gate": (
                "Stage 15 may compare only this immutable terminal BLOCKED result; "
                "do not retry, reinterpret, retune, or start another challenger."
            ),
        }
        return _publish_result(
            root,
            repo_root=repository,
            created=created,
            report=report,
            proof=alias_proof.loc[:, list(_ALIAS_PROOF_COLUMNS)].copy(),
            source_inventory=source["inventory"],
            gate_inventory=gate_inventory,
            implementation_inventory=implementation_inventory,
        )
    canonical = _project_exact_outcomes(exact, fee_per_leg=CANONICAL_FEE_PER_LEG)
    horizon_evidence = {
        horizon: _horizon_evidence(
            canonical.loc[canonical["horizon"].eq(horizon)].copy(),
            horizon=horizon,
            model_evidence=model_evidence[horizon],
        )
        for horizon in HORIZONS
    }
    diagnostics = _diagnostic_evidence(exact)
    failed_gates = {
        horizon: sorted(
            name
            for name, passed in evidence["gates"].items()
            if not bool(passed)
        )
        for horizon, evidence in horizon_evidence.items()
    }
    all_pass = all(not failures for failures in failed_gates.values())
    decision = "PROPOSAL_ONLY" if all_pass else "BLOCKED"

    report: dict[str, object] = {
        "schema_version": ABLATION_SCHEMA_VERSION,
        "status": "COMPLETE_SHADOW_ONLY",
        "decision": decision,
        "created_at": created.isoformat(),
        "eligible_session": ELIGIBLE_SESSION,
        "scope": "ISOLATED_ASSESSMENT_ONLY_EXECUTION_COHERENCE_PROOF",
        "proof_kind": "COMPLETED_ADMISSIBILITY_TRANSFORM",
        "preregistration": preregistration,
        "source": {
            "authority_generation": SOURCE_GENERATION,
            "run_path": f"ml/strategy-profit-training-runs/{SOURCE_GENERATION}",
            "causal_input_cutoff": CAUSAL_INPUT_CUTOFF.isoformat(),
            "source_set_sha256": EXPECTED_SOURCE_SET_SHA256,
            "source_files": source["inventory"],
            "authority_pointer": source["authority_pointer"],
            "models": model_evidence,
        },
        "checked_in_gates": {
            "gate_source_set_sha256": EXPECTED_GATE_SOURCE_SET_SHA256,
            "files": gate_inventory,
        },
        "implementation": {
            "files": implementation_inventory,
            "focused_test_command": (
                ".\\.venv\\Scripts\\python.exe -m pytest -q "
                + FOCUSED_TEST_RELATIVE_PATH.replace("/", "\\")
            ),
        },
        "cohort": {
            "raw_rows": len(raw),
            "exact_construction_rows": len(exact),
            "raw_cohort_sha256": EXPECTED_RAW_COHORT_SHA256,
            "exact_construction_cohort_sha256": EXPECTED_EXACT_COHORT_SHA256,
            "alias_evidence": alias_evidence,
        },
        "transform": {
            "method": (
                "lexicographic_candidate_key_alias_collapse_then_"
                "portfolio_level_expiration_payoff_admissibility_projection"
            ),
            "fee_per_leg": CANONICAL_FEE_PER_LEG,
            "path_dependent_rows_changed": int(
                canonical.loc[
                    ~canonical["risk_calculation_status"].eq(
                        "EXPIRATION_PAYOFF_EXACT"
                    ),
                    "projection_applied",
                ].sum()
            ),
            "fit_performed": False,
            "calibration_performed": False,
            "threshold_selection_performed": False,
            "ranking_performed": False,
        },
        "horizons": horizon_evidence,
        "failed_gates": failed_gates,
        "diagnostics": diagnostics,
        "safety": {
            "chronological_partitions_frozen": True,
            "assessment_only": True,
            "prediction_vintages_frozen": True,
            "model_generation_frozen": True,
            "real_lockbox_opened": False,
            "opra_archive_rescan_performed": False,
            "fit_performed": False,
            "calibration_performed": False,
            "threshold_selection_performed": False,
            "ranking_performed": False,
            "account_or_portfolio_read_performed": False,
            "production_mutation": False,
            "production_candidate_mutation": False,
            "production_model_authority_mutation": False,
            "production_authority_mutation": False,
            "runtime_mutation": False,
            "ui_or_ranking_mutation": False,
            "promotion_performed": False,
            "orders_enabled": False,
            "orders_placed": 0,
        },
        "limitations": [
            "The projection proves internal payoff admissibility, not joint market execution.",
            "Early-exit time value, assignment, alias semantics, fee coverage, and train/live mismatch remain unresolved.",
            "The current models were fit on unprojected rows.",
            "No canonical regime category exists, so no regime bins were created.",
        ],
        "next_gate": (
            "Stage 15 may compare only this immutable result without retuning. "
            "A production outcome-contract repair requires separate approval even "
            "when every preregistered gate passes."
        ),
    }
    proof = canonical.loc[:, list(_PROOF_COLUMNS)].copy()
    return _publish_result(
        root,
        repo_root=repository,
        created=created,
        report=report,
        proof=proof,
        source_inventory=source["inventory"],
        gate_inventory=gate_inventory,
        implementation_inventory=implementation_inventory,
    )


def _validate_preregistration(root: Path) -> dict[str, object]:
    handoff_path = root / EXPECTED_HANDOFF_RELATIVE_PATH
    _require_file_checksum(
        handoff_path,
        EXPECTED_HANDOFF_SHA256,
        label="scheduler handoff receipt",
    )
    payload = _read_json(handoff_path)
    if int(payload.get("sequence", -1)) != 85:
        raise RuntimeError("Preregistration handoff sequence is not 85")
    schedule = payload.get("schedule")
    if not isinstance(schedule, Mapping):
        raise RuntimeError("Preregistration handoff schedule is missing")
    if str(schedule.get("eligible_session")) != ELIGIBLE_SESSION:
        raise RuntimeError("Preregistration eligible session changed")
    if str(schedule.get("stage_id")) != "select-nightly-bottleneck":
        raise RuntimeError("Preregistration did not originate at stage 13")
    continuity = payload.get("continuity")
    actions = continuity.get("actions") if isinstance(continuity, Mapping) else None
    if not isinstance(actions, list):
        raise RuntimeError("Preregistration canonical fragments are missing")
    fragments: dict[int, str] = {}
    declared_fingerprint: str | None = None
    for raw_action in actions:
        action = str(raw_action)
        if action.startswith("PREREG_CANONICAL_") and "=" in action:
            marker, value = action.split("=", 1)
            number = int(marker.split("_OF_", 1)[0].rsplit("_", 1)[1])
            fragments[number] = value
        elif action.startswith("PREREG_SHA256="):
            declared_fingerprint = action.split("=", 1)[1]
    if sorted(fragments) != list(range(1, 9)):
        raise RuntimeError("Preregistration must contain exactly eight ordered fragments")
    canonical = "".join(fragments[index] for index in range(1, 9))
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if fingerprint != PREREGISTRATION_SHA256 or declared_fingerprint != fingerprint:
        raise RuntimeError("Preregistration fingerprint does not verify")
    required = (
        f"schema=loops-nightly-prereg-v1|id={PREREGISTRATION_ID}",
        f"eligible_session={ELIGIBLE_SESSION}",
        f"authority_generation={SOURCE_GENERATION}",
        f"source_set_sha256={EXPECTED_SOURCE_SET_SHA256}",
        f"gate_source_set_sha256={EXPECTED_GATE_SOURCE_SET_SHA256}",
        f"raw_assessment_cohort_sha256={EXPECTED_RAW_COHORT_SHA256}",
        f"exact_construction_cohort_sha256={EXPECTED_EXACT_COHORT_SHA256}",
        "fee_per_leg=0.65",
        "no fit,calibration,threshold,selection,ranking or production wiring",
        "orders_placed=0",
        "terminal BLOCKED",
        "PROPOSAL_ONLY",
    )
    missing = [value for value in required if value not in canonical]
    if missing:
        raise RuntimeError(
            "Preregistration is missing required canonical content: " + repr(missing)
        )
    return {
        "id": PREREGISTRATION_ID,
        "sha256": fingerprint,
        "schema_version": "loops-nightly-prereg-v1",
        "fragment_count": 8,
        "canonical_utf8_bytes": len(canonical.encode("utf-8")),
        "handoff_sequence": 85,
        "handoff_receipt_path": EXPECTED_HANDOFF_RELATIVE_PATH.as_posix(),
        "handoff_receipt_checksum_sha256": EXPECTED_HANDOFF_SHA256,
    }


def _validate_source_authority(root: Path) -> dict[str, object]:
    run = root / "ml" / "strategy-profit-training-runs" / SOURCE_GENERATION
    inventory = _validate_inventory(
        root,
        tuple(
            f"ml/strategy-profit-training-runs/{SOURCE_GENERATION}/{name}"
            for name in SOURCE_FILES
        ),
        expected_sha256=EXPECTED_SOURCE_SET_SHA256,
        label="immutable Strategy training source",
    )
    receipt_path = run / "receipt.json"
    manifest_path = run / "manifest.json"
    receipt = _read_json(receipt_path)
    manifest = _read_json(manifest_path)
    if str(receipt.get("run_path")) != (
        f"ml/strategy-profit-training-runs/{SOURCE_GENERATION}"
    ):
        raise RuntimeError("Strategy training receipt run path changed")
    if str(receipt.get("manifest_checksum_sha256")) != file_checksum(manifest_path):
        raise RuntimeError("Strategy training receipt no longer binds its manifest")
    if int(receipt.get("orders_placed", -1)) != 0:
        raise RuntimeError("Strategy training receipt violates zero-order safety")
    if bool(manifest.get("orders_enabled", True)) or not bool(
        manifest.get("research_only", False)
    ):
        raise RuntimeError("Strategy training manifest safety changed")
    if pd.Timestamp(str(receipt.get("published_at"))) != CAUSAL_INPUT_CUTOFF:
        raise RuntimeError("Strategy training causal cutoff changed")

    outputs = manifest.get("outputs")
    if not isinstance(outputs, list):
        raise RuntimeError("Strategy training manifest outputs are missing")
    output_by_name = {
        str(item.get("path")): item
        for item in outputs
        if isinstance(item, Mapping)
    }
    for name in SOURCE_FILES:
        if name in {"receipt.json", "manifest.json"}:
            continue
        record = output_by_name.get(name)
        path = run / name
        if not isinstance(record, Mapping):
            raise RuntimeError(f"Strategy training output is not manifested: {name}")
        if (
            int(record.get("size", -1)) != path.stat().st_size
            or str(record.get("checksum_sha256")) != file_checksum(path)
        ):
            raise RuntimeError(f"Strategy training output manifest changed: {name}")

    pointer_path = root / "ml" / "strategy-profit-training-latest" / "run.json"
    pointer = _read_json(pointer_path)
    current = pointer.get("current")
    if not isinstance(current, Mapping):
        raise RuntimeError("Strategy training authority pointer has no current record")
    if (
        str(current.get("run_path")) != receipt.get("run_path")
        or str(current.get("receipt_checksum_sha256")) != file_checksum(receipt_path)
    ):
        raise RuntimeError("Frozen Strategy generation is no longer current authority")
    return {
        "manifest": manifest,
        "receipt": receipt,
        "inventory": inventory,
        "authority_pointer": {
            "path": pointer_path.relative_to(root).as_posix(),
            "checksum_sha256": file_checksum(pointer_path),
            "run_path": str(current.get("run_path")),
            "receipt_checksum_sha256": str(
                current.get("receipt_checksum_sha256")
            ),
        },
    }


def _validate_inventory(
    base: Path,
    relative_paths: Sequence[str],
    *,
    expected_sha256: str,
    label: str,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    hash_records: list[str] = []
    for relative in relative_paths:
        normalized = Path(relative).as_posix()
        path = Path(base).joinpath(*normalized.split("/"))
        if not path.is_file():
            raise RuntimeError(f"{label} is missing: {path}")
        checksum = file_checksum(path)
        records.append(
            {
                "path": normalized,
                "size": path.stat().st_size,
                "checksum_sha256": checksum,
            }
        )
        hash_records.append(f"{normalized}|{checksum}")
    aggregate = _aggregate_records(hash_records)
    if aggregate != expected_sha256:
        raise RuntimeError(f"{label} set checksum changed")
    return sorted(records, key=lambda item: str(item["path"]))


def _validate_implementation_sources(repo_root: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for relative in (HARNESS_RELATIVE_PATH, FOCUSED_TEST_RELATIVE_PATH):
        path = repo_root.joinpath(*relative.split("/"))
        if not path.is_file():
            raise RuntimeError(f"Shadow proof implementation source is missing: {path}")
        records.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "checksum_sha256": file_checksum(path),
            }
        )
    return records


def _load_assessment_with_scores(
    root: Path,
    *,
    horizon: str,
    training_manifest: Mapping[str, object],
) -> tuple[pd.DataFrame, dict[str, object]]:
    run = root / "ml" / "strategy-profit-training-runs" / SOURCE_GENERATION
    report = _read_json(run / f"{horizon}-model-report.json")
    if (
        str(report.get("horizon")) != horizon
        or str(report.get("status")) != "MODEL_FIT"
        or int(report.get("orders_placed", -1)) != 0
        or bool(report.get("real_lockbox_used", True))
        or str(report.get("model_source")) != "OPRA_OHLCV_MODELED_EXECUTION"
    ):
        raise RuntimeError(f"{horizon} model report authority or safety changed")
    promotion = report.get("promotion_gate")
    if not isinstance(promotion, Mapping):
        raise RuntimeError(f"{horizon} promotion gate is missing")
    checks = promotion.get("checks")
    if (
        str(promotion.get("status")) != "PROMOTED"
        or not isinstance(checks, Mapping)
        or not checks
        or not all(bool(value) for value in checks.values())
        or int(promotion.get("assessment_decisions", -1)) != EXPECTED_DECISIONS
        or bool(promotion.get("assessment_used_for_training", True))
        or bool(promotion.get("assessment_used_for_calibration", True))
        or not bool(promotion.get("assessment_used_for_promotion_only", False))
    ):
        raise RuntimeError(f"{horizon} current promotion gate changed")
    offline = report.get("offline_evaluation")
    if not isinstance(offline, Mapping):
        raise RuntimeError(f"{horizon} offline evaluation is missing")
    if (
        bool(offline.get("assessment_used_for_training", True))
        or bool(offline.get("assessment_used_for_calibration", True))
        or bool(offline.get("assessment_used_for_ranking_policy_selection", True))
        or bool(offline.get("real_lockbox_used", True))
    ):
        raise RuntimeError(f"{horizon} assessment separation changed")

    models = training_manifest.get("models")
    model_record = models.get(horizon) if isinstance(models, Mapping) else None
    if not isinstance(model_record, Mapping):
        raise RuntimeError(f"{horizon} model authority record is missing")
    artifact = _safe_datastore_path(
        root,
        model_record.get("artifact_path"),
        label=f"{horizon} model artifact",
    )
    artifact_manifest_path = artifact / "manifest.json"
    model_path = artifact / "model.joblib"
    _require_file_checksum(
        artifact_manifest_path,
        str(model_record.get("artifact_manifest_checksum_sha256")),
        label=f"{horizon} model artifact manifest",
    )
    _require_file_checksum(
        model_path,
        str(model_record.get("model_file_checksum_sha256")),
        label=f"{horizon} model binary",
    )
    artifact_manifest = _read_json(artifact_manifest_path)
    numeric = tuple(str(value) for value in artifact_manifest.get("numeric_features", ()))
    categorical = tuple(
        str(value) for value in artifact_manifest.get("categorical_features", ())
    )
    if (
        len(numeric) != EXPECTED_NUMERIC_FEATURES
        or len(categorical) != EXPECTED_CATEGORICAL_FEATURES
        or str(artifact_manifest.get("horizon")) != horizon
    ):
        raise RuntimeError(f"{horizon} model feature schema changed")
    model_file = artifact_manifest.get("model_file")
    if (
        not isinstance(model_file, Mapping)
        or str(model_file.get("path")) != "model.joblib"
        or str(model_file.get("checksum_sha256")) != file_checksum(model_path)
    ):
        raise RuntimeError(f"{horizon} artifact manifest no longer binds model.joblib")

    frame = pd.read_parquet(run / f"{horizon}-modeled-outcomes.parquet")
    lower = pd.Timestamp(str(report["assessment_date_range"]["start"]))
    upper = pd.Timestamp(str(report["assessment_date_range"]["end"]))
    targets = pd.to_datetime(frame["target_window_start"], utc=True, errors="coerce")
    frame = frame.loc[targets.between(lower, upper, inclusive="both")].copy()
    if len(frame) != EXPECTED_RAW_ROWS[horizon]:
        raise RuntimeError(f"{horizon} raw assessment row count changed")
    if int(frame["target_window_start"].nunique()) != EXPECTED_DECISIONS:
        raise RuntimeError(f"{horizon} assessment decision count changed")
    if tuple(sorted(frame["symbol"].astype(str).unique())) != EXPECTED_SYMBOLS:
        raise RuntimeError(f"{horizon} assessment symbols changed")
    if not frame["execution_evidence_type"].eq("MODELED_OPRA_OHLCV_1H").all():
        raise RuntimeError(f"{horizon} assessment execution evidence changed")
    max_exit = pd.to_datetime(frame["exit_available_at"], utc=True).max()
    if max_exit != EXPECTED_MAX_EXIT_AVAILABLE_AT[horizon] or max_exit > CAUSAL_INPUT_CUTOFF:
        raise RuntimeError(f"{horizon} assessment causal maturity changed")

    missing_features = sorted(set((*numeric, *categorical)).difference(frame.columns))
    if missing_features:
        raise RuntimeError(
            f"{horizon} inference feature schema is incomplete: {missing_features}"
        )
    bundle = joblib.load(model_path)
    if not isinstance(bundle, Mapping):
        raise RuntimeError(f"{horizon} model bundle is invalid")
    matrix = frame.loc[:, list((*numeric, *categorical))].copy()
    raw = np.asarray(bundle["estimator"].predict_proba(matrix)[:, 1], dtype=float)
    calibrated = np.asarray(bundle["calibrator"].predict(raw), dtype=float)
    _require_probability_array(raw, label=f"{horizon} raw")
    _require_probability_array(calibrated, label=f"{horizon} calibrated")
    frame["raw_profit_probability"] = raw
    frame["calibrated_profit_probability"] = calibrated

    target = pd.to_numeric(frame["profitable"], errors="raise").to_numpy(dtype=int)
    weights = _decision_weights(frame)
    reproduced = {
        "raw_model": _probability_metrics(target, raw, sample_weight=weights),
        "calibrated_model": _probability_metrics(
            target, calibrated, sample_weight=weights
        ),
        "candidate_level_raw_model": _probability_metrics(target, raw),
        "candidate_level_calibrated_model": _probability_metrics(
            target, calibrated
        ),
    }
    for name, metrics in reproduced.items():
        expected = offline.get(name)
        if not isinstance(expected, Mapping):
            raise RuntimeError(f"{horizon} published {name} metrics are missing")
        for metric in (
            "brier_score",
            "log_loss",
            "expected_calibration_error_10_bin",
        ):
            _require_close(
                metrics[metric],
                expected.get(metric),
                label=f"{horizon} reproduced {name} {metric}",
            )
    return frame, {
        "artifact_path": artifact.relative_to(root).as_posix(),
        "artifact_manifest_checksum_sha256": file_checksum(artifact_manifest_path),
        "model_checksum_sha256": file_checksum(model_path),
        "numeric_feature_count": len(numeric),
        "categorical_feature_count": len(categorical),
        "assessment_rows": len(frame),
        "assessment_decisions": int(frame["target_window_start"].nunique()),
        "assessment_date_range": report.get("assessment_date_range"),
        "max_exit_available_at": max_exit.isoformat(),
        "promotion_gate": promotion,
        "reproduced_published_metrics": reproduced,
        "model_data_feature_health": "BLOCKED",
        "retraining_due": False,
    }


def _validate_raw_cohort_identity(frame: pd.DataFrame) -> None:
    records = _cohort_records(frame, include_candidate_key=True)
    if len(records) != sum(EXPECTED_RAW_ROWS.values()):
        raise RuntimeError("Raw assessment cohort row count changed")
    if _aggregate_records(records) != EXPECTED_RAW_COHORT_SHA256:
        raise RuntimeError("Raw assessment cohort fingerprint changed")


def _validate_exact_cohort_identity(frame: pd.DataFrame) -> None:
    records = _cohort_records(frame, include_candidate_key=False)
    if len(records) != sum(EXPECTED_EXACT_ROWS.values()):
        raise RuntimeError("Exact-construction cohort row count changed")
    if len(set(records)) != len(records):
        raise RuntimeError("Exact-construction cohort still contains aliases")
    if _aggregate_records(records) != EXPECTED_EXACT_COHORT_SHA256:
        raise RuntimeError("Exact-construction cohort fingerprint changed")


def _cohort_records(
    frame: pd.DataFrame,
    *,
    include_candidate_key: bool,
) -> list[str]:
    records: list[str] = []
    for row in frame.itertuples(index=False):
        values: list[object] = [
            str(row.horizon),
            pd.Timestamp(row.target_window_start).isoformat(),
            pd.Timestamp(row.decision_timestamp).isoformat(),
            str(row.symbol),
        ]
        if include_candidate_key:
            values.append(str(row.candidate_key))
        values.append(hashlib.sha256(str(row.legs_json).encode("utf-8")).hexdigest())
        records.append(
            json.dumps(values, ensure_ascii=False, separators=(",", ":"))
        )
    return records


def _deduplicate_exact_constructions(
    frame: pd.DataFrame,
    *,
    validate_preregistered_counts: bool = True,
) -> tuple[pd.DataFrame, dict[str, object], pd.DataFrame]:
    working = frame.copy()
    working["legs_sha256"] = working["legs_json"].astype(str).map(
        lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest()
    )
    working["exact_construction_identity"] = [
        json.dumps(
            [
                str(row.horizon),
                pd.Timestamp(row.target_window_start).isoformat(),
                pd.Timestamp(row.decision_timestamp).isoformat(),
                str(row.symbol),
                str(row.legs_sha256),
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for row in working.itertuples(index=False)
    ]
    duplicate_groups = 0
    duplicate_rows = 0
    proof_rows: list[dict[str, object]] = []
    by_horizon = {
        horizon: {
            "duplicate_groups": 0,
            "non_probability_mismatch_groups": 0,
            "raw_probability_mismatch_groups": 0,
            "calibrated_probability_mismatch_groups": 0,
            "maximum_raw_probability_delta": 0.0,
            "maximum_calibrated_probability_delta": 0.0,
        }
        for horizon in HORIZONS
    }
    for identity, group in working.groupby(
        "exact_construction_identity", sort=False, dropna=False
    ):
        if len(group) <= 1:
            continue
        duplicate_groups += 1
        duplicate_rows += len(group) - 1
        horizon = str(group.iloc[0]["horizon"])
        by_horizon[horizon]["duplicate_groups"] += 1
        non_probability_equal = True
        for column in _NON_PROBABILITY_ALIAS_EQUALITY_COLUMNS:
            values = {_canonical_scalar(value) for value in group[column]}
            if len(values) != 1:
                non_probability_equal = False
        raw_values = group["raw_profit_probability"].to_numpy(dtype=float)
        calibrated_values = group[
            "calibrated_profit_probability"
        ].to_numpy(dtype=float)
        raw_delta = float(np.max(raw_values) - np.min(raw_values))
        calibrated_delta = float(
            np.max(calibrated_values) - np.min(calibrated_values)
        )
        raw_equal = bool(raw_delta == 0.0)
        calibrated_equal = bool(calibrated_delta == 0.0)
        if not non_probability_equal:
            by_horizon[horizon]["non_probability_mismatch_groups"] += 1
        if not raw_equal:
            by_horizon[horizon]["raw_probability_mismatch_groups"] += 1
        if not calibrated_equal:
            by_horizon[horizon]["calibrated_probability_mismatch_groups"] += 1
        by_horizon[horizon]["maximum_raw_probability_delta"] = max(
            float(by_horizon[horizon]["maximum_raw_probability_delta"]),
            raw_delta,
        )
        by_horizon[horizon]["maximum_calibrated_probability_delta"] = max(
            float(
                by_horizon[horizon]["maximum_calibrated_probability_delta"]
            ),
            calibrated_delta,
        )
        first = group.iloc[0]
        proof_rows.append(
            {
                "horizon": horizon,
                "target_window_start": first["target_window_start"],
                "decision_timestamp": first["decision_timestamp"],
                "symbol": str(first["symbol"]),
                "exact_construction_identity": identity,
                "alias_count": len(group),
                "alias_candidate_keys_sha256": _aggregate_records(
                    group["candidate_key"].astype(str).tolist()
                ),
                "non_probability_fields_equal": non_probability_equal,
                "raw_profit_probability_equal": raw_equal,
                "calibrated_profit_probability_equal": calibrated_equal,
                "maximum_raw_probability_delta": raw_delta,
                "maximum_calibrated_probability_delta": calibrated_delta,
            }
        )

    working = working.sort_values(
        ["exact_construction_identity", "candidate_key"], kind="mergesort"
    )
    alias_counts = working.groupby("exact_construction_identity")[
        "candidate_key"
    ].transform("size")
    working["alias_count"] = alias_counts.astype(int)
    key_hashes = {
        identity: _aggregate_records(group["candidate_key"].astype(str).tolist())
        for identity, group in working.groupby(
            "exact_construction_identity", sort=False
        )
    }
    working["alias_candidate_keys_sha256"] = working[
        "exact_construction_identity"
    ].map(key_hashes)
    exact = working.drop_duplicates(
        "exact_construction_identity", keep="first"
    ).reset_index(drop=True)
    counts = exact["horizon"].value_counts().to_dict()
    positives = (
        exact.groupby("horizon")["profitable"].sum().astype(int).to_dict()
    )
    if validate_preregistered_counts and (
        counts != EXPECTED_EXACT_ROWS or positives != EXPECTED_EXACT_POSITIVES
    ):
        raise RuntimeError("Exact-construction cohort counts changed")
    raw_positives = (
        frame.groupby("horizon")["profitable"].sum().astype(int).to_dict()
    )
    if validate_preregistered_counts and raw_positives != EXPECTED_RAW_POSITIVES:
        raise RuntimeError("Raw assessment positive counts changed")
    alias_evidence = {
        "equality_columns": list(_ALIAS_EQUALITY_COLUMNS),
        "duplicate_groups": duplicate_groups,
        "duplicate_rows_removed": duplicate_rows,
        "all_aliases_equal": all(
            int(values["non_probability_mismatch_groups"]) == 0
            and int(values["raw_probability_mismatch_groups"]) == 0
            and int(values["calibrated_probability_mismatch_groups"]) == 0
            for values in by_horizon.values()
        ),
        "by_horizon": by_horizon,
        "selection_rule": "lexicographically_first_candidate_key",
        "raw_rows_by_horizon": EXPECTED_RAW_ROWS,
        "exact_rows_by_horizon": EXPECTED_EXACT_ROWS,
        "raw_positives_by_horizon": EXPECTED_RAW_POSITIVES,
        "exact_positives_by_horizon": EXPECTED_EXACT_POSITIVES,
    }
    return exact, alias_evidence, pd.DataFrame(proof_rows)


def _project_exact_outcomes(
    frame: pd.DataFrame,
    *,
    fee_per_leg: float,
) -> pd.DataFrame:
    if not math.isfinite(fee_per_leg) or fee_per_leg < 0.0:
        raise ValueError("Fee stress must be finite and nonnegative")
    output = frame.copy()
    output["baseline_net_profit"] = pd.to_numeric(
        output["net_profit"], errors="raise"
    ).astype(float)
    output["baseline_return_on_risk"] = pd.to_numeric(
        output["return_on_risk"], errors="raise"
    ).astype(float)
    output["baseline_profitable"] = pd.to_numeric(
        output["profitable"], errors="raise"
    ).astype(int)
    output["candidate_net_profit"] = output["baseline_net_profit"]
    output["candidate_return_on_risk"] = output["baseline_return_on_risk"]
    output["candidate_profitable"] = output["baseline_profitable"]

    exact = output["risk_calculation_status"].eq("EXPIRATION_PAYOFF_EXACT")
    leg_count = pd.to_numeric(output["leg_count"], errors="coerce").to_numpy(float)
    max_loss = pd.to_numeric(output["max_loss"], errors="coerce").to_numpy(float)
    max_profit = pd.to_numeric(output["max_profit"], errors="coerce").to_numpy(float)
    capital = pd.to_numeric(
        output["capital_required"], errors="coerce"
    ).to_numpy(float)
    if (
        not np.isfinite(leg_count[exact]).all()
        or np.any(leg_count[exact] <= 0.0)
        or not np.isfinite(max_loss[exact]).all()
        or np.any(max_loss[exact] < 0.0)
        or not np.isfinite(capital[exact]).all()
        or np.any(capital[exact] <= 0.0)
    ):
        raise RuntimeError("Exact-payoff bounds or capital are invalid")

    exit_fee = leg_count * fee_per_leg
    lower = -max_loss - exit_fee
    unbounded_upper = np.isnan(max_profit)
    upper = np.where(unbounded_upper, np.inf, max_profit - exit_fee)
    baseline = output["baseline_net_profit"].to_numpy(dtype=float)
    lower_breach = exact.to_numpy() & (baseline < lower)
    upper_breach = exact.to_numpy() & (baseline > upper)
    changed = lower_breach | upper_breach
    projected = np.minimum(np.maximum(baseline, lower), upper)
    output.loc[changed, "candidate_net_profit"] = projected[changed]
    output.loc[changed, "candidate_return_on_risk"] = (
        projected[changed] / capital[changed]
    )
    output.loc[changed, "candidate_profitable"] = (
        projected[changed] > 0.0
    ).astype(int)
    candidate = output["candidate_net_profit"].to_numpy(dtype=float)
    candidate_lower_breach = exact.to_numpy() & (candidate < lower)
    candidate_upper_breach = exact.to_numpy() & (candidate > upper)

    output["exit_fee_allowance"] = np.where(exact, exit_fee, np.nan)
    output["admissible_lower_bound"] = np.where(exact, lower, np.nan)
    output["admissible_upper_bound"] = np.where(
        exact & ~unbounded_upper, upper, np.nan
    )
    output["unbounded_upper"] = exact.to_numpy() & unbounded_upper
    output["baseline_lower_breach"] = lower_breach
    output["baseline_upper_breach"] = upper_breach
    output["candidate_lower_breach"] = candidate_lower_breach
    output["candidate_upper_breach"] = candidate_upper_breach
    output["projection_applied"] = changed
    output["label_sign_changed"] = output["candidate_profitable"].ne(
        output["baseline_profitable"]
    )

    unchanged = ~changed
    for candidate_column, baseline_column in (
        ("candidate_net_profit", "baseline_net_profit"),
        ("candidate_return_on_risk", "baseline_return_on_risk"),
        ("candidate_profitable", "baseline_profitable"),
    ):
        if not output.loc[unchanged, candidate_column].equals(
            output.loc[unchanged, baseline_column]
        ):
            raise RuntimeError("Initially valid or path-dependent rows changed")
    return output


def _horizon_evidence(
    frame: pd.DataFrame,
    *,
    horizon: str,
    model_evidence: Mapping[str, object],
) -> dict[str, object]:
    exact = frame["risk_calculation_status"].eq("EXPIRATION_PAYOFF_EXACT")
    if int(exact.sum()) != EXPECTED_EXACT_PAYOFF_ROWS[horizon]:
        raise RuntimeError(f"{horizon} exact-payoff row count changed")
    lower_breaches = int(frame["baseline_lower_breach"].sum())
    upper_breaches = int(frame["baseline_upper_breach"].sum())
    if (
        lower_breaches != EXPECTED_LOWER_BREACHES[horizon]
        or upper_breaches != EXPECTED_UPPER_BREACHES[horizon]
    ):
        raise RuntimeError(f"{horizon} preregistered baseline breaches changed")
    target = frame["candidate_profitable"].to_numpy(dtype=int)
    raw = frame["raw_profit_probability"].to_numpy(dtype=float)
    calibrated = frame["calibrated_profit_probability"].to_numpy(dtype=float)
    weighted = _decision_weights(frame)
    raw_metrics = _probability_metrics(target, raw, sample_weight=weighted)
    calibrated_metrics = _probability_metrics(
        target, calibrated, sample_weight=weighted
    )
    candidate_raw = _probability_metrics(target, raw)
    candidate_calibrated = _probability_metrics(target, calibrated)
    candidate_nondegradation = _metric_nondegradation_gates(
        candidate_raw,
        candidate_calibrated,
    )
    promotion = model_evidence["promotion_gate"]
    gates = {
        "exact_construction_row_count": len(frame) == EXPECTED_EXACT_ROWS[horizon],
        "assessment_decisions_at_least_63": int(
            frame["target_window_start"].nunique()
        )
        >= EXPECTED_DECISIONS,
        "primary_exact_payoff_admissibility_failures_zero": int(
            frame["candidate_lower_breach"].sum()
            + frame["candidate_upper_breach"].sum()
        )
        == 0,
        "zero_label_sign_changes": int(frame["label_sign_changed"].sum()) == 0,
        "initially_valid_and_path_rows_unchanged": _unchanged_rows_match(frame),
        "all_probabilities_finite_and_bounded": bool(
            np.isfinite(raw).all()
            and np.isfinite(calibrated).all()
            and np.all((raw >= 0.0) & (raw <= 1.0))
            and np.all((calibrated >= 0.0) & (calibrated <= 1.0))
        ),
        "current_promotion_gate_still_passes": bool(
            promotion.get("status") == "PROMOTED"
            and all(bool(value) for value in promotion.get("checks", {}).values())
        ),
        "equal_decision_calibrated_brier_within_training_base": bool(
            calibrated_metrics["brier_score"]
            <= float(promotion["base_rate_brier_score"]) + 1e-12
        ),
        "equal_decision_calibrated_log_loss_within_training_base": bool(
            calibrated_metrics["log_loss"]
            <= float(promotion["base_rate_log_loss"]) + 1e-12
        ),
        "equal_decision_calibrated_ece_at_most_0_12": bool(
            calibrated_metrics["expected_calibration_error_10_bin"] <= 0.12
        ),
        "candidate_level_calibrated_brier_not_worse_than_raw": bool(
            candidate_nondegradation["brier_score"]
        ),
        "candidate_level_calibrated_log_loss_not_worse_than_raw": bool(
            candidate_nondegradation["log_loss"]
        ),
        "candidate_level_calibrated_ece_not_worse_than_raw": bool(
            candidate_nondegradation["expected_calibration_error_10_bin"]
        ),
    }
    return {
        "rows": len(frame),
        "decisions": int(frame["target_window_start"].nunique()),
        "exact_payoff_rows": int(exact.sum()),
        "path_dependent_rows": int((~exact).sum()),
        "baseline_lower_bound_failures": lower_breaches,
        "baseline_upper_bound_failures": upper_breaches,
        "candidate_admissibility_failures": int(
            frame["candidate_lower_breach"].sum()
            + frame["candidate_upper_breach"].sum()
        ),
        "rows_projected": int(frame["projection_applied"].sum()),
        "label_sign_changes": int(frame["label_sign_changed"].sum()),
        "raw_probability": raw_metrics,
        "calibrated_probability": calibrated_metrics,
        "candidate_level_raw_probability": candidate_raw,
        "candidate_level_calibrated_probability": candidate_calibrated,
        "return_effect": _return_effect(frame),
        "fee_stresses": {
            str(fee): _stress_summary(
                _project_exact_outcomes(
                    frame.drop(
                        columns=[
                            "baseline_net_profit",
                            "candidate_net_profit",
                            "baseline_return_on_risk",
                            "candidate_return_on_risk",
                            "baseline_profitable",
                            "candidate_profitable",
                            "exit_fee_allowance",
                            "admissible_lower_bound",
                            "admissible_upper_bound",
                            "unbounded_upper",
                            "baseline_lower_breach",
                            "baseline_upper_breach",
                            "candidate_lower_breach",
                            "candidate_upper_breach",
                            "projection_applied",
                            "label_sign_changed",
                        ],
                        errors="ignore",
                    ),
                    fee_per_leg=fee,
                )
            )
            for fee in FEE_STRESSES
        },
        "gates": gates,
        "gate_status": "PASS" if all(gates.values()) else "BLOCKED",
    }


def _diagnostic_evidence(exact: pd.DataFrame) -> dict[str, object]:
    working = exact.copy()
    half_labels = pd.Series(index=working.index, dtype="string")
    for horizon, group in working.groupby("horizon", sort=True):
        decisions = sorted(pd.to_datetime(group["target_window_start"], utc=True).unique())
        first = set(decisions[: len(decisions) // 2])
        values = pd.to_datetime(group["target_window_start"], utc=True)
        half_labels.loc[group.index] = np.where(
            values.isin(first),
            f"{horizon}:FIRST_{len(first)}_DECISIONS",
            f"{horizon}:SECOND_{len(decisions) - len(first)}_DECISIONS",
        )
    working["chronological_assessment_half"] = half_labels
    groups: list[tuple[str, str, pd.DataFrame]] = []
    for dimension in ("horizon", "chronological_assessment_half"):
        for key, group in working.groupby(dimension, sort=True, dropna=False):
            groups.append((dimension, str(key), group.copy()))
    for dimension in ("symbol", "strategy_family"):
        for (horizon, key), group in working.groupby(
            ["horizon", dimension], sort=True, dropna=False
        ):
            groups.append((dimension, f"{horizon}:{key}", group.copy()))
    records: list[dict[str, object]] = []
    for dimension, key, group in groups:
        for fee in FEE_STRESSES:
            projected = _project_exact_outcomes(group, fee_per_leg=fee)
            target = projected["candidate_profitable"].to_numpy(dtype=int)
            raw = projected["raw_profit_probability"].to_numpy(dtype=float)
            calibrated = projected["calibrated_profit_probability"].to_numpy(
                dtype=float
            )
            weights = _decision_weights(projected)
            records.append(
                {
                    "dimension": dimension,
                    "key": key,
                    "fee_per_leg": fee,
                    "rows": len(projected),
                    "decisions": int(projected["target_window_start"].nunique()),
                    "raw_probability": _compact_metrics(
                        _probability_metrics(target, raw, sample_weight=weights)
                    ),
                    "calibrated_probability": _compact_metrics(
                        _probability_metrics(
                            target, calibrated, sample_weight=weights
                        )
                    ),
                    "candidate_level_raw_probability": _compact_metrics(
                        _probability_metrics(target, raw)
                    ),
                    "candidate_level_calibrated_probability": _compact_metrics(
                        _probability_metrics(target, calibrated)
                    ),
                    "return_effect": _return_effect(projected),
                    "baseline_admissibility_failures": int(
                        projected["baseline_lower_breach"].sum()
                        + projected["baseline_upper_breach"].sum()
                    ),
                    "candidate_admissibility_failures": int(
                        projected["candidate_lower_breach"].sum()
                        + projected["candidate_upper_breach"].sum()
                    ),
                    "label_sign_changes": int(
                        projected["label_sign_changed"].sum()
                    ),
                }
            )
    return {
        "regime_bins": "NOT_APPLICABLE_NO_CANONICAL_REGIME_CATEGORY",
        "chronological_half_rule": (
            "within_horizon_first_floor_63_over_2_decisions_then_remainder"
        ),
        "fee_stresses": list(FEE_STRESSES),
        "records": records,
    }


def _stress_summary(frame: pd.DataFrame) -> dict[str, object]:
    return {
        "rows": len(frame),
        "baseline_admissibility_failures": int(
            frame["baseline_lower_breach"].sum()
            + frame["baseline_upper_breach"].sum()
        ),
        "candidate_admissibility_failures": int(
            frame["candidate_lower_breach"].sum()
            + frame["candidate_upper_breach"].sum()
        ),
        "rows_projected": int(frame["projection_applied"].sum()),
        "label_sign_changes": int(frame["label_sign_changed"].sum()),
        "return_effect": _return_effect(frame),
    }


def _return_effect(frame: pd.DataFrame) -> dict[str, object]:
    baseline_profit = frame["baseline_net_profit"].to_numpy(dtype=float)
    candidate_profit = frame["candidate_net_profit"].to_numpy(dtype=float)
    baseline_return = frame["baseline_return_on_risk"].to_numpy(dtype=float)
    candidate_return = frame["candidate_return_on_risk"].to_numpy(dtype=float)
    return {
        "baseline_total_net_profit": float(np.sum(baseline_profit)),
        "candidate_total_net_profit": float(np.sum(candidate_profit)),
        "total_net_profit_change": float(np.sum(candidate_profit - baseline_profit)),
        "baseline_mean_return_on_risk": float(np.mean(baseline_return)),
        "candidate_mean_return_on_risk": float(np.mean(candidate_return)),
        "mean_return_on_risk_change": float(
            np.mean(candidate_return - baseline_return)
        ),
        "baseline_positive_rate": float(
            frame["baseline_profitable"].astype(float).mean()
        ),
        "candidate_positive_rate": float(
            frame["candidate_profitable"].astype(float).mean()
        ),
    }


def _unchanged_rows_match(frame: pd.DataFrame) -> bool:
    unchanged = ~frame["projection_applied"]
    return bool(
        frame.loc[unchanged, "candidate_net_profit"].equals(
            frame.loc[unchanged, "baseline_net_profit"]
        )
        and frame.loc[unchanged, "candidate_return_on_risk"].equals(
            frame.loc[unchanged, "baseline_return_on_risk"]
        )
        and frame.loc[unchanged, "candidate_profitable"].equals(
            frame.loc[unchanged, "baseline_profitable"]
        )
    )


def _decision_weights(frame: pd.DataFrame) -> np.ndarray:
    counts = frame.groupby("target_window_start")["candidate_key"].transform(
        "count"
    )
    values = pd.to_numeric(counts, errors="coerce").to_numpy(dtype=float)
    if not np.isfinite(values).all() or np.any(values <= 0.0):
        raise RuntimeError("Assessment decision weights are invalid")
    return 1.0 / values


def _probability_metrics(
    target: Sequence[object] | np.ndarray,
    probability: Sequence[object] | np.ndarray,
    *,
    sample_weight: np.ndarray | None = None,
) -> dict[str, object]:
    observed = np.asarray(target, dtype=int)
    predicted = np.asarray(probability, dtype=float)
    _require_probability_array(predicted, label="metric")
    if observed.ndim != 1 or len(observed) != len(predicted):
        raise ValueError("Probability metric inputs are not aligned")
    if not np.isin(observed, (0, 1)).all():
        raise ValueError("Probability metric targets must be binary")
    weights = (
        np.ones(len(observed), dtype=float)
        if sample_weight is None
        else np.asarray(sample_weight, dtype=float)
    )
    if (
        weights.shape != observed.shape
        or not np.isfinite(weights).all()
        or np.any(weights <= 0.0)
    ):
        raise ValueError("Probability metric weights are invalid")
    clipped = np.clip(predicted, 1e-12, 1.0 - 1e-12)
    log_loss = -np.average(
        observed * np.log(clipped) + (1 - observed) * np.log(1 - clipped),
        weights=weights,
    )
    brier = np.average(np.square(predicted - observed), weights=weights)
    auc = (
        float(roc_auc_score(observed, predicted, sample_weight=weights))
        if np.unique(observed).size == 2
        else None
    )
    total_weight = float(weights.sum())
    ece = 0.0
    reliability_bins: list[dict[str, object]] = []
    boundaries = np.linspace(0.0, 1.0, 11)
    for index in range(10):
        lower = float(boundaries[index])
        upper = float(boundaries[index + 1])
        selected = (predicted >= lower) & (
            predicted <= upper if index == 9 else predicted < upper
        )
        count = int(selected.sum())
        selected_weight = float(weights[selected].sum())
        if count:
            mean_probability = float(
                np.average(predicted[selected], weights=weights[selected])
            )
            observed_rate = float(
                np.average(observed[selected], weights=weights[selected])
            )
            ece += selected_weight / total_weight * abs(
                mean_probability - observed_rate
            )
        else:
            mean_probability = None
            observed_rate = None
        reliability_bins.append(
            {
                "lower": lower,
                "upper": upper,
                "rows": count,
                "weight": selected_weight,
                "mean_probability": mean_probability,
                "observed_rate": observed_rate,
            }
        )
    return {
        "rows": len(observed),
        "probability_coverage": 1.0,
        "target_base_rate": float(np.average(observed, weights=weights)),
        "brier_score": float(brier),
        "log_loss": float(log_loss),
        "roc_auc": auc,
        "expected_calibration_error_10_bin": float(ece),
        "reliability_bins": reliability_bins,
    }


def _compact_metrics(metrics: Mapping[str, object]) -> dict[str, object]:
    return {
        key: metrics.get(key)
        for key in (
            "rows",
            "probability_coverage",
            "target_base_rate",
            "brier_score",
            "log_loss",
            "roc_auc",
            "expected_calibration_error_10_bin",
        )
    }


def _metric_nondegradation_gates(
    raw: Mapping[str, object],
    calibrated: Mapping[str, object],
) -> dict[str, bool]:
    return {
        metric: bool(float(calibrated[metric]) <= float(raw[metric]) + 1e-12)
        for metric in (
            "brier_score",
            "log_loss",
            "expected_calibration_error_10_bin",
        )
    }


def _publish_result(
    root: Path,
    *,
    repo_root: Path,
    created: pd.Timestamp,
    report: Mapping[str, object],
    proof: pd.DataFrame,
    source_inventory: Sequence[Mapping[str, object]],
    gate_inventory: Sequence[Mapping[str, object]],
    implementation_inventory: Sequence[Mapping[str, object]],
) -> ExecutionCoherenceAblationResult:
    parent = root / "ml" / "nightly-strategy-execution-coherence-ablation-runs"
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / PREREGISTRATION_SHA256
    if destination.exists():
        return _load_result(
            destination,
            datastore_root=root,
            repo_root=repo_root,
            status="UNCHANGED_SKIPPED",
        )
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.tmp-{os.getpid()}-",
            dir=parent,
        )
    )
    try:
        report_path = staging / "report.json"
        proof_path = staging / "transform-proof.parquet"
        manifest_path = staging / "manifest.json"
        _write_json(report_path, report)
        proof.to_parquet(proof_path, index=False)
        implementation = {
            str(item["path"]): str(item["checksum_sha256"])
            for item in implementation_inventory
        }
        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "created_at": created.isoformat(),
            "status": "COMPLETE_SHADOW_ONLY",
            "decision": str(report["decision"]),
            "run_path": destination.relative_to(root).as_posix(),
            "preregistration_id": PREREGISTRATION_ID,
            "source_fingerprint_sha256": PREREGISTRATION_SHA256,
            "inputs": {
                "source_set_sha256": EXPECTED_SOURCE_SET_SHA256,
                "source_files": list(source_inventory),
                "gate_source_set_sha256": EXPECTED_GATE_SOURCE_SET_SHA256,
                "gate_files": list(gate_inventory),
                "raw_cohort_sha256": EXPECTED_RAW_COHORT_SHA256,
                "exact_construction_cohort_sha256": EXPECTED_EXACT_COHORT_SHA256,
                "implementation_files": list(implementation_inventory),
            },
            "outputs": {
                "report.json": {
                    "size": report_path.stat().st_size,
                    "checksum_sha256": file_checksum(report_path),
                },
                "transform-proof.parquet": {
                    "size": proof_path.stat().st_size,
                    "checksum_sha256": file_checksum(proof_path),
                    "rows": len(proof),
                    "columns": list(proof.columns),
                },
            },
            "production_mutation": False,
            "production_authority_mutation": False,
            "orders_enabled": False,
            "orders_placed": 0,
        }
        _write_json(manifest_path, manifest)
        failed_gates = report.get("failed_gates")
        receipt = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "status": "COMPLETE_SHADOW_ONLY",
            "decision": str(report["decision"]),
            "proof_kind": str(report.get("proof_kind", "UNKNOWN")),
            "created_at": created.isoformat(),
            "eligible_session": ELIGIBLE_SESSION,
            "run_path": destination.relative_to(root).as_posix(),
            "preregistration_id": PREREGISTRATION_ID,
            "source_fingerprint_sha256": PREREGISTRATION_SHA256,
            "authority_generation": SOURCE_GENERATION,
            "source_set_sha256": EXPECTED_SOURCE_SET_SHA256,
            "gate_source_set_sha256": EXPECTED_GATE_SOURCE_SET_SHA256,
            "raw_cohort_sha256": EXPECTED_RAW_COHORT_SHA256,
            "exact_construction_cohort_sha256": EXPECTED_EXACT_COHORT_SHA256,
            "harness_checksum_sha256": implementation[HARNESS_RELATIVE_PATH],
            "focused_test_checksum_sha256": implementation[
                FOCUSED_TEST_RELATIVE_PATH
            ],
            "row_count": len(proof),
            "failed_gates": failed_gates,
            "manifest_checksum_sha256": file_checksum(manifest_path),
            "report_checksum_sha256": file_checksum(report_path),
            "proof_checksum_sha256": file_checksum(proof_path),
            "promotion_performed": False,
            "production_mutation": False,
            "production_candidate_mutation": False,
            "production_model_authority_mutation": False,
            "production_authority_mutation": False,
            "runtime_mutation": False,
            "ui_or_ranking_mutation": False,
            "orders_enabled": False,
            "orders_placed": 0,
        }
        _write_json(staging / "receipt.json", receipt)
        try:
            staging.replace(destination)
        except (FileExistsError, OSError):
            if not destination.is_dir():
                raise
            if staging.exists() and staging.parent == parent:
                shutil.rmtree(staging)
            return _load_result(
                destination,
                datastore_root=root,
                repo_root=repo_root,
                status="UNCHANGED_SKIPPED",
            )
    except BaseException:
        if staging.exists() and staging.parent == parent:
            shutil.rmtree(staging)
        raise
    return _load_result(
        destination,
        datastore_root=root,
        repo_root=repo_root,
        status="COMPLETE_SHADOW_ONLY",
    )


def _find_existing_result(
    root: Path,
    *,
    repo_root: Path,
) -> ExecutionCoherenceAblationResult | None:
    parent = root / "ml" / "nightly-strategy-execution-coherence-ablation-runs"
    if not parent.is_dir():
        return None
    destination = parent / PREREGISTRATION_SHA256
    if destination.exists():
        if not destination.is_dir():
            raise RuntimeError("Preregistered result path is not a directory")
        result = _load_result(
            destination,
            datastore_root=root,
            repo_root=repo_root,
            status="UNCHANGED_SKIPPED",
        )
    else:
        result = None
    unexpected_children: list[Path] = []
    for child in sorted(parent.iterdir()):
        if child == destination:
            continue
        if child.name.startswith("."):
            raise RuntimeError(
                "A shadow-result staging claim is in flight or orphaned: "
                f"{child}"
            )
        unexpected_children.append(child)
    if unexpected_children:
        raise RuntimeError(
            "Unexpected committed material exists outside the content-addressed "
            "result path: "
            + repr([str(path) for path in unexpected_children])
        )
    return result


def _load_result(
    directory: Path,
    *,
    datastore_root: Path,
    repo_root: Path,
    status: str,
) -> ExecutionCoherenceAblationResult:
    receipt_path = directory / "receipt.json"
    report_path = directory / "report.json"
    proof_path = directory / "transform-proof.parquet"
    manifest_path = directory / "manifest.json"
    receipt = _read_json(receipt_path)
    if str(receipt.get("schema_version")) != RECEIPT_SCHEMA_VERSION:
        raise RuntimeError("Execution-coherence receipt schema is invalid")
    if str(receipt.get("status")) != "COMPLETE_SHADOW_ONLY":
        raise RuntimeError("Execution-coherence receipt is not complete")
    if str(receipt.get("decision")) not in {"PROPOSAL_ONLY", "BLOCKED"}:
        raise RuntimeError("Execution-coherence receipt decision is invalid")
    proof_kind = str(receipt.get("proof_kind"))
    expected_rows = {
        "TERMINAL_ALIAS_EQUALITY_ROLLBACK": 1205,
        "TERMINAL_VALIDATION_OR_RUNTIME_ROLLBACK": 1,
        "COMPLETED_ADMISSIBILITY_TRANSFORM": sum(EXPECTED_EXACT_ROWS.values()),
    }.get(proof_kind)
    if expected_rows is None or int(receipt.get("row_count", -1)) != expected_rows:
        raise RuntimeError("Execution-coherence receipt row count changed")
    expected_receipt_values = {
        "eligible_session": ELIGIBLE_SESSION,
        "preregistration_id": PREREGISTRATION_ID,
        "source_fingerprint_sha256": PREREGISTRATION_SHA256,
        "authority_generation": SOURCE_GENERATION,
        "source_set_sha256": EXPECTED_SOURCE_SET_SHA256,
        "gate_source_set_sha256": EXPECTED_GATE_SOURCE_SET_SHA256,
        "raw_cohort_sha256": EXPECTED_RAW_COHORT_SHA256,
        "exact_construction_cohort_sha256": EXPECTED_EXACT_COHORT_SHA256,
        "run_path": directory.relative_to(datastore_root).as_posix(),
    }
    changed = {
        name: {"expected": expected, "actual": receipt.get(name)}
        for name, expected in expected_receipt_values.items()
        if str(receipt.get(name)) != str(expected)
    }
    if changed:
        raise RuntimeError(
            "Execution-coherence receipt identity changed: " + repr(changed)
        )
    for path, checksum in (
        (manifest_path, receipt.get("manifest_checksum_sha256")),
        (report_path, receipt.get("report_checksum_sha256")),
        (proof_path, receipt.get("proof_checksum_sha256")),
    ):
        _require_file_checksum(path, str(checksum), label="shadow proof artifact")
    manifest = _read_json(manifest_path)
    if (
        str(manifest.get("schema_version")) != MANIFEST_SCHEMA_VERSION
        or str(manifest.get("status")) != "COMPLETE_SHADOW_ONLY"
        or str(manifest.get("decision")) != str(receipt.get("decision"))
        or str(manifest.get("run_path")) != str(receipt.get("run_path"))
        or str(manifest.get("preregistration_id")) != PREREGISTRATION_ID
        or str(manifest.get("source_fingerprint_sha256"))
        != PREREGISTRATION_SHA256
    ):
        raise RuntimeError("Execution-coherence manifest identity changed")
    manifest_inputs = manifest.get("inputs")
    if not isinstance(manifest_inputs, Mapping):
        raise RuntimeError("Execution-coherence manifest inputs are missing")
    expected_manifest_inputs = {
        "source_set_sha256": EXPECTED_SOURCE_SET_SHA256,
        "gate_source_set_sha256": EXPECTED_GATE_SOURCE_SET_SHA256,
        "raw_cohort_sha256": EXPECTED_RAW_COHORT_SHA256,
        "exact_construction_cohort_sha256": EXPECTED_EXACT_COHORT_SHA256,
    }
    if any(
        str(manifest_inputs.get(name)) != expected
        for name, expected in expected_manifest_inputs.items()
    ):
        raise RuntimeError("Execution-coherence manifest input identity changed")
    outputs = manifest.get("outputs")
    if not isinstance(outputs, Mapping):
        raise RuntimeError("Execution-coherence manifest outputs are missing")
    for name, path in (
        ("report.json", report_path),
        ("transform-proof.parquet", proof_path),
    ):
        record = outputs.get(name)
        if (
            not isinstance(record, Mapping)
            or int(record.get("size", -1)) != path.stat().st_size
            or str(record.get("checksum_sha256")) != file_checksum(path)
        ):
            raise RuntimeError(
                f"Execution-coherence manifest output changed: {name}"
            )
    if int(outputs["transform-proof.parquet"].get("rows", -1)) != expected_rows:
        raise RuntimeError("Execution-coherence manifest proof row count changed")
    expected_proof_columns = {
        "TERMINAL_ALIAS_EQUALITY_ROLLBACK": list(_ALIAS_PROOF_COLUMNS),
        "TERMINAL_VALIDATION_OR_RUNTIME_ROLLBACK": [
            "error_type",
            "error",
            "source_fingerprint_sha256",
            "terminal_blocked",
        ],
        "COMPLETED_ADMISSIBILITY_TRANSFORM": list(_PROOF_COLUMNS),
    }[proof_kind]
    if list(outputs["transform-proof.parquet"].get("columns", ())) != (
        expected_proof_columns
    ):
        raise RuntimeError("Execution-coherence manifest proof schema changed")
    observed_proof = pd.read_parquet(proof_path)
    observed_proof_columns = list(observed_proof.columns)
    if observed_proof_columns != expected_proof_columns:
        raise RuntimeError("Execution-coherence Parquet proof schema changed")
    if len(observed_proof) != expected_rows:
        raise RuntimeError("Execution-coherence Parquet proof row count changed")
    forbidden_manifest_true = (
        "production_mutation",
        "production_authority_mutation",
        "orders_enabled",
    )
    if any(bool(manifest.get(name)) for name in forbidden_manifest_true):
        raise RuntimeError("Execution-coherence manifest violates isolation safety")
    if int(manifest.get("orders_placed", -1)) != 0:
        raise RuntimeError("Execution-coherence manifest violates zero-order safety")
    implementation_files = manifest_inputs.get("implementation_files")
    if not isinstance(implementation_files, list):
        raise RuntimeError("Execution-coherence implementation inventory is missing")
    implementation_by_path = {
        str(item.get("path")): str(item.get("checksum_sha256"))
        for item in implementation_files
        if isinstance(item, Mapping)
    }
    if (
        implementation_by_path.get(HARNESS_RELATIVE_PATH)
        != str(receipt.get("harness_checksum_sha256"))
        or implementation_by_path.get(FOCUSED_TEST_RELATIVE_PATH)
        != str(receipt.get("focused_test_checksum_sha256"))
    ):
        raise RuntimeError("Execution-coherence implementation binding changed")
    implementation = {
        HARNESS_RELATIVE_PATH: receipt.get("harness_checksum_sha256"),
        FOCUSED_TEST_RELATIVE_PATH: receipt.get("focused_test_checksum_sha256"),
    }
    for relative, checksum in implementation.items():
        _require_file_checksum(
            repo_root.joinpath(*relative.split("/")),
            str(checksum),
            label="receipt-bound implementation source",
        )
    forbidden_true = (
        "promotion_performed",
        "production_mutation",
        "production_candidate_mutation",
        "production_model_authority_mutation",
        "production_authority_mutation",
        "runtime_mutation",
        "ui_or_ranking_mutation",
        "orders_enabled",
    )
    if any(bool(receipt.get(name)) for name in forbidden_true):
        raise RuntimeError("Execution-coherence receipt violates isolation safety")
    if int(receipt.get("orders_placed", -1)) != 0:
        raise RuntimeError("Execution-coherence receipt violates zero-order safety")
    report = _read_json(report_path)
    if (
        str(report.get("schema_version")) != ABLATION_SCHEMA_VERSION
        or str(report.get("status")) != "COMPLETE_SHADOW_ONLY"
        or str(report.get("decision")) != str(receipt.get("decision"))
        or str(report.get("proof_kind")) != proof_kind
    ):
        raise RuntimeError("Execution-coherence report/receipt decision mismatch")
    if str(report.get("eligible_session")) != ELIGIBLE_SESSION:
        raise RuntimeError("Execution-coherence report eligible session changed")
    report_preregistration = report.get("preregistration")
    report_source = report.get("source")
    report_gates = report.get("checked_in_gates")
    report_safety = report.get("safety")
    if (
        not isinstance(report_preregistration, Mapping)
        or str(report_preregistration.get("id")) != PREREGISTRATION_ID
        or str(report_preregistration.get("sha256")) != PREREGISTRATION_SHA256
        or not isinstance(report_source, Mapping)
        or str(report_source.get("authority_generation")) != SOURCE_GENERATION
        or str(report_source.get("source_set_sha256"))
        != EXPECTED_SOURCE_SET_SHA256
        or not isinstance(report_gates, Mapping)
        or str(report_gates.get("gate_source_set_sha256"))
        != EXPECTED_GATE_SOURCE_SET_SHA256
        or not isinstance(report_safety, Mapping)
    ):
        raise RuntimeError("Execution-coherence report identity changed")
    forbidden_report_true = (
        "real_lockbox_opened",
        "opra_archive_rescan_performed",
        "fit_performed",
        "calibration_performed",
        "threshold_selection_performed",
        "ranking_performed",
        "account_or_portfolio_read_performed",
        "production_mutation",
        "production_candidate_mutation",
        "production_model_authority_mutation",
        "production_authority_mutation",
        "runtime_mutation",
        "ui_or_ranking_mutation",
        "promotion_performed",
        "orders_enabled",
    )
    if any(bool(report_safety.get(name)) for name in forbidden_report_true):
        raise RuntimeError("Execution-coherence report violates isolation safety")
    if int(report_safety.get("orders_placed", -1)) != 0:
        raise RuntimeError("Execution-coherence report violates zero-order safety")
    if proof_kind != "TERMINAL_VALIDATION_OR_RUNTIME_ROLLBACK":
        report_cohort = report.get("cohort")
        if (
            not isinstance(report_cohort, Mapping)
            or str(report_cohort.get("raw_cohort_sha256"))
            != EXPECTED_RAW_COHORT_SHA256
            or str(report_cohort.get("exact_construction_cohort_sha256"))
            != EXPECTED_EXACT_COHORT_SHA256
        ):
            raise RuntimeError("Execution-coherence report cohort identity changed")
    if proof_kind.startswith("TERMINAL_"):
        rollback = report.get("terminal_rollback")
        if (
            str(report.get("decision")) != "BLOCKED"
            or not isinstance(rollback, Mapping)
            or not bool(rollback.get("triggered"))
            or bool(rollback.get("retry_allowed", True))
            or bool(rollback.get("reinterpretation_allowed", True))
        ):
            raise RuntimeError("Execution-coherence terminal rollback changed")
    return ExecutionCoherenceAblationResult(
        status=status,
        decision=str(receipt["decision"]),
        directory=directory,
        report_path=report_path,
        proof_path=proof_path,
        manifest_path=manifest_path,
        receipt_path=receipt_path,
        report=report,
    )


def _aggregate_records(records: Sequence[str]) -> str:
    payload = "\n".join(sorted(str(record) for record in records))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _observed_inventory(
    base: Path,
    relative_paths: Sequence[str],
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for relative in relative_paths:
        normalized = Path(relative).as_posix()
        path = Path(base).joinpath(*normalized.split("/"))
        if path.is_file():
            records.append(
                {
                    "path": normalized,
                    "status": "PRESENT",
                    "size": path.stat().st_size,
                    "checksum_sha256": file_checksum(path),
                }
            )
        else:
            records.append({"path": normalized, "status": "MISSING"})
    return records


def _result_store_has_material(datastore_root: Path) -> bool:
    parent = (
        Path(datastore_root)
        / "ml"
        / "nightly-strategy-execution-coherence-ablation-runs"
    )
    return parent.is_dir() and any(parent.iterdir())


def _publish_terminal_exception_result(
    datastore_root: Path,
    *,
    repo_root: Path,
    error: Exception,
) -> ExecutionCoherenceAblationResult:
    root = Path(datastore_root).resolve()
    repository = Path(repo_root).resolve()
    implementation_inventory = _validate_implementation_sources(repository)
    source_paths = tuple(
        f"ml/strategy-profit-training-runs/{SOURCE_GENERATION}/{name}"
        for name in SOURCE_FILES
    )
    source_inventory = _observed_inventory(root, source_paths)
    gate_inventory = _observed_inventory(repository, GATE_SOURCE_FILES)
    created = utc_timestamp()
    error_type = type(error).__name__
    error_text = str(error)
    report = {
        "schema_version": ABLATION_SCHEMA_VERSION,
        "status": "COMPLETE_SHADOW_ONLY",
        "decision": "BLOCKED",
        "proof_kind": "TERMINAL_VALIDATION_OR_RUNTIME_ROLLBACK",
        "created_at": created.isoformat(),
        "eligible_session": ELIGIBLE_SESSION,
        "scope": "ISOLATED_ASSESSMENT_ONLY_EXECUTION_COHERENCE_PROOF",
        "preregistration": {
            "id": PREREGISTRATION_ID,
            "sha256": PREREGISTRATION_SHA256,
            "validation_completed": False,
        },
        "source": {
            "authority_generation": SOURCE_GENERATION,
            "source_set_sha256": EXPECTED_SOURCE_SET_SHA256,
            "source_set_verified": False,
            "source_files_observed": source_inventory,
        },
        "checked_in_gates": {
            "gate_source_set_sha256": EXPECTED_GATE_SOURCE_SET_SHA256,
            "gate_source_set_verified": False,
            "files_observed": gate_inventory,
        },
        "implementation": {"files": implementation_inventory},
        "failed_gates": {
            "GLOBAL": [f"validation_or_runtime_exception:{error_type}"]
        },
        "terminal_rollback": {
            "triggered": True,
            "condition": "validation_or_runtime_exception",
            "error_type": error_type,
            "error": error_text,
            "retry_allowed": False,
            "reinterpretation_allowed": False,
        },
        "transform": {
            "performed": False,
            "reason": "TERMINAL_PREREGISTERED_VALIDATION_OR_RUNTIME_FAILURE",
        },
        "safety": {
            "real_lockbox_opened": False,
            "opra_archive_rescan_performed": False,
            "fit_performed": False,
            "calibration_performed": False,
            "threshold_selection_performed": False,
            "ranking_performed": False,
            "outcome_projection_performed": False,
            "account_or_portfolio_read_performed": False,
            "production_mutation": False,
            "production_candidate_mutation": False,
            "production_model_authority_mutation": False,
            "production_authority_mutation": False,
            "runtime_mutation": False,
            "ui_or_ranking_mutation": False,
            "promotion_performed": False,
            "orders_enabled": False,
            "orders_placed": 0,
        },
        "next_gate": (
            "The preregistered result is terminal BLOCKED; do not retry or "
            "reinterpret this fingerprint."
        ),
    }
    proof = pd.DataFrame(
        [
            {
                "error_type": error_type,
                "error": error_text,
                "source_fingerprint_sha256": PREREGISTRATION_SHA256,
                "terminal_blocked": True,
            }
        ]
    )
    return _publish_result(
        root,
        repo_root=repository,
        created=created,
        report=report,
        proof=proof,
        source_inventory=source_inventory,
        gate_inventory=gate_inventory,
        implementation_inventory=implementation_inventory,
    )


def _canonical_scalar(value: object) -> str:
    if value is None or bool(pd.isna(value)):
        return "null"
    if isinstance(value, pd.Timestamp):
        return "timestamp:" + value.isoformat()
    if isinstance(value, (np.bool_, bool)):
        return "bool:" + ("true" if bool(value) else "false")
    if isinstance(value, (np.integer, int)):
        return "int:" + str(int(value))
    if isinstance(value, (np.floating, float)):
        return "float:" + float(value).hex()
    return "text:" + str(value)


def _require_probability_array(values: np.ndarray, *, label: str) -> None:
    if (
        values.ndim != 1
        or not np.isfinite(values).all()
        or np.any(values < 0.0)
        or np.any(values > 1.0)
    ):
        raise RuntimeError(f"{label} probabilities are not finite values in [0, 1]")


def _require_close(left: object, right: object, *, label: str) -> None:
    try:
        left_value = float(left)
        right_value = float(right)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label} is not numeric") from exc
    if not math.isclose(left_value, right_value, rel_tol=0.0, abs_tol=1e-12):
        raise RuntimeError(f"{label} changed: {left_value} != {right_value}")


def _safe_datastore_path(root: Path, value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Invalid {label} path")
    relative = Path(value)
    if relative.is_absolute():
        raise RuntimeError(f"{label} path must be relative")
    resolved = (root / relative).resolve()
    if root != resolved and root not in resolved.parents:
        raise RuntimeError(f"{label} path escapes datastore")
    return resolved


def _require_file_checksum(path: Path, expected: str, *, label: str) -> None:
    if not path.is_file():
        raise RuntimeError(f"{label} is missing: {path}")
    actual = file_checksum(path)
    if actual != expected:
        raise RuntimeError(f"{label} checksum mismatch: {path}")


def _read_json(path: Path) -> Mapping[str, object]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unreadable JSON evidence: {path}") from exc
    if not isinstance(payload, Mapping):
        raise RuntimeError(f"JSON evidence is not a mapping: {path}")
    return payload


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _compact_result(result: ExecutionCoherenceAblationResult) -> dict[str, object]:
    horizons = result.report.get("horizons")
    gates = {
        horizon: evidence.get("gate_status")
        for horizon, evidence in horizons.items()
        if isinstance(horizons, Mapping) and isinstance(evidence, Mapping)
    } if isinstance(horizons, Mapping) else {}
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "status": result.status,
        "decision": result.decision,
        "proof_kind": result.report.get("proof_kind"),
        "run_path": str(result.directory),
        "receipt_path": str(result.receipt_path),
        "receipt_checksum_sha256": file_checksum(result.receipt_path),
        "report_path": str(result.report_path),
        "proof_path": str(result.proof_path),
        "source_fingerprint_sha256": PREREGISTRATION_SHA256,
        "gate_status": gates,
        "failed_gates": result.report.get("failed_gates"),
        "terminal_rollback": result.report.get("terminal_rollback"),
        "promotion_performed": False,
        "production_mutation": False,
        "production_candidate_mutation": False,
        "production_model_authority_mutation": False,
        "production_authority_mutation": False,
        "runtime_mutation": False,
        "ui_or_ranking_mutation": False,
        "orders_enabled": False,
        "orders_placed": 0,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the preregistered Strategy execution-coherence shadow proof."
        )
    )
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path, default=None)
    datastore.add_argument(
        "--datastore-target",
        choices=tuple(DATASTORE_TARGETS),
        default="pc",
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    root = resolve_datastore_dir(
        root_dir=args.datastore,
        target=None if args.datastore is not None else args.datastore_target,
    )
    try:
        result = run_execution_coherence_ablation(
            root,
            repo_root=args.repo_root,
        )
    except Exception as exc:
        if _result_store_has_material(root):
            payload = {
                "schema_version": RECEIPT_SCHEMA_VERSION,
                "status": "BLOCKED",
                "decision": "BLOCKED",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "receipt_error_type": "EXISTING_RESULT_STORE_INTEGRITY_INCIDENT",
                "receipt_error": (
                    "Refusing to publish or reuse evidence while the content-addressed "
                    "result store contains conflicting, unreadable, or invalid material."
                ),
                "source_fingerprint_sha256": PREREGISTRATION_SHA256,
                "promotion_performed": False,
                "production_mutation": False,
                "production_candidate_mutation": False,
                "production_model_authority_mutation": False,
                "production_authority_mutation": False,
                "runtime_mutation": False,
                "ui_or_ranking_mutation": False,
                "orders_enabled": False,
                "orders_placed": 0,
            }
            print(
                json.dumps(payload, sort_keys=True, default=str)
                if args.compact
                else json.dumps(payload, indent=2, sort_keys=True, default=str)
            )
            return 2
        try:
            result = _publish_terminal_exception_result(
                root,
                repo_root=args.repo_root,
                error=exc,
            )
        except Exception as publish_exc:
            payload = {
                "schema_version": RECEIPT_SCHEMA_VERSION,
                "status": "BLOCKED",
                "decision": "BLOCKED",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "receipt_error_type": type(publish_exc).__name__,
                "receipt_error": str(publish_exc),
                "source_fingerprint_sha256": PREREGISTRATION_SHA256,
                "promotion_performed": False,
                "production_mutation": False,
                "production_candidate_mutation": False,
                "production_model_authority_mutation": False,
                "production_authority_mutation": False,
                "runtime_mutation": False,
                "ui_or_ranking_mutation": False,
                "orders_enabled": False,
                "orders_placed": 0,
            }
            print(
                json.dumps(payload, sort_keys=True, default=str)
                if args.compact
                else json.dumps(payload, indent=2, sort_keys=True, default=str)
            )
            return 2
        payload = _compact_result(result)
        print(
            json.dumps(payload, sort_keys=True, default=str)
            if args.compact
            else json.dumps(payload, indent=2, sort_keys=True, default=str)
        )
        return 0
    payload = _compact_result(result)
    print(
        json.dumps(payload, sort_keys=True, default=str)
        if args.compact
        else json.dumps(payload, indent=2, sort_keys=True, default=str)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
