"""Same-owner writer contention; all databases and renewal workers are local fixtures."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import inspect
import socket
import sqlite3
import threading

import pytest

from ml.account_gameplan.authority import (AccountAuthority, AuthorityBinding, AuthorityError,
    IntradayCapacitySnapshot, IntradayReservationRequest)
from ml.intraday.account_execution import _LeaseKeeper
from ml.intraday.prediction import ContractError


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Authority transaction fixtures must never call a provider or broker")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.fixture
def owner(tmp_path, monkeypatch):
    now = [datetime(2026, 10, 26, 19, 15, tzinfo=timezone.utc)]
    authority = AccountAuthority(tmp_path / "synthetic-authority.sqlite3",
        AuthorityBinding("a" * 64, "pc-original", {"pc-original": ("AAPL",)}), clock=lambda: now[0])
    lease = authority.acquire_lease(coordinator_id="pc-original", holder_id="synthetic-holder")
    connect = sqlite3.connect

    def fail_fast_connect(*args, **kwargs):
        # Fixture acceleration only: production's existing busy timeout is unchanged.
        kwargs["timeout"] = 0
        return connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", fail_fast_connect)
    return authority, lease, now


def queued_call(authority, monkeypatch, operation):
    """Signal immediately before real transaction admission; retain worker errors."""
    original = authority._transaction
    queued, finished = threading.Event(), threading.Event()
    result = []

    @contextmanager
    def transaction():
        if threading.current_thread().name == "synthetic-renewal":
            queued.set()
        with original() as db:
            yield db

    monkeypatch.setattr(authority, "_transaction", transaction)

    def run():
        try:
            result.append(operation())
        except BaseException as exc:
            result.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=run, name="synthetic-renewal")
    return worker, queued, finished, result


def join(worker):
    worker.join(5)
    assert not worker.is_alive(), "A completed fixture transaction left a renewal pending"


def test_imports_the_reviewed_candidate_source():
    expected = Path(__file__).resolve().parents[1] / "ml/account_gameplan/authority.py"
    assert Path(inspect.getfile(AccountAuthority)).resolve() == expected


def test_same_instance_renewal_waits_for_owned_transaction_instead_of_sqlite_busy(owner, monkeypatch):
    authority, lease, now = owner
    original_transaction = authority._transaction
    worker, queued, finished, result = queued_call(authority, monkeypatch,
        lambda: authority.renew_lease(lease.token))
    try:
        with original_transaction() as db:
            db.execute("INSERT INTO metadata VALUES('synthetic-write','retained')")
            worker.start()
            assert queued.wait(5)
            assert not finished.wait(.1), "Own renewal entered SQLite while its writer still held the transaction"
            now[0] += timedelta(seconds=10)
    finally:
        join(worker)
    assert finished.is_set() and len(result) == 1 and not isinstance(result[0], BaseException)
    renewed = result[0]
    assert (renewed.token, renewed.epoch, renewed.holder_id) == (lease.token, lease.epoch, lease.holder_id)
    assert renewed.expires_at == (now[0] + timedelta(seconds=60)).isoformat()
    with authority._transaction() as db:
        assert db.execute("SELECT value FROM metadata WHERE key='synthetic-write'").fetchone()[0] == "retained"


@pytest.mark.parametrize("case", ["exact_expiry", "past_expiry", "replacement"])
def test_queued_renewal_rechecks_strict_fence_after_transaction_admission(owner, monkeypatch, case):
    authority, lease, now = owner
    original_transaction = authority._transaction
    worker, queued, finished, result = queued_call(authority, monkeypatch,
        lambda: authority.renew_lease(lease.token))
    try:
        with original_transaction() as db:
            worker.start()
            assert queued.wait(5)
            assert not finished.wait(.1)
            if case == "replacement":
                db.execute("UPDATE lease SET token='replacement-token',epoch=epoch+1,holder='replacement-holder'")
            else:
                now[0] += timedelta(seconds=60 if case == "exact_expiry" else 61)
    finally:
        join(worker)
    assert len(result) == 1 and isinstance(result[0], AuthorityError)
    assert str(result[0]) == "CURRENT_FENCED_LEASE_REQUIRED"
    with authority._transaction() as db:
        row = db.execute("SELECT token,epoch,holder,expires FROM lease").fetchone()
    expected = ("replacement-token", lease.epoch + 1, "replacement-holder", lease.expires_at) if case == "replacement" else (
        lease.token, lease.epoch, lease.holder_id, lease.expires_at)
    assert tuple(row) == expected


def test_rollback_releases_local_writer_before_queued_renewal(owner, monkeypatch):
    authority, lease, _ = owner
    original_transaction = authority._transaction
    worker, queued, finished, result = queued_call(authority, monkeypatch,
        lambda: authority.renew_lease(lease.token))
    try:
        with pytest.raises(ValueError, match="synthetic rollback"):
            with original_transaction() as db:
                db.execute("INSERT INTO metadata VALUES('rolled-back','no')")
                worker.start()
                assert queued.wait(5)
                assert not finished.wait(.1)
                raise ValueError("synthetic rollback")
    finally:
        join(worker)
    assert len(result) == 1 and not isinstance(result[0], BaseException)
    with authority._transaction() as db:
        assert db.execute("SELECT value FROM metadata WHERE key='rolled-back'").fetchone() is None


def test_external_sqlite_busy_still_latches_original_keeper_failure(owner):
    authority, lease, _ = owner
    blocker = sqlite3.connect(authority.path, timeout=0, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    keeper = _LeaseKeeper(authority, lease, active=lambda: True)
    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            keeper.renew()
        failure = caught.value
        assert failure.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert keeper.failure is failure and keeper.lease is lease
    finally:
        blocker.rollback()
        blocker.close()
    for operation in (keeper.check, keeper.renew):
        with pytest.raises(sqlite3.OperationalError) as repeated:
            operation()
        assert repeated.value is failure
    # The private local mutex was released after failure; no expiry was revived.
    assert authority.renew_lease(lease.token).token == lease.token


def test_connect_failure_releases_local_writer_lock(owner, monkeypatch):
    authority, lease, _ = owner
    connect = sqlite3.connect
    failed = []

    def fail_once(*args, **kwargs):
        if not failed:
            failed.append(True)
            raise OSError("synthetic connection setup failure")
        return connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", fail_once)
    with pytest.raises(OSError, match="synthetic connection setup"):
        authority.renew_lease(lease.token)
    assert authority.renew_lease(lease.token).token == lease.token


@pytest.mark.parametrize("stage", ["pragma", "commit"])
def test_transaction_entry_or_commit_error_closes_connection_and_releases_lock(owner, monkeypatch, stage):
    authority, _, _ = owner
    connect = sqlite3.connect
    failed, closed = [], []

    class Connection(sqlite3.Connection):
        def execute(self, sql, *parameters):
            if stage == "pragma" and sql == "PRAGMA synchronous=EXTRA" and not failed:
                failed.append(True)
                raise OSError("synthetic pragma failure")
            return super().execute(sql, *parameters)

        def commit(self):
            if stage == "commit" and not failed:
                failed.append(True)
                raise OSError("synthetic commit failure")
            return super().commit()

        def close(self):
            closed.append(True)
            return super().close()

    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connect(*args, factory=Connection, **kwargs))
    with pytest.raises(OSError, match="synthetic " + stage):
        with authority._transaction() as db:
            db.execute("INSERT INTO metadata VALUES('failed-transaction','no')")
    assert closed == [True]
    with authority._transaction() as db:
        assert db.execute("SELECT value FROM metadata WHERE key='failed-transaction'").fetchone() is None
    assert closed == [True, True]


def test_submission_callback_can_renew_then_enter_authority_gate_without_nested_writer(owner):
    authority, lease, now = owner
    authority.publish_intraday_snapshot(lease.token, IntradayCapacitySnapshot(
        "synthetic-actual", "a" * 64, "2026-10-26", now[0].isoformat(), "100", {"AAPL": 20},
        evidence_fingerprint="b" * 64, identity_before="a" * 64, identity_after="a" * 64, complete=True))
    request = IntradayReservationRequest(
        "synthetic-order", "synthetic-actual", "a" * 64, "2026-10-26", "pc-original", "AAPL",
        "c" * 64, "BUY", 1, "10", "10", "synthetic-native", "synthetic-allocation",
        "c" * 64, "d" * 64, "2026-10-26T19:15:00+00:00", "2026-10-26T19:00:00+00:00",
        "2026-10-26T19:45:00+00:00", "intraday-15m-open-to-third-close-not-down-v1", ("AAPL",))
    reservation = authority.reserve_intraday(lease.token, request)
    calls, results = [], []

    def callback(gate):
        renewed = authority.renew_lease(lease.token)
        calls.append((renewed.token, renewed.epoch, renewed.holder_id))
        gate()
        return "123456"

    def run():
        try:
            results.append(authority.submit(lease.token, reservation.reservation_id, callback))
        except BaseException as exc:
            results.append(exc)

    worker = threading.Thread(target=run, name="synthetic-submission", daemon=True)
    worker.start()
    join(worker)
    assert calls == [(lease.token, lease.epoch, lease.holder_id)]
    assert len(results) == 1 and not isinstance(results[0], BaseException)
    assert results[0].status == "SUBMITTED" and results[0].broker_order_id == "123456"
    assert authority.submission_receipt(reservation.reservation_id)["broker_order_id"] == "123456"


@pytest.mark.parametrize("stop_while_queued", [False, True])
def test_real_keeper_worker_preserves_manual_stop_and_does_not_poison_on_own_writer(owner, monkeypatch, stop_while_queued):
    authority, lease, _ = owner
    active = [True]
    keeper = _LeaseKeeper(authority, lease, active=lambda: active[0])
    original_transaction, original_renew = authority._transaction, authority.renew_lease
    queued, finished = threading.Event(), threading.Event()
    waits, results = [], []

    @contextmanager
    def transaction():
        if threading.current_thread().name == "intraday-lease-renewal":
            queued.set()
        with original_transaction() as db:
            yield db

    def renew(token):
        try:
            result = original_renew(token)
            results.append(result)
            return result
        finally:
            finished.set()

    def wait(seconds):
        waits.append(seconds)
        return keeper._stop.wait(5) if len(waits) > 1 else False

    monkeypatch.setattr(authority, "_transaction", transaction)
    monkeypatch.setattr(authority, "renew_lease", renew)
    monkeypatch.setattr(keeper, "_wait", wait)
    try:
        with original_transaction() as db:
            db.execute("INSERT INTO metadata VALUES('keeper-fixture','one')")
            keeper.start()
            assert queued.wait(5)
            assert not finished.wait(.1)
            active[0] = not stop_while_queued
        assert finished.wait(5)
    finally:
        keeper.stop()
    assert keeper._finished.is_set() and len(results) == 1
    assert (results[0].token, results[0].epoch, results[0].holder_id) == (lease.token, lease.epoch, lease.holder_id)
    if stop_while_queued:
        assert isinstance(keeper.failure, ContractError)
        active[0] = True
        for operation in (keeper.check, keeper.renew):
            with pytest.raises(ContractError) as stopped:
                operation()
            assert stopped.value is keeper.failure
    else:
        assert keeper.failure is None
