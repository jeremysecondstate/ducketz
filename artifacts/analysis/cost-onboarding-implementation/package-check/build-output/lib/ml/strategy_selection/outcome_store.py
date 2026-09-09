from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import pandas as pd

from ml.artifacts import file_checksum


STRATEGY_OUTCOME_STORE_VERSION = "strategy-outcome-store-v1"
STRATEGY_OUTCOME_RECEIPT_VERSION = "strategy-outcome-store-receipt-v1"
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")
_CACHE_KEY = re.compile(r"^[0-9a-f]{64}$")


class StrategyOutcomeStoreError(RuntimeError):
    """A content-addressed historical outcome artifact failed verification."""


@dataclass(frozen=True)
class StrategyOutcomeArtifact:
    cache_key: str
    horizon: str
    frame: pd.DataFrame
    candidate_rows: int
    failures: Mapping[str, int]
    directory: Path
    outcome_path: Path
    manifest_path: Path
    receipt_path: Path

    @property
    def evidence_files(self) -> tuple[Path, Path, Path]:
        return self.outcome_path, self.manifest_path, self.receipt_path


def read_strategy_outcome_artifact(
    datastore_root: Path,
    *,
    horizon: str,
    cache_key: str,
) -> StrategyOutcomeArtifact | None:
    root = Path(datastore_root).resolve()
    clean_horizon, clean_key = _identity(horizon, cache_key)
    directory = _artifact_directory(root, clean_horizon, clean_key)
    if not directory.exists():
        return None
    if not directory.is_dir():
        raise StrategyOutcomeStoreError(
            f"Strategy outcome artifact is not a directory: {directory}"
        )
    manifest_path = directory / "manifest.json"
    receipt_path = directory / "receipt.json"
    outcome_path = directory / "outcomes.parquet"
    manifest = _read_json(manifest_path, label="strategy outcome manifest")
    receipt = _read_json(receipt_path, label="strategy outcome receipt")
    relative_directory = directory.relative_to(root).as_posix()
    if (
        manifest.get("schema_version") != STRATEGY_OUTCOME_STORE_VERSION
        or receipt.get("schema_version") != STRATEGY_OUTCOME_RECEIPT_VERSION
        or manifest.get("cache_key") != clean_key
        or receipt.get("cache_key") != clean_key
        or manifest.get("horizon") != clean_horizon
        or receipt.get("horizon") != clean_horizon
        or receipt.get("artifact_path") != relative_directory
        or receipt.get("manifest_checksum_sha256") != file_checksum(manifest_path)
    ):
        raise StrategyOutcomeStoreError(
            f"Strategy outcome receipt verification failed: {directory}"
        )
    output = manifest.get("output")
    if not isinstance(output, Mapping) or output.get("path") != outcome_path.name:
        raise StrategyOutcomeStoreError(
            f"Strategy outcome output inventory is malformed: {manifest_path}"
        )
    try:
        expected_size = int(output.get("size", -1))
        expected_rows = int(output.get("rows", -1))
        candidate_rows = int(manifest.get("candidate_rows", -1))
    except (TypeError, ValueError) as exc:
        raise StrategyOutcomeStoreError(
            f"Strategy outcome counts are malformed: {manifest_path}"
        ) from exc
    if (
        not outcome_path.is_file()
        or expected_size < 0
        or outcome_path.stat().st_size != expected_size
        or output.get("checksum_sha256") != file_checksum(outcome_path)
        or expected_rows < 0
        or candidate_rows < 0
    ):
        raise StrategyOutcomeStoreError(
            f"Strategy outcome Parquet verification failed: {outcome_path}"
        )
    failures = _failures(manifest.get("failures"))
    try:
        frame = pd.read_parquet(outcome_path)
    except Exception as exc:
        raise StrategyOutcomeStoreError(
            f"Strategy outcome Parquet is unreadable: {outcome_path}"
        ) from exc
    if len(frame) != expected_rows or list(frame.columns) != list(
        manifest.get("columns", ())
    ):
        raise StrategyOutcomeStoreError(
            f"Strategy outcome Parquet shape does not match its manifest: {outcome_path}"
        )
    return StrategyOutcomeArtifact(
        cache_key=clean_key,
        horizon=clean_horizon,
        frame=frame,
        candidate_rows=candidate_rows,
        failures=failures,
        directory=directory,
        outcome_path=outcome_path,
        manifest_path=manifest_path,
        receipt_path=receipt_path,
    )


