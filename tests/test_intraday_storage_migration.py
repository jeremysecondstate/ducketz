"""Temporary-datastore migration fixtures; no live stores or operating calls."""
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from ml.intraday.prediction import encoded
from ml.intraday.snapshot_objects import SnapshotObjects
from tools import intraday_storage_migration as migration


PREPARATION = "intraday/system-1a/preparation-AAPL.sqlite3"
MODEL = "intraday/system-1b/models-0.sqlite3"


@contextmanager
def database(path):
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def fixture_db(root, name=PREPARATION, payloads=None):
    root.mkdir(exist_ok=True)
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    with database(path) as db:
        db.executescript("""
            CREATE TABLE snapshots(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE symbol_inputs(cycle_id TEXT,symbol TEXT,snapshot_id TEXT,
                PRIMARY KEY(cycle_id,symbol));
            CREATE TABLE forecasts(id TEXT PRIMARY KEY,payload TEXT);
            CREATE TABLE orders(id TEXT PRIMARY KEY,payload TEXT);
            INSERT INTO forecasts VALUES('forecast','original forecast');
            INSERT INTO orders VALUES('UNKNOWN','original broker evidence');
        """)
        for n, value in enumerate(payloads or [{"X": [[1.5, 2]], "y": [1], "cutoff": "original"}]):
            raw = encoded(value).encode()
            key = hashlib.sha256(raw).hexdigest()
            db.execute("INSERT INTO snapshots VALUES(?,?)", (key, raw.decode()))
            db.execute("INSERT INTO symbol_inputs VALUES(?,?,?)", (str(n), "AAPL", key))
            rows.append((key, raw))
    return path, rows


