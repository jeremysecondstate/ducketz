"""Renewed workers reuse exact validated headers from one bounded batch."""
from datetime import timedelta
import json
from pathlib import Path
import socket
import sqlite3
from types import SimpleNamespace

import pytest

from ml.intraday.forecast_header_cache import DurableForecastHeaders, _SCHEMA
from ml.intraday.prediction import ContractError, digest, encoded, instant
from ml.intraday.store import ShadowStore


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Header batch tests must remain offline")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def populate(path, count=4):
    store = ShadowStore(path)
    values = []
    with store.connect() as db:
        for i in range(count):
            reference = instant("2026-10-08T17:00:00Z") + timedelta(minutes=15 * i)
            value = dict(pc="Atlas", symbol="AAPL", cycle_id="cycle-" + str(i),
                reference_at=reference.isoformat(), issued_at=(reference + timedelta(minutes=16)).isoformat(),
                target_open=(reference + timedelta(minutes=30)).isoformat(),
                target_close=(reference + timedelta(minutes=45)).isoformat(),
                mode="live", ensemble_status="full", reference_open="100",
                probability_not_down=.5001, target_version="original-target", models={"original": "tree" * 1000})
            value["forecast_id"] = digest(value)
            db.execute("INSERT INTO forecasts VALUES(?,?,?,?,?,?,?)",
                (*(value[field] for field in ("forecast_id", "pc", "symbol", "cycle_id", "reference_at", "issued_at")), encoded(value)))
            values.append(value)
    return store, values


def headers(store):
    return store.execution_forecast_headers(pc="Atlas", symbol="AAPL")


def decoded(store, monkeypatch):
    calls = []
    original = store._forecast_value
    def checked(row):
        calls.append(row[0])
        return original(row)
    monkeypatch.setattr(store, "_forecast_value", checked)
    return calls


@pytest.mark.parametrize("count", [1, 458, 1000])
def test_default_budget_attests_whole_inventory_for_next_worker(tmp_path, monkeypatch, count):
    store, values = populate(tmp_path / "science.sqlite3", count)
    batches = []
    original = DurableForecastHeaders.save_many
    def counted(self, identity, entries):
        entries = list(entries)
        batches.append(len(entries))
        return original(self, identity, entries)
    monkeypatch.setattr(DurableForecastHeaders, "save_many", counted)
    before = [encoded(value) for value in store.forecasts()]
    first = headers(store)
    assert batches == [count]
    cold = ShadowStore(store.path)
    full_reads = decoded(cold, monkeypatch)
    assert headers(cold) == first and full_reads == []
    assert [encoded(value) for value in cold.forecasts()] == before
    assert len(first) == len(values)


@pytest.mark.parametrize("stop_after", [0, 1, 3])
def test_stopped_inventory_does_not_publish_partial_batch(tmp_path, monkeypatch, stop_after):
    store, _ = populate(tmp_path / "stopped.sqlite3")
    writes = []
    monkeypatch.setattr(DurableForecastHeaders, "save_many", lambda *args: writes.append(args))
    count = 0
    def stopped():
        nonlocal count
        count += 1
        return count > stop_after
    with store._execution_forecast_lock, store.connect() as db:
        db.execute("BEGIN")
        assert store._revision_forecast_headers(db, ["f.pc=?"], ["Atlas"], stopped=stopped) == []
    assert writes == [] and store._execution_forecast_highwater == 0


@pytest.mark.parametrize("failure", ["replaced", "stat_error"])
def test_final_source_identity_check_disables_whole_cache_batch(tmp_path, monkeypatch, failure):
    store, _ = populate(tmp_path / "source.sqlite3")
    full_reads = decoded(store, monkeypatch)
    stat = Path.stat
    def changed(path, *args, **kwargs):
        if path == store.path and full_reads:
            if failure == "stat_error":
                raise PermissionError("synthetic source path unavailable")
            value = stat(path, *args, **kwargs)
            return SimpleNamespace(st_dev=value.st_dev, st_ino=value.st_ino + 1)
        return stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, "stat", changed)
    writes = []
    monkeypatch.setattr(DurableForecastHeaders, "save_many", lambda *args: writes.append(args))
    assert len(headers(store)) == 4 and len(full_reads) == 4 and writes == []


def test_invalid_later_original_cannot_publish_earlier_partial_proofs(tmp_path, monkeypatch):
    store, values = populate(tmp_path / "invalid.sqlite3")
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE forecasts SET payload='{}' WHERE id=?", (values[-1]["forecast_id"],))
    writes = []
    monkeypatch.setattr(DurableForecastHeaders, "save_many", lambda *args: writes.append(args))
    with pytest.raises(ContractError, match="hash or indexed identity"):
        headers(store)
    assert writes == []


