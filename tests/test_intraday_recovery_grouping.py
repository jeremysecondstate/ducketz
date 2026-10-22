"""One pinned original per forecast; every own native attempt stays distinct."""
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, replace
import json
import sqlite3
from types import SimpleNamespace

import pytest

from ml.intraday.account_execution import IntradayAccountExecutor
from ml.intraday import intentions as module
from ml.intraday.prediction import ContractError, digest, encoded
from ml.intraday.technical_sizing import SizingResult
from tests.test_intraday_recovery_sizing_projection import prepared
from tests.test_intraday_store import forecast


def observation(order, *, at="2026-10-08T17:17:00Z", key=None, **changes):
    uncertain = not order.broker_order_id and order.status in {"RESERVED", "UNKNOWN"}
    disposition = "unresolved_submission_retained" if uncertain else "order_status_reconciled"
    evidence = {"native_reservation_id": order.reservation_id, "status": order.status,
                **({"commitment_retained": True, "complete_broker_order_evidence": False} if uncertain else {})}
    kind = "execution_disposition"
    item = dict(event_key=key or digest([kind, disposition, evidence, asdict(order), None]),
                recorded_at=at, disposition=disposition, evidence=evidence, kind=kind, order=order)
    return item | changes


def group(intents, original, *items, actor="Atlas"):
    return intents.record_recovery_group(original["forecast_id"], actor=actor, observations=items)


def projection_rows(store):
    with store.connect() as db:
        return (db.execute("SELECT * FROM intraday_intention_events ORDER BY rowid").fetchall(),
                db.execute("SELECT * FROM intents ORDER BY rowid").fetchall())


def spy_original_reads(store, intents, monkeypatch):
    counts = Counter()
    connect = store.connect
    with connect() as db:
        forecast_raw = db.execute("SELECT payload FROM forecasts ORDER BY rowid").fetchone()[0].encode()
        sizing_raw = db.execute("SELECT payload FROM intraday_intentions ORDER BY rowid").fetchone()[0].encode()
    sha256 = module.hashlib.sha256

    class Cursor:
        def __init__(self, cursor, kind):
            self.cursor, self.kind = cursor, kind
        def fetchone(self):
            row = self.cursor.fetchone()
            if row is not None and self.kind:
                counts[self.kind + "_reads"] += 1
                counts[self.kind + "_bytes"] += len(row[-1].encode())
            return row
        def __getattr__(self, name):
            return getattr(self.cursor, name)

    class Connection:
        def __init__(self, db):
            self.db = db
        def execute(self, sql, *args):
            if sql == "BEGIN IMMEDIATE":
                counts["transactions"] += 1
            kind = ("forecast" if "FROM forecasts WHERE id=?" in sql else
                    "sizing" if "FROM intraday_intentions WHERE forecast_id=?" in sql else None)
            return Cursor(self.db.execute(sql, *args), kind)

    @contextmanager
    def traced_connect():
        with connect() as db:
            yield Connection(db)

    def traced_hash(raw=b""):
        if raw == forecast_raw:
            counts["forecast_raw_hashes"] += 1
        if raw == sizing_raw:
            counts["sizing_raw_hashes"] += 1
        return sha256(raw)

    monkeypatch.setattr(store, "connect", traced_connect)
    monkeypatch.setattr(module.hashlib, "sha256", traced_hash)
    return counts


def test_same_forecast_group_reads_and_hashes_once_while_preserving_each_attempt(tmp_path, monkeypatch):
    store, original, intents, order = prepared(tmp_path)
    orders = [order, replace(order, reservation_id="rejected-attempt", status="REJECTED"),
              replace(order, reservation_id="unknown-attempt", status="UNKNOWN")]
    items = [observation(value) for value in orders]
    expected = [intents.record_recovery(original["forecast_id"], actor="Atlas", **item) for item in items]
    before = projection_rows(store)
    counts = spy_original_reads(store, intents, monkeypatch)
    for item in items:
        intents.record_recovery(original["forecast_id"], actor="Atlas", **item)
    assert counts["forecast_reads"] == counts["sizing_reads"] == counts["transactions"] == 3
    single_bytes = counts["forecast_bytes"] + counts["sizing_bytes"]
    counts.clear()
    assert group(intents, original, *items) == expected
    assert counts["forecast_reads"] == counts["sizing_reads"] == counts["transactions"] == 1
    assert counts["forecast_raw_hashes"] == counts["sizing_raw_hashes"] == 1
    assert 3 * (counts["forecast_bytes"] + counts["sizing_bytes"]) == single_bytes
    assert projection_rows(store) == before
    assert len({row["event_identity"] for row in expected}) == 3
    assert expected[2]["submitted_quantity"] is None
    assert all(row["order"]["reservation_id"] == value.reservation_id for row, value in zip(expected, orders))
    counts.clear()
    assert group(intents, original, *(item | {"recorded_at": "2026-10-08T17:20:00Z"} for item in items)) == expected
    assert counts["forecast_reads"] == counts["sizing_reads"] == 1
    assert counts["forecast_raw_hashes"] == counts["sizing_raw_hashes"] == 1


