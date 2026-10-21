"""Explicit manual local 15m execution against only its own ledgers.

Construction performs no broker IO. The caller owns the manual session and
repeatedly invokes one bounded step. Pending recovery returns to that loop so
the next attempt can reobserve its own orders and account capacity.
"""
from contextlib import contextmanager
from dataclasses import asdict, replace
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from urllib.parse import urlparse
import json
import math
import sqlite3
import threading
import time
import uuid

from datafetching.runtime_lock import exclusive_runtime_lock
from ml.account_gameplan.authority import (AccountAuthority, AuthorityBinding, AuthorityError,
    IntradayCapacitySnapshot, IntradayReservationRequest, Fill, OrderEvidence, OPEN, SubmissionNotStarted,
    lease_acquisition_failure_evidence)
from ml.stock_trader.horizon_broker import capture_order_evidence
from ml.stock_trader.horizon_ledger import LedgerError, PortfolioEvidence
from .local_execution import (load_local_binding, IntradayLedger as HorizonLedger, NATIVE_PATH, AUTHORITY_PATH, LOCK_PATH, broker_pending)
from ml.stock_trader.state import capture_portfolio_state
from ml.stock_trader.session import stock_execution_window
from app.services.schwab_stock_orders import build_schwab_stock_order_payload

from .capacity import clip_to_capacity
from .execution_policy import submission_gate
from .intentions import IntentionStore
from .prediction import ContractError, PACIFIC, TARGET_VERSION, digest, instant
from .starter_sizing import QuoteObservation, size_forecast
from .technical_sizing import AvailableInput


_SAFE_SUBMISSION_REASONS = frozenset({
    "Manual 15m session is no longer active",
    "Independent local 15m account binding and symbols required",
    "Account binding changed during execution",
    "Broker account differs from local 15m binding",
    "Newer current-session forecast supersedes this intention",
    "Prepared order context differs from account snapshot",
    "Forecast eligibility changed before submission",
    "Market session changed before submission",
    "Quote needs refresh before submission",
    "CURRENT_FENCED_LEASE_REQUIRED",
    "LEASE_ACQUISITION_EXPIRED_BEFORE_RETURN",
    "AUTHORITY_CLOCK_MOVED_BACKWARDS",
    "LEASE_ALREADY_OWNED",
    "INTRADAY_CAPACITY_SNAPSHOT_REQUIRED",
    "LATEST_INTRADAY_CAPACITY_SNAPSHOT_REQUIRED",
    "INTRADAY_CAPACITY_SNAPSHOT_STALE_OR_FUTURE",
    "ACCOUNT_SNAPSHOT_STALE_OR_FUTURE",
    "INTRADAY_FORECAST_NOT_CURRENT",
    "SUBMISSION_GATE_ALREADY_USED_OR_STATE_CHANGED",
    "SUBMISSION_OUTCOME_REQUIRES_RECONCILIATION",
    "EXACT_BROKER_ORDER_ID_AND_FINAL_GATE_REQUIRED",
})

# Only this call site's marker attributes a later latched/rethrown failure.
# Arbitrary exception attributes cannot supply diagnostic text or provenance.
_AUTHORITY_RENEWAL_FAILURE = object()
_SQLITE_ERROR_CONSTANTS = {
    name: value for name, value in vars(sqlite3).items()
    if name.startswith("SQLITE_") and type(value) is int
}


def _submission_failure_evidence(exc, stage):
    """Keep bounded causes without copying provider messages or request secrets."""
    from collections import deque

    chain, locations, seen = [], [], set()
    current = exc
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        item = {"error_type": type(current).__name__[:80]}
        frames = deque(maxlen=8)
        trace = current.__traceback__
        while trace is not None:
            code = trace.tb_frame.f_code
            frames.append({"file": code.co_filename.replace("\\", "/").rsplit("/", 1)[-1][:120],
                           "function": code.co_name[:120], "line": trace.tb_lineno})
            trace = trace.tb_next
        location = {"chain_index": len(chain), "error_type": item["error_type"], "frames": list(frames)}
        if isinstance(current, OSError):
            for name in ("errno", "winerror"):
                value = getattr(current, name, None)
                if type(value) is int:
                    location[name] = value
        locations.append(location)
        # Local fixed messages are useful reasons. Arbitrary provider/transport
        # messages can contain account URLs, headers or credential material.
        if isinstance(current, (ContractError, AuthorityError)) and str(current) in _SAFE_SUBMISSION_REASONS:
            item["message"] = str(current)
        if getattr(current, "intraday_authority_renewal_failure", None) is _AUTHORITY_RENEWAL_FAILURE:
            item["local_operation"] = "authority.renew_lease"
        acquisition = lease_acquisition_failure_evidence(current)
        if acquisition is not None:
            item["lease_acquisition"] = acquisition
        if isinstance(current, sqlite3.Error):
            code = getattr(current, "sqlite_errorcode", None)
            name = getattr(current, "sqlite_errorname", None)
            if type(code) is int and type(name) is str and _SQLITE_ERROR_CONSTANTS.get(name) == code:
                item.update(sqlite_errorcode=code, sqlite_errorname=name)
        operation = getattr(current, "stock_trader_operation", None)
        if isinstance(operation, str) and operation in {"authentication", "account_identity", "account", "positions", "orders", "quotes"}:
            item["operation"] = operation
        status = getattr(getattr(current, "response", None), "status_code", None)
        if type(status) is int and 100 <= status <= 599:
            item["http_status"] = status
        chain.append(item)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return {"submission_stage": stage, "exception_chain": chain,
            "root_cause": chain[-1], "exception_chain_truncated": current is not None,
            "exception_locations": locations}


class _ExecutionFailure:
    """Only locally constructed diagnostics may cross the retry-event boundary."""
    def __init__(self, evidence, trace):
        self.evidence, self.trace = evidence, trace


def execution_failure_evidence(exc):
    saved = getattr(exc, "intraday_execution_failure", None)
    if isinstance(saved, _ExecutionFailure):
        return saved.evidence
    evidence = _submission_failure_evidence(exc, "unclassified")
    evidence["execution_stage"] = evidence.pop("submission_stage")
    return evidence


