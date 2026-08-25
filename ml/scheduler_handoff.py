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


HANDOFF_SCHEMA_VERSION = "loops-hourly-scheduler-handoff-v1"
HANDOFF_POINTER_SCHEMA_VERSION = "loops-hourly-scheduler-handoff-pointer-v1"
_HANDOFF_RELATIVE_DIRECTORY = (
    Path("logs") / "ducketz" / "system-guardian" / "scheduler-handoff"
)
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")


def handoff_directory(root: Path | str) -> Path:
    return (Path(root).resolve() / _HANDOFF_RELATIVE_DIRECTORY).resolve()


def read_current_handoff(root: Path | str) -> dict[str, object]:
    directory = handoff_directory(root)
    pointer_path = directory / "current.json"
    try:
        if not pointer_path.exists():
            receipts = list((directory / "runs").glob("*.json"))
            if receipts:
                raise ValueError("handoff receipts exist but current.json is missing")
            return {
                "schema_version": HANDOFF_POINTER_SCHEMA_VERSION,
                "status": "EMPTY",
                "pointer_path": str(pointer_path),
            }

        pointer = _read_mapping(pointer_path)
        if pointer.get("schema_version") != HANDOFF_POINTER_SCHEMA_VERSION:
            raise ValueError("current.json has an unsupported schema_version")
        sequence = _positive_int(pointer.get("sequence"), "pointer sequence")
        current = _required_text(pointer.get("current"), "pointer current", 512)
        checksum = _sha256_text(pointer.get("receipt_sha256"), "pointer receipt_sha256")
        receipt_path = _receipt_path(directory, current, "current.json")
        receipt = _verify_current_link(
            directory,
            receipt_path=receipt_path,
            expected_checksum=checksum,
            expected_sequence=sequence,
        )
        return {
            "schema_version": HANDOFF_POINTER_SCHEMA_VERSION,
            "status": "VALID",
            "pointer_path": str(pointer_path),
            "receipt_path": str(receipt_path),
            "receipt_sha256": checksum,
            "handoff": receipt,
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "schema_version": HANDOFF_POINTER_SCHEMA_VERSION,
            "status": "INVALID",
            "pointer_path": str(pointer_path),
            "error": str(exc),
        }


def commit_handoff(
    root: Path | str,
    *,
    wake_id: str,
    monitor_mode: str,
    lane: str,
    stage_id: str,
    eligible_session: str,
    final_status: str,
    stage_disposition: str,
    incident_status: str,
    summary: str,
    next_action: str,
    checked_at: str | None = None,
    stage_index: int | None = None,
    actions: Sequence[str] = (),
    evidence_paths: Sequence[str] = (),
    changed_files: Sequence[str] = (),
    created_at: datetime | None = None,
) -> dict[str, object]:
    directory = handoff_directory(root)
    directory.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(directory / "commit.lock"), timeout=5)
    try:
        with lock:
            return _commit_handoff_locked(
                root,
                wake_id=wake_id,
                monitor_mode=monitor_mode,
                lane=lane,
                stage_id=stage_id,
                eligible_session=eligible_session,
                final_status=final_status,
                stage_disposition=stage_disposition,
                incident_status=incident_status,
                summary=summary,
                next_action=next_action,
                checked_at=checked_at,
                stage_index=stage_index,
                actions=actions,
                evidence_paths=evidence_paths,
                changed_files=changed_files,
                created_at=created_at,
            )
    except Timeout as exc:
        raise ValueError("another scheduler handoff commit is in progress") from exc


