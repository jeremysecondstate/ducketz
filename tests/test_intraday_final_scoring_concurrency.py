"""Offline regressions for concurrent final scoring and bounded forecast memory."""
from datetime import timedelta
import json
import weakref

import pytest

from ml.intraday.prediction import ContractError
from ml.intraday import live_session
from ml.intraday.store import ShadowStore
from tests.test_intraday_final_scoring import CLOSE, SID, final_candle, save_forecast


def final_score(store, **changes):
    options = dict(session_id=SID, actor="Atlas", symbols=("AAPL",),
                   as_of=CLOSE + timedelta(seconds=5))
    options.update(changes)
    return live_session.score_saved_session_outcomes(store, **options)


def saved(tmp_path):
    store = ShadowStore(tmp_path / "final-scoring.sqlite3")
    original = save_forecast(store)
    candle = final_candle()
    store.record_candle(candle)
    return store, original, candle


def test_ordinary_observe_still_rejects_backdating_even_for_same_candle(tmp_path):
    store, original, candle = saved(tmp_path)
    latest = store.observe(original, as_of=CLOSE + timedelta(seconds=10), candle=candle)
    with pytest.raises(ContractError, match="backdated"):
        store.observe(original, as_of=CLOSE + timedelta(seconds=5), candle=candle)
    assert store.revisions(original["forecast_id"]) == [latest]


@pytest.mark.parametrize("corrected", [False, True])
def test_opt_in_observation_atomically_skips_newer_latest_without_returning_old_revision(tmp_path, corrected):
    store, original, candle = saved(tmp_path)
    early = store.observe(original, as_of=CLOSE + timedelta(seconds=5), candle=candle)
    later_candle = dict(candle, close=99., available_at=(CLOSE + timedelta(seconds=8)).isoformat()) if corrected else candle
    later = store.observe(original, as_of=CLOSE + timedelta(seconds=10), candle=later_candle)
    # For the same candle observe is idempotent, so establish a newer latest
    # through a distinct validated pending/missing predecessor in the final
    # scorer test below. This direct case is principally the corrected revision.
    before = store.revisions(original["forecast_id"])
    result = store.observe(original, as_of=CLOSE + timedelta(seconds=4), candle=candle, skip_newer=True)
    assert result is None
    assert store.revisions(original["forecast_id"]) == before
    assert before[-1] == later
    if corrected:
        assert later != early and later["target_not_down"] is False


@pytest.mark.parametrize("corrected", [False, True])
def test_final_scorer_retains_concurrent_newer_observation_without_backdating(tmp_path, monkeypatch, corrected):
    store, original, candle = saved(tmp_path)
    original_forecast = store.forecast
    committed = []
    def forecast_then_concurrent_score(cycle, symbol):
        value = original_forecast(cycle, symbol)
        if value is not None and not committed:
            later_candle = dict(candle, close=99., available_at=(CLOSE + timedelta(seconds=8)).isoformat()) if corrected else candle
            committed.append(store.observe(value, as_of=CLOSE + timedelta(seconds=10), candle=later_candle))
        return value
    monkeypatch.setattr(store, "forecast", forecast_then_concurrent_score)
    monkeypatch.setattr(live_session, "LiveHistories", lambda **kwargs: pytest.fail("Final scoring must use persisted inputs"))
    result = final_score(store)
    assert result["scored"] == result["synthetic"] == result["unavailable"] == 0
    assert result["newer_observation_retained"] == 1
    assert store.revisions(original["forecast_id"]) == committed
    assert committed[0]["observed_at"] == (CLOSE + timedelta(seconds=10)).isoformat()
    assert committed[0]["target_not_down"] is (not corrected)


def test_skip_newer_still_validates_immutable_original(tmp_path):
    store, original, candle = saved(tmp_path)
    store.observe(original, as_of=CLOSE + timedelta(seconds=10), candle=candle)
    with store.connect() as db:
        raw = db.execute("SELECT payload FROM forecasts WHERE id=?", (original["forecast_id"],)).fetchone()[0]
        changed = json.loads(raw)
        changed["reference_open"] = 101.
        db.execute("UPDATE forecasts SET payload=? WHERE id=?", (json.dumps(changed), original["forecast_id"]))
    with pytest.raises(ContractError):
        store.observe(original, as_of=CLOSE + timedelta(seconds=5), candle=candle, skip_newer=True)


def test_skip_newer_still_validates_newer_outcome_bytes(tmp_path):
    store, original, candle = saved(tmp_path)
    latest = store.observe(original, as_of=CLOSE + timedelta(seconds=10), candle=candle)
    with store.connect() as db:
        raw = db.execute("SELECT payload FROM outcomes WHERE id=?", (latest["revision_id"],)).fetchone()[0]
        changed = json.loads(raw)
        changed["target_not_down"] = False
        db.execute("UPDATE outcomes SET payload=? WHERE id=?", (json.dumps(changed), latest["revision_id"]))
    with pytest.raises(ContractError, match="outcome evidence hash"):
        store.observe(original, as_of=CLOSE + timedelta(seconds=5), candle=candle, skip_newer=True)


