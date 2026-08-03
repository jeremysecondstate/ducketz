from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import pandas as pd

from ml.artifacts import file_checksum, utc_timestamp, verify_manifest


PUBLICATION_CONTRACT_VERSION = "current-output-authoritative-pointer-v2"
PUBLICATION_POINTER_VERSION = "current-output-pointer-v1"
PUBLICATION_RECEIPT_NAME = "publication.json"


class CurrentPublicationError(RuntimeError):
    """The authoritative current-publication contract is invalid."""


@dataclass(frozen=True)
class CurrentPublication:
    run_directory: Path
    manifest: Mapping[str, object]
    receipt: Mapping[str, object] | None
    pointer: Mapping[str, object]


def publication_contract_kind(manifest: Mapping[str, object]) -> str:
    """Classify a verified manifest without failing open on malformed metadata."""

    if "configuration" not in manifest:
        return "invalid"
    configuration = manifest.get("configuration")
    if not isinstance(configuration, Mapping):
        return "invalid"
    if "publication_contract" not in configuration:
        return "legacy"
    contract = configuration.get("publication_contract")
    if not isinstance(contract, Mapping):
        return "invalid"
    if (
        contract.get("version") != PUBLICATION_CONTRACT_VERSION
        or contract.get("receipt") != PUBLICATION_RECEIPT_NAME
        or contract.get("required_for_live_evidence") is not True
        or contract.get("authority") != "ml/latest/run.json"
    ):
        return "invalid"
    return "receipt"


def expected_run_path(
    datastore_root: Path,
    run_directory: Path,
) -> str:
    root = Path(datastore_root).resolve()
    run = Path(run_directory).resolve()
    runs_root = (root / "ml" / "runs").resolve()
    if run.parent != runs_root:
        raise CurrentPublicationError(
            f"Published run is outside the immutable run directory: {run}"
        )
    return run.relative_to(root).as_posix()


def read_publication_receipt(
    run_directory: Path,
    manifest: Mapping[str, object],
    *,
    datastore_root: Path,
) -> Mapping[str, object]:
    """Return a strictly validated receipt for a receipt-era manifest."""

    if publication_contract_kind(manifest) != "receipt":
        raise CurrentPublicationError(
            "Manifest does not contain a valid receipt-era publication contract"
        )
    run = Path(run_directory)
    receipt_path = run / PUBLICATION_RECEIPT_NAME
    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CurrentPublicationError(
            f"Publication receipt is unreadable: {receipt_path}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise CurrentPublicationError(
            f"Publication receipt is not an object: {receipt_path}"
        )

    manifest_timestamp = _required_timestamp(
        manifest.get("run_timestamp"),
        "manifest run_timestamp",
    )
    receipt_timestamp = _required_timestamp(
        payload.get("run_timestamp"),
        "receipt run_timestamp",
    )
    promoted_at = _required_timestamp(
        payload.get("promoted_at"),
        "receipt promoted_at",
    )
    expected_path = expected_run_path(datastore_root, run)
    manifest_path = run / "manifest.json"
    if (
        payload.get("schema_version") != PUBLICATION_CONTRACT_VERSION
        or receipt_timestamp != manifest_timestamp
        or payload.get("run_path") != expected_path
        or payload.get("manifest_checksum_sha256")
        != file_checksum(manifest_path)
        or promoted_at < manifest_timestamp
    ):
        raise CurrentPublicationError(
            f"Publication receipt does not match its run manifest: {receipt_path}"
        )
    previous = payload.get("previous_publication")
    if previous is not None:
        _validate_record_shape(previous, label="previous_publication")
        if previous.get("run_path") == expected_path:
            raise CurrentPublicationError(
                f"Publication receipt points to itself: {receipt_path}"
            )
        previous_promoted = _required_timestamp(
            previous.get("promoted_at"),
            "previous publication promoted_at",
        )
        if previous_promoted > promoted_at:
            raise CurrentPublicationError(
                "Publication receipt chronology moves backwards"
            )
    return payload


def verify_publication_receipt(
    run_directory: Path,
    manifest: Mapping[str, object],
    *,
    datastore_root: Path,
) -> bool:
    try:
        read_publication_receipt(
            run_directory,
            manifest,
            datastore_root=datastore_root,
        )
        return True
    except (CurrentPublicationError, OSError, ValueError):
        return False


