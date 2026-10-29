"""Read original candle rows without putting their payloads in a temp sort."""
from __future__ import annotations


def ordered_candle_rows(db, symbol=None):
    """Keep the original availability/rowid order in one SQLite snapshot.

    The order query carries only integer keys. Large original payloads are
    fetched by primary rowid in bounded batches after sorting, without schema
    changes, filtering, caching or dropping any original validation input.
    The caller owns the connection and its transaction lifetime.
    """
    if not db.in_transaction:
        db.execute("BEGIN")
    keys = [row[0] for row in db.execute(
        "SELECT rowid FROM candles" + (" WHERE symbol=?" if symbol else "")
        + " ORDER BY available_at,rowid", (symbol,) if symbol else ())]
    result = []
    for offset in range(0, len(keys), 128):
        batch = keys[offset:offset + 128]
        marks = ",".join("?" for _ in batch)
        found = {row[0]: (row[1], row[2]) for row in db.execute(
            "SELECT rowid,id,payload FROM candles WHERE rowid IN (" + marks + ")", batch)}
        # A missing key in this pinned snapshot is an error, never permission
        # to omit retained evidence or silently score a different population.
        result.extend(found[key] for key in batch)
    return result
