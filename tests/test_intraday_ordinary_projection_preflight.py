"""Ordinary projections validate complete originals before reserving a writer."""
from contextlib import closing, contextmanager
from copy import deepcopy
import gc
import json
import socket
import sqlite3
import weakref

import pytest

from ml.intraday.intentions import IntentionStore, VERSION
from ml.intraday.prediction import ContractError, digest, encoded
from ml.intraday.store import ShadowStore, _EXECUTION_FORECAST_SCHEMA
from ml.intraday.technical_sizing import SizingResult
from tests.test_intraday_store import forecast


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Ordinary projection fixtures cannot contact providers")
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
    arguments.update(changes)
    return intents.record(original, **arguments)


def rows(store):
    with store.connect() as db:
        return {table: db.execute("SELECT * FROM " + table + " ORDER BY rowid").fetchall()
                for table in ("intents", "intraday_intention_events", "intraday_intentions", "forecasts")}


def interleave(store, monkeypatch, mutate, *, after=1):
    """Mutate only after the reader has committed and closed its snapshot."""
    connect = store.connect
    calls = []

    @contextmanager
    def connections():
        calls.append(True)
        index = len(calls)
        with connect() as db:
            yield db
        if index == after:
            with sqlite3.connect(store.path, timeout=0) as other:
                mutate(other)

    monkeypatch.setattr(store, "connect", connections)
    return calls


def test_full_original_validation_allows_independent_writer_commit(tmp_path, monkeypatch):
    store, original, intents = prepared(tmp_path)
    with store.connect() as db:
        db.execute("CREATE TABLE independent_writer(stage TEXT)")
    validate, phases = store._forecast_value, []

    def checked(row):
        # timeout=0 is a real lock-occupancy assertion, without timing/sleeps.
        with sqlite3.connect(store.path, timeout=0) as other:
            other.execute("BEGIN IMMEDIATE")
            other.execute("INSERT INTO independent_writer VALUES('full_original')")
        phases.append("reader")
        return validate(row)

    monkeypatch.setattr(store, "_forecast_value", checked)
    result = record(intents, original)
    assert phases == ["reader"]
    assert result["forecast_id"] == original["forecast_id"]
    with store.connect() as db:
        assert db.execute("SELECT * FROM independent_writer").fetchall() == [("full_original",)]


@pytest.mark.parametrize("change", ["nested", "extra", "probability"])
@pytest.mark.parametrize("when", ["before", "between"])
def test_full_caller_equality_rejects_nested_and_extra_mutations(tmp_path, monkeypatch, change, when):
    store, original, intents = prepared(tmp_path)
    caller = deepcopy(original)
    before = rows(store)

    def mutate(_db=None):
        if change == "nested":
            caller["models"][0]["history"]["nested"][1]["original"] = False
        elif change == "extra":
            caller["unrecorded_extra"] = ["forged"]
        else:
            caller["probability_not_down"] = .73

    if when == "between":
        interleave(store, monkeypatch, mutate)
    else:
        mutate()
    with pytest.raises(ContractError, match="original saved forecast"):
        record(intents, caller)
    assert rows(store) == before


@pytest.mark.parametrize("column", ["id", "pc", "symbol", "cycle_id", "reference_at", "issued_at", "payload"])
def test_changed_original_between_snapshots_is_fully_revalidated(tmp_path, monkeypatch, column):
    store, original, intents = prepared(tmp_path)
    before = rows(store)
    validate, validated = store._forecast_value, []

    def checked(row):
        validated.append(row)
        return validate(row)

    def mutate(db):
        if column == "payload":
            raw = db.execute("SELECT payload FROM forecasts").fetchone()[0]
            changed = raw.replace('"probability_not_down":0.72', '"probability_not_down":0.73')
            assert raw != changed and len(raw) == len(changed)
            db.execute("UPDATE forecasts SET payload=?", (changed,))
        else:
            db.execute("UPDATE forecasts SET " + column + "='changed'")

    monkeypatch.setattr(store, "_forecast_value", checked)
    interleave(store, monkeypatch, mutate)
    with pytest.raises(ContractError):
        record(intents, original)
    assert len(validated) == (1 if column == "id" else 2)
    after = rows(store)
    assert after["intents"] == before["intents"]
    assert after["intraday_intention_events"] == before["intraday_intention_events"]


