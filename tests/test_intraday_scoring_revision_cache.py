"""Saved-target drains reuse unchanged originals while retaining evidence checks."""
from contextlib import contextmanager
import json
import re
import socket
import sqlite3

import pytest

from ml.intraday.prediction import ContractError
from ml.intraday.store import ShadowStore
from tests.test_intraday_store import candle, forecast


NOW = "2026-10-08T18:00:00Z"


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Saved-target cache fixtures must never contact providers")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def own(store, *, cycle_id="own", symbol="AAPL"):
    value = forecast(mode="live", cycle_id=cycle_id, symbol=symbol,
                     diagnostics={"scientific_original": "retained evidence " * 10000})
    store.begin_cycle(cycle_id, dict(pc="Atlas", mode="live", experiment="manual",
        symbols=[symbol], reference_at=value["reference_at"], configuration_hash="fixture",
        started_at=value["issued_at"]))
    return store.save_forecast(value)


def drain(store):
    return store.retained_session_scoring_targets(pc="Atlas", session_id="manual", as_of=NOW)


def scored(store):
    original = own(store)
    target = candle()
    store.record_candle(target)
    store.observe(original, as_of="2026-10-08T17:46:00Z", candle=target)
    assert drain(store) == []
    return original, target


def trace(store, monkeypatch):
    statements = []
    connect = store.connect
    @contextmanager
    def observed():
        with connect() as db:
            db.set_trace_callback(statements.append)
            yield db
    monkeypatch.setattr(store, "connect", observed)
    return statements


def payload_reads(statements):
    return [statement for statement in statements
            if re.search(r"\bSELECT\b.*\bpayload\b.*\bFROM forecasts\b", statement, re.I | re.S)]


def test_warm_scored_drain_reads_no_original_payload_and_checks_revisions(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "warm.sqlite3")
    original, _ = scored(store)
    statements = trace(store, monkeypatch)
    revisions, checked = store.revisions, []
    def observed(key):
        checked.append(key)
        return revisions(key)
    monkeypatch.setattr(store, "revisions", observed)
    assert drain(store) == []
    assert checked == [original["forecast_id"]]
    assert payload_reads(statements) == []
    # An unrelated insertion advances the global generation without changing
    # the original's row revision or forcing another scientific payload read.
    own(store, cycle_id="unrelated", symbol="OTHER")
    statements.clear()
    checked.clear()
    assert drain(store) == []
    assert checked == [original["forecast_id"]]
    assert payload_reads(statements) == []


@pytest.mark.parametrize("damage", ["same_length_payload", "indexed_id", "indexed_issue",
                                   "indexed_reference", "indexed_symbol", "indexed_cycle"])
def test_warm_scored_original_mutations_are_revalidated(tmp_path, damage):
    store = ShadowStore(tmp_path / "mutated.sqlite3")
    original, _ = scored(store)
    if damage == "indexed_symbol":
        store.record_candle(candle(symbol="OTHER"))
    if damage == "indexed_cycle":
        own(store, cycle_id="other-cycle", symbol="OTHER")
    with sqlite3.connect(store.path) as db:
        if damage == "same_length_payload":
            raw = db.execute("SELECT payload FROM forecasts WHERE id=?", (original["forecast_id"],)).fetchone()[0]
            changed = raw.replace('"reference_open":"100"', '"reference_open":"999"')
            assert changed != raw and len(changed) == len(raw)
            db.execute("UPDATE forecasts SET payload=? WHERE id=?", (changed, original["forecast_id"]))
        else:
            column, value = {
                "indexed_id": ("id", "changed-id"),
                "indexed_issue": ("issued_at", "2026-10-08T17:16:03+00:00"),
                "indexed_reference": ("reference_at", "2026-10-08T10:00:00-07:00"),
                "indexed_symbol": ("symbol", "OTHER"),
                "indexed_cycle": ("cycle_id", "other-cycle"),
            }[damage]
            db.execute("UPDATE forecasts SET " + column + "=? WHERE id=?", (value, original["forecast_id"]))
    with pytest.raises(ContractError, match="hash or indexed identity"):
        drain(store)


