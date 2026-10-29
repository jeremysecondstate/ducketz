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



def test_one_issue_jsonb_reader_matches_legacy_sqlite_with_wal_and_exact_saved_values(tmp_path, monkeypatch):
    import math
    from app.ui import intraday_data
    path = tmp_path / "active-history.sqlite3"
    below_half = math.nextafter(.5, 0)
    exact_setting = math.nextafter(.25, 0)
    giant = {"unused_science": "MUST_NOT_DESERIALIZE_FITTED_SCIENCE" * 50000}
    spec = dict(name="native", family="logistic", parameters={"C": exact_setting},
                probability_calibration="none", calibration_fraction=exact_setting)
    original = forecast("older", mode="live", probability_not_down=below_half,
        reference_provenance=None, training_seconds=math.nextafter(1.0, 0),
        model_selection={"selection": "saved_policy", "models": [spec], **giant},
        dataset_manifest={"synthetic_policy": "exclude", **giant},
        models=[dict(name="native", status="success", probability_not_down=below_half,
                     metadata={"spec": spec, "calibration": {"fraction": exact_setting},
                               "effective_parameters": {"oversized": "x" * 70000}, **giant})])
    newer = forecast("newer", mode=None, issued_at="2026-10-12T18:15:30Z",
        probability_not_down=math.nextafter(.5, 1), reference_provenance=None)
    write_store(path, [newer, original], [(original["forecast_id"], original["outcome"])])
    native_connect = sqlite3.connect
    statements, deny_jsonb = [], False
    class CheckedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            statements.append((sql, tuple(parameters)))
            if deny_jsonb and "jsonb(" in sql:
                raise sqlite3.OperationalError("no such function: jsonb")
            return super().execute(sql, parameters)
    def checked_connect(*args, **kwargs):
        kwargs["factory"] = CheckedConnection
        return native_connect(*args, **kwargs)
    native_loads = json.loads
    def checked_loads(raw, *args, **kwargs):
        assert "MUST_NOT_DESERIALIZE_FITTED_SCIENCE" not in raw
        return native_loads(raw, *args, **kwargs)
    # Keep a committed new forecast in a live WAL while both readers run.
    with native_connect(path) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("UPDATE forecasts SET issued_at=issued_at WHERE id=?", ("newer",))
        writer.commit()
        # Force a real changed record into the WAL rather than depending on
        # whether SQLite elides a no-op update.
        writer.execute("UPDATE forecasts SET reference_at=? WHERE id=?", ("2026-10-12T17:15:00Z", "newer"))
        writer.commit()
        wal = path.with_name(path.name + "-wal")
        assert wal.exists() and wal.stat().st_size > 0
        before = {file: hashlib.sha256(file.read_bytes()).hexdigest() for file in (path, wal)}
        monkeypatch.setattr(intraday_data.sqlite3, "connect", checked_connect)
        monkeypatch.setattr(intraday_data.json, "loads", checked_loads)
        fast = load_intraday_snapshot({"Local": path}, now=NOW)
        assert not fast.warnings and len(fast.forecasts) == 2
        bounded_queries = [(sql, values) for sql, values in statements if "AS MATERIALIZED" in sql]
        if sqlite3.sqlite_version_info >= (3, 45, 0):
            assert len(bounded_queries) == 2
            assert [values for sql, values in bounded_queries] == [("older",), ("newer",)]
            assert all("WHERE id=?" in sql and "m.value" not in sql for sql, values in bounded_queries)
            assert "PRAGMA temp_store=MEMORY" in [sql for sql, values in statements]
        statements.clear()
        deny_jsonb = True
        legacy = load_intraday_snapshot({"Local": path}, now=NOW)
        assert not legacy.warnings
        assert not any("AS MATERIALIZED" in sql for sql, values in statements)
        assert fast == legacy
        saved = fast.forecasts[0]
        assert saved["probability_not_down"] == below_half < .5
        assert saved["models"][0]["metadata"]["spec"] == spec
        assert saved["models"][0]["metadata"]["calibration"]["fraction"] == exact_setting
        assert saved["models"][0]["metadata"]["effective_parameters"]["display_status"] == "unavailable"
        assert fast.forecasts[1]["mode"] is None and fast.forecasts[1]["reference_provenance"] is None
        assert forecast_metrics((saved,))["accuracy"] == 0
        assert before == {file: hashlib.sha256(file.read_bytes()).hexdigest() for file in (path, wal)}
