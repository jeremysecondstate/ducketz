"""Exact native prepared POST guards using synthetic journals and HTTP only."""
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace, MethodType
import socket
import json
import sqlite3
import pytest
from ml.intraday import scout_broker as sb
from ml.intraday.scout_client import ScoutClient
from ml.intraday.scout_journal import JournalStateError
from ml.intraday.scout_session_books import ScoutBooks
from ml.intraday import scout_journal as sj, initial_gate_clearance as clear
from ml.intraday.scout_protocol import Request, Grant
from ml.intraday import scout_local_execution as local_execution
from app.services import schwab
from test_intraday_scout_local_execution import env as local_env, forecast as local_forecast, record as local_record
from test_intraday_initial_gate_clearance import native_transport
CAND_SEND_FUNCTION = sb.ScoutBrokerAdapter.send
namespace = vars(sb)
ACCOUNT = 'a' * 64
GEN = 'offline-snapshot-generation'
NOW = datetime.fromisoformat('2026-10-12T17:15:05+00:00')

def req_grant():
    req = Request(request_id='scout-local:offline', account_fingerprint=ACCOUNT, session_id='manual-scout:offline', symbol='DOCU', side='BUY', quantity=10, limit_price='42.5', forecast_id='b' * 64, forecast_sha256='b' * 64, snapshot_sha256='c' * 64, sizing_sha256='d' * 64, reference_at='2026-10-12T17:00:00Z', issued_at=NOW.isoformat(), target_close_at=(NOW + timedelta(minutes=15)).isoformat(), valid_until=(NOW + timedelta(minutes=15)).isoformat())
    grant = Grant(grant_id='local-permit:offline', request_id=req.request_id, request_sha256=req.sha256, account_fingerprint=ACCOUNT, session_id=req.session_id, authority_epoch=1, quantity=3, reservation_id='local-order:offline', issued_at=req.issued_at, expires_at=req.valid_until)
    return (req, grant)

def build(tmp_path, monkeypatch, *, native=True, broker_type=schwab.SchwabSession):
    books = ScoutBooks(tmp_path / 'books', account_fingerprint=ACCOUNT)
    journal = books.create_session('manual-scout:offline')
    state = SimpleNamespace(now=NOW, manual=True, generation=GEN, account=ACCOUNT, posts=[], auth_calls=0, final_calls=0, verify_calls=0, stable_calls=0, client_calls=0, delay=0.0, prep_hook=lambda: None, auth_hook=lambda: None, final_hook=lambda: None, post_error=None, location='https://api.schwabapi.com/trader/v1/accounts/synthetic-account/orders/123')

    class Clock(datetime):

        @classmethod
        def now(cls, tz=None):
            return state.now if tz is not None else state.now.replace(tzinfo=None)
    monkeypatch.setattr(sj, 'datetime', Clock)
    broker = object.__new__(broker_type) if native else SimpleNamespace()

    def stable():
        state.stable_calls += 1
        return state.account

    def verify(expected):
        state.verify_calls += 1
        if expected != state.generation:
            raise sb.ScoutBrokerError('offline account generation changed')

    def prepare():
        state.prep_hook()
        return schwab.SchwabOrderSubmissionContext(account_hash='synthetic-account', access_token='offline-placeholder', identity_fingerprint=GEN)
    broker.stable_account_fingerprint = stable
    broker.verify_read_snapshot = verify
    broker.prepare_order_submission = prepare

    def post(*args, **kwargs):
        state.posts.append(kwargs['json'])
        if state.post_error:
            raise state.post_error
        return SimpleNamespace(raise_for_status=lambda: None, headers={'Location': state.location})
    monkeypatch.setattr(schwab.requests, 'post', post)
    if not native:

        def opaque(payload, context, *, before_post):
            before_post()
            state.posts.append(payload)
            return state.location
        broker.submit_prepared_order = opaque
    monkeypatch.setattr(sb.ScoutBrokerAdapter, 'send', CAND_SEND_FUNCTION)
    adapter = sb.ScoutBrokerAdapter(broker, journal, account_fingerprint=ACCOUNT, session_id='manual-scout:offline', symbols=frozenset({'DOCU'}), authority_epoch=1, maximum_evidence_age_seconds=60, clock=lambda: state.now)
    req, grant = req_grant()
    journal.prepare(req)
    journal.grant(grant)
    client = ScoutClient(journal, None, account_fingerprint=ACCOUNT, session_id=req.session_id, symbols=frozenset({'DOCU'}), authority_epoch=1, clock=lambda: state.now)

    def common_gate():
        if not state.manual:
            raise sb.ScoutBrokerError('offline manual stop')
        if state.account != ACCOUNT:
            raise sb.ScoutBrokerError('offline local account binding changed')
        if not 0 <= (state.now - NOW).total_seconds() <= 60:
            raise sb.ScoutBrokerError('offline stale account/quote')
        if state.now >= datetime.fromisoformat(req.target_close_at):
            raise sb.ScoutBrokerError('offline expired target')
        return True

    def authorization(*_):
        state.auth_calls += 1
        state.now += timedelta(seconds=state.delay)
        state.auth_hook()
        return common_gate()

    def final(*_):
        state.final_calls += 1
        state.now += timedelta(seconds=state.delay)
        state.final_hook()
        return common_gate()

    def client_gate(*_):
        state.client_calls += 1
        return common_gate()

    def send(actual, permitted):
        return adapter.send(actual, permitted, expected_snapshot_identity=GEN, authorization_gate=authorization, final_gate=final)
    return SimpleNamespace(books=books, journal=journal, state=state, broker=broker, adapter=adapter, req=req, grant=grant, client=client, send=send, client_gate=client_gate)

