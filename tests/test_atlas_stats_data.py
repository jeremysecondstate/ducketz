"""Offline Atlas saved-forecast fixtures; no execution/store/model imports."""
from datetime import datetime, timezone
import hashlib
import json
import sqlite3

import pytest

import app.ui.atlas_stats_data as data
from test_scout_stats_data import (
    NOW, REFERENCE, OPEN, CLOSE, ISSUED, add_forecast, database,
    prediction_store,
)


@pytest.fixture(autouse=True)
def independent_execution(monkeypatch):
    monkeypatch.setattr(data, "atlas_orders", lambda root, selected: ((), "missing", ("No order fixture.",)))
    monkeypatch.setattr(data, "atlas_execution_sessions", lambda root: ())


def forecast(path, key="atlas-1", **kwargs):
    return add_forecast(path, key, pc="Atlas", **kwargs)


def outcome(path, fid, *, status="scored", label=True, basis="actual", observed="2026-10-23T18:00:00+00:00"):
    evidence = dict(forecast_id=fid, status=status, target_not_down=label,
                    target_close_price="101", availability_basis=basis, unavailable_reason=None)
    raw = json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    revision = hashlib.sha256(raw.encode()).hexdigest()
    value = dict(evidence, revision_id=revision, observed_at=observed)
    with database(path) as db:
        db.execute("INSERT INTO outcomes VALUES(?,?,?,?)", (revision, fid, observed, json.dumps(value)))
    return revision


def test_atlas_live_only_latest_revision_and_immutable_originals(tmp_path):
    path = prediction_store(tmp_path)
    fid = forecast(path)
    outcome(path, fid, status="missing", label=None)
    latest = outcome(path, fid)
    add_forecast(path, "scout")
    forecast(path, "shadow", mode="shadow")
    forecast(path, "replay", mode="historical_replay")
    before = path.read_bytes()
    result = data.load_atlas_stats(tmp_path, "2026-10-23", now=NOW)
    assert result.prediction_status == "read" and result.execution_status == "missing"
    assert len(result.forecasts) == 1 and result.forecasts[0]["forecast_id"] == fid
    assert result.forecasts[0]["outcome_revision_id"] == latest
    assert "models" not in result.forecasts[0]
    assert data.prediction_summary(result.forecasts)["brier"] == pytest.approx(.04)
    assert path.read_bytes() == before


@pytest.mark.parametrize("damage", ["digest", "identity", "json", "future"])
def test_damaged_latest_revision_never_falls_back_to_old_score(tmp_path, damage):
    path = prediction_store(tmp_path)
    fid = forecast(path)
    outcome(path, fid, label=False)
    latest = outcome(path, fid, observed="2026-10-25T18:00:00+00:00" if damage == "future" else "2026-10-23T19:00:00+00:00")
    with database(path) as db:
        if damage == "digest":
            db.execute("UPDATE outcomes SET id='wrong' WHERE id=?", (latest,))
        elif damage == "identity":
            db.execute("UPDATE outcomes SET observed_at='different' WHERE id=?", (latest,))
        elif damage == "json":
            db.execute("UPDATE outcomes SET payload='not json' WHERE id=?", (latest,))
    result = data.load_atlas_stats(tmp_path, "2026-10-23", now=NOW)
    assert result.prediction_status == "partial" and len(result.forecasts) == 1
    metrics = data.prediction_summary(result.forecasts)
    assert metrics["scored"] == 0 and metrics["missing"] == 1 and metrics["accuracy"] is None


def test_only_observed_before_open_population_enters_binary_score(tmp_path):
    path = prediction_store(tmp_path)
    cases = [("scored", ISSUED, .8, "scored"), ("missing", ISSUED, .8, None),
             ("unscorable", ISSUED, .8, "unscorable"), ("synthetic", ISSUED, .8, "synthetic"),
             ("during", OPEN, .8, "scored"), ("after", CLOSE, .8, "scored"),
             ("unknown", "bad", .8, "scored"), ("invalid", ISSUED, 1.1, "scored")]
    for key, issued, probability, status in cases:
        forecast(path, key, issued=issued, probability=probability)
        if status:
            outcome(path, key, status=status)
    forecast(path, "incompatible", target="another-target")
    metrics = data.prediction_summary(data.load_atlas_stats(tmp_path, "2026-10-23", now=NOW).forecasts)
    assert metrics == dict(issued=9, eligible=5, scored=1, correct=1, pending=0, missing=1,
        unscorable=1, synthetic=1, during_target=1, after_target=1, timing_unknown=1,
        invalid_probability=1, unsupported_target=1, accuracy=1, brier=pytest.approx(.04))


@pytest.mark.parametrize("basis", [None, "assumed_historical_close"])
def test_saved_score_without_observed_availability_is_unscorable(tmp_path, basis):
    path = prediction_store(tmp_path)
    fid = forecast(path)
    outcome(path, fid, basis=basis)
    result = data.load_atlas_stats(tmp_path, "2026-10-23", now=NOW)
    assert result.prediction_status == "partial"
    assert data.prediction_summary(result.forecasts)["unscorable"] == 1
    assert data.prediction_summary(result.forecasts)["scored"] == 0


def test_synthetic_outcome_remains_explicit_without_observed_proof(tmp_path):
    path = prediction_store(tmp_path)
    fid = forecast(path)
    outcome(path, fid, status="synthetic", basis=None)
    metrics = data.prediction_summary(data.load_atlas_stats(tmp_path, "2026-10-23", now=NOW).forecasts)
    assert metrics["synthetic"] == 1 and metrics["scored"] == 0


