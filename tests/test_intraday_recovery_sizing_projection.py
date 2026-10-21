"""Recovery validates frozen sizing without repeatedly decoding research trees."""
from dataclasses import replace
import json

import pytest

from ml.intraday import intentions as module
from ml.intraday.intentions import IntentionStore
from ml.intraday.prediction import ContractError, digest, encoded
from ml.stock_trader.horizon_ledger import ReservationState
from tests.test_intraday_intentions import setup
from tests.test_intraday_store import forecast
from ml.intraday.technical_sizing import SizingResult


def prepared(tmp_path):
    store, original, intents, sized = setup(tmp_path)
    # Frozen research can be large; retain a distinguishable nested tree in
    # the test so a cache of the full value would violate the memory contract.
    value = sized.payload() | {"research": {"models": [{"history": list(range(100))}]}}
    intents.save_sizing(original, SizingResult(digest(value), encoded(value)))
    order = ReservationState("own-order", "own-allocation", "AAPL", "15m",
        original["forecast_id"], "BUY", 1, "50", 0, "RESERVED", None,
        "own-key", "own-batch", None)
    return store, original, intents, order


def recover(intents, original, order, **changes):
    arguments = dict(event_key="own-recovery", recorded_at="2026-10-08T17:17:00Z",
        disposition="unresolved_submission_retained", evidence={"own_order": "own-order"},
        kind="execution_disposition", order=order)
    arguments.update(changes)
    return intents.record_recovery(original["forecast_id"], actor="Atlas", **arguments)


def decoding_spy(intents, monkeypatch):
    reads = []
    original = intents._value
    def tracked(row):
        reads.append(None if row is None else row[0])
        return original(row)
    monkeypatch.setattr(intents, "_value", tracked)
    return reads


def test_recovery_matches_full_projection_and_hashes_every_read_without_redecoding(tmp_path, monkeypatch):
    store, original, intents, order = prepared(tmp_path)
    expected = intents.record(original, event_key="own-recovery", recorded_at="2026-10-08T17:17:00Z",
        disposition="unresolved_submission_retained", evidence={"own_order":"own-order"},
        kind="execution_disposition", order=order)
    with store.connect() as db:
        saved_row = db.execute("SELECT forecast_id,id,payload FROM intraday_intentions").fetchone()
        before = db.execute("SELECT * FROM intents ORDER BY rowid").fetchall()
    decoded = decoding_spy(intents, monkeypatch)
    raw_reads = []
    sha256 = module.hashlib.sha256
    def tracked_hash(raw):
        if raw == saved_row[2].encode():
            raw_reads.append(raw)
        return sha256(raw)
    monkeypatch.setattr(module.hashlib, "sha256", tracked_hash)
    assert recover(intents, original, order) == expected
    assert decoded == [original["forecast_id"]]
    cold_hash_reads = len(raw_reads)
    assert cold_hash_reads >= 1
    decoded.clear()
    assert recover(intents, original, order, recorded_at="2026-10-08T17:18:00Z") == expected
    assert decoded == [] and len(raw_reads) == cold_hash_reads + 1
    with store.connect() as db:
        assert db.execute("SELECT * FROM intents ORDER BY rowid").fetchall() == before
        assert db.execute("SELECT forecast_id,id,payload FROM intraday_intentions").fetchone() == saved_row
    # The full API still validates and returns the entire original sizing.
    assert intents.get(original["forecast_id"])["sizing"]["research"]["models"][0]["history"] == list(range(100))
    assert decoded == [original["forecast_id"]]
    assert not any(isinstance(item, (dict, list)) for item in intents._recovery_sizing_cache[original["forecast_id"]])


@pytest.mark.parametrize("damage", ["payload", "id", "forecast_id", "missing", "blob"])
def test_warm_recovery_never_hides_changed_sizing_bytes_or_indices(tmp_path, damage):
    store, original, intents, order = prepared(tmp_path)
    recover(intents, original, order)
    with store.connect() as db:
        row = db.execute("SELECT forecast_id,id,payload FROM intraday_intentions").fetchone()
        if damage == "payload":
            value = json.loads(row[2])
            value["sizing"]["research"]["models"][0]["history"][0] = 999
            db.execute("UPDATE intraday_intentions SET payload=?", (encoded(value),))
        elif damage == "id":
            db.execute("UPDATE intraday_intentions SET id='changed'")
        elif damage == "forecast_id":
            db.execute("UPDATE intraday_intentions SET forecast_id='changed'")
        elif damage == "missing":
            db.execute("DELETE FROM intraday_intentions")
        else:
            db.execute("UPDATE intraday_intentions SET payload=?", (row[2].encode(),))
    if damage == "blob":
        # Original bytes stored as a BLOB retain the original full validator's
        # behavior, rather than entering the string-only cache.
        assert recover(intents, original, order)["desired_quantity"] == 4
    else:
        with pytest.raises(ContractError, match="intention changed|original persisted intention"):
            recover(intents, original, order)


