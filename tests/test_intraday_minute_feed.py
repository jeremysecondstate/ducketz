"""Offline feed cadence and persistence independent of model consumers."""
from datetime import timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.intraday import data
from ml.intraday.minute_feed import QuarterHourMinuteFeed
from ml.intraday.observed_data import build_observed_dataset
from ml.intraday.prediction import ContractError, STEP, instant
from ml.intraday.store import ShadowStore


class Clock:
    def __init__(self, value):
        self.value = instant(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def setup(tmp_path, monkeypatch, *, now="2026-10-14T17:16:00Z", start=None, behavior=None, symbols=("ABCL", "DBX")):
    clock, calls = Clock(now), []
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    journal = data.AcquisitionJournal(tmp_path / "acquisition.sqlite3")
    provider = SimpleNamespace(dataset="EQUS.MINI")

    def fetch(provider, *, symbols, opened_at, attempt_id, clock, stopped=lambda: False):
        reference = instant(opened_at)
        assert reference + STEP <= clock()
        calls.append((reference, tuple(symbols), clock()))
        result = {}
        for symbol in symbols:
            status, rows, retry = "success", tuple(dict(opened_at=(reference + timedelta(minutes=n)).isoformat(),
                open=10+n*.01, high=11, low=9, close=10+n*.01, volume=100) for n in range(15)), 0
            if behavior:
                override = behavior(reference, symbol, len(calls))
                status = override.get("status", status)
                rows = override.get("rows", rows if status == "success" else ())
                retry = override.get("retry_after_seconds", retry)
            result[symbol] = data.IntervalResponse(attempt_id+":"+symbol, symbol,
                "databento:EQUS.MINI:ohlcv-1m", "EQUS.MINI", reference.isoformat(),
                (reference+STEP).isoformat(), clock().isoformat(), clock().isoformat(), status,
                rows=rows, coverage_open=reference.isoformat(), coverage_close=(reference+STEP).isoformat(),
                provider_no_trade_evidence="explicit exact covered native omission" if status == "success" else None,
                retry_after_seconds=retry).validate()
        return result

    monkeypatch.setattr(data, "fetch_databento_interval_attempts", fetch)
    arguments = dict(provider=provider, store=store, journal=journal, histories={},
        pc="Scout", symbols=symbols, session_id="manual-test", clock=clock, start_reference=start)
    return QuarterHourMinuteFeed(**arguments), arguments, clock, calls


def test_first_fresh_closed_window_has_all_symbols_and_fifteen_minutes(tmp_path, monkeypatch):
    feed, _, clock, calls = setup(tmp_path, monkeypatch)
    result = feed.step()
    assert result["status"] == "acquired"
    assert calls == [(instant("2026-10-14T17:00Z"), ("ABCL", "DBX"), clock())]
    for symbol in feed.symbols:
        assert len(feed._attempts(symbol, calls[0][0])[0].rows) == 15
        history = feed.history_source(calls[0][0]).get(symbol)
        assert len(history.frame) == 1
        candle = feed.store.observed_candles(symbol)[0]
        assert candle["open"] == 10 and candle["close"] == 10.14 and candle["volume"] == 1500
        assert not candle["synthetic"]


def test_two_quarters_advance_without_any_model_consumer(tmp_path, monkeypatch):
    feed, _, clock, calls = setup(tmp_path, monkeypatch)
    feed.step()
    clock.advance(15*60)
    feed.step()
    assert [r for r, _, _ in calls] == [instant("2026-10-14T17:00Z"), instant("2026-10-14T17:15Z")]
    assert len(feed.history_snapshot()["ABCL"].frame) == 2
    assert len(feed.store.observed_candles()) == 4


def test_model_rejection_does_not_hold_up_next_ingestion(tmp_path, monkeypatch):
    feed, _, clock, calls = setup(tmp_path, monkeypatch)
    feed.step()
    first = feed.history_source(calls[0][0]).get("ABCL")
    with pytest.raises(ContractError, match="warmup"):
        build_observed_dataset(first.frame, cutoff=clock(), reference_at=calls[0][0])
    clock.advance(900)
    feed.step()
    assert len(calls) == 2 and len(feed.history_snapshot()["ABCL"].frame) == 2


@pytest.mark.parametrize("reference,close", [
    ("2026-10-14T23:45Z", "2026-10-15T00:00Z"),
    ("2026-12-10T00:45Z", "2026-12-10T01:00Z"),
])
def test_pacific_final_window_pdt_and_pst_then_no_next_day_fetch(tmp_path, monkeypatch, reference, close):
    feed, _, clock, calls = setup(tmp_path, monkeypatch, now=close, start=reference)
    assert feed.end == instant(close)
    assert feed.step()["status"] == "acquired"
    assert feed.step()["status"] == "closed"
    assert len(calls) == 1 and calls[0][0] == instant(reference)
    clock.advance(36*3600)
    assert feed.step()["status"] == "closed" and len(calls) == 1


def test_final_provider_lag_finishes_after_five_then_stops(tmp_path, monkeypatch):
    feed, _, clock, calls = setup(tmp_path, monkeypatch, now="2026-10-15T00:00Z", start="2026-10-14T23:45Z",
        behavior=lambda r, s, n: {"status":"unavailable_coverage"} if n == 1 else {})
    assert feed.step()["status"] == "acquired"
    assert not feed.closed and not feed.store.observed_candles()
    assert feed.step()["status"] == "waiting" and len(calls) == 1
    clock.advance(8)
    assert feed.step()["status"] == "acquired"
    assert all(instant(c["available_at"]) > feed.end for c in feed.store.observed_candles())
    assert feed.step()["status"] == "closed" and len(calls) == 2


def test_final_confirmed_empty_window_saved_without_fabricating_prices(tmp_path, monkeypatch):
    feed, _, clock, calls = setup(tmp_path, monkeypatch, now="2026-10-15T00:00Z", start="2026-10-14T23:45Z",
        behavior=lambda *a: {"rows": ()})
    feed.step()
    assert not feed.outcomes and not feed.store.observed_candles()
    clock.advance(4)
    feed.step()
    assert feed.step()["status"] == "closed"
    assert len(calls) == 2 and not feed.store.observed_candles()
    assert all(v["status"] == "resolved_no_usable_15m" for v in feed.outcomes.values())
    with pytest.raises(ContractError, match="no usable 15m"):
        feed.history_source("2026-10-14T23:45Z").get("ABCL")


def test_unavailable_coverage_keeps_pending_without_fake_empty_candle(tmp_path, monkeypatch):
    feed, _, clock, calls = setup(tmp_path, monkeypatch,
        behavior=lambda *a: {"status":"unavailable_coverage"})
    for _ in range(3):
        feed.step()
        clock.advance(4)
    assert len(calls) == 3 and not feed.store.observed_candles()
    assert not feed.outcomes and not feed.closed
    with pytest.raises(data.AcquisitionPending):
        feed.history_source(calls[0][0]).get("ABCL")


def test_new_quarter_is_fair_while_previous_provider_response_remains_missing(tmp_path, monkeypatch):
    first = instant("2026-10-14T17:00Z")
    feed, _, clock, calls = setup(tmp_path, monkeypatch,
        behavior=lambda r, s, n: {"status":"unavailable_coverage"} if r == first else {})
    feed.step()
    clock.advance(900)
    feed.step()
    assert calls[-1][0] == first + STEP
    assert feed.history_source(first+STEP).get("ABCL")
    feed.step()
    assert calls[-1][0] == first


def test_provider_retry_after_blocks_other_quarters_and_survives_feed_recreation(tmp_path, monkeypatch):
    feed, args, clock, calls = setup(tmp_path, monkeypatch,
        behavior=lambda *a: {"status":"temporary_error", "retry_after_seconds":1200})
    feed.step()
    clock.advance(900)
    restored = QuarterHourMinuteFeed(**args)
    assert restored.step()["reason"] == "provider_retry_after" and len(calls) == 1
    clock.advance(301)
    restored.step()
    assert len(calls) == 2 and calls[-1][0] > calls[0][0]


def test_completed_windows_restore_without_refetch_or_model_dependency(tmp_path, monkeypatch):
    feed, args, _, calls = setup(tmp_path, monkeypatch)
    feed.step()
    args["histories"] = {}
    restored = QuarterHourMinuteFeed(**args)
    assert restored.step()["status"] == "waiting" and len(calls) == 1
    assert len(restored.history_source(calls[0][0]).get("ABCL").frame) == 1


def test_old_model_journal_attempt_is_not_proof_of_canonical_minute_persistence(tmp_path, monkeypatch):
    feed, args, clock, calls = setup(tmp_path, monkeypatch)
    response = data.IntervalResponse("old-model-attempt", "ABCL", feed.source_id, feed.dataset,
        "2026-10-14T17:00Z", "2026-10-14T17:15Z", clock().isoformat(), clock().isoformat(), "success",
        rows=tuple(dict(opened_at=(instant("2026-10-14T17:00Z")+timedelta(minutes=i)).isoformat(),
            open=10,high=11,low=9,close=10,volume=100) for i in range(15)),
        coverage_open="2026-10-14T17:00Z",coverage_close="2026-10-14T17:15Z")
    args["journal"].save(response)
    feed.step()
    assert len(calls) == 1 and feed.outcomes
    assert all("old-model-attempt" not in out["attempt_ids"] for out in feed.outcomes.values())


def test_manual_stop_never_fetches_or_changes_control_records(tmp_path, monkeypatch):
    feed, _, _, calls = setup(tmp_path, monkeypatch)
    result = feed.run(stopped=lambda: True, pause=lambda s: pytest.fail("no wait"))
    assert result["status"] == "manual_stop" and not calls
    assert not (tmp_path / "manual-session.json").exists()


def test_outside_feed_scope_or_latched_day_is_not_fetched(tmp_path, monkeypatch):
    feed, _, _, calls = setup(tmp_path, monkeypatch)
    with pytest.raises(ContractError, match="outside this independent"):
        feed.history_source("2026-10-14T17:00Z").get("OTHER")
    with pytest.raises(ContractError, match="latched eligible day"):
        feed.history_source("2026-10-15T17:00Z").get("ABCL")
    assert not calls


def test_final_offset_waits_until_final_window_has_been_queued(tmp_path, monkeypatch):
    feed, args, clock, calls = setup(tmp_path, monkeypatch,
        now="2026-10-15T00:00Z", start="2026-10-14T23:45Z")
    args["session_id"] = "offset-test"
    args["publication_offset_seconds"] = 30
    closed = []
    args["on_closed"] = closed.append
    feed = QuarterHourMinuteFeed(**args)
    assert feed.step()["status"] == "waiting" and not feed.closed and not calls
    clock.advance(30)
    assert feed.step()["status"] == "acquired" and len(calls) == 1
    assert feed.step()["status"] == "closed"
    assert len(closed) == 1 and closed[0]["all_queued_native_windows_saved"]


def test_late_manual_restart_recovers_only_same_day_final_window(tmp_path, monkeypatch):
    feed, _, _, calls = setup(tmp_path, monkeypatch, now="2026-10-15T01:25Z")
    assert feed.first == instant("2026-10-14T23:45Z")
    feed.step()
    assert feed.step()["status"] == "closed"
    assert len(calls) == 1 and calls[0][0] == instant("2026-10-14T23:45Z")


def test_prior_session_scoring_uses_only_actual_saved_candles_without_fetch(tmp_path, monkeypatch):
    feed, args, _, calls = setup(tmp_path, monkeypatch, start="2026-10-14T16:45Z")
    feed.step()
    feed.step()
    args["session_id"] = "later-manual"
    args["start_reference"] = "2026-10-14T17:00Z"
    args["histories"] = {}
    later = QuarterHourMinuteFeed(**args)
    assert later.history_source("2026-10-14T16:45Z").get("ABCL")
    assert len(calls) == 2
    with pytest.raises(ContractError, match="latched eligible day"):
        later.history_source("2026-10-14T16:30Z").get("ABCL")


def test_unverified_legacy_synthetic_history_is_excluded_without_mutating_original(tmp_path, monkeypatch):
    feed, args, _, _ = setup(tmp_path, monkeypatch)
    original = pd.DataFrame([dict(symbol="ABCL",opened_at=pd.Timestamp("2026-10-14T16:45Z"),
        available_at=pd.Timestamp("2026-10-14T17:00Z"),availability_kind="actual",
        source_id=feed.source_id,source_dataset=feed.dataset,synthetic=True,
        open=10,high=10,low=10,close=10,volume=0,session_id="2026-10-14")])
    args["histories"] = {"ABCL": data.History(original, {"source_dataset":feed.dataset})}
    args["session_id"] = "invalid-history-test"
    fresh = QuarterHourMinuteFeed(**args)
    assert fresh.history_snapshot()["ABCL"].frame.empty
    assert len(original) == 1


def continuity_fixture(tmp_path, monkeypatch):
    predecessor, current = instant("2026-10-14T17:00Z"), instant("2026-10-14T17:15Z")
    predecessor_attempts = []
    def behavior(reference, symbol, number):
        if reference == predecessor:
            predecessor_attempts.append(number)
            return {"status":"unavailable_coverage"} if len(predecessor_attempts) == 1 else {}
        return {"rows": tuple(dict(opened_at=(reference+timedelta(minutes=n)).isoformat(),
            open=10+n*.01, high=11, low=9, close=10+n*.01, volume=100) for n in range(1, 15))}
    feed, args, clock, calls = setup(tmp_path, monkeypatch, now="2026-10-14T17:31Z",
        start=predecessor, behavior=behavior, symbols=("ABCL",))
    feed.step()  # Current quarter, with an unconfirmed leading omission.
    feed.step()  # Predecessor temporarily unavailable.
    clock.advance(4)
    feed.step()  # Current omission now confirmed while predecessor is pending.
    return feed, clock, calls, predecessor, current


def test_confirmed_leading_omission_stays_pending_until_own_predecessor_completes(tmp_path, monkeypatch):
    feed, clock, calls, predecessor, current = continuity_fixture(tmp_path, monkeypatch)
    assert (current,"ABCL") not in feed.outcomes
    assert "ABCL" in feed.pending[current] and "ABCL" in feed.pending[predecessor]
    with pytest.raises(data.AcquisitionPending):
        feed.history_source(current).get("ABCL")
    feed.step()  # Save predecessor at the same actual receipt time.
    assert feed.outcomes[predecessor,"ABCL"]["status"] == "observed"
    feed.step()  # Original confirmed current attempts can now resolve causally.
    assert feed.outcomes[current,"ABCL"]["status"] == "mixed_synthetic"
    assert sum(r == current for r,_,_ in calls) == 2
    assert len(feed.store.observed_candles("ABCL")) == 2


def test_late_predecessor_receipt_requires_fresh_current_attempt_instead_of_invalid_completion(tmp_path, monkeypatch):
    feed, clock, calls, predecessor, current = continuity_fixture(tmp_path, monkeypatch)
    old_response_end = instant(feed._attempts("ABCL",current)[-1].request_ended_at)
    clock.advance(4)
    feed.step()  # Predecessor finally arrives later than the current receipt.
    previous = feed._previous("ABCL",current)
    assert instant(previous["available_at"]) > old_response_end
    assert (current,"ABCL") not in feed.outcomes
    feed.step()  # Fresh current response, now including the real prior receipt.
    assert feed.outcomes[current,"ABCL"]["status"] == "mixed_synthetic"
    assert sum(r == current for r,_,_ in calls) == 3
    candle = feed.store.observed_candles("ABCL")[-1]
    assert instant(candle["available_at"]) >= instant(previous["available_at"])
    assert instant(feed._attempts("ABCL",current)[-1].request_ended_at) > old_response_end
