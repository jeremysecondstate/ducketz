"""Offline execution failures retain code locations without credential content."""
import json

import pytest

from ml.intraday import account_execution as execution


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Network is forbidden in this regression")
    monkeypatch.setattr("socket.create_connection", denied)
    monkeypatch.setattr("socket.socket.connect", denied)
    monkeypatch.setattr("socket.socket.connect_ex", denied)


def permission_failure():
    try:
        error = PermissionError(13, "private-credential-content", "C:/private/account/cache.json")
        error.winerror = 5
        error.stock_trader_operation = "authentication"
        raise error
    except PermissionError as error:
        return error


def test_retry_trace_retains_permission_location_and_os_codes_without_private_content():
    error = permission_failure()
    trace = execution._ExecutionTrace()
    trace.advance("capture_lease_current")
    trace.remember(error)
    evidence = execution.execution_failure_evidence(error)
    assert evidence["execution_stage"] == "capture_lease_current"
    assert evidence["root_cause"] == {"error_type": "PermissionError", "operation": "authentication"}
    location, = evidence["exception_locations"]
    assert location["chain_index"] == 0 and location["errno"] == 13 and location["winerror"] == 5
    frame = location["frames"][-1]
    assert frame["file"] == "test_intraday_execution_error_locations.py"
    assert frame["function"] == "permission_failure" and frame["line"] > 0
    saved = json.dumps(evidence)
    assert "private-credential-content" not in saved and "C:/private/account" not in saved
    assert "cache.json" not in saved


def test_locations_follow_cause_and_bound_frames_and_chain():
    def recurse(depth):
        if depth:
            return recurse(depth - 1)
        raise permission_failure()
    try:
        recurse(20)
    except PermissionError as inner:
        error = inner
        for _ in range(12):
            outer = execution.SubmissionNotStarted("private-wrapper-message")
            outer.__cause__ = error
            error = outer
        bounded = execution._submission_failure_evidence(error, "fixture")
        assert len(bounded["exception_locations"]) == 8 and bounded["exception_chain_truncated"]
        assert [item["chain_index"] for item in bounded["exception_locations"]] == list(range(8))
        outer.__cause__ = inner
        evidence = execution._submission_failure_evidence(outer, "fixture")
        assert len(evidence["exception_locations"]) == 2
        assert len(evidence["exception_locations"][1]["frames"]) == 8
        assert evidence["exception_locations"][1]["winerror"] == 5
        assert "private-wrapper-message" not in json.dumps(evidence)


def test_suppressed_context_and_untrusted_os_attributes_are_not_saved():
    error = permission_failure()
    error.errno = "private-errno"
    error.winerror = True
    outer = execution.ContractError("private-message")
    outer.__context__ = error
    outer.__suppress_context__ = True
    evidence = execution._submission_failure_evidence(outer, "fixture")
    assert len(evidence["exception_locations"]) == 1
    outer.__cause__ = error
    evidence = execution._submission_failure_evidence(outer, "fixture")
    assert "errno" not in evidence["exception_locations"][1]
    assert "winerror" not in evidence["exception_locations"][1]
    error.__cause__ = error
    evidence = execution._submission_failure_evidence(error, "fixture")
    assert len(evidence["exception_locations"]) == 1 and evidence["exception_chain_truncated"]
    assert "private-errno" not in json.dumps(evidence)
