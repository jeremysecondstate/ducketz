"""New direction provenance is frozen with forecasts; legacy evidence stays intact."""
from copy import deepcopy
import json

import pytest

from ml.intraday.prediction import ContractError, TARGET_VERSION, digest, encoded
from ml.intraday.store import ShadowStore
from ml.intraday.telemetry import export_packet, import_packet, load_receipt, validate_packet, write_packet
from ml.stock_direction_policy import (
    BEARISH_PROBABILITY, BULLISH_PROBABILITY, STOCK_DIRECTION_POLICY_VERSION,
)


POLICY_FIELDS = {"direction", "direction_policy_version", "direction_up_threshold", "direction_down_threshold"}
PRODUCED = "2026-10-19T17:47:00+00:00"
RECEIVED = "2026-10-19T17:48:00+00:00"


def payload(*, pc="Atlas", probability=.72, cycle="policy-fixture"):
    return dict(pc=pc, symbol="AAPL", cycle_id=cycle,
        reference_at="2026-10-19T17:00:00+00:00", reference_open="100",
        target_open="2026-10-19T17:30:00+00:00", target_close="2026-10-19T17:45:00+00:00",
        issued_at="2026-10-19T17:16:02+00:00", recorded_at="2026-10-19T17:16:03+00:00",
        training_cutoff="2026-10-19T17:15:30+00:00", information_available_at="2026-10-19T17:15:03+00:00",
        data_through="2026-10-19T17:15:00+00:00", target_version=TARGET_VERSION,
        mode="live", availability_basis="observed", probability_not_down=probability,
        configured_count=2, successful_count=2, ensemble_status="full",
        models=[dict(name=name, status="success", probability_not_down=probability, version=mark * 64)
                for name, mark in (("a", "a"), ("b", "b"))])


def raw_historical(store, supplied, *, recorded_policy=False):
    """Insert pre-upgrade bytes directly, avoiding the prospective save boundary."""
    value = deepcopy(supplied)
    value.update(classification="not-down" if value["probability_not_down"] >= .5 else "down",
                 execution_eligibility="manual_live_checks_required")
    if isinstance(recorded_policy, dict):
        value.update(recorded_policy)
    elif recorded_policy:
        value.update(direction="BULLISH", direction_policy_version="stock-direction-50-v2",
                     direction_up_threshold=.5, direction_down_threshold=.5)
    key = digest(value)
    value["forecast_id"] = key
    intent = dict(forecast_id=key, side="BUY", desired_quantity=3, adjusted_quantity=0,
                  disposition="original_zero_capacity", submitted_quantity=0, filled_quantity=0)
    with store.connect() as db:
        db.execute("INSERT INTO forecasts VALUES(?,?,?,?,?,?,?)", (key, value["pc"], value["symbol"],
            value["cycle_id"], value["reference_at"], value["issued_at"], encoded(value)))
        db.execute("INSERT INTO intents VALUES(?,?,?,?)", (digest(intent), key, value["issued_at"], encoded(intent)))
    store.observe(value, as_of=PRODUCED, candle=dict(symbol="AAPL", opened_at=value["target_open"],
        available_at="2026-10-19T17:45:04+00:00", open="100", high="102", low="99", close="101",
        volume=10, interval_seconds=900, source_id="original-observed-candle",
        availability_kind="actual", synthetic=False))
    return value


def saved_bytes(store):
    with store.connect() as db:
        return {table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                for table in ("forecasts", "intents", "outcomes")}


@pytest.mark.parametrize("pc", ["Atlas", "Scout"])
@pytest.mark.parametrize("probability,direction,side", [
    (.5001, "BULLISH", "BUY"), (.4999, "BEARISH", "SELL"),
    (.5, "NO_EDGE", "HOLD"), (.50005, "NO_EDGE", "HOLD"), (.49995, "NO_EDGE", "HOLD"),
])
def test_new_forecasts_freeze_inclusive_policy_without_changing_model_target(tmp_path, pc, probability, direction, side):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    supplied = payload(pc=pc, probability=probability)
    original = deepcopy(supplied)
    saved = store.save_forecast(supplied)
    assert saved["direction"] == direction
    assert saved["direction_policy_version"] == STOCK_DIRECTION_POLICY_VERSION
    assert saved["direction_up_threshold"] == BULLISH_PROBABILITY == .5001
    assert saved["direction_down_threshold"] == BEARISH_PROBABILITY == .4999
    assert saved["probability_not_down"] == probability
    assert saved["classification"] == ("not-down" if probability >= .5 else "down")
    for field in ("models", "target_version", "target_open", "target_close", "reference_open"):
        assert saved[field] == original[field]
    assert supplied == original and POLICY_FIELDS.isdisjoint(supplied)
    assert saved["forecast_id"] == digest({k: v for k, v in saved.items() if k != "forecast_id"})
    assert saved["forecast_id"] != digest({k: v for k, v in saved.items() if k not in POLICY_FIELDS | {"forecast_id"}})
    with store.connect() as db:
        intent = json.loads(db.execute("SELECT payload FROM intents").fetchone()[0])
    assert intent["side"] == side and intent["forecast_id"] == saved["forecast_id"]