def bindings(root, names=(PREPARATION,)):
    evidence = root / "reader-evidence.json"
    evidence.write_text('{"fixture":"reviewed test reader binding"}')
    return {name: {"object_directory": str(root / migration.OBJECT_DIRECTORY),
        "evidence": str(evidence), "evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}
        for name in names}


def unchanged_tables(path):
    with database(path) as db:
        return {name: db.execute("SELECT * FROM " + name).fetchall()
                for name in ("symbol_inputs", "forecasts", "orders")}


def test_default_dry_run_does_not_change_database_or_create_objects(tmp_path):
    root = tmp_path / "data"
    path, rows = fixture_db(root)
    before = path.read_bytes()
    report = migration.migrate(root, [PREPARATION])
    assert report["eligible_rows"] == 1
    assert report["migrated_rows"] == 0
    assert report["filesystem_bytes_reclaimed"] == 0
    assert path.read_bytes() == before
    assert not (root / migration.OBJECT_DIRECTORY).exists()
    assert (root / migration.STATE_DIRECTORY / "runs" / (report["run_id"] + ".json")).exists()


def test_apply_reuses_exact_ready_object_and_preserves_other_tables(tmp_path):
    root = tmp_path / "data"
    path, rows = fixture_db(root)
    key, raw = rows[0]
    objects = SnapshotObjects(root / migration.OBJECT_DIRECTORY)
    assert objects.write(raw) == key
    object_file = objects._path(key)
    old_stat = object_file.stat()
    original = unchanged_tables(path)
    report = migration.migrate(root, [PREPARATION], apply=True, reader_bindings=bindings(root))
    with database(path) as db:
        saved_id, saved = db.execute("SELECT id,payload FROM snapshots").fetchone()
    reference = json.loads(saved)
    assert saved_id == key
    assert reference == {"snapshot_storage": migration.REFERENCE, "sha256": key, "bytes": len(raw)}
    assert objects.read(key, reference["bytes"]) == raw
    assert object_file.stat().st_mtime_ns == old_stat.st_mtime_ns
    assert unchanged_tables(path) == original
    assert report["migrated_rows"] == 1
    assert report["logical_inline_bytes_removed"] == len(raw) - len(saved.encode())
    assert report["filesystem_bytes_reclaimed"] == 0
    again = migration.migrate(root, [PREPARATION], apply=True, reader_bindings=bindings(root))
    assert again["migrated_rows"] == 0
    assert again["databases"][0]["rows"][0]["status"] == "EXISTING_REFERENCE"


def test_model_database_uses_the_same_object_as_preparation(tmp_path):
    root = tmp_path / "data"
    _, a = fixture_db(root)
    b_path, b = fixture_db(root, MODEL)
    report = migration.migrate(root, [PREPARATION, MODEL], apply=True,
                               reader_bindings=bindings(root, (PREPARATION, MODEL)))
    assert a == b
    assert report["migrated_rows"] == 2
    files = list((root / migration.OBJECT_DIRECTORY).rglob("*.json"))
    assert len(files) == 1
    assert files[0].read_bytes() == b[0][1]
    assert unchanged_tables(b_path)["orders"] == [("UNKNOWN", "original broker evidence")]


def test_chunked_write_roundtrips_original_bytes_and_shares_chunks(tmp_path):
    root = tmp_path / "data"
    history = [[index, "same observation" * 20] for index in range(2000)]
    _, rows = fixture_db(root, payloads=[{"history": history, "cutoff": "one"},
                                       {"history": history, "cutoff": "two"}])
    report = migration.migrate(root, [PREPARATION], apply=True,
        reader_bindings=bindings(root), chunked=True, max_bytes=8 * 1024 * 1024)
    assert report["migrated_rows"] == 2
    objects = SnapshotObjects(root / migration.OBJECT_DIRECTORY)
    for key, raw in rows:
        assert objects.read(key, len(raw)) == raw
        assert not objects._path(key).exists()
    manifests = [json.loads(objects._manifest_path(key).read_text()) for key, _ in rows]
    shared = set(x["sha256"] for x in manifests[0]["chunks"]) & set(x["sha256"] for x in manifests[1]["chunks"])
    assert len(shared) > 1


def test_apply_requires_exact_reader_binding_and_unmodified_evidence(tmp_path):
    root = tmp_path / "data"
    path, _ = fixture_db(root)
    before = path.read_bytes()
    with pytest.raises(migration.MigrationError, match="reader binding"):
        migration.migrate(root, [PREPARATION], apply=True)
    review = bindings(root)
    review[PREPARATION]["object_directory"] = str(root / "different")
    with pytest.raises(migration.MigrationError, match="different snapshot"):
        migration.migrate(root, [PREPARATION], apply=True, reader_bindings=review)
    review = bindings(root)
    Path(review[PREPARATION]["evidence"]).write_text("changed")
    with pytest.raises(migration.MigrationError, match="evidence changed"):
        migration.migrate(root, [PREPARATION], apply=True, reader_bindings=review)
    assert path.read_bytes() == before


def test_corrupt_inline_or_existing_object_never_replaces_database_row(tmp_path):
    root = tmp_path / "data"
    path, [(key, raw)] = fixture_db(root)
    objects = SnapshotObjects(root / migration.OBJECT_DIRECTORY)
    objects.write(raw)
    objects._path(key).write_bytes(b"corrupt")
    report = migration.migrate(root, [PREPARATION], apply=True, reader_bindings=bindings(root))
    assert report["status"] == "PENDING"
    assert report["databases"][0]["status"] == "ERROR"
    with database(path) as db:
        assert db.execute("SELECT payload FROM snapshots").fetchone()[0].encode() == raw
        db.execute("UPDATE snapshots SET payload='{}'")
    report = migration.migrate(root, [PREPARATION], apply=True, reader_bindings=bindings(root))
    assert report["migrated_rows"] == 0
    assert "identity" in report["databases"][0]["error"]


def test_row_and_byte_limits_resume_without_skipping(tmp_path):
    root = tmp_path / "data"
    path, rows = fixture_db(root, payloads=[{"sequence": i, "X": [i] * 200} for i in range(3)])
    review = bindings(root)
    too_small = migration.migrate(root, [PREPARATION], apply=True, reader_bindings=review, max_bytes=1)
    assert too_small["migrated_rows"] == 0
    assert too_small["read_bytes"] == 0
    assert too_small["databases"][0]["status"] == "BYTE_LIMIT"
    reports = [migration.migrate(root, [PREPARATION], apply=True, reader_bindings=review, max_rows=1)
               for _ in range(3)]
    assert sum(r["migrated_rows"] for r in reports) == 3
    assert len({r["databases"][0]["rows"][0]["id"] for r in reports}) == 3
    complete = migration.migrate(root, [PREPARATION], apply=True, reader_bindings=review)
    assert complete["databases"][0]["status"] == "PASS_COMPLETE"
    assert complete["scanned_rows"] == 0


def test_busy_database_remains_pending_and_row_is_not_skipped(tmp_path):
    root = tmp_path / "data"
    path, rows = fixture_db(root)
    review = bindings(root)
    with database(path) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        report = migration.migrate(root, [PREPARATION], apply=True, reader_bindings=review)
        assert report["databases"][0]["status"] == "BUSY"
        assert report["migrated_rows"] == 0
    after = migration.migrate(root, [PREPARATION], apply=True, reader_bindings=review)
    assert after["migrated_rows"] == 1


def test_compare_and_swap_preserves_concurrent_replacement(tmp_path, monkeypatch):
    root = tmp_path / "data"
    path, [(key, raw)] = fixture_db(root)
    original_replace = migration._replace
    def racing_replace(connection, row, reference, deadline):
        with database(path) as other:
            other.execute("UPDATE snapshots SET payload=? WHERE id=?", ('{"new":"other writer"}', key))
        return original_replace(connection, row, reference, deadline)
    monkeypatch.setattr(migration, "_replace", racing_replace)
    report = migration.migrate(root, [PREPARATION], apply=True, reader_bindings=bindings(root))
    assert report["databases"][0]["status"] == "CHANGED_DURING_REVIEW"
    assert report["migrated_rows"] == 0
    with database(path) as db:
        assert db.execute("SELECT payload FROM snapshots").fetchone()[0] == '{"new":"other writer"}'
    assert SnapshotObjects(root / migration.OBJECT_DIRECTORY).read(key) == raw


def test_maintenance_lock_refuses_second_owner(tmp_path):
    root = tmp_path / "data"
    path, _ = fixture_db(root)
    with migration.maintenance_lock(root):
        with pytest.raises(migration.MigrationBusy):
            migration.migrate(root, [PREPARATION])


@pytest.mark.parametrize("name", ["intraday/shadow.sqlite3", "intraday/execution/holdings.sqlite3",
    "../outside.sqlite3", "intraday/system-1a/../../outside.sqlite3", "/outside.sqlite3",
    "intraday/system-1a/preparation-AAPL.sqlite3/extra"])
def test_unsupported_and_escaping_database_paths_are_rejected(tmp_path, name):
    root = tmp_path / "data"
    fixture_db(root)
    with pytest.raises(migration.MigrationError, match="allowlist"):
        migration.migrate(root, [name])


def test_symlink_database_or_object_directory_is_rejected(tmp_path):
    root = tmp_path / "data"
    path, _ = fixture_db(root)
    target = tmp_path / "outside.sqlite3"
    path.rename(target)
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("Symlink creation unavailable")
    with pytest.raises(migration.MigrationError, match="reparse"):
        migration.migrate(root, [PREPARATION])


def test_trigger_cannot_modify_other_table_and_is_rejected(tmp_path):
    root = tmp_path / "data"
    path, _ = fixture_db(root)
    with database(path) as db:
        db.execute("CREATE TRIGGER alter_orders AFTER UPDATE ON snapshots BEGIN DELETE FROM orders; END")
    report = migration.migrate(root, [PREPARATION], apply=True, reader_bindings=bindings(root))
    assert report["databases"][0]["status"] == "ERROR"
    assert "triggers" in report["databases"][0]["error"]
    assert unchanged_tables(path)["orders"] == [("UNKNOWN", "original broker evidence")]


def test_preview_cursor_does_not_skip_apply_work(tmp_path):
    root = tmp_path / "data"
    fixture_db(root, payloads=[{"i": 1}, {"i": 2}])
    assert migration.migrate(root, [PREPARATION], max_rows=1)["eligible_rows"] == 1
    applied = migration.migrate(root, [PREPARATION], apply=True, reader_bindings=bindings(root))
    assert applied["migrated_rows"] == 2


def test_no_production_import_or_database_initialization_needed(tmp_path):
    root = tmp_path / "data"
    path, _ = fixture_db(root)
    with database(path) as db:
        before_schema = db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    migration.migrate(root, [PREPARATION], apply=True, reader_bindings=bindings(root))
    with database(path) as db:
        assert db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall() == before_schema


def test_crash_after_commit_resumes_from_exact_reference(tmp_path, monkeypatch):
    root = tmp_path / "data"
    path, [(key, raw)] = fixture_db(root)
    review = bindings(root)
    original_replace = migration._replace
    def crash_after_commit(*args):
        assert original_replace(*args) == "MIGRATED"
        raise SystemExit("simulated process loss after commit")
    monkeypatch.setattr(migration, "_replace", crash_after_commit)
    with pytest.raises(SystemExit):
        migration.migrate(root, [PREPARATION], apply=True, reader_bindings=review)
    monkeypatch.setattr(migration, "_replace", original_replace)
    report = migration.migrate(root, [PREPARATION], apply=True, reader_bindings=review)
    assert report["migrated_rows"] == 0
    assert report["databases"][0]["rows"][0]["status"] == "EXISTING_REFERENCE"
    assert SnapshotObjects(root / migration.OBJECT_DIRECTORY).read(key) == raw


def test_elapsed_budget_before_compare_and_swap_leaves_inline_row(tmp_path, monkeypatch):
    root = tmp_path / "data"
    path, [(key, raw)] = fixture_db(root)
    original_write = migration._SafeObjects.write
    clock = {"value": 100.0}
    monkeypatch.setattr(migration.time, "monotonic", lambda: clock["value"])
    def expire_after_object(self, value, **kwargs):
        result = original_write(self, value, **kwargs)
        clock["value"] += 2
        return result
    monkeypatch.setattr(migration._SafeObjects, "write", expire_after_object)
    report = migration.migrate(root, [PREPARATION], apply=True,
                               reader_bindings=bindings(root), max_seconds=1)
    assert report["databases"][0]["status"] == "TIME_LIMIT"
    with database(path) as db:
        assert db.execute("SELECT payload FROM snapshots").fetchone()[0].encode() == raw
    assert SnapshotObjects(root / migration.OBJECT_DIRECTORY).read(key) == raw


def test_reparse_parent_is_rejected_before_database_or_object_mutation(tmp_path, monkeypatch):
    root = tmp_path / "data"
    path, _ = fixture_db(root)
    review = bindings(root)
    before = path.read_bytes()
    blocked = root / "intraday" / "system-1a" / "ready"
    blocked.mkdir()
    original_lstat = Path.lstat
    def junction_lstat(self, *args, **kwargs):
        value = original_lstat(self, *args, **kwargs)
        if self == blocked:
            return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=1024)
        return value
    monkeypatch.setattr(Path, "lstat", junction_lstat)
    with pytest.raises(migration.MigrationError, match="reparse"):
        migration.migrate(root, [PREPARATION], apply=True, reader_bindings=review)
    assert path.read_bytes() == before


def test_trigger_added_after_review_is_rechecked_inside_transaction(tmp_path, monkeypatch):
    root = tmp_path / "data"
    path, _ = fixture_db(root)
    original_replace = migration._replace
    def install_trigger(*args):
        with database(path) as other:
            other.execute("CREATE TRIGGER unexpected AFTER UPDATE ON snapshots BEGIN DELETE FROM orders; END")
        return original_replace(*args)
    monkeypatch.setattr(migration, "_replace", install_trigger)
    report = migration.migrate(root, [PREPARATION], apply=True, reader_bindings=bindings(root))
    assert report["databases"][0]["status"] == "ERROR"
    assert report["migrated_rows"] == 0
    assert unchanged_tables(path)["orders"] == [("UNKNOWN", "original broker evidence")]
