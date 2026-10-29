"""UI provenance remains an exact, read-only projection after snapshot migration."""

from tests.intraday_shared_storage_support import require_composed_storage
require_composed_storage()
from contextlib import closing
import json
import sqlite3

import pytest

from app.ui import intraday_data
from ml.intraday.prediction import digest, encoded
from ml.intraday.snapshot_objects import SnapshotObjects
from tests.test_intraday_input_provenance import candle, forecast, write_snapshot
from tests.test_intraday_readiness import inventory


def _original(tmp_path, layout, *, database_name="shadow.sqlite3"):
    path = tmp_path / "intraday" / database_name
    path.parent.mkdir(parents=True)
    snapshot = {"X": [["TRAINING_MATRIX_MUST_NOT_BE_DECODED_IN_PYTHON"]] * 3000,
                "candles": [candle(14, 1)]}
    if layout != "outer":
        compact = candle(14, 1)
        if layout == "compact_missing_counts":
            compact.pop("observed_constituents")
            compact.pop("synthetic_constituents")
        snapshot["inference"] = {"snapshot": {"candles": [compact]}}
    key = digest(snapshot)
    write_snapshot(path, snapshot, snapshot_id=key)
    return path, snapshot, forecast(snapshot_hash=key)


def _migrate_fixture(path, snapshot, *, chunked=False, changes=None):
    objects = SnapshotObjects(path.parent / "system-1a/ready/snapshots", chunked=chunked)
    raw = encoded(snapshot).encode()
    key = objects.write(raw, payload=snapshot)
    descriptor = {"snapshot_storage": "intraday-input-reference-v1", "sha256": key, "bytes": len(raw)}
    descriptor.update(changes or {})
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE snapshots SET payload=? WHERE id=?", (encoded(descriptor), key))
    return objects, key


@pytest.mark.parametrize("chunked", [False, True])
@pytest.mark.parametrize("layout", ["outer", "compact", "compact_missing_counts"])
def test_raw_and_chunk_references_keep_exact_legacy_metadata_without_decoding_arrays(tmp_path, monkeypatch, chunked, layout):
    path, snapshot, row = _original(tmp_path, layout)
    original = intraday_data.reference_input_provenance(row, database_path=path)
    assert original["status"] == "known"
    objects, key = _migrate_fixture(path, snapshot, chunked=chunked)
    assert (objects.root / "manifests" / key[:2] / (key + ".json")).exists() == chunked
    before = inventory(tmp_path)
    loads = json.loads
    decoded = []

    def small_only(raw, *args, **kwargs):
        assert "TRAINING_MATRIX_MUST_NOT_BE_DECODED_IN_PYTHON" not in str(raw)
        decoded.append(len(raw))
        return loads(raw, *args, **kwargs)

    monkeypatch.setattr(intraday_data.json, "loads", small_only)
    assert intraday_data.reference_input_provenance(row, database_path=path) == original
    assert decoded and inventory(tmp_path) == before


@pytest.mark.parametrize("damage", ["missing", "corrupt", "bytes", "sha256", "extra"])
def test_broken_reference_is_unknown_without_substituting_other_evidence(tmp_path, damage):
    path, snapshot, row = _original(tmp_path, "outer")
    changes = {"bytes": True} if damage == "bytes" else {"sha256": "a" * 64} if damage == "sha256" else {"path": "alternate.json"} if damage == "extra" else {}
    objects, key = _migrate_fixture(path, snapshot, changes=changes)
    raw_path = objects.root / key[:2] / (key + ".json")
    if damage == "missing":
        raw_path.unlink()
    elif damage == "corrupt":
        raw_path.write_bytes(b"{}")
    before = inventory(tmp_path)
    actual = intraday_data.reference_input_provenance(row, database_path=path)
    assert actual["status"] == "unknown"
    assert actual["observed_constituents"] is actual["synthetic_constituents"] is None
    assert inventory(tmp_path) == before


def test_unrecognized_database_layout_never_guesses_an_object_root(tmp_path, monkeypatch):
    path, snapshot, row = _original(tmp_path, "outer", database_name="archived.sqlite3")
    _migrate_fixture(path, snapshot)

    def no_read(*args, **kwargs):
        raise AssertionError("Unbound UI store must not read a guessed object")

    monkeypatch.setattr(SnapshotObjects, "read", no_read)
    result = intraday_data.reference_input_provenance(row, database_path=path)
    assert result["status"] == "unknown" and "unbound" in result["reason"]


def test_inline_scientific_storage_key_still_uses_original_metadata(tmp_path):
    path = tmp_path / "inputs.sqlite3"
    snapshot = {"snapshot_storage": "intraday-input-reference-v1", "candles": [candle(15, 0)]}
    write_snapshot(path, snapshot)
    actual = intraday_data.reference_input_provenance(forecast(), database_path=path)
    assert actual["status"] == "known" and actual["observed_constituents"] == 15
