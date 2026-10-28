"""Offline Tk integration for imported peer display evidence, using invented rows."""
from __future__ import annotations

import gc
import hashlib
import json
import threading
import time
import tkinter as tk
import weakref
from tkinter import ttk

import pytest

import app.ui.gameplan_stats as stats_ui
from app.ui.atlas_stats_data import AtlasStatsSnapshot
from app.ui.scout_stats_data import (
    TARGET_VERSION, execution_summary, load_scout_stats, prediction_summary,
    scout_stats_sessions,
)
from gameplan_stats_fixture import write_review
from test_scout_stats_data import add_forecast, add_outcome, prediction_store


def test_empty_default_retains_missing_snapshot(tmp_path):
    assert scout_stats_sessions(tmp_path) == ()
    saved = load_scout_stats(tmp_path)
    assert saved.session is None
    assert saved.status == "missing"
    assert saved.forecasts == saved.orders == ()


def archive(root, *, session="2026-10-27", revision="first", probability=.8,
            generated_at="2026-10-28T01:32:47Z"):
    """Write an invented, pinned peer projection; never read real exchange evidence."""
    completion = "20261028T013247Z-scout-stats-fixture-" + revision
    directory = root / "intraday/scout/display-exports/v1" / completion
    directory.mkdir(parents=True)
    forecast = dict(forecast_id="fixture-forecast", pc="Scout", symbol="TEST", mode="live",
        target_version=TARGET_VERSION, supported_target=True,
        reference_at=f"{session}T16:00:00Z", issued_at=f"{session}T16:15:00Z",
        target_open=f"{session}T16:30:00Z", target_close=f"{session}T16:45:00Z",
        probability_not_down=probability, outcome_status="scored", target_not_down=True,
        outcome_observed_at=f"{session}T16:46:00Z", unavailable_reason=None)
    snapshot = dict(session=session, status="read", message="Invented saved Scout display fixture.",
        forecasts=[forecast], orders=[], warnings=[], prediction_status="read", execution_status="read")
    prediction, execution = prediction_summary([forecast]), execution_summary([])
    common = dict(producer="Scout", recipient="Atlas", session=session, generated_at=generated_at,
        completion_id=completion, source_files={name: "a" * 64 for name in
            ("app/ui/scout_stats_data.py", "app/ui/scout_stats_widgets.py")})
    payload = dict(common, schema_version="scout-15m-display-export-v1", snapshot=snapshot,
        prediction_summary=prediction, execution_summary=execution,
        per_symbol={"TEST": {"prediction": prediction, "execution": execution}})
    raw = json.dumps(payload, sort_keys=True).encode()
    report = b"<html><body>Invented offline fixture</body></html>"
    manifest = dict(common, schema_version="scout-15m-display-delivery-v1", machine="pc-new",
        files={name: {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
            for name, content in (("snapshot.json", raw), ("report.html", report))})
    manifest_raw = json.dumps(manifest, sort_keys=True).encode()
    for name, content in (("snapshot.json", raw), ("report.html", report), ("manifest.json", manifest_raw)):
        (directory / name).write_bytes(content)
    receipt = dict(session=session, completion_id=completion, verified_at="2026-10-28T02:00:00Z",
        manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(), snapshot_sha256=hashlib.sha256(raw).hexdigest())
    (directory / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    return directory, payload


@pytest.fixture(scope="module")
def tk_root():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk unavailable: {exc}")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def view(tmp_path, tk_root):
    frame = ttk.Frame(tk_root)
    frame.pack(fill="both", expand=True)
    tab = stats_ui.GameplanStatsTab(tk_root, frame, datastore_root=tmp_path, auto_load=False)
    reference = weakref.ref(tab)
    try:
        yield weakref.proxy(tab)
    finally:
        frame.destroy()
        del tab, frame
        gc.collect()
        assert reference() is None, "Peer integration fixture retained destroyed Tk view"


def finish(view):
    deadline = time.monotonic() + 10
    while view._loading and time.monotonic() < deadline:
        view.root.update()
        time.sleep(.01)
    assert not view._loading


def test_peer_session_is_discovered_selected_and_source_is_visible(view, tmp_path):
    _, payload = archive(tmp_path)
    write_review(tmp_path)
    view.refresh()
    finish(view)
    assert "2026-10-27" in view.date_box.cget("values")
    assert view.session.get() == view.scout.snapshot.session == "2026-10-27"
    assert view.scout.accuracy_label.cget("text") == "100.0%"
    assert view.scout.brier_label.cget("text") == "0.040"
    source = view.scout.source_label.cget("text")
    assert "Scout saved export" in source
    assert "Oct 27, 18:32:47 PDT" in source
    assert payload["completion_id"] in source
    assert view.review is None
    assert all(value.get() == "—" for value in view.values.values())


def test_scout_displays_before_slow_atlas_read_and_ignores_stale_updates(view, tmp_path, monkeypatch):
    archive(tmp_path)
    release = threading.Event()
    def atlas_read(*_):
        assert release.wait(10)
        return AtlasStatsSnapshot(session="2026-10-27")
    monkeypatch.setattr(stats_ui, "load_atlas_stats", atlas_read)
    view.session.set("2026-10-27")
    view._date_changed()
    try:
        deadline = time.monotonic() + 5
        while view.scout.snapshot is None and time.monotonic() < deadline:
            view.root.update()
            time.sleep(.01)
        assert view._loading
        assert view.scout.accuracy_label.cget("text") == "100.0%"
        saved = view.scout.snapshot
        view._messages.put(("scout", view._request_id - 1, ("2026-10-26",), None))
        view._poll()
        assert view.scout.snapshot is saved
        assert view.session.get() == "2026-10-27"
    finally:
        release.set()
        finish(view)


def test_pinned_peer_refresh_selects_new_export_and_keeps_atlas_and_gameplan(view, tmp_path, monkeypatch):
    _, first = archive(tmp_path)
    write_review(tmp_path, session="2026-10-27")
    atlas = AtlasStatsSnapshot(session="2026-10-27", status="read", message="Atlas fixture",
        forecasts=tuple(first["snapshot"]["forecasts"]), prediction_status="read", execution_status="read")
    monkeypatch.setattr(stats_ui, "load_atlas_stats", lambda *_: atlas)
    view.session.set("2026-10-27")
    view._date_changed()
    finish(view)
    before = tuple(value.get() for value in view.values.values())
    assert view.scout.accuracy_label.cget("text") == "100.0%"
    _, second = archive(tmp_path, revision="second", probability=.2, generated_at="2026-10-28T01:40:00Z")
    view.refresh()
    finish(view)
    assert view.session.get() == "2026-10-27"
    assert view.scout.accuracy_label.cget("text") == "0.0%"
    assert view.scout.brier_label.cget("text") == "0.640"
    assert second["completion_id"] in view.scout.source_label.cget("text")
    assert tuple(value.get() for value in view.values.values()) == before
    assert view.values["accuracy"].get() == "50.0%"
    assert view.atlas.snapshot is atlas
    assert view.atlas.accuracy_label.cget("text") == "100.0%"
    view.horizon.set("4 hours")
    view.render()
    assert view.scout.accuracy_label.cget("text") == "0.0%"


def test_atlas_only_local_database_does_not_hide_peer_session(tmp_path):
    path = prediction_store(tmp_path)
    add_forecast(path, pc="Atlas")
    archive(tmp_path)
    before = path.read_bytes()
    saved = load_scout_stats(tmp_path, "2026-10-27")
    assert saved.source == "Scout saved export"
    assert len(saved.forecasts) == 1
    assert prediction_summary(saved.forecasts)["accuracy"] == 1
    assert "2026-10-27" in scout_stats_sessions(tmp_path)
    assert path.read_bytes() == before


def test_local_scout_population_takes_precedence_and_is_not_mixed(tmp_path):
    path = prediction_store(tmp_path)
    fid = add_forecast(path)
    add_outcome(path, fid)
    archive(tmp_path, session="2026-10-23", probability=.2)
    before = path.read_bytes()
    saved = load_scout_stats(tmp_path, "2026-10-23")
    assert saved.source == "local"
    assert len(saved.forecasts) == 1
    assert saved.forecasts[0]["forecast_id"] == fid
    assert prediction_summary(saved.forecasts)["accuracy"] == 1
    assert path.read_bytes() == before


def test_invalid_peer_bytes_are_visible_without_changing_other_panels(view, tmp_path, monkeypatch):
    directory, _ = archive(tmp_path)
    write_review(tmp_path, session="2026-10-27")
    atlas = AtlasStatsSnapshot(session="2026-10-27", status="read", message="Atlas fixture",
        prediction_status="read", execution_status="read")
    monkeypatch.setattr(stats_ui, "load_atlas_stats", lambda *_: atlas)
    (directory / "snapshot.json").write_bytes(b"tampered")
    view.session.set("2026-10-27")
    view._date_changed()
    finish(view)
    assert view.scout.snapshot.prediction_status == "unreadable"
    assert view.scout.accuracy_label.cget("text") == "—"
    assert view.values["accuracy"].get() == "50.0%"
    assert view.atlas.snapshot is atlas
