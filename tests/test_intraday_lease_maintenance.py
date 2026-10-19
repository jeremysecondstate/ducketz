"""Real temporary fences and worker threads; all broker actions are fixtures."""
from queue import Queue
import socket
import threading

import pandas as pd
import pytest

from test_intraday_account_execution import env, advance, native
from ml.account_gameplan.authority import AccountAuthority, AuthorityBinding, AuthorityError
from ml.intraday import account_execution as execution
from ml.intraday.prediction import ContractError, instant


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Lease regressions must never connect to a provider")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


class WorkerClock:
    """Advance fake time only after the real keeper thread finishes each tick."""
    def __init__(self, env, monkeypatch):
        self.env = env
        self.main_thread = threading.get_ident()
        self.pulses = threading.Semaphore(0)
        self.completed = Queue()
        self.records = []
        self.fail_next = None
        self.keepers = []
        original_wait = execution._LeaseKeeper._wait
        original_renew = AccountAuthority.renew_lease
        original_start = execution._LeaseKeeper.start

        def wait(keeper, seconds):
            assert 0 < seconds < 60
            while not original_wait(keeper, 0):
                if self.pulses.acquire(timeout=.01):
                    return original_wait(keeper, 0)
            return True

        def renew(authority, token, **kwargs):
            background = threading.get_ident() != self.main_thread
            try:
                if background and self.fail_next is not None:
                    error, self.fail_next = self.fail_next, None
                    raise error
                lease = original_renew(authority, token, **kwargs)
                ttl = (instant(lease.expires_at) - instant(env.clock())).total_seconds()
                self.records.append((background, lease.token, lease.epoch, lease.holder_id, ttl))
            except BaseException as error:
                if background:
                    self.completed.put(error)
                raise
            if background:
                self.completed.put(lease)
            return lease

        def start(keeper):
            self.keepers.append(keeper)
            return original_start(keeper)

        monkeypatch.setattr(execution._LeaseKeeper, "_wait", wait)
        monkeypatch.setattr(execution._LeaseKeeper, "start", start)
        monkeypatch.setattr(AccountAuthority, "renew_lease", renew)

    def tick(self, seconds, *, error=None):
        advance(self.env, seconds)
        self.pulses.release()
        result = self.completed.get(timeout=5)
        if error is None:
            assert not isinstance(result, BaseException), repr(result)
        else:
            assert result is error
        return result

    def slow_preparation(self):
        for seconds in (20, 20, 20, 5):
            self.tick(seconds)


def authority_and_lease(env):
    authority = AccountAuthority(env.root / "keeper.sqlite3",
        AuthorityBinding(env.config.account_fingerprint, env.config.coordinator_id, env.config.participants),
        clock=lambda: instant(env.clock()))
    lease = authority.acquire_lease(coordinator_id=env.config.machine_id, holder_id="fixture-keeper")
    return authority, lease


@pytest.mark.parametrize("operation", ["recover", "step"])
def test_worker_preserves_same_fence_across_65_second_preparation(env, monkeypatch, operation):
    clock = WorkerClock(env, monkeypatch)
    forecast = env.forecast()
    original_repair = env.executor._repair_pre_send
    releases = []
    original_release = AccountAuthority.release_lease

    def prepare():
        original_repair()
        clock.slow_preparation()

    def release(authority, token):
        assert all(not keeper._thread.is_alive() for keeper in clock.keepers)
        releases.append((token, len(clock.records)))
        return original_release(authority, token)

    monkeypatch.setattr(env.executor, "_repair_pre_send", prepare)
    monkeypatch.setattr(AccountAuthority, "release_lease", release)
    result = env.executor.recover() if operation == "recover" else env.executor.step(forecast)
    if operation == "recover":
        assert result == []
    else:
        assert result["disposition"] == "submitted"
    assert len(env.broker.submissions) == (operation == "step")
    assert len(releases) == 1 and releases[0][1] == len(clock.records)
    assert len([row for row in clock.records if row[0]]) == 4
    identities = {(row[1], row[2], row[3]) for row in clock.records}
    assert len(identities) == 1
    assert all(0 < row[4] <= 60 for row in clock.records)
    assert clock.keepers[0].failure is None
    assert env.executor._lease_keeper is None
    with env.executor.authority._transaction() as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='lease-acquired'").fetchone()[0] == 1
        assert instant(db.execute("SELECT expires FROM lease").fetchone()[0]) <= instant(env.clock())


@pytest.mark.parametrize("takeover", [False, True])
def test_expired_or_replaced_fence_is_never_reacquired(env, takeover):
    authority, lease = authority_and_lease(env)
    keeper = execution._LeaseKeeper(authority, lease, active=lambda: env.active)
    advance(env, 61)
    replacement = authority.acquire_lease(coordinator_id=env.config.machine_id, holder_id="other-fixture") if takeover else None
    with pytest.raises(AuthorityError, match="CURRENT_FENCED_LEASE_REQUIRED") as failure:
        keeper.renew()
    assert keeper.failure is failure.value
    with pytest.raises(AuthorityError) as repeated:
        keeper.renew()
    assert repeated.value is failure.value
    with authority._transaction() as db:
        current = db.execute("SELECT token,epoch FROM lease").fetchone()
    assert tuple(current) == ((replacement.token, replacement.epoch) if replacement else (lease.token, lease.epoch))
    assert env.broker.submissions == []


