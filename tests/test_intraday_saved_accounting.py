"""Synthetic retained accounting bytes; no authority client or legacy imports.

The envelope fixtures follow the historical accounting regression format. They
write temporary fixtures directly so this suite can run with common-main source
alone, independently of the unpublished legacy writer/client test fixtures.
"""
import builtins
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import sqlite3
from types import SimpleNamespace

import pytest

from ml.intraday import saved_accounting as saved
from ml.intraday import saved_authority as authority
from ml.intraday.scout_journal import JournalError
from ml.intraday.scout_protocol import Request, canonical_sha256


NOW = datetime(2026, 10, 12, 16, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Historical evidence must not connect or listen')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'bind', forbidden)


@pytest.fixture
def book(tmp_path):
    request = Request(
        request_id='saved-request:1', account_fingerprint='a' * 64,
        session_id='saved-session:1', symbol='DOCU', side='BUY', quantity=10,
        limit_price='42.5', forecast_id='b' * 64, forecast_sha256='b' * 64,
        snapshot_sha256='c' * 64, sizing_sha256='d' * 64,
        reference_at='2026-10-12T14:00:00Z', issued_at='2026-10-12T14:15:01Z',
        target_close_at='2026-10-12T14:45:00Z', valid_until='2026-10-12T14:16:00Z',
    )
    binding = authority.ScoutAuthorityBinding(
        endpoint='https://saved.example.test:8443', server_identity='saved-atlas:1',
        account_fingerprint=request.account_fingerprint, symbols=('DOCU', 'DBX'),
        client_certificate=str(tmp_path / 'client.pem'), private_key=str(tmp_path / 'client.key'),
        server_ca=str(tmp_path / 'ca.pem'), client_certificate_sha256='b' * 64,
        server_ca_sha256='c' * 64, server_certificate_sha256='d' * 64,
    )
    root = tmp_path / 'private-history'
    name = canonical_sha256({'request_id': request.request_id})
    return SimpleNamespace(root=root, request=request, binding=binding,
                           index=root / 'accounting' / (name + '.json'), name=name)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False),
                    encoding='utf-8')


def projection(book):
    req = book.request
    return dict(
        request={name: getattr(req, name) for name in
                 ('symbol', 'side', 'quantity', 'limit_price', 'forecast_id')},
        account_reservation=dict(reservation_id='reservation:1', status='RESERVED', quantity=3,
            filled_quantity=0, broker_order_id=None, created_at=req.issued_at, last_evidence_at=None),
        native_reservation=dict(reservation_id='native:1', allocation_id='allocation:1',
            status='RESERVED', quantity=3, filled_quantity=0, broker_order_id=None, last_evidence_at=None),
        allocations=[dict(allocation_id='allocation:1', symbol=req.symbol, horizon='15m',
            forecast_id=req.forecast_id, target_start=req.reference_at, target_end=req.target_close_at,
            status='ACTIVE', purchase_fills=[], observed_inventory=[])],
        fills=[], sell_slices=([dict(allocation_id='allocation:1', quantity=3, ordinal=0)]
                               if req.side == 'SELL' else []),
        fill_attribution=[], totals=dict(filled_quantity=0, gross_cost='0', gross_proceeds='0',
            cost_basis=None, realized_pnl=None, basis_status='unknown', fees=None),
        provenance=dict(journal_request_sha256=req.sha256, authority_request_sha256='e' * 64,
            native_request_sha256='f' * 64, account_order_evidence_sha256=[], native_order_evidence_sha256=[]),
    )


def with_fill(book, quantity=2):
    value = projection(book)
    at = (NOW - timedelta(seconds=1)).isoformat()
    for name in ('account_reservation', 'native_reservation'):
        value[name].update(status='FILLED' if quantity == 3 else 'PARTIAL', filled_quantity=quantity,
                           broker_order_id='123456', last_evidence_at=at)
    fill = dict(fill_id='fill:1', quantity=quantity, price='42.5', executed_at=at,
                native_reservation_id='native:1', account_reservation_id='reservation:1')
    value['fills'] = [fill]
    if book.request.side == 'BUY':
        value['allocations'][0]['purchase_fills'] = [
            {key: item for key, item in fill.items() if key != 'account_reservation_id'}]
    else:
        value['fill_attribution'] = [dict(fill_id='fill:1', allocation_id='allocation:1',
                                        quantity=quantity, slice_ordinal=0)]
    gross = {1: '42.5', 2: '85', 3: '127.5'}[quantity]
    value['totals'].update(filled_quantity=quantity,
        gross_cost=gross if book.request.side == 'BUY' else '0',
        gross_proceeds=gross if book.request.side == 'SELL' else '0')
    return value