def run(env):
    error = None
    try:
        env.client.submit(env.req.request_id, send=env.send, final_gate=env.client_gate)
    except Exception as exc:
        error = exc
    record = env.journal.get(env.req.request_id)
    return (error, record)

@pytest.fixture(autouse=True)
def forbid_socket(monkeypatch):

    def forbidden(*args, **kwargs):
        raise AssertionError('No network allowed in offline regression')
    monkeypatch.setattr(socket, 'socket', forbidden)

@pytest.mark.parametrize('native', [True, False])
def test_normal_same_exact_quantity_one_post_and_receipt(tmp_path, monkeypatch, native):
    env = build(tmp_path, monkeypatch, native=native)
    error, record = run(env)
    assert error is None and record['state'] == 'ACCEPTED' and (record['broker_order_id'] == '123')
    assert len(env.state.posts) == 1 and env.state.posts[0]['orderLegCollection'][0]['quantity'] == 3
    expected = 1 if True and native else 2
    assert env.state.auth_calls == env.state.final_calls == expected
    assert env.state.verify_calls == expected * 2 and env.state.client_calls == 2
    with pytest.raises(JournalStateError):
        env.client.submit(env.req.request_id, send=env.send, final_gate=env.client_gate)
    assert len(env.state.posts) == 1
    env.journal.close()

@pytest.mark.parametrize('fault', ['manual_stop', 'account_identity', 'generation', 'stale', 'target_expiry'])
def test_native_mutation_during_preparation_stays_unsent_unknown(tmp_path, monkeypatch, fault):
    env = build(tmp_path, monkeypatch)

    def mutate():
        if fault == 'manual_stop':
            env.state.manual = False
        elif fault == 'account_identity':
            env.state.account = 'e' * 64
        elif fault == 'generation':
            env.state.generation = 'changed'
        elif fault == 'stale':
            env.state.now += timedelta(seconds=61)
        else:
            env.state.now += timedelta(minutes=16)
    env.state.prep_hook = mutate
    error, record = run(env)
    assert error is not None and record['state'] == 'UNKNOWN' and (not env.state.posts)
    observation = env.adapter.native_before_post_failure
    assert observation is not None and observation.error is error
    assert observation.request_sha256 == env.req.sha256 and observation.grant_sha256 == env.grant.sha256
    before = record
    with pytest.raises(JournalStateError):
        env.client.submit(env.req.request_id, send=env.send, final_gate=env.client_gate)
    assert env.journal.get(env.req.request_id) == before and (not env.state.posts)
    env.journal.close()

