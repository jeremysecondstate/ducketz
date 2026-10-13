"""Offline Scout effects through real private and shared books and wire validators.

Only portfolio capture and the broker transport are synthetic. The admission
service, authority, native ledger, Scout client, journal, receipts and final POST
gates are real temporary instances. No socket or production store is opened.
"""
from datetime import timedelta
from types import SimpleNamespace
import json
import sqlite3

import pytest

from datafetching.runtime_lock import exclusive_runtime_lock
from ml.intraday import scout_execution as execution
from ml.intraday.admission_service import AdmissionError, BookSnapshot
from ml.intraday.prediction import ContractError, digest, encoded, instant
from ml.intraday.scout_authority import ScoutAuthorityClient
from ml.intraday.scout_books import ScoutBooks
from ml.intraday.technical_sizing import SizingResult
from ml.stock_trader.contracts import PortfolioState, QuoteState
from ml.stock_trader.horizon_ledger import PortfolioEvidence
from test_intraday_admission_service import Fixture as BaseFixture
from test_intraday_account_authority import ACCOUNT, ATLAS, SCOUT, snapshot
from test_intraday_scout_authority import Transport, binding
from test_intraday_scout_broker import Broker, GENERATION
from test_intraday_scout_runtime_gate import make_case


class Fixture(BaseFixture):
    def refresh(self, operation):
        """Synthetic account evidence includes fills already in the real books."""
        self.clock.tick()
        self.captures += 1
        identity = f'capture-{self.captures}'
        native = operation.ledger.snapshot()
        held = {symbol: 20 + sum(a.filled_shares for a in native.allocations if a.symbol == symbol)
            for symbol in (*ATLAS, *SCOUT)}
        fills = {r.broker_order_id: r.filled_quantity for r in native.reservations
            if r.broker_order_id and r.filled_quantity}
        value = snapshot(self.clock, snapshot_id=identity, cash_available=self.cash,
            held_shares=held, accounted_fills=fills)
        operation.authority.publish_intraday_snapshot(operation.lease.token, value)
        operation.ledger.reconcile(PortfolioEvidence(identity, ACCOUNT, self.clock().isoformat(),
            held, {s:'100' for s in held}, {s:'10000' for s in held}, 'f'*64))
        return BookSnapshot(identity, self.clock().isoformat())


def save_sizing(executor, forecast, quantity=4):
    value = dict(status='sized', forecast_id=forecast['forecast_id'], symbol=forecast['symbol'],
        side='BUY' if forecast['probability_not_down'] >= .5 else 'SELL',
        desired_quantity=quantity, calculated_at=executor.clock().isoformat())
    return executor.intents.save_sizing(forecast, SizingResult(digest(value), encoded(value)))


def save_forecast(env, *, session=None, probability=.72, advance=True):
    old = env.forecast
    value = {key: item for key, item in old.items() if key != 'forecast_id'}
    cycle = 'cycle-' + str(len(env.store.forecasts()) + 1)
    if advance:
        reference = instant(old['reference_at']) + timedelta(minutes=15)
        env.atlas.clock.value = max(env.atlas.clock() + timedelta(seconds=1),
            reference + timedelta(minutes=16, seconds=2))
        value.update(reference_at=reference.isoformat(), data_through=(reference+timedelta(minutes=15)).isoformat(),
            information_available_at=(reference+timedelta(minutes=15, seconds=3)).isoformat(),
            target_open=(reference+timedelta(minutes=30)).isoformat(),
            target_close=(reference+timedelta(minutes=45)).isoformat())
    else:
        env.atlas.clock.tick()
    value.update(cycle_id=cycle, symbol='MSFT', probability_not_down=probability,
        issued_at=env.atlas.clock().isoformat(), models=[dict(name=name, status='success',
            probability_not_down=probability) for name in ('a', 'b')])
    env.store.begin_cycle(cycle, dict(pc='Scout', mode='live', experiment=session or env.executor.session_id,
        started_at=env.atlas.clock().isoformat(), reference_at=value['reference_at'],
        symbols=['MSFT'], configuration_hash='c'*64))
    value['snapshot_hash'], _ = env.store.freeze_inputs(cycle, 'MSFT', {'input': 'immutable'})
    value = env.store.save_forecast(value)
    save_sizing(env.executor, value)
    return value


def raw_order(env, *, status='WORKING', filled=0, order_id='123', quantity=2):
    return dict(orderId=order_id, status=status, quantity=quantity, filledQuantity=filled,
        remainingQuantity=0 if status == 'CANCELED' else quantity-filled,
        orderLegCollection=[dict(legId=1, instruction='BUY', quantity=quantity,
            instrument=dict(assetType='EQUITY', symbol='MSFT'))],
        orderActivityCollection=([dict(activityType='EXECUTION', executionLegs=[dict(legId=1,
            executionId='fill:1', quantity=filled, price=99.9, time=env.atlas.clock().isoformat())])] if filled else []))


