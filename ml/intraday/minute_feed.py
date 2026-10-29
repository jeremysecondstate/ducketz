"""Independent closed-minute ingestion for one manually started trading day.

The feed owns market-data acquisition only. Models consume saved frames without
fetching; a model rejection never prevents the next quarter from being saved.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from copy import deepcopy
import hashlib
import json
import threading
import uuid

import pandas as pd

from . import data
from .prediction import ContractError, PACIFIC, STEP, digest, encoded, instant
from .store import utcnow


VERSION = "closed-quarter-minute-feed-v1"


class _HistorySource:
    def __init__(self, feed, reference, *, genuine_only=False):
        self.feed, self.reference = feed, instant(reference)
        self.genuine_only = genuine_only

    def bind_cycle(self, cycle_id):
        # Acquisition records belong to the independent data feed, not a model.
        pass

    def get(self, symbol):
        if self.genuine_only:
            return self.feed.history_at(symbol, self.reference, genuine_only=True)
        return self.feed.history_at(symbol, self.reference)


class QuarterHourMinuteFeed:
    def __init__(self, *, provider, store, journal, histories, pc, symbols,
                 session_id, clock=utcnow, start_reference=None,
                 publication_offset_seconds=0, on_closed=None, native_interval_symbols=(), reference_fill_policy=None,
                 event_persistence_stopped=None, event_persistence_pause=None, on_event_persistence_retry=None):
        self._event_persistence_stopped = event_persistence_stopped
        self._event_persistence_pause = event_persistence_pause
        self._on_event_persistence_retry = on_event_persistence_retry
        self.provider, self.store, self.journal = provider, store, journal
        self.histories, self.clock = histories, clock
        self.pc, self.symbols, self.session_id = pc, tuple(symbols), session_id
        if (pc not in {"Scout", "Atlas"} or not self.symbols
                or len(set(self.symbols)) != len(self.symbols) or not session_id
                or any(not isinstance(s, str) or not s for s in self.symbols)):
            raise ContractError("An explicit independent local feed identity is required")
        if (not isinstance(native_interval_symbols, (list, tuple))
                or any(not isinstance(s, str) for s in native_interval_symbols)
                or len(set(native_interval_symbols)) != len(native_interval_symbols)
                or not set(native_interval_symbols) <= set(self.symbols)):
            raise ContractError("Native interval scope must be an explicit subset of this feed's symbols")
        self.native_interval_symbols = tuple(native_interval_symbols)
        self.reference_fill_policy = deepcopy(reference_fill_policy)
        if reference_fill_policy is not None:
            from .forward_fill import policy_contract
            if reference_fill_policy != policy_contract() or set(self.native_interval_symbols) != set(self.symbols):
                raise ContractError("Reference filling requires the exact frozen policy and native local scope")
        self.native_interval_contract = None
        if self.native_interval_symbols:
            from .native_interval import CONTRACT
            self.native_interval_contract = CONTRACT
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
        self.reference_fills = {}
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
                    or initial.get("source_dataset") != self.dataset
                    or initial.get("native_interval_symbols", []) != list(self.native_interval_symbols)
                    or initial.get("reference_fill_policy") != self.reference_fill_policy
                    or initial.get("native_interval_contract") != self.native_interval_contract):
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
                first_reference=self.first.isoformat(), close_at=self.end.isoformat(),
                **({"reference_fill_policy": self.reference_fill_policy} if self.reference_fill_policy else {}),
                **({"native_interval_symbols": list(self.native_interval_symbols),
                    "native_interval_contract": self.native_interval_contract} if self.native_interval_symbols else {})))
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
        if self.reference_fill_policy is not None:
            self._restore_reference_rechecks()

    def _event(self, kind, value, symbol=None):
        stopped, pause = self._event_persistence_stopped, self._event_persistence_pause
        if self.pc != "Scout" or stopped is None or pause is None:
            return self.store.event(self.identity, kind, value, symbol=symbol,
                                    recorded_at=instant(self.clock()).isoformat())
        # Journaled responses, cursor changes and completed acquisition stay in
        # this stack frame while only the original event INSERT is retried.
        frozen, recorded = json.loads(encoded(value)), instant(self.clock()).isoformat()
        pending = {"operation": "minute_feed_event", "feed_id": self.identity,
                   "source_session": self.session_id, "pc": self.pc, "kind": kind,
                   "symbol": symbol, "recorded_at": recorded, "payload_sha256": digest(frozen)}
        episode = None
        def receipt(phase):
            if self._on_event_persistence_retry is not None:
                try:
                    self._on_event_persistence_retry(phase, {**pending, **episode})
                except Exception:
                    pass  # Optional evidence cannot replay or mask the effect.
        while True:
            if stopped():
                if episode is not None:
                    receipt("stopped")
                raise InterruptedError("Minute feed stopped with original event pending")
            try:
                result = self.store.event(self.identity, kind, frozen, symbol=symbol, recorded_at=recorded)
            except Exception as exc:
                from .live_session import _execution_store_busy
                if not _execution_store_busy(exc):
                    raise
                if episode is None:
                    episode = {"episode_id": uuid.uuid4().hex, "busy_count": 1}
                    try:
                        from .runtime import _symbol_exception_diagnostics
                        episode["diagnostics"] = _symbol_exception_diagnostics(exc)
                    except Exception:
                        pass
                    receipt("started")
                else:
                    episode["busy_count"] += 1
                pause(.2)
            else:
                if episode is not None:
                    receipt("recovered")
                return result

    def _fill_id(self, symbol, reference):
        return digest({"pc": self.pc, "symbol": symbol, "reference_at": reference.isoformat(),
                       "source_id": self.source_id, "policy": self.reference_fill_policy})

    def _restore_reference_rechecks(self):
        # These are this PC's own feed records, including unfinished work from
        # a prior manual session. They never open another session or trader.
        kinds = ("minute_feed_reference_filled", "minute_feed_reference_fill_revised", "minute_feed_reference_recheck_started",
                 "minute_feed_reference_recheck_batch_saved", "minute_feed_reference_recheck_completed",
                 "minute_feed_reference_recheck_request_error")
        with self.store.connect() as db:
            saved = db.execute("SELECT kind,symbol,payload FROM events WHERE cycle_id LIKE ? AND kind IN (?,?,?,?,?,?) ORDER BY id",
                ("minute-feed:" + self.pc + ":%", *kinds)).fetchall()
        for kind, symbol, raw in saved:
            value = json.loads(raw)
            if symbol not in self.symbols:
                continue
            reference = instant(value["reference_at"])
            key = (reference, symbol)
            if value.get("fill_id") != self._fill_id(symbol, reference):
                continue  # A distinct source or explicitly different policy is not this scope.
            if kind == "minute_feed_reference_filled":
                self._validate_fill_receipt(value, symbol, reference)
                if type(value.get("projection_revision")) is not int or value["projection_revision"] != 0 or key in self.reference_fills:
                    raise ContractError("Original reference fill identity must be unique")
                self.reference_fills.setdefault(key, {"record": value, "attempt_ids": set(),
                    "latest_projection": value, "started": False, "completed": None, "retry_at": None})
                continue
            state = self.reference_fills.get(key)
            if state is None:
                raise ContractError("Reference recheck lacks its original recorded fill")
            if kind == "minute_feed_reference_fill_revised":
                self._validate_fill_receipt(value, symbol, reference)
                previous = state["latest_projection"]
                if (value.get("prior_reference_sha256") != previous["reference_sha256"]
                        or type(value.get("projection_revision")) is not int
                        or value["projection_revision"] != previous["projection_revision"] + 1
                        or value["reference_sha256"] == previous["reference_sha256"]
                        or value["recheck_due_at"] != state["record"]["recheck_due_at"]):
                    raise ContractError("Reference projection revision does not follow its immutable receipt")
                state["latest_projection"] = value
            elif kind == "minute_feed_reference_recheck_started":
                state["started"] = True
            elif kind == "minute_feed_reference_recheck_batch_saved":
                state["attempt_ids"].add(value["attempt_id"])
            elif kind == "minute_feed_reference_recheck_completed":
                state["completed"] = value
            elif kind == "minute_feed_reference_recheck_request_error":
                state["retry_at"] = instant(value["retry_at"])

    def _validate_fill_receipt(self, value, symbol, reference):
        from .forward_fill import verified_reference
        candle = value.get("reference")
        if (not isinstance(candle, dict) or not verified_reference(candle)
                or candle["symbol"] != symbol or candle["source_id"] != self.source_id
                or candle["source_dataset"] != self.dataset or instant(candle["opened_at"]) != reference
                or value.get("source_id") != self.source_id or value.get("source_dataset") != self.dataset
                or value.get("interval_close") != (reference + STEP).isoformat()
                or value.get("reference_fill_policy") != self.reference_fill_policy
                or value.get("reference_sha256") != digest(candle)
                or value.get("evidence_sha256") != hashlib.sha256(candle["evidence_json"].encode()).hexdigest()
                or any(value.get(field) != candle[field] for field in
                    ("anchor_sha256", "anchor_opened_at", "anchor_available_at", "anchor_age_seconds",
                     "anchor_last_observation_at", "anchor_observation_age_seconds", "reference_price_basis"))):
            raise ContractError("Recorded reference projection does not match its full source proof")
        proof = json.loads(candle["evidence_json"])
        if value.get("anchor_source_evidence_sha256") != hashlib.sha256(proof["anchor"]["evidence_json"].encode()).hexdigest():
            raise ContractError("Recorded reference anchor evidence checksum differs")
        if not reference + 2 * STEP <= instant(value["recheck_due_at"]) < reference + 3 * STEP:
            raise ContractError("Recorded reference overlap must be due at the next completed quarter")

    def _record_reference_fill(self, symbol, reference, candle):
        key = (reference, symbol)
        existing = self.reference_fills.get(key)
        if existing is not None and existing["latest_projection"]["reference_sha256"] == digest(candle):
            return
        proof = json.loads(candle["evidence_json"])
        value = {"fill_id": self._fill_id(symbol, reference), "reference_at": reference.isoformat(),
            "interval_close": (reference + STEP).isoformat(), "source_id": self.source_id,
            "source_dataset": self.dataset, "reference_fill_policy": deepcopy(self.reference_fill_policy),
            "reference_sha256": digest(candle), "reference": deepcopy(candle),
            "evidence_sha256": hashlib.sha256(candle["evidence_json"].encode()).hexdigest(),
            "anchor_source_evidence_sha256": hashlib.sha256(proof["anchor"]["evidence_json"].encode()).hexdigest(),
            **{field: candle[field] for field in ("anchor_sha256", "anchor_opened_at", "anchor_available_at",
                                                 "anchor_age_seconds", "anchor_last_observation_at",
                                                 "anchor_observation_age_seconds", "reference_price_basis")},
            "recheck_due_at": (reference + 2 * STEP + self.offset).isoformat(),
            "recheck_policy": "one_fresh_overlap_at_next_completed_quarter_resume_across_manual_sessions",
            "original_source_and_forecasts_unchanged": True, "projection_revision": 0}
        if existing is not None:
            previous = existing["latest_projection"]
            value.update(prior_reference_sha256=previous["reference_sha256"],
                projection_revision=previous["projection_revision"] + 1,
                recheck_due_at=existing["record"]["recheck_due_at"])
            self._event("minute_feed_reference_fill_revised", value, symbol)
            existing["latest_projection"] = value
            return
        self._event("minute_feed_reference_filled", value, symbol)
        self.reference_fills[key] = {"record": value, "attempt_ids": set(), "started": False,
                                     "latest_projection": value, "completed": None, "retry_at": None}

    def _exact_genuine(self, symbol, reference):
        history = self.histories.get(symbol)
        if history is None:
            return None
        frame = history.frame
        match = frame.loc[pd.to_datetime(frame.opened_at, utc=True).eq(pd.Timestamp(reference))
            & pd.to_datetime(frame.available_at, utc=True).le(pd.Timestamp(instant(self.clock())))
            & frame.availability_kind.eq("actual") & frame.synthetic.eq(False)
            & frame.source_id.eq(self.source_id) & frame.source_dataset.eq(self.dataset)]
        for row in reversed(match.to_dict("records")):
            if self._matches_interval_contract(symbol, row) and data.completed_15m_observation(row, from_frame=True):
                return row
        return None

    def _recheck_attempts(self, symbol, reference, state):
        # The initial terminal empty responses must never satisfy a recheck.
        return tuple(a for a in self.journal.responses(**self._slot(symbol, reference))
                     if a.attempt_id in state["attempt_ids"])

    def _resolve_recheck(self, symbol, reference, state, attempts):
        if not attempts:
            return False
        from .native_interval import resolve_native_interval
        final = attempts[-1]
        expired_range = None
        if (final.status == "unavailable_coverage"
                and final.error == "requested_interval_outside_advertised_schema_range"
                and final.coverage_open is not None and final.coverage_close is not None):
            lower, upper = data.utc(final.coverage_open), data.utc(final.coverage_close)
            if not pd.isna(lower) and not pd.isna(upper) and lower < upper and lower > pd.Timestamp(reference):
                # This is positive lower-bound evidence from the actual adapter
                # response, not an age guess or a temporarily late schema end.
                # A historical recovery remains open; live polling cannot move
                # this exact older quarter into the advertised request range.
                expired_range = {
                    "status": "historical_recovery_required", "reason": "interval_precedes_advertised_schema_start",
                    "recovery_state": "unrecovered", "live_recheck_disposition": "terminal_for_this_provider_range",
                    "schema_start": final.coverage_open, "schema_end": final.coverage_close,
                    "coverage_bound_basis": "configured_provider_advertised_request_range",
                    "provider_response_status": final.status, "provider_response_error": final.error,
                    "provider_request_started_at": final.request_started_at,
                    "provider_request_ended_at": final.request_ended_at,
                    "remaining_operation": {"pc": self.pc, "symbol": symbol,
                        "source_dataset": self.dataset, "schema": "ohlcv-1m",
                        "interval_open": reference.isoformat(), "interval_close": (reference + STEP).isoformat()},
                    "next_action": "Recover this exact local symbol and interval through the existing receipt-bound native historical-recovery workflow; preserve actual receipt time and merge verified evidence with ml.intraday.native_recovery.merge_recovered_history.",
                    "candle": None}
        session = self._session(reference)
        if session is None:
            session = next((s for s in data.equity_sessions(reference, reference + STEP) if s.contains(reference)), None)
        try:
            if session is None:
                raise ContractError("Reference overlap no longer has a valid exact equity session")
            result = expired_range or resolve_native_interval(attempts, session=session)
        except (ContractError, IndexError) as exc:
            result = {"status": "unusable", "reason": "invalid_observed_aggregate", "candle": None,
                      "error_type": type(exc).__name__, "message": str(exc)}
        candle = result.get("candle")
        if candle is None and result.get("reason") != "source_empty_interval" and result.get("status") not in {"failed", "unusable", "historical_recovery_required"}:
            return False
        value = {"fill_id": state["record"]["fill_id"], "reference_at": reference.isoformat(),
            "attempt_ids": [a.attempt_id for a in attempts], "source_id": self.source_id,
            "source_dataset": self.dataset, "status": "observed" if candle is not None else result["status"],
            "reason": result.get("reason"), "genuine_rows_arrived": candle is not None,
            "frozen_forecasts_unchanged": True}
        if candle is not None:
            if (not self._matches_interval_contract(symbol, candle)
                    or candle.get("symbol") != symbol or candle.get("source_id") != self.source_id
                    or candle.get("source_dataset") != self.dataset or instant(candle["opened_at"]) != reference):
                raise ContractError("Reference overlap changed its exact native source identity")
            value["candle_sha256"] = self.store.record_candle(candle)
            value["observed_constituents"] = candle["observed_constituents"]
            with self._lock:
                self._merge(symbol, [candle])
        else:
            value.update({key: result[key] for key in ("error_type", "message") if key in result})
            if expired_range is not None:
                value.update({key: item for key, item in expired_range.items() if key != "candle"})
        self._event("minute_feed_reference_recheck_completed", value, symbol)
        with self._lock:
            state["completed"] = value
        return True

    def _reference_recheck_step(self, now, stopped):
        # Called only after current feed windows have had their acquisition turn,
        # under the same provider lock and Retry-After policy as ordinary batches.
        with self._lock:
            pending = [(key, state) for key, state in self.reference_fills.items()
                       if state["completed"] is None and instant(state["record"]["recheck_due_at"]) <= now
                       and (state["retry_at"] is None or state["retry_at"] <= now)]
        pending.sort(key=lambda item: (len(item[1]["attempt_ids"]), item[0][0], item[0][1]))
        ready, selected = [], None
        for (reference, symbol), state in pending:
            attempts = self._recheck_attempts(symbol, reference, state)
            if self._resolve_recheck(symbol, reference, state, attempts):
                continue
            if attempts and instant(attempts[-1].request_ended_at) + timedelta(seconds=max(4, attempts[-1].retry_after_seconds)) > now:
                continue
            if selected is None:
                selected = reference
            if reference == selected:
                ready.append(symbol)
        if not ready:
            return None
        provider_due = self.journal.provider_retry_due()
        if provider_due is not None and instant(provider_due) > now:
            return {"status": "waiting", "reason": "provider_retry_after",
                    "retry_after_seconds": (instant(provider_due) - now).total_seconds()}
        for symbol in ready:
            state = self.reference_fills[selected, symbol]
            if not state["started"]:
                self._event("minute_feed_reference_recheck_started", {
                    "fill_id": state["record"]["fill_id"], "reference_at": selected.isoformat(),
                    "source_id": self.source_id, "source_dataset": self.dataset,
                    "fresh_responses_required": True}, symbol)
                state["started"] = True
        try:
            responses = data.fetch_databento_interval_attempts(self.provider, symbols=ready,
                opened_at=selected, attempt_id=uuid.uuid4().hex, clock=self.clock, stopped=stopped)
        except InterruptedError:
            raise
        except Exception as exc:
            due = instant(self.clock()) + timedelta(seconds=4)
            for symbol in ready:
                state = self.reference_fills[selected, symbol]
                state["retry_at"] = due
                self._event("minute_feed_reference_recheck_request_error", {
                    "fill_id": state["record"]["fill_id"], "reference_at": selected.isoformat(),
                    "retry_at": due.isoformat(), "error_type": type(exc).__name__, "message": str(exc)}, symbol)
            return {"status": "retry", "reason": "reference_recheck_request_error", "retry_after_seconds": 4}
        if set(responses) != set(ready):
            raise ContractError("Reference overlap changed its requested local symbols")
        for symbol in ready:
            response, state = responses[symbol], self.reference_fills[selected, symbol]
            if (response.symbol != symbol or response.source_id != self.source_id
                    or response.source_dataset != self.dataset or instant(response.interval_open) != selected
                    or instant(response.interval_close) != selected + STEP):
                raise ContractError("Reference overlap changed its exact interval/source identity")
            self.journal.save(response)
            self._event("minute_feed_reference_recheck_batch_saved", {
                "fill_id": state["record"]["fill_id"], "reference_at": selected.isoformat(),
                "attempt_id": response.attempt_id}, symbol)
            state["attempt_ids"].add(response.attempt_id)
            self._resolve_recheck(symbol, selected, state, self._recheck_attempts(symbol, selected, state))
        return {"status": "acquired", "kind": "reference_fill_overlap", "reference_at": selected.isoformat(),
                "symbols": ready, "retry_after_seconds": 1}

    def _matches_interval_contract(self, symbol, candle):
        contract = candle.get("native_interval_contract")
        if symbol in self.native_interval_symbols:
            return isinstance(contract, str) and contract == self.native_interval_contract
        # Concatenated legacy frames may have an absent optional tag as NaN.
        return contract is None or isinstance(contract, float) and pd.isna(contract)

    def _merge(self, symbol, candles):
        if not candles:
            return
        if any(not self._matches_interval_contract(symbol, candle) for candle in candles):
            raise ContractError("Saved candle differs from this symbol's frozen interval contract")
        history = self.histories.get(symbol)
        manifest = dict(history.manifest) if history is not None else {"source_dataset": self.dataset}
        if manifest.get("source_dataset") != self.dataset:
            raise ContractError("Saved history differs from the feed's single source")
        original = history.frame if history is not None else pd.DataFrame()
        addition = pd.DataFrame(candles)
        for column in ("opened_at", "available_at"):
            addition[column] = pd.to_datetime(addition[column], utc=True)
        from .native_interval import merge_native_history_frames
        merged = merge_native_history_frames(original, addition)
        manifest.update(source_dataset=self.dataset, current_reference_availability="actual")
        if symbol in self.native_interval_symbols:
            manifest["native_interval_contract"] = self.native_interval_contract
        else:
            manifest.pop("native_interval_contract", None)
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
                    history = self.histories[symbol]
                    eligible = [self._matches_interval_contract(symbol, row)
                                and data.completed_15m_observation(row, from_frame=True)
                                and instant(row["available_at"]) <= now
                                for row in history.frame.to_dict("records")]
                    clean = history.frame.loc[eligible].copy()
                    clean.attrs["intraday_history"] = history.manifest
                    self.histories[symbol] = data.History(clean, history.manifest)
                saved = [c for c in self.store.observed_candles(symbol)
                         if c.get("source_id") == self.source_id
                         and c.get("source_dataset") == self.dataset
                         and self._matches_interval_contract(symbol, c)
                         and data.completed_15m_observation(c)
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
        native = symbol in self.native_interval_symbols
        previous = None if native else self._previous(symbol, reference)
        final = attempts[-1]
        needs_previous = (not native and final.status == "success" and final.covered()
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
            if native:
                from .native_interval import resolve_native_interval
                result = resolve_native_interval(attempts, session=self._session(reference))
            else:
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
        terminal_missing = result.get("status") == "missing" and (
            result.get("reason") in {"no_prior_known_price", "unverified_prior_price_continuity", "unverified_source_no_trade"}
            or native and result.get("reason") == "source_empty_interval")
        if candle is None and not terminal_missing and result.get("status") != "unusable":
            return False
        value = {"reference_at": reference.isoformat(), "status": result["status"],
                 "reason": result.get("reason"), "attempt_ids": [a.attempt_id for a in attempts],
                 "source_dataset": self.dataset, "native_window_saved": True}
        if native:
            value["native_interval_contract"] = self.native_interval_contract
        if candle is not None:
            if (not self._matches_interval_contract(symbol, candle)
                    or candle.get("symbol") != symbol or candle.get("source_id") != self.source_id
                    or candle.get("source_dataset") != self.dataset
                    or instant(candle["opened_at"]) != reference):
                raise ContractError("Resolved candle differs from this symbol's frozen interval contract")
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

    def step(self, *, stopped=lambda: False):
        """At most one exact native-minute batch; never sleep or fit a model."""
        if stopped():
            raise InterruptedError("Minute feed manually stopped")
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
                        opened_at=reference, attempt_id=uuid.uuid4().hex, clock=self.clock, stopped=stopped)
                except InterruptedError:
                    raise
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
            recheck = self._reference_recheck_step(now, stopped)
            if recheck is not None:
                return recheck
        finally:
            lock.release()
        closed_result = None
        with self._lock:
            # Model consumers may record a fill while this independent feed
            # finishes acquisition. Snapshot and close atomically with their
            # writes; no live dict iterator escapes the same lock.
            fill_states = list(self.reference_fills.values())
            due_rechecks = any(state["completed"] is None and instant(state["record"]["recheck_due_at"]) <= now
                              for state in fill_states)
            if now >= self.end + self.offset and self.next_reference >= self.end and not any(self.pending.values()) and not due_rechecks:
                if not self.closed:
                    self.closed = True
                    closed_result = {"status": "closed", "close_at": self.end.isoformat(),
                        "all_queued_native_windows_saved": True, "later_sessions_automatic": False,
                        "pending_reference_rechecks": sum(state["completed"] is None for state in fill_states),
                        "historical_recovery_required": sum(
                            (state["completed"] or {}).get("status") == "historical_recovery_required" for state in fill_states),
                        "pending_rechecks_resume_on_next_manual_session": True}
                    self._event("minute_feed_closed", closed_result)
            closed = self.closed
        if closed_result is not None and self.on_closed:
            self.on_closed(closed_result)
        if closed:
            return {"status": "closed", "retry_after_seconds": 0}
        return {"status": "waiting", "reason": "closed_quarter_or_retry_wait", "retry_after_seconds": 1}

    def run(self, *, stopped, pause):
        while not stopped():
            result = self.step(stopped=stopped)
            if result["status"] == "closed":
                return result
            if stopped():
                break
            pause(min(4, max(.1, result.get("retry_after_seconds", 1))))
        return {"status": "manual_stop", "pending_windows_retained": True}

    def history_source(self, reference):
        return _HistorySource(self, reference)

    def scoring_history_source(self, reference):
        return _HistorySource(self, reference, genuine_only=True)

    def history_snapshot(self):
        with self._lock:
            return dict(self.histories)

    def history_at(self, symbol, reference, *, genuine_only=False):
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
                    # Validated histories already own UTC datetime columns.
                    # Keep the original conversion/error path for every other
                    # representation; do not repeatedly inspect all timestamps.
                    opened = frame.opened_at
                    if not (isinstance(opened, pd.Series) and isinstance(opened.dtype, pd.DatetimeTZDtype)
                            and str(opened.dtype.tz) == "UTC"):
                        opened = pd.to_datetime(opened, utc=True)
                    available = frame.available_at
                    if not (isinstance(available, pd.Series) and isinstance(available.dtype, pd.DatetimeTZDtype)
                            and str(available.dtype.tz) == "UTC"):
                        available = pd.to_datetime(available, utc=True)
                    matching = frame.loc[opened.eq(pd.Timestamp(reference))
                        & available.le(pd.Timestamp(instant(self.clock())))
                        & frame.availability_kind.eq("actual")
                        & frame.source_id.eq(self.source_id) & frame.source_dataset.eq(self.dataset)]
                    if not matching.empty and any(self._matches_interval_contract(symbol, row)
                           and data.completed_15m_observation(row, from_frame=True) and data.synthetic_continuity_verified(row)
                           for row in matching.to_dict("records")):
                        return history
            raise ContractError("History reference lies outside this feed's latched eligible day")
        with self._lock:
            result = self.outcomes.get((reference, symbol))
            if result is None:
                raise data.AcquisitionPending("independent_minute_feed_pending", 1)
            # A later overlap or restored observation supersedes the old empty
            # outcome for new consumers; immutable outcomes/forecasts stay intact.
            if self._exact_genuine(symbol, reference) is not None:
                return self.histories[symbol]
            if result["status"] == "resolved_no_usable_15m":
                if self.reference_fill_policy is not None and result.get("reason") == "source_empty_interval":
                    # Scoring needs genuine targets, while the first missing
                    # target still registers its durable real-data recheck.
                    if not genuine_only or (reference, symbol) not in self.reference_fills:
                        from .forward_fill import make_reference
                        history = self.histories.get(symbol)
                        if history is None:
                            raise ContractError("Forward-filled reference has no genuine source history")
                        candle = make_reference(history.frame, self._attempts(symbol, reference), session=self._session(reference))
                        self._record_reference_fill(symbol, reference, candle)
                    if genuine_only:
                        raise ContractError("Exact target interval unavailable after acquisition")
                    # A projection belongs to this model request, never the source
                    # ledger or shared history. Future real bars retain priority.
                    frame = history.frame.loc[~history.frame.opened_at.eq(pd.Timestamp(reference))].copy()
                    frame = pd.concat([frame, pd.DataFrame([candle])], ignore_index=True)
                    for field in ("opened_at", "available_at"):
                        frame[field] = pd.to_datetime(frame[field], utc=True)
                    manifest = {**deepcopy(history.manifest), "reference_fill_policy": deepcopy(self.reference_fill_policy),
                                "reference_projection": "latest_genuine_close_current_reference_only"}
                    frame.attrs["intraday_history"] = manifest
                    return data.History(frame, manifest)
                raise ContractError("Exact saved native minute window has no usable 15m candle: " + str(result.get("reason")))
            history = self.histories.get(symbol)
            if history is None or not pd.to_datetime(history.frame.opened_at, utc=True).eq(pd.Timestamp(reference)).any():
                raise ContractError("Completed feed candle is missing from its saved history")
            return history
