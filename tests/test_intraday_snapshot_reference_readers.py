"""Offline compatibility for migrated originals; no runtime or provider calls."""

from tests.intraday_shared_storage_support import require_composed_storage
require_composed_storage()
import json
import sqlite3

import pytest

from ml.intraday import snapshot_objects
from ml.intraday.prediction import ContractError, digest, encoded
from ml.intraday.snapshot_objects import SnapshotObjects
from ml.intraday.store import ShadowStore
from tests.test_intraday_readiness import account, inventory, local, pair, report, write_store


def _reference(snapshot, objects):
    raw = encoded(snapshot).encode()
    key = objects.write(raw, payload=snapshot)
    assert key == digest(snapshot)
    return key, encoded({"snapshot_storage": "intraday-input-reference-v1",
                         "sha256": key, "bytes": len(raw)})


@pytest.mark.parametrize("relative", ["shadow.sqlite3", "system-1a/preparation.sqlite3",
    "system-1a/preparation-AAPL.sqlite3", "system-1b/models.sqlite3", "system-1b/models-7.sqlite3"])
@pytest.mark.parametrize("chunked", [False, True])
def test_known_layout_reads_shared_original_but_new_writes_stay_inline(tmp_path, monkeypatch, relative, chunked):
    monkeypatch.setattr(snapshot_objects, "CHUNK_THRESHOLD", 1)
    root = tmp_path / "intraday"
    store = ShadowStore(root / relative)
    snapshot = {"cutoff": "original", "X": [[1, 2], [3, 4]], "probability": .6}
    key, value = store.freeze_inputs("original-cycle", "AAPL", snapshot)
    objects = SnapshotObjects(root / "system-1a/ready/snapshots", chunked=chunked)
    object_key, descriptor = _reference(snapshot, objects)
    assert object_key == key
    with store.connect() as db:
        db.execute("UPDATE snapshots SET payload=? WHERE id=?", (descriptor, key))
    before = inventory(objects.root)
    reopened = ShadowStore(store.path)
    assert reopened.snapshot_objects is None
    assert reopened.inputs("original-cycle", "AAPL") == (key, value)
    assert reopened.freeze_inputs("original-cycle", "AAPL", {"replacement": True}) == (key, value)
    next_snapshot = {"next": "original bytes"}
    next_key, _ = reopened.freeze_inputs("next-cycle", "AAPL", next_snapshot)
    with reopened.connect() as db:
        rows = dict(db.execute("SELECT id,payload FROM snapshots"))
    assert rows[key] == descriptor
    assert rows[next_key] == encoded(next_snapshot)
    assert inventory(objects.root) == before
    assert any(name.replace("\\", "/").startswith("manifests/") for name in before) == chunked


@pytest.mark.parametrize("relative", ["other.sqlite3", "system-1b/models-copy.sqlite3",
    "system-1a/preparation-backup.sqlite3", "archive/shadow.sqlite3"])
def test_other_layouts_require_explicit_reference_binding(tmp_path, relative):
    root = tmp_path / "intraday"
    store = ShadowStore(root / relative)
    snapshot = {"science": "unchanged"}
    key, _ = store.freeze_inputs("cycle", "AAPL", snapshot)
    objects = SnapshotObjects(root / "system-1a/ready/snapshots")
    _, descriptor = _reference(snapshot, objects)
    with store.connect() as db:
        db.execute("UPDATE snapshots SET payload=? WHERE id=?", (descriptor, key))
    with pytest.raises(ContractError, match="unbound"):
        store.inputs("cycle", "AAPL")
    assert ShadowStore(store.path, snapshot_directory=objects.root).inputs("cycle", "AAPL") == (key, snapshot)


def test_pure_decoder_preserves_inline_semantics_and_rejects_invalid_descriptors(tmp_path):
    objects = SnapshotObjects(tmp_path / "objects")
    snapshot = {"snapshot_storage": "scientific-key", "probability": .5}
    key, descriptor = _reference(snapshot, objects)
    assert ShadowStore.read_input_value(key, json.dumps(snapshot, indent=2)) == snapshot
    assert ShadowStore.read_input_value(key, descriptor, snapshot_objects=objects) == snapshot
    for change in ({"bytes": True}, {"sha256": "f" * 64}, {"path": "alternate.json"},
                   {"snapshot_storage": "unknown"}, {"bytes": -1}):
        malformed = {**json.loads(descriptor), **change}
        with pytest.raises(ContractError, match="invalid or unbound"):
            ShadowStore.read_input_value(key, encoded(malformed), snapshot_objects=objects)


@pytest.mark.parametrize("chunked", [False, True])
def test_readiness_uses_exact_references_without_initializing_or_changing_originals(local, monkeypatch, chunked):
    account(local)
    pairs = [pair(symbol="AAPL"), pair(symbol="IONQ")]
    path = write_store(local, pairs)
    original = report(local)["saved_forecasting"]
    monkeypatch.setattr(snapshot_objects, "CHUNK_THRESHOLD", 1)
    objects = SnapshotObjects(local["datastore"] / "intraday/system-1a/ready/snapshots", chunked=chunked)
    with sqlite3.connect(path) as db:
        for _, snapshot in pairs:
            key, descriptor = _reference(snapshot, objects)
            db.execute("UPDATE snapshots SET payload=? WHERE id=?", (descriptor, key))
    before = inventory(local["datastore"])

    def no_initialization(*args, **kwargs):
        raise AssertionError("Readiness must not initialize a writable store")

    monkeypatch.setattr(ShadowStore, "__init__", no_initialization)
    actual = report(local)["saved_forecasting"]
    assert actual == original
    assert actual["status"] == "read"
    assert actual["invalid_bound_data_count"] == 0
    assert inventory(local["datastore"]) == before
    assert any(name.replace("\\", "/").startswith("manifests/") for name in inventory(objects.root)) == chunked


@pytest.mark.parametrize("damage", ["missing", "corrupt", "length", "extra_field"])
def test_readiness_rejects_missing_or_changed_original_without_substitution(local, damage):
    account(local)
    pairs = [pair(symbol="AAPL"), pair(symbol="IONQ")]
    path = write_store(local, pairs)
    objects = SnapshotObjects(local["datastore"] / "intraday/system-1a/ready/snapshots")
    key, descriptor = _reference(pairs[0][1], objects)
    raw_path = objects.root / key[:2] / (key + ".json")
    if damage == "missing":
        raw_path.unlink()
    elif damage == "corrupt":
        raw_path.write_bytes(b"{}")
    else:
        value = json.loads(descriptor)
        if damage == "length":
            value["bytes"] += 1
        else:
            value["path"] = "alternate.json"
        descriptor = encoded(value)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE snapshots SET payload=? WHERE id=?", (descriptor, key))
    before = inventory(local["datastore"])
    actual = report(local)["saved_forecasting"]
    assert actual["status"] == "invalid_evidence"
    assert actual["invalid_bound_data_count"] == 1
    by_symbol = {row["symbol"]: row for row in actual["latest_forecasts"]}
    assert by_symbol["AAPL"]["bound_data"]["status"] == "invalid_recorded_inputs"
    assert by_symbol["IONQ"]["bound_data"]["status"] == "verified_recorded_inputs"
    assert inventory(local["datastore"]) == before
