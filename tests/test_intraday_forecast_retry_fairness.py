"""Due scientific recovery progresses even when fresh work spans quarters."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from ml.intraday import live_session
from ml.intraday.prediction import instant, STEP
from ml.intraday.store import ShadowStore
from tests.test_intraday_store import forecast


def exercise(tmp_path, monkeypatch, *, fresh_seconds=966, stop_after=6,
             retry_at="2026-10-12T17:16:00Z", close_after_first=False,
             persistent_symbol=None, prior_recovery_symbol=None, other_due_cycle=False):
    store = ShadowStore(tmp_path / "fair.sqlite3")
    old = "original-pending"
    symbols = ["AAPL", "CROX", "TWST"]
    reference = instant("2026-10-12T17:00:00Z")
    store.begin_cycle(old, {"pc": "Atlas", "symbols": symbols, "reference_at": reference.isoformat(),
        "mode": "live", "experiment": "manual", "configuration_hash": "fixture",
        "started_at": "2026-10-12T17:15:30+00:00", "status": "partial_failure",
        "last_recovery_symbol": prior_recovery_symbol,
        "failures": {s: {"retryable": True, "retry_at": retry_at,
                         "type": "AcquisitionPending"} for s in symbols}})
    store.begin_cycle("latest-saved", {"pc": "Atlas", "symbols": [],
        "reference_at": "2026-10-12T17:45:00+00:00", "mode": "live", "experiment": "manual",
        "configuration_hash": "fixture", "started_at": "2026-10-12T18:00:30+00:00",
        "status": "completed"})
    if other_due_cycle:
        store.begin_cycle("other-pending", {"pc": "Atlas", "symbols": ["PATH"],
            "reference_at": "2026-10-12T16:45:00+00:00", "mode": "live", "experiment": "manual",
            "configuration_hash": "fixture", "started_at": "2026-10-12T17:16:00+00:00",
            "status": "partial_failure", "failures": {"PATH": {"retryable": True,
                "retry_at": "2026-10-12T17:17:00+00:00"}}})
    # A different manual session and a nonretryable error remain outside this work.
    for identity, session, retryable in (("foreign", "other-human", True), ("permanent", "manual", False)):
        store.begin_cycle(identity, {"pc": "Atlas", "symbols": ["MU"],
            "reference_at": "2026-10-12T16:45:00+00:00", "mode": "live", "experiment": session,
            "configuration_hash": "fixture", "started_at": "2026-10-12T17:00:30+00:00",
            "status": "partial_failure", "failures": {"MU": {"retryable": retryable,
                "retry_at": "2026-10-12T17:01:00+00:00"}}})
    queue = live_session.ExecutionQueue(store, "manual")
    now = [instant("2026-10-12T18:20:14Z")]
    calls, originals = [], []

    def run(inputs, **kw):
        retry = kw["retry_symbols"]
        calls.append({"reference": kw["reference_at"], "retry": retry,
                      "allow_execution": kw["allow_execution"], "start": now[0]})
        if retry is None:
            identity = "fresh:" + kw["reference_at"].isoformat()
            store.begin_cycle(identity, {"pc": "Atlas", "symbols": [],
                "reference_at": kw["reference_at"].isoformat(), "mode": "live",
                "experiment": "manual", "configuration_hash": "fixture",
                "started_at": now[0].isoformat(), "status": "completed"})
            now[0] += timedelta(seconds=fresh_seconds)
        else:
            assert len(retry) == 1
            assert not kw["allow_execution"]
            identity = "other-pending" if retry[0] == "PATH" else old
            if retry[0] != persistent_symbol:
                value = store.save_forecast(forecast(mode="live", symbol=retry[0], cycle_id=identity,
                    issued_at=now[0].isoformat(), reference_at=kw["reference_at"].isoformat(),
                    target_open=(kw["reference_at"] + 2 * STEP).isoformat(),
                    target_close=(kw["reference_at"] + 3 * STEP).isoformat(),
                    data_through=(kw["reference_at"] + STEP).isoformat(),
                    execution_blocked_reason="recovered_older_cycle"))
                originals.append(value)
                kw["on_published"](value)
            now[0] += timedelta(seconds=2)
        return {"cycle_id": identity}

    monkeypatch.setattr(live_session, "LiveHistories", lambda **kw: SimpleNamespace(bounded=kw["bounded"]))
    monkeypatch.setattr(live_session, "eligible_reference", lambda ref: True)
    runner = SimpleNamespace(store=store, config=SimpleNamespace(publication_offset_seconds=30), run_cycle=run)
    close_at = now[0] + timedelta(seconds=fresh_seconds) if close_after_first else None
    try:
        live_session.forecasting_loop(runner, histories={}, provider=object(), journal=object(), queue=queue,
            stopped=lambda: len(calls) >= stop_after, clock=lambda: now[0],
            pause=lambda seconds: now.__setitem__(0, now[0] + timedelta(seconds=seconds)), close_at=close_at)
    except InterruptedError:
        # Installed storage continuation retains its existing close exception
        # when finalization encounters the close boundary during a long pass.
        if not close_after_first or now[0] < close_at:
            raise
    return store, queue, calls, originals, now[0]


@pytest.mark.parametrize("fresh_seconds", [900, 966, 1800])
def test_long_fresh_passes_cannot_starve_original_due_recovery(tmp_path, monkeypatch, fresh_seconds):
    store, queue, calls, originals, now = exercise(tmp_path, monkeypatch, fresh_seconds=fresh_seconds)
    assert [c["retry"] for c in calls] == [None, ["AAPL"], None, ["CROX"], None, ["TWST"]]
    fresh = [c for c in calls if c["retry"] is None]
    assert all(c["reference"] == live_session.latest_completed_reference(
        c["start"], publication_offset_seconds=30) for c in fresh)
    assert all(c["allow_execution"] for c in fresh)
    assert [f["symbol"] for f in originals] == ["AAPL", "CROX", "TWST"]
    assert all(store.forecast("original-pending", f["symbol"]) == f for f in originals)
    assert queue.due(now) == []
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM intraday_live_queue").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='forecast_execution_blocked'").fetchone()[0] == 3


@pytest.mark.parametrize("close_after_first", [False, True])
def test_stop_or_close_after_fresh_pass_never_starts_recovery(tmp_path, monkeypatch, close_after_first):
    store, queue, calls, originals, now = exercise(tmp_path, monkeypatch,
        stop_after=6 if close_after_first else 1, close_after_first=close_after_first)
    assert len(calls) == 1 and calls[0]["retry"] is None and originals == []
    assert queue.due(now) == []


def test_future_retry_time_does_not_delay_fresh_quarters(tmp_path, monkeypatch):
    _, queue, calls, originals, now = exercise(tmp_path, monkeypatch, retry_at="2026-10-13T17:00:00Z")
    assert len(calls) == 6 and all(c["retry"] is None for c in calls)
    assert originals == [] and queue.due(now) == []


def test_still_missing_first_symbol_cannot_starve_other_originals(tmp_path, monkeypatch):
    store, queue, calls, originals, now = exercise(tmp_path, monkeypatch, persistent_symbol="AAPL")
    assert [c["retry"] for c in calls] == [None, ["AAPL"], None, ["CROX"], None, ["TWST"]]
    assert [f["symbol"] for f in originals] == ["CROX", "TWST"]
    assert store.forecast("original-pending", "AAPL") is None and queue.due(now) == []


def test_saved_symbol_cursor_survives_new_loop_without_lost_memory(tmp_path, monkeypatch):
    _, queue, calls, originals, now = exercise(tmp_path, monkeypatch, stop_after=4,
        persistent_symbol="AAPL", prior_recovery_symbol="AAPL")
    assert [c["retry"] for c in calls] == [None, ["CROX"], None, ["TWST"]]
    assert [f["symbol"] for f in originals] == ["CROX", "TWST"] and queue.due(now) == []


def test_fair_turns_rotate_across_distinct_original_cycles(tmp_path, monkeypatch):
    _, queue, calls, originals, now = exercise(tmp_path, monkeypatch, other_due_cycle=True, stop_after=8)
    assert [c["retry"] for c in calls] == [None, ["AAPL"], None, ["PATH"], None, ["CROX"], None, ["TWST"]]
    assert [f["symbol"] for f in originals] == ["AAPL", "PATH", "CROX", "TWST"] and queue.due(now) == []


def test_current_valid_retry_keeps_execution_eligibility(tmp_path, monkeypatch):
    from tests.test_intraday_live_session import saved_pending_cycle
    store = ShadowStore(tmp_path / "current.sqlite3")
    saved_pending_cycle(store, "manual", "2026-10-12T17:00:00Z")
    calls = []
    monkeypatch.setattr(live_session, "LiveHistories", lambda **kw: object())
    runner = SimpleNamespace(store=store, config=SimpleNamespace(publication_offset_seconds=30),
        run_cycle=lambda inputs, **kw: calls.append(kw))
    live_session.forecasting_loop(runner, histories={}, provider=object(), journal=object(),
        queue=SimpleNamespace(session_id="manual", enqueue=lambda _: pytest.fail("No synthetic publish")),
        stopped=lambda: bool(calls), clock=lambda: instant("2026-10-12T17:16:00Z"), pause=lambda _: None)
    assert calls[0]["retry_symbols"] == ["AAPL"] and calls[0]["allow_execution"]
