"""Durable proof of an observed failure before the exact order POST.

The original UNKNOWN and one-send token remain unchanged. Only distinct future
forecasts can pass this commitment, with all their ordinary effect gates. The
creator accepts a synchronous outer-gate or native before-POST observation from
the exact retained adapter, not an exception's name, message, traceback or
caller-supplied exception attribute.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from .prediction import ContractError, instant
from .pretransport_clearance import _audit, _keys, _read, _require, _write_once
from .scout_broker import InitialGateFailure, NativeBeforePostFailure, ScoutBrokerAdapter
from .scout_client import exception_details
from .scout_journal import _safe_path
from .scout_protocol import canonical_sha256

VERSION = 'scout-observed-initial-gate-clearance-v1'
NATIVE_VERSION = 'scout-observed-native-before-post-clearance-v1'
COMMITMENT = 'no_order_transport_entered'


def _path(books, record):
    return _safe_path(Path(books.root) / 'initial-gate-clearances' /
        (canonical_sha256({'request_id': record['request']['request_id']}) + '.json'))


def _validate(payload, record, *, journal_id, events, as_of):
    _keys(payload, {'version', 'created_at', 'journal_id', 'original_record_sha256',
        'journal_events_sha256', 'observation', 'historical_execution', 'commitment'},
        'initial-gate receipt fields differ')
    _require(payload['version'] in {VERSION, NATIVE_VERSION} and payload['journal_id'] == journal_id
        and payload['original_record_sha256'] == canonical_sha256(record)
        and payload['journal_events_sha256'] == canonical_sha256({'events': events})
        and payload['historical_execution'] == 'UNKNOWN' and payload['commitment'] == COMMITMENT,
        'initial-gate receipt differs from immutable attempt')
    _require(instant(record['request']['issued_at']) <= instant(events[-2]['event']['recorded_at'])
        <= instant(events[-1]['event']['recorded_at']) <= instant(payload['created_at']) <= instant(as_of),
        'initial-gate receipt clock differs')
    observed = payload['observation']
    native = payload['version'] == NATIVE_VERSION
    _keys(observed, {'stage', 'request_sha256', 'grant_sha256', 'sending_record_sha256', 'failure'}
        | ({'native_sender'} if native else set()),
        'initial-gate observation fields differ')
    sending = dict(record, state='SENDING')
    _require(observed['stage'] == ('native_schwab_before_post' if native else 'adapter_initial_gate')
        and (not native or observed['native_sender'] == 'SchwabSession.submit_prepared_order')
        and observed['request_sha256'] == record['request_sha256']
        and observed['grant_sha256'] == record['grant_sha256']
        and observed['sending_record_sha256'] == canonical_sha256(sending),
        'initial-gate observation differs from exact claimed attempt')
    # Diagnostic values are retained evidence, never the source of clearance.
    _require(type(observed['failure']) is dict and set(observed['failure']) == {'chain', 'chain_truncated'}
        and type(observed['failure']['chain']) is list and bool(observed['failure']['chain'])
        and type(observed['failure']['chain_truncated']) is bool,
        'initial-gate diagnostics unavailable')


def load_clearance(books, journal, record, *, as_of):
    """Re-audit the complete original chain; a corrupt receipt fails closed."""
    path = _path(books, record)
    if not path.exists():
        return None
    try:
        journal_id, events = _audit(books, journal, record)
        envelope = json.loads(_read(path))
        _keys(envelope, {'payload', 'sha256'}, 'initial-gate envelope differs')
        payload = envelope['payload']
        _require(canonical_sha256(payload) == envelope['sha256'], 'initial-gate receipt hash differs')
        _validate(payload, record, journal_id=journal_id, events=events, as_of=as_of)
        return dict(path=str(path), sha256=envelope['sha256'], created_at=payload['created_at'],
                    historical_execution='UNKNOWN', commitment=COMMITMENT)
    except (OSError, ValueError, TypeError, KeyError, RecursionError, sqlite3.Error) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError('Initial-gate clearance: invalid or unavailable receipt') from exc


def create_clearance(books, journal, record, *, adapter, error, as_of):
    """Persist a proven completed pre-POST gate failure without changing history."""
    observed = adapter.initial_gate_failure or adapter.native_before_post_failure
    native = type(observed) is NativeBeforePostFailure
    _require(type(adapter) is ScoutBrokerAdapter and adapter.journal is journal
        and type(observed) in {InitialGateFailure, NativeBeforePostFailure} and observed.error is error
        and adapter.account_fingerprint == books.account_fingerprint
        and adapter.session_id == record['request']['session_id'],
        'exact retained adapter observation required')
    with books.locked():
        books.inspect()
        journal_id, events = _audit(books, journal, record)
        payload = dict(version=NATIVE_VERSION if native else VERSION, created_at=instant(as_of).isoformat(), journal_id=journal_id,
            original_record_sha256=canonical_sha256(record), journal_events_sha256=canonical_sha256({'events': events}),
            historical_execution='UNKNOWN', commitment=COMMITMENT,
            observation=dict(stage='native_schwab_before_post' if native else 'adapter_initial_gate', request_sha256=observed.request_sha256,
                grant_sha256=observed.grant_sha256, sending_record_sha256=observed.sending_record_sha256,
                failure=exception_details(error),
                **({'native_sender': 'SchwabSession.submit_prepared_order'} if native else {})))
        _validate(payload, record, journal_id=journal_id, events=events, as_of=as_of)
        path = _path(books, record)
        if path.exists():
            return load_clearance(books, journal, record, as_of=as_of)
        path.parent.mkdir(exist_ok=True)
        envelope = dict(payload=payload, sha256=canonical_sha256(payload))
        _write_once(path, json.dumps(envelope, sort_keys=True, separators=(',', ':'),
            ensure_ascii=False, allow_nan=False).encode('utf-8'))
    return load_clearance(books, journal, record, as_of=as_of)
