"""Exact existing groups need no writer; new groups keep protected appends."""
from contextlib import contextmanager
from dataclasses import replace
import json
import socket
import sqlite3

import pytest

from ml.intraday.execution_intentions import _RecoveryTimings
from ml.intraday.prediction import ContractError, digest, encoded
from tests.test_intraday_durable_sizing_headers import observation, projection_rows, prepared


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("existing-projection tests are offline")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def trace(store, monkeypatch):
    statements = []
    connect = store.connect
    @contextmanager
    def traced():
        with connect() as db:
            db.set_trace_callback(statements.append)
            yield db
    monkeypatch.setattr(store, "connect", traced)
    return statements


def run(intents, original, *items, timings=None, actor="Atlas"):
    return intents.record_recovery_group(original["forecast_id"], actor=actor,
                                         observations=items, timings=timings)


def writers(statements):
    return [sql for sql in statements if sql == "BEGIN IMMEDIATE"]


def mutations(statements):
    return [sql for sql in statements if sql.upper().startswith(("INSERT ", "UPDATE ", "DELETE "))]


def test_every_exact_attempt_returns_without_writer_or_original_redecode(tmp_path, monkeypatch):
    store, original, intents, order = prepared(tmp_path)
    items = [observation(order), observation(replace(order, reservation_id="uncertain", status="UNKNOWN"))]
    expected = run(intents, original, *items)
    before = projection_rows(store)
    statements = trace(store, monkeypatch)
    timings = _RecoveryTimings()
    for _ in range(3):
        assert run(intents, original, *(item | {"recorded_at": "2026-10-08T17:20:00Z"} for item in items), timings=timings) == expected
    assert writers(statements) == mutations(statements) == []
    assert timings.snapshot()["read_details"]["workload"] == {"forecast_cache_hits": 3, "sizing_cache_hits": 3}
    assert projection_rows(store) == before
    assert expected[1]["submitted_quantity"] is None


def test_any_missing_member_keeps_second_check_and_one_atomic_writer(tmp_path, monkeypatch):
    store, original, intents, order = prepared(tmp_path)
    old = observation(order)
    expected = run(intents, original, old)
    newer = observation(replace(order, reservation_id="second", status="REJECTED"))
    statements = trace(store, monkeypatch)
    timings = _RecoveryTimings()
    result = run(intents, original, old, newer, timings=timings)
    assert result[0] == expected[0]
    assert len(writers(statements)) == 1
    assert timings.snapshot()["read_details"]["workload"] == {"forecast_cache_hits": 2, "sizing_cache_hits": 2}
    assert result[1]["order"]["reservation_id"] == "second"
    statements.clear()
    assert run(intents, original, old, newer) == result
    assert writers(statements) == mutations(statements) == []


@pytest.mark.parametrize("damage", ["payload", "indexed_time", "projection_forecast", "event_forecast", "dangling_event"])
def test_existing_projection_corruption_is_rejected_in_read_snapshot(tmp_path, monkeypatch, damage):
    store, original, intents, order = prepared(tmp_path)
    item = observation(order)
    result = run(intents, original, item)[0]
    with store.connect() as db:
        if damage == "payload":
            db.execute("UPDATE intents SET payload=? WHERE id=?", (encoded(result | {"filled_quantity": 999}), digest(result)))
        elif damage == "indexed_time":
            db.execute("UPDATE intents SET recorded_at='2026-10-08T17:18:00Z' WHERE id=?", (digest(result),))
        elif damage == "projection_forecast":
            db.execute("UPDATE intents SET forecast_id='different' WHERE id=?", (digest(result),))
        elif damage == "event_forecast":
            db.execute("UPDATE intraday_intention_events SET forecast_id='different' WHERE intent_entry_id=?", (digest(result),))
        else:
            db.execute("UPDATE intraday_intention_events SET intent_entry_id='missing' WHERE intent_entry_id=?", (digest(result),))
    before = projection_rows(store)
    statements = trace(store, monkeypatch)
    with pytest.raises(ContractError):
        run(intents, original, item)
    assert writers(statements) == mutations(statements) == []
    assert projection_rows(store) == before