@pytest.mark.parametrize('fault', ['manual_stop', 'stale', 'target_expiry'])
def test_final_callback_cannot_outlive_permission(tmp_path, monkeypatch, fault):
    env = build(tmp_path, monkeypatch)

    def mutate():
        if fault == 'manual_stop':
            env.state.manual = False
        elif fault == 'stale':
            env.state.now += timedelta(seconds=61)
        else:
            env.state.now += timedelta(minutes=16)
    env.state.final_hook = mutate
    error, record = run(env)
    assert error is not None and record['state'] == 'UNKNOWN' and (not env.state.posts)
    assert env.adapter.native_before_post_failure is not None
    env.journal.close()

@pytest.mark.parametrize('fault', ['transport', 'receipt'])
def test_transport_or_receipt_failure_never_mints_unsent_proof_or_retries(tmp_path, monkeypatch, fault):
    env = build(tmp_path, monkeypatch)
    if fault == 'transport':
        env.state.post_error = TimeoutError('offline uncertain post')
    else:
        env.state.location = 'untrusted-location'
    error, record = run(env)
    assert error is not None and record['state'] == 'UNKNOWN' and (len(env.state.posts) == 1)
    assert env.adapter.initial_gate_failure is None and env.adapter.native_before_post_failure is None
    with pytest.raises(JournalStateError):
        env.client.submit(env.req.request_id, send=env.send, final_gate=env.client_gate)
    assert len(env.state.posts) == 1
    env.journal.close()

@pytest.mark.parametrize('override', ['instance', 'class'])
def test_overridden_or_wrapped_sender_retains_outer_checks_and_cannot_prove_later_failure_unsent(tmp_path, monkeypatch, override):
    env = build(tmp_path, monkeypatch)
    native = schwab.SchwabSession.submit_prepared_order

    def opaque(self, payload, context, *, before_post):
        env.state.posts.append('opaque-effect')
        env.state.manual = False
        return native(self, payload, context, before_post=before_post)
    if override == 'instance':
        env.broker.submit_prepared_order = MethodType(opaque, env.broker)
    else:
        monkeypatch.setattr(schwab.SchwabSession, 'submit_prepared_order', opaque)
    error, record = run(env)
    assert error is not None and record['state'] == 'UNKNOWN' and (env.state.posts == ['opaque-effect'])
    assert env.adapter.initial_gate_failure is None and env.adapter.native_before_post_failure is None
    assert env.state.auth_calls == 2 and env.state.final_calls == 1
    env.journal.close()

def test_simulated_redundant_native_io_can_expire_unchanged_freshness_guard(tmp_path, monkeypatch):
    env = build(tmp_path, monkeypatch)
    env.state.delay = 16
    error, record = run(env)
    assert error is None and record['state'] == 'ACCEPTED' and (len(env.state.posts) == 1)
    assert env.state.auth_calls == env.state.final_calls == 1
    env.journal.close()

@pytest.mark.parametrize('native', [True, False])
def test_saved_clearance_preserves_exact_unknown_token_and_chain(tmp_path, monkeypatch, native):
    env = build(tmp_path, monkeypatch, native=native)
    env.state.prep_hook = lambda: setattr(env.state, 'manual', False)
    error, record = run(env)
    assert record['state'] == 'UNKNOWN' and record['send_token'] and (env.state.posts == [])
    before_events = env.journal.pending_events()
    receipt = clear.create_clearance(env.books, env.journal, record, adapter=env.adapter, error=error, as_of=env.state.now)
    assert receipt['commitment'] == 'no_order_transport_entered' and receipt['historical_execution'] == 'UNKNOWN'
    value = json.loads(Path(receipt['path']).read_bytes())['payload']
    expected_native = True and native
    assert value['version'] == (clear.NATIVE_VERSION if expected_native else clear.VERSION)
    assert value['observation']['stage'] == ('native_schwab_before_post' if expected_native else 'adapter_initial_gate')
    assert clear.load_clearance(env.books, env.journal, record, as_of=env.state.now) == receipt
    assert env.journal.get(env.req.request_id) == record and env.journal.pending_events() == before_events
    with pytest.raises(JournalStateError):
        env.client.submit(env.req.request_id, send=env.send, final_gate=env.client_gate)
    assert env.state.posts == [] and env.journal.get(env.req.request_id) == record
    env.journal.close()

