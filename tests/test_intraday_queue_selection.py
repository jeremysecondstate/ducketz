"""Execution polling reads only eligible immutable forecasts from its own queue."""
from datetime import timedelta
import json

import pytest

from ml.intraday.live_session import ExecutionQueue
from ml.intraday.prediction import ContractError, instant
from ml.intraday.store import ShadowStore
from tests.test_intraday_store import forecast


NOW = "2026-10-08T17:40:00Z"


def saved(store, label, *, symbol=None, pc="Scout", reference="2026-10-08T17:00:00Z", **changes):
    reference = instant(reference)
    value = forecast(pc=pc, symbol=symbol or label, cycle_id=label, mode="live",
        reference_at=reference.isoformat(),
        issued_at=(reference + timedelta(minutes=16)).isoformat(),
        target_open=(reference + timedelta(minutes=30)).isoformat(),
        target_close=(reference + timedelta(minutes=45)).isoformat(),
        data_through=(reference + timedelta(minutes=15)).isoformat())
    value.update(changes)
    return store.save_forecast(value)


def queue(store, session="own", *, pc="Scout"):
    return ExecutionQueue(store, session, pc=pc, event_sink=lambda event: None)


def validation_calls(store, monkeypatch):
    original = store._forecast_value
    calls = []

    def validate(row):
        calls.append((row[0], len(row[-1])))
        return original(row)

    monkeypatch.setattr(store, "_forecast_value", validate)
    return calls


def corrupt_payload(store, value):
    changed = dict(value, probability_not_down=.71)
    with store.connect() as db:
        db.execute("UPDATE forecasts SET payload=? WHERE id=?",
                   (json.dumps(changed), value["forecast_id"]))


