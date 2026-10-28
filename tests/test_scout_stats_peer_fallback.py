"""Synthetic local-versus-export evidence precedence and corrupt archive cases."""
from datetime import datetime, timezone

import pytest

from app.ui.scout_stats_data import ScoutStatsSnapshot, load_scout_stats, scout_stats_sessions
from test_scout_stats_data import add_forecast, add_outcome, database, journal, prediction_store
from test_scout_stats_peer import export_fixture

NOW = datetime(2026, 10, 28, 18, tzinfo=timezone.utc)
DAY = "2026-10-27"


def corrupt_receipt(root):
    path = root / "intraday/scout/display-exports/v1/unrelated-corrupt-export"
    path.mkdir(parents=True)
    (path / "receipt.json").write_text('{"session":"2026-10-26"}', encoding="utf-8")


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("with_predictions", [False, True])
def test_unrelated_corrupt_export_keeps_local_dates_rows_and_warning(tmp_path, explicit, with_predictions):
    fid = None
    if with_predictions:
        path = prediction_store(tmp_path)
        fid = add_forecast(path, reference=f"{DAY}T17:00:00Z")
        add_outcome(path, fid)
    journal(tmp_path, requests=[dict(request_id="local-order", reference_at=f"{DAY}T17:00:00Z",
                                    status="FILLED", filled_quantity=10)])
    corrupt_receipt(tmp_path)
    assert scout_stats_sessions(tmp_path) == (DAY,)
    view = load_scout_stats(tmp_path, DAY if explicit else None, now=NOW)
    assert view.session == DAY and view.source == "local"
    assert [row["forecast_id"] for row in view.forecasts] == ([fid] if with_predictions else [])
    assert [row["request_id"] for row in view.orders] == ["local-order"]
    assert view.prediction_status == ("read" if with_predictions else "missing")
    assert view.execution_status == "read"
    assert view.status == "partial"
    assert any("exports failed verification" in warning for warning in view.warnings)


@pytest.mark.parametrize("corrupt_peer", [False, True])
@pytest.mark.parametrize("with_readable_row", [False, True])
def test_partial_local_predictions_never_fall_back_to_export(tmp_path, corrupt_peer, with_readable_row):
    path = prediction_store(tmp_path)
    fid = add_forecast(path, reference=f"{DAY}T17:00:00Z") if with_readable_row else None
    with database(path) as db:
        db.execute("INSERT INTO forecasts VALUES(?,?,?,?,?,?,?)",
                   ("broken", "Scout", "TEST", "broken", f"{DAY}T17:00:00Z", f"{DAY}T17:10:00Z", "{"))
    export_fixture(tmp_path)
    if corrupt_peer:
        corrupt_receipt(tmp_path)
    view = load_scout_stats(tmp_path, DAY, now=NOW)
    assert view.source == "local" and view.prediction_status == "partial"
    assert [row["forecast_id"] for row in view.forecasts] == ([fid] if with_readable_row else [])
    assert view.orders == () and view.exported_at is None


@pytest.mark.parametrize("broken", ["predictions", "orders"])
def test_unreadable_local_store_prevents_verified_export_fallback(tmp_path, broken):
    export_fixture(tmp_path)
    if broken == "predictions":
        path = tmp_path / "intraday/shadow.sqlite3"
        path.write_bytes(b"not sqlite")
    else:
        path = tmp_path / "intraday/scout/inventory.json"
        path.write_text("{}", encoding="utf-8")
    view = load_scout_stats(tmp_path, DAY, now=NOW)
    assert view.source == "local" and view.forecasts == view.orders == ()
    assert "unreadable" in {view.prediction_status, view.execution_status}
    assert view.exported_at is None


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("corruption", ["receipt", "snapshot"])
def test_export_only_corruption_is_unavailable_not_zero(tmp_path, explicit, corruption):
    if corruption == "receipt":
        corrupt_receipt(tmp_path)
    else:
        path = export_fixture(tmp_path)
        (path / "snapshot.json").write_text("{}", encoding="utf-8")
    view = load_scout_stats(tmp_path, DAY if explicit else None, now=NOW)
    assert view.forecasts == view.orders == () and view.exported_at is None
    assert view.prediction_status == view.execution_status == view.status == "unreadable"
    assert any("exports failed verification" in warning for warning in view.warnings)


@pytest.mark.parametrize("local_store", ["missing", "empty", "atlas-only"])
def test_cleanly_empty_local_evidence_permits_verified_export(tmp_path, local_store):
    if local_store != "missing":
        path = prediction_store(tmp_path)
        if local_store == "atlas-only":
            add_forecast(path, pc="Atlas", reference=f"{DAY}T17:00:00Z")
    export_fixture(tmp_path)
    assert scout_stats_sessions(tmp_path) == (DAY,)
    view = load_scout_stats(tmp_path, now=NOW)
    assert view.source == "Scout saved export" and view.session == DAY
    assert len(view.forecasts) == len(view.orders) == 1
    assert view.forecasts[0]["forecast_id"] == "synthetic-forecast"
    assert view.exported_at == "2026-10-28T01:32:47Z" and view.completion_id


def test_newest_export_replaces_population_and_identity_on_refresh(tmp_path):
    export_fixture(tmp_path, completion_id="20261028T010000Z-synthetic-first",
                   generated_at="2026-10-28T01:00:00Z", probability=.8)
    first = load_scout_stats(tmp_path, DAY, now=NOW)
    export_fixture(tmp_path, completion_id="20261028T020000Z-synthetic-second",
                   generated_at="2026-10-28T02:00:00Z", probability=.3)
    second = load_scout_stats(tmp_path, DAY, now=NOW)
    assert len(first.forecasts) == len(second.forecasts) == 1
    assert first.forecasts[0]["probability_not_down"] == .8
    assert second.forecasts[0]["probability_not_down"] == .3
    assert second.exported_at == "2026-10-28T02:00:00Z"
    assert second.completion_id == "20261028T020000Z-synthetic-second"
    assert first.completion_id != second.completion_id


def test_newest_corrupt_export_does_not_silently_use_old_revision(tmp_path):
    export_fixture(tmp_path, completion_id="20261028T010000Z-synthetic-first",
                   generated_at="2026-10-28T01:00:00Z")
    path = export_fixture(tmp_path, completion_id="20261028T020000Z-synthetic-second",
                          generated_at="2026-10-28T02:00:00Z")
    (path / "snapshot.json").write_text("{}", encoding="utf-8")
    view = load_scout_stats(tmp_path, DAY, now=NOW)
    assert view.status == "unreadable" and view.forecasts == ()
    assert view.completion_id is None


def test_snapshot_metadata_defaults_preserve_original_positional_contract():
    view = ScoutStatsSnapshot(DAY, "read", "Saved", (), (), (), "read", "read")
    assert view.source == "local" and view.exported_at is view.completion_id is None
