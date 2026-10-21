"""Bridge reviewed account plans to the existing native ownership/order runtime.

No broker calls occur in this module. The native caller supplies verified order
and portfolio evidence, its existing final submission checks, and its broker
callback. Account reservations add protection; they never create native lots.
"""
from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Mapping
from dataclasses import asdict
from decimal import Decimal, ROUND_CEILING
import os
from pathlib import Path
import uuid

from ml.account_gameplan.authority import (
    AccountAuthority, AccountSnapshot, AuthorityBinding, AuthorityError,
    BrokerReservation, Fill, OPEN, OrderEvidence, ReservationRequest, _money, _name, _symbol,
)
from ml.account_gameplan.config import assert_coordinator, load_account_config, verify_cutover
from ml.stock_trader.contracts import utc
from ml.stock_trader.fixed_horizon_engine import _joint_budgets
from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY


AUTHORITY_PATH = Path("state/gameplan-trader/authority.sqlite3")


def _binding(config):
    return AuthorityBinding(config.account_fingerprint,config.coordinator_id,config.participants)


def _whole(value):
    if isinstance(value,bool):
        raise AuthorityError("EXACT_WHOLE_BROKER_QUANTITY_REQUIRED")
    number = Decimal(str(value))
    if not number.is_finite() or number<0 or number!=number.to_integral_value():
        raise AuthorityError("EXACT_WHOLE_BROKER_QUANTITY_REQUIRED")
    return int(number)


class OrderSnapshotCoherenceError(AuthorityError):
    """Two valid reads disagree about the same tracked order's fill progress."""

    schwab_retry_safe = True
    stock_trader_operation = "tracked_order_snapshot_coherence"


def assert_order_snapshot_coherence(native_ledger, order_evidence, portfolio):
    """Recapture non-atomic order views before either own ledger is mutated.

    Keep every working row for capacity accounting. Only a balanced fill/status
    disagreement for an exact native order ID is a retryable read; identity,
    symbol, side and total-quantity failures retain their fatal meaning.
    """
    rows = portfolio.broker_working_orders
    if not isinstance(rows, (tuple, list)):
        return  # Existing complete-account validation retains this failure.
    known = {r.broker_order_id:r for r in native_ledger.snapshot().reservations if r.broker_order_id}
    observed = {r.reservation_id:r for r in order_evidence}
    seen = set()
    incoherent = False
    for row in rows:
        if not isinstance(row, Mapping):
            continue  # The existing account snapshot validator rejects it.
        identity = str(row.get("order_id", ""))
        native = known.get(identity)
        if native is None:
            continue  # Untracked broker orders remain ordinary capacity inputs.
        if identity in seen:
            raise AuthorityError("AMBIGUOUS_BROKER_RESERVATIONS")
        seen.add(identity)
        if (row.get("status") != "CURRENT" or row.get("asset_type") not in {"EQUITY","ETF","STOCK"}
                or row.get("symbol") != native.symbol or row.get("instruction") != native.side):
            raise AuthorityError("TRACKED_ORDER_SYMBOL_SIDE_OR_STATUS_MISMATCH")
        filled = _whole(row.get("filled_quantity"))
        remaining = _whole(row.get("remaining_quantity"))
        if remaining <= 0 or filled + remaining != native.quantity:
            raise AuthorityError("TRACKED_ORDER_QUANTITY_MISMATCH")
        detail = observed.get(native.reservation_id)
        if detail is None:
            if native.status not in {"FILLED","CANCELLED","REJECTED"}:
                continue  # Existing fresh exact authority evidence is required.
            status, detail_filled, detail_remaining = native.status, native.filled_quantity, 0
        else:
            if (detail.broker_order_id != identity or detail.account_fingerprint != native_ledger.account_fingerprint
                    or detail.order_quantity != native.quantity):
                raise AuthorityError("NATIVE_AND_ACCOUNT_ORDER_IDENTITIES_DIFFER")
            status = detail.status
            detail_filled = _whole(detail.cumulative_filled_quantity)
            detail_remaining = _whole(detail.remaining_quantity)
            valid = (status == "WORKING" and detail_filled == 0 and detail_remaining == native.quantity
                or status == "PARTIAL" and 0 < detail_filled < native.quantity
                    and detail_remaining == native.quantity-detail_filled
                or status == "FILLED" and detail_filled == native.quantity and detail_remaining == 0
                or status == "CANCELLED" and detail_filled <= native.quantity and detail_remaining == 0
                or status == "REJECTED" and detail_filled == detail_remaining == 0)
            if not valid:
                raise AuthorityError("TRACKED_ORDER_STATUS_OR_QUANTITY_INVALID")
        if (status not in {"WORKING","PARTIAL"} or detail_filled != filled or detail_remaining != remaining):
            incoherent = True
    if incoherent:
        # A valid progress disagreement must not hide another fatal snapshot
        # defect. Normalize every retained row before permitting a read retry.
        pending = _broker_pending(portfolio)
        identities = set()
        for row, item in zip(rows, pending):
            identity = row.get("order_id")
            _name(item.broker_order_id)
            _symbol(item.symbol)
            if (type(identity) not in (str, int) or not str(identity).strip()
                    or str(identity) in identities):
                raise AuthorityError("AMBIGUOUS_BROKER_RESERVATIONS")
            identities.add(str(identity))
            values = [_money(value) for value in (item.cash_reserved, item.exposure_reserved)]
            if item.side == "SELL" and any(values):
                raise AuthorityError("SALE_PROCEEDS_ARE_NOT_CAPACITY")
        raise OrderSnapshotCoherenceError("TRACKED_ORDER_AND_WORKING_SNAPSHOT_NOT_YET_COHERENT")


