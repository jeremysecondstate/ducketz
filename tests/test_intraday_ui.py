"""Focused offline checks; fixture probabilities are never shipped as UI data."""
from __future__ import annotations

import ast
import hashlib
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.ui.intraday_data import (
    IntradaySnapshot, PACIFIC, error_correlations, filter_forecasts,
    forecast_metrics, latest_matrix, load_intraday_snapshot, model_metrics,
    outcome_status, probability_geometry, regime_metrics, timestamp, window_rows,
)

NOW = datetime(2026, 10, 12, 19, tzinfo=timezone.utc)


def forecast(identity="f1", **changes):
    value = dict(forecast_id=identity, pc="Atlas", symbol="AAPL", cycle_id="cycle1",
                 reference_at="2026-10-12T17:00:00Z", reference_open=100,
                 target_open="2026-10-12T17:30:00Z", target_close="2026-10-12T17:45:00Z",
                 issued_at="2026-10-12T17:15:30Z", data_through="2026-10-12T17:15:00Z",
                 information_available_at="2026-10-12T17:15:20Z",
                 target_version="not-down-test", probability_not_down=.5,
                 configured_count=2, successful_count=1, ensemble_status="incomplete",
                 mode="shadow", availability_basis="observed",
                 models=[dict(name="logistic", probability_not_down=.5, status="success", version="v1"),
                         dict(name="forest", probability_not_down=None, status="failed", error="training failed")],
                 outcome_status="scored", outcome=dict(status="scored", target_not_down=True,
                     target_close_price=100, observed_at="2026-10-12T17:45:10Z"), intents=[])
    value.update(changes)
    return value


