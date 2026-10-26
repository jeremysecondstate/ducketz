"""Canonical Gameplan final guards distinguish proven elapsed targets from errors."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.stock_trader import independent_runtime as runtime
from ml.stock_trader import independent_session as worker
from ml.stock_trader.horizon_ledger import HorizonLedger
from ml.stock_trader.session_reporting import session_cycle_report
from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY
from test_independent_stock_runtime import environment, ACCOUNT


@pytest.fixture
def gameplan_env(environment, monkeypatch):
    env = environment
    env.signals = {key: value for key, value in env.signals.items()
                   if key[0] in ('AAPL', 'AMZN') and key[1] == '1h'}
    capture = runtime.capture_portfolio_state
    monkeypatch.setattr(runtime, 'capture_portfolio_state',
                        lambda broker, *, observed_at, parallel, **kwargs:
                        capture(broker, observed_at=observed_at, parallel=parallel))
    monkeypatch.setattr(runtime, '_assert_execution_deployment', lambda *a, **k: None)
    monkeypatch.setattr(runtime, '_loaded_gameplan_run', lambda *a, **k: None)
    monkeypatch.setattr(runtime, '_loaded_fallback_policy', lambda *a, **k: (None, None))
    return env


def run(env, *, execute=True):
    return runtime.run_independent_stock_trader_once(env.root, execute=execute,
        session=env.broker, runtime_clock=lambda: env.now, session_managed=True,
        sizing_policy=GAMEPLAN_SIZING_POLICY)


def short_targets(env):
    deadline = env.now + pd.Timedelta(seconds=20)
    env.signals = {key: replace(value, actionable_until=deadline.isoformat())
                   for key, value in env.signals.items()}
    return deadline


def reservations(env):
    return HorizonLedger(env.root / runtime.LEDGER_RELATIVE_PATH, ACCOUNT).snapshot().reservations


def report(result):
    return session_cycle_report(result.to_dict(), as_of='2026-09-08T11:01:30Z',
        session_orders_submitted=result.submitted_orders, session_cycles=1,
        session_failed_cycles=0, session_consecutive_failures=0)


def assert_elapsed(result, deadline, *, stage, submitted=0):
    assert result.status == 'PREDICTION_TARGET_ELAPSED', result.error
    assert result.error is None and result.stopped_after_error is False
    assert result.submitted_orders == submitted
    stop = result.to_dict()['submission_stop']
    assert stop['reason'] == 'PREDICTION_TARGET_SAFETY_BOUNDARY_REACHED'
    assert pd.Timestamp(stop['actionable_until']) == deadline
    assert pd.Timestamp(stop['observed_at']) >= deadline - pd.Timedelta(seconds=5)
    assert stop['stage'] == stage and stop['execution_lead_seconds'] == 5
    assert stop['no_order_posted'] is True and stop['cleanup_complete'] is True
    assert stop['decision_id'] and stop['prediction_id'] and stop['symbol']
    assert 'expired' in report(result)['message'].lower()
    assert 'replay' in report(result)['message'].lower()


def test_actual_target_elapsed_before_intent_preserves_original_and_session(gameplan_env, monkeypatch, tmp_path):
    env = gameplan_env
    deadline = short_targets(env)
    publish = runtime.publish_decision_run
    def delayed(*a, **k):
        result = publish(*a, **k)
        env.now += pd.Timedelta(seconds=16)
        return result
    monkeypatch.setattr(runtime, 'publish_decision_run', delayed)
    result = run(env)
    assert_elapsed(result, deadline, stage='BEFORE_INTENT')
    assert env.broker.submissions == env.broker.cancellations == []
    assert reservations(env) == ()
    frozen = (result.run_directory / 'decisions.json').read_bytes()
    saved = json.loads(frozen)
    assert saved['status'] == 'ORDERS_SELECTED'
    assert all(row['prediction']['actionable_until'] == deadline.isoformat()
               for row in saved['decisions'] if row['quantity'])
    clock = SimpleNamespace(now=pd.Timestamp('2026-09-08T11:01:30Z'))
    payload = result.to_dict(); lines = []; attempts = []
    monkeypatch.setattr(worker, '_has_inventory', lambda path: True)
    monkeypatch.setattr(worker, 'read_gameplan_stock_activation_intent',
                        lambda root: SimpleNamespace(active=not attempts))
    def runner(root, **kwargs):
        attempts.append(True)
        return SimpleNamespace(submitted_orders=0, to_dict=lambda: payload)
    worker.run_independent_stock_session(tmp_path / 'session',
        clock=lambda: clock.now, sleep=lambda seconds: None, runner=runner,
        reporter=lines.append)
    session = json.loads((tmp_path / 'session/state/independent-stock-trader/session-status.json').read_bytes())
    assert session['failed_cycles'] == session['consecutive_failures'] == 0
    assert session['last_cycle']['submission_stop'] == payload['submission_stop']
    assert any('expired' in line.lower() and 'ATTENTION' not in line for line in lines)
    assert (result.run_directory / 'decisions.json').read_bytes() == frozen


def test_actual_target_elapsed_before_post_has_audited_no_send(gameplan_env):
    env = gameplan_env
    deadline = short_targets(env)
    env.broker.before_post_hook = lambda: setattr(env, 'now', env.now + pd.Timedelta(seconds=16))
    result = run(env)
    assert_elapsed(result, deadline, stage='BEFORE_POST')
    assert env.broker.submissions == env.broker.cancellations == []
    assert [row.status for row in reservations(env)] == ['REJECTED']
    event, = (env.root / 'ml/stock-trader-execution-events').glob('*/result.json')
    evidence = json.loads(event.read_bytes())
    assert evidence['status'] == 'NOT_SUBMITTED_SAFETY_CHECK'
    assert 'PREDICTION_TARGET_SAFETY_BOUNDARY_REACHED' in evidence['error']


def test_partial_batch_preserves_earlier_acceptance_and_never_replays_tail(gameplan_env):
    env = gameplan_env
    deadline = short_targets(env)
    def delay_tail():
        if env.broker.submissions:
            env.now += pd.Timedelta(seconds=16)
    env.broker.before_post_hook = delay_tail
    result = run(env)
    assert_elapsed(result, deadline, stage='BEFORE_POST', submitted=1)
    rows = deepcopy(reservations(env))
    accepted, = [row for row in rows if row.status == 'SUBMITTED']
    assert accepted.broker_order_id == '1001'
    assert sorted(row.status for row in rows) == ['REJECTED', 'SUBMITTED']
    assert report(result)['activity'] == 'ORDERS SUBMITTED'
    env.broker.before_post_hook = None
    env.now += pd.Timedelta(seconds=10)
    run(env)
    assert len(env.broker.submissions) == 1
    current = {row.reservation_id: row for row in reservations(env)}
    assert current[accepted.reservation_id].broker_order_id == accepted.broker_order_id


@pytest.mark.parametrize('bad', [None, 'bad-date', 'NaT'])
def test_missing_or_malformed_deadline_remains_failure(gameplan_env, monkeypatch, bad):
    env = gameplan_env
    from ml.stock_trader import gameplan_direction_engine
    build = gameplan_direction_engine.build_gameplan_direction_trade_decisions
    def malformed(*a, **k):
        return tuple(replace(row, prediction={**row.prediction, 'actionable_until': bad})
                     if row.quantity else row for row in build(*a, **k))
    monkeypatch.setattr(gameplan_direction_engine, 'build_gameplan_direction_trade_decisions', malformed)
    result = run(env)
    assert result.status == 'SUBMISSION_STOPPED_SAFETY_CHECK' and result.error
    assert result.stopped_after_error and 'submission_stop' not in result.to_dict()
    assert report(result)['activity'] == 'ATTENTION' and env.broker.submissions == []


def test_reason_without_local_expired_cutoff_remains_failure(gameplan_env, monkeypatch):
    monkeypatch.setattr(runtime, '_submission_safety_reason',
                        lambda *a, **k: 'PREDICTION_TARGET_SAFETY_BOUNDARY_REACHED')
    result = run(gameplan_env)
    assert result.status == 'SUBMISSION_STOPPED_SAFETY_CHECK' and result.error
    assert 'submission_stop' not in result.to_dict() and gameplan_env.broker.submissions == []


def test_broker_exception_text_cannot_supply_local_expiry_proof(gameplan_env, monkeypatch):
    env = gameplan_env
    def fail(*a, **k):
        raise runtime._SubmissionStopped('PREDICTION_TARGET_SAFETY_BOUNDARY_REACHED')
    monkeypatch.setattr(env.broker, 'submit_prepared_order', fail)
    result = run(env)
    assert result.status == 'SUBMISSION_STOPPED_SAFETY_CHECK' and result.error
    assert 'submission_stop' not in result.to_dict() and env.broker.submissions == []


def test_manual_stop_wins_when_target_also_elapsed(gameplan_env):
    env = gameplan_env
    short_targets(env)
    def stopped():
        env.now += pd.Timedelta(seconds=16)
        env.active = False
    env.broker.before_post_hook = stopped
    result = run(env)
    assert result.status == 'SUBMISSION_STOPPED_SAFETY_CHECK'
    assert 'OPERATOR_INTENT_NOT_ACTIVE' in result.error
    assert env.active is False and env.broker.submissions == []
    assert 'submission_stop' not in result.to_dict()


def test_native_cleanup_failure_cannot_report_normal_expiry(gameplan_env, monkeypatch):
    env = gameplan_env
    short_targets(env)
    env.broker.before_post_hook = lambda: setattr(env, 'now', env.now + pd.Timedelta(seconds=16))
    mark = HorizonLedger.mark_submission
    def broken(self, reservation_id, **kwargs):
        if kwargs['status'] == 'REJECTED':
            raise RuntimeError('fixture cleanup unavailable')
        return mark(self, reservation_id, **kwargs)
    monkeypatch.setattr(HorizonLedger, 'mark_submission', broken)
    with pytest.raises(RuntimeError, match='fixture cleanup unavailable'):
        run(env)
    assert env.broker.submissions == []
    assert [row.status for row in reservations(env)] == ['RESERVED']


def test_unknown_post_remains_unknown_after_deadline(gameplan_env, monkeypatch):
    env = gameplan_env
    short_targets(env)
    env.broker.unknown_submission = True
    submit = env.broker.submit_prepared_order
    def unknown(*a, **k):
        try:
            return submit(*a, **k)
        finally:
            env.now += pd.Timedelta(seconds=16)
    monkeypatch.setattr(env.broker, 'submit_prepared_order', unknown)
    result = run(env)
    assert result.status == 'SUBMISSION_STOPPED_AFTER_ERROR' and result.error
    assert 'submission_stop' not in result.to_dict()
    assert [row.status for row in reservations(env)] == ['UNKNOWN']
    assert len(env.broker.submissions) == 1 and report(result)['activity'] == 'ATTENTION'


def test_stale_observation_remains_failure_when_target_elapsed(gameplan_env):
    env = gameplan_env
    short_targets(env)
    env.broker.before_post_hook = lambda: setattr(env, 'now', env.now + pd.Timedelta(seconds=65))
    result = run(env)
    assert result.status == 'SUBMISSION_STOPPED_SAFETY_CHECK' and result.error
    assert 'submission_stop' not in result.to_dict() and env.broker.submissions == []


def test_original_serialization_has_no_new_key_without_stop(gameplan_env):
    result = run(gameplan_env, execute=False)
    assert result.error is None and 'submission_stop' not in result.to_dict()


@pytest.mark.parametrize('capture', [None, {'status': 'CURRENT'}, {'status': 'CURRENT_AFTER_RETRY'}])
def test_elapsed_status_preserves_existing_failed_session_attention(capture):
    cycle = {'status': 'PREDICTION_TARGET_ELAPSED', 'error': None, 'stopped_after_error': False,
             'activation_active': True, 'execution_requested': True, 'submitted_orders': 0,
             'broker_state_capture': capture}
    result = session_cycle_report(cycle, as_of='2026-09-08T11:01:30Z',
        session_orders_submitted=0, session_cycles=2, session_failed_cycles=1,
        session_consecutive_failures=1)
    assert result['activity'] == 'ATTENTION' and result['session_status'] == 'DEGRADED'
    assert result['session_failed_cycles'] == result['session_consecutive_failures'] == 1
