"""Expired exact own order observations request complete fresh capture offline."""
from dataclasses import replace
from types import SimpleNamespace
import json

import pandas as pd
import pytest

from test_account_gameplan_execution import account_env, run, native, state
from test_independent_stock_runtime import environment, ACCOUNT
from test_account_gameplan_authority import (
    env as authority_env, submitted, evidence as authority_evidence, request as authority_request,
)
from ml.account_gameplan import authority, execution
from ml.stock_trader import gameplan_execution, independent_session as worker
from ml.stock_trader.horizon_ledger import OrderEvidence


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    import socket
    def denied(*args, **kwargs):
        pytest.fail('Order evidence refresh checks cannot connect to a provider')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)


def prepare_elapsed_evidence(env, monkeypatch, *, after=None):
    assert run(env).submitted_orders == 2
    originals = state(env)['reservations']
    env.now += pd.Timedelta(seconds=1)
    env.order_evidence = tuple(OrderEvidence('terminal-' + r.reservation_id, r.reservation_id,
        ACCOUNT, env.now.isoformat(), r.broker_order_id, 'CANCELLED', r.quantity, 0, 0, ())
        for r in native(env).snapshot().reservations)
    original = gameplan_execution.account_execution_plan
    delayed = [False]
    def plan(root, **kwargs):
        if env.captures > 1 and not delayed[0]:
            delayed[0] = True
            env.now += pd.Timedelta(seconds=65)
            if after:
                after()
        return original(root, **kwargs)
    monkeypatch.setattr(gameplan_execution, 'account_execution_plan', plan)
    return originals


def assert_no_new_intention(env, originals):
    assert len(env.broker.submissions) == 2 and env.broker.cancellations == []
    assert len(native(env).snapshot().reservations) == len(originals) == 2


def fresh_evidence(env):
    env.now += pd.Timedelta(milliseconds=1)
    env.order_evidence = tuple(replace(e, evidence_id='fresh-' + e.evidence_id,
        observed_at=env.now.isoformat()) for e in env.order_evidence)


def test_exact_elapsed_order_evidence_requests_one_complete_recapture(account_env, monkeypatch):
    env = account_env
    originals = prepare_elapsed_evidence(env, monkeypatch)
    frozen_evidence = env.order_evidence
    result = run(env)
    assert result.status == 'BROKER_STATE_REFRESH_REQUIRED', result.error
    assert result.error is None and result.submitted_orders == 0 and not result.stopped_after_error
    assert env.captures == 2  # No recursive capture or hidden retry in this call.
    assert_no_new_intention(env, originals)
    assert state(env)['reservations'] == originals
    assert env.order_evidence == frozen_evidence
    frozen_receipt = (result.run_directory / 'receipt.json').read_bytes()
    payload = json.loads(frozen_receipt)
    assert payload['decision_ids'] == []
    refresh = payload['prediction_handoff']['pre_order_exact_order_evidence_refresh']
    assert refresh['reason'] == 'VALIDATED_EXACT_ORDER_EVIDENCE_ELAPSED'
    original = next(e for e in frozen_evidence if e.evidence_id == refresh['evidence_id'])
    assert refresh['broker_order_id'] == original.broker_order_id
    assert refresh['observed_at'] == original.observed_at
    fresh_evidence(env)
    second = run(env)
    assert second.error is None and second.submitted_orders == 0, second.error
    assert env.captures == 3
    assert_no_new_intention(env, originals)
    assert all(r['status'] == 'CANCELLED' and r['filled_quantity'] == 0
               for r in state(env)['reservations'])
    assert (result.run_directory / 'receipt.json').read_bytes() == frozen_receipt


@pytest.mark.parametrize('damage', ['future', 'precreation', 'malformed', 'wrong_order',
    'wrong_account', 'quantity', 'status', 'filled_balance', 'provenance'])
