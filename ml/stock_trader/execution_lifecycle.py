from __future__ import annotations

import json
import math
from collections import Counter, deque
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

from ml.artifacts import file_checksum
from ml.stock_trader.contracts import (
    STOCK_TRADER_EXECUTION_EVENT_SCHEMA_VERSION,
    finite,
    utc,
)


STOCK_TRADER_EXECUTION_LIFECYCLE_VERSION = "stock-trader-execution-lifecycle-v1"


def build_stock_trader_execution_lifecycle(
    datastore_root: Path,
    decisions: Sequence[Mapping[str, object]],
    *,
    window_start: object,
    window_end: object,
) -> tuple[dict[str, object], tuple[Path, ...]]:
    """Attribute logged live stock fills into conservative local FIFO round trips.

    This is deliberately not Schwab tax-lot P/L. Only fills tied to stock-trader
    decision receipts are eligible, and sells without a prior tracked buy remain
    explicitly unattributed.
    """

    root = Path(datastore_root).resolve()
    start = utc(window_start)
    end = utc(window_end)
    if end <= start:
        raise ValueError("Execution lifecycle window_end must follow window_start")
    fills: list[dict[str, object]] = []
    sources: list[Path] = []
    submitted_without_fill = 0
    decision_by_id: dict[str, Mapping[str, object]] = {}
    for decision in decisions:
        decision_id = str(decision.get("decision_id") or "")
        lane = str(decision.get("decision_lane") or "LIVE").upper()
        action = str(decision.get("action") or "").upper()
        if (
            not decision_id
            or lane != "LIVE"
            or action not in {"BUY", "SELL"}
            or decision_id in decision_by_id
        ):
            continue
        decision_by_id[decision_id] = decision
        event, event_sources = _read_execution_evidence(root, decision_id)
        sources.extend(event_sources)
        if event is None:
            continue
        intent = event["intent"]
        result = event["result"]
        reconciliation = event.get("reconciliation")
        if (
            intent.get("decision_id") != decision_id
            or str(intent.get("action") or "").upper() != action
            or str(intent.get("symbol") or "").upper()
            != str(decision.get("symbol") or "").upper()
        ):
            raise ValueError("Stock execution intent differs from its decision receipt")
        if result.get("status") != "SUBMITTED":
            continue
        if not isinstance(reconciliation, Mapping):
            submitted_without_fill += 1
            continue
        decision_fills = _normalized_fills(
            decision,
            reconciliation,
            decision_id=decision_id,
        )
        if not decision_fills:
            submitted_without_fill += 1
        fills.extend(fill for fill in decision_fills if fill["executed_at"] < end)
    fills.sort(
        key=lambda row: (
            row["executed_at"],
            str(row["decision_id"]),
            int(row["fill_index"]),
        )
    )

    inventories: dict[str, deque[dict[str, object]]] = {}
    closures: list[dict[str, object]] = []
    unmatched_sells: list[dict[str, object]] = []
    for fill in fills:
        symbol = str(fill["symbol"])
        inventory = inventories.setdefault(symbol, deque())
        if fill["action"] == "BUY":
            inventory.append(dict(fill))
            continue
        remaining = float(fill["quantity"])
        while remaining > 1.0e-12 and inventory:
            lot = inventory[0]
            available = float(lot["remaining_quantity"])
            matched = min(remaining, available)
            gross_pnl = matched * (
                float(fill["price"]) - float(lot["price"])
            )
            closure = {
                "symbol": symbol,
                "quantity": matched,
                "entry_decision_id": lot["decision_id"],
                "entry_prediction_id": lot.get("prediction_id"),
                "entry_checkpoint_session": lot.get("checkpoint_session"),
                "entry_executed_at": lot["executed_at"].isoformat(),
                "entry_price": lot["price"],
                "exit_decision_id": fill["decision_id"],
                "exit_prediction_id": fill.get("prediction_id"),
                "exit_checkpoint_session": fill.get("checkpoint_session"),
                "exit_executed_at": fill["executed_at"].isoformat(),
                "exit_price": fill["price"],
                "gross_realized_pnl_before_unavailable_fees": round(
                    gross_pnl, 8
                ),
                "holding_seconds": max(
                    0.0,
                    (fill["executed_at"] - lot["executed_at"]).total_seconds(),
                ),
                "attribution": "STOCK_TRADER_RECEIPT_MATCHED_FIFO_FILL_PAIR",
            }
            closures.append(closure)
            remaining -= matched
            lot["remaining_quantity"] = available - matched
            if float(lot["remaining_quantity"]) <= 1.0e-12:
                inventory.popleft()
        if remaining > 1.0e-12:
            unmatched_sells.append(
                {
                    "symbol": symbol,
                    "decision_id": fill["decision_id"],
                    "prediction_id": fill.get("prediction_id"),
                    "executed_at": fill["executed_at"].isoformat(),
                    "quantity": remaining,
                    "price": fill["price"],
                    "reason": "NO_PRIOR_RECEIPT_MATCHED_STOCK_TRADER_BUY_LOT",
                }
            )

    weekly_closures = [
        closure
        for closure in closures
        if start
        <= utc(closure["exit_executed_at"])
        < end
    ]
    weekly_unmatched = [
        row
        for row in unmatched_sells
        if start <= utc(row["executed_at"]) < end
    ]
    open_lots: list[dict[str, object]] = []
    for symbol, inventory in sorted(inventories.items()):
        for lot in inventory:
            remaining = float(lot["remaining_quantity"])
            if remaining <= 1.0e-12:
                continue
            open_lots.append(
                {
                    "symbol": symbol,
                    "entry_decision_id": lot["decision_id"],
                    "entry_prediction_id": lot.get("prediction_id"),
                    "entry_checkpoint_session": lot.get("checkpoint_session"),
                    "entry_executed_at": lot["executed_at"].isoformat(),
                    "entry_price": lot["price"],
                    "remaining_quantity": remaining,
                    "status": "OPEN_TRACKED_INVENTORY",
                }
            )
    values = [
        float(row["gross_realized_pnl_before_unavailable_fees"])
        for row in weekly_closures
    ]
    by_symbol: dict[str, dict[str, object]] = {}
    symbols = sorted(
        {
            str(row["symbol"])
            for row in (*weekly_closures, *weekly_unmatched, *open_lots)
        }
    )
    for symbol in symbols:
        symbol_closures = [row for row in weekly_closures if row["symbol"] == symbol]
        symbol_values = [
            float(row["gross_realized_pnl_before_unavailable_fees"])
            for row in symbol_closures
        ]
        by_symbol[symbol] = {
            "matched_fifo_segment_count": len(symbol_closures),
            "matched_quantity": sum(
                float(row["quantity"]) for row in symbol_closures
            ),
            "gross_realized_pnl_before_unavailable_fees": round(
                sum(symbol_values), 2
            ),
            "unmatched_sell_quantity": sum(
                float(row["quantity"])
                for row in weekly_unmatched
                if row["symbol"] == symbol
            ),
            "open_tracked_quantity_at_window_end": sum(
                float(row["remaining_quantity"])
                for row in open_lots
                if row["symbol"] == symbol
            ),
        }
    fill_actions = Counter(str(row["action"]) for row in fills)
    return (
        {
            "schema_version": STOCK_TRADER_EXECUTION_LIFECYCLE_VERSION,
            "status": (
                "RECEIPT_MATCHED_ROUND_TRIPS"
                if weekly_closures
                else "NO_RECEIPT_MATCHED_ROUND_TRIPS"
            ),
            "window": {
                "start": start.isoformat(),
                "end_exclusive": end.isoformat(),
            },
            "attribution": "LOCAL_FIFO_STOCK_TRADER_FILLS_NOT_BROKER_TAX_LOTS",
            "summary": {
                "submitted_decisions_with_no_observed_fill": submitted_without_fill,
                "sanitized_fill_count_before_window_end": len(fills),
                "fill_action_counts": dict(sorted(fill_actions.items())),
                "matched_fifo_segment_count": len(weekly_closures),
                "matched_round_trip_quantity": sum(
                    float(row["quantity"]) for row in weekly_closures
                ),
                "gross_realized_pnl_before_unavailable_fees": round(
                    sum(values), 2
                ),
                "wins": sum(value > 0.0 for value in values),
                "losses": sum(value < 0.0 for value in values),
                "breakeven": sum(value == 0.0 for value in values),
                "unmatched_sell_quantity": sum(
                    float(row["quantity"]) for row in weekly_unmatched
                ),
                "open_tracked_quantity_at_window_end": sum(
                    float(row["remaining_quantity"]) for row in open_lots
                ),
                "fees_included": False,
                "broker_tax_lot_method_claimed": False,
            },
            "by_symbol": by_symbol,
            "matched_fifo_segments": weekly_closures,
            "unmatched_sells": weekly_unmatched,
            "open_tracked_inventory": open_lots,
            "limitations": [
                "Only receipt-matched stock-trader fills are included.",
                "Sells of pre-existing or manually acquired shares remain unattributed.",
                "This local FIFO view does not replace Schwab statements or tax-lot accounting.",
                "Commissions, regulatory fees, dividends, splits, and cash flows are not reconstructed here.",
            ],
        },
        tuple(dict.fromkeys(sources)),
    )


