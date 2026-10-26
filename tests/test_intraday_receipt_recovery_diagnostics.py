"""Receipt recovery timings use only temporary ledgers, fake clocks and broker fixtures."""
from contextlib import contextmanager
from datetime import timedelta
import json
import math
import socket
import sqlite3

import pytest

from test_intraday_account_execution import env
from test_intraday_lease_acquisition import acquire, delayed_commit, owner, retained_receipt, rows
from ml.account_gameplan import authority as authority_module
from ml.account_gameplan.authority import (
    AuthorityError, IntradayCapacitySnapshot, IntradayReservationRequest, SubmissionNotStarted,
    lease_acquisition_failure_evidence, lease_acquisition_success_evidence,
    receipt_recovery_details_evidence,
)
from ml.intraday import account_execution as execution


PHASES = {"receipt_read", "receipt_validate", "reservation_read", "scope_validate", "projection"}
METRICS = {"count", "elapsed_count", "total_seconds", "maximum_seconds", "thread_cpu_count",
           "thread_cpu_total_seconds", "thread_cpu_maximum_seconds"}


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Receipt recovery diagnostics cannot call a provider")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def details(evidence):
    result = evidence["receipt_recovery_details"]
    assert set(result) == {"schema_version", "attempts"}
    assert result["schema_version"] == "single-host-receipt-recovery-details-v1"
    assert 1 <= len(result["attempts"]) <= 3
    for item in result["attempts"]:
        assert set(item) == {"attempt", "phases", "diagnostics_unavailable", "thread_cpu_unavailable"}
        assert type(item["attempt"]) is int and 1 <= item["attempt"] <= 3
        assert type(item["diagnostics_unavailable"]) is bool
        assert type(item["thread_cpu_unavailable"]) is bool
        assert set(item["phases"]) == PHASES
        for phase in item["phases"].values():
            assert set(phase) == METRICS
            for key, value in phase.items():
                assert type(value) in {int, float} and math.isfinite(value) and value >= 0
                if key.endswith("count"):
                    assert type(value) is int
    json.dumps(result, allow_nan=False)
    return result["attempts"]


def test_empty_receipts_record_one_read_without_work_or_private_values(owner):
    authority, _ = owner
    lease = acquire(authority)
    attempt, = details(lease_acquisition_success_evidence(authority, lease))
    assert attempt["phases"]["receipt_read"]["count"] == 1
    assert all(phase["count"] == 0 for name, phase in attempt["phases"].items() if name != "receipt_read")
    assert rows(authority, "SELECT * FROM reservations") == []
    encoded = json.dumps(attempt)
    for private in (lease.token, lease.holder_id, str(authority.path), authority.binding["account_fingerprint"]):
        assert private not in encoded


@pytest.mark.parametrize("stage", sorted(PHASES))
def test_actual_receipt_work_attributes_elapsed_and_thread_cpu_to_each_fixed_phase(owner, monkeypatch, stage):
    authority, clock = owner
    before, receipt = retained_receipt(authority, clock)
    measure = authority_module._ReceiptRecoveryTimings.measure
    monkeypatch.setattr(authority_module.time, "thread_time", lambda: clock().timestamp() / 2)
    @contextmanager
    def delayed(self, name):
        with measure(self, name):
            yield
            if name == stage:
                clock.advance(5)
    monkeypatch.setattr(authority_module._ReceiptRecoveryTimings, "measure", delayed)
    lease = acquire(authority)
    attempt, = details(lease_acquisition_success_evidence(authority, lease))
    for name, phase in attempt["phases"].items():
        assert phase["count"] == phase["elapsed_count"] == phase["thread_cpu_count"] == 1
        assert phase["total_seconds"] == phase["maximum_seconds"] == (5 if name == stage else 0)
        assert phase["thread_cpu_total_seconds"] == phase["thread_cpu_maximum_seconds"] == (2.5 if name == stage else 0)
    assert authority.reservation(before.reservation_id).broker_order_id == "101"
    assert authority.submission_receipt(before.reservation_id) == receipt
    assert len(rows(authority, "SELECT * FROM events WHERE kind='submitted'")) == 1
    assert authority_module._RECEIPT_RECOVERY_CONTEXT.get() is None


