from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Mapping, Sequence

from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from ml.artifacts import canonical_metadata_json, file_checksum
from ml.current_publication import read_current_publication
from ml.sequence_encoder.contracts import SequenceEncoderConfig
from ml.sequence_encoder.preregistration import (
    SEQUENCE_PREREGISTRATION_BASELINE,
    SEQUENCE_PREREGISTRATION_CHALLENGER,
    SEQUENCE_PREREGISTRATION_HYPOTHESIS,
    SEQUENCE_PREREGISTRATION_PRIMARY_METRIC,
    SEQUENCE_PREREGISTRATION_RISKS,
    SEQUENCE_PREREGISTRATION_ROLLBACK_CONDITION,
    SEQUENCE_PREREGISTRATION_SAFETY_METRICS,
    SEQUENCE_PREREGISTRATION_SCHEMA_VERSION,
    SEQUENCE_PREREGISTRATION_STOP_CONDITIONS,
)


def build_sequence_preregistration_proposal(
    datastore_root: Path,
    *,
    eligible_session: str,
    symbols: Sequence[str],
    maximum_sessions_per_symbol: int,
    config: SequenceEncoderConfig | None = None,
    fragment_size: int = 850,
) -> Mapping[str, object]:
    """Build canonical stage-13 actions without committing scheduler state."""

    root = Path(datastore_root).resolve()
    runtime = config or SequenceEncoderConfig()
    try:
        normalized_session = date.fromisoformat(str(eligible_session)).isoformat()
    except ValueError as exc:
        raise ValueError("eligible_session must be an ISO calendar date") from exc
    selected_symbols = tuple(
        dict.fromkeys(str(value).strip().upper() for value in symbols)
    )
    if not selected_symbols or any(not value for value in selected_symbols):
        raise ValueError("At least one normalized symbol is required")
    if isinstance(maximum_sessions_per_symbol, bool) or int(
        maximum_sessions_per_symbol
    ) < 1:
        raise ValueError("maximum_sessions_per_symbol must be positive")
    if fragment_size < 128 or fragment_size > 1_000:
        raise ValueError("fragment_size must be between 128 and 1000")

    publication = read_current_publication(root)
    current = publication.pointer.get("current")
    if not isinstance(current, Mapping):
        raise ValueError("A receipt-era current Loop B publication is required")
    run = publication.run_directory
    manifest = run / "manifest.json"
    samples = run / "samples.parquet"
    predictions = run / "predictions.parquet"
    if not samples.is_file() or not predictions.is_file():
        raise ValueError("Current Loop B samples or predictions are missing")
    configuration = publication.manifest.get("configuration")
    if not isinstance(configuration, Mapping):
        raise ValueError("Current Loop B manifest configuration is missing")
    cutoff = configuration.get("causal_input_cutoff")
    if cutoff is None or not str(cutoff).strip():
        raise ValueError("Current Loop B causal input cutoff is missing")

    canonical = {
        "schema_version": SEQUENCE_PREREGISTRATION_SCHEMA_VERSION,
        "challenger": SEQUENCE_PREREGISTRATION_CHALLENGER,
        "authority": "SHADOW_ONLY",
        "eligible_session": normalized_session,
        "source_loop_b_run_path": str(current["run_path"]),
        "source_loop_b_manifest_sha256": file_checksum(manifest),
        "source_loop_b_samples_sha256": file_checksum(samples),
        "source_loop_b_predictions_sha256": file_checksum(predictions),
        "causal_input_cutoff": str(cutoff),
        "symbols": list(selected_symbols),
        "maximum_sessions_per_symbol": int(maximum_sessions_per_symbol),
        "configuration_fingerprint": runtime.semantic_fingerprint,
        "hypothesis": SEQUENCE_PREREGISTRATION_HYPOTHESIS,
        "primary_metric": SEQUENCE_PREREGISTRATION_PRIMARY_METRIC,
        "safety_metrics": list(SEQUENCE_PREREGISTRATION_SAFETY_METRICS),
        "baseline": SEQUENCE_PREREGISTRATION_BASELINE,
        "compute_bound": {
            "maximum_runs": 1,
            "maximum_sessions_per_symbol": int(maximum_sessions_per_symbol),
            "ensemble_members": runtime.ensemble_size,
            "pretrain_epochs_per_member": runtime.pretrain_epochs,
            "supervised_epochs_per_member": runtime.supervised_epochs,
        },
        "leakage_and_regime_risks": list(SEQUENCE_PREREGISTRATION_RISKS),
        "stop_conditions": list(SEQUENCE_PREREGISTRATION_STOP_CONDITIONS),
        "rollback_condition": SEQUENCE_PREREGISTRATION_ROLLBACK_CONDITION,
        "orders_enabled": False,
        "orders_placed": 0,
    }
    canonical_text = canonical_metadata_json(canonical)
    fingerprint = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    fragments = [
        canonical_text[index : index + fragment_size]
        for index in range(0, len(canonical_text), fragment_size)
    ]
    if len(fragments) > 32:
        raise ValueError("Canonical preregistration exceeds the handoff fragment bound")
    actions = [
        f"SEQUENCE_PREREG_CANONICAL_{index}_OF_{len(fragments)}={fragment}"
        for index, fragment in enumerate(fragments, start=1)
    ]
    actions.append(f"SEQUENCE_PREREG_SHA256={fingerprint}")
    return {
        "schema_version": "pooled-causal-sequence-preregistration-proposal-v1",
        "status": "PROPOSAL_ONLY",
        "canonical": canonical,
        "canonical_json": canonical_text,
        "fingerprint_sha256": fingerprint,
        "handoff_actions": actions,
        "orders_enabled": False,
        "orders_placed": 0,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build receipt-ready pooled-sequence stage-13 handoff actions."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--root-dir", type=Path)
    group.add_argument("--datastore-target", choices=sorted(DATASTORE_TARGETS))
    parser.add_argument("--eligible-session", required=True)
    parser.add_argument("--symbol", action="append", required=True)
    parser.add_argument("--maximum-sessions-per-symbol", type=int, required=True)
    parser.add_argument("--compact", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = resolve_datastore_dir(
            root_dir=args.root_dir,
            target=args.datastore_target,
        )
        result = build_sequence_preregistration_proposal(
            root,
            eligible_session=args.eligible_session,
            symbols=args.symbol,
            maximum_sessions_per_symbol=args.maximum_sessions_per_symbol,
        )
        exit_code = 0
    except Exception as exc:
        result = {
            "status": "ERROR",
            "error": str(exc),
            "orders_enabled": False,
            "orders_placed": 0,
        }
        exit_code = 2
    print(
        json.dumps(
            result,
            separators=(",", ":") if args.compact else None,
            default=str,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_sequence_preregistration_proposal", "main"]