@pytest.mark.parametrize("damage", ["payload", "pc", "symbol", "cycle_id", "reference_at", "issued_at", "missing"])
def test_group_revalidates_exact_forecast_bytes_and_all_indices_between_transactions(tmp_path, damage):
    store, original, intents, order = prepared(tmp_path)
    item = observation(order)
    group(intents, original, item)
    before = projection_rows(store)
    with store.connect() as db:
        if damage == "payload":
            db.execute("UPDATE forecasts SET payload=?", (encoded(original | {"research_changed": True}),))
        elif damage == "missing":
            db.execute("DELETE FROM forecasts")
        else:
            db.execute("UPDATE forecasts SET " + damage + "='changed'")
    with pytest.raises(ContractError):
        group(intents, original, item)
    assert projection_rows(store) == before


@pytest.mark.parametrize("damage", ["payload", "id", "forecast_id", "missing"])
def test_group_revalidates_exact_sizing_bytes_and_indices_between_transactions(tmp_path, damage):
    store, original, intents, order = prepared(tmp_path)
    item = observation(order)
    group(intents, original, item)
    before = projection_rows(store)
    with store.connect() as db:
        if damage == "payload":
            row = json.loads(db.execute("SELECT payload FROM intraday_intentions").fetchone()[0])
            row["sizing"]["research"]["models"][0]["history"][0] = 999
            db.execute("UPDATE intraday_intentions SET payload=?", (encoded(row),))
        elif damage == "missing":
            db.execute("DELETE FROM intraday_intentions")
        else:
            db.execute("UPDATE intraday_intentions SET " + damage + "='changed'")
    with pytest.raises(ContractError):
        group(intents, original, item)
    assert projection_rows(store) == before


@pytest.mark.parametrize("actor", ["Scout", "unknown", None])
def test_group_rejects_other_or_missing_actor_even_after_warm_replay(tmp_path, actor):
    store, original, intents, order = prepared(tmp_path)
    group(intents, original, observation(order))
    before = projection_rows(store)
    with pytest.raises(ContractError):
        group(intents, original, observation(order), actor=actor)
    assert projection_rows(store) == before


@pytest.mark.parametrize("damage", ["side", "quantity", "symbol", "forecast_id", "filled_without_identity",
                                   "before_issue", "before_sizing", "before_latest", "empty_evidence", "no_order"])
def test_invalid_second_member_rolls_back_first_and_retains_all_order_guards(tmp_path, damage):
    store, original, intents, order = prepared(tmp_path)
    first = observation(order, key="new-first", at="2026-10-08T17:18:00Z")
    second = observation(replace(order, reservation_id="second-attempt"), key="new-second",
                         at="2026-10-08T17:18:00Z")
    if damage in {"side", "quantity", "symbol", "forecast_id"}:
        change = {"side": "SELL", "quantity": 5, "symbol": "AMZN", "forecast_id": "other"}[damage]
        second["order"] = replace(second["order"], **{damage: change})
    elif damage == "filled_without_identity":
        second["order"] = replace(second["order"], filled_quantity=1)
    elif damage == "before_issue":
        second["recorded_at"] = "2026-10-08T17:15:00Z"
    elif damage == "before_sizing":
        second["recorded_at"] = "2026-10-08T17:16:02.5Z"
    elif damage == "before_latest":
        second["recorded_at"] = "2026-10-08T17:17:00Z"
    elif damage == "empty_evidence":
        second["evidence"] = {}
    else:
        second["order"] = None
    before = projection_rows(store)
    with pytest.raises(ContractError):
        group(intents, original, first, second)
    assert projection_rows(store) == before


