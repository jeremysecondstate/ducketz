"""Offline retry observations preserve timing, exact failures and private data."""
from datetime import datetime, timedelta
from pathlib import Path
import json
import sqlite3
from types import SimpleNamespace

import pytest

from ml.intraday import local_forecast, scout_client
from ml.intraday.prediction import TARGET_VERSION, digest, instant
from ml.stock_direction_policy import stock_action
from test_intraday_scout_local_execution import env, record


class Clock:
    def __init__(self, overshoot=0, sleep_error=None):
        self.now, self.overshoot, self.sleeps, self.reads = 0.0, overshoot, [], 0
        self.sleep_error = sleep_error

    def monotonic(self):
        self.reads += 1
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        if self.sleep_error is not None:
            raise self.sleep_error
        self.now += seconds + self.overshoot


def native_error(code=261):
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = code
    return error


def run_reads(monkeypatch, outcomes, *, overshoot=0, sleep_error=None):
    clock, calls, remaining = Clock(overshoot, sleep_error), [], list(outcomes)
    def once(path, forecast, **kwargs):
        assert path == "fixture-path" and forecast == {"forecast_id": "fixture"}
        assert kwargs == {"session_id": "fixture-session", "session_started": "fixture-start"}
        duration, result = remaining.pop(0)
        clock.now += duration
        calls.append(result)
        if isinstance(result, BaseException):
            raise result
        return result
    monkeypatch.setattr(local_forecast, "time", clock)
    monkeypatch.setattr(local_forecast, "_saved_forecast_once", once)
    try:
        value = local_forecast._saved_forecast("fixture-path", {"forecast_id": "fixture"},
            session_id="fixture-session", session_started="fixture-start")
        return SimpleNamespace(value=value, error=None, clock=clock, calls=calls)
    except BaseException as error:
        return SimpleNamespace(value=None, error=error, clock=clock, calls=calls)


@pytest.mark.parametrize("code", [5, 6, 261, 262])
def test_slow_first_contention_keeps_original_deadline_and_error(monkeypatch, code):
    error = native_error(code)
    cause = RuntimeError("private-cause")
    error.__cause__ = cause
    result = run_reads(monkeypatch, [(.5794415, error), (0, "must-not-run")])
    assert result.error is error and error.__cause__ is cause
    assert len(result.calls) == 1 and result.clock.sleeps == [] and result.clock.reads == 2
    details = scout_client.exception_details(error)
    retry = details["chain"][0]["saved_forecast_retry"]
    assert retry["attempt_count"] == 1 and retry["sleep_count"] == 0 and retry["stop_code"] == 1
    assert retry["elapsed_seconds"] == pytest.approx(.5794415)
    assert details["chain"][0]["sqlite_errorcode"] == code
    assert any(f["function"] == "once" for f in details["chain"][0]["frames"])
    assert all(f["function"] != "_record_read_retry_failure" for f in details["chain"][0]["frames"])
    assert set(details) == {"chain", "chain_truncated"}


def test_eight_attempts_and_requested_sleeps_remain_unchanged(monkeypatch):
    errors = [native_error() for _ in range(8)]
    result = run_reads(monkeypatch, [(0, error) for error in errors])
    assert result.error is errors[-1] and len(result.calls) == 8
    assert result.clock.sleeps == [.01] * 7 and result.clock.reads == 16
    assert all(not hasattr(error, "scout_saved_forecast_retry") for error in errors[:-1])
    value = scout_client.exception_details(result.error)["chain"][0]["saved_forecast_retry"]
    assert value["attempt_count"] == 8 and value["sleep_count"] == 7 and value["stop_code"] == 2
    assert value["elapsed_seconds"] == pytest.approx(.07)
    assert value["sleep_requested_seconds"] == pytest.approx(.07)


def test_sleep_overshoot_is_observed_without_another_attempt(monkeypatch):
    error = native_error()
    result = run_reads(monkeypatch, [(0, error), (0, "must-not-run")], overshoot=.4)
    assert result.error is error and len(result.calls) == 1
    assert result.clock.sleeps == [.01] and result.clock.reads == 3
    value = scout_client.exception_details(error)["chain"][0]["saved_forecast_retry"]
    assert value["stop_code"] == 3 and value["sleep_count"] == 1 and value["attempt_count"] == 1
    assert value["elapsed_seconds"] == pytest.approx(.41)


