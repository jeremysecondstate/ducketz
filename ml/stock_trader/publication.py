from __future__ import annotations

import hashlib
import json
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
) -> DecisionPublication:
    root = Path(datastore_root).resolve()
    timestamp = utc(decided_at)
    run = create_timestamp_directory(
        root / "ml" / "stock-trader-decision-runs", timestamp=timestamp
    )
    decisions_path = run / "decisions.json"
    payload = {
        "schema_version": STOCK_TRADER_DECISION_RUN_SCHEMA_VERSION,
        "status": status or _run_status(decisions),
        "decided_at": timestamp.isoformat(),
        "universe": [decision.symbol for decision in decisions],
        "activation": {
            "active": activation.active,
            "status": activation.status,
            "reason": activation.reason,
            "path": activation.path,
            "checksum_sha256": activation.checksum_sha256,
        },
        "execution_requested": bool(execution_requested),
        "orders_selected": sum(decision.quantity > 0 for decision in decisions),
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
    event_directory = (
        root / "ml" / "stock-trader-execution-events" / decision.decision_id
    )
    try:
        event_directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return None
    payload = {
        "schema_version": STOCK_TRADER_EXECUTION_EVENT_SCHEMA_VERSION,
        "event": "SUBMISSION_INTENT_RESERVED",
        "decision_id": decision.decision_id,
        "symbol": decision.symbol,
        "action": decision.action,
        "quantity": decision.quantity,
        "reserved_at": utc(submitted_at).isoformat(),
        "decision_run_path": decision_publication.run_directory.relative_to(root).as_posix(),
        "decision_receipt_sha256": decision_publication.receipt_checksum_sha256,
        "order_payload_sha256": _payload_checksum(decision.order_payload),
        "order_payload": decision.order_payload,
    }
    _write_json_exclusive(event_directory / "intent.json", payload)
    return event_directory


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
    if any(decision.quantity > 0 for decision in decisions):
        return "ORDERS_SELECTED"
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
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(dict(payload), handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")


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
    "reserve_execution_intent",
]