def test_changed_evidence_under_saved_member_identity_rolls_back_new_sibling(tmp_path):
    store, original, intents, order = prepared(tmp_path)
    existing = observation(order, key="original-identity")
    group(intents, original, existing)
    before = projection_rows(store)
    first = observation(replace(order, reservation_id="new-attempt"), key="new-identity")
    altered = existing | {"evidence": {"native_reservation_id": "changed"}}
    with pytest.raises(ContractError, match="identity reused"):
        group(intents, original, first, altered)
    assert projection_rows(store) == before


def test_pinned_group_prevents_original_change_between_members(tmp_path, monkeypatch):
    store, original, intents, order = prepared(tmp_path)
    appended = intents._append_validated
    attempts = []
    def checked(*args, **kwargs):
        with sqlite3.connect(store.path, timeout=0) as competitor:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competitor.execute("UPDATE intraday_intentions SET id='changed'")
        attempts.append(True)
        return appended(*args, **kwargs)
    monkeypatch.setattr(intents, "_append_validated", checked)
    result = group(intents, original, observation(order),
                   observation(replace(order, reservation_id="second-attempt")))
    assert len(attempts) == len(result) == 2
    assert intents.get(original["forecast_id"])["desired_quantity"] == 4


def test_group_preserves_exact_known_fill_and_unknown_commitment(tmp_path):
    _, original, intents, order = prepared(tmp_path)
    filled = replace(order, reservation_id="known-order", status="FILLED", broker_order_id="broker-123",
                     filled_quantity=order.quantity)
    unknown = replace(order, reservation_id="unknown-order", status="UNKNOWN")
    result = group(intents, original, observation(filled), observation(unknown))
    assert result[0]["order"] == asdict(filled)
    assert result[0]["filled_quantity"] == result[0]["submitted_quantity"] == order.quantity
    assert result[0]["broker_order_id"] == "broker-123"
    assert result[1]["order"] == asdict(unknown)
    assert result[1]["submitted_quantity"] is None and result[1]["filled_quantity"] == 0
    assert result[1]["evidence"]["commitment_retained"] is True
    assert result[1]["evidence"]["complete_broker_order_evidence"] is False


def test_executor_groups_noncontiguous_forecasts_and_restores_native_result_order(tmp_path, monkeypatch):
    store, original, intents, order = prepared(tmp_path)
    other = store.save_forecast(forecast(cycle_id="other"))
    value = dict(forecast_id=other["forecast_id"], symbol="AAPL", side="BUY", status="sized",
                 desired_quantity=4, calculated_at="2026-10-08T17:16:03Z")
    intents.save_sizing(other, SizingResult(digest(value), encoded(value)))
    owned = (order, replace(order, reservation_id="other-original", forecast_id=other["forecast_id"]),
             replace(order, reservation_id="same-original-second", status="REJECTED"))

    @contextmanager
    def transaction(*args, **kwargs):
        yield None

    executor = object.__new__(IntradayAccountExecutor)
    executor._account = transaction
    executor._capture = lambda: None
    executor.ledger = SimpleNamespace(_transaction=transaction)
    executor._owned_native_reservations = lambda db: owned
    executor._execution_stage = lambda stage: None
    executor.actor = "Atlas"
    executor.clock = lambda: "2026-10-08T17:17:00Z"
    executor.intents = intents
    calls = []
    grouped = intents.record_recovery_group
    def tracked(forecast_id, **kwargs):
        calls.append((forecast_id, len(kwargs["observations"])))
        return grouped(forecast_id, **kwargs)
    monkeypatch.setattr(intents, "record_recovery_group", tracked)
    result = executor.recover()
    assert calls == [(original["forecast_id"], 2), (other["forecast_id"], 1)]
    assert [row["evidence"]["native_reservation_id"] for row in result] == [row.reservation_id for row in owned]
    assert [row["order"] for row in result] == [asdict(row) for row in owned]
    assert executor.recover() == result


def test_empty_group_needs_no_transaction_or_original(tmp_path, monkeypatch):
    store, original, intents, _ = prepared(tmp_path)
    def unexpected():
        pytest.fail("Empty recovery group opened a transaction")
    monkeypatch.setattr(store, "connect", unexpected)
    assert group(intents, original) == []
