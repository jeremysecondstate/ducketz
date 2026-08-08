from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd


def canonical_metadata_json(metadata: Mapping[str, object]) -> str:
    """Serialize semantic manifest metadata with deterministic key ordering."""

    return json.dumps(
        dict(metadata),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )


def semantic_metadata_fingerprint(metadata: Mapping[str, object]) -> str:
    """Fingerprint semantic metadata for compatibility, never as a model value."""

    return hashlib.sha256(canonical_metadata_json(metadata).encode("utf-8")).hexdigest()


def utc_timestamp(value: object | None = None) -> pd.Timestamp:
    timestamp = pd.Timestamp(
        value if value is not None else datetime.now(timezone.utc)
    )
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def timestamp_directory_name(value: object | None = None) -> str:
    return utc_timestamp(value).strftime("%Y%m%dT%H%M%S.%fZ")


def create_timestamp_directory(
    root: Path,
    *,
    timestamp: object | None = None,
) -> Path:
    """Create a readable timestamp directory, adding a numeric suffix on collision."""

    parent = Path(root)
    parent.mkdir(parents=True, exist_ok=True)
    base = timestamp_directory_name(timestamp)
    candidate = parent / base
    suffix = 2
    while candidate.exists():
        candidate = parent / f"{base}-{suffix}"
        suffix += 1
    candidate.mkdir()
    return candidate


def file_checksum(path: Path) -> str:
    """Return a SHA-256 checksum used only to verify file integrity."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_inventory(
    directory: Path,
    names: Sequence[str],
) -> dict[str, dict[str, object]]:
    root = Path(directory)
    inventory: dict[str, dict[str, object]] = {}
    for name in names:
        path = root / name
        inventory[name] = {
            "size": path.stat().st_size,
            "checksum_sha256": file_checksum(path),
        }
    return inventory


def input_inventory(
    paths: Sequence[Path],
    *,
    relative_to: Path | None = None,
) -> list[dict[str, object]]:
    root = Path(relative_to).resolve() if relative_to is not None else None
    records: list[dict[str, object]] = []
    for raw_path in dict.fromkeys(Path(path) for path in paths):
        path = raw_path.resolve()
        try:
            rendered = str(path.relative_to(root)) if root is not None else str(path)
        except ValueError:
            rendered = str(path)
        if not path.is_file():
            records.append({"path": rendered, "status": "missing"})
            continue
        stat = path.stat()
        records.append(
            {
                "path": rendered,
                "status": "present",
                "size": stat.st_size,
                "modified_time_ns": stat.st_mtime_ns,
                "checksum_sha256": file_checksum(path),
            }
        )
    return records


def write_manifest(
    directory: Path,
    *,
    run_timestamp: object,
    input_files: Sequence[Path],
    output_files: Sequence[str],
    model_name: str | None = None,
    feature_columns: Sequence[str] = (),
    target_column: str | None = None,
    configuration: Mapping[str, object] | None = None,
    datastore_root: Path | None = None,
) -> Path:
    """Write a small readable manifest without artifact or lineage identities."""

    root = Path(directory)
    payload: dict[str, object] = {
        "run_timestamp": utc_timestamp(run_timestamp).isoformat(),
        "input_files": input_inventory(
            input_files,
            relative_to=datastore_root,
        ),
        "output_files": file_inventory(root, output_files),
        "feature_columns": list(feature_columns),
        "configuration": dict(configuration or {}),
    }
    if model_name:
        payload["model_name"] = model_name
    if target_column:
        payload["target_column"] = target_column
    path = root / "manifest.json"
    _write_json_atomic(path, payload)
    return path


def verify_manifest(directory: Path) -> dict[str, object]:
    """Verify only the file-integrity metadata recorded in a manifest."""

    root = Path(directory)
    path = root / "manifest.json"
    if not path.is_file():
        raise RuntimeError(f"Manifest is missing: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    outputs = payload.get("output_files")
    if not isinstance(outputs, Mapping):
        raise RuntimeError(f"Manifest output inventory is invalid: {path}")
    for raw_name, raw_metadata in outputs.items():
        name = str(raw_name)
        metadata = raw_metadata if isinstance(raw_metadata, Mapping) else {}
        artifact = root / name
        if not artifact.is_file():
            raise RuntimeError(f"Manifest output is missing: {artifact}")
        if int(metadata.get("size", -1)) != artifact.stat().st_size:
            raise RuntimeError(f"Manifest output size mismatch: {artifact}")
        if metadata.get("checksum_sha256") != file_checksum(artifact):
            raise RuntimeError(f"Manifest output checksum mismatch: {artifact}")
    return payload


def refresh_latest_file(source: Path, destination: Path) -> None:
    """Atomically replace one predictable latest file with a completed output."""

    source_path = Path(source)
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    shutil.copyfile(source_path, temporary)
    temporary.replace(target)


def write_latest_pointer(
    path: Path,
    *,
    timestamp_directory: Path,
    root: Path,
) -> None:
    relative = Path(timestamp_directory).resolve().relative_to(Path(root).resolve())
    _write_json_atomic(
        Path(path),
        {
            "run_timestamp": timestamp_directory.name.split("-", 1)[0],
            "path": relative.as_posix(),
        },
    )


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