def test_manual_stop_prevents_even_one_expensive_original_read(tmp_path, monkeypatch):
    store, _, _ = saved(tmp_path)
    monkeypatch.setattr(store, "forecast", lambda *args: pytest.fail("Stopped final pass decoded an original"))
    result = final_score(store, stopped=lambda: True)
    assert result["scored"] == result["synthetic"] == result["unavailable"] == 0


def test_final_pass_retains_at_most_one_prior_full_original_between_reads(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "streaming.sqlite3")
    for index in range(3):
        save_forecast(store, cycle=f"own-cycle-{index}")
    store.record_candle(final_candle())
    original_forecast = store.forecast
    refs, live_counts = [], []
    class TrackedForecast(dict):
        pass
    def tracking_read(cycle, symbol):
        live_counts.append(sum(ref() is not None for ref in refs))
        assert live_counts[-1] <= 1, "Final pass retained multiple decoded model-bearing originals"
        original = original_forecast(cycle, symbol)
        if original is None:
            return None
        tracked = TrackedForecast(original)
        refs.append(weakref.ref(tracked))
        return tracked
    monkeypatch.setattr(store, "forecast", tracking_read)
    result = final_score(store)
    assert result["scored"] == 3
    assert len(refs) == 3 and max(live_counts) <= 1
    assert all(ref() is None for ref in refs)

def test_final_pass_handles_writer_committing_during_observe_validation(tmp_path, monkeypatch):
    store, original, candle = saved(tmp_path)
    record = store.record_candle
    other = ShadowStore(store.path)
    committed = []
    def commit_during_validation(value):
        key = record(value)
        if not committed:
            committed.append(other.observe(original, as_of=CLOSE + timedelta(seconds=10), candle=candle))
        return key
    monkeypatch.setattr(store, "record_candle", commit_during_validation)
    result = final_score(store)
    assert result == {"session_id": SID, "scored": 0, "synthetic": 0,
                      "unavailable": 0, "newer_observation_retained": 1}
    assert store.revisions(original["forecast_id"]) == committed

@pytest.mark.parametrize("issue_offset_seconds, expected_scored", [(-1, 1), (0, 1), (1, 0)])
def test_final_scoring_observes_only_forecasts_issued_by_its_cutoff(
        tmp_path, issue_offset_seconds, expected_scored):
    """A late-produced forecast cannot acquire an outcome before it existed."""
    from ml.intraday.prediction import instant
    from tests.test_intraday_store import forecast
    close = instant("2026-10-15T00:00:00Z")
    as_of = close + timedelta(seconds=5)
    reference = close - timedelta(minutes=45)
    session = "manual-final-issue-cutoff-fixture"
    source = "databento:EQUS.MINI:ohlcv-1m"
    store = ShadowStore(tmp_path / "final-issue-cutoff.sqlite3")
    store.begin_cycle("own-cycle", {"pc": "Atlas", "mode": "live", "experiment": session,
        "symbols": ["AAPL"], "reference_at": reference.isoformat(),
        "configuration_hash": "fixture", "started_at": reference.isoformat()})
    original = store.save_forecast(forecast(pc="Atlas", symbol="AAPL", cycle_id="own-cycle", mode="live",
        reference_at=reference.isoformat(), data_through=(reference + timedelta(minutes=15)).isoformat(),
        issued_at=(as_of + timedelta(seconds=issue_offset_seconds)).isoformat(),
        information_available_at=(reference + timedelta(minutes=15, seconds=10)).isoformat(),
        target_open=(close - timedelta(minutes=15)).isoformat(), target_close=close.isoformat(),
        source_id=source, source_dataset="EQUS.MINI"))
    store.record_candle({"symbol": "AAPL", "opened_at": (close - timedelta(minutes=15)).isoformat(),
        "available_at": (close + timedelta(seconds=4)).isoformat(), "source_id": source,
        "source_dataset": "EQUS.MINI", "availability_kind": "actual", "synthetic": False,
        "no_trade": False, "open": 100., "high": 101., "low": 99., "close": 100.5,
        "volume": 100., "evidence_json": "{}", "interval_seconds": 900})
    assert store.revisions(original["forecast_id"]) == []

    result = live_session.score_saved_session_outcomes(store, session_id=session, actor="Atlas",
        symbols=("AAPL",), as_of=as_of)

    assert result == {"session_id": session, "scored": expected_scored, "synthetic": 0, "unavailable": 0}
    revisions = store.revisions(original["forecast_id"])
    assert len(revisions) == expected_scored
    assert all(instant(value["observed_at"]) >= instant(original["issued_at"]) for value in revisions)
    assert store.forecast("own-cycle", "AAPL") == original
