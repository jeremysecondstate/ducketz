"""Real local journals/effect gates with injected account and broker fixtures."""
from datetime import timedelta
from types import SimpleNamespace
import json
import socket

import pytest

from datafetching.runtime_lock import exclusive_runtime_lock
from ml.intraday import scout_local_execution as execution
from ml.intraday.prediction import ContractError, TARGET_VERSION, digest, encoded, instant
from ml.intraday.store import ShadowStore
from ml.intraday.technical_sizing import SizingResult
from ml.stock_trader.contracts import PortfolioState, QuoteState
from test_intraday_scout_broker import Broker, GENERATION, ACCOUNT


def forecast(env, *, advance=False, probability=.72):
    if advance:
        env.reference += timedelta(minutes=15)
        env.state.now = env.reference + timedelta(minutes=16, seconds=10)
    cycle = 'cycle-' + str(len(env.store.forecasts()))
    env.store.begin_cycle(cycle, dict(pc='Scout', mode='live', experiment=env.executor.session_id,
        started_at=(env.reference + timedelta(minutes=15)).isoformat(), reference_at=env.reference.isoformat(),
        symbols=['DOCU'], configuration_hash='c' * 64))
    snapshot_hash, _ = env.store.freeze_inputs(cycle, 'DOCU', {'input': 'immutable'})
    return env.store.save_forecast(dict(pc='Scout', symbol='DOCU', cycle_id=cycle,
        reference_at=env.reference.isoformat(), reference_open='100',
        target_open=(env.reference + timedelta(minutes=30)).isoformat(),
        target_close=(env.reference + timedelta(minutes=45)).isoformat(),
        issued_at=(env.reference + timedelta(minutes=16, seconds=2)).isoformat(),
        information_available_at=(env.reference + timedelta(minutes=15, seconds=3)).isoformat(),
        data_through=(env.reference + timedelta(minutes=15)).isoformat(), target_version=TARGET_VERSION,
        snapshot_hash=snapshot_hash, mode='live', availability_basis='observed',
        probability_not_down=probability, configured_count=2, successful_count=2,
        ensemble_status='full', models=[dict(name=name, status='success', probability_not_down=probability)
                                      for name in ('a', 'b')]))


def raw_order(env, *, status='WORKING', filled=0, quantity=2):
    return dict(orderId='123', status=status, quantity=quantity, filledQuantity=filled,
        remainingQuantity=0 if status == 'CANCELED' else quantity-filled,
        orderLegCollection=[dict(legId=1, instruction='BUY', quantity=quantity,
            instrument=dict(assetType='EQUITY', symbol='DOCU'))],
        orderActivityCollection=([dict(activityType='EXECUTION', executionLegs=[dict(legId=1,
            executionId='fill:1', quantity=filled, price=99.9, time=env.fill_time.isoformat())])] if filled else []))


