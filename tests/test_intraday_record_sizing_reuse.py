"""Ordinary projections reuse protected sizing facts, preserving original guards."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from ml.intraday.intentions import IntentionStore
from ml.intraday.prediction import ContractError, digest, encoded
from tests.test_intraday_intentions import setup
from tests.test_intraday_recovery_grouping import group, observation, projection_rows
from tests.test_intraday_recovery_row_revisions import external, watch, payload_reads
from tests.test_intraday_recovery_sizing_projection import prepared


def record(intents, forecast, **changes):
    arguments = dict(event_key="ordinary-projection", recorded_at="2026-10-08T17:18:00Z",
        disposition="awaiting_capacity", evidence={"reason": "constructed-fixture"},
        kind="execution_disposition", scope_to_intention=True)
    arguments.update(changes)
    return intents.record(forecast, **arguments)


def sizing_reads(statements):
    return [sql for sql in payload_reads(statements) if "FROM intraday_intentions " in sql]


def originals(store):
    with store.connect() as db:
        return {name: db.execute("SELECT * FROM " + name).fetchall()
                for name in ("forecasts", "intraday_intentions")}


def test_cold_then_warm_ordinary_projection_keeps_full_caller_check_without_sizing_reads(tmp_path, monkeypatch):
    store, forecast, _, _ = prepared(tmp_path)
    # save_sizing itself records a projection. A newly opened object is cold.
    intents = IntentionStore(store)
    before = originals(store)
    statements, _ = watch(store, monkeypatch)
    checked, decoded = [], []
    full_forecast, full_sizing = store._forecast_value, intents._value
    def forecast_check(row):
        checked.append(row[0])
        return full_forecast(row)
    def sizing_check(row):
        decoded.append(None if row is None else row[0])
        return full_sizing(row)
    monkeypatch.setattr(store, "_forecast_value", forecast_check)
    monkeypatch.setattr(intents, "_value", sizing_check)
    first = record(intents, forecast)
    assert len(sizing_reads(statements)) == 1 and decoded == [forecast["forecast_id"]]
    assert checked == [forecast["forecast_id"]]
    before_projections = projection_rows(store)
    statements.clear(); checked.clear(); decoded.clear()
    assert record(intents, forecast, recorded_at="2026-10-08T17:19:00Z") == first
    assert sizing_reads(statements) == [] and decoded == []
    assert checked == [forecast["forecast_id"]]
    assert projection_rows(store) == before_projections and originals(store) == before
    assert first["desired_quantity"] == 4
    assert not any(isinstance(v, (dict, list)) for v in intents._recovery_row_cache[forecast["forecast_id"]])
    assert intents.get(forecast["forecast_id"])["sizing"]["research"]["models"][0]["history"] == list(range(100))


def test_nested_caller_forecast_mutation_still_requires_full_original_equality(tmp_path):
    store, forecast, intents, _ = prepared(tmp_path)
    record(intents, forecast)
    before = projection_rows(store)
    changed = deepcopy(forecast)
    changed["models"][0]["probability_not_down"] = .71
    with pytest.raises(ContractError, match="original saved forecast"):
        record(intents, changed, event_key="nested-caller-change")
    assert projection_rows(store) == before


@pytest.mark.parametrize("column", ["id", "payload"])
def test_warm_ordinary_record_rejects_changed_sizing_digest_or_body(tmp_path, column):
    store, forecast, intents, _ = prepared(tmp_path)
    record(intents, forecast)
    before = projection_rows(store)
    with external(store.path) as db:
        if column == "payload":
            raw = db.execute("SELECT payload FROM intraday_intentions").fetchone()[0]
            changed = raw.replace('"desired_quantity":4', '"desired_quantity":5')
            assert changed != raw and len(changed) == len(raw)
            db.execute("UPDATE intraday_intentions SET payload=?", (changed,))
        else:
            db.execute("UPDATE intraday_intentions SET id='changed'")
    with pytest.raises(ContractError, match="intention changed"):
        record(intents, forecast, event_key="after-corruption")
    assert projection_rows(store) == before


@pytest.mark.parametrize("operation", ["delete", "move"])
def test_removed_or_reindexed_sizing_never_reuses_old_scalar_facts(tmp_path, operation):
    store, forecast, intents, _ = prepared(tmp_path)
    record(intents, forecast)
    with external(store.path) as db:
        db.execute("DELETE FROM intraday_intentions" if operation == "delete" else
                   "UPDATE intraday_intentions SET forecast_id='different-original'")
    result = record(intents, forecast, event_key="now-unsized")
    assert result["desired_quantity"] is None
    assert result["event_identity"] == digest([forecast["forecast_id"], "now-unsized"])


def test_valid_changed_sizing_id_uses_new_scalars_and_scoped_identity(tmp_path, monkeypatch):
    store, forecast, intents, _ = prepared(tmp_path)
    first = record(intents, forecast)
    with external(store.path) as db:
        value = json.loads(db.execute("SELECT payload FROM intraday_intentions").fetchone()[0])
        value["desired_quantity"] = 3
        new_id = digest(value)
        db.execute("UPDATE intraday_intentions SET id=?,payload=?", (new_id, encoded(value)))
    statements, _ = watch(store, monkeypatch)
    changed = record(intents, forecast, recorded_at="2026-10-08T17:19:00Z")
    assert len(sizing_reads(statements)) == 1
    assert first["desired_quantity"] == 4 and changed["desired_quantity"] == 3
    assert changed["event_identity"] == digest([forecast["forecast_id"], "ordinary-projection", {"intention_id": new_id}])
    assert changed["event_identity"] != first["event_identity"]


@pytest.mark.parametrize("encoding", ["indent", "blob"])
def test_cold_noncanonical_and_blob_sizing_keep_full_semantics(tmp_path, encoding):
    store, forecast, intents, _ = prepared(tmp_path)
    expected = record(intents, forecast)
    with external(store.path) as db:
        value = json.loads(db.execute("SELECT payload FROM intraday_intentions").fetchone()[0])
        raw = json.dumps(value, indent=2)
        db.execute("UPDATE intraday_intentions SET payload=?", (raw.encode() if encoding == "blob" else raw,))
    assert record(intents, forecast) == expected
    assert record(intents, forecast) == expected


@pytest.mark.parametrize("damage", ["trigger", "revision", "generation"])
def test_missing_protected_marker_never_authorizes_ordinary_cached_sizing(tmp_path, damage):
    store, forecast, intents, _ = prepared(tmp_path)
    record(intents, forecast)
    before = projection_rows(store)
    with external(store.path) as db:
        if damage == "trigger":
            db.execute("DROP TRIGGER recovery_sizing_update")
        elif damage == "revision":
            db.execute("DELETE FROM recovery_sizing_revisions")
        else:
            db.execute("DROP TRIGGER recovery_generation_delete")
            db.execute("DELETE FROM recovery_sizing_generation")
    with pytest.raises(ContractError, match="Recovery sizing"):
        record(intents, forecast, event_key="damaged-marker")
    assert projection_rows(store) == before


def test_unsized_record_retains_legacy_key_and_no_fabricated_quantity(tmp_path):
    store, forecast, intents, _ = setup(tmp_path)
    expected = record(intents, forecast)
    assert expected["desired_quantity"] is None and expected["side"] == "BUY"
    assert expected["event_identity"] == digest([forecast["forecast_id"], "ordinary-projection"])
    assert record(intents, forecast, scope_to_intention=False) == expected


def test_failed_projection_does_not_publish_sizing_watermark(tmp_path):
    store, forecast, _, order = prepared(tmp_path)
    intents = IntentionStore(store)
    assert intents._recovery_row_highwater == 0
    with pytest.raises(ContractError, match="Order differs"):
        record(intents, forecast, order=replace(order, quantity=5))
    assert intents._recovery_row_highwater == 0
    record(intents, forecast)
    assert intents._recovery_row_highwater > 0


def test_failed_commit_exit_rolls_back_projection_without_publishing_sizing_watermark(tmp_path, monkeypatch):
    store, forecast, intents, _ = prepared(tmp_path)
    record(intents, forecast)
    watermark = intents._recovery_row_highwater
    with external(store.path) as db:
        value = json.loads(db.execute("SELECT payload FROM intraday_intentions").fetchone()[0])
        value["desired_quantity"] = 3
        db.execute("UPDATE intraday_intentions SET id=?,payload=?", (digest(value), encoded(value)))
    before = projection_rows(store)
    connect = store.connect
    @contextmanager
    def interrupted():
        with connect() as db:
            yield db
            raise RuntimeError("constructed commit-exit failure")
    with monkeypatch.context() as patch:
        patch.setattr(store, "connect", interrupted)
        with pytest.raises(RuntimeError, match="commit-exit"):
            record(intents, forecast, event_key="new-after-sizing")
    assert intents._recovery_row_highwater == watermark
    assert projection_rows(store) == before
    assert record(intents, forecast, event_key="new-after-sizing")["desired_quantity"] == 3
    assert intents._recovery_row_highwater > watermark


def test_ordinary_group_and_chronology_concurrency_keep_shared_lock_order(tmp_path):
    store, forecast, intents, order = prepared(tmp_path)
    expected = record(intents, forecast)
    recovered = group(intents, forecast, observation(order, at="2026-10-08T17:18:00Z"))
    def check(index):
        if index % 3 == 0:
            return record(intents, forecast) == expected
        if index % 3 == 1:
            return group(intents, forecast, observation(order, at="2026-10-08T17:18:00Z")) == recovered
        return store.execution_forecast_headers(pc="Atlas", symbol="AAPL")[0]["forecast_id"] == forecast["forecast_id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(check, range(24)))


def test_unknown_and_fill_identity_guards_survive_warm_ordinary_record(tmp_path):
    store, forecast, intents, order = prepared(tmp_path)
    record(intents, forecast)
    unknown = record(intents, forecast, event_key="exact-unknown", order=replace(order, status="UNKNOWN"))
    assert unknown["submitted_quantity"] is None and unknown["filled_quantity"] == 0
    before = projection_rows(store)
    with pytest.raises(ContractError, match="exact broker identity"):
        record(intents, forecast, event_key="invented-fill", order=replace(order, filled_quantity=1))
    assert projection_rows(store) == before
