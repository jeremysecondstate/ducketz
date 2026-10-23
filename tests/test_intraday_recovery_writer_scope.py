"""Cold scientific validation must not monopolize SQLite's writer slot."""
from contextlib import closing, contextmanager
import json
import sqlite3

import pytest

from ml.intraday import intentions as module
from ml.intraday.intentions import IntentionStore
from ml.intraday.prediction import ContractError, digest, encoded
from ml.intraday.store import ShadowStore
from tests.test_intraday_recovery_grouping import group, observation, projection_rows
from tests.test_intraday_recovery_sizing_projection import prepared


def cold(tmp_path):
    store, original, _, order = prepared(tmp_path)
    reopened = ShadowStore(store.path)
    return reopened, original, IntentionStore(reopened), order


def competing_write(path):
    with closing(sqlite3.connect(path, timeout=0)) as db, db:
        db.execute("INSERT INTO events(cycle_id,kind,payload) VALUES('offline','independent_writer','{}')")


@pytest.mark.parametrize("validation", ["forecast", "sizing"])
def test_real_competing_writer_succeeds_during_cold_full_validation(tmp_path, monkeypatch, validation):
    store, original, intents, order = cold(tmp_path)
    owner, name = (store, "_forecast_value") if validation == "forecast" else (intents, "_value")
    full = getattr(owner, name)
    calls = []
    def validate(row):
        competing_write(store.path)
        calls.append(True)
        return full(row)
    monkeypatch.setattr(owner, name, validate)
    result = group(intents, original, observation(order))
    assert calls == [True] and result[0]["order"]["reservation_id"] == order.reservation_id
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='independent_writer'").fetchone()[0] == 1


def test_payload_validation_is_query_only_and_projection_still_uses_pinned_writer(tmp_path, monkeypatch):
    store, original, intents, order = cold(tmp_path)
    connect = store.connect
    transactions, writes, validations = [], [], []
    class Connection:
        def __init__(self, db): self.db = db
        def execute(self, sql, *args):
            if sql in {"BEGIN", "BEGIN IMMEDIATE"}: transactions.append(sql)
            if sql.startswith("INSERT"): writes.append((sql, self.db.execute("PRAGMA query_only").fetchone()[0]))
            return self.db.execute(sql, *args)
        def __getattr__(self, name): return getattr(self.db, name)
    @contextmanager
    def checked():
        with connect() as db:
            proxy = Connection(db)
            yield proxy
    original_sizing = intents._revision_sizing
    def sizing(db, *args):
        validations.append(db.execute("PRAGMA query_only").fetchone()[0])
        return original_sizing(db, *args)
    monkeypatch.setattr(store, "connect", checked)
    monkeypatch.setattr(intents, "_revision_sizing", sizing)
    group(intents, original, observation(order))
    assert transactions == ["BEGIN", "BEGIN IMMEDIATE"]
    assert validations == [1, 0] and writes and all(query_only == 0 for _, query_only in writes)


@pytest.mark.parametrize("table,damage", [
    ("forecasts", "payload"), ("forecasts", "pc"), ("forecasts", "reference_at"),
    ("intraday_intentions", "payload"), ("intraday_intentions", "id"),
    ("forecasts", "delete"), ("intraday_intentions", "delete"),
])
def test_original_mutation_between_read_and_write_is_rejected_without_projection(tmp_path, monkeypatch, table, damage):
    store, original, intents, order = cold(tmp_path)
    before = projection_rows(store)
    connect, calls = store.connect, []
    @contextmanager
    def raced():
        with connect() as db:
            calls.append(True)
            yield db
        if len(calls) == 1:
            with closing(sqlite3.connect(store.path)) as external, external:
                if damage == "payload":
                    external.execute("UPDATE " + table + " SET payload=replace(payload,?,?)",
                        ('"probability_not_down":0.72', '"probability_not_down":0.73') if table == "forecasts"
                        else ('"desired_quantity":4', '"desired_quantity":5'))
                elif damage == "delete": external.execute("DELETE FROM " + table)
                elif damage == "pc": external.execute("UPDATE forecasts SET pc='Scout'")
                elif damage == "reference_at": external.execute("UPDATE forecasts SET reference_at='2026-10-08T17:01:00Z'")
                else: external.execute("UPDATE intraday_intentions SET id='changed-original'")
    monkeypatch.setattr(store, "connect", raced)
    with pytest.raises((ContractError, json.JSONDecodeError)):
        group(intents, original, observation(order))
    assert projection_rows(store) == before