@pytest.fixture
def env(tmp_path, monkeypatch):
    case = make_case(tmp_path / 'forecast', reference='2026-10-12T17:00:00Z')
    atlas = Fixture(tmp_path / 'atlas')
    atlas.clock.value = case.state.now
    atlas.cash = '250'
    books = ScoutBooks(tmp_path / 'scout', account_fingerprint='a'*64)
    transport = Transport(atlas.service)
    client = ScoutAuthorityClient(binding(tmp_path), books, transport=transport, clock=atlas.clock)
    ready = dict(ready=True, authenticated=True, protocol_compatible=True, participant_id='pc-new',
        stream='scout-15m', account_fingerprint='a'*64)
    # The currently published route intentionally refuses production readiness.
    # This exact offline proof is an injected host capability, not a live receipt.
    monkeypatch.setattr(client, 'readiness', lambda: dict(ready))
    monkeypatch.setattr(execution, 'inspect_local_account_writers',
        lambda *args, **kwargs: dict(ready=True, mode='verified_gameplan_absent'))
    broker = Broker()
    executor = execution.ScoutAccountExecutor(repository=tmp_path, profile={}, datastore=tmp_path,
        store=case.store, broker=broker, local_symbols=('MSFT',),
        session_started_at=case.kwargs['session_started'], session_id=case.kwargs['session_id'],
        clock=atlas.clock, manual_authority=lambda: case.state.manual, stopped=lambda: case.state.stop,
        authority=client, books=books)
    result = SimpleNamespace(atlas=atlas, store=case.store, books=books, transport=transport,
        client=client, broker=broker, executor=executor, forecast=case.forecast, state=case.state,
        ready=ready, quote_age=0, quote_ask=100.0, capture_count=0)
    result.forecast = save_forecast(result, advance=False)
    def capture(*args, **kwargs):
        result.capture_count += 1
        at = atlas.clock()
        quote = QuoteState('MSFT', 99.99, result.quote_ask, 100.0, 100.0, 1.,
            (at-timedelta(seconds=result.quote_age)).isoformat())
        return PortfolioState(at.isoformat(), 10000, 250, 0, 0, {'MSFT':20}, {'MSFT':2000}, {}, {}, 0,
            {'MSFT':quote}, 'd'*64, GENERATION, ())
    monkeypatch.setattr(execution, 'capture_portfolio_state', capture)
    broker.orders = [raw_order(result)]
    executor.preflight()
    yield result
    if executor.journal is not None:
        executor.journal.close()


def record(env, forecast=None):
    return env.executor.journal.get('scout-15m:'+(forecast or env.forecast)['forecast_id'])


def reservation(env, forecast=None):
    return env.atlas.authority.reservation(record(env, forecast)['grant']['reservation_id'])


def submit(env):
    result = env.executor.step(env.forecast)
    assert result['disposition'] == 'submitted', result
    return result


def test_real_wire_authorization_and_durable_local_claim_precede_one_exact_reduced_post(env):
    checks = []
    def before_post():
        current = record(env)
        assert current['state'] == 'SENDING'
        shared = reservation(env)
        assert shared.status == 'SUBMITTING' and shared.broker_order_id is None
        assert any(path == '/v1/authorize-send' for _, path, _ in env.transport.calls)
        checks.append('durable-local-and-shared-claim')
    env.broker.before_transport = before_post
    value = submit(env)
    assert checks == ['durable-local-and-shared-claim']
    assert value['desired_quantity'] == 4 and value['adjusted_quantity'] == 2
    assert value['submitted_quantity'] == 2 and value['filled_quantity'] == 0
    assert record(env)['broker_order_id'] == '123'
    assert reservation(env).status == 'SUBMITTED'
    payload, _ = env.broker.posts[0]
    assert payload['price'] == '100.00' and payload['orderLegCollection'][0]['quantity'] == 2
    assert list((env.books.root / 'receipts').rglob('*.json'))


def test_repeated_forecast_recovers_exact_order_without_second_post(env):
    submit(env)
    value = env.executor.step(env.forecast)
    assert value['disposition'] == 'recover_existing_intention'
    assert len(env.broker.posts) == 1 and reservation(env).status == 'WORKING'
    assert len([1 for _, path, _ in env.transport.calls if path == '/v1/authorize-send']) == 1


def test_zero_capacity_records_decline_without_claim_or_broker_effect(env):
    env.atlas.cash = '0'
    value = env.executor.step(env.forecast)
    assert value['disposition'] == 'declined' and value['adjusted_quantity'] == 0
    assert record(env)['state'] == 'NOT_SENT' and record(env)['grant'] is None
    assert env.broker.posts == [] and env.atlas.authority.read_state()['reservations'] == []
    # Repeating a completed zero-capacity decision must not turn into an error or reserve anew.
    env.executor.step(env.forecast)
    assert env.broker.posts == [] and env.atlas.captures == 1


def test_lost_reserve_reply_keeps_current_unclaimed_attempt_for_exact_queue_retry(env):
    env.transport.drop = '/v1/reserve'
    with pytest.raises(TimeoutError):
        env.executor.step(env.forecast)
    assert record(env)['state'] == 'PREPARED' and record(env)['grant'] is None
    assert len(env.atlas.authority.read_state()['reservations']) == 1
    env.executor.recover()
    assert record(env)['state'] == 'PREPARED' and env.broker.posts == []
    assert env.executor.step(env.forecast)['disposition'] == 'submitted'
    assert env.atlas.captures == 2 and len(env.broker.posts) == 1
    assert len(env.atlas.authority.read_state()['reservations']) == 1


def test_superseded_prepared_attempt_recovers_original_grant_then_releases_without_post(env):
    env.transport.drop = '/v1/reserve'
    with pytest.raises(TimeoutError):
        env.executor.step(env.forecast)
    save_forecast(env)
    env.executor.recover()
    assert record(env)['state'] == 'NOT_SENT' and reservation(env).status == 'NOT_SUBMITTED'
    assert env.broker.posts == [] and len(env.atlas.authority.read_state()['reservations']) == 1


