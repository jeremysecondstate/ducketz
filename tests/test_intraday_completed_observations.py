"""Full-quarter evidence is required before restoration or outcome selection."""
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from ml.intraday import data, live_session
from ml.intraday.minute_feed import QuarterHourMinuteFeed
from ml.intraday.prediction import ContractError, STEP, digest, instant
from ml.intraday.runtime import LiveHistories, ShadowRunner
from ml.intraday.store import ShadowStore, encoded
from tests.test_intraday_store import forecast


OPEN = instant("2026-10-08T17:30:00Z")
NOW = instant("2026-10-08T17:50:00Z")
SOURCE = "databento:EQUS.MINI:ohlcv-1m"


def observation(**changes):
    value = dict(symbol="AAPL", opened_at=OPEN.isoformat(),
        available_at=(OPEN + STEP + timedelta(seconds=4)).isoformat(),
        availability_kind="actual", interval_seconds=900, open=100, high=110,
        low=90, close=101, volume=100, synthetic=False, no_trade=False,
        source_id=SOURCE, source_dataset="EQUS.MINI", evidence_json="{}")
    value.update(changes)
    return value


def constituents():
    return [dict(opened_at=(OPEN + timedelta(minutes=index)).isoformat()) for index in range(15)]


@pytest.mark.parametrize("proof", [
    {"interval_seconds": 900},
    {"interval_seconds": np.int64(900)},
    {"observed_constituents": 15, "synthetic_constituents": 0},
    {"observed_constituents": np.float64(14), "synthetic_constituents": np.float64(1)},
    {"evidence_json": {"constituents": constituents()}},
    {"evidence_json": json.dumps({"constituents": constituents()})},
])
def test_completed_observation_accepts_positive_quarter_evidence(proof):
    row = observation()
    del row["interval_seconds"]
    row.update(proof)
    assert data.completed_15m_observation(row)


@pytest.mark.parametrize("changes", [
    {"interval_seconds": None},
    {"interval_seconds": np.nan, "observed_constituents": None, "synthetic_constituents": np.nan},
    {"interval_seconds": 60},
    {"interval_seconds": "900"},
    {"interval_seconds": 60, "observed_constituents": 15, "synthetic_constituents": 0},
    {"observed_constituents": 14, "synthetic_constituents": 0},
    {"observed_constituents": 15},
    {"observed_constituents": 15, "synthetic_constituents": None},
    {"observed_constituents": 14.5, "synthetic_constituents": .5},
    {"observed_constituents": 14, "synthetic_constituents": True},
    {"observed_constituents": 16, "synthetic_constituents": -1},
    {"opened_at": (OPEN + timedelta(minutes=1)).isoformat()},
    {"opened_at": (OPEN + timedelta(microseconds=1)).isoformat()},
    {"opened_at": None},
    {"available_at": None},
    {"available_at": (OPEN + STEP - timedelta(microseconds=1)).isoformat()},
    {"evidence_json": "invalid json"},
    {"evidence_json": {"constituents": constituents()[:-1]}},
    {"evidence_json": {"constituents": list(reversed(constituents()))}},
    {"evidence_json": {"constituents": [constituents()[0]] * 15}},
    {"evidence_json": {"constituents": [None] * 15}},
])
def test_completed_observation_rejects_missing_or_conflicting_proof(changes):
    assert not data.completed_15m_observation(observation(**changes))


def test_mixed_pandas_schemas_preserve_each_rows_positive_proof():
    interval = observation()
    counts = observation(opened_at=(OPEN + STEP).isoformat(),
                         available_at=(OPEN + 2 * STEP).isoformat(),
                         observed_constituents=15, synthetic_constituents=0)
    del counts["interval_seconds"]
    del counts["evidence_json"]
    rows = pd.DataFrame([interval, counts]).to_dict("records")
    assert pd.isna(rows[0]["observed_constituents"])
    assert pd.isna(rows[1]["interval_seconds"])
    assert all(data.completed_15m_observation(row, from_frame=True) for row in rows)
    assert all(not data.completed_15m_observation(row) for row in rows)
    assert not data.completed_15m_observation({**rows[1], "synthetic_constituents": np.nan}, from_frame=True)


