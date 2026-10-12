"""Explicit manual Atlas execution against the existing shared account ledgers.

Construction performs no broker IO. The caller owns the manual session and
repeatedly invokes one bounded step. Pending recovery returns to that loop so
Gameplan can use its existing cycle lock between attempts.
"""
from contextlib import contextmanager
from dataclasses import asdict, replace
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from urllib.parse import urlparse
import json
import uuid

from datafetching.runtime_lock import exclusive_runtime_lock
from ml.account_gameplan.authority import (AccountAuthority, AuthorityBinding, AuthorityError,
    IntradayCapacitySnapshot, IntradayReservationRequest, Fill, OrderEvidence, OPEN, SubmissionNotStarted)
from ml.account_gameplan.config import load_account_config, verify_cutover
from ml.account_gameplan.execution import AUTHORITY_PATH, _broker_pending, evidence_ledger
from ml.stock_trader.horizon_broker import capture_order_evidence
from ml.stock_trader.horizon_ledger import HorizonLedger, PortfolioEvidence
from ml.stock_trader.state import capture_portfolio_state
from ml.stock_trader.session import stock_execution_window
from app.services.schwab_stock_orders import build_schwab_stock_order_payload

from .capacity import clip_to_capacity
from .execution_policy import submission_gate
from .intentions import IntentionStore
from .prediction import ContractError, PACIFIC, TARGET_VERSION, digest, instant
from .starter_sizing import QuoteObservation, size_forecast
from .technical_sizing import AvailableInput

NATIVE_PATH = Path("state/independent-stock-trader/holdings.sqlite3")


