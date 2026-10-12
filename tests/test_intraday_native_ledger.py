"""Exact native lot accounting in temporary databases, without order calls."""
from dataclasses import asdict
from contextlib import closing
import hashlib
import sqlite3

import pytest

from ml.stock_trader.horizon_ledger import FillEvidence, HorizonLedger, LedgerError, OrderEvidence, PortfolioEvidence


ACCOUNT = "a"*64
REFERENCE = "2026-10-12T17:00:00+00:00"
NOW = "2026-10-12T17:15:03+00:00"
TARGET = "2026-10-12T17:45:00+00:00"
LOCAL = ("AAPL","AMZN")


def portfolio(key="actual",at=NOW,held=20,budget=0):
    return PortfolioEvidence(key,ACCOUNT,at,{"AAPL":held,"AMZN":0},{"AAPL":10,"AMZN":10},
        {"AAPL":budget,"AMZN":budget},"b"*64)


@pytest.fixture
def ledger(tmp_path):
    result = HorizonLedger(tmp_path/"native.sqlite3",ACCOUNT)
    assert result.reconcile(portfolio()).ready
    return result


def reserve(ledger,key="intent",**changes):
    forecast = hashlib.sha256(key.encode()).hexdigest()
    values = dict(symbol="AAPL",forecast_id=forecast,side="BUY",quantity=10,limit_price=10,
        snapshot_id="actual",idempotency_key=key,batch_id="batch-"+key,as_of=NOW,
        reference_at=REFERENCE,target_close_at=TARGET,forecast_sha256=forecast,
        forecast_snapshot_sha256="d"*64,local_symbols=LOCAL,pending_sell_shares=0)
    values.update(changes)
    return ledger.reserve_intraday(**values)


def evidence(order, key, *, filled, status="FILLED",at="2026-10-12T17:15:04+00:00",fills=None):
    fills = ((FillEvidence("fill-"+key,filled,10,at),) if filled else ()) if fills is None else fills
    return OrderEvidence(key,order.reservation_id,ACCOUNT,at,"broker-"+order.idempotency_key,
        status,order.quantity,filled,order.quantity-filled if status in {"WORKING","PARTIAL"} else 0,fills)


def legacy_filled_lots(ledger):
    # Deliberately distinct four-horizon purchases. The 15m SELL is permitted
    # regardless of those allocation labels or selected Gameplan directions.
    assert ledger.reconcile(portfolio("legacy-start","2026-10-12T17:00:00+00:00",held=2,budget=100000)).ready
    first = ledger.reserve_entry(symbol="AAPL",horizon="1h",forecast_id="gp-first",target_start=REFERENCE,
        target_end="2026-10-12T18:00:00+00:00",quantity=4,limit_price=10,snapshot_id="legacy-start",
        idempotency_key="gp-first",batch_id="legacy",as_of="2026-10-12T17:00:00+00:00")
    second = ledger.reserve_entry(symbol="AAPL",horizon="1w",forecast_id="gp-second",target_start=REFERENCE,
        target_end="2026-10-19T17:00:00+00:00",quantity=6,limit_price=10,snapshot_id="legacy-start",
        idempotency_key="gp-second",batch_id="legacy",as_of="2026-10-12T17:00:00+00:00")
    assert ledger.reconcile(portfolio("actual",NOW,held=12,budget=0),order_evidence=(
        evidence(first,"gp-fill-one",filled=4,at="2026-10-12T17:00:01+00:00"),
        evidence(second,"gp-fill-two",filled=6,at="2026-10-12T17:00:01+00:00"))).ready
    return first,second


def test_no_horizon_budget_minimum_notional_or_target_window_gate(ledger):
    order = reserve(ledger,quantity=10000,limit_price="0.01")
    assert order.horizon == "15m" and order.quantity == 10000
    assert ledger.reservation_stream(order.reservation_id) == "atlas-15m"
    assert ledger.allocation_stream(order.allocation_id) == "atlas-15m"
    assert order.target_start == REFERENCE and order.target_end == TARGET
    assert ledger.snapshot().allocations[0].filled_shares == 0