@pytest.mark.parametrize("fields", [
    {"interval_seconds": None, "observed_constituents": 15, "synthetic_constituents": 0},
    {"interval_seconds": np.nan, "observed_constituents": 15, "synthetic_constituents": 0},
    {"interval_seconds": 900, "observed_constituents": np.nan, "synthetic_constituents": None,
     "evidence_json": np.nan},
])
def test_null_schema_fields_are_absent_only_at_dataframe_boundary(fields):
    row = observation(**fields)
    assert not data.completed_15m_observation(row)
    assert data.completed_15m_observation(row, from_frame=True)


def test_full_quarter_evidence_does_not_certify_synthetic_continuity():
    row = observation(synthetic=True, evidence_json='{"legacy_confirmations":2}')
    assert data.completed_15m_observation(row)
    assert not data.synthetic_continuity_verified(row)


def test_legacy_attempt_list_needs_independent_full_quarter_proof():
    row = observation(evidence_json='[{"attempt_id":"legacy"}]')
    assert data.completed_15m_observation(row)
    del row["interval_seconds"]
    assert not data.completed_15m_observation(row)


def revisions():
    return [observation(),
        observation(interval_seconds=60, close=99, available_at="2026-10-08T17:46:00Z"),
        observation(observed_constituents=14, synthetic_constituents=0,
                    close=98, available_at="2026-10-08T17:47:00Z"),
        observation(close=97, available_at="2026-10-08T18:00:00Z")]


def frame(rows):
    result = pd.DataFrame(rows)
    for column in ("opened_at", "available_at"):
        result[column] = pd.to_datetime(result[column], utc=True)
    return result


def raw_rows(store):
    with store.connect() as connection:
        return connection.execute("SELECT id,payload FROM candles ORDER BY rowid").fetchall()


def save_raw(store, rows):
    # Existing saved evidence can predate the current completion check.
    with store.connect() as connection:
        for row in rows:
            connection.execute("INSERT INTO candles VALUES(?,?,?,?,?)",
                (digest(row), row["symbol"], row["opened_at"], row["available_at"], encoded(row)))


@pytest.mark.parametrize("consumer", ["minute_feed", "live_histories"])
@pytest.mark.parametrize("location", ["saved", "history"])
def test_restoration_keeps_valid_revision_before_native_partial_and_future_rows(tmp_path, consumer, location):
    store = ShadowStore(tmp_path / "observations.sqlite3")
    rows = revisions()
    rows.append(observation(available_at=(OPEN + timedelta(minutes=1)).isoformat(), interval_seconds=60))
    original = frame(rows) if location == "history" else frame(rows[:1]).iloc[:0].copy()
    original.attrs["immutable-fixture"] = "retained"
    original_before = original.copy(deep=True)
    if location == "saved":
        save_raw(store, rows)
    saved_before = raw_rows(store)
    history = data.History(original, {"source_dataset": "EQUS.MINI"})
    histories = {"AAPL": history}
    provider = SimpleNamespace(dataset="EQUS.MINI")
    if consumer == "minute_feed":
        feed = QuarterHourMinuteFeed(provider=provider, store=store, journal=object(),
            histories=histories, pc="Scout", symbols=("AAPL",), session_id="restored", clock=lambda: NOW,
            start_reference=OPEN)
        admitted = feed.history_snapshot()["AAPL"].frame
    else:
        LiveHistories(histories=histories, provider=provider, store=store, journal=object(),
            reference_at=OPEN, clock=lambda: NOW, sleep=lambda _: pytest.fail("restoration fetched"))
        admitted = histories["AAPL"].frame
    assert len(admitted) == 1 and admitted.iloc[0].close == 101
    assert pd.Timestamp(admitted.iloc[0].available_at) == pd.Timestamp(rows[0]["available_at"])
    pd.testing.assert_frame_equal(original, original_before)
    assert original.attrs == original_before.attrs and history.frame is original
    assert raw_rows(store) == saved_before


def test_minute_feed_restoration_keeps_symbols_and_sources_separate(tmp_path):
    store = ShadowStore(tmp_path / "observations.sqlite3")
    save_raw(store, [observation(), observation(source_id="other:EQUS.MINI", close=99),
                    observation(source_dataset="XNAS.ITCH", close=98), observation(symbol="PEER", close=97)])
    original = raw_rows(store)
    feed = QuarterHourMinuteFeed(provider=SimpleNamespace(dataset="EQUS.MINI"), store=store,
        journal=object(), histories={}, pc="Scout", symbols=("AAPL",), session_id="source-scope",
        clock=lambda: NOW, start_reference=OPEN)
    snapshot = feed.history_snapshot()
    assert set(snapshot) == {"AAPL"} and snapshot["AAPL"].frame.iloc[0].close == 101
    with pytest.raises(ContractError, match="outside this independent"):
        feed.history_source(OPEN).get("PEER")
    assert raw_rows(store) == original