def test_deadline_after_native_retries_uses_existing_clock_read(monkeypatch):
    errors = [native_error(), native_error()]
    result = run_reads(monkeypatch, [(.1, errors[0]), (.15, errors[1])])
    assert result.error is errors[1] and len(result.calls) == 2 and result.clock.sleeps == [.01]
    value = scout_client.exception_details(result.error)["chain"][0]["saved_forecast_retry"]
    assert value["stop_code"] == 1 and value["attempt_count"] == 2 and value["sleep_count"] == 1
    assert value["elapsed_seconds"] == pytest.approx(.26)


@pytest.mark.parametrize("delay", [0, 5])
def test_success_does_not_add_failure_metadata_or_extra_clock_reads(monkeypatch, delay):
    error = native_error()
    result = run_reads(monkeypatch, [(0, error), (delay, "validated")])
    assert result.error is None and result.value == "validated" and len(result.calls) == 2
    assert result.clock.sleeps == [.01] and result.clock.reads == 3
    assert not hasattr(error, "scout_saved_forecast_retry")


@pytest.mark.parametrize("code", [None, -1, "261", True, 8, 10, 11])
def test_nonretryable_native_errors_keep_identity_without_metadata(monkeypatch, code):
    error = native_error(code)
    result = run_reads(monkeypatch, [(.7, error)])
    assert result.error is error and result.clock.sleeps == [] and len(result.calls) == 1
    assert not hasattr(error, "scout_saved_forecast_retry")
    assert "saved_forecast_retry" not in scout_client.exception_details(error)["chain"][0]


@pytest.mark.parametrize("error", [ValueError("invalid evidence"), OSError("private path")])
def test_non_sqlite_rejections_are_unchanged(monkeypatch, error):
    result = run_reads(monkeypatch, [(.7, error)])
    assert result.error is error and result.clock.reads == 1 and result.clock.sleeps == []
    assert not hasattr(error, "scout_saved_forecast_retry")


