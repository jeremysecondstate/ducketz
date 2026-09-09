from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence
from uuid import uuid4

from filelock import FileLock, Timeout

from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir


MEMORY_SCHEMA_VERSION = "loop-c-weekly-scheduler-memory-v1"
MEMORY_POINTER_SCHEMA_VERSION = "loop-c-weekly-scheduler-memory-pointer-v1"
_MEMORY_RELATIVE_DIRECTORY = (
    Path("logs") / "ducketz" / "loop-c" / "weekly-scheduler-memory"
)
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")


def memory_directory(root: Path | str) -> Path:
    return (Path(root).resolve() / _MEMORY_RELATIVE_DIRECTORY).resolve()


def read_current_memory(root: Path | str) -> dict[str, object]:
    directory = memory_directory(root)
    pointer_path = directory / "current.json"
    try:
        if not pointer_path.exists():
            receipts = list((directory / "runs").glob("*.json"))
            if receipts:
                raise ValueError("memory receipts exist but current.json is missing")
            return {
                "schema_version": MEMORY_POINTER_SCHEMA_VERSION,
                "status": "EMPTY",
                "pointer_path": str(pointer_path),
            }

        pointer = _read_mapping(pointer_path)
        if pointer.get("schema_version") != MEMORY_POINTER_SCHEMA_VERSION:
            raise ValueError("current.json has an unsupported schema_version")
        sequence = _positive_int(pointer.get("sequence"), "pointer sequence")
        current = _required_text(pointer.get("current"), "pointer current", 512)
        checksum = _sha256_text(
            pointer.get("receipt_sha256"), "pointer receipt_sha256"
        )
        receipt_path = _receipt_path(directory, current, "current.json")
        receipt = _verify_chain(
            directory,
            receipt_path=receipt_path,
            expected_checksum=checksum,
            expected_sequence=sequence,
        )
        return {
            "schema_version": MEMORY_POINTER_SCHEMA_VERSION,
            "status": "VALID",
            "pointer_path": str(pointer_path),
            "receipt_path": str(receipt_path),
            "receipt_sha256": checksum,
            "memory": receipt,
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "schema_version": MEMORY_POINTER_SCHEMA_VERSION,
            "status": "INVALID",
            "pointer_path": str(pointer_path),
            "error": str(exc),
        }


def commit_memory(
    root: Path | str,
    *,
    wake_id: str,
    review_window: str,
    final_status: str,
    incident_status: str,
    summary: str,
    next_action: str,
    actions: Sequence[str] = (),
    evidence_paths: Sequence[str] = (),
    changed_files: Sequence[str] = (),
    created_at: datetime | None = None,
) -> dict[str, object]:
    directory = memory_directory(root)
    directory.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(directory / "commit.lock"), timeout=5)
    try:
        with lock:
            return _commit_memory_locked(
                root,
                wake_id=wake_id,
                review_window=review_window,
                final_status=final_status,
                incident_status=incident_status,
                summary=summary,
                next_action=next_action,
                actions=actions,
                evidence_paths=evidence_paths,
                changed_files=changed_files,
                created_at=created_at,
            )
    except Timeout as exc:
        raise ValueError("another weekly scheduler memory commit is in progress") from exc