def forecasts(store):
    own = store.save_forecast(forecast(pc="Scout", mode="live", cycle_id="scout",
                                     source_id=SOURCE, source_dataset="EQUS.MINI"))
    peer = store.save_forecast(forecast(pc="Atlas", mode="live", cycle_id="atlas",
                                      source_id=SOURCE, source_dataset="EQUS.MINI"))
    return own, peer


def score_once(store, history, *, source=None, stopped=None):
    done = []
    live_session.scoring_loop(store, histories={"AAPL": history}, provider=object(), journal=object(),
        session_id="scout-score", stopped=stopped or (lambda: bool(done)), clock=lambda: NOW,
        pause=lambda _: done.append(True), pc="Scout", history_source=source)


class Incoming:
    def __init__(self, history):
        self.history = history

    def bind_cycle(self, identity):
        assert identity.startswith("score:")

    def get(self, symbol):
        assert symbol == "AAPL"
        return self.history


@pytest.mark.parametrize("path", ["stored", "fallback", "shadow_runner"])
def test_scoring_selects_earlier_complete_revision_with_source_and_pc_boundaries(tmp_path, path):
    store = ShadowStore(tmp_path / "scoring.sqlite3")
    own, peer = forecasts(store)
    rows = revisions() + [observation(source_id="other:EQUS.MINI", close=96,
                                     available_at="2026-10-08T17:48:00Z")]
    history = data.History(frame(rows), {"source_dataset": "EQUS.MINI"})
    before = history.frame.copy(deep=True)
    if path == "stored":
        save_raw(store, rows)
        saved_before = raw_rows(store)
        score_once(store, history, source=lambda _: pytest.fail("valid saved target requested acquisition"))
        assert raw_rows(store) == saved_before
    elif path == "fallback":
        score_once(store, history, source=lambda _: Incoming(history))
    else:
        runner = ShadowRunner(store=store, pc="Scout", symbols=("AAPL",))
        result = runner.score_available({"AAPL": history}, as_of=NOW, mode="live")
        assert len(result) == 1 and result[0]["status"] == "scored"
    observed = store.revisions(own["forecast_id"])
    assert len(observed) == 1 and observed[0]["target_not_down"] is True
    assert observed[0]["target_close_price"] == "101"
    assert not store.revisions(peer["forecast_id"])
    assert store.forecast("scout", "AAPL") == own
    pd.testing.assert_frame_equal(history.frame, before)


@pytest.mark.parametrize("changes", [
    {"interval_seconds": 60},
    {"interval_seconds": None},
    {"observed_constituents": 14, "synthetic_constituents": 0},
    {"available_at": "2026-10-08T18:00:00Z"},
    {"synthetic": True, "evidence_json": '{"legacy_confirmations":2}'},
    {"source_id": "other:EQUS.MINI"},
])
def test_invalid_fallback_remains_unresolved_without_fabricated_score(tmp_path, changes):
    store = ShadowStore(tmp_path / "scoring.sqlite3")
    own, peer = forecasts(store)
    history = data.History(frame([observation(**changes)]), {"source_dataset": "EQUS.MINI"})
    before = deepcopy(history.frame)
    score_once(store, history, source=lambda _: Incoming(history))
    assert not store.revisions(own["forecast_id"]) and not store.revisions(peer["forecast_id"])
    assert not raw_rows(store)
    with store.connect() as connection:
        retries = connection.execute("SELECT payload FROM events WHERE kind='target_scoring_retry'").fetchall()
    assert len(retries) == 1 and json.loads(retries[0][0])["forecast_id"] == own["forecast_id"]
    pd.testing.assert_frame_equal(history.frame, before)


