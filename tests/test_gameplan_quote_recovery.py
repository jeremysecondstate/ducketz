"""Quote refresh and operator-requested recovery with synthetic brokers only."""
from dataclasses import replace
from types import SimpleNamespace

import pandas as pd
import pytest

from test_independent_stock_runtime import environment, _decisions, ACCOUNT
from test_gameplan_direction_runtime import prepare
from test_independent_stock_session import Clock
from ml.stock_trader import independent_runtime as runtime, independent_session as worker, quote_recovery
from ml.stock_trader.horizon_ledger import HorizonLedger
from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY


def setup_quotes(env, monkeypatch):
    prepare(env, monkeypatch, probability=.7)
    env.signals = {("AAPL", "1h"): env.signals["AAPL", "1h"]}
    env.live_quote = False
    original = runtime.capture_portfolio_state
    def capture(*args, **kwargs):
        value = original(*args, **kwargs)
        return replace(value, quotes={s: replace(q,
            observed_at=(env.now-pd.Timedelta(minutes=13)).isoformat(),
            received_at=env.now.isoformat() if env.live_quote else None,
            realtime=True if env.live_quote else None, quote_type="NBBO") for s,q in value.quotes.items()})
    monkeypatch.setattr(runtime, "capture_portfolio_state", capture)
    monkeypatch.setattr(quote_recovery, "load_current_independent_gameplan_signals", lambda *a, **kw: (env.signals, ()))


def execute(env, **kwargs):
    return runtime.run_independent_stock_trader_once(env.root, execute=True, session=env.broker,
        runtime_clock=lambda: env.now, session_managed=True, sizing_policy=GAMEPLAN_SIZING_POLICY,
        **kwargs)


def test_quote_refresh_recaptures_complete_snapshot_then_submits(environment, monkeypatch):
    env = environment
    setup_quotes(env, monkeypatch)
    elapsed = [0.]
    def sleep(seconds):
        elapsed[0] += seconds
        env.now += pd.Timedelta(seconds=seconds)
        env.live_quote = True
    result = execute(env, broker_state_retry_sleep=sleep, broker_state_retry_clock=lambda: elapsed[0])
    assert result.submitted_orders == 1
    assert env.captures == 2 and elapsed[0] == 3
    assert result.broker_state_capture["quote_refresh"] == {"status":"CURRENT_AFTER_REFRESH", "refreshes":1, "unavailable_symbols":[]}


def test_exhausted_quote_refresh_is_reported_as_unavailable(environment, monkeypatch):
    env = environment
    setup_quotes(env, monkeypatch)
    elapsed = [0.]
    def sleep(seconds):
        elapsed[0] += seconds
        env.now += pd.Timedelta(seconds=seconds)
    result = execute(env, broker_state_retry_sleep=sleep, broker_state_retry_clock=lambda: elapsed[0], broker_state_retry_max_seconds=3)
    assert env.captures == 2 and elapsed[0] == 3
    assert result.status == "HORIZON_ENTRY_QUOTE_UNAVAILABLE" and result.error
    assert result.broker_state_capture["quote_refresh"]["status"] == "EXHAUSTED"
    assert env.broker.submissions == []


def test_explicit_recovery_submits_only_once_preserves_expiry_and_original_claim(environment, monkeypatch):
    env = environment
    setup_quotes(env, monkeypatch)
    skipped = execute(env, broker_state_retry_max_seconds=0)
    assert skipped.selected_orders == skipped.submitted_orders == 0
    original_claims = {p:p.read_bytes() for p in (env.root/'state/independent-stock-trader/entry-slots').glob('*.json')}
    env.now += pd.Timedelta(minutes=29)
    env.live_quote = True
    options = dict(resume_quote_run=skipped.run_directory.name, resume_quote_symbol="AAPL")
    result = execute(env, **options)
    assert result.status == "ORDERS_SUBMITTED", result.error
    assert result.submitted_orders == len(env.broker.submissions) == 1
    allocation = HorizonLedger(env.root/runtime.LEDGER_RELATIVE_PATH, ACCOUNT).snapshot().allocations[0]
    assert pd.Timestamp(allocation.target_end) == pd.Timestamp("2026-09-08T12:00:00Z")
    assert quote_recovery.recovered_entry_deadline(env.root, allocation, pd.Timestamp("2026-09-08T11:05:00Z")) == pd.Timestamp(allocation.target_end)
    assert _decisions(result)["prediction_handoff"]["quote_recovery"]["source_run"] == skipped.run_directory.name
    assert execute(env, **options).submitted_orders == 0
    assert len(env.broker.submissions) == 1
    assert {p:p.read_bytes() for p in original_claims} == original_claims


def test_recovery_rejects_expired_or_different_symbol_before_broker_reads(environment, monkeypatch):
    env = environment
    setup_quotes(env, monkeypatch)
    skipped = execute(env, broker_state_retry_max_seconds=0)
    env.now += pd.Timedelta(minutes=59)
    before = env.captures
    result = execute(env, resume_quote_run=skipped.run_directory.name, resume_quote_symbol="AAPL")
    assert result.submitted_orders == 0 and result.error
    assert env.captures == before
    with pytest.raises(ValueError, match="one unsubmitted"):
        quote_recovery.load_quote_recovery(env.root, skipped.run_directory.name, "TWST", as_of=env.now)


def test_recovery_is_rejected_for_a_decision_that_already_selected_an_order(environment, monkeypatch):
    env = environment
    setup_quotes(env, monkeypatch)
    env.live_quote = True
    submitted = execute(env)
    with pytest.raises(ValueError, match="one unsubmitted"):
        quote_recovery.load_quote_recovery(env.root, submitted.run_directory.name, "AAPL", as_of=env.now)


def test_session_recovers_once_then_runs_next_normal_hour(tmp_path, monkeypatch):
    clock = Clock("2026-09-08T22:30:00Z")
    calls = []
    monkeypatch.setattr(worker, "_has_inventory", lambda _: False)
    monkeypatch.setattr(worker, "read_gameplan_stock_activation_intent", lambda _: SimpleNamespace(active=True))
    monkeypatch.setattr(worker, "_independent_forecast_preflight", lambda *a, **kw: {"status":"READY"})
    monkeypatch.setattr(quote_recovery, "load_quote_recovery", lambda *a, **kw: ({}, (), {}))
    def runner(root, **kwargs):
        calls.append((clock(), kwargs))
        return SimpleNamespace(submitted_orders=0, to_dict=lambda:{"status":"SYNTHETIC_NO_ORDERS"})
    worker.run_independent_stock_session(tmp_path, clock=clock, sleep=clock.sleep, runner=runner,
        reporter=lambda _: None, sizing_policy=GAMEPLAN_SIZING_POLICY,
        resume_quote_run="20260908T220100.000000Z", resume_quote_symbol="AAPL")
    assert len(calls) == 2
    assert calls[0][1]["resume_quote_symbol"] == "AAPL"
    assert calls[1][0] == pd.Timestamp("2026-09-08T23:01:00Z")
    assert "resume_quote_run" not in calls[1][1]
