"""Read-only order-view recovery with synthetic Gameplan orders and ledgers."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from test_independent_stock_runtime import environment, ACCOUNT
from test_account_gameplan_execution import account_env, run, native, state
from ml.account_gameplan import execution
from ml.stock_trader import independent_runtime as runtime
from ml.stock_trader.horizon_ledger import OrderEvidence, FillEvidence
from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY


def own_sell(env):
    key = ('AAPL', '1h')
    env.signals = {key: replace(env.signals[key], calibrated_probability=.2)}
    env.plan.rows = env.plan.rows.loc[env.plan.rows.id.eq(env.signals[key].prediction_id)].copy()
    env.plan.rows.loc[:, 'calibrated_probability'] = .2
    env.held['AAPL'] = 20.
    result = run(env)
    assert result.submitted_orders == 1, (result.status, result.error)
    prior, = native(env).snapshot().reservations
    assert prior.side == 'SELL' and prior.quantity == 20
    env.now += pd.Timedelta(seconds=1)
    return prior


def working(prior, filled=1):
    return dict(status='CURRENT', asset_type='EQUITY', instruction=prior.side,
        symbol=prior.symbol, remaining_quantity=prior.quantity-filled,
        filled_quantity=filled, limit_price=prior.limit_price, reserved_cash=0.,
        order_id=prior.broker_order_id)


def detail(prior, now, filled=0, terminal=False):
    return OrderEvidence('history-'+now.isoformat(), prior.reservation_id, ACCOUNT,
        now.isoformat(), prior.broker_order_id,
        'FILLED' if terminal else 'PARTIAL' if filled else 'WORKING',
        prior.quantity, filled, 0 if terminal else prior.quantity-filled,
        (FillEvidence('original-fill', filled, prior.limit_price, now.isoformat()),) if filled else ())


def retry_run(env, *, budget=10., attempts=60, on_sleep=None):
    elapsed = [0.]
    sleeps = []
    def sleep(seconds):
        sleeps.append(seconds)
        elapsed[0] += seconds
        env.now += pd.Timedelta(seconds=seconds)
        if on_sleep:
            on_sleep()
    result = runtime.run_independent_stock_trader_once(env.root, execute=True, session=env.broker,
        runtime_clock=lambda: env.now, session_managed=True, sizing_policy=GAMEPLAN_SIZING_POLICY,
        broker_state_retry_max_seconds=budget, broker_state_retry_max_attempts=attempts,
        broker_state_retry_sleep=sleep, broker_state_retry_clock=lambda: elapsed[0])
    return result, sleeps


def install_pair(env, prior, monkeypatch, *, stable_after_first=True):
    reads = []
    def history(*args, **kwargs):
        reads.append('history')
        filled = 1 if len(reads) > 2 and stable_after_first else 0
        return (detail(prior, env.now, filled),)
    monkeypatch.setattr('ml.stock_trader.horizon_broker.capture_order_evidence', history)
    capture = runtime.capture_portfolio_state
    def portfolio(*args, **kwargs):
        reads.append('portfolio')
        # The broker's working-order endpoint sees the same real fill first.
        env.held['AAPL'] = 19.
        result = capture(*args, **kwargs)
        return replace(result, broker_working_orders=(working(prior),),
            working_order_count=1, pending_sell_shares={'AAPL':19.})
    monkeypatch.setattr(runtime, 'capture_portfolio_state', portfolio)
    return reads


def test_recaptures_complete_pair_before_reconciling_a_naturally_partial_sell(account_env, monkeypatch):
    env = account_env
    prior = own_sell(env)
    reads = install_pair(env, prior, monkeypatch)
    reconcile = runtime.HorizonLedger.reconcile
    native_reconciliations = []
    def tracked(self, *args, **kwargs):
        native_reconciliations.append(tuple(reads))
        return reconcile(self, *args, **kwargs)
    monkeypatch.setattr(runtime.HorizonLedger, 'reconcile', tracked)
    result, sleeps = retry_run(env)
    assert result.status == 'NO_ORDERS_SUBMITTED', (result.status, result.error)
    assert sleeps == [3.]
    assert reads == ['history','portfolio','history','portfolio']
    assert native_reconciliations == [tuple(reads)]
    assert result.broker_state_capture['status'] == 'CURRENT_AFTER_RETRY'
    assert result.broker_state_capture['last_error_operation'] == 'tracked_order_snapshot_coherence'
    assert len(env.broker.submissions) == 1 and not env.broker.cancellations
    row, = state(env)['reservations']
    assert row['broker_order_id'] == prior.broker_order_id and row['filled_quantity'] == 1
    assert native(env).snapshot().reservations[0].filled_quantity == 1


@pytest.mark.parametrize('ending', ['exhausted','manual_stop','signal_expired'])
def test_inconsistent_pair_preserves_orders_and_existing_capture_bounds(account_env, monkeypatch, ending):
    env = account_env
    prior = own_sell(env)
    before_native = native(env).snapshot().reservations
    before_authority = state(env)['reservations']
    reads = install_pair(env, prior, monkeypatch, stable_after_first=False)
    def after_sleep():
        if ending == 'manual_stop':
            env.active = False
        elif ending == 'signal_expired':
            env.now = pd.Timestamp(next(iter(env.signals.values())).actionable_until)
    result, sleeps = retry_run(env, budget=7., on_sleep=after_sleep)
    assert result.status == 'HORIZON_BROKER_RECONCILIATION_UNAVAILABLE'
    assert result.submitted_orders == 0 and len(env.broker.submissions) == 1
    assert not env.broker.cancellations
    assert native(env).snapshot().reservations == before_native
    assert state(env)['reservations'] == before_authority
    if ending == 'exhausted':
        assert sleeps == [3.,3.] and reads == ['history','portfolio']*3
        assert result.broker_state_capture['attempts'] == 3
    else:
        assert sleeps == [3.] and reads == ['history','portfolio']
        assert ('TRADER_INACTIVE_DURING_BROKER_CAPTURE' if ending == 'manual_stop'
                else 'BROKER_READ_EXECUTION_WINDOW_CLOSED') in result.error


def pure(prior=None, observed=None, row=None):
    prior = prior or SimpleNamespace(reservation_id='native-1', broker_order_id='order-1',
        symbol='AAPL', side='SELL', quantity=20, status='WORKING', filled_quantity=0,
        reserved_quantity=20)
    observed = observed or detail(prior, pd.Timestamp('2026-09-08T11:01:01Z'))
    ledger = SimpleNamespace(account_fingerprint=ACCOUNT,
        snapshot=lambda: SimpleNamespace(reservations=(prior,)))
    portfolio = SimpleNamespace(broker_working_orders=(row or working(prior),),quotes={})
    return execution.assert_order_snapshot_coherence(ledger, (observed,), portfolio)


def test_valid_partial_progression_is_retryable_and_does_not_mutate_rows():
    row = working(SimpleNamespace(side='SELL',symbol='AAPL',quantity=20,limit_price=100.,
        broker_order_id='order-1'))
    before = dict(row)
    with pytest.raises(execution.OrderSnapshotCoherenceError) as caught:
        pure(row=row)
    assert caught.value.schwab_retry_safe is True
    assert caught.value.stock_trader_operation == 'tracked_order_snapshot_coherence'
    assert row == before


@pytest.mark.parametrize('field,value', [('symbol','WRONG'),('instruction','BUY'),
    ('remaining_quantity',20),('filled_quantity',.5),('filled_quantity',True),
    ('status','UNKNOWN'),('asset_type','OPTION'),('remaining_quantity',-1),
    ('filled_quantity',float('nan'))])
def test_identity_quantity_and_malformed_evidence_remain_fatal(field, value):
    row = working(SimpleNamespace(side='SELL',symbol='AAPL',quantity=20,limit_price=100.,
        broker_order_id='order-1'))
    row[field] = value
    with pytest.raises((ValueError,RuntimeError)) as caught:
        pure(row=row)
    assert not isinstance(caught.value, execution.OrderSnapshotCoherenceError)
    assert getattr(caught.value,'schwab_retry_safe',False) is False


def test_unknown_order_identity_is_retained_for_existing_capacity_handling():
    row = working(SimpleNamespace(side='SELL',symbol='AAPL',quantity=20,limit_price=100.,
        broker_order_id='untracked-order'))
    before = dict(row)
    assert pure(row=row) is None
    assert row == before


@pytest.mark.parametrize('terminal_status', ['FILLED','CANCELLED','REJECTED'])
def test_terminal_history_and_stale_working_view_recapture_without_releasing_order(terminal_status):
    prior = SimpleNamespace(reservation_id='native-1', broker_order_id='order-1',
        symbol='AAPL', side='SELL', quantity=20, status='WORKING', filled_quantity=0,
        reserved_quantity=20, limit_price=100.)
    observed = replace(detail(prior,pd.Timestamp('2026-09-08T11:01:01Z'),
        filled=20 if terminal_status == 'FILLED' else 0, terminal=True),
        status=terminal_status)
    with pytest.raises(execution.OrderSnapshotCoherenceError):
        pure(prior=prior,observed=observed,row=working(prior,0))


@pytest.mark.parametrize('field,value', [('broker_order_id','wrong-order'),
    ('account_fingerprint','b'*64),('order_quantity',19),
    ('remaining_quantity',19),('status','UNKNOWN')])
def test_malformed_history_identity_and_quantity_remain_fatal(field,value):
    prior = SimpleNamespace(reservation_id='native-1', broker_order_id='order-1',
        symbol='AAPL', side='SELL', quantity=20, status='WORKING', filled_quantity=0,
        reserved_quantity=20, limit_price=100.)
    observed = replace(detail(prior,pd.Timestamp('2026-09-08T11:01:01Z')),**{field:value})
    with pytest.raises((ValueError,RuntimeError)) as caught:
        pure(prior=prior,observed=observed,row=working(prior,0))
    assert not isinstance(caught.value,execution.OrderSnapshotCoherenceError)


def test_matching_pair_and_missing_working_rows_keep_existing_handling():
    prior = SimpleNamespace(reservation_id='native-1', broker_order_id='order-1',
        symbol='AAPL', side='SELL', quantity=20, status='WORKING', filled_quantity=0,
        reserved_quantity=20, limit_price=100.)
    assert pure(prior=prior,row=working(prior,0)) is None
    ledger = SimpleNamespace(account_fingerprint=ACCOUNT,
        snapshot=lambda: SimpleNamespace(reservations=(prior,)))
    assert execution.assert_order_snapshot_coherence(ledger,(),
        SimpleNamespace(broker_working_orders=None)) is None


def test_terminal_native_with_no_pending_history_still_recaptures_stale_working_row():
    prior = SimpleNamespace(reservation_id='native-1', broker_order_id='order-1',
        symbol='AAPL', side='SELL', quantity=20, status='FILLED', filled_quantity=20,
        reserved_quantity=0, limit_price=100.)
    ledger = SimpleNamespace(account_fingerprint=ACCOUNT,
        snapshot=lambda: SimpleNamespace(reservations=(prior,)))
    with pytest.raises(execution.OrderSnapshotCoherenceError):
        execution.assert_order_snapshot_coherence(ledger,(),
            SimpleNamespace(broker_working_orders=(working(prior,0),),quotes={}))


@pytest.mark.parametrize('bad', ['price_zero','price_nan','cash_negative','cash_nan',
    'sell_cash','duplicate_known','duplicate_unknown','late_bad_symbol','late_bad_quantity',
    'late_bad_price','late_bad_cash','late_bad_shape','late_bad_status','late_bad_asset',
    'late_control_id','late_long_id','late_bad_symbol_format'])
def test_progress_disagreement_never_masks_another_fatal_working_snapshot_defect(bad):
    prior = SimpleNamespace(reservation_id='native-1', broker_order_id='order-1',
        symbol='AAPL', side='SELL', quantity=20, status='WORKING', filled_quantity=0,
        reserved_quantity=20, limit_price=100.)
    first = working(prior)
    rows = [first]
    other = {**working(prior,0),'order_id':'untracked','instruction':'BUY','reserved_cash':0}
    if bad == 'price_zero': first['limit_price'] = 0
    elif bad == 'price_nan': first['limit_price'] = float('nan')
    elif bad == 'cash_negative': first['reserved_cash'] = -1
    elif bad == 'cash_nan': first['reserved_cash'] = float('nan')
    elif bad == 'sell_cash': first['reserved_cash'] = 1
    elif bad == 'duplicate_known': rows.append(dict(first))
    elif bad == 'duplicate_unknown': rows.extend([other,dict(other)])
    elif bad == 'late_bad_symbol': rows.append({**working(prior,0),'symbol':'WRONG'})
    elif bad == 'late_bad_quantity': rows.append({**working(prior,0),'remaining_quantity':19})
    elif bad == 'late_bad_price': rows.append({**other,'limit_price':0})
    elif bad == 'late_bad_cash': rows.append({**other,'reserved_cash':float('nan')})
    elif bad == 'late_bad_shape': rows.append(None)
    elif bad == 'late_bad_status': rows.append({**other,'status':'UNKNOWN'})
    elif bad == 'late_bad_asset': rows.append({**other,'asset_type':'OPTION'})
    elif bad == 'late_control_id': rows.append({**other,'order_id':'bad\nidentity'})
    elif bad == 'late_long_id': rows.append({**other,'order_id':'x'*257})
    elif bad == 'late_bad_symbol_format': rows.append({**other,'symbol':'bad-symbol'})
    ledger = SimpleNamespace(account_fingerprint=ACCOUNT,
        snapshot=lambda: SimpleNamespace(reservations=(prior,)))
    with pytest.raises(Exception) as caught:
        execution.assert_order_snapshot_coherence(ledger,
            (detail(prior,pd.Timestamp('2026-09-08T11:01:01Z')),),
            SimpleNamespace(broker_working_orders=tuple(rows),quotes={}))
    assert not isinstance(caught.value,execution.OrderSnapshotCoherenceError)
    assert getattr(caught.value,'schwab_retry_safe',False) is False
