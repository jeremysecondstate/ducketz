from __future__ import annotations

import argparse
import copy
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

import pandas as pd
from filelock import FileLock

from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from ml.artifacts import (
    create_timestamp_directory,
    file_checksum,
    refresh_latest_file,
    utc_timestamp,
    verify_manifest,
    write_manifest,
)
from ml.loop_c.inputs import (
    LOOP_C_APPROVAL_SCOPE,
    LOOP_C_HALT_CONTROL_SCHEMA_VERSION,
    LOOP_C_RISK_APPROVAL_SCHEMA_VERSION,
    _risk_limits,
)
from ml.loop_c.policy import expected_sequence_model_binding


LOOP_C_OPERATOR_CONTROL_SCHEMA_VERSION = "loop-c-operator-control-issuance-v1"
_PACIFIC = ZoneInfo("America/Los_Angeles")


def issue_loop_c_operator_controls(
    datastore_root: Path,
    *,
    pending_risk_approval_path: Path,
    approved_by: str,
    expires_at: object,
    rationale: str,
    halt_requested: bool,
    approved_at: object | None = None,
    replace_current: bool = False,
) -> dict[str, object]:
    """Issue explicit, time-limited Loop C observe-only controls.

    The pending proposal is immutable input. This function changes only approval
    metadata, issues an independent halt control, and copies the verified pair to
    the canonical control paths. It grants no ranking or broker authority.
    """

    root = Path(datastore_root).resolve()
    pending_path = Path(pending_risk_approval_path).resolve()
    proposal_root = (root / "controls" / "loop-c" / "proposals").resolve()
    if not pending_path.is_relative_to(proposal_root):
        raise ValueError("Pending risk approval must be inside the Loop C proposal root")
    if pending_path.name != "risk-approval.pending.json" or not pending_path.is_file():
        raise ValueError("Pending risk approval path is missing or not a proposal artifact")

    run_directory = pending_path.parent
    manifest = verify_manifest(run_directory)
    configuration = manifest.get("configuration")
    if not isinstance(configuration, Mapping):
        raise ValueError("Pending proposal manifest configuration is invalid")
    if (
        configuration.get("status") != "PENDING_OPERATOR_APPROVAL"
        or configuration.get("authority") != "PROPOSAL_ONLY"
        or configuration.get("orders_enabled") is not False
        or configuration.get("orders_placed") != 0
    ):
        raise ValueError("Pending proposal manifest does not preserve proposal-only safety")

    pending = _read_object(pending_path, "pending risk approval")
    proposal_path = run_directory / "proposal.json"
    proposal = _read_object(proposal_path, "pending proposal")
    if proposal.get("risk_approval") != pending:
        raise ValueError("Pending approval does not match its receipt-backed proposal")
    if pending.get("schema_version") != LOOP_C_RISK_APPROVAL_SCHEMA_VERSION:
        raise ValueError("Pending approval uses an unsupported Loop C risk schema")
    pending_metadata = pending.get("approval")
    if not isinstance(pending_metadata, Mapping):
        raise ValueError("Pending approval metadata is invalid")
    if (
        pending_metadata.get("status") != "PENDING_OPERATOR_APPROVAL"
        or pending_metadata.get("scope") != LOOP_C_APPROVAL_SCOPE
        or pending_metadata.get("approved_by") is not None
        or pending_metadata.get("approved_at") is not None
        or pending_metadata.get("expires_at") is not None
        or pending_metadata.get("rationale") is not None
    ):
        raise ValueError("Source risk record is not an untouched pending approval")

    expected_binding = asdict(expected_sequence_model_binding())
    expected_binding["horizons"] = list(expected_binding["horizons"])
    if pending.get("model_binding") != expected_binding:
        raise ValueError("Pending approval does not match the exact Loop C model binding")
    if configuration.get("model_configuration_fingerprint") != expected_binding[
        "configuration_fingerprint"
    ]:
        raise ValueError("Proposal manifest and model binding fingerprints disagree")

    identity = str(approved_by).strip()
    reason = str(rationale).strip()
    if not identity:
        raise ValueError("approved_by is required")
    if not reason:
        raise ValueError("approval rationale is required")
    if not isinstance(halt_requested, bool):
        raise ValueError("halt_requested must be an explicit boolean")

    issued = utc_timestamp(approved_at)
    expires = utc_timestamp(expires_at)
    _validate_weekly_expiry(issued, expires)
    identifier_stamp = issued.strftime("%Y%m%dT%H%M%S%fZ").lower()

    approved = copy.deepcopy(dict(pending))
    approved["approval"] = {
        "status": "APPROVED",
        "approval_id": f"loop-c-observe-{identifier_stamp}",
        "approved_by": identity,
        "approved_at": issued.isoformat(),
        "expires_at": expires.isoformat(),
        "scope": LOOP_C_APPROVAL_SCOPE,
        "rationale": reason,
    }
    # Reuse the runtime's strict schema validation before any canonical write.
    _risk_limits(approved, as_of=issued)

    halt_control = {
        "schema_version": LOOP_C_HALT_CONTROL_SCHEMA_VERSION,
        "control_id": f"loop-c-{'halt' if halt_requested else 'unhalt'}-{identifier_stamp}",
        "issued_at": issued.isoformat(),
        "expires_at": expires.isoformat(),
        "halt_requested": halt_requested,
        "set_by": identity,
    }
    receipt = {
        "schema_version": LOOP_C_OPERATOR_CONTROL_SCHEMA_VERSION,
        "status": "HALTED" if halt_requested else "OBSERVE_ONLY_LEASE_ISSUED",
        "issued_at": issued.isoformat(),
        "expires_at": expires.isoformat(),
        "source": {
            "pending_risk_approval": str(pending_path.relative_to(root)).replace("\\", "/"),
            "pending_risk_approval_sha256": file_checksum(pending_path),
            "proposal": str(proposal_path.relative_to(root)).replace("\\", "/"),
            "proposal_sha256": file_checksum(proposal_path),
            "proposal_manifest_sha256": file_checksum(run_directory / "manifest.json"),
        },
        "approval": {
            "approval_id": approved["approval"]["approval_id"],
            "approved_by": identity,
            "scope": LOOP_C_APPROVAL_SCOPE,
            "rationale": reason,
        },
        "halt_control_id": halt_control["control_id"],
        "halt_requested": halt_requested,
        "safety": {
            "authority": "OBSERVE_ONLY",
            "automated_action_allowed": False,
            "broker_submission_path_present": False,
            "orders_enabled": False,
            "orders_placed": 0,
        },
    }

    current = root / "controls" / "loop-c" / "current"
    current_approval = current / "risk-approval.json"
    current_halt = current / "halt-control.json"
    lock_path = root / "controls" / "loop-c" / ".operator-controls.lock"
    with FileLock(str(lock_path), timeout=0):
        if not replace_current and (current_approval.exists() or current_halt.exists()):
            raise FileExistsError(
                "Canonical Loop C controls already exist; explicit replacement is required"
            )
        run = create_timestamp_directory(
            root / "controls" / "loop-c" / "operator-control-runs",
            timestamp=issued,
        )
        approval_artifact = run / "risk-approval.json"
        halt_artifact = run / "halt-control.json"
        receipt_path = run / "receipt.json"
        _write_json_atomic(approval_artifact, approved)
        _write_json_atomic(halt_artifact, halt_control)
        _write_json_atomic(receipt_path, receipt)
        manifest_path = write_manifest(
            run,
            run_timestamp=issued,
            input_files=(pending_path, proposal_path, run_directory / "manifest.json"),
            output_files=(
                approval_artifact.name,
                halt_artifact.name,
                receipt_path.name,
            ),
            configuration={
                "schema_version": LOOP_C_OPERATOR_CONTROL_SCHEMA_VERSION,
                "scope": LOOP_C_APPROVAL_SCOPE,
                "halt_requested": halt_requested,
                "authority": "OBSERVE_ONLY",
                "orders_enabled": False,
                "orders_placed": 0,
            },
            datastore_root=root,
        )
        # A partially replaced pair fails closed because Loop C requires both files.
        refresh_latest_file(approval_artifact, current_approval)
        refresh_latest_file(halt_artifact, current_halt)
        if file_checksum(current_approval) != file_checksum(approval_artifact):
            raise RuntimeError("Canonical Loop C risk approval failed verification")
        if file_checksum(current_halt) != file_checksum(halt_artifact):
            raise RuntimeError("Canonical Loop C halt control failed verification")

    return {
        "status": receipt["status"],
        "run_directory": str(run),
        "manifest_path": str(manifest_path),
        "receipt_path": str(receipt_path),
        "risk_approval_path": str(current_approval),
        "halt_control_path": str(current_halt),
        "approval_id": approved["approval"]["approval_id"],
        "approved_by": identity,
        "approved_at": issued.isoformat(),
        "expires_at": expires.isoformat(),
        "halt_requested": halt_requested,
        "model_configuration_fingerprint": expected_binding[
            "configuration_fingerprint"
        ],
        "limits": approved["limits"],
        "safety": receipt["safety"],
    }


