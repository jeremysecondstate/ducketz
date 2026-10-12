"""Host-local, one-shot Scout submission journal; no broker or capacity authority.

The local NOT_SENT state proves only that this journal never issued a send
claim. It is not Atlas capacity-release evidence: admission may have succeeded
before a reply was lost. SENDING and UNKNOWN never become sendable again.

Every transition and its immutable outbox event commit together with SQLite
EXTRA synchronization. Hashes and replay detect accidental stored corruption;
they do not authenticate Atlas, defend against a malicious local administrator,
or make copying/deleting a journal safe. An outstanding journal must be restored,
never replaced with a fresh file. Forecast-level duplicate admission, current
authorization, freshness and broker recovery belong to later integrations.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import uuid

from .scout_protocol import Grant, Request, PARTICIPANT_ID, STREAM, canonical_sha256


VERSION = "scout-intraday-local-journal-v1"
OUTBOX_VERSION = "scout-intraday-local-outbox-v1"
_HASH = re.compile(r"[a-f0-9]{64}", re.ASCII)
_REQUIRED_BINDING = {"account_fingerprint", "session_id", "participant_id", "stream"}
_RECORD_KEYS = {"request", "request_sha256", "grant", "grant_sha256", "state",
                "send_token", "broker_order_id", "revoke_reason"}
_SCHEMA = {
    "control": "CREATE TABLE control (id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL, sha256 TEXT NOT NULL)",
    "records": "CREATE TABLE records (request_id TEXT PRIMARY KEY, payload TEXT NOT NULL, sha256 TEXT NOT NULL)",
    "events": "CREATE TABLE events (sequence INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, sha256 TEXT NOT NULL)",
    "acknowledgements": "CREATE TABLE acknowledgements (event_id TEXT PRIMARY KEY REFERENCES events(event_id), event_sha256 TEXT NOT NULL)",
}


class JournalError(ValueError):
    """The local journal operation cannot safely proceed."""


class JournalCorruptionError(JournalError):
    """Stored bytes, schema or transition history failed validation."""


class JournalStateError(JournalError):
    """This transition would violate immutable identity or single-send fencing."""


def _encoded(value):
    canonical_sha256(value)  # Apply the protocol's bounded canonical JSON rules.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def _checked_json(payload, digest):
    try:
        value = json.loads(payload)
        if _encoded(value) != payload or canonical_sha256(value) != digest:
            raise JournalCorruptionError("Stored payload hash or canonical bytes differ")
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise JournalCorruptionError("Invalid stored canonical payload") from exc


def _text(value, name):
    if type(value) is not str or not value.strip() or len(value) > 512 or any(ord(c) < 32 for c in value):
        raise JournalError(f"Invalid {name}")
    return value


def _hash(value):
    return type(value) is str and _HASH.fullmatch(value) is not None


def _safe_path(path):
    """Reject network and redirected paths before SQLite can create any files."""
    raw = os.fspath(path)
    if not isinstance(raw, str) or raw.startswith(("\\\\", "//")):
        raise JournalError("Journal must use an absolute host-local path")
    result = Path(raw)
    if not result.is_absolute() or ".." in result.parts or result.name in {"", ".", ".."}:
        raise JournalError("Journal must use an absolute host-local file path")
    if os.name == "nt":
        import ctypes
        if ctypes.windll.kernel32.GetDriveTypeW(str(result.anchor)) != 3:
            raise JournalError("Journal requires a fixed host-local drive")
        # Alternate streams are not standalone files and bypass normal sidecars.
        if any(":" in part for part in result.parts[1:]):
            raise JournalError("Journal cannot use an alternate data stream")
    candidates = [result, *result.parents]
    candidates.extend(Path(str(result) + suffix) for suffix in ("-journal", "-wal", "-shm"))
    for candidate in candidates:
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise JournalError("Journal and its ancestors/sidecars cannot be symlinks or reparse points")
        if candidate == result and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise JournalError("Journal must be a standalone regular file")
    return result


def _event_record(record):
    result = deepcopy(record)
    token = result.pop("send_token")
    result["send_token_sha256"] = hashlib.sha256(token.encode("ascii")).hexdigest() if token else None
    return result


class ScoutJournal:
    """An explicit file and immutable account/session/participant/stream binding.

    Additional string-valued binding fields (for example an authority identity)
    are preserved exactly. No connections remain open between method calls.
    A successful claim commits before returning its token; a crash before return
    still consumes that claim. Outbox envelopes never contain the send token.
    """

    def __init__(self, path: Path, *, binding: Mapping[str, str]):
        if (not isinstance(binding, Mapping) or not _REQUIRED_BINDING <= set(binding)
                or any(type(key) is not str or not key for key in binding)):
            raise JournalError("Explicit account/session/participant/stream binding required")
        self.binding = {key: _text(value, "binding value") for key, value in binding.items()}
        if (not _hash(self.binding["account_fingerprint"])
                or self.binding["participant_id"] != PARTICIPANT_ID or self.binding["stream"] != STREAM):
            raise JournalError("Journal binding requires Scout participant and stream")
        self.path = _safe_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _safe_path(self.path)
        self._closed = False
        self._journal_id = None
        # Only the exclusive creator may initialize. An existing empty/dropped
        # database may be a damaged outstanding journal and must never reset.
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            created = False
        else:
            os.close(descriptor)
            created = True
        with self._transaction(initialize=created) as db:
            control, _, _, _ = self._audit(db)
            self._journal_id = control["journal_id"]

    def __enter__(self):
        if self._closed:
            raise JournalError("Journal is closed")
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self._closed = True

    @contextmanager
    def _transaction(self, *, initialize=False):
        if self._closed:
            raise JournalError("Journal is closed")
        _safe_path(self.path)
        db = None
        try:
            # mode=rw cannot silently recreate a file lost after construction.
            db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True,
                                 timeout=30, isolation_level=None)
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA trusted_schema = OFF")
            db.execute("PRAGMA synchronous = EXTRA")
            if db.execute("PRAGMA synchronous").fetchone()[0] != 3:
                raise JournalError("SQLite EXTRA durability is required")
            if db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise JournalCorruptionError("Unexpected journal mode")
            db.execute("BEGIN IMMEDIATE")
            schema = list(db.execute("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL"))
            if not schema and initialize:
                for statement in _SCHEMA.values():
                    db.execute(statement)
                control = {"schema_version": VERSION, "binding": self.binding,
                           "journal_id": uuid.uuid4().hex, "last_sequence": 0, "last_sha256": None}
                db.execute("INSERT INTO control VALUES (1, ?, ?)",
                           (_encoded(control), canonical_sha256(control)))
            elif {name: sql for kind, name, sql in schema if kind == "table"} != _SCHEMA or any(kind != "table" for kind, _, _ in schema):
                raise JournalCorruptionError("Unrecognized journal schema; refusing to modify it")
            yield db
            db.commit()
        except sqlite3.Error as exc:
            if db is not None and db.in_transaction:
                db.rollback()
            raise JournalCorruptionError("SQLite journal operation failed") from exc
        except BaseException:
            if db is not None and db.in_transaction:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()

    def _validate_record(self, record, *, event=False):
        keys = _RECORD_KEYS - {"send_token"} | {"send_token_sha256"} if event else _RECORD_KEYS
        if type(record) is not dict or set(record) != keys:
            raise JournalCorruptionError("Invalid record fields")
        request = Request.from_payload(record["request"])
        if request.payload() != record["request"] or request.sha256 != record["request_sha256"]:
            raise JournalCorruptionError("Immutable request differs")
        if any(record["request"][key] != self.binding[key] for key in _REQUIRED_BINDING):
            raise JournalCorruptionError("Request differs from journal binding")
        grant = record["grant"]
        if grant is None:
            if record["grant_sha256"] is not None:
                raise JournalCorruptionError("Missing grant with stored hash")
        else:
            parsed = Grant.from_payload(grant).validate(request)
            if parsed.payload() != grant or parsed.sha256 != record["grant_sha256"]:
                raise JournalCorruptionError("Immutable grant differs")
        state = record["state"]
        token = record["send_token_sha256" if event else "send_token"]
        order_id, reason = record["broker_order_id"], record["revoke_reason"]
        if state not in {"PREPARED", "GRANTED", "SENDING", "UNKNOWN", "ACCEPTED", "NOT_SENT"}:
            raise JournalCorruptionError("Invalid submission state")
        if state == "PREPARED" and grant is not None or state in {"GRANTED", "SENDING", "UNKNOWN", "ACCEPTED"} and grant is None:
            raise JournalCorruptionError("State and grant disagree")
        if state in {"SENDING", "UNKNOWN", "ACCEPTED"}:
            if not _hash(token):
                raise JournalCorruptionError("Claimed state lacks a valid send token")
        elif token is not None:
            raise JournalCorruptionError("Unclaimed state contains a send token")
        if state == "ACCEPTED":
            _text(order_id, "broker order ID")
        elif order_id is not None:
            raise JournalCorruptionError("Nonaccepted state contains a broker ID")
        if state == "NOT_SENT":
            _text(reason, "revocation reason")
        elif reason is not None:
            raise JournalCorruptionError("Unexpected revocation reason")

    @staticmethod
    def _replay(previous, record, kind):
        if kind == "PREPARED":
            if previous is not None or record["state"] != "PREPARED":
                raise JournalCorruptionError("Request identity was reused")
            return
        allowed = {"GRANTED": {"PREPARED"}, "SENDING": {"GRANTED"},
                   "UNKNOWN": {"SENDING"}, "ACCEPTED": {"SENDING", "UNKNOWN"},
                   "NOT_SENT": {"PREPARED", "GRANTED"}}
        if kind not in allowed or previous is None or previous["state"] not in allowed[kind]:
            raise JournalCorruptionError("Invalid transition history")
        expected = deepcopy(previous)
        expected["state"] = kind
        changed = {"GRANTED": ("grant", "grant_sha256"), "SENDING": ("send_token_sha256",),
                   "UNKNOWN": (), "ACCEPTED": ("broker_order_id",), "NOT_SENT": ("revoke_reason",)}
        for key in changed[kind]:
            expected[key] = record[key]
        if expected != record:
            raise JournalCorruptionError("Transition changed immutable record fields")

    def _audit(self, db):
        try:
            rows = list(db.execute("SELECT id, payload, sha256 FROM control"))
            if len(rows) != 1 or rows[0][0] != 1:
                raise JournalCorruptionError("Missing journal identity")
            control = _checked_json(rows[0][1], rows[0][2])
            if (set(control) != {"schema_version", "binding", "journal_id", "last_sequence", "last_sha256"}
                    or control["schema_version"] != VERSION or control["binding"] != self.binding
                    or type(control["journal_id"]) is not str or not re.fullmatch(r"[a-f0-9]{32}", control["journal_id"])
                    or self._journal_id is not None and control["journal_id"] != self._journal_id
                    or type(control["last_sequence"]) is not int or control["last_sequence"] < 0):
                raise JournalCorruptionError("Journal version, identity or binding differs")
            replayed, events, prior, grants, reservations, tokens, receipts = {}, {}, None, {}, {}, set(), set()
            for expected_sequence, (sequence, event_id, raw, digest) in enumerate(db.execute(
                    "SELECT sequence, event_id, payload, sha256 FROM events ORDER BY sequence"), 1):
                event = _checked_json(raw, digest)
                if (set(event) != {"schema_version", "journal_id", "sequence", "event_id", "prior_sha256", "recorded_at", "kind", "request_id", "request_sha256", "record"}
                        or sequence != expected_sequence or type(event["sequence"]) is not int
                        or event["sequence"] != sequence
                        or event["schema_version"] != OUTBOX_VERSION or event["journal_id"] != control["journal_id"]
                        or event["event_id"] != event_id or event["prior_sha256"] != prior
                        or type(event_id) is not str or not re.fullmatch(r"[a-f0-9]{32}", event_id)):
                    raise JournalCorruptionError("Outbox chain differs")
                recorded_at = datetime.fromisoformat(event["recorded_at"])
                if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
                    raise JournalCorruptionError("Outbox timestamp lacks a timezone")
                record, rid = event["record"], event["request_id"]
                self._validate_record(record, event=True)
                if rid != record["request"]["request_id"] or event["request_sha256"] != record["request_sha256"]:
                    raise JournalCorruptionError("Outbox request differs")
                self._replay(replayed.get(rid), record, event["kind"])
                if event["kind"] == "GRANTED":
                    for identities, key in ((grants, "grant_id"), (reservations, "reservation_id")):
                        identity = record["grant"][key]
                        if identity in identities:
                            raise JournalCorruptionError("Authority grant or reservation identity reused")
                        identities[identity] = rid
                if event["kind"] == "SENDING":
                    if record["send_token_sha256"] in tokens:
                        raise JournalCorruptionError("Send token reused")
                    tokens.add(record["send_token_sha256"])
                if event["kind"] == "ACCEPTED":
                    if record["broker_order_id"] in receipts:
                        raise JournalCorruptionError("Broker receipt identity reused")
                    receipts.add(record["broker_order_id"])
                replayed[rid], prior = record, digest
                events[event_id] = {"event_id": event_id, "event_sha256": digest, "event": event}
            if control["last_sequence"] != len(events) or control["last_sha256"] != prior:
                raise JournalCorruptionError("Outbox history was lost or changed")
            records = {}
            for rid, raw, digest in db.execute("SELECT request_id, payload, sha256 FROM records"):
                record = _checked_json(raw, digest)
                self._validate_record(record)
                if rid != record["request"]["request_id"] or _event_record(record) != replayed.get(rid):
                    raise JournalCorruptionError("Current record differs from durable history")
                records[rid] = record
            if set(records) != set(replayed):
                raise JournalCorruptionError("Current records are missing")
            acked = set()
            for event_id, digest in db.execute("SELECT event_id, event_sha256 FROM acknowledgements"):
                if event_id not in events or events[event_id]["event_sha256"] != digest:
                    raise JournalCorruptionError("Acknowledgement differs from immutable event")
                acked.add(event_id)
            return control, records, events, acked
        except JournalCorruptionError:
            raise
        except (ValueError, TypeError, KeyError, OverflowError, UnicodeError) as exc:
            raise JournalCorruptionError("Journal audit failed") from exc

    def _append(self, db, control, record, kind):
        self._validate_record(record)
        rid = record["request"]["request_id"]
        event = {"schema_version": OUTBOX_VERSION, "journal_id": control["journal_id"],
                 "sequence": control["last_sequence"] + 1, "event_id": uuid.uuid4().hex,
                 "prior_sha256": control["last_sha256"],
                 "recorded_at": datetime.now(timezone.utc).isoformat(), "kind": kind,
                 "request_id": rid, "request_sha256": record["request_sha256"],
                 "record": _event_record(record)}
        digest = canonical_sha256(event)
        db.execute("INSERT INTO events VALUES (?, ?, ?, ?)",
                   (event["sequence"], event["event_id"], _encoded(event), digest))
        db.execute("INSERT OR REPLACE INTO records VALUES (?, ?, ?)",
                   (rid, _encoded(record), canonical_sha256(record)))
        control.update(last_sequence=event["sequence"], last_sha256=digest)
        db.execute("UPDATE control SET payload = ?, sha256 = ? WHERE id = 1",
                   (_encoded(control), canonical_sha256(control)))

    def get(self, request_id: str) -> dict | None:
        with self._transaction() as db:
            _, records, _, _ = self._audit(db)
            return deepcopy(records.get(request_id))

    def prepare(self, request: Request) -> dict:
        if type(request) is not Request:
            raise JournalError("Exact Request required")
        payload = request.payload()
        if any(payload[key] != self.binding[key] for key in _REQUIRED_BINDING):
            raise JournalStateError("Request differs from journal binding")
        with self._transaction() as db:
            control, records, _, _ = self._audit(db)
            if request.request_id in records:
                record = records[request.request_id]
                if record["request"] != payload:
                    raise JournalStateError("Request ID reused with changed request")
                return deepcopy(record)
            record = {"request": payload, "request_sha256": request.sha256, "grant": None,
                      "grant_sha256": None, "state": "PREPARED", "send_token": None,
                      "broker_order_id": None, "revoke_reason": None}
            self._append(db, control, record, "PREPARED")
            return deepcopy(record)

    def grant(self, grant: Grant) -> dict:
        if type(grant) is not Grant:
            raise JournalError("Exact Grant required")
        with self._transaction() as db:
            control, records, _, _ = self._audit(db)
            record = self._required(records, grant.request_id)
            grant.validate(Request.from_payload(record["request"]))
            if record["grant"] is not None:
                if record["grant"] != grant.payload():
                    raise JournalStateError("Grant is immutable")
                return deepcopy(record)
            if record["state"] != "PREPARED":
                raise JournalStateError("Only a prepared request can receive a grant")
            if any(other["grant"] is not None and (
                    other["grant"]["grant_id"] == grant.grant_id
                    or other["grant"]["reservation_id"] == grant.reservation_id) for other in records.values()):
                raise JournalStateError("Authority grant or reservation identity reused")
            record.update(grant=grant.payload(), grant_sha256=grant.sha256, state="GRANTED")
            self._append(db, control, record, "GRANTED")
            return deepcopy(record)

    @staticmethod
    def _required(records, request_id):
        if request_id not in records:
            raise JournalStateError("Unknown request ID")
        return records[request_id]

    def claim_send(self, request_id: str) -> str:
        """Consume one local send claim, including when its return value is lost."""
        with self._transaction() as db:
            control, records, _, _ = self._audit(db)
            record = self._required(records, request_id)
            if record["state"] != "GRANTED":
                raise JournalStateError("Send can only be claimed once from GRANTED")
            token = secrets.token_hex(32)
            if any(other["send_token"] == token for other in records.values()):
                raise JournalError("Send token collision")
            record.update(state="SENDING", send_token=token)
            self._append(db, control, record, "SENDING")
        return token

    def outcome(self, request_id: str, token: str, broker_order_id: str | None) -> dict:
        """Persist uncertainty or one exact receipt; never demote ACCEPTED."""
        if not _hash(token):
            raise JournalStateError("Exact send token required")
        if broker_order_id is not None:
            _text(broker_order_id, "broker order ID")
        with self._transaction() as db:
            control, records, _, _ = self._audit(db)
            record = self._required(records, request_id)
            if record["send_token"] != token or record["state"] not in {"SENDING", "UNKNOWN", "ACCEPTED"}:
                raise JournalStateError("Outcome differs from the one send claim")
            if record["state"] == "ACCEPTED":
                if broker_order_id is not None and record["broker_order_id"] != broker_order_id:
                    raise JournalStateError("Conflicting broker order ID")
                return deepcopy(record)
            state = "ACCEPTED" if broker_order_id is not None else "UNKNOWN"
            if record["state"] != state:
                if broker_order_id is not None and any(other["broker_order_id"] == broker_order_id
                                                       for other in records.values()):
                    raise JournalStateError("Broker receipt identity reused")
                record.update(state=state, broker_order_id=broker_order_id)
                self._append(db, control, record, state)
            return deepcopy(record)

    def revoke_unsent(self, request_id: str, reason: str) -> dict:
        """Fence an unclaimed local request; this never releases Atlas capacity."""
        _text(reason, "revocation reason")
        with self._transaction() as db:
            control, records, _, _ = self._audit(db)
            record = self._required(records, request_id)
            if record["state"] == "NOT_SENT" and record["revoke_reason"] == reason:
                return deepcopy(record)
            if record["state"] not in {"PREPARED", "GRANTED"}:
                raise JournalStateError("Only an unclaimed request can be revoked")
            record.update(state="NOT_SENT", revoke_reason=reason)
            self._append(db, control, record, "NOT_SENT")
            return deepcopy(record)

    def pending_events(self) -> list[dict]:
        with self._transaction() as db:
            _, _, events, acked = self._audit(db)
            return [deepcopy(event) for event_id, event in events.items() if event_id not in acked]

    def acknowledge(self, event_id: str, event_sha256: str) -> None:
        """Record only an acknowledgement bound to the exact immutable event."""
        with self._transaction() as db:
            _, _, events, acked = self._audit(db)
            if event_id not in events or events[event_id]["event_sha256"] != event_sha256:
                raise JournalStateError("Acknowledgement differs from exact event ID/hash")
            if event_id not in acked:
                db.execute("INSERT INTO acknowledgements VALUES (?, ?)", (event_id, event_sha256))
