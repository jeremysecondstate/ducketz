"""Ordinary main intentions release the writer during full original validation."""
from contextlib import closing, contextmanager
from copy import deepcopy
import gc
import json
import socket
import sqlite3
import weakref

import pytest

from ml.intraday.intentions import IntentionStore
from ml.intraday.prediction import ContractError, digest, encoded
from ml.intraday.store import ShadowStore, _EXECUTION_FORECAST_SCHEMA
from ml.intraday.technical_sizing import SizingResult
from tests.test_intraday_store import forecast


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Main intention fixtures cannot contact providers")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def prepared(tmp_path):
    store = ShadowStore(tmp_path / "own.sqlite3")
    value = forecast()
    value["models"][0]["history"] = {"nested": [1, {"original": True}]}
    original = store.save_forecast(value)
    return store, original, IntentionStore(store)


def record(intents, original, **changes):
    arguments = dict(event_key="ordinary", recorded_at="2026-10-08T17:17:00Z",
        disposition="no_capacity", evidence={"actual": "own-observation"}, kind="capacity")
    return intents.record(original, **(arguments | changes))


def sizing(original, **changes):
    value = dict(forecast_id=original["forecast_id"], symbol=original["symbol"], side="BUY",
        status="sized", desired_quantity=4, calculated_at="2026-10-08T17:16:03Z") | changes
    return SizingResult(digest(value), encoded(value))


def rows(store):
    with store.connect() as db:
        return {table: db.execute("SELECT * FROM " + table + " ORDER BY rowid").fetchall()
                for table in ("intents", "intraday_intention_events", "intraday_intentions", "forecasts")}


def after_preflight(store, intents, monkeypatch, mutate):
    preflight = intents._ordinary_forecast_preflight
    calls = []

    def read(forecast):
        value = preflight(forecast)
        calls.append(True)
        if len(calls) == 1:
            with closing(sqlite3.connect(store.path, timeout=0)) as other, other:
                mutate(other)
        return value

    monkeypatch.setattr(intents, "_ordinary_forecast_preflight", read)


@pytest.mark.parametrize("operation", ["record", "sizing"])
def test_full_original_validation_allows_independent_writer_commit(tmp_path, monkeypatch, operation):
    store, original, intents = prepared(tmp_path)
    with store.connect() as db:
        db.execute("CREATE TABLE independent_writer(stage TEXT)")
    validate, phases = store._forecast_value, []

    def checked(row):
        # This fails immediately if original decoding owns SQLite's writer.
        with closing(sqlite3.connect(store.path, timeout=0)) as other, other:
            other.execute("BEGIN IMMEDIATE")
            other.execute("INSERT INTO independent_writer VALUES('original')")
        phases.append(True)
        return validate(row)

    monkeypatch.setattr(store, "_forecast_value", checked)
    if operation == "record":
        assert record(intents, original)["forecast_id"] == original["forecast_id"]
        assert phases == [True]
    else:
        assert intents.save_sizing(original, sizing(original))["desired_quantity"] == 4
        assert phases == [True, True]
    with store.connect() as db:
        assert len(db.execute("SELECT * FROM independent_writer").fetchall()) == len(phases)


@pytest.mark.parametrize("operation", ["record", "sizing"])
@pytest.mark.parametrize("change", ["nested", "extra", "probability"])
@pytest.mark.parametrize("when", ["before", "between"])
def test_full_caller_equality_rejects_mutation(tmp_path, monkeypatch, operation, change, when):
    store, original, intents = prepared(tmp_path)
    caller, before = deepcopy(original), rows(store)

    def mutate(_db=None):
        if change == "nested":
            caller["models"][0]["history"]["nested"][1]["original"] = False
        elif change == "extra":
            caller["unrecorded_extra"] = ["forged"]
        else:
            caller["probability_not_down"] = .73

    if when == "between":
        after_preflight(store, intents, monkeypatch, mutate)
    else:
        mutate()
    with pytest.raises(ContractError):
        if operation == "record":
            record(intents, caller)
        else:
            intents.save_sizing(caller, sizing(original))
    assert rows(store) == before


