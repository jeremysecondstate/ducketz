"""Cold recovery validates without a reserved writer, then rechecks atomically."""
from contextlib import contextmanager
import json
import socket
import sqlite3
import weakref

import pytest

from ml.intraday import intentions as module
from ml.intraday.intentions import IntentionStore
from ml.intraday.prediction import ContractError, digest, encoded
from tests.test_intraday_recovery_grouping import group, observation, projection_rows
from tests.test_intraday_recovery_sizing_projection import prepared


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Recovery read-preflight fixtures must not contact a provider")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def cold(tmp_path):
    store, original, _, order = prepared(tmp_path)
    store._execution_forecast_cache.clear()
    getattr(store, "_forecast_header_cache", {}).clear()
    return store, original, IntentionStore(store), order


def between_preflight_and_writer(store, monkeypatch, mutate):
    """Apply one external committed change after the read snapshot has closed."""
    connect = store.connect
    calls = []

    @contextmanager
    def interleaved():
        calls.append(True)
        index = len(calls)
        with connect() as db:
            yield db
        if index == 1:
            with sqlite3.connect(store.path, timeout=0) as other:
                mutate(other)

    monkeypatch.setattr(store, "connect", interleaved)
    return calls


def test_competing_writer_commits_during_cold_original_reads_and_validation(tmp_path, monkeypatch):
    store, original, intents, order = cold(tmp_path)
    with store.connect() as db:
        db.execute("CREATE TABLE independent_writer(stage TEXT NOT NULL)")
    connect = store.connect
    stages = []

    def competitor(stage):
        # timeout=0 makes this a deterministic writer-occupancy check rather
        # than a timing/sleep assertion. WAL readers permit this real commit.
        with sqlite3.connect(store.path, timeout=0) as other:
            other.execute("BEGIN IMMEDIATE")
            other.execute("INSERT INTO independent_writer VALUES(?)", (stage,))
        stages.append(stage)

    class Cursor:
        def __init__(self, cursor, stage):
            self.cursor, self.stage = cursor, stage

        def fetchone(self):
            competitor(self.stage)
            return self.cursor.fetchone()

        def __getattr__(self, name):
            return getattr(self.cursor, name)

    class Connection:
        def __init__(self, db):
            self.db = db

        def execute(self, sql, *args):
            cursor = self.db.execute(sql, *args)
            if "FROM forecasts WHERE id=?" in sql:
                return Cursor(cursor, "forecast_payload_read")
            if "FROM intraday_intentions WHERE forecast_id=?" in sql:
                return Cursor(cursor, "sizing_payload_read")
            return cursor

        def __getattr__(self, name):
            return getattr(self.db, name)

    @contextmanager
    def traced():
        with connect() as db:
            yield Connection(db)

    monkeypatch.setattr(store, "connect", traced)
    for owner, name, stage in ((store, "_forecast_projection", "forecast_validate"),
                               (intents, "_recovery_sizing", "sizing_validate")):
        original_method = getattr(owner, name)

        def checked(*args, _original=original_method, _stage=stage, **kwargs):
            competitor(_stage)
            return _original(*args, **kwargs)

        monkeypatch.setattr(owner, name, checked)

    result = group(intents, original, observation(order))
    assert stages == ["forecast_payload_read", "forecast_validate",
                      "sizing_payload_read", "sizing_validate"]
    assert result[0]["desired_quantity"] == 4
    with connect() as db:
        assert [row[0] for row in db.execute("SELECT stage FROM independent_writer ORDER BY rowid")] == stages


@pytest.mark.parametrize("table,column", [("forecasts", column) for column in
    ("id", "pc", "symbol", "cycle_id", "reference_at", "issued_at", "payload")] +
    [("intraday_intentions", column) for column in ("forecast_id", "id", "payload")])
def test_changed_original_payload_or_index_between_passes_is_revalidated(tmp_path, monkeypatch, table, column):
    store, original, intents, order = cold(tmp_path)
    before = projection_rows(store)

    def mutate(db):
        if column == "payload":
            raw = db.execute("SELECT payload FROM " + table).fetchone()[0]
            changed = (raw.replace('"probability_not_down":0.72', '"probability_not_down":0.73')
                       if table == "forecasts" else raw.replace('"desired_quantity":4', '"desired_quantity":5'))
            assert changed != raw and len(changed) == len(raw)
            db.execute("UPDATE " + table + " SET payload=?", (changed,))
        else:
            db.execute("UPDATE " + table + " SET " + column + "='changed'")

    calls = between_preflight_and_writer(store, monkeypatch, mutate)
    with pytest.raises(ContractError):
        group(intents, original, observation(order))
    assert len(calls) == 2
    assert intents._recovery_row_highwater == 0
    assert projection_rows(store) == before


