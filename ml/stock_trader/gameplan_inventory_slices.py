"""Append-only owner attribution for autonomous Gameplan stock sales.

These SQLite helpers operate inside the caller's existing ledger transaction.
They neither transfer positions nor manufacture fills or order authority.
"""
from __future__ import annotations


def _fail(message):
    from ml.stock_trader.horizon_ledger import LedgerError
    raise LedgerError(message)


def _exists(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='gameplan_sell_slices'").fetchone()


def ensure_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS gameplan_sell_slices (
        reservation TEXT NOT NULL REFERENCES reservations(id),
        allocation TEXT NOT NULL REFERENCES allocations(id),
        quantity INTEGER NOT NULL CHECK(quantity>0), ordinal INTEGER NOT NULL,
        PRIMARY KEY(reservation,ordinal))""")
    db.execute("""CREATE TABLE IF NOT EXISTS gameplan_fill_attribution (
        fill TEXT NOT NULL REFERENCES fills(id), allocation TEXT NOT NULL REFERENCES allocations(id),
        slice_ordinal INTEGER NOT NULL, quantity INTEGER NOT NULL CHECK(quantity>0),
        PRIMARY KEY(fill,slice_ordinal))""")


def share_accounting(db, reservations):
    """Return sliced order IDs, actual owner debits and pending owner shares."""
    if not _exists(db):
        return set(), {}, {}
    by_id = {order.reservation_id: order for order in reservations}
    filled = {(row[0], row[1]): row[2] for row in db.execute("""SELECT f.reservation,
        a.slice_ordinal,SUM(a.quantity) FROM gameplan_fill_attribution a
        JOIN fills f ON f.id=a.fill GROUP BY f.reservation,a.slice_ordinal""")}
    sliced, debits, pending = set(), {}, {}
    for reservation, allocation, quantity, ordinal in db.execute(
            "SELECT reservation,allocation,quantity,ordinal FROM gameplan_sell_slices"):
        order = by_id[reservation]
        charged = filled.get((reservation, ordinal), 0)
        if order.side != "SELL" or not 0 <= charged <= quantity:
            _fail("GAMEPLAN_OWNER_ATTRIBUTION_DOES_NOT_BALANCE")
        sliced.add(reservation)
        debits[allocation] = debits.get(allocation, 0) + charged
        if order.reserved_quantity:
            pending[allocation] = pending.get(allocation, 0) + quantity - charged
    for identity in sliced:
        if sum(quantity for (reservation, _), quantity in filled.items() if reservation == identity) != by_id[identity].filled_quantity:
            _fail("GAMEPLAN_OWNER_ATTRIBUTION_DOES_NOT_BALANCE")
    return sliced, debits, pending


def attribute_fills(db, reservation_id):
    """Attribute each actual fill once; existing fill attribution never moves."""
    if not _exists(db):
        return
    slices = db.execute("SELECT allocation,quantity,ordinal FROM gameplan_sell_slices WHERE reservation=? ORDER BY ordinal",
                        (reservation_id,)).fetchall()
    if not slices:
        return
    used = {row[0]: row[1] for row in db.execute("""SELECT a.slice_ordinal,SUM(a.quantity)
        FROM gameplan_fill_attribution a JOIN fills f ON f.id=a.fill
        WHERE f.reservation=? GROUP BY a.slice_ordinal""", (reservation_id,))}
    for fill_id, quantity in db.execute("SELECT id,quantity FROM fills WHERE reservation=? ORDER BY executed_at,id",
                                        (reservation_id,)).fetchall():
        old = db.execute("SELECT COALESCE(SUM(quantity),0) FROM gameplan_fill_attribution WHERE fill=?", (fill_id,)).fetchone()[0]
        if old:
            if old != quantity:
                _fail("GAMEPLAN_OWNER_ATTRIBUTION_DOES_NOT_BALANCE")
            continue
        remaining = quantity
        for allocation, capacity, ordinal in slices:
            taken = min(remaining, capacity - used.get(ordinal, 0))
            if taken:
                db.execute("INSERT INTO gameplan_fill_attribution VALUES (?,?,?,?)", (fill_id, allocation, ordinal, taken))
                used[ordinal] = used.get(ordinal, 0) + taken
                remaining -= taken
            if not remaining:
                break
        if remaining:
            _fail("GAMEPLAN_FILL_EXCEEDS_OWNER_SLICES")


def attribution(db, reservation_id):
    if not _exists(db):
        return {"slices": [], "fills": []}
    return {"slices": [dict(row) for row in db.execute(
        "SELECT allocation,quantity,ordinal FROM gameplan_sell_slices WHERE reservation=? ORDER BY ordinal", (reservation_id,))],
        "fills": [dict(row) for row in db.execute("""SELECT a.fill,a.allocation,a.slice_ordinal,a.quantity
            FROM gameplan_fill_attribution a JOIN fills f ON f.id=a.fill
            WHERE f.reservation=? ORDER BY a.fill,a.slice_ordinal""", (reservation_id,))]}