def test_legacy_stream_and_request_serialization_do_not_change(tmp_path):
    ledger = HorizonLedger(tmp_path/"native.sqlite3",ACCOUNT)
    assert ledger.reconcile(portfolio("start",REFERENCE,budget=1000)).ready
    order = ledger.reserve_entry(symbol="AAPL",horizon="1h",forecast_id="old",target_start=REFERENCE,
        target_end="2026-10-12T18:00:00+00:00",quantity=1,limit_price=10,snapshot_id="start",
        idempotency_key="old",batch_id="old",as_of=REFERENCE)
    assert ledger.reservation_stream(order.reservation_id) == "gameplan"
    assert ledger.allocation_stream(order.allocation_id) == "gameplan"
    assert "stream" not in asdict(order)
    with closing(sqlite3.connect(ledger.path)) as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='intraday_streams'").fetchone()


def test_multilot_sell_with_one_order_identity_exact_fills_and_no_donor_gate(tmp_path):
    ledger = HorizonLedger(tmp_path/"native.sqlite3",ACCOUNT)
    owners = legacy_filled_lots(ledger)
    order = reserve(ledger,side="SELL",quantity=12)
    plan = ledger.intraday_sell_attribution(order.reservation_id)
    assert sum(s["quantity"] for s in plan["slices"]) == 12
    assert {s["allocation"] for s in plan["slices"]} >= {o.allocation_id for o in owners}
    assert plan["fills"] == []
    assert sum(a.reserved_sell_shares for a in ledger.snapshot().allocations) == 12
    fills = (FillEvidence("actual-one",3,10,"2026-10-12T17:15:04+00:00"),
             FillEvidence("actual-two",9,10,"2026-10-12T17:15:04+00:00"))
    assert ledger.reconcile(portfolio("sold","2026-10-12T17:15:05+00:00",held=0),
        order_evidence=(evidence(order,"sold-evidence",filled=12,fills=fills),)).ready
    reopened = HorizonLedger(ledger.path,ACCOUNT)
    assert sum(a.filled_shares for a in reopened.snapshot().allocations) == 0
    audited = reopened.intraday_sell_attribution(order.reservation_id)
    assert sum(f["quantity"] for f in audited["fills"]) == 12
    assert {f["fill"] for f in audited["fills"]} == {"actual-one","actual-two"}
    state = reopened.snapshot()
    assert {r.forecast_id:r.filled_quantity for r in state.reservations if r.side=="BUY"} == {"gp-first":4,"gp-second":6}
    assert reopened.reconcile(portfolio("restoration-check","2026-10-12T17:15:06+00:00",held=0,budget=100000)).ready
    with pytest.raises(LedgerError,match="FORECAST_ALREADY_RESERVED"):
        reopened.reserve_entry(symbol="AAPL",horizon="1h",forecast_id="gp-first",target_start=REFERENCE,
            target_end="2026-10-12T18:00:00+00:00",quantity=1,limit_price=10,snapshot_id="restoration-check",
            idempotency_key="do-not-restore",batch_id="new",as_of="2026-10-12T17:15:06+00:00")