class _ExecutionTrace:
    """Bounded monotonic stage timings, without account values or lease tokens."""
    def __init__(self):
        self.started = self.stage_started = time.monotonic()
        self.stage = "account_entry"
        self.phases = []
        self.lease_expires_at = self.snapshot_observed_at = None

    def advance(self, stage):
        now = time.monotonic()
        self.phases.append({"stage": self.stage, "elapsed_seconds": round(max(0, now-self.stage_started), 6)})
        self.phases = self.phases[-32:]
        self.stage, self.stage_started = stage, now

    def snapshot(self):
        """Copy bounded timings for either an exception or a normal result."""
        now = time.monotonic()
        return dict(execution_stage=self.stage,
            execution_elapsed_seconds=round(max(0, now-self.started), 6),
            execution_stage_elapsed_seconds=round(max(0, now-self.stage_started), 6),
            execution_phases=[dict(phase) for phase in self.phases],
            lease_expires_at=self.lease_expires_at, snapshot_observed_at=self.snapshot_observed_at)

    def remember(self, exc):
        saved = getattr(exc, "intraday_execution_failure", None)
        if isinstance(saved, _ExecutionFailure) and saved.trace is self:
            return
        evidence = _submission_failure_evidence(exc, self.stage)
        evidence.pop("submission_stage")
        evidence.update(self.snapshot())
        # A later failure while retaining a submission must not erase the
        # originating clock evidence. Trust only this operation's own marker.
        current = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
        seen = set()
        while current is not None and id(current) not in seen and len(seen) < 8:
            seen.add(id(current))
            prior = getattr(current, "intraday_execution_failure", None)
            if (isinstance(prior, _ExecutionFailure) and prior.trace is self
                    and "submission_final_gate" in prior.evidence):
                evidence["original_submission_failure"] = dict(prior.evidence)
                break
            current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
        exc.intraday_execution_failure = _ExecutionFailure(evidence, self)


class _SubmissionDiagnostics:
    """Best-effort local clocks only; never change a submission decision."""
    def __init__(self):
        self.unavailable = False
        self.stage = "prepare_order_submission"
        self.started = self.stage_started = self._now()
        self.phases = []
        self.quote_at = self.target_at = None
        self.final_gate = dict(clock_reached=False, checked_at=None,
            quote_observed_at=None, quote_age_seconds=None, maximum_quote_age_seconds=None,
            target_close_at=None, seconds_to_target_close=None, snapshot_id=None, snapshot_observed_at=None)

    def _now(self):
        try:
            value = time.monotonic()
            if type(value) in {int, float} and math.isfinite(value):
                return value
        except Exception:
            pass
        self.unavailable = True
        return None

    def _elapsed(self, end, start):
        if end is not None and start is not None:
            elapsed = max(0, end - start)
            if math.isfinite(elapsed):
                return round(elapsed, 6)
        self.unavailable = True
        return None

    def context(self, executor, forecast, quote):
        try:
            # These are the actual execution observation, never frozen sizing.
            self.quote_at, self.target_at = instant(quote.freshness_observed_at), instant(forecast["target_close"])
            self.final_gate.update(quote_observed_at=self.quote_at.isoformat(),
                target_close_at=self.target_at.isoformat(),
                snapshot_observed_at=instant(executor.portfolio.observed_at).isoformat())
            snapshot = executor.snapshot_id
            if type(snapshot) is str and len(snapshot) == 64 and all(c in "0123456789abcdef" for c in snapshot):
                self.final_gate["snapshot_id"] = snapshot
            else:
                self.unavailable = True
            age_limit = executor.ledger.maximum_evidence_age_seconds
            if type(age_limit) in {int, float} and math.isfinite(age_limit):
                self.final_gate["maximum_quote_age_seconds"] = age_limit
            else:
                self.unavailable = True
        except Exception:
            self.unavailable = True

    def advance(self, stage):
        now = self._now()
        self.phases.append(dict(stage=self.stage, elapsed_seconds=self._elapsed(now, self.stage_started)))
        self.phases = self.phases[-32:]
        self.stage, self.stage_started = stage, now

    def evaluated(self, current):
        self.final_gate["clock_reached"] = True
        try:
            self.final_gate["checked_at"] = current.isoformat()
            for name, delta in (("quote_age_seconds", current - self.quote_at),
                                ("seconds_to_target_close", self.target_at - current)):
                value = delta.total_seconds()
                if math.isfinite(value):
                    self.final_gate[name] = value
                else:
                    self.unavailable = True
        except Exception:
            self.unavailable = True

    def snapshot(self):
        now = self._now()
        result = dict(submission_final_gate=dict(self.final_gate), submission_timing=dict(
            stage=self.stage, elapsed_seconds=self._elapsed(now, self.started),
            stage_elapsed_seconds=self._elapsed(now, self.stage_started),
            phases=[dict(phase) for phase in self.phases]))
        if self.unavailable:
            result["submission_diagnostics_unavailable"] = True
        return result


