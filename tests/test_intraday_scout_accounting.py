import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ml.intraday.scout_books import ScoutBooks
from ml.intraday.scout_journal import JournalError
from ml.intraday.scout_protocol import canonical_sha256
from test_intraday_scout_client import request, grant


NOW = '2026-10-12T16:00:00+00:00'


def accounting_response(request, binding, now, nonce, **changes):
    """Complete Atlas6139260261 accounting fixture; no service/store effects."""
    from ml.intraday.scout_accounting import ACCOUNTING_VERSION
    from ml.intraday.scout_authority import PROTOCOL_VERSION
    amount = min(3, request.quantity)
    projection = dict(
        request={name: getattr(request, name) for name in ('symbol', 'side', 'quantity', 'limit_price', 'forecast_id')},
        account_reservation=dict(reservation_id='reservation:1', status='RESERVED', quantity=amount,
            filled_quantity=0, broker_order_id=None, created_at=request.issued_at, last_evidence_at=None),
        native_reservation=dict(reservation_id='native:1', allocation_id='allocation:1', status='RESERVED',
            quantity=amount, filled_quantity=0, broker_order_id=None, last_evidence_at=None),
        allocations=[dict(allocation_id='allocation:1', symbol=request.symbol, horizon='15m',
            forecast_id=request.forecast_id, target_start=request.reference_at, target_end=request.target_close_at,
            status='ACTIVE', purchase_fills=[], observed_inventory=[])],
        fills=[], sell_slices=([dict(allocation_id='allocation:1', quantity=amount, ordinal=0)] if request.side == 'SELL' else []),
        fill_attribution=[], totals=dict(filled_quantity=0, gross_cost='0', gross_proceeds='0', cost_basis=None,
            realized_pnl=None, basis_status='unknown', fees=None),
        provenance=dict(journal_request_sha256=request.sha256, authority_request_sha256='e'*64,
            native_request_sha256='f'*64, account_order_evidence_sha256=[], native_order_evidence_sha256=[]))
    return dict(version=ACCOUNTING_VERSION, protocol_version=PROTOCOL_VERSION,
        server_identity=binding.server_identity, participant_id='pc-new', stream='scout-15m',
        account_fingerprint=binding.account_fingerprint, request_nonce=nonce, observed_at=now.isoformat(),
        expires_at=(now+timedelta(seconds=30)).isoformat(), authenticated=True, protocol_compatible=True,
        read_only=True, orders_or_commitments_created=0, request_id=request.request_id,
        request_sha256=request.sha256, session_id=request.session_id, status='AVAILABLE',
        snapshot_sha256=canonical_sha256(projection), projection=projection) | changes


def with_fill(response, request, *, quantity=2, status='PARTIAL'):
    from decimal import Decimal
    value = deepcopy(response)
    p = value['projection']
    time = (datetime.fromisoformat(response['observed_at']) - timedelta(seconds=1)).isoformat()
    price = request.limit_price
    for name in ('account_reservation', 'native_reservation'):
        p[name].update(status=status, filled_quantity=quantity, broker_order_id='123456', last_evidence_at=time)
    fill = dict(fill_id='fill:1', quantity=quantity, price=price, executed_at=time,
                native_reservation_id=p['native_reservation']['reservation_id'],
                account_reservation_id=p['account_reservation']['reservation_id'])
    p['fills'] = [fill]
    if request.side == 'BUY':
        p['allocations'][0]['purchase_fills'] = [{k: v for k, v in fill.items() if k != 'account_reservation_id'}]
    else:
        p['fill_attribution'] = [dict(fill_id='fill:1', allocation_id='allocation:1', quantity=quantity, slice_ordinal=0)]
    gross = format(Decimal(price) * quantity, 'f')
    if '.' in gross:
        gross = gross.rstrip('0').rstrip('.')
    p['totals'].update(filled_quantity=quantity,
        gross_cost=gross if request.side == 'BUY' else '0',
        gross_proceeds=gross if request.side == 'SELL' else '0')
    p['provenance']['account_order_evidence_sha256'] = ['1'*64]
    p['provenance']['native_order_evidence_sha256'] = ['2'*64]
    value['snapshot_sha256'] = canonical_sha256(p)
    return value


def validate(value, req, binding, now):
    from ml.intraday.scout_accounting import validate_accounting_response
    return validate_accounting_response(value, req, binding, request_nonce='nonce-1',
                                       requested_at=now, received_at=now, now=now)


@pytest.fixture
def canonical(tmp_path):
    from test_intraday_scout_authority import binding
    req = request(symbol='MSFT')
    bound = binding(tmp_path)
    now = datetime.fromisoformat(NOW)
    return req, bound, now, accounting_response(req, bound, now, 'nonce-1')


def test_exact_reduced_canonical_projection_retains_unknown_economics(canonical):
    req, bound, now, value = canonical
    output = validate(value, req, bound, now)
    assert output['request']['quantity'] == 10
    assert output['account_reservation']['quantity'] == output['native_reservation']['quantity'] == 3
    assert output['totals'] == dict(filled_quantity=0, gross_cost='0', gross_proceeds='0',
        cost_basis=None, realized_pnl=None, fees=None, basis_status='unknown')


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
def test_actual_fills_balance_canonical_gross_and_private_provenance(canonical, side):
    original, bound, now, _ = canonical
    req = replace(original, side=side)
    value = with_fill(accounting_response(req, bound, now, 'nonce-1'), req)
    output = validate(value, req, bound, now)
    assert output['totals']['filled_quantity'] == 2
    assert output['totals']['gross_cost' if side == 'BUY' else 'gross_proceeds'] == '85'
    assert output['totals']['realized_pnl'] is None


def test_cross_horizon_sale_keeps_exact_donor_attribution(canonical):
    original, bound, now, _ = canonical
    req = replace(original, side='SELL')
    value = with_fill(accounting_response(req, bound, now, 'nonce-1'), req)
    p = value['projection']
    p['sell_slices'] = [dict(allocation_id='donor:4h', quantity=1, ordinal=0),
                        dict(allocation_id='allocation:1', quantity=2, ordinal=1)]
    p['fill_attribution'] = [dict(fill_id='fill:1', allocation_id='donor:4h', quantity=1, slice_ordinal=0),
                            dict(fill_id='fill:1', allocation_id='allocation:1', quantity=1, slice_ordinal=1)]
    donor = deepcopy(p['allocations'][0])
    donor.update(allocation_id='donor:4h', horizon='4h', forecast_id='prior-owned-forecast')
    donor['observed_inventory'] = [dict(id='opening:1', quantity=1, snapshot_id='snapshot:1', observed_at=req.reference_at)]
    p['allocations'].append(donor)
    value['snapshot_sha256'] = canonical_sha256(p)
    assert validate(value, req, bound, now)['allocations'][1]['horizon'] == '4h'


@pytest.mark.parametrize('path,bad', [
    (('request', 'symbol'), 'FOREIGN'), (('request', 'quantity'), 3),
    (('account_reservation', 'quantity'), True), (('native_reservation', 'quantity'), 4),
    (('native_reservation', 'status'), 'UNKNOWN'), (('account_reservation', 'filled_quantity'), 1),
    (('totals', 'gross_cost'), '1'), (('totals', 'cost_basis'), '0'),
    (('totals', 'fees'), '0'), (('totals', 'realized_pnl'), '1'),
    (('totals', 'basis_status'), 'known'), (('totals', 'gross_proceeds'), '1E+999999999'),
    (('provenance', 'journal_request_sha256'), '0'*64),
    (('provenance', 'native_request_sha256'), 'NOTAHASH'),
    (('account_reservation', 'created_at'), '2026-10-12T14:14:59+00:00'),
])
def test_malformed_or_unbalanced_canonical_projection_rejected(canonical, path, bad):
    req, bound, now, value = canonical
    value['projection'][path[0]][path[1]] = bad
    value['snapshot_sha256'] = canonical_sha256(value['projection'])
    with pytest.raises(ValueError):
        validate(value, req, bound, now)