@pytest.mark.parametrize("observer_error", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_metadata_observer_failure_cannot_replace_original_exception(monkeypatch, observer_error):
    error = native_error()
    cause = ValueError("original private cause")
    error.__cause__ = cause
    def broken_observer(*args, **kwargs):
        raise observer_error("private observer failure")
    monkeypatch.setattr(local_forecast, "_record_read_retry_failure", broken_observer)
    result = run_reads(monkeypatch, [(.7, error)])
    assert result.error is error and error.__context__ is None and error.__cause__ is cause
    assert error.sqlite_errorcode == 261
    frames = scout_client.exception_details(error)["chain"][0]["frames"]
    assert any(frame["function"] == "once" for frame in frames)
    assert all(frame["function"] != "broken_observer" for frame in frames)
    assert len(result.calls) == 1 and result.clock.sleeps == []


def valid_metadata():
    return dict(version="saved-forecast-read-retry-v1", attempt_count=1,
        maximum_attempts=8, retry_budget_seconds=.25, retry_pause_seconds=.01,
        elapsed_seconds=.7, sleep_count=0, sleep_requested_seconds=0.0, stop_code=1)


@pytest.mark.parametrize("changes", [
    {"private_token": "account-secret"}, {"version": "private-version"},
    {"elapsed_seconds": "account-secret"}, {"elapsed_seconds": float("nan")},
    {"elapsed_seconds": float("inf")}, {"elapsed_seconds": 3601}, {"elapsed_seconds": -1},
    {"attempt_count": True}, {"attempt_count": 9}, {"attempt_count": 0},
    {"maximum_attempts": 9}, {"sleep_count": -1}, {"sleep_count": 8},
    {"sleep_count": 1}, {"sleep_requested_seconds": .01},
    {"retry_budget_seconds": .5}, {"retry_pause_seconds": .02},
    {"stop_code": 2}, {"stop_code": "private-stop"}, {"elapsed_seconds": .2},
])
def test_unknown_or_malformed_metadata_is_omitted_entirely(changes):
    error = native_error()
    error.scout_saved_forecast_retry = {**valid_metadata(), **changes}
    details = scout_client.exception_details(error)
    assert "saved_forecast_retry" not in details["chain"][0]
    assert "account-secret" not in json.dumps(details) and "private-stop" not in json.dumps(details)
    assert set(details) == {"chain", "chain_truncated"}


def test_allowlisted_metadata_copied_only_into_matching_causal_item():
    cause = native_error()
    cause.scout_saved_forecast_retry = valid_metadata()
    original = ValueError("https://broker/accounts/account-secret?token=private-token")
    original.__cause__ = cause
    original.scout_saved_forecast_retry = valid_metadata()
    details = scout_client.exception_details(original)
    assert "saved_forecast_retry" not in details["chain"][0]
    assert details["chain"][1]["saved_forecast_retry"] == valid_metadata()
    assert details["chain"][1]["saved_forecast_retry"] is not cause.scout_saved_forecast_retry
    assert "account-secret" not in json.dumps(details) and "private-token" not in json.dumps(details)


@pytest.mark.parametrize("observer_error", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_formatter_observer_failure_is_ignored(monkeypatch, observer_error):
    error = native_error()
    def broken_observer(*args):
        raise observer_error("private formatter failure")
    monkeypatch.setattr(scout_client, "_saved_forecast_retry_details", broken_observer)
    result = scout_client.exception_details(error)
    assert result["chain"][0]["sqlite_errorcode"] == 261
    assert "saved_forecast_retry" not in result["chain"][0]


@pytest.mark.parametrize("probability,action,disposition", [
    (.4999, "SELL", "eligible_for_account_checks"),
    (.5001, "BUY", "eligible_for_account_checks"),
    (.499900001, "HOLD", "probability_between_action_cutoffs"),
    (.5, "HOLD", "probability_between_action_cutoffs"),
    (.500099999, "HOLD", "probability_between_action_cutoffs"),
])
def test_installed_exact_neutral_guard_is_preserved(probability, action, disposition):
    reference = instant("2026-10-23T17:00:00Z")
    value = dict(pc="Scout", symbol="SDGR", mode="live", availability_basis="observed",
        reference_at=reference.isoformat(), issued_at=(reference + timedelta(minutes=16)).isoformat(),
        target_close=(reference + timedelta(minutes=45)).isoformat(), target_version=TARGET_VERSION,
        ensemble_status="full", successful_count=2, configured_count=2, probability_not_down=probability)
    forecast = {**value, "forecast_id": digest(value)}
    before = dict(forecast)
    assert stock_action(probability) == action
    assert local_forecast.forecast_disposition(forecast,
        now=reference + timedelta(minutes=17), session_started_at=reference,
        local_symbols={"SDGR"}) == disposition
    assert forecast == before


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_actual_sleep_interruption_remains_control_flow(monkeypatch, interruption):
    original = native_error()
    interrupted = interruption("offline user interruption")
    result = run_reads(monkeypatch, [(0, original)], sleep_error=interrupted)
    assert result.error is interrupted and result.error is not original
    assert len(result.calls) == 1 and result.clock.sleeps == [.01]
    assert result.clock.reads == 2
    assert not hasattr(original, "scout_saved_forecast_retry")


def test_retry_metadata_survives_genuine_adapter_clearance_without_rearming(env, monkeypatch):
    from ml.intraday import initial_gate_clearance as clearance
    from ml.intraday import scout_journal, scout_local_execution as execution
    class JournalClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return env.state.now
    monkeypatch.setattr(scout_journal, "datetime", JournalClock)
    saved_read, calls = execution._saved_forecast, []
    error = native_error()
    def read(*args, **kwargs):
        calls.append(1)
        if len(calls) == 4:
            with monkeypatch.context() as nested:
                failure = run_reads(nested, [(.5794415, error)])
            assert failure.error is error
            raise error
        return saved_read(*args, **kwargs)
    monkeypatch.setattr(execution, "_saved_forecast", read)
    result = env.executor.step(env.forecast)
    original = record(env)
    token = original["send_token"]
    assert len(calls) == 4 and token and env.broker.posts == []
    assert original["state"] == "UNKNOWN" and original["broker_order_id"] is None
    receipt = result["evidence"]["pretransport_clearance"]
    loaded = clearance.load_clearance(env.executor.books, env.executor.journal,
        original, as_of=env.state.now)
    assert receipt == loaded and receipt["commitment"] == "no_order_transport_entered"
    envelope = json.loads(Path(receipt["path"]).read_text())
    failure = envelope["payload"]["observation"]["failure"]
    assert set(failure) == {"chain", "chain_truncated"} and failure["chain_truncated"] is False
    metadata = failure["chain"][0]["saved_forecast_retry"]
    assert metadata["attempt_count"] == 1 and metadata["sleep_count"] == 0
    assert metadata["elapsed_seconds"] == pytest.approx(.5794415)
    assert result["evidence"]["submission_failure"]["original"] == failure
    env.executor.step(env.forecast)
    assert record(env) == original and record(env)["send_token"] == token
    assert env.broker.posts == []
