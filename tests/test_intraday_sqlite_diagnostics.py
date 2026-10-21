"""Own-authority contention diagnostics, with real temporary SQLite failures."""
from dataclasses import replace
from datetime import datetime, timezone
import json
import socket
import sqlite3

import pytest

from ml.account_gameplan.authority import AccountAuthority, AuthorityBinding, AuthorityError
from ml.intraday import account_execution as execution
from ml.intraday.prediction import ContractError


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("SQLite diagnostics regressions must never connect to a provider")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.fixture
def authority_and_lease(tmp_path):
    now = datetime(2026, 10, 19, 17, tzinfo=timezone.utc)
    authority = AccountAuthority(tmp_path / "private-account-fixture.sqlite3",
        AuthorityBinding("a" * 64, "Atlas", {"Atlas": ("AMZN", "COST")}), clock=lambda: now)
    lease = authority.acquire_lease(coordinator_id="Atlas", holder_id="private-holder-fixture")
    return authority, lease


@pytest.mark.parametrize("wrapped", [False, True])
def test_background_busy_preserves_original_failure_and_local_origin(authority_and_lease, monkeypatch, wrapped):
    authority, lease = authority_and_lease
    connect = sqlite3.connect
    blocker = connect(authority.path, timeout=0, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    original_renew = authority.renew_lease
    captured = []
    private_message = "private-account-url-and-auth-fixture"

    def fail_fast_connect(*args, **kwargs):
        kwargs["timeout"] = 0
        return connect(*args, **kwargs)

    def renew(token):
        try:
            return original_renew(token)
        except sqlite3.OperationalError as exc:
            captured.append(exc)
            if wrapped:
                raise AuthorityError(private_message) from exc
            raise

    monkeypatch.setattr(sqlite3, "connect", fail_fast_connect)
    monkeypatch.setattr(authority, "renew_lease", renew)
    monkeypatch.setattr(execution._LeaseKeeper, "_wait", lambda keeper, seconds: keeper._stop.is_set())
    keeper = execution._LeaseKeeper(authority, lease, active=lambda: True)
    try:
        keeper.start()
        assert keeper._finished.wait(5), "The real renewal thread must finish its failed write"
        keeper.stop()
        assert len(captured) == 1
        original = captured[0]
        assert original.sqlite_errorcode == sqlite3.SQLITE_BUSY
        failure = keeper.failure
        assert (failure.__cause__ if wrapped else failure) is original
        assert keeper.lease is lease
        for operation in (keeper.check, keeper.renew):
            with pytest.raises(AuthorityError if wrapped else sqlite3.OperationalError) as repeated:
                operation()
            assert repeated.value is failure
        trace = execution._ExecutionTrace()
        trace.advance("capacity_publish")
        trace.remember(failure)
        evidence = execution.execution_failure_evidence(failure)
        assert evidence["execution_stage"] == "capacity_publish"
        assert evidence["exception_chain"][0]["local_operation"] == "authority.renew_lease"
        assert evidence["root_cause"]["sqlite_errorcode"] == sqlite3.SQLITE_BUSY
        assert evidence["root_cause"]["sqlite_errorname"] == "SQLITE_BUSY"
        assert evidence["root_cause"]["error_type"] == "OperationalError"
        if wrapped:
            assert "local_operation" not in evidence["root_cause"]
        serialized = json.dumps(evidence)
        for secret in (private_message, str(authority.path), lease.token, lease.holder_id, "database is locked"):
            assert secret not in serialized
        assert len(captured) == 1, "Rethrowing a latched failure must not retry the authority write"
    finally:
        keeper.stop()
        blocker.rollback()
        blocker.close()


def test_manual_stop_is_not_attributed_to_authority_renewal(authority_and_lease, monkeypatch):
    authority, lease = authority_and_lease
    monkeypatch.setattr(authority, "renew_lease", lambda token: pytest.fail("A manual stop must prevent renewal"))
    keeper = execution._LeaseKeeper(authority, lease, active=lambda: False)
    with pytest.raises(ContractError) as stopped:
        keeper.renew()
    assert keeper.failure is stopped.value
    evidence = execution._submission_failure_evidence(stopped.value, "capacity_publish")
    assert evidence["root_cause"] == {
        "error_type": "ContractError", "message": "Manual 15m session is no longer active"}
    with pytest.raises(ContractError) as repeated:
        keeper.renew()
    assert repeated.value is stopped.value
    repeated_evidence = execution._submission_failure_evidence(repeated.value, "capacity_publish")
    # Rethrowing the same latched exception extends its traceback. The cause
    # and attribution stay exact; bounded locations retain the original root.
    assert {key: value for key, value in repeated_evidence.items() if key != "exception_locations"} == {
        key: value for key, value in evidence.items() if key != "exception_locations"}
    original_location, = evidence["exception_locations"]
    current_location, = repeated_evidence["exception_locations"]
    assert {key: value for key, value in current_location.items() if key != "frames"} == {
        key: value for key, value in original_location.items() if key != "frames"}
    assert len(current_location["frames"]) <= 8
    assert current_location["frames"][-2:] == original_location["frames"][-2:]
    assert all("local_operation" not in item for item in repeated_evidence["exception_chain"])


def test_returned_identity_mismatch_is_not_a_renewal_exception(authority_and_lease, monkeypatch):
    authority, lease = authority_and_lease
    monkeypatch.setattr(authority, "renew_lease", lambda token: replace(lease, epoch=lease.epoch + 1))
    keeper = execution._LeaseKeeper(authority, lease, active=lambda: True)
    with pytest.raises(AuthorityError) as mismatch:
        keeper.renew()
    assert execution._submission_failure_evidence(mismatch.value, "fixture")["root_cause"] == {
        "error_type": "AuthorityError", "message": "CURRENT_FENCED_LEASE_REQUIRED"}


@pytest.mark.parametrize("error_type,code,name", [
    (RuntimeError, sqlite3.SQLITE_BUSY, "SQLITE_BUSY"),
    (sqlite3.OperationalError, sqlite3.SQLITE_BUSY, "private-fixture-error-name"),
    (sqlite3.OperationalError, sqlite3.SQLITE_LOCKED, "SQLITE_BUSY"),
    (sqlite3.OperationalError, True, "SQLITE_ERROR"),
    (sqlite3.OperationalError, "private-fixture-code", "SQLITE_BUSY"),
])
def test_untrusted_sqlite_and_origin_attributes_cannot_supply_diagnostics(error_type, code, name):
    failure = error_type("private-fixture-message")
    failure.sqlite_errorcode = code
    failure.sqlite_errorname = name
    failure.intraday_authority_renewal_failure = "private-fixture-origin"
    assert execution._submission_failure_evidence(failure, "fixture")["root_cause"] == {
        "error_type": error_type.__name__}
