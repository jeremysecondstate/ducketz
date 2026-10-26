"""Whole own recovery preflight shares one snapshot, preserving every original guard."""
from contextlib import contextmanager
from dataclasses import replace
import json
import socket
import sqlite3

import pytest

from ml.intraday.execution_intentions import _RecoveryTimings
from ml.intraday.prediction import ContractError, digest, encoded
from ml.intraday.technical_sizing import SizingResult
from tests.test_intraday_durable_sizing_headers import observation, prepared, projection_rows
from tests.test_intraday_store import forecast


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("batch recovery tests are offline")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def fixture(tmp_path, count=3):
    store, original, intents, order = prepared(tmp_path)
    originals = [(original, order)]
    for index in range(1, count):
        f = store.save_forecast(forecast(cycle_id="batch-" + str(index)))
        value = dict(forecast_id=f["forecast_id"], symbol="AAPL", side="BUY", status="sized",
            desired_quantity=4, calculated_at="2026-10-08T17:16:03Z", research={"fixture": index})
        intents.save_sizing(f, SizingResult(digest(value), encoded(value)))
        originals.append((f, replace(order, forecast_id=f["forecast_id"], reservation_id="r" + str(index))))
    groups = {f["forecast_id"]: (observation(o),) for f, o in originals}
    expected = {key: intents.record_recovery_group(key, actor="Atlas", observations=items)
                for key, items in groups.items()}
    return store, originals, intents, groups, expected


def tracked(store, monkeypatch, after=None, fail_exit=False):
    connect, statements, calls = store.connect, [], []
    @contextmanager
    def wrapper():
        calls.append(True)
        with connect() as db:
            db.set_trace_callback(statements.append)
            yield db
            if fail_exit:
                raise OSError("batch read exit failed")
        if after is not None and len(calls) == 1:
            with sqlite3.connect(store.path) as other:
                after(other)
    monkeypatch.setattr(store, "connect", wrapper)
    return calls, statements


def run(intents, groups, **kwargs):
    return intents.record_recovery_groups(groups, actor="Atlas", **kwargs)


def test_exact_groups_share_one_connection_and_protected_metadata(tmp_path, monkeypatch):
    store, _, intents, groups, expected = fixture(tmp_path, 422)
    before = projection_rows(store)
    calls, statements = tracked(store, monkeypatch)
    timings = _RecoveryTimings()
    assert run(intents, groups, timings=timings) == expected
    assert len(calls) == 1
    assert statements.count("BEGIN") == 1
    assert "BEGIN IMMEDIATE" not in statements
    assert not any(q.startswith(("INSERT ", "UPDATE ", "DELETE ")) for q in statements)
    for table in ("execution_forecast_generation", "recovery_sizing_generation"):
        assert sum(q == "SELECT singleton,generation,epoch FROM " + table for q in statements) == 1
    assert timings.snapshot()["read_details"]["workload"] == {"forecast_cache_hits": 422, "sizing_cache_hits": 422}
    assert projection_rows(store) == before


def test_only_missing_group_takes_fresh_group_writer(tmp_path, monkeypatch):
    store, originals, intents, groups, expected = fixture(tmp_path)
    key = originals[1][0]["forecast_id"]
    groups[key] += (observation(replace(originals[1][1], reservation_id="new-own", status="UNKNOWN")),)
    calls, statements = tracked(store, monkeypatch)
    result = run(intents, groups)
    assert len(calls) == 3  # Batch read, missing-group read, missing-group writer.
    assert statements.count("BEGIN IMMEDIATE") == 1
    assert list(result) == list(groups)
    assert result[key][0] == expected[key][0]
    assert result[key][1]["submitted_quantity"] is None
    assert result[key][1]["evidence"]["commitment_retained"] is True
    assert all(result[k] == v for k, v in expected.items() if k != key)


@pytest.mark.parametrize("damage", ["forecast", "sizing"])
def test_missing_group_does_not_reuse_batch_proof_after_committed_mutation(tmp_path, monkeypatch, damage):
    store, originals, intents, groups, _ = fixture(tmp_path)
    key = originals[-1][0]["forecast_id"]
    groups[key] += (observation(replace(originals[-1][1], reservation_id="new-own")),)
    before = projection_rows(store)
    def mutate(db):
        if damage == "forecast":
            db.execute("UPDATE forecasts SET pc='Scout' WHERE id=?", (key,))
        else:
            db.execute("UPDATE intraday_intentions SET payload='{}' WHERE forecast_id=?", (key,))
    calls, _ = tracked(store, monkeypatch, after=mutate)
    with pytest.raises(ContractError):
        run(intents, groups)
    assert len(calls) == 2
    assert projection_rows(store) == before