@pytest.mark.parametrize("operation", ["record", "sizing"])
@pytest.mark.parametrize("column", ["id", "pc", "symbol", "cycle_id", "reference_at", "issued_at", "payload"])
def test_changed_original_between_snapshots_is_revalidated(tmp_path, monkeypatch, operation, column):
    store, original, intents = prepared(tmp_path)
    before = rows(store)
    validate, validated = store._forecast_value, []

    def checked(row):
        validated.append(True)
        return validate(row)

    def mutate(db):
        db.execute("UPDATE forecasts SET " + column + "='changed'")

    monkeypatch.setattr(store, "_forecast_value", checked)
    after_preflight(store, intents, monkeypatch, mutate)
    with pytest.raises((ContractError, json.JSONDecodeError)):
        if operation == "record":
            record(intents, original)
        else:
            intents.save_sizing(original, sizing(original))
    assert len(validated) == (1 if column == "id" else 2)
    after = rows(store)
    assert after["intents"] == before["intents"]
    assert after["intraday_intention_events"] == before["intraday_intention_events"]
    assert after["intraday_intentions"] == before["intraday_intentions"]


@pytest.mark.parametrize("operation", ["delete", "reinsert", "replace", "rollback", "reencode", "blob"])
def test_original_revision_changes_retain_full_semantics(tmp_path, monkeypatch, operation):
    store, original, intents = prepared(tmp_path)
    validate, validated = store._forecast_value, []

    def checked(row):
        validated.append(True)
        return validate(row)

    def mutate(db):
        row = db.execute("SELECT * FROM forecasts").fetchone()
        if operation in {"delete", "reinsert"}:
            db.execute("DELETE FROM forecasts")
            if operation == "reinsert":
                db.execute("INSERT INTO forecasts VALUES(?,?,?,?,?,?,?)", row)
        elif operation == "replace":
            db.execute("INSERT OR REPLACE INTO forecasts VALUES(?,?,?,?,?,?,?)", row)
        elif operation == "rollback":
            db.execute("UPDATE forecasts SET payload='broken'")
            db.rollback()
        else:
            raw = json.dumps(original, indent=2)
            db.execute("UPDATE forecasts SET payload=?", (raw.encode() if operation == "blob" else raw,))

    monkeypatch.setattr(store, "_forecast_value", checked)
    after_preflight(store, intents, monkeypatch, mutate)
    if operation == "delete":
        with pytest.raises(ContractError, match="original saved forecast"):
            record(intents, original)
    else:
        assert record(intents, original)["forecast_id"] == original["forecast_id"]
    assert len(validated) == (1 if operation in {"delete", "rollback"} else 2)


@pytest.mark.parametrize("damage", ["missing_trigger", "changed_trigger", "missing_revision",
    "changed_table", "invalid_revision", "epoch", "generation_regression", "schema_change"])
def test_uncertain_marker_falls_back_to_original_validation(tmp_path, monkeypatch, damage):
    store, original, intents = prepared(tmp_path)
    validate, validated = store._forecast_value, []

    def checked(row):
        validated.append(True)
        return validate(row)

    def mutate(db):
        if damage in {"missing_trigger", "changed_trigger"}:
            db.execute("DROP TRIGGER execution_forecast_update")
            if damage == "changed_trigger":
                db.execute("CREATE TRIGGER execution_forecast_update AFTER UPDATE ON forecasts BEGIN SELECT 1; END")
        elif damage == "missing_revision":
            db.execute("DELETE FROM execution_forecast_revisions")
        elif damage == "changed_table":
            db.execute("ALTER TABLE execution_forecast_revisions ADD COLUMN extra TEXT")
        elif damage == "invalid_revision":
            db.execute("DROP TRIGGER execution_revision_update")
            db.execute("UPDATE execution_forecast_revisions SET generation=999")
            db.execute(_EXECUTION_FORECAST_SCHEMA["execution_revision_update"])
        elif damage == "schema_change":
            db.execute("CREATE TABLE unrelated(value TEXT)")
        else:
            db.execute("DROP TRIGGER execution_generation_update")
            if damage == "epoch":
                db.execute("UPDATE execution_forecast_generation SET epoch=?", ("a" * 32,))
            else:
                db.execute("UPDATE execution_forecast_generation SET generation=0")
            db.execute(_EXECUTION_FORECAST_SCHEMA["execution_generation_update"])

    monkeypatch.setattr(store, "_forecast_value", checked)
    after_preflight(store, intents, monkeypatch, mutate)
    assert record(intents, original)["forecast_id"] == original["forecast_id"]
    assert validated == [True, True]


