"""Elapsed own account snapshots recapture before decisions or order intentions."""
from dataclasses import replace
from types import SimpleNamespace
import json

import pandas as pd
import pytest

from test_account_gameplan_execution import account_env, run, native, state
from test_independent_stock_runtime import environment
from ml.account_gameplan import authority, execution
from ml.stock_trader import gameplan_execution, independent_session as worker


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    import socket
    def denied(*args, **kwargs):
        pytest.fail('Pre-context refresh checks must never connect to a provider')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)


def delay_once(env, monkeypatch, phase, *, after=None):
    delayed = [False]
    if phase == 'before_context':
        original = gameplan_execution.account_execution_plan
        def current(root, **kwargs):
            if env.captures and not delayed[0]:
                delayed[0] = True
                env.now += pd.Timedelta(seconds=65)
                if after:
                    after()
            return original(root, **kwargs)
        monkeypatch.setattr(gameplan_execution, 'account_execution_plan', current)
    else:
        # A still-current lease can protect a snapshot that ages across two
        # ordinary pre-order phases. No test renews or replaces that lease.
        plan = gameplan_execution.account_execution_plan
        plan_delayed = [False]
        def current_plan(root, **kwargs):
            if env.captures and not plan_delayed[0]:
                plan_delayed[0] = True
                env.now += pd.Timedelta(seconds=35)
            return plan(root, **kwargs)
        monkeypatch.setattr(gameplan_execution, 'account_execution_plan', current_plan)
        target = '_snapshot' if phase == 'snapshot_publish' else 'available_capacity'
        original = getattr(execution.AccountExecutionContext, target)
        def current(self, *args, **kwargs):
            if not delayed[0]:
                delayed[0] = True
                env.now += pd.Timedelta(seconds=30)
                if after:
                    after()
            return original(self, *args, **kwargs)
        monkeypatch.setattr(execution.AccountExecutionContext, target, current)


def no_intention(env):
    assert env.broker.submissions == env.broker.cancellations == []
    assert native(env).snapshot().reservations == ()
    assert state(env)['reservations'] == []
    events = env.root / 'ml/stock-trader-execution-events'
    assert not events.exists() or not list(events.iterdir())


@pytest.mark.parametrize('phase', ['before_context', 'snapshot_publish', 'available_capacity'])
def test_elapsed_context_recaptures_original_forecasts_once(account_env, monkeypatch, phase):
    env = account_env
    originals = {s.prediction_id: (s.calibrated_probability, s.target_window_start, s.target_window_end)
                 for s in env.signals.values()}
    delay_once(env, monkeypatch, phase)
    first = run(env)
    assert first.status == 'BROKER_STATE_REFRESH_REQUIRED', first.error
    assert first.error is None and first.submitted_orders == 0 and not first.stopped_after_error
    no_intention(env)
    frozen = (first.run_directory / 'decisions.json').read_bytes()
    receipt = json.loads((first.run_directory / 'receipt.json').read_bytes())
    refresh = receipt['prediction_handoff']['pre_order_account_snapshot_refresh']
    assert refresh['reason'] == 'VALIDATED_ACCOUNT_SNAPSHOT_ELAPSED'
    assert json.loads(frozen)['decisions'] == []
    observed = refresh['observed_at']
    assert (env.now - pd.Timestamp(observed)).total_seconds() == 65
    env.now += pd.Timedelta(milliseconds=1)
    second = run(env)
    assert second.status == 'ORDERS_SUBMITTED', second.error
    assert second.submitted_orders == len(env.broker.submissions) == len(originals)
    assert {r.forecast_id for r in native(env).snapshot().reservations} == set(originals)
    assert len(native(env).snapshot().reservations) == len(originals)
    assert (first.run_directory / 'decisions.json').read_bytes() == frozen
    for d in json.loads((second.run_directory / 'decisions.json').read_bytes())['decisions']:
        p = d['prediction']
        assert (p['calibrated_probability'], p['target_window_start'], p['target_window_end']) == originals[p['prediction_id']]


