"""Only fake observations and temporary own ledgers; no broker or provider IO."""
from dataclasses import asdict, replace
import socket

import pandas as pd
import pytest

from test_intraday_main_account_execution import env, native, account, advance
from ml.intraday import account_execution as execution
from ml.intraday.prediction import ContractError
from ml.account_gameplan.authority import AuthorityError


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Capture pair tests must never connect to a broker or provider")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def uncertain(env, *, symbol='AAPL'):
    env.broker.unknown_submission = True
    forecast = env.forecast(symbol=symbol)
    assert env.executor.step(forecast)['disposition'] == 'submission_recovery_required'
    env.broker.unknown_submission = False
    advance(env)
    return forecast, next(r for r in native(env).snapshot().reservations
                          if r.forecast_id == forecast['forecast_id'])


def working_order(env, symbol='AMZN'):
    forecast = env.forecast(symbol=symbol)
    assert env.executor.step(forecast)['disposition'] == 'submitted'
    return next(r for r in native(env).snapshot().reservations if r.forecast_id == forecast['forecast_id'])


def capacity_count(env):
    with account(env)._transaction() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='intraday_capacity_snapshots'").fetchone():
            return 0
        return db.execute('SELECT COUNT(*) FROM intraday_capacity_snapshots').fetchone()[0]


def delayed_pair(env, monkeypatch, *, portfolio_delays=(49, 0), authority_delay=12, refreshed=None):
    order_capture = execution.capture_order_evidence
    portfolio_capture = execution.capture_portfolio_state
    authority = env.executor._reconcile_captured_authority_orders
    seen = {'orders': [], 'portfolios': [], 'authority': []}

    def orders(*args, **kwargs):
        if seen['orders'] and refreshed is not None:
            refreshed()
        result = order_capture(*args, **kwargs)
        seen['orders'].append(result)
        return result

    def portfolio(*args, **kwargs):
        index = len(seen['portfolios'])
        remaining = portfolio_delays[min(index, len(portfolio_delays) - 1)]
        # Fake time jumps must still model the ordinary background lease
        # renewal during slow IO, rather than silently expiring that fence.
        while remaining:
            elapsed = min(10, remaining)
            advance(env, elapsed)
            env.executor._lease_keeper.renew()
            remaining = max(0, remaining - elapsed)
        result = replace(portfolio_capture(*args, **kwargs), observed_at=kwargs['observed_at'])
        seen['portfolios'].append(result)
        return result

    def reconcile(*args, **kwargs):
        result = authority(*args, **kwargs)
        if len(seen['authority']) < 2:
            advance(env, authority_delay)
        seen['authority'].append(True)
        return result

    monkeypatch.setattr(execution, 'capture_order_evidence', orders)
    monkeypatch.setattr(execution, 'capture_portfolio_state', portfolio)
    monkeypatch.setattr(env.executor, '_reconcile_captured_authority_orders', reconcile)
    return seen


def test_aged_first_pair_gets_one_fresh_order_then_new_portfolio(env, monkeypatch):
    order = working_order(env)
    advance(env)
    seen = delayed_pair(env, monkeypatch)
    before = capacity_count(env)
    result = env.executor.recover()
    assert result and len(seen['orders']) == len(seen['portfolios']) == 2
    assert pd.Timestamp(seen['orders'][0][0].observed_at) < pd.Timestamp(seen['portfolios'][0].observed_at)
    assert pd.Timestamp(seen['orders'][1][0].observed_at) > pd.Timestamp(seen['portfolios'][0].observed_at)
    assert pd.Timestamp(seen['orders'][1][0].observed_at) <= pd.Timestamp(seen['portfolios'][1].observed_at)
    assert env.executor.portfolio.quotes == seen['portfolios'][1].quotes
    assert env.executor.portfolio.held_shares == seen['portfolios'][1].held_shares
    assert native(env).lookup_reservation(order.reservation_id).status == 'WORKING'
    assert capacity_count(env) == before + 1
    assert len(env.broker.submissions) == 1 and not env.broker.cancellations