def test_native_identity_changes_after_authorization_are_caught_before_final_post(tmp_path, monkeypatch):
    env = build(tmp_path, monkeypatch)
    env.state.auth_hook = lambda: setattr(env.state, 'generation', 'changed-after-authorization')
    error, record = run(env)
    assert error is not None and record['state'] == 'UNKNOWN' and (env.state.posts == [])
    assert env.state.auth_calls == 1 and env.state.final_calls == 0 and (env.state.verify_calls == 2)
    env.journal.close()

def test_exact_grant_expiration_after_authorization_blocks_native_post(tmp_path, monkeypatch):
    env = build(tmp_path, monkeypatch)
    env.state.auth_hook = lambda: setattr(env.state, 'now', datetime.fromisoformat(env.grant.expires_at))
    error, record = run(env)
    assert error is not None and record['state'] == 'UNKNOWN' and (env.state.posts == [])
    assert env.state.auth_calls == 1 and env.state.final_calls == 0
    env.journal.close()

def test_sender_replacement_during_preparation_is_reclassified_as_opaque(tmp_path, monkeypatch):
    env = build(tmp_path, monkeypatch)
    native = schwab.SchwabSession.submit_prepared_order

    def opaque(self, payload, context, *, before_post):
        env.state.posts.append('opaque-effect')
        env.state.manual = False
        return native(self, payload, context, before_post=before_post)
    env.state.prep_hook = lambda: setattr(env.broker, 'submit_prepared_order', MethodType(opaque, env.broker))
    error, record = run(env)
    assert error is not None and record['state'] == 'UNKNOWN' and (env.state.posts == ['opaque-effect'])
    assert env.state.auth_calls == 2 and env.state.final_calls == 1
    assert env.adapter.initial_gate_failure is None and env.adapter.native_before_post_failure is None
    env.journal.close()

def test_spoofed_bound_method_attributes_cannot_bypass_outer_manual_gate(tmp_path, monkeypatch):
    env = build(tmp_path, monkeypatch)

    class Spoof:
        __self__ = env.broker
        __func__ = sb._NATIVE_PREPARED_POST

        def __call__(self, payload, context, *, before_post, **kwargs):
            env.state.posts.append('spoof-ignored-gate')
            return env.state.location
    env.broker.submit_prepared_order = Spoof()
    env.state.prep_hook = lambda: setattr(env.state, 'manual', False)
    error, record = run(env)
    assert error is not None and record['state'] == 'UNKNOWN' and (env.state.posts == [])
    assert env.adapter.initial_gate_failure is not None and env.adapter.native_before_post_failure is None
    env.journal.close()

def test_opaque_descriptor_is_not_evaluated_before_original_outer_gate(tmp_path, monkeypatch):
    effects = []

    class OpaqueDescriptor(schwab.SchwabSession):

        @property
        def submit_prepared_order(self):
            effects.append('opaque-getter-effect')
            return lambda *a, **k: None
    env = build(tmp_path, monkeypatch, broker_type=OpaqueDescriptor)
    env.state.prep_hook = lambda: setattr(env.state, 'manual', False)
    error, record = run(env)
    assert error is not None and record['state'] == 'UNKNOWN' and (effects == []) and (env.state.posts == [])
    assert env.adapter.initial_gate_failure is not None
    env.journal.close()

