"""Isolate damaged queued scientific records without authorizing their execution.

Quarantine retains the original record and its supersession position. It never
changes an intention, order, reservation or UNKNOWN-send fence. Recovery is a
separate lane operation and can still inspect its own original evidence.
"""
import sqlite3

from .prediction import ContractError, encoded, instant


def install_indexes(db):
    db.execute("CREATE INDEX IF NOT EXISTS intraday_queue_due "
               "ON intraday_live_queue(session_id,status,next_attempt_at)")
    db.execute("CREATE INDEX IF NOT EXISTS intraday_queue_symbol "
               "ON intraday_live_queue(session_id,symbol,issued_at)")


def _storage_retry(exc, retries, operation):
    code = getattr(exc, "sqlite_errorcode", None)
    if type(code) is not int or code & 255 not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return False
    if len(retries) < 4:
        retries.append({"operation": operation, "error_type": type(exc).__name__,
                        "sqlite_errorcode": code, "sqlite_errorname": getattr(exc, "sqlite_errorname", None),
                        "retry_after_seconds": 3, "pending_originals_retained": True,
                        "broker_wait_changed": False})
    return True


def broker_due_for_pass(queue, now, *, retries):
    """Unreadable broker retry state cannot authorize a broker operation."""
    try:
        return queue.broker_due(now)
    except sqlite3.OperationalError as exc:
        if not _storage_retry(exc, retries, "broker_retry_read"):
            raise
        return False


def due_for_pass(queue, now, *, stopped, retries):
    """One bounded read attempt; contention leaves independent recovery due.

    Checkpoint evidence is persisted by the caller outside the busy database.
    Do not turn a storage read retry into a global broker backoff or a changed
    scientific disposition. A future pass reads exact originals again.
    """
    try:
        yield from (queue.iter_due(now, stopped=stopped)
                    if hasattr(queue, "iter_due") else queue.due(now))
    except sqlite3.OperationalError as exc:
        if not _storage_retry(exc, retries, "execution_queue_read"):
            raise


def due_forecasts(queue, now, *, stopped=lambda: False):
    """Validate one due symbol at a time; one bad original cannot abort the sweep."""
    current = instant(now).isoformat()
    if stopped() or not queue.broker_due(now):
        return
    with queue.store.connect() as db:
        # Include missing originals so they produce durable failures, not silent
        # omissions. An unreadable reference cannot promote older same-symbol
        # work. A quarantined row continues to supersede earlier opportunities.
        rows = db.execute("""SELECT q.forecast_id,q.symbol,q.issued_at
            FROM intraday_live_queue q LEFT JOIN forecasts f ON f.id=q.forecast_id
            WHERE q.session_id=? AND q.status='pending' AND q.next_attempt_at<=?
            AND (f.id IS NULL OR NOT EXISTS(
                SELECT 1 FROM intraday_live_queue n LEFT JOIN forecasts nf ON nf.id=n.forecast_id
                WHERE n.session_id=q.session_id AND n.symbol=q.symbol AND n.forecast_id!=q.forecast_id
                AND (nf.id IS NULL OR nf.reference_at>f.reference_at
                     OR (nf.reference_at=f.reference_at AND n.issued_at>q.issued_at))))
            ORDER BY f.reference_at,q.symbol,q.forecast_id""", (queue.session_id, current)).fetchall()
    for identity, symbol, issued_at in rows:
        if stopped():
            return
        try:
            values = queue.store.forecasts(pc=queue.actor, forecast_ids=[identity])
            if len(values) != 1:
                raise ContractError("Queued original forecast is missing or has changed actor")
            value = values[0]
            if (value["forecast_id"] != identity or value["symbol"] != symbol
                    or instant(value["issued_at"]).isoformat() != issued_at):
                raise ContractError("Queued original forecast metadata changed")
        except (ValueError, TypeError, KeyError) as exc:
            # The failure and queue disposition commit together. Storage errors
            # propagate rather than pretending the quarantine was durable.
            with queue.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                changed = db.execute("UPDATE intraday_live_queue SET status='invalid_original' "
                    "WHERE forecast_id=? AND session_id=? AND status='pending' AND symbol=? AND issued_at=?",
                    (identity, queue.session_id, symbol, issued_at)).rowcount
                if changed:
                    db.execute("INSERT INTO events(cycle_id,symbol,recorded_at,kind,payload) VALUES(?,?,?,?,?)",
                        (queue.session_id, symbol, current, "execution_queue_original_rejected", encoded({
                            "forecast_id": identity, "error_type": type(exc).__name__,
                            "original_evidence_retained": True, "same_symbol_supersession_retained": True,
                            "order_state_changed": False})))
            continue
        if stopped():
            return
        yield value
