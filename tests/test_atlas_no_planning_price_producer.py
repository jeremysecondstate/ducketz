"""Offline regressions for Atlas's price-free producer and retained work."""
from copy import deepcopy
from datetime import date
import json

import pandas as pd
import pytest

from ml.artifacts import file_checksum
from ml import gameplan_trade_planning as planning
from ml import nightly_workflow as workflow
from ml.gameplan_execution_policy import is_current_gameplan_recipe
from ml.nightly_gameplan import _finalize_forecasts
from ml.independent_stock_targets import stock_target_windows
from test_gameplan_trade_planning import publication_case


@pytest.mark.parametrize("research_only", [False, True])
def test_current_atlas_producer_never_loads_or_generates_planning_prices(publication_case, monkeypatch, research_only):
    from ml import gameplan_cash_ledger, gameplan_price_bands, gameplan_trade_snapshot
    from ml.stock_trader import independent_signals
    case = publication_case
    frame = pd.read_parquet(case.source / "forecasts.parquet")
    frame = frame.assign(calibrated_probability=.5001, raw_probability=.4999,
                         direction="NO_EDGE", model_status="RESEARCH_NO_TARGET_HISTORY")
    frame.to_parquet(case.source / "forecasts.parquet", index=False)
    before = {path: path.read_bytes() for path in case.source.iterdir()}

    def forbidden(*args, **kwargs):
        pytest.fail("Current production must not calculate prices, projected capacity or a model gate")

    monkeypatch.setattr(gameplan_price_bands, "build_entry_price_bands", forbidden)
    monkeypatch.setattr(gameplan_price_bands, "build_planning_price_path", forbidden)
    monkeypatch.setattr(gameplan_cash_ledger, "project_direction_trades", forbidden)
    monkeypatch.setattr(independent_signals, "verified_promoted_model_groups", forbidden)
    monkeypatch.setattr(gameplan_trade_snapshot, "capture_trade_planning_snapshot", forbidden)
    kwargs = {"research_producer_only": True} if research_only else {}
    run = planning.publish_trade_plan(case.root, gameplan_run=case.source,
        price_loader=forbidden, clock=case.clock, **kwargs)
    rows = pd.read_parquet(run / "trade-plan.parquet")
    receipt = json.loads((run / "receipt.json").read_text())
    manifest = json.loads((run / "manifest.json").read_text())
    assert is_current_gameplan_recipe(receipt)
    assert is_current_gameplan_recipe(manifest["configuration"])
    assert receipt["orders_placed"] == 0 and receipt["broker_orders_enabled"] is False
    assert rows.calibrated_probability.eq(.5001).all() and rows.raw_probability.eq(.4999).all()
    assert rows.source_saved_direction.eq("NO_EDGE").all()
    assert rows.loc[rows.execution_eligible, "direction_based_action"].eq("BUY").all()
    assert rows.loc[rows.execution_eligible, "direction_based_trade_quantity"].isna().all()
    assert not any(name.startswith(("projected_", "planning_price_", "trade_price_", "scheduled_trade_"))
                   for name in rows.columns)
    assert not {"planning-price-path.json", "price-bands.json", "synthetic-reference-bars.parquet"} & set(manifest["output_files"])
    assert not list(run.glob("*price*.json"))
    assert not case.calls and all(path.read_bytes() == raw for path, raw in before.items())


def test_current_refresh_keeps_original_artifacts_and_records_ancestry_without_price_reads(publication_case):
    case = publication_case
    first = planning.publish_trade_plan(case.root, gameplan_run=case.source,
        snapshot_loader=lambda *a, **k: {"status": "UNAVAILABLE"}, clock=case.clock)
    # An old immutable estimate can remain beside the original publication;
    # it is neither a required input nor a source for the new publication.
    old_estimate = first / "planning-price-path.json"
    old_estimate.write_bytes(b"historical bytes; not a current JSON input")
    before = {path: path.read_bytes() for path in first.iterdir()}
    source_before = (case.source / "forecasts.parquet").read_bytes()
    second = planning.publish_trade_plan(case.root, gameplan_run=case.source,
        refresh_plan=first, clock=lambda: pd.Timestamp("2026-09-09T14:00Z"))
    report = json.loads((second / "report.json").read_text())
    manifest = json.loads((second / "manifest.json").read_text())
    assert report["review_refresh"]["base_run"] == first.relative_to(case.root).as_posix()
    assert report["review_refresh"]["base_receipt_sha256"] == file_checksum(first / "receipt.json")
    assert report["review_refresh"]["source_forecasts_unchanged"] is True
    assert len(manifest["input_files"]) == 6
    assert not (second / "planning-price-path.json").exists()
    assert source_before == (case.source / "forecasts.parquet").read_bytes()
    assert all(path.read_bytes() == raw for path, raw in before.items())


def test_current_direction_production_keeps_continuation_after_target_time(publication_case, monkeypatch):
    case = publication_case
    monkeypatch.setenv("DUCKETZ_NIGHTLY_ACTION_DATE", "2026-09-09")
    run = planning.publish_trade_plan(case.root, gameplan_run=case.source,
        research_producer_only=True, clock=lambda: pd.Timestamp("2026-09-10T04:00Z"))
    report = json.loads((run / "report.json").read_text())
    assert report["status"] == "COMPLETE" and report["action_date"] == "2026-09-09"
    assert report["deadline_at"] == "2026-09-09T11:00:00+00:00"
    assert not case.calls and not (run / "planning-price-path.json").exists()


