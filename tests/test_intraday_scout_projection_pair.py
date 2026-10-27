"""Fresh native projection pairs preserve original order and lifecycle evidence."""
from contextlib import contextmanager
from dataclasses import asdict
import os
import sqlite3

import pytest

from ml.intraday import scout_journal as journal_module
from ml.intraday import scout_local_execution as execution
from ml.intraday.scout_journal import JournalCorruptionError, JournalStateError, ScoutJournal
from test_intraday_scout_journal import (
    BINDING, accepted, cancellation, observe, order_evidence, ready,
)
from test_intraday_scout_periodic_paired_recovery import fixture, count_audits


def pair(journal, reservation='reservation-1', request='request-1'):
    return journal.lookup_reservation_with_order_state(reservation, expected_request_id=request)


def project(case):
    record = case.journal.get(case.ids[0])
    return case.executor._project(case.journal, record,
        {'forecast_id': record['request']['forecast_id'], 'symbol': 'DOCU'}, 'order_status_reconciled')


def writer_can_enter(path):
    with sqlite3.connect(path, timeout=0, isolation_level=None) as db:
        db.execute('BEGIN IMMEDIATE')
        db.rollback()


@pytest.mark.parametrize('status,filled', [
    ('WORKING', 0), ('PARTIAL', 1), ('FILLED', 2), ('CANCELLED', 1), ('REJECTED', 0)])
@pytest.mark.parametrize('cancelled', [False, True])
def test_pair_matches_original_readers_and_detaches_nested_values(tmp_path, status, filled, cancelled):
    journal = accepted(tmp_path)
    if cancelled:
        observe(journal, order_evidence(identity='pre-cancel', status='WORKING', filled=0))
        cancellation(journal)
        observe(journal, order_evidence(status=status, filled=filled,
            observed='2026-10-12T18:02:00+00:00'))
    else:
        observe(journal, order_evidence(status=status, filled=filled))
    expected = journal.lookup_reservation('reservation-1'), journal.order_state('request-1')
    before = journal.path.read_bytes()
    result = pair(journal)
    assert result == expected and journal.path.read_bytes() == before
    result[1]['evidence']['status'] = 'CHANGED'
    if result[1]['evidence']['fills']:
        result[1]['evidence']['fills'][0]['quantity'] = 999
    if result[1]['cancellation']:
        result[1]['cancellation']['reason'] = 'CHANGED'
    assert pair(journal) == expected
    writer_can_enter(journal.path)


def test_pair_before_first_status_preserves_unknown_fills_and_original_reservation(tmp_path):
    journal = accepted(tmp_path)
    result = pair(journal)
    assert result == (journal.lookup_reservation('reservation-1'), journal.order_state('request-1'))
    assert result[0].status == 'SUBMITTED' and result[0].filled_quantity == 0
    assert result[1]['evidence'] is None


@pytest.mark.parametrize('table,column', [
    ('control', 'sha256'), ('records', 'sha256'), ('events', 'sha256'),
    ('lifecycle_control', 'sha256'), ('lifecycle_records', 'sha256'),
    ('lifecycle_events', 'sha256'), ('lifecycle_evidence_ids', 'evidence_sha256')])
def test_pair_audits_complete_original_submission_and_lifecycle(tmp_path, table, column):
    journal = accepted(tmp_path)
    observe(journal, order_evidence())
    with sqlite3.connect(journal.path) as db:
        db.execute('UPDATE ' + table + ' SET ' + column + "='corrupt'")
    before = journal.path.read_bytes()
    with pytest.raises(JournalCorruptionError):
        pair(journal)
    assert journal.path.read_bytes() == before
    writer_can_enter(journal.path)


def test_pair_rejects_replacement_with_same_binding(tmp_path):
    journal = accepted(tmp_path)
    replacement = ScoutJournal(tmp_path / 'replacement.sqlite3', binding=BINDING)
    replacement.close()
    os.replace(replacement.path, journal.path)
    before = journal.path.read_bytes()
    with pytest.raises(JournalCorruptionError, match='identity'):
        pair(journal)
    assert journal.path.read_bytes() == before


@pytest.mark.parametrize('reservation,expected_request', [
    ('missing', 'request-1'), ('reservation-1', 'another-request')])