def test_genuine_inherited_native_bound_method_keeps_final_checks(tmp_path, monkeypatch):

    class Inherited(schwab.SchwabSession):
        pass
    env = build(tmp_path, monkeypatch, broker_type=Inherited)
    error, record = run(env)
    assert error is None and record['state'] == 'ACCEPTED' and (len(env.state.posts) == 1)
    expected = 2
    assert env.state.auth_calls == env.state.final_calls == expected
    env.journal.close()

def test_subclass_override_retains_outer_gate_and_opaque_uncertainty(tmp_path, monkeypatch):

    class Override(schwab.SchwabSession):

        def submit_prepared_order(self, payload, context, *, before_post):
            env.state.posts.append('subclass-opaque-effect')
            env.state.manual = False
            before_post()
    env = build(tmp_path, monkeypatch, broker_type=Override)
    error, record = run(env)
    assert error is not None and record['state'] == 'UNKNOWN' and (env.state.posts == ['subclass-opaque-effect'])
    assert env.state.auth_calls == 2 and env.state.final_calls == 1
    assert env.adapter.initial_gate_failure is None and env.adapter.native_before_post_failure is None
    env.journal.close()

def test_overridden_getattribute_cannot_bypass_outer_gate(tmp_path, monkeypatch):
    effects = []

    class OpaqueGetattribute(schwab.SchwabSession):

        def __getattribute__(self, name):
            if name == 'submit_prepared_order':
                effects.append('opaque-getattribute-effect')
            return super().__getattribute__(name)
    env = build(tmp_path, monkeypatch, broker_type=OpaqueGetattribute)
    env.state.prep_hook = lambda: setattr(env.state, 'manual', False)
    error, record = run(env)
    assert error is not None and record['state'] == 'UNKNOWN' and (effects == []) and (env.state.posts == [])
    assert env.adapter.initial_gate_failure is not None
    env.journal.close()

@pytest.mark.parametrize('fault', ['normal', 'stale', 'manual_stop', 'identity', 'supersession'])
def test_real_local_executor_capacity_savedforecast_clearance_and_no_replay(local_env, monkeypatch, fault):
    env = local_env

    class Clock(datetime):

        @classmethod
        def now(cls, tz=None):
            return env.state.now
    monkeypatch.setattr(sj, 'datetime', Clock)
    monkeypatch.setattr(sb.ScoutBrokerAdapter, 'send', CAND_SEND_FUNCTION)
    native_transport(env, monkeypatch)
    original = env.forecast
    saved_read = local_execution._saved_forecast
    calls = []

    def read(*args, **kwargs):
        calls.append(1)
        value = saved_read(*args, **kwargs)
        if len(calls) == 4:
            if fault == 'stale':
                env.state.now += timedelta(seconds=61)
            elif fault == 'manual_stop':
                env.state.manual = False
            elif fault == 'identity':
                env.broker.stable_account_fingerprint = lambda: 'e' * 64
            elif fault == 'supersession':
                env.forecast = local_forecast(env, advance=True)
        return value
    monkeypatch.setattr(local_execution, '_saved_forecast', read)
    result = env.executor.step(original)
    old = local_record(env, original)
    if fault == 'normal':
        assert result['disposition'] == 'submitted' and result['adjusted_quantity'] == 2 and (len(env.broker.posts) == 1)
        assert old['state'] == 'ACCEPTED'
        assert len(calls) == 5
        assert clear.load_clearance(env.executor.books, env.executor.journal, old, as_of=env.state.now) is None
    else:
        assert old['state'] == 'UNKNOWN' and old['send_token'] and (env.broker.posts == [])
        receipt = clear.load_clearance(env.executor.books, env.executor.journal, old, as_of=env.state.now)
        assert receipt is not None and receipt['commitment'] == 'no_order_transport_entered'
        value = json.loads(Path(receipt['path']).read_bytes())['payload']
        assert value['version'] == clear.NATIVE_VERSION
        before = env.executor.journal.pending_events()
        env.state.manual = True
        env.broker.stable_account_fingerprint = lambda: ACCOUNT
        env.executor.recover()
        if fault == 'supersession':
            with pytest.raises(local_execution.ContractError, match='supersedes'):
                env.executor.step(original)
        else:
            env.executor.step(original)
        assert env.executor.journal.get(old['request']['request_id']) == old and env.executor.journal.pending_events() == before and (env.broker.posts == [])
        env.forecast = local_forecast(env, advance=True)
        current = env.executor.step(env.forecast)
        assert current['disposition'] == 'submitted' and len(env.broker.posts) == 1
        assert env.executor.journal.get(old['request']['request_id']) == old