def test_target_geometry_is_not_reinterpreted_and_binary_half_is_not_hold(tmp_path):
    path = prediction_store(tmp_path)
    fid = forecast(path, probability=.5)
    outcome(path, fid)
    forecast(path, "wrong-window")
    with database(path) as db:
        raw = json.loads(db.execute("SELECT payload FROM forecasts WHERE id='wrong-window'").fetchone()[0])
        raw.update(target_open="2026-10-23T17:15:00+00:00")
        db.execute("UPDATE forecasts SET payload=? WHERE id='wrong-window'", (json.dumps(raw),))
    result = data.load_atlas_stats(tmp_path, "2026-10-23", now=NOW)
    metrics = data.prediction_summary(result.forecasts)
    assert metrics["unsupported_target"] == 1 and metrics["scored"] == metrics["correct"] == 1
    assert metrics["brier"] == .25
    assert result.forecasts[0]["probability_not_down"] == .5


def test_missing_target_timestamp_is_unknown_timing_not_a_different_target(tmp_path):
    path = prediction_store(tmp_path)
    fid = forecast(path)
    with database(path) as db:
        raw = json.loads(db.execute("SELECT payload FROM forecasts WHERE id=?", (fid,)).fetchone()[0])
        raw["target_close"] = None
        db.execute("UPDATE forecasts SET payload=? WHERE id=?", (json.dumps(raw), fid))
    metrics = data.prediction_summary(data.load_atlas_stats(tmp_path, "2026-10-23", now=NOW).forecasts)
    assert metrics["timing_unknown"] == 1 and metrics["unsupported_target"] == 0


def test_pending_and_missing_outcomes_stay_outside_denominator(tmp_path):
    path = prediction_store(tmp_path)
    fid = forecast(path)
    outcome(path, fid, status="pending", label=None)
    early = datetime(2026, 10, 23, 17, 40, tzinfo=timezone.utc)
    assert data.prediction_summary(data.load_atlas_stats(tmp_path, "2026-10-23", now=early).forecasts)["pending"] == 1
    assert data.prediction_summary(data.load_atlas_stats(tmp_path, "2026-10-23", now=NOW).forecasts)["missing"] == 1


def test_selected_reference_session_is_pacific_and_discovery_is_header_only(tmp_path, monkeypatch):
    path = prediction_store(tmp_path)
    forecast(path, "utc-next-day", reference="2026-10-24T00:00:00+00:00")
    forecast(path, "other-day", reference="2026-10-22T17:00:00+00:00")
    queries = []
    original = data._connect
    def traced(path):
        db = original(path)
        db.set_trace_callback(queries.append)
        assert db.execute("PRAGMA query_only").fetchone() == (1,)
        with pytest.raises(sqlite3.OperationalError):
            db.execute("CREATE TABLE forbidden(value)")
        return db
    monkeypatch.setattr(data, "_connect", traced)
    assert data.atlas_stats_sessions(tmp_path) == ("2026-10-23", "2026-10-22")
    assert not any("payload" in query for query in queries)
    queries.clear()
    result = data.load_atlas_stats(tmp_path, "2026-10-23", now=NOW)
    assert len(result.forecasts) == 1 and result.forecasts[0]["forecast_id"] == "utc-next-day"
    projected = [query for query in queries if "json_extract(payload" in query]
    assert len(projected) == 1 and "WHERE id='utc-next-day' AND pc='Atlas'" in projected[0]


def test_malformed_source_retains_valid_records_and_execution(tmp_path, monkeypatch):
    path = prediction_store(tmp_path)
    forecast(path)
    forecast(path, "broken")
    with database(path) as db:
        db.execute("UPDATE forecasts SET payload='broken' WHERE id='broken'")
        db.execute("DROP TABLE outcomes")
    order = dict(request_id="own", status="UNKNOWN")
    monkeypatch.setattr(data, "atlas_orders", lambda root, selected: ((order,), "read", ()))
    result = data.load_atlas_stats(tmp_path, "2026-10-23", now=NOW)
    assert result.prediction_status == "partial" and len(result.forecasts) == 1
    assert result.orders == (order,) and result.execution_status == "read"
    assert any("malformed" in warning for warning in result.warnings)
    assert any("outcome table" in warning for warning in result.warnings)


@pytest.mark.parametrize("kind", ["missing", "unreadable"])
def test_prediction_failure_does_not_hide_execution_only_sessions(tmp_path, monkeypatch, kind):
    if kind == "unreadable":
        path = tmp_path / "intraday/shadow.sqlite3"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not sqlite")
    order = dict(request_id="order-only", status="UNKNOWN")
    monkeypatch.setattr(data, "atlas_execution_sessions", lambda root: ("2026-10-23",))
    monkeypatch.setattr(data, "atlas_orders", lambda root, selected: ((order,), "read", ()))
    result = data.load_atlas_stats(tmp_path, now=NOW)
    assert result.session == "2026-10-23" and result.prediction_status == kind
    assert result.orders == (order,) and result.execution_status == "read"


def test_execution_error_does_not_hide_predictions(tmp_path, monkeypatch):
    path = prediction_store(tmp_path)
    fid = forecast(path)
    outcome(path, fid)
    def fail(*args):
        raise sqlite3.OperationalError("broken order source")
    monkeypatch.setattr(data, "atlas_orders", fail)
    result = data.load_atlas_stats(tmp_path, "2026-10-23", now=NOW)
    assert result.execution_status == "unreadable" and result.prediction_status == "read"
    assert data.prediction_summary(result.forecasts)["scored"] == 1