def _read_execution_evidence(
    root: Path,
    decision_id: str,
) -> tuple[dict[str, object] | None, tuple[Path, ...]]:
    directory = root / "ml" / "stock-trader-execution-events" / decision_id
    if not directory.is_dir():
        return None, ()
    intent_path = directory / "intent.json"
    result_path = directory / "result.json"
    intent = _read_object(intent_path, "stock execution intent")
    if intent.get("schema_version") != STOCK_TRADER_EXECUTION_EVENT_SCHEMA_VERSION:
        raise ValueError("Stock execution intent schema is unsupported")
    if result_path.is_file():
        result = _read_object(result_path, "stock execution result")
        if result.get("schema_version") != STOCK_TRADER_EXECUTION_EVENT_SCHEMA_VERSION:
            raise ValueError("Stock execution result schema is unsupported")
    else:
        result = {"status": "SUBMISSION_STATUS_UNKNOWN"}
    sources: list[Path] = [intent_path]
    if result_path.is_file():
        sources.append(result_path)
    snapshots: list[Mapping[str, object]] = []
    for path in sorted(directory.glob("reconciliations/*/snapshot.json")):
        snapshot = _read_object(path, "stock execution reconciliation")
        if (
            snapshot.get("schema_version")
            != STOCK_TRADER_EXECUTION_EVENT_SCHEMA_VERSION
            or snapshot.get("event") != "BROKER_RECONCILIATION_SNAPSHOT"
            or snapshot.get("decision_id") != decision_id
        ):
            raise ValueError("Stock reconciliation snapshot failed identity checks")
        snapshots.append(snapshot)
        sources.append(path)
    pointer_path = directory / "reconciliation-latest.json"
    if pointer_path.is_file():
        pointer = _read_object(pointer_path, "stock reconciliation pointer")
        raw = pointer.get("path")
        if not isinstance(raw, str) or not raw:
            raise ValueError("Stock reconciliation pointer has no path")
        target = (directory / raw).resolve()
        if not target.is_relative_to(directory / "reconciliations"):
            raise ValueError("Stock reconciliation pointer escapes its event")
        if pointer.get("snapshot_sha256") != file_checksum(target):
            raise ValueError("Stock reconciliation pointer checksum differs")
        sources.append(pointer_path)
    best = max(snapshots, key=_snapshot_score) if snapshots else None
    return (
        {"intent": intent, "result": result, "reconciliation": best},
        tuple(dict.fromkeys(sources)),
    )