def receipt_file(book, kind, payload):
    envelope = dict(version='scout-session-books-v1', account_fingerprint=book.request.account_fingerprint,
                    participant_id='pc-new', stream='scout-15m', kind=kind,
                    request_id=book.request.request_id, payload=payload)
    digest = canonical_sha256(envelope)
    path = book.root / 'receipts' / (digest + '.json')
    write_json(path, envelope)
    return dict(sha256=digest, path=str(path))


def observation(book, ordinal=0, *, value=None, response_changes=None, at=None):
    req, binding = book.request, book.binding
    now = NOW + timedelta(seconds=ordinal) if at is None else at
    content = projection(book) if value is None else deepcopy(value)
    body = dict(request_nonce='saved-nonce:' + str(ordinal), request_id=req.request_id,
                request_sha256=req.sha256, session_id=req.session_id)
    response = dict(version='intraday-canonical-accounting-v1',
        protocol_version='intraday-shared-admission-v1', server_identity=binding.server_identity,
        participant_id='pc-new', stream='scout-15m', account_fingerprint=req.account_fingerprint,
        request_nonce=body['request_nonce'], observed_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=30)).isoformat(), authenticated=True,
        protocol_compatible=True, read_only=True, orders_or_commitments_created=0,
        request_id=req.request_id, request_sha256=req.sha256, session_id=req.session_id,
        status='AVAILABLE', snapshot_sha256=canonical_sha256(content), projection=content)
    response.update(response_changes or {})
    payload = dict(version='scout-atlas-authority-receipt-v1', binding_sha256=binding.sha256,
        operation='accounting', request=body, response=response,
        requested_at=now.isoformat(), received_at=now.isoformat())
    reference = receipt_file(book, 'authority:accounting', payload)
    return dict(snapshot_sha256=response['snapshot_sha256'], authority_receipt=reference,
                binding_sha256=binding.sha256, observed_at=response['observed_at'])


def index_value(book, observations, *, history=None, legacy=False):
    value = dict(version='scout-canonical-accounting-books-v1' if legacy else
                 'scout-canonical-accounting-books-v2',
        account_fingerprint=book.request.account_fingerprint, participant_id='pc-new', stream='scout-15m',
        request=book.request.payload(), request_sha256=book.request.sha256,
        bindings={book.binding.sha256: book.binding.payload()} if observations else {},
        snapshots=deepcopy(observations), latest_snapshot=deepcopy(observations[-1]) if observations else None,
        last_attempt=deepcopy(observations[-1]['authority_receipt']) if observations else None,
        pending=not bool(observations))
    if not legacy:
        value.update(history=history, snapshot_count=len(observations) + (history['count'] if history else 0))
    return value


def publish_index(book, value):
    write_json(book.index, dict(accounting=value, sha256=canonical_sha256(value)))


def seed(book, count=1, *, legacy=False):
    observations = [observation(book, ordinal) for ordinal in range(count)]
    tail, previous = observations[:], None
    while not legacy and len(tail) > 128:
        page = dict(version='scout-canonical-accounting-page-v1',
            account_fingerprint=book.request.account_fingerprint, request_sha256=book.request.sha256,
            previous=previous, observations=tail[:128])
        digest = canonical_sha256(page)
        path = book.root / 'accounting-history' / book.name / (digest + '.json')
        write_json(path, dict(page=page, sha256=digest))
        pages = previous['pages'] + 1 if previous else 1
        previous = dict(sha256=digest, path=str(path), pages=pages, count=128 * pages)
        tail = tail[128:]
    value = index_value(book, tail, history=previous, legacy=legacy)
    publish_index(book, value)
    return value, observations


def read(book, **changes):
    arguments = dict(account_fingerprint=book.request.account_fingerprint,
                     request=book.request.payload(), check_deadline=lambda: None)
    arguments.update(changes)
    return saved.read_saved_accounting(book.root, **arguments)