def test_lost_authorize_reply_keeps_commitment_and_never_retries_post(env):
    env.transport.drop = '/v1/authorize-send'
    value = env.executor.step(env.forecast)
    assert value['disposition'] == 'submission_recovery_required'
    assert record(env)['state'] == 'UNKNOWN' and env.broker.posts == []
    env.executor.recover()
    assert reservation(env).status == 'UNKNOWN' and env.broker.posts == []
    assert len([1 for _, path, _ in env.transport.calls if path == '/v1/authorize-send']) == 1


@pytest.mark.parametrize('error', [TimeoutError('lost POST reply'), SystemExit('crash during POST')])
def test_post_uncertainty_or_crash_retains_single_attempt_and_shared_commitment(env, error):
    env.broker.post_error = error
    if isinstance(error, Exception):
        assert env.executor.step(env.forecast)['disposition'] == 'submission_recovery_required'
    else:
        with pytest.raises(SystemExit):
            env.executor.step(env.forecast)
        assert record(env)['state'] == 'SENDING'
    assert len(env.broker.posts) == 1
    env.executor.recover()
    assert record(env)['state'] == 'UNKNOWN' and reservation(env).status == 'UNKNOWN'
    assert len(env.broker.posts) == 1 and env.broker.deletes == []


def test_lost_accepted_event_reply_replays_same_event_and_exact_order(env):
    env.transport.drop = '/v1/evidence'
    assert env.executor.step(env.forecast)['disposition'] == 'submission_recovery_required'
    assert record(env)['state'] == 'ACCEPTED' and reservation(env).broker_order_id == '123'
    first = next(body for _, path, body in env.transport.calls if path == '/v1/evidence')
    env.executor.recover()
    accepted = [body for _, path, body in env.transport.calls if path == '/v1/evidence' and body['kind'] == 'ACCEPTED']
    assert accepted == [first, first] and len(env.broker.posts) == 1
    assert reservation(env).status == 'WORKING'


def test_partial_fill_is_private_and_shared_actual_fill_not_submission_count(env):
    submit(env)
    env.atlas.clock.tick()
    env.broker.orders = [raw_order(env, filled=1)]
    value, = env.executor.recover()
    assert value['submitted_quantity'] == 2 and value['filled_quantity'] == 1
    assert reservation(env).status == 'PARTIAL' and reservation(env).filled_quantity == 1
    state = env.executor.journal.order_state(record(env)['request']['request_id'])
    assert state['evidence']['cumulative_filled_quantity'] == 1
    assert len(state['evidence']['fills']) == 1


def test_cancel_ack_does_not_release_previous_and_no_repeat_delete_while_working(env):
    submit(env)
    newer = save_forecast(env)
    first = env.executor.step(newer)
    assert first['disposition'] == 'previous_intention_reconciliation'
    assert len(env.broker.posts) == 1 and len(env.broker.deletes) == 1
    assert reservation(env).status == 'WORKING'
    env.atlas.clock.tick()
    env.executor.step(newer)
    assert len(env.broker.posts) == 1 and len(env.broker.deletes) == 1


def test_partial_fill_during_cancel_reconciles_terminal_before_replacement(env):
    submit(env)
    newer = save_forecast(env)
    env.executor.step(newer)
    env.atlas.clock.tick()
    env.broker.orders = [raw_order(env, status='CANCELED', filled=1)]
    env.broker.location = 'https://api.schwabapi.com/trader/v1/accounts/synthetic-account/orders/124'
    value = env.executor.step(newer)
    assert value['disposition'] == 'submitted'
    previous = reservation(env)
    assert previous.status == 'CANCELLED' and previous.filled_quantity == 1
    assert len(env.broker.posts) == 2 and len(env.broker.deletes) == 1


def test_terminal_local_evidence_waits_for_shared_ack_before_new_reservation(env):
    submit(env)
    newer = save_forecast(env)
    env.atlas.clock.tick()
    env.broker.orders = [raw_order(env, status='FILLED', filled=2)]
    env.transport.drop = '/v1/evidence'
    with pytest.raises(TimeoutError):
        env.executor.step(newer)
    assert len(env.broker.posts) == 1 and len(env.atlas.authority.read_state()['reservations']) == 1
    assert env.executor.journal.order_state(record(env)['request']['request_id'])['evidence']['status'] == 'FILLED'
    env.atlas.clock.tick(seconds=65)
    env.broker.location = 'https://api.schwabapi.com/trader/v1/accounts/synthetic-account/orders/124'
    # Exact old evidence is replayed with the same event, even after its freshness window.
    assert env.executor.step(newer)['disposition'] == 'submitted'
    assert len(env.broker.posts) == 2


def test_unknown_previous_order_blocks_replacement_across_original_session(env):
    env.transport.drop = '/v1/authorize-send'
    env.executor.step(env.forecast)
    old = env.executor
    old.journal.close()
    old.journal = None
    next_session = old.session_id + '-reload'
    executor = execution.ScoutAccountExecutor(repository=old.repository, profile={}, datastore=old.root,
        store=old.store, broker=old.broker, local_symbols=old.symbols,
        session_started_at=env.atlas.clock(), session_id=next_session, clock=env.atlas.clock,
        manual_authority=old.manual_authority, stopped=old.stopped, authority=env.client, books=env.books)
    env.executor = executor
    executor.preflight()
    newer = save_forecast(env)
    value = executor.step(newer)
    assert value['disposition'] == 'previous_intention_reconciliation'
    assert len(env.books.sessions()) == 2 and env.broker.posts == []
    assert len(env.atlas.authority.read_state()['reservations']) == 1


@pytest.mark.parametrize('field,value', [('ready',False), ('authenticated',False),
    ('protocol_compatible',False), ('participant_id','pc-original'), ('stream','atlas-15m'),
    ('account_fingerprint','b'*64)])
