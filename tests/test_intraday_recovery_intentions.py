"""Recovery validates original sizing once, in its real projection transaction."""
from contextlib import nullcontext
from copy import deepcopy
import json
import socket
from types import SimpleNamespace

import pytest

from ml.intraday.intentions import IntentionStore
from ml.intraday.prediction import ContractError, digest, encoded, instant
from ml.intraday.scout_local_execution import PREFIX, ScoutLocalAccountExecutor
from ml.intraday.technical_sizing import SizingResult
from ml.intraday.store import ShadowStore
from test_intraday_forecast_lookup import saved_forecast


AT = "2026-10-08T17:17:00+00:00"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Recovery intention checks must remain offline")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def setup_store(tmp_path, *, sizing=True):
    store = ShadowStore(tmp_path / "forecasts.sqlite3")
    saved = saved_forecast(store)
    intents = IntentionStore(store)
    if sizing:
        value = dict(status="sized", forecast_id=saved["forecast_id"], symbol=saved["symbol"],
            side="BUY", desired_quantity=3, calculated_at=AT,
            immutable_diagnostics={"candles": [{"original": True}]})
        intents.save_sizing(saved, SizingResult(digest(value), encoded(value)))
    return store, saved, intents


def projection(intents, saved, *, required=True):
    return intents.record(saved, event_key="retained-projection", recorded_at=AT,
        disposition="order_status_reconciled", evidence={"original_status": "NOT_SENT"},
        kind="execution_disposition", require_original_intention=required)


def events(store):
    with store.connect() as db:
        return (db.execute("SELECT * FROM intents ORDER BY rowid").fetchall(),
                db.execute("SELECT * FROM intraday_intention_events ORDER BY rowid").fetchall())


@pytest.mark.parametrize("existing_projection", [False, True])
@pytest.mark.parametrize("damage", ["missing", "payload", "identity"])
def test_required_original_is_checked_before_insert_or_idempotent_return(tmp_path, existing_projection, damage):
    store, saved, intents = setup_store(tmp_path)
    if existing_projection:
        projection(intents, saved)
    with store.connect() as db:
        if damage == "missing":
            db.execute("DELETE FROM intraday_intentions WHERE forecast_id=?", (saved["forecast_id"],))
        elif damage == "identity":
            db.execute("UPDATE intraday_intentions SET id=? WHERE forecast_id=?", ("f" * 64, saved["forecast_id"]))
        else:
            value = intents.get(saved["forecast_id"])
            value["desired_quantity"] = 300
            db.execute("UPDATE intraday_intentions SET payload=? WHERE forecast_id=?",
                       (json.dumps(value), saved["forecast_id"]))
    before = events(store)
    message = "Local order lacks its original immutable intention" if damage == "missing" else "Persisted sizing intention changed"
    with pytest.raises(ContractError, match=message):
        projection(intents, saved)
    assert events(store) == before


def test_required_projection_is_idempotent_and_never_rewrites_original_sizing(tmp_path):
    store, saved, intents = setup_store(tmp_path)
    with store.connect() as db:
        original = db.execute("SELECT * FROM intraday_intentions").fetchall()
    first = projection(intents, saved)
    before = events(store)
    assert projection(intents, saved) == first
    assert first["desired_quantity"] == 3 and events(store) == before
    with store.connect() as db:
        assert db.execute("SELECT * FROM intraday_intentions").fetchall() == original


def test_required_projection_still_rejects_changed_saved_forecast(tmp_path):
    store, saved, intents = setup_store(tmp_path)
    projection(intents, saved)
    with store.connect() as db:
        changed = dict(saved, reference_open="101")
        db.execute("UPDATE forecasts SET payload=? WHERE id=?", (json.dumps(changed), saved["forecast_id"]))
    before = events(store)
    with pytest.raises(ContractError, match="Persisted forecast evidence"):
        projection(intents, saved)
    assert events(store) == before


