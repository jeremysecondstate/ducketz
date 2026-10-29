"""Bounded conversion of reviewed inline input snapshots to shared exact objects.

This tool never vacuums, deletes, runs providers, or alters any other table.
Apply requires caller-reviewed reader bindings: the receipt is evidence retained
for audit, not a claim that this helper can inspect running Python readers.
The caller must establish that every reader of each selected database supports
the shared snapshot directory before applying. Ordinary data work is unaffected
by this maintenance-only prerequisite.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import sys
import time
import uuid

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ml.intraday.prediction import encoded
from ml.intraday.snapshot_objects import MAX_SNAPSHOT_BYTES, SnapshotObjects


SCHEMA = "intraday-inline-snapshot-migration-v1"
REFERENCE = "intraday-input-reference-v1"
OBJECT_DIRECTORY = "intraday/system-1a/ready/snapshots"
STATE_DIRECTORY = "state/intraday-storage-manager/snapshot-migration"
LOCK_NAME = "locks/intraday-storage-manager.lock"
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_ALLOWED = re.compile(
    r"intraday/(?:system-1a/preparation(?:-[A-Z0-9_.-]{1,16})?"
    r"|system-1b/models(?:-[0-9]{1,3})?)\.sqlite3\Z"
)


class MigrationError(ValueError):
    pass


class MigrationBusy(MigrationError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _plain(path):
    """Reject symlinks/junctions before resolving or opening any path."""
    path = Path(os.path.abspath(path))
    for item in (*reversed(path.parents), path):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
            raise MigrationError("Symlink or reparse path is not supported")
    return path


def _within(root, relative):
    if not isinstance(relative, str) or "\\" in relative:
        raise MigrationError("Expected a portable relative datastore path")
    parts = PurePosixPath(relative)
    if parts.is_absolute() or not parts.parts or any(p in {".", ".."} for p in parts.parts):
        raise MigrationError("Path escapes datastore")
    path = _plain(root / relative)
    if not path.is_relative_to(root):
        raise MigrationError("Path escapes datastore")
    return path


def _json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise MigrationError("Duplicate JSON key")
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=unique,
        parse_constant=lambda _: (_ for _ in ()).throw(MigrationError("Nonfinite JSON")))


def _atomic(path, value):
    path = _plain(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _plain(path.parent)
    temporary = path.with_name("." + uuid.uuid4().hex + ".partial")
    try:
        with temporary.open("xb") as stream:
            stream.write((json.dumps(value, sort_keys=True, indent=2) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        _plain(path)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def maintenance_lock(datastore):
    """Nonblocking native lock shared by cooperating storage maintenance."""
    root = _plain(datastore)
    target = _within(root, LOCK_NAME)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a+b") as stream:
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise MigrationBusy("Another datastore maintenance owner is active") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


class _SafeObjects(SnapshotObjects):
    """Check each exact object/chunk path instead of traversing the whole tree."""
    def _path(self, identity):
        return _plain(super()._path(identity))

    def _manifest_path(self, identity):
        return _plain(super()._manifest_path(identity))

    def _chunk_path(self, identity):
        return _plain(super()._chunk_path(identity))

    def _publish(self, path, raw, bound):
        _plain(path)
        return super()._publish(path, raw, bound)


def _binding(root, name, bindings):
    value = bindings.get(name)
    if not isinstance(value, dict) or set(value) != {"object_directory", "evidence", "evidence_sha256"}:
        raise MigrationError("Apply requires an exact reviewed reader binding for every database")
    if (not isinstance(value["object_directory"], str)
        or not Path(value["object_directory"]).is_absolute()
        or _plain(value["object_directory"]) != _within(root, OBJECT_DIRECTORY)):
        raise MigrationError("Reader uses a different snapshot directory")
    if not isinstance(value["evidence"], str):
        raise MigrationError("Reader evidence requires an absolute path and SHA-256")
    evidence = Path(value["evidence"])
    if not evidence.is_absolute() or not _HASH.fullmatch(str(value["evidence_sha256"])):
        raise MigrationError("Reader evidence requires an absolute path and SHA-256")
    evidence = _plain(evidence)
    if not evidence.is_file() or evidence.stat().st_size > 4 * 1024 * 1024:
        raise MigrationError("Reader evidence must be a bounded regular file")
    if _sha(evidence.read_bytes()) != value["evidence_sha256"]:
        raise MigrationError("Reader evidence changed")
    return dict(value)


def _connect(path, *, write, deadline):
    for suffix in ("", "-wal", "-shm", "-journal"):
        item = _plain(str(path) + suffix)
        if item.exists() and not item.is_file():
            raise MigrationError("Database or sidecar is not a regular file")
    connection = sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                                uri=True, timeout=0, isolation_level=None)
    try:
        connection.execute("PRAGMA trusted_schema=OFF")
        if not write:
            connection.execute("PRAGMA query_only=ON")
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        _schema(connection)
    except BaseException:
        connection.close()
        raise
    return connection


def _schema(connection):
    columns = connection.execute("PRAGMA table_info(snapshots)").fetchall()
    if [(r[1], r[2].upper(), r[5]) for r in columns] != [("id", "TEXT", 1), ("payload", "TEXT", 0)]:
        raise MigrationError("Unsupported snapshots schema")
    # A trigger could modify an unrelated table despite the narrow UPDATE.
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name='snapshots'").fetchone():
        raise MigrationError("Snapshot mutation triggers require separate review")


def _row(connection, cursor, remaining):
    row = connection.execute(
        "SELECT rowid,id,typeof(payload) FROM snapshots WHERE id>? ORDER BY id LIMIT 1", (cursor,)).fetchone()
    if row is None:
        return None
    rowid, identity, kind = row
    if not isinstance(identity, str) or not _HASH.fullmatch(identity) or kind != "text":
        raise MigrationError("Invalid snapshot identity or representation")
    # The SQLite incremental-blob reader exposes the byte length before reading
    # any overflow pages. This avoids fetching a huge row just to check a limit.
    with connection.blobopen("snapshots", "payload", rowid, readonly=True) as blob:
        size = len(blob)
        if size > remaining or size > MAX_SNAPSHOT_BYTES:
            return {"id": identity, "rowid": rowid, "bytes": size, "limit": True}
        raw = blob.read()
    return {"id": identity, "rowid": rowid, "bytes": size, "raw": raw}


def _is_reference(identity, value):
    return (isinstance(value, dict) and set(value) == {"snapshot_storage", "sha256", "bytes"}
        and value["snapshot_storage"] == REFERENCE and value["sha256"] == identity
        and type(value["bytes"]) is int and 0 <= value["bytes"] <= MAX_SNAPSHOT_BYTES)


def _replace(connection, row, reference, deadline):
    if time.monotonic() >= deadline:
        return "TIME_LIMIT"
    try:
        connection.execute("BEGIN IMMEDIATE")
        _schema(connection)
        changed = connection.execute(
            "UPDATE snapshots SET payload=? WHERE rowid=? AND id=? AND payload=?",
            (reference, row["rowid"], row["id"], row["raw"].decode("utf-8"))).rowcount
        if changed != 1:
            connection.execute("ROLLBACK")
            return "CHANGED_DURING_REVIEW"
        saved = connection.execute("SELECT payload FROM snapshots WHERE id=?", (row["id"],)).fetchone()
        if saved != (reference,):
            raise MigrationError("Snapshot reference readback differs")
        connection.execute("COMMIT")
        return "MIGRATED"
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def migrate(datastore, databases, *, apply=False, reader_bindings=None,
            max_rows=256, max_bytes=256 * 1024 * 1024, max_seconds=20, chunked=False):
    """Return one durable bounded result. No table/file/DB deletion or compaction.

    Cursor progress is independent per database and stable snapshot ID. A
    completed pass wraps to the start on the next wake, catching newly inserted
    identities. A crash after commit but before checkpoint is harmless: the next
    run sees the reference. Failed/busy rows are not skipped.
    """
    if (type(max_rows) is not int or not 1 <= max_rows <= 10000
        or type(max_bytes) is not int or not 1 <= max_bytes <= MAX_SNAPSHOT_BYTES
        or not isinstance(max_seconds, (int, float)) or isinstance(max_seconds, bool)
        or not 0 < max_seconds <= 300 or type(apply) is not bool or type(chunked) is not bool):
        raise MigrationError("Invalid migration bounds or mode")
    if not Path(datastore).is_absolute():
        raise MigrationError("Datastore requires an absolute path")
    root = _plain(datastore)
    if not root.is_dir():
        raise MigrationError("Datastore must already exist")
    names = list(databases)
    if not names or len(names) > 128 or len(set(names)) != len(names):
        raise MigrationError("An explicit unique bounded database list is required")
    for name in names:
        if not isinstance(name, str) or not _ALLOWED.fullmatch(name):
            raise MigrationError("Database is outside the migration allowlist")
        if not _within(root, name).is_file():
            raise MigrationError("Selected database is missing")
    if reader_bindings is not None and not isinstance(reader_bindings, dict):
        raise MigrationError("Reader bindings must be an explicit mapping")
    bindings = {name: _binding(root, name, reader_bindings or {}) for name in names} if apply else {}
    started = time.monotonic()
    deadline = started + max_seconds
    report = {"schema": SCHEMA, "run_id": uuid.uuid4().hex, "started_at": _now(),
        "mode": "apply" if apply else "dry_run", "status": "COMPLETE_BATCH", "databases": [],
        "bounds": {"rows": max_rows, "bytes": max_bytes, "seconds": max_seconds},
        "scanned_rows": 0, "read_bytes": 0, "migrated_rows": 0, "eligible_rows": 0,
        "logical_inline_bytes_removed": 0, "filesystem_bytes_reclaimed": 0,
        "reader_bindings": bindings, "objects": OBJECT_DIRECTORY,
        "limitations": ["No vacuum, file deletion or filesystem space reclamation.",
            "Caller supplies reviewed reader compatibility; this helper does not inspect running processes.",
            "Existing references are classified, not a full object-integrity audit."]}
    state_root = _within(root, STATE_DIRECTORY)
    # Dry run uses its own cursor so a preview cannot skip work for application.
    state_path = state_root / ("apply-cursor.json" if apply else "dry-run-cursor.json")
    with maintenance_lock(root):
        state = _json(_plain(state_path).read_bytes()) if state_path.exists() else {"schema": SCHEMA, "cursors": {}}
        if not isinstance(state, dict) or state.get("schema") != SCHEMA or not isinstance(state.get("cursors"), dict):
            raise MigrationError("Invalid migration checkpoint")
        objects = _SafeObjects(_within(root, OBJECT_DIRECTORY), chunked=chunked)
        for name in names:
            item = {"database": name, "status": "COMPLETE_BATCH", "rows": []}
            report["databases"].append(item)
            cursor = state["cursors"].get(name, "")
            if cursor != "" and (not isinstance(cursor, str) or not _HASH.fullmatch(cursor)):
                raise MigrationError("Invalid migration cursor")
            connection = None
            try:
                connection = _connect(_within(root, name), write=apply, deadline=deadline)
                while report["scanned_rows"] < max_rows and time.monotonic() < deadline:
                    row = _row(connection, cursor, max_bytes - report["read_bytes"])
                    if row is None:
                        item["status"] = "PASS_COMPLETE"
                        state["cursors"][name] = ""
                        break
                    if row.get("limit"):
                        item["status"] = "BYTE_LIMIT"
                        item["pending_id"] = row["id"]
                        item["pending_bytes"] = row["bytes"]
                        break
                    report["scanned_rows"] += 1
                    report["read_bytes"] += row["bytes"]
                    value = _json(row["raw"])
                    exact = _sha(row["raw"]) == row["id"]
                    entry = {"id": row["id"], "original_bytes": row["bytes"]}
                    item["rows"].append(entry)
                    if not exact and _is_reference(row["id"], value):
                        entry["status"] = "EXISTING_REFERENCE"
                    else:
                        if (not exact or not isinstance(value, dict)
                            or encoded(value).encode("utf-8") != row["raw"]):
                            raise MigrationError("Inline snapshot bytes or canonical identity changed")
                        report["eligible_rows"] += 1
                        entry["status"] = "ELIGIBLE"
                        if apply:
                            _binding(root, name, bindings)  # Evidence cannot mutate during a batch.
                            if time.monotonic() >= deadline:
                                item["status"] = "TIME_LIMIT"
                                break
                            identity = objects.write(row["raw"], payload=value) if chunked else objects.write(row["raw"])
                            if identity != row["id"] or objects.read(identity, row["bytes"]) != row["raw"]:
                                raise MigrationError("Original object bytes failed roundtrip")
                            reference = encoded({"snapshot_storage": REFERENCE,
                                "sha256": identity, "bytes": row["bytes"]})
                            result = _replace(connection, row, reference, deadline)
                            entry["status"] = result
                            if result != "MIGRATED":
                                item["status"] = result
                                break
                            report["migrated_rows"] += 1
                            report["logical_inline_bytes_removed"] += row["bytes"] - len(reference.encode())
                    cursor = row["id"]
                    state["cursors"][name] = cursor
                    _atomic(state_path, state)
                else:
                    item["status"] = "TIME_LIMIT" if time.monotonic() >= deadline else "ROW_LIMIT"
            except (sqlite3.Error, MigrationError, OSError, ValueError) as exc:
                is_busy = isinstance(exc, sqlite3.OperationalError) and (
                    getattr(exc, "sqlite_errorcode", None) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                    or "locked" in str(exc).lower())
                item["status"] = "BUSY" if is_busy else "TIME_LIMIT" if time.monotonic() >= deadline else "ERROR"
                item["error_type"] = type(exc).__name__
                item["error"] = str(exc)
            finally:
                if connection is not None:
                    connection.close()
            _atomic(state_path, state)
            if item["status"] not in {"COMPLETE_BATCH", "PASS_COMPLETE"}:
                report["status"] = "PENDING"
            if report["scanned_rows"] >= max_rows or report["read_bytes"] >= max_bytes or time.monotonic() >= deadline:
                break
        report["completed_at"] = _now()
        report["elapsed_seconds"] = time.monotonic() - started
        _atomic(state_root / "runs" / (report["run_id"] + ".json"), report)
        _atomic(state_root / "latest.json", {"schema": SCHEMA, "run_id": report["run_id"]})
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datastore", required=True, type=Path)
    parser.add_argument("--database", action="append", required=True,
                        help="Explicit allowlisted path relative to datastore; repeat for several databases")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reader-bindings", type=Path,
                        help="Reviewed JSON mapping database paths to object_directory, evidence and evidence_sha256")
    parser.add_argument("--max-rows", type=int, default=256)
    parser.add_argument("--max-bytes", type=int, default=256 * 1024 * 1024)
    parser.add_argument("--max-seconds", type=float, default=20)
    parser.add_argument("--chunked", action="store_true",
                        help="Use only after every selected reader supports immutable chunk manifests")
    options = parser.parse_args(argv)
    try:
        bindings = None
        if options.reader_bindings:
            binding_path = _plain(options.reader_bindings)
            if not binding_path.is_file() or binding_path.stat().st_size > 4 * 1024 * 1024:
                raise MigrationError("Reader bindings file exceeds its resource bound")
            bindings = _json(binding_path.read_bytes())
        result = migrate(options.datastore, options.database, apply=options.apply, reader_bindings=bindings,
            max_rows=options.max_rows, max_bytes=options.max_bytes, max_seconds=options.max_seconds,
            chunked=options.chunked)
    except (MigrationError, OSError, ValueError) as exc:
        print(json.dumps({"schema": SCHEMA, "status": "NOT_APPLIED", "error_type": type(exc).__name__, "error": str(exc)}))
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "COMPLETE_BATCH" else 1


if __name__ == "__main__":
    raise SystemExit(main())