@pytest.mark.parametrize('damage', ['missing_section', 'extra_section', 'extra_nested_field', 'unrelated_allocation',
                                   'duplicate_fill', 'old_fill', 'evidence_before_fill', 'missing_purchase',
                                   'wrong_native_fill', 'wrong_account_fill', 'wrong_fill_quantity'])
def test_incomplete_or_foreign_canonical_evidence_rejected(canonical, damage):
    req, bound, now, value = canonical
    value = with_fill(value, req)
    p = value['projection']
    if damage == 'missing_section': p.pop('totals')
    elif damage == 'extra_section': p['account_dump'] = {}
    elif damage == 'extra_nested_field': p['request']['foreign_stream'] = 'atlas-15m'
    elif damage == 'unrelated_allocation':
        other = deepcopy(p['allocations'][0]); other.update(allocation_id='unrelated:1', purchase_fills=[])
        p['allocations'].append(other)
    elif damage == 'duplicate_fill': p['fills'].append(deepcopy(p['fills'][0]))
    elif damage == 'old_fill': p['fills'][0]['executed_at'] = req.reference_at
    elif damage == 'evidence_before_fill': p['account_reservation']['last_evidence_at'] = req.issued_at
    elif damage == 'missing_purchase': p['allocations'][0]['purchase_fills'] = []
    elif damage == 'wrong_native_fill': p['fills'][0]['native_reservation_id'] = 'other:1'
    elif damage == 'wrong_account_fill': p['fills'][0]['account_reservation_id'] = 'other:1'
    else: p['fills'][0]['quantity'] = 1
    value['snapshot_sha256'] = canonical_sha256(p)
    with pytest.raises(ValueError): validate(value, req, bound, now)


def saved_accounting(books, req, bound, now, response):
    from ml.intraday.scout_authority import ScoutAuthorityClient
    body = dict(request_nonce=response['request_nonce'], request_id=req.request_id,
                request_sha256=req.sha256, session_id=req.session_id)
    client = ScoutAuthorityClient(bound, books, clock=lambda: now)
    return client._save('accounting', req.request_id, body, response, requested_at=now)


def admitted(books, req, *, accepted=False):
    with books.create_session(req.session_id) as journal:
        journal.prepare(req)
        journal.grant(grant(req))
        if accepted:
            token = journal.claim_send(req.request_id)
            journal.outcome(req.request_id, token, '123456')


def test_private_snapshot_survives_reload_pending_failure_and_expiration(books, canonical):
    req, bound, now, response = canonical
    admitted(books, req)
    pending(books, req)
    receipt = saved_accounting(books, req, bound, now, response)
    entry = books.record_accounting(req, receipt, binding=bound, now=now)
    assert entry['snapshot_sha256'] == response['snapshot_sha256']
    reopened = ScoutBooks(books.root, account_fingerprint=books.account_fingerprint)
    pending(reopened, req, observed_at=(now+timedelta(minutes=2)).isoformat())
    state = reopened.accounting_state(req)
    assert state['pending'] is True and state['latest_snapshot'] == entry
    assert len(state['snapshots']) == 1
    assert audit(reopened, [req.payload()])['snapshots'] == 1


def test_identical_canonical_contents_retain_every_distinct_authority_observation(books, canonical):
    req, bound, now, response = canonical
    admitted(books, req)
    for counter in range(3):
        at = now + timedelta(seconds=counter)
        value = accounting_response(req, bound, at, 'nonce-' + str(counter))
        receipt = saved_accounting(books, req, bound, at, value)
        books.record_accounting(req, receipt, binding=bound, now=at)
    state = books.accounting_state(req)
    assert len(state['snapshots']) == 3 and state['pending'] is False
    assert state['latest_snapshot']['observed_at'] == (now+timedelta(seconds=2)).isoformat()
    assert state['last_attempt'] == receipt.saved


def test_canonical_fill_regression_or_mutation_cannot_replace_valid_private_books(books, canonical):
    req, bound, now, response = canonical
    admitted(books, req, accepted=True)
    first = with_fill(response, req)
    receipt = saved_accounting(books, req, bound, now, first)
    books.record_accounting(req, receipt, binding=bound, now=now)
    before = books.accounting_state(req)
    later = now + timedelta(seconds=1)
    changed = with_fill(accounting_response(req, bound, later, 'nonce-2'), req, quantity=1)
    receipt = saved_accounting(books, req, bound, later, changed)
    with pytest.raises(JournalError): books.record_accounting(req, receipt, binding=bound, now=later)
    assert books.accounting_state(req) == before


def test_saved_snapshot_requires_exact_local_grant_and_durable_file(books, canonical):
    req, bound, now, response = canonical
    admitted(books, req)
    bad = deepcopy(response)
    bad['projection']['account_reservation']['reservation_id'] = 'foreign-reservation'
    bad['snapshot_sha256'] = canonical_sha256(bad['projection'])
    receipt = saved_accounting(books, req, bound, now, bad)
    with pytest.raises(JournalError): books.record_accounting(req, receipt, binding=bound, now=now)
    valid = saved_accounting(books, req, bound, now, response)
    Path(valid.path).unlink()
    with pytest.raises(JournalError): books.record_accounting(req, valid, binding=bound, now=now)
    assert books.accounting_state(req)['latest_snapshot'] is None


def test_interrupted_authority_receipt_is_retained_but_never_promoted_by_audit(books, canonical):
    req, bound, now, response = canonical
    admitted(books, req)
    receipt = saved_accounting(books, req, bound, now, response)
    assert audit(books, [req.payload()])['snapshots'] == 0
    assert books.accounting_state(req)['latest_snapshot'] is None
    with pytest.raises(JournalError): audit(books, [])
    books.record_accounting(req, receipt, binding=bound, now=now)
    assert audit(books, [req.payload()])['snapshots'] == 1


def test_pending_after_snapshot_never_rewrites_immutable_receipts(books, canonical):
    req, bound, now, response = canonical
    admitted(books, req)
    receipt = saved_accounting(books, req, bound, now, response)
    books.record_accounting(req, receipt, binding=bound, now=now)
    before = Path(receipt.path).read_bytes()
    books.record_accounting_pending(req, reason_code='accounting-timeout', observed_at=(now+timedelta(seconds=1)).isoformat())
    assert Path(receipt.path).read_bytes() == before
    assert books.accounting_state(req)['latest_snapshot']['authority_receipt'] == receipt.saved


@pytest.mark.parametrize('damage', ['wrong_broker_id', 'wrong_granted_quantity', 'unclaimed_broker_order'])
def test_canonical_snapshot_cannot_cross_local_submission_identity(books, canonical, damage):
    req, bound, now, response = canonical
    admitted(books, req, accepted=damage != 'unclaimed_broker_order')
    value = with_fill(response, req)
    if damage == 'wrong_broker_id':
        for name in ('account_reservation', 'native_reservation'): value['projection'][name]['broker_order_id'] = '999999'
    elif damage == 'wrong_granted_quantity':
        for name in ('account_reservation', 'native_reservation'): value['projection'][name]['quantity'] = 4
    value['snapshot_sha256'] = canonical_sha256(value['projection'])
    receipt = saved_accounting(books, req, bound, now, value)
    with pytest.raises(JournalError): books.record_accounting(req, receipt, binding=bound, now=now)
    assert books.accounting_state(req)['latest_snapshot'] is None


