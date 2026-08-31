from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from app.services.schwab import SchwabSession
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.artifacts import create_timestamp_directory, file_checksum
from ml.stock_trader.contracts import (
    STOCK_TRADER_EXECUTION_EVENT_SCHEMA_VERSION,
    finite,
    utc,
)
from ml.stock_trader.publication import read_execution_event


STOCK_TRADER_RECONCILIATION_POINTER_VERSION = (
    "stock-trader-execution-reconciliation-pointer-v1"
)


class SchwabOrderHistorySession(Protocol):
    def get_recent_orders(self) -> Any: ...


@dataclass(frozen=True)
class ReconciliationResult:
    status: str
    observed_at: str
    submitted_event_count: int
    matched_order_count: int
    unmatched_order_count: int
    snapshot_paths: tuple[Path, ...]


def reconcile_submitted_orders(
    datastore_root: Path,
    *,
    session: SchwabOrderHistorySession,
    observed_at: object | None = None,
) -> ReconciliationResult:
    """Attach read-only broker order/fill state after the submit critical path."""

    root = Path(datastore_root).resolve()
    timestamp = utc(observed_at)
    events_root = root / "ml" / "stock-trader-execution-events"
    submitted: list[tuple[str, Mapping[str, object]]] = []
    if events_root.is_dir():
        for directory in sorted(path for path in events_root.iterdir() if path.is_dir()):
            event = read_execution_event(root, directory.name)
            result = event.get("result") if isinstance(event, Mapping) else None
            if (
                isinstance(result, Mapping)
                and result.get("status") == "SUBMITTED"
                and result.get("broker_order_id")
            ):
                submitted.append((directory.name, result))
    if not submitted:
        return ReconciliationResult(
            status="NO_SUBMITTED_ORDERS",
            observed_at=timestamp.isoformat(),
            submitted_event_count=0,
            matched_order_count=0,
            unmatched_order_count=0,
            snapshot_paths=(),
        )
    raw_orders = session.get_recent_orders()
    if not isinstance(raw_orders, list):
        raise ValueError("Schwab recent-order history is not a list")
    orders = {
        order_id: row
        for row in _walk_orders(raw_orders)
        if (order_id := str(row.get("orderId") or "").strip())
    }
    paths: list[Path] = []
    matched = 0
    for decision_id, result in submitted:
        broker_order_id = str(result["broker_order_id"])
        raw_order = orders.get(broker_order_id)
        if raw_order is None:
            snapshot = {
                "schema_version": STOCK_TRADER_EXECUTION_EVENT_SCHEMA_VERSION,
                "event": "BROKER_RECONCILIATION_SNAPSHOT",
                "decision_id": decision_id,
                "broker_order_id": broker_order_id,
                "observed_at": timestamp.isoformat(),
                "reconciliation_status": "ORDER_NOT_FOUND_IN_RECENT_HISTORY",
                "broker_status": None,
                "filled_quantity": 0.0,
                "remaining_quantity": None,
                "average_fill_price": None,
                "fill_count": 0,
            }
        else:
            matched += 1
            snapshot = _reconciliation_snapshot(
                decision_id,
                broker_order_id,
                raw_order,
                observed_at=timestamp.isoformat(),
            )
        paths.append(_publish_snapshot(root, decision_id, timestamp, snapshot))
    return ReconciliationResult(
        status=(
            "RECONCILED"
            if matched == len(submitted)
            else "PARTIAL_RECONCILIATION"
        ),
        observed_at=timestamp.isoformat(),
        submitted_event_count=len(submitted),
        matched_order_count=matched,
        unmatched_order_count=len(submitted) - matched,
        snapshot_paths=tuple(paths),
    )


