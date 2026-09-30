"""Observed real-account equity, kept separately from Paper/Powder ledgers.

Collection is an explicit UI operation. Reading history never reaches a provider,
creates a ledger, signs an order, or changes a Paper opening.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from pathlib import Path
import sqlite3
import threading
import time

from app.services.hyperliquid_paper_view import DEFAULT_DATA_ROOT

ACCOUNTS = ("alex", "jeremy", "clearpond")
OBSERVATION_SECONDS = 60
GAP_SECONDS = 180


def _number(value):
    try:
        return float(value) if not isinstance(value, bool) and math.isfinite(float(value)) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _epoch(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.timestamp() if stamp.tzinfo else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def opening_equities(seed):
    """Use the observed real balances at the mirror, not Paper's marked equity."""
    metadata = seed.get("metadata", {})
    sources = metadata.get("accounts", {}) if isinstance(metadata, dict) else {}
    if not isinstance(sources, dict):
        return {}
    values = {key: _number(sources.get(key, {}).get("source_equity"))
              for key in ACCOUNTS if isinstance(sources.get(key), dict)}
    if all(values.get(key) is not None for key in ACCOUNTS):
        values["pooled"] = sum(values[key] for key in ACCOUNTS)
    return {key: value for key, value in values.items() if value is not None}


class HyperliquidAccountHistoryService:
    def __init__(self, data_root=DEFAULT_DATA_ROOT, *, loader=None, clock=time.time,
                 history_limit=6000):
        self.path = Path(data_root) / "_account_history" / "equity.sqlite3"
        self.loader = loader
        self.clock = clock
        self.history_limit = max(16, min(int(history_limit), 20000))
        self.error = ""
        self._sync_lock = threading.Lock()
        self._last_attempt = float("-inf")
        self._last_recorded = float("-inf")

    def sync(self, loader=None, *, force=False):
        """Reuse explicit Duckets syncs; throttle automatic balance reads to 60s."""
        with self._sync_lock:
            now = self.clock()
            if not force and now - max(self._last_attempt, self._last_recorded) < OBSERVATION_SECONDS:
                return None
            self._last_attempt = now
            if loader is None:
                loader = self.loader
            if loader is None:
                from app.services.hyperliquid import sync_hyperliquid_balances
                loader = sync_hyperliquid_balances
            try:
                snapshots = loader()
            except Exception as error:
                self._try_record([])
                self.error = "Real account read unavailable (" + type(error).__name__ + ")"
                raise
            self._try_record(snapshots)
            return snapshots

    def _try_record(self, snapshots):
        try:
            complete = self.record_snapshots(snapshots)
            self.error = "" if complete else "Some real accounts are unavailable"
        except (OSError, sqlite3.Error) as error:
            # A history failure must not discard the live Duckets balances.
            self.error = "Real account history unavailable (" + type(error).__name__ + ")"

    def record_snapshots(self, snapshots):
        now = self.clock()
        stamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
        values, source_times, seen = {}, {}, set()
        for snapshot in snapshots:
            key = snapshot.account_label.casefold().replace(" ", "")
            if key not in ACCOUNTS or snapshot.source != "hyperliquid":
                continue
            if key in seen:
                values[key] = None
                continue
            seen.add(key)
            facts = snapshot.account_facts
            value = _number(snapshot.reported_total_value)
            values[key] = (round(value, 2) if value is not None and not facts.get("sync_error") else None)
            source_times[key] = (snapshot.synced_at.astimezone(timezone.utc).isoformat()
                                 if snapshot.synced_at else None)
        complete = all(values.get(key) is not None for key in ACCOUNTS)
        values["pooled"] = sum(values[key] for key in ACCOUNTS) if complete else None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=2) as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS observations (
                timestamp_utc TEXT NOT NULL, observed_epoch REAL NOT NULL,
                account TEXT NOT NULL, equity REAL, source_at_utc TEXT,
                PRIMARY KEY (timestamp_utc, account))""")
            connection.execute("CREATE INDEX IF NOT EXISTS observation_time ON observations(observed_epoch)")
            connection.executemany("INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?)", [
                (stamp, now, key, values.get(key), source_times.get(key))
                for key in (*ACCOUNTS, "pooled")])
        self._last_recorded = now
        return complete

    def load_history(self, seed):
        """Bounded local read since this Paper opening, including missing samples."""
        start = _epoch(seed.get("timestamp_utc"))
        if start is None or not self.path.is_file():
            return []
        end = self.clock()
        connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.2)
        connection.row_factory = sqlite3.Row
        deadline = time.monotonic() + 2
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            count = connection.execute("SELECT COUNT(*) FROM observations WHERE observed_epoch BETWEEN ? AND ?",
                                       (start, end)).fetchone()[0]
            if count <= self.history_limit:
                rows = connection.execute("SELECT * FROM observations WHERE observed_epoch BETWEEN ? AND ? ORDER BY observed_epoch",
                                          (start, end)).fetchall()
            else:
                bucket = max(1, (end-start) / (self.history_limit // 4 - 2))
                rows = connection.execute("""WITH ranked AS (
                    SELECT *, ROW_NUMBER() OVER (PARTITION BY account ORDER BY observed_epoch) AS first_rank,
                      ROW_NUMBER() OVER (PARTITION BY account ORDER BY observed_epoch DESC) AS last_rank,
                      ROW_NUMBER() OVER (PARTITION BY account,CAST((observed_epoch-?)/? AS INTEGER)
                        ORDER BY observed_epoch DESC) AS bucket_rank,
                      MAX(equity IS NULL) OVER (PARTITION BY account,CAST((observed_epoch-?)/? AS INTEGER)) AS had_gap
                    FROM observations WHERE observed_epoch BETWEEN ? AND ?)
                    SELECT * FROM ranked WHERE first_rank=1 OR last_rank=1 OR bucket_rank=1
                    ORDER BY observed_epoch""", (start, bucket, start, bucket, start, end)).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()