def test_second_slow_pair_keeps_original_guard_without_third_read(env, monkeypatch):
    order = working_order(env)
    advance(env)
    seen = delayed_pair(env, monkeypatch, portfolio_delays=(49, 49))
    before = capacity_count(env)
    with pytest.raises(ContractError, match='STALE_WORKING_ORDER_EVIDENCE'):
        env.executor.recover()
    assert len(seen['orders']) == len(seen['portfolios']) == 2
    assert capacity_count(env) == before
    assert next(r for r in account(env).read_state()['reservations']
                if r['request']['native_reservation_id'] == order.reservation_id)['status'] == 'WORKING'
    assert len(env.broker.submissions) == 1 and not env.broker.cancellations


@pytest.mark.parametrize('first_delay', [49, 61])
@pytest.mark.parametrize('status, filled', [('PARTIAL', 1), ('FILLED', 2), ('CANCELLED', 0)])
def test_refresh_progress_requires_new_balances_and_working_rows(env, monkeypatch, status, filled, first_delay):
    order = working_order(env)
    assert order.quantity == 2
    advance(env)

    def refreshed():
        env.orders[order.broker_order_id].update(status=status, filled=filled, fill_at=env.clock())
        env.held['AMZN'] = filled
        env.cash = 100 - 50 * filled

    seen = delayed_pair(env, monkeypatch, portfolio_delays=(first_delay, 0), refreshed=refreshed)
    env.executor.recover()
    assert len(seen['portfolios']) == 2
    assert seen['portfolios'][0].held_shares['AMZN'] == 0
    assert env.executor.portfolio.held_shares['AMZN'] == filled
    assert native(env).lookup_reservation(order.reservation_id).filled_quantity == filled
    assert native(env).lookup_reservation(order.reservation_id).status == status
    assert len(env.broker.submissions) == 1 and not env.broker.cancellations


def test_refresh_does_not_hide_stale_new_portfolio_request(env, monkeypatch):
    working_order(env)
    advance(env)
    seen = delayed_pair(env, monkeypatch, portfolio_delays=(49, 61))
    before = capacity_count(env)
    with pytest.raises(ContractError, match='Account snapshot is not fresh'):
        env.executor.recover()
    assert len(seen['orders']) == len(seen['portfolios']) == 2
    assert capacity_count(env) == before
    assert len(env.broker.submissions) == 1 and not env.broker.cancellations


@pytest.mark.parametrize('first_delay', [49, 61])
@pytest.mark.parametrize('change', ['manual', 'stop', 'binding', 'lease'])
def test_refresh_preserves_manual_binding_and_fence_checks(env, monkeypatch, change, first_delay):
    working_order(env)
    advance(env)

    def refreshed():
        if change == 'manual':
            env.active = False
        elif change == 'stop':
            env.executor.stopped = lambda: True
        elif change == 'binding':
            env.config = replace(env.config, fingerprint='changed-fingerprint')
        else:
            env.executor.authority.release_lease(env.executor.lease.token)

    seen = delayed_pair(env, monkeypatch, portfolio_delays=(first_delay, 0), refreshed=refreshed)
    before = capacity_count(env)
    with pytest.raises((ContractError, AuthorityError)):
        env.executor.recover()
    assert len(seen['orders']) == 2 and len(seen['portfolios']) == 1
    assert capacity_count(env) == before
    assert len(env.broker.submissions) == 1 and not env.broker.cancellations


def test_unknown_own_commitment_survives_pair_refresh(env, monkeypatch):
    env.cash = 500
    working_order(env)
    _, unknown = uncertain(env, symbol='AAPL')
    prior_native = asdict(native(env).lookup_reservation(unknown.reservation_id))
    prior_authority = next(r for r in account(env).read_state()['reservations']
                           if r['request']['native_reservation_id'] == unknown.reservation_id)
    advance(env)
    seen = delayed_pair(env, monkeypatch)
    env.executor.recover()
    assert len(seen['orders']) == len(seen['portfolios']) == 2
    assert all(row.reservation_id != unknown.reservation_id for rows in seen['orders'] for row in rows)
    assert asdict(native(env).lookup_reservation(unknown.reservation_id)) == prior_native
    assert next(r for r in account(env).read_state()['reservations']
                if r['request']['native_reservation_id'] == unknown.reservation_id) == prior_authority
    assert len(env.broker.submissions) == 2 and not env.broker.cancellations


