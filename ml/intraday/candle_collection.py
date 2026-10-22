"""Own-session completed candles, including delayed publication of the final bar."""
from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import time
import traceback

import pandas as pd

from .prediction import ContractError, PACIFIC, STEP, encoded, instant
from .runtime import LiveHistories, SourceIntervalEmpty
from .store import utcnow


def _storage_failure_evidence(exc):
    """Keep actual locations and native SQLite codes without private values."""
    chain, seen, current = [], set(), exc
    while current is not None and id(current) not in seen and len(chain) < 4:
        seen.add(id(current))
        item = {"error_type": type(current).__name__[:80],
                "frames": [{"file": Path(frame.filename).name[-120:],
                            "function": frame.name[:120], "line": frame.lineno}
                           for frame in traceback.extract_tb(current.__traceback__, limit=-8)]}
        code, name = getattr(current, "sqlite_errorcode", None), getattr(current, "sqlite_errorname", None)
        if (isinstance(current, sqlite3.Error) and type(code) is int and type(name) is str
                and name.startswith("SQLITE_") and getattr(sqlite3, name, None) == code):
            item.update(sqlite_errorcode=code, sqlite_errorname=name)
        chain.append(item)
        current = (current.__cause__ if current.__cause__ is not None else
                   None if current.__suppress_context__ else current.__context__)
    return {"exception_chain": chain, "root_cause": chain[-1],
            "exception_chain_truncated": current is not None}


def _retry_collection_storage(action, *, store, session_id, operation, stopped, pause):
    """Retry only native transient contention, retaining the exact operation."""
    retries, failure = 0, None
    while not stopped():
        try:
            result = action()
        except sqlite3.OperationalError as exc:
            code = getattr(exc, "sqlite_errorcode", None)
            if type(code) is not int or code & 255 not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise
            retries += 1
            failure = _storage_failure_evidence(exc)
            # A contended database cannot be required to retain its own error.
            print(encoded({"event": "collection_storage_contention", "operation": operation,
                           "retry_count": retries, "diagnostics": failure}), flush=True)
            pause(1)
            continue
        if retries:
            evidence = {"operation": operation, "retry_count": retries,
                        "last_failure": failure, "operation_returned_normally": True}
            try:
                store.event(session_id, "collection_storage_retry_completed", evidence)
            except Exception as exc:
                print(encoded({"event": "collection_storage_retry_diagnostic_unavailable",
                               "operation": operation, "diagnostics": _storage_failure_evidence(exc)}),
                      flush=True)
        return result
    raise InterruptedError("Manual session stopped; collection continuation retained")


def market_close_at(started_at):
    """Freeze this manually started session's 17:00 Pacific cutoff across DST."""
    local = instant(started_at).astimezone(PACIFIC)
    return instant(local.replace(hour=17, minute=0, second=0, microsecond=0))


