"""An elapsed own pre-order context recaptures without accepting an old fence."""
from dataclasses import replace
from types import SimpleNamespace
import json
import socket

import pandas as pd
import pytest

from test_account_gameplan_execution import account_env, run, native, state
from test_independent_stock_runtime import environment
from ml.account_gameplan import authority, execution
from ml.stock_trader import gameplan_execution, independent_session as worker


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail('Context lease fixtures must never contact a provider')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)


def delay_once(env, monkeypatch, phase, after=None):
    original = getattr(execution.AccountExecutionContext, phase)
    delayed = False
    def current(self, *args, **kwargs):
        nonlocal delayed
        if not delayed:
            delayed = True
            env.now += pd.Timedelta(seconds=65)
            if after:
                after(self)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(execution.AccountExecutionContext, phase, current)


def no_intention(env):
    assert env.broker.submissions == env.broker.cancellations == []
    assert native(env).snapshot().reservations == ()
    assert state(env)['reservations'] == []
    events = env.root / 'ml/stock-trader-execution-events'
    assert not events.exists() or not list(events.iterdir())


@pytest.mark.parametrize('phase', ['assert_current', '_reconcile', '_snapshot', 'available_capacity'])
def test_exact_elapsed_context_recaptures_and_submits_originals_once(account_env, monkeypatch, phase):
    env = account_env
    originals = {s.prediction_id: (s.calibrated_probability, s.target_window_start, s.target_window_end)
                 for s in env.signals.values()}
    delay_once(env, monkeypatch, phase)
    first = run(env)
    assert first.status == 'BROKER_STATE_REFRESH_REQUIRED', first.error
    assert first.error is None and first.submitted_orders == 0 and not first.stopped_after_error
    no_intention(env)
    receipt = json.loads((first.run_directory / 'receipt.json').read_bytes())
    refresh = receipt['prediction_handoff']['pre_order_account_lease_refresh']
    assert refresh['reason'] == 'VALIDATED_EXACT_CONTEXT_LEASE_ELAPSED'
    assert refresh['snapshot_id']
    assert 'token' not in json.dumps(receipt)
    frozen = (first.run_directory / 'decisions.json').read_bytes()
    assert json.loads(frozen)['decisions'] == []
    env.now += pd.Timedelta(milliseconds=1)
    second = run(env)
    assert second.status == 'ORDERS_SUBMITTED', second.error
    assert env.captures == 2 and second.submitted_orders == len(env.broker.submissions) == len(originals)
    reservations = native(env).snapshot().reservations
    assert len(reservations) == len({r.forecast_id for r in reservations}) == len(originals)
    assert {r.forecast_id for r in reservations} == set(originals)
    assert (first.run_directory / 'decisions.json').read_bytes() == frozen
    for d in json.loads((second.run_directory / 'decisions.json').read_bytes())['decisions']:
        p = d['prediction']
        assert (p['calibrated_probability'], p['target_window_start'], p['target_window_end']) == originals[p['prediction_id']]


@pytest.mark.parametrize('damage', ['snapshot', 'config', 'path', 'manifest', 'token', 'epoch', 'holder',
                                   'expiry', 'untyped', 'missing'])
def test_constructor_requires_exact_typed_context_and_same_lease(account_env, monkeypatch, damage):
    env = account_env
    original = execution.AccountExecutionContext.__init__
    delay_once(env, monkeypatch, '_snapshot')
    def corrupt(self, *args, **kwargs):
        try:
            original(self, *args, **kwargs)
        except authority.AuthorityError as exc:
            proof = exc.initial_context_lease_expiry
            if damage == 'missing':
                del exc.initial_context_lease_expiry
            elif damage == 'untyped':
                exc.initial_context_lease_expiry = SimpleNamespace(**vars(proof))
            elif damage in {'token', 'epoch', 'holder', 'expiry'}:
                field = {'holder': 'holder_id', 'expiry': 'expires_at'}.get(damage, damage)
                value = (proof.lease.epoch + 1 if damage == 'epoch' else
                         (env.now + pd.Timedelta(seconds=10)).isoformat() if damage == 'expiry' else 'different')
                exc.initial_context_lease_expiry = replace(proof, lease=replace(proof.lease, **{field: value}))
            else:
                field = {'snapshot': 'snapshot_id', 'config': 'config_fingerprint',
                         'path': 'plan_path', 'manifest': 'manifest_sha256'}[damage]
                value = env.root / 'another-plan' if damage == 'path' else 'different'
                exc.initial_context_lease_expiry = replace(proof, **{field: value})
            raise
    monkeypatch.setattr(execution.AccountExecutionContext, '__init__', corrupt)
    result = run(env)
    assert result.status == 'ACCOUNT_RESERVATION_AUTHORITY_UNAVAILABLE'
    assert result.error == 'AuthorityError: CURRENT_FENCED_LEASE_REQUIRED'
    no_intention(env)


