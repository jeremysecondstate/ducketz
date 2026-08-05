from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import pandas as pd

from datafetching.ids import add_readable_id
from datafetching.layout import safe_token
from ml.artifacts import file_checksum


OPTION_SNAPSHOT_PUBLICATION_VERSION = "option-snapshot-publication-v1"
OPTION_SNAPSHOT_POINTER_VERSION = "option-snapshot-pointer-v1"
OPTION_SNAPSHOT_RECEIPT_NAME = "receipt.json"
OPTION_SNAPSHOT_OUTPUTS = (
    "raw.parquet",
    "contracts.parquet",
    "option-quality.parquet",
)
_SNAPSHOT_KEY = ("symbol", "snapshot_for", "available_at")


class OptionSnapshotPublicationError(RuntimeError):
    """A committed Schwab option snapshot failed strict validation."""


@dataclass(frozen=True)
class CommittedOptionSnapshot:
    symbol: str
    snapshot_for: pd.Timestamp
    available_at: pd.Timestamp
    directory: Path
    raw_path: Path
    contracts_path: Path
    features_path: Path
    receipt_path: Path
    receipt: Mapping[str, object]


def option_writer_lock_path(datastore_root: Path) -> Path:
    return Path(datastore_root) / ".ducketz-options-writer.lock"


def option_snapshot_root(datastore_root: Path, *, symbol: str) -> Path:
    return (
        Path(datastore_root)
        / "stocks"
        / safe_token(symbol.strip().upper())
        / "options"
        / "snapshots"
        / "schwab"
    )


def option_snapshot_pointer_path(datastore_root: Path, *, symbol: str) -> Path:
    return (
        Path(datastore_root)
        / "stocks"
        / safe_token(symbol.strip().upper())
        / "options"
        / "latest"
        / "schwab.json"
    )


def publish_option_snapshot(
    datastore_root: Path,
    *,
    symbol: str,
    raw: pd.DataFrame,
    contracts: pd.DataFrame,
    features: pd.DataFrame,
) -> CommittedOptionSnapshot:
    """Atomically expose three immutable files through one receipt and pointer."""

    clean_symbol = symbol.strip().upper()
    snapshot_for, available_at = _coherent_key(
        clean_symbol,
        (raw, contracts, features),
    )
    parent = option_snapshot_root(datastore_root, symbol=clean_symbol)
    parent.mkdir(parents=True, exist_ok=True)
    run_name = f"{available_at.value}-{snapshot_for.value}"
    destination = parent / run_name
    if destination.is_dir():
        committed = read_option_snapshot(destination)
        _publish_pointer(datastore_root, committed)
        return committed

    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{run_name}.tmp-{os.getpid()}-",
            dir=parent,
        )
    )
    try:
        prepared = {
            "raw.parquet": add_readable_id(
                raw.reset_index(drop=True),
                key_columns=_SNAPSHOT_KEY,
            ),
            "contracts.parquet": add_readable_id(
                contracts.reset_index(drop=True),
                key_columns=(*_SNAPSHOT_KEY, "contract_symbol"),
            ),
            "option-quality.parquet": add_readable_id(
                features.reset_index(drop=True),
                key_columns=_SNAPSHOT_KEY,
            ),
        }
        for name, frame in prepared.items():
            frame.to_parquet(staging / name, index=False)

        output_inventory = {
            name: {
                "rows": len(prepared[name]),
                "size": (staging / name).stat().st_size,
                "checksum_sha256": file_checksum(staging / name),
            }
            for name in OPTION_SNAPSHOT_OUTPUTS
        }
        manifest = {
            "schema_version": OPTION_SNAPSHOT_PUBLICATION_VERSION,
            "symbol": clean_symbol,
            "snapshot_for": snapshot_for.isoformat(),
            "available_at": available_at.isoformat(),
            "outputs": output_inventory,
        }
        manifest_path = staging / "manifest.json"
        _write_json(manifest_path, manifest)
        receipt = {
            "schema_version": OPTION_SNAPSHOT_PUBLICATION_VERSION,
            "symbol": clean_symbol,
            "snapshot_for": snapshot_for.isoformat(),
            "available_at": available_at.isoformat(),
            "run_path": destination.relative_to(Path(datastore_root)).as_posix(),
            "manifest_checksum_sha256": file_checksum(manifest_path),
            "outputs": output_inventory,
        }
        _write_json(staging / OPTION_SNAPSHOT_RECEIPT_NAME, receipt)
        staging.replace(destination)
    except BaseException:
        _remove_unpublished_staging(staging)
        raise

    committed = read_option_snapshot(destination)
    _publish_pointer(datastore_root, committed)
    return committed