def test_incompatible_readiness_fails_before_account_capture_or_reserve(env, field, value):
    env.ready[field] = value
    with pytest.raises(ContractError):
        env.executor.step(env.forecast)
    assert env.capture_count == 0 and env.atlas.captures == 0 and env.broker.posts == []


def test_account_change_fails_before_reservation(env):
    env.broker.account = 'b'*64
    with pytest.raises(ContractError):
        env.executor.step(env.forecast)
    assert env.atlas.captures == 0 and env.broker.posts == []


def test_manual_stop_during_authorization_retains_claim_without_post(env):
    def stop(path, result):
        if path == '/v1/authorize-send':
            env.state.stop = True
        return result
    env.transport.mutate = stop
    assert env.executor.step(env.forecast)['disposition'] == 'submission_recovery_required'
    assert record(env)['state'] == 'UNKNOWN' and reservation(env).status == 'SUBMITTING'
    assert env.broker.posts == []


def test_stop_arriving_during_manual_authority_read_is_observed(env):
    def manual():
        env.state.stop = True
        return True
    env.executor.manual_authority = manual
    with pytest.raises(ContractError):
        env.executor._controls()


def test_stale_quote_prevents_local_claim_or_post_even_after_authority_admission(env):
    env.quote_age = 61
    value = env.executor.step(env.forecast)
    assert value['disposition'] == 'not_submitted'
    assert value['evidence']['proven_unsent_retry_eligible'] is True
    assert record(env)['state'] == 'GRANTED' and env.broker.posts == []
    env.executor.recover()
    assert record(env)['state'] == 'GRANTED' and env.broker.posts == []
    env.quote_age = 0
    assert env.executor.step(env.forecast)['disposition'] == 'submitted'
    assert len(env.broker.posts) == 1


def test_crash_after_local_claim_before_remote_authorize_releases_only_with_shared_proof(env, monkeypatch):
    original = env.client.authorize_send
    monkeypatch.setattr(env.client, 'authorize_send', lambda _: (_ for _ in ()).throw(SystemExit('crash before remote call')))
    with pytest.raises(SystemExit):
        env.executor.step(env.forecast)
    assert record(env)['state'] == 'SENDING' and reservation(env).status == 'RESERVED'
    monkeypatch.setattr(env.client, 'authorize_send', original)
    recovered, = env.executor.recover()
    assert record(env)['state'] == 'UNKNOWN' and reservation(env).status == 'NOT_SUBMITTED'
    assert recovered['disposition'] == 'not_submitted'
    assert recovered['submitted_quantity'] == 0 and recovered['submission_status'] == 'REJECTED'
    env.executor.recover()
    assert env.broker.posts == []
    newer = save_forecast(env)
    assert env.executor.step(newer)['disposition'] == 'submitted'
    assert len(env.broker.posts) == 1


def test_manual_stop_during_cancel_intent_io_prevents_delete_and_retains_order(env):
    submit(env)
    newer = save_forecast(env)
    def stop(path, result):
        if path == '/v1/cancel-intent':
            env.state.stop = True
        return result
    env.transport.mutate = stop
    value = env.executor.step(newer)
    assert value['disposition'] == 'previous_intention_reconciliation'
    assert env.broker.deletes == [] and len(env.broker.posts) == 1
    assert reservation(env).status == 'WORKING'


def test_changed_accepted_identity_cannot_reconcile_or_enable_replacement(env):
    submit(env)
    newer = save_forecast(env)
    def wrong_order(path, result):
        if path == '/v1/status':
            result['broker_order_id'] = '987'
        return result
    env.transport.mutate = wrong_order
    with pytest.raises(ValueError):
        env.executor.step(newer)
    assert record(env)['broker_order_id'] == '123'
    assert len(env.broker.posts) == 1 and env.broker.deletes == []


@pytest.mark.parametrize('fence', ['expired', 'manual', 'readiness'])
def test_unclaimed_retry_checks_current_gates_and_never_posts_using_old_permission(env, fence):
    env.quote_age = 61
    env.executor.step(env.forecast)
    assert record(env)['state'] == 'GRANTED'
    env.quote_age = 0
    if fence == 'expired':
        env.atlas.clock.value = instant(env.forecast['target_close'])
        env.executor.step(env.forecast)
    elif fence == 'manual':
        env.state.manual = False
        with pytest.raises(ContractError):
            env.executor.step(env.forecast)
    else:
        env.ready['ready'] = False
        with pytest.raises(ContractError):
            env.executor.step(env.forecast)
    assert env.broker.posts == []


def test_unclaimed_retry_with_changed_quote_releases_original_without_mutated_request(env):
    env.quote_age = 61
    env.executor.step(env.forecast)
    original = record(env)['request']
    env.quote_age, env.quote_ask = 0, 100.01
    value = env.executor.step(env.forecast)
    assert value['disposition'] == 'unclaimed_quote_changed'
    assert record(env)['request'] == original and record(env)['state'] == 'NOT_SENT'
    assert reservation(env).status == 'NOT_SUBMITTED' and env.broker.posts == []
    assert len(env.atlas.authority.read_state()['reservations']) == 1


@pytest.mark.parametrize('damage', ['mutate', 'delete'])
def test_frozen_sizing_changed_during_remote_authorize_cannot_reach_post(env, damage):
    def damage_sizing(path, result):
        if path == '/v1/authorize-send':
            with sqlite3.connect(env.store.path) as db:
                if damage == 'mutate':
                    db.execute("UPDATE intraday_intentions SET payload='{}' WHERE forecast_id=?",
                        (env.forecast['forecast_id'],))
                else:
                    db.execute('DELETE FROM intraday_intentions WHERE forecast_id=?',
                        (env.forecast['forecast_id'],))
        return result
    env.transport.mutate = damage_sizing
    # The projection also fails closed if its original sizing has disappeared.
    with pytest.raises(ValueError):
        env.executor.step(env.forecast)
    assert record(env)['state'] == 'UNKNOWN' and env.broker.posts == []
    assert reservation(env).status == 'SUBMITTING'