def audit(book, **changes):
    arguments = dict(account_fingerprint=book.request.account_fingerprint,
                     requests=[book.request.payload()], check_deadline=lambda: None)
    arguments.update(changes)
    return saved.audit_saved_accounting(book.root, **arguments)


def fingerprint(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file()}


def test_absent_history_is_pending_without_creating_files(book):
    result = read(book)
    assert result['pending'] and result['latest_snapshot'] is None and result['snapshot_count'] == 0
    assert audit(book) == dict(requests=1, indexed_requests=0, pending_requests=0, snapshots=0)
    assert not book.root.exists()


@pytest.mark.parametrize('count', [1, 128, 129, 256, 257])
def test_complete_history_replays_real_128_observation_pages(book, count):
    value, observations = seed(book, count)
    before = fingerprint(book.root)
    assert read(book) == value
    assert audit(book) == dict(requests=1, indexed_requests=1, pending_requests=0, snapshots=count)
    assert read(book)['latest_snapshot'] == observations[-1]
    assert fingerprint(book.root) == before
    assert len(value['snapshots']) <= 128


def test_result_is_detached_from_retained_bytes(book):
    expected, _ = seed(book)
    changed = read(book)
    changed['bindings'].clear()
    changed['snapshots'][0]['authority_receipt']['sha256'] = 'f' * 64
    assert read(book) == expected


@pytest.mark.parametrize('legacy', [False, True])
def test_pending_attempt_preserves_prior_snapshot_and_original_version(book, legacy):
    value, _ = seed(book, legacy=legacy)
    value['last_attempt'] = receipt_file(book, 'accounting:pending', dict(
        version=value['version'], request_sha256=book.request.sha256, session_id=book.request.session_id,
        reason_code='view-unavailable', observed_at=(NOW + timedelta(minutes=2)).isoformat()))
    value['pending'] = True
    publish_index(book, value)
    before = fingerprint(book.root)
    assert read(book) == value
    assert audit(book)['pending_requests'] == 1
    assert fingerprint(book.root) == before


def test_v1_index_is_never_migrated_by_saved_readers(book):
    value, _ = seed(book, 3, legacy=True)
    before = fingerprint(book.root)
    assert read(book) == value
    assert audit(book)['snapshots'] == 3
    assert fingerprint(book.root) == before
    assert 'history' not in read(book) and 'snapshot_count' not in read(book)


@pytest.mark.parametrize('damage', [
    'digest', 'account', 'request', 'request-sha', 'missing', 'pending', 'duplicate',
    'no-binding', 'latest', 'count', 'no-latest', 'foreign-path', 'binding-hash',
])
def test_rehashed_index_corruption_cannot_replace_original_identity(book, damage):
    value, _ = seed(book)
    if damage == 'account': value['account_fingerprint'] = 'e' * 64
    elif damage == 'request': value['request']['quantity'] = 11
    elif damage == 'request-sha': value['request_sha256'] = 'e' * 64
    elif damage == 'missing': value.pop('pending')
    elif damage == 'pending': value['pending'] = True
    elif damage == 'duplicate':
        value['snapshots'].append(deepcopy(value['snapshots'][0])); value['snapshot_count'] = 2
    elif damage == 'no-binding': value['bindings'].clear()
    elif damage == 'latest': value['latest_snapshot']['observed_at'] = '2000-01-01T00:00:00Z'
    elif damage == 'count': value['snapshot_count'] = 2
    elif damage == 'no-latest': value['latest_snapshot'] = None
    elif damage == 'foreign-path': value['snapshots'][0]['authority_receipt']['path'] = str(book.root / 'other.json')
    elif damage == 'binding-hash': value['bindings'][book.binding.sha256]['server_certificate_sha256'] = 'e' * 64
    publish_index(book, value)
    if damage == 'digest':
        wrapper = json.loads(book.index.read_text()); wrapper['sha256'] = 'f' * 64
        write_json(book.index, wrapper)
    before = fingerprint(book.root)
    with pytest.raises(JournalError): read(book)
    with pytest.raises(JournalError): audit(book)
    assert fingerprint(book.root) == before