@pytest.mark.parametrize("pc", ["Atlas", "Scout"])
def test_new_forecast_raw_and_full_retries_preserve_hash_and_initial_intent(tmp_path, pc):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    supplied = payload(pc=pc)
    saved = store.save_forecast(supplied)
    before = saved_bytes(store)
    reopened = ShadowStore(store.path)
    assert reopened.save_forecast(supplied) == saved
    assert reopened.save_forecast(saved) == saved
    partial = deepcopy(supplied)
    partial["direction_policy_version"] = saved["direction_policy_version"]
    assert reopened.save_forecast(partial) == saved
    assert reopened.forecast(supplied["cycle_id"], supplied["symbol"]) == saved
    assert saved_bytes(reopened) == before


@pytest.mark.parametrize("pc", ["Atlas", "Scout"])
@pytest.mark.parametrize("recorded_policy", [False, True])
def test_genuine_old_rows_retain_original_metadata_absence_intents_and_scores(tmp_path, pc, recorded_policy):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    supplied = payload(pc=pc, probability=.50005)
    saved = raw_historical(store, supplied, recorded_policy=recorded_policy)
    before = saved_bytes(store)
    reopened = ShadowStore(store.path)
    assert reopened.save_forecast(supplied) == saved
    assert reopened.save_forecast(saved) == saved
    assert reopened.forecast(supplied["cycle_id"], supplied["symbol"]) == saved
    assert saved_bytes(reopened) == before
    if not recorded_policy:
        assert POLICY_FIELDS.isdisjoint(saved)
    else:
        assert saved["direction_policy_version"] == "stock-direction-50-v2"
        assert saved["direction_up_threshold"] == saved["direction_down_threshold"] == .5


@pytest.mark.parametrize("recorded_policy", [
    {"direction": "legacy-custom", "direction_policy_version": "custom-unregistered-policy",
     "direction_up_threshold": .61, "direction_down_threshold": .39},
    {"direction_policy_version": "custom-unregistered-policy"},
])
def test_unknown_or_partial_historical_policy_remains_frozen_on_local_retries(tmp_path, recorded_policy):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    supplied = payload(probability=.50005)
    saved = raw_historical(store, supplied, recorded_policy=recorded_policy)
    before = saved_bytes(store)
    reopened = ShadowStore(store.path)
    assert reopened.save_forecast(supplied) == saved
    assert reopened.save_forecast(saved) == saved
    assert saved_bytes(reopened) == before
    assert {field: saved[field] for field in POLICY_FIELDS if field in saved} == recorded_policy


@pytest.mark.parametrize("change", ["issued_at", "probability", "target", "extra", *sorted(POLICY_FIELDS)])
def test_retry_rejects_changed_original_inputs_and_supplied_policy(tmp_path, change):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    supplied = payload()
    store.save_forecast(supplied)
    before = saved_bytes(store)
    changed = deepcopy(supplied)
    if change == "issued_at":
        changed[change] = "2026-10-19T17:16:04+00:00"
    elif change == "probability":
        changed["probability_not_down"] = .71
        for model in changed["models"]:
            model["probability_not_down"] = .71
    elif change == "target":
        changed["target_close"] = "2026-10-19T18:00:00+00:00"
    elif change == "extra":
        changed["new_extra_evidence"] = True
    else:
        changed[change] = {"direction": "BEARISH", "direction_policy_version": "changed-policy",
                           "direction_up_threshold": .9, "direction_down_threshold": .1}[change]
    with pytest.raises(ContractError):
        store.save_forecast(changed)
    assert saved_bytes(store) == before