def test_final_sizing_check_fails_immediately_on_database_lock(env):
    original = env.executor.intents.get(env.forecast['forecast_id'])
    with sqlite3.connect(env.store.path) as locker:
        # WAL permits concurrent readers; use a genuinely blocked fixture read.
        assert locker.execute('PRAGMA journal_mode=DELETE').fetchone()[0] == 'delete'
        locker.execute('BEGIN EXCLUSIVE')
        try:
            with pytest.raises((sqlite3.OperationalError, ContractError)):
                env.executor._sizing_current(env.forecast['forecast_id'], original)
        finally:
            locker.rollback()


def test_newer_forecast_arriving_during_authorize_prevents_post(env):
    def supersede(path, result):
        if path == '/v1/authorize-send':
            save_forecast(env)
        return result
    env.transport.mutate = supersede
    assert env.executor.step(env.forecast)['disposition'] == 'submission_recovery_required'
    assert record(env)['state'] == 'UNKNOWN' and env.broker.posts == []
    assert reservation(env).status == 'SUBMITTING'


def test_cancel_status_becoming_stale_during_authority_io_cannot_reach_delete(env):
    submit(env)
    newer = save_forecast(env)
    def delay(path, result):
        if path == '/v1/cancel-intent':
            env.atlas.clock.tick(seconds=65)
        return result
    env.transport.mutate = delay
    value = env.executor.step(newer)
    assert value['disposition'] == 'previous_intention_reconciliation'
    assert value['evidence']['complete_terminal_evidence'] is False
    assert env.broker.deletes == [] and len(env.broker.posts) == 1
    assert reservation(env).status == 'WORKING'


def test_cancel_age_is_rechecked_after_last_manual_control_read(env):
    submit(env)
    newer = save_forecast(env)
    state = dict(armed=False, manual_calls=0)
    def arm(path, result):
        if path == '/v1/cancel-intent':
            state['armed'] = True
        return result
    def manual():
        if state['armed']:
            state['manual_calls'] += 1
            # First gate after IO, second gate before controls, then the exact
            # final manual-status read inside broker.before_delete.
            if state['manual_calls'] == 3:
                env.atlas.clock.tick(seconds=65)
        return True
    env.transport.mutate, env.executor.manual_authority = arm, manual
    env.executor.step(newer)
    assert state['manual_calls'] == 3
    assert env.broker.deletes == [] and reservation(env).status == 'WORKING'


def test_proved_gate_stop_before_delete_can_resume_same_cancel_after_fresh_status(env):
    submit(env)
    newer = save_forecast(env)
    def stop(path, result):
        if path == '/v1/cancel-intent':
            env.state.stop = True
        return result
    env.transport.mutate = stop
    env.executor.step(newer)
    assert env.broker.deletes == []
    state = env.executor.journal.order_state(record(env)['request']['request_id'])
    assert state['cancellation_events'][-1]['payload']['known_before_delete'] is True
    env.state.stop = False
    env.transport.mutate = lambda path, result: result
    env.atlas.clock.tick(seconds=3)
    env.executor.step(newer)
    assert len(env.broker.deletes) == 1 and reservation(env).status == 'WORKING'
    env.atlas.clock.tick(seconds=3)
    env.executor.step(newer)
    assert len(env.broker.deletes) == 1


def test_stop_inside_broker_final_delete_gate_remains_proven_before_effect_on_resume(env):
    submit(env)
    newer = save_forecast(env)
    def stop_at_transport():
        env.state.stop = True
    env.broker.before_transport = stop_at_transport
    env.executor.step(newer)
    assert env.broker.deletes == []
    env.state.stop = False
    env.broker.before_transport = lambda: None
    env.atlas.clock.tick(seconds=3)
    env.executor.step(newer)
    assert len(env.broker.deletes) == 1 and reservation(env).status == 'WORKING'


def test_status_only_failure_preserves_earlier_proof_that_no_delete_was_sent(env):
    submit(env)
    newer = save_forecast(env)
    def stop(path, result):
        if path == '/v1/cancel-intent':
            env.state.stop = True
        return result
    env.transport.mutate = stop
    env.executor.step(newer)
    env.state.stop = False
    env.transport.mutate = lambda path, result: result
    reads = []
    def fail_second_read():
        reads.append(1)
        if len(reads) == 2:
            raise RuntimeError('synthetic status-only malformed evidence')
    env.broker.after_read = fail_second_read
    env.atlas.clock.tick(seconds=3)
    env.executor.step(newer)
    assert len(reads) == 2 and env.broker.deletes == []
    env.broker.after_read = lambda: None
    env.atlas.clock.tick(seconds=3)
    env.executor.step(newer)
    assert len(env.broker.deletes) == 1 and reservation(env).status == 'WORKING'


def test_abrupt_process_exit_inside_delete_gate_persists_proved_unsent_attempt(env):
    submit(env)
    newer = save_forecast(env)
    def crash_at_marked_gate():
        if env.executor.journal.order_state(record(env)['request']['request_id'])['cancellation'] is not None:
            raise SystemExit('synthetic process exit strictly before DELETE')
        return True
    env.executor.manual_authority = crash_at_marked_gate
    with pytest.raises(SystemExit):
        env.executor.step(newer)
    assert env.broker.deletes == []
    state = env.executor.journal.order_state(record(env)['request']['request_id'])
    assert state['cancellation_events'][-1]['payload']['known_before_delete'] is True
    env.executor.manual_authority = lambda: True
    env.atlas.clock.tick(seconds=3)
    env.executor.step(newer)
    assert len(env.broker.deletes) == 1


