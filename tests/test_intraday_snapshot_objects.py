"""Temporary-file fixtures only; no live database, provider, fit or trader."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
import threading

import pytest

from ml.intraday.prediction import ContractError
from ml.intraday.snapshot_objects import MAX_SNAPSHOT_BYTES, SnapshotObjects


def object_path(root, identity):
    return root / identity[:2] / (identity + ".json")


def test_original_bytes_identity_and_existing_ready_layout_are_preserved(tmp_path):
    root = tmp_path / "ready" / "snapshots"
    objects = SnapshotObjects(root)
    raw = b'{"X":[[0.10000000000000001]],"original":"\\u03bb","y":[1]}'
    identity = hashlib.sha256(raw).hexdigest()
    path = object_path(root, identity)
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)  # Existing ReadySnapshots object, never rewritten.
    before = path.stat()
    assert objects.write(raw) == identity
    assert objects.read(identity, expected_bytes=len(raw)) == raw
    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns, after.st_size) == (before.st_ino, before.st_mtime_ns, before.st_size)
    assert list(root.rglob("*.partial")) == []


def test_read_and_constructor_do_not_create_missing_storage(tmp_path):
    root = tmp_path / "absent"
    objects = SnapshotObjects(root)
    with pytest.raises(FileNotFoundError):
        objects.read("a" * 64)
    assert not root.exists()


@pytest.mark.parametrize("identity", [None, "../escape", "A" * 64, "a" * 63, "a" * 64 + "\n"])
def test_invalid_identity_cannot_escape_object_root(tmp_path, identity):
    objects = SnapshotObjects(tmp_path / "objects")
    with pytest.raises(ContractError, match="identity"):
        objects.read(identity)
    assert not objects.root.exists()


@pytest.mark.parametrize("bound", [0, -1, True, 1.5, MAX_SNAPSHOT_BYTES + 1])
def test_invalid_resource_bounds_are_rejected(tmp_path, bound):
    with pytest.raises(ContractError, match="resource bound"):
        SnapshotObjects(tmp_path, max_bytes=bound)


@pytest.mark.parametrize("expected", [-1, True, 1.5, MAX_SNAPSHOT_BYTES + 1])
def test_invalid_expected_lengths_are_rejected_before_read(tmp_path, expected):
    with pytest.raises(ContractError, match="byte count"):
        SnapshotObjects(tmp_path).read("a" * 64, expected_bytes=expected)


def test_write_bound_and_original_byte_type_reject_without_side_effect(tmp_path):
    objects = SnapshotObjects(tmp_path / "objects", max_bytes=8)
    with pytest.raises(ContractError, match="resource bound"):
        objects.write(b"123456789")
    for value in ("{}", bytearray(b"{}"), None):
        with pytest.raises(ContractError, match="original bytes"):
            objects.write(value)
    assert not objects.root.exists()


def test_exact_bound_and_empty_original_roundtrip(tmp_path):
    objects = SnapshotObjects(tmp_path, max_bytes=8)
    for raw in (b"12345678", b""):
        identity = objects.write(raw)
        assert objects.read(identity, len(raw)) == raw


@pytest.mark.parametrize("replacement", [b"changed!", b"", b"123456789"])
def test_corruption_or_truncation_never_overwrites_original_path(tmp_path, replacement):
    objects = SnapshotObjects(tmp_path, max_bytes=8)
    identity = objects.write(b"original")
    path = object_path(tmp_path, identity)
    path.write_bytes(replacement)
    before = path.stat()
    with pytest.raises(ContractError, match="changed"):
        objects.read(identity, expected_bytes=8)
    with pytest.raises(ContractError, match="changed"):
        objects.write(b"original")
    assert path.read_bytes() == replacement
    assert path.stat().st_mtime_ns == before.st_mtime_ns


def test_valid_hash_with_wrong_recorded_length_is_rejected(tmp_path):
    objects = SnapshotObjects(tmp_path)
    identity = objects.write(b"original")
    with pytest.raises(ContractError, match="changed"):
        objects.read(identity, expected_bytes=7)


def test_incomplete_temporary_is_invisible_and_failure_is_cleaned(tmp_path, monkeypatch):
    objects = SnapshotObjects(tmp_path)
    raw = b'{"unchanged":"source"}'
    identity = hashlib.sha256(raw).hexdigest()
    seen = []
    def fail_fsync(fd):
        temporary = list(tmp_path.rglob("*.partial"))
        assert len(temporary) == 1
        assert not object_path(tmp_path, identity).exists()
        with pytest.raises(FileNotFoundError):
            objects.read(identity)
        seen.append(True)
        raise OSError("fixture interrupted flush")
    monkeypatch.setattr(os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="interrupted"):
        objects.write(raw)
    assert seen == [True]
    assert list(tmp_path.rglob("*.json")) == []
    assert list(tmp_path.rglob("*.partial")) == []


def test_complete_bytes_are_flushed_before_atomic_publication(tmp_path, monkeypatch):
    objects = SnapshotObjects(tmp_path)
    raw = b'{"cutoff":"frozen","y":[0,1]}'
    identity = hashlib.sha256(raw).hexdigest()
    original_link, original_fsync = os.link, os.fsync
    flushed = []
    def fsync(fd):
        original_fsync(fd)
        flushed.append(True)
    def link(source, target):
        assert flushed == [True]
        assert source.read_bytes() == raw
        assert target == object_path(tmp_path, identity)
        assert not target.exists()
        with pytest.raises(FileNotFoundError):
            objects.read(identity)
        original_link(source, target)
        assert objects.read(identity, len(raw)) == raw
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "link", link)
    assert objects.write(raw) == identity
    assert list(tmp_path.rglob("*.partial")) == []


def test_concurrent_identical_first_publications_keep_one_complete_object(tmp_path, monkeypatch):
    objects = SnapshotObjects(tmp_path)
    raw = b'{"unchanged":"causal-history","X":[[1.0,2.0]],"y":[0]}'
    barrier = threading.Barrier(6)
    original_link = os.link
    def link(source, target):
        barrier.wait(timeout=10)
        return original_link(source, target)
    monkeypatch.setattr(os, "link", link)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: objects.write(raw), range(6)))
    assert len(set(results)) == 1
    assert objects.read(results[0], len(raw)) == raw
    assert len(list(tmp_path.rglob("*.json"))) == 1
    assert list(tmp_path.rglob("*.partial")) == []


def test_racing_corrupt_publication_is_retained_and_rejected(tmp_path, monkeypatch):
    objects = SnapshotObjects(tmp_path)
    raw = b"original"
    identity = hashlib.sha256(raw).hexdigest()
    def conflicting_link(source, target):
        target.write_bytes(b"corrupt!")
        raise FileExistsError("fixture first writer")
    monkeypatch.setattr(os, "link", conflicting_link)
    with pytest.raises(ContractError, match="changed"):
        objects.write(raw)
    assert object_path(tmp_path, identity).read_bytes() == b"corrupt!"
    assert list(tmp_path.rglob("*.partial")) == []