def test_snapshot_index_crash_keeps_original_snapshot_and_saved_new_receipt(books, canonical, monkeypatch):
    from ml.intraday import scout_accounting
    req, bound, now, response = canonical
    admitted(books, req, accepted=True)
    first = with_fill(response, req, quantity=1)
    books.record_accounting(req, saved_accounting(books, req, bound, now, first), binding=bound, now=now)
    previous = books.accounting_state(req)
    later = now+timedelta(seconds=2)
    current = with_fill(accounting_response(req, bound, later, 'nonce-2'), req, quantity=1)
    # Preserve immutable first fill and add a second cumulative fill.
    current['projection']['fills'] = deepcopy(first['projection']['fills'])
    extra = deepcopy(current['projection']['fills'][0]); extra.update(fill_id='fill:2', executed_at=(later-timedelta(seconds=1)).isoformat())
    current['projection']['fills'].append(extra)
    current['projection']['allocations'][0]['purchase_fills'] = [{k:v for k,v in f.items() if k != 'account_reservation_id'} for f in current['projection']['fills']]
    for name in ('account_reservation', 'native_reservation'): current['projection'][name]['filled_quantity'] = 2
    current['projection']['totals'].update(filled_quantity=2, gross_cost='85')
    current['snapshot_sha256'] = canonical_sha256(current['projection'])
    saved = saved_accounting(books, req, bound, later, current)
    write = scout_accounting._write
    monkeypatch.setattr(scout_accounting, '_write', lambda *a: (_ for _ in ()).throw(OSError('synthetic crash')))
    with pytest.raises(OSError): books.record_accounting(req, saved, binding=bound, now=later)
    assert books.accounting_state(req) == previous and Path(saved.path).is_file()
    monkeypatch.setattr(scout_accounting, '_write', write)
    books.record_accounting(req, saved, binding=bound, now=later)
    assert len(books.accounting_state(req)['snapshots']) == 2


@pytest.mark.parametrize('damage', ['deleted_receipt', 'mutated_binding', 'missing_history', 'mutated_latest', 'changed_pending'])
def test_corrupt_canonical_index_fails_closed(books, canonical, damage):
    req, bound, now, response = canonical
    admitted(books, req)
    receipt = saved_accounting(books, req, bound, now, response)
    books.record_accounting(req, receipt, binding=bound, now=now)
    path = next((books.root/'accounting').glob('*.json'))
    wrapper = json.loads(path.read_text()); value = wrapper['accounting']
    if damage == 'deleted_receipt': Path(receipt.path).unlink()
    else:
        if damage == 'mutated_binding': value['bindings'][bound.sha256]['server_identity'] = 'foreign'
        elif damage == 'missing_history': value['snapshots'] = []
        elif damage == 'mutated_latest': value['latest_snapshot']['snapshot_sha256'] = 'f'*64
        else: value['pending'] = True
        wrapper['sha256'] = canonical_sha256(value); path.write_text(json.dumps(wrapper))
    with pytest.raises(JournalError): books.accounting_state(req)
    with pytest.raises(JournalError): books.record_accounting_pending(req, reason_code='retry', observed_at=NOW)


@pytest.fixture
def books(tmp_path):
    return ScoutBooks(tmp_path / 'private-books', account_fingerprint='a' * 64)


def pending(books, req=None, **kwargs):
    return books.record_accounting_pending(req or request(), reason_code='authority-unavailable',
                                           observed_at=kwargs.get('observed_at', NOW))


def test_read_only_unstarted_accounting_never_creates(books):
    value = books.accounting_state(request())
    assert value['pending'] is True
    assert value['latest_snapshot'] is value['last_attempt'] is None
    assert value['snapshots'] == []
    assert not books.root.exists()


def test_pending_receipt_survives_reload_is_idempotent_and_has_no_authority_effect(books):
    req = request()
    with books.create_session(req.session_id) as journal:
        journal.prepare(req)
        journal.grant(grant(req))
        journal.claim_send(req.request_id)
        before = journal.get(req.request_id)
    first = pending(books)
    assert pending(books) == first
    reopened = ScoutBooks(books.root, account_fingerprint='a' * 64)
    value = reopened.accounting_state(req)
    assert value['last_attempt'] == first
    assert value['pending'] is True and value['latest_snapshot'] is None
    with reopened.open_session(req.session_id) as journal:
        assert journal.get(req.request_id) == before
    assert len(list((books.root / 'receipts').glob('*.json'))) == 1


def test_mismatched_original_request_cannot_replace_private_index(books):
    req = request()
    pending(books, req)
    before = next((books.root / 'accounting').glob('*.json')).read_bytes()
    changed = replace(req, quantity=req.quantity + 1)
    with pytest.raises(JournalError):
        pending(books, changed)
    assert next((books.root / 'accounting').glob('*.json')).read_bytes() == before
    with pytest.raises(JournalError):
        books.accounting_state(changed)


@pytest.mark.parametrize('change', ['account', 'session'])
def test_foreign_identity_cannot_read_accounting(books, change):
    req = request()
    pending(books)
    if change == 'account':
        altered = ScoutBooks(books.root, account_fingerprint='b' * 64)
        with pytest.raises(JournalError):
            altered.accounting_state(req)
    else:
        with pytest.raises(JournalError):
            books.accounting_state(replace(req, session_id='other-session'))


@pytest.mark.parametrize('value', [None, '', 'bad reason', '../secret', 1, 'x' * 129])
def test_reason_codes_are_bounded_and_not_arbitrary_error_messages(books, value):
    with pytest.raises(JournalError):
        books.record_accounting_pending(request(), reason_code=value, observed_at=NOW)
    assert not books.root.exists()


@pytest.mark.parametrize('value', [None, '', '2026-10-12T16:00:00', 'bad', 1])
def test_pending_observation_requires_aware_valid_timestamp(books, value):
    with pytest.raises(JournalError):
        pending(books, observed_at=value)
    assert not books.root.exists()


def test_time_rollback_cannot_replace_current_accounting_state(books):
    pending(books)
    before = books.accounting_state(request())
    with pytest.raises(JournalError):
        pending(books, observed_at='2026-10-12T15:59:59+00:00')
    assert books.accounting_state(request()) == before


@pytest.mark.parametrize('damage', ['index_digest', 'receipt_deleted', 'receipt_corrupt', 'foreign_receipt_path',
                                     'request_hash', 'invented_available'])
def test_corrupt_accounting_is_never_silently_replaced(books, damage):
    saved = pending(books)
    path = next((books.root / 'accounting').glob('*.json'))
    value = json.loads(path.read_text())
    if damage == 'receipt_deleted':
        Path(saved['path']).unlink()
    elif damage == 'receipt_corrupt':
        Path(saved['path']).write_text('{}')
    elif damage == 'index_digest':
        value['sha256'] = 'f' * 64
        path.write_text(json.dumps(value))
    else:
        if damage == 'foreign_receipt_path':
            value['accounting']['last_attempt']['path'] = str(books.root / 'outside.json')
        elif damage == 'request_hash':
            value['accounting']['request_sha256'] = 'f' * 64
        else:
            value['accounting']['pending'] = False
        value['sha256'] = canonical_sha256(value['accounting'])
        path.write_text(json.dumps(value))
    with pytest.raises(JournalError):
        books.accounting_state(request())
    with pytest.raises(JournalError):
        pending(books)


def test_crash_after_receipt_before_index_retries_without_losing_evidence(books, monkeypatch):
    from ml.intraday import scout_accounting
    original = scout_accounting._write
    def crash(*args):
        raise OSError('synthetic accounting-index failure')
    monkeypatch.setattr(scout_accounting, '_write', crash)
    with pytest.raises(OSError):
        pending(books)
    receipts = list((books.root / 'receipts').glob('*.json'))
    assert len(receipts) == 1
    assert books.accounting_state(request())['last_attempt'] is None
    monkeypatch.setattr(scout_accounting, '_write', original)
    saved = pending(books)
    assert Path(saved['path']) == receipts[0]
    assert books.accounting_state(request())['last_attempt'] == saved


