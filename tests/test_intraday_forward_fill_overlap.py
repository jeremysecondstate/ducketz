"""Cured windows get one durable next-quarter overlap without replaying trades."""
from datetime import timedelta
from copy import deepcopy
import json
import threading

import pandas as pd
import pytest

from ml.intraday import data
from ml.intraday.forward_fill import make_reference, policy_contract, verified_reference
from ml.intraday.minute_feed import QuarterHourMinuteFeed
from ml.intraday.prediction import ContractError, STEP, digest, instant
from tests.test_intraday_minute_feed import setup
from tests.test_intraday_native_feed import complete, native_candle, sparse_rows
from tests.test_intraday_forward_fill_runtime import fixture_history
from tests.test_intraday_forward_fill import empty_pair


def feed_fixture(tmp_path, monkeypatch, *, recovery="empty", reference="2026-10-14T16:00Z"):
    first = instant(reference)
    counts = {}
    def behavior(at, symbol, _):
        counts[at] = counts.get(at, 0) + 1
        if at != first:
            return {}
        if counts[at] <= 2 or recovery == "empty":
            return {"rows": ()}
        if recovery == "error":
            raise OSError("fixture temporarily unavailable")
        if recovery == "permanent":
            return {"status": "permanent_error"}
        return {"rows": sparse_rows(at)}
    _, args, clock, calls = setup(tmp_path, monkeypatch, symbols=("ABCL",),
        now=(first + STEP + timedelta(seconds=30)).isoformat(), start=reference, behavior=behavior)
    args.update(session_id="filled-overlap", native_interval_symbols=("ABCL",),
        reference_fill_policy=policy_contract(), histories={"ABCL": fixture_history(reference=reference)})
    feed = QuarterHourMinuteFeed(**args)
    complete(feed, clock)
    return feed, args, clock, calls, counts


def events(feed, kind):
    with feed.store.connect() as db:
        return [json.loads(raw) for (raw,) in db.execute("SELECT payload FROM events WHERE kind=? ORDER BY id", (kind,))]


def settle(feed, clock, reference, *, limit=12):
    for _ in range(limit):
        feed.step()
        state = feed.reference_fills[reference, "ABCL"]
        if state["completed"] is not None:
            return state["completed"]
        clock.advance(4)
    pytest.fail("Bounded fixture overlap did not complete")


def test_every_cured_window_has_one_hash_receipt_even_across_session_recreation(tmp_path, monkeypatch):
    feed, args, clock, calls, _ = feed_fixture(tmp_path, monkeypatch)
    for _ in range(3):
        assert verified_reference(feed.history_at("ABCL", feed.first).frame.iloc[-1].to_dict())
    recorded = events(feed, "minute_feed_reference_filled")
    assert len(recorded) == 1
    row = recorded[0]
    for key in ("reference_sha256", "evidence_sha256", "anchor_sha256", "anchor_source_evidence_sha256"):
        assert len(row[key]) == 64
    assert row["reference_at"] == feed.first.isoformat()
    assert row["anchor_age_seconds"] > 0
    assert row["source_id"] == feed.source_id and row["original_source_and_forecasts_unchanged"]
    assert verified_reference(row["reference"]) and digest(row["reference"]) == row["reference_sha256"]
    restored = QuarterHourMinuteFeed(**args)
    restored.history_at("ABCL", feed.first)
    assert events(restored, "minute_feed_reference_filled") == recorded
    assert len(calls) == 2 and not restored.store.observed_candles("ABCL")


def test_next_quarter_has_priority_and_empty_recheck_requires_fresh_pair(tmp_path, monkeypatch):
    feed, _, clock, calls, counts = feed_fixture(tmp_path, monkeypatch)
    feed.history_at("ABCL", feed.first)
    original_attempts = {a.attempt_id for a in feed._attempts("ABCL", feed.first)}
    feed.step()
    assert len(calls) == 2
    clock.advance(900)
    feed.step()
    assert calls[-1][0] == feed.first + STEP
    result = settle(feed, clock, feed.first)
    assert result["reason"] == "source_empty_interval" and not result["genuine_rows_arrived"]
    assert counts[feed.first] == 4 and len(result["attempt_ids"]) == 2
    assert not set(result["attempt_ids"]) & original_attempts
    assert len(events(feed, "minute_feed_reference_recheck_started")) == 1
    assert len(events(feed, "minute_feed_reference_recheck_completed")) == 1
    old_calls = len(calls)
    for _ in range(3):
        feed.step()
        feed.history_at("ABCL", feed.first)
        clock.advance(4)
    assert len(calls) == old_calls
    assert counts[feed.first] == 4


