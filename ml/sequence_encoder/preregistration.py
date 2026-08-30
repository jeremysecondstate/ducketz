from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

from ml.artifacts import canonical_metadata_json, file_checksum, utc_timestamp
from ml.current_publication import read_current_publication
from ml.scheduler_handoff import read_current_handoff
from ml.sequence_encoder.contracts import SequenceEncoderConfig


SEQUENCE_PREREGISTRATION_SCHEMA_VERSION = (
    "pooled-causal-sequence-preregistration-v1"
)
SEQUENCE_PREREGISTRATION_CHALLENGER = "pooled-causal-sequence-encoder"
SEQUENCE_PREREGISTRATION_CLAIM_SCHEMA_VERSION = (
    "pooled-causal-sequence-preregistration-claim-v1"
)
SEQUENCE_PREREGISTRATION_HYPOTHESIS = (
    "A pooled causal hourly stock-and-option sequence representation improves "
    "chronological horizon-macro log loss versus current Loop B without "
    "degrading the declared calibration and safety metrics."
)
SEQUENCE_PREREGISTRATION_PRIMARY_METRIC = "assessment_horizon_macro_log_loss"
SEQUENCE_PREREGISTRATION_SAFETY_METRICS = (
    "assessment_horizon_macro_brier",
    "assessment_horizon_calibration_error",
    "assessment_horizon_return_interval_coverage",
    "symbol_and_regime_stability",
    "orders_placed_equals_zero",
)
SEQUENCE_PREREGISTRATION_BASELINE = (
    "current receipt-verified Loop B horizon probabilities"
)
SEQUENCE_PREREGISTRATION_RISKS = (
    "shared market decision clusters across symbols",
    "overlapping daily and weekly target windows",
    "option-surface coverage and missingness regime shifts",
    "timestamp availability or session-boundary leakage",
)
SEQUENCE_PREREGISTRATION_STOP_CONDITIONS = (
    "source authority or checksum drift",
    "causal or chronological partition violation",
    "insufficient preregistered horizon partitions",
    "non-finite training, calibration, or assessment output",
    "runtime exceeds the scheduler's bounded stage-14 window",
)
SEQUENCE_PREREGISTRATION_ROLLBACK_CONDITION = (
    "Retain all current Loop B, Options Strategy, and order authorities; do not "
    "publish the shadow pointer if any integrity or safety gate fails."
)
_FRAGMENT_PATTERN = re.compile(
    r"^SEQUENCE_PREREG_CANONICAL_(\d+)_OF_(\d+)=(.*)$",
    re.DOTALL,
)
_FINGERPRINT_PREFIX = "SEQUENCE_PREREG_SHA256="
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class SequencePreregistration:
    """Receipt-bound stage-13 contract for one exact stage-14 run."""

    receipt_path: Path
    receipt_sha256: str
    handoff_sequence: int
    eligible_session: str
    fingerprint_sha256: str
    canonical: Mapping[str, object]
    source_run_directory: Path
    source_files: tuple[Path, ...]
    symbols: tuple[str, ...]
    information_cutoff: pd.Timestamp
    maximum_sessions_per_symbol: int

    def require_runtime_contract(
        self,
        *,
        symbols: Sequence[str],
        information_cutoff: object,
        maximum_sessions_per_symbol: int,
        config: SequenceEncoderConfig,
    ) -> None:
        selected = tuple(
            dict.fromkeys(str(value).strip().upper() for value in symbols)
        )
        if selected != self.symbols:
            raise ValueError(
                "Runtime symbols differ from the stage-13 preregistration"
            )
        if utc_timestamp(information_cutoff) != self.information_cutoff:
            raise ValueError(
                "Runtime information cutoff differs from the stage-13 preregistration"
            )
        if int(maximum_sessions_per_symbol) != self.maximum_sessions_per_symbol:
            raise ValueError(
                "Runtime data bound differs from the stage-13 preregistration"
            )
        if (
            str(self.canonical.get("configuration_fingerprint"))
            != config.semantic_fingerprint
        ):
            raise ValueError(
                "Runtime encoder configuration differs from the stage-13 preregistration"
            )