@pytest.mark.parametrize('which', ['authorization', 'final'])
@pytest.mark.parametrize('answer', [False, None])
def test_missing_explicit_gate_approval_is_unsent_unknown(tmp_path, monkeypatch, which, answer):
    env = build(tmp_path, monkeypatch)

    def send(req, grant):
        options = {'expected_snapshot_identity': GEN, 'authorization_gate': lambda *_: True, 'final_gate': lambda *_: True}
        options[which + '_gate' if which == 'authorization' else 'final_gate'] = lambda *_: answer
        return env.adapter.send(req, grant, **options)
    env.send = send
    error, record = run(env)
    assert error is not None and record['state'] == 'UNKNOWN' and (env.state.posts == [])
    assert env.adapter.native_before_post_failure is not None
    env.journal.close()

def test_opaque_preparation_exception_never_mints_unsent_proof(tmp_path, monkeypatch):
    env = build(tmp_path, monkeypatch)

    def prepare():
        raise OSError('offline opaque preparation failed')
    env.state.prep_hook = prepare
    error, record = run(env)
    assert error is not None and record['state'] == 'UNKNOWN' and (env.state.posts == [])
    assert env.adapter.initial_gate_failure is None and env.adapter.native_before_post_failure is None
    env.journal.close()

@pytest.mark.parametrize('stop_after_static_check', [False, True])
def test_class_descriptor_replacement_seam_binds_frozen_native_without_opaque_getter(tmp_path, monkeypatch, stop_after_static_check):
    env = build(tmp_path, monkeypatch)
    actual = namespace['getattr_static']
    effects = []
    armed = [False]

    def inspect_then_replace(obj, name, *args):
        result = actual(obj, name, *args)
        if obj is env.broker and name == 'submit_prepared_order' and (not armed[0]):
            armed[0] = True

            def opaque_getter(self):
                effects.append('opaque-getter-effect')
                return lambda *a, **k: None
            monkeypatch.setattr(schwab.SchwabSession, 'submit_prepared_order', property(opaque_getter))
            if stop_after_static_check:
                env.state.manual = False
        return result
    monkeypatch.setitem(namespace, 'getattr_static', inspect_then_replace)
    error, record = run(env)
    assert armed[0] and effects == []
    if stop_after_static_check:
        assert error is not None and record['state'] == 'UNKNOWN' and (env.state.posts == [])
        assert env.adapter.native_before_post_failure is not None
    else:
        assert error is None and record['state'] == 'ACCEPTED' and (len(env.state.posts) == 1)
    env.journal.close()

@pytest.mark.parametrize('boundary', ['cutoff', 'tif'])
@pytest.mark.parametrize('seam', ['authorization', 'final'])
def test_exact_cutoff_and_tif_crossing_during_gate_blocks_post(tmp_path, monkeypatch, boundary, seam):
    initial = datetime.fromisoformat('2026-10-12T23:44:59+00:00' if boundary == 'cutoff' else '2026-10-12T19:59:59+00:00')
    monkeypatch.setitem(globals(), 'NOW', initial)
    env = build(tmp_path, monkeypatch)
    hook = lambda: setattr(env.state, 'now', initial + timedelta(seconds=1))
    setattr(env.state, 'auth_hook' if seam == 'authorization' else 'final_hook', hook)
    error, record = run(env)
    assert error is not None and record['state'] == 'UNKNOWN' and (env.state.posts == [])
    assert ('16:45' if boundary == 'cutoff' else 'session is closed') in str(error)
    observation = env.adapter.native_before_post_failure
    assert observation is not None and observation.error is error
    proof = clear.create_clearance(env.books, env.journal, record, adapter=env.adapter, error=error, as_of=env.state.now)
    assert proof['commitment'] == 'no_order_transport_entered'
    before = env.journal.pending_events()
    with pytest.raises(JournalStateError):
        env.client.submit(env.req.request_id, send=env.send, final_gate=env.client_gate)
    assert env.journal.get(env.req.request_id) == record and env.journal.pending_events() == before and (not env.state.posts)
    env.journal.close()