def test_manual_stop_latches_without_renewal(env, monkeypatch):
    authority, lease = authority_and_lease(env)
    keeper = execution._LeaseKeeper(authority, lease, active=lambda: env.active)
    monkeypatch.setattr(authority, "renew_lease", lambda *a, **k: pytest.fail("Manual stop must prevent renewal"))
    env.active = False
    with pytest.raises(ContractError, match="Manual 15m session") as failure:
        keeper.renew()
    env.active = True
    with pytest.raises(ContractError) as repeated:
        keeper.renew()
    assert repeated.value is failure.value and keeper.failure is failure.value


def test_background_renewal_error_cannot_silently_resume(env, monkeypatch):
    clock = WorkerClock(env, monkeypatch)
    authority, lease = authority_and_lease(env)
    keeper = execution._LeaseKeeper(authority, lease, active=lambda: env.active)
    error = TimeoutError("fixture transient lease write")
    keeper.start()
    try:
        clock.fail_next = error
        clock.tick(20, error=error)
        # stop joins the worker, proving its failure was durably latched in memory.
        keeper.stop()
        with pytest.raises(TimeoutError) as failure:
            keeper.check()
        assert failure.value is error and keeper.failure is error
        with pytest.raises(TimeoutError) as repeated:
            keeper.renew()
        assert repeated.value is error
        assert clock.records == []
    finally:
        keeper.stop()


def test_renewal_does_not_make_old_capacity_fresh(env, monkeypatch):
    clock = WorkerClock(env, monkeypatch)
    original = execution.HorizonLedger.reconcile

    def slow_reconcile(ledger, *args, **kwargs):
        result = original(ledger, *args, **kwargs)
        clock.slow_preparation()
        return result

    monkeypatch.setattr(execution.HorizonLedger, "reconcile", slow_reconcile)
    with pytest.raises(AuthorityError, match="ACCOUNT_SNAPSHOT_STALE_OR_FUTURE"):
        env.executor.recover()
    assert clock.keepers[0].failure is None
    assert env.broker.submissions == []


def test_renewal_does_not_allow_stale_quote_at_final_post(env, monkeypatch):
    clock = WorkerClock(env, monkeypatch)
    forecast = env.forecast()
    env.broker.before_post_hook = clock.slow_preparation
    result = env.executor.step(forecast)
    assert result["disposition"] == "not_submitted"
    assert result["evidence"]["root_cause"] == {"error_type": "ContractError", "message": "Quote needs refresh before submission"}
    assert clock.keepers[0].failure is None
    assert env.broker.submissions == []


def test_final_post_checks_manual_stop_after_identity_read(env, monkeypatch):
    forecast = env.forecast()
    original = env.broker.verify_read_snapshot
    def before_post():
        def verify(identity):
            original(identity)
            env.active = False
        env.broker.verify_read_snapshot = verify
    env.broker.before_post_hook = before_post
    result = env.executor.step(forecast)
    assert result["disposition"] == "not_submitted"
    assert env.broker.submissions == []
    assert result["evidence"]["root_cause"]["message"] == "Manual 15m session is no longer active"


def test_final_post_rechecks_quote_after_slow_foreground_renewal(env, monkeypatch):
    forecast = env.forecast()
    env.quote_age = native(env).maximum_evidence_age_seconds - 1
    original_renew = AccountAuthority.renew_lease
    main_thread = threading.get_ident()
    in_final_gate = [False]
    final_renewals = []

    def renew(authority, token, **kwargs):
        result = original_renew(authority, token, **kwargs)
        if in_final_gate[0] and threading.get_ident() == main_thread:
            final_renewals.append(result)
            if len(final_renewals) == 2:
                advance(env, 20)
        return result

    monkeypatch.setattr(AccountAuthority, "renew_lease", renew)
    env.broker.before_post_hook = lambda: in_final_gate.__setitem__(0, True)
    result = env.executor.step(forecast)
    assert len(final_renewals) == 2
    assert result["disposition"] == "not_submitted"
    assert result["evidence"]["root_cause"] == {"error_type": "ContractError", "message": "Quote needs refresh before submission"}
    assert env.broker.submissions == []


