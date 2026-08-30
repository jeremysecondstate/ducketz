from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ml.artifacts import (
    canonical_metadata_json,
    file_checksum,
    verify_manifest,
    write_manifest,
)
from ml.current_publication import (
    PUBLICATION_CONTRACT_VERSION,
    authoritative_pointer_payload,
    publication_record,
)
from ml.scheduler_handoff import commit_handoff
from ml.sequence_encoder.contracts import SequenceEncoderConfig
from ml.sequence_encoder.preregistration import (
    SEQUENCE_PREREGISTRATION_BASELINE,
    SEQUENCE_PREREGISTRATION_HYPOTHESIS,
    SEQUENCE_PREREGISTRATION_PRIMARY_METRIC,
    SEQUENCE_PREREGISTRATION_RISKS,
    SEQUENCE_PREREGISTRATION_ROLLBACK_CONDITION,
    SEQUENCE_PREREGISTRATION_SAFETY_METRICS,
    SEQUENCE_PREREGISTRATION_SCHEMA_VERSION,
    SEQUENCE_PREREGISTRATION_STOP_CONDITIONS,
    claim_sequence_preregistration,
    validate_sequence_preregistration,
)
from ml.sequence_encoder.preregistration_proposal import (
    build_sequence_preregistration_proposal,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _stage_13_preregistration(root: Path) -> tuple[Path, dict[str, object]]:
    run = root / "ml" / "runs" / "20260901T083631.000000Z"
    run.mkdir(parents=True)
    samples = run / "samples.parquet"
    predictions = run / "predictions.parquet"
    samples.write_bytes(b"frozen samples")
    predictions.write_bytes(b"frozen predictions")
    cutoff = "2026-09-01T08:36:30+00:00"
    write_manifest(
        run,
        run_timestamp="2026-09-01T08:36:31Z",
        input_files=(),
        output_files=("samples.parquet", "predictions.parquet"),
        configuration={
            "causal_input_cutoff": cutoff,
            "publication_contract": {
                "version": PUBLICATION_CONTRACT_VERSION,
                "receipt": "publication.json",
                "required_for_live_evidence": True,
                "authority": "ml/latest/run.json",
            },
        },
        datastore_root=root,
    )
    manifest = verify_manifest(run)
    receipt = {
        "schema_version": PUBLICATION_CONTRACT_VERSION,
        "run_path": "ml/runs/20260901T083631.000000Z",
        "run_timestamp": "2026-09-01T08:36:31Z",
        "promoted_at": "2026-09-01T08:50:00Z",
        "manifest_checksum_sha256": file_checksum(run / "manifest.json"),
        "previous_publication": None,
    }
    _write_json(run / "publication.json", receipt)
    record = publication_record(
        run,
        manifest,
        receipt,
        datastore_root=root,
    )
    _write_json(
        root / "ml" / "latest" / "run.json",
        authoritative_pointer_payload(record),
    )

    canonical = {
        "schema_version": SEQUENCE_PREREGISTRATION_SCHEMA_VERSION,
        "challenger": "pooled-causal-sequence-encoder",
        "authority": "SHADOW_ONLY",
        "eligible_session": "2026-08-31",
        "source_loop_b_run_path": "ml/runs/20260901T083631.000000Z",
        "source_loop_b_manifest_sha256": file_checksum(run / "manifest.json"),
        "source_loop_b_samples_sha256": file_checksum(samples),
        "source_loop_b_predictions_sha256": file_checksum(predictions),
        "causal_input_cutoff": cutoff,
        "symbols": ["AAPL"],
        "maximum_sessions_per_symbol": 250,
        "configuration_fingerprint": SequenceEncoderConfig().semantic_fingerprint,
        "hypothesis": SEQUENCE_PREREGISTRATION_HYPOTHESIS,
        "primary_metric": SEQUENCE_PREREGISTRATION_PRIMARY_METRIC,
        "safety_metrics": list(SEQUENCE_PREREGISTRATION_SAFETY_METRICS),
        "baseline": SEQUENCE_PREREGISTRATION_BASELINE,
        "compute_bound": {
            "maximum_runs": 1,
            "maximum_sessions_per_symbol": 250,
            "ensemble_members": 3,
            "pretrain_epochs_per_member": 4,
            "supervised_epochs_per_member": 8,
        },
        "leakage_and_regime_risks": list(SEQUENCE_PREREGISTRATION_RISKS),
        "stop_conditions": list(SEQUENCE_PREREGISTRATION_STOP_CONDITIONS),
        "rollback_condition": SEQUENCE_PREREGISTRATION_ROLLBACK_CONDITION,
        "orders_enabled": False,
        "orders_placed": 0,
    }
    canonical_text = canonical_metadata_json(canonical)
    fingerprint = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    chunks = [canonical_text[index : index + 700] for index in range(0, len(canonical_text), 700)]
    actions = tuple(
        f"SEQUENCE_PREREG_CANONICAL_{index}_OF_{len(chunks)}={chunk}"
        for index, chunk in enumerate(chunks, start=1)
    ) + (f"SEQUENCE_PREREG_SHA256={fingerprint}",)
    committed = commit_handoff(
        root,
        wake_id="2026-09-01T08:42:00Z",
        monitor_mode="daily",
        lane="OVERNIGHT_ACCURACY",
        stage_id="select-nightly-bottleneck",
        stage_index=13,
        eligible_session="2026-08-31",
        checked_at="2026-09-01T08:42:00Z",
        final_status="HEALTHY",
        stage_disposition="PROPOSAL_ONLY",
        incident_status="NONE",
        summary="Preregistered the pooled sequence encoder.",
        next_action="Run the exact bounded stage-14 shadow experiment.",
        actions=actions,
        evidence_paths=(str(run / "manifest.json"),),
        created_at=datetime(2026, 9, 1, 8, 43, tzinfo=timezone.utc),
    )
    return Path(str(committed["receipt_path"])), canonical


def test_sequence_preregistration_binds_current_stage_and_source(
    tmp_path: Path,
) -> None:
    receipt, canonical = _stage_13_preregistration(tmp_path)

    registration = validate_sequence_preregistration(
        tmp_path,
        receipt_path=receipt,
        as_of="2026-09-01T09:42:00Z",
    )

    assert registration.canonical == canonical
    assert registration.symbols == ("AAPL",)
    assert registration.maximum_sessions_per_symbol == 250
    assert registration.handoff_sequence == 1
    registration.require_runtime_contract(
        symbols=("AAPL",),
        information_cutoff="2026-09-01T08:36:30Z",
        maximum_sessions_per_symbol=250,
        config=SequenceEncoderConfig(),
    )
    proposal = build_sequence_preregistration_proposal(
        tmp_path,
        eligible_session="2026-08-31",
        symbols=("AAPL",),
        maximum_sessions_per_symbol=250,
    )
    assert proposal["canonical"]["source_loop_b_run_path"] == (
        "ml/runs/20260901T083631.000000Z"
    )
    assert proposal["fingerprint_sha256"] == hashlib.sha256(
        str(proposal["canonical_json"]).encode("utf-8")
    ).hexdigest()
    assert proposal["handoff_actions"][-1].startswith(
        "SEQUENCE_PREREG_SHA256="
    )
    claim = claim_sequence_preregistration(
        tmp_path,
        preregistration=registration,
        claimed_at="2026-09-01T09:42:00Z",
    )
    assert json.loads(claim.read_text(encoding="utf-8"))["status"] == "CONSUMED_ONCE"
    with pytest.raises(ValueError, match="already been consumed"):
        claim_sequence_preregistration(
            tmp_path,
            preregistration=registration,
            claimed_at="2026-09-01T09:43:00Z",
        )


def test_sequence_preregistration_expires_when_scheduler_advances(
    tmp_path: Path,
) -> None:
    receipt, _ = _stage_13_preregistration(tmp_path)
    commit_handoff(
        tmp_path,
        wake_id="2026-09-01T09:42:00Z",
        monitor_mode="daily",
        lane="OVERNIGHT_ACCURACY",
        stage_id="run-shadow-ablation",
        stage_index=14,
        eligible_session="2026-08-31",
        checked_at="2026-09-01T09:42:00Z",
        final_status="HEALTHY",
        stage_disposition="COMPLETED_EVIDENCE",
        incident_status="NONE",
        summary="Stage 14 completed.",
        next_action="Compare the immutable challenger.",
        created_at=datetime(2026, 9, 1, 9, 43, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError, match="must be the current scheduler handoff"):
        validate_sequence_preregistration(
            tmp_path,
            receipt_path=receipt,
            as_of="2026-09-01T10:00:00Z",
        )
