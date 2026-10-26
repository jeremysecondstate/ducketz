"""Native paired reads and the real executor keep effect gates and send fences."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
import json
import os
import sqlite3
from threading import Thread
from types import SimpleNamespace

import pytest

from ml.intraday import scout_journal as journal_module
from ml.intraday import scout_local_execution as execution
from ml.intraday.scout_journal import JournalCorruptionError, JournalStateError, ScoutJournal
from test_intraday_scout_journal import BINDING, accepted, observe, order_evidence
from test_intraday_scout_local_execution import env, forecast, raw_order, record


def worker(call):
    errors = []
    def run():
        try:
            call()
        except BaseException as error:
            errors.append(error)
    thread = Thread(target=run)
    thread.start()
    thread.join(5)
    assert not thread.is_alive(), 'native fixture writer did not finish'
    assert not errors, errors


def writer_can_enter(path):
    with sqlite3.connect(path, timeout=0, isolation_level=None) as db:
        db.execute('BEGIN IMMEDIATE')
        db.rollback()


@pytest.mark.parametrize('status,filled', [
    ('WORKING', 0), ('PARTIAL', 1), ('FILLED', 2), ('CANCELLED', 1), ('REJECTED', 0)])
def test_pair_matches_independent_full_reads_and_is_deeply_detached(tmp_path, status, filled):
    journal = accepted(tmp_path)
    observe(journal, order_evidence(status=status, filled=filled))
    original = journal.get('request-1'), journal.order_state('request-1')
    pair = journal.get_with_order_state('request-1')
    assert pair == original
    pair[0]['request']['symbol'] = 'CHANGED'
    pair[1]['evidence']['status'] = 'CHANGED'
    if pair[1]['evidence']['fills']:
        pair[1]['evidence']['fills'][0]['quantity'] = 999
    assert journal.get_with_order_state('request-1') == original


def test_pair_returns_original_empty_lifecycle_before_first_status(tmp_path):
    journal = accepted(tmp_path)
    assert journal.get_with_order_state('request-1') == (
        journal.get('request-1'), journal.order_state('request-1'))
    assert journal.get_with_order_state('request-1')[1]['evidence'] is None


@pytest.mark.parametrize('table,column', [
    ('control', 'sha256'), ('records', 'sha256'), ('events', 'sha256'),
    ('lifecycle_control', 'sha256'), ('lifecycle_records', 'sha256'),
    ('lifecycle_events', 'sha256'), ('lifecycle_evidence_ids', 'evidence_sha256')])
def test_pair_keeps_complete_submission_and_lifecycle_audit(tmp_path, table, column):
    journal = accepted(tmp_path)
    observe(journal, order_evidence())
    with sqlite3.connect(journal.path) as db:
        db.execute(f"UPDATE {table} SET {column}='corrupt'")
    before = journal.path.read_bytes()
    with pytest.raises(JournalCorruptionError):
        journal.get_with_order_state('request-1')
    assert journal.path.read_bytes() == before
    writer_can_enter(journal.path)


def test_pair_does_not_accept_replacement_with_same_binding(tmp_path):
    journal = accepted(tmp_path)
    replacement = ScoutJournal(tmp_path / 'replacement.sqlite3', binding=BINDING)
    replacement.close()
    os.replace(replacement.path, journal.path)
    before = journal.path.read_bytes()
    with pytest.raises(JournalCorruptionError, match='identity'):
        journal.get_with_order_state('request-1')
    assert journal.path.read_bytes() == before


def test_pair_rolls_back_and_releases_connection_on_original_baseexception(tmp_path, monkeypatch):
    journal = accepted(tmp_path)
    original_audit = journal._audit
    before = journal.path.read_bytes()
    error = SystemExit('private copy boundary')
    def audit(db, **kwargs):
        result = original_audit(db, **kwargs)
        db.execute("UPDATE control SET sha256='must-rollback'")
        return result
    def failed_copy(value):
        raise error
    with monkeypatch.context() as change:
        change.setattr(journal, '_audit', audit)
        change.setattr(journal_module, 'deepcopy', failed_copy)
        with pytest.raises(SystemExit) as caught:
            journal.get_with_order_state('request-1')
        assert caught.value is error
    assert journal.path.read_bytes() == before
    worker(lambda: writer_can_enter(journal.path))
    assert journal.get_with_order_state('request-1')[0]['state'] == 'ACCEPTED'


def test_pair_excludes_concurrent_writer_only_until_transaction_returns(tmp_path, monkeypatch):
    journal = accepted(tmp_path)
    original = journal._audit
    attempts = []
    def audit(db, **kwargs):
        def contend():
            try:
                writer_can_enter(journal.path)
            except sqlite3.OperationalError as error:
                attempts.append(error.sqlite_errorcode & 255)
        worker(contend)
        return original(db, **kwargs)
    with monkeypatch.context() as change:
        change.setattr(journal, '_audit', audit)
        journal.get_with_order_state('request-1')
    assert attempts == [sqlite3.SQLITE_BUSY]
    worker(lambda: writer_can_enter(journal.path))


@pytest.mark.parametrize('status,filled,saving', [
    ('WORKING', 0, 1), ('FILLED', 2, 2), ('CANCELLED', 1, 2), ('REJECTED', 0, 2)])
def test_recovery_pair_removes_redundant_audits_without_changing_native_state(tmp_path, monkeypatch, status, filled, saving):
    journal = accepted(tmp_path)
    observe(journal, order_evidence(status=status, filled=filled))
    old = journal.get('request-1')
    executor = execution.ScoutLocalAccountExecutor.__new__(execution.ScoutLocalAccountExecutor)
    executor.clock = lambda: execution.instant('2026-10-12T18:10:00Z')
    executor._adapter = lambda *_: SimpleNamespace(status=lambda *_: order_evidence(
        identity='later-status', status=status, filled=filled, observed='2026-10-12T18:02:00+00:00'))
    count = []
    original = journal._audit
    def audit(*args, **kwargs):
        count.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(journal, '_audit', audit)
    baseline = executor._recover_record(journal, old), journal.order_state('request-1')
    baseline_count = len(count)
    count.clear()
    pair = executor._recover_record_for_step(journal, old)
    assert pair == baseline
    assert baseline_count - len(count) == saving


@pytest.mark.parametrize('optional', ['absent', 'noncallable'])
def test_custom_journal_without_optional_pair_keeps_original_recovery(tmp_path, optional):
    journal = accepted(tmp_path)
    observe(journal, order_evidence(status='FILLED', filled=2))
    class Legacy:
        def __getattr__(self, name):
            if name == 'get_with_order_state':
                if optional == 'absent':
                    raise AttributeError(name)
                return None
            return getattr(journal, name)
    executor = execution.ScoutLocalAccountExecutor.__new__(execution.ScoutLocalAccountExecutor)
    old = journal.get('request-1')
    expected = executor._recover_record(journal, old)
    assert executor._recover_record_for_step(Legacy(), old) == (expected, None)


@pytest.mark.parametrize('action', ['partial', 'terminal', 'corrupt', 'rollback', 'replace'])
def test_recovery_closes_pair_before_status_and_reaudits_after_concurrent_io(tmp_path, monkeypatch, action):
    journal = accepted(tmp_path)
    observe(journal, order_evidence())
    old = journal.get('request-1')
    executor = execution.ScoutLocalAccountExecutor.__new__(execution.ScoutLocalAccountExecutor)
    executor.clock = lambda: execution.instant('2026-10-12T18:10:00Z')
    active = []
    original = journal._transaction
    @contextmanager
    def transaction(**kwargs):
        with original(**kwargs) as db:
            active.append(db)
            try:
                yield db
            finally:
                active.pop()
    monkeypatch.setattr(journal, '_transaction', transaction)
    after_writer = []
    def mutate():
        if action in {'partial', 'terminal'}:
            with ScoutJournal(journal.path, binding=BINDING, create=False,
                              expected_journal_id=journal.journal_id) as other:
                observe(other, order_evidence(identity='concurrent-status',
                    status='PARTIAL' if action == 'partial' else 'FILLED',
                    filled=1 if action == 'partial' else 2, observed='2026-10-12T18:02:00+00:00'))
        elif action in {'corrupt', 'rollback'}:
            with sqlite3.connect(journal.path, timeout=0, isolation_level=None) as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute("UPDATE lifecycle_control SET sha256='concurrent-corruption'")
                db.rollback() if action == 'rollback' else db.commit()
        else:
            replacement = ScoutJournal(tmp_path / 'replacement.sqlite3', binding=BINDING)
            replacement.close()
            os.replace(replacement.path, journal.path)
        after_writer.append(journal.path.read_bytes())
    def status(rid):
        assert rid == 'request-1' and not active
        worker(mutate)
        return order_evidence(identity='response-status', status='WORKING' if action == 'terminal' else 'FILLED',
            filled=0 if action == 'terminal' else 2, observed='2026-10-12T18:03:00+00:00')
    def adapter(given_journal, given_old):
        assert given_journal is journal and given_old is old
        return SimpleNamespace(status=status)
    executor._adapter = adapter
    if action in {'partial', 'rollback'}:
        pair = executor._recover_record_for_step(journal, old)
        assert pair[1]['evidence']['status'] == 'FILLED'
        assert pair[1]['evidence']['cumulative_filled_quantity'] == 2
        assert pair == (journal.get('request-1'), journal.order_state('request-1'))
    else:
        error = JournalStateError if action == 'terminal' else JournalCorruptionError
        with pytest.raises(error):
            executor._recover_record_for_step(journal, old)
        assert journal.path.read_bytes() == after_writer[0]
    assert not active
    worker(lambda: writer_can_enter(journal.path))


def terminal_order(env):
    assert env.executor.step(env.forecast)['disposition'] == 'submitted'
    original = env.forecast
    env.state.now += timedelta(seconds=2)
    env.broker.orders = [raw_order(env, status='FILLED', filled=2)]
    assert env.executor.recover()[0]['filled_quantity'] == 2
    return original


def test_real_step_same_forecast_projects_exact_terminal_fills_without_replay(env):
    original = terminal_order(env)
    before = record(env, original)
    result = env.executor.step(original)
    assert result['disposition'] == 'recover_existing_intention'
    assert result['filled_quantity'] == 2 and result['order']['status'] == 'FILLED'
    assert record(env, original) == before
    assert len(env.broker.posts) == 1 and env.broker.deletes == []


def test_real_step_terminal_scan_still_uses_actual_zero_capacity(env):
    original = terminal_order(env)
    env.forecast = forecast(env, advance=True)
    env.cash = 0
    value = env.executor.step(env.forecast)
    assert value['disposition'] == 'no_purchasing_capacity'
    assert value['desired_quantity'] == 4 and value['adjusted_quantity'] == 0
    assert record(env) is None and record(env, original)['state'] == 'ACCEPTED'
    assert len(env.broker.posts) == 1 and env.broker.deletes == []


@pytest.mark.parametrize('change', ['stop', 'account', 'quote', 'receipt'])
def test_real_step_terminal_pair_does_not_bypass_final_native_post_gates(env, monkeypatch, change):
    terminal_order(env)
    env.forecast = forecast(env, advance=True)
    paired = []
    original = ScoutJournal.get_with_order_state
    def pair(journal, rid):
        paired.append(rid)
        return original(journal, rid)
    monkeypatch.setattr(ScoutJournal, 'get_with_order_state', pair)
    def before_post():
        if change == 'stop':
            env.state.stop = True
        elif change == 'account':
            env.broker.account = 'b' * 64
        elif change == 'quote':
            env.state.now += timedelta(seconds=61)
        else:
            for path in (env.executor.books.root / 'receipts').glob('*.json'):
                value = json.loads(path.read_text())
                if value['kind'] == 'local-account-permit' and value['payload']['manual_session_id'] == env.executor.session_id:
                    value['payload']['capacity']['adjusted_quantity'] = 99
                    path.write_text(json.dumps(value))
    env.broker.before_transport = before_post
    result = env.executor.step(env.forecast)
    assert paired
    assert result['disposition'] == 'submission_recovery_required'
    assert record(env)['state'] == 'UNKNOWN'
    assert len(env.broker.posts) == 1 and env.broker.deletes == []


def test_real_step_expiry_during_terminal_pair_does_not_create_attempt(env, monkeypatch):
    terminal_order(env)
    env.forecast = forecast(env, advance=True)
    original = ScoutJournal.get_with_order_state
    def expires(journal, rid):
        result = original(journal, rid)
        env.state.now = execution.instant(env.forecast['target_close'])
        return result
    monkeypatch.setattr(ScoutJournal, 'get_with_order_state', expires)
    result = env.executor.step(env.forecast)
    assert result['disposition'] == 'fixed_target_already_elapsed'
    assert record(env) is None
    assert len(env.broker.posts) == 1 and env.broker.deletes == []


@pytest.mark.parametrize('state', ['SENDING', 'UNKNOWN'])
def test_real_step_uncertain_send_never_uses_pair_or_reclaims_send_token(env, monkeypatch, state):
    env.broker.post_error = TimeoutError('lost native reply')
    assert env.executor.step(env.forecast)['submission_status'] == 'UNKNOWN'
    old_forecast = env.forecast
    old = record(env)
    if state == 'SENDING':
        # The native journal-only fixture starts at an interrupted claimed-send boundary.
        # An actual SENDING case is covered by the unchanged journal transition below.
        journal = env.executor.books.create_session('prior-interrupted-send')
        req = execution.Request.from_payload(dict(old['request'], request_id='scout-local:interrupted', session_id='prior-interrupted-send'))
        permit = execution.Grant.from_payload(dict(old['grant'], request_id=req.request_id, request_sha256=req.sha256,
            session_id=req.session_id, grant_id='interrupted-grant', reservation_id='interrupted-reservation'))
        journal.prepare(req); journal.grant(permit); token = journal.claim_send(req.request_id)
        recovered, pair = env.executor._recover_record_for_step(journal, journal.get(req.request_id))
        assert recovered['state'] == 'UNKNOWN' and recovered['send_token'] == token and pair is None
        journal.close()
    def forbidden(*_):
        raise AssertionError('UNKNOWN must keep the original recovery path')
    monkeypatch.setattr(ScoutJournal, 'get_with_order_state', forbidden)
    env.forecast = forecast(env, advance=True)
    result = env.executor.step(env.forecast)
    assert result['disposition'] == 'submission_recovery_required'
    assert record(env, old_forecast)['send_token'] == old['send_token']
    assert len(env.broker.posts) == 1 and env.broker.deletes == []


def test_real_step_working_pair_keeps_exact_cancellation_and_one_delete(env):
    assert env.executor.step(env.forecast)['disposition'] == 'submitted'
    original = env.forecast
    env.state.now += timedelta(seconds=2)
    env.broker.orders = [raw_order(env, status='PARTIALLY_FILLED', filled=1)]
    env.forecast = forecast(env, advance=True)
    first = env.executor.step(env.forecast)
    assert first['disposition'] == 'previous_intention_reconciliation'
    assert [identity for identity, _ in env.broker.deletes] == ['123']
    env.state.now += timedelta(seconds=1)
    env.executor.step(env.forecast)
    assert len(env.broker.deletes) == 1 and len(env.broker.posts) == 1
    old = record(env, original)
    lifecycle = env.executor.journal.order_state(old['request']['request_id'])
    assert lifecycle['evidence']['status'] == 'PARTIAL'
    assert lifecycle['evidence']['cumulative_filled_quantity'] == 1


def test_real_step_same_forecast_unknown_projects_before_distinct_forecast_clearance(env, monkeypatch):
    env.broker.post_error = TimeoutError('lost native reply')
    assert env.executor.step(env.forecast)['submission_status'] == 'UNKNOWN'
    before = record(env)
    def forbidden(*_):
        raise AssertionError('same forecast must project its original UNKNOWN first')
    monkeypatch.setattr(env.executor, '_commitment_clearance', forbidden)
    value = env.executor.step(env.forecast)
    assert value['disposition'] == 'recover_existing_intention'
    assert value['submission_status'] == 'UNKNOWN'
    assert record(env) == before
    assert len(env.broker.posts) == 1 and env.broker.deletes == []


@pytest.mark.parametrize('visible', [False, True])
def test_real_step_cross_symbol_pair_still_requires_current_broker_working_capacity(env, visible):
    env.executor.symbols = ('DOCU', 'DBX')
    env.executor.profile['symbols'] = ['DOCU', 'DBX']
    previous = env.executor.books.create_session('prior-other-symbol')
    now = env.state.now.isoformat()
    req = execution.Request('scout-local:other-symbol', env.executor.account_fingerprint,
        'prior-other-symbol', 'DBX', 'BUY', 2, '100', 'b' * 64, 'b' * 64,
        'c' * 64, 'd' * 64, env.forecast['reference_at'], now,
        env.forecast['target_close'], env.forecast['target_close'])
    permit = execution.Grant('other-symbol-permit', req.request_id, req.sha256,
        req.account_fingerprint, req.session_id, 1, 2, 'other-symbol-reservation', now, req.valid_until)
    previous.prepare(req)
    previous.grant(permit)
    token = previous.claim_send(req.request_id)
    previous.outcome(req.request_id, token, '123')
    previous.close()
    other_order = raw_order(env)
    other_order['orderLegCollection'][0]['instrument']['symbol'] = 'DBX'
    env.broker.orders = [other_order]
    env.cash = 0
    env.working = ((dict(order_id='123', symbol='DBX', instruction='BUY', remaining_quantity=2),)
                   if visible else ())
    value = env.executor.step(env.forecast)
    assert value['disposition'] == ('no_purchasing_capacity' if visible else 'broker_capacity_reconciliation_required')
    assert record(env) is None
    assert env.broker.posts == [] and env.broker.deletes == []
