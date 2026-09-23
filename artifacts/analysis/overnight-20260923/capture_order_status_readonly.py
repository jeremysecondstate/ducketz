"""Capture exact TWST SELL38 order with GET-only broker data reads; never mutate ledger.

Run only after root review. Shares supervision/session guards with the separately
reviewable native reconciliation helper, but does not invoke its main or ledger
constructor. Working, missing, partial or ambiguous evidence remains unresolved.
"""
import json
import sqlite3
import sys
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal

sys.dont_write_bytecode = True
sys.path.insert(0, "C:/dev/ducketz")
from native_reconcile import (
    ACCOUNT, DB, EXPECTED, OUT, TOKEN, STATUS, FINAL_RUN, ReadOnlyBroker,
    check_owner, check_finished_session, guarded_artifacts, require, utc,
)
from ml.stock_trader.horizon_ledger import HorizonLedger
from ml.stock_trader.horizon_broker import normalize_order_evidence, _walk_orders, ORDER_HISTORY_LIMIT


def main():
    output = OUT / "broker-order-status-readonly.json"
    require(not output.exists(), "CAPTURE_ARTIFACT_ALREADY_EXISTS")
    result = {
        "observed_at": utc().isoformat(), "supervision_owner": TOKEN,
        "scope": "READ_ONLY_EXACT_TRACKED_ORDER_HISTORY", "ledger_mutations": 0,
        "orders_submitted": 0, "orders_cancelled": 0, "orders_replaced": 0,
        "broker_data_http_methods": ["GET"], "orders": [],
    }
    try:
        check_owner()
        check_finished_session()
        guarded = guarded_artifacts()
        with sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            require(dict(db.execute("SELECT key,value FROM metadata")).get("account") == ACCOUNT,
                    "SAVED_LEDGER_ACCOUNT_CHANGED")
            ids = {r[0] for r in db.execute("SELECT id FROM reservations WHERE status IN ('RESERVED','SUBMITTED','UNKNOWN','WORKING','PARTIAL')")}
            require(ids == set(EXPECTED), "PENDING_RESERVATION_SET_CHANGED")
            reservations = [HorizonLedger._reservation(db, rid) for rid in ids]
            for reservation in reservations:
                require((reservation.symbol, reservation.horizon, reservation.side,
                         reservation.quantity, reservation.broker_order_id, Decimal(str(reservation.limit_price)))
                        == EXPECTED[reservation.reservation_id], "PENDING_RESERVATION_IDENTITY_CHANGED")
                require(reservation.status == "WORKING" and reservation.filled_quantity == 0
                        and reservation.cancel_requested_at is None, "PENDING_RESERVATION_STATE_CHANGED")
            before = {rid: HorizonLedger._reservation(db, rid) for rid in ids}
        broker = ReadOnlyBroker()
        identity = broker.prepare_read_snapshot()
        require(broker.stable_account_fingerprint() == ACCOUNT, "BROKER_ACCOUNT_MISMATCH")
        now = utc()
        raw = broker.get_orders(from_entered_time=now - timedelta(days=14),
                                to_entered_time=now, max_results=ORDER_HISTORY_LIMIT)
        broker.verify_read_snapshot(identity)
        require(broker.stable_account_fingerprint() == ACCOUNT, "BROKER_ACCOUNT_CHANGED")
        require(isinstance(raw, list) and len(raw) < ORDER_HISTORY_LIMIT, "INCOMPLETE_ORDER_HISTORY")
        rows = list(_walk_orders(raw))
        observed = utc().isoformat()
        result.update(account_matches=True, observed_at=observed, history_rows=len(raw))
        for reservation in reservations:
            matches = [row for row in rows if str(row.get("orderId", "")) == reservation.broker_order_id]
            require(len(matches) == 1, "TRACKED_ID_NOT_UNIQUE_IN_HISTORY")
            row = matches[0]
            fields = ("status", "quantity", "filledQuantity", "remainingQuantity", "cancelable",
                      "enteredTime", "closeTime", "orderType", "price", "duration", "session")
            safe = {key: row.get(key) for key in fields}
            safe.update(reservation_id=reservation.reservation_id, broker_order_id=reservation.broker_order_id,
                        symbol=reservation.symbol, horizon=reservation.horizon, side=reservation.side,
                        ledger_status=reservation.status, reserved_quantity=reservation.quantity,
                        ledger_filled_quantity=reservation.filled_quantity, exact_broker_order_id_matched=True)
            evidence = normalize_order_evidence(row, reservation, account_fingerprint=ACCOUNT, observed_at=observed)
            safe.update(native_normalization="VERIFIED", native_status=evidence.status,
                        native_filled=evidence.cumulative_filled_quantity, native_remaining=evidence.remaining_quantity,
                        native_evidence=asdict(evidence))
            result["orders"].append(safe)
        check_owner()
        check_finished_session()
        require(guarded_artifacts() == guarded, "GUARDED_ARTIFACT_CHANGED")
        with sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            require({rid: HorizonLedger._reservation(db, rid) for rid in before} == before,
                    "TARGET_RESERVATION_CHANGED_DURING_READ")
        result["status"] = "READ_COMPLETE"
    except Exception as exc:
        result.update(status="READ_FAILED", error_type=type(exc).__name__,
                      error_code=str(exc) if str(exc).replace("_", "").isalnum() and str(exc).isupper()
                      else "INSPECT_CAPTURE_FAILURE")
    with output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, default=str)
        handle.write("\n")
    print(json.dumps(result, default=str))
    return 0 if result["status"] == "READ_COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
