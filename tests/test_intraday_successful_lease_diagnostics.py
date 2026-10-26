"""Successful own-lease timings, using temporary ledgers and synthetic clocks only."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
import json
import math
import socket

import pytest

from test_intraday_account_execution import env, native
from test_intraday_lease_acquisition import acquire, delayed_commit, owner, retained_receipt, rows
from ml.account_gameplan import authority as authority_module
from ml.account_gameplan.authority import (
    AccountAuthority, AuthorityError, lease_acquisition_failure_evidence,
    lease_acquisition_success_evidence,
)
from ml.intraday import account_execution as execution


SCHEMA = "single-host-lease-acquisition-timings-v1"
PHASES = {"validation", "transaction_enter", "lease_event", "receipt_recovery",
          "grant_timestamp", "transaction_commit", "post_commit_clock"}


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Successful lease diagnostics cannot contact a provider")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def assert_bounded_success(evidence, *, attempt=1):
    assert evidence["schema_version"] == SCHEMA
    assert evidence["stage"] == "acquisition_complete" and evidence["attempt"] == attempt
    assert type(evidence["diagnostics_unavailable"]) is bool
    assert 1 <= len(evidence["phases"]) <= 20
    assert set(evidence) - {"receipt_recovery_details"} == {
        "schema_version", "attempt", "stage", "elapsed_seconds", "stage_elapsed_seconds",
        "phases", "diagnostics_unavailable"}
    if "receipt_recovery_details" in evidence:
        assert evidence["receipt_recovery_details"]["schema_version"] == "single-host-receipt-recovery-details-v1"
        assert 1 <= len(evidence["receipt_recovery_details"]["attempts"]) <= 3
    for phase in evidence["phases"]:
        assert set(phase) == {"attempt", "stage", "elapsed_seconds"}
        assert phase["stage"] in PHASES
        assert type(phase["attempt"]) is int and 0 <= phase["attempt"] <= 3
    for value in [evidence["elapsed_seconds"], evidence["stage_elapsed_seconds"],
                  *(phase["elapsed_seconds"] for phase in evidence["phases"])]:
        assert value is None or (type(value) in {int, float} and math.isfinite(value) and value >= 0)
    json.dumps(evidence, allow_nan=False)


@pytest.mark.parametrize("stage,seconds", [("transaction_enter", 7), ("lease_event", 3),
    ("receipt_recovery", 80.547168), ("transaction_commit", 11)])
def test_success_distinguishes_slow_recovery_from_entry_event_and_commit(owner, monkeypatch, stage, seconds):
    authority, clock = owner
    original_transaction = authority._transaction
    original_recover = authority._recover_submission_receipts
    original_event = authority._event

    @contextmanager
    def transaction():
        if stage == "transaction_enter":
            clock.advance(seconds)
        with original_transaction() as db:
            yield db
            if stage == "transaction_commit":
                clock.advance(seconds)

    def recover(*args):
        result = original_recover(*args)
        if stage == "receipt_recovery":
            clock.advance(seconds)
        return result

    def event(*args):
        result = original_event(*args)
        if stage == "lease_event":
            clock.advance(seconds)
        return result

    monkeypatch.setattr(authority, "_transaction", transaction)
    monkeypatch.setattr(authority, "_recover_submission_receipts", recover)
    monkeypatch.setattr(authority, "_event", event)
    lease = acquire(authority)
    evidence = lease_acquisition_success_evidence(authority, lease)
    assert_bounded_success(evidence)
    assert evidence["diagnostics_unavailable"] is False
    assert evidence["elapsed_seconds"] == seconds
    assert evidence["stage_elapsed_seconds"] == 0
    measured = {phase["stage"]: phase["elapsed_seconds"] for phase in evidence["phases"]}
    assert set(measured) == PHASES and measured[stage] == seconds
    assert all(value == 0 for name, value in measured.items() if name != stage)
    # Slow recovery precedes the grant; commit duration alone consumes granted TTL.
    remaining = 60 - seconds if stage == "transaction_commit" else 60
    assert datetime.fromisoformat(lease.expires_at) == clock() + timedelta(seconds=remaining)
    encoded = json.dumps(evidence)
    for private in (lease.token, lease.holder_id, authority.binding["account_fingerprint"],
                    str(authority.path), "own-snapshot", "own-native", "own-allocation"):
        assert private not in encoded


def test_successful_commit_retry_records_attempts_without_reviving_old_fence(owner, monkeypatch):
    authority, clock = owner
    delayed_commit(monkeypatch, authority, clock, seconds=80)
    lease = acquire(authority)
    evidence = lease_acquisition_success_evidence(authority, lease)
    assert_bounded_success(evidence, attempt=2)
    assert evidence["elapsed_seconds"] == 80 and lease.epoch == 2
    assert [(p["attempt"], p["elapsed_seconds"]) for p in evidence["phases"]
            if p["stage"] == "transaction_commit"] == [(1, 80), (2, 0)]
    assert datetime.fromisoformat(lease.expires_at) == clock() + timedelta(seconds=60)
    assert rows(authority, "SELECT epoch FROM lease") == [(2,)]
    assert len(rows(authority, "SELECT * FROM events WHERE kind='lease-acquired'")) == 2


def test_slow_success_keeps_exact_receipt_original_intention_and_fence(owner, monkeypatch):
    authority, clock = owner
    original, receipt = retained_receipt(authority, clock)
    recover = authority._recover_submission_receipts

    def slow(*args):
        result = recover(*args)
        clock.advance(80)
        return result

    monkeypatch.setattr(authority, "_recover_submission_receipts", slow)
    lease = acquire(authority)
    evidence = lease_acquisition_success_evidence(authority, lease)
    assert_bounded_success(evidence)
    assert evidence["elapsed_seconds"] == 80 and lease.epoch == 2
    assert datetime.fromisoformat(lease.expires_at) == clock() + timedelta(seconds=60)
    recovered = authority.reservation(original.reservation_id)
    assert recovered.status == "SUBMITTED" and recovered.broker_order_id == "101"
    assert recovered.request == original.request and recovered.filled_quantity == original.filled_quantity == 0
    assert authority.submission_receipt(original.reservation_id) == receipt
    assert len(rows(authority, "SELECT * FROM events WHERE kind='submitted'")) == 1
    assert original.reservation_id not in json.dumps(evidence)


def test_success_marker_requires_exact_returned_lease_and_detaches_copies(owner):
    authority, _ = owner
    assert lease_acquisition_success_evidence(authority, None) is None
    lease = acquire(authority)
    evidence = lease_acquisition_success_evidence(authority, lease)
    assert replace(lease) == lease and replace(lease) is not lease
    assert lease_acquisition_success_evidence(authority, replace(lease)) is None
    assert lease_acquisition_success_evidence(object(), lease) is None
    evidence["phases"][0]["elapsed_seconds"] = 999
    evidence["stage"] = "private-provider-url"
    assert lease_acquisition_success_evidence(authority, lease)["phases"][0]["elapsed_seconds"] == 0
    assert lease_acquisition_success_evidence(authority, lease)["stage"] == "acquisition_complete"
    authority._lease_acquisition_success = {"lease": lease, "evidence": evidence}
    assert lease_acquisition_success_evidence(authority, lease) is None


@pytest.mark.parametrize("failure", ["active_owner", "invalid_ttl", "wrong_coordinator", "receipt_recovery"])
def test_next_failed_acquisition_cannot_reuse_earlier_success(owner, monkeypatch, failure):
    authority, clock = owner
    lease = acquire(authority)
    assert lease_acquisition_success_evidence(authority, lease) is not None
    kwargs = dict(coordinator_id="pc-original", holder_id="new-fixture-owner")
    if failure == "invalid_ttl":
        kwargs["ttl_seconds"] = 0
    elif failure == "wrong_coordinator":
        kwargs["coordinator_id"] = "different-host"
    elif failure == "receipt_recovery":
        clock.advance(61)
        def fail(*args):
            raise AuthorityError("fixture-recovery-error")
        monkeypatch.setattr(authority, "_recover_submission_receipts", fail)
    with pytest.raises(AuthorityError):
        authority.acquire_lease(**kwargs)
    assert lease_acquisition_success_evidence(authority, lease) is None


@pytest.mark.parametrize("bad_clock", ["exception", "nan", "inf", "wrong_type"])
def test_unavailable_diagnostic_clock_cannot_prevent_success(owner, monkeypatch, bad_clock):
    authority, clock = owner
    def broken():
        if bad_clock == "exception":
            raise ValueError("private-diagnostic-clock")
        return {"nan": float("nan"), "inf": float("inf"), "wrong_type": "private-clock-value"}[bad_clock]
    monkeypatch.setattr(authority_module.time, "monotonic", broken)
    lease = acquire(authority)
    evidence = lease_acquisition_success_evidence(authority, lease)
    assert_bounded_success(evidence)
    assert evidence["diagnostics_unavailable"] is True and evidence["elapsed_seconds"] is None
    assert datetime.fromisoformat(lease.expires_at) == clock() + timedelta(seconds=60)
    assert authority.renew_lease(lease.token).epoch == lease.epoch
    assert "private" not in json.dumps(evidence, allow_nan=False)


def test_failed_diagnostic_clock_preserves_original_acquisition_exception(owner, monkeypatch):
    authority, _ = owner
    failure = AuthorityError("fixture-receipt-projection-failure")
    def broken_clock():
        raise ValueError("private-diagnostic-clock")
    def fail(*args):
        raise failure
    monkeypatch.setattr(authority_module.time, "monotonic", broken_clock)
    monkeypatch.setattr(authority, "_recover_submission_receipts", fail)
    with pytest.raises(AuthorityError) as caught:
        acquire(authority)
    assert caught.value is failure
    evidence = lease_acquisition_failure_evidence(failure)
    assert evidence["stage"] == "receipt_recovery" and evidence["diagnostics_unavailable"] is True
    assert "private" not in json.dumps(evidence, allow_nan=False)
    assert lease_acquisition_success_evidence(authority, None) is None
    assert rows(authority, "SELECT * FROM lease") == []


def test_execution_trace_keeps_sanitized_detached_success_phases(owner):
    authority, _ = owner
    lease = acquire(authority)
    trace = execution._ExecutionTrace()
    assert trace.lease_acquisition is None and "lease_acquisition" not in trace.snapshot()
    trace.lease_acquisition = lease_acquisition_success_evidence(authority, lease)
    trace.lease_acquisition["private"] = "private-account-url-and-token"
    first = trace.snapshot()["lease_acquisition"]
    assert_bounded_success(first)
    assert "private" not in json.dumps(first)
    first["phases"][0]["elapsed_seconds"] = 999
    assert trace.snapshot()["lease_acquisition"]["phases"][0]["elapsed_seconds"] == 0


def operation_observations(state):
    with state.store.connect() as db:
        return [json.loads(row[0]) for row in db.execute(
            "SELECT payload FROM events WHERE kind='execution_operation_diagnostics' ORDER BY id")]


def test_normal_recovery_exports_own_successful_receipt_recovery_phase(env, monkeypatch):
    env.executor.diagnostic_session_id = "manual-atlas-15m:" + "a" * 32
    monkeypatch.setattr(authority_module.time, "monotonic", lambda: env.now.timestamp())
    original = AccountAuthority._recover_submission_receipts
    def slow(authority, *args):
        result = original(authority, *args)
        env.now += timedelta(seconds=80.547168)
        return result
    monkeypatch.setattr(AccountAuthority, "_recover_submission_receipts", slow)
    assert env.executor.recover() == []
    saved, = operation_observations(env)
    evidence = saved["diagnostics"]["lease_acquisition"]
    assert_bounded_success(evidence)
    assert evidence["elapsed_seconds"] == 80.547168
    assert next(p["elapsed_seconds"] for p in evidence["phases"] if p["stage"] == "receipt_recovery") == 80.547168
    assert saved["operation"] == "recover" and saved["operation_returned_normally"] is True
    assert env.captures == 1 and not env.broker.submissions and not env.broker.cancellations
    assert native(env).snapshot().reservations == ()
    assert env.executor._execution_trace is env.executor._lease_keeper is None
    assert env.executor.lease.token not in json.dumps(saved)


def test_later_failure_retains_success_timing_without_changing_primary_cause(env):
    failure = TimeoutError("private-fixture-capture-url-and-token")
    env.capture_fail = failure
    with pytest.raises(TimeoutError) as caught:
        env.executor.recover()
    assert caught.value is failure
    exported = execution.execution_failure_evidence(failure)
    assert_bounded_success(exported["lease_acquisition"])
    assert exported["root_cause"] == {"error_type": "TimeoutError"}
    assert "private-fixture" not in json.dumps(exported)
    assert not env.broker.submissions and not env.broker.cancellations


def test_optional_success_getter_failure_cannot_change_normal_recovery(env, monkeypatch):
    env.executor.diagnostic_session_id = "manual-atlas-15m:" + "a" * 32
    def unavailable(*args):
        raise ValueError("private-success-diagnostics-getter")
    monkeypatch.setattr(execution, "lease_acquisition_success_evidence", unavailable)
    assert env.executor.recover() == []
    saved, = operation_observations(env)
    assert "lease_acquisition" not in saved["diagnostics"]
    assert "private-success" not in json.dumps(saved)
    assert env.captures == 1 and not env.broker.submissions and not env.broker.cancellations