def _reconciliation_snapshot(
    decision_id: str,
    broker_order_id: str,
    order: Mapping[str, object],
    *,
    observed_at: str,
) -> dict[str, object]:
    fills = _execution_fills(order)
    fill_quantity = sum(quantity for quantity, _price in fills)
    fill_notional = sum(quantity * price for quantity, price in fills)
    top_level_filled = finite(order.get("filledQuantity"))
    if fill_quantity <= 0.0 and top_level_filled is not None:
        fill_quantity = max(0.0, top_level_filled)
    average_fill = fill_notional / fill_quantity if fill_notional > 0.0 else None
    return {
        "schema_version": STOCK_TRADER_EXECUTION_EVENT_SCHEMA_VERSION,
        "event": "BROKER_RECONCILIATION_SNAPSHOT",
        "decision_id": decision_id,
        "broker_order_id": broker_order_id,
        "observed_at": observed_at,
        "reconciliation_status": "MATCHED",
        "broker_status": str(order.get("status") or "UNKNOWN").upper(),
        "order_type": str(order.get("orderType") or "").upper() or None,
        "session": str(order.get("session") or "").upper() or None,
        "duration": str(order.get("duration") or "").upper() or None,
        "entered_time": order.get("enteredTime"),
        "close_time": order.get("closeTime"),
        "requested_quantity": finite(order.get("quantity")),
        "filled_quantity": fill_quantity,
        "remaining_quantity": finite(order.get("remainingQuantity")),
        "average_fill_price": average_fill,
        "fill_count": len(fills),
        "limit_price": finite(order.get("price")),
        "stop_price": finite(order.get("stopPrice")),
    }


def _execution_fills(order: Mapping[str, object]) -> list[tuple[float, float]]:
    fills: list[tuple[float, float]] = []
    activities = order.get("orderActivityCollection")
    if not isinstance(activities, list):
        return fills
    for activity in activities:
        if not isinstance(activity, Mapping):
            continue
        legs = activity.get("executionLegs")
        if not isinstance(legs, list):
            continue
        for leg in legs:
            if not isinstance(leg, Mapping):
                continue
            quantity = finite(leg.get("quantity"))
            price = finite(leg.get("price"))
            if quantity is not None and price is not None and quantity > 0.0 and price > 0.0:
                fills.append((quantity, price))
    return fills


def _walk_orders(rows: Sequence[object]):
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        yield row
        children = row.get("childOrderStrategies")
        if isinstance(children, list):
            yield from _walk_orders(children)


def _publish_snapshot(
    root: Path,
    decision_id: str,
    timestamp: object,
    snapshot: Mapping[str, object],
) -> Path:
    event_directory = root / "ml" / "stock-trader-execution-events" / decision_id
    run = create_timestamp_directory(
        event_directory / "reconciliations", timestamp=timestamp
    )
    snapshot_path = run / "snapshot.json"
    _write_json_atomic(snapshot_path, snapshot)
    pointer_path = event_directory / "reconciliation-latest.json"
    _write_json_atomic(
        pointer_path,
        {
            "schema_version": STOCK_TRADER_RECONCILIATION_POINTER_VERSION,
            "path": snapshot_path.relative_to(event_directory).as_posix(),
            "observed_at": snapshot.get("observed_at"),
            "snapshot_sha256": file_checksum(snapshot_path),
        },
    )
    return snapshot_path


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reconcile submitted stock-trader decisions with Schwab order history."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--root-dir", type=Path)
    group.add_argument("--datastore-target", choices=sorted(DATASTORE_TARGETS))
    parser.add_argument("--observed-at")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = resolve_datastore_dir(
            root_dir=args.root_dir, target=args.datastore_target
        )
        with exclusive_runtime_lock(
            root / "locks" / "stock-trader-reconciliation.lock",
            process_name="stock-trader-reconciliation",
        ):
            result = reconcile_submitted_orders(
                root,
                session=SchwabSession(),
                observed_at=args.observed_at,
            )
    except Exception as exc:
        print(json.dumps({"status": "FAILED", "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(
        json.dumps(
            {
                "status": result.status,
                "observed_at": result.observed_at,
                "submitted_event_count": result.submitted_event_count,
                "matched_order_count": result.matched_order_count,
                "unmatched_order_count": result.unmatched_order_count,
                "snapshot_paths": [str(path) for path in result.snapshot_paths],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ReconciliationResult", "main", "reconcile_submitted_orders"]
