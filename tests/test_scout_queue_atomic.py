"""Offline insertion/retirement transaction checks using synthetic forecasts."""
from datetime import timedelta
import json

import pytest

from ml.intraday.live_session import ExecutionQueue
from ml.intraday.prediction import TARGET_VERSION, instant
from ml.intraday.scout_services import ServiceExecutionQueue
from ml.intraday.store import ShadowStore


def saved(store, cycle, *, issued_at="2026-10-08T17:16:02Z", pc="Scout", offset=0, **changes):
    reference = instant("2026-10-08T17:00:00Z") + timedelta(minutes=offset)
    value = dict(pc=pc, symbol="AAPL", cycle_id=cycle,
        reference_at=reference.isoformat(), reference_open="100",
        target_open=(reference + timedelta(minutes=30)).isoformat(),
        target_close=(reference + timedelta(minutes=45)).isoformat(), issued_at=issued_at,
        information_available_at=(reference + timedelta(minutes=15, seconds=3)).isoformat(),
        data_through=(reference + timedelta(minutes=15)).isoformat(), target_version=TARGET_VERSION,
        mode="live", availability_basis="observed", probability_not_down=.72,
        configured_count=1, successful_count=1, ensemble_status="full",
        models=[dict(name="fixture", status="success", probability_not_down=.72)])
    return store.save_forecast({**value, **changes})


@pytest.fixture
def queue(tmp_path):
    store = ShadowStore(tmp_path / "fixture.sqlite3")
    return ServiceExecutionQueue(store, "scout-atomic-fixture", pc="Scout", event_sink=lambda event: None)


def statuses(queue):
    with queue.store.connect() as db:
        return dict(db.execute("SELECT forecast_id,status FROM intraday_live_queue"))


@pytest.mark.parametrize("after_retirement", [False, True])
def test_failed_retirement_rolls_back_insertion_and_retry_publishes_once(queue, monkeypatch, after_retirement):
    old = saved(queue.store, "old")
    newer = saved(queue.store, "new", issued_at="2026-10-08T17:17:02Z")
    queue.enqueue(old)
    publications = []
    queue.event_sink = publications.append
    retire = queue._retire

    def failed(db, now):
        if after_retirement:
            retire(db, now)
        raise RuntimeError("fixture interrupted retirement")

    monkeypatch.setattr(queue, "_retire", failed)
    with pytest.raises(RuntimeError, match="interrupted retirement"):
        queue.enqueue(newer)
    assert statuses(queue) == {old["forecast_id"]: "pending"}
    assert publications == []
    assert queue.store.forecast("new", "AAPL") == newer

    monkeypatch.setattr(queue, "_retire", retire)
    queue.enqueue(newer)
    queue.enqueue(newer)
    assert statuses(queue) == {old["forecast_id"]: "superseded", newer["forecast_id"]: "pending"}
    assert [event["forecast_id"] for event in publications] == [newer["forecast_id"]]


def test_publication_observes_new_row_and_retired_predecessor_committed_together(queue):
    old = saved(queue.store, "old")
    newer = saved(queue.store, "new", issued_at="2026-10-08T17:17:02Z")
    queue.enqueue(old)
    observed = []
    queue.event_sink = lambda event: observed.append((event["forecast_id"], statuses(queue)))
    queue.enqueue(newer)
    assert observed == [(newer["forecast_id"], {
        old["forecast_id"]: "superseded", newer["forecast_id"]: "pending"})]


def test_retirement_preserves_other_participant_and_session_rows(queue):
    old = saved(queue.store, "old")
    queue.enqueue(old)
    peer = saved(queue.store, "peer", pc="Atlas")
    ExecutionQueue(queue.store, queue.session_id, pc="Atlas", event_sink=lambda event: None).enqueue(peer)
    other = saved(queue.store, "other")
    ServiceExecutionQueue(queue.store, "other-scout-session", pc="Scout", event_sink=lambda event: None).enqueue(other)
    with queue.store.connect() as db:
        preserved = db.execute("SELECT * FROM intraday_live_queue WHERE forecast_id IN (?,?) ORDER BY forecast_id",
            (peer["forecast_id"], other["forecast_id"])).fetchall()
    newer = saved(queue.store, "new", issued_at="2026-10-08T17:17:02Z")
    queue.enqueue(newer)
    queue.retire("2026-10-08T17:46:00Z")
    with queue.store.connect() as db:
        assert db.execute("SELECT * FROM intraday_live_queue WHERE forecast_id IN (?,?) ORDER BY forecast_id",
            (peer["forecast_id"], other["forecast_id"])).fetchall() == preserved
    assert statuses(queue)[old["forecast_id"]] == "superseded"
    assert statuses(queue)[newer["forecast_id"]] == "expired"


@pytest.mark.parametrize("reason", ["fixture_blocked", None])
def test_blocked_forecast_still_expires_prior_work_without_being_enqueued(queue, reason):
    old = saved(queue.store, "old")
    queue.enqueue(old)
    changes = dict(offset=15, execution_blocked_reason=reason) if reason else {}
    blocked = saved(queue.store, "blocked", issued_at="2026-10-08T17:46:00Z", **changes)
    publications = []
    queue.event_sink = publications.append
    queue.enqueue(blocked)
    assert statuses(queue) == {old["forecast_id"]: "expired"}
    assert publications == []
    with queue.store.connect() as db:
        events = db.execute("SELECT payload FROM events WHERE kind='forecast_execution_blocked'").fetchall()
    assert len(events) == 1
    assert json.loads(events[0][0])["forecast_id"] == blocked["forecast_id"]


def test_base_queue_keeps_existing_nonretiring_behavior(tmp_path):
    store = ShadowStore(tmp_path / "base.sqlite3")
    queue = ExecutionQueue(store, "base-fixture", pc="Atlas", event_sink=lambda event: None)
    old = saved(store, "old", pc="Atlas")
    newer = saved(store, "new", pc="Atlas", issued_at="2026-10-08T17:17:02Z")
    queue.enqueue(old)
    queue.enqueue(newer)
    assert statuses(queue) == {old["forecast_id"]: "pending", newer["forecast_id"]: "pending"}