def _validate_weekly_expiry(issued: pd.Timestamp, expires: pd.Timestamp) -> None:
    if expires <= issued:
        raise ValueError("Loop C approval expiry must be after issuance")
    local = expires.to_pydatetime().astimezone(_PACIFIC)
    if (
        local.weekday() != 4
        or local.hour != 17
        or local.minute != 0
        or local.second != 0
        or local.microsecond != 0
    ):
        raise ValueError(
            "Loop C pilot approval must expire Friday at 17:00 America/Los_Angeles"
        )
    if expires - issued > pd.Timedelta(days=8):
        raise ValueError("Loop C pilot approval cannot exceed one weekly lease")


def _read_object(path: Path, label: str) -> Mapping[str, object]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is unreadable: {path}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    return payload


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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Issue explicit weekly Loop C observe-only operator controls."
    )
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--root-dir", type=Path, default=None)
    datastore.add_argument(
        "--datastore-target", choices=tuple(DATASTORE_TARGETS), default="pc"
    )
    parser.add_argument("--pending-risk-approval", type=Path, required=True)
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--approved-at", default=None)
    parser.add_argument("--expires-at", required=True)
    parser.add_argument("--rationale", required=True)
    state = parser.add_mutually_exclusive_group(required=True)
    state.add_argument("--unhalt", action="store_false", dest="halt_requested")
    state.add_argument("--halt-requested", action="store_true", dest="halt_requested")
    parser.set_defaults(halt_requested=None)
    parser.add_argument("--approve-observe-only", action="store_true")
    parser.add_argument("--replace-current", action="store_true")
    parser.add_argument("--compact", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.approve_observe_only:
        raise SystemExit("--approve-observe-only is required")
    root = (
        args.root_dir.resolve()
        if args.root_dir is not None
        else resolve_datastore_dir(target=args.datastore_target)
    )
    result = issue_loop_c_operator_controls(
        root,
        pending_risk_approval_path=args.pending_risk_approval,
        approved_by=args.approved_by,
        approved_at=args.approved_at,
        expires_at=args.expires_at,
        rationale=args.rationale,
        halt_requested=args.halt_requested,
        replace_current=args.replace_current,
    )
    print(
        json.dumps(
            result,
            separators=(",", ":") if args.compact else None,
            indent=None if args.compact else 2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
