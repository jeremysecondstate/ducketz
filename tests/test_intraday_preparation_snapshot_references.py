"""Opt-in 1A references retain original science and unchanged consumer bytes."""
import inspect
import json
from dataclasses import asdict
from pathlib import Path
from contextlib import contextmanager
import sqlite3

import pytest

from ml.intraday.prediction import ContractError, digest, encoded
from ml.intraday.store import ShadowStore

if "snapshot_directory" not in inspect.signature(ShadowStore).parameters:
    pytest.skip("Exact composed 1A snapshot-reference adapters are absent", allow_module_level=True)


def contents(store):
    with store.connect() as db:
        return db.execute("SELECT id,payload FROM snapshots ORDER BY rowid").fetchall()


def test_reference_roundtrip_default_inline_and_existing_rows_untouched(tmp_path):
    path = tmp_path / "preparation.sqlite3"
    original = ShadowStore(path)
    old = original.freeze_inputs("old", "AAPL", {"legacy": 1})
    before = contents(original)
    directory = tmp_path / "ready/snapshots"
    new = ShadowStore(path, snapshot_directory=directory)
    assert new.inputs("old", "AAPL") == old
    assert new.freeze_inputs("old", "AAPL", {"unused": 99}) == old
    assert not directory.exists()
    payload = {"candles": [{"source": "original", "observed_at": "original-clock"}],
               "X": [[.5001000000000001]], "y": [1], "target": "original"}
    identity, frozen = new.freeze_inputs("new", "AAPL", payload)
    assert identity == digest(payload) and frozen == payload
    assert contents(new)[:1] == before
    descriptor = json.loads(contents(new)[1][1])
    assert descriptor == {"snapshot_storage": "intraday-input-reference-v1", "sha256": identity,
                          "bytes": len(encoded(payload).encode())}
    blob = directory / identity[:2] / (identity + ".json")
    assert blob.read_bytes() == encoded(payload).encode()
    reopened = ShadowStore(path, snapshot_directory=directory)
    assert reopened.inputs("new", "AAPL") == (identity, payload)
    assert reopened.freeze_inputs("new", "AAPL", {"changed": 1}) == (identity, payload)
    assert len(list(directory.rglob("*.json"))) == 1
    default = ShadowStore(tmp_path / "system-1b/models.sqlite3")
    assert default.freeze_inputs("b", "AAPL", payload) == (identity, payload)
    assert contents(default) == [(identity, encoded(payload))]


def test_inline_scientific_keys_never_become_storage_instructions(tmp_path):
    for index, payload in enumerate(({"snapshot_storage": "scientific-original", "X": [1]},
            {"snapshot_storage": "intraday-input-reference-v1", "sha256": "a" * 64, "bytes": 1})):
        path = tmp_path / (str(index) + ".sqlite3")
        original = ShadowStore(path)
        frozen = original.freeze_inputs("cycle", "AAPL", payload)
        assert ShadowStore(path).inputs("cycle", "AAPL") == frozen
        assert ShadowStore(path, snapshot_directory=tmp_path / "objects").inputs("cycle", "AAPL") == frozen


def test_object_failure_cannot_commit_snapshot_or_symbol_binding(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "prep.sqlite3", snapshot_directory=tmp_path / "objects")
    def failed(raw):
        raise OSError("fixture disk write failed")
    monkeypatch.setattr(store.snapshot_objects, "write", failed)
    with pytest.raises(OSError, match="disk write"):
        store.freeze_inputs("cycle", "AAPL", {"input": 1})
    assert contents(store) == []
    assert store.inputs("cycle", "AAPL") is None


def test_db_failure_leaves_only_complete_unadvertised_object_and_retry_recovers(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "prep.sqlite3", snapshot_directory=tmp_path / "objects")
    original = store.connect
    calls = []
    @contextmanager
    def busy():
        calls.append(1)
        if len(calls) > 1:
            raise sqlite3.OperationalError("fixture database busy")
        with original() as db:
            yield db
    monkeypatch.setattr(store, "connect", busy)
    with pytest.raises(sqlite3.OperationalError, match="busy"):
        store.freeze_inputs("cycle", "AAPL", {"input": 1})
    monkeypatch.setattr(store, "connect", original)
    assert contents(store) == [] and store.inputs("cycle", "AAPL") is None
    identity = digest({"input": 1})
    assert store.snapshot_objects.read(identity) == encoded({"input": 1}).encode()
    assert store.freeze_inputs("cycle", "AAPL", {"input": 1}) == (identity, {"input": 1})


