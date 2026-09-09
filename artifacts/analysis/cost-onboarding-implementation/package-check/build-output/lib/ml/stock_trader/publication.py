from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import urlparse

from ml.artifacts import (
    create_timestamp_directory,
    file_checksum,
    verify_manifest,
    write_manifest,
)
from ml.stock_trader.contracts import (
    ActivationIntent,
    STOCK_TRADER_DECISION_RUN_SCHEMA_VERSION,
    STOCK_TRADER_EXECUTION_EVENT_SCHEMA_VERSION,
    StockTraderPolicy,
    TradeDecision,
    utc,
)


STOCK_TRADER_DECISION_RECEIPT_VERSION = "stock-trader-decision-receipt-v1"
STOCK_TRADER_DECISION_POINTER_VERSION = "stock-trader-decision-pointer-v1"
_EXECUTION_RESERVATION_DATABASE = "stock-trader-execution-reservations.sqlite3"
_EXECUTION_RESERVATION_TABLE = "execution_reservations_v2"


@dataclass(frozen=True)
class DecisionPublication:
    run_directory: Path
    decisions_path: Path
    manifest_path: Path
    receipt_path: Path
    receipt_checksum_sha256: str


def publish_decision_run(
    datastore_root: Path,
    decisions: Sequence[TradeDecision],
    *,
    decided_at: object,
    activation: ActivationIntent,
    policy: StockTraderPolicy,
    execution_requested: bool,
    source_files: Sequence[Path] = (),
    status: str | None = None,
    prediction_handoff: Mapping[str, object] | None = None,
    broker_state_capture: Mapping[str, object] | None = None,
) -> DecisionPublication:
    root = Path(datastore_root).resolve()
    timestamp = utc(decided_at)
    live_decisions = [
        decision for decision in decisions if decision.decision_lane == "LIVE"
    ]
    shadow_decisions = [
        decision for decision in decisions if decision.decision_lane == "SHADOW"
    ]
    run = create_timestamp_directory(
        root / "ml" / "stock-trader-decision-runs", timestamp=timestamp
    )
    decisions_path = run / "decisions.json"
    payload = {
        "schema_version": STOCK_TRADER_DECISION_RUN_SCHEMA_VERSION,
        "status": status or _run_status(decisions),
        "decided_at": timestamp.isoformat(),
        "universe": list(dict.fromkeys(decision.symbol for decision in decisions)),
        "activation": {
            "active": activation.active,
            "status": activation.status,
            "reason": activation.reason,
            "path": activation.path,
            "checksum_sha256": activation.checksum_sha256,
        },
        "execution_requested": bool(execution_requested),
        "orders_selected": sum(decision.quantity > 0 for decision in live_decisions),
        "shadow_orders_selected": sum(
            decision.quantity > 0 for decision in shadow_decisions
        ),
        "live_decision_count": len(live_decisions),
        "shadow_decision_count": len(shadow_decisions),
        "prediction_handoff": dict(prediction_handoff or {}),
        "broker_state_capture": dict(broker_state_capture or {}),
        "decisions": [decision.to_dict() for decision in decisions],
    }
    _write_json_atomic(decisions_path, payload)
    manifest_path = write_manifest(
        run,
        run_timestamp=timestamp,
        input_files=tuple(Path(path) for path in source_files if Path(path).is_file()),
        output_files=(decisions_path.name,),
        model_name=(
            str(decisions[0].enrichment.get("model_name"))
            if decisions and decisions[0].enrichment.get("model_name")
            else None
        ),
        configuration={
            "asset_class": "EQUITY",
            "options_trading_allowed": False,
            "short_selling_allowed": False,
            "execution_requested": bool(execution_requested),
            "activation_active": activation.active,
            "activation_checksum_sha256": activation.checksum_sha256,
            "live_decision_count": len(live_decisions),
            "shadow_decision_count": len(shadow_decisions),
            "prediction_handoff_status": (
                prediction_handoff.get("status") if prediction_handoff else None
            ),
            "prediction_handoff_fallback_used": bool(
                prediction_handoff.get("fallback_used", False)
                if prediction_handoff
                else False
            ),
            "broker_state_capture_status": (
                broker_state_capture.get("status") if broker_state_capture else None
            ),
            "broker_state_capture_attempts": (
                broker_state_capture.get("attempts") if broker_state_capture else 0
            ),
            "policy_version": policy.policy_version,
            "policy_fingerprint": policy.fingerprint,
        },
        datastore_root=root,
    )
    receipt_path = run / "receipt.json"
    receipt = {
        "schema_version": STOCK_TRADER_DECISION_RECEIPT_VERSION,
        "run_path": run.relative_to(root).as_posix(),
        "decided_at": timestamp.isoformat(),
        "status": payload["status"],
        "decision_ids": [decision.decision_id for decision in decisions],
        "manifest_sha256": file_checksum(manifest_path),
        "decisions_sha256": file_checksum(decisions_path),
        "execution_requested": bool(execution_requested),
        "orders_selected": payload["orders_selected"],
        "shadow_orders_selected": payload["shadow_orders_selected"],
        "prediction_handoff": dict(prediction_handoff or {}),
        "broker_state_capture": dict(broker_state_capture or {}),
    }
    _write_json_atomic(receipt_path, receipt)
    receipt_checksum = file_checksum(receipt_path)
    pointer_path = root / "ml" / "stock-trader-decision-latest" / "run.json"
    _write_json_atomic(
        pointer_path,
        {
            "schema_version": STOCK_TRADER_DECISION_POINTER_VERSION,
            "run_path": receipt["run_path"],
            "decided_at": timestamp.isoformat(),
            "manifest_sha256": receipt["manifest_sha256"],
            "receipt_sha256": receipt_checksum,
        },
    )
    return DecisionPublication(
        run_directory=run,
        decisions_path=decisions_path,
        manifest_path=manifest_path,
        receipt_path=receipt_path,
        receipt_checksum_sha256=receipt_checksum,
    )