def pending_no_send(authority, clock):
    lease = acquire(authority)
    authority.publish_intraday_snapshot(lease.token, IntradayCapacitySnapshot(
        "own-snapshot", "a" * 64, "2026-10-19", clock().isoformat(), "100", {"AAPL": 20},
        evidence_fingerprint="b" * 64, identity_before="a" * 64, identity_after="a" * 64, complete=True))
    request = IntradayReservationRequest("own-intention", "own-snapshot", "a" * 64, "2026-10-19",
        "pc-original", "AAPL", "c" * 64, "BUY", 1, "10", "10", "own-native", "own-allocation",
        "c" * 64, "d" * 64, "2026-10-19T17:15:02+00:00", "2026-10-19T17:00:00+00:00",
        "2026-10-19T17:45:00+00:00", "intraday-15m-open-to-third-close-not-down-v1", ("AAPL",))
    reservation = authority.reserve_intraday(lease.token, request)
    def no_send(gate):
        clock.advance(61)
        raise SubmissionNotStarted("fixture-pre-gate-stop")
    with pytest.raises(AuthorityError):
        authority.submit(lease.token, reservation.reservation_id, no_send)
    return authority.reservation(reservation.reservation_id), authority.submission_receipt(reservation.reservation_id)


@pytest.mark.parametrize("outcome", ["ACCEPTED", "NOT_SUBMITTED"])
def test_accepted_and_audited_no_send_keep_exact_projection_and_repeated_noop(owner, outcome):
    authority, clock = owner
    before, receipt = (retained_receipt if outcome == "ACCEPTED" else pending_no_send)(authority, clock)
    lease = acquire(authority)
    after = authority.reservation(before.reservation_id)
    assert after.status == ("SUBMITTED" if outcome == "ACCEPTED" else "NOT_SUBMITTED")
    assert after.broker_order_id == ("101" if outcome == "ACCEPTED" else None)
    assert after.request == before.request and after.filled_quantity == before.filled_quantity == 0
    assert authority.submission_receipt(before.reservation_id) == receipt
    event_kind = "submitted" if outcome == "ACCEPTED" else "not-submitted"
    assert len(rows(authority, "SELECT * FROM events WHERE kind='" + event_kind + "'")) == 1
    clock.advance(61)
    repeated = acquire(authority)
    attempt, = details(lease_acquisition_success_evidence(authority, repeated))
    assert all(phase["count"] == 1 for phase in attempt["phases"].values())
    assert authority.reservation(before.reservation_id) == after
    assert len(rows(authority, "SELECT * FROM events WHERE kind='" + event_kind + "'")) == 1
    assert lease_acquisition_success_evidence(authority, lease) is None


def test_corrupt_receipt_preserves_failure_and_rolls_back_fence_with_partial_diagnostics(owner):
    authority, clock = owner
    before, _ = retained_receipt(authority, clock)
    prior_lease = rows(authority, "SELECT * FROM lease")
    with sqlite3.connect(authority.path) as db:
        db.execute("UPDATE submission_receipts SET payload='{}'")
    with pytest.raises(AuthorityError, match="IMMUTABLE_SUBMISSION_RECEIPT_CHANGED") as caught:
        acquire(authority)
    evidence = lease_acquisition_failure_evidence(caught.value)
    attempt, = details(evidence)
    assert attempt["phases"]["receipt_read"]["count"] == attempt["phases"]["receipt_validate"]["count"] == 1
    assert all(attempt["phases"][name]["count"] == 0 for name in ("reservation_read", "scope_validate", "projection"))
    assert rows(authority, "SELECT * FROM lease") == prior_lease
    assert authority.reservation(before.reservation_id) == before
    assert authority_module._RECEIPT_RECOVERY_CONTEXT.get() is None
    assert execution.execution_failure_evidence(caught.value)["root_cause"]["lease_acquisition"] == evidence