def _commit_memory_locked(
    root: Path | str,
    *,
    wake_id: str,
    review_window: str,
    final_status: str,
    incident_status: str,
    summary: str,
    next_action: str,
    actions: Sequence[str],
    evidence_paths: Sequence[str],
    changed_files: Sequence[str],
    created_at: datetime | None,
) -> dict[str, object]:
    current = read_current_memory(root)
    current_status = str(current["status"])
    if current_status == "INVALID":
        raise ValueError(
            "refusing to extend an invalid weekly scheduler memory chain: "
            + str(current.get("error", "unknown validation error"))
        )

    normalized_wake_id = _iso_timestamp(wake_id, "wake_id")
    prior: dict[str, object] | None = None
    sequence = 1
    if current_status == "VALID":
        previous = current["memory"]
        if not isinstance(previous, Mapping):
            raise ValueError("validated weekly scheduler memory is not an object")
        if _iso_timestamp(previous.get("wake_id"), "prior wake_id") == normalized_wake_id:
            return {
                "schema_version": MEMORY_POINTER_SCHEMA_VERSION,
                "status": "ALREADY_COMMITTED",
                "wake_id": normalized_wake_id,
                "sequence": _positive_int(previous.get("sequence"), "prior sequence"),
                "pointer_path": str(current["pointer_path"]),
                "receipt_path": str(current["receipt_path"]),
                "receipt_sha256": str(current["receipt_sha256"]),
            }
        sequence = _positive_int(previous.get("sequence"), "prior sequence") + 1
        directory = memory_directory(root)
        prior = {
            "sequence": sequence - 1,
            "receipt_path": Path(str(current["receipt_path"]))
            .relative_to(directory)
            .as_posix(),
            "receipt_sha256": str(current["receipt_sha256"]),
        }

    timestamp = created_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        raise ValueError("created_at must include a timezone")
    timestamp = timestamp.astimezone(timezone.utc)
    created_text = timestamp.isoformat().replace("+00:00", "Z")

    receipt: dict[str, object] = {
        "schema_version": MEMORY_SCHEMA_VERSION,
        "wake_id": normalized_wake_id,
        "sequence": sequence,
        "created_at": created_text,
        "previous": prior,
        "review": {
            "window": _required_text(review_window, "review_window", 256),
            "final_status": _token(final_status, "final_status"),
            "incident_status": _token(incident_status, "incident_status"),
        },
        "continuity": {
            "summary": _required_text(summary, "summary", 2400),
            "next_action": _required_text(next_action, "next_action", 1200),
            "actions": _text_list(actions, "actions", 64, 1200),
            "evidence_paths": _text_list(
                evidence_paths, "evidence_paths", 128, 2048
            ),
            "changed_files": _text_list(
                changed_files, "changed_files", 64, 2048
            ),
        },
        "safety": {
            "authority": "ADVISORY_MEMORY_ONLY",
            "automatic_change_allowed": False,
            "orders_enabled": False,
            "orders_placed": 0,
            "resume_requires_live_revalidation": True,
            "review_authority": (
                "verified weekly review receipts and explicit operator controls"
            ),
        },
    }

    directory = memory_directory(root)
    receipt_name = f"{sequence:08d}-{_timestamp_slug(timestamp)}.json"
    receipt_path = directory / "runs" / receipt_name
    _write_unique_json(receipt_path, receipt)
    receipt_checksum = _sha256_path(receipt_path)
    pointer = {
        "schema_version": MEMORY_POINTER_SCHEMA_VERSION,
        "sequence": sequence,
        "updated_at": created_text,
        "current": receipt_path.relative_to(directory).as_posix(),
        "receipt_sha256": receipt_checksum,
    }
    pointer_path = directory / "current.json"
    _atomic_replace_json(pointer_path, pointer)
    return {
        "schema_version": MEMORY_POINTER_SCHEMA_VERSION,
        "status": "COMMITTED",
        "sequence": sequence,
        "pointer_path": str(pointer_path),
        "receipt_path": str(receipt_path),
        "receipt_sha256": receipt_checksum,
    }


def _verify_chain(
    directory: Path,
    *,
    receipt_path: Path,
    expected_checksum: str,
    expected_sequence: int,
) -> Mapping[str, object]:
    current_path = receipt_path
    checksum = expected_checksum
    sequence = expected_sequence
    newest: Mapping[str, object] | None = None
    while True:
        if not current_path.is_file():
            raise ValueError(f"memory receipt {sequence} is missing")
        if _sha256_path(current_path) != checksum:
            raise ValueError(f"memory receipt {sequence} checksum does not match")
        receipt = _read_mapping(current_path)
        if receipt.get("schema_version") != MEMORY_SCHEMA_VERSION:
            raise ValueError(
                f"memory receipt {sequence} has an unsupported schema_version"
            )
        if _positive_int(receipt.get("sequence"), "receipt sequence") != sequence:
            raise ValueError(f"memory receipt {sequence} has the wrong sequence")
        _iso_timestamp(receipt.get("wake_id"), "receipt wake_id")
        if newest is None:
            newest = receipt

        previous = receipt.get("previous")
        if sequence == 1:
            if previous is not None:
                raise ValueError("the first memory receipt must not have a predecessor")
            assert newest is not None
            return newest
        if not isinstance(previous, Mapping):
            raise ValueError(f"memory receipt {sequence} has no predecessor")
        prior_sequence = _positive_int(previous.get("sequence"), "prior sequence")
        if prior_sequence != sequence - 1:
            raise ValueError(f"memory receipt {sequence} skips its predecessor")
        current_path = _receipt_path(
            directory,
            _required_text(previous.get("receipt_path"), "prior receipt_path", 2048),
            f"memory receipt {sequence}",
        )
        checksum = _sha256_text(
            previous.get("receipt_sha256"), "prior receipt_sha256"
        )
        sequence = prior_sequence