def validate_sequence_preregistration(
    datastore_root: Path,
    *,
    receipt_path: Path,
    config: SequenceEncoderConfig | None = None,
    as_of: object | None = None,
) -> SequencePreregistration:
    """Validate the current immutable handoff and its canonical experiment.

    The current handoff must itself be the stage-13 receipt. This intentionally
    makes a stale preregistration unusable after the scheduler advances to a
    later stage or a new eligible session.
    """

    root = Path(datastore_root).resolve()
    runtime = config or SequenceEncoderConfig()
    handoff = read_current_handoff(root)
    if handoff.get("status") != "VALID":
        raise ValueError(
            "A checksum-valid current scheduler handoff is required for training"
        )
    current_receipt = Path(str(handoff["receipt_path"])).resolve()
    supplied_receipt = Path(receipt_path).resolve()
    if supplied_receipt != current_receipt:
        raise ValueError(
            "The sequence preregistration must be the current scheduler handoff"
        )
    payload = handoff.get("handoff")
    if not isinstance(payload, Mapping):
        raise ValueError("The validated scheduler handoff payload is missing")
    schedule = payload.get("schedule")
    outcome = payload.get("outcome")
    safety = payload.get("safety")
    continuity = payload.get("continuity")
    if not all(
        isinstance(value, Mapping)
        for value in (schedule, outcome, safety, continuity)
    ):
        raise ValueError("The stage-13 handoff contract is incomplete")
    assert isinstance(schedule, Mapping)
    assert isinstance(outcome, Mapping)
    assert isinstance(safety, Mapping)
    assert isinstance(continuity, Mapping)
    if str(schedule.get("lane")) != "OVERNIGHT_ACCURACY":
        raise ValueError("Sequence training requires the overnight accuracy lane")
    if str(schedule.get("stage_id")) != "select-nightly-bottleneck":
        raise ValueError("Sequence training requires a stage-13 preregistration")
    if str(outcome.get("stage_disposition")) != "PROPOSAL_ONLY":
        raise ValueError("Stage 13 did not finish with PROPOSAL_ONLY disposition")
    if (
        str(safety.get("authority")) != "ADVISORY_HANDOFF_ONLY"
        or int(safety.get("orders_placed", -1)) != 0
    ):
        raise ValueError("The stage-13 handoff violates the zero-order boundary")
    raw_created_at = payload.get("created_at")
    if raw_created_at is None or not str(raw_created_at).strip():
        raise ValueError("The stage-13 handoff created_at is missing")
    created_at = utc_timestamp(raw_created_at)
    current_time = utc_timestamp(as_of)
    if created_at > current_time:
        raise ValueError("The stage-13 preregistration is future-dated")

    actions = continuity.get("actions")
    if not isinstance(actions, list):
        raise ValueError("The stage-13 preregistration fragments are missing")
    canonical_text, declared_fingerprint = _canonical_from_actions(actions)
    fingerprint = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    if fingerprint != declared_fingerprint:
        raise ValueError("The stage-13 preregistration fingerprint does not verify")
    try:
        canonical = json.loads(canonical_text)
    except json.JSONDecodeError as exc:
        raise ValueError("The stage-13 canonical preregistration is not JSON") from exc
    if not isinstance(canonical, Mapping):
        raise ValueError("The stage-13 canonical preregistration must be an object")
    if canonical_metadata_json(canonical) != canonical_text:
        raise ValueError("The stage-13 preregistration JSON is not canonical")

    eligible_session = str(schedule.get("eligible_session", "")).strip()
    try:
        eligible_session = date.fromisoformat(eligible_session).isoformat()
    except ValueError as exc:
        raise ValueError("The stage-13 eligible session is invalid") from exc
    _validate_canonical_contract(
        canonical,
        eligible_session=eligible_session,
        config=runtime,
    )
    source_run, source_files = _validate_frozen_source(root, canonical)
    symbols = tuple(str(value) for value in canonical["symbols"])
    cutoff = utc_timestamp(canonical["causal_input_cutoff"])
    maximum_sessions = int(canonical["maximum_sessions_per_symbol"])
    return SequencePreregistration(
        receipt_path=current_receipt,
        receipt_sha256=file_checksum(current_receipt),
        handoff_sequence=int(payload["sequence"]),
        eligible_session=eligible_session,
        fingerprint_sha256=fingerprint,
        canonical=dict(canonical),
        source_run_directory=source_run,
        source_files=source_files,
        symbols=symbols,
        information_cutoff=cutoff,
        maximum_sessions_per_symbol=maximum_sessions,
    )


