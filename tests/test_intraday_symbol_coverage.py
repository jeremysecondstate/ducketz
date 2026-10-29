"""Saved cycle membership must remain visible when no forecast was produced."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from app.ui.intraday_data import IntradaySnapshot, forecast_metrics, symbol_coverage


NOW = datetime(2026, 10, 17, 10, tzinfo=timezone.utc)


def saved_forecast(symbol="ABCL", **changes):
    row = dict(forecast_id="forecast-" + symbol, pc="Scout", symbol=symbol, cycle_id="cycle-1",
        source_label="Local", source_dataset="EQUS.MINI", mode="live",
        reference_at="2026-10-16T19:30:00Z", issued_at="2026-10-16T19:45:30Z",
        target_open="2026-10-16T20:00:00Z", target_close="2026-10-16T20:15:00Z",
        probability_not_down=.7, configured_count=2, successful_count=2, ensemble_status="full",
        outcome_status="scored", outcome=dict(status="scored", target_not_down=True, target_close_price=10),
        models=[], intents=[])
    return {**row, **changes}


def saved_cycle(**changes):
    row = dict(id="cycle-1", pc="Scout", source_label="Local", mode="live",
        reference_at="2026-10-16T19:30:00Z", started_at="2026-10-16T19:45:20Z",
        completed_at="2026-10-16T19:46:00Z", status="partial_failure",
        configuration=dict(source_dataset="EQUS.MINI"), symbols=["ABCL", "AVAV"],
        failures=dict(AVAV=dict(type="AcquisitionPending", message="EQUS.MINI reference is synthetic",
            recorded_at="2026-10-16T19:45:40Z")))
    return {**row, **changes}


def test_missing_symbol_is_display_evidence_only_and_retains_source_specific_reason():
    row, cycle = saved_forecast(), saved_cycle()
    snapshot = IntradaySnapshot(NOW, (row,), (cycle,))
    before = deepcopy(snapshot)
    displayed = symbol_coverage(snapshot, mode="live", session="2026-10-16")
    assert [item["symbol"] for item in displayed] == ["ABCL", "AVAV"]
    assert displayed[0]["forecast"] is row
    missing = displayed[1]
    assert missing["forecast"] is None and missing["coverage_status"] == "failed"
    assert missing["coverage_reason"] == "AcquisitionPending: EQUS.MINI reference is synthetic"
    assert missing["evidence_at"] == "2026-10-16T19:45:40Z"
    assert snapshot == before and len(snapshot.forecasts) == 1
    assert forecast_metrics(snapshot.forecasts)["issued"] == 1
    assert forecast_metrics(snapshot.forecasts)["denominator"] == 1


@pytest.mark.parametrize("filters", [
    {"pc": "Atlas"}, {"symbol": "BEAM"}, {"session": "2026-10-15"},
    {"mode": "shadow"}, {"source_dataset": "XNAS.ITCH"}, {"reference_slot": "12:45"},
])
def test_missing_evidence_obeys_every_filter_even_without_any_forecasts(filters):
    snapshot = IntradaySnapshot(NOW, (), (saved_cycle(),))
    assert symbol_coverage(snapshot, **{**dict(mode="live"), **filters}) == ()
    rows = symbol_coverage(snapshot, pc="Scout", symbol="AVAV", mode="live", session="2026-10-16",
        source_dataset="EQUS.MINI", reference_slot="12:30")
    assert len(rows) == 1 and rows[0]["forecast"] is None


def test_later_failure_preserves_prior_saved_forecast_and_recovery_clears_stale_failure():
    row = saved_forecast("AVAV", cycle_id="cycle-older", reference_at="2026-10-16T19:15:00Z",
        issued_at="2026-10-16T19:30:30Z")
    snapshot = IntradaySnapshot(NOW, (row,), (saved_cycle(),))
    display = next(item for item in symbol_coverage(snapshot, mode="live") if item["symbol"] == "AVAV")
    assert display["forecast"] is row and display["forecast"]["probability_not_down"] == .7
    assert display["coverage_status"] == "failed"
    recovered = saved_forecast("AVAV")
    snapshot = IntradaySnapshot(NOW, (row, recovered), (saved_cycle(),))
    display = next(item for item in symbol_coverage(snapshot, mode="live") if item["symbol"] == "AVAV")
    assert display["forecast"] is recovered
    assert display["coverage_reason"] == "" and display["coverage_status"] == "saved"


def test_newest_cycle_is_selected_by_saved_time_and_old_failures_do_not_mask_newer_success():
    row = saved_forecast("AVAV")
    older = saved_cycle(id="older", reference_at="2026-10-16T19:15:00Z",
        started_at="2026-10-16T19:30:20Z")
    for cycles in ((saved_cycle(), older), (older, saved_cycle())):
        display = next(item for item in symbol_coverage(IntradaySnapshot(NOW, (row,), cycles), mode="live")
            if item["symbol"] == "AVAV")
        assert display["forecast"] is row and display["coverage_reason"] == ""


def test_peer_failure_scope_does_not_invent_membership_or_dataset_or_failure_detail():
    peer = saved_cycle(pc="Atlas", source_label="Peer", symbols=None, configuration=None, failures=None,
        failed_symbol_names=["AAPL"])
    snapshot = IntradaySnapshot(NOW, (), (peer,))
    displayed = symbol_coverage(snapshot, mode="live")
    assert len(displayed) == 1 and displayed[0]["symbol"] == "AAPL"
    assert displayed[0]["source_dataset"] == "Unrecorded"
    assert displayed[0]["coverage_reason"] == "Saved cycle reports a failure; failure details unavailable."
    assert symbol_coverage(snapshot, mode="live", source_dataset="EQUS.MINI") == ()
    # A same-cycle issue provides dataset evidence, unlike another cycle's issue.
    row = saved_forecast("AMZN", pc="Atlas", source_label="Peer")
    snapshot = IntradaySnapshot(NOW, (row,), (peer,))
    displayed = symbol_coverage(snapshot, mode="live", source_dataset="EQUS.MINI")
    assert {item["symbol"] for item in displayed} == {"AAPL", "AMZN"}
    other = saved_forecast("AMZN", pc="Atlas", source_label="Peer", cycle_id="other")
    snapshot = IntradaySnapshot(NOW, (other,), (peer,))
    assert {item["symbol"] for item in symbol_coverage(snapshot, mode="live", source_dataset="EQUS.MINI")} == {"AMZN"}


@pytest.mark.parametrize("include_success_cycle", [True, False])
def test_peer_older_same_reference_failure_cannot_annotate_newer_success(include_success_cycle):
    failed = saved_cycle(symbols=None, configuration=None, failures=None, failed_symbol_names=["AVAV"],
        source_label="Peer")
    success = saved_cycle(id="new-cycle", symbols=None, configuration=None, failures=None,
        failed_symbol_names=[], source_label="Peer", status="completed",
        started_at="2026-10-16T19:49:00Z", completed_at="2026-10-16T19:50:00Z")
    sibling = saved_forecast("ABCL", source_label="Peer")
    latest = saved_forecast("AVAV", source_label="Peer", cycle_id="new-cycle", issued_at="2026-10-16T19:50:00Z")
    snapshot = IntradaySnapshot(NOW, (sibling, latest), (failed, success) if include_success_cycle else (failed,))
    display = next(item for item in symbol_coverage(snapshot, mode="live") if item["symbol"] == "AVAV")
    assert display["forecast"] is latest and display["coverage_reason"] == ""
    assert display["coverage_status"] == "saved"


def test_running_missing_issue_is_unknown_training_and_has_no_fabricated_numbers():
    cycle = saved_cycle(status="running", symbols=["AVAV"], failures={})
    display, = symbol_coverage(IntradaySnapshot(NOW, (), (cycle,)), mode="live")
    assert display["forecast"] is None and display["coverage_status"] == "pending"
    assert "running" in display["coverage_reason"]
    assert "probability_not_down" not in display and "successful_count" not in display


def test_failure_display_is_bounded_without_mutating_saved_diagnostics():
    message = "Source-specific reference is synthetic.\n" + "diagnostics " * 500
    cycle = saved_cycle(failures={"AVAV": {"type": "AcquisitionPending", "message": message}})
    display = next(row for row in symbol_coverage(IntradaySnapshot(NOW, (), (cycle,)), mode="live")
        if row["symbol"] == "AVAV")
    assert len(display["coverage_reason"]) < 470 and display["coverage_reason"].endswith("…")
    assert "\n" not in display["coverage_reason"]
    assert cycle["failures"]["AVAV"]["message"] == message


def test_tk_matrix_consensus_and_selectors_show_all_recorded_symbols_without_changing_scores(tmp_path):
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
        tab = IntradayTab(root, frame, datastore_root=tmp_path, local_pc="Scout", auto_load=False)
        tab.set_snapshot(IntradaySnapshot(NOW, (saved_forecast(),), (saved_cycle(),)))
        root.update()
        assert set(tab.boxes["symbol"].cget("values")) == {"All", "ABCL", "AVAV"}
        assert "2026-10-16" in tab.boxes["session"].cget("values")
        assert len(tab.matrix.get_children()) == 2 and len(tab.visible_rows) == 1
        missing = tab.matrix.item(tab.matrix.get_children()[1], "values")
        assert missing[1] == "AVAV" and missing[3] == "—" and missing[5] == "—"
        assert "EQUS.MINI reference is synthetic" in missing[-2]
        assert "1 with saved forecasts · 1 without" in tab.metric_status.cget("text")
        tab.consensus_choice.set(next(key for key in tab.consensus_rows if "AVAV" in key))
        tab._render_consensus()
        assert tab.consensus_value.cget("text") == "No saved forecast for this symbol"
        assert "Oct 16 12:45:40 PDT" in tab.consensus_context.cget("text")
        assert "market-wide" in tab.consensus_meta.cget("text")
        assert len(tab.models_frame.winfo_children()) == 0
        assert tab.model_table.item(tab.model_table.get_children()[0], "values")[2] == "1"
        # An all-failed cycle remains discoverable and selectable.
        tab.set_snapshot(IntradaySnapshot(NOW, (), (saved_cycle(),)))
        assert len(tab.matrix.get_children()) == 2 and len(tab.visible_rows) == 0
        tab.symbol.set("AVAV")
        tab.session.set("2026-10-16")
        tab.source_dataset.set("EQUS.MINI")
        tab.reference_slot.set("12:30")
        tab.render()
        assert len(tab.matrix.get_children()) == 1
        assert tab.consensus_value.cget("text") == "No saved forecast for this symbol"
        # Existing saved probabilities survive a subsequent failed attempt.
        older = saved_forecast("AVAV", cycle_id="older", reference_at="2026-10-16T19:30:00Z",
            issued_at="2026-10-16T19:45:00Z")
        tab.set_snapshot(IntradaySnapshot(NOW, (older,), (saved_cycle(),)))
        assert tab.consensus_value.cget("text") == "70.0% ensemble probability · not-down"
        assert "EQUS.MINI reference is synthetic" in tab.consensus_context.cget("text")
    finally:
        root.destroy()



def test_sqlite_cycle_projection_retains_missing_membership_and_source_without_science(tmp_path):
    from app.ui.intraday_data import load_intraday_snapshot
    from test_intraday_ui import write_store
    path = tmp_path / "saved.sqlite3"
    cycle = saved_cycle()
    cycle["configuration"]["unused_scientific_tree"] = "DO_NOT_LOAD_CYCLE_SCIENCE" * 10000
    write_store(path, [], cycles=[cycle])
    snapshot = load_intraday_snapshot({"Local": path}, now=NOW)
    assert not snapshot.warnings
    rows = symbol_coverage(snapshot, mode="live", source_dataset="EQUS.MINI", reference_slot="12:30")
    assert {row["symbol"] for row in rows} == {"ABCL", "AVAV"}
    assert all(row["forecast"] is None for row in rows)
    assert snapshot.cycles[0]["configuration"] == {"source_dataset": "EQUS.MINI"}
    assert snapshot.cycles[0]["reference_at"] == cycle["reference_at"]


def test_compact_sqlite_projection_retains_original_settings_and_historical_context(tmp_path, monkeypatch):
    import hashlib
    import json
    from app.ui import intraday_data
    from test_intraday_ui import forecast, write_store
    path = tmp_path / "saved.sqlite3"
    saved_spec = dict(name="local-original", family="logistic", parameters={"C": .125, "solver": "lbfgs"},
        random_state=42, probability_calibration="purged_chronological_sigmoid", training_max_rows=321,
        calibration_fraction=.25)
    giant = {"unused_training_matrix": "DO_NOT_LOAD_ORIGINAL_SCIENCE" * 10000}
    original_metadata = dict(spec=saved_spec, effective_parameters={"C": .125, "solver": "lbfgs"},
        calibration={"method": "purged_chronological_sigmoid", "base_rows": 100},
        feature_names=["return1", "volume"], dataset_metadata=giant)
    row = forecast("original-settings", mode="live", dataset_manifest={**giant, "synthetic_policy": "exclude",
        "manual_model_recipe": {"schema_version": "intraday-manual-observed-calibrated-v2", **giant}},
        model_selection={"selection": "manual_observed_only", "selection_id": "saved-selection", **giant},
        models=[dict(name="local-original", status="success", probability_not_down=.7,
                     metadata=original_metadata)])
    selection_spec = dict(saved_spec, name="native-atlas", probability_calibration="none")
    native = forecast("native-settings", mode="live", model_selection={"selection": "supervised_policy",
        "selection_id": "frozen-native", "active_policy_sha256": "saved-policy", "models": [selection_spec], **giant},
        models=[dict(name="native-atlas", family="logistic", status="success", probability_not_down=.6)])
    write_store(path, [row, native])
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    original_loads = json.loads
    def bounded_loads(raw, *args, **kwargs):
        assert "DO_NOT_LOAD_ORIGINAL_SCIENCE" not in raw
        return original_loads(raw, *args, **kwargs)
    monkeypatch.setattr(intraday_data.json, "loads", bounded_loads)
    snapshot = intraday_data.load_intraday_snapshot({"Local": path}, now=NOW)
    assert not snapshot.warnings
    by_id = {row["forecast_id"]: row for row in snapshot.forecasts}
    projected = by_id["original-settings"]
    assert projected["models"][0]["metadata"] == {key: value for key, value in original_metadata.items()
                                                 if key != "dataset_metadata"}
    context = intraday_data.saved_forecast_context(projected, now=NOW)
    assert "manual observed-only recipe" in context and "synthetic labels exclude" in context
    assert "calibration purged chronological sigmoid" in context
    native_model = by_id["native-settings"]["models"][0]
    assert native_model["metadata"]["spec"] == selection_spec
    assert native_model["saved_setting_source"] == "forecast.model_selection.models"
    assert by_id["native-settings"]["model_selection"]["active_policy_sha256"] == "saved-policy"
    assert "calibration none" in intraday_data.saved_forecast_context(by_id["native-settings"], now=NOW)
    assert len(json.dumps(snapshot.forecasts)) < 10000
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_oversized_saved_setting_is_explicit_and_wal_backed_results_remain_read_only(tmp_path):
    import hashlib
    import json
    import sqlite3
    from app.ui.intraday_data import load_intraday_snapshot, MAX_SAVED_SETTING_CHARS
    from test_intraday_ui import forecast, write_store
    path = tmp_path / "active.sqlite3"
    row = forecast(mode="live", probability_not_down=.51,
        models=[dict(name="saved", status="success", probability_not_down=.51,
                     metadata={"effective_parameters": {"oversized": "x" * (MAX_SAVED_SETTING_CHARS + 1)}})])
    write_store(path, [row], [(row["forecast_id"], row["outcome"])])
    # Keep the writer open so committed evidence remains in the WAL.
    with sqlite3.connect(path) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        row["probability_not_down"] = .77
        writer.execute("UPDATE forecasts SET payload=? WHERE id=?", (json.dumps({key: value for key, value in row.items()
            if key not in ("outcome", "outcome_status", "intents")}), row["forecast_id"]))
        writer.commit()
        wal = path.with_name(path.name + "-wal")
        assert wal.is_file() and wal.stat().st_size > 0
        before = {file.name: hashlib.sha256(file.read_bytes()).hexdigest() for file in (path, wal)}
        snapshot = load_intraday_snapshot({"Local": path}, now=NOW)
        assert not snapshot.warnings
        saved = snapshot.forecasts[0]
        assert saved["probability_not_down"] == .77
        omitted = saved["models"][0]["metadata"]["effective_parameters"]
        assert omitted["display_status"] == "unavailable" and "original retained" in omitted["reason"]
        assert before == {file.name: hashlib.sha256(file.read_bytes()).hexdigest() for file in (path, wal)}



def test_sqlite_projection_preserves_exact_probability_boundary_timings_and_setting_floats(tmp_path):
    import hashlib
    import math
    from app.ui.intraday_data import load_intraday_snapshot, saved_forecast_context
    from test_intraday_ui import forecast, write_store
    path = tmp_path / "precise.sqlite3"
    below_half = math.nextafter(.5, 0)
    precise_seconds = math.nextafter(1.0, 0)
    precise_setting = math.nextafter(.25, 0)
    spec = dict(name="native", family="logistic", parameters={"C": precise_setting},
                calibration_fraction=precise_setting, probability_calibration="none")
    row = forecast("precise", mode="live", probability_not_down=below_half,
        training_seconds=precise_seconds, inference_seconds=precise_seconds,
        model_selection={"selection": "saved_native", "models": [spec]},
        models=[dict(name="native", status="success", probability_not_down=below_half,
                     training_seconds=precise_seconds, inference_seconds=precise_seconds),
                dict(name="metadata", status="success", probability_not_down=below_half,
                     metadata={"spec": dict(spec, name="metadata"),
                               "effective_parameters": {"C": precise_setting},
                               "calibration": {"fraction": precise_setting}})])
    write_store(path, [row], [(row["forecast_id"], row["outcome"])])
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    snapshot = load_intraday_snapshot({"Local": path}, now=NOW)
    assert not snapshot.warnings
    saved = snapshot.forecasts[0]
    assert saved["probability_not_down"] == below_half < .5
    assert saved["training_seconds"] == saved["inference_seconds"] == precise_seconds
    # SQLite's default REAL-to-JSON formatting rounds the original below-half
    # value to .5 and would incorrectly count this observed not-down target.
    assert forecast_metrics(snapshot.forecasts)["accuracy"] == 0
    for model in saved["models"]:
        assert model["probability_not_down"] == below_half
        assert model["metadata"]["spec"]["parameters"]["C"] == precise_setting
        assert model["metadata"]["spec"]["calibration_fraction"] == precise_setting
    native, metadata = saved["models"]
    assert native["training_seconds"] == native["inference_seconds"] == precise_seconds
    assert metadata["metadata"]["effective_parameters"]["C"] == precise_setting
    assert metadata["metadata"]["calibration"]["fraction"] == precise_setting
    assert "calibration none" in saved_forecast_context(saved, now=NOW)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
