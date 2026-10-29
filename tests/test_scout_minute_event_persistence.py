"""Native event-only persistence preserves acquired minute responses and cursors."""
from copy import deepcopy
import json
import sqlite3

import pytest

from ml.intraday.minute_feed import QuarterHourMinuteFeed
from tests.test_intraday_minute_feed import setup
from tests.test_intraday_forward_fill_overlap import feed_fixture

BATCH = "minute_feed_batch_saved"
QUEUED = "minute_feed_window_queued"
STARTED = "minute_feed_reference_recheck_started"
RECHECK_BATCH = "minute_feed_reference_recheck_batch_saved"


def event_rows(feed, kind):
    with feed.store.connect() as db:
        return db.execute("SELECT symbol,recorded_at,payload FROM events WHERE cycle_id=? AND kind=? ORDER BY id",
            (feed.identity, kind)).fetchall()


def bind(feed, pause, stopped=lambda: False, callback=None):
    feed._event_persistence_stopped = stopped
    feed._event_persistence_pause = pause
    feed._on_event_persistence_retry = callback


def block_event(feed, monkeypatch, kind):
    calls, writers = [], []
    real = feed.store.event
    feed.store.connection_timeout = 0
    def event(identity, name, value, **kwargs):
        if name == kind:
            calls.append(deepcopy((identity, name, value, kwargs)))
            if len(calls) == 1:
                writer = sqlite3.connect(feed.store.path, timeout=0)
                writer.execute("BEGIN IMMEDIATE")
                writers.append(writer)
        return real(identity, name, value, **kwargs)
    monkeypatch.setattr(feed.store, "event", event)
    return calls, writers


def unlock(writers):
    while writers:
        writer = writers.pop()
        writer.rollback()
        writer.close()


@pytest.mark.parametrize("kind", [QUEUED, BATCH])
def test_normal_feed_event_busy_preserves_one_batch_and_original_event(tmp_path, monkeypatch, kind):
    feed, args, clock, fetches = setup(tmp_path, monkeypatch)
    calls, writers = block_event(feed, monkeypatch, kind)
    original_clock = clock()
    pauses = []
    def pause(seconds):
        pauses.append(seconds)
        assert seconds == .2 and len(calls) == 1
        assert len(fetches) == (1 if kind == BATCH else 0)
        clock.advance(2)
        unlock(writers)
    bind(feed, pause)
    try:
        result = feed.step()
    finally:
        unlock(writers)
    assert result["status"] == "acquired" and len(fetches) == 1 and pauses == [.2]
    assert len(calls) == 2 and calls[0] == calls[1]
    rows = event_rows(feed, kind)
    assert len(rows) == 1 and rows[0][1] == original_clock.isoformat()
    assert json.loads(rows[0][2]) == calls[0][2]
    assert set(feed._owned_attempts) == set(json.loads(event_rows(feed, BATCH)[0][2])["attempt_ids"])
    assert all(len(feed._attempts(symbol, feed.first)) == 1 for symbol in feed.symbols)
    assert set(feed.outcomes) == {(feed.first, symbol) for symbol in feed.symbols}
    restored = QuarterHourMinuteFeed(**args)
    assert restored._owned_attempts == feed._owned_attempts
    assert restored.pending == feed.pending and restored.next_reference == feed.next_reference
    restored.step()
    assert len(fetches) == 1


@pytest.mark.parametrize("kind", [STARTED, RECHECK_BATCH])
def test_recheck_event_busy_preserves_saved_response_association_without_refetch(tmp_path, monkeypatch, kind):
    feed, args, clock, fetches, _ = feed_fixture(tmp_path, monkeypatch)
    feed.history_at("ABCL", feed.first)
    clock.advance(900)
    feed.step()  # Existing new-quarter priority remains intact.
    before = len(fetches)
    calls, writers = block_event(feed, monkeypatch, kind)
    pauses = []
    def pause(seconds):
        pauses.append(seconds)
        assert seconds == .2 and len(calls) == 1
        assert len(fetches) == before + (1 if kind == RECHECK_BATCH else 0)
        unlock(writers)
    bind(feed, pause)
    try:
        result = feed.step()
    finally:
        unlock(writers)
    assert result["kind"] == "reference_fill_overlap" and len(fetches) == before + 1
    assert pauses == [.2] and len(calls) == 2 and calls[0] == calls[1]
    assert len(event_rows(feed, kind)) == 1
    state = feed.reference_fills[feed.first, "ABCL"]
    assert state["started"] is True and len(state["attempt_ids"]) == 1
    assert {a.attempt_id for a in feed._recheck_attempts("ABCL", feed.first, state)} == state["attempt_ids"]
    restored = QuarterHourMinuteFeed(**args)
    saved = restored.reference_fills[feed.first, "ABCL"]
    assert saved["attempt_ids"] == state["attempt_ids"] and saved["started"] is True
    restored.step()
    assert len(fetches) == before + 1


