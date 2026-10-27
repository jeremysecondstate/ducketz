"""Offline receipt recovery must preserve projections without redundant queue writes."""
from dataclasses import dataclass
from pathlib import Path
import json
import sqlite3

import pytest

from ml.intraday import market_collection as collection
from ml.intraday import market_collection_index as index
from ml.intraday import prospective_context as worker


@dataclass
class SavedReceipt:
    collector: object
    request: object
    key: str
    start: object
    path: Path
    snapshot: dict

    def recover(self):
        return worker._existing_receipt(self.collector, self.key, self.request, self.start)

    def change_state(self, edit, *, raw=False):
        with self.collector.lock:
            state = self.collector._state()
            edit(state)
            if raw:
                self.collector.state_path.write_text(json.dumps(state), encoding="utf-8")
            else:
                collection._atomic(self.collector.state_path, state)


@pytest.fixture
def make_receipt(tmp_path):
    def make(*, status="EMPTY", collector_type=collection.QuarterHourMarketCollector):
        request = collection.SourceRequest("fmp", "stock_quote", "TEST", "fixture", "quote", "quote")
        calls = []
        def fixture_adapter(*args, **kwargs):
            if calls:
                raise AssertionError("Recovery must never reacquire provider data")
            calls.append(True)
            return collection.CollectionResult([], status, "Fixture receipt")
        collector = collector_type(tmp_path / "market-data", symbols=["TEST"], requests=[request],
            adapters={"fmp": fixture_adapter}, clock=lambda: "2026-10-27T16:15:02Z",
            configuration_sha256="a" * 64)
        start = collection.instant("2026-10-27T16:00:00Z")
        collector.enqueue(start)
        key = next(iter(collector._state()["tasks"]))
        task = collector._state()["tasks"][key]
        snapshot = collector._collect(key, task)
        assert calls == [True]
        path = collector.root / "snapshots" / start.date().isoformat() / key / (snapshot["snapshot_id"] + ".json")
        fixture = SavedReceipt(collector, request, key, start, path, snapshot)
        assert fixture.recover() == snapshot
        return fixture
    return make


@pytest.fixture
def queue_writes(monkeypatch):
    writes = []
    original = Path.replace
    def replace(path, destination):
        if Path(destination).name == "queue.json":
            writes.append(Path(destination))
        return original(path, destination)
    monkeypatch.setattr(Path, "replace", replace)
    return writes


def test_native_noop_repairs_index_without_republishing_queue(make_receipt, queue_writes):
    saved = make_receipt()
    queue_writes.clear()
    before_queue = saved.collector.state_path.read_bytes()
    before_snapshot = saved.path.read_bytes()
    with sqlite3.connect(saved.collector.root / index.INDEX_NAME) as db:
        db.execute("DELETE FROM receipts WHERE snapshot_id=?", (saved.snapshot["snapshot_id"],))
    assert saved.recover() == saved.snapshot
    assert queue_writes == []
    assert saved.collector.state_path.read_bytes() == before_queue
    assert saved.path.read_bytes() == before_snapshot
    with sqlite3.connect(saved.collector.root / index.INDEX_NAME) as db:
        assert db.execute("SELECT count(*) FROM receipts WHERE snapshot_id=?",
            (saved.snapshot["snapshot_id"],)).fetchone()[0] == 1


@pytest.mark.parametrize("field,value", [("status", "QUEUED"), ("status", False),
    ("window_finalized", True), ("window_finalized", 0), ("window_finalized", 0.0)])
def test_changed_projection_is_published_with_original_types(make_receipt, queue_writes, field, value):
    saved = make_receipt()
    saved.change_state(lambda state: state["tasks"][saved.key].__setitem__(field, value))
    queue_writes.clear()
    assert saved.recover() == saved.snapshot
    assert len(queue_writes) == 1
    task = saved.collector._state()["tasks"][saved.key]
    assert task["status"] == "EMPTY" and type(task["status"]) is str
    assert task["window_finalized"] is False
    assert task["attempts"] == 1 and task["snapshots"] == [str(saved.path.relative_to(saved.collector.root))]


@pytest.mark.parametrize("field", ["status", "window_finalized"])
def test_missing_projection_key_is_published(make_receipt, queue_writes, field):
    saved = make_receipt()
    saved.change_state(lambda state: state["tasks"][saved.key].pop(field))
    queue_writes.clear()
    saved.recover()
    assert len(queue_writes) == 1
    task = saved.collector._state()["tasks"][saved.key]
    assert task["status"] == "EMPTY" and task["window_finalized"] is False


def test_missing_snapshot_is_added_once_and_attempt_identity_preserved(make_receipt, queue_writes):
    saved = make_receipt()
    saved.change_state(lambda state: state["tasks"][saved.key]["snapshots"].clear())
    queue_writes.clear()
    saved.recover()
    assert len(queue_writes) == 1
    task = saved.collector._state()["tasks"][saved.key]
    assert task["attempts"] == 2
    assert task["snapshots"] == [str(saved.path.relative_to(saved.collector.root))]
    saved.recover()
    assert len(queue_writes) == 1
    assert saved.collector._state()["tasks"][saved.key]["attempts"] == 2