def test_corrupt_journal_after_authorization_never_reaches_sender(tmp_path, monkeypatch):
    env = build(tmp_path, monkeypatch)
    corruption = []

    def corrupt():
        with sqlite3.connect(env.journal.path) as db:
            db.execute('UPDATE records SET payload=? WHERE request_id=?', ('{}', env.req.request_id))
        corruption.append(True)
    env.state.auth_hook = corrupt
    with pytest.raises(sj.JournalCorruptionError):
        env.client.submit(env.req.request_id, send=env.send, final_gate=env.client_gate)
    assert corruption and (not env.state.posts) and (env.state.final_calls == 0)
    with pytest.raises(sj.JournalCorruptionError):
        env.journal.get(env.req.request_id)
    with pytest.raises(sj.JournalCorruptionError):
        env.client.submit(env.req.request_id, send=env.send, final_gate=env.client_gate)
    assert not env.state.posts
    env.journal.close()

@pytest.mark.parametrize('fault', ['capacity', 'reservation'])
def test_saved_permit_mutation_between_authorization_and_final_is_unsent(local_env, monkeypatch, fault):
    env = local_env

    class Clock(datetime):

        @classmethod
        def now(cls, tz=None):
            return env.state.now
    monkeypatch.setattr(sj, 'datetime', Clock)
    monkeypatch.setattr(sb.ScoutBrokerAdapter, 'send', CAND_SEND_FUNCTION)
    native_transport(env, monkeypatch)
    original_save = env.executor.books.save_receipt
    permit = []

    def save(*args, **kwargs):
        value = original_save(*args, **kwargs)
        if kwargs.get('kind') == 'local-account-permit':
            permit.append(Path(value['path']))
        return value
    monkeypatch.setattr(env.executor.books, 'save_receipt', save)
    original_read = local_execution._saved_forecast
    calls = []
    tampered = []

    def read(*args, **kwargs):
        value = original_read(*args, **kwargs)
        calls.append(1)
        if len(calls) == 4:
            assert len(permit) == 1
            payload = json.loads(permit[0].read_bytes())
            if fault == 'capacity':
                payload['payload']['capacity']['adjusted_quantity'] = 999
            else:
                payload['payload']['permit']['reservation_id'] = 'changed-reservation'
            permit[0].write_text(json.dumps(payload), encoding='utf-8')
            tampered.append(True)
        return value
    monkeypatch.setattr(local_execution, '_saved_forecast', read)
    result = env.executor.step(env.forecast)
    record = local_record(env)
    assert tampered and len(calls) == 4
    assert result['disposition'] == 'submission_recovery_required' and record['state'] == 'UNKNOWN' and (env.broker.posts == [])
    proof = clear.load_clearance(env.executor.books, env.executor.journal, record, as_of=env.state.now)
    assert proof is not None and proof['commitment'] == 'no_order_transport_entered'
    raw = json.loads(Path(proof['path']).read_bytes())['payload']
    assert raw['version'] == clear.NATIVE_VERSION
    before = env.executor.journal.pending_events()
    env.executor.step(env.forecast)
    assert env.executor.journal.get(record['request']['request_id']) == record and env.executor.journal.pending_events() == before and (env.broker.posts == [])

