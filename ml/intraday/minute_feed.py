"""Independent closed-minute ingestion for one manually started trading day.

The feed owns market-data acquisition only. Models consume saved frames without
fetching; a model rejection never prevents the next quarter from being saved.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
import json
import threading
import uuid

import pandas as pd

from . import data
from .prediction import ContractError, PACIFIC, STEP, instant
from .store import utcnow


VERSION = "closed-quarter-minute-feed-v1"


class _HistorySource:
    def __init__(self, feed, reference):
        self.feed, self.reference = feed, instant(reference)

    def bind_cycle(self, cycle_id):
        # Acquisition records belong to the independent data feed, not a model.
        pass

    def get(self, symbol):
        return self.feed.history_at(symbol, self.reference)


class QuarterHourMinuteFeed:
    def __init__(self, *, provider, store, journal, histories, pc, symbols,
                 session_id, clock=utcnow, start_reference=None,
                 publication_offset_seconds=0, on_closed=None):
        self.provider, self.store, self.journal = provider, store, journal
        self.histories, self.clock = histories, clock
        self.pc, self.symbols, self.session_id = pc, tuple(symbols), session_id
        if (pc not in {"Scout", "Atlas"} or not self.symbols
                or len(set(self.symbols)) != len(self.symbols) or not session_id
                or any(not isinstance(s, str) or not s for s in self.symbols)):
            raise ContractError("An explicit independent local feed identity is required")
        self.dataset = getattr(provider, "dataset", None)
        if self.dataset != "EQUS.MINI":
            raise ContractError("Minute feed requires the existing EQUS.MINI provider")
        self.source_id = f"databento:{self.dataset}:ohlcv-1m"
        if not 0 <= float(publication_offset_seconds) < 900:
            raise ContractError("Minute feed offset must be within one quarter-hour")
        self.offset = timedelta(seconds=float(publication_offset_seconds))
        self.on_closed = on_closed
        self._lock = threading.RLock()
        self.identity = "minute-feed:" + pc + ":" + session_id
        self.pending, self.outcomes, self.served = {}, {}, {}
        self._owned_attempts, self._request_error_due = set(), {}
        self.closed = False
        now = instant(clock())
        with store.connect() as db:
            saved = db.execute("SELECT kind,symbol,payload FROM events WHERE cycle_id=? ORDER BY id",
                               (self.identity,)).fetchall()
        initialized = [json.loads(raw) for kind, _, raw in saved if kind == "minute_feed_initialized"]
        if initialized:
            initial = initialized[0]
            if (initial.get("version") != VERSION or initial.get("pc") != pc
                    or initial.get("symbols") != list(self.symbols)
                    or initial.get("source_dataset") != self.dataset):
                raise ContractError("Saved minute feed identity or source differs")
            self.day = pd.Timestamp(initial["day"]).date()
            self.first = instant(initial["first_reference"])
        else:
            proposed = instant(start_reference) if start_reference is not None else (
                pd.Timestamp(now - self.offset).floor("15min").to_pydatetime() - STEP)
            self.day = (proposed if start_reference is not None else now).astimezone(PACIFIC).date()
            lower = datetime.combine(self.day, time(4), tzinfo=PACIFIC).astimezone(timezone.utc)
            self.first = max(lower, proposed)
            if self.first.minute % 15 or self.first.second or self.first.microsecond:
                raise ContractError("Minute feed start must name an exact quarter-hour opening")
        self.end = datetime.combine(self.day, time(17), tzinfo=PACIFIC).astimezone(timezone.utc)
        if not initialized:
            # A manual restart after the day's close can still save today's
            # final completed window; it never opens tomorrow's acquisition.
            self.first = min(self.first, self.end - STEP)
        lower = datetime.combine(self.day, time(4), tzinfo=PACIFIC).astimezone(timezone.utc)
        self.sessions = data.equity_sessions(lower, self.end)
        self.next_reference = self.first
        self._restore_histories()
        if not initialized:
            self._event("minute_feed_initialized", dict(version=VERSION, pc=pc,
                symbols=list(self.symbols), source_dataset=self.dataset, day=str(self.day),
                first_reference=self.first.isoformat(), close_at=self.end.isoformat()))
        for kind, symbol, raw in saved:
            value = json.loads(raw)
            if kind == "minute_feed_window_queued":
                reference = instant(value["reference_at"])
                if reference < self.first or reference + STEP > self.end:
                    raise ContractError("Saved minute feed window exceeds its latched day")
                self.pending.setdefault(reference, set(self.symbols))
                self.next_reference = max(self.next_reference, reference + STEP)
            elif kind == "minute_feed_symbol_completed":
                reference = instant(value["reference_at"])
                if symbol not in self.symbols or reference not in self.pending:
                    raise ContractError("Saved minute feed completion lacks its own queued window")
                self.outcomes[reference, symbol] = value
                self.pending[reference].discard(symbol)
            elif kind == "minute_feed_batch_saved":
                self._owned_attempts.update(value["attempt_ids"])
            elif kind == "minute_feed_request_error":
                self._request_error_due[instant(value["reference_at"])] = instant(value["retry_at"])

    def _event(self, kind, value, symbol=None):
        self.store.event(self.identity, kind, value, symbol=symbol,
                         recorded_at=instant(self.clock()).isoformat())

    def _merge(self, symbol, candles):
        if not candles:
            return
        history = self.histories.get(symbol)
        manifest = dict(history.manifest) if history is not None else {"source_dataset": self.dataset}
        if manifest.get("source_dataset") != self.dataset:
            raise ContractError("Saved history differs from the feed's single source")
        original = history.frame if history is not None else pd.DataFrame()
        addition = pd.DataFrame(candles)
        for column in ("opened_at", "available_at"):
            addition[column] = pd.to_datetime(addition[column], utc=True)
        merged = (pd.concat([original, addition], ignore_index=True)
                  .sort_values("available_at").drop_duplicates("opened_at", keep="last")
                  .sort_values("opened_at").reset_index(drop=True))
        manifest.update(source_dataset=self.dataset, current_reference_availability="actual")
        merged.attrs["intraday_history"] = manifest
        self.histories[symbol] = data.History(merged, manifest)

    def _restore_histories(self):
        now = instant(self.clock())
        with self._lock:
            for symbol in self.symbols:
                history = self.histories.get(symbol)
                if history is not None:
                    invalid = [index for index, row in history.frame.loc[history.frame.synthetic.eq(True)].iterrows()
                               if not data.synthetic_continuity_verified(row.to_dict())]
                    if invalid:
                        clean = history.frame.drop(index=invalid).copy()
                        clean.attrs["intraday_history"] = history.manifest
                        self.histories[symbol] = data.History(clean, history.manifest)
                        self._event("minute_feed_invalid_synthetic_excluded", {
                            "history_rows_excluded": len(invalid), "immutable_originals_retained": True}, symbol)
                saved = [c for c in self.store.observed_candles(symbol)
                         if c.get("source_id") == self.source_id
                         and c.get("source_dataset") == self.dataset
                         and instant(c["available_at"]) <= now
                         and data.synthetic_continuity_verified(c)]
                self._merge(symbol, saved)

    def _session(self, reference):
        return next((session for session in self.sessions if session.contains(reference)), None)

    def _enqueue(self, now):
        while self.next_reference + STEP <= min(now - self.offset, self.end):
            reference = self.next_reference
            self.next_reference += STEP
            if reference.astimezone(PACIFIC).date() != self.day or self._session(reference) is None:
                continue
            self._event("minute_feed_window_queued", {"reference_at": reference.isoformat(),
                "interval_close": (reference + STEP).isoformat()})
            with self._lock:
                self.pending[reference] = set(self.symbols)

    def _slot(self, symbol, reference):
        return dict(symbol=symbol, source_id=self.source_id,
                    interval_open=reference.isoformat(), interval_close=(reference + STEP).isoformat())

    def _attempts(self, symbol, reference):
        # Older model-coupled journal responses need not have canonical native
        # persistence. Only this feed's successful saved batches complete it.
        return tuple(a for a in self.journal.responses(**self._slot(symbol, reference))
                     if a.attempt_id in self._owned_attempts)

    def _previous(self, symbol, reference):
        with self._lock:
            history = self.histories.get(symbol)
            if history is None:
                return None
            frame = history.frame
            before = frame.loc[pd.to_datetime(frame.opened_at, utc=True).eq(pd.Timestamp(reference - STEP))
                               & pd.to_datetime(frame.available_at, utc=True).le(pd.Timestamp(instant(self.clock())))]
            if before.empty:
                return None
            value = before.iloc[-1].to_dict()
            for name in ("opened_at", "available_at"):
                value[name] = instant(value[name]).isoformat()
            return value if data.synthetic_continuity_verified(value) else None

    def _resolve(self, symbol, reference, attempts):
        if not attempts:
            return False
        previous = self._previous(symbol, reference)
        final = attempts[-1]
        needs_previous = (final.status == "success" and final.covered()
                          and not any(data.utc(row.get("opened_at", row.get("timestamp"))) == data.utc(reference)
                                      for row in final.rows))
        if needs_previous:
            with self._lock:
                predecessor_pending = symbol in self.pending.get(reference - STEP, set())
            if predecessor_pending:
                # Confirmed leading omissions can become usable when this
                # feed's own preceding quarter completes. Keep native evidence
                # pending rather than sealing a premature unusable outcome.
                return False
            if (previous is not None
                    and instant(previous["available_at"]) > instant(final.request_ended_at)):
                # A newly recovered prior close was not known at this attempt's
                # receipt. Request the current interval again so its actual
                # availability can include the genuinely late dependency.
                return False
        try:
            result = data.resolve_minute_candle(attempts, session=self._session(reference),
                                               previous=previous)
        except (ContractError, IndexError) as exc:
            # Canonical native bytes and immutable attempts are already saved.
            # Invalid zero/other OHLC is never turned into a fake usable candle.
            if attempts[-1].status != "success" or not attempts[-1].covered():
                raise
            result = {"status": "unusable", "reason": "invalid_observed_aggregate", "candle": None,
                      "error_type": type(exc).__name__, "message": str(exc)}
        candle = result.get("candle")
        terminal_missing = result.get("status") == "missing" and result.get("reason") in {
            "no_prior_known_price", "unverified_prior_price_continuity"}
        if candle is None and not terminal_missing and result.get("status") != "unusable":
            return False
        value = {"reference_at": reference.isoformat(), "status": result["status"],
                 "reason": result.get("reason"), "attempt_ids": [a.attempt_id for a in attempts],
                 "source_dataset": self.dataset, "native_window_saved": True}
        if candle is not None:
            value["candle_sha256"] = self.store.record_candle(candle)
            with self._lock:
                self._merge(symbol, [candle])
        else:
            value["status"] = "resolved_no_usable_15m"
            value.update({key: result[key] for key in ("error_type", "message") if key in result})
        self._event("minute_feed_symbol_completed", value, symbol)
        with self._lock:
            self.outcomes[reference, symbol] = value
            self.pending[reference].discard(symbol)
        return True

    def step(self):
        """At most one exact native-minute batch; never sleep or fit a model."""
        if self.closed:
            return {"status": "closed", "retry_after_seconds": 0}
        now = instant(self.clock())
        self._enqueue(now)
        pending = [r for r, symbols in self.pending.items() if symbols]
        # New quarters cannot be held behind an older unavailable response.
        ordered = sorted(pending, key=lambda r: (r in self.served,
                         self.served.get(r, datetime.min.replace(tzinfo=timezone.utc)), -r.timestamp()))
        lock = data._provider_request_lock(self.journal)
        if not lock.acquire(blocking=False):
            return {"status": "waiting", "reason": "provider_request_in_flight", "retry_after_seconds": 1}
        try:
            for reference in ordered:
                if now < self._request_error_due.get(reference, now):
                    continue
                ready = []
                for symbol in self.symbols:
                    if symbol not in self.pending[reference]:
                        continue
                    attempts = self._attempts(symbol, reference)
                    if self._resolve(symbol, reference, attempts):
                        continue
                    if not attempts or instant(attempts[-1].request_ended_at) + timedelta(
                            seconds=max(4, attempts[-1].retry_after_seconds)) <= now:
                        ready.append(symbol)
                if not ready:
                    continue
                provider_due = self.journal.provider_retry_due()
                if provider_due is not None and instant(provider_due) > now:
                    return {"status": "waiting", "reason": "provider_retry_after",
                            "retry_after_seconds": (instant(provider_due) - now).total_seconds()}
                self.served[reference] = now
                try:
                    responses = data.fetch_databento_interval_attempts(self.provider, symbols=ready,
                        opened_at=reference, attempt_id=uuid.uuid4().hex, clock=self.clock)
                except Exception as exc:
                    due = instant(self.clock()) + timedelta(seconds=4)
                    self._request_error_due[reference] = due
                    self._event("minute_feed_request_error", {"reference_at": reference.isoformat(),
                        "error_type": type(exc).__name__, "message": str(exc), "retry_after_seconds": 4,
                        "retry_at": due.isoformat()})
                    return {"status": "retry", "reason": "request_error", "retry_after_seconds": 4}
                if set(responses) != set(ready):
                    raise ContractError("Minute feed batch changed its requested symbol set")
                for symbol in ready:
                    response = responses[symbol]
                    if (response.symbol != symbol or response.source_id != self.source_id
                            or response.source_dataset != self.dataset
                            or instant(response.interval_open) != reference
                            or instant(response.interval_close) != reference + STEP):
                        raise ContractError("Minute feed batch changed its exact interval/source identity")
                    self.journal.save(response)
                ids = [responses[symbol].attempt_id for symbol in ready]
                self._event("minute_feed_batch_saved", {"reference_at": reference.isoformat(), "attempt_ids": ids})
                self._owned_attempts.update(ids)
                for symbol in ready:
                    self._resolve(symbol, reference, self._attempts(symbol, reference))
                return {"status": "acquired", "reference_at": reference.isoformat(), "symbols": ready,
                        "retry_after_seconds": 1}
        finally:
            lock.release()
        if now >= self.end + self.offset and self.next_reference >= self.end and not any(self.pending.values()):
            if not self.closed:
                self.closed = True
                result = {"status": "closed", "close_at": self.end.isoformat(),
                    "all_queued_native_windows_saved": True, "later_sessions_automatic": False}
                self._event("minute_feed_closed", result)
                if self.on_closed:
                    self.on_closed(result)
            return {"status": "closed", "retry_after_seconds": 0}
        return {"status": "waiting", "reason": "closed_quarter_or_retry_wait", "retry_after_seconds": 1}

    def run(self, *, stopped, pause):
        while not stopped():
            result = self.step()
            if result["status"] == "closed":
                return result
            pause(min(4, max(.1, result.get("retry_after_seconds", 1))))
        return {"status": "manual_stop", "pending_windows_retained": True}

    def history_source(self, reference):
        return _HistorySource(self, reference)

    def history_snapshot(self):
        with self._lock:
            return dict(self.histories)

    def history_at(self, symbol, reference):
        reference = instant(reference)
        if symbol not in self.symbols:
            raise ContractError("History requested for a symbol outside this independent feed")
        if reference < self.first or reference + STEP > self.end or self._session(reference) is None:
            # Scoring a prior session may reuse an actually saved candle, but
            # it never expands this feed's acquisition day or causes a request.
            with self._lock:
                history = self.histories.get(symbol)
                if history is not None:
                    frame = history.frame
                    matching = frame.loc[pd.to_datetime(frame.opened_at, utc=True).eq(pd.Timestamp(reference))
                        & pd.to_datetime(frame.available_at, utc=True).le(pd.Timestamp(instant(self.clock())))
                        & frame.availability_kind.eq("actual")
                        & frame.source_id.eq(self.source_id) & frame.source_dataset.eq(self.dataset)]
                    if not matching.empty and data.synthetic_continuity_verified(matching.iloc[-1].to_dict()):
                        return history
            raise ContractError("History reference lies outside this feed's latched eligible day")
        with self._lock:
            result = self.outcomes.get((reference, symbol))
            if result is None:
                raise data.AcquisitionPending("independent_minute_feed_pending", 1)
            if result["status"] == "resolved_no_usable_15m":
                raise ContractError("Exact saved native minute window has no usable 15m candle: " + str(result.get("reason")))
            history = self.histories.get(symbol)
            if history is None or not pd.to_datetime(history.frame.opened_at, utc=True).eq(pd.Timestamp(reference)).any():
                raise ContractError("Completed feed candle is missing from its saved history")
            return history