def claim_sequence_preregistration(
    datastore_root: Path,
    *,
    preregistration: SequencePreregistration,
    claimed_at: object,
) -> Path:
    """Atomically consume a preregistration before any fit begins.

    A failed or interrupted experiment remains consumed. This preserves the
    scheduler's one-experiment, no-retry boundary and forces explicit review of
    partial evidence instead of silently trying again against the same cohort.
    """

    root = Path(datastore_root).resolve()
    directory = root / "ml" / "sequence-encoder-preregistrations"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{preregistration.fingerprint_sha256}.json"
    try:
        receipt_relative = preregistration.receipt_path.resolve().relative_to(root)
    except ValueError as exc:
        raise ValueError("Sequence preregistration receipt escapes the datastore") from exc
    payload = {
        "schema_version": SEQUENCE_PREREGISTRATION_CLAIM_SCHEMA_VERSION,
        "status": "CONSUMED_ONCE",
        "claimed_at": utc_timestamp(claimed_at).isoformat(),
        "eligible_session": preregistration.eligible_session,
        "fingerprint_sha256": preregistration.fingerprint_sha256,
        "handoff_sequence": preregistration.handoff_sequence,
        "handoff_receipt_path": receipt_relative.as_posix(),
        "handoff_receipt_sha256": preregistration.receipt_sha256,
        "authority": "SHADOW_ONLY",
        "orders_enabled": False,
        "orders_placed": 0,
    }
    data = (
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    ).encode("utf-8")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError(
            "The stage-13 sequence preregistration has already been consumed"
        ) from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return path


def _canonical_from_actions(actions: Sequence[object]) -> tuple[str, str]:
    fragments: dict[int, str] = {}
    declared_count: int | None = None
    declared_fingerprint: str | None = None
    for raw in actions:
        action = str(raw)
        match = _FRAGMENT_PATTERN.fullmatch(action)
        if match:
            index, count, value = int(match[1]), int(match[2]), match[3]
            if declared_count is not None and count != declared_count:
                raise ValueError("Sequence preregistration fragment counts disagree")
            if index in fragments:
                raise ValueError("Sequence preregistration repeats a fragment")
            declared_count = count
            fragments[index] = value
        elif action.startswith(_FINGERPRINT_PREFIX):
            if declared_fingerprint is not None:
                raise ValueError("Sequence preregistration repeats its fingerprint")
            declared_fingerprint = action.removeprefix(_FINGERPRINT_PREFIX).lower()
    if declared_count is None or declared_count < 1 or declared_count > 32:
        raise ValueError("Sequence preregistration fragment count is invalid")
    if sorted(fragments) != list(range(1, declared_count + 1)):
        raise ValueError("Sequence preregistration fragments are incomplete")
    if declared_fingerprint is None or not _SHA256_PATTERN.fullmatch(
        declared_fingerprint
    ):
        raise ValueError("Sequence preregistration fingerprint is missing or invalid")
    return (
        "".join(fragments[index] for index in range(1, declared_count + 1)),
        declared_fingerprint,
    )


