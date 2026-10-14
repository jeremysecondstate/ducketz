"""Saved final targets require full 15m evidence, not a late native minute."""
from datetime import timedelta
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.intraday import candle_collection, data
from ml.intraday.data import AcquisitionPending, History, SessionBounds, aggregate_minutes, completed_15m_observation
from ml.intraday.prediction import STEP, instant
from ml.intraday.runtime import LiveHistories
from ml.intraday.store import ShadowStore


CLOSE = instant("2026-07-11T00:00:00Z")
REFERENCE = CLOSE-STEP
SOURCE = "databento:EQUS.MINI:ohlcv-1m"


def observation(**changes):
    value = dict(symbol="AAPL", opened_at=REFERENCE.isoformat(),
        available_at=(CLOSE+timedelta(seconds=1)).isoformat(), interval_seconds=900,
        availability_kind="actual", source_dataset="EQUS.MINI", source_id=SOURCE,
        session_id="fixture", synthetic=False, no_trade=False, evidence_json="{}",
        open=100., high=101., low=99., close=100.5, volume=100.)
    value.update(changes)
    return value


@pytest.mark.parametrize("changes", [
    {"interval_seconds": 60}, {"interval_seconds": 1200}, {"interval_seconds": None},
    {"opened_at": (REFERENCE+timedelta(minutes=1)).isoformat()},
    {"available_at": (CLOSE-timedelta(microseconds=1)).isoformat()},
    {"observed_constituents": 14, "synthetic_constituents": 0},
    {"observed_constituents": 15},
    {"observed_constituents": 14.5, "synthetic_constituents": .5},
    {"observed_constituents": float("nan"), "synthetic_constituents": 0},
    {"observed_constituents": float("inf"), "synthetic_constituents": 0},
    {"observed_constituents": True, "synthetic_constituents": 14},
    {"observed_constituents": 16, "synthetic_constituents": -1},
])
def test_incomplete_or_conflicting_full_quarter_evidence_is_rejected(changes):
    assert not completed_15m_observation(observation(**changes))


def test_legacy_explicit_900_second_bar_is_supported_without_counts():
    assert completed_15m_observation(observation())


def test_integral_float_counts_from_dataframe_concatenation_are_supported():
    value = observation(observed_constituents=15., synthetic_constituents=0., evidence_json=None)
    value.pop("interval_seconds")
    assert completed_15m_observation(value)


@pytest.mark.parametrize("missing", [None, float("nan")])
def test_legacy_duration_proof_survives_nullable_optional_evidence(missing):
    assert completed_15m_observation(observation(evidence_json=missing))


def test_malformed_constituent_evidence_is_not_treated_as_nullable_legacy_metadata():
    assert not completed_15m_observation(observation(evidence_json="{broken"))


def test_unknown_shape_requires_positive_15m_proof():
    value = observation()
    value.pop("interval_seconds")
    assert not completed_15m_observation(value)
    value.update(observed_constituents=15, synthetic_constituents=0)
    assert completed_15m_observation(value)


@pytest.mark.parametrize("kind", ["missing", "extra", "duplicate", "out_of_order"])
def test_constituent_grid_must_be_exact_even_with_other_15m_proof(kind):
    rows = [{"opened_at": (REFERENCE+timedelta(minutes=i)).isoformat()} for i in range(15)]
    if kind == "missing":
        rows.pop()
    elif kind == "extra":
        rows.append({"opened_at": CLOSE.isoformat()})
    elif kind == "duplicate":
        rows[-1] = dict(rows[-2])
    else:
        rows[3], rows[4] = rows[4], rows[3]
    assert not completed_15m_observation(observation(evidence_json=json.dumps({"constituents": rows})))


