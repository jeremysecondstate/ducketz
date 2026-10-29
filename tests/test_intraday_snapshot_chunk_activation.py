"""Prospective 1A writer activation; original bytes and existing readers survive."""
from copy import deepcopy
from dataclasses import asdict
import ast
import hashlib
import importlib.util
import inspect
import os
from pathlib import Path
import socket
import subprocess

import pytest

from ml.intraday.prediction import digest, encoded
from ml.intraday.snapshot_objects import SnapshotObjects


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("1A snapshot activation fixtures prohibit network and child processes")
    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, denied)
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, denied)
    class DeniedProcess(subprocess.Popen):
        # Windows asyncio imports a Popen subclass. Keep that import valid
        # while denying every attempted process construction.
        def __init__(self, *args, **kwargs):
            denied(*args, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", DeniedProcess)
    monkeypatch.setattr(os, "system", denied)


def original_payload():
    return {"observations": [{"index": i, "original": "retained-\u03bb-" + "x" * 80}
                              for i in range(1600)],
            "probability": .5001000000000001, "target": "original-not-down"}


def raw(value):
    return encoded(value).encode()


def test_prospective_writer_keeps_old_raw_and_default_reader_decodes_new_object(tmp_path):
    objects = SnapshotObjects(tmp_path / "snapshots")
    payload = original_payload()
    old = objects.write(raw(payload))
    original_path = objects._path(old)
    original_bytes, original_stat = original_path.read_bytes(), original_path.stat()
    writer = SnapshotObjects(objects.root, chunked=True)
    assert writer.write(raw(payload), payload=payload) == old
    assert not writer._manifest_path(old).exists()
    assert original_path.read_bytes() == original_bytes
    assert (original_path.stat().st_ino, original_path.stat().st_mtime_ns) == (
        original_stat.st_ino, original_stat.st_mtime_ns)
    changed = deepcopy(payload)
    changed["observations"][-1]["original"] = "new actual tail"
    identity = writer.write(raw(changed), payload=changed)
    assert writer._manifest_path(identity).is_file() and not writer._path(identity).exists()
    assert SnapshotObjects(objects.root).read(identity, expected_bytes=len(raw(changed))) == raw(changed)
    assert SnapshotObjects(objects.root).read(old) == original_bytes


def test_explicit_1a_wiring_keeps_chunk_option_out_of_other_stores():
    root = Path(__file__).resolve().parents[1]
    sources = [root / "ml/intraday/data_renewal.py", root / "ml/intraday/split_service.py"]
    if not all(path.is_file() for path in sources):
        pytest.skip("Composed independent 1A service constructors are absent")
    all_calls = []
    for path in sources:
        tree = ast.parse(path.read_bytes())
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name)
                 and node.func.id in {"ShadowStore", "ReadySnapshots"}]
        chosen = [node for node in calls if node.func.id == "ReadySnapshots"
                  or any(keyword.arg == "snapshot_directory" for keyword in node.keywords)]
        if not any(any(keyword.arg == "chunked_snapshots" for keyword in node.keywords)
                   for node in chosen):
            pytest.skip("Exact composed 1A writer activation is absent")
        assert len(chosen) == 3
        for node in chosen:
            option = [keyword.value for keyword in node.keywords if keyword.arg == "chunked_snapshots"]
            assert len(option) == 1 and isinstance(option[0], ast.Constant) and option[0].value is True
        assert all(not any(keyword.arg == "chunked_snapshots" for keyword in node.keywords)
                   for node in calls if node not in chosen)
        all_calls.extend(chosen)
    assert len(all_calls) == 6


