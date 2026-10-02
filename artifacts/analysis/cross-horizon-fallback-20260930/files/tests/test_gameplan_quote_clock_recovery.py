"""Clock-skew recovery uses actual time and synthetic broker evidence only."""
from dataclasses import replace

import pandas as pd
import pytest

from test_independent_stock_runtime import environment, _owned_allocation, _decisions
from test_gameplan_direction_runtime import prepare
from ml.stock_trader import independent_runtime as runtime
from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY


def setup_owned_bearish_signal(env, monkeypatch, *, ahead=2.3, portfolio_age=0.):
    prepare(env, monkeypatch)
    original = env.signals['SNDK', '1h']
    ledger = _owned_allocation(env, symbol="SNDK", quantity=1, manual=0)
    env.now = pd.Timestamp("2026-09-08T12:00:01Z")
    # Manual Gameplan inventory persists across the old forecast boundary.
    # A genuine new bearish instruction, not expiry, authorizes this sale.
    env.signals = {('SNDK', '1h'): replace(original, prediction_id='next-hour-bearish',
        target_window_start='2026-09-08T12:00:00Z', target_window_end='2026-09-08T13:00:00Z',
        actionable_until='2026-09-08T12:05:00Z')}
    capture = runtime.capture_portfolio_state
    captured = []

    def skewed(*args, **kwargs):
        portfolio = capture(*args, **kwargs)
        result = replace(portfolio,
            observed_at=(env.now - pd.Timedelta(seconds=portfolio_age)).isoformat(),
            quotes={symbol: replace(quote, observed_at=(env.now + pd.Timedelta(seconds=ahead)).isoformat())
                    for symbol, quote in portfolio.quotes.items()})
        captured.append(result)
        return result

    monkeypatch.setattr(runtime, "capture_portfolio_state", skewed)
    return ledger, captured


def run_bearish(env, *, advance_clock=True, deactivate=False):
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if advance_clock:
            env.now += pd.Timedelta(seconds=seconds)
        if deactivate:
            env.active = False

    result = runtime.run_independent_stock_trader_once(
        env.root, execute=True, session=env.broker, runtime_clock=lambda: env.now,
        entries=True, session_managed=True, sizing_policy=GAMEPLAN_SIZING_POLICY,
        # Isolate the clock wait from the separate complete-quote refresh loop.
        # Quote retry behavior is covered in test_gameplan_quote_recovery.
        broker_state_retry_max_seconds=0, broker_state_retry_sleep=sleep)
    return result, sleeps


def test_recent_future_quote_waits_before_bearish_sale_without_relabeling_evidence(environment, monkeypatch):
    env = environment
    ledger, captured = setup_owned_bearish_signal(env, monkeypatch)
    result, sleeps = run_bearish(env)
    assert sleeps == pytest.approx([2.301])
    assert result.status == "ORDERS_SUBMITTED" and result.submitted_orders == 1
    decision = _decisions(result)["decisions"][0]
    assert decision["action"] == "SELL" and decision["quantity"] == 1
    assert decision["quote"]["observed_at"] == captured[0].quotes["SNDK"].observed_at
    assert decision["portfolio"]["observed_at"] == captured[0].observed_at
    assert pd.Timestamp(decision["decided_at"]) >= pd.Timestamp(decision["quote"]["observed_at"])
    assert decision["prediction"]["prediction_id"] == 'next-hour-bearish'
    assert pd.Timestamp(decision["prediction"]["target_window_end"]) == pd.Timestamp('2026-09-08T13:00:00Z')
    assert decision["enrichment"]["maximum_quote_age_seconds"] == 60
    assert _decisions(result)["prediction_handoff"]["quote_timestamp_wait"]["status"] == "CURRENT_AFTER_WAIT"
    state = ledger.snapshot()
    assert state.allocations[0].target_end == '2026-09-08T12:00:00+00:00'
    assert state.allocations[0].filled_shares == state.allocations[0].reserved_sell_shares == 1


@pytest.mark.parametrize("ahead,advance,expected_wait", [(10., True, False), (2.3, False, True), (-61., True, False)])
def test_unusable_bearish_quote_is_reported_unhealthy_and_preserves_owned_stock(environment, monkeypatch, ahead, advance, expected_wait):
    env = environment
    ledger, _ = setup_owned_bearish_signal(env, monkeypatch, ahead=ahead)
    result, sleeps = run_bearish(env, advance_clock=advance)
    assert bool(sleeps) is expected_wait
    assert result.status == "HORIZON_ENTRY_QUOTE_UNAVAILABLE" and result.error
    assert result.submitted_orders == 0 and env.broker.submissions == []
    decisions = _decisions(result)
    assert 'blocked_owned_exit_quotes' not in decisions['prediction_handoff']
    assert decisions['decisions'][0]['decision_reason_code'] in {'USABLE_QUOTE_UNAVAILABLE', 'CURRENT_QUOTE_TOO_OLD'}
    assert ledger.snapshot().allocations[0].filled_shares == 1
    assert ledger.snapshot().allocations[0].reserved_sell_shares == 0


def test_wait_cannot_refresh_an_aged_portfolio(environment, monkeypatch):
    env = environment
    ledger, _ = setup_owned_bearish_signal(env, monkeypatch, portfolio_age=59.)
    result, sleeps = run_bearish(env)
    assert sleeps and result.status == "HORIZON_BROKER_RECONCILIATION_UNAVAILABLE"
    assert "BROKER_PORTFOLIO_CAPTURE_TOO_OLD" in result.error
    assert env.broker.submissions == [] and ledger.snapshot().allocations[0].filled_shares == 1


def test_deactivation_during_wait_prevents_submission(environment, monkeypatch):
    env = environment
    ledger, _ = setup_owned_bearish_signal(env, monkeypatch)
    result, sleeps = run_bearish(env, deactivate=True)
    assert sleeps and result.submitted_orders == 0
    assert "TRADER_INACTIVE_DURING_BROKER_CAPTURE" in result.error
    assert env.broker.submissions == [] and ledger.snapshot().allocations[0].filled_shares == 1


def test_wait_preserves_closing_deadline_and_execution_lead(environment, monkeypatch):
    env = environment
    ledger, _ = setup_owned_bearish_signal(env, monkeypatch)
    env.now = pd.Timestamp("2026-09-08T23:59:50Z")
    env.signals = {}  # No current bearish instruction; expiry alone cannot sell.
    result, sleeps = run_bearish(env)
    assert sleeps == [] and result.status == "NO_ORDERS_SUBMITTED"
    assert _decisions(result)["prediction_handoff"]["quote_timestamp_wait"]["status"] == "OUTSIDE_WAIT_BUDGET"
    assert env.broker.submissions == [] and ledger.snapshot().allocations[0].filled_shares == 1
