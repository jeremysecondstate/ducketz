"""Original row revisions remove warm recovery payload reads, not validation guards."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import json
import re
import socket
import sqlite3

import pytest

from ml.intraday import intentions as module
from ml.intraday.intentions import IntentionStore
from ml.intraday.prediction import ContractError, digest, encoded
from ml.intraday.store import ShadowStore
from ml.intraday.technical_sizing import SizingResult
from tests.test_intraday_recovery_grouping import group, observation, projection_rows
from tests.test_intraday_recovery_sizing_projection import prepared
from tests.test_intraday_store import forecast


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Recovery row-revision fixtures must not contact a provider")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def watch(store, monkeypatch):
    statements, raw_hashes = [], []
    with store.connect() as db:
        originals = {row[0].encode() for table in ("forecasts", "intraday_intentions")
                     for row in db.execute("SELECT payload FROM " + table)}
    connect, sha = store.connect, module.hashlib.sha256

    @contextmanager
    def traced():
        with connect() as db:
            db.set_trace_callback(statements.append)
            yield db

    def hashed(raw=b""):
        if raw in originals:
            raw_hashes.append(len(raw))
        return sha(raw)

    monkeypatch.setattr(store, "connect", traced)
    monkeypatch.setattr(module.hashlib, "sha256", hashed)
    return statements, raw_hashes


def payload_reads(statements):
    return [sql for sql in statements if re.search(
        r"\bSELECT\b.*\bpayload\b.*\bFROM (?:forecasts|intraday_intentions)\b", sql, re.I | re.S)]


def remove_schema(db):
    for name in module._RECOVERY_ROW_SCHEMA:
        if "CREATE TRIGGER" in module._RECOVERY_ROW_SCHEMA[name]:
            db.execute("DROP TRIGGER " + name)
    db.execute("DROP TABLE recovery_sizing_revisions")
    db.execute("DROP TABLE recovery_sizing_generation")


@contextmanager
def external(path):
    db = sqlite3.connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()


def test_cold_and_warm_exact_group_validate_originals_without_warm_payload_reads(tmp_path, monkeypatch):
    store, original, intents, order = prepared(tmp_path)
    # First sizing records an ordinary projection and warms its protected row.
    # Reopen to exercise genuinely cold recovery rather than construction work.
    intents = IntentionStore(store)
    filled = replace(order, reservation_id="filled", status="FILLED", broker_order_id="exact-broker",
                     filled_quantity=order.quantity)
    unknown = replace(order, reservation_id="unknown", status="UNKNOWN")
    items = (observation(filled), observation(unknown))
    statements, hashes = watch(store, monkeypatch)
    expected = group(intents, original, *items)
    assert len(payload_reads(statements)) == 2 and len(hashes) >= 2
    before = projection_rows(store)
    statements.clear(); hashes.clear()
    assert group(intents, original, *items) == expected
    assert payload_reads(statements) == [] and hashes == []
    assert projection_rows(store) == before
    assert expected[0]["submitted_quantity"] == expected[0]["filled_quantity"] == order.quantity
    assert expected[1]["submitted_quantity"] is None and expected[1]["filled_quantity"] == 0
    assert not any(isinstance(value, (dict, list)) for value in intents._recovery_row_cache[original["forecast_id"]])


@pytest.mark.parametrize("table,column", [("forecasts", column) for column in
    ("id", "pc", "symbol", "cycle_id", "reference_at", "issued_at", "payload")] +
    [("intraday_intentions", column) for column in ("forecast_id", "id", "payload")])
def test_every_index_or_same_length_original_body_change_invalidates_warm_replay(tmp_path, table, column):
    store, original, intents, order = prepared(tmp_path)
    item = observation(order)
    group(intents, original, item)
    before = projection_rows(store)
    with external(store.path) as db:
        if column == "payload":
            raw = db.execute("SELECT payload FROM " + table).fetchone()[0]
            changed = raw.replace('"probability_not_down":0.72', '"probability_not_down":0.73') if table == "forecasts" else raw.replace('"desired_quantity":4', '"desired_quantity":5')
            assert changed != raw and len(changed) == len(raw)
            db.execute("UPDATE " + table + " SET payload=?", (changed,))
        else:
            db.execute("UPDATE " + table + " SET " + column + "='changed'")
    with pytest.raises(ContractError):
        group(intents, original, item)
    assert projection_rows(store) == before


def test_valid_changed_sizing_id_and_payload_do_not_reuse_cached_scalar_facts(tmp_path):
    store, original, intents, order = prepared(tmp_path)
    group(intents, original, observation(order))
    with store.connect() as db:
        value = json.loads(db.execute("SELECT payload FROM intraday_intentions").fetchone()[0])
        value["desired_quantity"] = 3
        db.execute("UPDATE intraday_intentions SET id=?,payload=?", (digest(value), encoded(value)))
    changed = group(intents, original, observation(order, key="new-valid-sizing-evidence"))[0]
    assert changed["desired_quantity"] == 3


@pytest.mark.parametrize("operation", ["delete", "reinsert", "replace", "rollback"])
def test_delete_reinsert_replace_and_rollback_have_coherent_row_revisions(tmp_path, monkeypatch, operation):
    store, original, intents, order = prepared(tmp_path)
    item = observation(order)
    expected = group(intents, original, item)
    with store.connect() as db:
        row = db.execute("SELECT * FROM intraday_intentions").fetchone()
    statements, _ = watch(store, monkeypatch)
    with external(store.path) as db:
        db.execute("BEGIN IMMEDIATE")
        if operation in {"delete", "reinsert"}:
            db.execute("DELETE FROM intraday_intentions")
            if operation == "reinsert":
                db.execute("INSERT INTO intraday_intentions VALUES(?,?,?)", row)
        elif operation == "replace":
            db.execute("INSERT OR REPLACE INTO intraday_intentions VALUES(?,?,?)", row)
        else:
            db.execute("UPDATE intraday_intentions SET payload='broken'")
            db.rollback()
    statements.clear()
    if operation == "delete":
        with pytest.raises(ContractError, match="original persisted intention"):
            group(intents, original, item)
    else:
        assert group(intents, original, item) == expected
        assert len(payload_reads(statements)) == (0 if operation == "rollback" else 1)


@pytest.mark.parametrize("encoding", ["blob", "indent", "unicode"])
def test_full_original_semantics_support_noncanonical_and_blob_sizing(tmp_path, encoding):
    store, original, intents, order = prepared(tmp_path)
    expected = group(intents, original, observation(order))
    with store.connect() as db:
        value = json.loads(db.execute("SELECT payload FROM intraday_intentions").fetchone()[0])
        raw = json.dumps(value, indent=2 if encoding == "indent" else None, ensure_ascii=encoding != "unicode")
        db.execute("UPDATE intraday_intentions SET payload=?", (raw.encode() if encoding == "blob" else raw,))
    assert group(intents, original, observation(order)) == expected
    assert group(intents, original, observation(order)) == expected


@pytest.mark.parametrize("damage", ["missing_trigger", "changed_trigger", "missing_row", "changed_table", "missing_generation"])
def test_partial_or_changed_marker_evidence_never_authorizes_cache(tmp_path, damage):
    store, original, intents, order = prepared(tmp_path)
    group(intents, original, observation(order))
    with store.connect() as db:
        if damage in {"missing_trigger", "changed_trigger"}:
            db.execute("DROP TRIGGER recovery_sizing_update")
            if damage == "changed_trigger":
                db.execute("CREATE TRIGGER recovery_sizing_update AFTER UPDATE ON intraday_intentions BEGIN SELECT 1; END")
        elif damage == "missing_row":
            db.execute("DELETE FROM recovery_sizing_revisions")
        elif damage == "changed_table":
            db.execute("ALTER TABLE recovery_sizing_revisions ADD COLUMN forged TEXT")
        else:
            db.execute("DROP TRIGGER recovery_generation_delete")
            db.execute("DELETE FROM recovery_sizing_generation")
    with pytest.raises(ContractError, match="Recovery sizing"):
        group(intents, original, observation(order))
    if damage != "missing_row":
        with pytest.raises(ContractError, match="Recovery sizing"):
            IntentionStore(store)


@pytest.mark.parametrize("operation", ["delete_generation", "regress_generation", "replace_generation", "change_epoch", "regress_revision", "replace_revision"])
def test_direct_marker_regression_or_replacement_is_rejected_atomically(tmp_path, operation):
    store, original, intents, order = prepared(tmp_path)
    expected = group(intents, original, observation(order))
    with external(store.path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="Recovery sizing"):
            if operation == "delete_generation":
                db.execute("DELETE FROM recovery_sizing_generation")
            elif operation == "regress_generation":
                db.execute("UPDATE recovery_sizing_generation SET generation=0")
            elif operation == "replace_generation":
                db.execute("INSERT OR REPLACE INTO recovery_sizing_generation SELECT singleton,0,epoch FROM recovery_sizing_generation")
            elif operation == "change_epoch":
                db.execute("UPDATE recovery_sizing_generation SET epoch='00000000000000000000000000000000',generation=generation+1")
            elif operation == "regress_revision":
                db.execute("UPDATE recovery_sizing_revisions SET generation=0")
            else:
                db.execute("INSERT OR REPLACE INTO recovery_sizing_revisions VALUES(?,0)", (original["forecast_id"],))
    assert group(intents, original, observation(order)) == expected


def test_absent_schema_upgrade_preserves_original_bytes_but_partial_schema_is_rejected(tmp_path):
    store, original, intents, order = prepared(tmp_path)
    expected = group(intents, original, observation(order))
    with store.connect() as db:
        originals = db.execute("SELECT * FROM intraday_intentions").fetchall()
        remove_schema(db)
    upgraded = IntentionStore(store)
    with store.connect() as db:
        assert db.execute("SELECT * FROM intraday_intentions").fetchall() == originals
    assert group(upgraded, original, observation(order)) == expected
    with store.connect() as db:
        db.execute("DROP TRIGGER recovery_sizing_update")
    with pytest.raises(ContractError, match="revision schema"):
        IntentionStore(store)


def test_epoch_replacement_invalidates_old_cache_even_when_row_markers_repeat(tmp_path):
    store, original, intents, order = prepared(tmp_path)
    item = observation(order)
    group(intents, original, item)
    with store.connect() as db:
        remove_schema(db)
        db.execute("UPDATE intraday_intentions SET payload='broken'")
    IntentionStore(store)
    with pytest.raises(json.JSONDecodeError):
        group(intents, original, item)


def test_physical_database_replacement_discards_cached_originals(tmp_path):
    store, original, intents, order = prepared(tmp_path)
    group(intents, original, observation(order))
    replacement = tmp_path / "replacement.sqlite3"
    with external(store.path) as source, external(replacement) as target:
        source.backup(target)
        target.execute("UPDATE intraday_intentions SET payload='broken'")
    with store.connect() as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    replacement.replace(store.path)
    with pytest.raises(json.JSONDecodeError):
        group(intents, original, observation(order))


def test_cache_eviction_reopen_and_detached_results_keep_full_validators(tmp_path, monkeypatch):
    store, first, intents, order = prepared(tmp_path)
    monkeypatch.setattr(module, "_RECOVERY_SIZING_CACHE_LIMIT", 1)
    expected = group(intents, first, observation(order))
    other = store.save_forecast(forecast(cycle_id="equal-time-other"))
    sized = dict(forecast_id=other["forecast_id"], symbol="AAPL", side="BUY", status="sized", desired_quantity=4,
                 calculated_at="2026-10-08T17:16:03Z")
    intents.save_sizing(other, SizingResult(digest(sized), encoded(sized)))
    other_order = replace(order, forecast_id=other["forecast_id"], reservation_id="other-original")
    group(intents, other, observation(other_order))
    assert list(intents._recovery_row_cache) == [other["forecast_id"]]
    result = group(intents, first, observation(order))
    result[0]["desired_quantity"] = 999
    assert group(intents, first, observation(order)) == expected
    assert group(IntentionStore(store), first, observation(order)) == expected


def test_equal_time_ids_and_invalid_second_member_keep_collision_and_atomicity_guards(tmp_path):
    store, original, intents, order = prepared(tmp_path)
    first = observation(order, key="existing", at="2026-10-08T17:18:00Z")
    group(intents, original, first)
    before = projection_rows(store)
    new = observation(replace(order, reservation_id="new"), key="new", at="2026-10-08T17:19:00Z")
    with pytest.raises(ContractError, match="identity reused"):
        group(intents, original, new, first | {"evidence": {"changed": True}})
    assert projection_rows(store) == before
    with pytest.raises(ContractError, match="backdated"):
        group(intents, original, new, observation(order, key="before-latest", at="2026-10-08T17:17:00Z"))
    assert projection_rows(store) == before


def test_concurrent_group_and_chronology_reads_follow_shared_lock_order(tmp_path):
    store, original, intents, order = prepared(tmp_path)
    item = observation(order)
    expected = group(intents, original, item)
    def read(index):
        if index % 2:
            return group(intents, original, item) == expected
        return store.execution_forecast_headers(pc="Atlas", symbol="AAPL")[0]["forecast_id"] == original["forecast_id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(read, range(24)))


def test_failed_member_does_not_advance_committed_sizing_watermark(tmp_path):
    store, original, intents, order = prepared(tmp_path)
    intents = IntentionStore(store)
    assert intents._recovery_row_highwater == 0
    with pytest.raises(ContractError):
        group(intents, original, observation(order, evidence={}))
    assert intents._recovery_row_highwater == 0
    group(intents, original, observation(order))
    assert intents._recovery_row_highwater > 0


def test_failed_transaction_exit_rolls_back_projection_and_does_not_publish_new_watermark(tmp_path, monkeypatch):
    store, original, intents, order = prepared(tmp_path)
    group(intents, original, observation(order))
    watermark = intents._recovery_row_highwater
    with store.connect() as db:
        value = json.loads(db.execute("SELECT payload FROM intraday_intentions").fetchone()[0])
        value["desired_quantity"] = 3
        db.execute("UPDATE intraday_intentions SET id=?,payload=?", (digest(value), encoded(value)))
    before = projection_rows(store)
    connect = store.connect
    @contextmanager
    def failed_exit():
        with connect() as db:
            yield db
            raise RuntimeError("synthetic transaction exit failure")
    monkeypatch.setattr(store, "connect", failed_exit)
    new = observation(order, key="after-new-committed-sizing")
    with pytest.raises(RuntimeError, match="transaction exit failure"):
        group(intents, original, new)
    assert intents._recovery_row_highwater == watermark
    monkeypatch.setattr(store, "connect", connect)
    assert projection_rows(store) == before
    assert group(intents, original, new)[0]["desired_quantity"] == 3
    assert intents._recovery_row_highwater > watermark