def read_decision_run(
    datastore_root: Path, run_directory: Path
) -> tuple[dict[str, object], dict[str, object]]:
    root = Path(datastore_root).resolve()
    run = Path(run_directory).resolve()
    allowed = root / "ml" / "stock-trader-decision-runs"
    if not run.is_relative_to(allowed):
        raise ValueError("Stock trader decision run escapes its artifact directory")
    manifest = verify_manifest(run)
    decisions_path = run / "decisions.json"
    receipt_path = run / "receipt.json"
    decisions = _read_object(decisions_path, "stock trader decisions")
    receipt = _read_object(receipt_path, "stock trader decision receipt")
    if decisions.get("schema_version") != STOCK_TRADER_DECISION_RUN_SCHEMA_VERSION:
        raise ValueError(f"Unsupported stock trader decision run: {run}")
    if receipt.get("schema_version") != STOCK_TRADER_DECISION_RECEIPT_VERSION:
        raise ValueError(f"Unsupported stock trader decision receipt: {run}")
    if receipt.get("run_path") != run.relative_to(root).as_posix():
        raise ValueError("Stock trader decision receipt run path differs")
    if receipt.get("manifest_sha256") != file_checksum(run / "manifest.json"):
        raise ValueError("Stock trader decision receipt manifest checksum differs")
    if receipt.get("decisions_sha256") != file_checksum(decisions_path):
        raise ValueError("Stock trader decision receipt decisions checksum differs")
    outputs = manifest.get("output_files")
    if not isinstance(outputs, Mapping) or "decisions.json" not in outputs:
        raise ValueError("Stock trader decision manifest omits decisions.json")
    return decisions, receipt


