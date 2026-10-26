"""Optional one-row reads of exact protected SQLite schema definitions."""
import json
import sqlite3


def schema_definitions(db, expected):
    """Reduce SQLite/Python handoffs while preserving the canonical validator.

    Only an aggregate exactly equal to the known definition map is reused.
    Missing, changed, irregular or unsupported results take the original row
    reader, including its original errors. No source values or guards change.
    """
    names = tuple(expected)
    where = " WHERE name IN (" + ",".join("?" for _ in names) + ")"
    was_in_transaction = getattr(db, "in_transaction", False)
    try:
        raw = db.execute("SELECT json_group_object(name,sql) FROM sqlite_master" + where,
                         names).fetchone()[0]
        rows = json.loads(raw)
        if not isinstance(rows, dict) or rows != expected:
            raise ValueError("Optional schema aggregate does not match known definitions")
        return rows
    except (sqlite3.Error, TypeError, ValueError, UnicodeError, IndexError):
        # A failed optional read cannot replace an existing pinned transaction
        # with a new unfenced observation. Calls outside a transaction retain
        # their historical canonical read behavior.
        if was_in_transaction and not getattr(db, "in_transaction", True):
            raise
    return dict(db.execute("SELECT name,sql FROM sqlite_master" + where, names))
