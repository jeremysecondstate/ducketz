"""Fresh manual workers reuse sizing facts without rereading immutable research."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import socket
import sqlite3

import pytest

from ml.intraday.execution_intentions import IntentionStore, _RecoveryTimings
from ml.intraday.prediction import ContractError, digest, encoded
from ml.intraday.store import ShadowStore
from ml.intraday.technical_sizing import SizingResult
from tests.test_intraday_store import forecast



# Main keeps execution recovery in its separate subclass; the fixture below
# creates the same original sizing/native observation without legacy adapters.
from dataclasses import asdict
from ml.stock_trader.horizon_ledger import ReservationState


def prepared(tmp_path):
    store = ShadowStore(tmp_path / "evidence.sqlite3")
    original = store.save_forecast(forecast())
    intents = IntentionStore(store)
    value = dict(forecast_id=original["forecast_id"], symbol="AAPL", side="BUY", status="sized",
        desired_quantity=4, calculated_at="2026-10-08T17:16:03Z",
        research={"models": [{"history": list(range(100))}]})
    intents.save_sizing(original, SizingResult(digest(value), encoded(value)))
    order = ReservationState("own-order", "own-allocation", "AAPL", "15m",
        original["forecast_id"], "BUY", 1, "50", 0, "RESERVED", None,
        "own-key", "own-batch", None)
    return store, original, intents, order


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


def between_preflight_and_writer(store, monkeypatch, mutate):
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


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Durable sizing tests prohibit provider and broker calls")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def cache_path(path):
    return path.with_name(path.name + ".sizing-headers.sqlite3")


def populated(tmp_path):
    store, original, intents, order = prepared(tmp_path)
    expected = group(intents, original, observation(order))
    return store, original, order, expected


def decoded(intents, monkeypatch):
    seen = []
    original = intents._value
    def checked(row):
        seen.append(row[0])
        return original(row)
    monkeypatch.setattr(intents, "_value", checked)
    return seen


def test_fresh_worker_does_not_reread_prior_sizing_research(tmp_path):
    store, original, intents, order = prepared(tmp_path)
    originals = [(original, order)]
    for index in range(8):
        f = store.save_forecast(forecast(cycle_id="cold-" + str(index)))
        value = dict(forecast_id=f["forecast_id"], symbol="AAPL", side="BUY", status="sized",
            desired_quantity=4, calculated_at="2026-10-08T17:16:03Z",
            research={"tree": "retained-scientific-original" * 20000})
        intents.save_sizing(f, SizingResult(digest(value), encoded(value)))
        originals.append((f, replace(order, forecast_id=f["forecast_id"], reservation_id="r" + str(index))))
    before = {}
    with store.connect() as db:
        for row in db.execute("SELECT forecast_id,id,payload FROM intraday_intentions"):
            before[row[0]] = (row[1], hashlib.sha256(row[2].encode()).hexdigest())
    # Simulate an existing installation with no sidecar yet, then retain the
    # proof only from full cold validation, never from fixture construction.
    cache_path(store.path).unlink(missing_ok=True)
    intents = IntentionStore(ShadowStore(store.path))
    cold_timings = _RecoveryTimings()
    for f, native in originals:
        intents.record_recovery_group(f["forecast_id"], actor="Atlas",
            observations=[observation(native)], timings=cold_timings)
    assert cold_timings.workload["sizing_payload_reads"] == len(originals)
    assert cold_timings.workload["sizing_text_characters"] > 4_000_000
    prior_events = projection_rows(store)
    # Construct both stores again: no in-memory sizing proof survives.
    new_store = ShadowStore(store.path)
    new_intents = IntentionStore(new_store)
    fresh_timings = _RecoveryTimings()
    for f, native in originals:
        new_intents.record_recovery_group(f["forecast_id"], actor="Atlas",
            observations=[observation(native)], timings=fresh_timings)
    assert fresh_timings.workload.get("sizing_payload_reads", 0) == 0
    assert fresh_timings.workload.get("sizing_text_characters", 0) == 0
    assert projection_rows(store) == prior_events
    with store.connect() as db:
        after = {row[0]: (row[1], hashlib.sha256(row[2].encode()).hexdigest())
                 for row in db.execute("SELECT forecast_id,id,payload FROM intraday_intentions")}
    assert after == before
    with sqlite3.connect(cache_path(store.path)) as db:
        for raw, in db.execute("SELECT payload FROM sizing_header_cache_rows"):
            value = json.loads(raw)["projection"]
            assert set(value) == {"forecast_id", "intention_id", "side", "desired_quantity", "calculated_at"}
            assert len(raw) < 2048


@pytest.mark.parametrize("damage", ["payload", "id", "forecast_id", "missing"])
def test_changed_original_cannot_reuse_authenticated_sizing(tmp_path, damage):
    store, original, order, _ = populated(tmp_path)
    before = projection_rows(store)
    with sqlite3.connect(store.path) as db:
        if damage == "payload":
            raw = db.execute("SELECT payload FROM intraday_intentions").fetchone()[0]
            changed = raw.replace('"desired_quantity":4', '"desired_quantity":5')
            assert changed != raw and len(changed) == len(raw)
            db.execute("UPDATE intraday_intentions SET payload=?", (changed,))
        elif damage == "missing":
            db.execute("DELETE FROM intraday_intentions")
        else:
            db.execute("UPDATE intraday_intentions SET " + damage + "='changed'")
    with pytest.raises(ContractError):
        group(IntentionStore(ShadowStore(store.path)), original, observation(order))
    assert projection_rows(store) == before


def test_valid_changed_original_revalidates_and_uses_new_quantity(tmp_path, monkeypatch):
    store, original, order, _ = populated(tmp_path)
    with sqlite3.connect(store.path) as db:
        value = json.loads(db.execute("SELECT payload FROM intraday_intentions").fetchone()[0])
        value["desired_quantity"] = 3
        db.execute("UPDATE intraday_intentions SET id=?,payload=?", (digest(value), encoded(value)))
    cold = IntentionStore(ShadowStore(store.path)); seen = decoded(cold, monkeypatch)
    result = group(cold, original, observation(order, key="new-quantity"))
    assert result[0]["desired_quantity"] == 3
    assert seen == [original["forecast_id"]]


@pytest.mark.parametrize("failure", ["missing", "malformed", "directory", "busy", "missing_key", "malformed_key"])
def test_optional_cache_failure_falls_back_to_original_validator(tmp_path, monkeypatch, failure):
    from ml.intraday.sizing_header_cache import DurableSizingHeaders
    store, original, order, expected = populated(tmp_path)
    helper = DurableSizingHeaders(store.path); path = cache_path(store.path); busy = None
    if failure in {"missing", "malformed", "directory"}:
        path.unlink()
        if failure == "malformed": path.write_bytes(b"not sqlite")
        if failure == "directory": path.mkdir()
    elif failure == "busy":
        busy = sqlite3.connect(path, timeout=0); busy.execute("BEGIN EXCLUSIVE")
    elif failure == "missing_key": helper.key_path.unlink()
    else: helper.key_path.write_bytes(b"bad protected key")
    try:
        cold = IntentionStore(ShadowStore(store.path)); seen = decoded(cold, monkeypatch)
        assert group(cold, original, observation(order)) == expected
        assert seen == [original["forecast_id"]]
    finally:
        if busy is not None: busy.rollback(); busy.close()


def test_forged_exact_schema_cache_cannot_skip_semantic_validation(tmp_path, monkeypatch):
    from ml.intraday.sizing_header_cache import _SCHEMA
    store, original, order, expected = populated(tmp_path)
    with sqlite3.connect(cache_path(store.path)) as db:
        version = db.execute("PRAGMA schema_version").fetchone()[0]
        key, raw, _ = db.execute("SELECT cache_key,payload,record_mac FROM sizing_header_cache_rows").fetchone()
        record = json.loads(raw); record["projection"]["desired_quantity"] = 999
        forged = encoded(record).encode()
        db.execute("DROP TRIGGER sizing_header_cache_rows_update")
        db.execute("UPDATE sizing_header_cache_rows SET payload=?,record_mac=? WHERE cache_key=?",
            (forged, hashlib.sha256(forged).digest(), key))
        db.execute(_SCHEMA["sizing_header_cache_rows_update"])
        db.execute("PRAGMA schema_version=" + str(version))
    cold = IntentionStore(ShadowStore(store.path)); seen = decoded(cold, monkeypatch)
    assert group(cold, original, observation(order)) == expected
    assert seen == [original["forecast_id"]]


def test_changed_sizing_between_preflight_and_writer_revalidates(tmp_path, monkeypatch):
    store, original, order, _ = populated(tmp_path)
    cold_store = ShadowStore(store.path); cold = IntentionStore(cold_store)
    def mutate(db):
        raw = db.execute("SELECT payload FROM intraday_intentions").fetchone()[0]
        db.execute("UPDATE intraday_intentions SET payload=?",
                   (raw.replace('"desired_quantity":4', '"desired_quantity":5'),))
    between_preflight_and_writer(cold_store, monkeypatch, mutate)
    before = projection_rows(store)
    with pytest.raises(ContractError):
        group(cold, original, observation(order))
    assert projection_rows(store) == before


def test_uncertain_opened_source_identity_disables_durable_hit(tmp_path, monkeypatch):
    store, original, order, expected = populated(tmp_path)
    cold_store = ShadowStore(store.path); cold = IntentionStore(cold_store)
    connect = cold_store.connect
    @contextmanager
    def uncertain():
        with connect() as db:
            cold_store._connection_source_identities[id(db)] = None
            yield db
    monkeypatch.setattr(cold_store, "connect", uncertain)
    seen = decoded(cold, monkeypatch)
    assert group(cold, original, observation(order)) == expected
    assert seen == [original["forecast_id"]]


def test_replaced_source_with_same_schema_epoch_revision_still_invalidates_proof(tmp_path):
    from ml.intraday.execution_intentions import _RECOVERY_ROW_SCHEMA
    store, original, order, _ = populated(tmp_path)
    replacement = tmp_path / "replacement.sqlite3"
    with sqlite3.connect(store.path) as source, sqlite3.connect(replacement) as target:
        source.backup(target)
        version = target.execute("PRAGMA schema_version").fetchone()[0]
        for name, sql in _RECOVERY_ROW_SCHEMA.items():
            if "CREATE TRIGGER" in sql: target.execute("DROP TRIGGER " + name)
        target.execute("UPDATE intraday_intentions SET payload='broken'")
        for sql in _RECOVERY_ROW_SCHEMA.values():
            if "CREATE TRIGGER" in sql: target.execute(sql)
        target.execute("PRAGMA schema_version=" + str(version))
    source.close(); target.close()
    with store.connect() as db: db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    replacement.replace(store.path)
    with pytest.raises(json.JSONDecodeError):
        group(IntentionStore(ShadowStore(store.path)), original, observation(order))


@pytest.mark.parametrize("damage", ["missing_trigger", "missing_revision", "new_epoch"])
def test_protected_source_schema_and_epoch_cannot_authorize_stale_proof(tmp_path, damage):
    from ml.intraday.execution_intentions import _RECOVERY_ROW_SCHEMA
    store, original, order, _ = populated(tmp_path)
    before = projection_rows(store)
    with sqlite3.connect(store.path) as db:
        if damage == "missing_trigger": db.execute("DROP TRIGGER recovery_sizing_update")
        elif damage == "missing_revision": db.execute("DELETE FROM recovery_sizing_revisions")
        else:
            for name, sql in _RECOVERY_ROW_SCHEMA.items():
                if "CREATE TRIGGER" in sql: db.execute("DROP TRIGGER " + name)
            db.execute("DROP TABLE recovery_sizing_revisions")
            db.execute("DROP TABLE recovery_sizing_generation")
            db.execute("UPDATE intraday_intentions SET payload='broken'")
            for sql in _RECOVERY_ROW_SCHEMA.values(): db.execute(sql)
            db.execute("INSERT INTO recovery_sizing_generation VALUES(1,0,lower(hex(randomblob(16))))")
            db.execute("INSERT INTO recovery_sizing_revisions SELECT forecast_id,0 FROM intraday_intentions")
    with pytest.raises((ContractError, json.JSONDecodeError)):
        group(IntentionStore(ShadowStore(store.path)), original, observation(order))
    assert projection_rows(store) == before
