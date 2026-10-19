"""Native history reads stay bounded while preserving exact order evidence."""
import json
import sqlite3

import pytest

from ml.stock_trader.horizon_ledger import HorizonLedger, LedgerError


AT = "2026-10-19T16:00:00+00:00"
LATER = "2026-10-19T16:00:10+00:00"
ACCOUNT = "a" * 64


def populated(tmp_path, count, *, attribution=False):
    ledger = HorizonLedger(tmp_path / "native.sqlite3", ACCOUNT)
    with ledger._transaction() as db:
        if attribution:
            ledger._intraday_schema(db)
            db.execute("CREATE TABLE inventory_assignments(id TEXT PRIMARY KEY,allocation TEXT,quantity INTEGER)")
            db.execute("CREATE TABLE inventory_assignment_releases(id TEXT PRIMARY KEY,assignment TEXT,quantity INTEGER)")
        for index in range(count):
            allocation = f"a{index:04}"
            db.execute("INSERT INTO allocations VALUES(?,?,?,?,?,?,?,?)",
                (allocation, ACCOUNT, f"S{index}", "1h", f"allocation-{index}", AT, LATER, "ACTIVE"))
            # A fully filled buy, partially filled sale, and rejected attempt.
            for suffix, side, quantity, filled, status in (
                ("buy", "BUY", 10, 10, "FILLED"),
                ("sell", "SELL", 4, 2, "PARTIAL"),
                ("rejected", "BUY", 1, 0, "REJECTED")):
                identity = f"{allocation}-{suffix}"
                request = {"forecast": identity, "start": AT, "end": LATER,
                    "trigger_horizon": "1h", "trigger_forecast": f"trigger-{index}"}
                db.execute("INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (identity, allocation, side, quantity, "100", filled, status,
                     None if suffix == "rejected" else f"broker-{identity}", identity, "batch",
                     json.dumps(request), LATER))
                if suffix != "rejected":
                    # Legacy receipts intentionally lack cached submission time.
                    for ordinal, stamp in enumerate((AT, LATER)):
                        db.execute("INSERT INTO evidence VALUES(?,?,?)", (f"{identity}-{ordinal}", "submission",
                            json.dumps({"reservation_id": identity, "status": "SUBMITTED", "observed_at": stamp})))
                if attribution and suffix == "sell":
                    db.execute("INSERT INTO intraday_sell_slices VALUES(?,?,?,?)", (identity, allocation, 4, 0))
                    db.execute("INSERT INTO fills VALUES(?,?,?,?,?)", (f"fill-{identity}", identity, 2, "100", LATER))
                    db.execute("INSERT INTO intraday_fill_attribution VALUES(?,?,?,?)", (f"fill-{identity}", allocation, 0, 2))
            if attribution:
                db.execute("INSERT INTO inventory_assignments VALUES(?,?,?)", (allocation, allocation, 5))
                db.execute("INSERT INTO inventory_assignment_releases VALUES(?,?,?)", (allocation, allocation, 2))
    return ledger


@pytest.mark.parametrize("count", [1, 80])
@pytest.mark.parametrize("attribution", [False, True])
def test_snapshot_batches_sql_and_preserves_legacy_identity(tmp_path, count, attribution):
    ledger = populated(tmp_path, count, attribution=attribution)
    queries = []
    with ledger._transaction() as db:
        db.set_trace_callback(queries.append)
        snapshot = ledger._snapshot(db)
        db.set_trace_callback(None)
    # Deterministic SQL-call bound reproduces the per-query contention hazard
    # without wall-clock assertions, real models, network calls or live state.
    assert len([sql for sql in queries if sql.lstrip().upper().startswith("SELECT")]) <= 11
    assert len(snapshot.reservations) == count * 3
    assert snapshot.reservations == tuple(ledger.lookup_reservation(row.reservation_id)
                                         for row in snapshot.reservations)
    for order in snapshot.reservations:
        assert order.submitted_at == (None if order.status == "REJECTED" else AT)
        assert order.target_start == AT and order.target_end == LATER
        assert order.trigger_forecast_id.startswith("trigger-")
    for allocation in snapshot.allocations:
        assert allocation.filled_shares == (11 if attribution else 8)
        assert allocation.reserved_buy_shares == 0
        assert allocation.reserved_sell_shares == 2
    assert ledger.snapshot() == snapshot


def test_batch_snapshot_preserves_attribution_corruption_detection(tmp_path):
    ledger = populated(tmp_path, 2, attribution=True)
    with ledger._transaction() as db:
        db.execute("UPDATE intraday_fill_attribution SET quantity=3 WHERE allocation='a0000'")
    with pytest.raises(LedgerError, match="INTRADAY_ATTRIBUTION_DOES_NOT_BALANCE"):
        ledger.snapshot()


def test_batch_snapshot_rejects_orphaned_reservation(tmp_path):
    ledger = populated(tmp_path, 2)
    with sqlite3.connect(ledger.path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("DELETE FROM allocations WHERE id='a0000'")
    with pytest.raises(LedgerError, match="UNKNOWN_RESERVATION"):
        ledger.snapshot()


def test_single_handoff_retains_sqlite_scalar_values_and_order(tmp_path):
    ledger = HorizonLedger(tmp_path / "values.sqlite3", ACCOUNT)
    payload = '{"literal":"null","quote":"\\\"","unicode":"\u2603"}'
    with ledger._transaction() as db:
        db.execute("CREATE TABLE scalar_fixture(ordinal INTEGER,quantity INTEGER,price TEXT,payload TEXT,optional TEXT)")
        db.executemany("INSERT INTO scalar_fixture VALUES(?,?,?,?,?)", [
            (2, 9007199254740993, "000.001000", payload, None),
            (1, -9007199254740993, "-0.000000", "[]", "null")])
        expected = [list(row) for row in db.execute("SELECT * FROM scalar_fixture ORDER BY ordinal")]
        assert ledger._read_rows(db, "SELECT * FROM scalar_fixture ORDER BY ordinal",
            ("ordinal", "quantity", "price", "payload", "optional")) == expected