@pytest.mark.parametrize("missing_map", [False, True])
def test_denial_repairs_missing_exact_source_block(make_receipt, queue_writes, missing_map):
    saved = make_receipt(status="NOT_AUTHORIZED")
    def edit(state):
        if missing_map:
            state.pop("source_blocks")
        else:
            state["source_blocks"].pop(saved.request.identity)
    saved.change_state(edit)
    queue_writes.clear()
    saved.recover()
    assert len(queue_writes) == 1
    block = saved.collector._state()["source_blocks"][saved.request.identity]
    assert block == {"observed_at": saved.snapshot["receipt_at"],
        "snapshot": str(saved.path.relative_to(saved.collector.root))}


@pytest.mark.parametrize("block", [{"reason": "Earlier exact source denial"}, None])
def test_existing_source_block_is_preserved_without_queue_rewrite(make_receipt, queue_writes, block):
    saved = make_receipt(status="NOT_AUTHORIZED")
    saved.change_state(lambda state: state["source_blocks"].__setitem__(saved.request.identity, block))
    queue_writes.clear()
    before = saved.collector.state_path.read_bytes()
    saved.recover()
    assert queue_writes == []
    assert saved.collector.state_path.read_bytes() == before
    assert saved.collector._state()["source_blocks"][saved.request.identity] == block


@pytest.mark.parametrize("field", ["snapshot_sha256", "request_identity", "source_id", "configuration_sha256"])
def test_invalid_receipt_never_changes_queue_or_repairs_index(make_receipt, queue_writes, monkeypatch, field):
    saved = make_receipt()
    changed = dict(saved.snapshot)
    changed[field] = "invalid"
    if field != "snapshot_sha256":
        changed["snapshot_sha256"] = collection.snapshot_digest(changed)
    saved.path.write_bytes(collection.canonical(changed))
    before = saved.collector.state_path.read_bytes()
    queue_writes.clear()
    def forbidden(*args):
        raise AssertionError("Invalid immutable receipt reached index")
    monkeypatch.setattr(index, "index_receipt", forbidden)
    with pytest.raises(ValueError, match="recovery identity changed"):
        saved.recover()
    assert queue_writes == [] and saved.collector.state_path.read_bytes() == before


def test_custom_state_reader_preserves_original_publication(make_receipt, queue_writes, monkeypatch):
    saved = make_receipt()
    original = saved.collector._state
    monkeypatch.setattr(saved.collector, "_state", lambda: original())
    queue_writes.clear()
    saved.recover()
    assert len(queue_writes) == 1


def test_collector_subclass_preserves_original_publication(make_receipt, queue_writes):
    class CustomCollector(collection.QuarterHourMarketCollector):
        pass
    saved = make_receipt(collector_type=CustomCollector)
    queue_writes.clear()
    saved.recover()
    assert len(queue_writes) == 1


def test_custom_atomic_failure_is_not_skipped(make_receipt, monkeypatch):
    saved = make_receipt()
    failure = OSError("Fixture publication failure")
    def fail(*args):
        raise failure
    monkeypatch.setattr(worker, "_atomic", fail)
    with pytest.raises(OSError) as caught:
        saved.recover()
    assert caught.value is failure


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_native_field_keeps_original_canonical_normalization(make_receipt, queue_writes, value):
    saved = make_receipt()
    saved.change_state(lambda state: state.__setitem__("unrelated", value), raw=True)
    queue_writes.clear()
    saved.recover()
    assert len(queue_writes) == 1
    assert saved.collector._state()["unrelated"] is None


def test_deep_native_json_declines_optimization_without_new_recursion_error(make_receipt, queue_writes):
    saved = make_receipt()
    value = []
    for _ in range(70):
        value = [value]
    saved.change_state(lambda state: state.__setitem__("unrelated", value))
    queue_writes.clear()
    saved.recover()
    assert len(queue_writes) == 1
    assert saved.collector._state()["unrelated"] == value


def test_bounded_receipt_inventory_failure_is_preserved(make_receipt, queue_writes):
    saved = make_receipt()
    for number in range(32):
        saved.path.with_name(f"copy-{number}.json").write_bytes(saved.path.read_bytes())
    queue_writes.clear()
    with pytest.raises(ValueError, match="bounded receipt recovery inventory"):
        saved.recover()
    assert queue_writes == []


@pytest.mark.parametrize("field", ["status", "window_finalized"])
def test_missing_key_is_restored_even_when_receipt_value_is_null(make_receipt, queue_writes, monkeypatch, field):
    saved = make_receipt()
    receipt = dict(saved.snapshot)
    receipt[field] = None
    receipt["snapshot_sha256"] = collection.snapshot_digest(receipt)
    saved.path.write_bytes(collection.canonical(receipt))
    # Isolate the projection after the original index callback. A null status
    # normally fails its SQLite constraint, which the no-op path must not mask.
    monkeypatch.setattr(index, "index_receipt", lambda *args: None)
    saved.change_state(lambda state: state["tasks"][saved.key].pop(field))
    queue_writes.clear()
    saved.recover()
    assert len(queue_writes) == 1
    task = saved.collector._state()["tasks"][saved.key]
    assert field in task and task[field] is None


@pytest.mark.parametrize("as_key", [False, True])
def test_lone_surrogate_keeps_original_unicode_publication_failure(make_receipt, queue_writes, as_key):
    saved = make_receipt()
    def edit(state):
        state["\ud800" if as_key else "unrelated"] = "value" if as_key else "\ud800"
    saved.change_state(edit, raw=True)
    queue_writes.clear()
    with pytest.raises(UnicodeEncodeError):
        saved.recover()
    assert queue_writes == []