@pytest.fixture
def env(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('No network or Atlas access in local execution tests')
    monkeypatch.setattr(socket, 'socket', forbidden)
    reference = instant('2026-10-12T17:00:00Z')
    state = SimpleNamespace(now=reference + timedelta(minutes=16, seconds=10), manual=True, stop=False)
    store, broker = ShadowStore(tmp_path / 'forecasts.sqlite3'), Broker()
    executor = execution.ScoutLocalAccountExecutor(repository=tmp_path, datastore=tmp_path,
        profile=dict(actor='Scout', machine='pc-new', symbols=['DOCU']), store=store, broker=broker,
        local_symbols=('DOCU',), session_started_at=reference + timedelta(seconds=1), session_id='manual-local-test',
        clock=lambda: state.now, manual_authority=lambda: state.manual, stopped=lambda: state.stop)
    result = SimpleNamespace(executor=executor, state=state, broker=broker, store=store,
        reference=reference, cash=250, held=20, pending_sell=0, quote_age=0, captures=0,
        root=tmp_path, fill_time=state.now + timedelta(seconds=1), working=())
    def capture(*args, **kwargs):
        result.captures += 1
        at = state.now
        quote = QuoteState('DOCU', 99.99, 100., 100., 100., 1., (at - timedelta(seconds=result.quote_age)).isoformat())
        return PortfolioState(at.isoformat(), 10000, result.cash, 0, 0, {'DOCU': result.held}, {}, {},
            {'DOCU': result.pending_sell}, len(result.working), {'DOCU': quote}, 'd' * 64, GENERATION, result.working)
    monkeypatch.setattr(execution, 'capture_portfolio_state', capture)
    def size(saved, **kwargs):
        value = dict(status='sized', forecast_id=saved['forecast_id'], symbol=saved['symbol'],
            side='BUY' if saved['probability_not_down'] >= .5 else 'SELL', desired_quantity=4,
            calculated_at=state.now.isoformat())
        return SizingResult(digest(value), encoded(value))
    monkeypatch.setattr(execution, 'size_forecast', size)
    result.forecast = forecast(result)
    broker.orders = [raw_order(result)]
    result.proof = executor.preflight()
    yield result
    executor.journal.close()


def record(env, saved=None):
    return env.executor.journal.get(execution.PREFIX + (saved or env.forecast)['forecast_id'])


def test_local_preflight_uses_existing_broker_without_gameplan_config_atlas_or_enrollment(env):
    assert env.proof['mode'] == execution.MODE and env.proof['ready'] is True
    assert env.proof['local_proof']['broker_identity_verified'] is True
    assert 'readiness_receipt' not in env.proof and 'authority_binding' not in env.proof
    assert not (env.root / 'intraday/scout/authority-binding.json').exists()
    assert not (env.root / 'state/account-gameplan/config.json').exists()
    assert env.broker.posts == []


def test_zero_cash_still_records_full_intention_then_zero_capacity(env):
    env.cash = 0
    value = env.executor.step(env.forecast)
    assert value['desired_quantity'] == 4 and value['adjusted_quantity'] == 0
    assert value['disposition'] == 'no_purchasing_capacity'
    assert env.executor.intents.get(env.forecast['forecast_id'])['desired_quantity'] == 4
    assert env.broker.posts == []


def test_real_prepared_post_uses_local_durable_permit_and_net_cash_once(env):
    seen = []
    def before_post():
        item = record(env)
        assert item['state'] == 'SENDING' and item['grant']['grant_id'].startswith('local-permit:')
        permits = [json.loads(p.read_text()) for p in (env.executor.books.root / 'receipts').glob('*.json')]
        assert any(p['kind'] == 'local-account-permit' and p['payload']['mode'] == execution.MODE for p in permits)
        seen.append('fenced')
    env.broker.before_transport = before_post
    value = env.executor.step(env.forecast)
    assert value['disposition'] == 'submitted', value
    assert (value['desired_quantity'], value['adjusted_quantity'], value['submitted_quantity']) == (4, 2, 2)
    assert len(env.broker.posts) == 1 and seen == ['fenced']
    assert record(env)['broker_order_id'] == '123'
    assert value['order']['allocation_id'] == ''
    again = env.executor.step(env.forecast)
    assert again['order']['status'] == 'WORKING' and len(env.broker.posts) == 1


def test_sell_capacity_is_held_less_pending_and_never_buys_on_margin(env):
    env.held, env.pending_sell, env.cash = 3, 2, 0
    env.forecast = forecast(env, advance=True, probability=.2)
    value = env.executor.step(env.forecast)
    assert value['disposition'] == 'submitted', value
    assert value['desired_quantity'] == 4 and value['adjusted_quantity'] == 1
    assert env.broker.posts[0][0]['orderLegCollection'][0]['instruction'] == 'SELL'


@pytest.mark.parametrize('error', [TimeoutError('lost reply'), SystemExit('interrupted effect')])
def test_unknown_post_stays_fenced_across_recovery_and_new_forecast(env, error):
    env.broker.post_error = error
    if isinstance(error, Exception):
        value = env.executor.step(env.forecast)
        assert value['submitted_quantity'] is None and value['submission_status'] == 'UNKNOWN'
    else:
        with pytest.raises(SystemExit):
            env.executor.step(env.forecast)
    env.executor.recover()
    assert record(env)['state'] == 'UNKNOWN'
    env.forecast = forecast(env, advance=True)
    value = env.executor.step(env.forecast)
    assert value['disposition'] == 'submission_recovery_required'
    assert value['desired_quantity'] == 4
    assert len(env.broker.posts) == 1 and env.broker.deletes == []


def test_exact_partial_fill_cancel_ack_and_terminal_replacement(env):
    assert env.executor.step(env.forecast)['disposition'] == 'submitted'
    original = env.forecast
    env.state.now += timedelta(seconds=2)
    env.broker.orders = [raw_order(env, status='PARTIALLY_FILLED', filled=1)]
    observed = env.executor.recover()[0]
    assert observed['filled_quantity'] == 1
    env.forecast = forecast(env, advance=True)
    assert env.executor.step(env.forecast)['disposition'] == 'previous_intention_reconciliation'
    assert [identity for identity, _ in env.broker.deletes] == ['123']
    assert len(env.broker.posts) == 1
    env.state.now += timedelta(seconds=1)
    env.executor.step(env.forecast)
    assert len(env.broker.deletes) == 1 and len(env.broker.posts) == 1
    env.broker.orders = [raw_order(env, status='CANCELED', filled=1)]
    env.state.now += timedelta(seconds=1)
    env.broker.location = env.broker.location.replace('/123', '/124')
    assert env.executor.step(env.forecast)['disposition'] == 'submitted'
    state = env.executor.journal.order_state(record(env, original)['request']['request_id'])
    assert state['evidence']['status'] == 'CANCELLED' and state['evidence']['cumulative_filled_quantity'] == 1
    assert len(env.broker.posts) == 2


@pytest.mark.parametrize('change', ['stop', 'account', 'quote', 'receipt'])
def test_final_local_proof_and_manual_account_freshness_gates_block_post(env, change):
    def before_post():
        if change == 'stop':
            env.state.stop = True
        elif change == 'account':
            env.broker.account = 'b' * 64
        elif change == 'quote':
            env.state.now += timedelta(seconds=61)
        else:
            for path in (env.executor.books.root / 'receipts').glob('*.json'):
                value = json.loads(path.read_text())
                if value['kind'] == 'local-account-permit':
                    value['payload']['capacity']['adjusted_quantity'] = 99
                    path.write_text(json.dumps(value))
    env.broker.before_transport = before_post
    value = env.executor.step(env.forecast)
    assert value['disposition'] == 'submission_recovery_required' and env.broker.posts == []
    assert record(env)['state'] == 'UNKNOWN'


def test_stale_quote_is_not_an_order_and_retains_desired_sizing(env):
    env.quote_age = 61
    result = env.executor.step(env.forecast)
    assert result['desired_quantity'] == 4 and env.broker.posts == []


def test_broker_identity_change_cannot_rebind_existing_books(env):
    env.broker.account = 'b' * 64
    with pytest.raises(ContractError, match='broker account changed'):
        env.executor.step(env.forecast)


def test_gameplan_lock_and_other_trader_verification_are_not_scout_prerequisites(env, monkeypatch):
    from ml.intraday import scout_prestart, scout_execution
    def forbidden(*args, **kwargs):
        raise AssertionError('Scout must not verify Gameplan or another trader')
    monkeypatch.setattr(scout_prestart, 'inspect_local_account_writers', forbidden)
    monkeypatch.setattr(scout_execution, 'inspect_local_account_writers', forbidden)
    def own_lock_held():
        assert (env.executor.books.root / 'execution.lock').exists()
    env.broker.before_transport = own_lock_held
    with exclusive_runtime_lock(env.root / 'locks/stock-trader-hourly.lock', process_name='stock-trader-hourly'):
        result = env.executor.step(env.forecast)
    assert result['disposition'] == 'submitted' and len(env.broker.posts) == 1