@pytest.mark.parametrize("operation", ["delete", "reinsert", "replace", "rollback", "reencode"])
def test_original_row_revision_and_rollback_preserve_semantics(tmp_path, monkeypatch, operation):
    store, original, intents = prepared(tmp_path)
    validate, validated = store._forecast_value, []

    def checked(row):
        validated.append(row)
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
            db.execute("UPDATE forecasts SET payload=?", (json.dumps(original, indent=2),))

    monkeypatch.setattr(store, "_forecast_value", checked)
    interleave(store, monkeypatch, mutate)
    if operation == "delete":
        with pytest.raises(ContractError, match="original saved forecast"):
            record(intents, original)
    else:
        assert record(intents, original)["forecast_id"] == original["forecast_id"]
    assert len(validated) == (1 if operation in {"delete", "rollback"} else 2)


@pytest.mark.parametrize("damage", ["missing_trigger", "changed_trigger", "missing_revision",
    "changed_table", "invalid_revision", "epoch", "generation_regression"])
def test_optional_marker_damage_falls_back_to_original_validator(tmp_path, monkeypatch, damage):
    store, original, intents = prepared(tmp_path)
    validate, validated = store._forecast_value, []

    def checked(row):
        validated.append(row)
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
        else:
            db.execute("DROP TRIGGER execution_generation_update")
            if damage == "epoch":
                db.execute("UPDATE execution_forecast_generation SET epoch=?", ("a" * 32,))
            else:
                db.execute("UPDATE execution_forecast_generation SET generation=0")
            db.execute(_EXECUTION_FORECAST_SCHEMA["execution_generation_update"])

    monkeypatch.setattr(store, "_forecast_value", checked)
    interleave(store, monkeypatch, mutate)
    assert record(intents, original)["forecast_id"] == original["forecast_id"]
    assert len(validated) == 2


def test_markers_unavailable_before_read_do_not_become_a_new_prerequisite(tmp_path, monkeypatch):
    store, original, intents = prepared(tmp_path)
    with store.connect() as db:
        db.execute("DROP TRIGGER execution_forecast_update")
    validated, validate = [], store._forecast_value

    def checked(row):
        validated.append(row)
        return validate(row)

    monkeypatch.setattr(store, "_forecast_value", checked)
    assert record(intents, original)["forecast_id"] == original["forecast_id"]
    assert len(validated) == 2


def test_certain_physical_identity_is_required_for_fast_path(tmp_path, monkeypatch):
    store, original, intents = prepared(tmp_path)
    connect, validate = store.connect, store._forecast_value
    validated, connections = [], []

    def checked(row):
        validated.append(row)
        return validate(row)

    @contextmanager
    def uncertain():
        with connect() as db:
            connections.append(True)
            if len(connections) == 2:
                store._connection_source_identities[id(db)] = None
            yield db

    monkeypatch.setattr(store, "connect", uncertain)
    monkeypatch.setattr(store, "_forecast_value", checked)
    assert record(intents, original)["forecast_id"] == original["forecast_id"]
    assert len(validated) == 2


@pytest.mark.parametrize("corrupt", [False, True])
def test_physical_database_replacement_revalidates_full_original(tmp_path, monkeypatch, corrupt):
    store, original, intents = prepared(tmp_path)
    replacement = tmp_path / "replacement.sqlite3"
    with closing(sqlite3.connect(store.path)) as db, closing(sqlite3.connect(replacement)) as target:
        db.backup(target)
    if corrupt:
        with closing(sqlite3.connect(replacement)) as db, db:
            db.execute("UPDATE forecasts SET payload='broken'")
    connect, validated, validate, calls = store.connect, [], store._forecast_value, []

    def checked(row):
        validated.append(row)
        return validate(row)

    @contextmanager
    def replaced():
        calls.append(True)
        index = len(calls)
        with connect() as db:
            yield db
        if index == 1:
            # Reader connection is now closed; the replacement is independent.
            replacement.replace(store.path)

    monkeypatch.setattr(store, "connect", replaced)
    monkeypatch.setattr(store, "_forecast_value", checked)
    if corrupt:
        with pytest.raises(json.JSONDecodeError):
            record(intents, original)
    else:
        assert record(intents, original)["forecast_id"] == original["forecast_id"]
    assert len(validated) == 2