def test_empty_due_queue_does_not_decode_any_historical_forecast(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    saved(store, "historical", diagnostic_padding="x" * 100000)
    own = queue(store)
    calls = validation_calls(store, monkeypatch)
    monkeypatch.setattr(store, "forecasts", lambda **kwargs: pytest.fail("Execution poll requested all historical forecasts"))

    assert own.due(NOW) == []
    assert calls == []


def test_due_validates_only_selected_payloads_and_preserves_original_rowid_order(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    for index in range(12):
        saved(store, "history-" + str(index), diagnostic_padding="x" * 100000)
    first = saved(store, "first-row", symbol="ZZZ", reference="2026-10-08T17:15:00Z")
    second = saved(store, "second-row", symbol="AAA")
    own = queue(store)
    own.enqueue(second)
    own.enqueue(first)
    calls = validation_calls(store, monkeypatch)

    assert own.due(NOW) == [first, second]
    assert [identity for identity, _ in calls] == [first["forecast_id"], second["forecast_id"]]
    assert sum(characters for _, characters in calls) < 10000


@pytest.mark.parametrize("scope", ["history", "other-session", "foreign-participant", "deferred", "completed"])
def test_unselected_corrupt_forecast_is_not_a_current_execution_dependency(tmp_path, scope):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    own = queue(store)
    selected = saved(store, "selected")
    own.enqueue(selected)
    unrelated = saved(store, "unrelated", pc="Atlas" if scope == "foreign-participant" else "Scout")
    if scope == "other-session":
        queue(store, "other").enqueue(unrelated)
    elif scope == "foreign-participant":
        queue(store, pc="Atlas").enqueue(unrelated)
    elif scope in {"deferred", "completed"}:
        own.enqueue(unrelated)
        if scope == "deferred":
            own.defer(unrelated["forecast_id"], now=NOW, seconds=30)
        else:
            own.complete(unrelated["forecast_id"], "completed")
    corrupt_payload(store, unrelated)

    assert own.due(NOW) == [selected]


@pytest.mark.parametrize("column", ["payload", "symbol", "cycle_id", "reference_at", "issued_at"])
def test_selected_forecast_keeps_payload_and_indexed_identity_validation(tmp_path, column):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    selected = saved(store, "selected")
    own = queue(store)
    own.enqueue(selected)
    if column == "payload":
        corrupt_payload(store, selected)
    else:
        with store.connect() as db:
            db.execute("UPDATE forecasts SET " + column + "=? WHERE id=?",
                       ("changed", selected["forecast_id"]))

    with pytest.raises(ContractError, match="forecast evidence hash or indexed identity"):
        own.due(NOW)


def test_due_preserves_foreign_participant_and_manual_session_scope(tmp_path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    own_value = saved(store, "own")
    previous_value = saved(store, "previous-session")
    foreign_value = saved(store, "foreign-participant", pc="Atlas")
    own = queue(store)
    own.enqueue(own_value)
    queue(store, "previous").enqueue(previous_value)
    queue(store, pc="Atlas").enqueue(foreign_value)

    assert own.due(NOW) == [own_value]
    assert queue(store, "fresh-manual-session").due(NOW) == []


def test_queue_retry_and_broker_wait_remain_durable_and_skip_payload_reads(tmp_path, monkeypatch):
    path = tmp_path / "shadow.sqlite3"
    store = ShadowStore(path)
    selected = saved(store, "selected")
    own = queue(store)
    own.enqueue(selected)
    own.defer(selected["forecast_id"], now=NOW, seconds=30, broker_wait=True)
    restored_store = ShadowStore(path)
    restored = queue(restored_store)
    calls = validation_calls(restored_store, monkeypatch)

    assert restored.due("2026-10-08T17:40:29Z") == []
    assert calls == []
    assert restored.due("2026-10-08T17:40:30Z") == [selected]
    assert [identity for identity, _ in calls] == [selected["forecast_id"]]
    restored.complete(selected["forecast_id"], "submitted")
    calls.clear()
    assert restored.due("2026-10-08T17:40:31Z") == []
    assert calls == []


@pytest.mark.parametrize("newer_state", ["pending", "deferred", "completed"])
def test_newer_queued_forecast_still_supersedes_unsent_older_work(tmp_path, newer_state):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    older = saved(store, "older", symbol="DOCU")
    newer = saved(store, "newer", symbol="DOCU", reference="2026-10-08T17:15:00Z")
    own = queue(store)
    own.enqueue(older)
    own.enqueue(newer)
    expected = [newer]
    if newer_state == "deferred":
        own.defer(newer["forecast_id"], now=NOW, seconds=30)
        expected = []
    elif newer_state == "completed":
        own.complete(newer["forecast_id"], "no_purchasing_capacity")
        expected = []

    assert own.due(NOW) == expected
    assert store.forecasts(pc="Scout") == [older, newer]


def test_later_issue_of_older_reference_never_supersedes_newer_reference(tmp_path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    newer = saved(store, "newer-reference", symbol="DOCU", reference="2026-10-08T17:15:00Z")
    older = saved(store, "older-reference", symbol="DOCU", issued_at="2026-10-08T17:32:00Z")
    own = queue(store)
    own.enqueue(newer)
    own.enqueue(older)

    assert own.due(NOW) == [newer]


def test_later_issue_for_same_reference_supersedes_without_rewriting_forecasts(tmp_path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    first = saved(store, "first", symbol="DOCU")
    second = saved(store, "second", symbol="DOCU", issued_at="2026-10-08T17:17:00Z")
    own = queue(store)
    own.enqueue(first)
    own.enqueue(second)
    with store.connect() as db:
        before = db.execute("SELECT id,payload FROM forecasts ORDER BY rowid").fetchall()

    assert own.due(NOW) == [second]
    assert own.due(NOW) == [second]
    with store.connect() as db:
        assert db.execute("SELECT id,payload FROM forecasts ORDER BY rowid").fetchall() == before
        assert db.execute("SELECT status FROM intraday_live_queue ORDER BY rowid").fetchall() == [("pending",), ("pending",)]