def test_projection_exception_is_original_error_and_rolls_back_all_mutations(owner, monkeypatch):
    authority, clock = owner
    before, receipt = retained_receipt(authority, clock)
    failure = OSError("private-projection-path")
    event = authority._event
    def fail(db, now, kind, payload):
        if kind == "submitted":
            raise failure
        return event(db, now, kind, payload)
    monkeypatch.setattr(authority, "_event", fail)
    with pytest.raises(OSError) as caught:
        acquire(authority)
    assert caught.value is failure
    evidence = lease_acquisition_failure_evidence(failure)
    assert details(evidence)[0]["phases"]["projection"]["count"] == 1
    assert authority.reservation(before.reservation_id) == before
    assert authority.submission_receipt(before.reservation_id) == receipt
    assert "private" not in json.dumps(execution.execution_failure_evidence(failure))
    assert authority_module._RECEIPT_RECOVERY_CONTEXT.get() is None


def test_commit_retry_keeps_separate_attempts_and_next_call_resets_details(owner, monkeypatch):
    authority, clock = owner
    retained_receipt(authority, clock)
    delayed_commit(monkeypatch, authority, clock, seconds=80)
    lease = acquire(authority)
    attempts = details(lease_acquisition_success_evidence(authority, lease))
    assert [item["attempt"] for item in attempts] == [1, 2]
    assert all(item["phases"]["receipt_read"]["count"] == 1 for item in attempts)
    clock.advance(61)
    next_lease = acquire(authority)
    next_attempt, = details(lease_acquisition_success_evidence(authority, next_lease))
    assert next_attempt["attempt"] == 1 and next_attempt["phases"]["receipt_validate"]["count"] == 1
    with pytest.raises(AuthorityError):
        acquire(authority, ttl_seconds=0)
    assert lease_acquisition_success_evidence(authority, next_lease) is None


def test_diagnostics_require_active_acquisition_attempt_and_exact_connection(owner):
    authority, clock = owner
    lease = acquire(authority)
    trace = authority_module._LeaseAcquisitionTrace()
    trace.advance("receipt_recovery", 1)
    with authority._transaction() as db:
        with trace.receipt_recovery(db, 1):
            with sqlite3.connect(authority.path) as other:
                authority._recover_submission_receipts(other, clock(), lease.epoch)
            bound = trace.receipt_recoveries[0]
            assert all(phase["count"] == 0 for phase in bound.phases.values())
            trace.advance("grant_timestamp", 1)
            authority._recover_submission_receipts(db, clock(), lease.epoch)
            assert all(phase["count"] == 0 for phase in bound.phases.values())
    assert authority_module._RECEIPT_RECOVERY_CONTEXT.get() is None


@pytest.mark.parametrize("clock_name", ["monotonic", "thread_time"])
@pytest.mark.parametrize("fault", ["exception", "nan", "inf", "bool"])
def test_diagnostic_clock_failures_do_not_change_recovery_or_emit_values(owner, monkeypatch, clock_name, fault):
    authority, clock = owner
    before, _ = retained_receipt(authority, clock)
    def bad():
        if fault == "exception":
            raise ValueError("private-clock-detail")
        return {"nan": float("nan"), "inf": float("inf"), "bool": True}[fault]
    monkeypatch.setattr(authority_module.time, clock_name, bad)
    lease = acquire(authority)
    evidence = lease_acquisition_success_evidence(authority, lease)
    attempt, = details(evidence)
    assert attempt["diagnostics_unavailable" if clock_name == "monotonic" else "thread_cpu_unavailable"]
    assert authority.reservation(before.reservation_id).broker_order_id == "101"
    assert "private" not in json.dumps(evidence, allow_nan=False)