def test_abrupt_exit_after_actual_delete_never_gets_unsent_proof_or_second_delete(env):
    submit(env)
    newer = save_forecast(env)
    env.broker.delete_error = SystemExit('synthetic process exit after DELETE effect')
    with pytest.raises(SystemExit):
        env.executor.step(newer)
    assert len(env.broker.deletes) == 1
    state = env.executor.journal.order_state(record(env)['request']['request_id'])
    assert not any(item['payload'].get('known_before_delete') for item in state['cancellation_events'])
    env.broker.delete_error = None
    env.atlas.clock.tick(seconds=3)
    env.executor.step(newer)
    assert len(env.broker.deletes) == 1 and reservation(env).status == 'WORKING'


def test_retry_consumes_previous_unsent_proof_before_a_later_delete_crash(env):
    submit(env)
    newer = save_forecast(env)
    def stop(path, result):
        if path == '/v1/cancel-intent':
            env.state.stop = True
        return result
    env.transport.mutate = stop
    env.executor.step(newer)
    assert env.broker.deletes == []
    env.state.stop = False
    env.transport.mutate = lambda path, result: result
    env.broker.delete_error = SystemExit('crash after actual retried DELETE')
    env.atlas.clock.tick(seconds=3)
    with pytest.raises(SystemExit):
        env.executor.step(newer)
    assert len(env.broker.deletes) == 1
    env.broker.delete_error = None
    env.atlas.clock.tick(seconds=3)
    env.executor.step(newer)
    assert len(env.broker.deletes) == 1 and reservation(env).status == 'WORKING'


@pytest.mark.parametrize('proof', [dict(ready=False, mode='verified_gameplan_absent'),
    dict(ready=True, mode='loaded_compatible'), dict(ready=1, mode='verified_gameplan_absent')])
def test_local_writer_absence_must_be_explicit_before_any_authority_or_broker_operation(env, monkeypatch, proof):
    monkeypatch.setattr(execution, 'inspect_local_account_writers', lambda *a, **kw: proof)
    calls = len(env.transport.calls)
    with pytest.raises(ContractError):
        env.executor.step(env.forecast)
    assert len(env.transport.calls) == calls and env.broker.posts == [] and env.capture_count == 0


def test_existing_hourly_lock_is_held_from_local_absence_proof_through_post(env, monkeypatch):
    lock = env.executor.root / 'locks/stock-trader-hourly.lock'
    checks = []
    def held():
        with pytest.raises(RuntimeError):
            with exclusive_runtime_lock(lock, process_name='stock-trader-hourly'):
                pytest.fail('Concurrent local account writer acquired the guarded lock')
    def absence(*args, **kwargs):
        held()
        checks.append('absence-under-lock')
        return dict(ready=True, mode='verified_gameplan_absent')
    monkeypatch.setattr(execution, 'inspect_local_account_writers', absence)
    def before_post():
        held()
        checks.append('post-under-lock')
    env.broker.before_transport = before_post
    submit(env)
    assert 'absence-under-lock' in checks and checks[-1] == 'post-under-lock'
    with exclusive_runtime_lock(lock, process_name='stock-trader-hourly'):
        pass


def test_an_existing_local_cycle_lock_blocks_scout_without_authority_or_broker_effect(env):
    calls = len(env.transport.calls)
    with exclusive_runtime_lock(env.executor.root / 'locks/stock-trader-hourly.lock', process_name='stock-trader-hourly'):
        with pytest.raises(RuntimeError):
            env.executor.step(env.forecast)
    assert len(env.transport.calls) == calls and env.broker.posts == []


def test_starting_preflight_allows_registration_before_running_manual_authority(env):
    old = env.executor
    old.journal.close()
    old.journal = None
    env.state.manual = False
    env.executor = execution.ScoutAccountExecutor(repository=old.repository, profile={}, datastore=old.root,
        store=old.store, broker=old.broker, local_symbols=old.symbols,
        session_started_at=env.atlas.clock(), session_id=old.session_id+'-starting', clock=env.atlas.clock,
        manual_authority=old.manual_authority, stopped=old.stopped, authority=env.client, books=env.books)
    proof = env.executor.preflight()
    assert proof['ready'] is True and env.executor.journal is not None
    assert env.executor.session_id in env.books.sessions()
    with pytest.raises(ContractError):
        env.executor.step(env.forecast)
    with pytest.raises(ContractError):
        env.executor.recover()
    assert env.broker.posts == [] and env.broker.deletes == []
    assert env.atlas.authority.read_state()['reservations'] == []


def test_preflight_never_ignores_a_stop_even_before_manual_running_authority(env):
    env.state.manual, env.state.stop = False, True
    with pytest.raises(ContractError):
        env.executor.preflight()
    assert env.broker.posts == [] and env.broker.deletes == []


def test_lost_reserve_then_expiry_remains_resolved_after_process_cache_is_lost(env):
    env.transport.drop = '/v1/reserve'
    with pytest.raises(TimeoutError):
        env.executor.step(env.forecast)
    env.atlas.clock.value = instant(env.forecast['target_close'])
    env.executor.recover()
    assert record(env)['state'] == 'NOT_SENT' and record(env)['grant'] is None
    env.executor._settled.clear()
    value, = env.executor.recover()
    assert value['submitted_quantity'] == 0 and env.broker.posts == []
    assert env.atlas.authority.read_state()['reservations'][0]['status'] == 'NOT_SUBMITTED'


