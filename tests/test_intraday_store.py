from datetime import timedelta
import json
import sqlite3

import pytest

from ml.intraday.prediction import ContractError, TARGET_VERSION, instant
from ml.intraday.store import ShadowStore


def forecast(**changes):
    value = dict(pc="Atlas", symbol="AAPL", cycle_id="cycle", reference_at="2026-10-08T17:00:00+00:00",
        reference_open="100", target_open="2026-10-08T17:30:00+00:00", target_close="2026-10-08T17:45:00+00:00",
        issued_at="2026-10-08T17:16:02+00:00", information_available_at="2026-10-08T17:15:03+00:00",
        data_through="2026-10-08T17:15:00+00:00", target_version=TARGET_VERSION,
        mode="shadow", availability_basis="observed", probability_not_down=.72, configured_count=2, successful_count=2,
        ensemble_status="full", models=[dict(name="a",status="success",probability_not_down=.7),dict(name="b",status="success",probability_not_down=.74)])
    value.update(changes)
    return value


def candle(**changes):
    value = dict(symbol="AAPL", opened_at="2026-10-08T17:30:00+00:00", available_at="2026-10-08T17:45:04+00:00",
                 open="100", high="101", low="99", close="100", volume=100, source_id="source", availability_kind="actual", synthetic=False)
    value.update(changes)
    return value


