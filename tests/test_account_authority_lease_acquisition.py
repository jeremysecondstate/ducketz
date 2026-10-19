"""Lease acquisition duration excludes its own uncommitted receipt recovery."""
from datetime import datetime, timedelta, timezone
import socket
import sqlite3

import pytest

from ml.account_gameplan.authority import (
    AccountAuthority, AccountSnapshot, AuthorityBinding, AuthorityError, ReservationRequest,
)


ACCOUNT = "a" * 64
SOURCES = {"Atlas": "b" * 64}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 19, 20, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def tick(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    authority = AccountAuthority(tmp_path / "authority.sqlite3",
        AuthorityBinding(ACCOUNT, "Atlas", {"Atlas": ("AAPL",)}), clock=clock)
    lease = authority.acquire_lease(coordinator_id="Atlas", holder_id="own-operation")
    authority.publish_snapshot(lease.token, AccountSnapshot(
        "snapshot", ACCOUNT, "generation", "2026-10-19", SOURCES,
        clock().isoformat(), "10000", "100", "95", "0", "1000",
        {"AAPL": "0"}, {"AAPL": "1000"}, {"AAPL": 100},
        evidence_fingerprint="d" * 64, identity_before=ACCOUNT, identity_after=ACCOUNT, complete=True))
    return authority, lease.token, clock


def request():
    return ReservationRequest("order", "snapshot", "generation", "2026-10-19", SOURCES,
        "Atlas", "AAPL", "1h", "forecast", "BUY", 6, "10", "10", "native-reservation", "native-allocation")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Lease acquisition regressions use synthetic local state only")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def pending_exact_receipt(authority, token, clock):
    reservation = authority.reserve(token, request())
    callbacks = []
    def returned_after_expiry(gate):
        callbacks.append(reservation.reservation_id)
        gate()
        clock.tick(61)
        return "1001"
    with pytest.raises(AuthorityError, match="CURRENT_FENCED_LEASE_REQUIRED"):
        authority.submit(token, reservation.reservation_id, returned_after_expiry)
    assert authority.reservation(reservation.reservation_id).status == "SUBMITTING"
    return reservation, callbacks


@pytest.mark.parametrize("ttl", [1, 60])
@pytest.mark.parametrize("delay", [.5, 80.547168])
@pytest.mark.parametrize("phase", ["receipt-recovery", "lease-event"])
def test_slow_acquisition_returns_current_fence_and_preserves_exact_response(env, monkeypatch, ttl, delay, phase):
    authority, old_token, clock = env
    original, callbacks = pending_exact_receipt(authority, old_token, clock)
    receipt = authority.submission_receipt(original.reservation_id)
    recover, record_event = authority._recover_submission_receipts, authority._event
    def slow_recover(db, now, epoch):
        result = recover(db, now, epoch)
        if phase == "receipt-recovery":
            clock.now += timedelta(seconds=delay)
        return result
    def slow_event(db, now, kind, payload):
        record_event(db, now, kind, payload)
        if phase == "lease-event" and kind == "lease-acquired":
            clock.now += timedelta(seconds=delay)
    monkeypatch.setattr(authority, "_recover_submission_receipts", slow_recover)
    monkeypatch.setattr(authority, "_event", slow_event)
    lease = authority.acquire_lease(coordinator_id="Atlas", holder_id="next-own-operation", ttl_seconds=ttl)
    assert datetime.fromisoformat(lease.expires_at) == clock() + timedelta(seconds=ttl)
    renewed = authority.renew_lease(lease.token, ttl_seconds=ttl)
    assert (renewed.token, renewed.epoch, renewed.holder_id) == (lease.token, lease.epoch, lease.holder_id)
    assert lease.epoch == 2 and lease.token != old_token
    recovered = authority.reservation(original.reservation_id)
    assert recovered.status == "SUBMITTED"
    assert recovered.broker_order_id == "1001"
    assert recovered.filled_quantity == 0
    assert recovered.request == original.request
    assert authority.submission_receipt(original.reservation_id) == receipt
    assert callbacks == [original.reservation_id]
    with authority._transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='lease-acquired'").fetchone()[0] == 2


@pytest.mark.parametrize("failure", ["receipt-validation", "backward-clock"])
def test_failed_acquisition_rolls_back_recovered_receipt_and_new_fence(env, monkeypatch, failure):
    authority, old_token, clock = env
    original, callbacks = pending_exact_receipt(authority, old_token, clock)
    with authority._transaction() as db:
        before_lease = dict(db.execute("SELECT * FROM lease").fetchone())
        before_clock = db.execute("SELECT value FROM metadata WHERE key='clock'").fetchone()[0]
        before_events = [tuple(row) for row in db.execute("SELECT * FROM events ORDER BY sequence")]
    recover = authority._recover_submission_receipts
    def failed_recover(db, now, epoch):
        recover(db, now, epoch)
        if failure == "receipt-validation":
            raise AuthorityError("SUBMISSION_RECEIPT_SCOPE_OR_TIME_MISMATCH")
        clock.now -= timedelta(seconds=1)
    monkeypatch.setattr(authority, "_recover_submission_receipts", failed_recover)
    expected = "SUBMISSION_RECEIPT_SCOPE_OR_TIME_MISMATCH" if failure == "receipt-validation" else "AUTHORITY_CLOCK_MOVED_BACKWARDS"
    with pytest.raises(AuthorityError, match=expected):
        authority.acquire_lease(coordinator_id="Atlas", holder_id="next-own-operation")
    assert authority.reservation(original.reservation_id).status == "SUBMITTING"
    assert authority.reservation(original.reservation_id).broker_order_id is None
    assert callbacks == [original.reservation_id]
    with authority._transaction() as db:
        assert dict(db.execute("SELECT * FROM lease").fetchone()) == before_lease
        assert db.execute("SELECT value FROM metadata WHERE key='clock'").fetchone()[0] == before_clock
        assert [tuple(row) for row in db.execute("SELECT * FROM events ORDER BY sequence")] == before_events


def test_active_owner_and_committed_expiry_remain_enforced(env):
    authority, token, clock = env
    with pytest.raises(AuthorityError, match="LEASE_ALREADY_OWNED"):
        authority.acquire_lease(coordinator_id="Atlas", holder_id="different-operation")
    clock.tick(61)
    with pytest.raises(AuthorityError, match="CURRENT_FENCED_LEASE_REQUIRED"):
        authority.renew_lease(token)
    replacement = authority.acquire_lease(coordinator_id="Atlas", holder_id="next-own-operation")
    with pytest.raises(AuthorityError, match="CURRENT_FENCED_LEASE_REQUIRED"):
        authority.renew_lease(token)
    assert replacement.epoch == 2


def test_slow_receipt_recovery_keeps_new_fence_unpublished_and_excludes_competing_write(env, monkeypatch):
    authority, old_token, clock = env
    original, callbacks = pending_exact_receipt(authority, old_token, clock)
    recover = authority._recover_submission_receipts
    def slow_recover(db, now, epoch):
        result = recover(db, now, epoch)
        clock.tick(81)
        with sqlite3.connect(authority.path, timeout=0, isolation_level=None) as competing:
            assert competing.execute("SELECT token FROM lease").fetchone()[0] == old_token
            assert competing.execute("SELECT status FROM reservations WHERE id=?",
                (original.reservation_id,)).fetchone()[0] == "SUBMITTING"
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competing.execute("BEGIN IMMEDIATE")
        return result
    monkeypatch.setattr(authority, "_recover_submission_receipts", slow_recover)
    lease = authority.acquire_lease(coordinator_id="Atlas", holder_id="next-own-operation")
    assert datetime.fromisoformat(lease.expires_at) == clock() + timedelta(seconds=60)
    assert authority.reservation(original.reservation_id).status == "SUBMITTED"
    with pytest.raises(AuthorityError, match="LEASE_ALREADY_OWNED"):
        authority.acquire_lease(coordinator_id="Atlas", holder_id="competing-operation")
    assert callbacks == [original.reservation_id]