def read_option_snapshot(directory: Path) -> CommittedOptionSnapshot:
    run = Path(directory)
    receipt_path = run / OPTION_SNAPSHOT_RECEIPT_NAME
    manifest_path = run / "manifest.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OptionSnapshotPublicationError(
            f"Option snapshot receipt is unreadable: {run}"
        ) from exc
    if not isinstance(receipt, Mapping) or not isinstance(manifest, Mapping):
        raise OptionSnapshotPublicationError(
            f"Option snapshot metadata is malformed: {run}"
        )
    if (
        receipt.get("schema_version") != OPTION_SNAPSHOT_PUBLICATION_VERSION
        or manifest.get("schema_version") != OPTION_SNAPSHOT_PUBLICATION_VERSION
        or receipt.get("manifest_checksum_sha256") != file_checksum(manifest_path)
    ):
        raise OptionSnapshotPublicationError(
            f"Option snapshot metadata does not validate: {run}"
        )
    symbol = str(receipt.get("symbol") or "").strip().upper()
    snapshot_for = _utc(receipt.get("snapshot_for"), "snapshot_for")
    available_at = _utc(receipt.get("available_at"), "available_at")
    if (
        manifest.get("symbol") != symbol
        or _utc(manifest.get("snapshot_for"), "manifest snapshot_for")
        != snapshot_for
        or _utc(manifest.get("available_at"), "manifest available_at")
        != available_at
    ):
        raise OptionSnapshotPublicationError(
            f"Option snapshot receipt disagrees with its manifest: {run}"
        )
    outputs = receipt.get("outputs")
    manifest_outputs = manifest.get("outputs")
    if (
        not isinstance(outputs, Mapping)
        or not isinstance(manifest_outputs, Mapping)
        or set(outputs) != set(OPTION_SNAPSHOT_OUTPUTS)
        or dict(outputs) != dict(manifest_outputs)
    ):
        raise OptionSnapshotPublicationError(
            f"Option snapshot output inventory is invalid: {run}"
        )
    for name in OPTION_SNAPSHOT_OUTPUTS:
        path = run / name
        metadata = outputs.get(name)
        if not path.is_file() or not isinstance(metadata, Mapping):
            raise OptionSnapshotPublicationError(
                f"Option snapshot output is missing: {path}"
            )
        if (
            int(metadata.get("size", -1)) != path.stat().st_size
            or metadata.get("checksum_sha256") != file_checksum(path)
        ):
            raise OptionSnapshotPublicationError(
                f"Option snapshot output checksum mismatch: {path}"
            )
    return CommittedOptionSnapshot(
        symbol=symbol,
        snapshot_for=snapshot_for,
        available_at=available_at,
        directory=run,
        raw_path=run / "raw.parquet",
        contracts_path=run / "contracts.parquet",
        features_path=run / "option-quality.parquet",
        receipt_path=receipt_path,
        receipt=receipt,
    )


def committed_option_snapshots(
    datastore_root: Path,
    *,
    symbol: str,
    available_not_after: object | None = None,
) -> tuple[CommittedOptionSnapshot, ...]:
    cutoff = (
        _utc(available_not_after, "available_not_after")
        if available_not_after is not None
        else None
    )
    parent = option_snapshot_root(datastore_root, symbol=symbol)
    if not parent.is_dir():
        return ()
    committed: list[CommittedOptionSnapshot] = []
    for receipt_path in sorted(parent.glob(f"*/{OPTION_SNAPSHOT_RECEIPT_NAME}")):
        snapshot = read_option_snapshot(receipt_path.parent)
        if cutoff is None or snapshot.available_at <= cutoff:
            committed.append(snapshot)
    return tuple(
        sorted(
            committed,
            key=lambda value: (value.available_at, value.snapshot_for),
        )
    )