def audit(books, requests, check_deadline=lambda: None):
    from ml.intraday.scout_accounting import audit_saved_accounting
    return audit_saved_accounting(books.root, account_fingerprint=books.account_fingerprint,
                                  requests=requests, check_deadline=check_deadline)


def test_raw_audit_of_absent_accounting_does_not_create(books, monkeypatch):
    from ml.intraday import scout_journal
    monkeypatch.setattr(ScoutBooks, '__init__', lambda *a, **k: pytest.fail('No store constructor'))
    monkeypatch.setattr(scout_journal.ScoutJournal, '__init__', lambda *a, **k: pytest.fail('No journal constructor'))
    assert audit(books, []) == dict(requests=0, indexed_requests=0, pending_requests=0, snapshots=0)
    assert not books.root.exists()


def test_raw_audit_retains_exact_original_request_binding(books):
    pending(books)
    assert audit(books, [request().payload()]) == dict(requests=1, indexed_requests=1, pending_requests=1, snapshots=0)
    with pytest.raises(JournalError):
        audit(books, [])
    with pytest.raises(JournalError):
        audit(books, [replace(request(), quantity=9).payload()])
    with pytest.raises(JournalError):
        audit(books, [request().payload(), request().payload()])


def test_raw_audit_rejects_orphan_receipt_without_original_journal(books, monkeypatch):
    from ml.intraday import scout_accounting
    monkeypatch.setattr(scout_accounting, '_write', lambda *a: (_ for _ in ()).throw(OSError('interrupted')))
    with pytest.raises(OSError):
        pending(books)
    with pytest.raises(JournalError):
        audit(books, [])
    # Its original journal makes this interrupted index update recoverable.
    assert audit(books, [request().payload()])['indexed_requests'] == 0


def test_raw_audit_honors_deadline_and_rejects_partial_index(books):
    pending(books)
    def expired():
        raise TimeoutError('synthetic total deadline')
    with pytest.raises(TimeoutError):
        audit(books, [request().payload()], expired)
    (books.root / 'accounting' / '.accounting-interrupted.tmp').write_text('{}')
    with pytest.raises(JournalError):
        audit(books, [request().payload()])


@pytest.mark.parametrize('damage', ['oversize', 'duplicate_json_key'])
def test_raw_audit_rejects_unbounded_or_ambiguous_evidence(books, damage):
    pending(books)
    path = next((books.root / 'accounting').glob('*.json'))
    if damage == 'oversize':
        path.write_bytes(b' ' * (2 * 1024 * 1024 + 1))
    else:
        text = path.read_text()
        path.write_text(text[:-1] + ',"sha256":"' + json.loads(text)['sha256'] + '"}')
    with pytest.raises(JournalError):
        audit(books, [request().payload()])


def historic_inventory(response, req):
    value = deepcopy(response)
    row = value['projection']['allocations'][0]
    row['forecast_id'] = 'earlier-allocation-forecast'
    row['purchase_fills'] = [dict(fill_id='prior-fill', native_reservation_id='prior-order',
        quantity=2, price='40', executed_at=req.reference_at)]
    row['observed_inventory'] = [dict(id='opening:1', quantity=1,
        snapshot_id='snapshot:1', observed_at=req.reference_at)]
    value['snapshot_sha256'] = canonical_sha256(value['projection'])
    return value


@pytest.mark.parametrize('damage', ['forecast', 'targets', 'purchase', 'assignment', 'lost_purchase', 'lost_assignment'])
def test_prior_canonical_inventory_facts_cannot_be_rewritten_after_reload(books, canonical, damage):
    req, bound, now, response = canonical
    admitted(books, req)
    original = historic_inventory(response, req)
    receipt = saved_accounting(books, req, bound, now, original)
    books.record_accounting(req, receipt, binding=bound, now=now)
    reopened = ScoutBooks(books.root, account_fingerprint=books.account_fingerprint)
    before = reopened.accounting_state(req)
    later = now + timedelta(seconds=1)
    changed = historic_inventory(accounting_response(req, bound, later, 'nonce-2'), req)
    row = changed['projection']['allocations'][0]
    if damage == 'forecast': row['forecast_id'] = 'rewritten-original-forecast'
    elif damage == 'targets': row['target_start'] = (datetime.fromisoformat(row['target_start']) - timedelta(minutes=15)).isoformat()
    elif damage == 'purchase': row['purchase_fills'][0]['price'] = '39'
    elif damage == 'assignment': row['observed_inventory'][0]['quantity'] = 2
    elif damage == 'lost_purchase': row['purchase_fills'].clear()
    else: row['observed_inventory'].clear()
    changed['snapshot_sha256'] = canonical_sha256(changed['projection'])
    # Each isolated response remains coherent; history supplies the missing fence.
    from ml.intraday.scout_accounting import validate_accounting_projection
    validate_accounting_projection(changed['projection'], req, observed_at=later.isoformat())
    receipt = saved_accounting(reopened, req, bound, later, changed)
    with pytest.raises(JournalError, match='Immutable canonical'):
        reopened.record_accounting(req, receipt, binding=bound, now=later)
    assert reopened.accounting_state(req) == before
    assert audit(reopened, [req.payload()])['snapshots'] == 1


def test_reused_allocation_permits_new_inventory_facts_and_status_reopening(books, canonical):
    req, bound, now, response = canonical
    admitted(books, req)
    original = historic_inventory(response, req)
    original['projection']['allocations'][0]['status'] = 'CLOSED'
    original['snapshot_sha256'] = canonical_sha256(original['projection'])
    books.record_accounting(req, saved_accounting(books, req, bound, now, original), binding=bound, now=now)
    later = now + timedelta(seconds=1)
    current = historic_inventory(accounting_response(req, bound, later, 'nonce-2'), req)
    row = current['projection']['allocations'][0]
    row['purchase_fills'].append(dict(fill_id='new-other-fill', native_reservation_id='new-other-order',
        quantity=1, price='41', executed_at=now.isoformat()))
    row['observed_inventory'].append(dict(id='opening:2', quantity=2,
        snapshot_id='snapshot:2', observed_at=now.isoformat()))
    current['snapshot_sha256'] = canonical_sha256(current['projection'])
    receipt = saved_accounting(books, req, bound, later, current)
    books.record_accounting(req, receipt, binding=bound, now=later)
    assert len(books.accounting_state(req)['snapshots']) == 2
    assert audit(books, [req.payload()])['snapshots'] == 2


@pytest.mark.parametrize('same_time', [False, True])
def test_allocation_reopening_retains_nonconsecutive_identical_snapshot_observations(books, canonical, same_time):
    from ml.intraday.scout_accounting import read_saved_accounting
    req, bound, now, _ = canonical
    admitted(books, req)
    receipts = []
    for counter, status in enumerate(('ACTIVE', 'CLOSED', 'ACTIVE', 'ACTIVE')):
        at = now + timedelta(seconds=0 if same_time else counter)
        value = historic_inventory(accounting_response(req, bound, at, 'nonce-' + str(counter)), req)
        value['projection']['allocations'][0]['status'] = status
        value['snapshot_sha256'] = canonical_sha256(value['projection'])
        receipt = saved_accounting(books, req, bound, at, value)
        receipts.append((receipt, Path(receipt.path).read_bytes()))
        books.record_accounting(req, receipt, binding=bound, now=at)
        # Reopen after each observation to exercise the saved history audit.
        books = ScoutBooks(books.root, account_fingerprint=books.account_fingerprint)
    state = books.accounting_state(req)
    assert len(state['snapshots']) == 4
    assert state['snapshots'][0]['snapshot_sha256'] == state['snapshots'][2]['snapshot_sha256']
    assert state['snapshots'][0]['authority_receipt'] != state['snapshots'][2]['authority_receipt']
    assert state['latest_snapshot']['authority_receipt'] == receipts[-1][0].saved
    assert state['latest_snapshot']['observed_at'] == (now + timedelta(seconds=0 if same_time else 3)).isoformat()
    assert all(Path(receipt.path).read_bytes() == original for receipt, original in receipts)
    assert audit(books, [req.payload()])['snapshots'] == 4
    assert read_saved_accounting(books.root, account_fingerprint=books.account_fingerprint,
        request=req.payload(), check_deadline=lambda: None) == state