@pytest.mark.parametrize("table", ["forecasts", "intraday_intentions"])
def test_removed_original_between_passes_cannot_project_cached_values(tmp_path, monkeypatch, table):
    store, original, intents, order = cold(tmp_path)
    before = projection_rows(store)
    between_preflight_and_writer(store, monkeypatch, lambda db: db.execute("DELETE FROM " + table))
    with pytest.raises(ContractError, match="original persisted intention"):
        group(intents, original, observation(order))
    assert intents._recovery_row_highwater == 0
    assert projection_rows(store) == before


@pytest.mark.parametrize("prefix", ["execution_forecast", "recovery_sizing"])
@pytest.mark.parametrize("damage", ["missing_trigger", "changed_trigger", "missing_revision", "changed_table"])
def test_protected_schema_and_markers_are_rechecked_in_final_writer(tmp_path, monkeypatch, prefix, damage):
    store, original, intents, order = cold(tmp_path)
    before = projection_rows(store)

    def mutate(db):
        if damage in {"missing_trigger", "changed_trigger"}:
            db.execute("DROP TRIGGER " + prefix + "_update")
            if damage == "changed_trigger":
                table = "forecasts" if prefix == "execution_forecast" else "intraday_intentions"
                db.execute("CREATE TRIGGER " + prefix + "_update AFTER UPDATE ON " + table + " BEGIN SELECT 1; END")
        elif damage == "missing_revision":
            db.execute("DELETE FROM " + prefix + "_revisions")
        else:
            db.execute("ALTER TABLE " + prefix + "_revisions ADD COLUMN forged TEXT")

    between_preflight_and_writer(store, monkeypatch, mutate)
    with pytest.raises(ContractError, match="(Recovery sizing|Execution forecast)"):
        group(intents, original, observation(order))
    assert intents._recovery_row_highwater == 0
    assert projection_rows(store) == before


@pytest.mark.parametrize("prefix", ["execution_forecast", "recovery_sizing"])
def test_recreated_epoch_between_passes_does_not_authorize_stale_scalars(tmp_path, monkeypatch, prefix):
    store, original, intents, order = cold(tmp_path)
    before = projection_rows(store)

    def mutate(db):
        if prefix == "execution_forecast":
            from ml.intraday.store import _EXECUTION_FORECAST_SCHEMA
            schema, table, key = _EXECUTION_FORECAST_SCHEMA, "forecasts", "id"
        else:
            schema, table, key = module._RECOVERY_ROW_SCHEMA, "intraday_intentions", "forecast_id"
        for name, statement in schema.items():
            if "CREATE TRIGGER" in statement:
                db.execute("DROP TRIGGER " + name)
        db.execute("DROP TABLE " + prefix + "_revisions")
        db.execute("DROP TABLE " + prefix + "_generation")
        db.execute("UPDATE " + table + " SET payload='broken'")
        for statement in schema.values():
            db.execute(statement)
        db.execute("INSERT INTO " + prefix + "_generation VALUES(1,0,lower(hex(randomblob(16))))")
        db.execute("INSERT INTO " + prefix + "_revisions SELECT " + key + ",0 FROM " + table)

    between_preflight_and_writer(store, monkeypatch, mutate)
    with pytest.raises(json.JSONDecodeError):
        group(intents, original, observation(order))
    assert projection_rows(store) == before


def test_valid_new_sizing_between_passes_revalidates_and_uses_current_scalar_facts(tmp_path, monkeypatch):
    store, original, intents, order = cold(tmp_path)
    validated = []
    full = intents._value

    def validate(row):
        value = full(row)
        validated.append(value["desired_quantity"])
        return value

    monkeypatch.setattr(intents, "_value", validate)

    def mutate(db):
        value = json.loads(db.execute("SELECT payload FROM intraday_intentions").fetchone()[0])
        value["desired_quantity"] = 3
        db.execute("UPDATE intraday_intentions SET id=?,payload=?", (digest(value), encoded(value)))

    between_preflight_and_writer(store, monkeypatch, mutate)
    result = group(intents, original, observation(order))
    assert validated == [4, 3]
    assert result[0]["desired_quantity"] == 3
    assert intents._recovery_row_highwater > 0


