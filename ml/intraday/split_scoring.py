"""Provider-free own-session scoring from System 1A's original observations.

The scoring process can finish the original post-close collection boundary.
It has no broker, order queue, manual activation or acquisition adapter.
"""
from __future__ import annotations

from datetime import timedelta
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time

from .prediction import ContractError, PACIFIC, STEP, digest, instant


def _source(path, symbols, final_identity, final_reference, day_start, close):
    """Read a short source WAL snapshot, then validate original bytes outside it."""
    if not path.is_file():
        return [], set()
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.25)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        rows = db.execute("SELECT id,symbol,opened_at,available_at,payload FROM candles "
                          "WHERE symbol IN (SELECT value FROM json_each(?)) AND opened_at>=? AND opened_at<? "
                          "ORDER BY available_at,rowid",
                          (json.dumps(list(symbols)), day_start.isoformat(), close.isoformat())).fetchall()
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='intraday_candle_collection'").fetchone()
        empty = ({row[0] for row in db.execute("SELECT symbol FROM intraday_candle_collection "
                  "WHERE session_id=? AND reference_at=? AND status='source_empty'",
                  (final_identity, final_reference.isoformat()))} if exists else set())
    return rows, empty


def scoring_pass(*, root, store, session, config, clock, stopped=lambda: False):
    """Mirror exact complete observations and score preserved saved directions."""
    from .data import completed_15m_observation, synthetic_continuity_verified
    from .final_scoring import score_saved_session_outcomes
    from .coverage import RECOVERY_POPULATION_CONTRACT

    began = time.perf_counter()
    now, close = instant(clock()), instant(session["close_at"])
    symbols = tuple(session["symbols"])
    if stopped():
        return {"status": "STOPPED", "mirrored": 0}
    day = close.astimezone(PACIFIC).date().isoformat()
    day_start = instant(close.astimezone(PACIFIC).replace(hour=0, minute=0, second=0, microsecond=0))
    collection_id = "system-1a:" + session["actor"].lower() + ":" + day + ":collection"
    final_reference = close - STEP
    source_path = Path(root) / "intraday/system-1a/collection.sqlite3"
    try:
        rows, empty = _source(source_path, symbols, collection_id, final_reference, day_start, close)
    except sqlite3.OperationalError as exc:
        return {"status": "WAITING_FOR_DATA", "mirrored": 0, "error_type": type(exc).__name__,
                "sqlite_errorname": getattr(exc, "sqlite_errorname", None)}
    recovery = config.population_contract == RECOVERY_POPULATION_CONTRACT
    source_id = "multi:databento-schwab-fmp:15m" if recovery else "databento:" + config.source_dataset + ":ohlcv-1m"
    dataset = "MULTI_PROVIDER" if recovery else config.source_dataset
    # Revalidate source bytes each pass, but avoid a writer transaction for an
    # already mirrored exact original. Corrupt/replaced target rows cannot be
    # authorized by a remembered in-process identity.
    identities = [row[0] for row in rows]
    with store.connect() as db:
        known = {row[0]: row[1:] for row in db.execute(
            "SELECT id,symbol,opened_at,available_at,payload FROM candles "
            "WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(identities),))}
    final_ready, mirrored, newly_mirrored, rejected, rejected_symbols = set(empty) & set(symbols), 0, 0, [], set()
    for identity, symbol, opened_at, available_at, raw in rows:
        if stopped():
            return {"status": "STOPPED", "mirrored": mirrored}
        try:
            candle = json.loads(raw)
            if (digest(candle) != identity or (candle["symbol"], candle["opened_at"], candle["available_at"])
                    != (symbol, opened_at, available_at)):
                raise ContractError("System 1A original candle identity changed")
            if (candle.get("availability_kind") != "actual" or instant(candle["available_at"]) > now
                    or not completed_15m_observation(candle) or not synthetic_continuity_verified(candle)):
                continue
            if (instant(candle["opened_at"]).astimezone(PACIFIC).date().isoformat() != day
                    or candle.get("population_contract") != config.population_contract
                    or candle.get("source_id") != source_id or candle.get("source_dataset") != dataset):
                continue
            previous = known.get(identity)
            if previous is not None and previous != (symbol, opened_at, available_at, raw):
                raise ContractError("Mirrored original candle evidence changed")
            if previous is None:
                store.record_candle(candle)
                newly_mirrored += 1
            mirrored += 1
            if instant(candle["opened_at"]) == final_reference:
                final_ready.add(symbol)
        except (ContractError, ValueError, TypeError, KeyError) as exc:
            rejected_symbols.add(symbol)
            if len(rejected) < 32:
                rejected.append({"symbol": symbol, "candle_id": identity, "error_type": type(exc).__name__})
    if stopped():
        return {"status": "STOPPED", "mirrored": mirrored}
    scored = {"session_id": session["session_id"], "scored": 0, "synthetic": 0, "unavailable": 0}
    for symbol in symbols:
        if stopped():
            return {"status": "STOPPED", "mirrored": mirrored}
        try:
            value = score_saved_session_outcomes(store, session_id=session["session_id"], actor=session["actor"],
                symbols=(symbol,), as_of=now, stopped=stopped)
            for key, count in value.items():
                if key != "session_id":
                    scored[key] = scored.get(key, 0) + count
        except (ContractError, ValueError, TypeError, KeyError) as exc:
            # Original forecast, candle and outcome readers may reject malformed
            # JSON before reaching their semantic ContractError checks. Preserve
            # that evidence and keep valid unrelated symbols progressing.
            rejected_symbols.add(symbol)
            if len(rejected) < 32:
                rejected.append({"symbol": symbol, "error_type": type(exc).__name__,
                                 "operation": "original_saved_outcome_validation"})
    final_ready -= rejected_symbols
    result = {"status": "COMPLETED" if now >= close and set(symbols) <= final_ready else "WAITING_FOR_DATA",
        "mirrored": mirrored, "newly_mirrored": newly_mirrored, "outcomes": scored, "final_symbols_ready": sorted(final_ready),
        "rejected_candles": rejected, "scoring_seconds": time.perf_counter() - began,
        "provider_calls": 0, "trading_authority": False}
    if stopped():
        result["status"] = "STOPPED"
    return result


def renewal_scoring_pass(*, root, store, renewal, config, clock):
    try:
        result = scoring_pass(root=root, store=store, session=renewal.session, config=config,
                              clock=clock, stopped=renewal.stopped)
        if result["status"] in {"STOPPED", "COMPLETED"}:
            if result["status"] == "COMPLETED":
                store.event(renewal.session["session_id"], "final_saved_outcomes_scored", result)
            return {"status": result["status"], "role": "scoring", **result}
    except sqlite3.OperationalError as exc:
        code = getattr(exc, "sqlite_errorcode", None)
        if type(code) is not int or code & 255 not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise
        from .scoring_storage import scoring_failure_evidence
        # A target mirror, outcome write or completion receipt can encounter
        # ordinary concurrent storage work too. Checkpoint one bounded retry;
        # exact immutable reads and idempotent writes revalidate on the next
        # turn, including any work committed before contention occurred.
        result = {"status": "WAITING_FOR_STORAGE", "partial_progress_retained": True,
                  "diagnostics": scoring_failure_evidence(exc), "retry_after_seconds": 3,
                  "provider_calls": 0, "trading_authority": False}
    if renewal.stopped():
        return {"status": "STOPPED", "role": "scoring", "last_scoring": result}
    renewal.checkpoint({"last_scoring": result}, (instant(clock()) + timedelta(seconds=3)).isoformat())
    raise RuntimeError("Renewal checkpoint must end the scoring worker")