def _validate_canonical_contract(
    canonical: Mapping[str, object],
    *,
    eligible_session: str,
    config: SequenceEncoderConfig,
) -> None:
    required = {
        "schema_version",
        "challenger",
        "authority",
        "eligible_session",
        "source_loop_b_run_path",
        "source_loop_b_manifest_sha256",
        "source_loop_b_samples_sha256",
        "source_loop_b_predictions_sha256",
        "causal_input_cutoff",
        "symbols",
        "maximum_sessions_per_symbol",
        "configuration_fingerprint",
        "hypothesis",
        "primary_metric",
        "safety_metrics",
        "baseline",
        "compute_bound",
        "leakage_and_regime_risks",
        "stop_conditions",
        "rollback_condition",
        "orders_enabled",
        "orders_placed",
    }
    if set(canonical) != required:
        missing = sorted(required.difference(canonical))
        extra = sorted(set(canonical).difference(required))
        raise ValueError(
            f"Sequence preregistration fields differ; missing={missing}, extra={extra}"
        )
    if canonical["schema_version"] != SEQUENCE_PREREGISTRATION_SCHEMA_VERSION:
        raise ValueError("Unsupported sequence preregistration schema")
    if canonical["challenger"] != SEQUENCE_PREREGISTRATION_CHALLENGER:
        raise ValueError("Stage 13 selected a different challenger")
    if canonical["authority"] != "SHADOW_ONLY":
        raise ValueError("Sequence preregistration must remain SHADOW_ONLY")
    if str(canonical["eligible_session"]) != eligible_session or not eligible_session:
        raise ValueError("Sequence preregistration eligible session changed")
    if canonical["configuration_fingerprint"] != config.semantic_fingerprint:
        raise ValueError("Sequence preregistration configuration fingerprint changed")
    if (
        canonical["orders_enabled"] is not False
        or isinstance(canonical["orders_placed"], bool)
        or canonical["orders_placed"] != 0
    ):
        raise ValueError("Sequence preregistration violates zero-order safety")
    symbols = canonical["symbols"]
    if not isinstance(symbols, list) or not symbols:
        raise ValueError("Sequence preregistration symbols must be a non-empty list")
    normalized = [str(value).strip().upper() for value in symbols]
    if (
        symbols != normalized
        or normalized != list(dict.fromkeys(normalized))
        or any(not value for value in normalized)
    ):
        raise ValueError("Sequence preregistration symbols are not normalized and unique")
    maximum_sessions = canonical["maximum_sessions_per_symbol"]
    if (
        isinstance(maximum_sessions, bool)
        or not isinstance(maximum_sessions, int)
        or maximum_sessions < 1
    ):
        raise ValueError("Sequence preregistration data bound must be positive")
    if canonical["causal_input_cutoff"] is None or not str(
        canonical["causal_input_cutoff"]
    ).strip():
        raise ValueError("Sequence preregistration causal cutoff is empty")
    utc_timestamp(canonical["causal_input_cutoff"])
    for field in (
        "hypothesis",
        "primary_metric",
        "baseline",
        "rollback_condition",
    ):
        if not str(canonical[field]).strip():
            raise ValueError(f"Sequence preregistration {field} is empty")
    for field in (
        "safety_metrics",
        "leakage_and_regime_risks",
        "stop_conditions",
    ):
        value = canonical[field]
        if not isinstance(value, list) or not value or any(
            not str(item).strip() for item in value
        ):
            raise ValueError(f"Sequence preregistration {field} is empty")
    fixed_policy = {
        "hypothesis": SEQUENCE_PREREGISTRATION_HYPOTHESIS,
        "primary_metric": SEQUENCE_PREREGISTRATION_PRIMARY_METRIC,
        "safety_metrics": list(SEQUENCE_PREREGISTRATION_SAFETY_METRICS),
        "baseline": SEQUENCE_PREREGISTRATION_BASELINE,
        "leakage_and_regime_risks": list(SEQUENCE_PREREGISTRATION_RISKS),
        "stop_conditions": list(SEQUENCE_PREREGISTRATION_STOP_CONDITIONS),
        "rollback_condition": SEQUENCE_PREREGISTRATION_ROLLBACK_CONDITION,
    }
    for field, expected_value in fixed_policy.items():
        if canonical[field] != expected_value:
            raise ValueError(f"Sequence preregistration {field} changed")
    compute = canonical["compute_bound"]
    if not isinstance(compute, Mapping) or not compute:
        raise ValueError("Sequence preregistration compute bound is empty")
    expected_compute = {
        "maximum_runs": 1,
        "maximum_sessions_per_symbol": int(maximum_sessions),
        "ensemble_members": config.ensemble_size,
        "pretrain_epochs_per_member": config.pretrain_epochs,
        "supervised_epochs_per_member": config.supervised_epochs,
    }
    if dict(compute) != expected_compute:
        raise ValueError("Sequence preregistration compute bound changed")
    for field in (
        "source_loop_b_manifest_sha256",
        "source_loop_b_samples_sha256",
        "source_loop_b_predictions_sha256",
    ):
        if not _SHA256_PATTERN.fullmatch(str(canonical[field])):
            raise ValueError(f"Sequence preregistration {field} is invalid")