def local_environment(tmp_path, monkeypatch):
    from ml.intraday.store import ShadowStore
    from ml.intraday.split_handoff import ReadySnapshots
    if ("chunked_snapshots" not in inspect.signature(ShadowStore).parameters
            or "chunked_snapshots" not in inspect.signature(ReadySnapshots).parameters):
        pytest.skip("Exact composed store/handoff chunk adapters are absent")
    fixture_path = Path(__file__).with_name("test_intraday_split_services.py")
    if not fixture_path.is_file():
        pytest.skip("Local composed DataService integration fixture is absent")
    spec = importlib.util.spec_from_file_location("snapshot_chunk_activation_service_fixture", fixture_path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    env = fixture._environment(tmp_path)
    old = env.data_store.freeze_inputs("old-cycle", "AAPL", {"original_inline": "retained"})
    with env.data_store.connect() as db:
        old_row = db.execute("SELECT id,payload FROM snapshots WHERE id=?", (old[0],)).fetchone()
    env.data_store = ShadowStore(env.data_store.path,
        snapshot_directory=env.handoff.root / "snapshots", chunked_snapshots=True)
    env.runner.store = env.data_store
    env.handoff = ReadySnapshots(env.handoff.root, actor="Atlas", symbols=env.symbols,
        clock=env.clock, chunked_snapshots=True)
    env.service.handoff = env.handoff
    freeze = env.data_store.freeze_inputs
    env.original_frozen = {}
    def large_original(cycle, symbol, payload):
        payload = {**payload, "offline_original_observations": original_payload()["observations"]}
        result = freeze(cycle, symbol, payload)
        env.original_frozen[symbol] = result
        return result
    monkeypatch.setattr(env.data_store, "freeze_inputs", large_original)
    env.old_inline, env.old_row = old, old_row
    return env


def test_real_data_service_reuses_chunks_and_default_reader_keeps_original_science(tmp_path, monkeypatch):
    from ml.intraday.split_handoff import ReadySnapshots
    env = local_environment(tmp_path, monkeypatch)
    result = env.service.pass_once()
    assert set(result["prepared_symbols"]) == set(env.symbols)
    reader = ReadySnapshots(env.handoff.root, actor="Atlas", symbols=env.symbols, clock=env.clock)
    records = reader.ready(env.clock(), configuration_hash=digest(asdict(env.config)))
    assert len(records) == len(env.symbols)
    for record in records:
        cycle, payload = reader.load(record)
        identity, original = env.original_frozen[record["symbol"]]
        original_bytes = raw(original)
        assert len(original_bytes) > 64 * 1024
        assert payload == original and record["snapshot_hash"] == identity == digest(original)
        assert record["snapshot_bytes"] == len(original_bytes)
        objects = env.data_store.snapshot_objects
        assert objects._manifest_path(identity).is_file() and not objects._path(identity).exists()
        assert objects.read(identity) == original_bytes
        assert hashlib.sha256(original_bytes).hexdigest() == identity
        assert env.data_store.inputs(record["cycle_id"], record["symbol"]) == (identity, original)
        assert cycle["configuration_hash"] == digest(asdict(env.config))
        assert record["training_cutoff"] == original["cutoff"]
        assert record["feature_order_sha256"] == digest(original["feature_names"])
    with env.data_store.connect() as db:
        assert db.execute("SELECT id,payload FROM snapshots WHERE id=?", (env.old_inline[0],)).fetchone() == env.old_row
    assert env.data_store.inputs("old-cycle", "AAPL") == env.old_inline
    claims = {str(path): path.read_bytes() for path in (env.handoff.root / "claims").rglob("*.json")}
    assert env.service.pass_once()["reused_ready_index"] is True
    assert {str(path): path.read_bytes() for path in (env.handoff.root / "claims").rglob("*.json")} == claims
    assert env.data_store.forecasts() == [] and env.model_store.forecasts() == []


def test_interrupted_chunk_publication_exposes_no_new_binding_or_ready_claim(tmp_path, monkeypatch):
    env = local_environment(tmp_path, monkeypatch)
    link = os.link
    def interrupted(source, target):
        if "manifests" in Path(target).parts:
            raise OSError("offline fixture interrupted manifest publication")
        return link(source, target)
    monkeypatch.setattr(os, "link", interrupted)
    result = env.service.pass_once()
    assert result["prepared_symbols"] == []
    with env.data_store.connect() as db:
        assert db.execute("SELECT id,payload FROM snapshots").fetchall() == [env.old_row]
        assert db.execute("SELECT cycle_id,symbol FROM symbol_inputs").fetchall() == [("old-cycle", "AAPL")]
    with env.handoff.connect() as db:
        assert db.execute("SELECT * FROM ready").fetchall() == []
    assert not list((env.handoff.root / "claims").rglob("*.json"))
    assert not list((env.handoff.root / "snapshots/manifests").rglob("*.json"))