@pytest.mark.parametrize('changes', [
    {'account_fingerprint': 'e' * 64}, {'request_id': 'foreign-request:1'},
    {'session_id': 'foreign-session:1'}, {'symbol': 'DBX'}, {'quantity': 11},
])
def test_foreign_original_requests_cannot_inherit_accounting(book, changes):
    seed(book)
    foreign = replace(book.request, **changes)
    if 'request_id' in changes:
        assert read(book, request=foreign.payload())['pending']
    else:
        with pytest.raises(JournalError): read(book, request=foreign.payload())
    with pytest.raises(JournalError): audit(book, requests=[foreign.payload()])


@pytest.mark.parametrize('damage', ['missing-page', 'page-content', 'page-count', 'old-receipt', 'duplicate-receipt'])
def test_old_history_is_fully_revalidated(book, damage):
    value, observations = seed(book, 129)
    if damage == 'missing-page': Path(value['history']['path']).unlink()
    elif damage == 'page-content':
        path = Path(value['history']['path']); wrapper = json.loads(path.read_text())
        wrapper['page']['observations'][0]['observed_at'] = '2000-01-01T00:00:00Z'
        write_json(path, wrapper)
    elif damage == 'page-count':
        value['history']['count'] = 127; publish_index(book, value)
    elif damage == 'old-receipt':
        path = Path(observations[0]['authority_receipt']['path']); path.write_bytes(path.read_bytes() + b'changed')
    else:
        value['snapshots'][0] = deepcopy(observations[0])
        value['latest_snapshot'] = deepcopy(observations[0])
        value['last_attempt'] = deepcopy(observations[0]['authority_receipt'])
        publish_index(book, value)
    with pytest.raises(JournalError): read(book)
    with pytest.raises(JournalError): audit(book)


@pytest.mark.parametrize('reader', [read, audit])
@pytest.mark.parametrize('stop', [1, 6, 70])
def test_deadline_interrupts_full_history_without_partial_success(book, reader, stop):
    seed(book, 129)
    before, calls = fingerprint(book.root), []
    def deadline():
        calls.append(None)
        if len(calls) == stop: raise TimeoutError('synthetic deadline')
    with pytest.raises(TimeoutError, match='synthetic deadline'):
        reader(book, check_deadline=deadline)
    assert len(calls) == stop and fingerprint(book.root) == before


@pytest.mark.parametrize('reader', [read, audit])
def test_saved_read_requires_explicit_deadline(book, reader):
    with pytest.raises(JournalError): reader(book, check_deadline=None)
    assert not book.root.exists()


def test_interrupted_receipt_is_audited_without_promoting_it_to_snapshot(book):
    observation(book)
    before = fingerprint(book.root)
    assert read(book)['latest_snapshot'] is None
    assert audit(book) == dict(requests=1, indexed_requests=0, pending_requests=0, snapshots=0)
    with pytest.raises(JournalError): audit(book, requests=[])
    assert fingerprint(book.root) == before


def test_unreferenced_published_page_is_retained_without_counting_it(book):
    value, _ = seed(book, 129)
    empty = index_value(book, [])
    publish_index(book, empty)
    temporary = Path(value['history']['path']).parent / ('.page-' + 'a' * 32 + '.tmp')
    temporary.write_bytes(b'partial staging bytes')
    before = fingerprint(book.root)
    assert audit(book) == dict(requests=1, indexed_requests=1, pending_requests=1, snapshots=0)
    assert read(book) == empty and fingerprint(book.root) == before


@pytest.mark.parametrize('damage', ['duplicate-original', 'unknown-index', 'partial-index', 'duplicate-json', 'oversize'])
def test_audit_rejects_ambiguous_or_unbounded_saved_inventory(book, damage):
    seed(book)
    if damage == 'duplicate-original':
        with pytest.raises(JournalError): audit(book, requests=[book.request.payload()] * 2)
        return
    if damage == 'unknown-index': write_json(book.index.parent / 'unknown.json', {})
    elif damage == 'partial-index': (book.index.parent / '.pending.tmp').write_bytes(b'{')
    elif damage == 'duplicate-json':
        book.index.write_bytes(b'{"accounting":{},"accounting":{},"sha256":"x"}')
    else: book.index.write_bytes(b' ' * (2 * 1024 * 1024 + 1))
    with pytest.raises(JournalError): audit(book)


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
def test_fills_and_unknown_basis_retain_exact_original_economics(book, side):
    book.request = replace(book.request, side=side)
    filled = with_fill(book)
    entry = observation(book, value=filled)
    value = index_value(book, [entry]); publish_index(book, value)
    assert read(book) == value and audit(book)['snapshots'] == 1
    receipt = json.loads(Path(entry['authority_receipt']['path']).read_text())
    assert receipt['payload']['response']['projection'] == filled
    assert filled['totals']['cost_basis'] is None and filled['totals']['fees'] is None