def publish_strategy_outcome_artifact(
    datastore_root: Path,
    *,
    horizon: str,
    cache_key: str,
    frame: pd.DataFrame,
    candidate_rows: int,
    failures: Mapping[str, int],
    created_at: object | None = None,
) -> StrategyOutcomeArtifact:
    """Publish one immutable observation artifact, or verify an existing peer."""

    root = Path(datastore_root).resolve()
    clean_horizon, clean_key = _identity(horizon, cache_key)
    clean_candidate_rows = int(candidate_rows)
    if clean_candidate_rows < 0:
        raise ValueError("Strategy outcome candidate row count cannot be negative")
    clean_failures = _failures(failures)
    existing = read_strategy_outcome_artifact(
        root,
        horizon=clean_horizon,
        cache_key=clean_key,
    )
    if existing is not None:
        _assert_same_content(
            existing,
            frame=frame,
            candidate_rows=clean_candidate_rows,
            failures=clean_failures,
        )
        return existing

    destination = _artifact_directory(root, clean_horizon, clean_key)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{clean_key}.tmp-{os.getpid()}-",
            dir=destination.parent,
        )
    )
    outcome_path = staging / "outcomes.parquet"
    manifest_path = staging / "manifest.json"
    receipt_path = staging / "receipt.json"
    created = _utc(created_at or datetime.now(timezone.utc))
    try:
        frame.to_parquet(outcome_path, index=False)
        manifest = {
            "schema_version": STRATEGY_OUTCOME_STORE_VERSION,
            "cache_key": clean_key,
            "horizon": clean_horizon,
            "created_at": created.isoformat(),
            "candidate_rows": clean_candidate_rows,
            "failures": clean_failures,
            "columns": list(frame.columns),
            "output": {
                "path": outcome_path.name,
                "rows": len(frame),
                "size": outcome_path.stat().st_size,
                "checksum_sha256": file_checksum(outcome_path),
            },
        }
        _write_json(manifest_path, manifest)
        _write_json(
            receipt_path,
            {
                "schema_version": STRATEGY_OUTCOME_RECEIPT_VERSION,
                "cache_key": clean_key,
                "horizon": clean_horizon,
                "artifact_path": destination.relative_to(root).as_posix(),
                "manifest_checksum_sha256": file_checksum(manifest_path),
            },
        )
        try:
            staging.replace(destination)
        except OSError:
            # Another process may have won the same content-addressed publish.
            if not destination.is_dir():
                raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)

    published = read_strategy_outcome_artifact(
        root,
        horizon=clean_horizon,
        cache_key=clean_key,
    )
    if published is None:
        raise StrategyOutcomeStoreError(
            f"Strategy outcome publication did not become visible: {destination}"
        )
    _assert_same_content(
        published,
        frame=frame,
        candidate_rows=clean_candidate_rows,
        failures=clean_failures,
    )
    return published


def _assert_same_content(
    artifact: StrategyOutcomeArtifact,
    *,
    frame: pd.DataFrame,
    candidate_rows: int,
    failures: Mapping[str, int],
) -> None:
    frames_match = True
    try:
        pd.testing.assert_frame_equal(
            artifact.frame,
            frame.reset_index(drop=True),
            check_dtype=False,
            check_exact=True,
        )
    except AssertionError:
        frames_match = False
    if (
        artifact.candidate_rows != candidate_rows
        or dict(artifact.failures) != dict(failures)
        or list(artifact.frame.columns) != list(frame.columns)
        or not frames_match
    ):
        raise StrategyOutcomeStoreError(
            "Strategy outcome content-address collision or non-deterministic replay: "
            f"{artifact.directory}"
        )


def _artifact_directory(root: Path, horizon: str, cache_key: str) -> Path:
    return root / "ml" / "strategy-outcomes" / horizon / cache_key


def _identity(horizon: str, cache_key: str) -> tuple[str, str]:
    clean_horizon = str(horizon).strip().lower()
    clean_key = str(cache_key).strip().lower()
    if not _SAFE_COMPONENT.fullmatch(clean_horizon):
        raise ValueError("Strategy outcome horizon is unsafe")
    if not _CACHE_KEY.fullmatch(clean_key):
        raise ValueError("Strategy outcome cache key must be a lowercase SHA-256")
    return clean_horizon, clean_key


def _failures(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise StrategyOutcomeStoreError("Strategy outcome failures must be an object")
    output: dict[str, int] = {}
    for key, raw_count in value.items():
        try:
            count = int(raw_count)
        except (TypeError, ValueError) as exc:
            raise StrategyOutcomeStoreError(
                "Strategy outcome failure count is invalid"
            ) from exc
        clean_key = str(key).strip()
        if not clean_key or count < 0:
            raise StrategyOutcomeStoreError(
                "Strategy outcome failure inventory is invalid"
            )
        output[clean_key] = count
    return dict(sorted(output.items()))


def _read_json(path: Path, *, label: str) -> Mapping[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StrategyOutcomeStoreError(f"Unreadable {label}: {path}") from exc
    if not isinstance(payload, Mapping):
        raise StrategyOutcomeStoreError(f"Malformed {label}: {path}")
    return payload


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _utc(value: object) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise ValueError("Strategy outcome created_at is invalid")
    return pd.Timestamp(timestamp)


__all__ = [
    "STRATEGY_OUTCOME_RECEIPT_VERSION",
    "STRATEGY_OUTCOME_STORE_VERSION",
    "StrategyOutcomeArtifact",
    "StrategyOutcomeStoreError",
    "publish_strategy_outcome_artifact",
    "read_strategy_outcome_artifact",
]
