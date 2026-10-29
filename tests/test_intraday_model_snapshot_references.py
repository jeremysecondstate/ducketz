"""Offline 1B reference integration with exact 1A bytes and fixture predictions."""

from tests.intraday_shared_storage_support import require_composed_storage
require_composed_storage()
from dataclasses import asdict
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from tests.intraday_split_package_support import require_split_integration
require_split_integration(__file__, files=("tests/test_intraday_split_services.py",),
    interfaces=(("ml/intraday/runtime.py", "ShadowRunner", "prepare_cycle", ()),
                ("ml/intraday/store.py", "ShadowStore", "__init__", ("snapshot_directory",))))

from ml.intraday import snapshot_objects, split_model
from ml.intraday.prediction import ContractError, digest, encoded
from ml.intraday.split_handoff import ReadySnapshots
from ml.intraday.store import ShadowStore
from tests.test_intraday_split_services import _environment, no_network


def _producer(tmp_path, *, chunked=False):
    env = _environment(tmp_path)
    env.root = tmp_path / "datastore"
    ready = env.root / "intraday/system-1a/ready"
    env.handoff = ReadySnapshots(ready, actor="Atlas", symbols=env.symbols,
        clock=env.clock, chunked_snapshots=chunked)
    env.data_store = ShadowStore(ready.parent / "preparation.sqlite3",
        snapshot_directory=ready / "snapshots", chunked_snapshots=chunked)
    env.model_store = ShadowStore(env.root / "intraday/shadow.sqlite3")
    env.runner.store = env.data_store
    env.service.handoff = env.handoff
    return env


def _consume(env, **options):
    return split_model.model_pass(store=env.model_store, queue=env.queue,
        session=env.session, config=env.config, handoff=env.handoff,
        clock=env.clock, runner_factory=env.runner_factory, **options)


def _objects(directory):
    return {p.relative_to(directory).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in directory.rglob("*") if p.is_file()}


@pytest.mark.parametrize("chunked", [False, True])
def test_default_model_store_reuses_original_raw_or_chunked_objects(tmp_path, monkeypatch, chunked):
    if chunked:
        monkeypatch.setattr(snapshot_objects, "CHUNK_THRESHOLD", 1)
    env = _producer(tmp_path, chunked=chunked)
    assert env.service.pass_once()["status"] == "prepared"
    directory = env.handoff.root / "snapshots"
    before = _objects(directory)
    if chunked:
        assert any(name.startswith("manifests/") for name in before)
    model_path = env.root / "intraday/system-1b/models.sqlite3"
    legacy = ShadowStore(model_path)
    legacy_frozen = legacy.freeze_inputs("legacy-cycle", "AAPL", {"original": "old-inline"})
    with legacy.connect() as db:
        old_row = db.execute("SELECT id,payload FROM snapshots").fetchone()

    result = _consume(env)
    assert result["status"] == "PUBLISHED" and len(env.outputs) == len(env.symbols)
    model = ShadowStore(model_path, snapshot_directory=directory)
    assert model.inputs("legacy-cycle", "AAPL") == legacy_frozen
    with model.connect() as db:
        assert db.execute("SELECT id,payload FROM snapshots WHERE id=?", (old_row[0],)).fetchone() == old_row
    records = {row["symbol"]: row for row in env.handoff.ready(env.clock(),
        configuration_hash=digest(asdict(env.config)))}
    for forecast in env.outputs:
        record = records[forecast["symbol"]]
        _, original = env.handoff.load(record)
        key = record["snapshot_hash"]
        assert forecast["snapshot_hash"] == key
        assert forecast["probability_not_down"] == .6
        assert forecast["training_cutoff"] == original["cutoff"]
        assert forecast["target_close"] == record["target_close"]
        assert model.inputs(forecast["cycle_id"], forecast["symbol"]) == (key, original)
        with model.connect() as db:
            raw = db.execute("SELECT payload FROM snapshots WHERE id=?", (key,)).fetchone()[0]
        assert json.loads(raw) == {"snapshot_storage": "intraday-input-reference-v1",
                                  "sha256": key, "bytes": len(encoded(original).encode())}
        assert len(raw) < 200
        assert env.model_store.inputs(forecast["cycle_id"], forecast["symbol"]) is None
    assert _objects(directory) == before
    _consume(env, state=result["state"])
    assert len(env.outputs) == len(env.symbols) and _objects(directory) == before


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_damaged_shared_original_does_not_fit_or_reconstruct_that_symbol(tmp_path, damage):
    env = _producer(tmp_path)
    env.service.pass_once()
    record = env.handoff.ready(env.clock(), configuration_hash=digest(asdict(env.config)))[0]
    path = env.handoff._path("snapshots", record["snapshot_hash"])
    original = path.read_bytes()
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(original[:-1] + b" ")
    result = _consume(env)
    assert result["status"] == "PUBLISHED"
    assert record["symbol"] not in result["symbols"]
    assert all(forecast["symbol"] != record["symbol"] for forecast in env.outputs)
    assert (not path.exists()) if damage == "missing" else path.read_bytes() == original[:-1] + b" "
    model = ShadowStore(env.root / "intraday/system-1b/models.sqlite3",
        snapshot_directory=env.handoff.root / "snapshots")
    with model.connect() as db:
        assert db.execute("SELECT 1 FROM snapshots WHERE id=?", (record["snapshot_hash"],)).fetchone() is None