@pytest.mark.parametrize("schema", ["forecast", "sizing"])
def test_revision_schema_change_between_read_and_write_cannot_authorize_cached_values(tmp_path, monkeypatch, schema):
    store, original, intents, order = cold(tmp_path)
    connect, calls = store.connect, []
    before = projection_rows(store)
    @contextmanager
    def raced():
        with connect() as db:
            calls.append(True)
            yield db
        if len(calls) == 1:
            with closing(sqlite3.connect(store.path)) as external, external:
                external.execute("DROP TRIGGER " + ("execution_forecast_update" if schema == "forecast" else "recovery_sizing_update"))
    monkeypatch.setattr(store, "connect", raced)
    with pytest.raises(ContractError, match="revision schema"):
        group(intents, original, observation(order))
    assert projection_rows(store) == before


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, InterruptedError])
def test_interruption_during_prewarm_never_enters_writer_or_appends_projection(tmp_path, monkeypatch, interruption):
    store, original, intents, order = cold(tmp_path)
    before, transactions = projection_rows(store), []
    connect = store.connect
    @contextmanager
    def traced():
        with connect() as db:
            db.set_trace_callback(lambda sql: transactions.append(sql) if sql.startswith("BEGIN") else None)
            yield db
    def interrupted(*args, **kwargs): raise interruption()
    monkeypatch.setattr(store, "connect", traced)
    monkeypatch.setattr(intents, "_value", interrupted)
    with pytest.raises(interruption): group(intents, original, observation(order))
    assert transactions == ["BEGIN"] and projection_rows(store) == before


def test_failed_projection_does_not_publish_sizing_watermark_or_partial_rows(tmp_path, monkeypatch):
    store, original, intents, order = cold(tmp_path)
    before, watermark = projection_rows(store), intents._recovery_row_highwater
    append = intents._append_validated
    def fail(*args, **kwargs):
        append(*args, **kwargs)
        raise ContractError("fixture append failure")
    monkeypatch.setattr(intents, "_append_validated", fail)
    with pytest.raises(ContractError, match="fixture append failure"):
        group(intents, original, observation(order))
    assert projection_rows(store) == before and intents._recovery_row_highwater == watermark


def test_warm_retry_returns_exact_original_projection_and_validates_only_scalars(tmp_path, monkeypatch):
    store, original, intents, order = cold(tmp_path)
    expected = group(intents, original, observation(order))
    before = projection_rows(store)
    def forbidden(*args): pytest.fail("Warm unchanged originals must not be decoded")
    monkeypatch.setattr(store, "_forecast_value", forbidden)
    monkeypatch.setattr(intents, "_value", forbidden)
    assert group(intents, original, observation(order)) == expected
    assert projection_rows(store) == before


def test_valid_sizing_revision_between_snapshots_uses_revalidated_scalar_facts(tmp_path, monkeypatch):
    store, original, intents, order = cold(tmp_path)
    connect, calls, updated = store.connect, [], []
    @contextmanager
    def raced():
        with connect() as db:
            calls.append(True)
            yield db
        if len(calls) == 1:
            with closing(sqlite3.connect(store.path)) as external, external:
                value = json.loads(external.execute("SELECT payload FROM intraday_intentions").fetchone()[0])
                value["desired_quantity"] += 3
                value["sizing"]["desired_quantity"] += 3
                updated.append(value["desired_quantity"])
                external.execute("UPDATE intraday_intentions SET id=?,payload=?", (digest(value), encoded(value)))
    monkeypatch.setattr(store, "connect", raced)
    result = group(intents, original, observation(order))
    assert updated and result[0]["desired_quantity"] == updated[0]
    assert result[0]["order"]["quantity"] == order.quantity


def test_replaced_epoch_between_snapshots_revalidates_unchanged_original(tmp_path, monkeypatch):
    store, original, intents, order = cold(tmp_path)
    connect, calls, validations = store.connect, [], []
    original_value = intents._value
    def validate(row):
        validations.append(True)
        return original_value(row)
    @contextmanager
    def raced():
        with connect() as db:
            calls.append(True)
            yield db
        if len(calls) == 1:
            with closing(sqlite3.connect(store.path)) as external, external:
                for name, statement in module._RECOVERY_ROW_SCHEMA.items():
                    if "CREATE TRIGGER" in statement: external.execute("DROP TRIGGER " + name)
                for name, statement in module._RECOVERY_ROW_SCHEMA.items():
                    if "CREATE TABLE" in statement: external.execute("DROP TABLE " + name)
                for statement in module._RECOVERY_ROW_SCHEMA.values(): external.execute(statement)
                external.execute("INSERT INTO recovery_sizing_generation VALUES(1,0,lower(hex(randomblob(16))))")
                external.execute("INSERT INTO recovery_sizing_revisions SELECT forecast_id,0 FROM intraday_intentions")
    monkeypatch.setattr(store, "connect", raced)
    monkeypatch.setattr(intents, "_value", validate)
    result = group(intents, original, observation(order))
    assert len(validations) == 2 and result[0]["order"]["reservation_id"] == order.reservation_id
