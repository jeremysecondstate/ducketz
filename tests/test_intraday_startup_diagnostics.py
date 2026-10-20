"""Offline startup failures with fixture clients and temporary session records."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
from types import ModuleType, SimpleNamespace

import pytest

from ml.intraday import live_session
from ml.intraday.prediction import instant


PRIVATE_MARKER = "private-startup-fixture-key-and-account"


def _credential_permission_error():
    credential_local = PRIVATE_MARKER
    try:
        error = PermissionError(13, credential_local, "C:/private/" + credential_local)
        error.winerror = 5
        raise error
    except PermissionError as error:
        return error


def _wrapped_credential_error():
    try:
        raise _credential_permission_error()
    except PermissionError as cause:
        try:
            raise RuntimeError(PRIVATE_MARKER) from cause
        except RuntimeError as error:
            return error


def _assert_safe_frames(evidence):
    for item in evidence["exception_chain"]:
        assert set(item) <= {"error_type", "frames", "errno", "winerror"}
        assert len(item["frames"]) <= 8
        for frame in item["frames"]:
            assert set(frame) == {"file", "function", "line"}
            assert Path(frame["file"]).name == frame["file"]
            assert "/" not in frame["file"] and "\\" not in frame["file"]
            assert isinstance(frame["function"], str)
            assert type(frame["line"]) is int and frame["line"] > 0
    assert PRIVATE_MARKER not in json.dumps(evidence)


@pytest.fixture
def startup(tmp_path, monkeypatch):
    """Inject every startup client before run_manual can construct real clients."""
    calls = []
    state = SimpleNamespace(provider_error=None, broker_error=None, executor_error=None,
                            close_error=None, calls=calls, root=tmp_path)

    def module(name, **values):
        fake = ModuleType(name)
        vars(fake).update(values)
        monkeypatch.setitem(sys.modules, name, fake)
        return fake

    def provider():
        calls.append("provider")
        if state.provider_error is not None:
            raise state.provider_error
        return SimpleNamespace(dataset="EQUS.MINI")

    def broker():
        calls.append("broker")
        if state.broker_error is not None:
            raise state.broker_error
        return object()

    def executor(**kwargs):
        calls.append("executor")
        if state.executor_error is not None:
            raise state.executor_error
        pytest.fail("Fixture startup must fail before any worker starts")

    class Collector:
        def __init__(self, *args, **kwargs):
            calls.append("collector")

        def start(self):
            calls.append("collector_start")
            return {"fixture": "offline"}

        def close(self):
            calls.append("collector_close")
            if state.close_error is not None:
                raise state.close_error

    module("app.services.schwab", SchwabSession=broker)
    module("app.services.databento_market_data", DatabentoMarketDataProvider=provider)
    module("dotenv", load_dotenv=lambda *args: None)
    module("ml.intraday.account_execution", IntradayAccountExecutor=executor,
           execution_failure_evidence=lambda *args: pytest.fail("Execution diagnostics called during startup"))
    module("ml.intraday.local_execution", load_local_binding=lambda *args:
           SimpleNamespace(actor="Atlas", symbols=("AAPL",)))
    module("ml.intraday.data", AcquisitionJournal=lambda *args: object(),
           load_local_history=lambda *args, **kwargs: object())
    module("ml.intraday.coverage", RECOVERY_POPULATION_CONTRACT="offline-recovery-fixture")
    module("ml.intraday.cancellation", retry_delay=lambda *args: 3)
    module("ml.intraday.market_inputs", MarketInputCollector=Collector)
    monkeypatch.setattr(live_session, "local_symbols", lambda *args: ("Atlas", ("AAPL",)))
    monkeypatch.setattr(live_session, "with_local_data_policy", lambda config, root: config)
    monkeypatch.setattr(live_session, "ShadowRunner", lambda **kwargs: object())

    def run():
        return live_session.run_manual(
            repository=tmp_path, datastore=tmp_path, profile=tmp_path / "profile.json",
            watchlist=tmp_path / "watchlist.txt",
            config=live_session.ResearchConfig(market_inputs_enabled=True),
            clock=lambda: instant("2026-10-20T18:00:00Z"),
            sleep=lambda *args: pytest.fail("Startup failure reached worker sleep"),
        )

    def records():
        status = json.loads((tmp_path / "intraday/manual-session.json").read_text())
        with sqlite3.connect(tmp_path / "intraday/shadow.sqlite3") as db:
            rows = db.execute("SELECT payload FROM events WHERE kind='manual_startup_stopped' ORDER BY id").fetchall()
        assert len(rows) == 1
        return status, json.loads(rows[0][0])

    state.run, state.records = run, records
    return state


@pytest.mark.parametrize("failure_at,stage,expected_calls", [
    ("provider", "provider_setup", ["provider"]),
    ("broker", "broker_session", ["provider", "collector", "collector_start", "broker", "collector_close"]),
    ("executor", "executor_setup", ["provider", "collector", "collector_start", "broker", "executor", "collector_close"]),
])
def test_startup_failure_persists_exact_phase_and_bounded_private_cause(startup, failure_at, stage, expected_calls):
    failure = _wrapped_credential_error()
    setattr(startup, failure_at + "_error", failure)
    with pytest.raises(RuntimeError) as caught:
        startup.run()
    assert caught.value is failure
    status, event = startup.records()
    assert startup.calls == expected_calls
    assert status["status"] == "FAILED" and status["stop_reason"] == "startup_failure"
    assert status["broker_orders_automatically_cancelled"] is False
    assert event["error_type"] == "RuntimeError" and event["message"] == "Startup failed"
    evidence = event["diagnostics"]
    assert evidence["startup_stage"] == stage
    assert [item["error_type"] for item in evidence["exception_chain"]] == ["RuntimeError", "PermissionError"]
    assert evidence["root_cause"] == evidence["exception_chain"][-1]
    assert evidence["root_cause"]["errno"] == 13
    assert evidence["root_cause"]["winerror"] == 5
    assert any(frame["function"] == "_credential_permission_error" for frame in evidence["root_cause"]["frames"])
    assert evidence["exception_chain_truncated"] is False
    _assert_safe_frames(evidence)
    assert PRIVATE_MARKER not in json.dumps(event)
    assert str(startup.root) not in json.dumps(event)


def test_startup_cleanup_failure_is_secondary_to_original_broker_failure(startup):
    failure = _credential_permission_error()
    startup.broker_error = failure
    startup.close_error = ValueError(PRIVATE_MARKER)
    with pytest.raises(PermissionError) as caught:
        startup.run()
    assert caught.value is failure
    status, event = startup.records()
    assert status["status"] == "FAILED" and status["error_type"] == "PermissionError"
    assert event["diagnostics"]["startup_stage"] == "broker_session"
    assert event["diagnostics"]["root_cause"]["winerror"] == 5
    assert event["cleanup_diagnostics"]["startup_stage"] == "market_inputs_close"
    assert event["cleanup_diagnostics"]["exception_chain"][0]["error_type"] == "ValueError"
    assert startup.calls.count("collector_close") == 1
    assert "executor" not in startup.calls
    _assert_safe_frames(event["diagnostics"])
    _assert_safe_frames(event["cleanup_diagnostics"])
    assert PRIVATE_MARKER not in json.dumps(event)


def test_startup_keyboard_interrupt_preserves_manual_stop_without_broker_construction(startup):
    startup.provider_error = KeyboardInterrupt(PRIVATE_MARKER)
    result = startup.run()
    status, event = startup.records()
    assert result["status"] == status["status"] == "STOPPED"
    assert status["stop_reason"] == "manual_stop"
    assert status["broker_orders_automatically_cancelled"] is False
    assert event["error_type"] == "KeyboardInterrupt" and event["message"] == "Startup interrupted"
    assert event["diagnostics"]["startup_stage"] == "provider_setup"
    assert startup.calls == ["provider"]
    assert PRIVATE_MARKER not in json.dumps(event)


def test_startup_diagnostics_preserve_implicit_context_but_honor_suppression():
    def captured(suppress):
        try:
            raise _credential_permission_error()
        except PermissionError:
            try:
                if suppress:
                    raise RuntimeError(PRIVATE_MARKER) from None
                raise RuntimeError(PRIVATE_MARKER)
            except RuntimeError as error:
                return error

    evidence = live_session._startup_failure_evidence(captured(False), stage="broker_session")
    assert [item["error_type"] for item in evidence["exception_chain"]] == ["RuntimeError", "PermissionError"]
    assert evidence["root_cause"]["winerror"] == 5
    _assert_safe_frames(evidence)
    suppressed = live_session._startup_failure_evidence(captured(True), stage="broker_session")
    assert [item["error_type"] for item in suppressed["exception_chain"]] == ["RuntimeError"]
    assert suppressed["exception_chain_truncated"] is False
    _assert_safe_frames(suppressed)


def test_startup_diagnostics_bound_cause_cycles_and_chain_depth():
    error = RuntimeError(PRIVATE_MARKER)
    error.__cause__ = error
    evidence = live_session._startup_failure_evidence(error, stage="broker_session")
    assert len(evidence["exception_chain"]) == 1
    assert evidence["exception_chain_truncated"] is True
    _assert_safe_frames(evidence)

    error = _credential_permission_error()
    for _ in range(12):
        outer = RuntimeError(PRIVATE_MARKER)
        outer.__cause__ = error
        error = outer
    evidence = live_session._startup_failure_evidence(error, stage="executor_setup")
    assert len(evidence["exception_chain"]) == 8
    assert evidence["exception_chain_truncated"] is True
    assert evidence["root_cause"] == evidence["exception_chain"][-1]
    _assert_safe_frames(evidence)


def test_startup_diagnostics_bound_frames_and_reject_untyped_os_codes():
    def recursive_failure(depth):
        private_frame_local = PRIVATE_MARKER
        if depth:
            return recursive_failure(depth - 1)
        raise PermissionError(13, private_frame_local, "C:/private/" + private_frame_local)

    try:
        recursive_failure(20)
    except PermissionError as error:
        error.errno = PRIVATE_MARKER
        error.winerror = True
        evidence = live_session._startup_failure_evidence(error, stage=PRIVATE_MARKER)
    assert evidence["startup_stage"] == "unknown"
    assert len(evidence["root_cause"]["frames"]) == 8
    assert evidence["root_cause"]["frames"][-1]["function"] == "recursive_failure"
    assert "errno" not in evidence["root_cause"] and "winerror" not in evidence["root_cause"]
    _assert_safe_frames(evidence)