@pytest.mark.parametrize('damage', ['fill-regression', 'fill-mutation', 'allocation-mutation', 'provenance-mutation'])
def test_rehashed_later_observation_cannot_rewrite_historical_economics(book, damage):
    first = with_fill(book)
    second = deepcopy(first)
    if damage == 'fill-regression': second = projection(book)
    elif damage == 'fill-mutation':
        second['fills'][0]['price'] = '42'
        second['allocations'][0]['purchase_fills'][0]['price'] = '42'
        second['totals']['gross_cost'] = '84'
    elif damage == 'allocation-mutation': second['allocations'][0]['forecast_id'] = 'c' * 64
    else: second['provenance']['authority_request_sha256'] = 'c' * 64
    value = index_value(book, [observation(book, value=first), observation(book, 1, value=second)])
    publish_index(book, value)
    with pytest.raises(JournalError): read(book)


@pytest.mark.parametrize('changes', [
    {'orders_or_commitments_created': True}, {'read_only': False},
    {'protocol_compatible': 1}, {'request_nonce': 'foreign:1'},
    {'server_identity': 'foreign:1'}, {'expires_at': NOW.isoformat()},
])
def test_rehashed_authority_view_must_preserve_exact_saved_scope(book, changes):
    entry = observation(book, response_changes=changes)
    publish_index(book, index_value(book, [entry]))
    with pytest.raises(JournalError): read(book)


def test_readers_never_open_writers_construct_stores_or_read_credentials(book, monkeypatch):
    expected, _ = seed(book, 129)
    before = fingerprint(book.root)
    from ml.intraday.scout_journal import ScoutJournal
    from ml.intraday.scout_session_books import ScoutBooks
    original_open = Path.open
    def guarded_open(path, mode='r', *args, **kwargs):
        assert not any(flag in mode for flag in 'wax+'), 'Reader attempted to write'
        assert path.suffix not in {'.pem', '.key'}, 'Reader accessed historical credential paths'
        return original_open(path, mode, *args, **kwargs)
    def forbidden(*args, **kwargs):
        raise AssertionError('Reader constructed a store or opened SQLite')
    with monkeypatch.context() as guard:
        guard.setattr(Path, 'open', guarded_open)
        guard.setattr(ScoutBooks, '__init__', forbidden)
        guard.setattr(ScoutJournal, '__init__', forbidden)
        guard.setattr(sqlite3, 'connect', forbidden)
        assert read(book) == expected and audit(book)['snapshots'] == 129
    assert fingerprint(book.root) == before


def test_saved_reader_import_and_reads_do_not_resolve_legacy_runtime_modules(book, monkeypatch):
    seed(book)
    blocked = {'ml.intraday.scout_accounting', 'ml.intraday.scout_authority',
        'ml.intraday.scout_books', 'ml.intraday.admission_transport', 'ml.intraday.admission_service',
        'ml.intraday.scout_execution', 'ml.intraday.scout_runtime_gate', 'ml.intraday.scout_prestart'}
    original_import = builtins.__import__
    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        resolved = importlib.util.resolve_name('.' * level + name, globals['__package__']) if level else name
        requested = {resolved, *(resolved + '.' + item for item in (fromlist or ()))}
        assert not requested.intersection(blocked), 'Saved reader imported executable authority state'
        return original_import(name, globals, locals, fromlist, level)
    monkeypatch.setattr(builtins, '__import__', guarded)
    spec = importlib.util.spec_from_file_location('ml.intraday._saved_accounting_dependency_probe', saved.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.read_saved_accounting(book.root, account_fingerprint=book.request.account_fingerprint,
        request=book.request.payload(), check_deadline=lambda: None)['snapshot_count'] == 1
    assert module.audit_saved_accounting(book.root, account_fingerprint=book.request.account_fingerprint,
        requests=[book.request.payload()], check_deadline=lambda: None)['snapshots'] == 1
    assert not any(hasattr(module, name) for name in
                   ('record_accounting', 'record_pending', '_write', '_seal_page', 'state'))