def test_immutable_forecast_and_zero_capacity_intention_scores(tmp_path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    original = store.save_forecast(forecast(), intent=dict(desired_quantity=4,adjusted_quantity=0,disposition="no_purchasing_capacity"))
    assert store.save_forecast(forecast()) == original
    with pytest.raises(ContractError, match="immutable"):
        store.save_forecast(forecast(issued_at="2026-10-08T17:16:03+00:00"))
    result = store.observe(original, as_of="2026-10-08T17:45:05Z", candle=candle())
    assert result["target_not_down"] is True
    with store.connect() as db:
        intent = json.loads(db.execute("SELECT payload FROM intents").fetchone()[0])
    assert intent["desired_quantity"] == 4 and intent["adjusted_quantity"] == 0
    assert intent["submitted_quantity"] == intent["filled_quantity"] == 0
    assert intent["realized_pnl"] is None


def test_scoring_availability_pending_missing_and_corrections(tmp_path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    f = store.save_forecast(forecast())
    assert store.observe(f,as_of="2026-10-08T17:44:00Z")["status"] == "pending"
    assert store.observe(f,as_of="2026-10-08T17:45:00Z")["status"] == "missing"
    with pytest.raises(ContractError,match="available target"):
        store.observe(f,as_of="2026-10-08T17:45:00Z",candle=candle())
    first = store.observe(f,as_of="2026-10-08T17:46:00Z",candle=candle())
    assert store.observe(f,as_of="2026-10-08T17:47:00Z",candle=candle()) == first
    corrected = store.observe(f,as_of="2026-10-08T17:49:00Z",candle=candle(close="99",available_at="2026-10-08T17:48:00Z"))
    assert corrected["target_not_down"] is False
    assert store.observe(f,as_of="2026-10-08T17:50:00Z") == corrected
    assert len(store.revisions(f["forecast_id"])) == 4
    assert store.forecast("cycle","AAPL") == f


def test_synthetic_outcome_is_separate_and_failure_is_not_forecast(tmp_path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    f = store.save_forecast(forecast())
    c = candle(high="100",low="100",volume=0,synthetic=True,no_trade=True,evidence_json='{"covered":true,"empty_responses":2}')
    assert store.observe(f,as_of="2026-10-08T17:46:00Z",candle=c)["status"] == "synthetic"
    with pytest.raises(ContractError,match="Failed cycle"):
        store.save_forecast(forecast(cycle_id="failure",successful_count=0,models=[dict(name="a",status="failed"),dict(name="b",status="failed")]))
    with pytest.raises(ContractError,match="Incomplete"):
        store.save_forecast(forecast(successful_count=1,models=[dict(name="a",status="success",probability_not_down=.72),dict(name="b",status="failed")]))


def test_late_issue_does_not_move_target_and_snapshot_survives_restart(tmp_path):
    path = tmp_path / "shadow.sqlite3"
    store = ShadowStore(path)
    key, frozen = store.freeze_inputs("cycle","AAPL",{"input":1})
    store = ShadowStore(path)
    assert store.freeze_inputs("cycle","AAPL",{"input":2}) == (key,frozen)
    f = store.save_forecast(forecast(issued_at="2026-10-08T18:01:00Z"))
    assert f["target_close"] == "2026-10-08T17:45:00+00:00"
    with pytest.raises(ContractError,match="target changed"):
        store.save_forecast(forecast(target_open="2026-10-08T18:15:00Z"))


def test_sell_without_shares_persists_and_shadow_rejects_assumed_availability(tmp_path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    f = forecast(probability_not_down=.2,models=[dict(name="a",status="success",probability_not_down=.1),dict(name="b",status="success",probability_not_down=.3)])
    saved = store.save_forecast(f,intent=dict(side="SELL",desired_quantity=4,adjusted_quantity=0,disposition="no_eligible_uncommitted_shares"))
    assert saved["classification"] == "down"
    with pytest.raises(ContractError,match="observed"):
        store.save_forecast(forecast(availability_basis="assumed_historical"))


def test_connections_commit_or_rollback_and_close_even_when_interrupted(tmp_path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    with store.connect() as connection:
        connection.execute("INSERT INTO events(kind,payload) VALUES(?,?)", ("committed", "{}"))
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    with pytest.raises(KeyboardInterrupt):
        with store.connect() as interrupted:
            interrupted.execute("INSERT INTO events(kind,payload) VALUES(?,?)", ("rolled_back", "{}"))
            raise KeyboardInterrupt()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        interrupted.execute("SELECT 1")
    with store.connect() as verified:
        assert verified.execute("SELECT kind FROM events").fetchall() == [("committed",)]


def test_model_tuple_metadata_has_identical_first_issue_restart_and_score_representation(tmp_path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    supplied = forecast()
    supplied["models"][0]["metadata"] = {"feature_names": ("momentum", "rsi")}
    original = store.save_forecast(supplied)
    assert original["models"][0]["metadata"]["feature_names"] == ["momentum", "rsi"]
    assert store.save_forecast(supplied) == original
    assert store.forecast("cycle", "AAPL") == original
    assert store.observe(original, as_of="2026-10-08T17:46:00Z", candle=candle())["status"] == "scored"


@pytest.mark.parametrize("operation", ["inputs", "retry", "new_binding"])
def test_recovery_rejects_changed_frozen_snapshot_bytes(tmp_path, operation):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    key, _ = store.freeze_inputs("cycle", "AAPL", {"input": 1})
    with store.connect() as connection:
        connection.execute("UPDATE snapshots SET payload=? WHERE id=?", ('{"input":2}', key))
    with pytest.raises(ContractError, match="snapshot evidence hash"):
        if operation == "inputs":
            store.inputs("cycle", "AAPL")
        elif operation == "retry":
            store.freeze_inputs("cycle", "AAPL", {"input": 3})
        else:
            store.freeze_inputs("new-cycle", "AAPL", {"input": 1})
    with store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM symbol_inputs WHERE cycle_id='new-cycle'").fetchone()[0] == 0


@pytest.mark.parametrize("operation", ["single", "all", "save", "score"])
def test_recovery_rejects_changed_forecast_bytes_before_reusing_or_scoring(tmp_path, operation):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    original = store.save_forecast(forecast())
    changed = dict(original, probability_not_down=.71)
    with store.connect() as connection:
        connection.execute("UPDATE forecasts SET payload=?", (json.dumps(changed),))
    with pytest.raises(ContractError, match="forecast evidence hash"):
        if operation == "single":
            store.forecast("cycle", "AAPL")
        elif operation == "all":
            store.forecasts(pc="Atlas")
        elif operation == "save":
            store.save_forecast(forecast())
        else:
            store.observe(changed, as_of="2026-10-08T17:46:00Z", candle=candle())


def test_recovery_checks_forecast_indexed_symbol_against_original_payload(tmp_path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    store.save_forecast(forecast())
    with store.connect() as connection:
        connection.execute("UPDATE forecasts SET symbol='IONQ'")
    with pytest.raises(ContractError, match="indexed identity"):
        store.forecast("cycle", "IONQ")


@pytest.mark.parametrize("operation", ["revisions", "observe"])
def test_corrupt_outcome_revision_cannot_be_reported_as_a_valid_score(tmp_path, operation):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    original = store.save_forecast(forecast())
    outcome = store.observe(original, as_of="2026-10-08T17:46:00Z", candle=candle())
    changed = dict(outcome, target_not_down=False)
    with store.connect() as connection:
        connection.execute("UPDATE outcomes SET payload=?", (json.dumps(changed),))
    with pytest.raises(ContractError, match="outcome evidence hash"):
        if operation == "revisions":
            store.revisions(original["forecast_id"])
        else:
            store.observe(original, as_of="2026-10-08T17:47:00Z")