@pytest.mark.parametrize("damage", ["missing", "bytes", "length", "identity", "unbound", "extra"])
def test_reference_damage_is_not_reconstructed_from_current_data(tmp_path, damage):
    path = tmp_path / "prep.sqlite3"
    directory = tmp_path / "objects"
    store = ShadowStore(path, snapshot_directory=directory)
    key, _ = store.freeze_inputs("cycle", "AAPL", {"input": 1})
    blob = directory / key[:2] / (key + ".json")
    if damage == "missing":
        blob.unlink()
    elif damage == "bytes":
        blob.write_bytes(blob.read_bytes()[:-1] + b" ")
    elif damage in {"length", "identity", "extra"}:
        value = json.loads(contents(store)[0][1])
        if damage == "length": value["bytes"] += 1
        if damage == "identity": value["sha256"] = "0" * 64
        if damage == "extra": value["path"] = "untrusted"
        with store.connect() as db:
            db.execute("UPDATE snapshots SET payload=? WHERE id=?", (encoded(value), key))
    else:
        store = ShadowStore(path)
    with pytest.raises((ContractError, FileNotFoundError)):
        store.inputs("cycle", "AAPL")
    with pytest.raises((ContractError, FileNotFoundError)):
        store.freeze_inputs("cycle", "AAPL", {"input": 2})


def test_legacy_common_to_lane_reuses_existing_original_without_rewriting(tmp_path):
    shared = ShadowStore(tmp_path / "common.sqlite3")
    key, payload = shared.freeze_inputs("cycle", "AAPL", {"original": 1})
    before = contents(shared)
    lane = ShadowStore(tmp_path / "lane.sqlite3", snapshot_directory=tmp_path / "ready/snapshots")
    assert lane.freeze_inputs("cycle", "AAPL", shared.inputs("cycle", "AAPL")[1]) == (key, payload)
    assert contents(shared) == before


def test_real_data_service_and_unchanged_ready_consumer_reuse_exact_file(tmp_path, monkeypatch):
    fixture_path = Path(__file__).with_name("test_intraday_split_services.py")
    if not fixture_path.is_file():
        pytest.skip("Local composed DataService fixture absent")
    import importlib.util
    spec = importlib.util.spec_from_file_location("snapshot_reference_service_fixture", fixture_path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    env = fixture._environment(tmp_path)
    env.data_store = ShadowStore(env.data_store.path, snapshot_directory=env.handoff.root / "snapshots")
    env.runner.store = env.data_store
    before_write = env.handoff._write
    reused = []
    def checked(kind, raw, bound):
        if kind == "snapshots":
            import hashlib
            identity = hashlib.sha256(raw).hexdigest()
            path = env.handoff._path(kind, identity)
            assert path.is_file() and path.read_bytes() == raw
            previous = path.stat()
            result = before_write(kind, raw, bound)
            after = path.stat()
            assert (previous.st_ino, previous.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)
            reused.append(identity)
            return result
        return before_write(kind, raw, bound)
    monkeypatch.setattr(env.handoff, "_write", checked)
    result = env.service.pass_once()
    assert set(result["prepared_symbols"]) == set(env.symbols)
    assert len(reused) == len(env.symbols)
    records = env.handoff.ready(env.clock(), configuration_hash=digest(asdict(env.config)))
    assert len(records) == len(env.symbols)
    for record in records:
        cycle, snapshot = env.handoff.load(record)
        assert env.data_store.inputs(record["cycle_id"], record["symbol"]) == (record["snapshot_hash"], snapshot)
    assert all(json.loads(row[1])["snapshot_storage"] == "intraday-input-reference-v1" for row in contents(env.data_store))
    assert len(list((env.handoff.root / "snapshots").rglob("*.json"))) == len(env.symbols)