class Checkpoint(BaseException):
    pass


@pytest.mark.parametrize("lane_index", [0, 1])
def test_renewal_lanes_share_1a_objects_and_keep_exact_partition_and_checkpoint(tmp_path, monkeypatch, lane_index):
    env = _producer(tmp_path)
    env.service.pass_once()
    record = next(row for row in env.handoff.ready(env.clock(),
        configuration_hash=digest(asdict(env.config))) if row["symbol"] == env.symbols[lane_index])
    _, original = env.handoff.load(record)
    directory = env.handoff.root / "snapshots"
    before = _objects(directory)
    calls, checkpoints = [], []

    def model_pass(**kwargs):
        model = kwargs["science_store"]
        assert model.path == env.root / f"intraday/system-1b/models-{lane_index}.sqlite3"
        assert model.snapshot_objects.root == directory.resolve()
        assert kwargs["assigned_symbols"] == (env.symbols[lane_index],)
        assert kwargs["max_symbols"] == 1
        assert model.freeze_inputs("renewal-cycle", record["symbol"], original) == (record["snapshot_hash"], original)
        with model.connect() as db:
            raw = db.execute("SELECT payload FROM snapshots WHERE id=?", (record["snapshot_hash"],)).fetchone()[0]
        assert json.loads(raw)["snapshot_storage"] == "intraday-input-reference-v1" and len(raw) < 200
        calls.append(1)
        return {"status": "PUBLISHED", "state": {"original_cursor": 1}}

    def checkpoint(state, due):
        checkpoints.append((state, due))
        raise Checkpoint()

    renewal = SimpleNamespace(session=env.session, request={"role": f"forecast-{lane_index}"},
        state={}, stopped=lambda: False, checkpoint=checkpoint)
    monkeypatch.setattr(split_model, "model_pass", model_pass)
    with pytest.raises(Checkpoint):
        split_model.renewal_model_pass(root=env.root, store=env.model_store, queue=env.queue,
            renewal=renewal, config=env.config, clock=env.clock, lane_index=lane_index, lane_count=2)
    assert calls == [1]
    assert checkpoints == [({"original_cursor": 1}, (env.now + timedelta(seconds=1)).isoformat())]
    assert _objects(directory) == before


def test_existing_model_reference_fails_if_original_is_lost(tmp_path):
    env = _producer(tmp_path)
    env.service.pass_once()
    result = _consume(env)
    forecast = env.outputs[0]
    key = forecast["snapshot_hash"]
    model = ShadowStore(env.root / "intraday/system-1b/models.sqlite3",
        snapshot_directory=env.handoff.root / "snapshots")
    env.handoff._path("snapshots", key).unlink()
    with pytest.raises((ContractError, FileNotFoundError)):
        model.inputs(result["cycle_id"], forecast["symbol"])
    with pytest.raises((ContractError, FileNotFoundError)):
        model.freeze_inputs(result["cycle_id"], forecast["symbol"], {"replacement": "forbidden"})
