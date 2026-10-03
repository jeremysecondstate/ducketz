"""Bridge reviewed account plans to the existing native ownership/order runtime.

No broker calls occur in this module. The native caller supplies verified order
and portfolio evidence, its existing final submission checks, and its broker
callback. Account reservations add protection; they never create native lots.
"""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, ROUND_CEILING
import os
from pathlib import Path
import uuid

from ml.account_gameplan.authority import (
    AccountAuthority, AccountSnapshot, AuthorityBinding, AuthorityError,
    BrokerReservation, Fill, OPEN, OrderEvidence, ReservationRequest,
)
from ml.account_gameplan.config import assert_coordinator, load_account_config, verify_cutover
from ml.stock_trader.contracts import utc
from ml.stock_trader.fixed_horizon_engine import _joint_budgets
from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY


AUTHORITY_PATH = Path("state/account-gameplan/authority.sqlite3")


def _binding(config):
    return AuthorityBinding(config.account_fingerprint,config.coordinator_id,config.participants)


def _whole(value):
    if isinstance(value,bool):
        raise AuthorityError("EXACT_WHOLE_BROKER_QUANTITY_REQUIRED")
    number = Decimal(str(value))
    if not number.is_finite() or number<0 or number!=number.to_integral_value():
        raise AuthorityError("EXACT_WHOLE_BROKER_QUANTITY_REQUIRED")
    return int(number)


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
                observed.remaining_quantity,tuple(Fill(f.fill_id,f.quantity,f.price,f.executed_at) for f in observed.fills),complete=True))

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

    def reserve(self, decision, native):
        self.assert_current()
        forecast_id = decision.prediction["prediction_id"]
        rows = self.plan.rows.loc[self.plan.rows.id.eq(forecast_id)]
        if len(rows)!=1:
            raise AuthorityError("DECISION_NOT_IN_PINNED_ACCOUNT_PLAN")
        row = rows.iloc[0]
        if (row.symbol!=decision.symbol or row.model_group!=decision.prediction["primary_horizon"]
                or float(row.calibrated_probability)!=float(decision.prediction["calibrated_probability"])
                or utc(row.target_window_start)!=utc(decision.prediction["target_window_start"])
                or utc(row.target_window_end)!=utc(decision.prediction["target_window_end"])):
            raise AuthorityError("DECISION_DIFFERS_FROM_PINNED_ACCOUNT_FORECAST")
        if (native.symbol!=decision.symbol or native.side!=decision.action or native.quantity!=decision.quantity
                or Decimal(str(native.limit_price))!=Decimal(str(decision.limit_price))):
            raise AuthorityError("NATIVE_RESERVATION_DIFFERS_FROM_ACCOUNT_REQUEST")
        valuation = max(Decimal(str(decision.limit_price)), Decimal(str(decision.quote["ask"])))
        valuation = valuation.quantize(Decimal(".01"), rounding=ROUND_CEILING)
        return self.authority.reserve(self.lease.token,ReservationRequest(
            decision.decision_id,self.snapshot_id,self.plan.manifest_sha256,self.plan.report["action_date"],self.sources,
            str(row.producer_id),decision.symbol,decision.prediction["primary_horizon"],forecast_id,decision.action,
            decision.quantity,str(decision.limit_price),str(valuation),
            native.reservation_id,native.allocation_id))

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