def test_empty_ledger_worker_recaptures_at_existing_cadence_without_failure(account_env, monkeypatch):
    env = account_env
    delay_once(env, monkeypatch, 'before_context')
    monkeypatch.setattr(gameplan_execution, 'execution_preflight', lambda *a, **k: {'status': 'READY'})
    monkeypatch.setattr(worker, 'read_gameplan_stock_activation_intent', lambda _: SimpleNamespace(active=env.active))
    results, sleeps = [], []
    def runner(root, **kwargs):
        assert kwargs['entries']
        result = run(env)
        results.append(result)
        if len(results) == 2:
            env.active = False
        assert len(results) <= 2
        return result
    def sleep(seconds):
        sleeps.append(seconds)
        env.now += pd.Timedelta(seconds=seconds)
    assert not worker._has_inventory(env.root / worker.LEDGER_RELATIVE_PATH)
    result = worker.run_independent_stock_session(env.root, execute=True, clock=lambda: env.now,
        sleep=sleep, runner=runner, reporter=lambda _: None, sizing_policy=worker.GAMEPLAN_SIZING_POLICY)
    assert [r.status for r in results] == ['BROKER_STATE_REFRESH_REQUIRED', 'ORDERS_SUBMITTED']
    assert env.captures == 2 and result['orders_submitted'] == 2
    assert sleeps and all(0 < seconds <= 30 for seconds in sleeps)
    saved = json.loads((env.root / 'state/independent-stock-trader/session-status.json').read_bytes())
    assert saved['failed_cycles'] == saved['consecutive_failures'] == 0


@pytest.mark.parametrize('boundary', ['manual_stop', 'expired_target'])
def test_context_refresh_keeps_stop_and_original_deadline(account_env, monkeypatch, boundary):
    env = account_env
    if boundary == 'expired_target':
        env.signals = {key: replace(s, actionable_until=(env.now + pd.Timedelta(seconds=30)).isoformat())
                       for key, s in env.signals.items()}
    delay_once(env, monkeypatch, 'before_context',
        after=(lambda: setattr(env, 'active', False)) if boundary == 'manual_stop' else None)
    first = run(env)
    assert first.status == 'BROKER_STATE_REFRESH_REQUIRED' and first.error is None
    no_intention(env)
    env.now += pd.Timedelta(milliseconds=1)
    assert run(env).submitted_orders == 0
    assert env.broker.submissions == env.broker.cancellations == []
    if boundary == 'manual_stop':
        assert not env.active


@pytest.mark.parametrize('damage', ['source', 'config', 'future', 'malformed', 'different_id', 'wrong_type', 'missing_marker'])
def test_positive_elapsed_age_never_masks_unproven_or_changed_context(account_env, monkeypatch, damage):
    env = account_env
    original_config = env.config
    if damage in {'source', 'config'}:
        def change():
            if damage == 'source':
                env.plan = SimpleNamespace(**{**vars(env.plan), 'manifest_sha256': '8' * 64})
            else:
                env.config = replace(env.config, fingerprint='9' * 64)
        # A pinned plan/config change during context assert_current stays hard.
        original = execution.AccountExecutionContext.assert_current
        changed = [False]
        def current(self):
            if not changed[0]:
                changed[0] = True
                env.now += pd.Timedelta(seconds=65)
                change()
            return original(self)
        monkeypatch.setattr(execution.AccountExecutionContext, 'assert_current', current)
    else:
        original = authority.AccountAuthority.publish_snapshot
        def publish(self, token, snapshot):
            if damage == 'future':
                return original(self, token, replace(snapshot, observed_at=(env.now + pd.Timedelta(seconds=1)).isoformat()))
            if damage == 'malformed':
                return original(self, token, replace(snapshot, observed_at='invalid'))
            error = authority.AuthorityError('ACCOUNT_SNAPSHOT_STALE_OR_FUTURE')
            if damage == 'different_id':
                proof = getattr(authority, '_CurrentSnapshotExpiry', None)
                if proof:
                    error.current_snapshot_expiry = proof('another-snapshot', snapshot.observed_at)
            elif damage == 'wrong_type':
                error = RuntimeError(str(error))
            raise error
        monkeypatch.setattr(authority.AccountAuthority, 'publish_snapshot', publish)
    result = run(env)
    assert result.status == 'ACCOUNT_RESERVATION_AUTHORITY_UNAVAILABLE', result.error
    assert result.error and result.submitted_orders == 0
    env.config = original_config
    no_intention(env)