@pytest.mark.parametrize("damage", ["forecast_payload", "sizing_payload", "forecast_index", "sizing_index"])
def test_original_mutation_does_not_hide_behind_existing_projection(tmp_path, damage):
    store, original, intents, order = prepared(tmp_path)
    item = observation(order)
    run(intents, original, item)
    before = projection_rows(store)
    with store.connect() as db:
        if damage == "forecast_payload":
            raw = json.loads(db.execute("SELECT payload FROM forecasts WHERE id=?", (original["forecast_id"],)).fetchone()[0])
            db.execute("UPDATE forecasts SET payload=? WHERE id=?", (encoded(raw | {"changed": True}), original["forecast_id"]))
        elif damage == "sizing_payload":
            db.execute("UPDATE intraday_intentions SET payload='{}' WHERE forecast_id=?", (original["forecast_id"],))
        elif damage == "forecast_index":
            db.execute("UPDATE forecasts SET pc='Scout' WHERE id=?", (original["forecast_id"],))
        else:
            db.execute("UPDATE intraday_intentions SET id='different' WHERE forecast_id=?", (original["forecast_id"],))
    with pytest.raises(ContractError):
        run(intents, original, item)
    assert projection_rows(store) == before


@pytest.mark.parametrize("damage", ["forecast", "sizing"])
def test_missing_event_rechecks_original_after_preflight_before_append(tmp_path, monkeypatch, damage):
    store, original, intents, order = prepared(tmp_path)
    first = observation(order)
    run(intents, original, first)
    second = observation(replace(order, reservation_id="second", status="REJECTED"))
    before = projection_rows(store)
    connect = store.connect
    calls = 0
    @contextmanager
    def changed_between():
        nonlocal calls
        calls += 1
        if calls == 2:
            with sqlite3.connect(store.path) as db:
                if damage == "forecast":
                    db.execute("UPDATE forecasts SET pc='Scout' WHERE id=?", (original["forecast_id"],))
                else:
                    db.execute("UPDATE intraday_intentions SET payload='{}' WHERE forecast_id=?", (original["forecast_id"],))
        with connect() as db:
            yield db
    monkeypatch.setattr(store, "connect", changed_between)
    with pytest.raises(ContractError):
        run(intents, original, first, second)
    assert calls == 2
    assert projection_rows(store) == before


@pytest.mark.parametrize("change", ["actor", "evidence", "event_key", "order", "issued_at", "quantity"])
def test_matching_group_still_checks_all_observation_guards(tmp_path, change):
    store, original, intents, order = prepared(tmp_path)
    item = observation(order)
    run(intents, original, item)
    before = projection_rows(store)
    actor = "Atlas"
    if change == "actor": actor = "Scout"
    elif change == "evidence": item = item | {"evidence": {}}
    elif change == "event_key": item = item | {"event_key": ""}
    elif change == "order": item = item | {"order": None}
    elif change == "issued_at": item = item | {"recorded_at": "2026-10-01T00:00:00Z"}
    else: item = item | {"order": replace(order, quantity=order.quantity + 1)}
    with pytest.raises(ContractError):
        run(intents, original, item, actor=actor)
    assert projection_rows(store) == before


def test_mixed_group_validation_failure_does_not_partially_append(tmp_path):
    store, original, intents, order = prepared(tmp_path)
    first = observation(order)
    original_projection = run(intents, original, first)[0]
    before = projection_rows(store)
    second = observation(replace(order, reservation_id="second", status="REJECTED"))
    invalid = observation(replace(order, reservation_id="invalid", quantity=original_projection["desired_quantity"] + 1))
    with pytest.raises(ContractError):
        run(intents, original, first, second, invalid)
    assert projection_rows(store) == before


def test_failed_read_context_exit_does_not_publish_new_highwater(tmp_path, monkeypatch):
    store, original, intents, order = prepared(tmp_path)
    item = observation(order)
    expected = run(intents, original, item)
    before = projection_rows(store)
    old_highwater = intents._recovery_row_highwater
    with store.connect() as db:
        # A real guarded same-byte update advances the protected revision.
        db.execute("UPDATE intraday_intentions SET payload=payload WHERE forecast_id=?", (original["forecast_id"],))
    connect = store.connect
    @contextmanager
    def failed_exit():
        with connect() as db:
            yield db
            raise OSError("read context exit failed")
    monkeypatch.setattr(store, "connect", failed_exit)
    with pytest.raises(OSError, match="read context exit failed"):
        run(intents, original, item)
    assert intents._recovery_row_highwater == old_highwater
    monkeypatch.setattr(store, "connect", connect)
    assert projection_rows(store) == before
    assert run(intents, original, item) == expected
    assert intents._recovery_row_highwater > old_highwater