def test_export_copies_only_fixed_schema_and_detaches_every_nested_level(owner):
    authority, clock = owner
    before, receipt = retained_receipt(authority, clock)
    lease = acquire(authority)
    evidence = lease_acquisition_success_evidence(authority, lease)
    evidence["receipt_recovery_details"]["private"] = "private-token"
    evidence["receipt_recovery_details"]["attempts"][0]["private"] = before.reservation_id
    evidence["receipt_recovery_details"]["attempts"][0]["phases"]["private"] = receipt
    evidence["receipt_recovery_details"]["attempts"][0]["phases"]["projection"]["private"] = lease.token
    trace = execution._ExecutionTrace()
    trace.lease_acquisition = evidence
    first = trace.snapshot()["lease_acquisition"]
    assert "private" not in json.dumps(first)
    first["receipt_recovery_details"]["attempts"][0]["phases"]["projection"]["count"] = 999
    assert details(trace.snapshot()["lease_acquisition"])[0]["phases"]["projection"]["count"] == 1
    assert details(lease_acquisition_success_evidence(authority, lease))[0]["phases"]["projection"]["count"] == 1
    evidence["receipt_recovery_details"]["attempts"][0]["phases"]["projection"]["count"] = "private-token"
    assert receipt_recovery_details_evidence(evidence) is None
    assert "receipt_recovery_details" not in trace.snapshot()["lease_acquisition"]


def test_optional_snapshot_export_failure_does_not_change_success_or_original_failure(owner, monkeypatch):
    authority, clock = owner
    before, _ = retained_receipt(authority, clock)
    def failed(*args):
        raise ValueError("private-export-detail")
    monkeypatch.setattr(authority_module._ReceiptRecoveryTimings, "snapshot", failed)
    lease = acquire(authority)
    evidence = lease_acquisition_success_evidence(authority, lease)
    assert evidence["diagnostics_unavailable"] is True and "receipt_recovery_details" not in evidence
    assert authority.reservation(before.reservation_id).broker_order_id == "101"
    clock.advance(61)
    failure = AuthorityError("private-original-failure")
    monkeypatch.setattr(authority, "_submission_receipt_value", lambda raw: (_ for _ in ()).throw(failure))
    with pytest.raises(AuthorityError) as caught:
        acquire(authority)
    assert caught.value is failure
    assert lease_acquisition_failure_evidence(failure)["diagnostics_unavailable"] is True


def test_normal_operation_exports_details_and_export_failure_stays_optional(env, monkeypatch):
    env.executor.diagnostic_session_id = "manual-atlas-15m:" + "a" * 32
    assert env.executor.recover() == []
    with env.store.connect() as db:
        saved = json.loads(db.execute("SELECT payload FROM events WHERE kind='execution_operation_diagnostics'").fetchone()[0])
    attempt, = details(saved["diagnostics"]["lease_acquisition"])
    assert attempt["phases"]["receipt_read"]["count"] == 1
    def failed(*args):
        raise ValueError("private-export-detail")
    monkeypatch.setattr(execution, "receipt_recovery_details_evidence", failed)
    assert env.executor.recover() == []
    assert not env.broker.submissions and not env.broker.cancellations


def test_diagnostics_do_not_add_sql_or_change_noop_receipt_validation(owner):
    authority, clock = owner
    retained_receipt(authority, clock)
    lease = acquire(authority)
    with authority._transaction() as db:
        baseline, instrumented = [], []
        db.set_trace_callback(baseline.append)
        assert authority._recover_submission_receipts(db, clock(), lease.epoch) == []
        trace = authority_module._LeaseAcquisitionTrace()
        trace.advance("receipt_recovery", 1)
        db.set_trace_callback(instrumented.append)
        with trace.receipt_recovery(db, 1):
            assert authority._recover_submission_receipts(db, clock(), lease.epoch) == []
        db.set_trace_callback(None)
        assert instrumented == baseline
        assert len(baseline) == 2  # Receipt inventory and the same exact reservation lookup.