@pytest.mark.parametrize("corrupt", [False, True])
def test_physical_replacement_revalidates_original(tmp_path, monkeypatch, corrupt):
    store, original, intents = prepared(tmp_path)
    replacement = tmp_path / "replacement.sqlite3"
    with closing(sqlite3.connect(store.path)) as db, closing(sqlite3.connect(replacement)) as target:
        db.backup(target)
    if corrupt:
        with closing(sqlite3.connect(replacement)) as db, db:
            db.execute("UPDATE forecasts SET payload='broken'")
    validate, validated = store._forecast_value, []

    def checked(row):
        validated.append(True)
        return validate(row)

    monkeypatch.setattr(store, "_forecast_value", checked)
    preflight = intents._ordinary_forecast_preflight

    def replaced(forecast):
        value = preflight(forecast)
        # No SQLite handle is open when replacing the physical database.
        replacement.replace(store.path)
        return value

    monkeypatch.setattr(intents, "_ordinary_forecast_preflight", replaced)
    if corrupt:
        with pytest.raises(json.JSONDecodeError):
            record(intents, original)
    else:
        assert record(intents, original)["forecast_id"] == original["forecast_id"]
    assert validated == [True, True]


def test_marker_unavailable_is_not_a_new_prerequisite(tmp_path, monkeypatch):
    store, original, intents = prepared(tmp_path)
    with store.connect() as db:
        db.execute("DROP TRIGGER execution_forecast_update")
    assert record(intents, original)["forecast_id"] == original["forecast_id"]


def test_each_call_validates_and_releases_a_fresh_original(tmp_path, monkeypatch):
    store, original, intents = prepared(tmp_path)
    validate, references = store._forecast_value, []

    class Original(dict):
        pass

    def checked(row):
        result = Original(validate(row))
        assert result is not original and result["models"] is not original["models"]
        references.append(weakref.ref(result))
        return result

    monkeypatch.setattr(store, "_forecast_value", checked)
    first = record(intents, original)
    before = rows(store)
    assert record(intents, deepcopy(original), recorded_at="2026-10-08T17:18:00Z") == first
    assert rows(store) == before
    gc.collect()
    assert len(references) == 2 and all(ref() is None for ref in references)
    assert not any(name.startswith("_ordinary") for name in vars(intents))


def test_missing_original_intention_scope_survives_preflight(tmp_path):
    store, original, intents = prepared(tmp_path)
    before = rows(store)
    with pytest.raises(ContractError, match="original immutable intention"):
        record(intents, original, require_original_intention=True)
    assert rows(store) == before


def test_final_writer_rollback_preserves_original_and_retry(tmp_path, monkeypatch):
    store, original, intents = prepared(tmp_path)
    before, connect = rows(store), store.connect

    @contextmanager
    def fail_write():
        with connect() as db:
            yield db
            if db.in_transaction and db.total_changes:
                raise RuntimeError("synthetic writer failure")

    monkeypatch.setattr(store, "connect", fail_write)
    with pytest.raises(RuntimeError, match="synthetic writer failure"):
        record(intents, original)
    assert rows(store) == before
    monkeypatch.setattr(store, "connect", connect)
    assert record(intents, original)["forecast_id"] == original["forecast_id"]