@pytest.mark.parametrize('phase', ['_snapshot', 'available_capacity'])
@pytest.mark.parametrize('damage', ['plain_error', 'fenced_token', 'different_typed_token', 'invalid_evidence'])
def test_nonexpiry_or_fenced_failures_remain_errors(account_env, monkeypatch, phase, damage):
    env = account_env
    original = getattr(execution.AccountExecutionContext, phase)
    def fail(self, *args, **kwargs):
        if damage == 'fenced_token':
            self.lease = replace(self.lease, token='different')
            return original(self, *args, **kwargs)
        error = authority.AuthorityError('ORDER_EVIDENCE_STALE_OR_INVALID' if damage == 'invalid_evidence'
                                         else 'CURRENT_FENCED_LEASE_REQUIRED')
        if damage == 'different_typed_token':
            error.current_lease_expiry = authority._CurrentLeaseExpiry(replace(self.lease, token='different'))
        raise error
    monkeypatch.setattr(execution.AccountExecutionContext, phase, fail)
    result = run(env)
    assert result.status == 'ACCOUNT_RESERVATION_AUTHORITY_UNAVAILABLE'
    assert result.error == ('AuthorityError: ORDER_EVIDENCE_STALE_OR_INVALID' if damage == 'invalid_evidence'
                            else 'AuthorityError: CURRENT_FENCED_LEASE_REQUIRED')
    no_intention(env)


@pytest.mark.parametrize('boundary', ['manual_stop', 'expired_target'])
def test_refresh_preserves_manual_stop_and_original_deadline(account_env, monkeypatch, boundary):
    env = account_env
    if boundary == 'expired_target':
        env.signals = {key: replace(s, actionable_until=(env.now + pd.Timedelta(seconds=30)).isoformat())
                       for key, s in env.signals.items()}
    delay_once(env, monkeypatch, '_snapshot',
               after=(lambda _: setattr(env, 'active', False)) if boundary == 'manual_stop' else None)
    first = run(env)
    assert first.status == 'BROKER_STATE_REFRESH_REQUIRED' and first.error is None
    no_intention(env)
    env.now += pd.Timedelta(milliseconds=1)
    assert run(env).submitted_orders == 0
    assert env.broker.submissions == []
    assert not env.active if boundary == 'manual_stop' else True


def test_ordinary_worker_recaptures_without_failure_or_extra_wait(account_env, monkeypatch):
    env = account_env
    delay_once(env, monkeypatch, '_snapshot')
    monkeypatch.setattr(gameplan_execution, 'execution_preflight', lambda *a, **k: {'status': 'READY'})
    monkeypatch.setattr(worker, 'read_gameplan_stock_activation_intent', lambda _: SimpleNamespace(active=env.active))
    results, sleeps = [], []
    def runner(root, **kwargs):
        result = run(env)
        results.append(result)
        if len(results) == 2:
            env.active = False
        assert len(results) <= 2
        return result
    def sleep(seconds):
        sleeps.append(seconds)
        env.now += pd.Timedelta(seconds=seconds)
    result = worker.run_independent_stock_session(env.root, execute=True, clock=lambda: env.now,
        sleep=sleep, runner=runner, reporter=lambda _: None, sizing_policy=worker.GAMEPLAN_SIZING_POLICY)
    assert [r.status for r in results] == ['BROKER_STATE_REFRESH_REQUIRED', 'ORDERS_SUBMITTED']
    assert env.captures == 2 and result['orders_submitted'] == 2
    assert sleeps and all(0 < seconds <= 30 for seconds in sleeps)
    saved = json.loads((env.root / 'state/independent-stock-trader/session-status.json').read_bytes())
    assert saved['failed_cycles'] == saved['consecutive_failures'] == 0