def test_reencoded_valid_row_is_semantically_revalidated_and_projections_are_fresh(tmp_path, monkeypatch):
    store, original, intents, order = prepared(tmp_path)
    expected = recover(intents, original, order)
    with store.connect() as db:
        row = db.execute("SELECT forecast_id,id,payload FROM intraday_intentions").fetchone()
        db.execute("UPDATE intraday_intentions SET payload=?", (json.dumps(json.loads(row[2]), indent=2),))
    decoded = decoding_spy(intents, monkeypatch)
    assert recover(intents, original, order) == expected
    assert decoded == [original["forecast_id"]]
    with store.connect() as db:
        changed = db.execute("SELECT forecast_id,id,payload FROM intraday_intentions").fetchone()
    projection = intents._recovery_sizing(changed)
    projection["sizing"]["calculated_at"] = "changed"
    projection["desired_quantity"] = 999
    assert recover(intents, original, order) == expected


def test_recovery_cache_is_bounded_and_evicted_original_is_revalidated(tmp_path, monkeypatch):
    store, original, intents, order = prepared(tmp_path)
    monkeypatch.setattr(module, "_RECOVERY_SIZING_CACHE_LIMIT", 2)
    decoded = decoding_spy(intents, monkeypatch)
    recover(intents, original, order)
    for index in range(2):
        other = store.save_forecast(forecast(cycle_id="other-" + str(index)))
        value = dict(forecast_id=other["forecast_id"], symbol="AAPL", side="BUY", status="sized",
                     desired_quantity=4, calculated_at="2026-10-08T17:16:03Z")
        intents.save_sizing(other, SizingResult(digest(value), encoded(value)))
        recover(intents, other, replace(order, forecast_id=other["forecast_id"]))
    assert len(intents._recovery_sizing_cache) == 2
    assert original["forecast_id"] not in intents._recovery_sizing_cache
    decoded.clear()
    recover(intents, original, order)
    assert decoded == [original["forecast_id"]]
    assert len(intents._recovery_sizing_cache) == 2


@pytest.mark.parametrize("damage", ["side", "quantity", "symbol", "forecast", "backdating", "identity"])
def test_warm_recovery_preserves_order_time_and_event_identity_guards(tmp_path, damage):
    _, original, intents, order = prepared(tmp_path)
    recover(intents, original, order)
    changes = {}
    if damage == "side":
        order = replace(order, side="SELL")
    elif damage == "quantity":
        order = replace(order, quantity=5)
    elif damage == "symbol":
        order = replace(order, symbol="AMZN")
    elif damage == "forecast":
        order = replace(order, forecast_id="wrong")
    elif damage == "backdating":
        changes = dict(event_key="new-event", recorded_at="2026-10-08T17:16:02.5Z")
    else:
        changes = dict(evidence={"own_order":"changed"})
    with pytest.raises(ContractError):
        recover(intents, original, order, **changes)


def test_missing_sizing_is_not_cached_and_later_saved_sizing_can_recover(tmp_path):
    store, original, intents, sized = setup(tmp_path)
    order = ReservationState("own-order", "own-allocation", "AAPL", "15m",
        original["forecast_id"], "BUY", 1, "50", 0, "RESERVED", None,
        "own-key", "own-batch", None)
    with pytest.raises(ContractError, match="original persisted intention"):
        recover(intents, original, order)
    assert not getattr(intents, "_recovery_sizing_cache", {})
    intents.save_sizing(original, sized)
    assert recover(intents, original, order)["desired_quantity"] == 4


def test_nonrecovery_records_continue_full_sizing_validation(tmp_path, monkeypatch):
    _, original, intents, order = prepared(tmp_path)
    recover(intents, original, order)
    decoded = decoding_spy(intents, monkeypatch)
    for index in range(2):
        intents.record(original, event_key="ordinary-" + str(index), recorded_at="2026-10-08T17:18:00Z",
            disposition="waiting", evidence={"test":True}, kind="execution_disposition", order=order)
    assert decoded == [original["forecast_id"]] * 2


@pytest.mark.parametrize("irregular", ["float_quantity", "null_clock", "sizing_list"])
def test_irregular_original_scalar_shapes_keep_full_validation(tmp_path, monkeypatch, irregular):
    store, original, intents, order = prepared(tmp_path)
    expected = recover(intents, original, order)
    with store.connect() as db:
        value = json.loads(db.execute("SELECT payload FROM intraday_intentions").fetchone()[0])
        if irregular == "float_quantity":
            value["desired_quantity"] = 4.0
        elif irregular == "null_clock":
            value["sizing"]["calculated_at"] = None
        else:
            value["sizing"] = []
        db.execute("UPDATE intraday_intentions SET id=?,payload=?", (digest(value), encoded(value)))
    # The original validator permits these shapes for exact event replay,
    # which returns before a fresh event would use the sizing timestamp.
    intents = IntentionStore(store)
    decoded = decoding_spy(intents, monkeypatch)
    if irregular == "float_quantity":
        # Its numeric value compares equally, but serialized event evidence
        # preserves the original int/float distinction and rejects reuse.
        for _ in range(2):
            with pytest.raises(ContractError, match="identity reused"):
                recover(intents, original, order)
    else:
        assert recover(intents, original, order) == expected
        assert recover(intents, original, order) == expected
    assert decoded == [original["forecast_id"]] * 2
    assert not intents._recovery_sizing_cache