def forecast_disposition(forecast, *, now, session_started_at, local_symbols):
    """A current manual session cannot execute a replay or previously saved issue."""
    original = dict(forecast)
    key = original.pop("forecast_id", None)
    if key != digest(original) or forecast["target_version"] != TARGET_VERSION:
        raise ContractError("Immutable exact-target forecast required")
    current = instant(now)
    if forecast["pc"] != "Atlas" or forecast["symbol"] not in local_symbols:
        return "outside_local_atlas_scope"
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
                 clock, manual_authority, compatibility, stopped=lambda: False):
        self.root, self.store, self.broker = Path(datastore), store, broker
        self.symbols = tuple(local_symbols)
        self.session_started_at = instant(session_started_at)
        self.clock, self.manual_authority, self.compatibility, self.stopped = clock, manual_authority, compatibility, stopped
        self.intents = IntentionStore(store)
        self.config = self.ledger = self.authority = self.lease = None
        self.portfolio = self.snapshot_id = None

    def _record(self, forecast, disposition, *, kind="execution_disposition", evidence=None, capacity=None, order=None):
        evidence = evidence or {"reason": disposition}
        event_key = digest([kind, disposition, evidence, asdict(order) if order else None,
                            capacity.payload() if capacity else None])
        return self.intents.record(forecast, event_key=event_key, recorded_at=self.clock(),
            disposition=disposition, kind=kind, evidence=evidence, capacity=capacity, order=order)

    def _binding(self):
        if self.stopped() or not self.manual_authority():
            raise ContractError("Manual 15m session is no longer active")
        config = load_account_config(self.root)
        if (config is None or config.machine_id != "pc-original" or config.role != "coordinator"
                or config.activation["status"] != "ACTIVE"
                or set(self.symbols) != set(config.participants["pc-original"])):
            raise ContractError("Existing Atlas account binding and local symbols required")
        verify_cutover(self.root, config)
        if self.config is not None and config.fingerprint != self.config.fingerprint:
            raise ContractError("Account binding changed during execution")
        proof = self.compatibility()
        if not proof["ready"]:
            raise ContractError("Loaded Gameplan compatibility: " + ",".join(proof["reason_codes"]))
        self.config = config
        return config

    @contextmanager
    def _account(self):
        # Same lock as Gameplan: account reads, native mutations and the final
        # send are serialized. Model training never holds this lock.
        with exclusive_runtime_lock(self.root / "locks/stock-trader-hourly.lock", process_name="stock-trader-hourly"):
            config = self._binding()
            self.ledger = HorizonLedger(self.root / NATIVE_PATH, config.account_fingerprint)
            self.authority = AccountAuthority(self.root / AUTHORITY_PATH,
                AuthorityBinding(config.account_fingerprint, config.coordinator_id, config.participants),
                clock=lambda: instant(self.clock()))
            self.lease = self.authority.acquire_lease(coordinator_id=config.machine_id,
                holder_id="manual-atlas-15m:" + uuid.uuid4().hex)
            try:
                self._repair_pre_send()
                yield
            finally:
                try:
                    self.authority.release_lease(self.lease.token)
                except AuthorityError:
                    pass

    def _assert_current(self):
        self._binding()
        if self.broker.stable_account_fingerprint() != self.config.account_fingerprint:
            raise ContractError("Broker account differs from reviewed Atlas account")
        self.lease = self.authority.renew_lease(self.lease.token)

    def _repair_pre_send(self):
        """Repair only provable no-send or exact accepted-ID crash boundaries."""
        records = {r["request"]["native_reservation_id"]:r for r in self.authority.read_state()["reservations"]}
        for native in self.ledger.pending_reservations():
            if self.ledger.reservation_stream(native.reservation_id) != "atlas-15m":
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

    def _capture(self, required_evidence=()):
        self._assert_current()
        owner = evidence_ledger(self.root, self.ledger, self.config)
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
        portfolio = capture_portfolio_state(self.broker, observed_at=self.clock(), parallel=True,
            literal_cash_only=True, use_actual_quote_timestamps=True, symbols=self.config.symbols,
            include_order_identities=True)
        self._assert_current()
        self.broker.verify_read_snapshot(portfolio.broker_identity_fingerprint)
        at = instant(self.clock())
        if not 0 <= (at - instant(portfolio.observed_at)).total_seconds() <= self.ledger.maximum_evidence_age_seconds:
            raise ContractError("Account snapshot is not fresh")
        # The existing capture API timestamps request start. Intraday evidence
        # must name actual information readiness, including quote receipt.
        portfolio = replace(portfolio, observed_at=at.isoformat(), source_fingerprint=digest({
            "provider_snapshot":portfolio.source_fingerprint, "request_started_at":portfolio.observed_at,
            "validated_at":at.isoformat()}))
        snapshot_id = digest(["intraday-account", portfolio.source_fingerprint, at.isoformat()])
        native_evidence = PortfolioEvidence(snapshot_id, self.config.account_fingerprint, portfolio.observed_at,
            portfolio.held_shares, {s:q.ask for s,q in portfolio.quotes.items()},
            {s:portfolio.account_equity for s in self.config.symbols}, portfolio.source_fingerprint)
        reconciliation = self.ledger.reconcile(native_evidence, order_evidence=evidence)
        by_native = {item.reservation_id:item for item in evidence}
        for record in self.authority.read_state()["reservations"]:
            native = self.ledger.lookup_reservation(record["request"]["native_reservation_id"])
            if native.broker_order_id != record["broker_order_id"] or native.allocation_id != record["request"]["native_allocation_id"]:
                raise ContractError("Shared account/native identity mismatch")
            observed = by_native.get(native.reservation_id)
            if observed is None:
                if record["status"] in OPEN:
                    raise ContractError("Shared commitment lacks complete status evidence")
                continue
            if record["last_evidence_at"] and instant(observed.observed_at) <= instant(record["last_evidence_at"]):
                if observed.cumulative_filled_quantity != record["filled_quantity"] or observed.status != record["status"]:
                    raise ContractError("Shared order evidence did not advance")
                continue
            self.authority.reconcile(self.lease.token, OrderEvidence(observed.evidence_id, record["reservation_id"],
                observed.account_fingerprint, observed.broker_order_id, observed.observed_at, observed.status,
                observed.order_quantity, observed.cumulative_filled_quantity, observed.remaining_quantity,
                tuple(Fill(f.fill_id,f.quantity,f.price,f.executed_at) for f in observed.fills), complete=True))
        if not reconciliation.ready:
            raise ContractError("Native account reconciliation: " + ",".join(reconciliation.reasons))
        accounted = {r["broker_order_id"]:r["filled_quantity"] for r in self.authority.read_state()["reservations"]
                     if r["broker_order_id"] and r["filled_quantity"]}
        self.authority.publish_intraday_snapshot(self.lease.token, IntradayCapacitySnapshot(
            snapshot_id, self.config.account_fingerprint, at.astimezone(PACIFIC).date().isoformat(),
            portfolio.observed_at, str(portfolio.available_cash),
            {s:int(Decimal(str(q)).to_integral_value(rounding=ROUND_FLOOR)) for s,q in portfolio.held_shares.items()},
            _broker_pending(portfolio), accounted, portfolio.source_fingerprint,
            self.config.account_fingerprint, self.config.account_fingerprint, True))
        self.portfolio, self.snapshot_id = portfolio, snapshot_id
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
        return [f["forecast_id"] for f in self.store.forecasts(pc="Atlas")
                if f["symbol"] == forecast["symbol"] and f["mode"] == "live"
                and f["ensemble_status"] == "full"
                and instant(f["issued_at"]) >= self.session_started_at
                and (instant(f["reference_at"]),instant(f["issued_at"])) >
                    (instant(forecast["reference_at"]),instant(forecast["issued_at"]))]

    def _assert_latest(self, forecast):
        if self._newer_forecasts(forecast):
            raise ContractError("Newer current-session forecast supersedes this intention")

    def step(self, forecast):
        """One bounded execution/recovery step; the outer session owns retries."""
        initial = forecast_disposition(forecast, now=self.clock(), session_started_at=self.session_started_at,
                                       local_symbols=self.symbols)
        if initial in {"outside_local_atlas_scope", "not_a_current_live_forecast", "forecast_precedes_manual_start",
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
            newer = [r for r in self.ledger.snapshot().reservations
                     if self.ledger.reservation_stream(r.reservation_id) == "atlas-15m"
                     and r.forecast_id != forecast["forecast_id"]
                     and r.symbol == forecast["symbol"] and r.target_start is not None
                     and instant(r.target_start) >= instant(forecast["reference_at"])]
            processed = self.intents.processed_at_or_after(forecast)
            if newer or processed:
                return self._record(forecast, "newer_or_same_reference_intention_already_processed",
                    evidence={"native_reservation_ids":[r.reservation_id for r in newer],
                              "processed_forecast_ids":processed})
            original = self.intents.get(forecast["forecast_id"])
            unavailable = None
            if original is None:
                frozen = self.store.inputs(forecast["cycle_id"], forecast["symbol"])
                if frozen is None or frozen[0] != forecast["snapshot_hash"]:
                    raise ContractError("Original forecasting inputs unavailable")
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
            # Supersession is independent of whether the new intention can trade.
            # A zero desired or elapsed forecast still retires an old intention.
            from .cancellation import CancellationService
            pending = [r for r in self.ledger.pending_reservations()
                       if self.ledger.reservation_stream(r.reservation_id) == "atlas-15m"
                       and r.symbol == forecast["symbol"] and r.forecast_id != forecast["forecast_id"]]
            if pending:
                def before_cancel(context, reservation):
                    self._assert_current()
                    self._assert_latest(forecast)
                    self.broker.verify_read_snapshot(context.identity_fingerprint)
                service = CancellationService(broker=self.broker, ledger=self.ledger,
                    account_fingerprint=self.config.account_fingerprint, before_cancel=before_cancel,
                    apply_evidence=lambda evidence:self._capture((evidence,)),
                    record_event=lambda kind,value:self.store.event(forecast["cycle_id"],kind,value,symbol=forecast["symbol"]),
                    clock=self.clock, stopped=self.stopped)
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
                                               local_symbols=self.symbols)
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
            forecasts = {f["forecast_id"]:f for f in self.store.forecasts(pc="Atlas")}
            results = []
            for native in self.ledger.snapshot().reservations:
                if self.ledger.reservation_stream(native.reservation_id) != "atlas-15m":
                    continue
                forecast = forecasts.get(native.forecast_id)
                if forecast is None or self.intents.get(native.forecast_id) is None:
                    raise ContractError("Native 15m order lacks original persisted intention")
                results.append(self._record(forecast, "order_status_reconciled", order=native,
                    evidence={"native_reservation_id":native.reservation_id, "status":native.status}))
            return results

    def _submit(self, forecast, capacity, quote, window):
        key = "atlas-15m:" + forecast["forecast_id"]
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
        context = self.broker.prepare_order_submission()
        try:
            native = self.ledger.reserve_intraday(symbol=forecast["symbol"], forecast_id=forecast["forecast_id"],
                side=capacity.side, quantity=capacity.adjusted_quantity, limit_price=capacity.limit_price,
                snapshot_id=self.snapshot_id, idempotency_key=key, batch_id=forecast["cycle_id"], as_of=self.clock(),
                reference_at=forecast["reference_at"], target_close_at=forecast["target_close"],
                forecast_sha256=forecast["forecast_id"], forecast_snapshot_sha256=forecast["snapshot_hash"],
                local_symbols=self.symbols, pending_sell_shares=int(self.portfolio.pending_sell_shares.get(forecast["symbol"],0)),
                broker_pending_sell_orders={str(row["order_id"]):int(Decimal(str(row["remaining_quantity"])))
                    for row in self.portfolio.broker_working_orders
                    if row["symbol"] == forecast["symbol"] and row["instruction"] == "SELL"},
                retry_of_reservation_id=previous.reservation_id if previous else None)
            account = self.authority.reserve_intraday(self.lease.token, IntradayReservationRequest(
                key, self.snapshot_id, self.config.account_fingerprint, instant(self.clock()).astimezone(PACIFIC).date().isoformat(),
                self.config.machine_id, forecast["symbol"], forecast["forecast_id"], capacity.side,
                capacity.adjusted_quantity, capacity.limit_price, capacity.limit_price,
                native.reservation_id, native.allocation_id, forecast["forecast_id"], forecast["snapshot_hash"],
                forecast["issued_at"], forecast["reference_at"], forecast["target_close"], TARGET_VERSION, self.symbols,
                retry_of_native_reservation_id=account_previous))
            payload = build_schwab_stock_order_payload(symbol=forecast["symbol"], instruction=capacity.side,
                order_type="LIMIT", time_in_force=window.time_in_force, position_effect="AUTO",
                quantity=capacity.adjusted_quantity, price=capacity.limit_price)
            def send(authority_gate):
                def gate():
                    try:
                        self._assert_current()
                        self._assert_latest(forecast)
                        self.broker.verify_read_snapshot(self.portfolio.broker_identity_fingerprint)
                        if context.identity_fingerprint != self.portfolio.broker_identity_fingerprint:
                            raise ContractError("Prepared order context differs from account snapshot")
                        current = instant(self.clock())
                        if forecast_disposition(forecast, now=current, session_started_at=self.session_started_at,
                                                local_symbols=self.symbols) != "eligible_for_account_checks":
                            raise ContractError("Forecast eligibility changed before submission")
                        if stock_execution_window(current).time_in_force != window.time_in_force:
                            raise ContractError("Market session changed before submission")
                        if not 0 <= (current-instant(quote.freshness_observed_at)).total_seconds() <= self.ledger.maximum_evidence_age_seconds:
                            raise ContractError("Quote needs refresh before submission")
                        authority_gate()
                    except Exception as exc:
                        raise SubmissionNotStarted(str(exc)) from exc
                location = self.broker.submit_prepared_order(payload, context, before_post=gate)
                return urlparse(str(location or "")).path.rstrip("/").split("/")[-1]
            accepted = self.authority.submit(self.lease.token, account.reservation_id, send)
            self.ledger.mark_submission(native.reservation_id, status="SUBMITTED", observed_at=self.clock(),
                evidence_id=key + ":submitted", broker_order_id=accepted.broker_order_id)
            return self._record(forecast, "submitted", order=self.ledger.lookup_reservation(native.reservation_id),
                                evidence={"account_reservation_id":account.reservation_id})
        except Exception as exc:
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
                        evidence={"error_type":type(exc).__name__,"proven_unsent_retry_eligible":True,
                                  "awaiting_fresh_authority_fence":True,"retry_after_seconds":delay,
                                  "submission_receipt":receipt})
                unsent = state is None or state.status in {"RESERVED", "NOT_SUBMITTED"}
                if state and state.status == "RESERVED":
                    self.authority.abandon_unsubmitted(self.lease.token, state.reservation_id)
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
                    order=self.ledger.lookup_reservation(native.reservation_id), evidence={"error_type":type(exc).__name__,
                        "proven_unsent_retry_eligible":unsent,"retry_after_seconds":delay})
            raise
