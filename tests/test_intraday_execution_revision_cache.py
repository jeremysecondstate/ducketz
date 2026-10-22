"""SQLite revisions preserve chronology without rereading unchanged model trees."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import re
import socket
import sqlite3

import pytest

from ml.intraday.prediction import ContractError, digest, encoded
from ml.intraday.store import ShadowStore
from test_intraday_execution_forecast_projection import put, owner, selected, original_selection
from test_intraday_account_execution import env
from test_intraday_lease_maintenance import WorkerClock
from test_intraday_store import forecast


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Execution revision checks must never contact providers")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


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


def test_warm_execution_scan_does_not_read_unchanged_model_payloads(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "large.sqlite3")
    current = put(store, "current", model_diagnostics={"tree": "scientific-original" * 100000})
    later = put(store, "later", offset=15)
    put(store, "old", offset=-60, model_diagnostics={"tree": "historical-tree" * 100000})
    statements = trace(store, monkeypatch)
    expected = original_selection(owner(store), current)
    statements.clear()
    assert selected(owner(store), current) == expected == [later["forecast_id"]]
    assert payload_reads(statements)
    statements.clear()
    headers = store.execution_forecast_headers(pc="Atlas", symbol="AAPL")
    assert selected(owner(store), current) == expected and payload_reads(statements) == []
    headers[0]["mode"] = "shadow"
    assert selected(owner(store), current) == expected
    put(store, "other", symbol="AMZN", offset=30)
    statements.clear()
    assert selected(owner(store), current) == expected and payload_reads(statements) == []


@pytest.mark.parametrize("damage", ["same_length_payload", "indexed_id", "indexed_cycle", "indexed_reference", "indexed_issue"])
def test_external_sql_changes_in_obsolete_history_invalidate_warm_cache(tmp_path, damage):
    store = ShadowStore(tmp_path / "change.sqlite3")
    current = put(store, "current")
    old = put(store, "old", offset=-60)
    selected(owner(store), current)
    with sqlite3.connect(store.path) as db:
        if damage == "same_length_payload":
            raw = db.execute("SELECT payload FROM forecasts WHERE id=?", (old["forecast_id"],)).fetchone()[0]
            changed = raw.replace('"probability_not_down":0.72', '"probability_not_down":0.73')
            assert changed != raw and len(changed) == len(raw)
            db.execute("UPDATE forecasts SET payload=? WHERE id=?", (changed, old["forecast_id"]))
        else:
            column = {"indexed_id":"id", "indexed_cycle":"cycle_id", "indexed_reference":"reference_at", "indexed_issue":"issued_at"}[damage]
            db.execute("UPDATE forecasts SET " + column + "=? WHERE id=?", ("changed", old["forecast_id"]))
    with pytest.raises(ContractError, match="hash or indexed identity"):
        selected(owner(store), current)


def test_delete_reinsert_and_replace_cannot_reuse_previous_revision(tmp_path):
    store = ShadowStore(tmp_path / "reuse.sqlite3")
    current = put(store, "current")
    later = put(store, "later", offset=15)
    assert selected(owner(store), current) == [later["forecast_id"]]
    with store.connect() as db:
        before = db.execute("SELECT generation FROM execution_forecast_revisions WHERE forecast_id=?", (later["forecast_id"],)).fetchone()[0]
        original = db.execute("SELECT * FROM forecasts WHERE id=?", (later["forecast_id"],)).fetchone()
        db.execute("DELETE FROM forecasts WHERE id=?", (later["forecast_id"],))
        db.execute("INSERT INTO forecasts VALUES(?,?,?,?,?,?,?)", original)
        after = db.execute("SELECT generation FROM execution_forecast_revisions WHERE forecast_id=?", (later["forecast_id"],)).fetchone()[0]
    assert after > before and selected(owner(store), current) == [later["forecast_id"]]
    broken = (*original[:6], original[6].replace('"probability_not_down":0.72', '"probability_not_down":0.73'))
    with store.connect() as db:
        db.execute("INSERT OR REPLACE INTO forecasts VALUES(?,?,?,?,?,?,?)", broken)
    with pytest.raises(ContractError, match="hash or indexed identity"):
        selected(owner(store), current)


def test_external_rollback_preserves_originals_and_generation(tmp_path):
    store = ShadowStore(tmp_path / "rollback.sqlite3")
    current = put(store, "current")
    later = put(store, "later", offset=15)
    expected = selected(owner(store), current)
    with sqlite3.connect(store.path) as db:
        before = db.execute("SELECT generation FROM execution_forecast_generation").fetchone()[0]
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE forecasts SET payload='broken' WHERE id=?", (later["forecast_id"],))
        assert db.execute("SELECT generation FROM execution_forecast_generation").fetchone()[0] > before
        db.rollback()
        assert db.execute("SELECT generation FROM execution_forecast_generation").fetchone()[0] == before
    assert selected(owner(store), current) == expected == [later["forecast_id"]]


def test_payload_and_revision_use_one_coherent_wal_snapshot(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "coherent.sqlite3")
    current = put(store, "current")
    later = put(store, "later", offset=15)
    validate, changed = store._forecast_value, []
    def concurrently_changed(row):
        if not changed:
            with sqlite3.connect(store.path) as db:
                db.execute("UPDATE forecasts SET payload='broken' WHERE id=?", (later["forecast_id"],))
            changed.append(True)
        return validate(row)
    monkeypatch.setattr(store, "_forecast_value", concurrently_changed)
    assert selected(owner(store), current) == [later["forecast_id"]] and changed == [True]
    with pytest.raises(json.JSONDecodeError):
        selected(owner(store), current)


@pytest.mark.parametrize("damage", ["missing_trigger", "altered_trigger", "missing_row", "altered_table"])
def test_missing_or_changed_revision_evidence_cannot_authorize_cached_header(tmp_path, damage):
    store = ShadowStore(tmp_path / "markers.sqlite3")
    current = put(store, "current")
    selected(owner(store), current)
    with sqlite3.connect(store.path) as db:
        if damage == "missing_trigger":
            db.execute("DROP TRIGGER execution_forecast_update")
        elif damage == "altered_trigger":
            db.execute("DROP TRIGGER execution_forecast_update")
            db.execute("CREATE TRIGGER execution_forecast_update AFTER UPDATE ON forecasts BEGIN SELECT 1; END")
        elif damage == "altered_table":
            db.execute("ALTER TABLE execution_forecast_revisions ADD COLUMN forged TEXT")
        else:
            db.execute("DELETE FROM execution_forecast_revisions WHERE forecast_id=?", (current["forecast_id"],))
    with pytest.raises(ContractError, match="[Ee]xecution forecast"):
        selected(owner(store), current)


def remove_revision_schema(db):
    names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'execution_%'")]
    for name in names:
        db.execute("DROP TRIGGER " + name)
    db.execute("DROP TABLE execution_forecast_revisions")
    db.execute("DROP TABLE execution_forecast_generation")


@pytest.mark.parametrize("damage", ["delete_generation", "regress_generation", "replace_generation", "regress_revision", "replace_revision"])
def test_direct_marker_regression_is_rejected_atomically(tmp_path, damage):
    store = ShadowStore(tmp_path / "direct-markers.sqlite3")
    current = put(store, "current")
    assert selected(owner(store), current) == []
    with sqlite3.connect(store.path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="Execution forecast"):
            if damage == "delete_generation":
                db.execute("DELETE FROM execution_forecast_generation")
            elif damage == "regress_generation":
                db.execute("UPDATE execution_forecast_generation SET generation=0")
            elif damage == "replace_generation":
                db.execute("INSERT OR REPLACE INTO execution_forecast_generation SELECT singleton,0,epoch FROM execution_forecast_generation")
            elif damage == "regress_revision":
                db.execute("UPDATE execution_forecast_revisions SET generation=0")
            else:
                db.execute("INSERT OR REPLACE INTO execution_forecast_revisions VALUES(?,0)", (current["forecast_id"],))
    assert selected(owner(store), current) == []


def test_upgrade_preserves_original_bytes_and_reopen_preserves_generation(tmp_path):
    path = tmp_path / "existing.sqlite3"
    store = ShadowStore(path)
    current, later = put(store, "current"), put(store, "later", offset=15)
    with store.connect() as db:
        originals = db.execute("SELECT * FROM forecasts ORDER BY rowid").fetchall()
        remove_revision_schema(db)
    upgraded = ShadowStore(path)
    with upgraded.connect() as db:
        assert db.execute("SELECT * FROM forecasts ORDER BY rowid").fetchall() == originals
    assert selected(owner(upgraded), current) == [later["forecast_id"]]
    put(upgraded, "new", offset=30)
    with upgraded.connect() as db:
        generation = db.execute("SELECT generation FROM execution_forecast_generation").fetchone()[0]
    reopened = ShadowStore(path)
    with reopened.connect() as db:
        assert db.execute("SELECT generation FROM execution_forecast_generation").fetchone()[0] == generation


def test_partial_schema_is_not_silently_recreated_on_reopen(tmp_path):
    path = tmp_path / "partial.sqlite3"
    store = ShadowStore(path)
    with store.connect() as db:
        db.execute("DROP TRIGGER execution_forecast_update")
    with pytest.raises(ContractError, match="revision schema"):
        ShadowStore(path)


def test_drop_restore_invalidates_warm_headers_even_with_identical_marker_values(tmp_path):
    path = tmp_path / "restore.sqlite3"
    store = ShadowStore(path)
    current = put(store, "current")
    assert selected(owner(store), current) == []
    with store.connect() as db:
        remove_revision_schema(db)
        db.execute("UPDATE forecasts SET payload='broken' WHERE id=?", (current["forecast_id"],))
    ShadowStore(path)
    with pytest.raises(json.JSONDecodeError):
        selected(owner(store), current)


def test_physical_database_replacement_invalidates_cached_headers(tmp_path):
    path, replacement = tmp_path / "original.sqlite3", tmp_path / "replacement.sqlite3"
    store, other = ShadowStore(path), ShadowStore(replacement)
    current = put(store, "current")
    assert selected(owner(store), current) == []
    put(other, "current")
    with other.connect() as db:
        db.execute("UPDATE forecasts SET payload='broken' WHERE id=?", (current["forecast_id"],))
    with other.connect() as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    with store.connect() as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    # All SQLite connections are closed, so replacement carries no stale WAL.
    replacement.replace(path)
    with pytest.raises(json.JSONDecodeError):
        selected(owner(store), current)


def test_blob_noncanonical_encodings_and_scope_match_original_selection(tmp_path):
    store = ShadowStore(tmp_path / "encoding.sqlite3")
    current = put(store, "current")
    later = put(store, "later", offset=15, details={"unicode":"\u96ea", "tiny":1e-7})
    put(store, "peer", pc="Scout", offset=45)
    put(store, "other", symbol="AMZN", offset=45)
    expected = selected(owner(store), current)
    payload = json.dumps(dict(reversed(list(later.items()))), ensure_ascii=False, indent=2)
    with store.connect() as db:
        db.execute("UPDATE forecasts SET payload=? WHERE id=?", (payload, later["forecast_id"]))
    assert selected(owner(store), current) == original_selection(owner(store), current) == expected
    with store.connect() as db:
        db.execute("UPDATE forecasts SET payload=CAST(payload AS BLOB) WHERE id=?", (later["forecast_id"],))
    assert selected(owner(store), current) == original_selection(owner(store), current) == expected
    assert store.execution_forecast_headers(pc="Atlas", symbol="AAPL", forecast_ids=[]) == []
    assert [v["forecast_id"] for v in store.execution_forecast_headers(pc="Atlas", symbol="AAPL", forecast_ids=[later["forecast_id"]])] == expected


def test_independent_connections_publish_new_rows_to_warm_chronology(tmp_path):
    path = tmp_path / "connections.sqlite3"
    reader, writer = ShadowStore(path), ShadowStore(path)
    current = put(writer, "current")
    assert selected(owner(reader), current) == []
    later = put(writer, "later", offset=15)
    assert selected(owner(reader), current) == [later["forecast_id"]]
    with writer.connect() as db:
        db.execute("DELETE FROM forecasts WHERE id=?", (later["forecast_id"],))
    assert selected(owner(reader), current) == []


def test_final_supersession_still_runs_immediately_before_post(env):
    current = env.forecast()
    env.broker.before_post_hook = lambda: env.forecast(offset=15)
    result = env.executor.step(current)
    assert result["disposition"] == "not_submitted" and not env.broker.submissions
    assert result["evidence"]["submission_stage"] == "latest_forecast"
    assert result["evidence"]["root_cause"]["message"] == "Newer current-session forecast supersedes this intention"


def test_repeated_model_tree_reads_cannot_age_quote_past_final_guard(env, monkeypatch):
    current = env.forecast()
    old = put(env.store, "expensive-original", offset=-60,
              model_diagnostics={"tree":"retained-scientific-evidence" * 10000})
    worker = WorkerClock(env, monkeypatch)
    original, reads = env.store._forecast_projection, []
    def slow_original_read(row):
        result = original(row)
        if row[0] == old["forecast_id"]:
            reads.append(row[0])
            worker.tick(20)
            worker.tick(15)
        return result
    monkeypatch.setattr(env.store, "_forecast_projection", slow_original_read)
    result = env.executor.step(current)
    assert result["disposition"] == "submitted", result
    assert reads == [old["forecast_id"]] and len(env.broker.submissions) == 1
    assert env.store.forecast(old["cycle_id"], old["symbol"]) == old


def test_execution_cache_is_bounded_and_concurrent_reads_are_safe(tmp_path):
    store = ShadowStore(tmp_path / "bounded.sqlite3")
    current = put(store, "current")
    rows = []
    for index in range(4100):
        value = forecast(cycle_id="row-" + str(index), mode="live")
        value["forecast_id"] = digest(value)
        rows.append((*(value[field] for field in ("forecast_id", "pc", "symbol", "cycle_id", "reference_at", "issued_at")), encoded(value)))
    with store.connect() as db:
        db.executemany("INSERT INTO forecasts VALUES(?,?,?,?,?,?,?)", rows)
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert list(pool.map(lambda _: selected(owner(store), current), range(3))) == [[], [], []]
    assert len(store._execution_forecast_cache) <= 4096