def publication_record(
    run_directory: Path,
    manifest: Mapping[str, object],
    receipt: Mapping[str, object],
    *,
    datastore_root: Path,
) -> dict[str, object]:
    run_path = expected_run_path(datastore_root, run_directory)
    receipt_path = Path(run_directory) / PUBLICATION_RECEIPT_NAME
    return {
        "run_path": run_path,
        "run_timestamp": utc_timestamp(
            manifest["run_timestamp"]
        ).isoformat(),
        "promoted_at": utc_timestamp(receipt["promoted_at"]).isoformat(),
        "manifest_checksum_sha256": file_checksum(
            Path(run_directory) / "manifest.json"
        ),
        "receipt_checksum_sha256": file_checksum(receipt_path),
    }


def authoritative_pointer_payload(
    record: Mapping[str, object],
) -> dict[str, object]:
    _validate_record_shape(record, label="current")
    return {
        "schema_version": PUBLICATION_POINTER_VERSION,
        # Retain the original keys for read-only compatibility. New readers
        # validate and use ``current``.
        "path": record["run_path"],
        "run_timestamp": record["run_timestamp"],
        "current": dict(record),
    }


def read_current_publication(datastore_root: Path) -> CurrentPublication:
    """Resolve and verify the one authoritative current-generation pointer."""

    root = Path(datastore_root)
    pointer_path = root / "ml" / "latest" / "run.json"
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CurrentPublicationError(
            f"Current publication pointer is unreadable: {pointer_path}"
        ) from exc
    if not isinstance(pointer, Mapping):
        raise CurrentPublicationError(
            f"Current publication pointer is not an object: {pointer_path}"
        )

    if "schema_version" not in pointer:
        return _read_legacy_current_publication(root, pointer)
    if pointer.get("schema_version") != PUBLICATION_POINTER_VERSION:
        raise CurrentPublicationError(
            f"Unsupported current publication pointer: {pointer_path}"
        )
    current = pointer.get("current")
    _validate_record_shape(current, label="current")
    if (
        pointer.get("path") != current.get("run_path")
        or _required_timestamp(
            pointer.get("run_timestamp"),
            "pointer run_timestamp",
        )
        != _required_timestamp(
            current.get("run_timestamp"),
            "current run_timestamp",
        )
    ):
        raise CurrentPublicationError(
            f"Current publication compatibility keys disagree: {pointer_path}"
        )
    run = _run_from_record(root, current)
    manifest = _verified_manifest(run)
    receipt = read_publication_receipt(
        run,
        manifest,
        datastore_root=root,
    )
    observed = publication_record(
        run,
        manifest,
        receipt,
        datastore_root=root,
    )
    if dict(current) != observed:
        raise CurrentPublicationError(
            f"Current publication pointer does not match its receipt: {pointer_path}"
        )
    return CurrentPublication(
        run_directory=run,
        manifest=manifest,
        receipt=receipt,
        pointer=pointer,
    )


def resolve_current_output(
    datastore_root: Path,
    output_name: str,
) -> Path:
    publication = read_current_publication(datastore_root)
    outputs = publication.manifest.get("output_files")
    if not isinstance(outputs, Mapping) or output_name not in outputs:
        raise CurrentPublicationError(
            f"Current run did not publish required output: {output_name}"
        )
    output = publication.run_directory / output_name
    if not output.is_file():
        raise CurrentPublicationError(
            f"Current run output is missing: {output}"
        )
    return output


