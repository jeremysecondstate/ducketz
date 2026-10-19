"""Slow Gameplan batches recapture current state without replaying orders."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from test_account_gameplan_execution import account_env, run, native, state
from test_gameplan_direction_runtime import prepare, run as native_run
from test_independent_stock_runtime import ACCOUNT, environment
from test_independent_stock_session import Clock, _shared_lock_session_fixture
from ml.stock_trader import independent_runtime as runtime, independent_session as worker
from ml.stock_trader.horizon_ledger import OrderEvidence, FillEvidence


def test_slow_account_batch_immediately_recaptures_and_replans_only_unsubmitted_tail(account_env, monkeypatch):
    env = account_env
    monkeypatch.setattr(worker, '_has_inventory', lambda _: True)
    monkeypatch.setattr(worker, 'read_gameplan_stock_activation_intent', lambda _: SimpleNamespace(active=env.active))
    env.broker.before_post_hook = lambda: setattr(env, 'now', env.now + pd.Timedelta(seconds=46))
    results, snapshots, sleeps = [], [], []
    forecasts = {s.prediction_id: (s.target_window_start, s.target_window_end, s.calibrated_probability)
                 for s in env.signals.values()}

    def runner(root, **kwargs):
        result = run(env)
        results.append(result)
        snapshots.append(json.loads((result.run_directory / 'decisions.json').read_text()))
        if len(results) == 1:
            assert result.status == 'BROKER_STATE_REFRESH_REQUIRED' and result.submitted_orders == 1
            assert result.error is None
            prior, = native(env).snapshot().reservations
            assert len(state(env)['reservations']) == 1  # No tail intent was reserved.
            env.now += pd.Timedelta(milliseconds=1)
            env.order_evidence = (OrderEvidence('filled-' + prior.reservation_id, prior.reservation_id, ACCOUNT,
                env.now.isoformat(), prior.broker_order_id, 'FILLED', prior.quantity, prior.quantity, 0,
                (FillEvidence('fill-' + prior.reservation_id, prior.quantity, prior.limit_price, env.now.isoformat()),)),)
            env.held[prior.symbol] = prior.quantity
            env.broker.before_post_hook = None
            env.portfolio_transform = lambda p: replace(p, available_cash=300., quotes={
                s: replace(q, bid=101., ask=101., mark=101., last=101.) for s, q in p.quotes.items()})
        else:
            assert result.status == 'ORDERS_SUBMITTED' and result.submitted_orders == 1, result.error
            env.active = False
        return result

    outcome = worker.run_independent_stock_session(env.root, execute=True, clock=lambda: env.now,
        sleep=lambda seconds: sleeps.append(seconds), runner=runner, reporter=lambda _: None,
        sizing_policy=worker.GAMEPLAN_SIZING_POLICY)
    assert len(results) == env.captures == 2
    assert outcome['orders_submitted'] == 2 and len(env.broker.submissions) == 2
    assert sleeps == [5]  # Only the completed second cycle sleeps.
    orders = env.broker.submissions
    assert orders[0]['orderLegCollection'][0]['instrument']['symbol'] != orders[1]['orderLegCollection'][0]['instrument']['symbol']
    assert float(orders[1]['price']) == 101. and orders[1]['orderLegCollection'][0]['quantity'] <= 2
    assert snapshots[0]['decided_at'] < snapshots[1]['decided_at']
    for snapshot in snapshots:
        for decision in snapshot['decisions']:
            prediction = decision['prediction']
            if prediction['prediction_id'] in forecasts:
                assert (prediction['target_window_start'], prediction['target_window_end'],
                        prediction['calibrated_probability']) == forecasts[prediction['prediction_id']]
    assert len({r['broker_order_id'] for r in state(env)['reservations']}) == 2


def test_zero_progress_still_uses_unchanged_before_post_quote_guard(environment, monkeypatch):
    env = environment
    prepare(env, monkeypatch, probability=.7)
    env.signals = {('AAPL', '1h'): env.signals['AAPL', '1h']}
    capture = runtime.capture_portfolio_state
    def stale_quote(*args, **kwargs):
        portfolio = capture(*args, **kwargs)
        return replace(portfolio, quotes={s: replace(q, observed_at=(env.now-pd.Timedelta(seconds=59)).isoformat())
                                          for s, q in portfolio.quotes.items()})
    monkeypatch.setattr(runtime, 'capture_portfolio_state', stale_quote)
    env.broker.before_post_hook = lambda: setattr(env, 'now', env.now + pd.Timedelta(seconds=2))
    result = native_run(env)
    assert result.status == 'SUBMISSION_STOPPED_SAFETY_CHECK'
    assert 'CURRENT_QUOTE_TOO_OLD_FOR_SUBMISSION' in result.error
    assert result.submitted_orders == 0 and env.broker.submissions == []
    assert [r.status for r in native(env).snapshot().reservations] == ['REJECTED']


def test_unknown_submission_retains_reservations_and_never_becomes_refresh(account_env):
    env = account_env
    env.broker.unknown_submission = True
    env.broker.before_post_hook = lambda: setattr(env, 'now', env.now + pd.Timedelta(seconds=46))
    first = run(env)
    assert first.status == 'SUBMISSION_STOPPED_AFTER_ERROR' and first.submitted_orders == 0
    assert [r['status'] for r in state(env)['reservations']] == ['UNKNOWN']
    env.now += pd.Timedelta(seconds=1)
    env.broker.before_post_hook = None
    second = run(env)
    assert second.submitted_orders == 0 and len(env.broker.submissions) == 1
    assert state(env)['reservations'][0]['status'] == 'UNKNOWN'


@pytest.mark.parametrize('boundary', ['manual_stop', 'close'])
def test_immediate_refresh_rechecks_manual_control_and_session_close(tmp_path, monkeypatch, boundary):
    clock, active, _, status_path = _shared_lock_session_fixture(tmp_path, monkeypatch, '2026-09-08T23:58:30Z', inventory=True)
    calls = []
    def runner(root, **kwargs):
        calls.append(clock())
        if boundary == 'manual_stop':
            active['value'] = False
        else:
            clock.now = pd.Timestamp('2026-09-09T00:00:00Z')
        return SimpleNamespace(submitted_orders=1, to_dict=lambda: {
            'status': 'BROKER_STATE_REFRESH_REQUIRED', 'submitted_orders': 1,
            'error': None, 'broker_state_capture': {'status': 'CURRENT'}})
    result = worker.run_independent_stock_session(tmp_path, clock=clock, sleep=lambda _: pytest.fail('No extra wait'),
        runner=runner, reporter=lambda _: None, sizing_policy=worker.GAMEPLAN_SIZING_POLICY)
    assert len(calls) == result['calls'] == result['orders_submitted'] == 1
    assert json.loads(status_path.read_text())['failed_cycles'] == 0


@pytest.mark.parametrize('submitted,error', [(0, None), (1, 'A real error remains')])
def test_no_progress_or_error_does_not_create_fast_refresh_loop(tmp_path, monkeypatch, submitted, error):
    clock, active, _, _ = _shared_lock_session_fixture(tmp_path, monkeypatch, '2026-09-08T22:02:00Z', inventory=True)
    calls, sleeps = [], []
    def runner(root, **kwargs):
        calls.append(clock())
        return SimpleNamespace(submitted_orders=submitted, to_dict=lambda: {
            'status': 'BROKER_STATE_REFRESH_REQUIRED', 'submitted_orders': submitted,
            'error': error, 'broker_state_capture': {'status': 'CURRENT'}})
    def sleep(seconds):
        sleeps.append(seconds)
        active['value'] = False
    worker.run_independent_stock_session(tmp_path, clock=clock, sleep=sleep, runner=runner,
        reporter=lambda _: None, sizing_policy=worker.GAMEPLAN_SIZING_POLICY)
    assert len(calls) == len(sleeps) == 1