@pytest.mark.parametrize('age, recaptures', [(60, 1), (60.000001, 2)])
def test_native_order_age_boundary_is_inclusive(env, monkeypatch, age, recaptures):
    working_order(env)
    advance(env)
    seen = delayed_pair(env, monkeypatch, portfolio_delays=(age - 12 - .001, 0), authority_delay=12)
    env.executor.recover()
    assert len(seen['orders']) == len(seen['portfolios']) == recaptures
    assert len(env.broker.submissions) == 1 and not env.broker.cancellations


@pytest.mark.parametrize('first_delay', [49, 61])
@pytest.mark.parametrize('regression', ['missing_fill', 'changed_fill'])
def test_retry_preserves_every_original_partial_fill(env, monkeypatch, regression, first_delay):
    order = working_order(env)
    env.orders[order.broker_order_id].update(status='PARTIAL', filled=1, fill_at=env.clock())
    env.held['AMZN'] = 1
    advance(env)
    seen = delayed_pair(env, monkeypatch, portfolio_delays=(first_delay, 0))
    capture = execution.capture_order_evidence

    def corrupted(*args, **kwargs):
        result = capture(*args, **kwargs)
        if len(seen['orders']) == 2:
            row = result[0]
            fills = () if regression == 'missing_fill' else (replace(row.fills[0], price='51.00'),)
            return (replace(row, fills=fills), *result[1:])
        return result

    monkeypatch.setattr(execution, 'capture_order_evidence', corrupted)
    before = capacity_count(env)
    with pytest.raises(ContractError, match='cannot discard observed order evidence'):
        env.executor.recover()
    assert len(seen['portfolios']) == 1 and capacity_count(env) == before
    assert next(r for r in account(env).read_state()['reservations']
                if r['request']['native_reservation_id'] == order.reservation_id)['filled_quantity'] == 1
    assert len(env.broker.submissions) == 1 and not env.broker.cancellations


@pytest.mark.parametrize('first_delay', [49, 61])
def test_retry_preserves_terminal_state_when_another_order_ages(env, monkeypatch, first_delay):
    env.cash = 500
    terminal = working_order(env)
    working_order(env, symbol='AAPL')
    env.orders[terminal.broker_order_id].update(status='FILLED', filled=terminal.quantity, fill_at=env.clock())
    env.held['AMZN'] = terminal.quantity
    advance(env)

    def regression():
        env.orders[terminal.broker_order_id].update(status='PARTIAL', filled=terminal.quantity - 1)

    seen = delayed_pair(env, monkeypatch, portfolio_delays=(first_delay, 0), refreshed=regression)
    before = capacity_count(env)
    with pytest.raises(ContractError, match='cannot discard observed order evidence'):
        env.executor.recover()
    assert len(seen['portfolios']) == 1 and capacity_count(env) == before
    assert next(r for r in account(env).read_state()['reservations']
                if r['request']['native_reservation_id'] == terminal.reservation_id)['status'] == 'FILLED'
    assert len(env.broker.submissions) == 2 and not env.broker.cancellations


def test_retry_rechecks_broker_account_identity_before_native_projection(env, monkeypatch):
    working_order(env)
    advance(env)

    def changed():
        monkeypatch.setattr(env.broker, 'stable_account_fingerprint', lambda: 'b' * 64)

    seen = delayed_pair(env, monkeypatch, refreshed=changed)
    before = capacity_count(env)
    with pytest.raises(ContractError, match='Broker account differs'):
        env.executor.recover()
    assert len(seen['orders']) == len(seen['portfolios']) == 2
    assert capacity_count(env) == before
    assert len(env.broker.submissions) == 1 and not env.broker.cancellations