def _snapshot_score(snapshot: Mapping[str, object]) -> tuple[float, int, int]:
    filled = max(0.0, finite(snapshot.get("filled_quantity"), default=0.0) or 0.0)
    matched = int(snapshot.get("reconciliation_status") == "MATCHED")
    observed = pd.to_datetime(snapshot.get("observed_at"), utc=True, errors="coerce")
    observed_ns = int(pd.Timestamp(observed).value) if not pd.isna(observed) else -1
    return filled, matched, observed_ns


def _normalized_fills(
    decision: Mapping[str, object],
    reconciliation: Mapping[str, object],
    *,
    decision_id: str,
) -> list[dict[str, object]]:
    filled_quantity = max(
        0.0, finite(reconciliation.get("filled_quantity"), default=0.0) or 0.0
    )
    if filled_quantity <= 0.0:
        return []
    raw_fills = reconciliation.get("fills")
    rows = raw_fills if isinstance(raw_fills, list) else []
    normalized: list[tuple[float, float, pd.Timestamp]] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        quantity = finite(raw.get("quantity"))
        price = finite(raw.get("price"))
        executed_at = _optional_utc(raw.get("executed_at"))
        if quantity and price and quantity > 0.0 and price > 0.0:
            normalized.append(
                (
                    quantity,
                    price,
                    executed_at
                    or _fallback_execution_time(decision, reconciliation),
                )
            )
    observed_quantity = sum(row[0] for row in normalized)
    if not normalized or not math.isclose(
        observed_quantity, filled_quantity, rel_tol=0.0, abs_tol=1.0e-8
    ):
        average = finite(reconciliation.get("average_fill_price"))
        if average is None or average <= 0.0:
            return []
        normalized = [
            (
                filled_quantity,
                average,
                _fallback_execution_time(decision, reconciliation),
            )
        ]
    prediction = decision.get("prediction")
    prediction_row = prediction if isinstance(prediction, Mapping) else {}
    action = str(decision.get("action") or "").upper()
    symbol = str(decision.get("symbol") or "").upper()
    output: list[dict[str, object]] = []
    for index, (quantity, price, executed_at) in enumerate(normalized):
        output.append(
            {
                "decision_id": decision_id,
                "prediction_id": prediction_row.get("prediction_id"),
                "checkpoint_session": prediction_row.get(
                    "checkpoint_session", "REGULAR"
                ),
                "symbol": symbol,
                "action": action,
                "quantity": quantity,
                "remaining_quantity": quantity,
                "price": price,
                "executed_at": executed_at,
                "fill_index": index,
            }
        )
    return output


def _fallback_execution_time(
    decision: Mapping[str, object], reconciliation: Mapping[str, object]
) -> pd.Timestamp:
    for value in (
        reconciliation.get("close_time"),
        reconciliation.get("entered_time"),
        reconciliation.get("observed_at"),
        decision.get("decided_at"),
    ):
        parsed = _optional_utc(value)
        if parsed is not None:
            return parsed
    raise ValueError("Stock fill has no usable execution clock")


def _optional_utc(value: object) -> pd.Timestamp | None:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    return None if pd.isna(parsed) else pd.Timestamp(parsed)


def _read_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not a JSON object")
    return value


__all__ = [
    "STOCK_TRADER_EXECUTION_LIFECYCLE_VERSION",
    "build_stock_trader_execution_lifecycle",
]
