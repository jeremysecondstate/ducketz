"""Prospective lossless snapshot fixtures; no live data, provider or trader."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import json
import os
import socket
import subprocess
import threading

import pytest

from ml.intraday.prediction import ContractError, TARGET_VERSION, digest, encoded
from ml.intraday.snapshot_objects import SnapshotObjects
import ml.intraday.snapshot_objects as codec

if "chunked" not in inspect.signature(SnapshotObjects).parameters:
    pytest.skip("Exact composed snapshot chunk codec is absent", allow_module_level=True)


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Snapshot chunk fixtures prohibit providers, network and child processes")
    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, denied)
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, denied)
    monkeypatch.setattr(subprocess, "Popen", denied)
    monkeypatch.setattr(os, "system", denied)


@pytest.fixture
def payload():
    return {
        "candles": [{"index": i, "price": .5001000000000001,
                     "original": "original-observed-" + str(i),
                     "unicode": "\u03bb\u96ea" + "frozen" * 14}
                    for i in range(1030)],
        "X": [[-0.0, 1e-100, 1.0], [2.0, .49989999999999996, 3.0]],
        "y": [0, 1], "empty_array": [], "empty_object": {},
        "nested": {"unicode": "\u03bb\U0001f680", "null": None, "bool": False,
                   "integer": 2 ** 80, "float": 2.0, "escaped": "\n\t\\\""},
        "original_probability": .5001000000000001, "target": "original-not-down",
    }


def raw(value):
    return encoded(value).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def manifest(objects, identity):
    return json.loads(objects._manifest_path(identity).read_bytes())


def rewrite_manifest(objects, identity, value):
    objects._manifest_path(identity).write_bytes(raw(value))


def chunked(tmp_path):
    return SnapshotObjects(tmp_path / "objects", chunked=True)


def test_exact_original_bytes_hash_length_and_reopened_reader(tmp_path, payload):
    objects = chunked(tmp_path)
    original = raw(payload)
    identity = objects.write(original, payload=payload)
    assert identity == digest(payload) == sha(original)
    assert not objects._path(identity).exists()
    descriptor = manifest(objects, identity)
    assert descriptor["schema"] == codec.MANIFEST_VERSION
    assert descriptor["sha256"] == identity and descriptor["bytes"] == len(original)
    assert objects.read(identity, expected_bytes=len(original)) == original
    assert SnapshotObjects(objects.root).read(identity) == original
    assert json.loads(objects.read(identity)) == payload
    assert not list(objects.root.rglob("*.partial"))


def test_original_512_row_candle_chunks_reused_when_tail_changes(tmp_path, payload):
    objects = chunked(tmp_path)
    first = objects.write(raw(payload), payload=payload)
    second_payload = deepcopy(payload)
    second_payload["candles"][-1]["price"] = .49989999999999996
    second_payload["candles"].append({"index": 1030, "price": 1.0, "observed": "new-original"})
    first_chunk = raw(payload["candles"][:codec.CHUNK_ROWS])[1:-1]
    chunk_id = sha(first_chunk)
    path = objects._chunk_path(chunk_id)
    before = path.stat()
    second = objects.write(raw(second_payload), payload=second_payload)
    after = path.stat()
    assert first != second
    for identity in (first, second):
        assert {"sha256": chunk_id, "bytes": len(first_chunk)} in manifest(objects, identity)["chunks"]
    assert path.read_bytes() == first_chunk
    assert (before.st_ino, before.st_size, before.st_mtime_ns) == (after.st_ino, after.st_size, after.st_mtime_ns)
    assert objects.read(first) == raw(payload)
    assert objects.read(second) == raw(second_payload)


@pytest.mark.parametrize("hint", [None, "wrong", [], {"unsupported": True}])
def test_missing_or_inexact_hint_retains_original_raw_layout(tmp_path, payload, hint):
    objects = chunked(tmp_path)
    original = raw(payload)
    identity = objects.write(original, payload=hint)
    assert objects._path(identity).read_bytes() == original
    assert not objects._manifest_path(identity).exists()
    assert objects.read(identity) == original


def test_default_writer_never_enables_chunk_format_from_hint(tmp_path, payload):
    objects = SnapshotObjects(tmp_path / "objects")
    original = raw(payload)
    identity = objects.write(original, payload=payload)
    assert objects._path(identity).read_bytes() == original
    assert not objects._manifest_path(identity).exists()


def test_noncanonical_original_never_reencoded_or_rewritten(tmp_path, payload):
    objects = chunked(tmp_path)
    original = json.dumps(payload, sort_keys=False, ensure_ascii=True, indent=1).encode()
    assert original != raw(payload)
    identity = objects.write(original, payload=payload)
    assert identity == sha(original)
    assert objects._path(identity).read_bytes() == original
    assert not objects._manifest_path(identity).exists()
    assert objects.read(identity) == original


@pytest.mark.parametrize("value", [{}, {"candles": []}, {"x": [1.0], "y": None}])
def test_small_canonical_original_retains_legacy_layout(tmp_path, value):
    objects = chunked(tmp_path)
    original = raw(value)
    identity = objects.write(original, payload=value)
    assert objects._path(identity).read_bytes() == original
    assert not objects._manifest_path(identity).exists()


def test_legacy_raw_file_always_preserved_and_unused_manifest_ignored(tmp_path, payload):
    objects = chunked(tmp_path)
    original = raw(payload)
    identity = sha(original)
    path = objects._path(identity)
    path.parent.mkdir(parents=True)
    path.write_bytes(original)
    before = path.stat()
    assert objects.write(original, payload=payload) == identity
    unused = objects._manifest_path(identity)
    unused.parent.mkdir(parents=True)
    unused.write_bytes(b"unused-invalid-manifest")
    assert objects.read(identity) == original
    after = path.stat()
    assert (before.st_ino, before.st_size, before.st_mtime_ns) == (after.st_ino, after.st_size, after.st_mtime_ns)


def test_corrupt_legacy_raw_never_falls_back_to_valid_manifest(tmp_path, payload):
    objects = chunked(tmp_path)
    identity = objects.write(raw(payload), payload=payload)
    legacy = objects._path(identity)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"corrupt-original")
    with pytest.raises(ContractError):
        objects.read(identity)
    with pytest.raises(ContractError):
        objects.write(raw(payload), payload=payload)
    assert legacy.read_bytes() == b"corrupt-original"


@pytest.mark.parametrize("damage", ["schema", "identity", "bytes", "bool_bytes", "extra", "empty_chunks",
                                    "reordered", "chunk_extra", "chunk_identity", "chunk_bytes",
                                    "chunk_bool", "chunk_oversize", "count_oversize"])
def test_manifest_damage_rejected_without_rebuilding_inputs(tmp_path, payload, damage):
    objects = chunked(tmp_path)
    identity = objects.write(raw(payload), payload=payload)
    value = manifest(objects, identity)
    if damage == "schema": value["schema"] = "future-unreviewed"
    if damage == "identity": value["sha256"] = "0" * 64
    if damage == "bytes": value["bytes"] += 1
    if damage == "bool_bytes": value["bytes"] = True
    if damage == "extra": value["path"] = "untrusted"
    if damage == "empty_chunks": value["chunks"] = []
    if damage == "reordered": value["chunks"] = list(reversed(value["chunks"]))
    if damage == "chunk_extra": value["chunks"][0]["path"] = "untrusted"
    if damage == "chunk_identity": value["chunks"][0]["sha256"] = "../escape"
    if damage == "chunk_bytes": value["chunks"][0]["bytes"] += 1
    if damage == "chunk_bool": value["chunks"][0]["bytes"] = True
    if damage == "chunk_oversize": value["chunks"][0]["bytes"] = codec.MAX_CHUNK_BYTES + 1
    if damage == "count_oversize": value["chunks"] = [value["chunks"][0]] * (codec.MAX_CHUNKS + 1)
    rewrite_manifest(objects, identity, value)
    damaged = objects._manifest_path(identity).read_bytes()
    with pytest.raises((ContractError, FileNotFoundError)):
        objects.read(identity)
    with pytest.raises((ContractError, FileNotFoundError)):
        objects.write(raw(payload), payload=payload)
    assert objects._manifest_path(identity).read_bytes() == damaged


@pytest.mark.parametrize("damage", ["missing", "changed", "truncated", "oversized"])
def test_chunk_damage_fails_original_identity_and_does_not_replace_evidence(tmp_path, payload, damage):
    objects = chunked(tmp_path)
    identity = objects.write(raw(payload), payload=payload)
    descriptor = max(manifest(objects, identity)["chunks"], key=lambda part: part["bytes"])
    path = objects._chunk_path(descriptor["sha256"])
    original = path.read_bytes()
    if damage == "missing": path.unlink()
    if damage == "changed": path.write_bytes(b"X" + original[1:])
    if damage == "truncated": path.write_bytes(original[:-1])
    if damage == "oversized": path.write_bytes(b"X" * (codec.MAX_CHUNK_BYTES + 1))
    before = path.read_bytes() if path.exists() else None
    with pytest.raises((ContractError, FileNotFoundError)):
        objects.read(identity)
    with pytest.raises((ContractError, FileNotFoundError)):
        objects.write(raw(payload), payload=payload)
    assert (path.read_bytes() if path.exists() else None) == before


def test_oversized_or_invalid_manifest_is_bounded(tmp_path, payload):
    objects = chunked(tmp_path)
    identity = objects.write(raw(payload), payload=payload)
    for original in (b"not-json", b"x" * (codec.MAX_MANIFEST_BYTES + 1)):
        objects._manifest_path(identity).write_bytes(original)
        with pytest.raises(ContractError):
            objects.read(identity)


def test_expected_original_length_and_total_resource_bound_checked(tmp_path, payload):
    objects = chunked(tmp_path)
    original = raw(payload)
    identity = objects.write(original, payload=payload)
    with pytest.raises(ContractError):
        objects.read(identity, expected_bytes=len(original) - 1)
    bounded = SnapshotObjects(objects.root, max_bytes=len(original) - 1, chunked=True)
    with pytest.raises(ContractError):
        bounded.read(identity)
    with pytest.raises(ContractError):
        bounded.write(original, payload=payload)


def test_large_single_value_splits_bounded_chunks_and_reconstructs_exactly(tmp_path):
    objects = chunked(tmp_path)
    payload = {"nested": {"original": "x" * (codec.MAX_CHUNK_BYTES + 201)}, "empty": []}
    original = raw(payload)
    identity = objects.write(original, payload=payload)
    assert all(part["bytes"] <= codec.MAX_CHUNK_BYTES for part in manifest(objects, identity)["chunks"])
    assert objects.read(identity, len(original)) == original


def test_chunk_flush_failure_never_publishes_manifest_and_retry_recovers(tmp_path, payload, monkeypatch):
    objects = chunked(tmp_path)
    original = raw(payload)
    identity = sha(original)
    fsync = os.fsync
    def interrupted(fd):
        assert not objects._manifest_path(identity).exists()
        raise OSError("fixture interrupted chunk flush")
    monkeypatch.setattr(os, "fsync", interrupted)
    with pytest.raises(OSError, match="chunk flush"):
        objects.write(original, payload=payload)
    assert not objects._manifest_path(identity).exists()
    assert not list(objects.root.rglob("*.partial"))
    monkeypatch.setattr(os, "fsync", fsync)
    assert objects.write(original, payload=payload) == identity
    assert objects.read(identity) == original


def test_all_chunks_complete_and_flushed_before_manifest_publication(tmp_path, payload, monkeypatch):
    objects = chunked(tmp_path)
    original = raw(payload)
    identity = sha(original)
    fsync, link = os.fsync, os.link
    flushed, published = set(), []
    def checked_fsync(fd):
        fsync(fd)
        flushed.add(fd)
    def checked_link(source, target):
        assert flushed
        if target == objects._manifest_path(identity):
            value = json.loads(source.read_bytes())
            for part in value["chunks"]:
                data = objects._chunk_path(part["sha256"]).read_bytes()
                assert sha(data) == part["sha256"] and len(data) == part["bytes"]
            with pytest.raises(FileNotFoundError):
                objects.read(identity)
            published.append(True)
        link(source, target)
        flushed.clear()
    monkeypatch.setattr(os, "fsync", checked_fsync)
    monkeypatch.setattr(os, "link", checked_link)
    assert objects.write(original, payload=payload) == identity
    assert published == [True]
    assert objects.read(identity) == original


def test_manifest_publication_failure_leaves_unadvertised_complete_chunks(tmp_path, payload, monkeypatch):
    objects = chunked(tmp_path)
    original = raw(payload)
    identity = sha(original)
    link = os.link
    def interrupted(source, target):
        if target == objects._manifest_path(identity):
            raise OSError("fixture interrupted manifest publication")
        return link(source, target)
    monkeypatch.setattr(os, "link", interrupted)
    with pytest.raises(OSError, match="manifest publication"):
        objects.write(original, payload=payload)
    assert not objects._manifest_path(identity).exists()
    with pytest.raises(FileNotFoundError):
        objects.read(identity)
    assert list((objects.root / "chunks").rglob("*.bin"))
    assert not list(objects.root.rglob("*.partial"))
    monkeypatch.setattr(os, "link", link)
    assert objects.write(original, payload=payload) == identity
    assert objects.read(identity) == original


def test_concurrent_identical_manifests_keep_first_complete_publication(tmp_path, payload, monkeypatch):
    objects = chunked(tmp_path)
    original = raw(payload)
    identity = sha(original)
    barrier, link = threading.Barrier(4), os.link
    def racing(source, target):
        if target == objects._manifest_path(identity):
            barrier.wait(timeout=20)
        return link(source, target)
    monkeypatch.setattr(os, "link", racing)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: objects.write(original, payload=payload), range(4)))
    assert results == [identity] * 4
    assert objects.read(identity) == original
    assert len(list((objects.root / "manifests").rglob("*.json"))) == 1
    assert not list(objects.root.rglob("*.partial"))


def test_racing_equivalent_hints_preserve_first_manifest_and_original_bytes(tmp_path, payload, monkeypatch):
    objects = chunked(tmp_path)
    original = raw(payload)
    identity = sha(original)
    alternate = deepcopy(payload)
    alternate["candles"] = tuple(alternate["candles"])
    assert raw(alternate) == original
    barrier, link = threading.Barrier(4), os.link
    def racing(source, target):
        if target == objects._manifest_path(identity):
            barrier.wait(timeout=20)
        return link(source, target)
    monkeypatch.setattr(os, "link", racing)
    hints = (payload, alternate, payload, alternate)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda hint: objects.write(original, payload=hint), hints))
    assert results == [identity] * 4
    first = objects._manifest_path(identity).read_bytes()
    before = objects._manifest_path(identity).stat()
    assert objects.read(identity) == original
    for hint in hints:
        assert objects.write(original, payload=hint) == identity
    after = objects._manifest_path(identity).stat()
    assert objects._manifest_path(identity).read_bytes() == first
    assert (before.st_ino, before.st_size, before.st_mtime_ns) == (after.st_ino, after.st_size, after.st_mtime_ns)
    assert not list(objects.root.rglob("*.partial"))


def test_racing_corrupt_manifest_is_retained_and_rejected(tmp_path, payload, monkeypatch):
    objects = chunked(tmp_path)
    original = raw(payload)
    identity = sha(original)
    link = os.link
    def conflicting(source, target):
        if target == objects._manifest_path(identity):
            target.write_bytes(b"fixture first corrupt manifest")
            raise FileExistsError("fixture first writer")
        return link(source, target)
    monkeypatch.setattr(os, "link", conflicting)
    with pytest.raises(ContractError):
        objects.write(original, payload=payload)
    assert objects._manifest_path(identity).read_bytes() == b"fixture first corrupt manifest"
    assert not list(objects.root.rglob("*.partial"))


def test_opted_in_1a_store_and_default_1b_retain_exact_original_inputs(tmp_path, payload):
    from ml.intraday.store import ShadowStore
    if "chunked_snapshots" not in inspect.signature(ShadowStore).parameters:
        pytest.skip("Local composed ShadowStore chunk opt-in is absent")
    directory = tmp_path / "ready/snapshots"
    store = ShadowStore(tmp_path / "preparation.sqlite3", snapshot_directory=directory, chunked_snapshots=True)
    identity, frozen = store.freeze_inputs("cycle", "AAPL", payload)
    assert frozen == payload and identity == digest(payload)
    assert store.snapshot_objects._manifest_path(identity).exists()
    assert store.inputs("cycle", "AAPL") == (identity, payload)
    assert store.freeze_inputs("cycle", "AAPL", {"changed": True}) == (identity, payload)
    with store.connect() as db:
        descriptor = json.loads(db.execute("SELECT payload FROM snapshots WHERE id=?", (identity,)).fetchone()[0])
    assert descriptor == {"snapshot_storage": "intraday-input-reference-v1", "sha256": identity, "bytes": len(raw(payload))}
    decoder = ShadowStore(store.path, snapshot_directory=directory)
    assert decoder.inputs("cycle", "AAPL") == (identity, payload)
    original = ShadowStore(tmp_path / "system-1b/science.sqlite3")
    assert original.freeze_inputs("cycle", "AAPL", payload) == (identity, payload)
    with original.connect() as db:
        assert db.execute("SELECT payload FROM snapshots").fetchall() == [(encoded(payload),)]


def ready_fixture(tmp_path, payload):
    from ml.intraday.split_handoff import ReadySnapshots
    if "chunked_snapshots" not in inspect.signature(ReadySnapshots).parameters:
        pytest.skip("Local composed ReadySnapshots chunk opt-in is absent")
    reference = datetime(2026, 10, 28, 14, 0, tzinfo=timezone.utc)
    cutoff = reference + timedelta(minutes=16)
    configuration = {"models": ["frozen-original"], "publication_offset_seconds": 30}
    cycle = {"pc": "Atlas", "symbols": ["AAPL"], "mode": "live", "experiment": "offline-original",
             "reference_at": reference.isoformat(), "configuration": configuration,
             "configuration_hash": digest(configuration), "started_at": cutoff.isoformat()}
    cycle_id = digest({key: cycle[key] for key in ("pc", "reference_at", "configuration_hash", "mode", "experiment")})
    snapshot = deepcopy(payload)
    snapshot.update(cutoff=cutoff.isoformat(), feature_names=["a", "b", "c"],
        inference={"symbol": "AAPL", "reference_at": reference.isoformat(), "availability_kind": "actual",
                   "information_available_at": (reference + timedelta(minutes=15)).isoformat()},
        inference_X=[[1.0, 2.0, 3.0]], manifest={"target_version": TARGET_VERSION},
        label_available_at=[(reference - timedelta(minutes=1)).isoformat()] * 2,
        reference_at=[(reference - timedelta(hours=1)).isoformat()] * 2,
        target_close_at=[(reference - timedelta(minutes=1)).isoformat()] * 2)
    for candle in snapshot["candles"]:
        candle.update(symbol="AAPL", opened_at=reference.isoformat(), available_at=cutoff.isoformat())
    ready = ReadySnapshots(tmp_path / "ready", actor="Atlas", symbols=("AAPL",), clock=lambda: cutoff,
                           chunked_snapshots=True)
    return ready, cycle_id, cycle, snapshot, cutoff


def test_ready_publication_and_default_decoder_preserve_science_and_first_claim(tmp_path, payload):
    from ml.intraday.split_handoff import ReadySnapshots
    ready, cycle_id, cycle, snapshot, cutoff = ready_fixture(tmp_path, payload)
    record = ready.publish(cycle_id, "AAPL", cycle, snapshot)
    original = raw(snapshot)
    objects = SnapshotObjects(ready.root / "snapshots")
    assert record["snapshot_hash"] == digest(snapshot) and record["snapshot_bytes"] == len(original)
    assert objects._manifest_path(record["snapshot_hash"]).exists()
    decoder = ReadySnapshots(ready.root, actor="Atlas", symbols=("AAPL",), clock=lambda: cutoff)
    loaded_cycle, loaded_snapshot = decoder.load(record)
    assert loaded_cycle == cycle and loaded_snapshot == snapshot
    claims = {str(p): p.read_bytes() for p in (ready.root / "claims").rglob("*.json")}
    assert ready.publish(cycle_id, "AAPL", cycle, snapshot) == record
    changed = deepcopy(snapshot)
    changed["original_probability"] = .49989999999999996
    with pytest.raises(ContractError):
        ready.publish(cycle_id, "AAPL", cycle, changed)
    assert {str(p): p.read_bytes() for p in (ready.root / "claims").rglob("*.json")} == claims
    assert decoder.load(record) == (cycle, snapshot)


def test_incomplete_chunked_object_never_becomes_ready_pointer_or_claim(tmp_path, payload, monkeypatch):
    ready, cycle_id, cycle, snapshot, _ = ready_fixture(tmp_path, payload)
    link = os.link
    def interrupted(source, target):
        if "manifests" in target.parts:
            raise OSError("fixture incomplete snapshot manifest")
        return link(source, target)
    monkeypatch.setattr(os, "link", interrupted)
    with pytest.raises(OSError, match="snapshot manifest"):
        ready.publish(cycle_id, "AAPL", cycle, snapshot)
    with ready.connect() as db:
        assert db.execute("SELECT * FROM ready").fetchall() == []
    assert not list((ready.root / "claims").rglob("*.json"))