def test_existing_group_completes_with_an_independent_reserved_writer(tmp_path, monkeypatch):
    store, original, intents, order = prepared(tmp_path)
    item = observation(order)
    expected = run(intents, original, item)
    before = projection_rows(store)
    connect = store.connect
    @contextmanager
    def no_wait():
        with connect() as db:
            db.execute("PRAGMA busy_timeout=0")
            yield db
    monkeypatch.setattr(store, "connect", no_wait)
    with sqlite3.connect(store.path, timeout=0) as competitor:
        competitor.execute("BEGIN IMMEDIATE")
        assert run(intents, original, item) == expected
        competitor.rollback()
    assert projection_rows(store) == before


def test_changed_evidence_under_existing_identity_is_not_a_noop(tmp_path):
    store, original, intents, order = prepared(tmp_path)
    item = observation(order)
    run(intents, original, item)
    before = projection_rows(store)
    with pytest.raises(ContractError, match="identity reused with changed evidence"):
        run(intents, original, item | {"evidence": {"different": True}})
    assert projection_rows(store) == before


@pytest.mark.parametrize("version", [None, "stock-direction-50-01-v3", "stock-direction-5001-4999-v3", "stock-direction-50-v2"])
def test_replay_preserves_exact_original_policy_without_assigning_absent_metadata(tmp_path, version):
    from ml.intraday.direction_metadata import FIELDS
    from tests import test_intraday_durable_sizing_headers as fixture
    metadata = {} if version is None else dict(stock_action="BUY", direction_policy_version=version,
        direction_up_threshold=.5 if version.endswith("v2") else .5001,
        direction_down_threshold=.5 if version.endswith("v2") else .4999)
    # Represent a frozen imported archive, whose policy must not be stamped by
    # the new-forecast writer. This fixture has no live/private source.
    store = fixture.ShadowStore(tmp_path / "historical.sqlite3")
    original = fixture.forecast(**metadata)
    key = digest(original)
    original["forecast_id"] = key
    initial = dict(forecast_id=key, side="BUY", **metadata)
    with store.connect() as db:
        db.execute("INSERT INTO forecasts VALUES(?,?,?,?,?,?,?)", (key, original["pc"], original["symbol"],
            original["cycle_id"], original["reference_at"], original["issued_at"], encoded(original)))
        db.execute("INSERT INTO intents VALUES(?,?,?,?)", (digest(initial), key, original["issued_at"], encoded(initial)))
    intents = fixture.IntentionStore(store)
    sized = dict(forecast_id=key, symbol="AAPL", side="BUY", status="sized", desired_quantity=4,
                 calculated_at="2026-10-08T17:16:03Z")
    intents.save_sizing(original, fixture.SizingResult(digest(sized), encoded(sized)))
    order = fixture.ReservationState("own-order", "own-allocation", "AAPL", "15m", key,
        "BUY", 1, "50", 0, "RESERVED", None, "own-key", "own-batch", None)
    item = observation(order)
    first = run(intents, original, item)
    again = run(intents, original, item | {"recorded_at": "2026-10-08T17:20:00Z"})
    assert first == again
    assert {name: first[0][name] for name in FIELDS if name in first[0]} == metadata
    with store.connect() as db:
        assert json.loads(db.execute("SELECT payload FROM forecasts WHERE id=?", (key,)).fetchone()[0]) == original


def test_noop_uses_one_wal_snapshot_and_next_call_rejects_changed_original(tmp_path, monkeypatch):
    store, original, intents, order = prepared(tmp_path)
    item = observation(order)
    expected = run(intents, original, item)
    before = projection_rows(store)
    read_sizing = intents._revision_sizing
    changed = []
    def update_after_original_reads(*args, **kwargs):
        result = read_sizing(*args, **kwargs)
        if not changed:
            with sqlite3.connect(store.path, timeout=0) as other:
                other.execute("UPDATE forecasts SET pc='Scout' WHERE id=?", (original["forecast_id"],))
            changed.append(True)
        return result
    monkeypatch.setattr(intents, "_revision_sizing", update_after_original_reads)
    # Existing projection and originals belong to the same earlier read
    # snapshot, even though an independent WAL writer can commit meanwhile.
    assert run(intents, original, item) == expected
    assert changed == [True]
    with pytest.raises(ContractError):
        run(intents, original, item)
    assert projection_rows(store) == before