def test_each_call_fullvalidates_independent_original_and_keeps_idempotence(tmp_path, monkeypatch):
    store, original, intents = prepared(tmp_path)
    validate, validated = store._forecast_value, []

    def checked(row):
        result = validate(row)
        assert result is not original and result["models"] is not original["models"]
        validated.append(True)
        return result

    monkeypatch.setattr(store, "_forecast_value", checked)
    expected = record(intents, original)
    before = rows(store)
    assert record(intents, deepcopy(original), recorded_at="2026-10-08T17:18:00Z") == expected
    assert validated == [True, True]
    assert rows(store) == before
    assert not any(name.startswith("_ordinary") for name in vars(intents))


def test_failed_append_rolls_back_and_retry_revalidates(tmp_path, monkeypatch):
    store, original, intents = prepared(tmp_path)
    before = rows(store)
    append = intents._append_validated

    def fail(*args, **kwargs):
        append(*args, **kwargs)
        raise RuntimeError("synthetic append failure")

    monkeypatch.setattr(intents, "_append_validated", fail)
    with pytest.raises(RuntimeError, match="append failure"):
        record(intents, original)
    assert rows(store) == before and intents._recovery_row_highwater == 0
    monkeypatch.setattr(intents, "_append_validated", append)
    assert record(intents, original)["forecast_id"] == original["forecast_id"]


@pytest.mark.parametrize("failed", [False, True])
def test_complete_original_is_released_after_each_call(tmp_path, monkeypatch, failed):
    store, original, intents = prepared(tmp_path)
    validate, references = store._forecast_value, []

    class Original(dict):
        pass

    def checked(row):
        value = Original(validate(row))
        value["models"] = [Original(model) for model in value["models"]]
        references.extend(weakref.ref(tree) for tree in (value, *value["models"]))
        return value

    monkeypatch.setattr(store, "_forecast_value", checked)
    if failed:
        append = intents._append_validated

        def fail(*args, **kwargs):
            append(*args, **kwargs)
            raise RuntimeError("synthetic failure")

        monkeypatch.setattr(intents, "_append_validated", fail)
        with pytest.raises(RuntimeError, match="synthetic failure"):
            record(intents, original)
    else:
        record(intents, original)
    gc.collect()
    assert references and all(reference() is None for reference in references)


@pytest.mark.parametrize("encoding", ["blob", "indented"])
def test_original_noncanonical_encodings_retain_full_semantics(tmp_path, encoding):
    store, original, intents = prepared(tmp_path)
    raw = json.dumps(original, indent=2)
    with store.connect() as db:
        db.execute("UPDATE forecasts SET payload=?", (raw.encode() if encoding == "blob" else raw,))
    assert record(intents, original)["forecast_id"] == original["forecast_id"]


def test_schema_version_change_revalidates_unchanged_original(tmp_path, monkeypatch):
    store, original, intents = prepared(tmp_path)
    validate, validated = store._forecast_value, []

    def checked(row):
        validated.append(True)
        return validate(row)

    monkeypatch.setattr(store, "_forecast_value", checked)
    interleave(store, monkeypatch, lambda db: db.execute("CREATE TABLE unrelated_schema_change(value TEXT)"))
    assert record(intents, original)["forecast_id"] == original["forecast_id"]
    assert validated == [True, True]


def test_corrupted_same_identity_is_rejected_before_writer(tmp_path):
    store, original, intents = prepared(tmp_path)
    with store.connect() as db:
        changed = deepcopy(original)
        changed["models"][0]["history"]["nested"][0] = 999
        db.execute("UPDATE forecasts SET payload=?", (json.dumps(changed),))
    before = rows(store)
    with pytest.raises(ContractError, match="hash or indexed identity changed"):
        record(intents, original)
    assert rows(store) == before


def sizing(original, **changes):
    value = dict(forecast_id=original["forecast_id"], symbol=original["symbol"], side="BUY",
        status="sized", desired_quantity=4, calculated_at="2026-10-08T17:16:03Z")
    value.update(changes)
    return SizingResult(digest(value), encoded(value))


def test_sizing_full_original_validation_allows_independent_writer_commit(tmp_path, monkeypatch):
    store, original, intents = prepared(tmp_path)
    with store.connect() as db:
        db.execute("CREATE TABLE independent_writer(stage TEXT)")
    validate, phases = store._forecast_value, []

    def checked(row):
        with closing(sqlite3.connect(store.path, timeout=0)) as other, other:
            other.execute("BEGIN IMMEDIATE")
            other.execute("INSERT INTO independent_writer VALUES('full_original')")
        phases.append(True)
        return validate(row)

    monkeypatch.setattr(store, "_forecast_value", checked)
    assert intents.save_sizing(original, sizing(original))["desired_quantity"] == 4
    # Sizing and its subsequent ordinary projection each independently validate
    # the complete original in their own closed reader, with no tree reuse.
    assert phases == [True, True]


