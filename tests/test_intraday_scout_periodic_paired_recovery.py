"""Periodic recovery uses audited pairs without changing projection/effect rules."""
from contextlib import nullcontext
from dataclasses import asdict, replace
import json
import sqlite3
from types import SimpleNamespace

import pytest

from ml.intraday import scout_local_execution as execution
from ml.intraday.scout_journal import ScoutJournal, JournalCorruptionError, JournalStateError
from test_intraday_scout_journal import BINDING, request, grant, order_evidence


def fixture(tmp_path, *, count=1, state='ACCEPTED', status='FILLED'):
    journal = ScoutJournal(tmp_path / 'private-journal.sqlite3', binding=BINDING)
    ids, tokens = [], {}
    for index in range(count):
        rid = execution.PREFIX + 'periodic-fixture-' + str(index)
        req = request(rid)
        journal.prepare(req)
        if state != 'PREPARED':
            journal.grant(grant(req, gid='grant-' + str(index), reservation='reservation-' + str(index)))
        if state in {'SENDING', 'UNKNOWN', 'ACCEPTED', 'NOT_SENT'}:
            tokens[rid] = journal.claim_send(rid)
        if state == 'UNKNOWN':
            journal.outcome(rid, tokens[rid], None)
        elif state == 'ACCEPTED':
            journal.outcome(rid, tokens[rid], 'order-' + str(index))
            evidence = order_evidence(identity='initial-' + str(index),
                status=status, filled=2 if status == 'FILLED' else 0,
                reservation_id='reservation-' + str(index), broker_order_id='order-' + str(index))
            evidence = replace(evidence, fills=tuple(replace(fill, fill_id='fill-' + str(index) + '-' + str(i))
                for i, fill in enumerate(evidence.fills)))
            journal.observe_order(rid, evidence, as_of='2026-10-12T18:10:00Z')
        elif state == 'NOT_SENT':
            journal.outcome(rid, tokens[rid], None)
        ids.append(rid)
    executor = execution.ScoutLocalAccountExecutor.__new__(execution.ScoutLocalAccountExecutor)
    lookups, controls, polls = [], [], []
    executor._account = lambda: nullcontext()
    executor._journals = lambda: iter((journal,))
    executor._controls = lambda: controls.append(True)
    def forecast(fid, *, pc):
        lookups.append((fid, pc))
        return {'forecast_id': fid, 'symbol': 'DOCU'}
    executor.store = SimpleNamespace(forecast_by_id=forecast)
    executor.clock = lambda: execution.instant('2026-10-12T18:10:00Z')
    executor._commitment_clearance = lambda *_: None
    def record(forecast, disposition, *, order=None, **kwargs):
        return {'forecast_id': forecast['forecast_id'], 'disposition': disposition,
                'order': asdict(order) if order else None, **kwargs}
    executor._record = record
    def adapter(given, old):
        def status_call(rid):
            polls.append(rid)
            with sqlite3.connect(journal.path, timeout=0, isolation_level=None) as db:
                db.execute('BEGIN IMMEDIATE')
                db.rollback()
            index = ids.index(rid)
            return order_evidence(identity='poll-' + str(index), status='WORKING', filled=0,
                observed='2026-10-12T18:02:00+00:00',
                reservation_id='reservation-' + str(index), broker_order_id='order-' + str(index))
        return SimpleNamespace(status=status_call)
    executor._adapter = adapter
    return SimpleNamespace(journal=journal, executor=executor, ids=ids, tokens=tokens,
                           lookups=lookups, controls=controls, polls=polls)


def count_audits(monkeypatch, journal):
    values = []
    original = journal._audit
    def counted(*args, **kwargs):
        values.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(journal, '_audit', counted)
    return values