@pytest.mark.parametrize("kind", [QUEUED, BATCH, STARTED, RECHECK_BATCH])
def test_stop_during_busy_retains_original_journal_without_falsely_completed_event(tmp_path, monkeypatch, kind):
    if kind in (STARTED, RECHECK_BATCH):
        feed, _, clock, fetches, _ = feed_fixture(tmp_path, monkeypatch)
        feed.history_at("ABCL", feed.first)
        clock.advance(900)
        feed.step()
    else:
        feed, _, clock, fetches = setup(tmp_path, monkeypatch)
    before = len(fetches)
    calls, writers = block_event(feed, monkeypatch, kind)
    stopped = []
    def pause(seconds):
        assert seconds == .2
        stopped.append(True)
        unlock(writers)
    bind(feed, pause, lambda: bool(stopped))
    try:
        with pytest.raises(InterruptedError):
            feed.step(stopped=lambda: bool(stopped))
    finally:
        unlock(writers)
    assert len(calls) == 1 and not event_rows(feed, kind)
    assert len(fetches) == before + (1 if kind in (BATCH, RECHECK_BATCH) else 0)
    if kind == BATCH:
        assert not feed._owned_attempts
        assert all(len(feed.journal.responses(**feed._slot(symbol, feed.first))) == 1 for symbol in feed.symbols)
    elif kind == RECHECK_BATCH:
        state = feed.reference_fills[feed.first, "ABCL"]
        assert not state["attempt_ids"] and state["completed"] is None
        assert len(feed.journal.responses(**feed._slot("ABCL", feed.first))) == 3
    elif kind == STARTED:
        assert feed.reference_fills[feed.first, "ABCL"]["started"] is False


def test_constructor_binds_initial_event_retry_and_optional_receipts_cannot_mask(tmp_path, monkeypatch):
    feed, args, clock, fetches = setup(tmp_path, monkeypatch)
    calls, writers = block_event(feed, monkeypatch, "minute_feed_initialized")
    notices = []
    def pause(seconds):
        assert seconds == .2
        unlock(writers)
    def receipt(phase, detail):
        notices.append((phase, deepcopy(detail)))
        raise OSError("optional receipt unavailable")
    try:
        new = QuarterHourMinuteFeed(**{**args, "session_id": "new-independent-feed",
            "event_persistence_stopped": lambda: False, "event_persistence_pause": pause,
            "on_event_persistence_retry": receipt})
    finally:
        unlock(writers)
    assert len(calls) == 2 and calls[0] == calls[1] and not fetches
    assert len(event_rows(new, "minute_feed_initialized")) == 1
    assert [phase for phase, _ in notices] == ["started", "recovered"]
    assert notices[0][1]["episode_id"] == notices[1][1]["episode_id"]
    assert notices[0][1]["kind"] == "minute_feed_initialized"


def test_nonbusy_event_failure_propagates_without_retry(tmp_path, monkeypatch):
    feed, _, _, fetches = setup(tmp_path, monkeypatch)
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("no such table: original")
    monkeypatch.setattr(feed.store, "event", fail)
    bind(feed, lambda _: pytest.fail("nonnative error retried"))
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        feed.step()
    assert not fetches


@pytest.mark.parametrize("case", ["default", "Atlas"])
def test_other_feed_callers_keep_original_contention_behavior(tmp_path, monkeypatch, case):
    feed, args, _, _ = setup(tmp_path, monkeypatch)
    if case == "Atlas":
        feed = QuarterHourMinuteFeed(**{**args, "pc": "Atlas", "session_id": "atlas-fixture"})
        bind(feed, lambda _: pytest.fail("Atlas event retry changed"))
    calls, writers = block_event(feed, monkeypatch, BATCH)
    try:
        with pytest.raises(sqlite3.OperationalError):
            feed.step()
    finally:
        unlock(writers)
    assert len(calls) == 1


@pytest.mark.parametrize("stopping", [True, False])
def test_data_service_classifies_stop_during_feed_initial_event_before_workers(tmp_path, monkeypatch, stopping):
    from ml.intraday import minute_feed, scout_services
    from tests.test_scout_services import data_case
    case = data_case(tmp_path, monkeypatch)
    stopped = []
    original = InterruptedError("initial event pending")
    def feed(**kwargs):
        assert kwargs["event_persistence_stopped"]() is False
        assert callable(kwargs["event_persistence_pause"])
        if stopping:
            stopped.append(True)
        kwargs["on_event_persistence_retry"]("stopped", {"kind": "minute_feed_initialized"})
        raise original
    monkeypatch.setattr(minute_feed, "QuarterHourMinuteFeed", feed)
    args = dict(repository=tmp_path, datastore=tmp_path, release=case.release,
        clock=lambda:case.now[0], sleep=case.sleep, stopped=lambda:bool(stopped))
    if stopping:
        result = scout_services.run_data_service(**args)
        status = json.loads((tmp_path / "intraday/services/data-status.json").read_bytes())
        assert result["status"] == status["status"] == "STOPPED" and status["errors"] == []
    else:
        with pytest.raises(InterruptedError) as caught:
            scout_services.run_data_service(**args)
        assert caught.value is original
    assert not case.stats.published and not case.stats.calls
    with sqlite3.connect(tmp_path / "intraday/services/handoff.sqlite3") as db:
        row = json.loads(db.execute("SELECT payload FROM recovery WHERE kind='minute_event_persistence_stopped'").fetchone()[0])
    assert row["source_session"].startswith("scout-data-")
    assert row["manifest_sha256"] == case.release["manifest_sha256"] and row["role"] == "data"
