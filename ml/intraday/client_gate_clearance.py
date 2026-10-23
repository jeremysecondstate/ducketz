"""Exact trusted local client-gate proof; the original send remains UNKNOWN.

A client witness alone cannot qualify an arbitrary callback. Creation also
requires the independent executor's exact retained local closures, their bound
objects and a sender which was never entered. Historical events cannot mint a
missing live witness. The receipt only releases a block on distinct forecasts.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from types import FunctionType

from .prediction import ContractError, instant
from .pretransport_clearance import _audit, _keys, _read, _require, _write_once
from .scout_client import ClientFinalGateFailure, ScoutClient, exception_details
from .scout_journal import _safe_path
from .scout_protocol import Grant, Request, canonical_sha256

VERSION = 'scout-observed-client-final-gate-clearance-v1'
COMMITMENT = 'no_order_transport_entered'
_STAGE = 'client_postclaim_final_gate'
_CLIENT_METHODS = {name: getattr(ScoutClient, name)
                   for name in ('submit', '_gate', '_current', '_bound', '_now')}


def _path(books, record):
    return _safe_path(Path(books.root) / 'client-final-gate-clearances' /
        (canonical_sha256({'request_id': record['request']['request_id']}) + '.json'))


def _validate(payload, record, *, journal_id, events, as_of):
    _keys(payload, {'version', 'created_at', 'journal_id', 'original_record_sha256',
        'journal_events_sha256', 'observation', 'historical_execution', 'commitment'},
        'client-gate receipt fields differ')
    _require(payload['version'] == VERSION and payload['journal_id'] == journal_id
        and payload['original_record_sha256'] == canonical_sha256(record)
        and payload['journal_events_sha256'] == canonical_sha256({'events': events})
        and payload['historical_execution'] == 'UNKNOWN' and payload['commitment'] == COMMITMENT,
        'client-gate receipt differs from immutable attempt')
    _require(instant(record['request']['issued_at']) <= instant(events[-2]['event']['recorded_at'])
        <= instant(events[-1]['event']['recorded_at']) <= instant(payload['created_at']) <= instant(as_of),
        'client-gate receipt clock differs')
    observed = payload['observation']
    _keys(observed, {'stage', 'request_sha256', 'grant_sha256', 'sending_record_sha256', 'failure'},
        'client-gate observation fields differ')
    _require(observed['stage'] == _STAGE
        and observed['request_sha256'] == record['request_sha256']
        and observed['grant_sha256'] == record['grant_sha256']
        and observed['sending_record_sha256'] == canonical_sha256(dict(record, state='SENDING')),
        'client-gate observation differs from exact claimed attempt')
    # Diagnostics are retained evidence, never clearance authority.
    failure = observed['failure']
    _require(type(failure) is dict and set(failure) == {'chain', 'chain_truncated'}
        and type(failure['chain']) is list and bool(failure['chain'])
        and type(failure['chain_truncated']) is bool, 'client-gate diagnostics unavailable')


def load_clearance(books, journal, record, *, as_of):
    path = _path(books, record)
    if not path.exists():
        return None
    try:
        journal_id, events = _audit(books, journal, record)
        envelope = json.loads(_read(path))
        _keys(envelope, {'payload', 'sha256'}, 'client-gate envelope differs')
        payload = envelope['payload']
        _require(canonical_sha256(payload) == envelope['sha256'], 'client-gate receipt hash differs')
        _validate(payload, record, journal_id=journal_id, events=events, as_of=as_of)
        return dict(path=str(path), sha256=envelope['sha256'], created_at=payload['created_at'],
                    historical_execution='UNKNOWN', commitment=COMMITMENT)
    except (OSError, ValueError, TypeError, KeyError, RecursionError, sqlite3.Error) as error:
        if isinstance(error, ContractError):
            raise
        raise ContractError('Client-gate clearance: invalid or unavailable receipt') from error


def _cells(callback):
    return dict(zip(callback.__code__.co_freevars,
                    (cell.cell_contents for cell in callback.__closure__ or ())))


def _trusted_observation(executor, client, record, gate, send, error):
    # Import after executor definition; these code objects were captured once
    # from its installed implementation, not supplied by the failing callback.
    from .scout_local_execution import (ScoutLocalAccountExecutor,
        _LOCAL_AUTHORIZATION_CODE, _LOCAL_SEND_CODE)
    code = getattr(error, 'sqlite_errorcode', None)
    _require(type(error) is sqlite3.OperationalError and type(code) is int and code >= 0
        and code & 255 in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED},
        'only native local contention qualifies for this client-gate proof')
    observed = client.final_gate_failure
    _require(type(executor) is ScoutLocalAccountExecutor and type(client) is ScoutClient
        and type(observed) is ClientFinalGateFailure and observed.client is client
        and observed.error is error and observed.final_gate is gate and observed.send is send
        and client.journal is executor.journal
        and client.account_fingerprint == executor.account_fingerprint
        and client.session_id == executor.session_id == record['request']['session_id']
        and client.symbols == frozenset(executor.symbols) and client.authority_epoch == 1,
        'exact retained local client observation required')
    _require(all(getattr(getattr(client, name), '__func__', None) is method
                 for name, method in _CLIENT_METHODS.items()), 'opaque client method cannot prove no transport')
    _require(type(gate) is FunctionType and type(send) is FunctionType
        and gate.__code__ is _LOCAL_AUTHORIZATION_CODE
        and getattr(send, '__code__', None) is _LOCAL_SEND_CODE,
        'opaque callback cannot prove no transport')
    gates, sends = _cells(gate), _cells(send)
    _require(gates.get('self') is executor and sends.get('self') is executor
        and sends.get('local_authorization') is gate and sends.get('adapter') is None
        and type(gates.get('request')) is Request and type(gates.get('permit')) is Grant
        and gates['request'].payload() == record['request']
        and gates['permit'].payload() == record['grant'], 'trusted local callback binding differs')
    _require(observed.request_sha256 == record['request_sha256']
        and observed.grant_sha256 == record['grant_sha256']
        and observed.sending_record_sha256 == canonical_sha256(dict(record, state='SENDING')),
        'client witness differs from exact claimed attempt')
    return observed


def create_clearance(books, journal, record, *, executor, client, gate, send, error, as_of):
    """Qualify only a synchronous retained trusted local callback failure."""
    _require(executor.books is books and executor.journal is journal,
             'local executor differs from original journal')
    observed = _trusted_observation(executor, client, record, gate, send, error)
    with books.locked():
        books.inspect()
        journal_id, events = _audit(books, journal, record)
        payload = dict(version=VERSION, created_at=instant(as_of).isoformat(), journal_id=journal_id,
            original_record_sha256=canonical_sha256(record),
            journal_events_sha256=canonical_sha256({'events': events}),
            historical_execution='UNKNOWN', commitment=COMMITMENT,
            observation=dict(stage=_STAGE, request_sha256=observed.request_sha256,
                grant_sha256=observed.grant_sha256,
                sending_record_sha256=observed.sending_record_sha256,
                failure=exception_details(error)))
        _validate(payload, record, journal_id=journal_id, events=events, as_of=as_of)
        path = _path(books, record)
        if path.exists():
            return load_clearance(books, journal, record, as_of=as_of)
        path.parent.mkdir(exist_ok=True)
        envelope = dict(payload=payload, sha256=canonical_sha256(payload))
        _write_once(path, json.dumps(envelope, sort_keys=True, separators=(',', ':'),
            ensure_ascii=False, allow_nan=False).encode('utf-8'))
    return load_clearance(books, journal, record, as_of=as_of)