def _read_mapping(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _receipt_path(directory: Path, value: str, source: str) -> Path:
    candidate = Path(value)
    path = candidate.resolve() if candidate.is_absolute() else (directory / candidate).resolve()
    runs_directory = (directory / "runs").resolve()
    if not path.is_relative_to(runs_directory):
        raise ValueError(f"{source} points outside the memory runs directory")
    return path


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, default=str) + "\n").encode(
        "utf-8"
    )


def _write_unique_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _atomic_replace_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        _write_unique_json(temporary, value)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if parsed < 1:
        raise ValueError(f"{field} must be a positive integer")
    return parsed


def _required_text(value: object, field: str, maximum: int) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        raise ValueError(f"{field} must not be empty")
    if len(text) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return text


def _token(value: object, field: str) -> str:
    text = _required_text(value, field, 128)
    if not _TOKEN_PATTERN.fullmatch(text):
        raise ValueError(
            f"{field} may contain only letters, digits, dot, underscore, colon, or dash"
        )
    return text


def _sha256_text(value: object, field: str) -> str:
    text = _required_text(value, field, 64).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise ValueError(f"{field} must be a SHA-256 hex digest")
    return text


def _iso_timestamp(value: object, field: str) -> str:
    text = _required_text(value, field, 128)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _text_list(
    values: Sequence[str], field: str, maximum_items: int, maximum_length: int
) -> list[str]:
    if len(values) > maximum_items:
        raise ValueError(f"{field} exceeds {maximum_items} items")
    return [
        _required_text(value, f"{field}[{index}]", maximum_length)
        for index, value in enumerate(values)
    ]


def _timestamp_slug(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _add_root_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--root-dir", type=Path)
    group.add_argument("--datastore-target", choices=sorted(DATASTORE_TARGETS))


def _root_from_args(args: argparse.Namespace) -> Path:
    return resolve_datastore_dir(
        root_dir=getattr(args, "root_dir", None),
        target=getattr(args, "datastore_target", None),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read or commit the Loop C weekly scheduler memory."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    read_parser = subparsers.add_parser("read")
    _add_root_arguments(read_parser)
    read_parser.add_argument("--compact", action="store_true")

    commit_parser = subparsers.add_parser("commit")
    _add_root_arguments(commit_parser)
    commit_parser.add_argument("--wake-id", required=True)
    commit_parser.add_argument("--review-window", required=True)
    commit_parser.add_argument("--final-status", required=True)
    commit_parser.add_argument("--incident-status", required=True)
    commit_parser.add_argument("--summary", required=True)
    commit_parser.add_argument("--next-action", required=True)
    commit_parser.add_argument("--action", action="append", default=[])
    commit_parser.add_argument("--evidence", action="append", default=[])
    commit_parser.add_argument("--changed-file", action="append", default=[])
    commit_parser.add_argument("--compact", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = _root_from_args(args)
        if args.command == "read":
            result = read_current_memory(root)
            exit_code = 2 if result["status"] == "INVALID" else 0
        else:
            result = commit_memory(
                root,
                wake_id=args.wake_id,
                review_window=args.review_window,
                final_status=args.final_status,
                incident_status=args.incident_status,
                summary=args.summary,
                next_action=args.next_action,
                actions=args.action,
                evidence_paths=args.evidence,
                changed_files=args.changed_file,
            )
            exit_code = 0
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        result = {
            "schema_version": MEMORY_POINTER_SCHEMA_VERSION,
            "status": "ERROR",
            "error": str(exc),
            "automatic_change_allowed": False,
            "orders_enabled": False,
            "orders_placed": 0,
        }
        exit_code = 2
    print(json.dumps(result, separators=(",", ":") if args.compact else None))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MEMORY_POINTER_SCHEMA_VERSION",
    "MEMORY_SCHEMA_VERSION",
    "commit_memory",
    "main",
    "memory_directory",
    "read_current_memory",
]
