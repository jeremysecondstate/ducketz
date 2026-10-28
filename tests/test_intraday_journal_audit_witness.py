"""Complete exact-row witnesses avoid replay work, never journal validation."""
from dataclasses import replace
import json
import sqlite3

import pytest

from ml.intraday import scout_journal as module
from ml.intraday.dataset_diagnostics import capture_dataset_build
from ml.intraday.scout_journal import ScoutJournal, JournalCorruptionError, JournalStateError
from test_intraday_scout_journal import BINDING, request, grant, order_evidence


@pytest.fixture(autouse=True)
def empty_audit_witnesses():
    with module._audit_witness_lock:
        module._audit_witnesses.clear()
        module._audit_witness_bytes = 0
    yield
    with module._audit_witness_lock:
        module._audit_witnesses.clear()
        module._audit_witness_bytes = 0


def case(tmp_path):
    journal = ScoutJournal(tmp_path / 'journal.sqlite3', binding=BINDING)
    journal.prepare(request())
    journal.grant(grant())
    token = journal.claim_send('request-1')
    journal.outcome('request-1', token, '123')
    journal.observe_order('request-1', order_evidence(status='FILLED', filled=2),
                          as_of='2026-10-12T18:10:00Z')
    journal.get('request-1')
    return journal, token


def measured(call):
    with capture_dataset_build() as profile:
        result = call()
    groups = {row['group']: row for row in profile['recovery_subphases']['groups']}
    return result, groups


def test_unchanged_complete_image_reuses_audit_across_reopen_without_writes(tmp_path):
    journal, _ = case(tmp_path)
    before = journal.path.read_bytes()
    result, phases = measured(lambda: journal.get_with_order_state('request-1'))
    assert result[0]['state'] == 'ACCEPTED' and result[1]['evidence']['status'] == 'FILLED'
    assert phases['journal_audit_image']['count'] == 1
    assert phases['journal_audit_reuse']['count'] == 1
    assert 'journal_full_audit' not in phases
    reopened, phases = measured(lambda: ScoutJournal(journal.path, binding=BINDING,
        create=False, expected_journal_id=journal.journal_id))
    assert phases['journal_audit_reuse']['count'] == 1
    assert reopened.get_with_order_state('request-1') == result
    assert journal.path.read_bytes() == before


def test_returned_nested_values_cannot_poison_an_audit_witness(tmp_path):
    journal, _ = case(tmp_path)
    original = journal.get_with_order_state('request-1')
    changed = journal.get_with_order_state('request-1')
    changed[0]['request']['quantity'] = 999
    changed[1]['evidence']['fills'][0]['quantity'] = 999
    changed[1]['cancellation_events'].append({'kind': 'invented'})
    outbox = journal.pending_events()
    outbox[0]['event']['record']['request']['symbol'] = 'OTHER'
    assert journal.get_with_order_state('request-1') == original
    assert journal.pending_events()[0]['event']['record']['request']['symbol'] == 'DOCU'


@pytest.mark.parametrize('sql', [
    "UPDATE control SET sha256='corrupt'",
    "UPDATE events SET payload='{}' WHERE sequence=1",
    "DELETE FROM events WHERE sequence=1",
    "UPDATE records SET sha256='corrupt'",
    "DELETE FROM records",
    "INSERT INTO acknowledgements VALUES ('missing-event','corrupt')",
    "UPDATE lifecycle_control SET sha256='corrupt'",
    "UPDATE lifecycle_events SET payload='{}' WHERE sequence=1",
    "DELETE FROM lifecycle_events WHERE sequence=1",
    "UPDATE lifecycle_records SET sha256='corrupt'",
    "UPDATE lifecycle_evidence_ids SET evidence_sha256='corrupt'",
    "CREATE TABLE unexpected (payload TEXT)",
])
def test_changed_historical_rows_and_corrupt_lifecycle_never_hit(tmp_path, sql):
    journal, token = case(tmp_path)
    _, phases = measured(lambda: journal.get('request-1'))
    assert phases['journal_audit_reuse']['count'] == 1
    with sqlite3.connect(journal.path) as db:
        db.execute(sql)
    with pytest.raises(JournalCorruptionError):
        journal.get('request-1')
    # Corruption neither erases a claim nor permits a second broker submission.
    with pytest.raises(JournalCorruptionError):
        journal.claim_send('request-1')
    assert token


def test_valid_lifecycle_change_and_acknowledgement_each_require_new_full_audit(tmp_path):
    journal, _ = case(tmp_path)
    event = journal.pending_events()[0]
    journal.acknowledge(event['event_id'], event['event_sha256'])
    pending, phases = measured(journal.pending_events)
    assert len(pending) == 3 and phases['journal_full_audit']['count'] == 1
    assert 'journal_audit_reuse' not in phases
    _, phases = measured(journal.pending_events)
    assert phases['journal_audit_reuse']['count'] == 1
    later = replace(order_evidence(status='FILLED', filled=2), evidence_id='later-observation',
                    observed_at='2026-10-12T18:11:00Z')
    journal.observe_order('request-1', later, as_of='2026-10-12T18:11:00Z')
    state, phases = measured(lambda: journal.order_state('request-1'))
    assert state['evidence']['evidence_id'] == 'later-observation'
    assert phases['journal_full_audit']['count'] == 1
    _, phases = measured(lambda: journal.order_state('request-1'))
    assert phases['journal_audit_reuse']['count'] == 1