@pytest.mark.parametrize('replay', ['record', 'saved_index'])
def test_equal_time_receipt_replay_cannot_become_a_new_changed_observation(books, canonical, replay):
    req, bound, now, response = canonical
    admitted(books, req)
    first = saved_accounting(books, req, bound, now, response)
    books.record_accounting(req, first, binding=bound, now=now)
    changed = accounting_response(req, bound, now, 'nonce-2')
    changed['projection']['allocations'][0]['status'] = 'CLOSED'
    changed['snapshot_sha256'] = canonical_sha256(changed['projection'])
    second = saved_accounting(books, req, bound, now, changed)
    books.record_accounting(req, second, binding=bound, now=now)
    before = books.accounting_state(req)
    if replay == 'record':
        with pytest.raises(JournalError, match='old canonical receipt'):
            books.record_accounting(req, first, binding=bound, now=now)
        assert books.accounting_state(req) == before
        # Exact current receipt replay remains idempotent.
        books.record_accounting(req, second, binding=bound, now=now)
        assert books.accounting_state(req) == before
    else:
        path = next((books.root / 'accounting').glob('*.json'))
        wrapper = json.loads(path.read_text())
        index = wrapper['accounting']
        index['snapshots'].append(deepcopy(index['snapshots'][0]))
        index['snapshot_count'] += 1
        index['latest_snapshot'] = deepcopy(index['snapshots'][0])
        index['last_attempt'] = first.saved
        wrapper['sha256'] = canonical_sha256(index)
        path.write_text(json.dumps(wrapper))
        with pytest.raises(JournalError, match='Private accounting index failed validation') as rejected:
            audit(books, [req.payload()])
        assert 'Duplicate canonical snapshot observation receipt' in str(rejected.value.__cause__)


def test_identical_content_observation_cannot_be_reused_after_an_intervening_change(books, canonical):
    req, bound, now, response = canonical
    admitted(books, req)
    first = saved_accounting(books, req, bound, now, response)
    books.record_accounting(req, first, binding=bound, now=now)
    identical = accounting_response(req, bound, now, 'nonce-2')
    second = saved_accounting(books, req, bound, now, identical)
    books.record_accounting(req, second, binding=bound, now=now)
    changed = accounting_response(req, bound, now, 'nonce-3')
    changed['projection']['allocations'][0]['status'] = 'CLOSED'
    changed['snapshot_sha256'] = canonical_sha256(changed['projection'])
    third = saved_accounting(books, req, bound, now, changed)
    books.record_accounting(req, third, binding=bound, now=now)
    books = ScoutBooks(books.root, account_fingerprint=books.account_fingerprint)
    before = books.accounting_state(req)
    assert len(before['snapshots']) == 3
    with pytest.raises(JournalError, match='old canonical receipt'):
        books.record_accounting(req, second, binding=bound, now=now)
    assert books.accounting_state(req) == before
    assert audit(books, [req.payload()])['snapshots'] == 3


@pytest.mark.parametrize('pending_delay', [0, 1])
def test_exact_current_receipt_retry_preserves_later_pending_attempt(books, canonical, pending_delay):
    req, bound, now, response = canonical
    admitted(books, req)
    receipt = saved_accounting(books, req, bound, now, response)
    original = books.record_accounting(req, receipt, binding=bound, now=now)
    books.record_accounting_pending(req, reason_code='view-retry',
        observed_at=(now + timedelta(seconds=pending_delay)).isoformat())
    before = books.accounting_state(req)
    files = {p: p.read_bytes() for p in books.root.rglob('*.json')}
    assert books.record_accounting(req, receipt, binding=bound, now=now + timedelta(seconds=2)) == original
    assert books.accounting_state(req) == before and before['pending'] is True
    assert all(p.read_bytes() == raw for p, raw in files.items())
    later = now + timedelta(seconds=3)
    fresh = saved_accounting(books, req, bound, later, accounting_response(req, bound, later, 'nonce-new'))
    books.record_accounting(req, fresh, binding=bound, now=later)
    current = books.accounting_state(req)
    assert current['pending'] is False and current['last_attempt'] == fresh.saved
    assert len(current['snapshots']) == 2


@pytest.mark.parametrize('damage', ['duplicate_adjacent', 'old_history_receipt', 'old_latest_receipt'])
def test_repeated_contents_do_not_allow_old_observations_to_replace_current_history(books, canonical, damage):
    req, bound, now, _ = canonical
    admitted(books, req)
    for counter, status in enumerate(('ACTIVE', 'CLOSED', 'ACTIVE')):
        at = now + timedelta(seconds=counter)
        value = accounting_response(req, bound, at, 'nonce-' + str(counter))
        value['projection']['allocations'][0]['status'] = status
        value['snapshot_sha256'] = canonical_sha256(value['projection'])
        books.record_accounting(req, saved_accounting(books, req, bound, at, value), binding=bound, now=at)
    path = next((books.root / 'accounting').glob('*.json'))
    wrapper = json.loads(path.read_text())
    index = wrapper['accounting']
    if damage == 'duplicate_adjacent':
        index['snapshots'].append(deepcopy(index['snapshots'][-1]))
    elif damage == 'old_history_receipt':
        index['snapshots'][-1] = deepcopy(index['snapshots'][0])
    else:
        index['latest_snapshot'] = deepcopy(index['snapshots'][0])
        index['last_attempt'] = index['latest_snapshot']['authority_receipt']
    wrapper['sha256'] = canonical_sha256(index)
    path.write_text(json.dumps(wrapper))
    with pytest.raises(JournalError):
        audit(books, [req.payload()])


@pytest.mark.parametrize('price,quantity', [('1' + '2' * 63 + '.' + '3' * 40, 2**63-1), ('1' + '9' * 127, 1)])
def test_gross_arithmetic_is_exact_beyond_default_decimal_precision(canonical, price, quantity):
    from decimal import Decimal, localcontext
    req, bound, now, _ = canonical
    req = replace(req, side='SELL', quantity=quantity)
    value = with_fill(accounting_response(req, bound, now, 'nonce-1'), req, quantity=quantity, status='FILLED')
    p = value['projection']
    for name in ('account_reservation', 'native_reservation'): p[name]['quantity'] = quantity
    p['sell_slices'][0]['quantity'] = quantity
    p['fills'][0]['price'] = price
    with localcontext() as ctx:
        ctx.prec = 300
        exact = format(Decimal(price) * quantity, 'f')
    assert len(exact) <= 128
    p['totals']['gross_proceeds'] = exact
    value['snapshot_sha256'] = canonical_sha256(p)
    with localcontext() as ctx:
        ctx.prec = 6
        assert validate(value, req, bound, now)['totals']['gross_proceeds'] == exact
    with localcontext() as ctx:
        ctx.prec = 28
        rounded = format(+Decimal(exact), 'f')
    assert rounded != exact
    p['totals']['gross_proceeds'] = rounded
    value['snapshot_sha256'] = canonical_sha256(p)
    with pytest.raises(JournalError):
        validate(value, req, bound, now)