def reserve_execution_intent(
    datastore_root: Path,
    decision: TradeDecision,
    *,
    submitted_at: object,
    decision_publication: DecisionPublication,
) -> Path | None:
    """Reserve a decision exactly once before making the broker mutation."""

    root = Path(datastore_root).resolve()
    events_root = root / "ml" / "stock-trader-execution-events"
    event_directory = events_root / decision.decision_id

    # Keep recognizing reservations made by releases that predate the durable
    # ledger.  The SQLite primary key below is the cross-process authority for
    # new reservations; synchronous=EXTRA makes its commit survive a host crash
    # before any broker POST can begin.
    if event_directory.exists():
        return None
    payload = {
        "schema_version": STOCK_TRADER_EXECUTION_EVENT_SCHEMA_VERSION,
        "event": "SUBMISSION_INTENT_RESERVED",
        "decision_id": decision.decision_id,
        "prediction_id": str(decision.prediction.get("prediction_id") or ""),
        "decision_lane": decision.decision_lane,
        "symbol": decision.symbol,
        "action": decision.action,
        "quantity": decision.quantity,
        "reserved_at": utc(submitted_at).isoformat(),
        "decision_run_path": decision_publication.run_directory.relative_to(root).as_posix(),
        "decision_receipt_sha256": decision_publication.receipt_checksum_sha256,
        "order_payload_sha256": _payload_checksum(decision.order_payload),
        "order_payload": decision.order_payload,
    }
    if not _reserve_execution_decision(root, decision.decision_id, payload):
        return None

    # The durable reservation deliberately precedes the human-readable event
    # artifact.  An artifact failure therefore suppresses a trade rather than
    # risking a duplicate after restart.
    try:
        event_directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return None
    _write_json_exclusive(event_directory / "intent.json", payload)
    return event_directory


