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


def forecast(env, *, advance=False, probability=.72, snapshot=None):
    if advance:
        env.reference += timedelta(minutes=15)
        env.state.now = env.reference + timedelta(minutes=16, seconds=10)
    cycle = 'cycle-' + str(len(env.store.forecasts()))
    env.store.begin_cycle(cycle, dict(pc='Scout', mode='live', experiment=env.executor.session_id,
        started_at=(env.reference + timedelta(minutes=15)).isoformat(), reference_at=env.reference.isoformat(),
        symbols=['DOCU'], configuration_hash='c' * 64))
    snapshot_hash, _ = env.store.freeze_inputs(cycle, 'DOCU', snapshot or {'input': 'immutable'})
    return env.store.save_forecast(dict(pc='Scout', symbol='DOCU', cycle_id=cycle,
        reference_at=env.reference.isoformat(),
        reference_open=str(snapshot['inference']['reference_open']) if snapshot else '100',
        source_id=snapshot['inference']['source'] if snapshot else None,
        source_dataset=snapshot['inference']['source_dataset'] if snapshot else None,
        target_open=(env.reference + timedelta(minutes=30)).isoformat(),
        target_close=(env.reference + timedelta(minutes=45)).isoformat(),
        issued_at=(env.reference + timedelta(minutes=16, seconds=2)).isoformat(),
        information_available_at=(env.reference + timedelta(minutes=15, seconds=3)).isoformat(),
        training_cutoff=(env.reference + timedelta(minutes=15, seconds=3)).isoformat(),
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


@pytest.fixture(params=['datetime', 'iso8601'])
def env(tmp_path, monkeypatch, request):
    def forbidden(*args, **kwargs):
        raise AssertionError('No network or Atlas access in local execution tests')
    monkeypatch.setattr(socket, 'socket', forbidden)
    reference = instant('2026-10-12T17:00:00Z')
    state = SimpleNamespace(now=reference + timedelta(minutes=16, seconds=10), manual=True, stop=False)
    store, broker = ShadowStore(tmp_path / 'forecasts.sqlite3'), Broker()
    executor = execution.ScoutLocalAccountExecutor(repository=tmp_path, datastore=tmp_path,
        profile=dict(actor='Scout', machine='pc-new', symbols=['DOCU']), store=store, broker=broker,
        local_symbols=('DOCU',), session_started_at=reference + timedelta(seconds=1), session_id='manual-local-test',
        clock=lambda: state.now.isoformat() if request.param == 'iso8601' else state.now,
        manual_authority=lambda: state.manual, stopped=lambda: state.stop)
    result = SimpleNamespace(executor=executor, state=state, broker=broker, store=store,
        reference=reference, cash=250, held=20, pending_sell=0, quote_age=0, captures=0,
        root=tmp_path, fill_time=state.now + timedelta(seconds=1), working=(),
        capture_seconds=0, account_age=0, realtime_nbbo=False)
    def capture(*args, **kwargs):
        result.captures += 1
        at = instant(kwargs['observed_at']) - timedelta(seconds=result.account_age)
        state.now += timedelta(seconds=result.capture_seconds)
        quote_at = state.now - timedelta(seconds=result.quote_age)
        quote = QuoteState('DOCU', 99.99, 100., 100., 100., 1.,
            (at - timedelta(seconds=120)).isoformat() if result.realtime_nbbo else quote_at.isoformat(),
            received_at=quote_at.isoformat() if result.realtime_nbbo else None,
            realtime=True if result.realtime_nbbo else None,
            quote_type='NBBO' if result.realtime_nbbo else None)
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


def use_real_sizing(env, monkeypatch):
    from ml.intraday.starter_sizing import size_forecast
    from test_intraday_starter_sizing import fixture as sizing_fixture

    _, snapshot, _, _ = sizing_fixture(symbol='DOCU')
    reference = env.reference + timedelta(minutes=15)
    offset = reference - instant(snapshot['inference']['reference_at'])
    for candle in snapshot['candles']:
        for key in ('opened_at', 'available_at', 'session_open', 'session_close'):
            candle[key] = (instant(candle[key]) + offset).isoformat()
        candle['session_id'] = instant(candle['session_open']).date().isoformat()
    snapshot['cutoff'] = (instant(snapshot['cutoff']) + offset).isoformat()
    snapshot['inference']['reference_at'] = reference.isoformat()
    env.forecast = forecast(env, advance=True, snapshot=snapshot)
    monkeypatch.setattr(execution, 'size_forecast', size_forecast)


@pytest.mark.parametrize('quote_age', [0, 61])
def test_real_sizing_preserves_captured_equity_provenance_and_quote_freshness(env, monkeypatch, quote_age):
    use_real_sizing(env, monkeypatch)
    env.cash, env.quote_age = 0, quote_age
    result = env.executor.step(env.forecast)
    if quote_age:
        assert result['kind'] == 'sizing_unavailable'
        sizing = result['evidence']['sizing']
        assert sizing['reason'] == 'quote_stale_or_future'
        assert env.executor.intents.get(env.forecast['forecast_id']) is None
    else:
        intention = env.executor.intents.get(env.forecast['forecast_id'])
        assert intention is not None, result
        sizing = intention['sizing']
        assert sizing['status'] == 'sized' and intention['desired_quantity'] > 0
        assert result['disposition'] == 'no_purchasing_capacity'
        assert sizing['indicator_snapshot']['forecast_snapshot_hash'] == env.forecast['snapshot_hash']
    equity = sizing['reference_context']['account_equity']
    assert equity['value'] == 10000 and equity['source'] == 'schwab-liquidation-equity'
    assert equity['available_at'] == env.state.now.isoformat()
    assert equity['evidence'] == dict(account_fingerprint=ACCOUNT,
        snapshot_identity=GENERATION, snapshot_sha256='d' * 64, observed_at=env.state.now.isoformat())
    assert env.broker.posts == [] and env.broker.deletes == []


@pytest.mark.parametrize('realtime_nbbo', [False, True], ids=['ordinary-quote', 'realtime-nbbo'])
def test_real_sizing_uses_quote_availability_after_capture_and_submits_positive_size(env, monkeypatch, realtime_nbbo):
    use_real_sizing(env, monkeypatch)
    env.capture_seconds, env.quote_age, env.realtime_nbbo = 2, 1, realtime_nbbo
    capture_started_at = env.state.now
    quote_observed_at = capture_started_at + timedelta(seconds=1)
    quote_available_at = capture_started_at + timedelta(seconds=2)

    result = env.executor.step(env.forecast)

    assert result['disposition'] == 'submitted', result
    assert result['desired_quantity'] > 0
    assert result['adjusted_quantity'] == result['submitted_quantity'] > 0
    assert len(env.broker.posts) == 1 and env.broker.deletes == []
    sizing = env.executor.intents.get(env.forecast['forecast_id'])['sizing']
    quote = sizing['reference_context']['quote']
    assert quote['observed_at'] == quote_observed_at.isoformat()
    assert quote['available_at'] == quote_available_at.isoformat()
    if realtime_nbbo:
        evidence = quote['evidence']['quote']
        assert evidence['received_at'] == quote_observed_at.isoformat()
        assert evidence['observed_at'] == (capture_started_at - timedelta(seconds=120)).isoformat()
    equity = sizing['reference_context']['account_equity']
    assert equity['available_at'] == capture_started_at.isoformat()
    assert equity['evidence'] == dict(account_fingerprint=ACCOUNT,
        snapshot_identity=GENERATION, snapshot_sha256='d' * 64, observed_at=capture_started_at.isoformat())


@pytest.mark.parametrize('realtime_nbbo', [False, True], ids=['ordinary-quote', 'realtime-nbbo'])
@pytest.mark.parametrize('quote_age,reason', [
    (61, 'quote_stale_or_future'), (-1, 'quote_observation_after_availability')], ids=['stale', 'future'])
def test_post_capture_availability_does_not_hide_stale_or_future_quotes(env, monkeypatch, realtime_nbbo, quote_age, reason):
    use_real_sizing(env, monkeypatch)
    env.capture_seconds, env.quote_age, env.realtime_nbbo = 2, quote_age, realtime_nbbo

    result = env.executor.step(env.forecast)

    assert result['kind'] == 'sizing_unavailable'
    assert result['evidence']['sizing']['reason'] == reason
    assert env.executor.intents.get(env.forecast['forecast_id']) is None
    assert env.broker.posts == [] and env.broker.deletes == []


def test_post_capture_availability_does_not_refresh_stale_account_evidence(env, monkeypatch):
    use_real_sizing(env, monkeypatch)
    env.capture_seconds, env.account_age, env.quote_age = 2, 61, 1
    with pytest.raises(ContractError, match='Local account snapshot is stale'):
        env.executor.step(env.forecast)
    assert env.executor.intents.get(env.forecast['forecast_id']) is None
    assert env.broker.posts == [] and env.broker.deletes == []


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
