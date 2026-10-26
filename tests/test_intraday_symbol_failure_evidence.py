"""Real SQLite denials retain bounded evidence across the research Future seam."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import sqlite3

import pytest

from ml.intraday import runtime
from ml.intraday.data import AcquisitionPending
from ml.intraday.store import ShadowStore
from tests.test_intraday_store import forecast


REF = datetime(2026, 10, 8, 17, tzinfo=timezone.utc)
NOW = REF + timedelta(minutes=16)


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Symbol evidence regressions must not use the network")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


def test_imports_reviewed_candidate_runtime():
    expected = Path(os.environ.get("ATLAS_SYMBOL_EVIDENCE_ROOT", Path(__file__).parents[1]))
    assert Path(runtime.__file__).resolve() == (expected / "ml/intraday/runtime.py").resolve()


def make_runner(tmp_path):
    return runtime.ShadowRunner(store=ShadowStore(tmp_path / "shadow.sqlite3"), pc="Atlas",
        symbols=("AAPL", "TWST"), config=runtime.ResearchConfig(models=("fixture",), symbol_workers=2),
        clock=lambda: NOW)


def saved_success(store, cycle_id, symbol):
    return store.save_forecast(forecast(cycle_id=cycle_id, symbol=symbol, mode="live"))


def deny_input_read(path):
    private_binding = "PRIVATE_BINDING_VALUE"
    private_sql = "SELECT private_value FROM private_inputs WHERE private_value=?"
    with sqlite3.connect(path) as db:
        db.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY
                          if action == sqlite3.SQLITE_SELECT else sqlite3.SQLITE_OK)
        try:
            db.execute(private_sql, (private_binding,)).fetchall()
        except sqlite3.DatabaseError as exc:
            assert str(exc) == "not authorized"
            assert exc.sqlite_errorcode == sqlite3.SQLITE_AUTH
            raise RuntimeError("local preparation failed") from exc


def test_actual_sqlite_auth_chain_survives_future_and_preserves_originals(tmp_path, monkeypatch):
    private_database = tmp_path / "PRIVATE_DATABASE_NAME.sqlite3"
    with sqlite3.connect(private_database) as db:
        db.execute("CREATE TABLE private_inputs(private_value TEXT)")
        db.execute("INSERT INTO private_inputs VALUES(?)", ("PRIVATE_BINDING_VALUE",))
    runner = make_runner(tmp_path)
    calls = []

    def symbol(cycle_id, name, *args):
        calls.append(name)
        if name == "TWST":
            deny_input_read(private_database)
        return saved_success(runner.store, cycle_id, name)

    monkeypatch.setattr(runner, "_symbol", symbol)
    first = runner.run_cycle({}, reference_at=REF, mode="live", experiment="manual-fixture")
    failure = first["failures"]["TWST"]
    assert failure["type"] == "RuntimeError" and failure["message"] == "local preparation failed"
    assert "retryable" not in failure and first["pending_recovery_symbols"] == []
    assert first["status"] == "partial_failure" and set(first["forecasts"]) == {"AAPL"}
    chain = failure["diagnostics"]["exception_chain"]
    assert [(item["type"], item["relation"]) for item in chain] == [
        ("RuntimeError", "raised"), ("DatabaseError", "cause")]
    assert chain[1]["sqlite_errorcode"] == sqlite3.SQLITE_AUTH
    assert chain[1]["sqlite_errorname"] == "SQLITE_AUTH"
    assert chain[1]["frames"][-1]["function"] == "deny_input_read"
    assert chain[1]["frames"][-1]["file"] == Path(__file__).name
    assert chain[1]["frames"][-1]["line"] > 0
    assert not failure["diagnostics"]["chain_truncated"]
    diagnostics = json.dumps(failure["diagnostics"])
    assert all(secret not in diagnostics for secret in [str(tmp_path), "PRIVATE_DATABASE_NAME",
        "PRIVATE_BINDING_VALUE", "private_inputs", "private_value", "SELECT", "db.execute"])
    original = runner.store.forecast(first["cycle_id"], "AAPL")
    with runner.store.connect() as db:
        old_event = db.execute("SELECT id,payload FROM events WHERE cycle_id=? AND symbol='TWST' "
                               "AND kind='symbol_failure'", (first["cycle_id"],)).fetchone()
        assert json.loads(old_event[1]) == failure
        saved_cycle = json.loads(db.execute("SELECT payload FROM cycles WHERE id=?",
                                            (first["cycle_id"],)).fetchone()[0])
        assert saved_cycle["failures"]["TWST"] == failure

    # An explicit offline recovery reuses the exact successful forecast while
    # leaving its original failure event and original forecast bytes intact.
    monkeypatch.setattr(runner, "_symbol", lambda cycle, name, *args:
                        saved_success(runner.store, cycle, name))
    recovered = runner.run_cycle({}, reference_at=REF, mode="live", experiment="manual-fixture",
                                 retry_symbols=("TWST",), allow_execution=False)
    assert recovered["status"] == "completed" and not recovered["failures"]
    assert recovered["forecasts"]["AAPL"] == original
    assert calls.count("AAPL") == calls.count("TWST") == 1
    with runner.store.connect() as db:
        assert db.execute("SELECT payload FROM events WHERE id=?", (old_event[0],)).fetchone()[0] == old_event[1]
    assert runner.store.forecast(first["cycle_id"], "AAPL") == original


@pytest.mark.parametrize("explicit_cause", [True, False])
def test_exception_context_and_cause_metadata_are_distinct(explicit_cause):
    try:
        try:
            raise ValueError("PRIVATE_INNER_MESSAGE")
        except ValueError as exc:
            if explicit_cause:
                raise RuntimeError("outer") from exc
            raise RuntimeError("outer")
    except RuntimeError as exc:
        details = runtime._symbol_exception_diagnostics(exc)
    assert [item["relation"] for item in details["exception_chain"]] == [
        "raised", "cause" if explicit_cause else "context"]
    assert "PRIVATE_INNER_MESSAGE" not in json.dumps(details)


def test_chain_and_frame_bounds_cycles_and_suppression():
    namespace = {}
    exec(compile("def recurse(n):\n private_local='PRIVATE_LOCAL'\n if n:return recurse(n-1)\n"
                 " raise ValueError('PRIVATE_INNER_MESSAGE')\n",
                 "C:/PRIVATE_PARENT/private-evidence.py", "exec"), namespace)
    try:
        namespace["recurse"](24)
    except ValueError as original:
        current = original
        for _ in range(9):
            outer = RuntimeError("PRIVATE_OUTER_MESSAGE")
            outer.__cause__ = current
            outer.__traceback__ = original.__traceback__
            current = outer
        details = runtime._symbol_exception_diagnostics(current)
    assert len(details["exception_chain"]) == 6 and details["chain_truncated"]
    assert all(len(item["frames"]) == 8 and item["frames_truncated"]
               for item in details["exception_chain"])
    assert all(set(frame) == {"file", "function", "line"}
               for item in details["exception_chain"] for frame in item["frames"])
    assert all(secret not in json.dumps(details) for secret in [
        "PRIVATE_PARENT", "PRIVATE_LOCAL", "PRIVATE_INNER_MESSAGE", "PRIVATE_OUTER_MESSAGE"])
    cyclic = RuntimeError("private")
    cyclic.__context__ = cyclic
    assert runtime._symbol_exception_diagnostics(cyclic)["chain_truncated"]
    assert len(runtime._symbol_exception_diagnostics(cyclic)["exception_chain"]) == 1
    cyclic.__suppress_context__ = True
    assert not runtime._symbol_exception_diagnostics(cyclic)["chain_truncated"]


def test_acquisition_pending_remains_retryable_while_other_symbol_succeeds(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)

    def symbol(cycle_id, name, *args):
        if name == "TWST":
            raise AcquisitionPending("unavailable_coverage", 4)
        return saved_success(runner.store, cycle_id, name)

    monkeypatch.setattr(runner, "_symbol", symbol)
    result = runner.run_cycle({}, reference_at=REF, mode="live", experiment="manual-fixture")
    failure = result["failures"]["TWST"]
    assert failure["type"] == "AcquisitionPending" and failure["reason"] == "unavailable_coverage"
    assert failure["message"] == str(AcquisitionPending("unavailable_coverage", 4))
    assert failure["retryable"] is True and failure["retry_after_seconds"] == 4
    assert failure["retry_at"] == (NOW + timedelta(seconds=4)).isoformat()
    assert result["pending_recovery_symbols"] == ["TWST"] and set(result["forecasts"]) == {"AAPL"}
    assert failure["diagnostics"]["exception_chain"][0]["type"] == "AcquisitionPending"