def _reserve_execution_decision(
    datastore_root: Path,
    decision_id: str,
    payload: Mapping[str, object],
) -> bool:
    # Keep the authority directly in the already-established datastore root.
    # A newly-created events subdirectory could itself disappear from its
    # parent after a first-use power loss, taking an otherwise-synced ledger
    # with it. SQLite EXTRA can durably sync this file's root directory.
    database = Path(datastore_root) / _EXECUTION_RESERVATION_DATABASE
    encoded = json.dumps(
        dict(payload), sort_keys=True, separators=(",", ":"), default=str
    )
    connection = sqlite3.connect(database, timeout=30.0, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA synchronous = EXTRA")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_EXECUTION_RESERVATION_TABLE} (
                decision_id TEXT PRIMARY KEY,
                prediction_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                decision_lane TEXT NOT NULL,
                reserved_at TEXT NOT NULL,
                intent_json TEXT NOT NULL,
                intent_sha256 TEXT NOT NULL,
                UNIQUE (prediction_id, symbol, decision_lane)
            ) WITHOUT ROWID
            """
        )
        try:
            connection.execute(
                """
                INSERT INTO execution_reservations_v2 (
                    decision_id, prediction_id, symbol, decision_lane,
                    reserved_at, intent_json, intent_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    str(payload["prediction_id"]),
                    str(payload["symbol"]),
                    str(payload["decision_lane"]),
                    str(payload["reserved_at"]),
                    encoded,
                    hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                ),
            )
        except sqlite3.IntegrityError:
            connection.execute("ROLLBACK")
            return False
        connection.execute("COMMIT")
        return True
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def reserved_live_prediction_ids(datastore_root: Path) -> set[str]:
    """Read prediction generations durably reserved for live execution."""

    database = Path(datastore_root).resolve() / _EXECUTION_RESERVATION_DATABASE
    if not database.is_file():
        return set()
    connection = sqlite3.connect(database, timeout=30.0)
    try:
        rows = connection.execute(
            f"""
            SELECT DISTINCT prediction_id
            FROM {_EXECUTION_RESERVATION_TABLE}
            WHERE UPPER(decision_lane) = 'LIVE' AND prediction_id <> ''
            """
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return set()
        raise
    finally:
        connection.close()
    return {str(row[0]) for row in rows if row and str(row[0]).strip()}


def record_execution_result(
    event_directory: Path,
    *,
    status: str,
    completed_at: object,
    broker_location: str | None = None,
    error: str | None = None,
) -> Path:
    target = Path(event_directory) / "result.json"
    location = str(broker_location or "").strip()
    payload = {
        "schema_version": STOCK_TRADER_EXECUTION_EVENT_SCHEMA_VERSION,
        "event": "SUBMISSION_RESULT",
        "status": str(status),
        "completed_at": utc(completed_at).isoformat(),
        "broker_order_id": _broker_order_id(location),
        "broker_location_sha256": (
            hashlib.sha256(location.encode("utf-8")).hexdigest() if location else None
        ),
        "error": str(error) if error else None,
    }
    _write_json_exclusive(target, payload)
    return target


def read_execution_event(
    datastore_root: Path, decision_id: str
) -> dict[str, object] | None:
    root = Path(datastore_root).resolve()
    directory = root / "ml" / "stock-trader-execution-events" / decision_id
    if not directory.is_dir():
        return None
    intent = _read_object(directory / "intent.json", "stock trader execution intent")
    result_path = directory / "result.json"
    result = (
        _read_object(result_path, "stock trader execution result")
        if result_path.is_file()
        else {
            "status": "SUBMISSION_STATUS_UNKNOWN",
            "completed_at": None,
            "broker_order_id": None,
        }
    )
    reconciliation = _read_latest_reconciliation(directory)
    return {
        "intent": intent,
        "result": result,
        "reconciliation": reconciliation,
    }


def _read_latest_reconciliation(directory: Path) -> dict[str, object] | None:
    pointer_path = directory / "reconciliation-latest.json"
    if not pointer_path.is_file():
        return None
    pointer = _read_object(pointer_path, "stock trader reconciliation pointer")
    raw_path = pointer.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("Stock trader reconciliation pointer has no path")
    snapshot_path = (directory / raw_path).resolve()
    if not snapshot_path.is_relative_to(directory / "reconciliations"):
        raise ValueError("Stock trader reconciliation pointer escapes its event")
    if pointer.get("snapshot_sha256") != file_checksum(snapshot_path):
        raise ValueError("Stock trader reconciliation snapshot checksum differs")
    return _read_object(snapshot_path, "stock trader reconciliation snapshot")


def _run_status(decisions: Sequence[TradeDecision]) -> str:
    if any(
        decision.decision_lane == "LIVE" and decision.quantity > 0
        for decision in decisions
    ):
        return "ORDERS_SELECTED"
    if any(
        decision.decision_lane == "SHADOW" and decision.quantity > 0
        for decision in decisions
    ):
        return "SHADOW_ORDERS_SELECTED"
    return "NO_TRADE"


def _payload_checksum(payload: Mapping[str, object] | None) -> str | None:
    if payload is None:
        return None
    encoded = json.dumps(
        dict(payload), sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _broker_order_id(location: str) -> str | None:
    if not location:
        return None
    path = urlparse(location).path.rstrip("/")
    value = path.rsplit("/", 1)[-1].strip()
    return value or None


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


def _write_json_exclusive(path: Path, payload: Mapping[str, object]) -> None:
    target = Path(path)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            json.dump(dict(payload), handle, indent=2, sort_keys=True, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(target.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        # os.fsync on the file above maps to FlushFileBuffers.  The durable
        # exact-once authority is SQLite's synchronous=EXTRA transaction.
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not an object: {path}")
    return value


__all__ = [
    "DecisionPublication",
    "publish_decision_run",
    "read_decision_run",
    "read_execution_event",
    "record_execution_result",
    "reserved_live_prediction_ids",
    "reserve_execution_intent",
]