def test_partial_cancellation_releases_only_unfilled_account_slices(tmp_path):
    ledger = HorizonLedger(tmp_path/"native.sqlite3",ACCOUNT)
    legacy_filled_lots(ledger)
    order = reserve(ledger,side="SELL",quantity=12)
    first_fill = FillEvidence("first-real",3,10,"2026-10-12T17:15:04+00:00")
    assert ledger.reconcile(portfolio("partial","2026-10-12T17:15:05+00:00",held=9),
        order_evidence=(evidence(order,"partial-status",filled=3,status="PARTIAL",fills=(first_fill,)),)).ready
    before = ledger.intraday_sell_attribution(order.reservation_id)
    assert sum(a.reserved_sell_shares for a in ledger.snapshot().allocations) == 9
    assert ledger.reserve_stream_cancellation(order.reservation_id,stream="atlas-15m",reason="new-forecast",
        as_of="2026-10-12T17:15:06+00:00",evidence_id="cancel-intent")
    assert sum(a.reserved_sell_shares for a in ledger.snapshot().allocations) == 9
    assert not ledger.reserve_stream_cancellation(order.reservation_id,stream="atlas-15m",reason="new-forecast",
        as_of="2026-10-12T17:15:06+00:00",evidence_id="cancel-duplicate")
    second_fill = FillEvidence("intervening-real",2,10,"2026-10-12T17:15:07+00:00")
    assert ledger.reconcile(portfolio("cancelled","2026-10-12T17:15:09+00:00",held=7),order_evidence=(
        evidence(order,"cancelled-status",filled=5,status="CANCELLED",at="2026-10-12T17:15:08+00:00",
                 fills=(second_fill,first_fill)),)).ready
    after = ledger.intraday_sell_attribution(order.reservation_id)
    assert [f for f in after["fills"] if f["fill"]=="first-real"] == before["fills"]
    assert sum(a.reserved_sell_shares for a in ledger.snapshot().allocations) == 0
    assert sum(a.filled_shares for a in ledger.snapshot().allocations) == 5
    # Two unallocated unsold shares return to the account pool, not a fake BUY.
    assert ledger.lookup_reservation(order.reservation_id).filled_quantity == 5


def test_existing_intraday_owner_and_unallocated_slice_can_share_one_allocation(ledger):
    buy = reserve(ledger,"buy",quantity=2)
    assert ledger.reconcile(portfolio("bought","2026-10-12T17:15:05+00:00",held=22),
        order_evidence=(evidence(buy,"bought-evidence",filled=2),)).ready
    sell = reserve(ledger,"sell",side="SELL",quantity=22,snapshot_id="bought",as_of="2026-10-12T17:15:06+00:00")
    assert sell.allocation_id == buy.allocation_id
    slices = ledger.intraday_sell_attribution(sell.reservation_id)["slices"]
    assert [s["quantity"] for s in slices] == [2,20]
    assert ledger.reconcile(portfolio("sold","2026-10-12T17:15:08+00:00",held=0),order_evidence=(
        evidence(sell,"sale",filled=22,at="2026-10-12T17:15:07+00:00"),)).ready
    assert sum(a.filled_shares for a in ledger.snapshot().allocations) == 0


def test_external_and_local_pending_sell_commitments_are_unavailable_once(ledger):
    first = reserve(ledger,"first",side="SELL",quantity=7)
    ledger.mark_submission(first.reservation_id,status="SUBMITTED",broker_order_id="101",
        observed_at="2026-10-12T17:15:04+00:00",evidence_id="accepted")
    with pytest.raises(LedgerError,match="ACCOUNT_ELIGIBLE"):
        reserve(ledger,"too-much",side="SELL",quantity=9,pending_sell_shares=12,
                broker_pending_sell_orders={"101":7,"external":5})
    second = reserve(ledger,"remaining",side="SELL",quantity=8,pending_sell_shares=12,
                     broker_pending_sell_orders={"101":7,"external":5})
    assert second.quantity == 8
    assert ledger.lookup_reservation(first.reservation_id).filled_quantity == 0
    assert len(ledger.snapshot().reservations) == 2


def test_unsubmitted_local_sell_is_not_guessed_to_overlap_broker_pending(ledger):
    reserve(ledger,"never-sent",side="SELL",quantity=7)
    with pytest.raises(LedgerError,match="ACCOUNT_ELIGIBLE"):
        reserve(ledger,"too-much",side="SELL",quantity=7,pending_sell_shares=7,
                broker_pending_sell_orders={"outside-order":7})
    assert reserve(ledger,"actual-remainder",side="SELL",quantity=6,pending_sell_shares=7,
                   broker_pending_sell_orders={"outside-order":7}).quantity == 6


