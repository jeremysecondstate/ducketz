"""Data worker retries retain bounded causes without changing the data lifecycle."""
from collections import Counter
import json
import socket
import sqlite3

import pytest

from ml.intraday import live_session, runtime, scout_services
from tests.test_scout_services import data_case


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Data retry diagnostic fixture reached the network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket.socket, "sendto", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def actual_retry(tmp_path, monkeypatch, fail):
    case = data_case(tmp_path, monkeypatch)
    attempts = []
    def scoring(*args, **kwargs):
        attempts.append(1)
        fail()
    monkeypatch.setattr(live_session, "scoring_loop", scoring)
    result = scout_services.run_data_service(repository=tmp_path, datastore=tmp_path,
        release=case.release, clock=lambda:case.now[0], sleep=case.sleep)
    path = tmp_path / "intraday/services/handoff.sqlite3"
    with sqlite3.connect(path) as db:
        rows = [json.loads(raw) for raw, in db.execute(
            "SELECT payload FROM recovery WHERE kind='data_worker_retry'")]
    assert len(rows) == 1 and attempts == [1]
    detail = rows[0]
    status = json.loads((path.parent / "data-status.json").read_bytes())
    assert status["errors"] == [detail]
    assert result["status"] == "FAILED"  # Existing error history remains visible.
    assert set(case.stats.published) == set(case.symbols)
    assert Counter(case.stats.calls) == Counter({symbol:1 for symbol in case.symbols})
    assert case.stats.maximum_active <= 2 and case.stats.active == 0
    assert status["deadline"] == case.deadline.isoformat()
    assert detail["worker"] == "scoring" and detail["attempt"] == 1
    assert detail["role"] == "data" and detail["pc"] == "Scout"
    assert detail["source_session"] == result["source_session"]
    assert detail["manifest_sha256"] == case.release["manifest_sha256"]
    assert not (tmp_path / "intraday/scout/controller.json").exists()
    return detail


def test_actual_data_retry_retains_native_sqlite_cause_and_source(tmp_path, monkeypatch):
    path = tmp_path / "native-busy.sqlite3"
    locker = sqlite3.connect(path)
    locker.execute("CREATE TABLE evidence(value)")
    locker.commit()
    locker.execute("BEGIN IMMEDIATE")
    def fail():
        with sqlite3.connect(path, timeout=0) as db:
            db.execute("INSERT INTO evidence VALUES(1)")
    try:
        detail = actual_retry(tmp_path, monkeypatch, fail)
    finally:
        locker.rollback()
        locker.close()
    assert detail["error_type"] == "OperationalError"
    assert detail["message"] == "database is locked"
    assert detail["message_truncated"] is False
    chain = detail["diagnostics"]["exception_chain"]
    assert chain[0]["sqlite_errorcode"] & 0xff == sqlite3.SQLITE_BUSY
    assert chain[0]["sqlite_errorname"] == "SQLITE_BUSY"
    assert any(frame["function"] == "fail" for frame in chain[0]["frames"])
    serialized = json.dumps(detail)
    assert str(path) not in serialized and "INSERT INTO" not in serialized


def test_actual_data_retry_retains_generic_nested_cause_without_nested_messages(tmp_path, monkeypatch):
    def fail():
        try:
            raise ValueError("private nested payload must not be repeated")
        except ValueError as cause:
            raise RuntimeError("outer operation failed") from cause
    detail = actual_retry(tmp_path, monkeypatch, fail)
    assert detail["message"] == "outer operation failed"
    chain = detail["diagnostics"]["exception_chain"]
    assert [(item["type"], item["relation"]) for item in chain] == [
        ("RuntimeError", "raised"), ("ValueError", "cause")]
    assert "private nested payload" not in json.dumps(detail)
    assert set(detail["diagnostics"]) == {"schema_version", "exception_chain", "chain_truncated"}


def test_actual_data_retry_bounds_message_and_excludes_optional_dataset_payload(tmp_path, monkeypatch):
    def fail():
        error = RuntimeError("x" * 10000)
        error._intraday_dataset_build_profile = {"private_dataset": "do not duplicate" * 1000}
        raise error
    detail = actual_retry(tmp_path, monkeypatch, fail)
    assert detail["message"] == "x" * 512 and detail["message_truncated"] is True
    assert "dataset_build_profile" not in json.dumps(detail)
    assert len(json.dumps(detail)) < 12000


@pytest.mark.parametrize("broken", ["message", "diagnostics", "both"])
def test_actual_data_retry_survives_broken_diagnostic_formatting(tmp_path, monkeypatch, broken):
    class BrokenMessage(RuntimeError):
        def __str__(self):
            raise ValueError("formatter unavailable")
    if broken in {"diagnostics", "both"}:
        def unavailable(error):
            raise ValueError("causal helper unavailable")
        monkeypatch.setattr(runtime, "_symbol_exception_diagnostics", unavailable)
    def fail():
        if broken in {"message", "both"}:
            raise BrokenMessage()
        raise RuntimeError("original retry")
    detail = actual_retry(tmp_path, monkeypatch, fail)
    if broken in {"message", "both"}:
        assert detail["message_unavailable"] is True and "message" not in detail
    else:
        assert detail["message"] == "original retry"
    if broken in {"diagnostics", "both"}:
        assert detail["diagnostics_unavailable"] is True and "diagnostics" not in detail
    else:
        assert detail["diagnostics"]["exception_chain"][0]["type"] == "BrokenMessage"
