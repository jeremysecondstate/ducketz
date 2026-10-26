"""Offline final-gate causality preserves freshness and the one-send fence."""
from datetime import datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ml.intraday import initial_gate_clearance as clearance
from ml.intraday import scout_client, scout_journal, scout_local_execution as execution
from ml.intraday.prediction import ContractError, instant
from test_intraday_scout_local_execution import env, record
from test_intraday_initial_gate_clearance import native_transport


def valid_metadata(**changes):
    return dict(version="scout-local-evidence-freshness-v1", account_age_seconds=61.,
        quote_age_seconds=41., maximum_age_seconds=60, gate_since_request_seconds=41.,
        failed_inputs=1, **changes)


def journal_clock(env, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return env.state.now
    monkeypatch.setattr(scout_journal, "datetime", Clock)


def expire_at(env, monkeypatch, number, seconds):
    original, calls = execution._saved_forecast, []
    def read(*args, **kwargs):
        calls.append(1)
        value = original(*args, **kwargs)
        if len(calls) == number:
            env.state.now += timedelta(seconds=seconds)
        return value
    monkeypatch.setattr(execution, "_saved_forecast", read)
    return calls


@pytest.mark.parametrize("capture_seconds,quote_age,advance,failed,account_age,final_quote_age", [
    (20, 0, 41, 1, 61, 41), (0, 20, 41, 2, 41, 61),
    (0, 0, 61, 3, 61, 61),
])
def test_native_failure_retains_exact_gate_ages_and_immutable_unknown(env, monkeypatch,
        capture_seconds, quote_age, advance, failed, account_age, final_quote_age):
    journal_clock(env, monkeypatch)
    native_transport(env, monkeypatch)
    env.capture_seconds, env.quote_age = capture_seconds, quote_age
    calls = expire_at(env, monkeypatch, 4, advance)
    result = env.executor.step(env.forecast)
    old = record(env)
    assert len(calls) == 4 and env.broker.posts == []
    assert old["state"] == "UNKNOWN" and old["send_token"] and old["broker_order_id"] is None
    failure = result["evidence"]["submission_failure"]
    item = failure["original"]["chain"][0]
    assert failure["stage"] == "send" and item["type"] == "ContractError"
    metadata = item["local_evidence_freshness"]
    assert metadata == dict(version="scout-local-evidence-freshness-v1",
        account_age_seconds=account_age, quote_age_seconds=final_quote_age,
        maximum_age_seconds=60, gate_since_request_seconds=advance, failed_inputs=failed)
    gate = instant(old["request"]["issued_at"]) + timedelta(seconds=metadata["gate_since_request_seconds"])
    assert gate == env.state.now
    assert (gate - timedelta(seconds=metadata["account_age_seconds"])).isoformat() == result["capacity"]["observed_at"]
    receipt = result["evidence"]["pretransport_clearance"]
    envelope = json.loads(Path(receipt["path"]).read_text())
    assert envelope["payload"]["version"] == clearance.NATIVE_VERSION
    observed = envelope["payload"]["observation"]["failure"]["chain"][0]
    assert observed["type"] == "ContractError" and observed["local_evidence_freshness"] == metadata
    assert clearance.load_clearance(env.executor.books, env.executor.journal, old,
        as_of=env.state.now) == receipt
    env.executor.recover()
    env.executor.step(env.forecast)
    assert record(env) == old and env.broker.posts == []


@pytest.mark.parametrize("number,state,stage,proof", [
    (2, "GRANTED", "outside_claimed_submit", False),
    (3, "UNKNOWN", "final_gate", False),
    (4, "UNKNOWN", "send", True),
])
def test_gate_position_keeps_original_claim_and_clearance_rules(env, monkeypatch,
        number, state, stage, proof):
    journal_clock(env, monkeypatch)
    calls = expire_at(env, monkeypatch, number, 61)
    result = env.executor.step(env.forecast)
    old = record(env)
    assert len(calls) == number and old["state"] == state and env.broker.posts == []
    assert result["evidence"]["submission_failure"]["stage"] == stage
    assert result["evidence"]["submission_failure"]["original"]["chain"][0]["local_evidence_freshness"]["failed_inputs"] == 3
    assert ("pretransport_clearance" in result["evidence"]) is proof


@pytest.mark.parametrize("seconds", [0, 60])
def test_valid_inclusive_boundary_sends_once_without_failure_metadata(env, monkeypatch, seconds):
    native_transport(env, monkeypatch)
    # Exact native authorization/final reads are4/5; prove the boundary seam ran.
    started_at = env.state.now
    calls = expire_at(env, monkeypatch, 4, seconds)
    def forbidden(*args, **kwargs):
        pytest.fail("Successful evidence must not invoke the failure observer")
    monkeypatch.setattr(execution, "_record_local_evidence_freshness_failure", forbidden)
    result = env.executor.step(env.forecast)
    assert len(calls) == 5 and env.state.now == started_at + timedelta(seconds=seconds)
    assert result["disposition"] == "submitted" and record(env)["state"] == "ACCEPTED"
    assert len(env.broker.posts) == 1 and "local_evidence_freshness" not in json.dumps(result)


@pytest.mark.parametrize("observer_error", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_failure_observer_cannot_replace_original_gate_rejection(env, monkeypatch, observer_error):
    calls = expire_at(env, monkeypatch, 3, 61)
    def broken(*args, **kwargs):
        raise observer_error("private-observer-token")
    monkeypatch.setattr(execution, "_record_local_evidence_freshness_failure", broken)
    result = env.executor.step(env.forecast)
    item = result["evidence"]["submission_failure"]["original"]["chain"][0]
    assert len(calls) == 3 and record(env)["state"] == "UNKNOWN" and env.broker.posts == []
    assert item["type"] == "ContractError" and "local_evidence_freshness" not in item
    assert "private-observer-token" not in json.dumps(result)
    assert all(f["function"] != "broken" for f in item["frames"])


def test_permission_persistence_phases_are_observational_and_ordered(env, monkeypatch):
    captured = []
    original = env.executor._record_execution_profile
    def profile(saved, value):
        captured.append(value)
        return original(saved, value)
    monkeypatch.setattr(env.executor, "_record_execution_profile", profile)
    assert env.executor.step(env.forecast)["disposition"] == "submitted"
    phases = [p["phase"] for p in captured[0]["phases"]]
    assert phases.index("capacity") < phases.index("permit_prepare") < phases.index("permit_persist") < phases.index("submission")
    for name in ("permit_prepare", "permit_persist"):
        phase, = (p for p in captured[0]["phases"] if p["phase"] == name)
        assert phase["status"] == "success" and phase["wall_seconds"] >= 0
    assert env.broker.posts and len(env.broker.posts) == 1


@pytest.mark.parametrize("changes", [
    {"private_token": "private-account-secret"}, {"version": "private-version"},
    {"account_age_seconds": "private-account-secret"}, {"account_age_seconds": True},
    {"account_age_seconds": float("nan")}, {"quote_age_seconds": float("inf")},
    {"gate_since_request_seconds": float("-inf")}, {"account_age_seconds": 86401},
    {"quote_age_seconds": -86401}, {"gate_since_request_seconds": 86401},
    {"maximum_age_seconds": True}, {"maximum_age_seconds": 60.},
    {"maximum_age_seconds": 0}, {"maximum_age_seconds": 301},
    {"failed_inputs": True}, {"failed_inputs": 0}, {"failed_inputs": 4},
    {"failed_inputs": 3}, {"account_age_seconds": 60}, {"quote_age_seconds": 61},
])
def test_formatter_rejects_malformed_or_inconsistent_metadata(changes):
    error = ContractError("private-account-secret")
    error.scout_local_evidence_freshness = {**valid_metadata(), **changes}
    details = scout_client.exception_details(error)
    assert "local_evidence_freshness" not in details["chain"][0]
    assert "private-account-secret" not in json.dumps(details)


def test_formatter_copies_only_matching_causal_contract_failure():
    cause = ContractError("private-cause")
    cause.scout_local_evidence_freshness = valid_metadata()
    outer = ValueError("private-account-secret")
    outer.__cause__ = cause
    outer.scout_local_evidence_freshness = valid_metadata()
    details = scout_client.exception_details(outer)
    assert "local_evidence_freshness" not in details["chain"][0]
    assert details["chain"][1]["local_evidence_freshness"] == valid_metadata()
    assert details["chain"][1]["local_evidence_freshness"] is not cause.scout_local_evidence_freshness
    assert "private-account-secret" not in json.dumps(details)


@pytest.mark.parametrize("observer_error", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_formatter_observer_failure_is_nonfatal(monkeypatch, observer_error):
    error = ContractError("private-account-secret")
    def broken(*args):
        raise observer_error("private-observer-secret")
    monkeypatch.setattr(scout_client, "_local_evidence_freshness_details", broken)
    details = scout_client.exception_details(error)
    assert details["chain"][0]["type"] == "ContractError"
    assert "local_evidence_freshness" not in details["chain"][0]
    assert "private-observer-secret" not in json.dumps(details)


@pytest.mark.parametrize("account_age,quote_age,expected", [(-1, 29, 1), (29, -1, 2)])
def test_numeric_observer_preserves_future_input_identity_without_another_clock_read(account_age, quote_age, expected):
    current = instant("2026-10-23T14:00:00Z")
    error = ContractError("Current local quote/account evidence expired")
    execution._record_local_evidence_freshness_failure(error, current=current,
        account_at=current - timedelta(seconds=account_age), quote_at=current - timedelta(seconds=quote_age),
        request_issued_at=current - timedelta(seconds=10), maximum_age_seconds=60)
    metadata = scout_client.exception_details(error)["chain"][0]["local_evidence_freshness"]
    assert metadata["account_age_seconds"] == account_age and metadata["quote_age_seconds"] == quote_age
    assert metadata["failed_inputs"] == expected and metadata["gate_since_request_seconds"] == 10


def test_actual_future_quote_rejection_is_retained_before_claim(env):
    env.quote_age = -1
    result = env.executor.step(env.forecast)
    assert record(env)["state"] == "GRANTED" and env.broker.posts == []
    metadata = result["evidence"]["submission_failure"]["original"]["chain"][0]["local_evidence_freshness"]
    assert metadata["account_age_seconds"] == 0 and metadata["quote_age_seconds"] == -1
    assert metadata["failed_inputs"] == 2 and metadata["gate_since_request_seconds"] == 0


@pytest.mark.parametrize("operation,phase,state", [
    ("grant", "permit_prepare", "PREPARED"),
    ("receipt", "permit_persist", "GRANTED"),
])
def test_persistence_error_phase_preserves_original_error_and_no_effect(env, monkeypatch, operation, phase, state):
    profiles = []
    error = OSError("offline-persistence-error")
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(env.executor, "_record_execution_profile", lambda saved, value: profiles.append(value))
    if operation == "grant":
        monkeypatch.setattr(env.executor.journal, "grant", fail)
    else:
        original = env.executor.books.save_receipt
        def receipt(*args, **kwargs):
            if kwargs.get("kind") == "local-account-permit":
                raise error
            return original(*args, **kwargs)
        monkeypatch.setattr(env.executor.books, "save_receipt", receipt)
    with pytest.raises(OSError) as caught:
        env.executor.step(env.forecast)
    assert caught.value is error and record(env)["state"] == state and env.broker.posts == []
    failed, = (p for p in profiles[0]["phases"] if p["phase"] == phase)
    assert failed["status"] == "error" and failed["error_type"] == "OSError"
    assert "submission" not in [p["phase"] for p in profiles[0]["phases"]]