@pytest.mark.parametrize("damage", ["forecast_payload", "sizing_payload", "forecast_index",
    "sizing_index", "missing_forecast_revision", "missing_sizing_revision", "forecast_trigger", "sizing_trigger"])
def test_last_original_and_exact_protected_schema_still_validate(tmp_path, damage):
    store, originals, intents, groups, _ = fixture(tmp_path)
    key = originals[-1][0]["forecast_id"]
    with store.connect() as db:
        if damage == "forecast_payload":
            raw = db.execute("SELECT payload FROM forecasts WHERE id=?", (key,)).fetchone()[0]
            assert '0.72' in raw
            db.execute("UPDATE forecasts SET payload=? WHERE id=?", (raw.replace('0.72', '0.73', 1), key))
        elif damage == "sizing_payload":
            raw = db.execute("SELECT payload FROM intraday_intentions WHERE forecast_id=?", (key,)).fetchone()[0]
            db.execute("UPDATE intraday_intentions SET payload=? WHERE forecast_id=?", (raw.replace('"desired_quantity":4', '"desired_quantity":5'), key))
        elif damage == "forecast_index": db.execute("UPDATE forecasts SET pc='Scout' WHERE id=?", (key,))
        elif damage == "sizing_index": db.execute("UPDATE intraday_intentions SET id='changed' WHERE forecast_id=?", (key,))
        elif damage == "missing_forecast_revision": db.execute("DELETE FROM execution_forecast_revisions WHERE forecast_id=?", (key,))
        elif damage == "missing_sizing_revision": db.execute("DELETE FROM recovery_sizing_revisions WHERE forecast_id=?", (key,))
        elif damage == "forecast_trigger": db.execute("DROP TRIGGER execution_forecast_update")
        else: db.execute("DROP TRIGGER recovery_sizing_update")
    before = projection_rows(store)
    with pytest.raises(ContractError): run(intents, groups)
    assert projection_rows(store) == before


@pytest.mark.parametrize("damage", ["payload", "indexed_time", "event_forecast", "dangling_event"])
def test_last_saved_projection_is_not_hidden_by_batch_reuse(tmp_path, damage):
    store, originals, intents, groups, expected = fixture(tmp_path)
    key = originals[-1][0]["forecast_id"]
    entry_id = digest(expected[key][0])
    with store.connect() as db:
        if damage == "payload": db.execute("UPDATE intents SET payload=? WHERE id=?",
            (encoded(expected[key][0] | {"filled_quantity": 99}), entry_id))
        elif damage == "indexed_time": db.execute("UPDATE intents SET recorded_at='2026-10-08T17:18:00Z' WHERE id=?", (entry_id,))
        elif damage == "event_forecast": db.execute("UPDATE intraday_intention_events SET forecast_id='other' WHERE intent_entry_id=?", (entry_id,))
        else: db.execute("UPDATE intraday_intention_events SET intent_entry_id='missing' WHERE intent_entry_id=?", (entry_id,))
    before = projection_rows(store)
    with pytest.raises(ContractError): run(intents, groups)
    assert projection_rows(store) == before


def test_missing_group_keeps_atomic_append_on_invalid_later_member(tmp_path):
    store, originals, intents, groups, _ = fixture(tmp_path)
    f, order = originals[-1];key = f["forecast_id"]
    groups[key] += (observation(replace(order, reservation_id="new-first")),
                    observation(replace(order, reservation_id="invalid-last", quantity=99)))
    before = projection_rows(store)
    with pytest.raises(ContractError): run(intents, groups)
    assert projection_rows(store) == before


def test_failed_read_exit_does_not_publish_sizing_highwater(tmp_path, monkeypatch):
    store, _, intents, groups, _ = fixture(tmp_path)
    highwater = intents._recovery_row_highwater
    with store.connect() as db:
        db.execute("UPDATE intraday_intentions SET payload=payload")
    tracked(store, monkeypatch, fail_exit=True)
    with pytest.raises(OSError, match="batch read exit failed"): run(intents, groups)
    assert intents._recovery_row_highwater == highwater