def test_new_forecast_directions_do_not_use_model_assessment_as_a_second_gate():
    day = date(2026, 10, 20)
    windows = stock_target_windows(day)
    probabilities = [.5001, .4999, .5, .50005, .49995, .7] * 4
    rows = pd.DataFrame([{**window, "symbol": "AAPL", "calibrated_probability": probability,
        "raw_probability": probability, "model_status": "RESEARCH_NO_TARGET_HISTORY",
        "probability_target_contract": "raw-price-direction-v1"}
        for window, probability in zip(windows, probabilities)])
    original = rows.copy(deep=True)
    result = _finalize_forecasts(rows, symbols=("AAPL",), action_date=day,
        frozen_at=pd.Timestamp("2026-10-20T05:00Z"), action_start=pd.Timestamp("2026-10-20T11:00Z"),
        action_end=pd.Timestamp("2026-10-21T00:00Z"), opra_freshness=None,
        target_contract_version="independent-stock-targets-v1")
    assert result.loc[result.calibrated_probability.eq(.5001), "direction"].eq("BULLISH").all()
    assert result.loc[result.calibrated_probability.eq(.4999), "direction"].eq("BEARISH").all()
    assert result.loc[result.calibrated_probability.between(.4999, .5001, inclusive="neither"), "direction"].eq("NO_EDGE").all()
    assert result.calibrated_probability.tolist() == probabilities
    assert result.raw_probability.tolist() == probabilities
    assert result.probability_target_contract.eq("raw-price-direction-v1").all()
    pd.testing.assert_frame_equal(rows, original)


def test_recipe_refresh_preserves_completed_data_models_and_prior_workflow_bytes(tmp_path, monkeypatch):
    from ml.nightly_dispatch import LAYOUT
    monkeypatch.setattr(workflow, "_verify_configuration_binding", lambda *a: None)
    monkeypatch.setattr(workflow, "_verify_symbol_binding", lambda *a: None)
    artifact = tmp_path / "completed-model.bin"
    artifact.write_bytes(b"original completed fitted-model bytes")
    state = {"action_date": "2026-10-20", "status": "LOCAL_COMPLETE_PEER_SETUP_PENDING",
        "workflow_layout": LAYOUT, "run_id": "same-native-identity", "steps": {
            "datastore_catchup": {"status": "COMPLETE", "output": {"files": {}}},
            "prepare_stats": {"status": "COMPLETE", "output": {"files": {}}},
            "model_review": {"status": "COMPLETE", "output": {"files": {}}},
            "train_and_plan": {"status": "COMPLETE", "output": {"files": {str(artifact): file_checksum(artifact)}}},
            "local_gameplan": {"status": "COMPLETE", "output": {"files": {}}},
            "verify_display": {"status": "COMPLETE", "output": {"files": {}}},
            "local_handoff": {"status": "COMPLETE", "output": {"files": {}}}}}
    preserved = deepcopy(state["steps"])
    path = tmp_path / "state.json"
    workflow._write(path, state)
    before = path.read_bytes()
    assert workflow._refresh_planning_recipe({}, state, path, pd.Timestamp("2026-10-20T05:00Z"),
        lambda: workflow._write(path, state))
    assert state["run_id"] == "same-native-identity"
    for step in ("datastore_catchup", "prepare_stats", "model_review", "train_and_plan"):
        assert state["steps"][step] == preserved[step]
    assert state["recipe_transitions"][0]["reset_steps"] == ["local_gameplan", "verify_display", "local_handoff"]
    assert artifact.read_bytes() == b"original completed fitted-model bytes"
    assert (tmp_path / "recipe-history" / state["recipe_transitions"][0]["id"] / "state.json").read_bytes() == before
    assert is_current_gameplan_recipe(state)



def test_current_local_publication_honors_explicit_context_without_ownership_gate(publication_case, monkeypatch):
    from ml import gameplan_trade_snapshot
    case = publication_case
    frame = pd.read_parquet(case.source / "forecasts.parquet").assign(
        calibrated_probability=.48, raw_probability=.471234,
        direction="NO_EDGE", model_status="RESEARCH_NO_TARGET_HISTORY")
    frame.to_parquet(case.source / "forecasts.parquet", index=False)
    original = {path: path.read_bytes() for path in case.source.iterdir()}
    observed = deepcopy(case.state)
    observed["ownership"] = {"status": "REVIEW_REQUIRED", "safe_for_planning": False,
        "reason": "Historical ledger diagnostics are preserved"}
    calls = []
    def explicit_context(root, *, symbols):
        calls.append((root, symbols))
        return observed
    def forbidden(*a, **k):
        pytest.fail("Local direction publication never performs implicit legacy account capture")
    monkeypatch.setattr(gameplan_trade_snapshot, "capture_trade_planning_snapshot", forbidden)
    run = planning.publish_trade_plan(case.root, gameplan_run=case.source,
        snapshot_loader=explicit_context, clock=case.clock)
    report = json.loads((run / "report.json").read_text())
    rows = pd.read_parquet(run / "trade-plan.parquet")
    assert calls == [(case.root.resolve(), ("AAPL",))]
    assert report["snapshot"] == observed and report["snapshot_status"] == "OBSERVED_CONTEXT_ONLY"
    assert json.loads((run / "account-snapshot.json").read_text()) == observed
    assert rows.calibrated_probability.eq(.48).all() and rows.raw_probability.eq(.471234).all()
    assert rows.source_saved_direction.eq("NO_EDGE").all()
    assert rows.loc[rows.execution_eligible, "direction_based_action"].eq("SELL").all()
    assert rows.loc[rows.execution_eligible, "direction_based_trade_quantity"].isna().all()
    assert all(path.read_bytes() == raw for path, raw in original.items())
