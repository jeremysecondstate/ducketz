"""Offline authority invariants; temporary SQLite files and fake callbacks only."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier

import pytest

from ml.account_gameplan.authority import (
    AccountAuthority, AccountSnapshot, AuthorityBinding, AuthorityError,
    BrokerReservation, Fill, OrderEvidence, ReservationRequest, SubmissionNotStarted,
)


ACCOUNT = "a" * 64
SOURCES = {"Atlas":"b"*64,"Scout":"c"*64}
BINDING = AuthorityBinding(ACCOUNT,"Atlas",{"Atlas":("AAPL",),"Scout":("MSFT",)})


class Clock:
    def __init__(self):
        self.now = datetime(2026,10,2,15,tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def tick(self, seconds=1):
        self.now += timedelta(seconds=seconds)
        return self.now.isoformat()


def snapshot(clock, **changes):
    base = AccountSnapshot("snapshot-1",ACCOUNT,"generation-1","2026-10-02",SOURCES,
        clock().isoformat(),"10000","100","95","0","1000",
        {"AAPL":"0","MSFT":"0"},{"AAPL":"1000","MSFT":"1000"},
        {"AAPL":100,"MSFT":100},evidence_fingerprint="d"*64,
        identity_before=ACCOUNT,identity_after=ACCOUNT,complete=True)
    return replace(base,**changes)


def request(key="order-1", *, symbol="AAPL", **changes):
    base = ReservationRequest(key,"snapshot-1","generation-1","2026-10-02",SOURCES,
        "Atlas" if symbol=="AAPL" else "Scout",symbol,"1h",key+"-forecast","BUY",6,"10","10",
        key+"-native-reservation",key+"-native-allocation")
    return replace(base,**changes)


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    authority = AccountAuthority(tmp_path/"account.sqlite3",BINDING,clock=clock)
    lease = authority.acquire_lease(coordinator_id="Atlas",holder_id="test-process")
    authority.publish_snapshot(lease.token,snapshot(clock))
    return authority,lease.token,clock


def submitted(authority,token,req,broker_id="1001"):
    reservation = authority.reserve(token,req)
    def callback(gate):
        assert authority.reservation(reservation.reservation_id).status=="SUBMITTING"
        gate()
        return broker_id
    return authority.submit(token,reservation.reservation_id,callback)


def evidence(reservation,clock,*,status="FILLED",filled=None,remaining=0,identity="evidence-1",fills=None):
    quantity = reservation.request["quantity"]
    filled = quantity if filled is None else filled
    if fills is None:
        fills = (Fill("fill-1",filled,"10",clock().isoformat()),) if filled else ()
    return OrderEvidence(identity,reservation.reservation_id,ACCOUNT,reservation.broker_order_id,
        clock().isoformat(),status,quantity,filled,remaining,fills,complete=True)


def test_two_connections_compete_for_one_cash_pool_atomically(env):
    authority,token,clock = env
    other = AccountAuthority(authority.path,BINDING,clock=clock)
    barrier = Barrier(2)
    def attempt(owner, req):
        barrier.wait()
        try:
            return owner.reserve(token,req).status
        except AuthorityError as exc:
            return str(exc)
    with ThreadPoolExecutor(2) as pool:
        one = pool.submit(attempt,authority,request("atlas"))
        two = pool.submit(attempt,other,request("scout",symbol="MSFT"))
        assert sorted([one.result(),two.result()])==["ACCOUNT_RESERVATION_BUDGET_EXHAUSTED","RESERVED"]
    assert len(authority.read_state()["reservations"])==1


def test_idempotent_reservation_and_submission_never_repeat_callback(env):
    authority,token,_ = env
    req = request()
    first = authority.reserve(token,req)
    assert authority.reserve(token,req)==first
    with pytest.raises(AuthorityError,match="IDEMPOTENCY_KEY"):
        authority.reserve(token,replace(req,quantity=5))
    count = []
    def callback(gate):
        gate(); count.append(1); return "1001"
    accepted = authority.submit(token,first.reservation_id,callback)
    assert authority.submit(token,first.reservation_id,callback)==accepted
    assert len(count)==1


@pytest.mark.parametrize("gate_called", [False, True])
def test_audited_presend_stop_releases_only_before_send_gate(env, gate_called):
    authority, token, _ = env
    reservation = authority.reserve(token, request())
    def callback(gate):
        if gate_called:
            gate()
        raise SubmissionNotStarted("audited adapter source gate rejected")
    with pytest.raises(AuthorityError if gate_called else SubmissionNotStarted) as failure:
        authority.submit(token, reservation.reservation_id, callback)
    if gate_called:
        assert not isinstance(failure.value, SubmissionNotStarted)
    assert authority.reservation(reservation.reservation_id).status == ("UNKNOWN" if gate_called else "NOT_SUBMITTED")


@pytest.mark.parametrize("field,value",[("coordinator_id","Scout"),("account_fingerprint","e"*64),
    ("participants",{"Atlas":("MSFT",),"Scout":("AAPL",)})])
def test_binding_is_immutable(env,field,value):
    authority,_,clock=env
    with pytest.raises(AuthorityError,match="IMMUTABLE"):
        AccountAuthority(authority.path,replace(BINDING,**{field:value}),clock=clock)


def test_only_reviewed_host_can_acquire_and_active_lease_is_exclusive(env):
    authority,_,_=env
    with pytest.raises(AuthorityError,match="NOT_THE_REVIEWED"):
        authority.acquire_lease(coordinator_id="Scout",holder_id="other")
    with pytest.raises(AuthorityError,match="ALREADY_OWNED"):
        authority.acquire_lease(coordinator_id="Atlas",holder_id="other")


def test_expired_token_cannot_renew_reserve_or_submit_and_new_epoch_fences_it(env):
    authority,old,clock=env
    first=authority.reserve(old,request())
    clock.tick(61)
    for operation in (lambda:authority.renew_lease(old),lambda:authority.reserve(old,request("next")),
                      lambda:authority.submit(old,first.reservation_id,lambda gate:"1001")):
        with pytest.raises(AuthorityError,match="CURRENT_FENCED_LEASE"):
            operation()
    new=authority.acquire_lease(coordinator_id="Atlas",holder_id="reviewed-restart")
    assert new.epoch==2 and new.token!=old
    with pytest.raises(AuthorityError,match="CURRENT_FENCED_LEASE"):
        authority.reserve(old,request("again"))
    assert authority.reservation(first.reservation_id).status=="RESERVED"


def test_lease_renewal_does_not_extend_snapshot_freshness(env):
    authority,token,clock=env
    clock.tick(50); authority.renew_lease(token)
    clock.tick(11)
    with pytest.raises(AuthorityError,match="SNAPSHOT_STALE"):
        authority.reserve(token,request())


def test_backwards_authority_clock_rejected(env):
    authority,token,clock=env
    clock.tick(-1)
    with pytest.raises(AuthorityError,match="CLOCK_MOVED_BACKWARDS"):
        authority.reserve(token,request())


@pytest.mark.parametrize("changes,reason",[
    ({"identity_after":"f"*64},"COHERENT"),({"account_fingerprint":"f"*64},"COHERENT"),
    ({"complete":False},"COHERENT"),({"source_fingerprints":{"Atlas":"b"*64}},"ALL_REGISTERED"),
    ({"held_shares":{"AAPL":100}},"SYMBOL_UNION"),({"cash_budget":"95.01"},"RISK_LIMIT"),
    ({"gross_budget":"13001"},"RISK_LIMIT"),({"symbol_budgets":{"AAPL":"1501","MSFT":"1000"}},"RISK_LIMIT"),
    ({"action_date":"2026-10-03"},"ACTION_DATE_NOT_CURRENT"),
])
def test_snapshot_binding_completeness_and_risk_limits_fail_closed(env,changes,reason):
    authority,token,clock=env
    clock.tick()
    with pytest.raises(AuthorityError,match=reason):
        authority.publish_snapshot(token,snapshot(clock,snapshot_id="next",**changes))


@pytest.mark.parametrize("changes",[
    {"generation":"other"},{"source_fingerprints":{"Atlas":"e"*64,"Scout":"c"*64}},
])
def test_action_date_source_binding_is_immutable(env,changes):
    authority,token,clock=env
    clock.tick()
    with pytest.raises(AuthorityError,match="SOURCE_BINDING_IS_IMMUTABLE"):
        authority.publish_snapshot(token,snapshot(clock,snapshot_id="next",**changes))


@pytest.mark.parametrize("changes,reason",[
    ({"generation":"other"},"SOURCE_BINDING_MISMATCH"),
    ({"source_fingerprints":{"Atlas":"e"*64,"Scout":"c"*64}},"SOURCE_BINDING_MISMATCH"),
    ({"participant_id":"Scout"},"PARTICIPANT_SCOPE"),({"horizon":"2h"},"PARTICIPANT_SCOPE"),
    ({"snapshot_id":"not-current"},"LATEST_ACCOUNT_SNAPSHOT"),
    ({"quantity":True},"WHOLE_NONNEGATIVE"),({"limit_price":"NaN"},"FINITE_NONNEGATIVE"),
    ({"exposure_price":"9"},"EXPOSURE_PRICE"),({"quantity":51},"SINGLE_ORDER_LIMIT"),
])
def test_request_bindings_and_existing_limits(env,changes,reason):
    authority,token,_=env
    with pytest.raises(AuthorityError,match=reason):
        authority.reserve(token,request(**changes))


def test_duplicate_forecast_or_native_reservation_does_not_create_new_order(env):
    authority,token,_=env
    req=request(quantity=2)
    authority.reserve(token,req)
    for changed in (replace(req,idempotency_key="new-key"),
                    replace(req,idempotency_key="new-key",forecast_id="different")):
        with pytest.raises(AuthorityError,match="ALREADY_REGISTERED"):
            authority.reserve(token,changed)


def test_unknown_submission_retains_reserve_and_blocks_even_preexisting_other_submit(env):
    authority,token,_=env
    first=authority.reserve(token,request(quantity=3))
    second=authority.reserve(token,request("second",symbol="MSFT",quantity=3))
    def uncertain(gate):
        gate(); raise TimeoutError("ambiguous acceptance")
    with pytest.raises(TimeoutError): authority.submit(token,first.reservation_id,uncertain)
    assert authority.reservation(first.reservation_id).status=="UNKNOWN"
    with pytest.raises(AuthorityError,match="UNCERTAIN_SUBMISSION"):
        authority.reserve(token,request("third",quantity=1))
    with pytest.raises(AuthorityError,match="UNCERTAIN_SUBMISSION"):
        authority.submit(token,second.reservation_id,lambda gate:"1002")
    with pytest.raises(AuthorityError,match="CANNOT_BE_ABANDONED"):
        authority.abandon_unsubmitted(token,first.reservation_id)
    called=[]
    assert authority.submit(token,first.reservation_id,lambda gate:called.append(1)).status=="UNKNOWN"
    assert not called


def test_expired_lease_inside_callback_cannot_pass_gate_or_release_reserve(env):
    authority,token,clock=env
    first=authority.reserve(token,request())
    sent=[]
    def stale(gate):
        clock.tick(61); gate(); sent.append(1); return "1001"
    with pytest.raises(AuthorityError,match="CURRENT_FENCED"):
        authority.submit(token,first.reservation_id,stale)
    assert not sent and authority.reservation(first.reservation_id).status=="SUBMITTING"
    fresh=authority.acquire_lease(coordinator_id="Atlas",holder_id="reviewed-restart")
    assert authority.submit(fresh.token,first.reservation_id,lambda gate:sent.append(1)).status=="SUBMITTING"
    assert not sent


def test_missing_final_gate_or_ambiguous_broker_id_is_unknown(env):
    authority,token,_=env
    first=authority.reserve(token,request())
    with pytest.raises(AuthorityError,match="EXACT_BROKER_ORDER"):
        authority.submit(token,first.reservation_id,lambda gate:"1001")
    assert authority.reservation(first.reservation_id).status=="UNKNOWN"


def test_unsubmitted_abandon_releases_once_but_never_reuses_forecast(env):
    authority,token,_=env
    first=authority.reserve(token,request())
    result=authority.abandon_unsubmitted(token,first.reservation_id)
    assert authority.abandon_unsubmitted(token,first.reservation_id)==result
    assert authority.reserve(token,request("second",symbol="MSFT")).status=="RESERVED"


def test_explicit_broker_identity_avoids_double_cash_reservation(env):
    authority,token,clock=env
    first=submitted(authority,token,request())
    clock.tick()
    # Broker cash already subtracts this exact pending $60. Do not subtract it twice.
    pending=BrokerReservation("1001","AAPL","BUY",6,0,"60","60")
    authority.publish_snapshot(token,snapshot(clock,snapshot_id="next",cash_available="40",cash_budget="38",
        gross_budget="940",symbol_budgets={"AAPL":"940","MSFT":"1000"},broker_pending=(pending,)))
    assert authority.reserve(token,request("second",symbol="MSFT",quantity=3,snapshot_id="next")).status=="RESERVED"
    with pytest.raises(AuthorityError,match="BUDGET_EXHAUSTED"):
        authority.reserve(token,request("third",symbol="MSFT",quantity=1,snapshot_id="next"))
    assert authority.reservation(first.reservation_id).status=="SUBMITTED"


def test_working_cash_overlap_never_erases_unaccounted_fill_cost(env):
    authority, token, clock = env
    first = submitted(authority, token, request())
    clock.tick()
    authority.reconcile(token, evidence(first, clock, status="PARTIAL", filled=2, remaining=4))
    clock.tick()
    authority.publish_snapshot(token, snapshot(clock, snapshot_id="partial",
        cash_available="40", cash_budget="38", gross_budget="900",
        symbol_budgets={"AAPL": "900", "MSFT": "1000"},
        broker_pending=(BrokerReservation("1001", "AAPL", "BUY", 4, 2, "100", "100"),)))
    # The working reservation can overlap only the remaining four shares.
    # The $20 fill has not been explicitly proven in this balance snapshot.
    with pytest.raises(AuthorityError, match="BUDGET_EXHAUSTED"):
        authority.reserve(token, request("after-partial", symbol="MSFT", quantity=2, snapshot_id="partial"))


def test_external_pending_cash_is_not_mistaken_for_local_identity_overlap(env):
    authority,token,clock=env
    submitted(authority,token,request(quantity=3))
    clock.tick()
    external=BrokerReservation("9999","MSFT","BUY",6,0,"60","60")
    authority.publish_snapshot(token,snapshot(clock,snapshot_id="next",cash_available="40",cash_budget="38",
        gross_budget="940",symbol_budgets={"AAPL":"1000","MSFT":"940"},broker_pending=(external,)))
    with pytest.raises(AuthorityError,match="BUDGET_EXHAUSTED"):
        authority.reserve(token,request("second",symbol="MSFT",quantity=1,snapshot_id="next"))


def test_mismatched_broker_reservation_quantity_rejected(env):
    authority,token,clock=env
    submitted(authority,token,request())
    clock.tick()
    with pytest.raises(AuthorityError,match="DOES_NOT_MATCH"):
        authority.publish_snapshot(token,snapshot(clock,snapshot_id="next",broker_pending=(
            BrokerReservation("1001","AAPL","BUY",5,0,"50","50"),)))


def test_partial_cancel_releases_only_unfilled_cash_and_fills_stay_charged(env):
    authority,token,clock=env
    first=submitted(authority,token,request())
    clock.tick()
    partial=evidence(first,clock,status="PARTIAL",filled=2,remaining=4)
    authority.reconcile(token,partial)
    with pytest.raises(AuthorityError,match="BUDGET_EXHAUSTED"):
        authority.reserve(token,request("cannot-spend-filled",symbol="MSFT",quantity=4))
    clock.tick()
    cancelled=replace(partial,evidence_id="cancelled",observed_at=clock().isoformat(),status="CANCELLED",remaining_quantity=0)
    authority.reconcile(token,cancelled)
    authority.reconcile(token,cancelled)  # exact replay does not release twice
    assert authority.reserve(token,request("after-cancel",symbol="MSFT",quantity=7)).status=="RESERVED"
    with pytest.raises(AuthorityError,match="BUDGET_EXHAUSTED"):
        authority.reserve(token,request("too-much",symbol="MSFT",quantity=1))


def test_filled_buy_remains_charged_until_newer_snapshot_accounts_for_exact_fills(env):
    authority,token,clock=env
    first=submitted(authority,token,request())
    clock.tick()
    authority.reconcile(token,evidence(first,clock))
    with pytest.raises(AuthorityError,match="BUDGET_EXHAUSTED"):
        authority.reserve(token,request("too-soon",symbol="MSFT",quantity=4))
    with pytest.raises(AuthorityError,match="NEWER_ACCOUNT_SNAPSHOT"):
        authority.publish_snapshot(token,snapshot(clock,snapshot_id="new",accounted_fills={"1001":6}))
    clock.tick()
    authority.publish_snapshot(token,snapshot(clock,snapshot_id="new",cash_available="40",cash_budget="38",
        held_shares={"AAPL":106,"MSFT":100},accounted_fills={"1001":6}))
    assert authority.reserve(token,request("after-account",symbol="MSFT",quantity=3,snapshot_id="new")).status=="RESERVED"


def test_sale_fill_never_increases_cash_without_new_account_snapshot(env):
    authority,token,clock=env
    first=submitted(authority,token,request(side="SELL",quantity=10))
    clock.tick()
    authority.reconcile(token,evidence(first,clock))
    with pytest.raises(AuthorityError,match="BUDGET_EXHAUSTED"):
        authority.reserve(token,request("unavailable-proceeds",symbol="MSFT",quantity=10))
    clock.tick()
    authority.publish_snapshot(token,snapshot(clock,snapshot_id="new",cash_available="200",cash_budget="190",
        held_shares={"AAPL":90,"MSFT":100},accounted_fills={"1001":10}))
    assert authority.reserve(token,request("actual-proceeds",symbol="MSFT",quantity=10,snapshot_id="new")).status=="RESERVED"


def test_partial_sale_cancel_preserves_filled_inventory_debit_without_manufacturing_shares(env):
    authority,token,clock=env
    clock.tick()
    authority.publish_snapshot(token,snapshot(clock,snapshot_id="new",held_shares={"AAPL":10,"MSFT":100}))
    first=submitted(authority,token,request(side="SELL",quantity=10,snapshot_id="new"))
    clock.tick()
    partial=evidence(first,clock,status="PARTIAL",filled=3,remaining=7)
    authority.reconcile(token,partial)
    with pytest.raises(AuthorityError,match="INVENTORY_EXHAUSTED"):
        authority.reserve(token,request("another",side="SELL",quantity=1,snapshot_id="new"))
    clock.tick()
    authority.reconcile(token,replace(partial,evidence_id="cancelled",observed_at=clock().isoformat(),status="CANCELLED",remaining_quantity=0))
    assert authority.reserve(token,request("remaining",side="SELL",quantity=7,snapshot_id="new")).status=="RESERVED"
    with pytest.raises(AuthorityError,match="INVENTORY_EXHAUSTED"):
        authority.reserve(token,request("extra",side="SELL",quantity=1,snapshot_id="new"))


@pytest.mark.parametrize("changes,reason",[
    ({"account_fingerprint":"e"*64},"SAME_ACCOUNT"),({"broker_order_id":"9999"},"EXACT_PREVIOUSLY_BOUND"),
    ({"complete":False},"SAME_ACCOUNT"),({"order_quantity":5},"DOES_NOT_BALANCE"),
    ({"remaining_quantity":1},"DISAGREE"),({"status":"REJECTED"},"DISAGREE"),
])
def test_reconciliation_requires_exact_balanced_identity(env,changes,reason):
    authority,token,clock=env
    first=submitted(authority,token,request())
    clock.tick()
    with pytest.raises(AuthorityError,match=reason):
        authority.reconcile(token,replace(evidence(first,clock),**changes))


def test_reconciliation_cannot_reduce_or_rewrite_previous_fills(env):
    authority,token,clock=env
    first=submitted(authority,token,request())
    clock.tick()
    initial=evidence(first,clock,status="PARTIAL",filled=2,remaining=4)
    authority.reconcile(token,initial)
    clock.tick()
    with pytest.raises(AuthorityError,match="DOES_NOT_BALANCE"):
        authority.reconcile(token,evidence(first,clock,status="PARTIAL",filled=1,remaining=5,identity="regression"))
    with pytest.raises(AuthorityError,match="CHANGED_OR_DISAPPEARED"):
        authority.reconcile(token,evidence(first,clock,status="PARTIAL",filled=2,remaining=4,identity="rewritten",
            fills=(replace(initial.fills[0],price="9.50"),)))


def test_terminal_order_cannot_reopen(env):
    authority,token,clock=env
    first=submitted(authority,token,request())
    clock.tick()
    authority.reconcile(token,evidence(first,clock,status="CANCELLED",filled=0))
    clock.tick()
    with pytest.raises(AuthorityError,match="TERMINAL_ORDER"):
        authority.reconcile(token,evidence(first,clock,status="WORKING",filled=0,remaining=6,identity="reopened"))
