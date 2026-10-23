"""Exact saved-forecast recovery keeps validation and single-send boundaries."""
from contextlib import nullcontext
from copy import deepcopy
import json
import socket
from types import SimpleNamespace

import pytest

from ml.intraday.prediction import ContractError, TARGET_VERSION
from ml.intraday.scout_local_execution import PREFIX, ScoutLocalAccountExecutor
from ml.intraday.store import ShadowStore


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Forecast lookup tests must remain offline")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def saved_forecast(store, *, pc="Scout", cycle="owned"):
    return store.save_forecast(dict(pc=pc, symbol="DOCU", cycle_id=cycle,
        reference_at="2026-10-08T17:00:00+00:00", reference_open="100",
        target_open="2026-10-08T17:30:00+00:00", target_close="2026-10-08T17:45:00+00:00",
        issued_at="2026-10-08T17:16:02+00:00", information_available_at="2026-10-08T17:15:03+00:00",
        data_through="2026-10-08T17:15:00+00:00", target_version=TARGET_VERSION,
        mode="shadow", availability_basis="observed", probability_not_down=.72,
        configured_count=1, successful_count=1, ensemble_status="full",
        models=[dict(name="original", status="success", probability_not_down=.72)]))


def test_lookup_returns_valid_original_and_enforces_pc_scope(tmp_path):
    store = ShadowStore(tmp_path / "forecasts.sqlite3")
    scout = saved_forecast(store)
    atlas = saved_forecast(store, pc="Atlas", cycle="other-pc")
    assert store.forecast_by_id(scout["forecast_id"]) == scout
    assert store.forecast_by_id(scout["forecast_id"], pc="Scout") == scout
    assert store.forecast_by_id(atlas["forecast_id"], pc="Scout") is None
    assert store.forecast_by_id("missing", pc="Scout") is None


@pytest.mark.parametrize("column,value", [
    ("pc", "Atlas"), ("symbol", "DBX"), ("cycle_id", "different"),
    ("reference_at", "2026-10-08T16:45:00+00:00"),
    ("issued_at", "2026-10-08T17:16:03+00:00")])
def test_lookup_rejects_changed_indexed_identity_before_scope_filter(tmp_path, column, value):
    store = ShadowStore(tmp_path / "forecasts.sqlite3")
    saved = saved_forecast(store)
    with store.connect() as db:
        db.execute(f"UPDATE forecasts SET {column}=? WHERE id=?", (value, saved["forecast_id"]))
    with pytest.raises(ContractError, match="hash or indexed identity"):
        store.forecast_by_id(saved["forecast_id"], pc="Scout")


@pytest.mark.parametrize("change", ["payload", "forecast_id", "not-an-object"])
def test_lookup_rejects_changed_payload(tmp_path, change):
    store = ShadowStore(tmp_path / "forecasts.sqlite3")
    saved = saved_forecast(store)
    value = dict(saved)
    if change == "payload":
        value["reference_open"] = "101"
    elif change == "forecast_id":
        value["forecast_id"] = "f" * 64
    else:
        value = []
    with store.connect() as db:
        db.execute("UPDATE forecasts SET payload=? WHERE id=?", (json.dumps(value), saved["forecast_id"]))
    with pytest.raises(ContractError, match="Persisted forecast evidence"):
        store.forecast_by_id(saved["forecast_id"], pc="Scout")


def recovery_fixture(store, forecast_id, *, copies=1, clearance=None, intention=True):
    """Exercise real recover orchestration with no journal or broker effects."""
    events = []
    original = dict(request=dict(request_id=PREFIX + forecast_id, forecast_id=forecast_id),
                    state="UNKNOWN", send_token="permanent-original-token")
    records = [deepcopy(original) for _ in range(copies)]
    journal = SimpleNamespace(records=lambda: records)
    executor = ScoutLocalAccountExecutor.__new__(ScoutLocalAccountExecutor)
    executor.store = store
    executor.intents = SimpleNamespace(get=lambda key: {"forecast_id": key} if intention else None)
    executor._account = nullcontext
    executor._controls = lambda: events.append("controls")
    executor._journals = lambda: iter([journal])
    def recover_record(actual_journal, record):
        assert actual_journal is journal
        events.append("recover")
        return record
    executor._recover_record = recover_record
    executor._commitment_clearance = lambda actual_journal, record: clearance
    def project(actual_journal, record, saved, disposition):
        # This orchestration double retains the required-original contract.
        # Real projection transaction validation is covered separately.
        if not intention:
            raise ContractError("Local order lacks its original immutable intention")
        return dict(forecast_id=saved["forecast_id"], disposition=disposition,
                    state=record["state"], send_token=record["send_token"])
    executor._project = project
    return SimpleNamespace(executor=executor, records=records, original=original, events=events)