class _LeaseKeeper:
    """Maintain this operation's existing fence; never acquire a replacement."""
    def __init__(self, authority, lease, *, active):
        self.authority, self.lease, self.active = authority, lease, active
        self.failure = None
        self._identity = (lease.token, lease.epoch, lease.holder_id)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._finished = threading.Event()
        self._thread = None

    def _check(self):
        if self.failure is not None:
            raise self.failure
        if self._stop.is_set() or not self.active():
            raise ContractError("Manual 15m session is no longer active")
        return self.lease

    def check(self):
        with self._lock:
            try:
                return self._check()
            except BaseException as exc:
                if self.failure is None:
                    self.failure = exc
                raise

    def renew(self):
        # Main-thread final gates and the worker share one renewal sequence.
        # An older result cannot replace a newer expiry in diagnostics.
        with self._lock:
            try:
                self._check()
                try:
                    renewed = self.authority.renew_lease(self.lease.token)
                except BaseException as exc:
                    exc.intraday_authority_renewal_failure = _AUTHORITY_RENEWAL_FAILURE
                    raise
                if (renewed.token, renewed.epoch, renewed.holder_id) != self._identity:
                    raise AuthorityError("CURRENT_FENCED_LEASE_REQUIRED")
                self.lease = renewed
                return self._check()
            except BaseException as exc:
                if self.failure is None:
                    self.failure = exc
                raise

    def _wait(self, seconds):
        return self._stop.wait(seconds)

    def _run(self):
        try:
            while not self._wait(15):
                self.renew()
        except BaseException as exc:
            with self._lock:
                if self.failure is None:
                    self.failure = exc
        finally:
            self._finished.set()

    def start(self):
        self.check()
        if self._thread is not None:
            raise RuntimeError("Lease keeper already started")
        self._thread = threading.Thread(target=self._run, name="intraday-lease-renewal", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None and self._thread.ident is not None:
            self._thread.join()



def forecast_disposition(forecast, *, now, session_started_at, local_symbols, actor="Atlas"):
    """A current manual session cannot execute a replay or previously saved issue."""
    original = dict(forecast)
    key = original.pop("forecast_id", None)
    if key != digest(original) or forecast["target_version"] != TARGET_VERSION:
        raise ContractError("Immutable exact-target forecast required")
    current = instant(now)
    if forecast["pc"] != actor or forecast["symbol"] not in local_symbols:
        return "outside_local_actor_scope"
    if forecast["mode"] != "live" or forecast["availability_basis"] != "observed":
        return "not_a_current_live_forecast"
    if instant(forecast["issued_at"]) < instant(session_started_at):
        return "forecast_precedes_manual_start"
    if instant(forecast["issued_at"]) > current:
        raise ContractError("Forecast issue lies in the future")
    if forecast["ensemble_status"] != "full" or forecast["successful_count"] != forecast["configured_count"]:
        return "incomplete_ensemble"
    gate = submission_gate(as_of=current, eligible_session=instant(forecast["reference_at"]).astimezone(PACIFIC).date())
    if gate.state != "before_submission_cutoff":
        return gate.state
    if current >= instant(forecast["target_close"]):
        return "fixed_target_already_elapsed"
    return "eligible_for_account_checks"


class IntradayAccountExecutor:
    def __init__(self, *, datastore, store, broker, local_symbols, session_started_at,
                 clock, manual_authority, stopped=lambda: False, actor="Atlas"):
        if actor not in {"Atlas", "Scout"}:
            raise ContractError("Explicit local 15m actor required")
        self.actor, self.stream = actor, actor.lower() + "-15m"
        self.root, self.store, self.broker = Path(datastore), store, broker
        self.symbols = tuple(local_symbols)
        self.session_started_at = instant(session_started_at)
        self.clock, self.manual_authority, self.stopped = clock, manual_authority, stopped
        self.intents = IntentionStore(store)
        self.config = self.ledger = self.authority = self.lease = None
        self.portfolio = self.snapshot_id = None
        self._execution_trace = None
        self._lease_keeper = None

    def _record(self, forecast, disposition, *, kind="execution_disposition", evidence=None, capacity=None, order=None):
        evidence = evidence or {"reason": disposition}
        event_key = digest([kind, disposition, evidence, asdict(order) if order else None,
                            capacity.payload() if capacity else None])
        return self.intents.record(forecast, event_key=event_key, recorded_at=self.clock(),
            disposition=disposition, kind=kind, evidence=evidence, capacity=capacity, order=order)

    def _binding(self):
        if self.stopped() or not self.manual_authority():
            raise ContractError("Manual 15m session is no longer active")
        config = load_local_binding(self.root)
        if config.actor != self.actor or set(self.symbols) != set(config.symbols):
            raise ContractError("Independent local 15m account binding and symbols required")
        if self.config is not None and config.fingerprint != self.config.fingerprint:
            raise ContractError("Account binding changed during execution")
        self.config = config
        return config

    @contextmanager
    def _account(self):
        # This trader serializes only its own orders, capacity and receipts.
        trace = self._execution_trace = _ExecutionTrace()
        try:
            self._execution_stage("execution_lock")
            with exclusive_runtime_lock(self.root / LOCK_PATH, process_name="intraday-execution"):
                self._execution_stage("local_binding")
                config = self._binding()
                self._execution_stage("native_ledger_open")
                self.ledger = HorizonLedger(self.root / NATIVE_PATH, config.account_fingerprint)
                self._execution_stage("authority_open")
                self.authority = AccountAuthority(self.root / AUTHORITY_PATH,
                    AuthorityBinding(config.account_fingerprint, config.coordinator_id, config.participants),
                    clock=lambda: instant(self.clock()))
                self._execution_stage("lease_acquire")
                self.lease = self.authority.acquire_lease(coordinator_id=config.machine_id,
                    holder_id="manual-" + self.stream + ":" + uuid.uuid4().hex)
                trace.lease_expires_at = self.lease.expires_at
                keeper = self._lease_keeper = _LeaseKeeper(self.authority, self.lease,
                    active=lambda: not self.stopped() and self.manual_authority())
                failure = None
                try:
                    keeper.start()
                    self._execution_stage("pre_send_repair")
                    self._repair_pre_send()
                    self._execution_stage("account_work")
                    yield
                except BaseException as exc:
                    failure = exc
                    trace.lease_expires_at = keeper.lease.expires_at
                    trace.remember(exc)
                    raise
                finally:
                    # No renewal may outlive this operation or its local lock.
                    self._execution_stage("lease_keeper_stop")
                    stop_failure = None
                    try:
                        keeper.stop()
                    except BaseException as cleanup:
                        stop_failure = cleanup
                        trace.remember(cleanup)
                        cleanup_evidence = execution_failure_evidence(cleanup)
                        keeper._stop.set()
                        if keeper._thread is not None and keeper._thread.ident is not None:
                            # Even an interrupted join cannot release ownership
                            # while a renewal still has a database write pending.
                            while not keeper._finished.is_set():
                                try:
                                    keeper._finished.wait()
                                except BaseException as interrupted:
                                    if "drain_wait_failure" not in cleanup_evidence:
                                        cleanup_evidence["drain_wait_failure"] = _submission_failure_evidence(interrupted, "lease_keeper_drain")
                            # No authority writes remain after the finished signal.
                            try:
                                keeper._thread.join()
                            except BaseException as interrupted:
                                cleanup_evidence["join_failure"] = _submission_failure_evidence(interrupted, "lease_keeper_join")
                        if failure is not None:
                            execution_failure_evidence(failure)["cleanup_failure"] = cleanup_evidence
                    self.lease = keeper.lease
                    trace.lease_expires_at = self.lease.expires_at
                    self._execution_stage("lease_release")
                    try:
                        self.authority.release_lease(self.lease.token)
                    except AuthorityError:
                        pass
                    except Exception as cleanup:
                        trace.remember(cleanup)
                        if failure is None and stop_failure is None:
                            raise
                        # Cleanup cannot hide the operation's original cause.
                        target = execution_failure_evidence(failure or stop_failure)
                        target["release_cleanup_failure" if "cleanup_failure" in target else "cleanup_failure"] = execution_failure_evidence(cleanup)
                    if failure is None and stop_failure is not None:
                        raise stop_failure
        except BaseException as exc:
            trace.remember(exc)
            raise
        finally:
            self._execution_trace = None
            self._lease_keeper = None

    def _execution_stage(self, stage):
        if self._execution_trace is not None:
            self._execution_trace.advance(stage)

    def _assert_current(self):
        self._binding()
        self._lease_keeper.check()
        if self.broker.stable_account_fingerprint() != self.config.account_fingerprint:
            raise ContractError("Broker account differs from local 15m binding")
        self._renew_current_lease()

    def _renew_current_lease(self):
        # Called after potentially slow preparation, immediately before effects.
        self._binding()
        self.lease = self._lease_keeper.renew()
        if self._execution_trace is not None:
            self._execution_trace.lease_expires_at = self.lease.expires_at

    def _repair_pre_send(self):
        """Repair only provable no-send or exact accepted-ID crash boundaries."""
        records = {r["request"]["native_reservation_id"]:r for r in self.authority.read_state()["reservations"]}
        for native in self.ledger.pending_reservations():
            if self.ledger.reservation_stream(native.reservation_id) != self.stream:
                continue
            record = records.get(native.reservation_id)
            if record is not None and record["broker_order_id"]:
                if native.broker_order_id and native.broker_order_id != record["broker_order_id"]:
                    raise ContractError("Native and account order IDs disagree")
                if not native.broker_order_id:
                    self.ledger.mark_submission(native.reservation_id, status="SUBMITTED", observed_at=self.clock(),
                        evidence_id="intraday-recover-accepted:" + record["reservation_id"],
                        broker_order_id=record["broker_order_id"])
            elif native.status == "RESERVED" and (record is None or record["status"] in {"RESERVED", "NOT_SUBMITTED"}):
                if record and record["status"] == "RESERVED":
                    self.authority.abandon_unsubmitted(self.lease.token, record["reservation_id"])
                self.ledger.mark_submission(native.reservation_id, status="REJECTED", observed_at=self.clock(),
                    evidence_id="intraday-recover-never-sent:" + native.reservation_id)
            # SUBMITTING/UNKNOWN without a reliable ID remains committed.

    @staticmethod
    def _validate_authority_native_identities(records, native_by_id):
        for record in records:
            native = native_by_id.get(record["request"]["native_reservation_id"])
            if native is None:
                raise LedgerError("UNKNOWN_RESERVATION")
            if native.broker_order_id != record["broker_order_id"] or native.allocation_id != record["request"]["native_allocation_id"]:
                raise ContractError("Local authority/native identity mismatch")

    def _reconcile_captured_authority_orders(self, evidence, uncertain, records, native_by_id):
        self._validate_authority_native_identities(records, native_by_id)
        by_native = {item.reservation_id:item for item in evidence}
        for record in records:
            native = native_by_id[record["request"]["native_reservation_id"]]
            observed = by_native.get(native.reservation_id)
            if observed is None and record["status"] in OPEN:
                retained = uncertain.get(native.reservation_id)
                if retained is None or asdict(native) != asdict(retained[0]) or record != retained[1]:
                    raise ContractError("Local commitment lacks complete status evidence")
            if observed is not None and sum(fill.quantity for fill in observed.fills) != observed.cumulative_filled_quantity:
                raise LedgerError("COMPLETE_PER_ORDER_FILL_EVIDENCE_REQUIRED")
        for record in records:
            native = native_by_id[record["request"]["native_reservation_id"]]
            observed = by_native.get(native.reservation_id)
            if observed is None:
                continue
            if record["last_evidence_at"] and instant(observed.observed_at) <= instant(record["last_evidence_at"]):
                if observed.cumulative_filled_quantity != record["filled_quantity"] or observed.status != record["status"]:
                    raise ContractError("Local order evidence did not advance")
                continue
            self.authority.reconcile(self.lease.token, OrderEvidence(observed.evidence_id, record["reservation_id"],
                observed.account_fingerprint, observed.broker_order_id, observed.observed_at, observed.status,
                observed.order_quantity, observed.cumulative_filled_quantity, observed.remaining_quantity,
                tuple(Fill(f.fill_id,f.quantity,f.price,f.executed_at) for f in observed.fills), complete=True,
                fill_time_provenance=observed.fill_time_provenance))

    def _capture(self, required_evidence=()):
        self._execution_stage("capture_lease_current")
        self._assert_current()
        # The local authority may have persisted acceptance before its native
        # row reached terminal state (or vice versa). Recover those exact own
        # identities without consulting any other trader's authority.
        self._execution_stage("own_pending_identity_read")
        pending = {r.reservation_id:r for r in self.ledger.pending_reservations()
                   if self.ledger.reservation_stream(r.reservation_id) == self.stream}
        uncertain = {}
        for record in self.authority.read_state()["reservations"]:
            if record["request"].get("stream") != self.stream:
                raise ContractError("Foreign stream in local 15m authority")
            if record["status"] in OPEN:
                native = self.ledger.lookup_reservation(record["request"]["native_reservation_id"])
                if self.ledger.reservation_stream(native.reservation_id) != self.stream:
                    raise ContractError("Foreign stream in local 15m native ledger")
                if native.broker_order_id != record["broker_order_id"] or native.allocation_id != record["request"]["native_allocation_id"]:
                    raise ContractError("Local order acceptance identities differ")
                if not native.broker_order_id:
                    request = record["request"]
                    with self.ledger._transaction() as db:
                        native_request = json.loads(db.execute(
                            "SELECT request FROM reservations WHERE id=?", (native.reservation_id,)).fetchone()[0])
                    if (record["status"] not in {"SUBMITTING", "UNKNOWN"}
                            or native.status not in {"RESERVED", "UNKNOWN"}
                            or native.filled_quantity or record["filled_quantity"]
                            or type(request["quantity"]) is not int
                            or type(native_request["quantity"]) is not int
                            or Decimal(str(request["limit_price"])) != Decimal(str(native.limit_price))
                            or Decimal(str(request["exposure_price"])) != Decimal(str(native.limit_price))
                            or Decimal(str(native_request["price"])) != Decimal(str(native.limit_price))
                            or request["account_fingerprint"] != self.config.account_fingerprint
                            or request["participant_id"] != self.config.machine_id
                            or request["local_symbols"] != sorted(self.symbols)
                            or request["horizon"] != "15m"
                            or native_request.get("kind") != "intraday-account-intention"
                            or native_request.get("stream") != self.stream
                            or native_request.get("local_symbols") != sorted(self.symbols)
                            or any(native_request[field] != value for field, value in (
                                ("symbol", native.symbol), ("side", native.side),
                                ("quantity", native.quantity), ("forecast", native.forecast_id),
                                ("forecast_sha256", native.forecast_id)))
                            or any(request[field] != value for field, value in (
                                ("symbol", native.symbol), ("side", native.side),
                                ("quantity", native.quantity), ("forecast_id", native.forecast_id),
                                ("idempotency_key", native.idempotency_key),
                                ("forecast_sha256", native_request["forecast_sha256"]),
                                ("forecast_snapshot_sha256", native_request["forecast_snapshot_sha256"]),
                                ("snapshot_id", native_request["snapshot"]),
                                ("reference_at", native.target_start), ("target_close_at", native.target_end)))):
                        raise ContractError("Uncertain local commitment identities differ")
                    # No ID means no history matching, cancellation, release or
                    # resend. Both own ledgers continue withholding this full
                    # commitment while exact-ID orders and other symbols proceed.
                    uncertain[native.reservation_id] = (native, record)
                pending[native.reservation_id] = native
        if any(not native.broker_order_id and identity not in uncertain
               for identity, native in pending.items()):
            raise ContractError("Uncertain native commitment lacks matching local authority")
        class OwnEvidence:
            account_fingerprint = self.config.account_fingerprint
            def pending_reservations(self):
                return tuple(native for native in pending.values() if native.broker_order_id)
        owner = OwnEvidence()
        # Validate the complete own identity graph before obtaining fresh order
        # observations. Local history reads must not age those observations.
        self._execution_stage("capture_identity_snapshot")
        captured_native_by_id = {item.reservation_id:item for item in self.ledger.snapshot().reservations}
        self._execution_stage("capture_authority_identity_read")
        captured_records = self.authority.read_state()["reservations"]
        self._validate_authority_native_identities(captured_records, captured_native_by_id)
        self._execution_stage("order_evidence_capture")
        evidence = capture_order_evidence(self.broker, owner, account_fingerprint=self.config.account_fingerprint,
                                         as_of=self.clock(), observation_clock=self.clock)
        current_by_id = {item.reservation_id:item for item in evidence}
        for prior in required_evidence:
            current = current_by_id.get(prior.reservation_id)
            old_fills = {f.fill_id:asdict(f) for f in prior.fills}
            new_fills = {f.fill_id:asdict(f) for f in current.fills} if current else {}
            if (current is None or not old_fills.keys() <= new_fills.keys()
                    or any(new_fills[key] != value for key,value in old_fills.items())
                    or (prior.status in {"FILLED", "CANCELLED", "REJECTED"} and current.status != prior.status)):
                raise ContractError("New account capture cannot discard observed order evidence")
        # Persist exact accepted-order evidence while it is fresh. Portfolio
        # capture and native reconciliation may take longer than the unchanged
        # authority fence. A later native failure leaves a recoverable exact-ID
        # authority/native asymmetry, never permission to resend an order.
        self._execution_stage("captured_order_authority_reconcile")
        self._binding()
        self._lease_keeper.check()
        self._reconcile_captured_authority_orders(evidence, uncertain, captured_records, captured_native_by_id)
        self._execution_stage("portfolio_capture")
        portfolio = capture_portfolio_state(self.broker, observed_at=self.clock(), parallel=True,
            literal_cash_only=True, use_actual_quote_timestamps=True, symbols=self.config.symbols,
            include_order_identities=True)
        self._execution_stage("capture_lease_refresh")
        self._assert_current()
        self._execution_stage("broker_snapshot_identity")
        self.broker.verify_read_snapshot(portfolio.broker_identity_fingerprint)
        self._execution_stage("native_snapshot_prepare")
        at = instant(self.clock())
        if not 0 <= (at - instant(portfolio.observed_at)).total_seconds() <= self.ledger.maximum_evidence_age_seconds:
            raise ContractError("Account snapshot is not fresh")
        # The existing capture API timestamps request start. Intraday evidence
        # must name actual information readiness, including quote receipt.
        portfolio = replace(portfolio, observed_at=at.isoformat(), source_fingerprint=digest({
            "provider_snapshot":portfolio.source_fingerprint, "request_started_at":portfolio.observed_at,
            "validated_at":at.isoformat()}))
        if self._execution_trace is not None:
            self._execution_trace.snapshot_observed_at = portfolio.observed_at
        snapshot_id = digest(["intraday-account", portfolio.source_fingerprint, at.isoformat()])
        native_evidence = PortfolioEvidence(snapshot_id, self.config.account_fingerprint, portfolio.observed_at,
            portfolio.held_shares, {s:q.ask for s,q in portfolio.quotes.items()},
            {s:portfolio.account_equity for s in self.config.symbols}, portfolio.source_fingerprint)
        self._execution_stage("native_reconcile")
        reconciliation = self.ledger.reconcile(native_evidence, order_evidence=evidence)
        # Validate every retained identity using one native transaction. Reopening
        # the database per historical order can outlive the unchanged 60s fence.
        self._execution_stage("native_identity_snapshot")
        native_by_id = {item.reservation_id:item for item in self.ledger.snapshot().reservations}
        self._execution_stage("authority_identity_read")
        records = self.authority.read_state()["reservations"]
        self._execution_stage("authority_identity_validation")
        self._reconcile_captured_authority_orders(evidence, uncertain, records, native_by_id)
        retained_reasons = {f"{native.symbol}:UNRECONCILED_{native.status}:{identity}"
                           for identity, (native, _) in uncertain.items()}
        if not reconciliation.ready and (not reconciliation.reasons
                or any(reason not in retained_reasons for reason in reconciliation.reasons)):
            raise ContractError("Native account reconciliation: " + ",".join(reconciliation.reasons))
        self._execution_stage("authority_accounted_read")
        accounted = {r["broker_order_id"]:r["filled_quantity"] for r in self.authority.read_state()["reservations"]
                     if r["broker_order_id"] and r["filled_quantity"]}
        self._execution_stage("capacity_publish")
        self._renew_current_lease()
        self.authority.publish_intraday_snapshot(self.lease.token, IntradayCapacitySnapshot(
            snapshot_id, self.config.account_fingerprint, at.astimezone(PACIFIC).date().isoformat(),
            portfolio.observed_at, str(portfolio.available_cash),
            {s:int(Decimal(str(portfolio.held_shares.get(s,0))).to_integral_value(rounding=ROUND_FLOOR)) for s in self.symbols},
            broker_pending(portfolio), accounted, portfolio.source_fingerprint,
            self.config.account_fingerprint, self.config.account_fingerprint, True))
        self.portfolio, self.snapshot_id = portfolio, snapshot_id
        self._execution_stage("capture_complete")
        return evidence

    def _same_forecast(self, forecast):
        matches = self.ledger.intraday_attempts(symbol=forecast["symbol"],forecast_id=forecast["forecast_id"])
        return matches[-1] if matches else None

    def _proven_unsent(self, native):
        if (native.status != "REJECTED" or native.broker_order_id is not None
                or native.filled_quantity or native.submitted_at is not None):
            return False
        matches = [r for r in self.authority.read_state()["reservations"]
                   if r["request"]["native_reservation_id"] == native.reservation_id]
        return not matches or all(r["status"] == "NOT_SUBMITTED" and r["broker_order_id"] is None
                                  and not r["filled_quantity"] for r in matches)

    def _newer_forecasts(self, forecast):
        """Per-symbol publication can supersede a queue item during account IO."""
        return [f["forecast_id"] for f in self.store.execution_forecast_headers(pc=self.actor, symbol=forecast["symbol"])
                if f["symbol"] == forecast["symbol"] and f["mode"] == "live"
                and f["ensemble_status"] == "full"
                and instant(f["issued_at"]) >= self.session_started_at
                and (instant(f["reference_at"]),instant(f["issued_at"])) >
                    (instant(forecast["reference_at"]),instant(forecast["issued_at"]))]

    def _assert_latest(self, forecast):
        if self._newer_forecasts(forecast):
            raise ContractError("Newer current-session forecast supersedes this intention")

    def _owned_native_reservations(self, db):
        """Keep full native validation and classify history in bounded SQL work."""
        reservations = self.ledger._snapshot(db).reservations
        streams = {}
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='intraday_streams'").fetchone():
            streams = dict(self.ledger._read_rows(db,
                "SELECT id,stream FROM intraday_streams WHERE kind=?", ("id", "stream"), ("reservation",)))
        # Match _stream's legacy fallback and retain the snapshot's exact order.
        return tuple(row for row in reservations
                     if streams.get(row.reservation_id, "gameplan") == self.stream)

    def step(self, forecast):
        """One bounded execution/recovery step; the outer session owns retries."""
        initial = forecast_disposition(forecast, now=self.clock(), session_started_at=self.session_started_at,
                                       local_symbols=self.symbols, actor=self.actor)
        if initial in {"outside_local_actor_scope", "not_a_current_live_forecast", "forecast_precedes_manual_start",
                       "incomplete_ensemble", "wrong_session_date"}:
            return self._record(forecast, initial)
        with self._account():
            self._capture()
            superseding = self._newer_forecasts(forecast)
            if superseding:
                return self._record(forecast,"newer_or_same_reference_intention_already_processed",
                                    evidence={"superseding_forecast_ids":superseding})
            existing = self._same_forecast(forecast)
            if existing and not self._proven_unsent(existing):
                return self._record(forecast, "recover_existing_intention", order=existing,
                    evidence={"native_reservation_id":existing.reservation_id, "status":existing.status})
            self._execution_stage("newer_native_order_scan")
            # Preserve all native/provenance checks in one transaction, as in
            # recover(); historical terminal orders must not age a fresh quote.
            with self.ledger._transaction() as db:
                newer = [r for r in self._owned_native_reservations(db)
                         if r.forecast_id != forecast["forecast_id"]
                         and r.symbol == forecast["symbol"] and r.target_start is not None
                         and instant(r.target_start) >= instant(forecast["reference_at"])]
            self._execution_stage("newer_intention_scan")
            processed = self.intents.processed_at_or_after(forecast)
            if newer or processed:
                return self._record(forecast, "newer_or_same_reference_intention_already_processed",
                    evidence={"native_reservation_ids":[r.reservation_id for r in newer],
                              "processed_forecast_ids":processed})
            self._execution_stage("sizing")
            original = self.intents.get(forecast["forecast_id"])
            unavailable = None
            if original is None:
                frozen = self.store.inputs(forecast["cycle_id"], forecast["symbol"])
                if frozen is None or frozen[0] != forecast["snapshot_hash"]:
                    raise ContractError("Original forecasting inputs unavailable")
            # Historical scans and frozen input decoding can consume an actual
            # observation's lifetime. Refresh once after that work, retaining
            # the real provider clocks and the original desired size, if any.
            at = instant(self.clock())
            quote = self.portfolio.quotes[forecast["symbol"]]
            ages = ((at - instant(self.portfolio.observed_at)).total_seconds(),
                    (at - instant(quote.freshness_observed_at)).total_seconds())
            if (all(age >= 0 for age in ages)
                    and max(ages) > self.ledger.maximum_evidence_age_seconds
                    and forecast_disposition(forecast, now=at, session_started_at=self.session_started_at,
                        local_symbols=self.symbols, actor=self.actor) == "eligible_for_account_checks"):
                self._execution_stage("sizing_evidence_refresh")
                self._capture()
                self._execution_stage("sizing")
            if original is None:
                quote = self.portfolio.quotes[forecast["symbol"]]
                result = size_forecast(forecast, forecast_snapshot=frozen[1],
                    account_equity=AvailableInput(self.portfolio.account_equity, self.portfolio.observed_at,
                        "schwab-liquidation-equity", {"snapshot_id":self.snapshot_id}),
                    quote=QuoteObservation(quote.bid, quote.ask, quote.freshness_observed_at,
                        self.portfolio.observed_at, "schwab-current-quote", {"snapshot_id":self.snapshot_id, "quote":asdict(quote)}),
                    calculated_at=self.clock(), maximum_quote_age_seconds=self.ledger.maximum_evidence_age_seconds)
                original = self.intents.save_sizing(forecast, result)
                if result.status != "sized":
                    unavailable = original
                    # Unavailable sizing is a normal result, so exception
                    # diagnostics never retain its slow phases. Keep a separate
                    # observation without changing the immutable sizing entry.
                    self.store.event(forecast["cycle_id"], "sizing_unavailable_diagnostics",
                        {"forecast_id":forecast["forecast_id"], "sizing_id":result.result_id,
                         "disposition":original["disposition"],
                         "diagnostics":self._execution_trace.snapshot()},
                        symbol=forecast["symbol"], recorded_at=self.clock())
            # Supersession is independent of whether the new intention can trade.
            # A zero desired or elapsed forecast still retires an old intention.
            self._execution_stage("execution_after_sizing")
            from .cancellation import CancellationService
            pending = [r for r in self.ledger.pending_reservations()
                       if self.ledger.reservation_stream(r.reservation_id) == self.stream
                       and r.symbol == forecast["symbol"] and r.forecast_id != forecast["forecast_id"]]
            if pending:
                def before_cancel(context, reservation):
                    self._assert_current()
                    self._assert_latest(forecast)
                    self.broker.verify_read_snapshot(context.identity_fingerprint)
                    self._renew_current_lease()
                service = CancellationService(broker=self.broker, ledger=self.ledger,
                    account_fingerprint=self.config.account_fingerprint, before_cancel=before_cancel,
                    apply_evidence=lambda evidence:self._capture((evidence,)),
                    record_event=lambda kind,value:self.store.event(forecast["cycle_id"],kind,value,symbol=forecast["symbol"]),
                    clock=self.clock, stopped=self.stopped, stream=self.stream)
                # A failed DELETE's durable phase survives a session restart.
                # An acknowledged DELETE is observed until terminal evidence.
                retry_send = False
                with self.store.connect() as db:
                    rows = db.execute("SELECT kind,payload FROM events WHERE kind IN ('cancellation_temporary_failure','cancellation_acknowledged') ORDER BY id").fetchall()
                for kind, raw in rows:
                    item = json.loads(raw)
                    if item.get("reservation_id") == pending[0].reservation_id:
                        if kind == "cancellation_acknowledged":
                            retry_send = False
                        elif item.get("stage") == "delete_exact_order":
                            retry_send = True
                outcome = service.step(pending[0].reservation_id, reason="new-forecast",
                    evidence_id="intraday-replace:" + forecast["forecast_id"] + ":" + pending[0].reservation_id,
                    trigger_forecast_id=forecast["forecast_id"], retry_send=retry_send)
                return self._record(forecast, "previous_intention_reconciliation", evidence=asdict(outcome))
            if unavailable is not None:
                return unavailable
            disposition = forecast_disposition(forecast, now=self.clock(), session_started_at=self.session_started_at,
                                               local_symbols=self.symbols, actor=self.actor)
            if disposition != "eligible_for_account_checks":
                return self._record(forecast, disposition)
            window = stock_execution_window(self.clock())
            if not window.executable:
                return self._record(forecast, "market_session_closed", evidence={"reason":window.reason})
            # Sizing and its durable write can outlive the preparation capture.
            # Retain that first desired size, but use an actual current account
            # observation for capacity and price. This boundary refreshes once;
            # submission still enforces the unchanged final freshness checks.
            at = instant(self.clock())
            quote = self.portfolio.quotes[forecast["symbol"]]
            ages = ((at - instant(self.portfolio.observed_at)).total_seconds(),
                    (at - instant(quote.freshness_observed_at)).total_seconds())
            if all(age >= 0 for age in ages) and max(ages) > self.ledger.maximum_evidence_age_seconds:
                self._execution_stage("capacity_evidence_refresh")
                self._capture()
                self._execution_stage("execution_after_sizing")
                disposition = forecast_disposition(forecast, now=self.clock(), session_started_at=self.session_started_at,
                                                   local_symbols=self.symbols, actor=self.actor)
                if disposition != "eligible_for_account_checks":
                    return self._record(forecast, disposition)
                window = stock_execution_window(self.clock())
                if not window.executable:
                    return self._record(forecast, "market_session_closed", evidence={"reason":window.reason})
            quote = self.portfolio.quotes[forecast["symbol"]]
            side = original["side"]
            price = Decimal(str(quote.ask if side == "BUY" else quote.bid)).quantize(Decimal(".01"),
                rounding=ROUND_CEILING if side == "BUY" else ROUND_FLOOR)
            available = self.authority.available_intraday_capacity(self.lease.token, self.snapshot_id)
            capacity = clip_to_capacity(symbol=forecast["symbol"], side=side,
                desired_quantity=original["desired_quantity"], limit_price=str(price),
                available_cash=available["cash_available"], available_shares=available["eligible_shares"][forecast["symbol"]],
                capacity_evidence_id=self.snapshot_id, observed_at=self.portfolio.observed_at, calculated_at=self.clock())
            self._record(forecast, capacity.disposition, kind="capacity", capacity=capacity,
                         evidence={"snapshot_id":self.snapshot_id})
            if not capacity.adjusted_quantity:
                return self._record(forecast, capacity.disposition, capacity=capacity,
                                     evidence={"snapshot_id":self.snapshot_id})
            return self._submit(forecast, capacity, quote, window)

    def recover(self):
        """Reconcile tracked orders even after cutoff; never submit from history."""
        with self._account():
            self._capture()
            # Retain the full snapshot validation and stream fallback while
            # avoiding per-order SQLite calls during model preparation.
            self._execution_stage("recovery_native_read")
            with self.ledger._transaction() as db:
                owned = self._owned_native_reservations(db)
            if not owned:
                return []
            results = []
            self._execution_stage("recovery_intention_record")
            for native in owned:
                uncertain = not native.broker_order_id and native.status in {"RESERVED", "UNKNOWN"}
                disposition = "unresolved_submission_retained" if uncertain else "order_status_reconciled"
                evidence = {"native_reservation_id":native.reservation_id, "status":native.status,
                            **({"commitment_retained":True, "complete_broker_order_evidence":False} if uncertain else {})}
                kind = "execution_disposition"
                # Read each original inside its intention transaction. The
                # validated scalar cache retains all immutable/index checks
                # without bulk-loading or repeatedly decoding model trees.
                event_key = digest([kind, disposition, evidence, asdict(native), None])
                results.append(self.intents.record_recovery(native.forecast_id, actor=self.actor,
                    event_key=event_key, recorded_at=self.clock(), disposition=disposition,
                    kind=kind, evidence=evidence, order=native))
            self._execution_stage("recovery_complete")
            return results

    def _submit(self, forecast, capacity, quote, window):
        key = self.stream + ":" + forecast["forecast_id"]
        previous = self._same_forecast(forecast)
        if previous:
            if not self._proven_unsent(previous):
                raise ContractError("Retry requires exact proof the previous attempt was never submitted")
            key += ":retry:" + previous.reservation_id
        attempts = self.ledger.intraday_attempts(symbol=forecast["symbol"],forecast_id=forecast["forecast_id"])
        account_records = {r["request"]["native_reservation_id"]:r for r in self.authority.read_state()["reservations"]}
        admitted = [r for r in attempts if r.reservation_id in account_records]
        account_previous = admitted[-1].reservation_id if admitted else None
        native = account = None
        detail = _SubmissionDiagnostics()
        detail.context(self, forecast, quote)
        stage = "prepare_order_submission"
        frozen = None
        def advance(stage_name):
            nonlocal stage
            stage = stage_name
            detail.advance(stage_name)
        def failure_clocks():
            nonlocal frozen
            if frozen is None:
                frozen = detail.snapshot()
            return frozen
        try:
            context = self.broker.prepare_order_submission()
            advance("reserve_native")
            native = self.ledger.reserve_intraday(symbol=forecast["symbol"], forecast_id=forecast["forecast_id"],
                side=capacity.side, quantity=capacity.adjusted_quantity, limit_price=capacity.limit_price,
                snapshot_id=self.snapshot_id, idempotency_key=key, batch_id=forecast["cycle_id"], as_of=self.clock(),
                reference_at=forecast["reference_at"], target_close_at=forecast["target_close"],
                forecast_sha256=forecast["forecast_id"], forecast_snapshot_sha256=forecast["snapshot_hash"],
                local_symbols=self.symbols, pending_sell_shares=int(self.portfolio.pending_sell_shares.get(forecast["symbol"],0)),
                broker_pending_sell_orders={str(row["order_id"]):int(Decimal(str(row["remaining_quantity"])))
                    for row in self.portfolio.broker_working_orders
                    if row["symbol"] == forecast["symbol"] and row["instruction"] == "SELL"},
                retry_of_reservation_id=previous.reservation_id if previous else None, stream=self.stream)
            advance("reserve_account")
            account = self.authority.reserve_intraday(self.lease.token, IntradayReservationRequest(
                key, self.snapshot_id, self.config.account_fingerprint, instant(self.clock()).astimezone(PACIFIC).date().isoformat(),
                self.config.machine_id, forecast["symbol"], forecast["forecast_id"], capacity.side,
                capacity.adjusted_quantity, capacity.limit_price, capacity.limit_price,
                native.reservation_id, native.allocation_id, forecast["forecast_id"], forecast["snapshot_hash"],
                forecast["issued_at"], forecast["reference_at"], forecast["target_close"], TARGET_VERSION, self.symbols,
                retry_of_native_reservation_id=account_previous, stream=self.stream))
            advance("build_order_payload")
            payload = build_schwab_stock_order_payload(symbol=forecast["symbol"], instruction=capacity.side,
                order_type="LIMIT", time_in_force=window.time_in_force, position_effect="AUTO",
                quantity=capacity.adjusted_quantity, price=capacity.limit_price)
            def send(authority_gate):
                def gate():
                    try:
                        advance("current_manual_session_and_account")
                        self._assert_current()
                        advance("latest_forecast")
                        self._assert_latest(forecast)
                        advance("broker_snapshot_identity")
                        self.broker.verify_read_snapshot(self.portfolio.broker_identity_fingerprint)
                        advance("prepared_context_identity")
                        if context.identity_fingerprint != self.portfolio.broker_identity_fingerprint:
                            raise ContractError("Prepared order context differs from account snapshot")
                        advance("current_manual_session_and_account")
                        self._renew_current_lease()
                        current = instant(self.clock())
                        detail.evaluated(current)
                        advance("forecast_eligibility")
                        if forecast_disposition(forecast, now=current, session_started_at=self.session_started_at,
                                                local_symbols=self.symbols, actor=self.actor) != "eligible_for_account_checks":
                            raise ContractError("Forecast eligibility changed before submission")
                        advance("market_session")
                        if stock_execution_window(current).time_in_force != window.time_in_force:
                            raise ContractError("Market session changed before submission")
                        advance("quote_freshness")
                        if not 0 <= (current-instant(quote.freshness_observed_at)).total_seconds() <= self.ledger.maximum_evidence_age_seconds:
                            raise ContractError("Quote needs refresh before submission")
                        advance("authority_submission_gate")
                        authority_gate()
                        advance("broker_submission")
                    except Exception as exc:
                        failure_clocks()
                        raise SubmissionNotStarted(str(exc)) from exc
                try:
                    location = self.broker.submit_prepared_order(payload, context, before_post=gate)
                except BaseException:
                    # The authority records no-send/unknown receipts before
                    # rethrowing. Their latency is not the failed broker stage.
                    failure_clocks()
                    raise
                return urlparse(str(location or "")).path.rstrip("/").split("/")[-1]
            advance("authority_submit")
            accepted = self.authority.submit(self.lease.token, account.reservation_id, send)
            advance("persist_native_submission")
            self.ledger.mark_submission(native.reservation_id, status="SUBMITTED", observed_at=self.clock(),
                evidence_id=key + ":submitted", broker_order_id=accepted.broker_order_id)
            return self._record(forecast, "submitted", order=self.ledger.lookup_reservation(native.reservation_id),
                                evidence={"account_reservation_id":account.reservation_id})
        except Exception as exc:
            # Freeze the originating clocks before receipt reads or cleanup.
            diagnostics = {**_submission_failure_evidence(exc, stage), **failure_clocks()}
            if self._execution_trace is not None:
                self._execution_trace.remember(exc)
                execution_failure_evidence(exc).update(diagnostics)
            if native is not None:
                state = self.authority.reservation(account.reservation_id) if account else None
                receipt = self.authority.submission_receipt(account.reservation_id) if account else None
                if receipt and receipt["outcome"] == "NOT_SUBMITTED" and state.status in {"SUBMITTING", "UNKNOWN"}:
                    # The audited pre-POST gate never ran. Its lease may have
                    # expired while reading identity, so leave both commitments
                    # until the next fresh fence promotes this durable proof.
                    # Native remains RESERVED; do not manufacture uncertainty or
                    # a submission timestamp for a positively unsent attempt.
                    from .cancellation import retry_delay
                    delay=max(retry_delay(exc,self.clock()),retry_delay(exc.__cause__,self.clock()) if exc.__cause__ else 3)
                    return self._record(forecast,"not_submitted",order=self.ledger.lookup_reservation(native.reservation_id),
                        evidence={**diagnostics,"error_type":type(exc).__name__,"proven_unsent_retry_eligible":True,
                                  "awaiting_fresh_authority_fence":True,"retry_after_seconds":delay,
                                  "submission_receipt":receipt})
                unsent = state is None or state.status in {"RESERVED", "NOT_SUBMITTED"}
                if state and state.status == "RESERVED":
                    try:
                        self.authority.abandon_unsubmitted(self.lease.token, state.reservation_id)
                    except Exception as cleanup:
                        # Keep both commitments for the next legitimate fence.
                        # Cleanup cannot replace the originating no-send cause.
                        diagnostics["cleanup_failure"] = _submission_failure_evidence(cleanup, "abandon_unsubmitted")
                        from .cancellation import retry_delay
                        return self._record(forecast, "not_submitted", order=self.ledger.lookup_reservation(native.reservation_id),
                            evidence={**diagnostics, "error_type":type(exc).__name__,
                                      "proven_unsent_retry_eligible":True, "awaiting_fresh_authority_fence":True,
                                      "retry_after_seconds":retry_delay(exc,self.clock())})
                if state and state.broker_order_id:
                    # Preserve the exact accepted identity even if native write
                    # failed. A later step repairs the local binding, never POSTs.
                    status, broker_id = "SUBMITTED", state.broker_order_id
                else:
                    status, broker_id = ("REJECTED" if unsent else "UNKNOWN"), None
                self.ledger.mark_submission(native.reservation_id, status=status, observed_at=self.clock(),
                    evidence_id=key + ":submission-error:" + status, broker_order_id=broker_id)
                from .cancellation import retry_delay
                delay = max(retry_delay(exc,self.clock()),retry_delay(exc.__cause__,self.clock()) if exc.__cause__ else 3)
                return self._record(forecast, "not_submitted" if unsent else "submission_recovery_required",
                    order=self.ledger.lookup_reservation(native.reservation_id), evidence={**diagnostics,"error_type":type(exc).__name__,
                        "proven_unsent_retry_eligible":unsent,"retry_after_seconds":delay})
            raise
        except BaseException as exc:
            # Manual interruption must still propagate, including an interrupted
            # POST whose acceptance is unknown. Keep its bounded original cause
            # without projecting RESERVED as unsent or inventing an order ID.
            try:
                self.store.event(forecast["cycle_id"], "submission_interrupted", {
                    "forecast_id":forecast["forecast_id"],
                    "native_reservation_id":native.reservation_id if native else None,
                    "account_reservation_id":account.reservation_id if account else None,
                    "diagnostics":{**_submission_failure_evidence(exc, stage), **failure_clocks()},
                    "acceptance_requires_exact_evidence":True,
                }, symbol=forecast["symbol"])
            except Exception:
                # An unavailable diagnostic sink cannot consume a manual stop.
                pass
            raise