def test_aggregate_pending_without_exact_ids_is_conservative(ledger):
    reserve(ledger,"local",side="SELL",quantity=7)
    with pytest.raises(LedgerError,match="ACCOUNT_ELIGIBLE"):
        reserve(ledger,"too-much",side="SELL",quantity=7,pending_sell_shares=7)
    assert reserve(ledger,"remaining",side="SELL",quantity=6,pending_sell_shares=7).quantity == 6


def test_pending_id_quantities_must_match_broker_total_and_local_remainder(ledger):
    first = reserve(ledger,"first",side="SELL",quantity=7)
    ledger.mark_submission(first.reservation_id,status="SUBMITTED",broker_order_id="101",
        observed_at="2026-10-12T17:15:04+00:00",evidence_id="accepted")
    with pytest.raises(LedgerError,match="EXACT_BROKER_PENDING"):
        reserve(ledger,"bad-total",side="SELL",quantity=1,pending_sell_shares=7,
                broker_pending_sell_orders={"101":6})
    with pytest.raises(LedgerError,match="DOES_NOT_MATCH"):
        reserve(ledger,"bad-remainder",side="SELL",quantity=1,pending_sell_shares=6,
                broker_pending_sell_orders={"101":6})


def test_unknown_retains_every_share_across_restart_without_resend(ledger):
    order = reserve(ledger,side="SELL",quantity=20)
    ledger.mark_submission(order.reservation_id,status="UNKNOWN",observed_at="2026-10-12T17:15:04+00:00",evidence_id="unknown")
    reopened = HorizonLedger(ledger.path,ACCOUNT)
    assert sum(a.reserved_sell_shares for a in reopened.snapshot().allocations) == 20
    assert reserve(reopened,side="SELL",quantity=20) == reopened.lookup_reservation(order.reservation_id)
    with pytest.raises(LedgerError,match="ACCOUNT_ELIGIBLE"):
        reserve(reopened,"new-sale",side="SELL",quantity=1)
    with pytest.raises(LedgerError,match="KNOWN_WORKING"):
        reopened.reserve_stream_cancellation(order.reservation_id,stream="atlas-15m",reason="new-forecast",as_of=NOW,evidence_id="no-id")


def test_completed_buy_accumulates_and_is_not_repeated_after_later_sale(ledger):
    first = reserve(ledger,"first",quantity=2)
    assert ledger.reconcile(portfolio("first-done","2026-10-12T17:15:05+00:00",held=22),
        order_evidence=(evidence(first,"first-fill",filled=2),)).ready
    second = reserve(ledger,"second",quantity=3,snapshot_id="first-done",as_of="2026-10-12T17:15:06+00:00")
    assert second.allocation_id == first.allocation_id
    with pytest.raises(LedgerError,match="FORECAST_ALREADY_RESERVED"):
        reserve(ledger,"another-key",forecast_id=first.forecast_id,forecast_sha256=first.forecast_id,
                snapshot_id="first-done",as_of="2026-10-12T17:15:06+00:00")


def test_no_scheduled_liquidation_for_intraday_positions(ledger):
    buy = reserve(ledger,quantity=2)
    assert ledger.reconcile(portfolio("held","2026-10-12T17:15:05+00:00",held=22),
        order_evidence=(evidence(buy,"filled",filled=2),)).ready
    assert ledger.reconcile(portfolio("later",TARGET,held=22)).ready
    assert ledger.due_exits(as_of=TARGET,snapshot_id="later") == ()
    with pytest.raises(LedgerError,match="NO_SCHEDULED_LIQUIDATION"):
        ledger.reserve_exit(allocation_id=buy.allocation_id,quantity=2,limit_price=10,snapshot_id="later",
            idempotency_key="forced",batch_id="forced",as_of=TARGET)


@pytest.mark.parametrize("changes",[
    {"symbol":"MSFT"},{"side":"SHORT"},{"quantity":0},{"local_symbols":LOCAL+("AAPL",)},
    {"forecast_sha256":"not-a-hash"},{"target_close_at":"2026-10-12T17:30:00+00:00"},
    {"reference_at":"2026-10-12T17:00:01+00:00"},{"as_of":TARGET},
])
def test_bad_scope_proof_target_or_quantity_does_not_create_records(ledger,changes):
    with pytest.raises(LedgerError):
        reserve(ledger,**changes)
    assert ledger.snapshot().reservations == ()


