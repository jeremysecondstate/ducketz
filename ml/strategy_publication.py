from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import pandas as pd

from ml.artifacts import file_checksum, verify_manifest
from ml.strategy_selection.contracts import (
    STRATEGY_CANDIDATE_SCHEMA_VERSION,
    STRATEGY_MODEL_POLICY_VERSION,
    STRATEGY_RANKING_POLICY_VERSION,
)


STRATEGY_PUBLICATION_VERSION = "strategy-publication-v2"
STRATEGY_POINTER_VERSION = "strategy-pointer-v2"
STRATEGY_RECEIPT_NAME = "publication.json"


class StrategyPublicationError(RuntimeError):
    """The authoritative Strategy publication is incomplete or inconsistent."""


@dataclass(frozen=True)
class StrategyPublication:
    run_directory: Path
    manifest: Mapping[str, object]
    receipt: Mapping[str, object]
    pointer: Mapping[str, object]


def strategy_pointer_path(datastore_root: Path) -> Path:
    return Path(datastore_root) / "ml" / "strategy-latest" / "run.json"


def publish_strategy_run(
    datastore_root: Path,
    *,
    run_directory: Path,
    source_loop_b: Mapping[str, object],
    published_at: object,
) -> StrategyPublication:
    root = Path(datastore_root).resolve()
    run = Path(run_directory).resolve()
    runs_root = (root / "ml" / "strategy-runs").resolve()
    if run.parent != runs_root:
        raise StrategyPublicationError(
            f"Strategy run is outside immutable strategy-runs: {run}"
        )
    manifest = verify_manifest(run)
    candidate_contract = _candidate_contract(manifest)
    published = _utc(published_at, "published_at")
    receipt = {
        "schema_version": STRATEGY_PUBLICATION_VERSION,
        "run_path": run.relative_to(root).as_posix(),
        "run_timestamp": _utc(
            manifest.get("run_timestamp"), "manifest run_timestamp"
        ).isoformat(),
        "published_at": published.isoformat(),
        "manifest_checksum_sha256": file_checksum(run / "manifest.json"),
        "source_loop_b": dict(source_loop_b),
        "candidate_contract": candidate_contract,
    }
    receipt_path = run / STRATEGY_RECEIPT_NAME
    _write_json_atomic(receipt_path, receipt)
    record = {
        "run_path": receipt["run_path"],
        "run_timestamp": receipt["run_timestamp"],
        "published_at": receipt["published_at"],
        "manifest_checksum_sha256": receipt["manifest_checksum_sha256"],
        "receipt_checksum_sha256": file_checksum(receipt_path),
    }
    pointer = {
        "schema_version": STRATEGY_POINTER_VERSION,
        "current": record,
    }
    _write_json_atomic(strategy_pointer_path(root), pointer)
    return read_current_strategy_publication(root)


def read_current_strategy_publication(datastore_root: Path) -> StrategyPublication:
    root = Path(datastore_root).resolve()
    pointer_path = strategy_pointer_path(root)
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StrategyPublicationError(
            f"Strategy pointer is unreadable: {pointer_path}"
        ) from exc
    if not isinstance(pointer, Mapping) or pointer.get("schema_version") != STRATEGY_POINTER_VERSION:
        raise StrategyPublicationError(f"Strategy pointer is invalid: {pointer_path}")
    current = pointer.get("current")
    if not isinstance(current, Mapping):
        raise StrategyPublicationError("Strategy pointer has no current record")
    raw_path = current.get("run_path")
    if not isinstance(raw_path, str) or not raw_path:
        raise StrategyPublicationError("Strategy pointer run_path is invalid")
    relative = Path(raw_path)
    if relative.is_absolute():
        raise StrategyPublicationError("Strategy run_path must be relative")
    run = (root / relative).resolve()
    if run.parent != (root / "ml" / "strategy-runs").resolve():
        raise StrategyPublicationError("Strategy pointer escapes strategy-runs")
    manifest = verify_manifest(run)
    receipt_path = run / STRATEGY_RECEIPT_NAME
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StrategyPublicationError(
            f"Strategy receipt is unreadable: {receipt_path}"
        ) from exc
    if not isinstance(receipt, Mapping):
        raise StrategyPublicationError("Strategy receipt is malformed")
    expected = {
        "run_path": raw_path,
        "run_timestamp": receipt.get("run_timestamp"),
        "published_at": receipt.get("published_at"),
        "manifest_checksum_sha256": file_checksum(run / "manifest.json"),
        "receipt_checksum_sha256": file_checksum(receipt_path),
    }
    configuration = manifest.get("configuration")
    manifest_source = (
        configuration.get("source_loop_b")
        if isinstance(configuration, Mapping)
        else None
    )
    receipt_source = receipt.get("source_loop_b")
    manifest_candidate_contract = _candidate_contract(manifest)
    receipt_candidate_contract = receipt.get("candidate_contract")
    if (
        receipt.get("schema_version") != STRATEGY_PUBLICATION_VERSION
        or receipt.get("run_path") != raw_path
        or dict(current) != expected
        or receipt.get("manifest_checksum_sha256")
        != file_checksum(run / "manifest.json")
        or not isinstance(manifest_source, Mapping)
        or not isinstance(receipt_source, Mapping)
        or dict(receipt_source) != dict(manifest_source)
        or not isinstance(receipt_candidate_contract, Mapping)
        or dict(receipt_candidate_contract) != manifest_candidate_contract
    ):
        raise StrategyPublicationError(
            f"Strategy pointer does not match its completed receipt: {run}"
        )
    return StrategyPublication(run, manifest, receipt, pointer)


def _candidate_contract(manifest: Mapping[str, object]) -> dict[str, object]:
    configuration = manifest.get("configuration")
    observed = (
        configuration.get("strategy_candidate_contract")
        if isinstance(configuration, Mapping)
        else None
    )
    expected: dict[str, object] = {
        "schema_version": STRATEGY_CANDIDATE_SCHEMA_VERSION,
        "model_policy_version": STRATEGY_MODEL_POLICY_VERSION,
        "ranking_policy_version": STRATEGY_RANKING_POLICY_VERSION,
        "decision_score": "profitable_outcome_probability",
        "fitted_score_basis": "CALIBRATED_MODEL",
        "fallback_score_basis": "SCENARIO_PRIOR",
    }
    if not isinstance(observed, Mapping) or dict(observed) != expected:
        raise StrategyPublicationError(
            "Strategy manifest candidate score contract is incompatible"
        )
    return expected


def resolve_current_strategy_output(datastore_root: Path, name: str) -> Path:
    publication = read_current_strategy_publication(datastore_root)
    outputs = publication.manifest.get("output_files")
    if not isinstance(outputs, Mapping) or name not in outputs:
        raise StrategyPublicationError(
            f"Current Strategy run did not publish {name}"
        )
    path = publication.run_directory / name
    if not path.is_file():
        raise StrategyPublicationError(f"Current Strategy output is missing: {path}")
    return path


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + f".tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def _utc(value: object, label: str) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise StrategyPublicationError(f"Invalid {label}")
    return pd.Timestamp(timestamp)


__all__ = [
    "STRATEGY_POINTER_VERSION",
    "STRATEGY_PUBLICATION_VERSION",
    "StrategyPublication",
    "StrategyPublicationError",
    "publish_strategy_run",
    "read_current_strategy_publication",
    "resolve_current_strategy_output",
    "strategy_pointer_path",
]