def test_bounded_per_request_observation_uses_no_stores_and_no_orphan_inference(books, canonical, monkeypatch):
    from ml.intraday import scout_journal
    from ml.intraday.scout_accounting import read_saved_accounting
    req, bound, now, response = canonical
    admitted(books, req)
    receipt = saved_accounting(books, req, bound, now, response)
    books.record_accounting(req, receipt, binding=bound, now=now)
    (books.root / 'accounting' / 'unrelated.json').write_text('{}')
    monkeypatch.setattr(ScoutBooks, '__init__', lambda *a, **k: pytest.fail('No store constructor'))
    monkeypatch.setattr(scout_journal.ScoutJournal, '__init__', lambda *a, **k: pytest.fail('No journal constructor'))
    checks = []
    value = read_saved_accounting(books.root, account_fingerprint=books.account_fingerprint,
        request=req.payload(), check_deadline=lambda: checks.append(True))
    assert value['latest_snapshot']['snapshot_sha256'] == response['snapshot_sha256']
    assert len(checks) >= 4
    def expired(): raise TimeoutError('synthetic total deadline')
    with pytest.raises(TimeoutError):
        read_saved_accounting(books.root, account_fingerprint=books.account_fingerprint,
            request=req.payload(), check_deadline=expired)


def test_per_request_absence_is_pending_without_creating_directories(books):
    from ml.intraday.scout_accounting import read_saved_accounting
    value = read_saved_accounting(books.root, account_fingerprint=books.account_fingerprint,
        request=request().payload(), check_deadline=lambda: None)
    assert value['pending'] is True and value['latest_snapshot'] is None
    assert not books.root.exists()


@pytest.mark.parametrize('value', ['00', '01', '1.0', '1.20', '0.0', '-0', '+1', '1E0', '.1', '1.',
                                   '0.'+'1'*127, '9'*129])
def test_published_canonical_money_schema_rejects_alternate_or_unbounded_spelling(value):
    from ml.intraday.scout_accounting import _money
    with pytest.raises(JournalError, match='canonical bounded decimal'):
        _money(value)


@pytest.mark.parametrize('raw', ['1E-8', '1.2300', '0.0000', '1E-126', '1E127', '1'*126+'.1'])
def test_published_atlas_numeric_outputs_conform_exactly_to_scout(raw):
    from decimal import Decimal, localcontext
    from ml.intraday.admission_views import _amount
    from ml.intraday.scout_accounting import _money
    with localcontext() as ctx:
        ctx.prec = 2
        output = _amount(raw)
        assert _money(output) == Decimal(raw)


@pytest.fixture
def producer(tmp_path, monkeypatch):
    """Actual published Atlas views and Scout client with temporary books only."""
    import socket
    from test_intraday_scout_authority import Setup
    from tests.test_intraday_admission_views import views
    def forbidden(*args, **kwargs):
        raise AssertionError('No network in producer/client conformance tests')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'bind', forbidden)
    setup = Setup(tmp_path)
    setup.views = views(setup.env)
    original = setup.transport
    class ViewTransport:
        def call(self, method, path, body=None):
            if path in {'/v1/readiness', '/v1/accounting'}:
                assert method == 'POST'
                return getattr(setup.views, path.rsplit('/', 1)[-1])('pc-new', deepcopy(body))
            return original.call(method, path, body)
    setup.client._transport = ViewTransport()
    try:
        yield setup
    finally:
        setup.journal.close()


@pytest.mark.parametrize('phase', ['RESERVED', 'SUBMITTING', 'SUBMITTED'])
def test_real_producer_client_and_books_preserve_exact_reduced_original_identity(producer, phase):
    from tests.test_intraday_admission_service import request as actual_request
    from tests.test_intraday_admission_views import fingerprints
    req = actual_request()
    if phase == 'RESERVED': producer.grant()
    elif phase == 'SUBMITTING': producer.authorized()
    else: producer.accepted()
    before = fingerprints(producer.env.root)
    captures = producer.env.captures
    receipt = producer.client.accounting(req)
    producer.books.record_accounting(req, receipt, binding=producer.binding, now=producer.env.clock())
    projection = receipt.response['projection']
    assert projection['request']['quantity'] == 20
    assert projection['account_reservation']['quantity'] == producer.record()['grant']['quantity'] == 10
    assert projection['account_reservation']['status'] == phase
    assert projection['native_reservation']['status'] == ('UNKNOWN' if phase == 'SUBMITTING' else phase)
    assert producer.books.accounting_state(req)['latest_snapshot']['snapshot_sha256'] == receipt.response['snapshot_sha256']
    assert audit(producer.books, [req.payload()])['snapshots'] == 1
    assert fingerprints(producer.env.root) == before and producer.env.captures == captures


def actual_status(producer, *, status, quantity, price, identity, prior=None):
    from ml.stock_trader.horizon_ledger import FillEvidence, OrderEvidence
    from tests.test_intraday_admission_service import request as actual_request
    producer.env.clock.tick()
    req, record = actual_request(), producer.record()
    fills = prior.fills if prior else (FillEvidence('canonical-fill', quantity, price, producer.env.clock().isoformat()),)
    total = record['grant']['quantity']
    evidence = OrderEvidence(identity, record['grant']['reservation_id'], req.account_fingerprint,
        producer.env.clock().isoformat(), '201', status, total, quantity,
        0 if status in {'FILLED', 'CANCELLED', 'REJECTED'} else total-quantity,
        fills, broker_status='CANCELED' if status == 'CANCELLED' else 'PARTIALLY_FILLED')
    producer.journal.observe_order(req.request_id, evidence, as_of=producer.env.clock().isoformat())
    producer.client.order_status(record, evidence)
    return evidence


@pytest.mark.parametrize('price', ['10', '0.00000001'])
def test_actual_cumulative_fill_terminal_and_later_provenance_remain_private_snapshots(producer, price):
    from tests.test_intraday_admission_service import request as actual_request
    from tests.test_intraday_admission_views import fingerprints
    req = actual_request()
    producer.accepted()
    partial = actual_status(producer, status='PARTIAL', quantity=2, price=price, identity='partial-canonical')
    receipts = []
    for status, identity in [('PARTIAL', 'partial-canonical'), ('CANCELLED', 'terminal-canonical'), ('CANCELLED', 'later-terminal-proof')]:
        if status == 'CANCELLED':
            actual_status(producer, status=status, quantity=2, price=price, identity=identity, prior=partial)
        before = fingerprints(producer.env.root)
        receipt = producer.client.accounting(req)
        producer.books.record_accounting(req, receipt, binding=producer.binding, now=producer.env.clock())
        assert fingerprints(producer.env.root) == before
        receipts.append(receipt)
    output = receipts[-1].response['projection']
    assert output['totals']['gross_cost'] == ('20' if price == '10' else '0.00000002')
    assert output['totals']['cost_basis'] is None and output['totals']['fees'] is None
    assert output['account_reservation']['status'] == 'CANCELLED'
    assert output['fills'] == receipts[0].response['projection']['fills']
    assert len(producer.books.accounting_state(req)['snapshots']) == 3
    assert audit(producer.books, [req.payload()])['snapshots'] == 3


@pytest.mark.parametrize('blocked', [False, True])
def test_actual_readiness_producer_matches_client_and_has_zero_book_effects(producer, blocked):
    from ml.intraday.scout_authority import validate_readiness_receipt
    from tests.test_intraday_admission_views import writers, fingerprints
    proofs = writers(producer.env.clock)
    if blocked:
        proofs['atlas'].update(ready=False, compatibility_state='blocked', reason_codes=['OLD_LOADED_WRITER'])
    else:
        proofs['atlas'].update(compatibility_state='compatible',
            expires_at=(producer.env.clock()+timedelta(seconds=2)).isoformat())
    producer.views.writers = lambda: proofs
    before = fingerprints(producer.env.root)
    captures = producer.env.captures
    receipt = producer.client.readiness()
    response = validate_readiness_receipt(receipt, producer.binding, now=producer.env.clock())
    assert response['ready'] is (not blocked)
    assert response['reason_codes'] == (['ATLAS:OLD_LOADED_WRITER'] if blocked else [])
    assert response['orders_or_commitments_created'] == 0
    assert fingerprints(producer.env.root) == before and producer.env.captures == captures
    if not blocked:
        producer.env.clock.tick(seconds=2)
        with pytest.raises(ValueError):
            validate_readiness_receipt(receipt, producer.binding, now=producer.env.clock())


