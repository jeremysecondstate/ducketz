"""Host-local, one-shot Scout submission and private lifecycle journal.

The local NOT_SENT state proves only that this journal never issued a send
claim. SENDING and UNKNOWN never become sendable again. Request, Grant and
their historical schema names remain immutable local record identities;
they establish no peer approval or shared account-capacity dependency.

Every transition and immutable outbox event commit together with SQLite EXTRA
synchronization. Hashes and replay detect accidental stored corruption; they
do not authenticate a broker or make copying/deleting a journal safe. Restore
an outstanding journal rather than replacing it. The private lifecycle log
retains exact order/fill evidence and cancellation progress. Actual capacity,
current manual authority, freshness and broker effects remain caller-owned.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
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

from .scout_protocol import Grant, Request, PARTICIPANT_ID, STREAM, canonical_sha256, _canonical_bytes
from . import scout_protocol as _scout_protocol
from .dataset_diagnostics import recovery_phase


LEGACY_VERSION = "scout-intraday-local-journal-v1"
VERSION = "scout-intraday-local-journal-v2"
LIFECYCLE_VERSION = "scout-intraday-private-lifecycle-v1"
OUTBOX_VERSION = "scout-intraday-local-outbox-v1"
_HASH = re.compile(r"[a-f0-9]{64}", re.ASCII)
_REQUIRED_BINDING = {"account_fingerprint", "session_id", "participant_id", "stream"}
_RECORD_KEYS = {"request", "request_sha256", "grant", "grant_sha256", "state",
                "send_token", "broker_order_id", "revoke_reason"}
_SUBMISSION_SCHEMA = {
    "control": "CREATE TABLE control (id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL, sha256 TEXT NOT NULL)",
    "records": "CREATE TABLE records (request_id TEXT PRIMARY KEY, payload TEXT NOT NULL, sha256 TEXT NOT NULL)",
    "events": "CREATE TABLE events (sequence INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, sha256 TEXT NOT NULL)",
    "acknowledgements": "CREATE TABLE acknowledgements (event_id TEXT PRIMARY KEY REFERENCES events(event_id), event_sha256 TEXT NOT NULL)",
}
_LIFECYCLE_SCHEMA = {
    "lifecycle_control": "CREATE TABLE lifecycle_control (id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL, sha256 TEXT NOT NULL)",
    "lifecycle_events": "CREATE TABLE lifecycle_events (sequence INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, sha256 TEXT NOT NULL)",
    "lifecycle_records": "CREATE TABLE lifecycle_records (request_id TEXT PRIMARY KEY REFERENCES records(request_id), payload TEXT NOT NULL, sha256 TEXT NOT NULL)",
    "lifecycle_evidence_ids": "CREATE TABLE lifecycle_evidence_ids (evidence_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES records(request_id), evidence_sha256 TEXT NOT NULL)",
}
_SCHEMA = _SUBMISSION_SCHEMA | _LIFECYCLE_SCHEMA
_TERMINAL = frozenset({"FILLED", "CANCELLED", "REJECTED"})


class JournalError(ValueError):
    """The local journal operation cannot safely proceed."""


class JournalCorruptionError(JournalError):
    """Stored bytes, schema or transition history failed validation."""


class JournalStateError(JournalError):
    """This transition would violate immutable identity or single-send fencing."""


def _encoded(value):
    plain_mappings = [True]
    raw = _canonical_bytes(value, _plain_mappings=plain_mappings)
    if plain_mappings[0]:
        return raw.decode("utf-8")
    # Custom mappings retain their original serializer and override behavior.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def _checked_json(payload, digest):
    try:
        value = json.loads(payload)
        encoded = _encoded(value)
        if encoded != payload or hashlib.sha256(encoded.encode("utf-8")).hexdigest() != digest:
            raise JournalCorruptionError("Stored payload hash or canonical bytes differ")
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise JournalCorruptionError("Invalid stored canonical payload") from exc


def _record_validation_key(payload, digest):
    """Key only exact flat DTO values; never coerce bool/float/custom values."""
    if (type(payload) is not dict or not _hash(digest)
            or any(type(key) is not str or type(value) not in (str, int)
                   for key, value in payload.items())):
        return None
    return (tuple((key, type(value), value) for key, value in sorted(payload.items())), digest)


def _record_validation_contract():
    # An override retains its original validation calls, including changes made
    # between rows of the same audit. No custom DTO behavior is memoized.
    try:
        return (Request, Grant,
                *(vars(kind).get(name) for kind in (Request, Grant)
                  for name in ("__init__", "__post_init__", "payload", "from_payload", "sha256", "validate", "__eq__", "__hash__")),
                *(_scout_protocol.__dict__.get(name) for name in
                  ("_payload", "_from_payload", "_json_value", "canonical_sha256",
                   "_string", "_positive_integer", "_timestamp", "Request", "Grant",
                   "SCHEMA_VERSION", "PARTICIPANT_ID", "STREAM", "MAX_INTEGER",
                   "MAX_DECIMAL_LENGTH", "MAX_DECIMAL_SCALE", "_ID", "_SYMBOL",
                   "_HASH", "_DECIMAL", "_TIME", "fields", "asdict", "datetime",
                   "timezone", "Mapping", "ProtocolError")),
                _scout_protocol.hashlib.sha256, _scout_protocol.json.dumps,
                _scout_protocol.math.isfinite, _hash)
    except (TypeError, AttributeError):
        return None


def _native_record_validation():
    current = _record_validation_contract()
    return (current is not None and len(current) == len(_NATIVE_DTO_VALIDATION)
            and all(actual is native for actual, native in zip(current, _NATIVE_DTO_VALIDATION)))


def _text(value, name):
    if type(value) is not str or not value.strip() or len(value) > 512 or any(ord(c) < 32 for c in value):
        raise JournalError(f"Invalid {name}")
    return value


def _hash(value):
    return type(value) is str and _HASH.fullmatch(value) is not None


def _safe_path(path):
    """Reject network and redirected paths before SQLite can create any files."""
    with recovery_phase('journal_safe_path'):
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


_NATIVE_EVENT_RECORD = _event_record


def _audit_event_record(record):
    """Compare checked native JSON without exposing a shallow record copy.

    The historical helper still returns detached values for appends and custom
    inputs. This temporary audit view only replaces the outer token field; its
    nested DTO values are neither changed nor returned to callers.
    """
    if (_event_record is not _NATIVE_EVENT_RECORD or type(record) is not dict
            or set(record) != _RECORD_KEYS):
        return _event_record(record)
    for key, value in record.items():
        if type(key) is not str:
            return _event_record(record)
        if key in {"request", "grant"}:
            if value is None and key == "grant":
                continue
            if (type(value) is not dict
                    or any(type(name) is not str or type(item) not in (str, int)
                           for name, item in value.items())):
                return _event_record(record)
        elif value is not None and type(value) is not str:
            return _event_record(record)
    result = dict(record)
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

    def __init__(self, path: Path, *, binding: Mapping[str, str], migrate_legacy: bool = False,
                 create: bool = True, expected_journal_id: str | None = None):
        if (not isinstance(binding, Mapping) or not _REQUIRED_BINDING <= set(binding)
                or any(type(key) is not str or not key for key in binding)):
            raise JournalError("Explicit account/session/participant/stream binding required")
        self.binding = {key: _text(value, "binding value") for key, value in binding.items()}
        if (not _hash(self.binding["account_fingerprint"])
                or self.binding["participant_id"] != PARTICIPANT_ID or self.binding["stream"] != STREAM):
            raise JournalError("Journal binding requires Scout participant and stream")
        self.path = _safe_path(path)
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        _safe_path(self.path)
        self._closed = False
        self._journal_id = None
        # Only the exclusive creator may initialize. An existing empty/dropped
        # database may be a damaged outstanding journal and must never reset.
        created = False
        if create:
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
                created = True
        with self._transaction(initialize=created, migrate_legacy=migrate_legacy) as db:
            control, _, _, _ = self._audit(db)
            if expected_journal_id is not None and control["journal_id"] != expected_journal_id:
                raise JournalCorruptionError("Journal differs from the expected original identity")
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
    def _transaction(self, *, initialize=False, migrate_legacy=False):
        if self._closed:
            raise JournalError("Journal is closed")
        _safe_path(self.path)
        db = None
        try:
            # mode=rw cannot silently recreate a file lost after construction.
            with recovery_phase('journal_connect', journal=self):
                db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True,
                                     timeout=30, isolation_level=None)
            with recovery_phase('journal_connection_setup', journal=self):
                db.execute("PRAGMA foreign_keys = ON")
                db.execute("PRAGMA trusted_schema = OFF")
                db.execute("PRAGMA synchronous = EXTRA")
                if db.execute("PRAGMA synchronous").fetchone()[0] != 3:
                    raise JournalError("SQLite EXTRA durability is required")
                if db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                    raise JournalCorruptionError("Unexpected journal mode")
            with recovery_phase('journal_begin_immediate', journal=self):
                db.execute("BEGIN IMMEDIATE")
            with recovery_phase('journal_schema_validation', journal=self):
                schema = list(db.execute("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL"))
                if not schema and initialize:
                    for statement in _SCHEMA.values():
                        db.execute(statement)
                    control = {"schema_version": VERSION, "binding": self.binding,
                               "journal_id": uuid.uuid4().hex, "last_sequence": 0, "last_sha256": None}
                    db.execute("INSERT INTO control VALUES (1, ?, ?)",
                               (_encoded(control), canonical_sha256(control)))
                    self._initialize_lifecycle(db, control["journal_id"])
                elif (migrate_legacy and {name: sql for kind, name, sql in schema if kind == "table"} == _SUBMISSION_SCHEMA
                      and all(kind == "table" for kind, _, _ in schema)):
                    # Audit the complete old journal before adding anything. Its
                    # event/outbox bytes and one-shot claims remain unchanged.
                    control, _, _, _ = self._audit(db, legacy=True)
                    for statement in _LIFECYCLE_SCHEMA.values():
                        db.execute(statement)
                    self._initialize_lifecycle(db, control["journal_id"])
                    control["schema_version"] = VERSION
                    db.execute("UPDATE control SET payload=?, sha256=? WHERE id=1",
                               (_encoded(control), canonical_sha256(control)))
                elif {name: sql for kind, name, sql in schema if kind == "table"} != _SCHEMA or any(kind != "table" for kind, _, _ in schema):
                    raise JournalCorruptionError("Unrecognized journal schema; refusing to modify it")
            yield db
            with recovery_phase('journal_commit', journal=self):
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
                with recovery_phase('journal_close', journal=self):
                    db.close()

    def _validate_record(self, record, *, event=False, _validation_memo=None):
        keys = _RECORD_KEYS - {"send_token"} | {"send_token_sha256"} if event else _RECORD_KEYS
        if type(record) is not dict or set(record) != keys:
            raise JournalCorruptionError("Invalid record fields")
        memo = (_validation_memo if _validation_memo is not None
                and _native_record_validation() else None)
        request_key = _record_validation_key(record["request"], record["request_sha256"]) if memo is not None else None
        request = memo[0].get(request_key) if request_key is not None else None
        if request is None:
            request = Request.from_payload(record["request"])
            if request.payload() != record["request"] or request.sha256 != record["request_sha256"]:
                raise JournalCorruptionError("Immutable request differs")
            if request_key is not None:
                memo[0][request_key] = request
        if any(record["request"][key] != self.binding[key] for key in _REQUIRED_BINDING):
            raise JournalCorruptionError("Request differs from journal binding")
        grant = record["grant"]
        if grant is None:
            if record["grant_sha256"] is not None:
                raise JournalCorruptionError("Missing grant with stored hash")
        else:
            grant_key = _record_validation_key(grant, record["grant_sha256"]) if memo is not None else None
            if grant_key is not None:
                grant_key = (grant_key, request)
            if grant_key is None or grant_key not in memo[1]:
                parsed = Grant.from_payload(grant).validate(request)
                if parsed.payload() != grant or parsed.sha256 != record["grant_sha256"]:
                    raise JournalCorruptionError("Immutable grant differs")
                if grant_key is not None:
                    memo[1][grant_key] = True
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
        expected = dict(previous)
        expected["state"] = kind
        changed = {"GRANTED": ("grant", "grant_sha256"), "SENDING": ("send_token_sha256",),
                   "UNKNOWN": (), "ACCEPTED": ("broker_order_id",), "NOT_SENT": ("revoke_reason",)}
        for key in changed[kind]:
            expected[key] = record[key]
        if expected != record:
            raise JournalCorruptionError("Transition changed immutable record fields")

    def _audit(self, db, *, legacy=False, include_lifecycle=False):
        with recovery_phase('journal_full_audit', journal=self):
            # Local to this exact audit, including concurrent/reentrant calls.
            # Every stored row still gets its fresh canonical/hash check and
            # complete replay. No state survives the transaction or broker IO.
            validation_memo = ({}, {})

            def validate(record, *, event=False):
                validator = self._validate_record
                if getattr(validator, "__func__", None) is _NATIVE_VALIDATE_RECORD:
                    validator(record, event=event, _validation_memo=validation_memo)
                else:
                    validator(record, event=event)

            try:
                rows = list(db.execute("SELECT id, payload, sha256 FROM control"))
                if len(rows) != 1 or rows[0][0] != 1:
                    raise JournalCorruptionError("Missing journal identity")
                control = _checked_json(rows[0][1], rows[0][2])
                if (set(control) != {"schema_version", "binding", "journal_id", "last_sequence", "last_sha256"}
                        or control["schema_version"] != (LEGACY_VERSION if legacy else VERSION) or control["binding"] != self.binding
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
                    validate(record, event=True)
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
                    validate(record)
                    if rid != record["request"]["request_id"] or _audit_event_record(record) != replayed.get(rid):
                        raise JournalCorruptionError("Current record differs from durable history")
                    records[rid] = record
                if set(records) != set(replayed):
                    raise JournalCorruptionError("Current records are missing")
                acked = set()
                for event_id, digest in db.execute("SELECT event_id, event_sha256 FROM acknowledgements"):
                    if event_id not in events or events[event_id]["event_sha256"] != digest:
                        raise JournalCorruptionError("Acknowledgement differs from immutable event")
                    acked.add(event_id)
                lifecycle = self._audit_lifecycle(db, records) if not legacy else None
                if include_lifecycle:
                    return control, records, events, acked, lifecycle
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
        """Fence an unclaimed local request; this changes no broker capacity."""
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

    @staticmethod
    def _initialize_lifecycle(db, journal_id):
        control = {"schema_version": LIFECYCLE_VERSION, "journal_id": journal_id,
                   "last_sequence": 0, "last_sha256": None, "evidence_count": 0}
        db.execute("INSERT INTO lifecycle_control VALUES (1,?,?)",
                   (_encoded(control), canonical_sha256(control)))

    @staticmethod
    def _empty_lifecycle():
        return {"evidence": None, "cancellation": None, "cancellation_events": []}

    @staticmethod
    def _economic_evidence(payload):
        return {key: value for key, value in payload.items() if key not in {"evidence_id", "observed_at"}}

    @staticmethod
    def _validate_order_payload(payload, record, previous=None):
        # Reuse native evidence numeric/time/provenance semantics without
        # constructing a native ledger or creating local capacity ownership.
        from ml.stock_trader.horizon_ledger import (
            FillEvidence, OrderEvidence, _number, _utc, validate_fill_time_provenance,
        )
        if record["state"] != "ACCEPTED":
            raise JournalStateError("Order evidence requires an exact accepted journal identity")
        try:
            value = OrderEvidence(**{**payload, "fills": tuple(FillEvidence(**item) for item in payload["fills"])})
            request, grant = record["request"], record["grant"]
            if (value.reservation_id != grant["reservation_id"] or value.broker_order_id != record["broker_order_id"]
                    or value.account_fingerprint != request["account_fingerprint"]
                    or value.order_quantity != grant["quantity"]):
                raise JournalStateError("Order evidence differs from accepted account/order/grant")
            _text(value.evidence_id, "order evidence ID")
            observed = _utc(value.observed_at)
            if observed < _utc(request["issued_at"]):
                raise JournalStateError("Order evidence precedes the immutable request")
            total, filled, remaining = value.order_quantity, value.cumulative_filled_quantity, value.remaining_quantity
            if (any(type(number) is not int for number in (total, filled, remaining))
                    or not 0 <= filled <= total or value.status not in _TERMINAL | {"WORKING", "PARTIAL"}
                    or remaining != (0 if value.status in _TERMINAL else total - filled)
                    or value.status == "FILLED" and filled != total
                    or value.status == "REJECTED" and filled
                    or value.status == "WORKING" and filled
                    or value.status == "PARTIAL" and not 0 < filled < total):
                raise JournalStateError("Complete cumulative order quantities required")
            fills = {}
            _, whole_second_ids = validate_fill_time_provenance(value.fill_time_provenance, value.fills)
            issued = datetime.fromisoformat(_utc(request["issued_at"]))
            for fill in value.fills:
                _text(fill.fill_id, "fill ID")
                executed = datetime.fromisoformat(_utc(fill.executed_at))
                price, limit = _number(fill.price, positive=True), _number(request["limit_price"], positive=True)
                lower_bound_valid = issued <= executed or (
                    fill.fill_id in whole_second_ids and executed == issued.replace(microsecond=0))
                if (fill.fill_id in fills or type(fill.quantity) is not int or fill.quantity <= 0
                        or _utc(fill.executed_at) > observed or not lower_bound_valid
                        or request["side"] == "BUY" and price > limit
                        or request["side"] == "SELL" and price < limit):
                    raise JournalStateError("Complete unique fill evidence required")
                fills[fill.fill_id] = asdict(fill)
            if sum(fill.quantity for fill in value.fills) != filled:
                raise JournalStateError("Complete per-order fills required")
            if previous is not None:
                if (observed < _utc(previous["observed_at"])
                        or filled < previous["cumulative_filled_quantity"]
                        or previous["status"] in _TERMINAL and (
                            value.status != previous["status"] or filled != previous["cumulative_filled_quantity"])
                        or observed == _utc(previous["observed_at"]) and
                            ScoutJournal._economic_evidence(payload) != ScoutJournal._economic_evidence(previous)):
                    raise JournalStateError("Order status, evidence time or cumulative fill regressed")
                for old in previous["fills"]:
                    if fills.get(old["fill_id"]) != old:
                        raise JournalStateError("Previous immutable fill missing or changed")
        except (TypeError, KeyError, ValueError) as exc:
            if isinstance(exc, JournalStateError):
                raise
            raise JournalStateError("Invalid complete order evidence") from exc

    def _lifecycle_transition(self, state, kind, payload, record):
        from ml.stock_trader.horizon_ledger import _utc
        result = deepcopy(state)
        if kind == "ORDER_EVIDENCE":
            self._validate_order_payload(payload, record, state["evidence"])
            if (state["cancellation"] is not None
                    and _utc(payload["observed_at"]) < _utc(state["cancellation"]["requested_at"])):
                raise JournalStateError("Order evidence predates the persisted cancellation request")
            result["evidence"] = deepcopy(payload)
        elif kind == "CANCEL_REQUESTED":
            if (set(payload) != {"reservation_id", "broker_order_id", "requested_at", "stream", "reason", "evidence_id"}
                    or state["cancellation"] is not None or record["state"] != "ACCEPTED"
                    or payload["reservation_id"] != record["grant"]["reservation_id"]
                    or payload["broker_order_id"] != record["broker_order_id"]
                    or payload["stream"] != STREAM or payload["reason"] not in {"new-forecast", "repricing", "manual-stop"}
                    or state["evidence"] is None or state["evidence"]["status"] not in {"WORKING", "PARTIAL"}
                    or _utc(payload["requested_at"]) < _utc(state["evidence"]["observed_at"])):
                raise JournalStateError("Cancellation requires the exact working order and stable intent")
            _text(payload["evidence_id"], "cancellation evidence ID")
            result["cancellation"] = deepcopy(payload)
        elif kind == "CANCEL_EVENT":
            if (set(payload) != {"kind", "payload"} or state["cancellation"] is None
                    or payload["kind"] not in {"cancellation_acknowledged", "cancellation_temporary_failure",
                                               "cancellation_reconciliation_required", "cancellation_send_claimed"}):
                raise JournalStateError("A persisted cancellation intent and recognized attempt event required")
            detail = payload["payload"]
            if (detail.get("reservation_id") != record["grant"]["reservation_id"] or detail.get("stream") != STREAM
                    or _utc(detail.get("observed_at")) < _utc(state["cancellation"]["requested_at"])
                    or payload["kind"] == "cancellation_acknowledged" and detail.get("acknowledgment_is_terminal") is not False):
                raise JournalStateError("Cancellation event differs from its exact private intent")
            if payload["kind"] == "cancellation_send_claimed":
                if (set(detail) != {"reservation_id", "observed_at", "stream", "attempt_id"}
                        or state["evidence"] is None or state["evidence"]["status"] not in {"WORKING", "PARTIAL"}):
                    raise JournalStateError("Cancellation send claim requires an exact working order and attempt identity")
                _text(detail["attempt_id"], "cancellation send attempt ID")
            result["cancellation_events"].append(deepcopy(payload))
        else:
            raise JournalStateError("Unknown private lifecycle event")
        return result

    def _audit_lifecycle(self, db, records):
        try:
            rows = list(db.execute("SELECT id,payload,sha256 FROM lifecycle_control"))
            if len(rows) != 1 or rows[0][0] != 1:
                raise JournalCorruptionError("Missing private lifecycle control")
            control = _checked_json(rows[0][1], rows[0][2])
            journal_control = _checked_json(*db.execute("SELECT payload,sha256 FROM control WHERE id=1").fetchone())
            if (set(control) != {"schema_version", "journal_id", "last_sequence", "last_sha256", "evidence_count"}
                    or control["schema_version"] != LIFECYCLE_VERSION
                    or control["journal_id"] != journal_control["journal_id"]
                    or type(control["last_sequence"]) is not int or control["last_sequence"] < 0
                    or type(control["evidence_count"]) is not int
                    or control["evidence_count"] != db.execute("SELECT COUNT(*) FROM lifecycle_evidence_ids").fetchone()[0]):
                raise JournalCorruptionError("Private lifecycle identity differs")
            states, events, prior, cancel_attempts = {}, {}, None, set()
            for number, (sequence, event_id, raw, digest) in enumerate(db.execute(
                    "SELECT sequence,event_id,payload,sha256 FROM lifecycle_events ORDER BY sequence"), 1):
                event = _checked_json(raw, digest)
                if (set(event) != {"sequence", "event_id", "prior_sha256", "request_id", "kind", "payload"}
                        or sequence != number or event["sequence"] != number or event["event_id"] != event_id
                        or event["prior_sha256"] != prior or event["request_id"] not in records):
                    raise JournalCorruptionError("Private lifecycle chain differs")
                rid = event["request_id"]
                states[rid] = self._lifecycle_transition(states.get(rid, self._empty_lifecycle()),
                    event["kind"], event["payload"], records[rid])
                if event["kind"] == "CANCEL_EVENT" and event["payload"]["kind"] == "cancellation_send_claimed":
                    attempt_id = event["payload"]["payload"]["attempt_id"]
                    if attempt_id in cancel_attempts:
                        raise JournalCorruptionError("Cancellation send attempt ID reused")
                    cancel_attempts.add(attempt_id)
                events[event_id], prior = event, digest
            if control["last_sequence"] != len(events) or control["last_sha256"] != prior:
                raise JournalCorruptionError("Private lifecycle history lost or changed")
            materialized = {}
            for rid, raw, digest in db.execute("SELECT request_id,payload,sha256 FROM lifecycle_records"):
                state = _checked_json(raw, digest)
                expected = deepcopy(states.get(rid))
                if expected is None or set(state) != set(expected):
                    raise JournalCorruptionError("Unexpected private lifecycle state")
                if state["evidence"] is not None:
                    self._validate_order_payload(state["evidence"], records[rid], expected["evidence"])
                    if self._economic_evidence(state["evidence"]) != self._economic_evidence(expected["evidence"]):
                        raise JournalCorruptionError("Unrecorded order economics changed")
                    identity = db.execute("SELECT request_id,evidence_sha256 FROM lifecycle_evidence_ids WHERE evidence_id=?",
                                          (state["evidence"]["evidence_id"],)).fetchone()
                    if identity != (rid, canonical_sha256(state["evidence"])):
                        raise JournalCorruptionError("Current order evidence identity differs")
                    expected["evidence"] = state["evidence"]
                if state != expected:
                    raise JournalCorruptionError("Private lifecycle state differs from replay")
                materialized[rid] = state
            if set(materialized) != set(states):
                raise JournalCorruptionError("Private lifecycle state is missing")
            return control, materialized, events
        except (ValueError, KeyError, TypeError) as exc:
            if isinstance(exc, JournalCorruptionError):
                raise
            raise JournalCorruptionError("Private lifecycle audit failed") from exc

    def _append_lifecycle(self, db, control, request_id, kind, payload, state, event_id):
        event = {"sequence": control["last_sequence"] + 1, "event_id": event_id,
                 "prior_sha256": control["last_sha256"], "request_id": request_id,
                 "kind": kind, "payload": payload}
        digest = canonical_sha256(event)
        db.execute("INSERT INTO lifecycle_events VALUES (?,?,?,?)",
                   (event["sequence"], event_id, _encoded(event), digest))
        self._save_lifecycle_state(db, request_id, state)
        control.update(last_sequence=event["sequence"], last_sha256=digest)
        db.execute("UPDATE lifecycle_control SET payload=?,sha256=? WHERE id=1",
                   (_encoded(control), canonical_sha256(control)))

    @staticmethod
    def _save_lifecycle_state(db, request_id, state):
        db.execute("INSERT OR REPLACE INTO lifecycle_records VALUES (?,?,?)",
                   (request_id, _encoded(state), canonical_sha256(state)))

    @property
    def journal_id(self):
        return self._journal_id

    @property
    def account_fingerprint(self):
        return self.binding["account_fingerprint"]

    def records(self):
        """Exact private submissions, including unresolved unaccepted send claims."""
        with recovery_phase('journal_records', journal=self):
            with self._transaction() as db:
                _, records, _, _ = self._audit(db)
                return tuple(deepcopy(record) for record in records.values())

    def get_with_order_state(self, request_id):
        """Read one submission and lifecycle from the same complete audit.

        The original transaction, identity/schema checks and full replay remain
        mandatory. Returned values share no mutable state with the journal, and
        the transaction closes before the caller performs broker work.
        """
        with self._transaction() as db:
            _, records, _, _, (_, states, _) = self._audit(db, include_lifecycle=True)
            record = self._required(records, request_id)
            return deepcopy(record), deepcopy(states.get(request_id, self._empty_lifecycle()))

    def order_state(self, request_id):
        with self._transaction() as db:
            _, records, _, _, (_, states, _) = self._audit(db, include_lifecycle=True)
            self._required(records, request_id)
            return deepcopy(states.get(request_id, self._empty_lifecycle()))

    def observe_order(self, request_id, evidence, *, as_of=None):
        """Persist exact monotonic fills without changing broker capacity.

        Unchanged polls update only the latest checked receipt and an indexed
        immutable ID/hash entry. They do not grow the replay event chain.
        """
        from ml.stock_trader.horizon_ledger import OrderEvidence, _utc
        if type(evidence) is not OrderEvidence:
            raise JournalStateError("Exact native OrderEvidence required")
        payload = asdict(evidence)
        payload["fills"] = list(payload["fills"])
        payload = json.loads(_encoded(payload))
        if _utc(payload["observed_at"]) > _utc(as_of or datetime.now(timezone.utc)):
            raise JournalStateError("Order observation is in the future")
        digest = canonical_sha256(payload)
        with self._transaction() as db:
            _, records, _, _, (control, states, _) = self._audit(db, include_lifecycle=True)
            record = self._required(records, request_id)
            old = states.get(request_id, self._empty_lifecycle())
            known = db.execute("SELECT request_id,evidence_sha256 FROM lifecycle_evidence_ids WHERE evidence_id=?",
                               (evidence.evidence_id,)).fetchone()
            if known is not None:
                if known != (request_id, digest):
                    raise JournalStateError("Order evidence ID reused with changed content")
                return deepcopy(old)
            state = self._lifecycle_transition(old, "ORDER_EVIDENCE", payload, record)
            # Fill IDs identify actual executions across the entire journal,
            # never two differently bound requests in the same private book.
            incoming_ids = {fill["fill_id"] for fill in payload["fills"]}
            if any(incoming_ids & {fill["fill_id"] for fill in other["evidence"]["fills"]}
                   for rid, other in states.items() if rid != request_id and other["evidence"]):
                raise JournalStateError("Fill identity reused across orders")
            db.execute("INSERT INTO lifecycle_evidence_ids VALUES (?,?,?)", (evidence.evidence_id, request_id, digest))
            control["evidence_count"] += 1
            if old["evidence"] is not None and self._economic_evidence(old["evidence"]) == self._economic_evidence(payload):
                self._save_lifecycle_state(db, request_id, state)
                db.execute("UPDATE lifecycle_control SET payload=?,sha256=? WHERE id=1",
                           (_encoded(control), canonical_sha256(control)))
            else:
                self._append_lifecycle(db, control, request_id, "ORDER_EVIDENCE", payload, state, uuid.uuid4().hex)
            return deepcopy(state)

    @staticmethod
    def _reservation_request(records, reservation_id):
        matches = [(rid, record) for rid, record in records.items()
                   if record["grant"] and record["grant"]["reservation_id"] == reservation_id]
        if len(matches) != 1 or matches[0][1]["state"] != "ACCEPTED":
            raise JournalStateError("Exact accepted reservation required")
        return matches[0]

    @staticmethod
    def _project_reservation(record, state):
        from ml.stock_trader.horizon_ledger import ReservationState
        request, grant, evidence, cancel = record["request"], record["grant"], state["evidence"], state["cancellation"]
        # Empty native allocation/batch identities mean not locally known.
        # This is a cancellation/status view, never an ownership/capacity ledger.
        return ReservationState(grant["reservation_id"], "", request["symbol"], "15m",
            request["forecast_id"], request["side"], grant["quantity"], request["limit_price"],
            evidence["cumulative_filled_quantity"] if evidence else 0,
            evidence["status"] if evidence else "SUBMITTED", record["broker_order_id"],
            request["request_id"], "", evidence["observed_at"] if evidence else None,
            cancel["requested_at"] if cancel else None)

    def lookup_reservation(self, reservation_id):
        with self._transaction() as db:
            _, records, _, _, (_, states, _) = self._audit(db, include_lifecycle=True)
            rid, record = self._reservation_request(records, reservation_id)
            return self._project_reservation(record, states.get(rid, self._empty_lifecycle()))

    def lookup_reservation_with_order_state(self, reservation_id, *, expected_request_id):
        """Project one exact accepted reservation and lifecycle from a fresh audit.

        No result survives this call. Keep the original transaction, complete
        replay and unique accepted-reservation check, and bind both views to
        the request being projected before returning detached values.
        """
        with self._transaction() as db:
            _, records, _, _, (_, states, _) = self._audit(db, include_lifecycle=True)
            rid, record = self._reservation_request(records, reservation_id)
            if rid != expected_request_id:
                raise JournalStateError("Accepted reservation belongs to another request")
            state = states.get(rid, self._empty_lifecycle())
            return self._project_reservation(record, state), deepcopy(state)

    def reservation_stream(self, reservation_id):
        self.lookup_reservation(reservation_id)
        return STREAM

    def pending_reservations(self):
        with self._transaction() as db:
            _, records, _, _, (_, states, _) = self._audit(db, include_lifecycle=True)
            return tuple(self._project_reservation(record, states.get(rid, self._empty_lifecycle()))
                         for rid, record in records.items() if record["state"] == "ACCEPTED"
                         and (not states.get(rid, {}).get("evidence") or states[rid]["evidence"]["status"] not in _TERMINAL))

    def reserve_stream_cancellation(self, reservation_id, *, stream, reason, as_of, evidence_id):
        from ml.stock_trader.horizon_ledger import _utc
        if stream != STREAM:
            raise JournalStateError("Cancellation stream differs from Scout")
        with self._transaction() as db:
            _, records, _, _, (control, states, _) = self._audit(db, include_lifecycle=True)
            rid, record = self._reservation_request(records, reservation_id)
            old = states.get(rid, self._empty_lifecycle())
            payload = {"reservation_id": reservation_id, "broker_order_id": record["broker_order_id"],
                       "requested_at": _utc(as_of), "stream": stream, "reason": reason, "evidence_id": evidence_id}
            if old["cancellation"] is not None:
                # Existing CancellationService calls this again with its new
                # attempt clock. Preserve the first marker, as the native
                # ledger does; retries never rewrite the cancellation intent.
                if (old["cancellation"]["evidence_id"] == evidence_id
                        and any(old["cancellation"][key] != payload[key] for key in payload if key != "requested_at")):
                    raise JournalStateError("Cancellation ID reused with changed content")
                return False
            state = self._lifecycle_transition(old, "CANCEL_REQUESTED", payload, record)
            self._append_lifecycle(db, control, rid, "CANCEL_REQUESTED", payload, state, "cancel:" + evidence_id)
            return True

    def record_cancellation_event(self, event_id, kind, payload):
        """Persist an immutable local attempt fact; caller owns retry policy.

        Status-pending/reconciled polling has its durable order evidence already
        and is deliberately excluded. Before every DELETE attempt, the caller
        commits cancellation_send_claimed with a new attempt_id and treats it
        as consumption of any earlier proven-unsent retry permission. A crash
        after this method returns retains that fence. A later exact failure may
        establish new proven-unsent evidence. These facts never alter order
        status or release authority commitments.
        """
        _text(event_id, "cancellation attempt event ID")
        detail = json.loads(_encoded({"kind": kind, "payload": payload}))
        with self._transaction() as db:
            _, records, _, _, (control, states, events) = self._audit(db, include_lifecycle=True)
            rid, record = self._reservation_request(records, payload.get("reservation_id"))
            old = states.get(rid, self._empty_lifecycle())
            if event_id in events:
                event = events[event_id]
                if event["request_id"] != rid or event["kind"] != "CANCEL_EVENT" or event["payload"] != detail:
                    raise JournalStateError("Cancellation attempt ID reused with changed content")
                return deepcopy(old)
            state = self._lifecycle_transition(old, "CANCEL_EVENT", detail, record)
            if kind == "cancellation_send_claimed" and any(
                    event["kind"] == "CANCEL_EVENT" and event["payload"]["kind"] == kind
                    and event["payload"]["payload"]["attempt_id"] == payload["attempt_id"]
                    for event in events.values()):
                raise JournalStateError("Cancellation send attempt ID reused")
            self._append_lifecycle(db, control, rid, "CANCEL_EVENT", detail, state, event_id)
            return deepcopy(state)


# Stable original identities: class/instance overrides keep the legacy call
# signature. These are not updated when a caller replaces a public attribute.
_NATIVE_VALIDATE_RECORD = ScoutJournal._validate_record
_NATIVE_DTO_VALIDATION = _record_validation_contract()
