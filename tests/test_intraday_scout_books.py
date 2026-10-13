import json
import os

import pytest

from ml.intraday.scout_books import ScoutBooks
from ml.intraday.scout_journal import JournalError
from ml.intraday.scout_protocol import canonical_sha256
from test_intraday_scout_client import request, grant


def test_read_only_inspection_does_not_create_books(tmp_path):
    root = tmp_path / 'books'
    assert ScoutBooks(root, account_fingerprint='a'*64).inspect()['sessions'] == {}
    assert not root.exists()


def test_new_session_preserves_old_claim_and_binding(tmp_path):
    books = ScoutBooks(tmp_path / 'books', account_fingerprint='a'*64)
    req = request()
    with books.create_session(req.session_id) as journal:
        journal.prepare(req)
        journal.grant(grant(req))
        token = journal.claim_send(req.request_id)
    with books.create_session('manual-scout:2') as journal:
        assert journal.binding['session_id'] == 'manual-scout:2'
    again = ScoutBooks(books.root, account_fingerprint='a'*64)
    assert set(again.sessions()) == {req.session_id, 'manual-scout:2'}
    with again.open_session(req.session_id) as journal:
        assert journal.get(req.request_id)['state'] == 'SENDING'
        assert journal.get(req.request_id)['send_token'] == token
        with pytest.raises(JournalError):
            journal.claim_send(req.request_id)
    with pytest.raises(JournalError):
        books.create_session(req.session_id)


@pytest.mark.parametrize('damage', ['missing_inventory', 'missing_journal', 'orphan', 'digest', 'account', 'binding', 'path'])
def test_missing_or_mutated_state_cannot_start_fresh(tmp_path, damage):
    books = ScoutBooks(tmp_path / 'books', account_fingerprint='a'*64)
    with books.create_session('manual-scout:1'):
        pass
    saved = json.loads(books.path.read_text())
    journal = next((books.root / 'journals').glob('*.sqlite3'))
    if damage == 'missing_inventory':
        books.path.unlink()
    elif damage == 'missing_journal':
        journal.unlink()
    elif damage == 'orphan':
        (journal.parent / 'unregistered.sqlite3').write_bytes(journal.read_bytes())
    else:
        if damage == 'digest':
            saved['sha256'] = 'f'*64
        elif damage == 'account':
            saved['inventory']['account_fingerprint'] = 'b'*64
        elif damage == 'binding':
            saved['inventory']['sessions']['manual-scout:1']['binding']['session_id'] = 'other'
        elif damage == 'path':
            saved['inventory']['sessions']['manual-scout:1']['filename'] = '../other.sqlite3'
        if damage != 'digest':
            saved['sha256'] = canonical_sha256(saved['inventory'])
        books.path.write_text(json.dumps(saved))
    with pytest.raises(JournalError):
        books.create_session('manual-scout:2')


def test_failed_inventory_commit_preserves_orphan_and_blocks_restart(tmp_path, monkeypatch):
    books = ScoutBooks(tmp_path / 'books', account_fingerprint='a'*64)
    def crash(*args):
        raise OSError('disk error')
    monkeypatch.setattr(os, 'replace', crash)
    with pytest.raises(OSError):
        books.create_session('manual-scout:1')
    assert len(list((books.root / 'journals').glob('*.sqlite3'))) == 1
    with pytest.raises(JournalError):
        books.inspect()


def test_wrong_account_cannot_open_previous_books(tmp_path):
    books = ScoutBooks(tmp_path / 'books', account_fingerprint='a'*64)
    with books.create_session('manual-scout:1'):
        pass
    with pytest.raises(JournalError):
        ScoutBooks(books.root, account_fingerprint='b'*64).open_session('manual-scout:1')


@pytest.mark.parametrize('session', ['', '../other', 'x'*129, None, 1, 'é'])
def test_invalid_session_never_creates_journal(tmp_path, session):
    books = ScoutBooks(tmp_path / 'books', account_fingerprint='a'*64)
    with pytest.raises(JournalError):
        books.create_session(session)
    assert not books.root.exists()


def test_receipt_persistence_is_exact_idempotent_and_has_no_journal_effect(tmp_path):
    books = ScoutBooks(tmp_path / 'books', account_fingerprint='a'*64)
    args = dict(kind='send-authorization', request_id='request:1', payload={'exact': 'evidence'})
    first = books.save_receipt(**args)
    assert books.save_receipt(**args) == first
    assert books.sessions() == ()
    assert not (books.root / 'journals').exists()
    saved = json.loads(open(first['path'], encoding='utf-8').read())
    assert saved['payload'] == args['payload']
    assert canonical_sha256(saved) == first['sha256']
    from pathlib import Path
    Path(first['path']).write_text('{}')
    with pytest.raises(JournalError):
        books.save_receipt(**args)


def test_same_binding_empty_replacement_cannot_erase_claims(tmp_path):
    books = ScoutBooks(tmp_path / 'books', account_fingerprint='a'*64)
    with books.create_session('manual-scout:1') as journal:
        original_path = journal.path
    from ml.intraday.scout_journal import ScoutJournal
    replacement_path = tmp_path / 'replacement.sqlite3'
    with ScoutJournal(replacement_path, binding=books.inspect()['sessions']['manual-scout:1']['binding']):
        pass
    original_path.write_bytes(replacement_path.read_bytes())
    with pytest.raises(JournalError):
        books.open_session('manual-scout:1')
    with pytest.raises(JournalError):
        books.create_session('manual-scout:2')


def test_corrupt_prior_journal_blocks_new_session(tmp_path):
    books = ScoutBooks(tmp_path / 'books', account_fingerprint='a'*64)
    with books.create_session('manual-scout:1') as journal:
        original_path = journal.path
    original_path.write_bytes(b'corrupt')
    with pytest.raises(JournalError):
        books.create_session('manual-scout:2')
    assert books.sessions() == ('manual-scout:1',)


def test_removal_between_inventory_and_reopen_never_creates(tmp_path, monkeypatch):
    books = ScoutBooks(tmp_path / 'books', account_fingerprint='a'*64)
    with books.create_session('manual-scout:1') as journal:
        original_path = journal.path
    old_read = books._read
    def remove_after_validation():
        result = old_read()
        original_path.unlink()
        return result
    monkeypatch.setattr(books, '_read', remove_after_validation)
    with pytest.raises(JournalError):
        books.open_session('manual-scout:1')
    assert not original_path.exists()


@pytest.mark.parametrize('residue', ['lost.sqlite3-journal', 'lost.sqlite3-wal', 'lost.sqlite3-shm', 'unexpected.db'])
def test_unregistered_residue_cannot_be_treated_as_empty_books(tmp_path, residue):
    books = ScoutBooks(tmp_path / 'books', account_fingerprint='a'*64)
    (books.root / 'journals').mkdir(parents=True)
    (books.root / 'journals' / residue).write_bytes(b'retained bytes')
    with pytest.raises(JournalError):
        books.create_session('manual-scout:1')