@pytest.mark.parametrize("field", ["pc", "symbol", "cycle_id", "reference_at", "issued_at"])
def test_changed_indexed_original_cannot_reuse_batch_proof(tmp_path, field):
    store, values = populate(tmp_path / "indexed.sqlite3")
    headers(store)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE forecasts SET " + field + "=? WHERE id=?", ("changed", values[0]["forecast_id"]))
    with pytest.raises(ContractError, match="hash or indexed identity"):
        # Read by identity so a changed symbol/PC cannot disappear from scope.
        cold = ShadowStore(store.path)
        with cold._execution_forecast_lock, cold.connect() as db:
            db.execute("BEGIN")
            cold._revision_forecast_headers(db, ["f.id=?"], [values[0]["forecast_id"]])


def test_forged_exact_schema_record_falls_back_to_full_original(tmp_path, monkeypatch):
    store, values = populate(tmp_path / "mac.sqlite3")
    original = headers(store)
    helper = DurableForecastHeaders(store.path)
    with sqlite3.connect(helper.path) as db:
        version = db.execute("PRAGMA schema_version").fetchone()[0]
        key, raw = db.execute("SELECT cache_key,payload FROM forecast_header_cache_rows LIMIT 1").fetchone()
        record = json.loads(raw)
        changed_id = record["projection"]["forecast_id"]
        record["projection"]["mode"] = "shadow"
        db.execute("DROP TRIGGER forecast_header_cache_rows_update")
        db.execute("UPDATE forecast_header_cache_rows SET payload=? WHERE cache_key=?", (encoded(record).encode(), key))
        db.execute(_SCHEMA["forecast_header_cache_rows_update"])
        db.execute("PRAGMA schema_version=" + str(version))
    cold = ShadowStore(store.path)
    full_reads = decoded(cold, monkeypatch)
    assert headers(cold) == original and full_reads == [changed_id]


@pytest.mark.parametrize("failure", ["busy", "malformed", "key_missing"])
def test_optional_batch_cache_failure_only_causes_original_reads(tmp_path, monkeypatch, failure):
    store, _ = populate(tmp_path / "optional.sqlite3")
    original = headers(store)
    helper = DurableForecastHeaders(store.path)
    blocker = None
    if failure == "busy":
        blocker = sqlite3.connect(helper.path, timeout=0)
        blocker.execute("BEGIN EXCLUSIVE")
    elif failure == "malformed":
        helper.path.write_bytes(b"not a cache")
    else:
        helper.key_path.unlink()
    try:
        cold = ShadowStore(store.path)
        full_reads = decoded(cold, monkeypatch)
        assert headers(cold) == original and len(full_reads) == 4
    finally:
        if blocker is not None:
            blocker.rollback()
            blocker.close()


def scalar_entries():
    signature = ("id", "Atlas", "AAPL", "cycle", "reference", "issue", 1)
    projection = dict(zip(("forecast_id", "pc", "symbol", "cycle_id", "reference_at", "issued_at"), signature[:6]), mode="live")
    return signature, projection


def test_invalid_projection_and_conflicting_duplicate_cannot_forge_batch(tmp_path):
    identity = ("fixture", 1, 2, 3, "epoch")
    signature, projection = scalar_entries()
    helper = DurableForecastHeaders(tmp_path / "source.sqlite3")
    helper.save_many(identity, [(signature, {**projection, "pc": "Scout"})])
    assert DurableForecastHeaders(helper.path.with_name("source.sqlite3")).load_many(identity, [signature]) == {}
    helper.save_many(identity, [(signature, projection), (signature, {**projection, "mode": "shadow"})])
    assert helper.load_many(identity, [signature]) == {}


def test_interrupted_batch_rolls_back_every_uncommitted_record(tmp_path, monkeypatch):
    identity = ("fixture", 1, 2, 3, "epoch")
    signature, projection = scalar_entries()
    helper = DurableForecastHeaders(tmp_path / "atomic.sqlite3")
    connect = helper._connect
    class Interrupted:
        def __init__(self, db): self.db = db
        def __getattr__(self, name): return getattr(self.db, name)
        def executemany(self, sql, entries):
            self.db.execute(sql, next(iter(entries)))
            raise sqlite3.OperationalError("synthetic interrupted optional batch")
    monkeypatch.setattr(helper, "_connect", lambda: Interrupted(connect()))
    helper.save_many(identity, [(signature, projection)])
    assert DurableForecastHeaders(tmp_path / "atomic.sqlite3").load_many(identity, [signature]) == {}


def test_budget_zero_disables_batch_without_affecting_originals(tmp_path, monkeypatch):
    store, _ = populate(tmp_path / "disabled.sqlite3")
    headers(store)
    cold = ShadowStore(store.path)
    full_reads = decoded(cold, monkeypatch)
    assert len(cold.execution_forecast_headers(pc="Atlas", symbol="AAPL", cache_io_budget_seconds=0)) == 4
    assert len(full_reads) == 4