def write_store(path, forecasts, outcomes=(), intents=(), cycles=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE forecasts(id TEXT PRIMARY KEY,pc TEXT,symbol TEXT,cycle_id TEXT,reference_at TEXT,issued_at TEXT,payload TEXT);
            CREATE TABLE outcomes(id TEXT PRIMARY KEY,forecast_id TEXT,observed_at TEXT,payload TEXT);
            CREATE TABLE intents(id TEXT PRIMARY KEY,forecast_id TEXT,recorded_at TEXT,payload TEXT);
            CREATE TABLE cycles(id TEXT PRIMARY KEY,pc TEXT,started_at TEXT,payload TEXT);
        """)
        for row in forecasts:
            conn.execute("INSERT INTO forecasts VALUES(?,?,?,?,?,?,?)", (row["forecast_id"], row["pc"], row["symbol"],
                row["cycle_id"], row["reference_at"], row["issued_at"], json.dumps({k: v for k, v in row.items() if k not in ("outcome", "outcome_status", "intents")})))
        for index, (identity, payload) in enumerate(outcomes):
            conn.execute("INSERT INTO outcomes VALUES(?,?,?,?)", (f"o{index}", identity, payload["observed_at"], json.dumps(payload)))
        for index, (identity, payload) in enumerate(intents):
            conn.execute("INSERT INTO intents VALUES(?,?,?,?)", (f"i{index}", identity, "2026-10-12T17:15:31Z", json.dumps(payload)))
        for index, payload in enumerate(cycles):
            conn.execute("INSERT INTO cycles VALUES(?,?,?,?)", (f"c{index}", payload["pc"], payload["started_at"], json.dumps(payload)))


def test_missing_store_is_empty_and_is_never_created(tmp_path):
    path = tmp_path / "intraday" / "shadow.sqlite3"
    result = load_intraday_snapshot({"Local": path}, now=NOW)
    assert result.forecasts == () and result.sources[0]["status"] == "missing"
    assert not path.exists() and not path.parent.exists()


def test_readonly_snapshot_latest_revision_and_zero_capacity(tmp_path):
    path = tmp_path / "shadow.sqlite3"
    row = forecast()
    old = row["outcome"]
    corrected = dict(old, target_not_down=False, target_close_price=99, observed_at="2026-10-12T18:00:00Z")
    write_store(path, [row], [("f1", old), ("f1", corrected)],
                [("f1", dict(intended_direction="BUY", desired_quantity=4, adjusted_quantity=0,
                              disposition="not submitted: no purchasing capacity"))])
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = load_intraday_snapshot({"Local": path}, now=NOW)
    saved = result.forecasts[0]
    assert saved["probability_not_down"] == .5
    assert saved["outcome"]["target_close_price"] == 99 and saved["outcome"]["revision_id"] == "o1"
    assert saved["outcome_status"] == "scored"
    assert saved["intents"][0]["adjusted_quantity"] == 0
    assert forecast_metrics(result.forecasts)["accuracy"] == 0
    assert before == hashlib.sha256(path.read_bytes()).hexdigest()
    assert not result.warnings


def test_completed_scored_row_remains_scored_without_a_newer_forecast(tmp_path):
    row = forecast()
    path = tmp_path / "shadow.sqlite3"
    write_store(path, [row], [("f1", row["outcome"])])
    result = load_intraday_snapshot({"Scout received receipt": path}, now=NOW + timedelta(days=4))
    assert result.forecasts[0]["outcome_status"] == "scored"
    assert result.sources[0]["latest_issue"] == timestamp(row["issued_at"])


def test_pending_missing_unscorable_and_information_availability_are_distinct():
    assert outcome_status(forecast(target_close="2026-10-12T20:00:00Z"), NOW) == "pending"
    assert outcome_status(forecast(outcome={}), NOW) == "missing"
    assert outcome_status(forecast(target_close="bad"), NOW) == "unscorable"
    assert outcome_status(forecast(outcome=dict(status="scored", target_not_down=True)), NOW) == "unscorable"
    unavailable = dict(forecast()["outcome"], available_at="2026-10-12T20:00:00Z")
    assert outcome_status(forecast(outcome=unavailable), NOW) == "pending"
    assert outcome_status(forecast(outcome=dict(status="pending", observed_at="2026-10-12T17:15:30Z")), NOW) == "missing"


def test_headline_includes_confirmed_synthetic_with_visible_breakdown():
    observed = forecast(probability_not_down=.7)
    synthetic = forecast("f2", probability_not_down=.3, outcome_status="synthetic",
                         outcome=dict(status="synthetic", target_not_down=True, target_close_price=100))
    pending = forecast("f3", probability_not_down=.99, outcome_status="pending")
    missing = forecast("f4", probability_not_down=.99, outcome_status="missing")
    values = forecast_metrics([observed, synthetic, pending, missing])
    assert values["issued"] == 4 and values["denominator"] == 2
    assert values["observed_denominator"] == values["synthetic_denominator"] == 1
    assert values["observed_accuracy"] == 1 and values["synthetic_accuracy"] == 0
    assert values["equality_denominator"] == 2 and values["equality_accuracy"] == .5
    assert values["observed_equality_denominator"] == values["synthetic_equality_denominator"] == 1
    assert values["accuracy"] == .5
    assert values["brier"] == pytest.approx((.3**2 + .7**2) / 2)
    assert values["counts"] == dict(scored=1, synthetic=1, pending=1, missing=1, unscorable=0)


def test_exact_probability_equality_balanced_accuracy_and_failed_model_denominator():
    up, down = forecast(), forecast("f2", probability_not_down=.2,
        outcome=dict(target_not_down=False, target_close_price=98))
    values = forecast_metrics([up, down])
    assert values["accuracy"] == values["balanced_accuracy"] == 1
    assert values["brier"] == pytest.approx((.5**2 + .2**2) / 2)
    model_values = model_metrics([up, down])
    assert model_values["forest"]["denominator"] == 0
    assert model_values["forest"]["accuracy"] is None
    assert model_values["logistic"]["denominator"] == 2
    assert forecast_metrics([up])["balanced_accuracy"] is None


def test_calibration_extremes_log_loss_and_disagreement():
    row = forecast(probability_not_down=1, models=[
        dict(name="one", status="success", probability_not_down=.2),
        dict(name="two", status="success", probability_not_down=.8)])
    values = forecast_metrics([row])
    assert values["calibration"][-1]["count"] == 1
    assert values["calibration"][-1]["observed_not_down"] == 1
    assert math.isfinite(values["log_loss"])
    assert values["mean_disagreement"] == pytest.approx(.3)
    assert probability_geometry(.72, 300) == (216, 150)
    assert probability_geometry(None, 300) == (None, 150)
    assert probability_geometry(float("nan"), 300) == (None, 150)


def test_metric_windows_count_issued_forecasts_then_select_scored_denominator():
    rows = [forecast(str(i), issued_at=(NOW-timedelta(minutes=15*(21-i))).isoformat(),
                     outcome_status="pending" if i == 20 else "scored") for i in range(21)]
    latest = window_rows(rows, "Last 20", NOW)
    assert len(latest) == 20 and latest[0]["forecast_id"] == "1"
    assert forecast_metrics(latest)["denominator"] == 19
    assert len(window_rows(rows, "Today", NOW)) == 21
    assert len(window_rows(rows, "7 days", NOW + timedelta(days=8))) == 0


def test_pacific_dst_and_mode_filters_do_not_conflate_replay_with_shadow():
    assert timestamp("2026-11-01T08:30:00Z").astimezone(PACIFIC).strftime("%H:%M %Z") == "01:30 PDT"
    assert timestamp("2026-11-01T09:30:00Z").astimezone(PACIFIC).strftime("%H:%M %Z") == "01:30 PST"
    assert timestamp("2026-10-12T10:00:00") is None
    rows = (forecast(), forecast("f2", pc="Scout", symbol="IONQ", mode="historical_replay", availability_basis="assumed_historical"))
    snapshot = IntradaySnapshot(NOW, rows)
    assert len(filter_forecasts(snapshot)) == 1
    assert len(filter_forecasts(snapshot, mode="All")) == 2
    assert filter_forecasts(snapshot, pc="Scout", mode="historical_replay")[0]["availability_basis"] == "assumed_historical"
    assert len(latest_matrix([*rows, forecast("f3", issued_at="2026-10-12T18:15:30Z")])) == 2


def test_reference_quarter_filter_preserves_exact_forecast_targets():
    first = forecast()
    next_row = forecast("f2", reference_at="2026-10-12T17:15:00Z", issued_at="2026-10-12T17:30:30Z",
                        target_open="2026-10-12T17:45:00Z", target_close="2026-10-12T18:00:00Z")
    snapshot = IntradaySnapshot(NOW, (first, next_row))
    selected = filter_forecasts(snapshot, reference_slot="10:00")
    assert selected == (first,) and selected[0]["target_close"] == "2026-10-12T17:45:00Z"
    assert filter_forecasts(snapshot, reference_slot="10:15") == (next_row,)


def test_source_dataset_filter_keeps_comparisons_and_legacy_provenance_explicit():
    legacy = forecast("legacy")
    eq = forecast("eq", source_dataset="EQUS.MINI", source_id="databento:EQUS.MINI:ohlcv-1m")
    xnas = forecast("xnas", source_dataset="XNAS.ITCH", source_id="databento:XNAS.ITCH:ohlcv-1m",
                    probability_not_down=.3)
    snapshot = IntradaySnapshot(NOW, (legacy, eq, xnas))
    assert len(latest_matrix(snapshot.forecasts)) == 3
    assert filter_forecasts(snapshot, source_dataset="Unrecorded") == (legacy,)
    assert filter_forecasts(snapshot, source_dataset="EQUS.MINI") == (eq,)
    assert forecast_metrics(filter_forecasts(snapshot, source_dataset="EQUS.MINI"))["accuracy"] == 1
    assert forecast_metrics(filter_forecasts(snapshot, source_dataset="XNAS.ITCH"))["accuracy"] == 0
    assert forecast_metrics(filter_forecasts(snapshot, source_dataset="All"))["denominator"] == 3


def test_pairwise_correlations_use_actual_overlaps_and_retrospective_bins():
    rows = [forecast(str(i), reference_open=100, outcome=dict(target_not_down=True, target_close_price=100+i),
        models=[dict(name="a", status="success", probability_not_down=.5+i*.1),
                dict(name="b", status="success", probability_not_down=.6+i*.1)]) for i in range(3)]
    values = error_correlations(rows)
    assert values[0]["count"] == 3 and values[0]["correlation"] == pytest.approx(1)
    groups = regime_metrics(rows)
    assert {row["bucket"] for row in groups if row["dimension"] == "Realized |return|"} == {"Flat ≤0.1%", "Large ≥1%"}
    assert not any(row["dimension"] == "choppiness" for row in groups)


def test_persisted_causal_regime_measurements_get_retrospective_cohort_bins():
    rows = [forecast(str(i), regime_measurements=dict(volatility20_observed=.01*i,
             trend_efficiency=i/4, choppiness20=100-20*i)) for i in range(5)]
    groups = regime_metrics(rows)
    for dimension in ("Causal volatility20", "Causal trend efficiency", "Causal choppiness20"):
        scoped = [row for row in groups if row["dimension"] == dimension]
        assert len(scoped) == 3 and sum(row["denominator"] for row in scoped) == 5
        assert any(row["bucket"].startswith("Low") for row in scoped)
        assert any(row["bucket"].startswith("High") for row in scoped)
    tied = regime_metrics([forecast(str(i), regime_measurements=dict(trend_efficiency=0)) for i in range(3)])
    scoped = [row for row in tied if row["dimension"] == "Causal trend efficiency"]
    assert len(scoped) == 1 and scoped[0]["denominator"] == 3


def test_broken_store_does_not_silently_look_like_empty_results(tmp_path):
    path = tmp_path / "broken.sqlite3"
    path.write_text("not a sqlite database", encoding="utf-8")
    result = load_intraday_snapshot({"Atlas": path}, now=NOW)
    assert result.forecasts == () and result.sources[0]["status"] == "unreadable"
    assert "DatabaseError" in result.warnings[0]


def test_verified_peer_receipt_projects_real_fields_without_private_intentions(tmp_path):
    from test_intraday_telemetry import source_store
    from ml.intraday.telemetry import export_packet, import_packet, write_packet
    store, issued = source_store(tmp_path / "Scout" / "shadow.sqlite3", pc="Scout", source_dataset="XNAS.ITCH")
    packet = export_packet(store.path, producer_pc="Scout", source_version="test-release", produced_at=NOW.isoformat())
    output = write_packet(packet, tmp_path / "Scout" / "packet.json")
    received = import_packet(output, tmp_path / "Atlas" / "receipts", expected_peer="Scout", local_pc="Atlas", received_at=NOW.isoformat())
    path = Path(received["receipt_path"])
    before = path.read_bytes()
    snapshot = load_intraday_snapshot({"Scout received": path}, local_pc="Atlas", now=NOW)
    assert not snapshot.warnings and len(snapshot.forecasts) == 1
    row = snapshot.forecasts[0]
    assert row["forecast_id"] == issued["forecast_id"] and row["probability_not_down"] == .72
    assert row["outcome_status"] == "scored" and row["intents"] == []
    assert row["source_dataset"] == "XNAS.ITCH" and row["source_id"] == "databento:XNAS.ITCH:ohlcv-1m"
    assert filter_forecasts(snapshot, source_dataset="EQUS.MINI") == ()
    assert snapshot.sources[0]["telemetry_status"] == "stale"  # A newly received old forecast stays old.
    assert snapshot.sources[0]["produced_at"] == snapshot.sources[0]["received_at"] == NOW.isoformat()
    assert path.read_bytes() == before
    rejected = load_intraday_snapshot({"Unadmitted receipt": path}, local_pc="Scout", now=NOW)
    assert not rejected.forecasts and rejected.sources[0]["status"] == "unreadable"
    assert rejected.warnings


def test_ui_modules_have_no_broker_provider_thread_or_training_imports():
    for name in ("intraday.py", "intraday_data.py"):
        module = Path(__file__).parents[1] / "app" / "ui" / name
        tree = ast.parse(module.read_text(encoding="utf-8"))
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        imports += [item.name for node in ast.walk(tree) if isinstance(node, ast.Import) for item in node.names]
        assert not any(any(forbidden in item for forbidden in ("broker", "schwab", "hyperliquid", "databento", "threading", "ml."))
                       for item in imports if item != "ml.intraday.telemetry")


@pytest.mark.parametrize("reverse_cycles", [False, True])
def test_operations_displays_newest_received_cycle_independent_of_packet_order(tmp_path, monkeypatch, reverse_cycles):
    from types import SimpleNamespace
    from test_intraday_telemetry import source_store
    from app.ui import intraday
    from ml.intraday.telemetry import export_packet, import_packet, write_packet

    store, _ = source_store(tmp_path / "atlas.sqlite3")
    store.begin_cycle("newest-atlas-cycle", dict(pc="Atlas", reference_at="2026-10-12T17:15:00Z",
        mode="shadow", started_at="2026-10-12T17:30:30Z", status="running"))
    store.update_cycle("newest-atlas-cycle", dict(status="completed", completed_at="2026-10-12T17:31:00Z",
        total_cycle_seconds=30.0, successful_symbols=1, failed_symbols=0))
    packet = export_packet(store.path, producer_pc="Atlas", source_version="reviewed-test-source",
                           produced_at="2026-10-12T17:47:00Z")
    assert packet["payload"]["cycles"][0]["cycle_id"] == "newest-atlas-cycle"
    packet_path = write_packet(packet, tmp_path / "atlas-packet.json")
    received = import_packet(packet_path, tmp_path / "receipts", expected_peer="Atlas", local_pc="Scout",
                             received_at="2026-10-12T17:48:00Z")
    snapshot = load_intraday_snapshot({"Peer": Path(received["receipt_path"])}, now=NOW, local_pc="Scout")
    assert not snapshot.warnings and len(snapshot.cycles) == 2
    if reverse_cycles:
        snapshot = IntradaySnapshot(snapshot.loaded_at, snapshot.forecasts, tuple(reversed(snapshot.cycles)),
                                    snapshot.sources, snapshot.warnings)
    displayed = {}
    monkeypatch.setattr(intraday, "_replace", lambda table, rows: displayed.update({table: rows}))
    view = SimpleNamespace(snapshot=snapshot, pc=SimpleNamespace(get=lambda: "All"),
        source_dataset=SimpleNamespace(get=lambda: "All"),
        mode=SimpleNamespace(get=lambda: "shadow"), operation_status=SimpleNamespace(configure=lambda **_: None),
        cycle_table="cycles", intent_table="intents", visible_rows=())
    intraday.IntradayTab._render_operations(view)
    assert len(displayed["cycles"]) == 1
    values, _ = displayed["cycles"][0]
    assert values[0:2] == ("Atlas", "newest-atlas-cycle")
    assert values[3] == "completed"
    assert json.loads(values[4])["total_cycle_seconds"] == 30.0
    view.source_dataset = SimpleNamespace(get=lambda: "Unrecorded")
    view.visible_rows = snapshot.forecasts
    intraday.IntradayTab._render_operations(view)
    values, _ = displayed["cycles"][0]
    assert values[1] == snapshot.forecasts[0]["cycle_id"]
    assert values[3] == "partial_failure"


def test_tk_consensus_uses_saved_outputs_and_displays_failed_models(tmp_path):
    import tkinter as tk
    from tkinter import ttk
    from app.ui.intraday import IntradayTab
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk unavailable: {exc}")
    root.withdraw()
    try:
        frame = ttk.Frame(root)
        frame.pack(fill="both", expand=True)
        tab = IntradayTab(root, frame, datastore_root=tmp_path, auto_load=False)
        path = tmp_path / "intraday" / "shadow.sqlite3"
        row = forecast()
        write_store(path, [row], [("f1", row["outcome"])], [("f1", dict(intended_direction="BUY", desired_quantity=4,
            adjusted_quantity=0, disposition="not submitted: no purchasing capacity"))])
        tab.set_snapshot(load_intraday_snapshot({"Local": path}, now=NOW))
        root.update()
        assert tab.consensus_value.cget("text") == "50.0% ensemble probability · not-down"
        assert "1/2 enabled models succeeded" in tab.consensus_meta.cget("text")
        assert "incomplete ensemble" in tab.consensus_meta.cget("text")
        assert len(tab.models_frame.winfo_children()) == 2
        texts = [str(child.cget("text")) for frame in tab.models_frame.winfo_children()
                 for child in frame.winfo_children() if isinstance(child, tk.Label)]
        assert "forest" in texts and any("training failed" in text for text in texts)
        assert len(tab.matrix.get_children()) == 1
        assert "no purchasing capacity" in str(tab.intent_table.item(tab.intent_table.get_children()[0], "values"))
        assert tab.model_table.item(tab.model_table.get_children()[0], "values")[4] == "100.0% / 1"
        assert not tab._auto_job
        source_rows = tuple(forecast(dataset, source_dataset=dataset, source_id=f"databento:{dataset}:ohlcv-1m")
                            for dataset in ("EQUS.MINI", "XNAS.ITCH"))
        tab.set_snapshot(IntradaySnapshot(NOW, source_rows))
        assert len(tab.consensus_rows) == 2 and len(tab.matrix.get_children()) == 2
        assert set(tab.boxes["source_dataset"].cget("values")) == {"All", "EQUS.MINI", "XNAS.ITCH"}
        tab.source_dataset.set("XNAS.ITCH")
        tab.render()
        assert len(tab.visible_rows) == 1 and tab.visible_rows[0]["source_dataset"] == "XNAS.ITCH"
        assert "Source dataset XNAS.ITCH · source databento:XNAS.ITCH:ohlcv-1m" in tab.consensus_meta.cget("text")
        assert tab.matrix.item(tab.matrix.get_children()[0], "values")[2] == "XNAS.ITCH"
        assert tab.model_table.item(tab.model_table.get_children()[0], "values")[2] == "1"
    finally:
        root.destroy()
