from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import pandas as pd

from ml.artifacts import file_checksum, verify_manifest
from ml.sequence_encoder.contracts import (
    SEQUENCE_ENCODER_POLICY_VERSION,
    SEQUENCE_POINTER_SCHEMA_VERSION,
    SEQUENCE_PUBLICATION_SCHEMA_VERSION,
)


SEQUENCE_RECEIPT_NAME = "publication.json"


class SequencePublicationError(RuntimeError):
    """A sequence publication is missing, corrupt, or authority-incompatible."""


@dataclass(frozen=True)
class SequencePublication:
    run_directory: Path
    manifest: Mapping[str, object]
    receipt: Mapping[str, object]
    pointer: Mapping[str, object]


def sequence_pointer_path(datastore_root: Path) -> Path:
    return Path(datastore_root) / "ml" / "sequence-encoder-latest" / "run.json"


def publish_sequence_run(
    datastore_root: Path,
    *,
    run_directory: Path,
    published_at: object,
    source_loop_b: Mapping[str, object],
    mode: str = "SHADOW_ONLY",
) -> SequencePublication:
    clean_mode = str(mode).strip().upper()
    if clean_mode != "SHADOW_ONLY":
        raise SequencePublicationError(
            "The v1 sequence publisher supports SHADOW_ONLY authority only"
        )
    root = Path(datastore_root).resolve()
    run = Path(run_directory).resolve()
    if run.parent != (root / "ml" / "sequence-encoder-runs").resolve():
        raise SequencePublicationError(
            f"Sequence run is outside immutable sequence-encoder-runs: {run}"
        )
    manifest = verify_manifest(run)
    configuration = manifest.get("configuration")
    if not isinstance(configuration, Mapping):
        raise SequencePublicationError("Sequence manifest configuration is invalid")
    if (
        configuration.get("policy_version") != SEQUENCE_ENCODER_POLICY_VERSION
        or configuration.get("authority") != "SHADOW_ONLY"
        or configuration.get("orders_enabled") is not False
        or int(configuration.get("orders_placed", -1)) != 0
    ):
        raise SequencePublicationError("Sequence manifest violates its safety contract")
    manifest_source = configuration.get("source_loop_b")
    if not isinstance(manifest_source, Mapping) or dict(manifest_source) != dict(
        source_loop_b
    ):
        raise SequencePublicationError("Sequence source Loop B record is inconsistent")
    published = _utc(published_at, "published_at")
    receipt = {
        "schema_version": SEQUENCE_PUBLICATION_SCHEMA_VERSION,
        "run_path": run.relative_to(root).as_posix(),
        "run_timestamp": _utc(
            manifest.get("run_timestamp"), "manifest run_timestamp"
        ).isoformat(),
        "published_at": published.isoformat(),
        "manifest_checksum_sha256": file_checksum(run / "manifest.json"),
        "source_loop_b": dict(source_loop_b),
        "authority": "SHADOW_ONLY",
        "consumers": ["LOOP_B", "OPTIONS_STRATEGY", "LOOP_C_OBSERVE"],
        "safety": {
            "orders_enabled": False,
            "orders_placed": 0,
            "automated_action_allowed": False,
            "deterministic_risk_authority_required": True,
        },
    }
    receipt_path = run / SEQUENCE_RECEIPT_NAME
    _write_json_atomic(receipt_path, receipt)
    record = _record(receipt, receipt_path)
    pointer = {
        "schema_version": SEQUENCE_POINTER_SCHEMA_VERSION,
        "current": record,
    }
    _write_json_atomic(sequence_pointer_path(root), pointer)
    return read_current_sequence_publication(root)


def read_current_sequence_publication(datastore_root: Path) -> SequencePublication:
    root = Path(datastore_root).resolve()
    pointer_path = sequence_pointer_path(root)
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SequencePublicationError(
            f"Sequence pointer is unreadable: {pointer_path}"
        ) from exc
    if (
        not isinstance(pointer, Mapping)
        or pointer.get("schema_version") != SEQUENCE_POINTER_SCHEMA_VERSION
    ):
        raise SequencePublicationError(f"Sequence pointer is invalid: {pointer_path}")
    current = pointer.get("current")
    if not isinstance(current, Mapping):
        raise SequencePublicationError("Sequence pointer has no current record")
    raw_path = current.get("run_path")
    if not isinstance(raw_path, str) or not raw_path:
        raise SequencePublicationError("Sequence run_path is invalid")
    relative = Path(raw_path)
    if relative.is_absolute():
        raise SequencePublicationError("Sequence run_path must be relative")
    run = (root / relative).resolve()
    if run.parent != (root / "ml" / "sequence-encoder-runs").resolve():
        raise SequencePublicationError("Sequence pointer escapes immutable runs")
    manifest = verify_manifest(run)
    receipt_path = run / SEQUENCE_RECEIPT_NAME
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SequencePublicationError(
            f"Sequence receipt is unreadable: {receipt_path}"
        ) from exc
    if not isinstance(receipt, Mapping):
        raise SequencePublicationError("Sequence receipt is malformed")
    expected = _record(receipt, receipt_path)
    configuration = manifest.get("configuration")
    manifest_source = (
        configuration.get("source_loop_b")
        if isinstance(configuration, Mapping)
        else None
    )
    safety = receipt.get("safety")
    if (
        receipt.get("schema_version") != SEQUENCE_PUBLICATION_SCHEMA_VERSION
        or dict(current) != expected
        or receipt.get("run_path") != raw_path
        or receipt.get("manifest_checksum_sha256")
        != file_checksum(run / "manifest.json")
        or receipt.get("authority") != "SHADOW_ONLY"
        or not isinstance(manifest_source, Mapping)
        or dict(manifest_source) != dict(receipt.get("source_loop_b", {}))
        or not isinstance(safety, Mapping)
        or safety.get("orders_enabled") is not False
        or int(safety.get("orders_placed", -1)) != 0
        or safety.get("automated_action_allowed") is not False
    ):
        raise SequencePublicationError(
            f"Sequence pointer does not match its completed receipt: {run}"
        )
    return SequencePublication(run, manifest, receipt, pointer)


def resolve_current_sequence_output(datastore_root: Path, name: str) -> Path:
    publication = read_current_sequence_publication(datastore_root)
    outputs = publication.manifest.get("output_files")
    if not isinstance(outputs, Mapping) or name not in outputs:
        raise SequencePublicationError(
            f"Current sequence run did not publish {name}"
        )
    path = publication.run_directory / name
    if not path.is_file():
        raise SequencePublicationError(f"Current sequence output is missing: {path}")
    return path


def _record(
    receipt: Mapping[str, object],
    receipt_path: Path,
) -> dict[str, object]:
    return {
        "run_path": receipt.get("run_path"),
        "run_timestamp": receipt.get("run_timestamp"),
        "published_at": receipt.get("published_at"),
        "manifest_checksum_sha256": receipt.get("manifest_checksum_sha256"),
        "receipt_checksum_sha256": file_checksum(receipt_path),
    }


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
        raise SequencePublicationError(f"Invalid {label}")
    return pd.Timestamp(timestamp)


__all__ = [
    "SEQUENCE_RECEIPT_NAME",
    "SequencePublication",
    "SequencePublicationError",
    "publish_sequence_run",
    "read_current_sequence_publication",
    "resolve_current_sequence_output",
    "sequence_pointer_path",
]
