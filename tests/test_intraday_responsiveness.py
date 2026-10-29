"""Offline regression checks for compact, cancellable saved-history display."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone

import pytest

from app.ui.intraday import _prepare_metrics
from app.ui.intraday_data import (
    DEFAULT_METRIC_SCOPE, DISPLAY_FORECAST_FIELDS, DISPLAY_MODEL_FIELDS,
    filter_forecasts, forecast_metrics, load_intraday_snapshot,
    reference_input_provenance,
)
from test_intraday_ui import forecast, write_store


NOW = datetime(2026, 10, 12, 19, tzinfo=timezone.utc)


def test_projection_keeps_all_saved_scores_without_loading_scientific_trees(tmp_path):
    path = tmp_path / "history.sqlite3"
    giant = {"unused_training_matrix": "synthetic-large-science" * 50000}
    rows = []
    for index, mode in enumerate(("live", "shadow", "historical_replay")):
        row = forecast(str(index), mode=mode, probability_not_down=.2 + index * .3,
                       dataset_manifest=giant, features=giant, model_selection=giant,
                       reference_provenance={"status": "unknown", "reason": "fixture"},
                       direction="SELL", direction_policy_version="fixture-saved-policy")
        row["models"][0]["metadata"] = giant
        rows.append(row)
    write_store(path, rows, [(row["forecast_id"], row["outcome"]) for row in rows],
                [(row["forecast_id"], dict(direction="BUY", desired_quantity=4,
                                         adjusted_quantity=0, disposition="no capacity",
                                         model_snapshot=giant)) for row in rows])
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE events(id INTEGER PRIMARY KEY,kind TEXT,payload TEXT)")
        for row in rows:
            connection.execute("INSERT INTO events(kind,payload) VALUES(?,?)", (
                "forecast_persisted", json.dumps(dict(forecast_id=row["forecast_id"], persistence_seconds=.3,
                                                     original_forecast=giant))))
    before = hashlib.sha256(path.read_bytes()).digest()
    snapshot = load_intraday_snapshot({"Local": path}, now=NOW)
    assert not snapshot.warnings
    assert len(snapshot.forecasts) == len(rows)
    assert forecast_metrics(snapshot.forecasts) == forecast_metrics(rows)
    assert len(filter_forecasts(snapshot, mode="All")) == 3
    encoded = json.dumps(snapshot.forecasts)
    assert len(encoded) < 15000
    assert "unused_training_matrix" not in encoded
    assert "synthetic-large-science" not in encoded
    for row in snapshot.forecasts:
        assert row["direction"] == "SELL"
        assert row["direction_policy_version"] == "fixture-saved-policy"
        assert row["intents"][0]["adjusted_quantity"] == 0
        assert row["persistence_evidence"]["persistence_seconds"] == .3
        assert row["models"][0]["saved_evidence_source"]["forecast_id"] == row["forecast_id"]
        assert "retained" in row["models"][0]["saved_evidence_note"]
    assert hashlib.sha256(path.read_bytes()).digest() == before


def test_cancelled_loader_does_not_open_a_source(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Cancelled view must not open its database")
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    snapshot = load_intraday_snapshot({"Local": tmp_path / "never-open.sqlite3"}, stopped=lambda: True)
    assert snapshot.forecasts == () and snapshot.sources == ()


def test_sqlite_progress_handler_interrupts_history_scan_and_releases_reader(tmp_path, monkeypatch):
    path = tmp_path / "history.sqlite3"
    write_store(path, [forecast(str(index)) for index in range(300)])
    calls = 0
    def stopped():
        nonlocal calls
        calls += 1
        return calls > 4
    snapshot = load_intraday_snapshot({"Local": path}, now=NOW, stopped=stopped)
    assert calls > 4
    assert snapshot.sources[0]["status"] == "unreadable"
    assert any("interrupted" in warning for warning in snapshot.warnings)
    # A cancelled source contributes no partial cumulative result, and the
    # reader is closed; a later fresh read can still show the complete history.
    assert snapshot.forecasts == ()
    assert len(load_intraday_snapshot({"Local": path}, now=NOW).forecasts) == 300


def test_prepared_statistics_retain_filters_and_all_history(tmp_path):
    rows = [forecast(str(index), mode="live") for index in range(101)]
    path = tmp_path / "history.sqlite3"
    write_store(path, rows, [(row["forecast_id"], row["outcome"]) for row in rows])
    snapshot = load_intraday_snapshot({"Local": path}, now=NOW)
    selection = dict(pc="All", symbol="All", session="All", mode="live", reference_slot="All",
                     source_dataset="All", metric_window="Cumulative", metric_scope="All saved outcomes")
    prepared = _prepare_metrics(snapshot, selection)
    assert len(prepared["rows"]) == 101
    assert prepared["ensemble"] == forecast_metrics(rows)
    assert dict(prepared["windows"])["Cumulative"] == forecast_metrics(rows)


def test_display_fields_exclude_original_model_and_dataset_trees():
    assert "probability_not_down" in DISPLAY_FORECAST_FIELDS
    assert "reference_provenance" in DISPLAY_FORECAST_FIELDS
    assert "direction_policy_version" in DISPLAY_FORECAST_FIELDS
    assert not {"features", "dataset_manifest", "model_selection"} & set(DISPLAY_FORECAST_FIELDS)
    assert "probability_not_down" in DISPLAY_MODEL_FIELDS
    assert "metadata" not in DISPLAY_MODEL_FIELDS


def test_projection_preserves_null_absence_boolean_and_snapshot_identity(tmp_path):
    path = tmp_path / "history.sqlite3"
    explicit_null = forecast("null", reference_provenance=None, mode=None)
    mismatched = forecast("mismatch", snapshot_hash="original-snapshot")
    write_store(path, [explicit_null, mismatched], intents=[("null", dict(
        intended_direction=None, direction="BUY", disposition=None, execution_disposition="saved fallback"))])
    with sqlite3.connect(path) as connection:
        connection.executescript("CREATE TABLE snapshots(id TEXT,payload TEXT);"
                                 "CREATE TABLE symbol_inputs(cycle_id TEXT,symbol TEXT,snapshot_id TEXT);")
        candle = dict(opened_at=mismatched["reference_at"], observed_constituents=15,
                      synthetic_constituents=0, synthetic=False, no_trade=False, availability_kind="actual")
        connection.execute("INSERT INTO snapshots VALUES(?,?)", ("different-snapshot", json.dumps(dict(candles=[candle]))))
        connection.execute("INSERT INTO symbol_inputs VALUES(?,?,?)", (mismatched["cycle_id"], mismatched["symbol"], "different-snapshot"))
    snapshot = load_intraday_snapshot({"Local": path}, now=NOW)
    values = {row["forecast_id"]: row for row in snapshot.forecasts}
    assert "reference_provenance" in values["null"] and values["null"]["reference_provenance"] is None
    assert values["null"]["mode"] is None
    assert values["null"]["intents"][0]["intended_direction"] is None
    assert values["null"]["intents"][0]["disposition"] is None
    assert "reference_provenance" not in values["mismatch"]
    assert values["mismatch"]["snapshot_hash"] == "original-snapshot"
    assert [row["forecast_id"] for row in filter_forecasts(snapshot, mode="shadow")] == ["mismatch"]
    null = reference_input_provenance(values["null"], database_path=path)
    mismatch = reference_input_provenance(values["mismatch"], database_path=path)
    assert null["status"] == mismatch["status"] == "unknown"
    assert "malformed" in null["reason"]
    assert "identity differs" in mismatch["reason"]


def test_unreadable_refresh_keeps_previous_saved_view(tmp_path, monkeypatch):
    import tkinter as tk
    from tkinter import ttk
    from app.ui import intraday
    from app.ui.intraday_data import IntradaySnapshot
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk unavailable: {exc}")
    root.withdraw()
    try:
        frame = ttk.Frame(root)
        tab = intraday.IntradayTab(root, frame, datastore_root=tmp_path, auto_load=False)
        previous = IntradaySnapshot(NOW, (forecast(mode="live"),))
        tab.set_snapshot(previous)
        old_label = tab.consensus_value.cget("text")
        monkeypatch.setattr(intraday, "load_model_research", lambda *args, **kwargs: {})
        monkeypatch.setattr(intraday, "load_intraday_snapshot", lambda *args, **kwargs:
                            IntradaySnapshot(NOW, sources=({"label": "Local", "status": "unreadable"},),
                                             warnings=("Local: saved file is unreadable",)))
        tab.refresh()
        deadline = time.monotonic() + 2
        while tab._loading and time.monotonic() < deadline:
            root.update()
            time.sleep(.005)
        assert not tab._loading
        assert tab.snapshot is previous
        assert tab.consensus_value.cget("text") == old_label
        assert "previous saved results retained" in tab.status.get()
        assert "unreadable" in tab.status.get()
    finally:
        root.destroy()
