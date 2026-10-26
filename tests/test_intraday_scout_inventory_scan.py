"""Native inventory scans retain file/identity audits without rereading N inventories."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import socket
from types import SimpleNamespace

import pytest

from ml.intraday import scout_session_books as books_module
from ml.intraday.scout_session_books import ScoutBooks
from ml.intraday.scout_journal import JournalError, ScoutJournal
from ml.intraday.scout_local_execution import ScoutLocalAccountExecutor
from ml.intraday.scout_protocol import canonical_sha256


def write_inventory(books, value):
    books.path.write_text(json.dumps(dict(inventory=value, sha256=canonical_sha256(value))), encoding='utf-8')


def add_journal(books, value, session):
    filename = canonical_sha256({'session_id': session}) + '.sqlite3'
    with ScoutJournal(books.root / 'journals' / filename, binding=books._binding(session)) as journal:
        value['sessions'][session] = dict(filename=filename, binding=books._binding(session), journal_id=journal.journal_id)
    return books.root / 'journals' / filename


def fixture(tmp_path, count):
    root = tmp_path / 'books'
    (root / 'journals').mkdir(parents=True)
    books = ScoutBooks(root, account_fingerprint='a' * 64)
    value = books._empty()
    for index in range(count):
        add_journal(books, value, 'fixture-session-' + str(index))
    write_inventory(books, value)
    return books, value


def executor(books):
    result = ScoutLocalAccountExecutor.__new__(ScoutLocalAccountExecutor)
    result.books = books
    return result


def collect(iterator):
    return [(journal.binding['session_id'], journal.journal_id, journal.records()) for journal in iterator]


def legacy(books):
    for session in books.sessions():
        with books.open_session(session) as journal:
            yield journal


def count_reads(books, monkeypatch):
    calls = []
    original = books._read
    def read(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(books, '_read', read)
    return calls


@pytest.mark.parametrize('count', [0, 1, 50])
def test_one_complete_inventory_read_same_original_order_records_and_audits(tmp_path, monkeypatch, count):
    books, _ = fixture(tmp_path, count)
    expected = collect(legacy(books))
    before = {str(p.relative_to(books.root)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in books.root.rglob('*') if p.is_file()}
    calls = count_reads(books, monkeypatch)
    audits = []
    original = ScoutJournal._audit
    def audit(self, *args, **kwargs):
        audits.append(self.path.name)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(ScoutJournal, '_audit', audit)
    assert collect(executor(books)._journals()) == expected
    assert len(calls) == 1
    assert len(audits) == count * 2  # Native constructor plus records; neither was skipped.
    after = {str(p.relative_to(books.root)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in books.root.rglob('*') if p.is_file()}
    assert after == before


def test_session_insertion_order_is_retained(tmp_path):
    books, value = fixture(tmp_path, 0)
    for name in ('z-last-name', 'a-first-name', 'middle'):
        add_journal(books, value, name)
    write_inventory(books, value)
    assert [row[0] for row in collect(executor(books)._journals())] == list(value['sessions'])


def test_native_generator_close_closes_current_journal(tmp_path):
    books, _ = fixture(tmp_path, 2)
    iterator = executor(books)._journals()
    current = next(iterator)
    assert not current._closed
    iterator.close()
    assert current._closed


def test_custom_books_without_native_api_keeps_original_path(tmp_path):
    books, _ = fixture(tmp_path, 2)
    calls = []
    @contextmanager
    def open_session(session):
        calls.append(session)
        with books.open_session(session) as journal:
            yield journal
    custom = SimpleNamespace(sessions=books.sessions, open_session=open_session)
    assert collect(executor(custom)._journals()) == collect(legacy(books))
    assert calls == list(books.sessions())


def test_subclass_override_keeps_original_sessions_open_session_contract(tmp_path, monkeypatch):
    books, _ = fixture(tmp_path, 2)
    class CustomBooks(ScoutBooks):
        def open_sessions(self):
            pytest.fail('A custom subclass keeps the legacy extension path')
    custom = CustomBooks(books.root, account_fingerprint=books.account_fingerprint)
    calls = count_reads(custom, monkeypatch)
    assert len(collect(executor(custom)._journals())) == 2
    assert len(calls) == 3


@pytest.mark.parametrize('damage', [
    'missing_inventory', 'digest', 'malformed', 'missing_later', 'orphan',
    'replaced_later', 'hardlink_later', 'symlink_later', 'sidecar_symlink_later',
    'directory_later', 'unexpected_wal',
])
def test_damage_between_yields_is_rejected_before_next_journal(tmp_path, damage):
    books, value = fixture(tmp_path, 2)
    sessions = list(value['sessions'])
    first_path = books.root / 'journals' / value['sessions'][sessions[0]]['filename']
    later_path = books.root / 'journals' / value['sessions'][sessions[1]]['filename']
    iterator = executor(books)._journals()
    first = next(iterator)
    if damage == 'missing_inventory':
        books.path.unlink()
    elif damage == 'digest':
        changed = json.loads(books.path.read_text())
        changed['sha256'] = '0' * 64
        books.path.write_text(json.dumps(changed))
    elif damage == 'malformed':
        books.path.write_bytes(b'{}')
    elif damage == 'orphan':
        (books.root / 'journals' / 'unregistered.sqlite3').write_bytes(b'preserve fault evidence')
    elif damage == 'unexpected_wal':
        Path(str(first_path) + '-wal').write_bytes(b'fault fixture')
    elif damage == 'sidecar_symlink_later':
        try:
            os.symlink(first_path, str(later_path) + '-journal')
        except OSError as exc:
            pytest.skip('Native symbolic-link creation unavailable: ' + str(exc))
    else:
        later_path.unlink()
        if damage == 'missing_later':
            pass
        elif damage == 'replaced_later':
            with ScoutJournal(later_path, binding=books._binding(sessions[1])):
                pass
        elif damage == 'directory_later':
            later_path.mkdir()
        elif damage in {'hardlink_later', 'symlink_later'}:
            try:
                (os.link if damage == 'hardlink_later' else os.symlink)(first_path, later_path)
            except OSError as exc:
                pytest.skip('Native file-link fixture unavailable: ' + str(exc))
    with pytest.raises(JournalError):
        next(iterator)
    assert first._closed


def test_unopened_reparse_metadata_is_not_hidden_by_unchanged_inventory(tmp_path, monkeypatch):
    books, value = fixture(tmp_path, 3)
    later = books.root / 'journals' / list(value['sessions'].values())[-1]['filename']
    iterator = executor(books)._journals()
    next(iterator)
    original = books_module.os.stat
    def stat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if os.fspath(path) == os.fspath(later):
            return SimpleNamespace(st_mode=info.st_mode, st_nlink=info.st_nlink,
                                   st_file_attributes=0x400)
        return info
    monkeypatch.setattr(books_module.os, 'stat', stat)
    with pytest.raises(JournalError, match='reparse'):
        next(iterator)


def test_changed_canonical_bytes_force_complete_validation(tmp_path, monkeypatch):
    books, _ = fixture(tmp_path, 2)
    reads = count_reads(books, monkeypatch)
    iterator = executor(books)._journals()
    next(iterator)
    books.path.write_text(json.dumps(json.loads(books.path.read_text()), indent=2))
    assert len(list(iterator)) == 1
    assert len(reads) == 2


def test_valid_append_is_validated_but_initial_session_list_stays_frozen(tmp_path, monkeypatch):
    books, value = fixture(tmp_path, 2)
    original_order = list(value['sessions'])
    reads = count_reads(books, monkeypatch)
    iterator = executor(books)._journals()
    first = next(iterator)
    add_journal(books, value, 'appended-later')
    write_inventory(books, value)
    remaining = [row[0] for row in collect(iterator)]
    assert [first.binding['session_id'], *remaining] == original_order
    assert len(reads) == 2


def test_valid_removal_of_a_selected_session_remains_unknown_session_error(tmp_path):
    books, value = fixture(tmp_path, 2)
    iterator = executor(books)._journals()
    next(iterator)
    session = list(value['sessions'])[-1]
    (books.root / 'journals' / value['sessions'][session]['filename']).unlink()
    del value['sessions'][session]
    write_inventory(books, value)
    with pytest.raises(JournalError, match='Unknown original manual session'):
        next(iterator)


def test_allowed_rollback_sidecar_does_not_become_unregistered_journal(tmp_path):
    books, value = fixture(tmp_path, 2)
    iterator = executor(books)._journals()
    first = next(iterator)
    Path(str(first.path) + '-journal').write_bytes(b'')
    assert len(list(iterator)) == 1


def test_each_opened_journal_still_audits_changed_full_chain(tmp_path):
    books, value = fixture(tmp_path, 3)
    iterator = executor(books)._journals()
    next(iterator)
    last = books.root / 'journals' / list(value['sessions'].values())[-1]['filename']
    with sqlite3.connect(last) as db:
        db.execute("UPDATE control SET sha256=? WHERE id=1", ('0' * 64,))
    next(iterator)  # An unrelated valid original journal keeps its original ordering.
    with pytest.raises(JournalError, match='Invalid stored canonical payload'):
        next(iterator)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('Inventory scan regressions do not call providers or brokers')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket.socket, 'connect_ex', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)


def test_absent_empty_books_scan_is_read_only(tmp_path):
    root = tmp_path / 'absent-books'
    books = ScoutBooks(root, account_fingerprint='a' * 64)
    assert collect(executor(books)._journals()) == []
    assert not root.exists()


def test_changed_books_account_identity_forces_original_full_validation(tmp_path, monkeypatch):
    books, _ = fixture(tmp_path, 2)
    reads = count_reads(books, monkeypatch)
    iterator = executor(books)._journals()
    next(iterator)
    books.account_fingerprint = 'b' * 64
    with pytest.raises(JournalError, match='inventory identity differs'):
        next(iterator)
    assert len(reads) == 2


def test_changed_current_binding_cannot_hide_behind_equal_inventory_bytes(tmp_path, monkeypatch):
    books, _ = fixture(tmp_path, 3)
    iterator = executor(books)._journals()
    next(iterator)
    original = books._binding
    def changed(session):
        return dict(original(session), participant_id='changed-native-owner')
    monkeypatch.setattr(books, '_binding', changed)
    with pytest.raises(JournalError, match='session binding differs'):
        next(iterator)