def test_arriving_real_data_replaces_projection_with_original_empty_outcome_intact(tmp_path, monkeypatch):
    feed, _, clock, _, counts = feed_fixture(tmp_path, monkeypatch, recovery="observed")
    before = feed.history_at("ABCL", feed.first)
    original_outcome = dict(feed.outcomes[feed.first, "ABCL"])
    clock.advance(900)
    result = settle(feed, clock, feed.first)
    assert result["status"] == "observed" and result["genuine_rows_arrived"]
    assert counts[feed.first] == 4 and result["observed_constituents"] == 3
    after = feed.history_at("ABCL", feed.first)
    exact = after.frame.loc[after.frame.opened_at.eq(pd.Timestamp(feed.first))].iloc[0]
    assert not exact.synthetic and exact.close == 10.13
    assert before.frame.iloc[-1].synthetic  # Previously handed-out snapshot did not change.
    assert feed.outcomes[feed.first, "ABCL"] == original_outcome
    assert len([c for c in feed.store.observed_candles("ABCL") if instant(c["opened_at"]) == feed.first]) == 1


def test_restored_real_candle_wins_even_when_saved_outcome_is_still_empty(tmp_path, monkeypatch):
    feed, args, _, _, _ = feed_fixture(tmp_path, monkeypatch)
    assert feed.outcomes[feed.first, "ABCL"]["reason"] == "source_empty_interval"
    actual = native_candle("ABCL", feed.first)
    feed.store.record_candle(actual)
    restored = QuarterHourMinuteFeed(**args)
    exact = restored.history_at("ABCL", feed.first).frame.iloc[-1]
    assert not exact.synthetic and exact.close == actual["close"]
    assert restored.outcomes[feed.first, "ABCL"]["reason"] == "source_empty_interval"
    assert not events(feed, "minute_feed_reference_filled")


def test_interrupted_recheck_restores_its_first_fresh_attempt(tmp_path, monkeypatch):
    feed, args, clock, _, counts = feed_fixture(tmp_path, monkeypatch)
    feed.history_at("ABCL", feed.first)
    clock.advance(900)
    feed.step()  # New current quarter.
    feed.step()  # First fresh overlap only; the old pair cannot finish it.
    assert counts[feed.first] == 3
    assert feed.reference_fills[feed.first, "ABCL"]["completed"] is None
    restored = QuarterHourMinuteFeed(**args)
    assert len(restored.reference_fills[feed.first, "ABCL"]["attempt_ids"]) == 1
    clock.advance(4)
    result = settle(restored, clock, feed.first)
    assert result["reason"] == "source_empty_interval" and counts[feed.first] == 4
    assert len(events(restored, "minute_feed_reference_recheck_started")) == 1


def test_unfinished_final_window_rechecks_on_next_manual_session_only(tmp_path, monkeypatch):
    feed, args, clock, calls, counts = feed_fixture(tmp_path, monkeypatch, reference="2026-10-14T23:45Z")
    feed.history_at("ABCL", feed.first)
    assert feed.step()["status"] == "closed"
    assert events(feed, "minute_feed_closed")[-1]["pending_reference_rechecks"] == 1
    # No session or process is started by feed code. This construction represents
    # the operator's next manual session with the same private store/journal.
    next_ref = instant("2026-10-15T16:00Z")
    clock.value = next_ref + STEP + timedelta(seconds=30)
    later = QuarterHourMinuteFeed(**{**args, "session_id": "next-manual-session", "start_reference": next_ref})
    assert feed.first in [ref for ref, _ in later.reference_fills]
    later.step()
    assert calls[-1][0] == next_ref
    result = settle(later, clock, feed.first)
    assert result["reason"] == "source_empty_interval" and counts[feed.first] == 4
    assert len(events(later, "minute_feed_reference_filled")) == 1


def test_error_recheck_is_logged_and_remains_pending_without_blocking_new_quarters(tmp_path, monkeypatch):
    feed, args, clock, calls, _ = feed_fixture(tmp_path, monkeypatch, recovery="error")
    feed.history_at("ABCL", feed.first)
    clock.advance(900)
    feed.step()
    assert feed.step()["reason"] == "reference_recheck_request_error"
    assert events(feed, "minute_feed_reference_recheck_request_error")[-1]["error_type"] == "OSError"
    assert feed.reference_fills[feed.first, "ABCL"]["completed"] is None
    restored = QuarterHourMinuteFeed(**args)
    assert restored.reference_fills[feed.first, "ABCL"]["retry_at"] > clock()
    clock.advance(900)
    restored.step()
    assert calls[-1][0] == feed.first + 2 * STEP


