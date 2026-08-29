from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import pandas as pd

from ml.artifacts import file_checksum, verify_manifest


LOOP_C_PUBLICATION_VERSION = "loop-c-observe-publication-v1"
LOOP_C_POINTER_VERSION = "loop-c-observe-pointer-v1"
LOOP_C_RECEIPT_NAME = "publication.json"


class LoopCPublicationError(RuntimeError):
    pass


@dataclass(frozen=True)
class LoopCPublication:
    run_directory: Path
    manifest: Mapping[str, object]
    receipt: Mapping[str, object]
    pointer: Mapping[str, object]


def loop_c_pointer_path(datastore_root: Path) -> Path:
    return Path(datastore_root) / "ml" / "loop-c-latest" / "run.json"


def publish_loop_c_observe_run(
    datastore_root: Path,
    *,
    run_directory: Path,
    published_at: object,
) -> LoopCPublication:
    root = Path(datastore_root).resolve()
    run = Path(run_directory).resolve()
    if run.parent != (root / "ml" / "loop-c-runs").resolve():
        raise LoopCPublicationError("Loop C run is outside immutable loop-c-runs")
    manifest = verify_manifest(run)
    configuration = manifest.get("configuration")
    if (
        not isinstance(configuration, Mapping)
        or configuration.get("authority") != "OBSERVE_ONLY"
        or configuration.get("orders_enabled") is not False
        or int(configuration.get("orders_placed", -1)) != 0
    ):
        raise LoopCPublicationError("Loop C manifest violates observe-only safety")
    receipt = {
        "schema_version": LOOP_C_PUBLICATION_VERSION,
        "run_path": run.relative_to(root).as_posix(),
        "run_timestamp": _utc(manifest.get("run_timestamp"), "run timestamp").isoformat(),
        "published_at": _utc(published_at, "published_at").isoformat(),
        "manifest_checksum_sha256": file_checksum(run / "manifest.json"),
        "authority": "OBSERVE_ONLY",
        "safety": {
            "orders_enabled": False,
            "orders_placed": 0,
            "broker_submission_path_present": False,
        },
    }
    receipt_path = run / LOOP_C_RECEIPT_NAME
    _write_json_atomic(receipt_path, receipt)
    pointer = {
        "schema_version": LOOP_C_POINTER_VERSION,
        "current": _record(receipt, receipt_path),
    }
    _write_json_atomic(loop_c_pointer_path(root), pointer)
    return read_current_loop_c_publication(root)


def read_current_loop_c_publication(datastore_root: Path) -> LoopCPublication:
    root = Path(datastore_root).resolve()
    pointer_path = loop_c_pointer_path(root)
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LoopCPublicationError(f"Loop C pointer is unreadable: {pointer_path}") from exc
    if (
        not isinstance(pointer, Mapping)
        or pointer.get("schema_version") != LOOP_C_POINTER_VERSION
        or not isinstance(pointer.get("current"), Mapping)
    ):
        raise LoopCPublicationError("Loop C pointer is invalid")
    current = pointer["current"]
    raw_path = current.get("run_path")
    if not isinstance(raw_path, str) or not raw_path:
        raise LoopCPublicationError("Loop C run_path is invalid")
    run = (root / raw_path).resolve()
    if Path(raw_path).is_absolute() or run.parent != (root / "ml" / "loop-c-runs").resolve():
        raise LoopCPublicationError("Loop C pointer escapes immutable runs")
    manifest = verify_manifest(run)
    receipt_path = run / LOOP_C_RECEIPT_NAME
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LoopCPublicationError("Loop C receipt is unreadable") from exc
    safety = receipt.get("safety") if isinstance(receipt, Mapping) else None
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("schema_version") != LOOP_C_PUBLICATION_VERSION
        or dict(current) != _record(receipt, receipt_path)
        or receipt.get("manifest_checksum_sha256") != file_checksum(run / "manifest.json")
        or receipt.get("authority") != "OBSERVE_ONLY"
        or not isinstance(safety, Mapping)
        or safety.get("orders_enabled") is not False
        or int(safety.get("orders_placed", -1)) != 0
        or safety.get("broker_submission_path_present") is not False
    ):
        raise LoopCPublicationError("Loop C pointer does not match its receipt")
    return LoopCPublication(run, manifest, receipt, pointer)


def _record(receipt: Mapping[str, object], path: Path) -> dict[str, object]:
    return {
        "run_path": receipt.get("run_path"),
        "run_timestamp": receipt.get("run_timestamp"),
        "published_at": receipt.get("published_at"),
        "manifest_checksum_sha256": receipt.get("manifest_checksum_sha256"),
        "receipt_checksum_sha256": file_checksum(path),
    }


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _utc(value: object, label: str) -> pd.Timestamp:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        raise LoopCPublicationError(f"Invalid {label}")
    return pd.Timestamp(parsed)


__all__ = [
    "LoopCPublication",
    "LoopCPublicationError",
    "loop_c_pointer_path",
    "publish_loop_c_observe_run",
    "read_current_loop_c_publication",
]