def test_elapsed_evidence_does_not_mask_invalid_bound_or_structural_error(account_env, monkeypatch, damage):
    env = account_env
    originals = prepare_elapsed_evidence(env, monkeypatch)
    original = authority.AccountAuthority.reconcile
    def reconcile(self, token, evidence):
        if damage == 'future':
            evidence = replace(evidence, observed_at=(env.now + pd.Timedelta(seconds=1)).isoformat())
        elif damage == 'precreation':
            evidence = replace(evidence, observed_at=(env.now - pd.Timedelta(minutes=5)).isoformat())
        elif damage == 'malformed':
            evidence = replace(evidence, observed_at='invalid')
        elif damage == 'wrong_order':
            evidence = replace(evidence, broker_order_id='another-order')
        elif damage == 'wrong_account':
            evidence = replace(evidence, account_fingerprint='9' * 64)
        elif damage == 'quantity':
            evidence = replace(evidence, order_quantity=evidence.order_quantity + 1)
        elif damage == 'status':
            evidence = replace(evidence, status='UNKNOWN')
        elif damage == 'filled_balance':
            evidence = replace(evidence, cumulative_filled_quantity=1)
        else:
            evidence = replace(evidence, fill_time_provenance={'invalid': True})
        return original(self, token, evidence)
    monkeypatch.setattr(authority.AccountAuthority, 'reconcile', reconcile)
    result = run(env)
    assert result.status == 'ACCOUNT_RESERVATION_AUTHORITY_UNAVAILABLE', result.error
    assert result.error and result.submitted_orders == 0
    assert_no_new_intention(env, originals)
    assert state(env)['reservations'] == originals


@pytest.mark.parametrize('damage', ['missing_marker', 'wrong_type', 'different_evidence',
    'different_account', 'different_order', 'different_observation', 'untyped_marker'])
def test_recapture_requires_typed_proof_of_the_original_captured_order(account_env, monkeypatch, damage):
    env = account_env
    originals = prepare_elapsed_evidence(env, monkeypatch)
    def reconcile(self, token, evidence):
        error = authority.AuthorityError('ORDER_EVIDENCE_STALE_OR_INVALID')
        values = dict(evidence_id=evidence.evidence_id,
            account_fingerprint=evidence.account_fingerprint, broker_order_id=evidence.broker_order_id,
            observed_at=evidence.observed_at)
        if damage == 'different_evidence':
            values['evidence_id'] = 'another-evidence'
        elif damage == 'different_account':
            values['account_fingerprint'] = '9' * 64
        elif damage == 'different_order':
            values['broker_order_id'] = 'another-order'
        elif damage == 'different_observation':
            values['observed_at'] = (pd.Timestamp(evidence.observed_at) + pd.Timedelta(milliseconds=1)).isoformat()
        if damage == 'wrong_type':
            error = RuntimeError(str(error))
        if damage != 'missing_marker':
            marker_type = SimpleNamespace if damage == 'untyped_marker' else authority._CurrentOrderEvidenceExpiry
            error.current_order_evidence_expiry = marker_type(**values)
        raise error
    monkeypatch.setattr(authority.AccountAuthority, 'reconcile', reconcile)
    result = run(env)
    assert result.status == 'ACCOUNT_RESERVATION_AUTHORITY_UNAVAILABLE', result.error
    assert result.error and result.submitted_orders == 0
    assert_no_new_intention(env, originals)
    assert state(env)['reservations'] == originals


@pytest.mark.parametrize('boundary', ['manual_stop', 'expired_target'])
def test_complete_recapture_preserves_stop_and_original_signal_deadline(account_env, monkeypatch, boundary):
    env = account_env
    originals = prepare_elapsed_evidence(env, monkeypatch,
        after=(lambda: setattr(env, 'active', False)) if boundary == 'manual_stop' else None)
    if boundary == 'expired_target':
        env.signals = {key: replace(s, actionable_until=(env.now + pd.Timedelta(seconds=30)).isoformat())
                       for key, s in env.signals.items()}
    first = run(env)
    assert first.status == 'BROKER_STATE_REFRESH_REQUIRED' and first.error is None
    fresh_evidence(env)
    assert run(env).submitted_orders == 0
    assert_no_new_intention(env, originals)
    if boundary == 'manual_stop':
        assert not env.active


