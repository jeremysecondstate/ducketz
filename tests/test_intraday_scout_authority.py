"""Exact published admission contract; temporary books, no sockets/broker IO."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
import socket

import pytest

from ml.intraday.admission_service import AdmissionError
from ml.account_gameplan.authority import AuthorityError
from ml.intraday.scout_authority import (
    AuthorityProtocolError, AuthorityReceipt, ScoutAuthorityBinding, ScoutAuthorityClient,
    claim_id, load_binding, validate_send_receipt, validate_negative_admission_receipt, validate_view_receipt,
    validate_readiness_receipt, validate_view_envelope, READINESS_VERSION, PROTOCOL_VERSION,
)
from ml.intraday.scout_books import ScoutBooks, VERSION as BOOKS_VERSION
from ml.intraday.scout_protocol import Grant, ProtocolError, canonical_sha256
from ml.stock_trader.horizon_ledger import FillEvidence, OrderEvidence
from tests.test_intraday_admission_service import Fixture, request


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Network is forbidden in authority wrapper tests')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'bind', forbidden)


class Transport:
    def __init__(self, service):
        self.service = service
        self.calls = []
        self.mutate = lambda path, result: result
        self.drop = None

    def call(self, method, path, body=None):
        self.calls.append((method, path, deepcopy(body)))
        operation = path.removeprefix('/v1/').replace('-', '_')
        result = getattr(self.service, operation)('pc-new', *([] if body is None else [body]))
        if self.drop == path:
            self.drop = None
            raise TimeoutError('synthetic lost reply')
        return self.mutate(path, deepcopy(result))


def binding(tmp_path):
    return ScoutAuthorityBinding(endpoint='https://atlas.example.test:8443',
        server_identity='reviewed-atlas-server', account_fingerprint=request().account_fingerprint,
        symbols=('MSFT',), client_certificate=str(tmp_path / 'client.pem'), private_key=str(tmp_path / 'client.key'),
        server_ca=str(tmp_path / 'ca.pem'), client_certificate_sha256=hashlib.sha256(b'client-public').hexdigest(),
        server_ca_sha256=hashlib.sha256(b'ca-public').hexdigest(), server_certificate_sha256='d'*64)


def readiness_response(binding, now, nonce, **changes):
    """Fixed Atlas6139260261 wire fixture; it never calls a production service."""
    return dict(version=READINESS_VERSION, protocol_version=PROTOCOL_VERSION,
        server_identity=binding.server_identity, participant_id='pc-new', stream='scout-15m',
        account_fingerprint=binding.account_fingerprint, request_nonce=nonce,
        observed_at=now.isoformat(), expires_at=(now + timedelta(seconds=30)).isoformat(),
        authenticated=True, read_only=True, orders_or_commitments_created=0,
        ready=True, protocol_compatible=True, reason_codes=[],
        writers={'atlas': {'ready': True, 'compatibility_state': 'compatible', 'reason_codes': [], 'proof_sha256': '1'*64},
                 'gameplan': {'ready': True, 'compatibility_state': 'absent', 'reason_codes': [], 'proof_sha256': '2'*64}}) | changes


class Setup:
    def __init__(self, tmp_path):
        self.env = Fixture(tmp_path / 'atlas')
        self.binding = binding(tmp_path)
        self.books = ScoutBooks(tmp_path / 'scout', account_fingerprint=self.binding.account_fingerprint)
        self.journal = self.books.create_session(request().session_id)
        self.transport = Transport(self.env.service)
        self.client = ScoutAuthorityClient(self.binding, self.books, transport=self.transport, clock=self.env.clock)
        self.journal.prepare(request())

    def grant(self):
        receipt = self.client.reserve(request())
        self.journal.grant(receipt.grant)
        return receipt

    def claimed(self):
        self.grant()
        self.journal.claim_send(request().request_id)
        return self.record()

    def record(self):
        return self.journal.get(request().request_id)

    def authorized(self):
        return self.client.authorize_send(self.claimed())

    def accepted(self):
        self.authorized()
        current = self.record()
        self.journal.outcome(request().request_id, current['send_token'], '201')
        return self.client.accepted(self.record())


@pytest.fixture
def setup(tmp_path):
    result = Setup(tmp_path)
    yield result
    result.journal.close()


def test_constructor_performs_no_io(tmp_path, monkeypatch):
    value = binding(tmp_path)
    class Books:
        account_fingerprint = value.account_fingerprint
    def forbidden(*args, **kwargs):
        raise AssertionError('constructor performed IO')
    monkeypatch.setattr('pathlib.Path.read_bytes', forbidden)
    monkeypatch.setattr('pathlib.Path.lstat', forbidden)
    monkeypatch.setattr('ml.intraday.scout_authority.client_tls_context', forbidden)
    client = ScoutAuthorityClient(value, Books())
    assert client.binding is value and client._transport is None


def test_private_binding_load_pins_public_files_and_preserves_key(tmp_path):
    value = binding(tmp_path)
    (tmp_path / 'client.pem').write_bytes(b'client-public')
    (tmp_path / 'ca.pem').write_bytes(b'ca-public')
    key = tmp_path / 'client.key'
    key.write_bytes(b'private-key-not-a-real-credential')
    path = tmp_path / 'binding.json'
    path.write_text(json.dumps(value.payload()), encoding='utf-8')
    assert load_binding(path) == value
    assert 'private-key-not-a-real-credential' not in path.read_text()
    (tmp_path / 'ca.pem').write_bytes(b'replaced')
    with pytest.raises(AuthorityProtocolError):
        load_binding(path)
    assert key.read_bytes() == b'private-key-not-a-real-credential'


def test_old_binding_without_explicit_enrolled_leaf_pin_cannot_be_adopted(tmp_path):
    value = binding(tmp_path).payload()
    value['version'] = 'scout-atlas-authority-binding-v1'
    del value['server_certificate_sha256']
    path = tmp_path / 'old-binding.json'
    path.write_text(json.dumps(value))
    with pytest.raises(AuthorityProtocolError):
        load_binding(path)


@pytest.mark.parametrize('changes', [
    {'endpoint': 'http://atlas.test'}, {'endpoint': 'https://a.test/path'},
    {'endpoint': 'https://u:p@a.test'}, {'endpoint': 'https://a.test:0'},
    {'endpoint': 'https://a.test?x=1'}, {'endpoint': 'https://a.test\n'}, {'endpoint': 42},
    {'server_identity': ''}, {'account_fingerprint': 'b'*63},
    {'symbols': ('MSFT', 'MSFT')}, {'symbols': ('msft',)}, {'symbols': ()}, {'symbols': ([],)},
    {'timeout_seconds': True}, {'timeout_seconds': float('nan')}, {'timeout_seconds': 61},
    {'private_key': 'relative.key'}, {'server_ca': r'\\peer\share\ca.pem'},
])
def test_binding_rejects_unsafe_or_ambiguous_scope(tmp_path, changes):
    with pytest.raises(AuthorityProtocolError):
        replace(binding(tmp_path), **changes)


@pytest.mark.parametrize('change', [
    {'server_identity': 'another-server'}, {'protocol_version': 'v-next'},
    {'participant_id': 'pc-original'}, {'stream': 'atlas-15m'},
    {'account_fingerprint': 'b'*64}, {'read_only': 1}, {'ready': True}, {'operations': []},
])
def test_capabilities_exact_identity_protocol_is_required_before_reserve(setup, change):
    setup.transport.mutate = lambda path, result: {**result, **change}
    with pytest.raises(AuthorityProtocolError):
        setup.client.reserve(request())
    assert len(setup.transport.calls) == 1
    assert not setup.env.service.path.exists()


def test_capabilities_are_noncommitting_and_never_readiness(setup):
    receipt = setup.client.capabilities()
    assert receipt.response['read_only'] is True and 'ready' not in receipt.response
    assert not setup.env.service.path.exists() and setup.env.captures == 0
    assert not hasattr(setup.client, '_readiness')


def test_view_receipt_is_saved_with_bound_request_start_and_immutable_copy(setup):
    body = {'request_nonce': setup.client._view_nonce()}
    response = {'fixture': 'view-envelope-only'}
    started = setup.env.clock()
    receipt = setup.client._save('fixture-view', 'fixture:1', body, response, requested_at=started)
    response['fixture'] = 'changed'
    value, requested, received = validate_view_receipt(receipt, setup.binding,
        operation='fixture-view', request_id='fixture:1', request=body, now=setup.env.clock())
    assert value == {'fixture': 'view-envelope-only'} and requested == received == started
    value['fixture'] = 'mutated returned view'
    assert receipt.response == {'fixture': 'view-envelope-only'}
    assert set(receipt.payload) == {'version', 'binding_sha256', 'operation', 'request', 'response', 'requested_at', 'received_at'}


@pytest.mark.parametrize('mutation', ['digest', 'path', 'binding', 'operation', 'request_id', 'request',
                                    'requested_future', 'received_future', 'missing_start', 'extra'])
def test_view_receipt_refuses_changed_binding_or_time(setup, mutation):
    started = setup.env.clock()
    body = {'request_nonce': 'fresh-fixture-1'}
    receipt = setup.client._save('fixture-view', 'fixture:1', body, {}, requested_at=started)
    arguments = dict(operation='fixture-view', request_id='fixture:1', request=body, now=started)
    value = receipt.payload
    if mutation == 'digest':
        receipt = replace(receipt, sha256='0' * 64)
    elif mutation == 'path':
        receipt = replace(receipt, path='')
    elif mutation == 'binding':
        value['binding_sha256'] = 'f' * 64
    elif mutation in {'operation', 'request_id', 'request'}:
        arguments[mutation] = {} if mutation == 'request' else 'another'
    elif mutation in {'requested_future', 'received_future'}:
        value['requested_at' if mutation == 'requested_future' else 'received_at'] = (started + timedelta(seconds=1)).isoformat()
    elif mutation == 'missing_start':
        del value['requested_at']
    else:
        value['extra'] = None
    if value != receipt.payload:
        saved = setup.books.save_receipt(kind='authority:fixture-view', request_id='fixture:1', payload=value)
        receipt = AuthorityReceipt(json.dumps(value), saved['sha256'], saved['path'])
    with pytest.raises(AuthorityProtocolError):
        validate_view_receipt(receipt, setup.binding, **arguments)


@pytest.mark.parametrize('nonce', ['', 'a'*129, '_first', 'has space', 'nonascii-é', [], None, True])
def test_view_nonce_is_strict_and_consumed_before_attempt(setup, nonce):
    setup.client._nonce_factory = lambda: nonce
    with pytest.raises(AuthorityProtocolError, match='fresh unique'):
        setup.client._view_nonce()


def test_view_nonce_cannot_be_reused_by_the_same_client(setup):
    setup.client._nonce_factory = lambda: 'fresh-once'
    assert setup.client._view_nonce() == 'fresh-once'
    with pytest.raises(AuthorityProtocolError, match='fresh unique'):
        setup.client._view_nonce()


def view_transport(setup, transform=lambda response: response):
    class Views:
        calls = []
        def call(self, method, path, body):
            self.calls.append((method, path, deepcopy(body)))
            assert method == 'POST' and path == '/v1/readiness' and set(body) == {'request_nonce'}
            return transform(readiness_response(setup.binding, setup.env.clock(), body['request_nonce']))
    setup.client._transport = Views()
    return setup.client._transport


def test_readiness_posts_fresh_nonce_saves_proof_and_remains_noncommitting(setup):
    transport = view_transport(setup)
    before = setup.journal.records()
    first = setup.client.readiness()
    second = setup.client.readiness()
    assert first.response['request_nonce'] != second.response['request_nonce']
    assert len(transport.calls) == 2 and first.sha256 != second.sha256
    assert validate_readiness_receipt(first, setup.binding, now=setup.env.clock()) == first.response
    assert first.grant is None
    assert setup.journal.records() == before and setup.env.captures == 0
    assert not setup.env.service.path.exists()


@pytest.mark.parametrize('changes', [
    {'version': 'new'}, {'protocol_version': 'other'}, {'server_identity': 'another-server'},
    {'participant_id': 'pc-original'}, {'stream': 'atlas-15m'}, {'account_fingerprint': 'b'*64},
    {'request_nonce': 'replayed'}, {'request_nonce': 1}, {'authenticated': False}, {'authenticated': 1},
    {'read_only': False}, {'read_only': 1}, {'protocol_compatible': False}, {'protocol_compatible': 1},
    {'orders_or_commitments_created': False}, {'orders_or_commitments_created': True},
    {'orders_or_commitments_created': 0.0}, {'orders_or_commitments_created': 1},
    {'ready': 1}, {'ready': False}, {'reason_codes': ['ATLAS:UNKNOWN']}, {'extra': None},
    {'writers': []}, {'writers': {}}, {'observed_at': '2026-10-12T12:00:00'},
    {'observed_at': '2026-10-12T12:00:00-00:00'},
])
def test_readiness_rejects_foreign_ambiguous_or_effectful_envelope(setup, changes):
    view_transport(setup, lambda value: value | changes)
    with pytest.raises(ProtocolError):
        setup.client.readiness()
    assert list((setup.books.root / 'receipts').glob('*.json')) == []


@pytest.mark.parametrize('mutation', [
    'missing_writer', 'extra_writer', 'not_object', 'missing_field', 'extra_field',
    'truthy_ready', 'unknown_state', 'false_compatible', 'true_blocked',
    'reason_for_ready', 'blocked_without_reason', 'proof_null', 'proof_upper', 'proof_short',
    'reason_type', 'reason_lower', 'reason_long', 'too_many_reasons', 'reason_nonascii',
])
def test_readiness_requires_exact_writer_proofs(setup, mutation):
    def mutate(value):
        writer = value['writers']['atlas']
        if mutation == 'missing_writer':
            del value['writers']['atlas']
        elif mutation == 'extra_writer':
            value['writers']['scout'] = deepcopy(writer)
        elif mutation == 'not_object':
            value['writers']['atlas'] = []
        elif mutation == 'missing_field':
            del writer['proof_sha256']
        elif mutation == 'extra_field':
            writer['extra'] = None
        elif mutation == 'truthy_ready':
            writer['ready'] = 1
        elif mutation == 'unknown_state':
            writer['compatibility_state'] = 'disk_verified'
        elif mutation == 'false_compatible':
            writer['ready'] = False
        elif mutation == 'true_blocked':
            writer['compatibility_state'] = 'blocked'
        elif mutation == 'reason_for_ready':
            writer['reason_codes'] = ['STALE']
        elif mutation == 'blocked_without_reason':
            writer.update(ready=False, compatibility_state='blocked')
            value['ready'] = False
        elif mutation.startswith('proof_'):
            writer['proof_sha256'] = {'proof_null': None, 'proof_upper': 'A'*64, 'proof_short': 'a'*63}[mutation]
        else:
            reasons = {'reason_type': 'STALE', 'reason_lower': ['stale'], 'reason_long': ['A'*129],
                       'too_many_reasons': ['STALE']*33, 'reason_nonascii': ['STALE_É']}[mutation]
            writer.update(ready=False, compatibility_state='blocked', reason_codes=reasons)
            value.update(ready=False, reason_codes=['ATLAS:' + code for code in reasons] if type(reasons) is list else [])
        return value
    view_transport(setup, mutate)
    with pytest.raises(ProtocolError):
        setup.client.readiness()


@pytest.mark.parametrize('atlas,gameplan', [('compatible', 'compatible'), ('absent', 'absent'),
                                          ('blocked', 'absent'), ('compatible', 'blocked'), ('blocked', 'blocked')])
def test_readiness_accepts_exact_ready_or_blocked_summaries(setup, atlas, gameplan):
    def mutate(value):
        value['reason_codes'] = []
        for name, state in [('atlas', atlas), ('gameplan', gameplan)]:
            reasons = ['PROOF_STALE', 'LOCK:UNKNOWN'] if state == 'blocked' else []
            value['writers'][name].update(ready=state != 'blocked', compatibility_state=state, reason_codes=reasons)
            value['reason_codes'].extend(name.upper() + ':' + reason for reason in reasons)
        value['ready'] = all(writer['ready'] for writer in value['writers'].values())
        return value
    view_transport(setup, mutate)
    receipt = setup.client.readiness()
    assert validate_readiness_receipt(receipt, setup.binding, now=setup.env.clock())['ready'] is (atlas != 'blocked' and gameplan != 'blocked')
    assert receipt.grant is None


@pytest.mark.parametrize('observed_offset,expires_offset', [(-1, 20), (1, 20), (0, 0), (0, -1), (0, 30.000001)])
def test_readiness_strict_request_observation_and_expiry_window(setup, observed_offset, expires_offset):
    now = setup.env.clock()
    view_transport(setup, lambda value: value | {
        'observed_at': (now + timedelta(seconds=observed_offset)).isoformat(),
        'expires_at': (now + timedelta(seconds=expires_offset)).isoformat()})
    with pytest.raises(AuthorityProtocolError):
        setup.client.readiness()


def test_readiness_saved_proof_expires_and_validation_is_pure(setup, monkeypatch):
    view_transport(setup)
    receipt = setup.client.readiness()
    def forbidden(*args, **kwargs):
        raise AssertionError('pure readiness validator performed IO')
    monkeypatch.setattr('pathlib.Path.read_bytes', forbidden)
    monkeypatch.setattr('pathlib.Path.lstat', forbidden)
    monkeypatch.setattr('ml.intraday.scout_authority.client_tls_context', forbidden)
    assert validate_readiness_receipt(receipt, setup.binding, now=setup.env.clock())['ready'] is True
    with pytest.raises(AuthorityProtocolError):
        validate_readiness_receipt(receipt, setup.binding, now=setup.env.clock() + timedelta(seconds=30))
    with pytest.raises(AuthorityProtocolError):
        validate_readiness_receipt(receipt, replace(setup.binding, server_certificate_sha256='e'*64), now=setup.env.clock())


def test_readiness_prestart_sink_is_explicit_and_cannot_validate_order_views(setup):
    view_transport(setup)
    receipt = setup.client.readiness()
    envelope = json.loads(__import__('pathlib').Path(receipt.path).read_text())
    envelope['version'] = 'scout-prestart-authority-receipt-v1'
    prestart = replace(receipt, sha256=canonical_sha256(envelope))
    assert validate_readiness_receipt(prestart, setup.binding, now=setup.env.clock())['ready'] is True
    with pytest.raises(AuthorityProtocolError):
        validate_view_receipt(prestart, setup.binding, operation='readiness', request_id=envelope['request_id'],
            request=prestart.payload['request'], now=setup.env.clock())
    envelope['version'] = 'untrusted-sink'
    with pytest.raises(AuthorityProtocolError):
        validate_readiness_receipt(replace(receipt, sha256=canonical_sha256(envelope)), setup.binding, now=setup.env.clock())


@pytest.mark.parametrize('raw', ['[]', 'null', '{"same":1,"same":2}', '{', '"text"'])
def test_readiness_malformed_saved_payload_raises_typed_protocol_error(setup, raw):
    with pytest.raises(AuthorityProtocolError):
        validate_readiness_receipt(AuthorityReceipt(raw, 'f'*64, 'synthetic-path'), setup.binding, now=setup.env.clock())


def test_readiness_never_returns_before_durable_save_or_after_save_expiry(setup, monkeypatch):
    view_transport(setup)
    original = setup.books.save_receipt
    def fail(**kwargs):
        raise OSError('synthetic private save failure')
    monkeypatch.setattr(setup.books, 'save_receipt', fail)
    with pytest.raises(OSError):
        setup.client.readiness()
    now = setup.env.clock()
    def delay(**kwargs):
        saved = original(**kwargs)
        setup.client.clock = lambda: now + timedelta(seconds=31)
        return saved
    monkeypatch.setattr(setup.books, 'save_receipt', delay)
    with pytest.raises(AuthorityProtocolError):
        setup.client.readiness()


def test_readiness_timeout_consumes_nonce_without_retry_or_claim(setup):
    class Timeout:
        calls = 0
        def call(self, *args):
            self.calls += 1
            raise TimeoutError('synthetic lost view reply')
    transport = Timeout()
    setup.client._transport = transport
    setup.client._nonce_factory = lambda: 'unique-attempt'
    with pytest.raises(TimeoutError):
        setup.client.readiness()
    with pytest.raises(AuthorityProtocolError):
        setup.client.readiness()
    assert transport.calls == 1 and not setup.env.service.path.exists()


def test_binding_requires_enrolled_server_leaf_pin_and_passes_it_to_transport(setup, monkeypatch):
    with pytest.raises(AuthorityProtocolError):
        replace(setup.binding, server_certificate_sha256=None)
    with pytest.raises(AuthorityProtocolError):
        replace(setup.binding, server_certificate_sha256='D'*64)
    seen = {}
    class Client:
        def __init__(self, endpoint, **kwargs):
            seen.update(endpoint=endpoint, **kwargs)
        def call(self, method, path, body):
            return readiness_response(setup.binding, setup.env.clock(), body['request_nonce'])
    monkeypatch.setattr('ml.intraday.scout_authority._verify_binding_files', lambda binding: None)
    monkeypatch.setattr('ml.intraday.scout_authority.client_tls_context', lambda **kwargs: 'synthetic-context')
    monkeypatch.setattr('ml.intraday.scout_authority.AdmissionHTTPSClient', Client)
    setup.client._transport = None
    setup.client.readiness()
    assert seen == dict(endpoint=setup.binding.endpoint, tls_context='synthetic-context',
                        timeout=setup.binding.timeout_seconds, server_certificate_sha256='d'*64)


def accounting_transport(setup, transform=lambda response: response):
    from tests.test_intraday_scout_accounting import accounting_response
    class Views:
        calls = []
        def call(self, method, path, body):
            self.calls.append((method, path, deepcopy(body)))
            assert method == 'POST' and path == '/v1/accounting'
            assert set(body) == {'request_nonce', 'request_id', 'request_sha256', 'session_id'}
            return transform(accounting_response(request(), setup.binding, setup.env.clock(), body['request_nonce']))
    setup.client._transport = Views()
    return setup.client._transport


def test_accounting_posts_original_identity_saves_validated_projection_and_never_claims(setup):
    from ml.intraday.scout_accounting import validate_accounting_receipt
    transport = accounting_transport(setup)
    before = setup.journal.records()
    first = setup.client.accounting(request())
    second = setup.client.accounting(request())
    assert first.response['request_nonce'] != second.response['request_nonce'] and len(transport.calls) == 2
    for _, _, body in transport.calls:
        assert body['request_id'] == request().request_id and body['session_id'] == request().session_id
        assert body['request_sha256'] == request().sha256
    assert validate_accounting_receipt(request(), first, setup.binding, now=setup.env.clock()) == first.response['projection']
    assert first.grant is None and setup.journal.records() == before
    assert setup.env.captures == 0 and not setup.env.service.path.exists()


@pytest.mark.parametrize('change', [
    {'status': 'UNAVAILABLE'}, {'projection': None}, {'snapshot_sha256': '0'*64},
    {'request_id': 'foreign'}, {'request_sha256': 'f'*64}, {'session_id': 'foreign'},
    {'request_nonce': 'old-nonce'}, {'account_fingerprint': 'b'*64},
    {'server_identity': 'foreign-server'}, {'stream': 'atlas-15m'}, {'participant_id': 'pc-original'},
    {'orders_or_commitments_created': False}, {'orders_or_commitments_created': 1},
    {'extra': None}, {'protocol_compatible': False},
])
def test_accounting_rejects_partial_foreign_or_mutated_view_before_save(setup, change):
    accounting_transport(setup, lambda value: value | change)
    with pytest.raises(AuthorityProtocolError):
        setup.client.accounting(request())
    assert list((setup.books.root / 'receipts').glob('*.json')) == []


def test_accounting_unavailable_http_or_local_save_failure_preserves_original_journal(setup, monkeypatch):
    before = setup.journal.records()
    transport = accounting_transport(setup)
    original_call = transport.call
    def unavailable(*args):
        raise OSError('sanitized503 service_unavailable')
    monkeypatch.setattr(transport, 'call', unavailable)
    with pytest.raises(OSError):
        setup.client.accounting(request())
    monkeypatch.setattr(transport, 'call', original_call)
    def save_failure(**kwargs):
        raise OSError('synthetic disk failure')
    monkeypatch.setattr(setup.books, 'save_receipt', save_failure)
    with pytest.raises(OSError):
        setup.client.accounting(request())
    assert setup.journal.records() == before and not setup.env.service.path.exists()


def test_accounting_fails_after_receipt_save_crosses_expiry(setup, monkeypatch):
    accounting_transport(setup)
    original = setup.books.save_receipt
    now = setup.env.clock()
    def slow_save(**kwargs):
        saved = original(**kwargs)
        setup.client.clock = lambda: now + timedelta(seconds=30)
        return saved
    monkeypatch.setattr(setup.books, 'save_receipt', slow_save)
    with pytest.raises(AuthorityProtocolError):
        setup.client.accounting(request())


def test_accounting_rejects_local_scope_or_original_economics_mismatch(setup):
    transport = accounting_transport(setup)
    for changed in (replace(request(), account_fingerprint='f'*64), replace(request(), symbol='DOCU')):
        with pytest.raises(AuthorityProtocolError):
            setup.client.accounting(changed)
    assert transport.calls == []
    with pytest.raises(AuthorityProtocolError):
        setup.client.accounting(replace(request(), limit_price='99'))


def test_exact_reduced_grant_and_saved_receipt(setup):
    result = setup.grant()
    assert result.grant.quantity == 10 < request().quantity
    assert result.response['capacity']['disposition'] == 'smaller_trade_possible'
    saved = json.loads(__import__('pathlib').Path(result.path).read_text(encoding='utf-8'))
    assert canonical_sha256(saved) == result.sha256
    assert saved['payload']['response'] == result.response
    assert result.payload['binding_sha256'] == setup.binding.sha256
    assert setup.client.reserve(request()).response == result.response
    assert setup.env.captures == 1
    mutated = result.response
    mutated['grant']['quantity'] = 100
    assert result.grant.quantity == 10


def test_zero_capacity_and_mutated_duplicate_request(setup):
    setup.env.cash = '0'
    result = setup.client.reserve(request())
    assert result.grant is None and result.response['status'] == 'DECLINED'
    assert setup.env.authority.read_state()['reservations'] == []
    with pytest.raises(AdmissionError, match='DIFFERENT_BYTES'):
        setup.client.reserve(replace(request(), quantity=21))


@pytest.mark.parametrize('case', ['request', 'quantity', 'account', 'epoch', 'future', 'capacity_bool', 'extra'])
def test_mutated_grant_reply_cannot_be_used(setup, case):
    def mutate(path, result):
        if path == '/v1/reserve':
            if case == 'request': result['request_sha256'] = 'b'*64
            if case == 'quantity': result['grant']['quantity'] += 1
            if case == 'account': result['grant']['account_fingerprint'] = 'b'*64
            if case == 'epoch': result['grant']['authority_epoch'] = True
            if case == 'future': result['grant']['issued_at'] = '2026-10-12T17:29:00+00:00'
            if case == 'capacity_bool': result['capacity']['available_shares'] = True
            if case == 'extra': result['send_authorized'] = True
        return result
    setup.transport.mutate = mutate
    with pytest.raises(ValueError):
        setup.client.reserve(request())
    assert setup.record()['state'] == 'PREPARED'


def test_authorization_uses_original_grant_epoch_and_fresh_operation_epoch(setup):
    receipt = setup.authorized()
    record = setup.record()
    assert receipt.response['claim']['authority_epoch'] > record['grant']['authority_epoch']
    assert record['send_token'] not in json.dumps(receipt.payload)
    assert receipt.response['claim']['claim_id'] == claim_id(record)
    assert validate_send_receipt(record, receipt, setup.binding, now=setup.env.clock()) is True
    with pytest.raises(AuthorityProtocolError, match='cannot be retried'):
        setup.client.authorize_send(record)
    status = setup.client.status(record)
    assert status.response['status'] == 'SUBMITTING' and status.response['native_status'] == 'UNKNOWN'
    assert status.response['send_authorized'] is False
    with pytest.raises(AuthorityProtocolError):
        validate_send_receipt(record, status, setup.binding, now=setup.env.clock())
    with pytest.raises(AuthorityProtocolError):
        validate_send_receipt(record, receipt, setup.binding,
                              now=setup.env.clock()+timedelta(hours=1))
    with pytest.raises(AuthorityProtocolError):
        validate_send_receipt(record, receipt, replace(setup.binding, endpoint='https://other.test'), now=setup.env.clock())


def test_lost_authorization_reply_never_rearms(setup):
    record = setup.claimed()
    setup.transport.drop = '/v1/authorize-send'
    with pytest.raises(TimeoutError):
        setup.client.authorize_send(record)
    with pytest.raises(AuthorityProtocolError, match='cannot be retried'):
        setup.client.authorize_send(record)
    assert len([c for c in setup.transport.calls if c[1] == '/v1/authorize-send']) == 1
    status = setup.client.status(record)
    assert status.response['claim']['claim_id'] == claim_id(record)
    assert status.response['send_authorized'] is False


def test_authorization_receipt_must_be_saved_before_return(setup, monkeypatch):
    record = setup.claimed()
    original = setup.books.save_receipt
    def save(**kwargs):
        if kwargs['kind'] == 'authority:authorize_send':
            raise OSError('synthetic disk full')
        return original(**kwargs)
    monkeypatch.setattr(setup.books, 'save_receipt', save)
    with pytest.raises(OSError, match='disk full'):
        setup.client.authorize_send(record)
    assert setup.client.status(record).response['status'] == 'SUBMITTING'
    with pytest.raises(AuthorityProtocolError):
        setup.client.authorize_send(record)


@pytest.mark.parametrize('case', ['claim', 'reservation', 'epoch', 'future', 'expiry', 'status'])
def test_authorization_rejects_wrong_claim_receipt(setup, case):
    record = setup.claimed()
    def mutate(path, result):
        if path == '/v1/authorize-send':
            if case == 'claim': result['claim']['claim_id'] = 'other'
            if case == 'reservation': result['claim']['reservation_id'] = 'other'
            if case == 'epoch': result['claim']['authority_epoch'] = 0
            if case == 'future': result['claim']['issued_at'] = '2026-10-12T17:29:00+00:00'
            if case == 'expiry': result['claim']['expires_at'] = '2026-10-12T17:31:00+00:00'
            if case == 'status': result['status'] = 'SUBMITTING'
        return result
    setup.transport.mutate = mutate
    with pytest.raises(AuthorityProtocolError):
        setup.client.authorize_send(record)


def test_sending_is_not_unknown_evidence_and_never_implicit_release(setup):
    record = setup.claimed()
    with pytest.raises(AuthorityProtocolError):
        setup.client.unknown(record)
    with pytest.raises(AuthorityProtocolError):
        setup.client.release_unsent(record)
    assert setup.client.status(record).response['status'] == 'RESERVED'


def test_durable_unknown_can_request_release_only_after_authority_unclaimed_status(setup):
    record = setup.claimed()
    setup.journal.outcome(request().request_id, record['send_token'], None)
    record = setup.record()
    with pytest.raises(AuthorityProtocolError):
        setup.client.release_unsent(record)
    status = setup.client.status(record)
    receipt = setup.client.release_unsent(record, status_receipt=status)
    assert receipt.response['status'] == 'NOT_SUBMITTED' and receipt.response['capacity_released'] is True
    assert setup.env.authority.reservation(record['grant']['reservation_id']).status == 'NOT_SUBMITTED'


def test_authority_rechecks_claim_after_prior_unclaimed_status(setup):
    record = setup.claimed()
    unclaimed = setup.client.status(record)
    setup.client.authorize_send(record)
    setup.journal.outcome(request().request_id, record['send_token'], None)
    with pytest.raises((AuthorityError, AdmissionError)):
        setup.client.release_unsent(setup.record(), status_receipt=unclaimed)
    assert setup.env.authority.reservation(record['grant']['reservation_id']).status == 'SUBMITTING'


def test_known_unknown_retains_commitment_and_stable_events(setup):
    setup.authorized()
    record = setup.record()
    setup.journal.outcome(request().request_id, record['send_token'], None)
    record = setup.record()
    first = setup.client.unknown(record)
    second = setup.client.unknown(record)
    assert first.response == second.response and first.response['status'] == 'UNKNOWN'
    assert first.payload['request'] == second.payload['request']
    with pytest.raises(AuthorityProtocolError):
        setup.client.release_unsent(record, status_receipt=setup.client.status(record))


def test_exact_acceptance_partial_fill_cancel_intent_and_terminal_receipts(setup):
    accepted = setup.accepted()
    record = setup.record()
    assert accepted.response['broker_order_id'] == '201' and accepted.response['status'] == 'SUBMITTED'
    setup.env.clock.tick()
    evidence = OrderEvidence('partial-one', record['grant']['reservation_id'], setup.binding.account_fingerprint,
        setup.env.clock().isoformat(), '201', 'PARTIAL', 10, 2, 8,
        (FillEvidence('fill-one', 2, '10', setup.env.clock().isoformat()),), broker_status='PARTIALLY_FILLED')
    setup.journal.observe_order(request().request_id, evidence, as_of=setup.env.clock().isoformat())
    result = setup.client.order_status(record, evidence)
    assert result.response['filled_quantity'] == 2
    wire = result.payload['request']['order_evidence']
    assert wire['complete'] is True and 'broker_status' not in wire and 'broker_status_description' not in wire
    assert wire['reservation_id'] == record['grant']['reservation_id']
    cancel = setup.client.cancel_intent(record, reason='manual-stop')
    assert cancel.response['capacity_released'] is False and cancel.response['broker_action_performed'] is False
    assert setup.env.authority.reservation(record['grant']['reservation_id']).status == 'PARTIAL'
    setup.env.clock.tick()
    terminal = replace(evidence, evidence_id='cancelled-one', status='CANCELLED', remaining_quantity=0,
                       observed_at=setup.env.clock().isoformat())
    setup.journal.observe_order(request().request_id, terminal, as_of=setup.env.clock().isoformat())
    assert setup.client.order_status(record, terminal).response['status'] == 'CANCELLED'
    # Exact immutable old event retries retain the old accepted response.
    assert setup.client.order_status(record, evidence).response == result.response


def test_lost_event_reply_retries_exact_id_and_body(setup):
    setup.authorized()
    record = setup.record()
    setup.journal.outcome(request().request_id, record['send_token'], '201')
    setup.transport.drop = '/v1/evidence'
    with pytest.raises(TimeoutError):
        setup.client.accepted(setup.record(), event_id='accepted-private-event')
    receipt = setup.client.accepted(setup.record(), event_id='accepted-private-event')
    calls = [c for c in setup.transport.calls if c[1] == '/v1/evidence']
    assert calls[0][2] == calls[1][2]
    assert receipt.response['event_sha256'] == canonical_sha256(calls[0][2])
    attempts = [json.loads(p.read_text()) for p in (setup.books.root/'receipts').glob('*.json')]
    assert any(a['kind'] == 'authority:attempt:evidence' and a['payload']['request'] == calls[0][2] for a in attempts)


def test_mutated_duplicate_event_is_rejected_by_authority(setup):
    setup.authorized()
    record = setup.record()
    setup.journal.outcome(request().request_id, record['send_token'], None)
    setup.client.unknown(setup.record(), event_id='one-immutable-event')
    setup.journal.outcome(request().request_id, record['send_token'], '201')
    with pytest.raises(AdmissionError, match='EVENT_ID_REUSED'):
        setup.client.accepted(setup.record(), event_id='one-immutable-event')
    assert setup.env.authority.reservation(record['grant']['reservation_id']).broker_order_id is None


@pytest.mark.parametrize('change', [
    {'event_id': 'another-event'}, {'event_sha256': 'b'*64}, {'broker_order_id': '202'},
    {'filled_quantity': True}, {'status': []}, {'status': 'FILLED'},
])
def test_malformed_evidence_ack_cannot_acknowledge_private_event(setup, change):
    setup.authorized()
    record = setup.record()
    setup.journal.outcome(request().request_id, record['send_token'], '201')
    def mutate(path, result):
        return {**result, **change} if path == '/v1/evidence' else result
    setup.transport.mutate = mutate
    with pytest.raises(AuthorityProtocolError):
        setup.client.accepted(setup.record())
    assert setup.record()['state'] == 'ACCEPTED'


def test_unsaved_event_attempt_prevents_network_mutation(setup, monkeypatch):
    setup.authorized()
    record = setup.record()
    setup.journal.outcome(request().request_id, record['send_token'], '201')
    def fail(**kwargs):
        raise OSError('synthetic attempt persistence failure')
    monkeypatch.setattr(setup.books, 'save_receipt', fail)
    before = len(setup.transport.calls)
    with pytest.raises(OSError):
        setup.client.accepted(setup.record())
    assert len(setup.transport.calls) == before
    assert setup.env.authority.reservation(record['grant']['reservation_id']).broker_order_id is None


@pytest.mark.parametrize('terminal', [False, True])
def test_order_evidence_ack_cannot_regress_fills_or_demote_terminal(setup, terminal):
    setup.accepted()
    record = setup.record()
    setup.env.clock.tick()
    evidence = OrderEvidence('evidence-ack', record['grant']['reservation_id'], setup.binding.account_fingerprint,
        setup.env.clock().isoformat(), '201', 'CANCELLED' if terminal else 'PARTIAL', 10, 2, 0 if terminal else 8,
        (FillEvidence('fill-ack', 2, '10', setup.env.clock().isoformat()),))
    def mutate(path, result):
        if path == '/v1/evidence':
            result.update(status='PARTIAL' if terminal else 'WORKING', filled_quantity=2 if terminal else 0)
        return result
    setup.transport.mutate = mutate
    with pytest.raises(AuthorityProtocolError):
        setup.client.order_status(record, evidence)


def test_status_rejects_cross_order_or_rearm_response(setup):
    setup.accepted()
    def mutate(path, result):
        if path == '/v1/status': result['send_authorized'] = True
        return result
    setup.transport.mutate = mutate
    with pytest.raises(AuthorityProtocolError):
        setup.client.status(setup.record())
    def mutate_order(path, result):
        if path == '/v1/status': result['broker_order_id'] = '202'
        return result
    setup.transport.mutate = mutate_order
    with pytest.raises(AuthorityProtocolError):
        setup.client.status(setup.record())


def test_cancel_ack_cannot_be_misrepresented_as_terminal_or_capacity_release(setup):
    setup.accepted()
    def mutate(path, result):
        if path == '/v1/cancel-intent': result['capacity_released'] = True
        return result
    setup.transport.mutate = mutate
    with pytest.raises(AuthorityProtocolError):
        setup.client.cancel_intent(setup.record(), reason='manual-stop')


def test_unsent_local_state_alone_does_not_release_capacity(setup):
    setup.grant()
    setup.journal.revoke_unsent(request().request_id, reason='manual-stop')
    before = setup.env.authority.reservation(setup.record()['grant']['reservation_id'])
    assert before.status == 'RESERVED'
    result = setup.client.release_unsent(setup.record())
    assert result.response['status'] == 'NOT_SUBMITTED'


def test_outside_scope_record_and_request_are_rejected_without_calls(setup):
    with pytest.raises(AuthorityProtocolError):
        setup.client.reserve(replace(request(), symbol='AAPL'))
    bad = setup.record()
    bad['request_sha256'] = 'b'*64
    with pytest.raises(AuthorityProtocolError):
        setup.client.status(bad)
    assert setup.transport.calls == []


def negative_proof(setup):
    setup.env.clock.value += timedelta(minutes=20)
    return setup.client.reserve(request())


@pytest.mark.parametrize('recovery', ['reserve', 'status'])
@pytest.mark.parametrize('interrupted_before_plan', [False, True])
def test_exact_negative_admission_is_saved_and_resolves_only_original_grantless_record(setup, recovery, interrupted_before_plan):
    if interrupted_before_plan:
        def failed_refresh(operation):
            raise RuntimeError('synthetic crash before admission plan')
        setup.env.service.refresh = failed_refresh
        with pytest.raises(RuntimeError):
            setup.client.reserve(request())
        assert setup.client.status(setup.record()).response['status'] == 'PENDING'
    setup.env.clock.value += timedelta(minutes=20)
    if recovery == 'status' and not interrupted_before_plan:
        with pytest.raises(AdmissionError, match='UNKNOWN_REQUEST'):
            setup.client.status(setup.record())
        setup.client.reserve(request())
    receipt = setup.client.reserve(request()) if recovery == 'reserve' else setup.client.status(setup.record())
    assert receipt.response['status'] == 'DECLINED' and receipt.grant is None
    assert receipt.response['reason'] == 'expired_before_admission'
    assert receipt.response['terminal'] is True and receipt.response['capacity_released'] is False
    assert validate_negative_admission_receipt(setup.record(), receipt, setup.binding, now=setup.env.clock())
    assert setup.record()['state'] == 'PREPARED'  # The effect-free client does not mutate the journal.
    saved = json.loads(__import__('pathlib').Path(receipt.path).read_text())
    assert canonical_sha256(saved) == receipt.sha256
    assert saved['payload']['response'] == receipt.response
    assert setup.env.authority.read_state()['reservations'] == []
    assert setup.env.ledger.snapshot().reservations == () and setup.env.captures == 0
    assert setup.client.reserve(request()).response == setup.client.status(setup.record()).response == receipt.response
    setup.journal.revoke_unsent(request().request_id, reason='verified-expired-before-admission')
    assert validate_negative_admission_receipt(setup.record(), receipt, setup.binding, now=setup.env.clock())
    with pytest.raises(AuthorityProtocolError):
        validate_send_receipt(setup.record(), receipt, setup.binding, now=setup.env.clock())


@pytest.mark.parametrize('operation', ['reserve', 'status'])
@pytest.mark.parametrize('field,value', [
    ('version', 'v-next'), ('server_identity', 'foreign-server'), ('account_fingerprint', 'b'*64),
    ('participant_id', 'pc-original'), ('stream', 'atlas-15m'), ('request_id', 'another-request'),
    ('request_sha256', 'b'*64), ('session_id', 'another-session'),
    ('valid_until', '2026-10-12T17:29:00+00:00'),
    ('canonical_account_absent', False), ('canonical_native_absent', False), ('send_claim_absent', False),
    ('canonical_account_absent', 1), ('canonical_native_absent', 1), ('send_claim_absent', 1),
    ('decided_at', '2026-10-12T17:29:59+00:00'), ('decided_at', '2026-10-13T00:00:00+00:00'),
    ('decided_at', '2026-10-12T17:40:00'), ('decided_at', None),
])
def test_negative_proof_rejects_mutated_scope_absence_and_time_even_with_new_hash(setup, operation, field, value):
    negative_proof(setup)
    before = set((setup.books.root/'receipts').glob('*.json'))
    def mutate(path, result):
        if path == '/v1/' + operation:
            result['negative_admission_proof'][field] = value
            result['negative_admission_proof_sha256'] = canonical_sha256(result['negative_admission_proof'])
        return result
    setup.transport.mutate = mutate
    with pytest.raises(AuthorityProtocolError):
        setup.client.reserve(request()) if operation == 'reserve' else setup.client.status(setup.record())
    assert set((setup.books.root/'receipts').glob('*.json')) == before
    assert setup.record()['state'] == 'PREPARED'


@pytest.mark.parametrize('change', [
    {'negative_admission_proof_sha256': 'b'*64}, {'negative_admission_proof': {}},
    {'status': 'GRANTED'}, {'request_id': 'another'}, {'session_id': 'another'},
    {'reason': 'interrupted_before_paired_admission'}, {'terminal': 1}, {'admitted': 0},
    {'send_authorized': 0}, {'capacity_released': 0}, {'capacity_released': True}, {'unknown': True},
])
def test_negative_admission_requires_exact_terminal_response(setup, change):
    negative_proof(setup)
    setup.transport.mutate = lambda path, result: {**result, **change} if path == '/v1/status' else result
    with pytest.raises(AuthorityProtocolError):
        setup.client.status(setup.record())


@pytest.mark.parametrize('part', ['proof-field', 'response-field'])
def test_negative_admission_requires_every_published_field(setup, part):
    negative_proof(setup)
    def mutate(path, result):
        if path == '/v1/status':
            if part == 'proof-field':
                del result['negative_admission_proof']['send_claim_absent']
                result['negative_admission_proof_sha256'] = canonical_sha256(result['negative_admission_proof'])
            else:
                del result['negative_admission_proof']
        return result
    setup.transport.mutate = mutate
    with pytest.raises(AuthorityProtocolError):
        setup.client.status(setup.record())


@pytest.mark.parametrize('operation', ['reserve', 'status'])
def test_negative_admission_receipt_must_commit_before_return(setup, monkeypatch, operation):
    negative_proof(setup)
    original = setup.books.save_receipt
    def fail(**kwargs):
        if kwargs['kind'] == 'authority:' + operation:
            raise OSError('synthetic private receipt commit failure')
        return original(**kwargs)
    monkeypatch.setattr(setup.books, 'save_receipt', fail)
    with pytest.raises(OSError):
        setup.client.reserve(request()) if operation == 'reserve' else setup.client.status(setup.record())
    assert setup.record()['state'] == 'PREPARED'
    monkeypatch.setattr(setup.books, 'save_receipt', original)
    receipt = setup.client.reserve(request()) if operation == 'reserve' else setup.client.status(setup.record())
    assert validate_negative_admission_receipt(setup.record(), receipt, setup.binding, now=setup.env.clock())


def test_lost_negative_reply_is_recoverable_with_identical_proof(setup):
    setup.env.clock.value += timedelta(minutes=20)
    setup.transport.drop = '/v1/reserve'
    with pytest.raises(TimeoutError):
        setup.client.reserve(request())
    assert setup.record()['state'] == 'PREPARED'
    first = setup.client.status(setup.record())
    setup.env.clock.value += timedelta(hours=1)
    second = setup.client.reserve(request())
    assert first.response == second.response
    assert validate_negative_admission_receipt(setup.record(), second, setup.binding, now=setup.env.clock())
    assert not any(call[1] in {'/v1/authorize-send', '/v1/release-unsent', '/v1/evidence'} for call in setup.transport.calls)


def test_clock_rollback_keeps_future_negative_proof_fail_closed_without_rearming(setup):
    receipt = negative_proof(setup)
    setup.client.clock = lambda: setup.env.clock() - timedelta(seconds=10)
    with pytest.raises(AuthorityProtocolError, match='future-dated'):
        setup.client.status(setup.record())
    with pytest.raises(AuthorityProtocolError, match='future-dated'):
        validate_negative_admission_receipt(setup.record(), receipt, setup.binding, now=setup.client.clock())
    assert setup.record()['state'] == 'PREPARED'
    setup.client.clock = setup.env.clock
    assert setup.client.status(setup.record()).response == receipt.response


@pytest.mark.parametrize('state', ['GRANTED', 'NOT_SENT', 'SENDING', 'UNKNOWN', 'ACCEPTED'])
def test_negative_proof_cannot_override_a_locally_known_grant_or_send(setup, state):
    receipt = negative_proof(setup)
    req = request()
    granted = Grant('synthetic-old-grant', req.request_id, req.sha256, req.account_fingerprint,
        req.session_id, 1, 10, 'old-reservation', req.issued_at, req.valid_until)
    setup.journal.grant(granted)
    if state == 'NOT_SENT':
        setup.journal.revoke_unsent(req.request_id, reason='manual-stop')
    if state in {'SENDING', 'UNKNOWN', 'ACCEPTED'}:
        token = setup.journal.claim_send(req.request_id)
        if state != 'SENDING': setup.journal.outcome(req.request_id, token, '201' if state == 'ACCEPTED' else None)
    with pytest.raises(AuthorityProtocolError):
        validate_negative_admission_receipt(setup.record(), receipt, setup.binding, now=setup.env.clock())
    with pytest.raises(AuthorityProtocolError, match='contradicts'):
        setup.client.status(setup.record())
    assert setup.record()['state'] == state


def test_negative_receipt_binding_digest_and_original_operation_are_required(setup):
    receipt = negative_proof(setup)
    for changed_binding in (replace(setup.binding, endpoint='https://foreign.test'),
                            replace(setup.binding, server_identity='foreign-server')):
        with pytest.raises(AuthorityProtocolError):
            validate_negative_admission_receipt(setup.record(), receipt, changed_binding, now=setup.env.clock())
    with pytest.raises(AuthorityProtocolError):
        validate_negative_admission_receipt(setup.record(), replace(receipt, sha256='b'*64), setup.binding, now=setup.env.clock())
    for key, value in [('operation', 'authorize_send'), ('request', {'request_id': 'other'}),
                       ('received_at', '2026-10-13T00:00:00+00:00')]:
        payload = receipt.payload
        payload[key] = value
        # Rehashing a local receipt cannot make an incompatible operation or time valid.
        saved = setup.books.save_receipt(kind='authority:' + value if key == 'operation' else 'authority:reserve',
                                         request_id=request().request_id, payload=payload)
        forged = replace(receipt, _json=json.dumps(payload), sha256=saved['sha256'], path=saved['path'])
        with pytest.raises(AuthorityProtocolError):
            validate_negative_admission_receipt(setup.record(), forged, setup.binding, now=setup.env.clock())