def test_warm_drain_checks_older_revision_behind_valid_latest(tmp_path):
    store = ShadowStore(tmp_path / "ancestry.sqlite3")
    original = own(store)
    first = store.observe(original, as_of="2026-10-08T17:45:00Z")
    target = candle()
    store.record_candle(target)
    store.observe(original, as_of="2026-10-08T17:46:00Z", candle=target)
    assert drain(store) == []
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE outcomes SET payload=json_set(payload,'$.status','wrong') WHERE id=?",
                   (first["revision_id"],))
    with pytest.raises(ContractError, match="hash or indexed identity"):
        drain(store)


def test_corrected_target_loads_exact_original_and_preserves_ancestry(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "correction.sqlite3")
    original, _ = scored(store)
    prior = store.revisions(original["forecast_id"])
    statements = trace(store, monkeypatch)
    corrected = candle(close="99", available_at="2026-10-08T17:58:00+00:00")
    store.record_candle(corrected)
    statements.clear()
    assert drain(store) == [(original, corrected)]
    assert len(payload_reads(statements)) == 1
    revised = store.observe(original, as_of=NOW, candle=corrected)
    assert revised["target_not_down"] is False
    assert store.revisions(original["forecast_id"]) == [*prior, revised]
    assert store.forecast(original["cycle_id"], original["symbol"]) == original
    statements.clear()
    assert drain(store) == [] and payload_reads(statements) == []


@pytest.mark.parametrize("damage", ["missing_trigger", "altered_trigger", "missing_row", "altered_table"])
def test_warm_scored_drain_rejects_missing_or_changed_revision_markers(tmp_path, damage):
    store = ShadowStore(tmp_path / "markers.sqlite3")
    original, _ = scored(store)
    with sqlite3.connect(store.path) as db:
        if damage == "missing_trigger":
            db.execute("DROP TRIGGER execution_forecast_update")
        elif damage == "altered_trigger":
            db.execute("DROP TRIGGER execution_forecast_update")
            db.execute("CREATE TRIGGER execution_forecast_update AFTER UPDATE ON forecasts BEGIN SELECT 1; END")
        elif damage == "altered_table":
            db.execute("ALTER TABLE execution_forecast_revisions ADD COLUMN forged TEXT")
        else:
            db.execute("DELETE FROM execution_forecast_revisions WHERE forecast_id=?", (original["forecast_id"],))
    with pytest.raises(ContractError, match="[Ee]xecution forecast"):
        drain(store)


def test_revision_lookup_and_original_selection_use_one_wal_snapshot(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "snapshot.sqlite3")
    original, _ = scored(store)
    generation, changed = store._execution_forecast_generation, []
    def concurrently_changed(db):
        result = generation(db)
        if not changed:
            with sqlite3.connect(store.path) as writer:
                writer.execute("UPDATE forecasts SET payload='broken' WHERE id=?", (original["forecast_id"],))
            changed.append(True)
        return result
    monkeypatch.setattr(store, "_execution_forecast_generation", concurrently_changed)
    assert drain(store) == [] and changed == [True]
    with pytest.raises(json.JSONDecodeError):
        drain(store)


@pytest.mark.parametrize("encoding", ["noncanonical", "blob"])
def test_semantically_identical_encoding_keeps_full_validation_when_needed(tmp_path, monkeypatch, encoding):
    store = ShadowStore(tmp_path / "encoding.sqlite3")
    original, _ = scored(store)
    raw = json.dumps(dict(reversed(list(original.items()))), ensure_ascii=False, indent=2)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE forecasts SET payload=? WHERE id=?", (raw, original["forecast_id"]))
        if encoding == "blob":
            db.execute("UPDATE forecasts SET payload=CAST(payload AS BLOB) WHERE id=?", (original["forecast_id"],))
    statements = trace(store, monkeypatch)
    assert drain(store) == [] and payload_reads(statements)
    statements.clear()
    assert drain(store) == []
    assert payload_reads(statements) == []
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE forecasts SET payload=CAST(json_set(payload,'$.reference_open','999') AS BLOB) WHERE id=?",
                   (original["forecast_id"],))
    with pytest.raises(ContractError, match="hash or indexed identity"):
        drain(store)


def test_evicted_and_independent_store_caches_revalidate_own_originals(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "evicted.sqlite3")
    scored(store)
    statements = trace(store, monkeypatch)
    store._execution_forecast_cache.clear()
    assert drain(store) == [] and payload_reads(statements)
    statements.clear()
    assert drain(store) == [] and payload_reads(statements) == []
    other = ShadowStore(store.path)
    independent = trace(other, monkeypatch)
    assert drain(other) == [] and payload_reads(independent)