def _validate_frozen_source(
    root: Path,
    canonical: Mapping[str, object],
) -> tuple[Path, tuple[Path, ...]]:
    relative = Path(str(canonical["source_loop_b_run_path"]))
    run = (root / relative).resolve()
    expected_parent = (root / "ml" / "runs").resolve()
    if relative.is_absolute() or run.parent != expected_parent:
        raise ValueError("Preregistered Loop B run is outside immutable ml/runs")
    manifest = run / "manifest.json"
    samples = run / "samples.parquet"
    predictions = run / "predictions.parquet"
    expected = {
        manifest: str(canonical["source_loop_b_manifest_sha256"]),
        samples: str(canonical["source_loop_b_samples_sha256"]),
        predictions: str(canonical["source_loop_b_predictions_sha256"]),
    }
    for path, checksum in expected.items():
        if not path.is_file() or file_checksum(path) != checksum:
            raise ValueError(f"Preregistered Loop B source changed: {path.name}")
    publication = read_current_publication(root)
    current = publication.pointer.get("current")
    if not isinstance(current, Mapping) or str(current.get("run_path")) != relative.as_posix():
        raise ValueError("Preregistered Loop B run is no longer current authority")
    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    configuration = manifest_payload.get("configuration")
    if not isinstance(configuration, Mapping):
        raise ValueError("Preregistered Loop B manifest configuration is missing")
    if utc_timestamp(configuration.get("causal_input_cutoff")) != utc_timestamp(
        canonical["causal_input_cutoff"]
    ):
        raise ValueError("Preregistered Loop B causal cutoff changed")
    return run, (manifest, samples, predictions)


__all__ = [
    "SEQUENCE_PREREGISTRATION_BASELINE",
    "SEQUENCE_PREREGISTRATION_CHALLENGER",
    "SEQUENCE_PREREGISTRATION_CLAIM_SCHEMA_VERSION",
    "SEQUENCE_PREREGISTRATION_HYPOTHESIS",
    "SEQUENCE_PREREGISTRATION_PRIMARY_METRIC",
    "SEQUENCE_PREREGISTRATION_RISKS",
    "SEQUENCE_PREREGISTRATION_ROLLBACK_CONDITION",
    "SEQUENCE_PREREGISTRATION_SAFETY_METRICS",
    "SEQUENCE_PREREGISTRATION_SCHEMA_VERSION",
    "SEQUENCE_PREREGISTRATION_STOP_CONDITIONS",
    "SequencePreregistration",
    "claim_sequence_preregistration",
    "validate_sequence_preregistration",
]