def evidence_ledger(root, ledger, config):
    """Include authority-pending IDs in the native caller's one history read.

    This also recovers a lagging authority after native terminal reconciliation,
    without extra broker requests or matching an unknown submission by its shape.
    """
    path = Path(root)/AUTHORITY_PATH
    if not path.exists():
        return ledger
    authority = AccountAuthority(path,_binding(config))
    pending = {r.reservation_id:r for r in ledger.pending_reservations()}
    for record in authority.read_state()["reservations"]:
        if record["status"] not in OPEN:
            continue
        native = ledger.lookup_reservation(record["request"]["native_reservation_id"])
        if (native.broker_order_id!=record["broker_order_id"]
                or native.allocation_id!=record["request"]["native_allocation_id"]):
            raise AuthorityError("NATIVE_AND_ACCOUNT_ORDER_IDENTITIES_DIFFER")
        pending[native.reservation_id] = native

    class PendingEvidence:
        account_fingerprint = ledger.account_fingerprint

        def pending_reservations(self):
            return tuple(pending.values())

    return PendingEvidence()


def _broker_pending(portfolio):
    rows = portfolio.broker_working_orders
    if rows is None:
        raise AuthorityError("EXACT_ACCOUNT_WIDE_ORDER_IDENTITIES_REQUIRED")
    pending = []
    for row in rows:
        if (row.get("status")!="CURRENT" or row.get("asset_type") not in {"EQUITY","ETF","STOCK"}
                or row.get("instruction") not in {"BUY","SELL"}):
            raise AuthorityError("UNSUPPORTED_OR_INCOMPLETE_ACCOUNT_WORKING_ORDER")
        quantity = _whole(row["remaining_quantity"])
        if quantity<=0:
            raise AuthorityError("WORKING_ORDER_REMAINING_QUANTITY_REQUIRED")
        price = Decimal(str(row["limit_price"]))
        if not price.is_finite() or price<=0:
            raise AuthorityError("WORKING_ORDER_LIMIT_PRICE_REQUIRED")
        side, symbol = row["instruction"],row["symbol"]
        quote = portfolio.quotes.get(symbol)
        valuation = (max(price,Decimal(str(quote.ask))) if quote is not None else price).quantize(Decimal(".01"), rounding=ROUND_CEILING)
        pending.append(BrokerReservation(str(row["order_id"]),symbol,side,quantity,
            _whole(row["filled_quantity"]),str(row["reserved_cash"]),
            str(quantity*valuation if side=="BUY" else Decimal(0))))
    return tuple(pending)