@pytest.mark.parametrize('seam', ['authorization', 'final'])
def test_changed_time_in_force_at_final_boundary_is_rejected(tmp_path, monkeypatch, seam):
    from dataclasses import replace
    env = build(tmp_path, monkeypatch)
    actual_window = sb.stock_execution_window
    changed = []

    def window(now):
        current = actual_window(now)
        return replace(current, time_in_force='PM') if changed else current
    monkeypatch.setattr(sb, 'stock_execution_window', window)
    setattr(env.state, 'auth_hook' if seam == 'authorization' else 'final_hook', lambda: changed.append(True))
    error, record = run(env)
    assert error is not None and 'session changed' in str(error) and (record['state'] == 'UNKNOWN') and (not env.state.posts)
    proof = clear.create_clearance(env.books, env.journal, record, adapter=env.adapter, error=error, as_of=env.state.now)
    assert proof['commitment'] == 'no_order_transport_entered'
    env.journal.close()


def test_raw_instance_native_function_keeps_original_unbound_call_semantics(tmp_path, monkeypatch):
    env = build(tmp_path, monkeypatch)
    env.broker.submit_prepared_order = sb._NATIVE_PREPARED_POST
    error, record = run(env)
    assert isinstance(error, TypeError) and record['state'] == 'UNKNOWN'
    assert not env.state.posts
    assert env.state.auth_calls == env.state.final_calls == 1
    assert env.adapter.initial_gate_failure is None and env.adapter.native_before_post_failure is None
    before = env.journal.pending_events()
    with pytest.raises(JournalStateError):
        env.client.submit(env.req.request_id, send=env.send, final_gate=env.client_gate)
    assert env.journal.get(env.req.request_id) == record and env.journal.pending_events() == before
    assert not env.state.posts
    env.journal.close()


def test_instance_bound_native_method_retains_original_outer_gate(tmp_path, monkeypatch):
    env = build(tmp_path, monkeypatch)
    env.broker.submit_prepared_order = MethodType(sb._NATIVE_PREPARED_POST, env.broker)
    error, record = run(env)
    assert error is None and record['state'] == 'ACCEPTED' and len(env.state.posts) == 1
    assert env.state.auth_calls == env.state.final_calls == 2
    env.journal.close()


@pytest.mark.parametrize('descriptor', ['opaque_property', 'foreign_owner', 'wrong_name'])
def test_unsupported_instance_dictionary_descriptor_is_never_evaluated(tmp_path, monkeypatch, descriptor):
    env = build(tmp_path, monkeypatch)
    effects = []
    if descriptor == 'opaque_property':
        replacement = property(lambda _self: effects.append('opaque-dictionary-effect'))
    elif descriptor == 'foreign_owner':
        class Foreign:
            pass
        replacement = vars(Foreign)['__dict__']
    else:
        replacement = sb.getattr_static(schwab.SchwabSession, '__weakref__')
    monkeypatch.setattr(sb, '_NATIVE_INSTANCE_DICT', replacement)
    error, record = run(env)
    assert error is None and record['state'] == 'ACCEPTED' and len(env.state.posts) == 1
    assert env.state.auth_calls == env.state.final_calls == 2 and effects == []
    env.journal.close()


def test_instance_shadow_inserted_during_static_sender_inspection_remains_unbound(tmp_path, monkeypatch):
    env = build(tmp_path, monkeypatch)
    original = sb.getattr_static
    inserted = []
    def inspect_then_shadow(obj, name, *args):
        value = original(obj, name, *args)
        if obj is env.broker and name == 'submit_prepared_order' and not inserted:
            inserted.append(True)
            env.broker.submit_prepared_order = sb._NATIVE_PREPARED_POST
        return value
    monkeypatch.setattr(sb, 'getattr_static', inspect_then_shadow)
    error, record = run(env)
    assert inserted and isinstance(error, TypeError) and record['state'] == 'UNKNOWN'
    assert not env.state.posts and env.state.auth_calls == env.state.final_calls == 1
    assert env.adapter.initial_gate_failure is None and env.adapter.native_before_post_failure is None
    env.journal.close()
