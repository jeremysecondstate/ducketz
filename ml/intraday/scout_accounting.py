"""Private canonical-accounting evidence, separate from order authority.

Accounting may lag immediate reservation/order reconciliation. A failed view
keeps the most recent valid snapshot and an explicit pending receipt; it never
releases capacity or rearms a submission. The wire projection validator is
defined against the published Atlas accounting contract, not local estimates.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from .scout_journal import JournalError, _safe_path
from .scout_protocol import PARTICIPANT_ID, STREAM, MAX_INTEGER, Request, canonical_sha256, _timestamp

LEGACY_BOOK_VERSION = 'scout-canonical-accounting-books-v1'
BOOK_VERSION = 'scout-canonical-accounting-books-v2'
PAGE_VERSION = 'scout-canonical-accounting-page-v1'
PAGE_SIZE = 128
ACCOUNTING_VERSION = 'intraday-canonical-accounting-v1'
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', re.ASCII)
_HASH = re.compile(r'[a-f0-9]{64}', re.ASCII)
_MONEY = re.compile(r'(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?', re.ASCII)
_MAX_BYTES = 2 * 1024 * 1024
_ACCOUNT_STATES = {'RESERVED', 'SUBMITTING', 'SUBMITTED', 'UNKNOWN', 'WORKING', 'PARTIAL',
                   'FILLED', 'CANCELLED', 'REJECTED', 'NOT_SUBMITTED'}
_TERMINAL = {'FILLED', 'CANCELLED', 'REJECTED', 'NOT_SUBMITTED'}
_ENVELOPE = {'version', 'protocol_version', 'server_identity', 'participant_id', 'stream',
             'account_fingerprint', 'request_nonce', 'observed_at', 'expires_at', 'authenticated',
             'protocol_compatible', 'read_only', 'orders_or_commitments_created'}


def _fields(value, fields, name):
    _require(type(value) is dict and set(value) == set(fields), 'Exact accounting ' + name + ' fields required')


def _identifier(value, name):
    _require(type(value) is str and _ID.fullmatch(value), 'Invalid accounting ' + name)
    return value


def _hash(value):
    _require(type(value) is str and _HASH.fullmatch(value), 'Invalid accounting SHA256')
    return value


def _quantity(value, *, positive=False):
    _require(type(value) is int and (1 if positive else 0) <= value <= MAX_INTEGER,
             'Accounting quantity/ordinal must be a bounded whole integer')
    return value


def _money(value, *, positive=False):
    _require(type(value) is str and len(value) <= 128
             and _MONEY.fullmatch(value) and len(value.partition('.')[2]) <= 126,
             'Accounting money must retain its canonical bounded decimal string')
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise JournalError('Invalid accounting decimal') from exc
    _require(result.is_finite() and result >= 0 and (not positive or result > 0),
             'Accounting money must be finite and nonnegative')
    return result


def _time(value):
    return datetime.fromisoformat(_timestamp(value, 'accounting timestamp'))


def _list(value, name):
    _require(type(value) is list, 'Accounting ' + name + ' must be an array')
    return value


def validate_accounting_projection(value, request, *, observed_at):
    """Validate the canonical per-request projection; never estimate its fields."""
    _require(type(request) is Request, 'Exact original accounting request required')
    canonical_sha256(value)  # Finite bounded JSON before any nested arithmetic.
    _fields(value, {'request', 'account_reservation', 'native_reservation', 'allocations', 'fills',
                    'sell_slices', 'fill_attribution', 'totals', 'provenance'}, 'projection')
    observed = _time(observed_at)
    original = value['request']
    _fields(original, {'symbol', 'side', 'quantity', 'limit_price', 'forecast_id'}, 'request')
    _quantity(original['quantity'], positive=True)
    _money(original['limit_price'], positive=True)
    _require(original == {name: getattr(request, name) for name in original},
             'Accounting request differs from original immutable economics')
    account, native = value['account_reservation'], value['native_reservation']
    _fields(account, {'reservation_id', 'status', 'quantity', 'filled_quantity', 'broker_order_id',
                      'created_at', 'last_evidence_at'}, 'account reservation')
    _fields(native, {'reservation_id', 'allocation_id', 'status', 'quantity', 'filled_quantity',
                     'broker_order_id', 'last_evidence_at'}, 'native reservation')
    for row in (account, native):
        _identifier(row['reservation_id'], 'reservation ID')
        _quantity(row['quantity'], positive=True)
        _quantity(row['filled_quantity'])
        _require(row['filled_quantity'] <= row['quantity'] <= request.quantity,
                 'Canonical reservation quantity exceeds original request')
        _require(type(row['status']) is str, 'Canonical reservation status must be a string')
        if row['broker_order_id'] is not None:
            _require(type(row['broker_order_id']) is str
                     and re.fullmatch(r'[1-9][0-9]{0,63}', row['broker_order_id'], re.ASCII),
                     'Exact accepted broker order ID required')
        if row['last_evidence_at'] is not None:
            _require(_time(row['last_evidence_at']) <= observed, 'Accounting order evidence is future dated')
    _identifier(native['allocation_id'], 'native allocation ID')
    _require(account['status'] in _ACCOUNT_STATES
             and native['status'] == {'SUBMITTING': 'UNKNOWN', 'NOT_SUBMITTED': 'REJECTED'}.get(account['status'], account['status'])
             and all(account[name] == native[name] for name in ('quantity', 'filled_quantity', 'broker_order_id')),
             'Canonical account/native reservation pair differs')
    _require(_time(request.issued_at) <= _time(account['created_at']) <= observed,
             'Canonical account creation time differs from original request')
    filled, quantity, status = account['filled_quantity'], account['quantity'], account['status']
    _require(not (status in {'RESERVED', 'SUBMITTING', 'SUBMITTED', 'UNKNOWN', 'WORKING', 'REJECTED', 'NOT_SUBMITTED'} and filled)
             and not (status == 'PARTIAL' and not 0 < filled < quantity)
             and not (status == 'FILLED' and filled != quantity), 'Accounting status and fills disagree')
    if status in {'RESERVED', 'SUBMITTING', 'NOT_SUBMITTED'}:
        _require(account['broker_order_id'] is None, 'Unsubmitted accounting reservation has an accepted order')
    if status in {'SUBMITTED', 'WORKING', 'PARTIAL', 'FILLED', 'CANCELLED'} or filled:
        _require(account['broker_order_id'] is not None, 'Accounting order lacks its exact broker identity')

    fills = {}
    for fill in _list(value['fills'], 'fills'):
        _fields(fill, {'fill_id', 'quantity', 'price', 'executed_at', 'native_reservation_id',
                       'account_reservation_id'}, 'fill')
        identity = _identifier(fill['fill_id'], 'fill ID')
        _require(identity not in fills, 'Duplicate canonical fill ID')
        _quantity(fill['quantity'], positive=True)
        price = _money(fill['price'], positive=True)
        executed = _time(fill['executed_at'])
        # Canonical authority evidence already checked original broker time
        # provenance. Its projection omits that metadata; preserve the existing
        # possible whole-second creation boundary, never an earlier second.
        created = _time(account['created_at'])
        lower = created <= executed or (executed.microsecond == 0 and executed == created.replace(microsecond=0))
        _require(lower and executed <= observed
                 and fill['native_reservation_id'] == native['reservation_id']
                 and fill['account_reservation_id'] == account['reservation_id'],
                 'Canonical fill time/reservation identity differs')
        _require(price <= Decimal(request.limit_price) if request.side == 'BUY' else price >= Decimal(request.limit_price),
                 'Canonical fill violates original limit price')
        fills[identity] = fill
    _require(sum(fill['quantity'] for fill in fills.values()) == filled, 'Complete canonical fills must equal cumulative quantity')
    if filled:
        _require(all(row['last_evidence_at'] is not None
                     and _time(row['last_evidence_at']) >= max(_time(fill['executed_at']) for fill in fills.values())
                     for row in (account, native)), 'Canonical fills require matching order evidence time')

    slices = {}
    for row in _list(value['sell_slices'], 'sell slices'):
        _fields(row, {'allocation_id', 'quantity', 'ordinal'}, 'sell slice')
        _identifier(row['allocation_id'], 'slice allocation ID')
        _quantity(row['quantity'], positive=True)
        ordinal = _quantity(row['ordinal'])
        _require(ordinal not in slices, 'Duplicate sell-slice ordinal')
        slices[ordinal] = row
    _require(set(slices) == set(range(len(slices))), 'Canonical sell-slice ordinals must be complete')
    if request.side == 'BUY':
        _require(not slices and value['fill_attribution'] == [], 'Buy accounting cannot contain sell attribution')
    else:
        _require(sum(row['quantity'] for row in slices.values()) == quantity,
                 'Complete sell slices must equal admitted quantity')
    attribution = set()
    by_fill = {name: 0 for name in fills}
    by_slice = {ordinal: 0 for ordinal in slices}
    for row in _list(value['fill_attribution'], 'fill attribution'):
        _fields(row, {'fill_id', 'allocation_id', 'quantity', 'slice_ordinal'}, 'fill attribution')
        _identifier(row['fill_id'], 'attributed fill ID')
        _identifier(row['allocation_id'], 'attributed allocation ID')
        _quantity(row['quantity'], positive=True)
        ordinal = _quantity(row['slice_ordinal'])
        key = (row['fill_id'], ordinal)
        _require(key not in attribution and row['fill_id'] in fills and ordinal in slices
                 and row['allocation_id'] == slices[ordinal]['allocation_id'],
                 'Canonical fill attribution differs from exact fill/slice identity')
        attribution.add(key)
        by_fill[row['fill_id']] += row['quantity']
        by_slice[ordinal] += row['quantity']
    if request.side == 'SELL':
        _require(all(by_fill[name] == fill['quantity'] for name, fill in fills.items())
                 and all(amount <= slices[ordinal]['quantity'] for ordinal, amount in by_slice.items()),
                 'Canonical attributed quantities do not balance')

    allocations, purchases, assignments = {}, {}, set()
    for row in _list(value['allocations'], 'allocations'):
        _fields(row, {'allocation_id', 'symbol', 'horizon', 'forecast_id', 'target_start', 'target_end',
                      'status', 'purchase_fills', 'observed_inventory'}, 'allocation')
        identity = _identifier(row['allocation_id'], 'allocation ID')
        _require(identity not in allocations and row['symbol'] == request.symbol
                 and type(row['horizon']) is str and row['horizon'] in {'15m', '1h', '4h', '1d', '1w'}
                 and type(row['status']) is str and row['status'] in {'ACTIVE', 'CLOSED'},
                 'Canonical allocation scope or status differs')
        _identifier(row['forecast_id'], 'allocation forecast ID')
        _require(_time(row['target_start']) < _time(row['target_end']), 'Canonical allocation target is invalid')
        for fill in _list(row['purchase_fills'], 'purchase fills'):
            _fields(fill, {'fill_id', 'native_reservation_id', 'quantity', 'price', 'executed_at'}, 'purchase fill')
            fid = _identifier(fill['fill_id'], 'purchase fill ID')
            _identifier(fill['native_reservation_id'], 'purchase reservation ID')
            _require(fid not in purchases, 'Canonical purchase fill duplicated across allocations')
            _quantity(fill['quantity'], positive=True)
            _money(fill['price'], positive=True)
            _require(_time(fill['executed_at']) <= observed, 'Canonical purchase fill is future dated')
            purchases[fid] = (identity, fill)
        for item in _list(row['observed_inventory'], 'observed inventory'):
            _fields(item, {'id', 'quantity', 'snapshot_id', 'observed_at'}, 'observed inventory')
            aid = _identifier(item['id'], 'observed assignment ID')
            _identifier(item['snapshot_id'], 'inventory snapshot ID')
            _require(aid not in assignments, 'Observed inventory assignment duplicated across allocations')
            _quantity(item['quantity'], positive=True)
            _require(_time(item['observed_at']) <= observed, 'Observed inventory is future dated')
            assignments.add(aid)
        allocations[identity] = row
    _require(set(allocations) == {native['allocation_id']} | {row['allocation_id'] for row in slices.values()},
             'Accounting contains missing or unrelated allocations')
    _require(allocations[native['allocation_id']]['horizon'] == '15m', 'Original Scout allocation must retain its 15m owner')
    for fid, fill in fills.items():
        if request.side == 'SELL':
            _require(fid not in purchases, 'A sell fill cannot become purchase inventory')
        else:
            _require(fid in purchases and purchases[fid][0] == native['allocation_id'],
                     'Current buy fill missing from original allocation')
            purchase = purchases[fid][1]
            _require(purchase['native_reservation_id'] == native['reservation_id']
                     and purchase['quantity'] == fill['quantity'] and Decimal(purchase['price']) == Decimal(fill['price'])
                     and _time(purchase['executed_at']) == _time(fill['executed_at']),
                     'Current buy fill and canonical purchase provenance differ')

    totals = value['totals']
    _fields(totals, {'filled_quantity', 'gross_cost', 'gross_proceeds', 'cost_basis', 'realized_pnl',
                     'basis_status', 'fees'}, 'totals')
    _quantity(totals['filled_quantity'])
    cost, proceeds = _money(totals['gross_cost']), _money(totals['gross_proceeds'])
    with localcontext() as context:
        context.prec = max(64, sum(len(fill['price']) for fill in fills.values())
                           + len(totals['gross_cost']) + len(totals['gross_proceeds']) + 64)
        gross = sum((Decimal(fill['price']) * fill['quantity'] for fill in fills.values()), Decimal(0))
    _require(totals['filled_quantity'] == filled
             and cost == (gross if request.side == 'BUY' else 0)
             and proceeds == (gross if request.side == 'SELL' else 0)
             and totals['cost_basis'] is None and totals['realized_pnl'] is None and totals['fees'] is None
             and totals['basis_status'] == 'unknown', 'Canonical gross totals or unknown basis/fees differ')
    provenance = value['provenance']
    _fields(provenance, {'journal_request_sha256', 'authority_request_sha256', 'native_request_sha256',
                         'account_order_evidence_sha256', 'native_order_evidence_sha256'}, 'provenance')
    _require(provenance['journal_request_sha256'] == request.sha256, 'Canonical journal request hash differs')
    for name in ('journal_request_sha256', 'authority_request_sha256', 'native_request_sha256'):
        _hash(provenance[name])
    for name in ('account_order_evidence_sha256', 'native_order_evidence_sha256'):
        values = _list(provenance[name], name)
        _require(len(set(_hash(item) for item in values)) == len(values), 'Duplicate canonical evidence hash')
    return deepcopy(value)


def validate_accounting_response(response, request, binding, *, request_nonce, requested_at, received_at, now):
    from .scout_authority import validate_view_envelope
    _fields(response, _ENVELOPE | {'request_id', 'request_sha256', 'session_id', 'status',
                                   'snapshot_sha256', 'projection'}, 'response')
    validate_view_envelope(response, binding, version=ACCOUNTING_VERSION, request_nonce=request_nonce,
                          requested_at=requested_at, received_at=received_at, now=now)
    _require(type(request) is Request and request.account_fingerprint == binding.account_fingerprint
             and request.symbol in binding.symbols and response['request_id'] == request.request_id
             and response['request_sha256'] == request.sha256 and response['session_id'] == request.session_id
             and response['status'] == 'AVAILABLE', 'Accounting view differs from original Scout request')
    _hash(response['snapshot_sha256'])
    _require(canonical_sha256(response['projection']) == response['snapshot_sha256'],
             'Canonical accounting snapshot digest differs')
    return validate_accounting_projection(response['projection'], request, observed_at=response['observed_at'])


def validate_accounting_receipt(request, receipt, binding, *, now):
    from .scout_authority import validate_view_receipt, _view_payload
    _require(type(request) is Request, 'Original Request required for a canonical accounting receipt')
    body = _view_payload(receipt).get('request')
    _fields(body, {'request_nonce', 'request_id', 'request_sha256', 'session_id'}, 'view request')
    nonce = _identifier(body['request_nonce'], 'request nonce')
    expected = dict(request_nonce=nonce, request_id=request.request_id,
                    request_sha256=request.sha256, session_id=request.session_id)
    response, requested, received = validate_view_receipt(receipt, binding, operation='accounting',
        request_id=request.request_id, request=expected, now=now)
    return validate_accounting_response(response, request, binding, request_nonce=nonce,
        requested_at=requested, received_at=received, now=now)


def _retained_unindexed_view(payload, request):
    """Audit retained bytes, without promoting an interrupted save to a snapshot.

    An interruption after the client saves its immutable receipt can precede
    recording the original binding in this index. Preserve that request's data;
    a later authenticated view must still pass record_accounting normally.
    """
    from .scout_authority import RECEIPT_VERSION, PROTOCOL_VERSION
    _fields(payload, {'version', 'binding_sha256', 'operation', 'request', 'response', 'received_at',
                      'requested_at'}, 'retained view receipt')
    _hash(payload['binding_sha256'])
    _require(payload['version'] == RECEIPT_VERSION and payload['operation'] == 'accounting',
             'Retained accounting receipt has a foreign operation')
    body = payload['request']
    _fields(body, {'request_nonce', 'request_id', 'request_sha256', 'session_id'}, 'retained view request')
    _identifier(body['request_nonce'], 'retained nonce')
    _require(body == dict(request_nonce=body['request_nonce'], request_id=request.request_id,
                         request_sha256=request.sha256, session_id=request.session_id),
             'Retained accounting request differs from original journal')
    response = payload['response']
    _fields(response, _ENVELOPE | {'request_id', 'request_sha256', 'session_id', 'status',
                                   'snapshot_sha256', 'projection'}, 'retained response')
    _identifier(response['server_identity'], 'retained server identity')
    _require(response['version'] == ACCOUNTING_VERSION and response['protocol_version'] == PROTOCOL_VERSION
             and response['account_fingerprint'] == request.account_fingerprint
             and response['participant_id'] == PARTICIPANT_ID and response['stream'] == STREAM
             and all(response[name] == body[name] for name in body)
             and response['status'] == 'AVAILABLE'
             and all(response[name] is True for name in ('authenticated', 'read_only', 'protocol_compatible'))
             and type(response['orders_or_commitments_created']) is int and response['orders_or_commitments_created'] == 0,
             'Retained accounting scope or zero-effect declaration differs')
    requested, observed, received, expires = map(_time, (payload['requested_at'], response['observed_at'],
                                                        payload['received_at'], response['expires_at']))
    _require(requested <= observed <= received < expires and 0 < (expires-observed).total_seconds() <= 30,
             'Retained accounting observation time differs')
    _require(_hash(response['snapshot_sha256']) == canonical_sha256(response['projection']),
             'Retained canonical snapshot hash differs')
    validate_accounting_projection(response['projection'], request, observed_at=response['observed_at'])


def _require(value, message):
    if not value:
        raise JournalError(message)


def _json(path):
    _safe_path(path)
    _require(path.stat().st_size <= _MAX_BYTES, 'Private accounting evidence exceeds bounded size')
    raw = path.read_bytes()
    return _json_bytes(raw)


def _json_bytes(raw):
    _require(len(raw) <= _MAX_BYTES, 'Private accounting evidence exceeds bounded size')
    def unique(pairs):
        result = {}
        for name, value in pairs:
            _require(name not in result, 'Duplicate field in private accounting evidence')
            result[name] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def _identity(books, request):
    _require(type(request) is Request and request.account_fingerprint == books.account_fingerprint,
             'Accounting request differs from private book identity')
    return dict(version=BOOK_VERSION, account_fingerprint=books.account_fingerprint,
                participant_id=PARTICIPANT_ID, stream=STREAM,
                request=request.payload(), request_sha256=request.sha256)


def _path(books, request):
    # The path binds the original request ID rather than its mutable payload,
    # so a reused ID with changed economics cannot silently create a new book.
    name = canonical_sha256({'request_id': request.request_id}) + '.json'
    return _safe_path(books.root / 'accounting' / name)


def _receipt_envelope(books, value, request, *, kind):
    _require(type(value) is dict and set(value) == {'sha256', 'path'}
             and type(value['sha256']) is str and _HASH.fullmatch(value['sha256']),
             'Invalid private accounting receipt reference')
    expected = _safe_path(books.root / 'receipts' / (value['sha256'] + '.json'))
    _require(type(value['path']) is str and Path(value['path']) == expected,
             'Accounting receipt must remain in its original private books')
    from .scout_books import VERSION
    try:
        envelope = _json(expected)
        _require(canonical_sha256(envelope) == value['sha256']
                 and set(envelope) == {'version', 'account_fingerprint', 'participant_id', 'stream',
                                       'kind', 'request_id', 'payload'}
                 and envelope['version'] == VERSION
                 and envelope['account_fingerprint'] == books.account_fingerprint
                 and envelope['participant_id'] == PARTICIPANT_ID and envelope['stream'] == STREAM
                 and envelope['request_id'] == request.request_id
                 and envelope['kind'] in ((kind,) if type(kind) is str else kind),
                 'Saved accounting receipt identity or bytes differ')
        return envelope
    except (OSError, TypeError, ValueError, KeyError) as exc:
        raise JournalError('Saved accounting receipt failed validation') from exc


def _receipt(books, value, request, *, kind):
    return _receipt_envelope(books, value, request, kind=kind)['payload']


def _stored_binding(value, digest, books):
    from .scout_authority import ScoutAuthorityBinding, BINDING_VERSION
    fields = set(ScoutAuthorityBinding.__dataclass_fields__)
    _fields(value, fields | {'version', 'participant_id', 'stream'}, 'saved authority binding')
    _require(value['version'] == BINDING_VERSION and value['participant_id'] == PARTICIPANT_ID
             and value['stream'] == STREAM and type(value['symbols']) is list,
             'Saved accounting binding version or participant differs')
    binding = ScoutAuthorityBinding(**{k: tuple(value[k]) if k == 'symbols' else value[k] for k in fields})
    _require(binding.account_fingerprint == books.account_fingerprint and binding.sha256 == digest,
             'Saved accounting binding digest differs')
    return binding


def _saved_view(books, request, reference, bindings):
    from .scout_authority import AuthorityReceipt
    payload = _receipt(books, reference, request, kind='authority:accounting')
    _require(type(payload) is dict and payload.get('binding_sha256') in bindings,
             'Accounting receipt has no original reviewed binding')
    binding = bindings[payload['binding_sha256']]
    receipt = AuthorityReceipt(json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False),
                               reference['sha256'], reference['path'])
    projection = validate_accounting_receipt(request, receipt, binding, now=_time(payload['received_at']))
    return payload, projection


def _snapshot(books, request, value, bindings):
    _fields(value, {'snapshot_sha256', 'authority_receipt', 'binding_sha256', 'observed_at'}, 'snapshot reference')
    _hash(value['snapshot_sha256'])
    _hash(value['binding_sha256'])
    payload, projection = _saved_view(books, request, value['authority_receipt'], bindings)
    _require(value['binding_sha256'] == payload['binding_sha256']
             and value['snapshot_sha256'] == payload['response']['snapshot_sha256']
             and value['observed_at'] == payload['response']['observed_at'],
             'Private snapshot reference differs from immutable authority evidence')
    return projection


def _progress(previous, current):
    """Retain immutable order economics; no inferred accounting-complete state."""
    _require(previous['request'] == current['request'] and previous['sell_slices'] == current['sell_slices'],
             'Canonical original request or sell reservations changed')
    for section, identity in (('account_reservation', ('reservation_id', 'quantity', 'created_at')),
                              ('native_reservation', ('reservation_id', 'allocation_id', 'quantity'))):
        old, new = previous[section], current[section]
        _require(all(old[name] == new[name] for name in identity)
                 and new['filled_quantity'] >= old['filled_quantity']
                 and (old['broker_order_id'] is None or new['broker_order_id'] == old['broker_order_id']),
                 'Canonical reservation identity/fills regressed')
        if old['status'] in _TERMINAL:
            _require(new['status'] == old['status'] and new['filled_quantity'] == old['filled_quantity'],
                     'Terminal canonical order economics changed')
        if old['last_evidence_at'] is not None:
            _require(new['last_evidence_at'] is not None
                     and _time(new['last_evidence_at']) >= _time(old['last_evidence_at']),
                     'Canonical order evidence time regressed')
    for section, key in (('fills', lambda row: row['fill_id']),
                         ('fill_attribution', lambda row: (row['fill_id'], row['slice_ordinal']))):
        new = {key(row): row for row in current[section]}
        _require(all(new.get(key(row)) == row for row in previous[section]),
                 'Immutable canonical fill/attribution disappeared or changed')
    allocations = {row['allocation_id']: row for row in current['allocations']}
    for old in previous['allocations']:
        new = allocations.get(old['allocation_id'])
        _require(new is not None and all(old[name] == new[name] for name in
                 ('allocation_id', 'symbol', 'horizon', 'forecast_id', 'target_start', 'target_end')),
                 'Immutable canonical allocation creation facts disappeared or changed')
        # An existing allocation may receive new purchases/assignments and may
        # reopen. Its original creation facts and recorded inventory provenance
        # remain immutable; releases are separate canonical ledger events.
        for section, key in (('purchase_fills', 'fill_id'), ('observed_inventory', 'id')):
            retained = {row[key]: row for row in new[section]}
            _require(all(retained.get(row[key]) == row for row in old[section]),
                     'Immutable canonical purchase/assignment facts disappeared or changed')
    _require(all(previous['provenance'][name] == current['provenance'][name]
                 for name in ('journal_request_sha256', 'authority_request_sha256', 'native_request_sha256')),
             'Canonical original request provenance changed')


def _attempt(books, request, reference, bindings):
    envelope = _receipt_envelope(books, reference, request, kind=('accounting:pending', 'authority:accounting'))
    if envelope['kind'] == 'accounting:pending':
        _pending_payload(envelope['payload'], request)
        return True, envelope['payload']['observed_at'], None
    payload, projection = _saved_view(books, request, reference, bindings)
    return False, payload['response']['observed_at'], projection


def _pending_payload(value, request):
    _require(type(value) is dict and set(value) == {'version', 'request_sha256', 'session_id',
                                                   'reason_code', 'observed_at'}
             and value['version'] in {LEGACY_BOOK_VERSION, BOOK_VERSION} and value['request_sha256'] == request.sha256
             and value['session_id'] == request.session_id
             and type(value['reason_code']) is str and _ID.fullmatch(value['reason_code']),
             'Pending accounting evidence differs from original request')
    try:
        _timestamp(value['observed_at'], 'accounting pending observation')
    except ValueError as exc:
        raise JournalError('Pending accounting observation requires an aware time') from exc


def _page_path(books, request, digest):
    return _safe_path(books.root / 'accounting-history' /
                      canonical_sha256({'request_id': request.request_id}) / (_hash(digest) + '.json'))


def _page(books, request, reference):
    _fields(reference, {'sha256', 'path', 'count', 'pages'}, 'history reference')
    _quantity(reference['count'], positive=True)
    _quantity(reference['pages'], positive=True)
    path = _page_path(books, request, reference['sha256'])
    _require(reference['path'] == str(path) and reference['count'] == reference['pages'] * PAGE_SIZE,
             'Accounting history reference differs')
    _require(path.stat().st_size <= _MAX_BYTES, 'Private accounting history page exceeds bounded size')
    raw = path.read_bytes()
    envelope = _json_bytes(raw)
    _fields(envelope, {'page', 'sha256'}, 'history page envelope')
    value = envelope['page']
    _require(envelope['sha256'] == reference['sha256'] == canonical_sha256(value),
             'Immutable accounting history page changed')
    _fields(value, {'version', 'account_fingerprint', 'request_sha256', 'previous', 'observations'}, 'history page')
    _require(value['version'] == PAGE_VERSION and value['account_fingerprint'] == books.account_fingerprint
             and value['request_sha256'] == request.sha256 and type(value['observations']) is list
             and len(value['observations']) == PAGE_SIZE, 'Accounting history page identity differs')
    prior = value['previous']
    if reference['pages'] == 1:
        _require(prior is None, 'First accounting history page has an ancestor')
    else:
        _require(type(prior) is dict and prior.get('pages') == reference['pages'] - 1
                 and prior.get('count') == reference['count'] - PAGE_SIZE,
                 'Accounting history page chain count differs')
    return value, hashlib.sha256(raw).hexdigest()


def _history(books, request, reference, bindings, *, check_deadline=None):
    """Validate sealed history; runtime reuses proofs for immutable page bytes.

    Saved-evidence callers always replay every receipt with their deadline.
    Runtime caches only completed sealed pages, never the mutable tail. Every
    cache hit rechecks exact page bytes; changed heads from another writer are
    replayed from their first unknown page. Receipt files are immutable by the
    private-books contract; full prestart/observer reads revalidate their bytes.
    """
    if reference is None:
        return set(), None, None, []
    runtime = check_deadline is None and hasattr(books, 'inspect')
    cache = getattr(books, '_accounting_history_cache', {}) if runtime else {}
    context = (request.sha256, tuple(sorted(bindings)))
    pending = []
    cursor = reference
    found = None
    while cursor is not None:
        if check_deadline is not None:
            check_deadline()
        key = (*context, cursor.get('sha256') if type(cursor) is dict else None)
        if key in cache:
            candidate = cache[key]
            if candidate['reference'] == cursor:
                for path, digest in candidate['pages']:
                    _safe_path(path)
                    _require(path.stat().st_size <= _MAX_BYTES
                             and hashlib.sha256(path.read_bytes()).hexdigest() == digest,
                             'Cached immutable accounting history page changed')
                found = candidate
                break
        page, raw_digest = _page(books, request, cursor)
        pending.append((cursor, page, raw_digest))
        cursor = page['previous']
    seen = set(found['seen']) if found else set()
    previous = deepcopy(found['projection']) if found else None
    observed_at = found['observed_at'] if found else None
    pages = list(found['pages']) if found else []
    for ref, page, raw_digest in reversed(pending):
        for entry in page['observations']:
            if check_deadline is not None:
                check_deadline()
            projection = _snapshot(books, request, entry, bindings)
            digest = entry['authority_receipt']['sha256']
            _require(digest not in seen, 'Duplicate canonical snapshot observation receipt')
            seen.add(digest)
            if previous is not None:
                _require(_time(entry['observed_at']) >= _time(observed_at),
                         'Canonical snapshot history observation time moved backwards')
                _progress(previous, projection)
            previous, observed_at = projection, entry['observed_at']
        pages.append((_page_path(books, request, ref['sha256']), raw_digest))
        if runtime:
            # One checkpoint per original request, rather than retaining a
            # growing copy of every prefix's receipt set.
            cache = {k: v for k, v in cache.items() if k[:2] != context}
            cache[(*context, ref['sha256'])] = dict(reference=deepcopy(ref), seen=frozenset(seen),
                projection=deepcopy(previous), observed_at=observed_at, pages=tuple(pages))
    if runtime:
        books._accounting_history_cache = cache
    return seen, previous, observed_at, pages


def _read(books, request, *, check_deadline=None):
    if check_deadline is not None:
        check_deadline()
    identity = _identity(books, request)
    path = _path(books, request)
    empty = dict(**identity, bindings={}, history=None, snapshot_count=0,
                 snapshots=[], latest_snapshot=None, last_attempt=None, pending=True)
    if not path.exists():
        return empty
    try:
        envelope = _json(path)
        _require(type(envelope) is dict and set(envelope) == {'accounting', 'sha256'}
                 and canonical_sha256(envelope['accounting']) == envelope['sha256'],
                 'Private accounting index digest differs')
        value = envelope['accounting']
        legacy = type(value) is dict and value.get('version') == LEGACY_BOOK_VERSION
        fields = set(empty) - {'history', 'snapshot_count'} if legacy else set(empty)
        _require(type(value) is dict and set(value) == fields
                 and value['version'] in {LEGACY_BOOK_VERSION, BOOK_VERSION}
                 and all(value[name] == data for name, data in identity.items() if name != 'version')
                 and type(value['bindings']) is dict and type(value['snapshots']) is list and type(value['pending']) is bool,
                 'Private accounting index identity differs')
        bindings = {digest: _stored_binding(payload, digest, books) for digest, payload in value['bindings'].items()}
        seen_receipts, previous, observed_at, _ = _history(books, request, value.get('history'), bindings,
                                                         check_deadline=check_deadline)
        if not legacy:
            _quantity(value['snapshot_count'])
            _require(len(value['snapshots']) <= PAGE_SIZE
                     and value['snapshot_count'] == len(value['snapshots']) + len(seen_receipts),
                     'Bounded accounting head count differs from history')
        for snapshot in value['snapshots']:
            if check_deadline is not None:
                check_deadline()
            projection = _snapshot(books, request, snapshot, bindings)
            receipt_sha256 = snapshot['authority_receipt']['sha256']
            _require(receipt_sha256 not in seen_receipts, 'Duplicate canonical snapshot observation receipt')
            seen_receipts.add(receipt_sha256)
            if observed_at is not None:
                _require(_time(snapshot['observed_at']) >= _time(observed_at),
                         'Canonical snapshot history observation time moved backwards')
            if previous is not None:
                _progress(previous, projection)
            previous, observed_at = projection, snapshot['observed_at']
        if value['latest_snapshot'] is None:
            _require(not seen_receipts, 'Canonical snapshot history lost its latest reference')
        else:
            latest = _snapshot(books, request, value['latest_snapshot'], bindings)
            _require(value['snapshots'] and (value['latest_snapshot'] == value['snapshots'][-1]
                     or legacy and value['latest_snapshot']['snapshot_sha256'] == value['snapshots'][-1]['snapshot_sha256']),
                     'Latest canonical snapshot does not match history')
            _require(_time(value['latest_snapshot']['observed_at']) >= _time(observed_at),
                     'Latest canonical snapshot predates its retained history')
            _progress(previous, latest)
        if value['last_attempt'] is not None:
            pending, observed_at, projection = _attempt(books, request, value['last_attempt'], bindings)
            _require(pending == value['pending'], 'Accounting pending flag differs from last attempt evidence')
            if value['latest_snapshot'] is not None:
                _require(_time(observed_at) >= _time(value['latest_snapshot']['observed_at']),
                         'Accounting attempt predates its retained latest snapshot')
            if not pending:
                _require(value['latest_snapshot'] is not None
                         and value['latest_snapshot']['authority_receipt'] == value['last_attempt'],
                         'Successful accounting attempt lost its latest canonical snapshot')
        else:
            _require(not seen_receipts and value['pending'], 'Canonical accounting state has no durable attempt')
        if check_deadline is not None:
            check_deadline()
        return value
    except TimeoutError:
        raise
    except (OSError, TypeError, ValueError, KeyError) as exc:
        raise JournalError('Private accounting index failed validation') from exc


def _seal_page(books, request, previous, observations):
    # Scout's supported operating hosts are Windows. There os.rename is an
    # atomic no-overwrite move. A link/unlink publication would expose nlink=2
    # after a crash, violating the private store's standalone-file guard.
    _require(os.name == 'nt', 'Immutable Scout accounting page publication requires Windows atomic rename')
    page = dict(version=PAGE_VERSION, account_fingerprint=books.account_fingerprint,
                request_sha256=request.sha256, previous=previous, observations=observations)
    digest = canonical_sha256(page)
    path = _page_path(books, request, digest)
    raw = json.dumps(dict(page=page, sha256=digest), sort_keys=True, separators=(',', ':'),
                     allow_nan=False).encode('utf-8')
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _require(path.read_bytes() == raw, 'Existing immutable accounting page differs')
    else:
        temporary = path.with_name('.page-' + uuid.uuid4().hex + '.tmp')
        with temporary.open('xb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            try:
                os.rename(temporary, path)  # Windows atomic create, never overwrite.
            except FileExistsError:
                _require(path.read_bytes() == raw, 'Concurrent immutable accounting page differs')
        finally:
            temporary.unlink(missing_ok=True)
    return dict(sha256=digest, path=str(path), count=(previous['count'] if previous else 0) + PAGE_SIZE,
                pages=(previous['pages'] if previous else 0) + 1)


def _complete_legacy(books, request, value, *, newly_validated):
    _require(not value['snapshots'] or value['latest_snapshot'] == value['snapshots'][-1],
             'Ambiguous legacy accounting history requires explicit review before migration')
    represented = newly_validated | {entry['authority_receipt']['sha256'] for entry in value['snapshots']}
    directory = _safe_path(books.root / 'receipts' / '.path-check').parent
    if directory.exists():
        for path in directory.glob('*.json'):
            envelope = _json(path)
            if envelope.get('kind') == 'authority:accounting' and envelope.get('request_id') == request.request_id:
                _require(path.stem in represented,
                         'Ambiguous unindexed legacy accounting receipt requires explicit review before migration')


def _write(books, request, value):
    value = deepcopy(value)
    if value['version'] == LEGACY_BOOK_VERSION:
        original = _json(_path(books, request))['accounting']
        _require(original['version'] == LEGACY_BOOK_VERSION, 'Legacy accounting changed before migration')
        _complete_legacy(books, request, original,
                         newly_validated={entry['authority_receipt']['sha256'] for entry in value['snapshots']})
        value.update(version=BOOK_VERSION, history=None, snapshot_count=len(value['snapshots']))
    while len(value['snapshots']) > PAGE_SIZE:
        value['history'] = _seal_page(books, request, value['history'], value['snapshots'][:PAGE_SIZE])
        value['snapshots'] = value['snapshots'][PAGE_SIZE:]
    value['snapshot_count'] = (value['history']['count'] if value['history'] else 0) + len(value['snapshots'])
    path = _path(books, request)
    path.parent.mkdir(exist_ok=True)
    envelope = dict(accounting=value, sha256=canonical_sha256(value))
    raw = json.dumps(envelope, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    # Staging bytes are never an index. A process death leaves them private but
    # outside the authoritative head directory, so exact retry can use the
    # intact old head and already-published immutable page without repair.
    temporary = _safe_path(books.root / ('.accounting-' + uuid.uuid4().hex + '.tmp'))
    with temporary.open('xb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def state(books, request):
    """Read only; does not construct stores, create directories or connect."""
    books.inspect()
    return deepcopy(_read(books, request))


def _read_only_books(root, account_fingerprint):
    _require(type(account_fingerprint) is str and _HASH.fullmatch(account_fingerprint),
             'Accounting audit requires the exact private account')
    class ReadOnlyBooks:
        pass
    books = ReadOnlyBooks()
    books.root = Path(root)
    books.account_fingerprint = account_fingerprint
    return books


def read_saved_accounting(root, *, account_fingerprint, request, check_deadline):
    """Read one original request's verified index without stores or side effects.

    ``request`` is its already-audited original journal payload. This bounded
    per-request read makes no claim about other requests or inventory orphans.
    An absent index returns the same empty pending state as the runtime reader.
    """
    _require(callable(check_deadline), 'Accounting audit requires a bounded caller deadline')
    check_deadline()
    books = _read_only_books(root, account_fingerprint)
    original = Request.from_payload(request)
    result = _read(books, original, check_deadline=check_deadline)
    check_deadline()
    return deepcopy(result)


def audit_saved_accounting(root, *, account_fingerprint, requests, check_deadline):
    """Audit saved private bytes only, under the caller's overall deadline.

    ``root`` is ScoutBooks.root. ``requests`` contains exact original payloads
    from the caller's already-audited journals. No store or client is created.
    """
    _require(callable(check_deadline), 'Accounting audit requires a bounded caller deadline')
    books = _read_only_books(root, account_fingerprint)
    originals = {}
    expected = {}
    for payload in requests:
        check_deadline()
        request = Request.from_payload(payload)
        _identity(books, request)
        _require(request.request_id not in originals, 'Duplicate original request in accounting audit')
        originals[request.request_id] = request
        expected[_path(books, request).name] = request
    directory = _safe_path(books.root / 'accounting' / '.path-check').parent
    result = dict(requests=len(originals), indexed_requests=0, pending_requests=0, snapshots=0)
    referenced_pages = set()
    if directory.exists():
        _require(directory.is_dir(), 'Private accounting path is not a directory')
        for path in directory.iterdir():
            check_deadline()
            _safe_path(path)
            # Interrupted temporary writes remain visible for explicit repair;
            # they are never confused with an empty or committed index.
            _require(path.is_file() and path.name in expected, 'Unregistered private accounting index')
            value = _read(books, expected[path.name], check_deadline=check_deadline)
            result['indexed_requests'] += 1
            result['pending_requests'] += int(value['pending'])
            result['snapshots'] += value.get('snapshot_count', len(value['snapshots']))
            cursor = value.get('history')
            while cursor is not None:
                check_deadline()
                page, _ = _page(books, expected[path.name], cursor)
                referenced_pages.add(Path(cursor['path']))
                cursor = page['previous']
    history_root = _safe_path(books.root / 'accounting-history' / '.path-check').parent
    by_directory = {canonical_sha256({'request_id': request.request_id}): request for request in originals.values()}
    if history_root.exists():
        for directory in history_root.iterdir():
            check_deadline()
            _safe_path(directory / '.path-check')
            _require(directory.is_dir() and directory.name in by_directory,
                     'Accounting history has no original journal request')
            request = by_directory[directory.name]
            for path in directory.iterdir():
                check_deadline()
                _safe_path(path)
                _require(path.is_file(), 'Unexpected accounting history entry')
                if re.fullmatch(r'\.page-[a-f0-9]{32}\.tmp', path.name):
                    continue  # Interrupted staging bytes are retained, never promoted.
                if path in referenced_pages:
                    continue
                # A crash after immutable page publication but before the head
                # leaves a valid unreferenced page. Audit its retained identity
                # and evidence without counting it as accepted observations.
                envelope = _json(path)
                _fields(envelope, {'page', 'sha256'}, 'retained history page envelope')
                _require(type(envelope['page']) is dict, 'Retained accounting page must be an object')
                previous = envelope['page'].get('previous')
                _require(previous is None or type(previous) is dict, 'Retained accounting page ancestor differs')
                pages = 1 if previous is None else previous.get('pages', 0) + 1
                reference = dict(sha256=envelope['sha256'], path=str(path), pages=pages, count=pages * PAGE_SIZE)
                page, _ = _page(books, request, reference)
                for entry in page['observations']:
                    check_deadline()
                    _fields(entry, {'snapshot_sha256', 'authority_receipt', 'binding_sha256', 'observed_at'}, 'retained snapshot')
                    payload = _receipt(books, entry['authority_receipt'], request, kind='authority:accounting')
                    _retained_unindexed_view(payload, request)
                    _require(entry['binding_sha256'] == payload['binding_sha256']
                             and entry['snapshot_sha256'] == payload['response']['snapshot_sha256']
                             and entry['observed_at'] == payload['response']['observed_at'],
                             'Retained history entry differs from its immutable receipt')
    receipts = _safe_path(books.root / 'receipts' / '.path-check').parent
    if receipts.exists():
        for path in receipts.iterdir():
            check_deadline()
            _safe_path(path)
            _require(path.is_file(), 'Unexpected private receipt entry')
            if path.suffix != '.json':
                continue  # Other receipt writers retain their own incomplete-write evidence.
            try:
                envelope = _json(path)
            except (OSError, ValueError, TypeError) as exc:
                raise JournalError('Private receipt cannot be audited for accounting identity') from exc
            if type(envelope) is dict and envelope.get('kind') in {'accounting:pending', 'authority:accounting'}:
                request = originals.get(envelope.get('request_id'))
                _require(request is not None, 'Accounting receipt has no original journal request')
                payload = _receipt(books, dict(sha256=path.stem, path=str(path)), request,
                                   kind=envelope['kind'])
                if envelope['kind'] == 'accounting:pending':
                    _pending_payload(payload, request)
                else:
                    _retained_unindexed_view(payload, request)
    check_deadline()
    return result


def record_pending(books, request, *, reason_code, observed_at):
    """Retain a bounded caller's failed attempt without changing order state."""
    _identity(books, request)
    value = dict(version=BOOK_VERSION, request_sha256=request.sha256, session_id=request.session_id,
                 reason_code=reason_code, observed_at=observed_at)
    _pending_payload(value, request)
    with books.locked():
        books.inspect()
        current = _read(books, request)
        if current['last_attempt'] is not None:
            bindings = {digest: _stored_binding(payload, digest, books) for digest, payload in current['bindings'].items()}
            _, old_time, _ = _attempt(books, request, current['last_attempt'], bindings)
            _require(_time(value['observed_at']) >= _time(old_time),
                     'Accounting attempt time cannot move backwards')
        saved = books._save_receipt_locked(kind='accounting:pending', request_id=request.request_id, payload=value)
        current['last_attempt'] = saved
        current['pending'] = True
        _write(books, request, current)
    return deepcopy(saved)


