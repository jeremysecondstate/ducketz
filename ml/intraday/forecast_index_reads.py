"""One-row reads of the unchanged execution forecast index inventory."""
import json
import sqlite3


def forecast_index_rows(db, clauses, parameters):
    """Keep canonical ordered scalars while avoiding per-row Python handoffs.

    Unusual storage classes or unavailable JSON retain the original reader and
    its errors. The caller retains every physical/schema/revision and original
    validation rule in its existing source read transaction.
    """
    query = ("SELECT f.id,f.pc,f.symbol,f.cycle_id,f.reference_at,f.issued_at,r.generation "
        "FROM forecasts f LEFT JOIN execution_forecast_revisions r ON r.forecast_id=f.id WHERE " +
        " AND ".join(clauses) + " ORDER BY f.rowid")
    # Custom SQLite factories own their canonical returned types.
    if (getattr(db, "text_factory", str) is not str
            or getattr(db, "row_factory", None) is not None):
        return db.execute(query, parameters).fetchall()
    was_in_transaction = getattr(db, "in_transaction", False)
    try:
        raw = db.execute("SELECT json_group_array(json_array(id,pc,symbol,cycle_id,reference_at,issued_at,generation)) FROM (" + query + ")", parameters).fetchone()[0]
        rows = json.loads(raw)
        if (not isinstance(rows, list) or any(not isinstance(row, list) or len(row) != 7
                or any(not isinstance(value, str) for value in row[:6])
                or row[6] is not None and type(row[6]) is not int for row in rows)):
            raise ValueError("Optional forecast index aggregate has irregular values")
        if was_in_transaction and not getattr(db, "in_transaction", True):
            raise sqlite3.OperationalError("Execution forecast index read lost pinned source snapshot")
        return [tuple(row) for row in rows]
    except (sqlite3.Error, TypeError, ValueError, UnicodeError, IndexError):
        # An interrupted pinned source snapshot cannot be replaced by a fresh
        # read while its former physical/schema/revision proof is retained.
        if was_in_transaction and not getattr(db, "in_transaction", True):
            raise
    return db.execute(query, parameters).fetchall()