def test_unknown_delete_outcome_is_not_labelled_proven_before_effect(env):
    submit(env)
    newer = save_forecast(env)
    env.broker.delete_error = RuntimeError('synthetic unknown response after DELETE effect')
    env.executor.step(newer)
    assert len(env.broker.deletes) == 1
    state = env.executor.journal.order_state(record(env)['request']['request_id'])
    assert not state['cancellation_events'][-1]['payload'].get('known_before_delete', False)
    env.broker.delete_error = None
    env.atlas.clock.tick(seconds=3)
    env.executor.step(newer)
    assert len(env.broker.deletes) == 1 and reservation(env).status == 'WORKING'


def test_acknowledged_terminal_recovery_is_cached_only_in_current_executor_process(env):
    submit(env)
    env.atlas.clock.tick()
    env.broker.orders = [raw_order(env, status='FILLED', filled=2)]
    env.executor.recover()
    calls = len(env.transport.calls)
    history = len(env.executor.intents.history(env.forecast['forecast_id']))
    reads = len(env.broker.reads)
    env.atlas.clock.tick(seconds=20)
    env.executor.recover()
    env.executor.recover()
    assert len(env.transport.calls) == calls and len(env.broker.reads) == reads
    assert len(env.executor.intents.history(env.forecast['forecast_id'])) == history
    old = env.executor
    old.journal.close()
    old.journal = None
    env.executor = execution.ScoutAccountExecutor(repository=old.repository, profile={}, datastore=old.root,
        store=old.store, broker=old.broker, local_symbols=old.symbols,
        session_started_at=env.atlas.clock(), session_id=old.session_id+'-reload', clock=env.atlas.clock,
        manual_authority=old.manual_authority, stopped=old.stopped, authority=env.client, books=env.books)
    env.executor.preflight()
    env.executor.recover()
    # A new process must authenticate the original terminal state once again.
    assert len(env.transport.calls) > calls and len(env.broker.reads) == reads
    rechecked = len(env.transport.calls)
    env.executor.recover()
    assert len(env.transport.calls) == rechecked


def test_fixed_target_expiry_and_strict_pacific_cutoff_never_request_capacity(env):
    env.atlas.clock.value = instant(env.forecast['target_close'])
    assert env.executor.step(env.forecast)['disposition'] == 'fixed_target_already_elapsed'
    env.atlas.clock.value = instant('2026-10-12T23:45:00Z')
    assert env.executor.step(env.forecast)['disposition'] == 'submission_cutoff'
    assert env.atlas.captures == 0 and env.broker.posts == []


def lose_reserve_before_atlas(env, monkeypatch):
    """Lose the first outbound request, without invoking the real service."""
    call = env.transport.call
    attempts = []
    def not_delivered(method, path, body=None):
        if path == '/v1/reserve':
            attempts.append(body)
            raise TimeoutError('synthetic reserve never reached Atlas')
        return call(method, path, body)
    monkeypatch.setattr(env.transport, 'call', not_delivered)
    with pytest.raises(TimeoutError, match='never reached Atlas'):
        env.executor.step(env.forecast)
    monkeypatch.setattr(env.transport, 'call', call)
    assert attempts == [record(env)['request']]
    assert record(env)['state'] == 'PREPARED' and record(env)['grant'] is None
    assert not env.atlas.service.path.exists()
    assert env.broker.posts == [] and env.broker.deletes == []
    return record(env)


def reload_scout(env, monkeypatch):
    """Discard all process caches; retain only original private/shared files."""
    old = env.executor
    old.journal.close()
    old.journal = None
    env.atlas.service = env.atlas.make_service()
    env.transport = Transport(env.atlas.service)
    env.books = ScoutBooks(env.books.root, account_fingerprint=ACCOUNT)
    env.client = ScoutAuthorityClient(env.client.binding, env.books, transport=env.transport, clock=env.atlas.clock)
    monkeypatch.setattr(env.client, 'readiness', lambda: dict(env.ready))
    env.executor = execution.ScoutAccountExecutor(repository=old.repository, profile={}, datastore=old.root,
        store=old.store, broker=old.broker, local_symbols=old.symbols,
        session_started_at=env.atlas.clock(), session_id=old.session_id+'-reload', clock=env.atlas.clock,
        manual_authority=old.manual_authority, stopped=old.stopped, authority=env.client, books=env.books)
    env.executor.preflight()
    return old.session_id


def test_reserve_never_reached_atlas_then_expiry_requires_exact_negative_proof_without_capacity(env, monkeypatch):
    original = lose_reserve_before_atlas(env, monkeypatch)
    env.atlas.clock.value = instant(env.forecast['target_close'])
    recovered, = env.executor.recover()
    assert recovered['disposition'] == 'not_submitted' and recovered['submitted_quantity'] == 0
    assert record(env)['state'] == 'NOT_SENT' and record(env)['grant'] is None
    request, plan, decision = env.atlas.service._stored(original['request']['request_id'])
    assert request.payload() == original['request'] and plan is None
    assert decision['reason'] == 'expired_before_admission'
    assert decision['terminal'] is True and decision['admitted'] is False
    assert decision['capacity_released'] is False and decision['send_authorized'] is False
    assert env.atlas.captures == 0 and env.atlas.authority.read_state()['reservations'] == []
    assert env.atlas.ledger.snapshot().reservations == ()
    assert env.broker.posts == [] and env.broker.deletes == []
    saved = [json.loads(path.read_text()) for path in (env.books.root/'receipts').glob('*.json')]
    assert any(value['payload']['response'] == decision for value in saved)