def _commit_handoff_locked(
    root: Path | str,
    *,
    wake_id: str,
    monitor_mode: str,
    lane: str,
    stage_id: str,
    eligible_session: str,
    final_status: str,
    stage_disposition: str,
    incident_status: str,
    summary: str,
    next_action: str,
    checked_at: str | None,
    stage_index: int | None,
    actions: Sequence[str],
    evidence_paths: Sequence[str],
    changed_files: Sequence[str],
    created_at: datetime | None,
) -> dict[str, object]:
    current = read_current_handoff(root)
    current_status = str(current["status"])
    if current_status == "INVALID":
        raise ValueError(
            "refusing to extend an invalid handoff chain: "
            + str(current.get("error", "unknown validation error"))
        )
    normalized_wake_id = _iso_timestamp(wake_id, "wake_id")
    prior: dict[str, object] | None = None
    sequence = 1
    if current_status == "VALID":
        previous = current["handoff"]
        if not isinstance(previous, Mapping):
            raise ValueError("validated handoff payload is not an object")
        if _receipt_wake_id(previous) == normalized_wake_id:
            return {
                "schema_version": HANDOFF_POINTER_SCHEMA_VERSION,
                "status": "ALREADY_COMMITTED",
                "wake_id": normalized_wake_id,
                "sequence": _positive_int(previous.get("sequence"), "prior sequence"),
                "pointer_path": str(current["pointer_path"]),
                "receipt_path": str(current["receipt_path"]),
                "receipt_sha256": str(current["receipt_sha256"]),
            }
        sequence = _positive_int(previous.get("sequence"), "prior sequence") + 1
        prior = {
            "sequence": sequence - 1,
            "receipt_path": str(current["receipt_path"]),
            "receipt_sha256": str(current["receipt_sha256"]),
        }

    timestamp = created_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        raise ValueError("created_at must include a timezone")
    timestamp = timestamp.astimezone(timezone.utc)
    created_text = timestamp.isoformat().replace("+00:00", "Z")
    schedule: dict[str, object] = {
        "monitor_mode": _token(monitor_mode, "monitor_mode"),
        "lane": _token(lane, "lane"),
        "stage_id": _token(stage_id, "stage_id"),
        "eligible_session": _required_text(
            eligible_session, "eligible_session", 128
        ),
    }
    if stage_index is not None:
        if stage_index < 0:
            raise ValueError("stage_index must be non-negative")
        schedule["stage_index"] = stage_index
    if checked_at is not None:
        schedule["checked_at"] = _iso_timestamp(checked_at, "checked_at")

    receipt: dict[str, object] = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "wake_id": normalized_wake_id,
        "sequence": sequence,
        "created_at": created_text,
        "previous": prior,
        "schedule": schedule,
        "outcome": {
            "final_status": _token(final_status, "final_status"),
            "stage_disposition": _token(
                stage_disposition, "stage_disposition"
            ),
            "incident_status": _token(incident_status, "incident_status"),
        },
        "continuity": {
            "summary": _required_text(summary, "summary", 2400),
            "next_action": _required_text(next_action, "next_action", 1200),
            "actions": _text_list(actions, "actions", 64, 1200),
            "evidence_paths": _text_list(
                evidence_paths, "evidence_paths", 128, 2048
            ),
            "changed_files": _text_list(changed_files, "changed_files", 64, 2048),
        },
        "safety": {
            "authority": "ADVISORY_HANDOFF_ONLY",
            "orders_placed": 0,
            "resume_requires_live_revalidation": True,
            "routing_authority": "guardian schedule metadata and verified receipts",
        },
    }

    directory = handoff_directory(root)
    receipt_name = f"{sequence:08d}-{_timestamp_slug(timestamp)}.json"
    receipt_path = directory / "runs" / receipt_name
    _write_unique_json(receipt_path, receipt)
    receipt_checksum = _sha256_path(receipt_path)
    pointer = {
        "schema_version": HANDOFF_POINTER_SCHEMA_VERSION,
        "sequence": sequence,
        "updated_at": created_text,
        "current": receipt_path.relative_to(directory).as_posix(),
        "receipt_sha256": receipt_checksum,
    }
    pointer_path = directory / "current.json"
    _atomic_replace_json(pointer_path, pointer)
    return {
        "schema_version": HANDOFF_POINTER_SCHEMA_VERSION,
        "status": "COMMITTED",
        "sequence": sequence,
        "pointer_path": str(pointer_path),
        "receipt_path": str(receipt_path),
        "receipt_sha256": receipt_checksum,
    }


