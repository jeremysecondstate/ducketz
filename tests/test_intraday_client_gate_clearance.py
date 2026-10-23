"""Trusted post-claim local gate proof without any network or old-order replay."""
from dataclasses import replace
from datetime import datetime, timedelta
import json
import sqlite3

import pytest

from ml.intraday import client_gate_clearance as clearance
from ml.intraday import scout_client, scout_journal
from ml.intraday import scout_local_execution as execution
from ml.intraday.prediction import ContractError
from ml.intraday.scout_journal import JournalStateError
from ml.intraday.scout_protocol import canonical_sha256
from test_intraday_scout_local_execution import env, forecast, record


@pytest.fixture(autouse=True)
def journal_clock(env, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return env.state.now
    monkeypatch.setattr(scout_journal, 'datetime', Clock)


_MISSING_CODE = object()


def failure(env, monkeypatch, *, at=3, error=None, automatic=True, code=261):
    clients, calls = [], []
    factory = execution.ScoutClient
    def client(*args, **kwargs):
        value = factory(*args, **kwargs)
        clients.append(value)
        return value
    monkeypatch.setattr(execution, 'ScoutClient', client)
    saved = execution._saved_forecast
    error = error or sqlite3.OperationalError('database is locked')
    if isinstance(error, sqlite3.Error):
        if code is _MISSING_CODE:
            if hasattr(error, 'sqlite_errorcode'):
                del error.sqlite_errorcode
        else:
            error.sqlite_errorcode = code
    def read(*args, **kwargs):
        calls.append(True)
        if len(calls) == at:
            raise error
        return saved(*args, **kwargs)
    monkeypatch.setattr(execution, '_saved_forecast', read)
    if not automatic:
        monkeypatch.setattr(execution, 'create_client_gate_clearance', lambda *a, **k: None)
    value = env.executor.step(env.forecast)
    return value, clients[-1], error, calls


def load(env):
    return clearance.load_clearance(env.executor.books, env.executor.journal,
                                   record(env), as_of=env.state.now)


def create(env, client, error, *, gate=None, send=None):
    witness = client.final_gate_failure
    return clearance.create_clearance(env.executor.books, env.executor.journal, record(env),
        executor=env.executor, client=client,
        gate=witness.final_gate if gate is None else gate,
        send=witness.send if send is None else send, error=error, as_of=env.state.now)


def test_exact_saved_read_failure_clears_only_distinct_forecasts_and_never_replays(env, monkeypatch):
    value, client, error, calls = failure(env, monkeypatch)
    original = record(env)
    assert len(calls) == 3 and env.broker.posts == [] and env.broker.reads == []
    assert original['state'] == 'UNKNOWN' and original['broker_order_id'] is None
    assert value['evidence']['submission_failure']['stage'] == 'final_gate'
    assert value['evidence']['pretransport_clearance'] == load(env)
    assert client.final_gate_failure.error is error
    before = clearance._audit(env.executor.books, env.executor.journal, original)[1]
    env.executor.recover()
    env.executor.step(env.forecast)
    assert record(env) == original
    assert clearance._audit(env.executor.books, env.executor.journal, original)[1] == before
    with pytest.raises(JournalStateError):
        client.submit(original['request']['request_id'], send=lambda *_: pytest.fail('No replay'),
                      final_gate=lambda *_: True)
    assert client.final_gate_failure is None
    env.forecast = forecast(env, advance=True)
    result = env.executor.step(env.forecast)
    assert result['disposition'] == 'submitted' and len(env.broker.posts) == 1
    assert env.executor.journal.get(original['request']['request_id']) == original


def test_first_gate_failure_stays_unclaimed_without_receipt(env, monkeypatch):
    value, client, _, _ = failure(env, monkeypatch, at=2)
    assert record(env)['state'] == 'GRANTED' and client.final_gate_failure is None
    assert value['evidence']['submission_failure']['stage'] == 'outside_claimed_submit'
    assert not clearance._path(env.executor.books, record(env)).exists()
    assert env.broker.posts == []


@pytest.mark.parametrize('damage', ['error', 'request', 'grant', 'sending', 'client', 'gate', 'send', 'adapter', 'method'])
def test_retained_observation_requires_exact_objects_and_callback_scope(env, monkeypatch, damage):
    _, client, error, _ = failure(env, monkeypatch, automatic=False)
    old, witness = record(env), client.final_gate_failure
    gate, send, changed_error = witness.final_gate, witness.send, error
    if damage == 'error':
        changed_error = sqlite3.OperationalError('database is locked')
        changed_error.sqlite_errorcode = 261
    elif damage == 'request': client.final_gate_failure = replace(witness, request_sha256='f' * 64)
    elif damage == 'grant': client.final_gate_failure = replace(witness, grant_sha256='f' * 64)
    elif damage == 'sending': client.final_gate_failure = replace(witness, sending_record_sha256='f' * 64)
    elif damage == 'client': client.final_gate_failure = replace(witness, client=object())
    elif damage == 'gate':
        gate = lambda *args: witness.final_gate(*args)
        client.final_gate_failure = replace(witness, final_gate=gate)
    elif damage == 'send':
        send = lambda *args: witness.send(*args)
        client.final_gate_failure = replace(witness, send=send)
    elif damage == 'adapter':
        cells = dict(zip(send.__code__.co_freevars, send.__closure__))
        cells['adapter'].cell_contents = object()
    else:
        monkeypatch.setattr(client, '_gate', lambda *args: None)
    with pytest.raises(ContractError):
        create(env, client, changed_error, gate=gate, send=send)
    assert not clearance._path(env.executor.books, old).exists()
    assert record(env) == old and env.broker.posts == []


def test_opaque_submit_wrapper_cannot_mint_proof(env, monkeypatch):
    original_submit = scout_client.ScoutClient.submit
    def submit(self, request_id, *, send, final_gate):
        def opaque(*args):
            return final_gate(*args)
        return original_submit(self, request_id, send=send, final_gate=opaque)
    monkeypatch.setattr(scout_client.ScoutClient, 'submit', submit)
    value, _, _, _ = failure(env, monkeypatch)
    assert record(env)['state'] == 'UNKNOWN' and load(env) is None
    assert 'pretransport_clearance' not in value['evidence'] and env.broker.posts == []
    env.forecast = forecast(env, advance=True)
    assert env.executor.step(env.forecast)['disposition'] == 'submission_recovery_required'


@pytest.mark.parametrize('damage', ['hash', 'version', 'stage', 'record', 'events', 'request', 'grant', 'sending', 'future', 'before_unknown', 'diagnostics'])
def test_saved_proof_rejects_corruption_and_does_not_fall_back(env, monkeypatch, damage):
    failure(env, monkeypatch)
    old = record(env)
    path = clearance._path(env.executor.books, old)
    envelope = json.loads(path.read_text())
    payload = envelope['payload']
    if damage == 'hash': envelope['sha256'] = 'f' * 64
    elif damage == 'version': payload['version'] = 'historical-client-gate'
    elif damage == 'stage': payload['observation']['stage'] = 'send'
    elif damage == 'record': payload['original_record_sha256'] = 'f' * 64
    elif damage == 'events': payload['journal_events_sha256'] = 'f' * 64
    elif damage == 'request': payload['observation']['request_sha256'] = 'f' * 64
    elif damage == 'grant': payload['observation']['grant_sha256'] = 'f' * 64
    elif damage == 'sending': payload['observation']['sending_record_sha256'] = 'f' * 64
    elif damage == 'future': payload['created_at'] = (env.state.now + timedelta(days=1)).isoformat()
    elif damage == 'before_unknown': payload['created_at'] = (env.state.now - timedelta(seconds=1)).isoformat()
    else: payload['observation']['failure'] = {'chain': [], 'chain_truncated': False}
    if damage != 'hash': envelope['sha256'] = canonical_sha256(payload)
    path.write_text(json.dumps(envelope))
    with pytest.raises(ContractError): load(env)
    env.forecast = forecast(env, advance=True)
    with pytest.raises(ContractError): env.executor.step(env.forecast)
    assert env.executor.journal.get(old['request']['request_id']) == old and env.broker.posts == []


def test_failed_receipt_write_preserves_original_error_and_block(env, monkeypatch):
    monkeypatch.setattr(clearance, '_write_once', lambda *_: (_ for _ in ()).throw(OSError('offline proof failure')))
    value, _, _, _ = failure(env, monkeypatch)
    old = record(env)
    assert old['state'] == 'UNKNOWN' and load(env) is None and env.broker.posts == []
    assert value['evidence']['submission_failure']['original']['chain'][0]['type'] == 'OperationalError'
    assert value['evidence']['clearance_persistence_failure']['chain'][0]['type'] == 'OSError'
    env.forecast = forecast(env, advance=True)
    assert env.executor.step(env.forecast)['disposition'] == 'submission_recovery_required'
    assert env.executor.journal.get(old['request']['request_id']) == old


def test_baseexception_retains_sending_fence_without_witness(env, monkeypatch):
    with pytest.raises(KeyboardInterrupt):
        failure(env, monkeypatch, error=KeyboardInterrupt())
    assert record(env)['state'] == 'SENDING' and env.broker.posts == []
    assert not clearance._path(env.executor.books, record(env)).exists()


@pytest.mark.parametrize('error', [ContractError('local refusal'), TimeoutError('opaque'),
                                   sqlite3.OperationalError('not contention')])
def test_noncontention_client_gate_errors_keep_original_unknown_block(env, monkeypatch, error):
    value, _, _, _ = failure(env, monkeypatch, error=error, code=1)
    assert record(env)['state'] == 'UNKNOWN' and load(env) is None
    assert 'pretransport_clearance' not in value['evidence'] and env.broker.posts == []


def test_sender_timeout_is_opaque_without_client_witness_or_clearance(env, monkeypatch):
    env.broker.post_error = TimeoutError('offline uncertain submission')
    value, client, _, _ = failure(env, monkeypatch, at=99)
    old = record(env)
    assert old['state'] == 'UNKNOWN' and len(env.broker.posts) == 1
    assert value['evidence']['submission_failure']['stage'] == 'send'
    assert client.final_gate_failure is None and load(env) is None
    env.forecast = forecast(env, advance=True)
    assert env.executor.step(env.forecast)['disposition'] == 'submission_recovery_required'
    assert len(env.broker.posts) == 1 and env.executor.journal.get(old['request']['request_id']) == old


def test_unknown_without_retained_live_witness_cannot_gain_retrospective_receipt(env, monkeypatch):
    _, client, error, _ = failure(env, monkeypatch, automatic=False)
    old, witness = record(env), client.final_gate_failure
    client.final_gate_failure = None
    with pytest.raises(ContractError):
        create(env, client, error, gate=witness.final_gate, send=witness.send)
    assert load(env) is None and record(env) == old and env.broker.posts == []


@pytest.mark.parametrize('code', [5, 6, 261, 262, 517])
def test_primary_and_extended_native_contention_keep_trusted_boundary(env, monkeypatch, code):
    value, _, _, _ = failure(env, monkeypatch, code=code)
    assert value['evidence']['pretransport_clearance'] == load(env)
    assert record(env)['state'] == 'UNKNOWN' and env.broker.posts == []


@pytest.mark.parametrize('code', [_MISSING_CODE, None, True, False, -1, 10, 11, '261'])
def test_absent_invalid_and_noncontention_codes_cannot_qualify(env, monkeypatch, code):
    value, client, error, _ = failure(env, monkeypatch, code=code)
    witness = client.final_gate_failure
    assert record(env)['state'] == 'UNKNOWN' and load(env) is None
    assert 'pretransport_clearance' not in value['evidence']
    with pytest.raises(ContractError):
        create(env, client, error, gate=witness.final_gate, send=witness.send)
    assert env.broker.posts == []


@pytest.mark.parametrize('kind', ['runtime_name', 'sqlite_base', 'sqlite_subclass'])
def test_native_code_or_exception_name_alone_is_not_proof(env, monkeypatch, kind):
    if kind == 'runtime_name':
        error = type('OperationalError', (RuntimeError,), {})('database is locked')
    elif kind == 'sqlite_base':
        error = sqlite3.DatabaseError('database is locked')
    else:
        error = type('OperationalError', (sqlite3.OperationalError,), {})('database is locked')
    error.sqlite_errorcode = 261
    value, client, _, _ = failure(env, monkeypatch, error=error)
    witness = client.final_gate_failure
    assert load(env) is None and 'pretransport_clearance' not in value['evidence']
    with pytest.raises(ContractError):
        create(env, client, error, gate=witness.final_gate, send=witness.send)
    assert record(env)['state'] == 'UNKNOWN' and env.broker.posts == []
