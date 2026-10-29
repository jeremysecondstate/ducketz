"""Offline snapshot compaction fixtures; never uses the live DATASTORE."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ml.intraday.prediction import encoded
from ml.intraday.snapshot_objects import SnapshotObjects
from tools import intraday_snapshot_compaction as compaction
from tools import intraday_storage_migration as migration


def snapshots(root, count=2):
    root.mkdir(exist_ok=True)
    objects = SnapshotObjects(root / compaction.OBJECT_DIRECTORY)
    common = [[i, "original observed value" * 10] for i in range(1600)]
    rows = []
    for n in range(count):
        raw = encoded({"history": common, "sequence": n}).encode()
        rows.append((objects.write(raw), raw))
    return objects, rows


def evidence(root, rows):
    value = {"identities": [key for key, _ in rows]}
    for kind in ("reader", "quiescence"):
        path = root / f"test-{kind}-evidence.json"
        path.write_text(json.dumps({"fixture": kind, "ids": value["identities"]}))
        value[kind] = {"object_directory": str(root / compaction.OBJECT_DIRECTORY),
                       "evidence": str(path),
                       "evidence_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return value


def apply(root, rows, **kwargs):
    return compaction.compact(root, [key for key, _ in rows], apply=True,
                              quiescent=True, evidence=evidence(root, rows), **kwargs)


def statuses(result):
    return [item["status"] for item in result["results"]]


def staged(root):
    return list((root / compaction.STATE_DIRECTORY / "staging").glob("*.raw"))


def test_dry_run_retains_raw_and_publishes_no_chunks(tmp_path):
    objects, rows = snapshots(tmp_path)
    before = [(objects._path(key).stat(), raw) for key, raw in rows]
    result = compaction.compact(tmp_path, [key for key, _ in rows])
    assert statuses(result) == ["WOULD_COMPACT", "WOULD_COMPACT"]
    assert result["raw_bytes_removed"] == result["new_object_bytes"] == 0
    assert not (objects.root / "chunks").exists()
    for (key, raw), (info, _) in zip(rows, before):
        assert objects.read(key) == raw
        assert objects._path(key).stat().st_mtime_ns == info.st_mtime_ns


def test_compacts_exact_identity_and_shares_chunks_without_database_changes(tmp_path):
    objects, rows = snapshots(tmp_path)
    untouched = tmp_path / "orders.sqlite3"
    untouched.write_bytes(b"unrelated exact bytes")
    result = apply(tmp_path, rows)
    assert statuses(result) == ["COMPACTED", "COMPACTED"]
    assert result["raw_bytes_removed"] == sum(len(raw) for _, raw in rows)
    assert result["filesystem_bytes_reclaimed"] is None
    assert result["net_object_allocation_reclaimed_estimate"] > 0
    assert untouched.read_bytes() == b"unrelated exact bytes"
    for key, raw in rows:
        assert not objects._path(key).exists()
        assert objects.read(key, len(raw)) == raw
    manifests = [json.loads(objects._manifest_path(key).read_bytes()) for key, _ in rows]
    assert len({item["sha256"] for item in manifests[0]["chunks"]}
               & {item["sha256"] for item in manifests[1]["chunks"]}) > 3
    assert not staged(tmp_path)
    again = apply(tmp_path, rows)
    assert statuses(again) == ["ALREADY_CHUNKED", "ALREADY_CHUNKED"]
    assert again["raw_bytes_removed"] == again["new_object_bytes"] == 0


def test_apply_requires_explicit_quiescence_and_matching_scoped_evidence(tmp_path):
    objects, rows = snapshots(tmp_path, 1)
    key, raw = rows[0]
    with pytest.raises(compaction.CompactionError, match="quiescence"):
        compaction.compact(tmp_path, [key], apply=True)
    with pytest.raises(compaction.CompactionError, match="evidence"):
        compaction.compact(tmp_path, [key], apply=True, quiescent=True)
    proof = evidence(tmp_path, rows)
    proof["identities"] = ["0" * 64]
    with pytest.raises(compaction.CompactionError, match="cover"):
        compaction.compact(tmp_path, [key], apply=True, quiescent=True, evidence=proof)
    proof = evidence(tmp_path, rows)
    Path(proof["quiescence"]["evidence"]).write_text("changed")
    with pytest.raises(migration.MigrationError, match="evidence changed"):
        compaction.compact(tmp_path, [key], apply=True, quiescent=True, evidence=proof)
    assert objects.read(key) == raw


@pytest.mark.parametrize("damage", ["chunk", "manifest", "raw"])
def test_corrupt_existing_representation_never_deletes_raw(tmp_path, damage):
    objects, rows = snapshots(tmp_path, 1)
    key, raw = rows[0]
    # Construct a full chunk representation before putting the original raw back.
    objects._path(key).unlink()
    chunked = SnapshotObjects(objects.root, chunked=True)
    chunked.write(raw, payload=json.loads(raw))
    objects._path(key).write_bytes(raw)
    if damage == "chunk":
        manifest = json.loads(objects._manifest_path(key).read_bytes())
        objects._chunk_path(manifest["chunks"][0]["sha256"]).write_bytes(b"corrupt")
    elif damage == "manifest":
        manifest = json.loads(objects._manifest_path(key).read_bytes())
        manifest["sha256"] = "0" * 64
        objects._manifest_path(key).write_text(json.dumps(manifest))
    else:
        objects._path(key).write_bytes(raw + b" ")
    before = objects._path(key).read_bytes()
    result = apply(tmp_path, rows)
    assert statuses(result) == ["PENDING_REVIEW"]
    assert objects._path(key).read_bytes() == before
    assert result["raw_bytes_removed"] == 0


def test_crash_before_staging_keeps_raw_and_resumes(tmp_path, monkeypatch):
    objects, rows = snapshots(tmp_path, 1)
    key, raw = rows[0]
    original = compaction.os.rename
    def fail(*args):
        raise OSError("simulated crash before staging")
    monkeypatch.setattr(compaction.os, "rename", fail)
    result = apply(tmp_path, rows)
    assert statuses(result) == ["PENDING_REVIEW"]
    assert objects.read(key) == raw
    assert objects._path(key).exists()
    monkeypatch.setattr(compaction.os, "rename", original)
    assert statuses(apply(tmp_path, rows)) == ["COMPACTED"]
    assert objects.read(key) == raw


def test_crash_after_staging_keeps_recoverable_bytes_and_resumes(tmp_path, monkeypatch):
    objects, rows = snapshots(tmp_path, 1)
    key, raw = rows[0]
    original = compaction._save
    def fail(root, journal):
        if journal["status"] == "staged":
            raise OSError("simulated crash after move")
        return original(root, journal)
    monkeypatch.setattr(compaction, "_save", fail)
    result = apply(tmp_path, rows)
    assert statuses(result) == ["PENDING_REVIEW"]
    assert not objects._path(key).exists()
    assert [path.read_bytes() for path in staged(tmp_path)] == [raw]
    assert objects.read(key) == raw
    monkeypatch.setattr(compaction, "_save", original)
    assert statuses(apply(tmp_path, rows)) == ["COMPACTED"]
    assert not staged(tmp_path)
    assert objects.read(key) == raw


def test_crash_after_unlink_is_idempotent_and_does_not_double_count(tmp_path, monkeypatch):
    objects, rows = snapshots(tmp_path, 1)
    key, raw = rows[0]
    original = compaction._save
    def fail(root, journal):
        if journal["status"] == "complete":
            raise OSError("simulated crash after removal")
        return original(root, journal)
    monkeypatch.setattr(compaction, "_save", fail)
    assert statuses(apply(tmp_path, rows)) == ["PENDING_REVIEW"]
    assert not objects._path(key).exists() and not staged(tmp_path)
    assert objects.read(key) == raw
    monkeypatch.setattr(compaction, "_save", original)
    result = apply(tmp_path, rows)
    assert statuses(result) == ["ALREADY_CHUNKED"]
    assert result["raw_bytes_removed"] == 0


def test_replacement_during_move_is_preserved_and_restored(tmp_path, monkeypatch):
    objects, rows = snapshots(tmp_path, 1)
    key, raw = rows[0]
    original = compaction.os.rename
    changed = raw.replace(b'"sequence":0', b'"sequence":1')
    def race(source, destination):
        Path(source).write_bytes(changed)
        return original(source, destination)
    monkeypatch.setattr(compaction.os, "rename", race)
    result = apply(tmp_path, rows)
    assert statuses(result) == ["PENDING_REVIEW"]
    assert objects._path(key).read_bytes() == changed
    assert result["raw_bytes_removed"] == 0
    assert not staged(tmp_path)


def test_new_raw_after_staging_is_never_deleted(tmp_path, monkeypatch):
    objects, rows = snapshots(tmp_path, 1)
    key, raw = rows[0]
    original = compaction.os.rename
    def race(source, destination):
        original(source, destination)
        Path(source).write_bytes(raw)
    monkeypatch.setattr(compaction.os, "rename", race)
    result = apply(tmp_path, rows)
    assert statuses(result) == ["COMPACTED_SOURCE_REAPPEARED"]
    assert objects._path(key).read_bytes() == raw


def test_bounded_rows_bytes_and_time_do_not_remove_pending_objects(tmp_path, monkeypatch):
    objects, rows = snapshots(tmp_path)
    result = apply(tmp_path, rows, max_objects=1)
    assert statuses(result) == ["COMPACTED"]
    assert result["pending_identities"] == [rows[1][0]]
    result = apply(tmp_path, rows[1:], max_bytes=len(rows[1][1]) - 1)
    assert statuses(result) == ["BYTE_LIMIT"]
    assert result["pending_identities"] == [rows[1][0]]
    assert objects._path(rows[1][0]).exists()
    monkeypatch.setattr(compaction, "_check_time",
                        lambda _: (_ for _ in ()).throw(compaction.TimeLimit("test deadline")))
    result = apply(tmp_path, rows[1:])
    assert statuses(result) == ["TIME_LIMIT"]
    assert result["pending_identities"] == [rows[1][0]]
    assert objects._path(rows[1][0]).exists()


def test_partial_publication_cost_is_reported_and_raw_stays(tmp_path, monkeypatch):
    objects, rows = snapshots(tmp_path, 1)
    original = compaction._SafeObjects._publish
    calls = []
    def fail(self, path, raw, bound):
        calls.append(path)
        if len(calls) == 3:
            raise OSError("simulated interrupted publication")
        return original(self, path, raw, bound)
    monkeypatch.setattr(compaction._SafeObjects, "_publish", fail)
    result = apply(tmp_path, rows)
    assert statuses(result) == ["PENDING_REVIEW"]
    assert result["new_object_bytes"] > 0
    assert result["net_object_allocation_reclaimed_estimate"] <= 0
    assert objects._path(rows[0][0]).exists()


def test_shared_lock_blocks_concurrent_maintenance(tmp_path):
    objects, rows = snapshots(tmp_path, 1)
    with migration.maintenance_lock(tmp_path):
        with pytest.raises(migration.MigrationBusy):
            apply(tmp_path, rows)
    assert objects._path(rows[0][0]).exists()


@pytest.mark.parametrize("identity", ["../outside", "A" * 64, "0" * 63, "0" * 64 + "/x"])
def test_identity_cannot_escape_canonical_storage(tmp_path, identity):
    with pytest.raises(compaction.CompactionError, match="identities"):
        compaction.compact(tmp_path, [identity])


def test_reparse_object_parent_is_rejected_before_open(tmp_path, monkeypatch):
    objects, rows = snapshots(tmp_path, 1)
    selected = objects._path(rows[0][0]).parent
    original = Path.lstat
    def marked(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == selected:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=1024)
        return info
    monkeypatch.setattr(Path, "lstat", marked)
    result = apply(tmp_path, rows)
    assert statuses(result) == ["PENDING_REVIEW"]
    assert "reparse" in result["results"][0]["error"]
    assert objects._path(rows[0][0]).exists()


def test_staging_escape_in_journal_is_rejected(tmp_path):
    objects, rows = snapshots(tmp_path, 1)
    key, _ = rows[0]
    migration._atomic(compaction._journal(tmp_path, key), {
        "schema": compaction.SCHEMA, "identity": key, "status": "verified",
        "staging": f"{compaction.STATE_DIRECTORY}/staging/{key}-/../../../../outside.raw"})
    result = apply(tmp_path, rows)
    assert statuses(result) == ["PENDING_REVIEW"]
    assert objects._path(key).exists()


def test_existing_manifest_must_reconstruct_raw_even_with_correct_declared_hash(tmp_path):
    objects, rows = snapshots(tmp_path)
    one, raw = rows[0]
    two, other = rows[1]
    apply(tmp_path, rows[1:])
    manifest = json.loads(objects._manifest_path(two).read_bytes())
    manifest["sha256"] = one
    objects._manifest_path(one).parent.mkdir(parents=True, exist_ok=True)
    objects._manifest_path(one).write_text(json.dumps(manifest))
    result = apply(tmp_path, rows[:1])
    assert statuses(result) == ["PENDING_REVIEW"]
    assert objects._path(one).read_bytes() == raw


def test_evidence_revocation_before_removal_retains_raw(tmp_path, monkeypatch):
    objects, rows = snapshots(tmp_path, 1)
    proof = evidence(tmp_path, rows)
    original = compaction._publish
    def revoke(*args, **kwargs):
        result = original(*args, **kwargs)
        Path(proof["quiescence"]["evidence"]).write_text("revoked")
        return result
    monkeypatch.setattr(compaction, "_publish", revoke)
    result = compaction.compact(tmp_path, [rows[0][0]], apply=True,
                                quiescent=True, evidence=proof)
    assert statuses(result) == ["PENDING_REVIEW"]
    assert objects._path(rows[0][0]).exists()


def test_already_chunked_verification_also_obeys_byte_budget(tmp_path):
    objects, rows = snapshots(tmp_path)
    apply(tmp_path, rows)
    result = apply(tmp_path, rows, max_bytes=len(rows[0][1]))
    assert statuses(result) == ["ALREADY_CHUNKED", "BYTE_LIMIT"]
    assert result["source_bytes_reviewed"] == len(rows[0][1])


def test_chunk_corruption_after_staging_retains_recoverable_raw(tmp_path, monkeypatch):
    objects, rows = snapshots(tmp_path, 1)
    key, raw = rows[0]
    original = compaction._save
    def corrupt(root, journal):
        original(root, journal)
        if journal["status"] == "staged":
            manifest = json.loads(objects._manifest_path(key).read_bytes())
            objects._chunk_path(manifest["chunks"][0]["sha256"]).write_bytes(b"broken")
    monkeypatch.setattr(compaction, "_save", corrupt)
    result = apply(tmp_path, rows)
    assert statuses(result) == ["PENDING_REVIEW"]
    assert objects._path(key).read_bytes() == raw
    assert not staged(tmp_path)
    assert result["raw_bytes_removed"] == 0


def test_hard_linked_raw_does_not_claim_allocation_reclaimed(tmp_path):
    objects, rows = snapshots(tmp_path, 1)
    key, raw = rows[0]
    retained = tmp_path / "retained-raw-link"
    compaction.os.link(objects._path(key), retained)
    result = apply(tmp_path, rows)
    assert statuses(result) == ["COMPACTED"]
    assert result["raw_allocation_removed_estimate"] == 0
    assert retained.read_bytes() == raw
