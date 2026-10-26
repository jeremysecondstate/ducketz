"""Exact rejected capture clocks with temporary own ledgers and fake inputs."""
from dataclasses import replace
import json

import pandas as pd
import pytest

from test_intraday_main_account_execution import env, advance, native
from ml.intraday import account_execution as execution
from ml.intraday.prediction import ContractError


def capture_age(env, monkeypatch, seconds):
    original = execution.capture_portfolio_state
    clocks = {}

    def capture(*args, **kwargs):
        portfolio = original(*args, **kwargs)
        clocks.update(checked_at=env.clock(),
            request_started_at=(env.now - pd.Timedelta(seconds=seconds)).isoformat())
        return replace(portfolio, observed_at=clocks["request_started_at"])

    monkeypatch.setattr(execution, "capture_portfolio_state", capture)
    return clocks


@pytest.mark.parametrize("age", [61, -1])
def test_rejected_capture_keeps_exact_clocks_before_cleanup(env, monkeypatch, age):
    clocks = capture_age(env, monkeypatch, age)
    original_release = execution.AccountAuthority.release_lease

    def slow_cleanup(authority, token):
        advance(env, 17)
        return original_release(authority, token)

    monkeypatch.setattr(execution.AccountAuthority, "release_lease", slow_cleanup)
    with pytest.raises(ContractError, match="Account snapshot is not fresh") as failed:
        env.executor.recover()
    evidence = execution.execution_failure_evidence(failed.value)
    assert evidence["execution_stage"] == "native_snapshot_prepare"
    assert evidence["capture_freshness"] == dict(clocks, age_seconds=age, maximum_age_seconds=60)
    assert evidence["capture_freshness"]["checked_at"] != env.clock()
    assert env.captures == (2 if age > 60 else 1)
    assert not native(env).snapshot().reservations
    assert not env.broker.submissions and not env.broker.cancellations
    encoded = json.dumps(evidence, allow_nan=False)
    assert env.config.account_fingerprint not in encoded
    assert env.executor.lease.token not in encoded
    assert "account_equity" not in encoded and "available_cash" not in encoded
    assert env.executor._execution_trace is env.executor._lease_keeper is None


@pytest.mark.parametrize("age", [0, 60])
def test_existing_inclusive_capture_boundaries_remain_accepted(env, monkeypatch, age):
    clocks = capture_age(env, monkeypatch, age)
    assert env.executor.recover() == []
    assert env.executor.portfolio.observed_at == clocks["checked_at"]
    assert env.captures == 1
    assert not env.broker.submissions and not env.broker.cancellations


def test_capture_failure_snapshot_is_detached_and_has_only_clock_fields():
    trace = execution._ExecutionTrace()
    assert "capture_freshness" not in trace.snapshot()
    trace.capture_freshness = dict(checked_at="2026-10-23T18:00:00+00:00",
        request_started_at="2026-10-23T17:58:59+00:00", age_seconds=61, maximum_age_seconds=60)
    saved = trace.snapshot()
    saved["capture_freshness"]["age_seconds"] = 999
    assert trace.snapshot()["capture_freshness"]["age_seconds"] == 61
    assert set(saved["capture_freshness"]) == {
        "checked_at", "request_started_at", "age_seconds", "maximum_age_seconds"}