@pytest.mark.parametrize("damage", ["nested_caller", "extra_caller", "original_payload", "original_index",
    "missing_marker", "schema_change", "epoch_change"])
def test_sizing_rechecks_full_original_and_caller_after_reader(tmp_path, monkeypatch, damage):
    store, original, intents = prepared(tmp_path)
    caller = deepcopy(original)
    validate, validated = store._forecast_value, []

    def checked(row):
        validated.append(True)
        return validate(row)

    def mutate(db):
        if damage == "nested_caller":
            caller["models"][0]["history"]["nested"].append("forged")
        elif damage == "extra_caller":
            caller["extra"] = {"forged": True}
        elif damage == "original_payload":
            db.execute("UPDATE forecasts SET payload='broken'")
        elif damage == "original_index":
            db.execute("UPDATE forecasts SET pc='Scout'")
        elif damage == "missing_marker":
            db.execute("DELETE FROM execution_forecast_revisions")
        elif damage == "schema_change":
            db.execute("CREATE TABLE unrelated_schema_change(value TEXT)")
        else:
            db.execute("DROP TRIGGER execution_generation_update")
            db.execute("UPDATE execution_forecast_generation SET epoch=?", ("b" * 32,))
            db.execute(_EXECUTION_FORECAST_SCHEMA["execution_generation_update"])

    monkeypatch.setattr(store, "_forecast_value", checked)
    interleave(store, monkeypatch, mutate, after=2)  # get, closed preflight, writer
    if damage in {"nested_caller", "extra_caller", "original_payload", "original_index"}:
        with pytest.raises((ContractError, json.JSONDecodeError)):
            intents.save_sizing(caller, sizing(original))
        state = rows(store)
        assert not state["intraday_intentions"] and not state["intraday_intention_events"]
    else:
        assert intents.save_sizing(caller, sizing(original))["desired_quantity"] == 4
        assert len(validated) >= 3  # changed sizing boundary plus independent record


def test_sizing_concurrent_immutable_intention_is_not_overwritten(tmp_path, monkeypatch):
    store, original, intents = prepared(tmp_path)
    other = sizing(original, desired_quantity=5)
    value = dict(version=VERSION, forecast_id=original["forecast_id"], symbol=original["symbol"],
        side="BUY", desired_quantity=5, sizing_id=other.result_id, sizing=other.payload())

    def insert(db):
        db.execute("INSERT INTO intraday_intentions VALUES(?,?,?)",
                   (original["forecast_id"], digest(value), encoded(value)))

    interleave(store, monkeypatch, insert, after=2)
    with pytest.raises(ContractError, match="immutable"):
        intents.save_sizing(original, sizing(original))
    assert intents.get(original["forecast_id"]) == value
    assert not rows(store)["intraday_intention_events"]


def test_sizing_final_writer_failure_rolls_back_original_and_projection(tmp_path, monkeypatch):
    store, original, intents = prepared(tmp_path)
    before, connect, calls = rows(store), store.connect, []

    @contextmanager
    def fail_writer():
        calls.append(True)
        index = len(calls)
        with connect() as db:
            yield db
            if index == 3:
                raise RuntimeError("synthetic sizing writer failure")

    monkeypatch.setattr(store, "connect", fail_writer)
    with pytest.raises(RuntimeError, match="sizing writer failure"):
        intents.save_sizing(original, sizing(original))
    assert rows(store) == before and intents._recovery_row_highwater == 0
    monkeypatch.setattr(store, "connect", connect)
    result = intents.save_sizing(original, sizing(original))
    assert intents.save_sizing(original, sizing(original)) == result


def test_full_preflight_tree_releases_before_any_sizing_research_decode(tmp_path, monkeypatch):
    store, original, intents = prepared(tmp_path)
    validate, references, sizing_validate = store._forecast_value, [], intents._value

    class Original(dict):
        pass

    def checked(row):
        value = Original(validate(row))
        references.append(weakref.ref(value))
        return value

    def checked_sizing(row):
        assert all(reference() is None for reference in references)
        return sizing_validate(row)

    monkeypatch.setattr(store, "_forecast_value", checked)
    monkeypatch.setattr(intents, "_value", checked_sizing)
    intents.save_sizing(original, sizing(original))
    assert len(references) == 2 and all(reference() is None for reference in references)