def test_stale_initial_portfolio_retries_full_pair_without_any_working_order(env, monkeypatch):
    seen = delayed_pair(env, monkeypatch, portfolio_delays=(65.691509, 0), authority_delay=0)
    before = capacity_count(env)
    assert env.executor.recover() == []
    assert len(seen['orders']) == len(seen['portfolios']) == 2
    assert not any(seen['orders']) and native(env).snapshot().reservations == ()
    assert env.executor.portfolio.quotes == seen['portfolios'][1].quotes
    assert capacity_count(env) == before + 1
    assert not env.broker.submissions and not env.broker.cancellations


def test_second_stale_portfolio_fails_without_a_third_pair(env, monkeypatch):
    seen = delayed_pair(env, monkeypatch, portfolio_delays=(61, 61), authority_delay=0)
    before = capacity_count(env)
    with pytest.raises(ContractError, match='Account snapshot is not fresh'):
        env.executor.recover()
    assert len(seen['orders']) == len(seen['portfolios']) == 2
    assert capacity_count(env) == before and native(env).snapshot().reservations == ()
    assert not env.broker.submissions and not env.broker.cancellations


@pytest.mark.parametrize('age, pairs', [(0, 1), (60, 1), (60.000001, 2), (-.000001, 1)])
def test_portfolio_request_clock_boundary_and_future_rejection(env, monkeypatch, age, pairs):
    original = execution.capture_portfolio_state
    seen = []

    def capture(*args, **kwargs):
        result = original(*args, **kwargs)
        seen.append(result)
        if len(seen) == 1:
            return replace(result, observed_at=(env.now - pd.Timedelta(seconds=age)).isoformat())
        return result

    monkeypatch.setattr(execution, 'capture_portfolio_state', capture)
    if age < 0:
        with pytest.raises(ContractError, match='Account snapshot is not fresh'):
            env.executor.recover()
    else:
        assert env.executor.recover() == []
    assert len(seen) == env.captures == pairs
    assert native(env).snapshot().reservations == ()
    assert not env.broker.submissions and not env.broker.cancellations


def test_portfolio_retry_cannot_add_a_third_pair_for_newly_aged_working_order(env, monkeypatch):
    working_order(env)
    advance(env)
    seen = delayed_pair(env, monkeypatch, portfolio_delays=(61, 49), authority_delay=12)
    before = capacity_count(env)
    with pytest.raises(ContractError, match='STALE_WORKING_ORDER_EVIDENCE'):
        env.executor.recover()
    assert len(seen['orders']) == len(seen['portfolios']) == 2
    assert capacity_count(env) == before
    assert len(env.broker.submissions) == 1 and not env.broker.cancellations


def test_expiry_during_initial_portfolio_retry_preserves_no_send(env, monkeypatch):
    forecast = env.forecast()
    env.now = pd.Timestamp(forecast['target_close']) - pd.Timedelta(seconds=30)
    seen = delayed_pair(env, monkeypatch, portfolio_delays=(61, 0), authority_delay=0)
    result = env.executor.step(forecast)
    assert result['disposition'] == 'fixed_target_already_elapsed'
    assert len(seen['orders']) == len(seen['portfolios']) == 2
    assert native(env).snapshot().reservations == ()
    assert not env.broker.submissions and not env.broker.cancellations


def test_unknown_only_commitment_survives_stale_portfolio_pair_retry(env, monkeypatch):
    env.cash = 500
    _, unknown = uncertain(env)
    prior_native = asdict(native(env).lookup_reservation(unknown.reservation_id))
    prior_authority = next(row for row in account(env).read_state()['reservations']
                           if row['request']['native_reservation_id'] == unknown.reservation_id)
    seen = delayed_pair(env, monkeypatch, portfolio_delays=(61, 0), authority_delay=0)
    env.executor.recover()
    assert len(seen['orders']) == len(seen['portfolios']) == 2 and not any(seen['orders'])
    assert asdict(native(env).lookup_reservation(unknown.reservation_id)) == prior_native
    assert next(row for row in account(env).read_state()['reservations']
                if row['request']['native_reservation_id'] == unknown.reservation_id) == prior_authority
    assert len(env.broker.submissions) == 1 and not env.broker.cancellations