def test_pair_keeps_exact_accepted_reservation_and_request_binding(tmp_path, reservation, expected_request):
    journal = accepted(tmp_path)
    before = journal.path.read_bytes()
    with pytest.raises(JournalStateError):
        pair(journal, reservation, expected_request)
    assert journal.path.read_bytes() == before


@pytest.mark.parametrize('state', ['GRANTED', 'SENDING', 'UNKNOWN', 'NOT_SENT'])
def test_pair_never_treats_unaccepted_send_as_an_accepted_reservation(tmp_path, state):
    journal = ready(tmp_path)
    if state == 'NOT_SENT':
        journal.revoke_unsent('request-1', 'fixture')
    elif state != 'GRANTED':
        token = journal.claim_send('request-1')
        if state == 'UNKNOWN':
            journal.outcome('request-1', token, None)
    before = journal.path.read_bytes()
    with pytest.raises(JournalStateError, match='accepted'):
        pair(journal)
    assert journal.path.read_bytes() == before


def test_pair_copy_failure_preserves_original_exception_and_rolls_back(tmp_path, monkeypatch):
    journal = accepted(tmp_path)
    original = journal._audit
    before = journal.path.read_bytes()
    failure = SystemExit('copy fixture')
    def audit(db, **kwargs):
        value = original(db, **kwargs)
        db.execute("UPDATE control SET sha256='rollback-required'")
        return value
    def failed_copy(value):
        raise failure
    with monkeypatch.context() as patch:
        patch.setattr(journal, '_audit', audit)
        patch.setattr(journal_module, 'deepcopy', failed_copy)
        with pytest.raises(SystemExit) as raised:
            pair(journal)
        assert raised.value is failure
    assert journal.path.read_bytes() == before
    writer_can_enter(journal.path)
    pair(journal)


def test_projection_uses_one_fresh_audit_and_closes_it_before_persistence(tmp_path, monkeypatch):
    case = fixture(tmp_path)
    record = case.journal.get(case.ids[0])
    audits = count_audits(monkeypatch, case.journal)
    active = []
    original_transaction = case.journal._transaction
    @contextmanager
    def transaction(**kwargs):
        with original_transaction(**kwargs) as db:
            active.append(db)
            try:
                yield db
            finally:
                active.pop()
    monkeypatch.setattr(case.journal, '_transaction', transaction)
    original_record = case.executor._record
    def persist(*args, **kwargs):
        assert not active
        writer_can_enter(case.journal.path)
        return original_record(*args, **kwargs)
    case.executor._record = persist
    result = case.executor._project(case.journal, record, {'forecast_id': 'b' * 64}, 'fixture')
    assert len(audits) == 1 and result['order']['status'] == 'FILLED'
    assert result['require_original_intention'] is True


def test_projection_rereads_fresh_evidence_after_recovery_and_each_later_call(tmp_path):
    case = fixture(tmp_path, status='WORKING')
    old = case.journal.get(case.ids[0])
    record, lifecycle = case.executor._recover_record_for_step(case.journal, old)
    assert lifecycle['evidence']['status'] == 'WORKING'
    evidence = order_evidence(identity='later-fill', status='PARTIAL', filled=1,
        observed='2026-10-12T18:03:00+00:00', reservation_id='reservation-0', broker_order_id='order-0')
    case.journal.observe_order(case.ids[0], evidence, as_of=case.executor.clock())
    first = case.executor._project(case.journal, record, {'forecast_id': 'b' * 64}, 'fixture')
    assert first['order']['filled_quantity'] == 1
    assert first['evidence']['order_evidence']['evidence_id'] == 'later-fill'
    evidence = order_evidence(identity='final-fill', status='FILLED', filled=2,
        observed='2026-10-12T18:04:00+00:00', reservation_id='reservation-0', broker_order_id='order-0')
    case.journal.observe_order(case.ids[0], evidence, as_of=case.executor.clock())
    second = case.executor._project(case.journal, record, {'forecast_id': 'b' * 64}, 'fixture')
    assert second['order']['filled_quantity'] == 2
    assert second['evidence']['order_evidence']['evidence_id'] == 'final-fill'


@pytest.mark.parametrize('table', ['control', 'lifecycle_control'])
def test_projection_does_not_reuse_pre_corruption_recovery_pair(tmp_path, table):
    case = fixture(tmp_path)
    record, _ = case.executor._recover_record_for_step(case.journal, case.journal.get(case.ids[0]))
    with sqlite3.connect(case.journal.path) as db:
        db.execute('UPDATE ' + table + " SET sha256='corrupt'")
    case.executor._record = lambda *a, **k: pytest.fail('Damaged projection must not persist')
    with pytest.raises(JournalCorruptionError):
        case.executor._project(case.journal, record, {'forecast_id': 'b' * 64}, 'fixture')