@pytest.mark.parametrize("field", sorted(POLICY_FIELDS))
def test_old_unrecorded_retry_cannot_retroactively_add_policy(tmp_path, field):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    supplied = payload()
    raw_historical(store, supplied)
    before = saved_bytes(store)
    supplied[field] = {"direction": "BULLISH", "direction_policy_version": STOCK_DIRECTION_POLICY_VERSION,
                       "direction_up_threshold": .5001, "direction_down_threshold": .4999}[field]
    with pytest.raises(ContractError, match="immutable"):
        store.save_forecast(supplied)
    assert saved_bytes(store) == before


@pytest.mark.parametrize("field,value", [("direction", "BEARISH"), ("direction_policy_version", "unknown-policy"),
    ("direction_up_threshold", .9), ("direction_down_threshold", .1)])
def test_new_forecast_rejects_conflicting_supplied_policy(tmp_path, field, value):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    supplied = payload()
    supplied[field] = value
    with pytest.raises(ContractError, match="direction policy"):
        store.save_forecast(supplied)
    assert saved_bytes(store) == {"forecasts": [], "intents": [], "outcomes": []}


def test_failed_initial_intent_leaves_no_forecast_or_policy_row(tmp_path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    with pytest.raises(ContractError, match="direction disagrees"):
        store.save_forecast(payload(), intent={"side": "SELL"})
    assert saved_bytes(store) == {"forecasts": [], "intents": [], "outcomes": []}


@pytest.mark.parametrize("pc,peer", [("Atlas", "Scout"), ("Scout", "Atlas")])
@pytest.mark.parametrize("historical", [False, True])
def test_telemetry_roundtrip_preserves_recorded_policy_and_legacy_absence(tmp_path, pc, peer, historical):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    supplied = payload(pc=pc)
    saved = raw_historical(store, supplied) if historical else store.save_forecast(supplied)
    packet = export_packet(store.path, producer_pc=pc, source_version="direction-policy-fixture", produced_at=PRODUCED)
    projected = packet["payload"]["forecasts"][0]
    assert projected["forecast_id"] == saved["forecast_id"]
    for field in POLICY_FIELDS:
        if historical:
            assert field not in projected
        else:
            assert projected[field] == saved[field]
    path = write_packet(packet, tmp_path / "packet.json")
    imported = import_packet(path, tmp_path / "receipts", expected_peer=pc, local_pc=peer, received_at=RECEIVED)
    loaded = load_receipt(imported["receipt_path"], expected_peer=pc, local_pc=peer, as_of=RECEIVED)
    assert loaded["receipt"]["packet"]["payload"]["forecasts"][0] == projected
    assert saved_bytes(store)["forecasts"][0][-1] == encoded(saved)


@pytest.mark.parametrize("change", ["partial", "direction", "version", "upper", "lower"])
def test_monitoring_rejects_partial_or_inconsistent_recorded_policy(tmp_path, change):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    store.save_forecast(payload())
    packet = export_packet(store.path, producer_pc="Atlas", source_version="policy-fixture", produced_at=PRODUCED)
    value = packet["payload"]["forecasts"][0]
    value.update(direction="BULLISH", direction_policy_version=STOCK_DIRECTION_POLICY_VERSION,
                 direction_up_threshold=.5001, direction_down_threshold=.4999)
    if change == "partial":
        value.pop("direction_policy_version")
    elif change == "direction":
        value["direction"] = "BEARISH"
    elif change == "version":
        value["direction_policy_version"] = "unknown-policy"
    elif change == "upper":
        value["direction_up_threshold"] = .9
    else:
        value["direction_down_threshold"] = .1
    packet["packet_id"] = digest(packet["payload"])
    with pytest.raises(ContractError, match="direction policy"):
        validate_packet(packet)


def test_monitoring_accepts_frozen_known_prior_policy_without_reclassification(tmp_path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    saved = raw_historical(store, payload(probability=.50005), recorded_policy=True)
    packet = export_packet(store.path, producer_pc="Atlas", source_version="prior-policy-fixture", produced_at=PRODUCED)
    row = packet["payload"]["forecasts"][0]
    assert row["direction"] == "BULLISH" and row["direction_policy_version"] == "stock-direction-50-v2"
    assert row["forecast_id"] == saved["forecast_id"]