@pytest.mark.parametrize('damage', ['cash', 'symbol_union', 'risk_budget', 'pending_identity', 'same_id_changed', 'must_advance', 'immutable_generation'])
def test_elapsed_publication_preserves_structural_risk_and_ancestry_errors(account_env, monkeypatch, damage):
    env = account_env
    original = authority.AccountAuthority.publish_snapshot
    def publish(self, token, snapshot):
        if damage in {'same_id_changed', 'must_advance', 'immutable_generation'}:
            original(self, token, snapshot)
        env.now += pd.Timedelta(seconds=65)
        lease = self.acquire_lease(coordinator_id=env.config.machine_id, holder_id='offline-current-owner')
        if damage == 'cash':
            snapshot = replace(snapshot, cash_available='invalid')
        elif damage == 'symbol_union':
            snapshot = replace(snapshot, held_shares={})
        elif damage == 'risk_budget':
            snapshot = replace(snapshot, cash_budget='100000000')
        elif damage == 'pending_identity':
            snapshot = replace(snapshot, broker_pending=(authority.BrokerReservation('same', 'AAPL', 'WRONG', 1, 0, '0', '0'),))
        elif damage == 'same_id_changed':
            snapshot = replace(snapshot, cash_available='99999')
        elif damage == 'must_advance':
            snapshot = replace(snapshot, snapshot_id='another-id')
        else:
            with self._transaction() as db:
                db.execute('UPDATE plan_revisions SET sources=?', ('{}',))
            snapshot = replace(snapshot, snapshot_id='another-id', observed_at=(pd.Timestamp(snapshot.observed_at) + pd.Timedelta(milliseconds=1)).isoformat())
        return original(self, lease.token, snapshot)
    monkeypatch.setattr(authority.AccountAuthority, 'publish_snapshot', publish)
    result = run(env)
    assert result.status == 'ACCOUNT_RESERVATION_AUTHORITY_UNAVAILABLE', result.error
    assert result.error and 'ACCOUNT_SNAPSHOT_STALE_OR_FUTURE' not in result.error
    no_intention(env)


def test_snapshot_expiry_proof_requires_exact_original_snapshot_and_observation(account_env):
    env = account_env
    store = authority.AccountAuthority(env.root / execution.AUTHORITY_PATH, execution._binding(env.config))
    assert store.read_state()['reservations'] == []
    error = authority.AuthorityError('ACCOUNT_SNAPSHOT_STALE_OR_FUTURE')
    marker = authority._CurrentSnapshotExpiry('own-snapshot', env.now.isoformat())
    error.current_snapshot_expiry = marker
    assert authority.matches_current_snapshot_expiry(error, 'own-snapshot', env.now.isoformat())
    assert not authority.matches_current_snapshot_expiry(error, 'other-snapshot', env.now.isoformat())
    assert not authority.matches_current_snapshot_expiry(error, 'own-snapshot', (env.now + pd.Timedelta(milliseconds=1)).isoformat())
    assert not authority.matches_current_snapshot_expiry(error, 'own-snapshot', 'invalid')
    error.current_snapshot_expiry = SimpleNamespace(snapshot_id='own-snapshot', observed_at=env.now.isoformat())
    assert not authority.matches_current_snapshot_expiry(error, 'own-snapshot', env.now.isoformat())