def test_cancellation_cannot_cross_stream_or_inherit_gameplan_timing(ledger):
    order = reserve(ledger,side="SELL",quantity=2)
    ledger.mark_submission(order.reservation_id,status="SUBMITTED",broker_order_id="123",
        observed_at="2026-10-12T17:15:04+00:00",evidence_id="submitted")
    for stream,reason in (("gameplan","new-forecast"),("atlas-15m","five-minute")):
        with pytest.raises(LedgerError,match="UNREVIEWED"):
            ledger.reserve_stream_cancellation(order.reservation_id,stream=stream,reason=reason,
                as_of="2026-10-12T17:15:05+00:00",evidence_id="wrong")
    assert ledger.reserve_stream_cancellation(order.reservation_id,stream="atlas-15m",reason="new-forecast",
        as_of="2026-10-12T17:15:05+00:00",evidence_id="right")
    for legacy_method in (ledger.reserve_cancellation,ledger.reserve_order_timeout):
        with pytest.raises(LedgerError,match="STREAM_MISMATCH"):
            legacy_method(order.reservation_id,as_of="2026-10-12T17:15:05+00:00",evidence_id="legacy-reason")


def test_fractional_observed_residual_is_preserved_while_whole_shares_can_sell(tmp_path):
    ledger = HorizonLedger(tmp_path/"fractional.sqlite3",ACCOUNT)
    assert ledger.reconcile(portfolio(held=20.5)).ready
    sell = reserve(ledger,side="SELL",quantity=20)
    assert ledger.reconcile(portfolio("sold","2026-10-12T17:15:05+00:00",held=.5),
        order_evidence=(evidence(sell,"whole-fill",filled=20),)).ready
    assert sum(a.filled_shares for a in ledger.snapshot().allocations) == 0


def test_incomplete_fill_evidence_cannot_debit_owner_or_release_commitment(ledger):
    order = reserve(ledger,side="SELL",quantity=20)
    before = ledger.snapshot()
    with pytest.raises(LedgerError,match="COMPLETE_PER_ORDER_FILL"):
        ledger.reconcile(portfolio("wrong","2026-10-12T17:15:05+00:00",held=10),
            order_evidence=(evidence(order,"wrong-fills",filled=10,status="PARTIAL",fills=()),))
    assert ledger.snapshot() == before
    assert ledger.intraday_sell_attribution(order.reservation_id)["fills"] == []


def retry(ledger, previous, key="retry", **changes):
    values=dict(forecast_id=previous.forecast_id,forecast_sha256=previous.forecast_id,side=previous.side,
                retry_of_reservation_id=previous.reservation_id)
    values.update(changes)
    return reserve(ledger,key,**values)