def _read_mapping(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _receipt_wake_id(receipt: Mapping[str, object]) -> str | None:
    value = receipt.get("wake_id")
    if value is None:
        schedule = receipt.get("schedule")
        if isinstance(schedule, Mapping):
            value = schedule.get("checked_at")
    if value is None:
        return None
    return _iso_timestamp(value, "receipt wake_id")


def _verify_current_link(
    directory: Path,
    *,
    receipt_path: Path,
    expected_checksum: str,
    expected_sequence: int,
) -> Mapping[str, object]:
    if not receipt_path.is_file():
        raise ValueError(f"handoff receipt {expected_sequence} is missing")
    if _sha256_path(receipt_path) != expected_checksum:
        raise ValueError(
            f"handoff receipt {expected_sequence} checksum does not match"
        )
    receipt = _read_mapping(receipt_path)
    if receipt.get("schema_version") != HANDOFF_SCHEMA_VERSION:
        raise ValueError(
            f"handoff receipt {expected_sequence} has an unsupported schema_version"
        )
    if _positive_int(receipt.get("sequence"), "receipt sequence") != expected_sequence:
        raise ValueError(f"handoff receipt {expected_sequence} has the wrong sequence")
    previous = receipt.get("previous")
    if expected_sequence == 1:
        if previous is not None:
            raise ValueError("the first handoff receipt must not have a predecessor")
        return receipt
    if not isinstance(previous, Mapping):
        raise ValueError(f"handoff receipt {expected_sequence} has no predecessor")
    prior_sequence = _positive_int(previous.get("sequence"), "prior sequence")
    if prior_sequence != expected_sequence - 1:
        raise ValueError(f"handoff receipt {expected_sequence} skips its predecessor")
    prior_path = _receipt_path(
        directory,
        _required_text(previous.get("receipt_path"), "prior receipt_path", 2048),
        f"handoff receipt {expected_sequence}",
    )
    if not prior_path.is_file():
        raise ValueError(f"handoff receipt {prior_sequence} is missing")
    prior_checksum = _sha256_text(
        previous.get("receipt_sha256"), "prior receipt_sha256"
    )
    if _sha256_path(prior_path) != prior_checksum:
        raise ValueError(f"handoff receipt {prior_sequence} checksum does not match")
    prior_receipt = _read_mapping(prior_path)
    if prior_receipt.get("schema_version") != HANDOFF_SCHEMA_VERSION:
        raise ValueError(
            f"handoff receipt {prior_sequence} has an unsupported schema_version"
        )
    if _positive_int(prior_receipt.get("sequence"), "prior receipt sequence") != (
        prior_sequence
    ):
        raise ValueError(f"handoff receipt {prior_sequence} has the wrong sequence")
    return receipt


def _receipt_path(directory: Path, value: str, source: str) -> Path:
    candidate = Path(value)
    path = candidate.resolve() if candidate.is_absolute() else (directory / candidate).resolve()
    runs_directory = (directory / "runs").resolve()
    if not path.is_relative_to(runs_directory):
        raise ValueError(f"{source} points outside the handoff runs directory")
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
        description="Read or commit the receipt-verified hourly scheduler handoff."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    read_parser = subparsers.add_parser("read")
    _add_root_arguments(read_parser)
    read_parser.add_argument("--compact", action="store_true")

    commit_parser = subparsers.add_parser("commit")
    _add_root_arguments(commit_parser)
    commit_parser.add_argument("--wake-id", required=True)
    commit_parser.add_argument("--monitor-mode", required=True)
    commit_parser.add_argument("--lane", required=True)
    commit_parser.add_argument("--stage-id", required=True)
    commit_parser.add_argument("--stage-index", type=int)
    commit_parser.add_argument("--eligible-session", required=True)
    commit_parser.add_argument("--checked-at")
    commit_parser.add_argument("--final-status", required=True)
    commit_parser.add_argument("--stage-disposition", required=True)
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
            result = read_current_handoff(root)
            exit_code = 2 if result["status"] == "INVALID" else 0
        else:
            result = commit_handoff(
                root,
                wake_id=args.wake_id,
                monitor_mode=args.monitor_mode,
                lane=args.lane,
                stage_id=args.stage_id,
                stage_index=args.stage_index,
                eligible_session=args.eligible_session,
                checked_at=args.checked_at,
                final_status=args.final_status,
                stage_disposition=args.stage_disposition,
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
            "schema_version": HANDOFF_POINTER_SCHEMA_VERSION,
            "status": "ERROR",
            "error": str(exc),
        }
        exit_code = 2
    print(json.dumps(result, separators=(",", ":") if args.compact else None))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