def recovery_executor(store, saved, intents, *, clearance=None):
    record = dict(request=dict(request_id=PREFIX + saved["forecast_id"], forecast_id=saved["forecast_id"],
        symbol="DOCU", side="BUY", limit_price="100"),
        grant=dict(reservation_id="original-reservation", quantity=2),
        state="UNKNOWN", send_token="permanent-original-token", broker_order_id=None)
    journal = SimpleNamespace(records=lambda: (record,), order_state=lambda rid: {"evidence": None})
    executor = ScoutLocalAccountExecutor.__new__(ScoutLocalAccountExecutor)
    executor.store, executor.intents = store, intents
    executor.clock = lambda: instant(AT)
    executor._account = nullcontext
    executor._controls = lambda: None
    executor._journals = lambda: iter((journal,))
    executor._recover_record = lambda actual_journal, original: original
    executor._commitment_clearance = lambda actual_journal, original: clearance
    return executor, record


@pytest.mark.parametrize("clearance", [None, {"commitment": "no_order_transport_entered"}])
def test_recovery_validates_each_original_once_and_preserves_unknown(tmp_path, monkeypatch, clearance):
    store, saved, intents = setup_store(tmp_path)
    executor, record = recovery_executor(store, saved, intents, clearance=clearance)
    original_record = deepcopy(record)
    validate = intents._value
    validations = []
    def counted(row):
        validations.append(row[0] if row else None)
        return validate(row)
    monkeypatch.setattr(intents, "_value", counted)
    before = events(store)
    first = executor.recover()[0]
    assert validations == [saved["forecast_id"]]
    assert record == original_record
    assert first["submitted_quantity"] is None
    if clearance:
        assert events(store) == before and first["filled_quantity"] is None
    else:
        assert first["disposition"] == "submission_recovery_required"
        assert first["submission_status"] == "UNKNOWN" and first["filled_quantity"] == 0
    after = events(store)
    assert executor.recover()[0] == first
    assert validations == [saved["forecast_id"]] * 2
    assert events(store) == after and record == original_record


@pytest.mark.parametrize("clearance", [None, {"commitment": "no_order_transport_entered"}])
def test_recovery_rejects_missing_intention_in_projection_and_clearance_paths(tmp_path, clearance):
    store, saved, intents = setup_store(tmp_path, sizing=False)
    executor, record = recovery_executor(store, saved, intents, clearance=clearance)
    original = deepcopy(record)
    before = events(store)
    with pytest.raises(ContractError, match="Local order lacks its original immutable intention"):
        executor.recover()
    assert events(store) == before and record == original


@pytest.mark.parametrize("probability", [.72, .5])
def test_default_record_still_allows_unsized_and_neutral_probability_dispositions(tmp_path, probability):
    store, saved, intents = setup_store(tmp_path, sizing=False)
    if probability != .72:
        payload = {k: v for k, v in saved.items() if k != "forecast_id"}
        # This constructs a different forecast, not a retry of saved policy.
        for key in ("stock_action", "direction_policy_version", "direction_up_threshold", "direction_down_threshold"):
            payload.pop(key, None)
        payload.update(cycle_id="neutral", probability_not_down=probability,
                       models=[dict(name="original", status="success", probability_not_down=probability)])
        saved = store.save_forecast(payload)
    result = intents.record(saved, event_key="before-sizing", recorded_at=AT,
        disposition="probability_between_action_cutoffs" if probability == .5 else "fixed_target_already_elapsed",
        evidence={"original_guard": True}, kind="execution_disposition")
    assert result["desired_quantity"] is None and result["submitted_quantity"] == 0
    assert intents.get(saved["forecast_id"]) is None


def test_default_failed_sizing_still_records_its_rejection_without_original_intention(tmp_path):
    store, saved, intents = setup_store(tmp_path, sizing=False)
    value = dict(status="unavailable", reason="quote_unavailable", forecast_id=saved["forecast_id"],
                 symbol="DOCU", side="BUY", calculated_at=AT)
    result = intents.save_sizing(saved, SizingResult(digest(value), encoded(value)))
    assert result["kind"] == "sizing_unavailable" and result["disposition"] == "quote_unavailable"
    assert result["desired_quantity"] is None and intents.get(saved["forecast_id"]) is None