def test_failed_group_after_preflight_does_not_publish_sizing_watermark(tmp_path):
    store, original, intents, order = cold(tmp_path)
    before = projection_rows(store)
    assert intents._recovery_row_highwater == 0
    with pytest.raises(ContractError, match="explicit evidence"):
        group(intents, original, observation(order, evidence={}))
    assert intents._recovery_row_highwater == 0
    assert projection_rows(store) == before
    group(intents, original, observation(order))
    assert intents._recovery_row_highwater > 0


def test_failed_final_writer_exit_rolls_back_and_does_not_publish_sizing_watermark(tmp_path, monkeypatch):
    store, original, intents, order = cold(tmp_path)
    before = projection_rows(store)
    connect = store.connect
    entries = []

    @contextmanager
    def fail_final_exit():
        entries.append(True)
        index = len(entries)
        with connect() as db:
            yield db
            if index == 2:
                raise RuntimeError("synthetic writer commit failure")

    monkeypatch.setattr(store, "connect", fail_final_exit)
    with pytest.raises(RuntimeError, match="writer commit failure"):
        group(intents, original, observation(order))
    assert len(entries) == 2
    assert intents._recovery_row_highwater == 0
    monkeypatch.setattr(store, "connect", connect)
    assert projection_rows(store) == before
    group(intents, original, observation(order))
    assert intents._recovery_row_highwater > 0


@pytest.mark.parametrize("invalid_projection", [False, True])
def test_full_cold_sizing_trees_release_before_reserving_final_writer(tmp_path, monkeypatch, invalid_projection):
    store, original, intents, order = cold(tmp_path)
    with store.connect() as db:
        originals = {name: db.execute("SELECT * FROM " + name).fetchall()
                     for name in ("forecasts", "intraday_intentions")}
        db.execute("CREATE TABLE independent_writer(stage TEXT NOT NULL)")
    connect, validate = store.connect, intents._value
    decoded, references, released, writer_entries = [], [], [], []

    class OriginalTree(dict):
        pass

    def release_tree(name):
        # A real second writer can commit while the validated tree is being
        # discarded. This fails if its lifetime crosses BEGIN IMMEDIATE.
        other = sqlite3.connect(store.path, timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("INSERT INTO independent_writer VALUES(?)", (name,))
            other.commit()
            released.append((name, "committed"))
        except sqlite3.OperationalError as exc:
            released.append((name, str(exc)))
        finally:
            other.close()

    def validated_tree(row):
        value = validate(row)
        decoded.append(row[0])
        # Wrap only after the unmodified full original validator succeeds.
        # Root and nested sizing references must both end before the writer.
        value = OriginalTree(value)
        value["sizing"] = OriginalTree(value["sizing"])
        for name, tree in (("original", value), ("sizing", value["sizing"])):
            references.append(weakref.ref(tree))
            weakref.finalize(tree, release_tree, name)
        return value

    @contextmanager
    def observed_connect():
        writer_entries.append(True)
        if len(writer_entries) == 2:
            assert len(references) == 2 and all(ref() is None for ref in references)
            assert sorted(released) == [("original", "committed"), ("sizing", "committed")]
        with connect() as db:
            yield db

    monkeypatch.setattr(intents, "_value", validated_tree)
    monkeypatch.setattr(store, "connect", observed_connect)
    item = observation(order, evidence={}) if invalid_projection else observation(order)
    if invalid_projection:
        with pytest.raises(ContractError, match="explicit evidence"):
            group(intents, original, item)
        assert intents._recovery_row_highwater == 0
    else:
        result = group(intents, original, item)
        assert result[0]["desired_quantity"] == 4
        assert result[0]["submitted_quantity"] == result[0]["filled_quantity"] == 0
    assert decoded == [original["forecast_id"]]
    monkeypatch.setattr(intents, "_value", validate)
    monkeypatch.setattr(store, "connect", connect)
    with store.connect() as db:
        assert {name: db.execute("SELECT * FROM " + name).fetchall() for name in originals} == originals
    assert intents.get(original["forecast_id"])["sizing"]["research"]["models"][0]["history"] == list(range(100))
