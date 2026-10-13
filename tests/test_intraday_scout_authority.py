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
    AuthorityProtocolError, ScoutAuthorityBinding, ScoutAuthorityClient,
    claim_id, load_binding, validate_send_receipt, validate_negative_admission_receipt,
)
from ml.intraday.scout_books import ScoutBooks
from ml.intraday.scout_protocol import Grant, canonical_sha256
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
        server_ca_sha256=hashlib.sha256(b'ca-public').hexdigest())


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
    with pytest.raises(AuthorityProtocolError, match='readiness route'):
        setup.client.readiness()


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