def read_committed_option_surfaces(
    datastore_root: Path,
    *,
    symbols: Sequence[str],
    available_not_after: object,
) -> tuple[pd.DataFrame, tuple[Path, ...]]:
    frames: list[pd.DataFrame] = []
    sources: list[Path] = []
    for symbol in dict.fromkeys(str(value).strip().upper() for value in symbols):
        for snapshot in committed_option_snapshots(
            datastore_root,
            symbol=symbol,
            available_not_after=available_not_after,
        ):
            frames.append(pd.read_parquet(snapshot.features_path))
            sources.extend((snapshot.features_path, snapshot.receipt_path))
    return (
        pd.concat(frames, ignore_index=True, sort=False)
        if frames
        else pd.DataFrame(),
        tuple(dict.fromkeys(sources)),
    )


def _coherent_key(
    symbol: str,
    frames: Iterable[pd.DataFrame],
) -> tuple[pd.Timestamp, pd.Timestamp]:
    keys: set[tuple[str, pd.Timestamp, pd.Timestamp]] = set()
    for frame in frames:
        missing = [column for column in _SNAPSHOT_KEY if column not in frame.columns]
        if frame.empty or missing:
            raise ValueError(
                "Committed option snapshot requires non-empty coherent frames; "
                + ", ".join(missing)
            )
        symbols = frame["symbol"].astype("string").str.strip().str.upper().unique()
        snapshot_values = pd.to_datetime(
            frame["snapshot_for"], utc=True, errors="coerce"
        ).drop_duplicates()
        available_values = pd.to_datetime(
            frame["available_at"], utc=True, errors="coerce"
        ).drop_duplicates()
        if len(symbols) != 1 or len(snapshot_values) != 1 or len(available_values) != 1:
            raise ValueError("Option snapshot frames do not share one receipt key")
        keys.add(
            (
                str(symbols[0]),
                pd.Timestamp(snapshot_values.iloc[0]),
                pd.Timestamp(available_values.iloc[0]),
            )
        )
    if len(keys) != 1:
        raise ValueError("Raw, normalized, and surface option files are incoherent")
    observed_symbol, snapshot_for, available_at = keys.pop()
    if observed_symbol != symbol:
        raise ValueError("Option snapshot symbol does not match publication target")
    return snapshot_for, available_at


def _publish_pointer(
    datastore_root: Path,
    snapshot: CommittedOptionSnapshot,
) -> None:
    pointer = option_snapshot_pointer_path(
        datastore_root,
        symbol=snapshot.symbol,
    )
    payload = {
        "schema_version": OPTION_SNAPSHOT_POINTER_VERSION,
        "symbol": snapshot.symbol,
        "snapshot_for": snapshot.snapshot_for.isoformat(),
        "available_at": snapshot.available_at.isoformat(),
        "run_path": snapshot.directory.relative_to(Path(datastore_root)).as_posix(),
        "receipt_checksum_sha256": file_checksum(snapshot.receipt_path),
    }
    _write_json_atomic(pointer, payload)


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    try:
        _write_json(temporary, payload)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_unpublished_staging(path: Path) -> None:
    if not path.is_dir() or ".tmp-" not in path.name:
        return
    for child in path.iterdir():
        if child.is_file():
            child.unlink(missing_ok=True)
    path.rmdir()


def _utc(value: object, label: str) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise OptionSnapshotPublicationError(f"Invalid option snapshot {label}")
    return pd.Timestamp(timestamp)


__all__ = [
    "CommittedOptionSnapshot",
    "OPTION_SNAPSHOT_PUBLICATION_VERSION",
    "OptionSnapshotPublicationError",
    "committed_option_snapshots",
    "option_snapshot_pointer_path",
    "option_writer_lock_path",
    "publish_option_snapshot",
    "read_committed_option_surfaces",
    "read_option_snapshot",
]