@pytest.mark.parametrize('publish_snapshot', [False, True])
def test_capture_failure_after_prepared_without_plan_expires_without_new_capture_or_send(env, monkeypatch, publish_snapshot):
    refresh = env.atlas.service.refresh
    def fail_capture(operation):
        if publish_snapshot:
            refresh(operation)
        raise TimeoutError('synthetic Atlas capture failed before capacity plan')
    monkeypatch.setattr(env.atlas.service, 'refresh', fail_capture)
    with pytest.raises(TimeoutError, match='before capacity plan'):
        env.executor.step(env.forecast)
    original = record(env)
    assert original['state'] == 'PREPARED' and original['grant'] is None
    assert env.atlas.service._stored(original['request']['request_id'])[1:] == (None, None)
    env.atlas.clock.value = instant(env.forecast['target_close'])
    # Keep the capture broken: expiry proof must use durable absence evidence,
    # never fetch a new portfolio or fabricate a capacity decision.
    recovered, = env.executor.recover()
    assert recovered['disposition'] == 'not_submitted' and record(env)['state'] == 'NOT_SENT'
    assert env.atlas.service._stored(original['request']['request_id'])[2]['reason'] == 'expired_before_admission'
    assert env.atlas.captures == int(publish_snapshot)
    assert env.atlas.authority.read_state()['reservations'] == [] and env.atlas.ledger.snapshot().reservations == ()
    assert env.broker.posts == [] and env.broker.deletes == []


@pytest.mark.parametrize('failure', ['reply', 'before_receipt_save', 'after_receipt_save'])
def test_negative_proof_loss_requires_fresh_exact_recovery_after_manual_reload(env, monkeypatch, failure):
    original = lose_reserve_before_atlas(env, monkeypatch)
    env.atlas.clock.value = instant(env.forecast['target_close'])
    if failure == 'reply':
        env.transport.drop = '/v1/reserve'
    else:
        save = env.books.save_receipt
        def lost_save(**kwargs):
            assert kwargs['payload']['response']['reason'] == 'expired_before_admission'
            if failure == 'after_receipt_save':
                save(**kwargs)
            raise OSError('synthetic negative receipt save interruption')
        monkeypatch.setattr(env.books, 'save_receipt', lost_save)
    with pytest.raises((TimeoutError, OSError)):
        env.executor.recover()
    assert record(env)['state'] == 'PREPARED' and record(env)['grant'] is None
    decision = env.atlas.service._stored(original['request']['request_id'])[2]
    assert decision['reason'] == 'expired_before_admission' and decision['capacity_released'] is False
    old_session = reload_scout(env, monkeypatch)
    try:
        recovered, = env.executor.recover()
        assert recovered['disposition'] == 'not_submitted' and recovered['submitted_quantity'] == 0
        with env.books.open_session(old_session) as previous:
            restored = previous.get(original['request']['request_id'])
            assert restored['request'] == original['request'] and restored['state'] == 'NOT_SENT'
            assert restored['grant'] is None and restored['send_token'] is None
        assert env.executor.journal.records() == ()
        assert env.atlas.service._stored(original['request']['request_id'])[2] == decision
        assert env.atlas.authority.read_state()['reservations'] == [] and env.atlas.captures == 0
        assert env.broker.posts == [] and env.broker.deletes == []
    finally:
        env.executor.journal.close()
        env.executor.journal = None


@pytest.mark.parametrize('negative_reply_was_lost', [False, True])
def test_retained_orphan_claim_blocks_expired_negative_proof_and_new_symbol_replacement(env, monkeypatch, negative_reply_was_lost):
    original = lose_reserve_before_atlas(env, monkeypatch)
    env.atlas.clock.value = instant(env.forecast['target_close'])
    if negative_reply_was_lost:
        env.transport.drop = '/v1/reserve'
        with pytest.raises(TimeoutError):
            env.executor.recover()
        assert env.atlas.service._stored(original['request']['request_id'])[2]['reason'] == 'expired_before_admission'
    orphan = dict(reservation_id='orphan-original-account-row', request_sha256=original['request_sha256'],
        participant_id='pc-new', claim_id='possible-original-send')
    with sqlite3.connect(env.atlas.authority.path) as db:
        db.execute('INSERT INTO remote_submission_claims VALUES(?,?)', (orphan['reservation_id'], json.dumps(orphan)))
    old_session = reload_scout(env, monkeypatch)
    try:
        with pytest.raises(AdmissionError, match='PRIOR_ADMISSION_EVIDENCE'):
            env.executor.recover()
        newer = save_forecast(env)
        with pytest.raises(AdmissionError, match='PRIOR_ADMISSION_EVIDENCE'):
            env.executor.step(newer)
        with env.books.open_session(old_session) as previous:
            current = previous.get(original['request']['request_id'])
            assert current['state'] == 'PREPARED' and current['grant'] is None
        assert env.executor.journal.records() == ()
        assert all(body['request_id'] == original['request']['request_id']
            for _, path, body in env.transport.calls if path == '/v1/reserve')
        with sqlite3.connect(env.atlas.authority.path) as db:
            assert json.loads(db.execute('SELECT payload FROM remote_submission_claims WHERE reservation=?',
                (orphan['reservation_id'],)).fetchone()[0]) == orphan
        assert env.broker.posts == [] and env.broker.deletes == [] and env.atlas.captures == 0
    finally:
        env.executor.journal.close()
        env.executor.journal = None