def test_genuine_fifteen_native_minute_aggregate_is_accepted():
    rows = []
    for i in range(15):
        row = observation(opened_at=(REFERENCE+timedelta(minutes=i)).isoformat(), interval_seconds=60)
        row.update(session_open=(REFERENCE-STEP).isoformat(), session_close=CLOSE.isoformat())
        rows.append(row)
    frame, report = aggregate_minutes(pd.DataFrame(rows),
        sessions=[SessionBounds("fixture", (REFERENCE-STEP).isoformat(), CLOSE.isoformat())])
    assert report["full_intervals"] == 1 and report["incomplete_intervals"] == 0
    aggregate = frame.iloc[0].to_dict()
    assert aggregate["observed_constituents"] == 15 and aggregate["synthetic_constituents"] == 0
    assert len(json.loads(aggregate["evidence_json"])["constituents"]) == 15
    assert completed_15m_observation(aggregate)


def test_saved_one_minute_row_does_not_finish_final_collection(monkeypatch, tmp_path):
    store = ShadowStore(tmp_path/"shadow.sqlite3")
    store.record_candle(observation(interval_seconds=60))

    def unavailable(**kwargs):
        raise AcquisitionPending("fixture_full_quarter_not_yet_received", 4)

    monkeypatch.setattr(candle_collection, "LiveHistories", unavailable)
    collector = candle_collection.CandleCollector(store, histories={}, symbols=("AAPL",),
        provider=SimpleNamespace(dataset="EQUS.MINI"), journal=object(), session_id="manual-fixture",
        started_at="2026-07-10T22:00:00Z", clock=lambda: CLOSE+timedelta(seconds=5))
    result = collector.pass_once()
    assert not result["final_complete"] and result["pending_final_symbols"] == ["AAPL"]
    assert result["saved"] == 0
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='final_candle_collection_completed'").fetchone()[0] == 0


def history_for(value):
    frame = pd.DataFrame([value])
    for name in ("opened_at", "available_at"):
        frame[name] = pd.to_datetime(frame[name], utc=True)
    return History(frame, {"source_dataset": "EQUS.MINI"})


def test_runtime_does_not_reuse_incomplete_saved_native_minute(monkeypatch, tmp_path):
    store = ShadowStore(tmp_path/"shadow.sqlite3")
    incomplete = observation(interval_seconds=60)
    store.record_candle(incomplete)
    attempted = []

    def unavailable(provider, **kwargs):
        attempted.append(kwargs["opened_at"])
        raise AcquisitionPending("fixture_full_quarter_not_yet_received", 4)

    monkeypatch.setattr(data, "recover_databento_candle_step", unavailable)
    incoming = LiveHistories(histories={"AAPL": history_for(incomplete)},
        provider=SimpleNamespace(dataset="EQUS.MINI"), store=store,
        journal=SimpleNamespace(responses=lambda **kwargs: ()), reference_at=REFERENCE,
        clock=lambda: CLOSE+timedelta(seconds=5), bounded=True, reuse_saved=True)
    with pytest.raises(AcquisitionPending):
        incoming.get("AAPL")
    assert attempted == [REFERENCE]


def test_runtime_restores_valid_15m_revision_instead_of_newer_incomplete_row(tmp_path):
    store = ShadowStore(tmp_path/"shadow.sqlite3")
    full = observation()
    minute = observation(interval_seconds=60, close=99., available_at=(CLOSE+timedelta(seconds=2)).isoformat())
    store.record_candle(full)
    store.record_candle(minute)
    incoming = LiveHistories(histories={"AAPL": history_for(minute)},
        provider=SimpleNamespace(dataset="EQUS.MINI"), store=store,
        journal=SimpleNamespace(responses=lambda **kwargs: pytest.fail("Validated durable quarter must be reused")),
        reference_at=REFERENCE, clock=lambda: CLOSE+timedelta(seconds=5), bounded=True, reuse_saved=True)
    restored = incoming.get("AAPL").frame.iloc[-1]
    assert restored.close == full["close"] and restored.interval_seconds == 900
    assert len(store.observed_candles("AAPL")) == 2  # Preserve both original records.
