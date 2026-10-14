"""Own-session completed candles, including delayed publication of the final bar."""
from __future__ import annotations

from datetime import timedelta
import json
import time

import pandas as pd

from .prediction import ContractError, PACIFIC, STEP, encoded, instant
from .runtime import LiveHistories
from .store import utcnow


def market_close_at(started_at):
    """Freeze this manually started session's 17:00 Pacific cutoff across DST."""
    local = instant(started_at).astimezone(PACIFIC)
    return instant(local.replace(hour=17, minute=0, second=0, microsecond=0))


class CandleCollector:
    """Durable exact intervals; collection has no model or execution interface."""
    def __init__(self, store, *, histories, provider, journal, symbols, session_id,
                 started_at, close_at=None, publication_offset_seconds=30,
                 clock=utcnow, stopped=lambda: False):
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
        self.clock, self.stopped = clock, stopped
        self.source_id = f"databento:{provider.dataset}:ohlcv-1m"
        self.completed = False
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS intraday_candle_collection(
                session_id TEXT NOT NULL,symbol TEXT NOT NULL,reference_at TEXT NOT NULL,
                status TEXT NOT NULL,next_attempt_at TEXT NOT NULL,last_error TEXT,
                PRIMARY KEY(session_id,symbol,reference_at))""")

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
                     and candle.get("source_dataset") == self.provider.dataset
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
                               "history_policy": "missing_original_history_retained_no_invented_rows"})

    def pass_once(self):
        """One bounded pass. After close, older retries cannot delay final slots."""
        if self.stopped():
            return {"closing": instant(self.clock()) >= self.close_at, "final_complete": False,
                    "pending_final_symbols": list(self.symbols), "saved": 0}
        now = instant(self.clock())
        self._schedule(now)
        with self.store.connect() as db:
            pending = db.execute("SELECT symbol,reference_at,next_attempt_at,last_error FROM intraday_candle_collection "
                                 "WHERE session_id=? AND status='pending' ORDER BY reference_at DESC,symbol",
                                 (self.session_id,)).fetchall()
        saved_count = 0
        for symbol, raw_reference, retry_at, old_error in pending:
            if self.stopped():
                break
            now, reference = instant(self.clock()), instant(raw_reference)
            if now >= self.close_at and reference != self.final_reference:
                continue
            candle = self._saved(symbol, reference, now)
            if candle is None and instant(retry_at) > now:
                continue
            history = self.histories.get(symbol)
            local = {symbol: history if history is not None else self._empty_history(symbol)}
            try:
                incoming = LiveHistories(histories=local, provider=self.provider, store=self.store,
                    journal=self.journal, reference_at=reference, clock=self.clock, bounded=True,
                    stopped=lambda: self.stopped() or (reference != self.final_reference
                        and instant(self.clock()) >= self.close_at), reuse_saved=True)
                incoming.bind_cycle(self.session_id)
                incoming.get(symbol)
                candle = self._saved(symbol, reference, instant(self.clock()))
                if candle is None:
                    raise ContractError("Collection completed without its exact durable validated candle")
                with self.store.connect() as db:
                    db.execute("UPDATE intraday_candle_collection SET status='saved',last_error=NULL "
                               "WHERE session_id=? AND symbol=? AND reference_at=?",
                               (self.session_id, symbol, raw_reference))
                saved_count += 1
                self.store.event(self.session_id, "candle_collection_saved", {
                    "reference_at": raw_reference, "available_at": candle["available_at"],
                    "observed_constituents": candle.get("observed_constituents"),
                    "synthetic_constituents": candle.get("synthetic_constituents"),
                    "synthetic": bool(candle.get("synthetic")), "final_interval": reference == self.final_reference}, symbol=symbol)
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
                with self.store.connect() as db:
                    db.execute("UPDATE intraday_candle_collection SET next_attempt_at=?,last_error=? "
                               "WHERE session_id=? AND symbol=? AND reference_at=?",
                               (due.isoformat(), error, self.session_id, symbol, raw_reference))
                if old_error != error:
                    self.store.event(self.session_id, "candle_collection_retry", {
                        "reference_at": raw_reference, "retry_at": due.isoformat(), **json.loads(error)}, symbol=symbol)
            finally:
                # Includes actual predecessor recovery even if this slot still
                # needs another pass; the original History object is untouched.
                self.histories[symbol] = local[symbol]
        now = instant(self.clock())
        if now < self.close_at:
            remaining = list(self.symbols)
        elif self.final_interval_eligible:
            remaining = [symbol for symbol in self.symbols if self._saved(symbol, self.final_reference, now) is None]
        else:
            remaining = []
        complete = now >= self.close_at and not remaining and not self.stopped()
        if complete and not self.completed:
            self.store.event(self.session_id, "final_candle_collection_completed", {
                "reference_at": self.final_reference.isoformat(), "closed_at": self.close_at.isoformat(),
                "symbols": list(self.symbols), "provider_published_after_close_allowed": True,
                "final_interval_eligible": self.final_interval_eligible,
                "next_day_autostart": False})
            self.completed = True
        return {"closing": now >= self.close_at, "final_complete": complete,
                "pending_final_symbols": remaining, "saved": saved_count}


def collection_loop(store, *, histories, provider, journal, symbols, session_id, started_at,
                    close_at=None, publication_offset_seconds=30, clock=utcnow,
                    stopped=lambda: False, pause=time.sleep, on_completed=None):
    collector = CandleCollector(store, histories=histories, provider=provider, journal=journal,
        symbols=symbols, session_id=session_id, started_at=started_at, close_at=close_at,
        publication_offset_seconds=publication_offset_seconds, clock=clock, stopped=stopped)
    while not stopped():
        result = collector.pass_once()
        if result["final_complete"]:
            if on_completed is not None:
                on_completed(result)
            return result
        pause(1)
    return {"final_complete": False, "manual_stop_preserved": True}