def test_manual_stop_prevents_scoring_or_acquisition_without_changing_saved_evidence(tmp_path):
    store = ShadowStore(tmp_path / "scoring.sqlite3")
    own, peer = forecasts(store)
    save_raw(store, [observation()])
    before = raw_rows(store)
    score_once(store, object(), source=lambda _: pytest.fail("manual stop fetched"), stopped=lambda: True)
    assert raw_rows(store) == before
    assert not store.revisions(own["forecast_id"]) and not store.revisions(peer["forecast_id"])


def test_legacy_forecast_with_only_source_id_can_score_its_saved_target(tmp_path):
    store = ShadowStore(tmp_path / "legacy.sqlite3")
    own = store.save_forecast(forecast(pc="Scout", mode="live", source_id=SOURCE))
    assert "source_dataset" not in own
    save_raw(store, [observation(), observation(source_id="other:EQUS.MINI", close=99,
                                               available_at="2026-10-08T17:48:00Z")])
    score_once(store, object(), source=lambda _: pytest.fail("saved legacy target fetched"))
    scored = store.revisions(own["forecast_id"])
    assert len(scored) == 1 and scored[0]["status"] == "scored"
    assert scored[0]["target_not_down"] is True


def verified_synthetic():
    session = data.SessionBounds("2026-10-08", "2026-10-08T11:00:00Z", "2026-10-09T00:00:00Z")
    previous = observation(opened_at=(OPEN - STEP).isoformat(), available_at=OPEN.isoformat(),
        session_id=session.session_id, session_open=session.opened_at, session_close=session.closed_at)
    attempts = [data.IntervalResponse(str(index), "AAPL", SOURCE, "EQUS.MINI", OPEN.isoformat(),
        (OPEN + STEP).isoformat(), (OPEN + STEP + timedelta(seconds=5 * index)).isoformat(),
        (OPEN + STEP + timedelta(seconds=5 * index + 1)).isoformat(), "success", (), OPEN.isoformat(),
        (OPEN + STEP).isoformat(), "successful covered provider omission") for index in (1, 2)]
    result = data.resolve_interval(attempts, session=session, previous=previous)
    assert result["status"] == "synthetic_no_trade"
    return result["candle"]


@pytest.mark.parametrize("path", ["stored", "fallback", "shadow_runner"])
def test_verified_synthetic_full_quarter_retains_separate_outcome_status(tmp_path, path):
    store = ShadowStore(tmp_path / "synthetic.sqlite3")
    own, peer = forecasts(store)
    row = verified_synthetic()
    assert data.synthetic_continuity_verified(row)
    assert data.completed_15m_observation(row)
    history = data.History(frame([row]), {"source_dataset": "EQUS.MINI"})
    if path == "stored":
        save_raw(store, [row])
        score_once(store, history, source=lambda _: pytest.fail("verified saved target fetched"))
    elif path == "fallback":
        score_once(store, history, source=lambda _: Incoming(history))
    else:
        ShadowRunner(store=store, pc="Scout", symbols=("AAPL",)).score_available(
            {"AAPL": history}, as_of=NOW, mode="live")
    result = store.revisions(own["forecast_id"])
    assert len(result) == 1 and result[0]["status"] == "synthetic"
    assert not store.revisions(peer["forecast_id"])


@pytest.mark.parametrize("synthetic", [False, True])
def test_shadow_invalid_candidate_preserves_existing_scored_or_synthetic_revision(tmp_path, synthetic):
    store = ShadowStore(tmp_path / "prior-outcome.sqlite3")
    own, peer = forecasts(store)
    original = observation(synthetic=synthetic, evidence_json='{"legacy_confirmations":2}')
    prior = store.observe(own, as_of="2026-10-08T17:49:00Z", candle=original)
    saved = raw_rows(store)
    # The old synthetic score remains immutable even though its provenance is no
    # longer sufficient to admit that candle as a fresh scoring candidate.
    incoming = data.History(frame([observation(synthetic=True,
        evidence_json='{"legacy_confirmations":2}', available_at="2026-10-08T17:48:00Z", close=99)]),
        {"source_dataset": "EQUS.MINI"})
    scores = ShadowRunner(store=store, pc="Scout", symbols=("AAPL",)).score_available(
        {"AAPL": incoming}, as_of=NOW, mode="live")
    assert scores == [prior] and store.revisions(own["forecast_id"]) == [prior]
    assert prior["status"] == ("synthetic" if synthetic else "scored")
    assert raw_rows(store) == saved and not store.revisions(peer["forecast_id"])
