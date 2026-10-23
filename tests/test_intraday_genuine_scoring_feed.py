"""Genuine scoring avoids repeated projection and preserves durable real rechecks."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import threading
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.intraday import data, forward_fill, live_session, native_interval
from ml.intraday.minute_feed import QuarterHourMinuteFeed
from ml.intraday.prediction import ContractError, STEP, digest, instant
from tests.test_intraday_forward_fill_overlap import events, feed_fixture, settle
from tests.test_intraday_native_feed import native_candle, sparse_rows
from tests.test_intraday_store import forecast


def saved_forecast(feed):
    reference = feed.first - 2 * STEP
    return feed.store.save_forecast(forecast(mode="live", pc="Scout", symbol="ABCL",
        reference_at=reference.isoformat(), reference_open="100",
        target_open=feed.first.isoformat(), target_close=(feed.first + STEP).isoformat(),
        issued_at=(reference + STEP + timedelta(seconds=2)).isoformat(),
        information_available_at=(reference + STEP + timedelta(seconds=1)).isoformat(),
        data_through=(reference + STEP).isoformat(), source_dataset=feed.dataset,
        source_id=feed.source_id, native_interval_contract=native_interval.CONTRACT))


def score(feed, clock, polls=2, *, history_source=None, after_poll=None):
    turns = []
    def pause(seconds):
        turns.append(seconds)
        if after_poll:
            after_poll(len(turns))
    live_session.scoring_loop(feed.store, histories=feed.histories, provider=None,
        journal=None, session_id="genuine-scoring-fixture", stopped=lambda: len(turns) >= polls,
        clock=clock, pause=pause, pc="Scout",
        history_source=history_source or feed.scoring_history_source)
    assert turns == [3] * polls


def projection_counter(monkeypatch):
    calls = []
    original = forward_fill.make_reference
    def counted(*args, **kwargs):
        calls.append(len(args[0]))
        return original(*args, **kwargs)
    monkeypatch.setattr(forward_fill, "make_reference", counted)
    return calls


def qualified_candle(symbol, reference, available_at, *, shift=0):
    """Generate proof-consistent native observations and actual receipt clocks."""
    opened, available = instant(reference), instant(available_at)
    rows = tuple({**row, **{k: row[k] + shift for k in ("open", "high", "low", "close")}}
                 for row in sparse_rows(opened))
    responses = tuple(data.IntervalResponse("qualified-" + str(n) + "-" + str(shift), symbol,
        "databento:EQUS.MINI:ohlcv-1m", "EQUS.MINI", opened.isoformat(),
        (opened + STEP).isoformat(), (available - timedelta(seconds=n)).isoformat(),
        (available - timedelta(seconds=n)).isoformat(), "success", rows=rows,
        coverage_open=opened.isoformat(), coverage_close=(opened + STEP).isoformat()) for n in (4, 0))
    session = next(s for s in data.equity_sessions(opened, opened + STEP) if s.contains(opened))
    candle = native_interval.resolve_native_interval(responses, session=session)["candle"]
    assert native_interval.completed_native_interval(candle)
    return candle


def test_seeded_missing_polls_do_no_projection_and_keep_forecast_recipe(tmp_path, monkeypatch):
    feed, _, clock, fetches, _ = feed_fixture(tmp_path, monkeypatch)
    saved = saved_forecast(feed)
    original = deepcopy(feed.histories["ABCL"])
    projected = feed.history_source(feed.first).get("ABCL")
    before = deepcopy(feed.reference_fills)
    calls = projection_counter(monkeypatch)
    score(feed, clock, 3)
    assert calls == [] and not feed.store.revisions(saved["forecast_id"])
    assert feed.reference_fills == before and len(fetches) == 2
    assert not events(feed, "minute_feed_reference_fill_revised")
    pd.testing.assert_frame_equal(feed.histories["ABCL"].frame, original.frame)
    assert feed.histories["ABCL"].manifest == original.manifest
    again = feed.history_source(feed.first).get("ABCL")
    assert len(calls) == 1
    pd.testing.assert_frame_equal(again.frame, projected.frame)
    assert again.manifest == projected.manifest
    assert feed.store.forecast_by_id(saved["forecast_id"]) == saved


def test_unprimed_scoring_registers_one_full_receipt_then_restores(tmp_path, monkeypatch):
    feed, args, clock, fetches, _ = feed_fixture(tmp_path, monkeypatch)
    saved = saved_forecast(feed)
    empty = deepcopy(feed.outcomes)
    calls = projection_counter(monkeypatch)
    score(feed, clock, 3)
    assert len(calls) == 1 and not feed.store.revisions(saved["forecast_id"])
    receipt = events(feed, "minute_feed_reference_filled")
    assert len(receipt) == 1 and forward_fill.verified_reference(receipt[0]["reference"])
    assert digest(receipt[0]["reference"]) == receipt[0]["reference_sha256"]
    state = deepcopy(feed.reference_fills)
    restored = QuarterHourMinuteFeed(**args)
    score(restored, clock, 3)
    assert len(calls) == 1 and restored.reference_fills == state
    assert events(restored, "minute_feed_reference_filled") == receipt
    assert restored.outcomes == empty and len(fetches) == 2


@pytest.mark.parametrize("recovery", ["empty", "observed"])
def test_scoring_registration_survives_interrupted_real_overlap(tmp_path, monkeypatch, recovery):
    feed, args, clock, fetches, counts = feed_fixture(tmp_path, monkeypatch, recovery=recovery)
    saved = saved_forecast(feed)
    original = deepcopy(feed.outcomes[feed.first, "ABCL"])
    calls = projection_counter(monkeypatch)
    score(feed, clock)
    assert len(calls) == 1
    clock.advance(900)
    feed.step()
    assert fetches[-1][0] == feed.first + STEP
    feed.step()
    key = feed.first, "ABCL"
    interrupted = deepcopy(feed.reference_fills[key])
    assert len(interrupted["attempt_ids"]) == 1 and interrupted["completed"] is None
    restored = QuarterHourMinuteFeed(**args)
    score(restored, clock)
    assert restored.reference_fills[key] == interrupted and len(calls) == 1
    clock.advance(4)
    completed = settle(restored, clock, feed.first)
    assert counts[feed.first] == 4 and len(completed["attempt_ids"]) == 2
    assert completed["genuine_rows_arrived"] == (recovery == "observed")
    score(restored, clock, 3)
    revisions = restored.store.revisions(saved["forecast_id"])
    assert len(revisions) == (1 if recovery == "observed" else 0)
    if revisions:
        assert revisions[0]["status"] == "scored"
        assert Decimal(revisions[0]["target_close_price"]) == Decimal("10.13")
    assert restored.outcomes[key] == original
    old_fetches = len(fetches)
    again = QuarterHourMinuteFeed(**args)
    score(again, clock)
    for _ in range(3):
        again.step()
        clock.advance(4)
    assert len(fetches) == old_fetches and len(calls) == 1
    assert len(events(again, "minute_feed_reference_recheck_started")) == 1
    assert len(events(again, "minute_feed_reference_recheck_completed")) == 1
    assert again.store.forecast_by_id(saved["forecast_id"]) == saved


@pytest.mark.parametrize("recovery", ["error", "permanent"])
def test_scoring_never_reopens_or_resets_failed_recheck(tmp_path, monkeypatch, recovery):
    feed, args, clock, fetches, _ = feed_fixture(tmp_path, monkeypatch, recovery=recovery)
    saved_forecast(feed)
    score(feed, clock)
    clock.advance(900)
    feed.step()
    feed.step()
    before = deepcopy(feed.reference_fills)
    key = feed.first, "ABCL"
    if recovery == "error":
        assert before[key]["retry_at"] > clock() and before[key]["completed"] is None
    else:
        assert before[key]["completed"]["reason"] == "permanent_error"
    restored = QuarterHourMinuteFeed(**args)
    count = len(fetches)
    calls = projection_counter(monkeypatch)
    score(restored, clock, 3)
    assert restored.reference_fills == before and len(fetches) == count and calls == []


def test_late_genuine_target_and_correction_score_once_without_accessor(tmp_path, monkeypatch):
    feed, _, clock, _, _ = feed_fixture(tmp_path, monkeypatch)
    saved = saved_forecast(feed)
    score(feed, clock)
    missing = deepcopy(feed.outcomes[feed.first, "ABCL"])
    first = qualified_candle("ABCL", feed.first, clock())
    second = qualified_candle("ABCL", feed.first, clock() + timedelta(seconds=8), shift=1)
    feed.store.record_candle(first)
    def bypass(_):
        pytest.fail("Saved genuine target must bypass the feed accessor")
    def correction(poll):
        if poll == 1:
            feed.store.record_candle(second)
            clock.advance(8)
    calls = projection_counter(monkeypatch)
    score(feed, clock, 3, history_source=bypass, after_poll=correction)
    revisions = feed.store.revisions(saved["forecast_id"])
    assert len(revisions) == 2 and all(r["status"] == "scored" for r in revisions)
    assert [Decimal(r["target_close_price"]) for r in revisions] == [Decimal("10.13"), Decimal("11.13")]
    assert revisions[0]["candle_id"] != revisions[1]["candle_id"]
    assert feed.store.forecast_by_id(saved["forecast_id"]) == saved
    assert feed.outcomes[feed.first, "ABCL"] == missing and calls == []


def test_accessor_sees_late_genuine_before_empty_receipt(tmp_path, monkeypatch):
    feed, _, clock, _, _ = feed_fixture(tmp_path, monkeypatch)
    saved = saved_forecast(feed)
    score(feed, clock)
    before = deepcopy(feed.reference_fills)
    feed._merge("ABCL", [qualified_candle("ABCL", feed.first, clock())])
    calls = projection_counter(monkeypatch)
    history = feed.scoring_history_source(feed.first).get("ABCL")
    assert not history.frame.iloc[-1].synthetic and calls == []
    score(feed, clock, 3)
    assert len(feed.store.revisions(saved["forecast_id"])) == 1
    assert feed.reference_fills == before and calls == []


def test_scoring_does_not_freeze_later_forecast_anchor_revision(tmp_path, monkeypatch):
    feed, args, clock, _, _ = feed_fixture(tmp_path, monkeypatch)
    saved_forecast(feed)
    original = feed.histories["ABCL"]
    score(feed, clock)
    receipt = events(feed, "minute_feed_reference_filled")[0]
    feed.histories["ABCL"] = data.History(original.frame.iloc[:-1].copy(), original.manifest)
    before = deepcopy(feed.reference_fills)
    calls = projection_counter(monkeypatch)
    score(feed, clock)
    assert feed.reference_fills == before and calls == []
    revised = feed.history_source(feed.first).get("ABCL").frame.iloc[-1].to_dict()
    assert revised["anchor_opened_at"] != receipt["anchor_opened_at"] and len(calls) == 1
    changes = events(feed, "minute_feed_reference_fill_revised")
    assert len(changes) == 1 and changes[0]["prior_reference_sha256"] == receipt["reference_sha256"]
    assert changes[0]["recheck_due_at"] == receipt["recheck_due_at"]
    restored = QuarterHourMinuteFeed(**args)
    assert restored.reference_fills[feed.first, "ABCL"]["latest_projection"] == changes[0]


def test_pending_unusable_no_anchor_and_outside_day_preserve_errors(tmp_path, monkeypatch):
    feed, _, _, fetches, _ = feed_fixture(tmp_path, monkeypatch)
    calls = projection_counter(monkeypatch)
    with pytest.raises(data.AcquisitionPending):
        feed.scoring_history_source(feed.first + STEP).get("ABCL")
    with pytest.raises(ContractError, match="latched eligible day"):
        feed.scoring_history_source(feed.first + timedelta(days=1)).get("ABCL")
    assert calls == [] and not feed.reference_fills and len(fetches) == 2
    feed.outcomes[feed.first, "ABCL"] = {"status": "resolved_no_usable_15m", "reason": "permanent_error"}
    with pytest.raises(ContractError, match="permanent_error"):
        feed.scoring_history_source(feed.first).get("ABCL")
    assert calls == [] and not feed.reference_fills
    feed.outcomes[feed.first, "ABCL"]["reason"] = "source_empty_interval"
    feed.histories.clear()
    with pytest.raises(ContractError, match="no genuine source history"):
        feed.scoring_history_source(feed.first).get("ABCL")
    assert not feed.reference_fills and len(fetches) == 2


def test_prior_session_genuine_reuse_does_not_expand_day(tmp_path, monkeypatch):
    feed, args, _, fetches, _ = feed_fixture(tmp_path, monkeypatch)
    actual = native_candle("ABCL", feed.first)
    feed.store.record_candle(actual)
    later = QuarterHourMinuteFeed(**{**args, "session_id": "later-fixture",
        "start_reference": feed.first + STEP, "histories": {}})
    first, end = later.first, later.end
    history = later.scoring_history_source(feed.first).get("ABCL")
    assert history.frame.iloc[-1].close == actual["close"]
    assert (later.first, later.end) == (first, end) and len(fetches) == 2


@pytest.mark.parametrize("first_kind", ["forecast", "scoring"])
def test_first_consumers_serialize_one_canonical_receipt(tmp_path, monkeypatch, first_kind):
    feed, _, _, fetches, _ = feed_fixture(tmp_path, monkeypatch)
    entered, release, attempting = threading.Event(), threading.Event(), threading.Event()
    original = forward_fill.make_reference
    calls, results, errors = [], {}, []
    def make(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(forward_fill, "make_reference", make)
    def consume(kind, second=False):
        try:
            if second:
                assert entered.wait(3)
                attempting.set()
            source = feed.history_source if kind == "forecast" else feed.scoring_history_source
            try:
                results[kind] = source(feed.first).get("ABCL")
            except ContractError as exc:
                assert kind == "scoring" and str(exc) == "Exact target interval unavailable after acquisition"
                results[kind] = str(exc)
        except BaseException as exc:
            errors.append(exc)
    second_kind = "scoring" if first_kind == "forecast" else "forecast"
    workers = [threading.Thread(target=consume, args=(first_kind,)),
               threading.Thread(target=consume, args=(second_kind, True))]
    try:
        for worker in workers:
            worker.start()
        assert attempting.wait(3)
        assert len(calls) == 1
    finally:
        release.set()
        for worker in workers:
            worker.join(5)
    assert not any(worker.is_alive() for worker in workers) and not errors
    assert len(calls) == (1 if first_kind == "forecast" else 2)
    assert set(results) == {"forecast", "scoring"}
    assert forward_fill.verified_reference(results["forecast"].frame.iloc[-1].to_dict())
    assert len(feed.reference_fills) == 1 and len(events(feed, "minute_feed_reference_filled")) == 1
    assert not events(feed, "minute_feed_reference_fill_revised") and len(fetches) == 2


def test_default_factory_preserves_two_argument_override(tmp_path, monkeypatch):
    feed, _, _, _, _ = feed_fixture(tmp_path, monkeypatch)
    seen = []
    def history_at(symbol, reference):
        seen.append((symbol, reference))
        return "old protocol"
    monkeypatch.setattr(feed, "history_at", history_at)
    assert feed.history_source(feed.first).get("ABCL") == "old protocol"
    assert seen == [("ABCL", feed.first)]


@pytest.mark.parametrize("kind", ["future", "wrong_symbol", "wrong_interval", "invalid_proof", "synthetic"])
def test_incoming_native_validation_remains_qualified_and_exact(tmp_path, monkeypatch, kind):
    feed, _, clock, fetches, _ = feed_fixture(tmp_path, monkeypatch)
    saved = saved_forecast(feed)
    symbol = "DBX" if kind == "wrong_symbol" else "ABCL"
    target = feed.first + STEP if kind == "wrong_interval" else feed.first
    actual = qualified_candle(symbol, target, max(clock(), target + STEP + timedelta(seconds=8)))
    assert native_interval.completed_native_interval(actual)
    if kind == "invalid_proof":
        actual["close"] += 1  # Malformed proof rejection, not a valid binding fixture.
    elif kind == "synthetic":
        actual["synthetic"] = True
    frame = pd.DataFrame([actual])
    for field in ("opened_at", "available_at"):
        frame[field] = pd.to_datetime(frame[field], utc=True)
    now = [instant(actual["available_at"]) - timedelta(microseconds=1) if kind == "future" else
           max(clock(), instant(actual["available_at"]))]
    class Incoming:
        def bind_cycle(self, identity):
            assert identity == "score:" + saved["forecast_id"]
        def get(self, requested):
            assert requested == "ABCL"
            return SimpleNamespace(frame=frame)
    def after_poll(poll):
        if kind == "future" and poll == 1:
            assert not feed.store.revisions(saved["forecast_id"])
            now[0] = instant(actual["available_at"])
    score(feed, lambda: now[0], history_source=lambda _: Incoming(), after_poll=after_poll)
    revisions = feed.store.revisions(saved["forecast_id"])
    assert len(revisions) == (1 if kind == "future" else 0)
    if revisions:
        assert instant(revisions[0]["observed_at"]) == instant(actual["available_at"])
    assert not feed.reference_fills and len(fetches) == 2
    assert feed.store.forecast_by_id(saved["forecast_id"]) == saved