@pytest.mark.parametrize('count', [1, 4, 12])
def test_terminal_periodic_pass_keeps_full_audits_and_rereads_each_next_pass(tmp_path, monkeypatch, count):
    case = fixture(tmp_path, count=count)
    audits = count_audits(monkeypatch, case.journal)
    before = case.journal.path.read_bytes()
    first = case.executor.recover()
    assert len(audits) == 1 + 2 * count
    assert len(case.lookups) == 1 and len(case.controls) == count and case.polls == []
    audits.clear()
    second = case.executor.recover()
    assert first == second and len(audits) == 1 + 2 * count
    assert len(case.lookups) == 2 and len(case.controls) == count * 2
    assert case.journal.path.read_bytes() == before
    assert all(row['order']['status'] == 'FILLED' and row['order']['filled_quantity'] == 2 for row in first)


def test_working_periodic_pass_closes_pair_before_status_and_reaudits_after_io(tmp_path, monkeypatch):
    case = fixture(tmp_path, status='WORKING')
    audits = count_audits(monkeypatch, case.journal)
    result = case.executor.recover()
    assert len(audits) == 5  # records + before pair + observation + after pair + fresh projection pair
    assert case.polls == case.ids and result[0]['order']['status'] == 'WORKING'
    assert result[0]['evidence']['order_evidence']['evidence_id'] == 'poll-0'


@pytest.mark.parametrize('optional', ['absent', 'noncallable'])
def test_custom_journal_periodic_fallback_retains_original_full_reads(tmp_path, monkeypatch, optional):
    case = fixture(tmp_path)
    class Legacy:
        def __getattr__(self, name):
            if name == 'get_with_order_state':
                if optional == 'absent':
                    raise AttributeError(name)
                return None
            return getattr(case.journal, name)
    case.executor._journals = lambda: iter((Legacy(),))
    audits = count_audits(monkeypatch, case.journal)
    assert case.executor.recover()[0]['order']['status'] == 'FILLED'
    assert len(audits) == 5  # records + original state/get + fresh reservation/state


@pytest.mark.parametrize('state', ['PREPARED', 'GRANTED', 'SENDING', 'UNKNOWN'])
def test_nonaccepted_states_keep_original_recovery_and_permanent_send_identity(tmp_path, monkeypatch, state):
    case = fixture(tmp_path, state=state)
    def forbidden(*_):
        raise AssertionError('Unaccepted state must retain original recovery path')
    monkeypatch.setattr(case.journal, 'get_with_order_state', forbidden)
    case.executor.recover()
    row = case.journal.get(case.ids[0])
    assert row['state'] == ('NOT_SENT' if state in {'PREPARED', 'GRANTED'} else 'UNKNOWN')
    if case.tokens:
        assert row['send_token'] == case.tokens[case.ids[0]]
        with pytest.raises(JournalStateError):
            case.journal.claim_send(case.ids[0])
    assert case.polls == []


@pytest.mark.parametrize('table', ['control', 'lifecycle_control'])
def test_corruption_after_records_is_not_hidden_by_periodic_pair(tmp_path, table):
    case = fixture(tmp_path)
    def corrupt():
        with sqlite3.connect(case.journal.path) as db:
            db.execute('UPDATE ' + table + " SET sha256='corrupt'")
    case.executor._controls = corrupt
    with pytest.raises(JournalCorruptionError):
        case.executor.recover()
    assert case.polls == []


def test_missing_forecast_still_fails_before_projection(tmp_path):
    case = fixture(tmp_path)
    case.executor.store.forecast_by_id = lambda *args, **kwargs: None
    case.executor._record = lambda *args, **kwargs: pytest.fail('Missing original must not project')
    with pytest.raises(execution.ContractError, match='original immutable intention'):
        case.executor.recover()


def test_controls_still_fence_recovery_before_pair(tmp_path, monkeypatch):
    case = fixture(tmp_path)
    original = RuntimeError('manual control fixture')
    def stopped():
        raise original
    case.executor._controls = stopped
    monkeypatch.setattr(case.journal, 'get_with_order_state', lambda *_: pytest.fail('Pair after stopped controls'))
    with pytest.raises(RuntimeError) as raised:
        case.executor.recover()
    assert raised.value is original
