"""Completed initial local rejections preserve UNKNOWN without blocking new calls."""
from datetime import datetime, timedelta
import json

import pytest

from ml.intraday import initial_gate_clearance as clearance
from ml.intraday import scout_broker, scout_journal
from ml.intraday.prediction import ContractError, instant
from ml.intraday.scout_client import ScoutClient
from ml.intraday.scout_journal import JournalStateError
from ml.intraday.scout_protocol import canonical_sha256
from ml.stock_trader.session import stock_execution_window
from test_intraday_scout_local_execution import env, forecast, record


@pytest.fixture(autouse=True)
def journal_clock(env, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return env.state.now
    monkeypatch.setattr(scout_journal, 'datetime', Clock)


def initial_clock_boundary(env, monkeypatch, boundary):
    """Move time after the real SENDING read and before initial local validation."""
    if boundary == 'closed':
        env.reference = instant('2026-10-12T19:30:00Z')
        env.state.now = instant('2026-10-12T19:59:59Z')
        env.forecast = forecast(env)
    adapters, injected = [], []
    factory = env.executor._adapter
    def capture(*args, **kwargs):
        adapter = factory(*args, **kwargs)
        adapters.append(adapter)
        real_record = adapter._record
        def read(request, grant, state):
            result = real_record(request, grant, state)
            if state == 'SENDING' and not injected:
                injected.append(result)
                env.state.now = (env.state.now + timedelta(seconds=1) if boundary == 'closed'
                    else instant(request.valid_until))
            return result
        monkeypatch.setattr(adapter, '_record', read)
        return adapter
    monkeypatch.setattr(env.executor, '_adapter', capture)
    return adapters, injected


def loaded(env, original=None):
    return clearance.load_clearance(env.executor.books, env.executor.journal,
        original or record(env), as_of=env.state.now)


def next_forecast(env, boundary):
    previous = env.state.now
    env.forecast = forecast(env, advance=True)
    env.state.now = max(env.state.now, previous + timedelta(seconds=1))
    if boundary == 'closed':
        env.state.now = instant('2026-10-12T20:06:10Z')
    return env.forecast


@pytest.mark.parametrize('boundary,message', [
    ('closed', 'Stock execution session is closed'),
    ('expired', 'Request or grant is no longer current'),
])
def test_initial_local_rejection_has_exact_proof_without_original_replay(env, monkeypatch, boundary, message):
    adapters, injected = initial_clock_boundary(env, monkeypatch, boundary)
    result = env.executor.step(env.forecast)
    old, adapter = record(env), adapters[-1]
    assert len(injected) == 1 and old['state'] == 'UNKNOWN' and old['send_token']
    assert old['broker_order_id'] is None and env.broker.posts == [] and env.broker.contexts == []
    observed = adapter.initial_gate_failure
    assert type(observed) is scout_broker.InitialGateFailure
    assert str(observed.error) == message and adapter.native_before_post_failure is None
    assert observed.request_sha256 == old['request_sha256']
    assert observed.grant_sha256 == old['grant_sha256']
    assert observed.sending_record_sha256 == canonical_sha256(injected[0])
    assert result['evidence']['submission_failure']['stage'] == 'send'
    assert result['evidence']['pretransport_clearance'] == loaded(env)
    if boundary == 'closed':
        assert not stock_execution_window(env.state.now).executable
        assert instant(old['request']['valid_until']) > env.state.now
    else:
        assert stock_execution_window(env.state.now).executable
        assert instant(old['request']['valid_until']) <= env.state.now
    history = clearance._audit(env.executor.books, env.executor.journal, old)[1]
    env.executor.recover()
    env.executor.step(env.forecast)
    assert record(env) == old
    assert clearance._audit(env.executor.books, env.executor.journal, old)[1] == history
    client = ScoutClient(env.executor.journal, None,
        account_fingerprint=old['request']['account_fingerprint'], session_id=env.executor.session_id,
        symbols=frozenset({'DOCU'}), authority_epoch=1, clock=lambda: env.state.now)
    with pytest.raises(JournalStateError):
        client.submit(old['request']['request_id'], send=lambda *_: pytest.fail('No original replay'),
            final_gate=lambda *_: True)
    env.cash = 105
    next_forecast(env, boundary)
    current = env.executor.step(env.forecast)
    assert current['disposition'] == 'submitted' and current['adjusted_quantity'] == 1
    assert len(env.broker.posts) == 1
    assert env.executor.journal.get(old['request']['request_id']) == old
    assert loaded(env, old)['commitment'] == 'no_order_transport_entered'


@pytest.mark.parametrize('boundary', ['closed', 'expired'])
def test_failed_initial_proof_persistence_keeps_global_unknown_block(env, monkeypatch, boundary):
    initial_clock_boundary(env, monkeypatch, boundary)
    def failed(*args):
        raise OSError('offline proof write failure')
    monkeypatch.setattr(clearance, '_write_once', failed)
    result = env.executor.step(env.forecast)
    old = record(env)
    assert old['state'] == 'UNKNOWN' and loaded(env) is None
    assert 'clearance_persistence_failure' in result['evidence']
    next_forecast(env, boundary)
    assert env.executor.step(env.forecast)['disposition'] == 'submission_recovery_required'
    assert env.broker.posts == [] and env.executor.journal.get(old['request']['request_id']) == old


@pytest.mark.parametrize('failure', ['preparer', 'payload', 'transport'])
def test_opaque_preparer_payload_and_transport_do_not_gain_initial_proof(env, monkeypatch, failure):
    touched = []
    error = scout_broker.ScoutBrokerError('Stock execution session is closed')
    error.initial_gate_failure = True
    error.scout_no_order_transport_entered = True
    def fail(*args, **kwargs):
        touched.append(failure)
        raise error
    if failure == 'preparer':
        monkeypatch.setattr(env.broker, 'prepare_order_submission', fail)
    elif failure == 'payload':
        monkeypatch.setattr(scout_broker, 'build_schwab_stock_order_payload', fail)
    else:
        monkeypatch.setattr(env.broker, 'submit_prepared_order', fail)
    result = env.executor.step(env.forecast)
    old = record(env)
    assert touched == [failure] and old['state'] == 'UNKNOWN'
    assert loaded(env) is None and 'pretransport_clearance' not in result['evidence']
    env.forecast = forecast(env, advance=True)
    assert env.executor.step(env.forecast)['disposition'] == 'submission_recovery_required'
    assert touched == [failure] and env.executor.journal.get(old['request']['request_id']) == old


def test_valid_initial_window_keeps_existing_submission_path(env):
    result = env.executor.step(env.forecast)
    current = record(env)
    assert result['disposition'] == 'submitted' and current['state'] == 'ACCEPTED'
    assert len(env.broker.posts) == 1 and len(env.broker.contexts) == 1
    assert not list((env.executor.books.root/'initial-gate-clearances').glob('*.json'))


def test_initial_local_interruption_is_not_a_completed_rejection(env, monkeypatch):
    adapters, _ = initial_clock_boundary(env, monkeypatch, 'expired')
    def interrupt(*args):
        raise KeyboardInterrupt('offline interrupted local validation')
    monkeypatch.setattr(scout_broker.ScoutBrokerAdapter, '_current', interrupt)
    with pytest.raises(KeyboardInterrupt):
        env.executor.step(env.forecast)
    old = record(env)
    assert old['state'] == 'SENDING' and adapters[-1].initial_gate_failure is None
    assert env.broker.posts == [] and env.broker.contexts == []
    env.executor.recover()
    assert record(env)['state'] == 'UNKNOWN' and loaded(env) is None


def test_initial_proof_is_bound_to_original_record_after_clock_rejection(env, monkeypatch):
    initial_clock_boundary(env, monkeypatch, 'expired')
    env.executor.step(env.forecast)
    old = record(env)
    path = clearance._path(env.executor.books, old)
    envelope = json.loads(path.read_text())
    envelope['payload']['observation']['sending_record_sha256'] = 'f'*64
    envelope['sha256'] = canonical_sha256(envelope['payload'])
    path.write_text(json.dumps(envelope))
    with pytest.raises(ContractError):
        loaded(env)
    next_forecast(env, 'expired')
    with pytest.raises(ContractError):
        env.executor.step(env.forecast)
    assert env.broker.posts == [] and env.executor.journal.get(old['request']['request_id']) == old