def authoritative_receipt_runs(datastore_root: Path) -> dict[Path, pd.Timestamp]:
    """Return receipt-era runs reachable from the authoritative pointer chain."""

    root = Path(datastore_root)
    if not (root / "ml" / "latest" / "run.json").is_file():
        return {}
    publication = read_current_publication(root)
    if publication.receipt is None:
        return {}

    output: dict[Path, pd.Timestamp] = {}
    record = publication.pointer["current"]
    seen: set[str] = set()
    while record is not None:
        _validate_record_shape(record, label="publication chain record")
        run_path = str(record["run_path"])
        if run_path in seen:
            raise CurrentPublicationError("Current publication chain contains a cycle")
        seen.add(run_path)
        run = _run_from_record(root, record)
        manifest = _verified_manifest(run)
        receipt = read_publication_receipt(
            run,
            manifest,
            datastore_root=root,
        )
        observed = publication_record(
            run,
            manifest,
            receipt,
            datastore_root=root,
        )
        if dict(record) != observed:
            raise CurrentPublicationError(
                f"Publication chain record does not match receipt: {run}"
            )
        output[run.resolve()] = utc_timestamp(receipt["promoted_at"])
        previous = receipt.get("previous_publication")
        record = previous if isinstance(previous, Mapping) else None
    return output


def _read_legacy_current_publication(
    datastore_root: Path,
    pointer: Mapping[str, object],
) -> CurrentPublication:
    if set(pointer).difference({"path", "run_timestamp"}):
        raise CurrentPublicationError(
            "Legacy current publication pointer contains unsupported fields"
        )
    run_path = pointer.get("path")
    if not isinstance(run_path, str) or not run_path:
        raise CurrentPublicationError(
            "Legacy current publication pointer has no run path"
        )
    record = {
        "run_path": run_path,
        "run_timestamp": pointer.get("run_timestamp"),
    }
    run = _run_from_legacy_record(datastore_root, record)
    manifest = _verified_manifest(run)
    manifest_timestamp = _required_timestamp(
        manifest.get("run_timestamp"),
        "manifest run_timestamp",
    )
    if manifest_timestamp != _required_timestamp(
        pointer.get("run_timestamp"),
        "pointer run_timestamp",
    ):
        raise CurrentPublicationError(
            "Legacy current pointer timestamp does not match its manifest"
        )
    if publication_contract_kind(manifest) != "legacy":
        raise CurrentPublicationError(
            "Legacy current pointer cannot select a receipt-era or malformed "
            "publication contract"
        )
    return CurrentPublication(
        run_directory=run,
        manifest=manifest,
        receipt=None,
        pointer=pointer,
    )


def _run_from_record(
    datastore_root: Path,
    record: Mapping[str, object],
) -> Path:
    _validate_record_shape(record, label="publication record")
    return _run_from_legacy_record(datastore_root, record)


def _run_from_legacy_record(
    datastore_root: Path,
    record: Mapping[str, object],
) -> Path:
    root = Path(datastore_root).resolve()
    raw_path = record.get("run_path")
    if not isinstance(raw_path, str) or not raw_path:
        raise CurrentPublicationError("Publication record has no run path")
    relative = Path(raw_path)
    if relative.is_absolute():
        raise CurrentPublicationError("Publication run path must be relative")
    run = (root / relative).resolve()
    runs_root = (root / "ml" / "runs").resolve()
    if run.parent != runs_root:
        raise CurrentPublicationError(
            f"Publication run path escapes immutable runs: {raw_path}"
        )
    return run


def _validate_record_shape(
    value: object,
    *,
    label: str,
) -> None:
    if not isinstance(value, Mapping):
        raise CurrentPublicationError(f"{label} is not an object")
    expected = {
        "run_path",
        "run_timestamp",
        "promoted_at",
        "manifest_checksum_sha256",
        "receipt_checksum_sha256",
    }
    if set(value) != expected:
        raise CurrentPublicationError(f"{label} has invalid fields")
    for key in (
        "run_path",
        "manifest_checksum_sha256",
        "receipt_checksum_sha256",
    ):
        if not isinstance(value.get(key), str) or not value.get(key):
            raise CurrentPublicationError(f"{label} has invalid {key}")
    _required_timestamp(value.get("run_timestamp"), f"{label} run_timestamp")
    _required_timestamp(value.get("promoted_at"), f"{label} promoted_at")


def _required_timestamp(value: object, label: str) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise CurrentPublicationError(f"{label} is invalid")
    return utc_timestamp(timestamp)


def _verified_manifest(run_directory: Path) -> Mapping[str, object]:
    try:
        return verify_manifest(run_directory)
    except Exception as exc:
        raise CurrentPublicationError(
            f"Current publication manifest is invalid: {run_directory}"
        ) from exc
