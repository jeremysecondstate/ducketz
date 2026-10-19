"""Initial grants survive local preparation; all stores and broker IO are fixtures."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import socket
import sqlite3

import pytest

from test_intraday_account_execution import env
from ml.account_gameplan import authority as authority_module
from ml.account_gameplan.authority import (
    AccountAuthority, AuthorityBinding, AuthorityError, IntradayCapacitySnapshot,
    IntradayReservationRequest, lease_acquisition_failure_evidence,
)
from ml.intraday import account_execution as execution
from ml.intraday.prediction import ContractError


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Lease acquisition regressions cannot call a provider")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 19, 17, 15, 3, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def owner(tmp_path, monkeypatch):
    clock = Clock()
    authority = AccountAuthority(tmp_path / "own-authority.sqlite3",
        AuthorityBinding("a" * 64, "pc-original", {"pc-original": ("AAPL",)}), clock=clock)
    monkeypatch.setattr(authority_module.time, "monotonic", lambda: clock().timestamp())
    return authority, clock


def acquire(authority, **kwargs):
    return authority.acquire_lease(coordinator_id="pc-original", holder_id="fixture-owner", **kwargs)


def rows(authority, sql):
    with sqlite3.connect(authority.path) as db:
        return db.execute(sql).fetchall()


def delayed_commit(monkeypatch, authority, clock, *, seconds=80, count=1, after=None):
    original = authority._transaction
    remaining = [count]

    @contextmanager
    def transaction():
        delayed = remaining[0] > 0
        with original() as db:
            yield db
            if delayed:
                remaining[0] -= 1
                clock.advance(seconds)
        if delayed and after is not None:
            after()

    monkeypatch.setattr(authority, "_transaction", transaction)


def test_slow_receipt_preparation_gets_full_grant_after_work(owner, monkeypatch):
    authority, clock = owner
    original = authority._recover_submission_receipts

    def slow(db, now, epoch):
        result = original(db, now, epoch)
        clock.advance(80)
        return result

    monkeypatch.setattr(authority, "_recover_submission_receipts", slow)
    lease = acquire(authority)
    assert lease.epoch == 1
    assert datetime.fromisoformat(lease.expires_at) == clock() + timedelta(seconds=60)
    assert authority.renew_lease(lease.token).epoch == 1


@pytest.mark.parametrize("seconds", [60, 80])
def test_commit_exhaustion_reacquires_only_before_return(owner, monkeypatch, seconds):
    authority, clock = owner
    delayed_commit(monkeypatch, authority, clock, seconds=seconds)
    lease = acquire(authority)
    assert lease.epoch == 2
    assert datetime.fromisoformat(lease.expires_at) == clock() + timedelta(seconds=60)
    assert len(rows(authority, "SELECT * FROM events WHERE kind='lease-acquired'")) == 2
    assert authority.renew_lease(lease.token).epoch == 2


def test_repeated_slow_commits_are_bounded_and_diagnosed(owner, monkeypatch):
    authority, clock = owner
    delayed_commit(monkeypatch, authority, clock, count=10)
    with pytest.raises(AuthorityError, match="LEASE_ACQUISITION_EXPIRED_BEFORE_RETURN") as failed:
        acquire(authority)
    assert rows(authority, "SELECT epoch FROM lease") == [(3,)]
    evidence = lease_acquisition_failure_evidence(failed.value)
    assert evidence["attempt"] == 3 and evidence["elapsed_seconds"] == 240
    assert [p["elapsed_seconds"] for p in evidence["phases"] if p["stage"] == "transaction_commit"] == [80, 80, 80]
    exported = execution.execution_failure_evidence(failed.value)
    assert exported["root_cause"]["message"] == "LEASE_ACQUISITION_EXPIRED_BEFORE_RETURN"
    assert exported["root_cause"]["lease_acquisition"] == evidence
    token = rows(authority, "SELECT token FROM lease")[0][0]
    assert token not in json.dumps(exported)
    assert "fixture-owner" not in json.dumps(exported)


def test_competing_holder_after_slow_commit_is_not_overwritten(owner, monkeypatch):
    authority, clock = owner
    competitor = AccountAuthority(authority.path,
        AuthorityBinding("a" * 64, "pc-original", {"pc-original": ("AAPL",)}), clock=clock)
    gained = []
    delayed_commit(monkeypatch, authority, clock, after=lambda: gained.append(
        competitor.acquire_lease(coordinator_id="pc-original", holder_id="other-owner")))
    with pytest.raises(AuthorityError, match="LEASE_ALREADY_OWNED"):
        acquire(authority)
    assert len(gained) == 1 and gained[0].epoch == 2
    assert rows(authority, "SELECT token,holder,epoch FROM lease") == [(gained[0].token, "other-owner", 2)]


@pytest.mark.parametrize("stage", ["recovery", "commit"])
def test_backwards_clock_never_returns_a_grant(owner, monkeypatch, stage):
    authority, clock = owner
    if stage == "commit":
        delayed_commit(monkeypatch, authority, clock, seconds=-1)
    else:
        monkeypatch.setattr(authority, "_recover_submission_receipts", lambda *args: clock.advance(-1))
    with pytest.raises(AuthorityError, match="AUTHORITY_CLOCK_MOVED_BACKWARDS"):
        acquire(authority)
    assert len(rows(authority, "SELECT * FROM lease")) == (stage == "commit")


@pytest.mark.parametrize("ttl", [0, 61, True, 1.5, "60"])
def test_invalid_ttl_does_no_projection_or_writes(owner, monkeypatch, ttl):
    authority, _ = owner
    monkeypatch.setattr(authority, "_recover_submission_receipts", lambda *args: pytest.fail("Invalid TTL reached projection"))
    with pytest.raises(AuthorityError, match="LEASE_DURATION_MUST_BE_1_TO_60_SECONDS"):
        acquire(authority, ttl_seconds=ttl)
    assert rows(authority, "SELECT * FROM lease") == []


@pytest.mark.parametrize("ttl", [1, 60])
def test_valid_ttl_limits_preserved(owner, ttl):
    authority, clock = owner
    lease = acquire(authority, ttl_seconds=ttl)
    assert datetime.fromisoformat(lease.expires_at) == clock() + timedelta(seconds=ttl)
    clock.advance(ttl)
    with pytest.raises(AuthorityError, match="CURRENT_FENCED_LEASE_REQUIRED"):
        authority.renew_lease(lease.token)


def retained_receipt(authority, clock):
    lease = acquire(authority)
    authority.publish_intraday_snapshot(lease.token, IntradayCapacitySnapshot(
        "own-snapshot", "a" * 64, "2026-10-19", clock().isoformat(), "100", {"AAPL": 20},
        evidence_fingerprint="b" * 64, identity_before="a" * 64, identity_after="a" * 64, complete=True))
    request = IntradayReservationRequest("own-intention", "own-snapshot", "a" * 64, "2026-10-19",
        "pc-original", "AAPL", "c" * 64, "BUY", 1, "10", "10", "own-native", "own-allocation",
        "c" * 64, "d" * 64, "2026-10-19T17:15:02+00:00", "2026-10-19T17:00:00+00:00",
        "2026-10-19T17:45:00+00:00", "intraday-15m-open-to-third-close-not-down-v1", ("AAPL",))
    reservation = authority.reserve_intraday(lease.token, request)

    def fixture_response(gate):
        gate()
        clock.advance(61)
        return "101"

    with pytest.raises(AuthorityError, match="CURRENT_FENCED_LEASE_REQUIRED"):
        authority.submit(lease.token, reservation.reservation_id, fixture_response)
    return authority.reservation(reservation.reservation_id), authority.submission_receipt(reservation.reservation_id)


def test_retried_grant_keeps_exact_receipt_and_promotes_only_once(owner, monkeypatch):
    authority, clock = owner
    before, receipt = retained_receipt(authority, clock)
    assert before.status == "SUBMITTING" and before.broker_order_id is None
    delayed_commit(monkeypatch, authority, clock)
    lease = acquire(authority)
    assert lease.epoch == 3
    after = authority.reservation(before.reservation_id)
    assert after.status == "SUBMITTED" and after.broker_order_id == "101"
    assert after.request == before.request and after.filled_quantity == before.filled_quantity == 0
    assert authority.submission_receipt(before.reservation_id) == receipt
    assert len(rows(authority, "SELECT * FROM events WHERE kind='submitted'")) == 1
    assert authority.submit(lease.token, before.reservation_id,
        lambda gate: pytest.fail("Recovered exact identity must never resubmit")) == after


def test_projection_failure_rolls_back_grant_and_preserves_receipt(owner, monkeypatch):
    authority, clock = owner
    before, receipt = retained_receipt(authority, clock)
    prior_lease = rows(authority, "SELECT * FROM lease")
    original = authority._recover_submission_receipts

    def failed(db, now, epoch):
        original(db, now, epoch)
        raise AuthorityError("fixture-projection-failure")

    monkeypatch.setattr(authority, "_recover_submission_receipts", failed)
    with pytest.raises(AuthorityError, match="fixture-projection-failure") as failure:
        acquire(authority)
    assert lease_acquisition_failure_evidence(failure.value)["stage"] == "receipt_recovery"
    assert rows(authority, "SELECT * FROM lease") == prior_lease
    assert authority.reservation(before.reservation_id) == before
    assert authority.submission_receipt(before.reservation_id) == receipt
    assert rows(authority, "SELECT * FROM events WHERE kind='submitted'") == []


def test_untrusted_exception_attributes_cannot_inject_acquisition_evidence():
    failure = AuthorityError("private-provider-message")
    failure.lease_acquisition_failure = {"stage": "private-url-and-token"}
    exported = execution.execution_failure_evidence(failure)
    assert exported["root_cause"] == {"error_type": "AuthorityError"}
    assert "private" not in json.dumps(exported)


def test_manual_stop_during_acquisition_prevents_broker_calls(env, monkeypatch):
    original = AccountAuthority._recover_submission_receipts

    def stop(authority, db, now, epoch):
        result = original(authority, db, now, epoch)
        env.active = False
        return result

    monkeypatch.setattr(AccountAuthority, "_recover_submission_receipts", stop)
    monkeypatch.setattr(env.broker, "stable_account_fingerprint", lambda: pytest.fail("Stopped session reached broker"))
    with pytest.raises(ContractError, match="Manual 15m session is no longer active"):
        env.executor.recover()
    assert env.broker.submissions == [] and env.captures == 0


@pytest.mark.parametrize("stop_stage", ["commit", "reacquisition"])
def test_manual_stop_during_commit_retry_never_reaches_broker(env, monkeypatch, stop_stage):
    original_transaction = AccountAuthority._transaction
    original_recover = AccountAuthority._recover_submission_receipts
    delayed = []

    @contextmanager
    def transaction(authority):
        with original_transaction(authority) as db:
            yield db
            row = db.execute("SELECT epoch FROM lease WHERE singleton=1").fetchone()
            if row is not None and row[0] == 1 and not delayed:
                delayed.append(row[0])
                env.now += timedelta(seconds=80)
                if stop_stage == "commit":
                    env.active = False

    def recover(authority, db, now, epoch):
        result = original_recover(authority, db, now, epoch)
        if epoch == 2 and stop_stage == "reacquisition":
            env.active = False
        return result

    monkeypatch.setattr(AccountAuthority, "_transaction", transaction)
    monkeypatch.setattr(AccountAuthority, "_recover_submission_receipts", recover)
    monkeypatch.setattr(env.broker, "stable_account_fingerprint", lambda: pytest.fail("Stopped session reached broker"))
    with pytest.raises(ContractError, match="Manual 15m session is no longer active"):
        env.executor.recover()
    assert delayed == [1]
    assert rows(env.executor.authority, "SELECT epoch FROM lease") == [(2,)]
    assert env.broker.submissions == [] and env.captures == 0