@pytest.mark.parametrize('name', ['lookup_reservation', 'order_state'])
@pytest.mark.parametrize('scope', ['instance', 'class'])
def test_overridden_original_readers_keep_separate_original_path(tmp_path, monkeypatch, name, scope):
    case = fixture(tmp_path)
    record = case.journal.get(case.ids[0])
    original = getattr(ScoutJournal, name)
    calls = []
    if scope == 'instance':
        def reader(key):
            calls.append(key)
            return original(case.journal, key)
        monkeypatch.setattr(case.journal, name, reader)
    else:
        def reader(self, key):
            calls.append(key)
            return original(self, key)
        monkeypatch.setattr(ScoutJournal, name, reader)
    audits = count_audits(monkeypatch, case.journal)
    case.executor._project(case.journal, record, {'forecast_id': 'b' * 64}, 'fixture')
    assert len(calls) == 1 and len(audits) == 2


@pytest.mark.parametrize('scope', ['instance', 'class'])
@pytest.mark.parametrize('callable_pair', [False, True])
def test_replaced_optional_pair_keeps_original_projection_path(tmp_path, monkeypatch, scope, callable_pair):
    case = fixture(tmp_path)
    record = case.journal.get(case.ids[0])
    def replacement(*args, **kwargs):
        pytest.fail('A custom optional pair must not bypass original readers')
    monkeypatch.setattr(case.journal if scope == 'instance' else ScoutJournal,
        'lookup_reservation_with_order_state', replacement if callable_pair else None)
    audits = count_audits(monkeypatch, case.journal)
    result = case.executor._project(case.journal, record, {'forecast_id': 'b' * 64}, 'fixture')
    assert result['order']['status'] == 'FILLED' and len(audits) == 2


@pytest.mark.parametrize('scope', ['instance', 'class', 'subclass'])
def test_overridden_order_view_is_never_bypassed(tmp_path, monkeypatch, scope):
    case = fixture(tmp_path)
    record = case.journal.get(case.ids[0])
    calls = []
    original = execution.ScoutLocalAccountExecutor._order
    def custom(journal, supplied):
        calls.append(supplied)
        return original(journal, supplied)
    if scope == 'instance':
        monkeypatch.setattr(case.executor, '_order', custom)
    elif scope == 'class':
        monkeypatch.setattr(execution.ScoutLocalAccountExecutor, '_order', staticmethod(custom))
    else:
        class Custom(execution.ScoutLocalAccountExecutor):
            _order = staticmethod(custom)
        case.executor.__class__ = Custom
    audits = count_audits(monkeypatch, case.journal)
    result = case.executor._project(case.journal, record, {'forecast_id': 'b' * 64}, 'fixture')
    assert calls == [record] and len(audits) == 2 and result['order']['status'] == 'FILLED'


def test_native_subclass_keeps_original_projection_path(tmp_path, monkeypatch):
    case = fixture(tmp_path)
    record = case.journal.get(case.ids[0])
    class Custom(ScoutJournal):
        pass
    case.journal.__class__ = Custom
    audits = count_audits(monkeypatch, case.journal)
    case.executor._project(case.journal, record, {'forecast_id': 'b' * 64}, 'fixture')
    assert len(audits) == 2


@pytest.mark.parametrize('state', ['PREPARED', 'GRANTED', 'SENDING', 'UNKNOWN'])
def test_nonaccepted_projection_preserves_original_unknown_and_reservation_view(tmp_path, monkeypatch, state):
    case = fixture(tmp_path, state=state)
    record = case.journal.get(case.ids[0])
    expected_order = case.executor._order(case.journal, record)
    expected_state = case.journal.order_state(case.ids[0])
    audits = count_audits(monkeypatch, case.journal)
    before = case.journal.path.read_bytes()
    result = case.executor._project(case.journal, record, {'forecast_id': 'b' * 64}, 'fixture')
    assert result['order'] == (asdict(expected_order) if expected_order else None)
    assert result['evidence']['order_evidence'] == expected_state['evidence']
    assert len(audits) == 1 and case.journal.path.read_bytes() == before