def record_accounting(books, request, receipt, *, binding, now):
    """Persist every distinct accepted observation, even with identical contents."""
    projection = validate_accounting_receipt(request, receipt, binding, now=now)
    _identity(books, request)
    # Inspect the exact original local request/grant. An authenticated view of
    # a different reservation must not become this journal's private books.
    with books.open_session(request.session_id) as journal:
        record = journal.get(request.request_id)
        _require(record['request'] == request.payload() and record['grant'] is not None,
                 'Canonical accounting requires the original admitted journal request')
        grant = record['grant']
        account = projection['account_reservation']
        _require(account['reservation_id'] == grant['reservation_id'] and account['quantity'] == grant['quantity']
                 and (record['broker_order_id'] is None or account['broker_order_id'] == record['broker_order_id']),
                 'Canonical accounting differs from exact original local grant/order')
        if record['state'] in {'GRANTED', 'NOT_SENT'}:
            _require(account['broker_order_id'] is None and account['filled_quantity'] == 0
                     and account['status'] in {'RESERVED', 'REJECTED', 'NOT_SUBMITTED'},
                     'Canonical order conflicts with the durable local unsubmitted fence')
        local = journal.order_state(request.request_id)['evidence']
        if local is not None:
            _require(account['filled_quantity'] >= local['cumulative_filled_quantity'],
                     'Canonical accounting lags saved local cumulative fills')
            by_id = {fill['fill_id']: fill for fill in projection['fills']}
            _require(all(fill['fill_id'] in by_id and by_id[fill['fill_id']]['quantity'] == fill['quantity']
                         and Decimal(by_id[fill['fill_id']]['price']) == Decimal(str(fill['price']))
                         and _time(by_id[fill['fill_id']]['executed_at']) == _time(fill['executed_at'])
                         for fill in local['fills']), 'Canonical accounting omits or mutates saved local fills')
            if local['status'] in _TERMINAL:
                _require(account['status'] == local['status'] and account['filled_quantity'] == local['cumulative_filled_quantity'],
                         'Canonical accounting differs from terminal local order evidence')
    response = receipt.response
    entry = dict(snapshot_sha256=response['snapshot_sha256'], authority_receipt=receipt.saved,
                 binding_sha256=binding.sha256, observed_at=response['observed_at'])
    with books.locked():
        books.inspect()
        current = _read(books, request)
        original_binding = current['bindings'].get(binding.sha256)
        _require(original_binding is None or original_binding == binding.payload(), 'Immutable accounting binding changed')
        current['bindings'][binding.sha256] = binding.payload()
        bindings = {digest: _stored_binding(payload, digest, books) for digest, payload in current['bindings'].items()}
        # This reads the exact immutable file; an in-memory object or path label
        # alone is not a successful durable receipt write.
        _snapshot(books, request, entry, bindings)
        if current['latest_snapshot'] == entry:
            # An exact successful-receipt retry is a no-op. In particular it
            # cannot erase a later pending attempt, even if their clocks tie.
            return deepcopy(entry)
        if current['last_attempt'] is not None:
            _, observed_at, _ = _attempt(books, request, current['last_attempt'], bindings)
            _require(_time(entry['observed_at']) >= _time(observed_at), 'Canonical accounting view predates latest attempt')
        if current['latest_snapshot'] is not None:
            previous = _snapshot(books, request, current['latest_snapshot'], bindings)
            _progress(previous, projection)
        # Content may repeat when nothing changed or an allocation reopened.
        # Every new nonce-bearing receipt is an observation. Only an exact
        # retry of the current receipt is idempotent; no earlier receipt may
        # become a new observation even when service timestamps tie.
        if not current['snapshots'] or current['snapshots'][-1]['authority_receipt'] != entry['authority_receipt']:
            archived, _, _, _ = _history(books, request, current.get('history'), bindings)
            _require(entry['authority_receipt']['sha256'] not in
                     archived | {snapshot['authority_receipt']['sha256'] for snapshot in current['snapshots']},
                     'An old canonical receipt cannot become a new observation')
            current['snapshots'].append(entry)
        current['latest_snapshot'] = entry
        current['last_attempt'] = receipt.saved
        current['pending'] = False
        _write(books, request, current)
    return deepcopy(entry)
