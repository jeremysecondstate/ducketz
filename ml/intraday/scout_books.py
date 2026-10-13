"""Scout's local session-journal inventory; never a shared capacity authority.

The inventory prevents a new manual session from silently forgetting an older
session's one-use sends. Each journal retains its original account/session
binding. Missing, changed or unregistered files fail closed for explicit repair;
an outstanding journal is never replaced to make startup succeed.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import uuid

from datafetching.runtime_lock import exclusive_runtime_lock
from .scout_journal import ScoutJournal, JournalError, _safe_path
from .scout_protocol import PARTICIPANT_ID, STREAM, canonical_sha256

VERSION = 'scout-session-books-v1'


class ScoutBooks:
    def __init__(self, root, *, account_fingerprint):
        self.root = Path(root)
        self.path = _safe_path(self.root / 'inventory.json')
        if (type(account_fingerprint) is not str or len(account_fingerprint) != 64
                or any(c not in '0123456789abcdef' for c in account_fingerprint)):
            raise JournalError('Books require the reviewed account fingerprint')
        self.account_fingerprint = account_fingerprint

    def _empty(self):
        return dict(version=VERSION, account_fingerprint=self.account_fingerprint,
                    participant_id=PARTICIPANT_ID, stream=STREAM, sessions={})

    def _read(self):
        _safe_path(self.path)
        if not self.path.exists():
            journals = self.root / 'journals'
            if journals.exists() and any(journals.iterdir()):
                raise JournalError('Journal inventory missing; preserve existing books for repair')
            return self._empty()
        try:
            envelope = json.loads(self.path.read_text(encoding='utf-8'))
            if (set(envelope) != {'inventory', 'sha256'}
                    or canonical_sha256(envelope['inventory']) != envelope['sha256']):
                raise ValueError('inventory digest differs')
            value = envelope['inventory']
            expected = self._empty()
            if (set(value) != set(expected) or type(value['sessions']) is not dict
                    or any(value[k] != expected[k] for k in expected if k != 'sessions')):
                raise ValueError('inventory identity differs')
            paths = set()
            for session, item in value['sessions'].items():
                if (type(session) is not str or not session
                        or set(item) != {'filename', 'binding', 'journal_id'}
                        or type(item['journal_id']) is not str
                        or re.fullmatch(r'[a-f0-9]{32}', item['journal_id']) is None
                        or item['filename'] != canonical_sha256({'session_id': session}) + '.sqlite3'
                        or item['binding'] != self._binding(session)):
                    raise ValueError('session binding differs')
                journal_path = _safe_path(self.root / 'journals' / item['filename'])
                if not journal_path.is_file():
                    raise ValueError('registered journal is missing')
                paths.add(item['filename'])
            journals = self.root / 'journals'
            actual = {p.name for p in journals.iterdir()} if journals.exists() else set()
            allowed = paths | {name + '-journal' for name in paths}
            if not paths <= actual or actual - allowed:
                raise ValueError('unregistered journal requires explicit repair')
            return value
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise JournalError('Scout journal inventory failed validation: ' + str(exc)) from exc

    def _binding(self, session):
        return dict(account_fingerprint=self.account_fingerprint, session_id=session,
                    participant_id=PARTICIPANT_ID, stream=STREAM)

    def inspect(self):
        """Read inventory only; never create a journal, directory or store."""
        return self._read()

    @contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        _safe_path(self.path)
        with exclusive_runtime_lock(self.root / 'inventory.lock', process_name='scout-journal-inventory'):
            yield

    def create_session(self, session_id):
        """Register exactly one new bound journal under the host's process lock."""
        if type(session_id) is not str or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', session_id, re.ASCII) is None:
            raise JournalError('Manual session identity must fit the immutable request protocol')
        binding = self._binding(session_id)
        with self.locked():
            value = self._read()
            if session_id in value['sessions']:
                raise JournalError('Manual session identity already registered; reopen its original journal')
            for item in value['sessions'].values():
                # Audit original journals before adding another session. Opening
                # must never recreate a missing file or accept a replacement.
                with self._open_item(item):
                    pass
            filename = canonical_sha256({'session_id': session_id}) + '.sqlite3'
            journal = ScoutJournal(self.root / 'journals' / filename, binding=binding)
            try:
                value['sessions'][session_id] = dict(filename=filename, binding=binding,
                                                    journal_id=journal.journal_id)
                envelope = dict(inventory=value, sha256=canonical_sha256(value))
                temporary = self.path.with_name('.inventory-' + uuid.uuid4().hex + '.tmp')
                raw = json.dumps(envelope, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
                with temporary.open('xb') as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            except BaseException:
                journal.close()
                # The orphan remains visible and blocks subsequent startup.
                raise
            return journal

    def open_session(self, session_id):
        value = self._read()
        try:
            item = value['sessions'][session_id]
        except KeyError as exc:
            raise JournalError('Unknown original manual session') from exc
        return self._open_item(item)

    def _open_item(self, item):
        return ScoutJournal(self.root / 'journals' / item['filename'], binding=item['binding'],
                            create=False, expected_journal_id=item['journal_id'])

    def sessions(self):
        return tuple(self._read()['sessions'])

    def save_receipt(self, *, kind, request_id, payload):
        """Persist exact already-validated service evidence before an effect.

        This is private evidence storage, not validation or an authorization
        decision. The transport/executor must authenticate and bind the payload
        first. Merely storing an ACK cannot release or rearm any order.
        """
        if (type(kind) is not str or not kind or type(request_id) is not str
                or not request_id or type(payload) is not dict):
            raise JournalError('Receipt requires explicit kind/request and exact object payload')
        receipt = dict(version=VERSION, account_fingerprint=self.account_fingerprint,
                       participant_id=PARTICIPANT_ID, stream=STREAM, kind=kind,
                       request_id=request_id, payload=payload)
        sha = canonical_sha256(receipt)
        raw = json.dumps(receipt, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
        destination = _safe_path(self.root / 'receipts' / (sha + '.json'))
        with self.locked():
            self._read()
            destination.parent.mkdir(exist_ok=True)
            _safe_path(destination)
            if destination.exists():
                if destination.read_bytes() != raw:
                    raise JournalError('Existing immutable authority receipt is corrupted')
            else:
                temporary = destination.with_name('.receipt-' + uuid.uuid4().hex + '.tmp')
                with temporary.open('xb') as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
        return dict(sha256=sha, path=str(destination))
