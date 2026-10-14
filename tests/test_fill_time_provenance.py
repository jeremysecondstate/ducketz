from copy import deepcopy
from dataclasses import asdict, replace
import json
import sqlite3

import pytest

from ml.stock_trader.horizon_ledger import FillEvidence, HorizonLedger, LedgerError, validate_fill_time_provenance
from test_stock_horizon_ledger import ACCOUNT, entry, order, portfolio


FILL = FillEvidence('fill-1', 10, '100', '2026-09-08T11:00:20+00:00')


def provenance(stamp='2026-09-08T11:00:20Z'):
    return {'version': 'schwab-execution-time-v1', 'fills': {
        FILL.fill_id: {'raw_timestamp': stamp, 'source_field': 'execution_leg.time'}}}


@pytest.mark.parametrize('stamp,whole', [
    ('2026-09-08T11:00:20Z', True),
    ('2026-09-08T04:00:20-0700', True),
    ('2026-09-08T11:00:20+00:00', True),
    ('2026-09-08T11:00:20.000Z', False),
    ('2026-09-08T11:00:20,000000000+0000', False),
])
def test_wire_precision_does_not_infer_whole_seconds_from_normalized_zero(stamp, whole):
    raw = provenance(stamp)
    normalized, ids = validate_fill_time_provenance(raw, (FILL,))
    assert normalized == raw
    assert ids == (frozenset({FILL.fill_id}) if whole else frozenset())
    assert asdict(FILL) == dict(fill_id='fill-1', quantity=10, price='100', executed_at='2026-09-08T11:00:20+00:00')
    normalized['fills']['fill-1']['raw_timestamp'] = 'changed-copy'
    assert raw['fills']['fill-1']['raw_timestamp'] == stamp


def test_missing_provenance_keeps_legacy_interpretation():
    assert validate_fill_time_provenance(None, (FILL,)) == (None, frozenset())


@pytest.mark.parametrize('invalid', [
    [], {}, {'version': 'unknown', 'fills': {}},
    {'version': 'schwab-execution-time-v1', 'fills': []},
    {'version': 'schwab-execution-time-v1', 'fills': {}},
    {'version': 'schwab-execution-time-v1', 'fills': {'other': {}}},
    {**provenance(), 'extra': True},
    {'version': 'schwab-execution-time-v1', 'fills': {'fill-1': {}}},
    {'version': 'schwab-execution-time-v1', 'fills': {'fill-1': {
        'raw_timestamp': '2026-09-08T11:00:20Z', 'source_field': 'order.closeTime'}}},
])
def test_provenance_rejects_incomplete_or_unknown_schema(invalid):
    with pytest.raises(LedgerError, match='FILL_TIME_PROVENANCE_INVALID'):
        validate_fill_time_provenance(invalid, (FILL,))


@pytest.mark.parametrize('stamp', [None, 123, '2026-09-08T11:00:20', '2026-09-08',
                                   '2026-09-08T11:00:20.1234567890Z'])
def test_provenance_requires_original_timezone_qualified_wire_string(stamp):
    with pytest.raises(LedgerError, match='FILL_TIME_PROVENANCE_INVALID'):
        validate_fill_time_provenance(provenance(stamp), (FILL,))


def test_provenance_rejects_mismatched_time_and_duplicate_fill_ids():
    with pytest.raises(LedgerError, match='DOES_NOT_MATCH_FILL'):
        validate_fill_time_provenance(provenance('2026-09-08T11:00:21Z'), (FILL,))
    with pytest.raises(LedgerError, match='FILL_TIME_PROVENANCE_INVALID'):
        validate_fill_time_provenance(provenance(), (FILL, FILL))


def test_legacy_evidence_replays_without_payload_rewrite_and_new_metadata_persists(tmp_path):
    ledger = HorizonLedger(tmp_path / 'ledger.sqlite3', ACCOUNT)
    ledger.reconcile(portfolio())
    reservation = entry(ledger)
    legacy = order(reservation, fills=(FILL,))
    legacy_payload = asdict(legacy)
    legacy_payload.pop('broker_status_description')
    legacy_payload.pop('fill_time_provenance')
    ledger.reconcile(portfolio('p1', '2026-09-08T11:00:21Z', 30), order_evidence=(legacy,))
    with sqlite3.connect(ledger.path) as db:
        saved = db.execute('SELECT payload FROM evidence WHERE id=?', (legacy.evidence_id,)).fetchone()[0]
    assert json.loads(saved) == json.loads(json.dumps(legacy_payload))
    ledger.reconcile(portfolio('p2', '2026-09-08T11:00:22Z', 30), order_evidence=(legacy,))
    detailed = replace(legacy, evidence_id='detailed', observed_at='2026-09-08T11:00:23Z',
                       fill_time_provenance=provenance(), broker_status_description='Synthetic detail')
    ledger.reconcile(portfolio('p3', '2026-09-08T11:00:24Z', 30), order_evidence=(detailed,))
    with sqlite3.connect(ledger.path) as db:
        assert db.execute('SELECT payload FROM evidence WHERE id=?', (legacy.evidence_id,)).fetchone()[0] == saved
        payload = json.loads(db.execute('SELECT payload FROM evidence WHERE id=?', ('detailed',)).fetchone()[0])
        assert payload['fill_time_provenance'] == provenance()
        assert payload['broker_status_description'] == 'Synthetic detail'
        assert db.execute('SELECT COUNT(*) FROM fills').fetchone()[0] == 1


def test_invalid_provenance_rolls_back_evidence_and_inventory(tmp_path):
    ledger = HorizonLedger(tmp_path / 'ledger.sqlite3', ACCOUNT)
    ledger.reconcile(portfolio())
    reservation = entry(ledger)
    evidence = replace(order(reservation, fills=(FILL,)), fill_time_provenance=provenance('2026-09-08T11:00:21Z'))
    before = deepcopy(ledger.snapshot())
    with pytest.raises(LedgerError, match='DOES_NOT_MATCH_FILL'):
        ledger.reconcile(portfolio('bad', '2026-09-08T11:00:22Z', 30), order_evidence=(evidence,))
    assert ledger.snapshot() == before
    with sqlite3.connect(ledger.path) as db:
        assert db.execute('SELECT COUNT(*) FROM fills').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM evidence WHERE id=?', (evidence.evidence_id,)).fetchone()[0] == 0