def test_actual_sale_view_keeps_opening_inventory_attribution_and_unknown_basis(tmp_path, monkeypatch):
    import socket
    from dataclasses import asdict
    from ml.account_gameplan.authority import Fill, OrderEvidence
    from ml.intraday.scout_authority import ScoutAuthorityClient
    from test_intraday_scout_authority import binding
    from tests.test_intraday_admission_service import Fixture, request as actual_request, bound as wire_bound
    from tests.test_intraday_admission_views import views, fingerprints
    def forbidden(*args, **kwargs): raise AssertionError('No network in sale conformance')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'bind', forbidden)
    env = Fixture(tmp_path/'atlas')
    req, decision, claim = env.accepted(actual_request(side='SELL', quantity=3))
    env.clock.tick()
    evidence = OrderEvidence('canonical-sale-terminal', decision.reservation_id, req.account_fingerprint,
        '201', env.clock().isoformat(), 'CANCELLED', 3, 1, 0,
        (Fill('canonical-sold-one', 1, '10', env.clock().isoformat()),), complete=True)
    env.service.evidence('pc-new', wire_bound(req, event_id='canonical-sale-event', kind='ORDER_STATUS',
        claim_id=claim['claim_id'], order_evidence=json.loads(json.dumps(asdict(evidence)))))
    books = ScoutBooks(tmp_path/'scout', account_fingerprint=req.account_fingerprint)
    with books.create_session(req.session_id) as journal:
        journal.prepare(req)
        journal.grant(decision)
        journal.outcome(req.request_id, journal.claim_send(req.request_id), '201')
    service = views(env)
    class ViewTransport:
        def call(self, method, path, body):
            assert (method, path) == ('POST', '/v1/accounting')
            return service.accounting('pc-new', body)
    bound = binding(tmp_path)
    client = ScoutAuthorityClient(bound, books, transport=ViewTransport(), clock=env.clock)
    before = fingerprints(env.root)
    receipt = client.accounting(req)
    books.record_accounting(req, receipt, binding=bound, now=env.clock())
    value = receipt.response['projection']
    assert value['totals'] == dict(filled_quantity=1, gross_cost='0', gross_proceeds='10',
        cost_basis=None, realized_pnl=None, basis_status='unknown', fees=None)
    assert sum(row['quantity'] for row in value['sell_slices']) == 3
    assert sum(row['quantity'] for row in value['fill_attribution']) == 1
    assert value['allocations'][0]['observed_inventory'] and not value['allocations'][0]['purchase_fills']
    assert audit(books, [req.payload()])['snapshots'] == 1
    assert fingerprints(env.root) == before


@pytest.fixture
def paged_books(books, canonical, monkeypatch):
    from ml.intraday import scout_accounting as module
    monkeypatch.setattr(module, 'PAGE_SIZE', 2)
    req, bound, now, _ = canonical
    admitted(books, req)
    receipts = []
    def append(n):
        at = now + timedelta(seconds=n)
        response = accounting_response(req, bound, at, 'page-nonce-' + str(n))
        receipt = saved_accounting(books, req, bound, at, response)
        receipts.append(receipt)
        books.record_accounting(req, receipt, binding=bound, now=at)
        return receipt
    return module, books, req, bound, now, receipts, append


def test_history_rollover_keeps_bounded_exact_head_and_all_immutable_observations(paged_books):
    module, books, req, bound, now, receipts, append = paged_books
    for n in range(7): append(n)
    state = books.accounting_state(req)
    assert state['snapshot_count'] == 7 and len(state['snapshots']) == 1
    assert state['history']['pages'] == 3 and state['history']['count'] == 6
    original = {p: p.read_bytes() for p in (books.root/'accounting-history').rglob('*.json')}
    assert len(original) == 3
    assert audit(books, [req.payload()])['snapshots'] == 7
    raw = json.loads(next((books.root/'accounting').glob('*.json')).read_text())['accounting']
    assert module.read_saved_accounting(books.root, account_fingerprint=books.account_fingerprint,
        request=req.payload(), check_deadline=lambda: None) == raw == state
    append(7)
    assert all(p.read_bytes() == value for p, value in original.items())
    # Archived observation receipts cannot be reused as new current views.
    with pytest.raises(JournalError):
        books.record_accounting(req, receipts[1], binding=bound, now=now+timedelta(seconds=8))


def test_immutable_page_survives_crash_before_head_and_exact_retry_reuses_it(paged_books, monkeypatch):
    module, books, req, bound, now, receipts, append = paged_books
    append(0); append(1)
    before = books.accounting_state(req)
    at = now + timedelta(seconds=2)
    receipt = saved_accounting(books, req, bound, at, accounting_response(req, bound, at, 'page-nonce-2'))
    replace = module.os.replace
    monkeypatch.setattr(module.os, 'replace', lambda *args: (_ for _ in ()).throw(OSError('head crash')))
    with pytest.raises(OSError, match='head crash'):
        books.record_accounting(req, receipt, binding=bound, now=at)
    pages = {p: p.read_bytes() for p in (books.root/'accounting-history').rglob('*.json')}
    assert len(pages) == 1 and books.accounting_state(req) == before
    assert audit(books, [req.payload()])['snapshots'] == 2
    monkeypatch.setattr(module.os, 'replace', replace)
    books.record_accounting(req, receipt, binding=bound, now=at)
    assert books.accounting_state(req)['snapshot_count'] == 3
    assert all(p.read_bytes() == raw for p, raw in pages.items())
    assert audit(books, [req.payload()])['snapshots'] == 3


@pytest.mark.parametrize('damage', ['missing', 'mutated'])
def test_history_page_corruption_is_detected_on_cached_runtime_and_pure_reads(paged_books, damage):
    module, books, req, _, _, _, append = paged_books
    for n in range(5): append(n)
    books.accounting_state(req)  # Populate runtime sealed-page cache.
    path = sorted((books.root/'accounting-history').rglob('*.json'))[0]
    if damage == 'missing': path.unlink()
    else: path.write_bytes(path.read_bytes()+b' ')
    with pytest.raises(JournalError): books.accounting_state(req)
    if damage == 'mutated':
        # A canonical content hash still allows harmless outer whitespace; make
        # a substantive content mutation for the uncached semantic audit.
        value = json.loads(path.read_text()); value['page']['observations'][0]['observed_at'] = '1999-01-01T00:00:00+00:00'
        path.write_text(json.dumps(value))
    with pytest.raises(JournalError):
        module.read_saved_accounting(books.root, account_fingerprint=books.account_fingerprint,
            request=req.payload(), check_deadline=lambda: None)


def test_saved_history_replays_receipts_and_honors_bounded_deadline(paged_books, monkeypatch):
    module, books, req, _, _, _, append = paged_books
    for n in range(7): append(n)
    count = [0]
    def deadline():
        count[0] += 1
        if count[0] == 6: raise TimeoutError('bounded history audit')
    with pytest.raises(TimeoutError):
        module.read_saved_accounting(books.root, account_fingerprint=books.account_fingerprint,
            request=req.payload(), check_deadline=deadline)
    # Runtime reuses already-validated sealed receipts; pure reads replay all.
    books.accounting_state(req)
    original = module._snapshot
    calls = []
    def counted(*args, **kwargs):
        calls.append(args[2]['authority_receipt']['sha256'])
        return original(*args, **kwargs)
    monkeypatch.setattr(module, '_snapshot', counted)
    books.accounting_state(req)
    assert len(calls) <= module.PAGE_SIZE + 1
    calls.clear()
    module.read_saved_accounting(books.root, account_fingerprint=books.account_fingerprint,
        request=req.payload(), check_deadline=lambda: None)
    assert len(calls) >= 7