def test_permanent_provider_error_does_not_become_a_success_or_repeat_loop(tmp_path, monkeypatch):
    feed, _, clock, _, counts = feed_fixture(tmp_path, monkeypatch, recovery="permanent")
    feed.history_at("ABCL", feed.first)
    clock.advance(900)
    result = settle(feed, clock, feed.first)
    assert result["status"] == "failed" and result["reason"] == "permanent_error"
    assert result["genuine_rows_arrived"] is False and counts[feed.first] == 3
    for _ in range(3):
        feed.step()
        clock.advance(4)
    assert counts[feed.first] == 3


def test_every_changed_projection_has_full_immutable_revision_even_when_anchor_reverts(tmp_path, monkeypatch):
    feed, args, _, _, _ = feed_fixture(tmp_path, monkeypatch)
    original = feed.histories["ABCL"]
    first = feed.history_at("ABCL", feed.first).frame.iloc[-1].to_dict()
    before = events(feed, "minute_feed_reference_filled")[0]
    trimmed = original.frame.iloc[:-1].copy()
    feed.histories["ABCL"] = data.History(trimmed, original.manifest)
    second = feed.history_at("ABCL", feed.first).frame.iloc[-1].to_dict()
    assert second["anchor_opened_at"] != first["anchor_opened_at"]
    changed = events(feed, "minute_feed_reference_fill_revised")
    assert len(changed) == 1 and changed[0]["projection_revision"] == 1
    assert changed[0]["prior_reference_sha256"] == before["reference_sha256"]
    assert changed[0]["reference_sha256"] == digest(changed[0]["reference"])
    assert verified_reference(changed[0]["reference"])
    assert changed[0]["reference"]["anchor_opened_at"] == second["anchor_opened_at"]
    feed.history_at("ABCL", feed.first)
    assert len(events(feed, "minute_feed_reference_fill_revised")) == 1
    feed.histories["ABCL"] = original
    third = feed.history_at("ABCL", feed.first).frame.iloc[-1].to_dict()
    reverted = events(feed, "minute_feed_reference_fill_revised")[-1]
    assert reverted["projection_revision"] == 2
    assert reverted["reference_sha256"] == before["reference_sha256"]
    assert reverted["prior_reference_sha256"] == changed[0]["reference_sha256"]
    assert third["anchor_opened_at"] == first["anchor_opened_at"]
    assert events(feed, "minute_feed_reference_filled") == [before]
    restored = QuarterHourMinuteFeed(**args)
    restored.history_at("ABCL", feed.first)
    assert restored.reference_fills[feed.first, "ABCL"]["latest_projection"] == reverted
    assert len(restored.reference_fills) == 1 and len(events(feed, "minute_feed_reference_fill_revised")) == 2


@pytest.mark.parametrize("corrupt", ["reference", "hash", "anchor", "source", "interval"])
def test_restoration_rejects_receipt_that_does_not_match_its_full_projection(tmp_path, monkeypatch, corrupt):
    feed, args, _, _, _ = feed_fixture(tmp_path, monkeypatch)
    feed.history_at("ABCL", feed.first)
    bad = deepcopy(events(feed, "minute_feed_reference_filled")[0])
    if corrupt == "reference":
        bad["reference"]["close"] += 1
    elif corrupt == "hash":
        bad["reference_sha256"] = "a" * 64
    elif corrupt == "source":
        bad["source_id"] = "unrelated:source"
    elif corrupt == "interval":
        bad["interval_close"] = bad["reference_at"]
    else:
        bad["anchor_age_seconds"] += 1
    feed.store.event(feed.identity, "minute_feed_reference_filled", bad, symbol="ABCL")
    with pytest.raises(ContractError, match="full source proof"):
        QuarterHourMinuteFeed(**args)


def test_feed_closing_and_concurrent_model_fill_never_iterate_a_mutating_dictionary(tmp_path, monkeypatch):
    feed, _, _, _, _ = feed_fixture(tmp_path, monkeypatch, reference="2026-10-14T23:45Z")
    feed.history_at("ABCL", feed.first)
    earlier = feed.first - STEP
    extra = make_reference(feed.histories["ABCL"].frame, empty_pair(earlier, symbol="ABCL"),
                           session=feed._session(earlier))
    entered, attempting, inserted = threading.Event(), threading.Event(), threading.Event()
    class SlowValues(dict):
        def values(self):
            for index, state in enumerate(super().values()):
                if index == 0 and not entered.is_set():
                    entered.set()
                    assert attempting.wait(2)
                    # Without the feed lock the model thread inserts here and
                    # invalidates this live iterator. With the lock it waits.
                    inserted.wait(.05)
                yield state
    feed.reference_fills = SlowValues(feed.reference_fills)
    errors = []
    def model_request():
        try:
            assert entered.wait(2)
            attempting.set()
            with feed._lock:
                feed._record_reference_fill("ABCL", earlier, extra)
            inserted.set()
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=model_request)
    worker.start()
    try:
        result = feed.step()
    finally:
        worker.join(timeout=3)
    assert not worker.is_alive() and not errors and inserted.is_set()
    assert result["status"] == "closed"
    assert len(events(feed, "minute_feed_reference_filled")) == 2