def test_unknown_send_stays_fenced_across_cache_hits_and_process_style_reopen(tmp_path):
    journal = ScoutJournal(tmp_path / 'unknown.sqlite3', binding=BINDING)
    journal.prepare(request())
    journal.grant(grant())
    token = journal.claim_send('request-1')
    journal.outcome('request-1', token, None)
    journal.get('request-1')
    for current in (journal, ScoutJournal(journal.path, binding=BINDING, create=False)):
        for _ in range(3):
            assert current.get('request-1')['state'] == 'UNKNOWN'
            with pytest.raises(JournalStateError, match='once'):
                current.claim_send('request-1')
            with pytest.raises(JournalStateError):
                current.revoke_unsent('request-1', 'must-not-rearm')
            assert current.get('request-1')['send_token'] == token


def test_replaced_validator_keeps_full_audit_and_its_business_calls(tmp_path, monkeypatch):
    journal, _ = case(tmp_path)
    calls = []
    native = ScoutJournal._validate_record
    def custom(self, record, **kwargs):
        calls.append(record['state'])
        return native(self, record, **kwargs)
    monkeypatch.setattr(ScoutJournal, '_validate_record', custom)
    _, phases = measured(lambda: journal.get('request-1'))
    assert calls and phases['journal_full_audit']['count'] == 1
    assert 'journal_audit_reuse' not in phases


def test_changed_global_or_order_validator_invalidates_existing_witness(tmp_path, monkeypatch):
    journal, _ = case(tmp_path)
    from ml.stock_trader import horizon_ledger
    calls = []
    native = horizon_ledger.validate_fill_time_provenance
    def custom(*args, **kwargs):
        calls.append(True)
        return native(*args, **kwargs)
    monkeypatch.setattr(horizon_ledger, 'validate_fill_time_provenance', custom)
    _, phases = measured(lambda: journal.get('request-1'))
    assert calls and phases['journal_full_audit']['count'] == 1
    assert 'journal_audit_reuse' not in phases


def test_incidental_lazy_namespace_metadata_does_not_disable_native_validation(tmp_path, monkeypatch):
    journal, _ = case(tmp_path)
    from ml.stock_trader import horizon_ledger
    # Introspection, warnings and deepcopy may lazily add these unrelated
    # metadata fields under a fresh test/runtime import layout. They are not
    # order validators or class behavior hooks and cannot interpret any row.
    monkeypatch.setattr(horizon_ledger, '__warningregistry__', {'fixture-only': True}, raising=False)
    monkeypatch.setattr(horizon_ledger.OrderEvidence, '__slotnames__', [], raising=False)
    monkeypatch.setattr(ScoutJournal, '__annotations__', {'fixture_only': 'metadata'}, raising=False)
    result, phases = measured(lambda: journal.get_with_order_state('request-1'))
    assert result[0]['state'] == 'ACCEPTED' and result[1]['evidence']['status'] == 'FILLED'
    assert phases['journal_audit_reuse']['count'] == 1
    assert 'journal_full_audit' not in phases


def test_bounded_witness_eviction_never_changes_audited_results(tmp_path, monkeypatch):
    monkeypatch.setattr(module, '_AUDIT_WITNESS_ENTRIES', 2)
    journals = [ScoutJournal(tmp_path / ('bounded-%s.sqlite3' % index), binding=BINDING)
                for index in range(4)]
    assert len(module._audit_witnesses) == 2
    assert 0 < module._audit_witness_bytes <= module._AUDIT_WITNESS_BYTES
    _, phases = measured(journals[0].records)
    assert phases['journal_full_audit']['count'] == 1
    assert journals[0].records() == () and len(module._audit_witnesses) == 2
    monkeypatch.setattr(module, '_AUDIT_WITNESS_BYTES', 1)
    _, phases = measured(journals[0].records)
    assert phases['journal_full_audit']['count'] == 1


def test_binding_and_expected_journal_identity_are_not_cache_authority(tmp_path):
    journal, _ = case(tmp_path)
    with pytest.raises(JournalCorruptionError):
        ScoutJournal(journal.path, binding={**BINDING, 'session_id': 'different'}, create=False)
    with pytest.raises(JournalCorruptionError):
        ScoutJournal(journal.path, binding=BINDING, create=False, expected_journal_id='f' * 32)


def test_rehashed_earlier_event_cannot_hide_behind_unchanged_heads(tmp_path):
    journal, _ = case(tmp_path)
    with sqlite3.connect(journal.path) as db:
        raw = db.execute('SELECT payload FROM events WHERE sequence=1').fetchone()[0]
        value = json.loads(raw)
        value['record']['request']['quantity'] = 999
        changed = module._encoded(value)
        db.execute('UPDATE events SET payload=?,sha256=? WHERE sequence=1',
                   (changed, module.canonical_sha256(value)))
    with pytest.raises(JournalCorruptionError):
        journal.get_with_order_state('request-1')