class CandleCollector:
    """Durable exact intervals; collection has no model or execution interface."""
    def __init__(self, store, *, histories, provider, journal, symbols, session_id,
                 started_at, close_at=None, publication_offset_seconds=30,
                 clock=utcnow, stopped=lambda: False,population_contract=None, pause=time.sleep):
        self.store, self.histories, self.provider, self.journal = store, histories, provider, journal
        self.symbols, self.session_id = tuple(symbols), session_id
        if not self.symbols or len(set(self.symbols)) != len(self.symbols):
            raise ContractError("Candle collection requires this session's unique symbols")
        self.close_at = instant(close_at) if close_at is not None else market_close_at(started_at)
        self.final_reference = self.close_at - STEP
        from .data import equity_sessions
        self.sessions = equity_sessions(self.close_at - timedelta(days=1), self.close_at)
        self.final_interval_eligible = self._eligible(self.final_reference)
        self.offset = float(publication_offset_seconds)
        if not 0 <= self.offset < 900:
            raise ContractError("Publication offset must lie within one quarter-hour")
        self.clock, self.stopped, self.pause = clock, stopped, pause
        self.population_contract = population_contract
        from .coverage import RECOVERY_POPULATION_CONTRACT
        recovery = population_contract == RECOVERY_POPULATION_CONTRACT
        self.source_id = "multi:databento-schwab-fmp:15m" if recovery else f"databento:{provider.dataset}:ohlcv-1m"
        self.candle_dataset = "MULTI_PROVIDER" if recovery else provider.dataset
        self.completed = False
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS intraday_candle_collection(
                session_id TEXT NOT NULL,symbol TEXT NOT NULL,reference_at TEXT NOT NULL,
                status TEXT NOT NULL,next_attempt_at TEXT NOT NULL,last_error TEXT,
                PRIMARY KEY(session_id,symbol,reference_at))""")
            db.execute("""CREATE TABLE IF NOT EXISTS intraday_candle_fairness(
                session_id TEXT PRIMARY KEY,fresh_streak INTEGER NOT NULL,last_fresh_symbol TEXT)""")
            db.execute("INSERT OR IGNORE INTO intraday_candle_fairness VALUES(?,0,NULL)", (self.session_id,))

    def _storage(self, operation, action):
        return _retry_collection_storage(action, store=self.store, session_id=self.session_id,
            operation=operation, stopped=self.stopped, pause=self.pause)

    def _update(self, operation, sql, arguments):
        def write():
            with self.store.connect() as db:
                db.execute(sql, arguments)
        return self._storage(operation, write)

    def _event(self, kind, payload, *, symbol=None):
        return self._storage("event:" + kind,
            lambda: self.store.event(self.session_id, kind, payload, symbol=symbol))

    def _eligible(self, reference):
        reference = instant(reference)
        return (reference.astimezone(PACIFIC).date() == self.close_at.astimezone(PACIFIC).date()
                and reference + STEP <= self.close_at
                and any(session.contains(reference) for session in self.sessions))

    def _schedule(self, now):
        closing = now >= self.close_at
        adjusted = now - timedelta(seconds=self.offset)
        newest = (self.final_reference if closing else
                  adjusted.replace(minute=adjusted.minute // 15 * 15, second=0, microsecond=0) - STEP)
        newest = min(newest, self.final_reference)
        with self.store.connect() as db:
            last = db.execute("SELECT max(reference_at) FROM intraday_candle_collection WHERE session_id=?",
                              (self.session_id,)).fetchone()[0]
            first = instant(last) + STEP if last is not None else newest
            references = [self.final_reference] if closing else []
            while first <= newest:
                if self._eligible(first):
                    references.append(first)
                first += STEP
            for reference in set(references):
                if self._eligible(reference):
                    db.executemany("INSERT OR IGNORE INTO intraday_candle_collection VALUES(?,?,?,'pending',?,NULL)",
                        [(self.session_id, symbol, reference.isoformat(), now.isoformat()) for symbol in self.symbols])

    def _saved(self, symbol, reference, now):
        from .data import completed_15m_observation, synthetic_continuity_verified
        return next((candle for candle in reversed(self.store.observed_candles(symbol))
                     if candle.get("source_id") == self.source_id
                     and candle.get("source_dataset") == self.candle_dataset
                     and candle.get("population_contract") == self.population_contract
                     and instant(candle["opened_at"]) == reference
                     and instant(candle["available_at"]) <= now
                     and completed_15m_observation(candle)
                     and synthetic_continuity_verified(candle)), None)

    def _empty_history(self, symbol):
        from .data import History
        columns = ("symbol", "opened_at", "available_at", "availability_kind", "open", "high", "low",
                   "close", "volume", "source_id", "source_dataset", "session_id", "synthetic")
        frame = pd.DataFrame(columns=columns)
        for column in ("opened_at", "available_at"):
            frame[column] = pd.Series(dtype="datetime64[ns, UTC]")
        frame["synthetic"] = pd.Series(dtype=bool)
        return History(frame, {"symbol": symbol, "source_dataset": self.provider.dataset,
                               "population_contract":self.population_contract,
                               "history_policy": "missing_original_history_retained_no_invented_rows"})

    @staticmethod
    def _next_pending(pending, newest, now, fairness):
        # Deferred rows remain inspectable for already-saved candles, but never
        # displace due work or consume a fairness turn merely by being skipped.
        due = [row for row in pending if instant(row[2]) <= now]
        available = due or pending
        fresh = [row for row in available if row[1] == newest]
        older = [row for row in available if row[1] != newest]
        streak, last_symbol = fairness
        if older and (streak >= 2 or not fresh):
            return min(older, key=lambda row: (instant(row[2]), row[1], row[0])), "older"
        # Rotation survives quarter changes, including a request that consumes
        # the entire quarter. Due timestamps keep same-quarter retries fair.
        return min(fresh, key=lambda row: (instant(row[2]),
            bool(last_symbol and row[0] <= last_symbol), row[0])), "fresh"

    def _record_turn(self, lane, symbol, fairness):
        streak, last_symbol = fairness
        value = (min(2, streak + 1), symbol) if lane == "fresh" else (0, last_symbol)
        # Persist before the bounded step: interruption retains the pending row
        # but cannot reset every restart to the first fresh symbol.
        with self.store.connect() as db:
            db.execute("UPDATE intraday_candle_fairness SET fresh_streak=?,last_fresh_symbol=? WHERE session_id=?",
                       (*value, self.session_id))
        return value

    def pass_once(self):
        """Yield old work when a new quarter is due; retain every pending slot."""
        if self.stopped():
            return {"closing": instant(self.clock()) >= self.close_at, "final_complete": False,
                    "pending_final_symbols": list(self.symbols), "saved": 0}
        now = instant(self.clock())
        adjusted = now - timedelta(seconds=self.offset)
        refresh_at = (min(self.close_at,
            adjusted.replace(minute=adjusted.minute // 15 * 15, second=0, microsecond=0)
            + STEP + timedelta(seconds=self.offset)) if now < self.close_at else None)
        self._schedule(now)
        with self.store.connect() as db:
            pending = db.execute("SELECT symbol,reference_at,next_attempt_at,last_error FROM intraday_candle_collection "
                                 "WHERE session_id=? AND status='pending' ORDER BY reference_at DESC,symbol",
                                 (self.session_id,)).fetchall()
            fairness = db.execute("SELECT fresh_streak,last_fresh_symbol FROM intraday_candle_fairness WHERE session_id=?",
                                  (self.session_id,)).fetchone()
        newest = max((row[1] for row in pending), default=None)
        saved_count = 0
        while pending:
            if self.stopped():
                break
            now = instant(self.clock())
            # The pending snapshot predates the next quarter. Finish the current
            # bounded step, then let the next pass schedule new slots first.
            # Yielding changes no stop flag, receipt, or durable pending work.
            if refresh_at is not None and now >= refresh_at:
                break
            row, lane = self._next_pending(pending, newest, now, fairness)
            pending.remove(row)
            symbol, raw_reference, retry_at, old_error = row
            reference = instant(raw_reference)
            if now >= self.close_at and reference != self.final_reference:
                continue
            candle = self._saved(symbol, reference, now)
            if candle is None and instant(retry_at) > now:
                continue
            if self.stopped():
                break
            if now < self.close_at:
                fairness = self._record_turn(lane, symbol, fairness)
            history = self.histories.get(symbol)
            local = {symbol: history if history is not None else self._empty_history(symbol)}
            try:
                incoming = LiveHistories(histories=local, provider=self.provider, store=self.store,
                    journal=self.journal, reference_at=reference, clock=self.clock, bounded=True,
                    stopped=lambda: self.stopped() or (reference != self.final_reference
                        and instant(self.clock()) >= self.close_at), reuse_saved=True,
                    population_contract=self.population_contract)
                incoming.bind_cycle(self.session_id)
                self._storage("acquire_completed_interval", lambda: incoming.get(symbol))
                candle = self._saved(symbol, reference, instant(self.clock()))
                if candle is None:
                    raise ContractError("Collection completed without its exact durable validated candle")
                self._update("save_completed_interval", "UPDATE intraday_candle_collection SET status='saved',last_error=NULL "
                             "WHERE session_id=? AND symbol=? AND reference_at=?",
                             (self.session_id, symbol, raw_reference))
                saved_count += 1
                self._event("candle_collection_saved", {
                    "reference_at": raw_reference, "available_at": candle["available_at"],
                    "observed_constituents": candle.get("observed_constituents"),
                    "synthetic_constituents": candle.get("synthetic_constituents"),
                    "synthetic": bool(candle.get("synthetic")), "final_interval": reference == self.final_reference}, symbol=symbol)
            except SourceIntervalEmpty as exc:
                # A successful empty source request completes acquisition, not a
                # price observation or outcome. Preserve its exact receipt.
                self._update("save_source_empty", "UPDATE intraday_candle_collection SET status='source_empty',last_error=? "
                             "WHERE session_id=? AND symbol=? AND reference_at=?",
                             (encoded(exc.evidence), self.session_id, symbol, raw_reference))
                self._event("candle_collection_source_empty", {
                    "reference_at":raw_reference, "source_dataset":self.provider.dataset,
                    "no_market_wide_no_trade_claim":True, "evidence":exc.evidence},symbol=symbol)
            except InterruptedError:
                if self.stopped():
                    break
                if reference != self.final_reference and instant(self.clock()) >= self.close_at:
                    continue
                raise
            except Exception as exc:
                error = encoded({"error_type": type(exc).__name__, "message": str(exc)})
                delay = max(4.0, float(getattr(exc, "retry_after_seconds", 4.0)))
                due = instant(self.clock()) + timedelta(seconds=delay)
                self._update("save_acquisition_retry", "UPDATE intraday_candle_collection SET next_attempt_at=?,last_error=? "
                             "WHERE session_id=? AND symbol=? AND reference_at=?",
                             (due.isoformat(), error, self.session_id, symbol, raw_reference))
                if old_error != error:
                    self._event("candle_collection_retry", {
                        "reference_at": raw_reference, "retry_at": due.isoformat(), **json.loads(error)}, symbol=symbol)
            finally:
                # Includes actual predecessor recovery even if this slot still
                # needs another pass; the original History object is untouched.
                self.histories[symbol] = local[symbol]
        now = instant(self.clock())
        if now < self.close_at:
            remaining = list(self.symbols)
        elif self.final_interval_eligible:
            with self.store.connect() as db:
                empty = {row[0] for row in db.execute("SELECT symbol FROM intraday_candle_collection "
                    "WHERE session_id=? AND reference_at=? AND status='source_empty'",
                    (self.session_id,self.final_reference.isoformat()))}
            remaining = [symbol for symbol in self.symbols if symbol not in empty
                         and self._saved(symbol, self.final_reference, now) is None]
        else:
            remaining = []
        complete = now >= self.close_at and not remaining and not self.stopped()
        if complete and not self.completed:
            self._event("final_candle_collection_completed", {
                "reference_at": self.final_reference.isoformat(), "closed_at": self.close_at.isoformat(),
                "symbols": list(self.symbols), "provider_published_after_close_allowed": True,
                "final_interval_eligible": self.final_interval_eligible,
                "next_day_autostart": False})
            self.completed = True
        return {"closing": now >= self.close_at, "final_complete": complete,
                "pending_final_symbols": remaining, "saved": saved_count}


def collection_loop(store, *, histories, provider, journal, symbols, session_id, started_at,
                    close_at=None, publication_offset_seconds=30, clock=utcnow,
                    stopped=lambda: False, pause=time.sleep, on_completed=None,population_contract=None):
    try:
        collector = _retry_collection_storage(lambda: CandleCollector(store, histories=histories,
            provider=provider, journal=journal, symbols=symbols, session_id=session_id,
            started_at=started_at, close_at=close_at, publication_offset_seconds=publication_offset_seconds,
            clock=clock, stopped=stopped, population_contract=population_contract, pause=pause),
            store=store, session_id=session_id, operation="initialize", stopped=stopped, pause=pause)
        while not stopped():
            result = collector._storage("pass_once", collector.pass_once)
            if result["final_complete"]:
                if on_completed is not None:
                    on_completed(result)
                return result
            pause(1)
    except InterruptedError:
        if not stopped():
            raise
    return {"final_complete": False, "manual_stop_preserved": True}