def test_session_observes_fresh_evidence_at_ordinary_cadence_without_false_failure(account_env, monkeypatch):
    env = account_env
    originals = prepare_elapsed_evidence(env, monkeypatch)
    monkeypatch.setattr(gameplan_execution, 'execution_preflight', lambda *a, **k: {'status': 'READY'})
    monkeypatch.setattr(worker, 'read_gameplan_stock_activation_intent', lambda _: SimpleNamespace(active=env.active))
    results, sleeps = [], []
    def runner(root, **kwargs):
        if results:
            fresh_evidence(env)
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
    assert results[0].status == 'BROKER_STATE_REFRESH_REQUIRED'
    assert all(r.error is None and r.submitted_orders == 0 for r in results)
    assert len(results) == 2 and sleeps and all(0 < seconds <= 30 for seconds in sleeps)
    saved = json.loads((env.root / 'state/independent-stock-trader/session-status.json').read_bytes())
    assert saved['failed_cycles'] == saved['consecutive_failures'] == 0
    assert_no_new_intention(env, originals)


def test_stale_exact_previously_applied_evidence_is_rejected_without_mutation(authority_env):
    store, token, clock = authority_env
    order = submitted(store, token, authority_request())
    clock.tick()
    original = authority_evidence(order, clock)
    store.reconcile(token, original)
    clock.tick(65)
    lease = store.acquire_lease(coordinator_id='Atlas', holder_id='next-current-owner')
    before = store.read_state()
    with pytest.raises(authority.AuthorityError, match='ORDER_EVIDENCE_STALE_OR_INVALID') as failure:
        store.reconcile(lease.token, original)
    assert authority.matches_current_order_evidence_expiry(failure.value, original)
    assert store.read_state() == before


@pytest.mark.parametrize('damage', ['valid_stale', 'reused_changed', 'terminal_change',
    'must_advance', 'prior_fill_missing', 'prior_fill_changed', 'fill_time', 'fill_price',
    'expired_lease', 'fenced_lease'])
def test_stale_age_never_masks_remaining_authority_invariants(authority_env, damage):
    store, token, clock = authority_env
    order = submitted(store, token, authority_request())
    clock.tick()
    observed = authority_evidence(order, clock)
    if damage in {'reused_changed', 'terminal_change', 'must_advance', 'prior_fill_missing', 'prior_fill_changed'}:
        if damage.startswith('prior_fill'):
            initial = authority_evidence(order, clock, status='PARTIAL', filled=3, remaining=3)
        else:
            initial = observed
        store.reconcile(token, initial)
        clock.tick()
        observed = replace(observed, evidence_id='next-observation', observed_at=clock().isoformat())
        if damage == 'reused_changed':
            observed = replace(observed, evidence_id=initial.evidence_id, status='CANCELLED')
        elif damage == 'terminal_change':
            observed = replace(observed, status='CANCELLED')
        elif damage == 'must_advance':
            observed = replace(observed, observed_at=initial.observed_at)
        elif damage == 'prior_fill_missing':
            observed = replace(observed, fills=(replace(observed.fills[0], fill_id='replacement'),))
        else:
            observed = replace(observed, fills=(replace(observed.fills[0], price='9'),))
    elif damage == 'fill_time':
        observed = replace(observed, fills=(replace(observed.fills[0],
            executed_at=(pd.Timestamp(observed.observed_at) + pd.Timedelta(seconds=1)).isoformat()),))
    elif damage == 'fill_price':
        observed = replace(observed, fills=(replace(observed.fills[0], price='10.01'),))
    clock.tick(65)
    if damage != 'expired_lease':
        lease = store.acquire_lease(coordinator_id='Atlas', holder_id='next-current-owner')
        current_token = token if damage == 'fenced_lease' else lease.token
    else:
        current_token = token
    before = store.read_state()
    with pytest.raises(authority.AuthorityError) as failure:
        store.reconcile(current_token, observed)
    assert authority.matches_current_order_evidence_expiry(failure.value, observed) == (damage == 'valid_stale')
    assert store.read_state() == before
    with store._transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM fills').fetchone()[0] == (
            1 if damage in {'reused_changed', 'terminal_change', 'must_advance', 'prior_fill_missing', 'prior_fill_changed'} else 0)
