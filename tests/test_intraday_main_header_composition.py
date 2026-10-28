"""Main scoring/direction APIs coexist with guarded execution header caches."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
import json
import os

import pytest

from ml.intraday.forecast_header_cache import DurableForecastHeaders
from ml.intraday.intentions import IntentionStore, VERSION
from ml.intraday.prediction import ContractError, digest, encoded, instant
from ml.intraday.store import ShadowStore, ScoringForecastReader, _EXECUTION_FORECAST_SCHEMA
from tests.test_intraday_store import forecast


def put(store, name, *, offset=0, raw=False, **changes):
    value = forecast(cycle_id=name, mode="live")
    for field in ("reference_at", "target_open", "target_close", "issued_at",
                  "information_available_at", "data_through"):
        value[field] = (instant(value[field]) + timedelta(minutes=offset)).isoformat()
    value.update(changes)
    if not raw:
        return store.save_forecast(value)
    value["forecast_id"] = digest(value)
    with store.connect() as db:
        db.execute("INSERT INTO forecasts VALUES(?,?,?,?,?,?,?)",
            (*(value[field] for field in ("forecast_id", "pc", "symbol", "cycle_id", "reference_at", "issued_at")), encoded(value)))
    return value


def headers(store, **changes):
    return store.execution_forecast_headers(pc="Atlas", symbol="AAPL", **changes)


def spy(store, monkeypatch):
    calls = []
    original = store._forecast_value
    def checked(row):
        calls.append(row[0])
        return original(row)
    monkeypatch.setattr(store, "_forecast_value", checked)
    return calls


def durable_spy(monkeypatch):
    calls = []
    original = DurableForecastHeaders.load_many
    def checked(self, identity, signatures):
        signatures = tuple(signatures)
        calls.append(signatures)
        return original(self, identity, signatures)
    monkeypatch.setattr(DurableForecastHeaders, "load_many", checked)
    return calls


def sized(intents, f):
    value = {"version": VERSION, "forecast_id": f["forecast_id"], "symbol": f["symbol"],
             "side": f["stock_action"], "desired_quantity": 1,
             "sizing_id": "fixture-sizing", "sizing": {"calculated_at": f["issued_at"]}}
    with intents.store.connect() as db:
        db.execute("INSERT INTO intraday_intentions VALUES(?,?,?)", (f["forecast_id"], digest(value), encoded(value)))


def test_cold_validates_originals_and_warm_uses_no_optional_cache(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "source.sqlite3")
    # Raw predecessor rows have no optional producer-seeded cache proof.
    originals = [put(store, "first", raw=True), put(store, "second", offset=15, raw=True)]
    reads = spy(store, monkeypatch)
    initial = headers(store)
    assert reads == [f["forecast_id"] for f in originals]
    reads.clear()
    disk = durable_spy(monkeypatch)
    assert headers(store) == initial and reads == [] and disk == []
    assert all("models" not in f for f in initial)
    assert len(repr(store._execution_forecast_cache)) < 4096


def test_mixed_reads_only_missing_signature_and_new_instance_reuses_proof(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "source.sqlite3")
    first = put(store, "first", raw=True)
    headers(store)
    second = put(store, "second", offset=15, raw=True)
    reads = spy(store, monkeypatch)
    disk = durable_spy(monkeypatch)
    assert [x["forecast_id"] for x in headers(store)] == [first["forecast_id"], second["forecast_id"]]
    assert reads == [second["forecast_id"]]
    assert [[x[0] for x in call] for call in disk] == [[second["forecast_id"]]]
    cold = ShadowStore(store.path)
    cold_reads = spy(cold, monkeypatch)
    assert headers(cold) == headers(store)
    assert cold_reads == []


def test_cache_bound_does_not_evict_scan_owned_proofs(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "source.sqlite3")
    original_connect = store.connect
    with original_connect() as db:
        @contextmanager
        def same():
            yield db
        with monkeypatch.context() as patch:
            patch.setattr(store, "connect", same)
            for i in range(4100):
                put(store, str(i), offset=15*i, raw=True)
    original_load, original_save = DurableForecastHeaders.load_many, DurableForecastHeaders.save
    monkeypatch.setattr(DurableForecastHeaders, "load_many", lambda *a, **k: {})
    monkeypatch.setattr(DurableForecastHeaders, "save", lambda *a, **k: None)
    expected = headers(store)
    assert len(store._execution_forecast_cache) == 4096
    monkeypatch.setattr(DurableForecastHeaders, "load_many", original_load)
    monkeypatch.setattr(DurableForecastHeaders, "save", original_save)
    disk = durable_spy(monkeypatch)
    reads = spy(store, monkeypatch)
    assert headers(store) == expected
    assert len(disk) == 1 and len(disk[0]) == 4
    assert reads == [x["forecast_id"] for x in expected[:4]]


@pytest.mark.parametrize("damage", ["payload", "pc", "symbol", "cycle_id", "reference_at", "issued_at"])
def test_warm_tampering_never_reuses_proof(tmp_path, damage):
    store = ShadowStore(tmp_path / "source.sqlite3")
    f = put(store, "first")
    headers(store)
    with store.connect() as db:
        if damage == "payload":
            raw = db.execute("SELECT payload FROM forecasts WHERE id=?", (f["forecast_id"],)).fetchone()[0]
            changed = raw.replace('"probability_not_down":0.72', '"probability_not_down":0.73')
            assert raw != changed
            db.execute("UPDATE forecasts SET payload=? WHERE id=?", (changed, f["forecast_id"]))
        else:
            db.execute("UPDATE forecasts SET " + damage + "='changed' WHERE id=?", (f["forecast_id"],))
    with pytest.raises(ContractError):
        with store._execution_forecast_lock, store.connect() as db:
            db.execute("BEGIN")
            store._revision_forecast_headers(db, ["f.id=?"], [f["forecast_id"]])


@pytest.mark.parametrize("damage", ["trigger", "revision", "generation", "epoch"])
def test_warm_schema_generation_epoch_and_revision_guards(tmp_path, damage):
    store = ShadowStore(tmp_path / "source.sqlite3")
    f = put(store, "first")
    headers(store)
    with store.connect() as db:
        if damage == "trigger":
            db.execute("DROP TRIGGER execution_forecast_update")
        elif damage == "revision":
            db.execute("DELETE FROM execution_forecast_revisions WHERE forecast_id=?", (f["forecast_id"],))
        else:
            cookie = db.execute("PRAGMA schema_version").fetchone()[0]
            db.execute("DROP TRIGGER execution_generation_update")
            db.execute("UPDATE execution_forecast_generation SET " + ("generation=0" if damage == "generation" else "epoch='gggggggggggggggggggggggggggggggg'"))
            db.execute(_EXECUTION_FORECAST_SCHEMA["execution_generation_update"])
            db.execute("PRAGMA schema_version=" + str(cookie))
    with pytest.raises(ContractError):
        headers(store)


def test_equal_bytes_new_revision_rereads_original_and_deletion_is_observed(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "source.sqlite3")
    f = put(store, "first")
    expected = headers(store)
    with store.connect() as db:
        db.execute("UPDATE forecasts SET payload=payload WHERE id=?", (f["forecast_id"],))
    reads = []
    original = store._forecast_projection
    def read(row):
        reads.append(row[0])
        return original(row)
    monkeypatch.setattr(store, "_forecast_projection", read)
    assert headers(store) == expected and reads == [f["forecast_id"]]
    with store.connect() as db:
        db.execute("DELETE FROM forecasts WHERE id=?", (f["forecast_id"],))
    assert headers(store) == []


def test_uncertain_source_identity_and_replaced_file_discard_warm_proofs(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "source.sqlite3")
    put(store, "old")
    headers(store)
    reads = []
    original_projection = store._forecast_projection
    def read(row):
        reads.append(row[0])
        return original_projection(row)
    monkeypatch.setattr(store, "_forecast_projection", read)
    with store._execution_forecast_lock, store.connect() as db:
        db.execute("BEGIN")
        store._connection_source_identities[id(db)] = None
        assert len(store._revision_forecast_headers(db, ["f.pc=?"], ["Atlas"])) == 1
    assert len(reads) == 1
    replacement = ShadowStore(tmp_path / "replacement.sqlite3")
    f = put(replacement, "replacement", offset=15)
    os.replace(replacement.path, store.path)
    assert [x["forecast_id"] for x in headers(store)] == [f["forecast_id"]]
    assert store._connection_source_identities == {}


@pytest.mark.parametrize("damage", ["cache", "key", "zero_budget"])
def test_optional_cache_failure_keeps_source_fallback(tmp_path, monkeypatch, damage):
    store = ShadowStore(tmp_path / "source.sqlite3")
    f = put(store, "first")
    expected = headers(store)
    cache = DurableForecastHeaders(store.path)
    if damage == "cache":
        cache.path.write_bytes(b"invalid optional cache")
    elif damage == "key":
        cache.key_path.write_bytes(b"invalid optional key")
    cold = ShadowStore(store.path)
    reads = spy(cold, monkeypatch)
    assert headers(cold, cache_io_budget_seconds=0 if damage == "zero_budget" else .25) == expected
    assert reads == [f["forecast_id"]]


def test_concurrent_reads_and_stopped_read_release_resources(tmp_path):
    store = ShadowStore(tmp_path / "source.sqlite3")
    for i in range(3):
        put(store, str(i), offset=15*i)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = [f.result() for f in [pool.submit(headers, store), pool.submit(headers, store)]]
    assert a == b and len(a) == 3
    with store._execution_forecast_lock, store.connect() as db:
        db.execute("BEGIN")
        assert store._revision_forecast_headers(db, ["f.pc=?"], ["Atlas"], stopped=lambda: True) == []
    assert store._connection_source_identities == {}


@pytest.mark.parametrize("pc", ["Atlas", "Scout"])
def test_intention_fence_keeps_scope_exact_instants_and_saved_directions(tmp_path, pc):
    store = ShadowStore(tmp_path / "source.sqlite3")
    intents = IntentionStore(store)
    current = put(store, "current", pc=pc)
    old = put(store, "older", offset=-15, pc=pc)
    same = put(store, "same", pc=pc, reference_at="2026-10-08T10:00:00-07:00")
    new = put(store, "new", offset=15, pc=pc)
    unsized = put(store, "unsized", offset=30, pc=pc)
    peer = put(store, "peer", offset=30, pc="Scout" if pc == "Atlas" else "Atlas")
    other = put(store, "other-symbol", offset=30, pc=pc, symbol="AMZN")
    for f in (current, old, same, new, peer, other):
        sized(intents, f)
    originals = [store.forecast_by_id(f["forecast_id"]) for f in (current, old, same, new, unsized, peer, other)]
    assert intents.processed_at_or_after(current) == [same["forecast_id"], new["forecast_id"]]
    assert intents.processed_at_or_after(current) == [same["forecast_id"], new["forecast_id"]]
    assert [store.forecast_by_id(f["forecast_id"]) for f in originals] == originals


def test_main_scoring_projection_and_original_reload_remain_available(tmp_path):
    store = ShadowStore(tmp_path / "source.sqlite3")
    f = put(store, "first", pc="Scout", native_interval_contract="native",
            dataset_manifest={"native_interval_contract": "native", "large": [1]*1000},
            reference_provenance={"forward_fill_contract": "carry", "large": [2]*1000})
    reader = ScoringForecastReader(store, pc="Scout")
    projection = list(reader)[0]
    assert projection["dataset_manifest"] == {"native_interval_contract": "native"}
    assert projection["reference_provenance"] == {"forward_fill_contract": "carry"}
    assert reader.original(projection) == f
    store.execution_forecast_headers(pc="Scout", symbol="AAPL")
    assert list(reader)[0] == projection and reader.original(projection) == f
    with store.connect() as db:
        db.execute("UPDATE forecasts SET payload=? WHERE id=?", ("{}", f["forecast_id"]))
    with pytest.raises(ContractError):
        reader.original(projection)


@pytest.mark.parametrize("pc", ["Atlas", "Scout"])
def test_fence_ignores_malformed_older_sized_original_but_checks_selected(tmp_path, pc):
    store = ShadowStore(tmp_path / "source.sqlite3")
    intents = IntentionStore(store)
    current = put(store, "current", pc=pc)
    old = put(store, "older", pc=pc, offset=-15,
              reference_at="2026-10-08T09:45:00-07:00")
    future = put(store, "future", pc=pc, offset=15,
                 reference_at="2026-10-08T10:15:00-07:00")
    for f in (old, future):
        sized(intents, f)
    with store.connect() as db:
        db.execute("UPDATE forecasts SET payload='{}' WHERE id=?", (old["forecast_id"],))
    assert intents.processed_at_or_after(current) == [future["forecast_id"]]
    with store.connect() as db:
        db.execute("UPDATE forecasts SET payload='{}' WHERE id=?", (future["forecast_id"],))
    with pytest.raises(ContractError):
        intents.processed_at_or_after(current)