def test_abandon_failure_preserves_unsent_cause_and_both_commitments(env, monkeypatch):
    forecast = env.forecast()
    def build(*args, **kwargs):
        raise ValueError("fixture original payload failure")
    def abandon(*args, **kwargs):
        raise AuthorityError("CURRENT_FENCED_LEASE_REQUIRED")
    monkeypatch.setattr(execution, "build_schwab_stock_order_payload", build)
    monkeypatch.setattr(AccountAuthority, "abandon_unsubmitted", abandon)
    result = env.executor.step(forecast)
    assert result["disposition"] == "not_submitted"
    evidence = result["evidence"]
    assert evidence["root_cause"] == {"error_type": "ValueError"}
    assert evidence["submission_stage"] == "build_order_payload"
    assert evidence["cleanup_failure"]["exception_chain"][0] == {
        "error_type": "AuthorityError", "message": "CURRENT_FENCED_LEASE_REQUIRED"}
    assert evidence["awaiting_fresh_authority_fence"] is True
    assert evidence["proven_unsent_retry_eligible"] is True
    assert [row.status for row in native(env).snapshot().reservations] == ["RESERVED"]
    assert [row["status"] for row in env.executor.authority.read_state()["reservations"]] == ["RESERVED"]
    saved = env.executor.intents.history(forecast["forecast_id"])[-1]
    assert saved["evidence"] == evidence
    assert env.broker.submissions == []


def test_final_delete_checks_worker_failure_after_slow_identity_read(env, monkeypatch):
    clock = WorkerClock(env, monkeypatch)
    old = env.forecast()
    assert env.executor.step(old)["disposition"] == "submitted"
    new = env.forecast(offset=15, probability=.25)
    env.now = pd.Timestamp(new["issued_at"])
    original_cancel = env.broker.cancel_prepared_order
    original_verify = env.broker.verify_read_snapshot
    error = TimeoutError("fixture lease write failed during final DELETE identity read")
    in_final_delete = [False]

    def cancel(order_id, context, *, before_delete):
        in_final_delete[0] = True
        try:
            return original_cancel(order_id, context, before_delete=before_delete)
        finally:
            in_final_delete[0] = False

    def verify(identity):
        original_verify(identity)
        if in_final_delete[0]:
            clock.fail_next = error
            clock.tick(20, error=error)
            # Join only after the failure so the deterministic test sees it latched.
            env.executor._lease_keeper.stop()

    monkeypatch.setattr(env.broker, "cancel_prepared_order", cancel)
    monkeypatch.setattr(env.broker, "verify_read_snapshot", verify)
    result = env.executor.step(new)
    assert result["disposition"] == "previous_intention_reconciliation"
    assert result["evidence"]["reason_code"] == "TEMPORARY_FAILURE_COMMITMENT_RETAINED"
    assert len(env.broker.submissions) == 1 and env.broker.cancellations == []
    assert clock.keepers[-1].failure is error


def test_keeper_cleanup_failure_preserves_original_capture_error(env, monkeypatch):
    error = ValueError("fixture original capture failure")
    env.capture_fail = error
    original_stop = execution._LeaseKeeper.stop
    def stop(keeper):
        original_stop(keeper)
        raise OSError("fixture keeper cleanup failed")
    monkeypatch.setattr(execution._LeaseKeeper, "stop", stop)
    with pytest.raises(ValueError) as failure:
        env.executor.recover()
    assert failure.value is error
    evidence = execution.execution_failure_evidence(error)
    assert evidence["root_cause"] == {"error_type": "ValueError"}
    assert evidence["cleanup_failure"]["exception_chain"][0] == {"error_type": "OSError"}
    assert env.broker.submissions == []


def test_finished_worker_join_error_cannot_loop_cleanup_forever(env, monkeypatch):
    error = ValueError("fixture original capture failure")
    env.capture_fail = error
    original_stop = execution._LeaseKeeper.stop
    captured, outcomes = Queue(), Queue()
    join_calls = []

    def stop(keeper):
        keeper._stop.set()
        assert keeper._finished.wait(5)
        original_join = keeper._thread.join
        def failed_join(*args, **kwargs):
            join_calls.append(1)
            raise OSError("fixture permanently failed join after worker finished")
        keeper._thread.join = failed_join
        captured.put((keeper, original_join))
        original_stop(keeper)

    def recover():
        try:
            env.executor.recover()
        except BaseException as raised:
            outcomes.put(raised)

    monkeypatch.setattr(execution._LeaseKeeper, "stop", stop)
    operation = threading.Thread(target=recover, name="fixture-account-cleanup", daemon=True)
    operation.start()
    keeper, original_join = captured.get(timeout=5)
    operation.join(timeout=5)
    stalled = operation.is_alive()
    if stalled:
        # Rescue a regressed busy loop so a failed test cannot leak a writer.
        # The permanently failing join was still observed for the full bound.
        keeper._thread.join = original_join
        operation.join(timeout=5)
    assert not operation.is_alive(), "Fixture cleanup thread failed to exit"
    assert not stalled, "Completed renewal worker caused an unbounded cleanup loop"
    assert outcomes.get(timeout=1) is error
    assert 1 <= len(join_calls) <= 2
    evidence = execution.execution_failure_evidence(error)
    assert evidence["cleanup_failure"]["exception_chain"][0] == {"error_type": "OSError"}
    assert env.broker.submissions == []
