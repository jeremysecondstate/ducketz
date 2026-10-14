"""Temporary-database actual-capacity checks; no providers or order adapter."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
from threading import Barrier

import pytest

from ml.account_gameplan.authority import (
    AccountAuthority, AccountSnapshot, AuthorityBinding, AuthorityError,
    BrokerReservation, Fill, IntradayCapacitySnapshot, IntradayReservationRequest,
    OrderEvidence, ReservationRequest, SubmissionNotStarted,
)


ACCOUNT = "a" * 64
ATLAS = ("AAPL","AMZN","GOOG","MU","NVDA","SNDK","COST","CROX","PATH","TWST","IONQ")
SCOUT = ("MSFT",)
BINDING = AuthorityBinding(ACCOUNT,"pc-original",{"pc-original":ATLAS,"pc-new":SCOUT})
REFERENCE = "2026-10-12T17:00:00+00:00"
ISSUED = "2026-10-12T17:15:02+00:00"
TARGET = "2026-10-12T17:45:00+00:00"


class Clock:
    def __init__(self):
        self.value = datetime(2026,10,12,17,15,3,tzinfo=timezone.utc)
    def __call__(self):
        return self.value
    def tick(self,seconds=1):
        self.value += timedelta(seconds=seconds)


def snapshot(clock, **changes):
    result = IntradayCapacitySnapshot("actual-1",ACCOUNT,"2026-10-12",clock().isoformat(),"100",
        {s:20 for s in (*ATLAS,*SCOUT)},evidence_fingerprint="b"*64,
        identity_before=ACCOUNT,identity_after=ACCOUNT,complete=True)
    return replace(result,**changes)


def request(key="new-forecast", **changes):
    forecast = hashlib.sha256(key.encode()).hexdigest()
    result = IntradayReservationRequest(key,"actual-1",ACCOUNT,"2026-10-12","pc-original","AAPL",
        forecast,"BUY",10,"10","10","native-"+key,"allocation-"+key,
        forecast,"d"*64,ISSUED,REFERENCE,TARGET,"intraday-15m-open-to-third-close-not-down-v1",ATLAS)
    return replace(result,**changes)


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    authority = AccountAuthority(tmp_path/"authority.sqlite3",BINDING,clock=clock)
    lease = authority.acquire_lease(coordinator_id="pc-original",holder_id="offline-test")
    authority.publish_intraday_snapshot(lease.token,snapshot(clock))
    return authority,lease.token,clock


def submit(authority, token, req, broker_id="101"):
    reserved = authority.reserve_intraday(token,req)
    calls = []
    def callback(gate):
        gate(); calls.append(1)
        return broker_id
    accepted = authority.submit(token,reserved.reservation_id,callback)
    assert calls == [1]
    return accepted


def test_full_available_cash_with_no_gameplan_plan_or_reserve_fraction(env):
    authority,token,_ = env
    reserved = authority.reserve_intraday(token,request())
    assert reserved.request["quantity"] == 10
    assert authority.available_intraday_capacity(token,"actual-1")["cash_available"] == "0"
    with closing(sqlite3.connect(authority.path)) as db:
        assert db.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0


def test_simultaneous_stream_requests_cannot_double_spend(env):
    authority,token,clock = env
    other = AccountAuthority(authority.path,BINDING,clock=clock)
    barrier = Barrier(2)
    def attempt(owner,key):
        barrier.wait()
        try:
            return owner.reserve_intraday(token,request(key,quantity=8)).status
        except AuthorityError as exc:
            return str(exc)
    with ThreadPoolExecutor(2) as pool:
        one = pool.submit(attempt,authority,"one")
        two = pool.submit(attempt,other,"two")
        assert sorted((one.result(),two.result())) == ["ACCOUNT_AVAILABLE_CASH_EXHAUSTED","RESERVED"]


def test_gameplan_and_intraday_commitments_share_cash_without_shared_budgets(env):
    authority,token,clock = env
    sources = {"pc-original":"b"*64,"pc-new":"c"*64}
    legacy_snapshot = AccountSnapshot("gp-1",ACCOUNT,"daily-generation","2026-10-12",sources,
        clock().isoformat(),"10000","100","95","0","13000",
        {s:"0" for s in (*ATLAS,*SCOUT)},{s:"1500" for s in (*ATLAS,*SCOUT)},
        {s:20 for s in (*ATLAS,*SCOUT)},evidence_fingerprint="e"*64,
        identity_before=ACCOUNT,identity_after=ACCOUNT,complete=True)
    authority.publish_snapshot(token,legacy_snapshot)
    legacy = ReservationRequest("gp-order","gp-1","daily-generation","2026-10-12",sources,
        "pc-original","AAPL","1h","gp-forecast","BUY",2,"10","10","gp-native","gp-allocation")
    authority.reserve(token,legacy)
    assert authority.available_intraday_capacity(token,"actual-1")["cash_available"] == "80"
    authority.reserve_intraday(token,request(quantity=8))
    with pytest.raises(AuthorityError,match="BUDGET_EXHAUSTED"):
        authority.reserve(token,replace(legacy,idempotency_key="gp-two",forecast_id="gp-two",
            native_reservation_id="gp-native-two",native_allocation_id="gp-allocation-two",quantity=1))


def test_actual_cash_has_no_single_order_equity_cap(env):
    authority,token,clock = env
    clock.tick()
    authority.publish_intraday_snapshot(token,snapshot(clock,snapshot_id="large",cash_available="50000"))
    assert authority.reserve_intraday(token,request(snapshot_id="large",quantity=5000)).status == "RESERVED"


@pytest.mark.parametrize("changes,reason",[
    ({"participant_id":"pc-new"},"INTRADAY_SCOPE"),
    ({"symbol":"MSFT"},"INTRADAY_SCOPE"),
    ({"local_symbols":ATLAS[:-1]},"INTRADAY_SCOPE"),
    ({"local_symbols":ATLAS+("AAPL",)},"INTRADAY_SCOPE"),
    ({"stream":"scout-15m"},"INTRADAY_SCOPE"),
    ({"horizon":"1h"},"INTRADAY_SCOPE"),
    ({"account_fingerprint":"f"*64},"INTRADAY_SCOPE"),
    ({"target_close_at":"2026-10-12T17:30:00+00:00"},"EXACT_INTRADAY_TARGET"),
    ({"reference_at":"2026-10-12T17:00:01+00:00"},"EXACT_INTRADAY_TARGET"),
    ({"issued_at":"2026-10-12T17:14:59+00:00"},"EXACT_INTRADAY_TARGET"),
    ({"target_version":"legacy-positive"},"EXACT_INTRADAY_TARGET"),
    ({"issued_at":"2026-10-12T17:15:04+00:00"},"NOT_CURRENT"),
    ({"action_date":"2026-10-11"},"NOT_CURRENT"),
    ({"quantity":0},"WHOLE_NONNEGATIVE"),
    ({"quantity":True},"WHOLE_NONNEGATIVE"),
    ({"forecast_sha256":"not-a-hash"},"SHA256"),
    ({"limit_price":"10.001"},"CENT_ROUNDED"),
])
def test_exact_scope_proof_and_target_reject_wrong_inputs(env,changes,reason):
    authority,token,_ = env
    with pytest.raises(AuthorityError,match=reason):
        authority.reserve_intraday(token,request(**changes))
    assert authority.read_state()["reservations"] == []


def test_snapshot_must_be_exact_complete_and_latest(env):
    authority,token,clock = env
    for changes in ({"held_shares":{"AAPL":20}},{"identity_after":"f"*64},{"complete":False},
                    {"broker_pending":(BrokerReservation("1","AAPL","SELL",1,0,"1","0"),)}):
        clock.tick()
        with pytest.raises(AuthorityError):
            authority.publish_intraday_snapshot(token,snapshot(clock,snapshot_id="invalid",**changes))
    authority.publish_intraday_snapshot(token,snapshot(clock,snapshot_id="newest"))
    with pytest.raises(AuthorityError,match="LATEST_INTRADAY"):
        authority.reserve_intraday(token,request())
    clock.tick(61)
    fresh_lease = authority.acquire_lease(coordinator_id="pc-original",holder_id="restart")
    with pytest.raises(AuthorityError,match="STALE_OR_FUTURE"):
        authority.available_intraday_capacity(fresh_lease.token,"newest")


def test_unknown_submission_retains_commitment_without_guessing_or_resending(env):
    authority,token,_ = env
    reserved = authority.reserve_intraday(token,request(quantity=6))
    calls=[]
    def lost(gate):
        gate(); calls.append(1)
        raise TimeoutError("no returned identity")
    with pytest.raises(TimeoutError):
        authority.submit(token,reserved.reservation_id,lost)
    assert authority.reservation(reserved.reservation_id).status == "UNKNOWN"
    assert authority.available_intraday_capacity(token,"actual-1")["cash_available"] == "40"
    assert authority.submit(token,reserved.reservation_id,lost).status == "UNKNOWN"
    assert calls == [1]
    assert authority.reserve_intraday(token,request("independent",symbol="AMZN",quantity=4)).status == "RESERVED"


def test_exact_broker_pending_overlap_is_not_deducted_twice(env):
    authority,token,clock = env
    accepted = submit(authority,token,request(quantity=6))
    clock.tick()
    authority.reconcile(token,OrderEvidence("working",accepted.reservation_id,ACCOUNT,"101",clock().isoformat(),"WORKING",6,0,6,(),True))
    clock.tick()
    authority.publish_intraday_snapshot(token,snapshot(clock,snapshot_id="broker-net",cash_available="40",
        broker_pending=(BrokerReservation("101","AAPL","BUY",6,0,"60","60"),)))
    assert authority.available_intraday_capacity(token,"broker-net")["cash_available"] == "40"
    authority.reserve_intraday(token,request("all-remainder",snapshot_id="broker-net",quantity=4))


def test_partial_fill_cancellation_keeps_filled_cash_until_newer_balance(env):
    authority,token,clock = env
    accepted = submit(authority,token,request(quantity=6))
    clock.tick()
    fill = Fill("actual-fill",2,"10",clock().isoformat())
    authority.reconcile(token,OrderEvidence("cancelled",accepted.reservation_id,ACCOUNT,"101",clock().isoformat(),"CANCELLED",6,2,0,(fill,),True))
    assert authority.available_intraday_capacity(token,"actual-1")["cash_available"] == "80"
    clock.tick()
    authority.publish_intraday_snapshot(token,snapshot(clock,snapshot_id="caught-up",cash_available="80",accounted_fills={"101":2}))
    assert authority.available_intraday_capacity(token,"caught-up")["cash_available"] == "80"


def test_sales_use_actual_account_shares_and_never_create_cash(env):
    authority,token,_ = env
    sell = authority.reserve_intraday(token,request(side="SELL",quantity=20))
    capacity = authority.available_intraday_capacity(token,"actual-1")
    assert capacity["eligible_shares"]["AAPL"] == 0
    assert capacity["cash_available"] == "100"
    with pytest.raises(AuthorityError,match="INVENTORY_EXHAUSTED"):
        authority.reserve_intraday(token,request("another-sale",side="SELL",quantity=1))
    assert authority.reservation(sell.reservation_id).filled_quantity == 0


def test_restart_idempotency_frozen_request_and_exact_native_ids(env):
    authority,token,clock = env
    req = request(quantity=2)
    before = asdict(req)
    first = authority.reserve_intraday(token,req)
    reopened = AccountAuthority(authority.path,BINDING,clock=clock)
    assert reopened.reserve_intraday(token,req) == first
    assert asdict(req) == before
    with pytest.raises(AuthorityError,match="IDEMPOTENCY"):
        reopened.reserve_intraday(token,replace(req,forecast_snapshot_sha256="f"*64))
    with pytest.raises(AuthorityError,match="ALREADY_REGISTERED"):
        reopened.reserve_intraday(token,request("duplicate-native",quantity=2,native_reservation_id=req.native_reservation_id))
    assert len(reopened.read_state()["reservations"]) == 1


def test_target_expiry_is_rechecked_at_the_submission_gate(env):
    authority,token,clock = env
    reserved = authority.reserve_intraday(token,request(quantity=1))
    clock.value = datetime(2026,10,12,17,44,59,tzinfo=timezone.utc)
    lease = authority.acquire_lease(coordinator_id="pc-original",holder_id="later")
    authority.publish_intraday_snapshot(lease.token,snapshot(clock,snapshot_id="later"))
    # The original snapshot cannot be silently changed to obtain a later send.
    with pytest.raises(AuthorityError,match="LATEST_INTRADAY"):
        authority.submit(lease.token,reserved.reservation_id,lambda gate:"102")
    current = authority.reserve_intraday(lease.token,request("current",snapshot_id="later",quantity=1))
    sent=[]
    def delayed(gate):
        clock.tick()
        gate()
        sent.append(1)
        return "103"
    with pytest.raises(AuthorityError,match="INTRADAY_FORECAST_NOT_CURRENT"):
        authority.submit(lease.token,current.reservation_id,delayed)
    assert sent == []


def test_legacy_request_bytes_remain_unchanged(env):
    assert "stream" not in asdict(ReservationRequest("a","b","c","2026-10-12",{},"p","AAPL","1h","f","BUY",1,"10","10","n","l"))
    authority,token,clock = env
    authority.publish_intraday_snapshot(token,snapshot(clock))
    with closing(sqlite3.connect(authority.path)) as db:
        payload = json.loads(db.execute("SELECT payload FROM intraday_capacity_snapshots").fetchone()[0])
        assert "generation" not in payload and "cash_budget" not in payload


def retry_request(previous, key="retry", **changes):
    values = dict(forecast_id=previous.request["forecast_id"],forecast_sha256=previous.request["forecast_sha256"],
        native_reservation_id="native-"+key,native_allocation_id="allocation-"+key,
        retry_of_native_reservation_id=previous.request["native_reservation_id"])
    values.update(changes)
    return request(key,**values)


def test_only_explicit_no_post_retry_creates_new_immutable_attempts(env):
    authority,token,clock=env
    original=authority.reserve_intraday(token,request(quantity=5))
    authority.abandon_unsubmitted(token,original.reservation_id)
    clock.tick()
    authority.publish_intraday_snapshot(token,snapshot(clock,snapshot_id="fresh"))
    retry=authority.reserve_intraday(token,retry_request(original,snapshot_id="fresh",quantity=4,limit_price="11",exposure_price="11"))
    assert retry.reservation_id != original.reservation_id
    assert retry.request["retry_of_native_reservation_id"] == original.request["native_reservation_id"]
    assert authority.reservation(original.reservation_id).request["quantity"]==5
    assert authority.reservation(original.reservation_id).status=="NOT_SUBMITTED"
    assert retry.request["quantity"]==4 and retry.request["limit_price"]=="11"
    assert authority.reserve_intraday(token,retry_request(original,snapshot_id="fresh",quantity=4,limit_price="11",exposure_price="11")) == retry
    authority.abandon_unsubmitted(token,retry.reservation_id)
    for index in range(4):
        retry=authority.reserve_intraday(token,retry_request(retry,"next-"+str(index),snapshot_id="fresh",quantity=1))
        authority.abandon_unsubmitted(token,retry.reservation_id)
    assert len(authority.read_state()["reservations"])==6


@pytest.mark.parametrize("status",["RESERVED","UNKNOWN","SUBMITTED","PARTIAL","FILLED","CANCELLED"])
def test_account_retry_never_replaces_possible_submission_or_fills(env,status):
    authority,token,clock=env
    original=authority.reserve_intraday(token,request(quantity=2))
    if status=="UNKNOWN":
        def lose(gate):
            gate();raise TimeoutError()
        with pytest.raises(TimeoutError):
            authority.submit(token,original.reservation_id,lose)
    elif status!="RESERVED":
        def accepted(gate):
            gate();return "101"
        original=authority.submit(token,original.reservation_id,accepted)
        if status!="SUBMITTED":
            clock.tick()
            filled=2 if status=="FILLED" else 1 if status=="PARTIAL" else 0
            fills=(Fill("actual",filled,"10",clock().isoformat()),) if filled else ()
            authority.reconcile(token,OrderEvidence("status",original.reservation_id,ACCOUNT,"101",clock().isoformat(),
                status,2,filled,2-filled if status=="PARTIAL" else 0,fills,True))
    with pytest.raises(AuthorityError,match="LATEST_PROVEN_UNSUBMITTED"):
        authority.reserve_intraday(token,retry_request(original,quantity=1))
    assert len(authority.read_state()["reservations"])==1


@pytest.mark.parametrize("changes",[
    {"symbol":"AMZN"},{"side":"SELL"},{"forecast_id":"e"*64,"forecast_sha256":"e"*64},
    {"forecast_snapshot_sha256":"f"*64},{"retry_of_native_reservation_id":"unrelated-native"},
])
def test_account_retry_requires_same_immutable_forecast_and_exact_native_predecessor(env,changes):
    authority,token,_=env
    original=authority.reserve_intraday(token,request(quantity=1))
    authority.abandon_unsubmitted(token,original.reservation_id)
    with pytest.raises(AuthorityError,match="PREDECESSOR_SCOPE"):
        authority.reserve_intraday(token,retry_request(original,quantity=1,**changes))


def test_stale_account_retry_link_cannot_fork_or_bypass_default_duplicate_rule(env):
    authority,token,_=env
    original=authority.reserve_intraday(token,request(quantity=1))
    authority.abandon_unsubmitted(token,original.reservation_id)
    child=authority.reserve_intraday(token,retry_request(original,quantity=1))
    authority.abandon_unsubmitted(token,child.reservation_id)
    with pytest.raises(AuthorityError,match="LATEST_PROVEN_UNSUBMITTED"):
        authority.reserve_intraday(token,retry_request(original,"stale-fork",quantity=1))
    with pytest.raises(AuthorityError,match="ALREADY_REGISTERED"):
        authority.reserve_intraday(token,retry_request(original,"implicit-retry",quantity=1,retry_of_native_reservation_id=None))
    assert len(authority.read_state()["reservations"])==2


def test_default_intraday_request_serialization_has_no_retry_marker(env):
    authority,token,_=env
    reserved=authority.reserve_intraday(token,request(quantity=1))
    assert "retry_of_native_reservation_id" not in reserved.request


def delayed_acceptance(authority, token, clock, *, key="slow-response", broker_id="101"):
    reserved = authority.reserve_intraday(token, request(key, quantity=1))
    calls = []
    def response(gate):
        gate()
        calls.append(broker_id)
        clock.tick(61)  # The exact successful response outlives its valid send lease.
        return broker_id
    with pytest.raises(AuthorityError, match="CURRENT_FENCED_LEASE_REQUIRED"):
        authority.submit(token, reserved.reservation_id, response)
    assert calls == [broker_id]
    return reserved


def test_exact_returned_id_survives_expired_lease_and_next_acquire_promotes(env):
    authority, token, clock = env
    reserved = delayed_acceptance(authority, token, clock)
    before = authority.reservation(reserved.reservation_id)
    receipt = authority.submission_receipt(reserved.reservation_id)
    assert before.status == "SUBMITTING" and before.broker_order_id is None
    assert receipt["broker_order_id"] == "101" and receipt["account_fingerprint"] == ACCOUNT
    assert receipt["received_at"] == clock().isoformat()
    assert datetime.fromisoformat(receipt["gate_at"]) < datetime.fromisoformat(receipt["received_at"])
    with pytest.raises(AuthorityError, match="CURRENT_FENCED_LEASE_REQUIRED"):
        authority.recover_submission_receipts(token)
    assert authority.reservation(reserved.reservation_id) == before  # Reading is not promotion.

    reopened = AccountAuthority(authority.path, BINDING, clock=clock)
    new = reopened.acquire_lease(coordinator_id="pc-original", holder_id="restart")
    accepted = reopened.reservation(reserved.reservation_id)
    assert accepted.status == "SUBMITTED" and accepted.broker_order_id == "101"
    assert accepted.request == before.request and accepted.filled_quantity == 0
    assert reopened.submission_receipt(reserved.reservation_id) == receipt
    assert reopened.recover_submission_receipts(new.token) == []
    assert reopened.submit(new.token, reserved.reservation_id,
        lambda gate: pytest.fail("a durable exact accepted receipt must never POST again")) == accepted
    reopened.publish_intraday_snapshot(new.token, snapshot(clock, snapshot_id="after-response"))
    assert reopened.available_intraday_capacity(new.token, "after-response")["cash_available"] == "90"


def test_receipt_survives_crash_before_fenced_projection(env, monkeypatch):
    authority, token, _ = env
    reserved = authority.reserve_intraday(token, request(quantity=1))
    promote = authority.recover_submission_receipts
    def crash(_):
        raise SystemExit("synthetic crash after exact receipt")
    monkeypatch.setattr(authority, "recover_submission_receipts", crash)
    with pytest.raises(SystemExit, match="after exact receipt"):
        authority.submit(token, reserved.reservation_id, lambda gate: (gate(), "101")[1])
    receipt = authority.submission_receipt(reserved.reservation_id)
    assert receipt["broker_order_id"] == "101"
    assert authority.reservation(reserved.reservation_id).status == "SUBMITTING"
    monkeypatch.setattr(authority, "recover_submission_receipts", promote)
    recovered = authority.recover_submission_receipts(token)
    assert [r.broker_order_id for r in recovered] == ["101"]
    assert authority.submission_receipt(reserved.reservation_id) == receipt


@pytest.mark.parametrize("response", [None, 101, "not-an-order-id"])
def test_invalid_response_or_missing_gate_has_no_invented_receipt(env, response):
    authority, token, _ = env
    reserved = authority.reserve_intraday(token, request(quantity=1))
    with pytest.raises(AuthorityError, match="EXACT_BROKER_ORDER_ID_AND_FINAL_GATE"):
        authority.submit(token, reserved.reservation_id, lambda gate: (gate(), response)[1])
    assert authority.submission_receipt(reserved.reservation_id) is None
    assert authority.reservation(reserved.reservation_id).status == "UNKNOWN"


def test_exact_id_without_final_gate_is_not_a_recoverable_receipt(env):
    authority, token, _ = env
    reserved = authority.reserve_intraday(token, request(quantity=1))
    with pytest.raises(AuthorityError, match="FINAL_GATE_REQUIRED"):
        authority.submit(token, reserved.reservation_id, lambda gate: "101")
    assert authority.submission_receipt(reserved.reservation_id) is None


def test_unknown_response_has_no_receipt_and_remains_committed_after_restart(env):
    authority, token, clock = env
    reserved = authority.reserve_intraday(token, request(quantity=1))
    def lost(gate):
        gate()
        raise TimeoutError("synthetic lost response")
    with pytest.raises(TimeoutError):
        authority.submit(token, reserved.reservation_id, lost)
    assert authority.submission_receipt(reserved.reservation_id) is None
    clock.tick(61)
    lease = authority.acquire_lease(coordinator_id="pc-original", holder_id="restart")
    current = authority.reservation(reserved.reservation_id)
    assert current.status == "UNKNOWN" and current.broker_order_id is None
    assert authority.recover_submission_receipts(lease.token) == []


def test_conflicting_exact_response_is_retained_without_rebinding_an_order(env):
    authority, token, _ = env
    accepted = submit(authority, token, request("first", quantity=1))
    second = authority.reserve_intraday(token, request("second", quantity=1))
    with pytest.raises(AuthorityError, match="ALREADY_BOUND"):
        authority.submit(token, second.reservation_id, lambda gate: (gate(), "101")[1])
    assert authority.submission_receipt(second.reservation_id)["broker_order_id"] == "101"
    assert authority.reservation(second.reservation_id).broker_order_id is None
    assert authority.reservation(second.reservation_id).status == "SUBMITTING"
    assert authority.reservation(accepted.reservation_id).broker_order_id == "101"


def test_tampered_receipt_is_rejected_without_releasing_or_promoting(env):
    authority, token, clock = env
    reserved = delayed_acceptance(authority, token, clock)
    with authority._transaction() as db:
        value = json.loads(db.execute("SELECT payload FROM submission_receipts").fetchone()[0])
        db.execute("UPDATE submission_receipts SET payload=?", (json.dumps(value | {"broker_order_id":"102"}),))
    with pytest.raises(AuthorityError, match="IMMUTABLE_SUBMISSION_RECEIPT_CHANGED"):
        authority.submission_receipt(reserved.reservation_id)
    with pytest.raises(AuthorityError, match="IMMUTABLE_SUBMISSION_RECEIPT_CHANGED"):
        authority.acquire_lease(coordinator_id="pc-original", holder_id="restart")
    assert authority.reservation(reserved.reservation_id).status == "SUBMITTING"
    assert authority.reservation(reserved.reservation_id).broker_order_id is None


def test_receipt_recovery_preserves_terminal_partial_fills(env):
    authority, token, clock = env
    accepted = submit(authority, token, request(quantity=2))
    receipt = authority.submission_receipt(accepted.reservation_id)
    clock.tick()
    fill = Fill("one-real-fill", 1, "10", clock().isoformat())
    authority.reconcile(token, OrderEvidence("terminal", accepted.reservation_id, ACCOUNT, "101",
        clock().isoformat(), "CANCELLED", 2, 1, 0, (fill,), True))
    clock.tick(61)
    lease = authority.acquire_lease(coordinator_id="pc-original", holder_id="restart")
    current = authority.reservation(accepted.reservation_id)
    assert current.status == "CANCELLED" and current.filled_quantity == 1
    assert current.broker_order_id == "101"
    assert authority.recover_submission_receipts(lease.token) == []
    assert authority.submission_receipt(accepted.reservation_id) == receipt


def test_audited_no_send_after_lease_expiry_is_durable_but_release_remains_fenced(env):
    authority, token, clock = env
    reserved = authority.reserve_intraday(token, request(quantity=1))
    posts = []
    def stopped_before_send(gate):
        clock.tick(61)
        raise SubmissionNotStarted("audited before-POST check failed")
    with pytest.raises(AuthorityError, match="OUTCOME_REQUIRES_RECONCILIATION"):
        authority.submit(token, reserved.reservation_id, stopped_before_send)
    receipt = authority.submission_receipt(reserved.reservation_id)
    assert receipt["outcome"] == "NOT_SUBMITTED" and receipt["gate_used"] is False
    assert receipt["broker_order_id"] is receipt["gate_at"] is receipt["gate_epoch"] is None
    assert receipt["no_send_error_type"] == "SubmissionNotStarted"
    assert receipt["received_at"] == clock().isoformat()
    assert posts == []
    assert authority.reservation(reserved.reservation_id).status == "SUBMITTING"
    with pytest.raises(AuthorityError, match="CURRENT_FENCED_LEASE_REQUIRED"):
        authority.recover_submission_receipts(token)
    lease = authority.acquire_lease(coordinator_id="pc-original", holder_id="fresh-recovery")
    rejected = authority.reservation(reserved.reservation_id)
    assert rejected.status == "NOT_SUBMITTED" and rejected.broker_order_id is None
    assert rejected.filled_quantity == 0 and rejected.request == reserved.request
    assert authority.submission_receipt(reserved.reservation_id) == receipt
    assert authority.recover_submission_receipts(lease.token) == []
    authority.publish_intraday_snapshot(lease.token, snapshot(clock, snapshot_id="fresh-no-send"))
    assert authority.available_intraday_capacity(lease.token, "fresh-no-send")["cash_available"] == "100"
    retry = authority.reserve_intraday(lease.token,
        retry_request(rejected, snapshot_id="fresh-no-send", quantity=1, limit_price="11", exposure_price="11"))
    assert retry.reservation_id != reserved.reservation_id
    assert authority.reservation(reserved.reservation_id).status == "NOT_SUBMITTED"


def test_no_send_under_current_fence_keeps_audited_receipt(env):
    authority, token, _ = env
    reserved = authority.reserve_intraday(token, request(quantity=1))
    def stop(gate):
        raise SubmissionNotStarted("audited pre-POST stop")
    with pytest.raises(SubmissionNotStarted):
        authority.submit(token, reserved.reservation_id, stop)
    assert authority.submission_receipt(reserved.reservation_id)["outcome"] == "NOT_SUBMITTED"
    assert authority.reservation(reserved.reservation_id).status == "NOT_SUBMITTED"


@pytest.mark.parametrize("gate_used,error", [(False,TimeoutError),(True,SubmissionNotStarted)])
def test_generic_or_after_gate_failure_never_manufactures_no_send_receipt(env,gate_used,error):
    authority, token, clock = env
    reserved = authority.reserve_intraday(token, request(quantity=1))
    def uncertain(gate):
        if gate_used:
            gate()
        clock.tick(61)
        raise error("no authoritative no-POST evidence")
    with pytest.raises((AuthorityError,TimeoutError)):
        authority.submit(token, reserved.reservation_id, uncertain)
    assert authority.submission_receipt(reserved.reservation_id) is None
    lease = authority.acquire_lease(coordinator_id="pc-original", holder_id="fresh-recovery")
    assert authority.recover_submission_receipts(lease.token) == []
    assert authority.reservation(reserved.reservation_id).status == "SUBMITTING"
    authority.publish_intraday_snapshot(lease.token, snapshot(clock, snapshot_id="still-uncertain"))
    assert authority.available_intraday_capacity(lease.token, "still-uncertain")["cash_available"] == "90"