@pytest.mark.parametrize("side",["BUY","SELL"])
def test_proven_never_submitted_retry_preserves_attempts_and_reuses_empty_accounting_allocation(ledger,side):
    original=reserve(ledger,side=side,quantity=5)
    ledger.mark_submission(original.reservation_id,status="REJECTED",observed_at="2026-10-12T17:15:04+00:00",evidence_id="no-post")
    assert ledger.snapshot().allocations[0].status=="CLOSED"
    assert ledger.reconcile(portfolio("fresh","2026-10-12T17:15:05+00:00")).ready
    child=retry(ledger,original,snapshot_id="fresh",as_of="2026-10-12T17:15:06+00:00",quantity=4,limit_price="11")
    assert child.reservation_id!=original.reservation_id and child.allocation_id==original.allocation_id
    assert child.quantity==4 and child.limit_price=="11"
    assert ledger.lookup_reservation(original.reservation_id).quantity==5
    assert ledger.lookup_reservation(original.reservation_id).status=="REJECTED"
    assert retry(ledger,original,snapshot_id="fresh",as_of="2026-10-12T17:15:06+00:00",quantity=4,limit_price="11")==child
    ledger.mark_submission(child.reservation_id,status="REJECTED",observed_at="2026-10-12T17:15:07+00:00",evidence_id="child-no-post")
    assert ledger.reconcile(portfolio("fresh-two","2026-10-12T17:15:08+00:00")).ready
    for index in range(4):
        child=retry(ledger,child,"next-"+str(index),snapshot_id="fresh-two",as_of="2026-10-12T17:15:09+00:00",quantity=1)
        ledger.mark_submission(child.reservation_id,status="REJECTED",observed_at="2026-10-12T17:15:09+00:00",evidence_id="next-no-post-"+str(index))
    assert len(ledger.snapshot().reservations)==6
    attempts=ledger.intraday_attempts(symbol="AAPL",forecast_id=original.forecast_id)
    assert len(attempts)==6 and attempts[0].reservation_id==original.reservation_id
    assert attempts[-1].reservation_id==child.reservation_id
    assert sum(a.filled_shares for a in ledger.snapshot().allocations)==0


@pytest.mark.parametrize("status",["RESERVED","UNKNOWN","SUBMITTED","WORKING","PARTIAL","FILLED","CANCELLED","REJECTED_WITH_ID"])
def test_native_retry_rejects_every_possible_acceptance_or_filled_predecessor(ledger,status):
    original=reserve(ledger,quantity=2)
    snapshot="actual"
    if status in {"UNKNOWN","SUBMITTED","REJECTED_WITH_ID"}:
        ledger.mark_submission(original.reservation_id,status="REJECTED" if status=="REJECTED_WITH_ID" else status,
            observed_at="2026-10-12T17:15:04+00:00",evidence_id="status",
            broker_order_id="101" if status!="UNKNOWN" else None)
    elif status!="RESERVED":
        filled=2 if status=="FILLED" else 1 if status=="PARTIAL" else 0
        assert ledger.reconcile(portfolio("observed","2026-10-12T17:15:05+00:00",held=20+filled),
            order_evidence=(evidence(original,"observed",filled=filled,status=status),)).ready
        snapshot="observed"
    with pytest.raises(LedgerError,match="LATEST_PROVEN_UNSUBMITTED"):
        retry(ledger,original,snapshot_id=snapshot,as_of="2026-10-12T17:15:06+00:00",quantity=1)
    assert len(ledger.snapshot().reservations)==1


@pytest.mark.parametrize("changes",[
    {"symbol":"AMZN"},{"side":"SELL"},{"forecast_id":"e"*64,"forecast_sha256":"e"*64},
    {"forecast_snapshot_sha256":"f"*64},
])
def test_native_retry_requires_same_forecast_symbol_side_and_proof(ledger,changes):
    original=reserve(ledger,quantity=1)
    ledger.mark_submission(original.reservation_id,status="REJECTED",observed_at="2026-10-12T17:15:04+00:00",evidence_id="unsent")
    with pytest.raises(LedgerError,match="PREDECESSOR_SCOPE"):
        retry(ledger,original,quantity=1,**changes)


def test_stale_native_retry_link_cannot_fork_and_default_duplicate_rule_remains(ledger):
    original=reserve(ledger,quantity=1)
    ledger.mark_submission(original.reservation_id,status="REJECTED",observed_at="2026-10-12T17:15:04+00:00",evidence_id="unsent")
    child=retry(ledger,original,quantity=1)
    ledger.mark_submission(child.reservation_id,status="REJECTED",observed_at="2026-10-12T17:15:04+00:00",evidence_id="child-unsent")
    with pytest.raises(LedgerError,match="LATEST_PROVEN_UNSUBMITTED"):
        retry(ledger,original,"stale-fork",quantity=1)
    with pytest.raises(LedgerError,match="FORECAST_ALREADY_RESERVED"):
        retry(ledger,original,"implicit-retry",quantity=1,retry_of_reservation_id=None)
    assert len(ledger.snapshot().reservations)==2