def test_sizing_snapshot_reader_cannot_escape_context_or_transaction(tmp_path):
    from contextlib import nullcontext
    store, originals, intents, _, _ = fixture(tmp_path)
    key = originals[0][0]["forecast_id"]
    with store.connect() as db:
        db.execute("BEGIN")
        with intents._recovery_sizing_reader(db, lambda stage: nullcontext()) as (read, _):
            assert read(key)[1]["desired_quantity"] == 4
            db.rollback()
            with pytest.raises(ContractError, match="pinned source snapshot"): read(key)
        db.execute("BEGIN")
        with pytest.raises(ContractError, match="pinned source snapshot"): read(key)


def test_original_science_policy_and_unknown_commitment_bytes_remain_unchanged(tmp_path):
    store, originals, intents, groups, expected = fixture(tmp_path)
    def originals_bytes():
        with store.connect() as db:
            return (db.execute("SELECT * FROM forecasts ORDER BY rowid").fetchall(),
                    db.execute("SELECT * FROM intraday_intentions ORDER BY rowid").fetchall())
    before = originals_bytes()
    changed_times = {k: tuple(item | {"recorded_at": "2026-10-08T17:29:00Z"} for item in v) for k, v in groups.items()}
    assert run(intents, changed_times) == expected
    assert originals_bytes() == before
    assert all(row[0]["evidence"]["commitment_retained"] is True and row[0]["side"] == "BUY" for row in expected.values())


@pytest.mark.parametrize("actor", [None, "other", "Scout"])
def test_batch_actor_checks_preserved(tmp_path, actor):
    _, _, intents, groups, _ = fixture(tmp_path)
    with pytest.raises(ContractError): intents.record_recovery_groups(groups, actor=actor)


def test_empty_inputs_require_no_connection_and_preserve_empty_keys(tmp_path, monkeypatch):
    store, _, intents, groups, _ = fixture(tmp_path)
    calls, _ = tracked(store, monkeypatch)
    assert run(intents, {}) == {}
    assert run(intents, {key: () for key in groups}) == {key: [] for key in groups}
    assert not calls


def test_executor_batch_restores_interleaved_native_order(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from ml.intraday.account_execution import IntradayAccountExecutor
    _, originals, intents, _, _ = fixture(tmp_path)
    owned = [originals[2][1], originals[0][1], replace(originals[2][1], reservation_id="another-unknown", status="UNKNOWN")]
    executor = object.__new__(IntradayAccountExecutor)
    executor.actor, executor.intents = "Atlas", intents
    executor.clock = lambda: "2026-10-08T17:18:00Z"
    executor._execution_trace = None
    executor.ledger = SimpleNamespace(_transaction=lambda: nullcontext(None))
    monkeypatch.setattr(executor, "_account", lambda **kwargs: nullcontext())
    monkeypatch.setattr(executor, "_capture", lambda: ())
    monkeypatch.setattr(executor, "_execution_stage", lambda stage: None)
    monkeypatch.setattr(executor, "_owned_native_reservations", lambda db: owned)
    result = executor.recover()
    assert [row["order"]["reservation_id"] for row in result] == [o.reservation_id for o in owned]
    assert result[-1]["submitted_quantity"] is None


def test_path_replacement_during_batch_invalidates_snapshot_proof(tmp_path, monkeypatch):
    import shutil
    store, _, intents, groups, _ = fixture(tmp_path)
    replacement = tmp_path / "replacement.sqlite3"
    shutil.copyfile(store.path, replacement)
    original_path, original_read = store.path, intents._revision_sizing_row
    highwater = intents._recovery_row_highwater
    calls = []
    def switched(*args, **kwargs):
        result = original_read(*args, **kwargs)
        calls.append(True)
        if len(calls) == 1:
            store.path = replacement
        return result
    monkeypatch.setattr(intents, "_revision_sizing_row", switched)
    try:
        with pytest.raises(ContractError, match="source identity changed"):
            run(intents, groups)
        assert intents._recovery_row_highwater <= highwater
    finally:
        store.path = original_path