def test_friday_fill_outside_monday_live_range_retains_historical_work_without_polling_or_holding_close(tmp_path, monkeypatch):
    feed, args, clock, calls, _ = feed_fixture(tmp_path, monkeypatch, reference="2026-10-16T23:45Z")
    feed.history_at("ABCL", feed.first)
    assert feed.step()["status"] == "closed"
    original_fetch = data.fetch_databento_interval_attempts
    expired_requests = []
    def rolling_range(provider, *, symbols, opened_at, attempt_id, clock, stopped=lambda: False):
        reference = instant(opened_at)
        if reference != feed.first:
            return original_fetch(provider, symbols=symbols, opened_at=opened_at,
                                  attempt_id=attempt_id, clock=clock, stopped=stopped)
        expired_requests.append(reference)
        calls.append((reference, tuple(symbols), clock()))
        return {symbol: data.IntervalResponse(attempt_id + ":" + symbol, symbol, feed.source_id, feed.dataset,
            reference.isoformat(), (reference + STEP).isoformat(), clock().isoformat(), clock().isoformat(),
            "unavailable_coverage", coverage_open=(clock() - timedelta(hours=48)).isoformat(),
            coverage_close=clock().isoformat(), error="requested_interval_outside_advertised_schema_range").validate()
            for symbol in symbols}
    monkeypatch.setattr(data, "fetch_databento_interval_attempts", rolling_range)
    monday = instant("2026-10-19T23:45Z")
    clock.value = monday + STEP + timedelta(seconds=30)
    monday_args = {**args, "session_id": "monday-manual", "start_reference": monday}
    later = QuarterHourMinuteFeed(**monday_args)
    later.step()
    assert calls[-1][0] == monday  # Current native work still goes first.
    result = settle(later, clock, feed.first)
    assert result["status"] == "historical_recovery_required" and result["recovery_state"] == "unrecovered"
    assert result["genuine_rows_arrived"] is False and len(result["attempt_ids"]) == 1
    assert instant(result["schema_start"]) > feed.first
    assert result["remaining_operation"]["interval_open"] == feed.first.isoformat()
    assert "native_recovery.merge_recovered_history" in result["next_action"]
    assert later.step()["status"] == "closed"
    closed = events(later, "minute_feed_closed")[-1]
    assert closed["pending_reference_rechecks"] == 0 and closed["historical_recovery_required"] == 1
    restored = QuarterHourMinuteFeed(**monday_args)
    assert restored.step()["status"] == "closed"
    assert restored.reference_fills[feed.first, "ABCL"]["completed"]["recovery_state"] == "unrecovered"
    assert expired_requests == [feed.first]


def test_temporarily_late_schema_upper_bound_remains_retryable(tmp_path, monkeypatch):
    feed, _, clock, _, _ = feed_fixture(tmp_path, monkeypatch)
    feed.history_at("ABCL", feed.first)
    clock.advance(900)
    feed.step()
    def late_upper(provider, *, symbols, opened_at, attempt_id, clock, stopped=lambda: False):
        reference = instant(opened_at)
        return {symbol: data.IntervalResponse(attempt_id + ":" + symbol, symbol, feed.source_id, feed.dataset,
            reference.isoformat(), (reference + STEP).isoformat(), clock().isoformat(), clock().isoformat(),
            "unavailable_coverage", coverage_open=(reference - timedelta(hours=48)).isoformat(),
            coverage_close=(reference + timedelta(minutes=14)).isoformat(),
            error="requested_interval_outside_advertised_schema_range").validate() for symbol in symbols}
    monkeypatch.setattr(data, "fetch_databento_interval_attempts", late_upper)
    assert feed.step()["kind"] == "reference_fill_overlap"
    assert feed.reference_fills[feed.first, "ABCL"]["completed"] is None
    assert not events(feed, "minute_feed_reference_recheck_completed")
    assert len(feed.reference_fills[feed.first, "ABCL"]["attempt_ids"]) == 1