def test_read_only_audit_detects_old_receipt_mutation_after_runtime_cached_history(paged_books):
    module, books, req, _, _, receipts, append = paged_books
    for n in range(5): append(n)
    books.accounting_state(req)
    old = Path(receipts[0].path)
    old.write_bytes(old.read_bytes()+b'changed')
    # Runtime's explicit sealed-history cache scope does not claim a new full
    # historical receipt audit. The pure prestart/watch reader always does.
    with pytest.raises(JournalError):
        module.read_saved_accounting(books.root, account_fingerprint=books.account_fingerprint,
            request=req.payload(), check_deadline=lambda: None)


def legacy_index(books, req):
    from ml.intraday import scout_accounting as module
    path = next((books.root/'accounting').glob('*.json'))
    wrapper = json.loads(path.read_text())
    value = wrapper['accounting']
    assert value['history'] is None
    value.pop('history'); value.pop('snapshot_count')
    value['version'] = module.LEGACY_BOOK_VERSION
    wrapper['sha256'] = canonical_sha256(value)
    path.write_text(json.dumps(wrapper))
    return path, value


def test_intact_v1_is_read_without_mutation_and_migrates_only_on_locked_write(books, canonical):
    from ml.intraday import scout_accounting as module
    req, bound, now, response = canonical
    admitted(books, req)
    receipt = saved_accounting(books, req, bound, now, response)
    books.record_accounting(req, receipt, binding=bound, now=now)
    path, legacy = legacy_index(books, req)
    before = path.read_bytes()
    assert module.read_saved_accounting(books.root, account_fingerprint=books.account_fingerprint,
        request=req.payload(), check_deadline=lambda: None) == legacy
    assert path.read_bytes() == before
    books.record_accounting_pending(req, reason_code='new-attempt', observed_at=now.isoformat())
    value = books.accounting_state(req)
    assert value['version'] == module.BOOK_VERSION and value['snapshot_count'] == 1
    assert Path(receipt.path).exists()


@pytest.mark.parametrize('ambiguity', ['latest-dedup', 'unindexed'])
def test_ambiguous_legacy_migration_preserves_original_index_and_receipts(books, canonical, ambiguity):
    from ml.intraday import scout_accounting as module
    req, bound, now, response = canonical
    admitted(books, req)
    first = saved_accounting(books, req, bound, now, response)
    books.record_accounting(req, first, binding=bound, now=now)
    path, legacy = legacy_index(books, req)
    later = saved_accounting(books, req, bound, now, accounting_response(req, bound, now, 'nonce-legacy-later'))
    if ambiguity == 'latest-dedup':
        legacy['latest_snapshot'] = dict(legacy['latest_snapshot'], authority_receipt=later.saved)
        legacy['last_attempt'] = later.saved
        path.write_text(json.dumps(dict(accounting=legacy,sha256=canonical_sha256(legacy))))
    before = path.read_bytes()
    assert module.read_saved_accounting(books.root, account_fingerprint=books.account_fingerprint,
        request=req.payload(), check_deadline=lambda: None) == legacy
    with pytest.raises(JournalError, match='Ambiguous'):
        books.record_accounting_pending(req, reason_code='retry', observed_at=now.isoformat())
    assert path.read_bytes() == before and Path(first.path).exists() and Path(later.path).exists()



def seed_paged_accounting(books, req, bound, now, observations):
    """Exact synthetic receipts + one locked bulk head write, for raw-reader tests.

    This fixture bypasses repeated append cost, not validation: pure readers
    subsequently validate every actual saved authority receipt and page.
    """
    from ml.intraday import scout_accounting as module
    state = books.accounting_state(req)
    state['bindings'][bound.sha256] = bound.payload()
    receipts = []
    for n in range(observations):
        value = accounting_response(req, bound, now, 'seed-nonce-' + str(n))
        receipt = saved_accounting(books, req, bound, now, value)
        receipts.append(receipt)
        state['snapshots'].append(dict(snapshot_sha256=value['snapshot_sha256'], authority_receipt=receipt.saved,
            binding_sha256=bound.sha256, observed_at=now.isoformat()))
    state.update(latest_snapshot=state['snapshots'][-1], last_attempt=receipts[-1].saved, pending=False)
    with books.locked():
        module._write(books, req, state)
    return receipts


def test_actual_128_observation_page_boundary_and_cross_page_equal_time_replay(books, canonical):
    from ml.intraday import scout_accounting as module
    req, bound, now, _ = canonical
    admitted(books, req)
    receipts = seed_paged_accounting(books, req, bound, now, 129)
    assert module.PAGE_SIZE == 128
    value = module.read_saved_accounting(books.root, account_fingerprint=books.account_fingerprint,
        request=req.payload(), check_deadline=lambda: None)
    assert value['snapshot_count'] == 129 and len(value['snapshots']) == 1
    assert value['history']['pages'] == 1
    with pytest.raises(JournalError, match='old canonical receipt'):
        books.record_accounting(req, receipts[1], binding=bound, now=now)
    assert audit(books, [req.payload()])['snapshots'] == 129


def test_long_history_exceeds_old_json_cap_without_growing_head_or_dropping_receipts(books, canonical):
    from ml.intraday import scout_accounting as module
    req, bound, now, _ = canonical
    admitted(books, req)
    receipts = seed_paged_accounting(books, req, bound, now, 2500)
    path = next((books.root/'accounting').glob('*.json'))
    assert path.stat().st_size < 100_000
    value = module.read_saved_accounting(books.root, account_fingerprint=books.account_fingerprint,
        request=req.payload(), check_deadline=lambda: None)
    assert value['snapshot_count'] == 2500 and len(value['snapshots']) <= module.PAGE_SIZE
    assert value['history']['pages'] == 19 and value['history']['count'] == 2432
    assert len(receipts) == len(set(receipt.sha256 for receipt in receipts)) == 2500
    assert all(Path(receipt.path).is_file() for receipt in receipts)



def test_crash_after_page_rename_before_cleanup_keeps_standalone_retryable_page(paged_books, monkeypatch):
    module, books, req, bound, now, receipts, append = paged_books
    append(0); append(1)
    before = books.accounting_state(req)
    at = now + timedelta(seconds=2)
    receipt = saved_accounting(books, req, bound, at, accounting_response(req, bound, at, 'nonce-after-rename'))
    real_unlink = Path.unlink
    def crash_on_staging_cleanup(path, *args, **kwargs):
        if path.name.startswith('.page-'):
            raise SystemExit('process ended after page publication')
        return real_unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', crash_on_staging_cleanup)
        with pytest.raises(SystemExit, match='after page publication'):
            books.record_accounting(req, receipt, binding=bound, now=at)
    pages = list((books.root/'accounting-history').rglob('*.json'))
    assert len(pages) == 1 and pages[0].stat().st_nlink == 1
    assert books.accounting_state(req) == before
    assert audit(books, [req.payload()])['snapshots'] == 2
    books.record_accounting(req, receipt, binding=bound, now=at)
    assert books.accounting_state(req)['snapshot_count'] == 3
    assert audit(books, [req.payload()])['snapshots'] == 3


def test_atomic_page_publication_never_overwrites_a_different_existing_destination(paged_books, monkeypatch):
    module, books, req, _, _, _, append = paged_books
    append(0); append(1)
    original_rename = module.os.rename
    targets = []
    def racing_destination(source, destination):
        targets.append(Path(destination))
        Path(destination).write_bytes(b'other writer bytes')
        return original_rename(source, destination)
    monkeypatch.setattr(module.os, 'rename', racing_destination)
    with pytest.raises(JournalError, match='Concurrent immutable accounting page differs'):
        append(2)
    assert len(targets) == 1 and targets[0].read_bytes() == b'other writer bytes'
    assert books.accounting_state(req)['snapshot_count'] == 2
