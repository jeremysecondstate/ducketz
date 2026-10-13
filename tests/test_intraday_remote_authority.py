"""Shared-account remote fencing with temporary books and synthetic evidence."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from ml.account_gameplan.authority import (
    AccountAuthority, AuthorityError, Fill, OrderEvidence,
)
from tests.test_intraday_account_authority import (
    ACCOUNT, BINDING, SCOUT, Clock, request, snapshot,
)


WIRE_HASH = "e" * 64
IDENTITY = {"participant_id": "pc-new", "request_sha256": WIRE_HASH}
CLAIM = {**IDENTITY, "claim_id": "durable-scout-send-1"}


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    authority = AccountAuthority(tmp_path / "authority.sqlite3", BINDING, clock=clock)
    lease = authority.acquire_lease(coordinator_id="pc-original", holder_id="offline-remote-test")
    authority.publish_intraday_snapshot(lease.token, snapshot(clock))
    return authority, lease.token, clock


def remote_request(key="scout-request", **changes):
    return request(key, participant_id="pc-new", stream="scout-15m", symbol="MSFT",
                   local_symbols=SCOUT, **changes)


def reserve(authority, token, **changes):
    return authority.reserve_remote_intraday(token, remote_request(**changes), request_sha256=WIRE_HASH)


def claim(authority, token, reserved, **changes):
    values = {**CLAIM, "expires_at": "2026-10-12T17:15:30+00:00", **changes}
    return authority.claim_remote_send(token, reserved.reservation_id, **values)


def renewed(authority, token):
    authority.release_lease(token)
    return authority.acquire_lease(coordinator_id="pc-original", holder_id="next-operation")


def test_remote_and_atlas_compete_for_same_pool_without_earmarks(env):
    authority, token, clock = env
    second = AccountAuthority(authority.path, BINDING, clock=clock)
    barrier = Barrier(2)

    def attempt(remote):
        barrier.wait()
        try:
            if remote:
                reserve(second, token, quantity=7)
            else:
                authority.reserve_intraday(token, request(quantity=7))
            return "RESERVED"
        except AuthorityError as exc:
            return str(exc)

    with ThreadPoolExecutor(2) as pool:
        a, b = pool.submit(attempt, True), pool.submit(attempt, False)
        assert sorted((a.result(), b.result())) == ["ACCOUNT_AVAILABLE_CASH_EXHAUSTED", "RESERVED"]
    assert authority.available_intraday_capacity(token, "actual-1")["cash_available"] == "30"


@pytest.mark.parametrize("changes", [
    {"participant_id": "pc-original"}, {"stream": "atlas-15m"}, {"symbol": "AAPL"},
    {"local_symbols": ("AAPL",)}, {"horizon": "1h"}, {"account_fingerprint": "f" * 64},
])
def test_remote_entry_point_requires_exact_scout_scope(env, changes):
    authority, token, _ = env
    with pytest.raises(AuthorityError, match="INTRADAY_SCOPE"):
        authority.reserve_remote_intraday(token, replace(remote_request(), **changes), request_sha256=WIRE_HASH)
    assert authority.read_state()["reservations"] == []


def test_wire_request_hash_is_immutable_at_admission(env):
    authority, token, _ = env
    reserved = reserve(authority, token)
    assert reserve(authority, token) == reserved
    with pytest.raises(AuthorityError, match="IDEMPOTENCY_KEY"):
        authority.reserve_remote_intraday(token, remote_request(), request_sha256="f" * 64)
    with pytest.raises(AuthorityError, match="INTRADAY_SCOPE"):
        authority.reserve_intraday(token, remote_request())


def test_claim_is_once_only_across_connections_and_lost_response(env):
    authority, token, clock = env
    reserved = reserve(authority, token, quantity=6)
    second = AccountAuthority(authority.path, BINDING, clock=clock)
    barrier = Barrier(2)

    def attempt(owner):
        barrier.wait()
        try:
            claim(owner, token, reserved)
            return "CLAIMED"
        except AuthorityError as exc:
            return str(exc)

    with ThreadPoolExecutor(2) as pool:
        a, b = pool.submit(attempt, authority), pool.submit(attempt, second)
        assert sorted((a.result(), b.result())) == ["CLAIMED", "REMOTE_SEND_ALREADY_CLAIMED"]
    retained = authority.remote_claim(reserved.reservation_id, **IDENTITY)
    lease = renewed(authority, token)
    assert lease.epoch > retained["authority_epoch"]
    reopened = AccountAuthority(authority.path, BINDING, clock=clock)
    assert reopened.remote_claim(reserved.reservation_id, **IDENTITY) == retained
    with pytest.raises(AuthorityError, match="ALREADY_CLAIMED"):
        claim(reopened, lease.token, reserved)
    assert reopened.reservation(reserved.reservation_id).status == "SUBMITTING"
    assert reopened.available_intraday_capacity(lease.token, "actual-1")["cash_available"] == "40"


def test_local_submit_and_abandon_cannot_touch_remote_reservation(env):
    authority, token, _ = env
    reserved = reserve(authority, token)
    calls = []
    with pytest.raises(AuthorityError, match="CANNOT_USE_LOCAL_SUBMIT"):
        authority.submit(token, reserved.reservation_id, lambda gate: calls.append(1))
    with pytest.raises(AuthorityError, match="REQUIRES_REMOTE_RECOVERY"):
        authority.abandon_unsubmitted(token, reserved.reservation_id)
    assert calls == []
    assert authority.reservation(reserved.reservation_id) == reserved


@pytest.mark.parametrize("damage", [
    {"participant_id": "pc-original"}, {"request_sha256": "f" * 64},
])
def test_cross_participant_or_request_cannot_claim_read_or_release(env, damage):
    authority, token, _ = env
    reserved = reserve(authority, token)
    identity = {**IDENTITY, **damage}
    for operation in (
        lambda: authority.remote_claim(reserved.reservation_id, **identity),
        lambda: claim(authority, token, reserved, **damage),
        lambda: authority.release_remote_unsubmitted(token, reserved.reservation_id,
                    **identity, proof_sha256="f" * 64),
    ):
        with pytest.raises(AuthorityError, match="SCOPE_MISMATCH"):
            operation()
    assert authority.reservation(reserved.reservation_id) == reserved


def test_preclaim_revocation_releases_once_with_immutable_proof(env):
    authority, token, _ = env
    reserved = reserve(authority, token)
    release = lambda proof: authority.release_remote_unsubmitted(token, reserved.reservation_id,
                **IDENTITY, proof_sha256=proof)
    result = release("f" * 64)
    assert result.status == "NOT_SUBMITTED" and release("f" * 64) == result
    assert authority.available_intraday_capacity(token, "actual-1")["cash_available"] == "100"
    with pytest.raises(AuthorityError, match="RECEIPT_CONFLICT"):
        release("b" * 64)
    with pytest.raises(AuthorityError, match="REQUIRES_RESERVED"):
        claim(authority, token, reserved)


def test_postclaim_not_sent_or_expiry_can_never_release_capacity(env):
    authority, token, clock = env
    reserved = reserve(authority, token, quantity=6)
    claim(authority, token, reserved)
    clock.tick(61)
    lease = authority.acquire_lease(coordinator_id="pc-original", holder_id="restarted-service")
    authority.publish_intraday_snapshot(lease.token, snapshot(clock, snapshot_id="fresh"))
    with pytest.raises(AuthorityError, match="CANNOT_BE_RELEASED"):
        authority.release_remote_unsubmitted(lease.token, reserved.reservation_id, **IDENTITY, proof_sha256="f" * 64)
    assert authority.mark_remote_unknown(lease.token, reserved.reservation_id, **CLAIM).status == "UNKNOWN"
    assert authority.available_intraday_capacity(lease.token, "fresh")["cash_available"] == "40"


def test_fresh_capture_rechecks_reserved_capacity_before_first_claim(env):
    authority, token, clock = env
    reserved = reserve(authority, token, quantity=6)
    clock.tick()
    authority.publish_intraday_snapshot(token, snapshot(clock, snapshot_id="less-cash", cash_available="50"))
    with pytest.raises(AuthorityError, match="NO_LONGER_AVAILABLE"):
        claim(authority, token, reserved, snapshot_id="less-cash")
    assert authority.remote_claim(reserved.reservation_id, **IDENTITY) is None
    assert authority.reservation(reserved.reservation_id).status == "RESERVED"
    clock.tick()
    authority.publish_intraday_snapshot(token, snapshot(clock, snapshot_id="fresh-cash", cash_available="60"))
    assert claim(authority, token, reserved, snapshot_id="fresh-cash")["snapshot_id"] == "fresh-cash"


def test_exact_recovery_after_new_epoch_keeps_fill_cost_and_terminal_result(env):
    authority, token, clock = env
    reserved = reserve(authority, token, quantity=6)
    claimed = claim(authority, token, reserved)
    lease = renewed(authority, token)
    with pytest.raises(AuthorityError, match="CURRENT_FENCED_LEASE"):
        authority.record_remote_submission(token, reserved.reservation_id, **CLAIM, broker_order_id="201")
    accepted = authority.record_remote_submission(lease.token, reserved.reservation_id, **CLAIM, broker_order_id="201")
    assert accepted.status == "SUBMITTED"
    clock.tick()
    fill = Fill("scout-fill", 2, "10", clock().isoformat())
    evidence = OrderEvidence("scout-cancel", reserved.reservation_id, ACCOUNT, "201", clock().isoformat(),
                             "CANCELLED", 6, 2, 0, (fill,), complete=True)
    terminal = authority.remote_reconcile(lease.token, evidence, **CLAIM)
    assert terminal.status == "CANCELLED" and terminal.filled_quantity == 2
    assert authority.record_remote_submission(lease.token, reserved.reservation_id, **CLAIM, broker_order_id="201") == terminal
    assert authority.mark_remote_unknown(lease.token, reserved.reservation_id, **CLAIM) == terminal
    assert authority.available_intraday_capacity(lease.token, "actual-1")["cash_available"] == "80"
    assert authority.remote_claim(reserved.reservation_id, **IDENTITY) == claimed
    clock.tick()
    authority.publish_intraday_snapshot(lease.token, snapshot(clock, snapshot_id="accounted", cash_available="80", accounted_fills={"201": 2}))
    assert authority.available_intraday_capacity(lease.token, "accounted")["cash_available"] == "80"


def test_coordinator_exact_reconciliation_still_accounts_remote_order(env):
    authority, token, clock = env
    reserved = reserve(authority, token, quantity=2)
    claim(authority, token, reserved)
    authority.record_remote_submission(token, reserved.reservation_id, **CLAIM, broker_order_id="201")
    clock.tick()
    evidence = OrderEvidence("coordinator-status", reserved.reservation_id, ACCOUNT, "201", clock().isoformat(),
                             "WORKING", 2, 0, 2, (), complete=True)
    assert authority.reconcile(token, evidence).status == "WORKING"


def test_remote_evidence_and_receipts_are_bound_to_exact_claim(env):
    authority, token, clock = env
    reserved = reserve(authority, token, quantity=2)
    claim(authority, token, reserved)
    wrong = {**CLAIM, "claim_id": "another-send"}
    with pytest.raises(AuthorityError, match="CLAIM_SCOPE_MISMATCH"):
        authority.record_remote_submission(token, reserved.reservation_id, **wrong, broker_order_id="201")
    authority.record_remote_submission(token, reserved.reservation_id, **CLAIM, broker_order_id="201")
    with pytest.raises(AuthorityError, match="ORDER_ID_CONFLICT"):
        authority.record_remote_submission(token, reserved.reservation_id, **CLAIM, broker_order_id="202")
    clock.tick()
    evidence = OrderEvidence("wrong-claim", reserved.reservation_id, ACCOUNT, "201", clock().isoformat(),
                             "WORKING", 2, 0, 2, (), complete=True)
    with pytest.raises(AuthorityError, match="CLAIM_SCOPE_MISMATCH"):
        authority.remote_reconcile(token, evidence, **wrong)
    assert authority.reservation(reserved.reservation_id).status == "SUBMITTED"


def test_exact_broker_id_cannot_alias_an_atlas_order(env):
    authority, token, _ = env
    native = authority.reserve_intraday(token, request(quantity=1))

    def send(gate):
        gate()
        return "201"

    authority.submit(token, native.reservation_id, send)
    reserved = reserve(authority, token, quantity=2)
    claim(authority, token, reserved)
    with pytest.raises(AuthorityError, match="ALREADY_BOUND"):
        authority.record_remote_submission(token, reserved.reservation_id, **CLAIM, broker_order_id="201")
    assert authority.reservation(reserved.reservation_id).status == "SUBMITTING"


def test_sales_use_shared_eligible_shares_without_horizon_partition(env):
    authority, token, _ = env
    reserved = reserve(authority, token, side="SELL", quantity=20)
    claim(authority, token, reserved)
    assert authority.available_intraday_capacity(token, "actual-1")["eligible_shares"]["MSFT"] == 0
    assert authority.available_intraday_capacity(token, "actual-1")["cash_available"] == "100"
