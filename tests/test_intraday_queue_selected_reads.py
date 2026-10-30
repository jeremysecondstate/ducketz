"""Queue selection must not decode unrelated saved scientific history."""
import json

import pytest

from ml.intraday.live_session import ExecutionQueue
from ml.intraday.prediction import ContractError
from ml.intraday.scout_services import ServiceExecutionQueue
from ml.intraday.store import ShadowStore
from tests.test_intraday_store import forecast

NOW = "2026-10-08T17:17:00Z"


@pytest.fixture(params=[ExecutionQueue, ServiceExecutionQueue])
def queue(tmp_path, request):
    return request.param(ShadowStore(tmp_path / "fixture.sqlite3"),
                         "selected-reads-fixture", pc="Scout", event_sink=lambda event: None)


def save(queue, cycle, *, symbol="AAPL", session=None):
    queue.store.begin_cycle(cycle, {"pc": "Scout", "experiment": session or queue.session_id,
                                   "mode": "live", "started_at": NOW})
    return queue.store.save_forecast(forecast(pc="Scout", symbol=symbol, mode="live", cycle_id=cycle))


def rows(store):
    with store.connect() as db:
        return {name: db.execute("SELECT * FROM " + name + " ORDER BY rowid").fetchall()
                for name in ("forecasts", "intraday_live_queue", "intraday_live_retry", "cycles", "events")}


@pytest.mark.parametrize("scope", ["unqueued", "completed", "other_session"])
@pytest.mark.parametrize("damage", ["json", "hash", "index"])
def test_unselected_corrupt_history_does_not_block_due_work_or_change_evidence(queue, scope, damage):
    prior = save(queue, "history", symbol="AMZN",
                 session="other-session" if scope == "other_session" else None)
    if scope != "unqueued":
        if scope == "other_session":
            ExecutionQueue(queue.store, "other-session", pc="Scout", event_sink=lambda event: None).enqueue(prior)
        else:
            queue.enqueue(prior)
            queue.complete(prior["forecast_id"], "completed")
    current = save(queue, "current")
    queue.enqueue(current)
    with queue.store.connect() as db:
        if damage == "json":
            db.execute("UPDATE forecasts SET payload='{' WHERE id=?", (prior["forecast_id"],))
        elif damage == "hash":
            db.execute("UPDATE forecasts SET payload=? WHERE id=?",
                       (json.dumps({**prior, "probability_not_down": .99}), prior["forecast_id"]))
        else:
            db.execute("UPDATE forecasts SET reference_at='changed' WHERE id=?", (prior["forecast_id"],))
    before = rows(queue.store)
    assert queue.due(NOW) == [current]
    assert queue.due(NOW) == [current]
    assert rows(queue.store) == before


def test_empty_selection_does_not_decode_unqueued_history(queue):
    prior = save(queue, "history")
    with queue.store.connect() as db:
        db.execute("UPDATE forecasts SET payload='{' WHERE id=?", (prior["forecast_id"],))
    before = rows(queue.store)
    assert queue.due(NOW) == []
    assert rows(queue.store) == before


def test_selected_original_still_requires_existing_hash_validation(queue):
    current = save(queue, "current")
    queue.enqueue(current)
    with queue.store.connect() as db:
        db.execute("UPDATE forecasts SET payload=? WHERE id=?",
                   (json.dumps({**current, "probability_not_down": .99}), current["forecast_id"]))
    before = rows(queue.store)
    with pytest.raises(ContractError, match="hash or indexed identity"):
        queue.due(NOW)
    assert rows(queue.store) == before


def test_selection_preserves_store_insertion_order_and_broker_retry_floor(queue):
    first = save(queue, "first", symbol="MSFT")
    second = save(queue, "second", symbol="AAPL")
    queue.enqueue(first)
    queue.enqueue(second)
    assert queue.due(NOW) == [first, second]
    queue.broker_wait(now=NOW, seconds=30)
    assert queue.due(NOW) == []
    assert queue.due("2026-10-08T17:17:30Z") == [first, second]