class AccountExecutionContext:
    def __init__(self, root, config, plan, portfolio, native_ledger, order_evidence, *, snapshot_id, policy, clock):
        self.root, self.config, self.plan = Path(root),config,plan
        self.clock, self.native_ledger, self.portfolio = clock,native_ledger,portfolio
        self.snapshot_id = snapshot_id
        self.sources = {row["producer_id"]:row["source_receipt_sha256"] for row in plan.report["sources"]}
        self.authority = AccountAuthority(self.root/AUTHORITY_PATH,_binding(config),clock=lambda:utc(clock()).to_pydatetime())
        self.lease = self.authority.acquire_lease(coordinator_id=config.machine_id,
            holder_id=f"native-stock-cycle:{os.getpid()}:{uuid.uuid4()}")
        try:
            self.assert_current()
            self._reconcile(order_evidence)
            self._snapshot(policy)
        except BaseException:
            self.close()
            raise

    def assert_current(self):
        from ml.stock_trader.gameplan_execution import account_execution_plan
        current = load_account_config(self.root)
        assert_coordinator(current,GAMEPLAN_SIZING_POLICY)
        if current is None or current.fingerprint!=self.config.fingerprint:
            raise AuthorityError("ACCOUNT_EXECUTION_CONFIG_CHANGED")
        plan, _ = account_execution_plan(self.root,action_date=self.plan.report["action_date"])
        if plan.path!=self.plan.path or plan.manifest_sha256!=self.plan.manifest_sha256:
            raise AuthorityError("PINNED_ACCOUNT_EXECUTION_PLAN_CHANGED")
        self.lease = self.authority.renew_lease(self.lease.token)

    def _reconcile(self, native_evidence):
        by_native = {r.reservation_id:r for r in native_evidence}
        for record in self.authority.read_state()["reservations"]:
            native = self.native_ledger.lookup_reservation(record["request"]["native_reservation_id"])
            if (native.allocation_id!=record["request"]["native_allocation_id"]
                    or native.broker_order_id!=record["broker_order_id"]):
                raise AuthorityError("NATIVE_AND_ACCOUNT_ORDER_IDENTITIES_DIFFER")
            observed = by_native.get(native.reservation_id)
            if observed is None:
                if (record["status"]=="RESERVED" and native.status=="REJECTED"
                        and not native.broker_order_id and native.filled_quantity==0):
                    self.authority.abandon_unsubmitted(self.lease.token,record["reservation_id"])
                elif record["status"] in OPEN:
                    raise AuthorityError("FRESH_EXACT_AUTHORITY_ORDER_EVIDENCE_REQUIRED")
                elif native.filled_quantity!=record["filled_quantity"]:
                    raise AuthorityError("NATIVE_AND_ACCOUNT_FILLS_DIFFER")
                continue
            if record["last_evidence_at"] and utc(observed.observed_at)<=utc(record["last_evidence_at"]):
                if observed.cumulative_filled_quantity!=record["filled_quantity"] or observed.status!=record["status"]:
                    raise AuthorityError("ORDER_EVIDENCE_DID_NOT_ADVANCE")
                continue
            self.authority.reconcile(self.lease.token,OrderEvidence(
                observed.evidence_id,record["reservation_id"],observed.account_fingerprint,observed.broker_order_id,
                observed.observed_at,observed.status,observed.order_quantity,observed.cumulative_filled_quantity,
                observed.remaining_quantity,tuple(Fill(f.fill_id,f.quantity,f.price,f.executed_at) for f in observed.fills),
                complete=True,fill_time_provenance=observed.fill_time_provenance))

    def _snapshot(self, policy):
        portfolio = self.portfolio
        if self.native_ledger.account_fingerprint!=self.config.account_fingerprint:
            raise AuthorityError("NATIVE_ACCOUNT_BINDING_CHANGED")
        if set(portfolio.held_shares)!=set(self.config.symbols):
            raise AuthorityError("EXPLICIT_COMPLETE_EXECUTION_UNION_REQUIRED")
        pending = _broker_pending(portfolio)
        equity = Decimal(str(portfolio.account_equity))
        pending_gross = sum((Decimal(str(p.exposure_reserved)) for p in pending),Decimal(0))
        gross = max(Decimal(0),equity*Decimal(str(policy.maximum_gross_equity_fraction))
                    -Decimal(str(portfolio.gross_exposure))-pending_gross)
        available,engine_symbols = _joint_budgets(portfolio,policy)
        budgets = {}
        for symbol in self.config.symbols:
            pending_symbol = sum((Decimal(str(p.exposure_reserved)) for p in pending if p.symbol == symbol), Decimal(0))
            remaining = max(Decimal(0), equity * Decimal(str(policy.maximum_symbol_equity_fraction))
                            - Decimal(str(portfolio.symbol_exposure.get(symbol, 0))) - pending_symbol)
            budgets[symbol] = str(min(engine_symbols.get(symbol, Decimal(0)), remaining))
        accounted = {r["broker_order_id"]:r["filled_quantity"] for r in self.authority.read_state()["reservations"]
                     if r["broker_order_id"] and r["filled_quantity"]}
        self.authority.publish_snapshot(self.lease.token,AccountSnapshot(
            self.snapshot_id,self.config.account_fingerprint,self.plan.manifest_sha256,self.plan.report["action_date"],
            self.sources,portfolio.observed_at,str(portfolio.account_equity),str(portfolio.available_cash),
            str(min(available,gross)),str(portfolio.gross_exposure),str(gross),
            {s:str(portfolio.symbol_exposure.get(s,0)) for s in self.config.symbols},budgets,
            {s:int(Decimal(str(q)).to_integral_value(rounding="ROUND_FLOOR")) for s,q in portfolio.held_shares.items()},
            pending,accounted,portfolio.source_fingerprint,self.config.account_fingerprint,self.config.account_fingerprint,True))

    def available_capacity(self):
        """Use the same own commitments for sizing and final reservation."""
        self.assert_current()
        return self.authority.available_capacity(self.lease.token,self.snapshot_id)

    def reserve(self, decision, native):
        self.assert_current()
        forecast_id = decision.prediction["prediction_id"]
        if decision.prediction.get("catchup_components"):
            participant = self._catchup_participant(decision, native)
        else:
            rows = self.plan.rows.loc[self.plan.rows.id.eq(forecast_id)]
            if len(rows)!=1:
                raise AuthorityError("DECISION_NOT_IN_PINNED_ACCOUNT_PLAN")
            row = rows.iloc[0]
            if (row.symbol!=decision.symbol or row.model_group!=decision.prediction["primary_horizon"]
                    or float(row.calibrated_probability)!=float(decision.prediction["calibrated_probability"])
                    or utc(row.target_window_start)!=utc(decision.prediction["target_window_start"])
                    or utc(row.target_window_end)!=utc(decision.prediction["target_window_end"])):
                raise AuthorityError("DECISION_DIFFERS_FROM_PINNED_ACCOUNT_FORECAST")
            participant = str(row.producer_id)
        if (native.symbol!=decision.symbol or native.side!=decision.action or native.quantity!=decision.quantity
                or Decimal(str(native.limit_price))!=Decimal(str(decision.limit_price))):
            raise AuthorityError("NATIVE_RESERVATION_DIFFERS_FROM_ACCOUNT_REQUEST")
        valuation = max(Decimal(str(decision.limit_price)), Decimal(str(decision.quote["ask"])))
        valuation = valuation.quantize(Decimal(".01"), rounding=ROUND_CEILING)
        return self.authority.reserve(self.lease.token,ReservationRequest(
            decision.decision_id,self.snapshot_id,self.plan.manifest_sha256,self.plan.report["action_date"],self.sources,
            participant,decision.symbol,decision.prediction["primary_horizon"],forecast_id,decision.action,
            decision.quantity,str(decision.limit_price),str(valuation),
            native.reservation_id,native.allocation_id))

    def _catchup_participant(self, decision, native):
        """Prove the net intention again before granting account-wide cash.

        The native reservation was just created by the caller. Excluding only
        that exact reservation reconstructs the reconciled state used to decide
        the order; all earlier fills and outstanding orders remain counted.
        """
        from ml.stock_trader.catchup import catchup_signals
        if self.plan.report.get("accepted_joint_plan") is not True:
            raise AuthorityError("CATCHUP_REQUIRES_ACCEPTED_ACCOUNT_PLAN")
        reservations = self.native_ledger.snapshot().reservations
        if sum(item.reservation_id == native.reservation_id for item in reservations) != 1:
            raise AuthorityError("EXACT_NEW_NATIVE_RESERVATION_REQUIRED")
        prior = tuple(item for item in reservations if item.reservation_id != native.reservation_id)
        signals = catchup_signals(self.plan.report["joint_composition"], as_of=decision.decided_at,
            source_fingerprint=self.plan.path.name, reservations=prior)
        signal = signals.get((decision.symbol, decision.prediction["primary_horizon"]))
        if signal is None or any(decision.prediction.get(key) != value for key, value in asdict(signal).items()):
            raise AuthorityError("DECISION_DIFFERS_FROM_RECONCILED_ACCOUNT_INTENTION")
        side = "BUY" if signal.calibrated_probability == 1 else "SELL"
        if decision.action != side or not 0 < decision.quantity <= signal.planned_quantity:
            raise AuthorityError("DECISION_EXCEEDS_RECONCILED_ACCOUNT_INTENTION")
        owners = [owner for owner, symbols in self.config.participants.items() if decision.symbol in symbols]
        if len(owners) != 1:
            raise AuthorityError("EXACT_ACCOUNT_INTENTION_OWNER_REQUIRED")
        return owners[0]

    def submit(self, reservation, callback):
        self.assert_current()
        def guarded(gate):
            def before_send():
                self.assert_current()
                gate()
            return callback(before_send)
        return self.authority.submit(self.lease.token,reservation.reservation_id,guarded)

    def abandon(self, reservation):
        return self.authority.abandon_unsubmitted(self.lease.token,reservation.reservation_id)

    def close(self):
        try:
            self.authority.release_lease(self.lease.token)
        except AuthorityError:
            # An expired/fenced holder cannot renew or release another owner.
            pass


@contextmanager
def account_execution_context(root, config, plan, portfolio, native_ledger, evidence, *, snapshot_id, policy, clock):
    assert_coordinator(config,GAMEPLAN_SIZING_POLICY)
    verify_cutover(root,config)
    context = AccountExecutionContext(root,config,plan,portfolio,native_ledger,evidence,
                                      snapshot_id=snapshot_id,policy=policy,clock=clock)
    try:
        yield context
    finally:
        context.close()