def forbid_full_scan(*args, **kwargs):
    raise AssertionError("Order recovery must not scan unrelated forecasts")


def test_recovery_reads_each_journal_forecast_once_per_pass_and_refreshes_next_pass(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "forecasts.sqlite3")
    saved = saved_forecast(store)
    env = recovery_fixture(store, saved["forecast_id"], copies=2)
    monkeypatch.setattr(store, "forecasts", forbid_full_scan)
    lookup = store.forecast_by_id
    calls = []
    def tracked(key, *, pc):
        env.events.append("lookup")
        calls.append((key, pc))
        return lookup(key, pc=pc)
    monkeypatch.setattr(store, "forecast_by_id", tracked)
    first = env.executor.recover()
    assert len(first) == 2 and first[0] == first[1]
    assert calls == [(saved["forecast_id"], "Scout")]
    assert env.events == ["controls", "lookup", "recover", "controls", "recover"]
    assert env.executor.recover() == first
    assert calls == [(saved["forecast_id"], "Scout")] * 2
    assert env.records == [env.original, env.original]
    with store.connect() as db:
        changed = dict(saved, reference_open="101")
        db.execute("UPDATE forecasts SET payload=? WHERE id=?", (json.dumps(changed), saved["forecast_id"]))
    env.events.clear()
    with pytest.raises(ContractError, match="hash or indexed identity"):
        env.executor.recover()
    assert len(calls) == 3 and env.records == [env.original, env.original]
    assert env.events == ["controls", "lookup"]


def test_unrelated_forecast_payload_is_not_read_by_order_recovery(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "forecasts.sqlite3")
    saved = saved_forecast(store)
    unrelated = saved_forecast(store, cycle="unrelated")
    with store.connect() as db:
        db.execute("UPDATE forecasts SET payload='not valid json' WHERE id=?", (unrelated["forecast_id"],))
    monkeypatch.setattr(store, "forecasts", forbid_full_scan)
    env = recovery_fixture(store, saved["forecast_id"])
    assert env.executor.recover()[0]["forecast_id"] == saved["forecast_id"]


@pytest.mark.parametrize("reason", ["missing", "other-pc", "missing-intention"])
def test_recovery_rejects_missing_or_out_of_scope_original_intention(tmp_path, monkeypatch, reason):
    store = ShadowStore(tmp_path / "forecasts.sqlite3")
    saved = saved_forecast(store, pc="Atlas" if reason == "other-pc" else "Scout")
    key = "missing" if reason == "missing" else saved["forecast_id"]
    env = recovery_fixture(store, key, intention=reason != "missing-intention")
    monkeypatch.setattr(store, "forecasts", forbid_full_scan)
    with pytest.raises(ContractError, match="original immutable intention"):
        env.executor.recover()
    assert env.records == [env.original]


@pytest.mark.parametrize("commitment", [None, "no_order_transport_entered", "no_open_commitment"])
def test_recovery_preserves_unknown_projection_and_clearance(tmp_path, commitment):
    store = ShadowStore(tmp_path / "forecasts.sqlite3")
    saved = saved_forecast(store)
    clearance = {"commitment": commitment, "original": "proof"} if commitment else None
    env = recovery_fixture(store, saved["forecast_id"], clearance=clearance)
    result = env.executor.recover()[0]
    if clearance:
        assert result["submission_status"] == "UNKNOWN"
        assert result["submitted_quantity"] is None and result["filled_quantity"] is None
        assert result["evidence"] == {"commitment_clearance": clearance}
        assert result["disposition"] == ("historical_submission_unknown_no_transport_commitment"
            if commitment == "no_order_transport_entered" else "historical_submission_unknown_no_open_commitment")
    else:
        assert result["disposition"] == "submission_recovery_required"
        assert result["state"] == "UNKNOWN" and result["send_token"] == env.original["send_token"]
    assert env.records == [env.original]


def test_recovery_with_empty_or_original_remote_journals_does_not_read_forecasts(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "forecasts.sqlite3")
    env = recovery_fixture(store, "original-remote")
    env.records[0]["request"]["request_id"] = "original-remote"
    monkeypatch.setattr(store, "forecasts", forbid_full_scan)
    monkeypatch.setattr(store, "forecast_by_id", forbid_full_scan)
    assert env.executor.recover() == []
    env.records.clear()
    assert env.executor.recover() == []
