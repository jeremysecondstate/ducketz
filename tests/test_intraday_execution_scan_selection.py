"""Own execution history reads stay bounded without weakening saved evidence."""
from contextlib import contextmanager
from datetime import timedelta
import json
import sqlite3
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.intraday import account_execution as execution
from ml.intraday.intentions import IntentionStore
from ml.intraday.prediction import ContractError, digest, encoded, instant
from ml.intraday.store import ShadowStore
from ml.intraday.technical_sizing import SizingResult
from ml.stock_trader.horizon_ledger import LedgerError
from test_intraday_account_execution import env, advance
from test_intraday_native_snapshot_performance import populated
from test_intraday_recovery_latency import terminal_orders
from test_intraday_store import forecast


def test_step_and_recovery_classify_native_history_with_bounded_sql(env, monkeypatch):
    terminal_orders(env, 4)
    current = env.forecast(offset=60)
    env.now = pd.Timestamp(current["issued_at"])
    stage = {"name": None}
    queries = {"newer_native_order_scan": [], "recovery_native_read": []}
    original_stage = env.executor._execution_stage
    original_transaction = execution.HorizonLedger._transaction

    def observed_stage(name):
        stage["name"] = name
        return original_stage(name)

    @contextmanager
    def measured_transaction(ledger):
        with original_transaction(ledger) as db:
            selected = queries.get(stage["name"])
            if selected is not None:
                db.set_trace_callback(selected.append)
            try:
                yield db
            finally:
                db.set_trace_callback(None)

    monkeypatch.setattr(env.executor, "_execution_stage", observed_stage)
    monkeypatch.setattr(execution.HorizonLedger, "_transaction", measured_transaction)
    assert env.executor.step(current)["disposition"] == "submitted"
    submissions = len(env.broker.submissions)
    env.orders[next(reversed(env.orders))]["status"] = "CANCELLED"
    advance(env)
    result = env.executor.recover()
    assert len(result) == 5
    assert len(env.broker.submissions) == submissions
    for name, statements in queries.items():
        selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
        assert 0 < len(selects) <= 13, (name, len(selects))


@pytest.mark.parametrize("count", [1, 80])
@pytest.mark.parametrize("markers", [False, True])
@pytest.mark.parametrize("stream", ["atlas-15m", "scout-15m", "gameplan"])
def test_bulk_native_selection_preserves_legacy_fallback_identity_and_order(tmp_path, count, markers, stream):
    ledger = populated(tmp_path, count)
    executor = SimpleNamespace(ledger=ledger, stream=stream)
    with ledger._transaction() as db:
        if markers:
            ledger._intraday_schema(db)
            ids = [row[0] for row in db.execute("SELECT id FROM reservations ORDER BY id")]
            for index, identity in enumerate(ids):
                if index % 4 != 3:
                    db.execute("INSERT INTO intraday_streams VALUES(?,?,?,?)",
                        ("reservation", identity, ("atlas-15m", "scout-15m", "gameplan")[index % 4], "{}"))
                # A different marker kind cannot alter reservation ownership.
                db.execute("INSERT INTO intraday_streams VALUES(?,?,?,?)",
                    ("allocation", identity, "unrelated-kind", "{}"))
        snapshot = ledger._snapshot(db)
        expected = tuple(row for row in snapshot.reservations
                         if ledger._stream(db, "reservation", row.reservation_id) == stream)
        queries = []
        db.set_trace_callback(queries.append)
        actual = execution.IntradayAccountExecutor._owned_native_reservations(executor, db)
        db.set_trace_callback(None)
        assert actual == expected
        assert len([sql for sql in queries if sql.lstrip().upper().startswith("SELECT")]) <= 13
        assert ledger._snapshot(db) == snapshot


@pytest.mark.parametrize("damage", ["allocation", "attribution"])
def test_bulk_selection_retains_full_native_snapshot_validation(tmp_path, damage):
    ledger = populated(tmp_path, 2, attribution=True)
    with sqlite3.connect(ledger.path) as db:
        if damage == "allocation":
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("DELETE FROM allocations WHERE id='a0000'")
        else:
            db.execute("UPDATE intraday_fill_attribution SET quantity=3 WHERE allocation='a0000'")
    executor = SimpleNamespace(ledger=ledger, stream="atlas-15m")
    with ledger._transaction() as db, pytest.raises(LedgerError):
        execution.IntradayAccountExecutor._owned_native_reservations(executor, db)


def saved(store, cycle, *, offset=0, **changes):
    raw = forecast(cycle_id=cycle, mode="live", **changes)
    for field in ("reference_at", "target_open", "target_close", "issued_at", "information_available_at", "data_through"):
        raw[field] = (instant(raw[field]) + timedelta(minutes=offset)).isoformat()
    return store.save_forecast(raw)


def size(intents, original):
    value = dict(forecast_id=original["forecast_id"], symbol=original["symbol"], side="BUY", status="sized",
                 desired_quantity=4, calculated_at=(instant(original["issued_at"]) + timedelta(seconds=1)).isoformat())
    return intents.save_sizing(original, SizingResult(digest(value), encoded(value)))


def test_processed_intentions_decode_only_referenced_actor_symbol_and_keep_chronology(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "intentions.sqlite3")
    intents = IntentionStore(store)
    current = saved(store, "current")
    same = saved(store, "same")
    older = saved(store, "older", offset=-15)
    later = saved(store, "later", offset=15)
    shadow_raw = forecast(cycle_id="shadow")
    shadow = store.save_forecast(shadow_raw)
    peer = saved(store, "peer", pc="Scout")
    other_symbol = saved(store, "other", symbol="AMZN")
    for original in (current, same, older, later, shadow, peer, other_symbol):
        size(intents, original)
    for index in range(20):
        saved(store, "unsized-" + str(index), offset=15 * index)
    decoded = []
    original_validate = store._forecast_value

    def validated(row):
        decoded.append(row[0])
        return original_validate(row)

    monkeypatch.setattr(store, "_forecast_value", validated)
    assert intents.processed_at_or_after(current) == [same["forecast_id"], later["forecast_id"]]
    assert decoded == [item["forecast_id"] for item in (current, same, older, later, shadow)]


def test_empty_intentions_decode_no_historical_forecasts(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "empty.sqlite3")
    intents = IntentionStore(store)
    current = saved(store, "current")
    saved(store, "unprocessed", offset=15)

    def forbidden(row):
        pytest.fail("An empty intention set decoded historical model evidence")

    monkeypatch.setattr(store, "_forecast_value", forbidden)
    assert intents.processed_at_or_after(current) == []


@pytest.mark.parametrize("damage", ["payload", "indexed_reference", "indexed_issue"])
def test_selected_intention_forecasts_keep_immutable_and_indexed_validation(tmp_path, damage):
    store = ShadowStore(tmp_path / "corrupt.sqlite3")
    intents = IntentionStore(store)
    current = saved(store, "current")
    later = saved(store, "later", offset=15)
    size(intents, later)
    with store.connect() as db:
        if damage == "payload":
            db.execute("UPDATE forecasts SET payload=? WHERE id=?",
                       (json.dumps(dict(later, probability_not_down=.99)), later["forecast_id"]))
        else:
            column = "reference_at" if damage == "indexed_reference" else "issued_at"
            db.execute(f"UPDATE forecasts SET {column}=? WHERE id=?", (current[column], later["forecast_id"]))
    with pytest.raises(ContractError, match="forecast evidence hash or indexed identity"):
        intents.processed_at_or_after(current)
