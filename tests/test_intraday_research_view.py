"""Offline saved-report fixtures; no model fitting or private state is read."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import time

import pytest

from app.ui import intraday_models
from app.ui.intraday_models import load_model_research, research_view


OLDER = "20261014T010000Z-111111111111.json"
NEWER = "20261014T020000Z-222222222222.json"
SELECTED = (
    "mlp_two_layers", "logistic_strong", "hist_boost_shallow", "random_forest_regularized",
)
NEURAL_FAMILIES = (
    "cnn_1d", "lstm_attention", "bidirectional_gru_attention",
    "stacked_bidirectional_lstm_attention_rnn", "attn", "affn", "csann", "dc_lstm",
    "garn", "tcn", "tft", "transformer", "transformer_a", "transformer_b",
)


@pytest.fixture
def legacy_report():
    """Pre-catalog v1 semantics: 21 results, four selected portable specs.

    Candidate status ``supported`` was distinct from selection; a supported
    variant could lose ranking or family deduplication under the old policy.
    The final holdout reported scores after that selection and did not alter it.
    """
    names_and_families = (
        ("mlp_two_layers", "mlp"), ("logistic_strong", "logistic"),
        ("hist_boost_shallow", "hist_gradient_boosting"),
        ("random_forest_regularized", "random_forest"),
        ("mlp_single_layer", "mlp"), ("gaussian_nb_smooth", "gaussian_nb"),
        ("knn_uniform", "knn"), ("lda_shrunk", "lda"),
        ("extra_trees_shallow", "extra_trees"),
        ("gradient_boost_shallow", "gradient_boosting"),
        ("logistic_moderate", "logistic"),
        ("gaussian_nb_regularized", "gaussian_nb"),
        ("random_forest_shallow", "random_forest"), ("mlp_compact", "mlp"),
        ("knn_distance", "knn"), ("lda_auto", "lda"),
        ("extra_trees_regularized", "extra_trees"),
        ("hist_boost_regularized", "hist_gradient_boosting"),
        ("gradient_boost_regularized", "gradient_boosting"),
        ("logistic_compact", "logistic"), ("mlp_regularized", "mlp"),
    )
    candidates = []
    for name, family in names_and_families:
        candidates.append({
            "spec": {"name": name, "family": family, "parameters": {}},
            "status": "supported", "rejection_reasons": [], "seconds": 0.125,
            "summary": {"mean_brier": 0.241, "mean_balanced_accuracy": 0.521,
                        "mean_baseline_brier_improvement": 0.003},
        })
    candidates[5]["status"] = "rejected"
    candidates[5]["rejection_reasons"] = ["training_validation_brier_gap"]
    candidates[6].update(status="failed", error={"type": "ValueError", "message": "fixture fitting error"})
    candidates[6].pop("summary")
    return {
        "schema_version": "intraday-model-supervision-v1", "actor": "Atlas",
        "source_dataset": "EQUS.MINI", "started_at": "2026-10-14T02:00:00+00:00",
        "symbols": {"AAPL": {"status": "selected", "selection": {
            "candidate_count": 21, "candidates": candidates,
            "selected_models": [deepcopy(row["spec"]) for row in candidates[:4]],
            "holdout": {"models": [
                {"name": name, "metrics": {"brier": 0.255 + index * 0.001}}
                for index, name in enumerate(SELECTED)
            ]},
        }}},
    }


def write_report(directory, name, report):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def test_missing_research_path_is_not_created(tmp_path):
    directory = tmp_path / "missing" / "reports"
    assert load_model_research(directory, actor="Atlas")["status"] == "missing"
    assert not directory.exists() and not directory.parent.exists()


@pytest.mark.parametrize("directory,actor", [(None, "Atlas"), ("irrelevant", None), ("irrelevant", "Other")])
def test_missing_local_identity_is_unavailable(directory, actor):
    assert load_model_research(directory, actor=actor)["status"] == "unavailable"


def test_loader_ignores_unrecognized_filenames_and_preserves_report_bytes(tmp_path, legacy_report):
    directory = tmp_path / "reports"
    path = write_report(directory, OLDER, legacy_report)
    before = path.read_bytes()
    (directory / "latest.json").write_text("{corrupt", encoding="utf-8")
    (directory / "20261014T030000Z-not-a-run.json").write_text("{corrupt", encoding="utf-8")
    saved = load_model_research(directory, actor="Atlas")
    assert saved["status"] == "loaded"
    assert saved["report"] == legacy_report
    assert path.read_bytes() == before


def test_newest_report_is_selected_by_run_filename(tmp_path, legacy_report):
    write_report(tmp_path, OLDER, legacy_report)
    newest = deepcopy(legacy_report)
    newest["started_at"] = "2026-10-14T02:10:00+00:00"
    write_report(tmp_path, NEWER, newest)
    assert load_model_research(tmp_path, actor="Atlas")["report"] == newest


@pytest.mark.parametrize("payload", [b"{corrupt", b"[]", b"null", b"\xff"])
def test_corrupt_newest_is_error_without_older_report_fallback(tmp_path, legacy_report, payload):
    write_report(tmp_path, OLDER, legacy_report)
    (tmp_path / NEWER).write_bytes(payload)
    saved = load_model_research(tmp_path, actor="Atlas")
    assert saved["status"] == "error"
    assert "report" not in saved
    assert research_view(saved, pc="Atlas", symbol="AAPL")["rows"] == ()


@pytest.mark.parametrize("field,value", [
    ("actor", "Scout"), ("schema_version", "other-version"), ("symbols", []),
])
def test_newest_report_identity_or_shape_error_does_not_fallback(tmp_path, legacy_report, field, value):
    write_report(tmp_path, OLDER, legacy_report)
    malformed = deepcopy(legacy_report)
    malformed[field] = value
    write_report(tmp_path, NEWER, malformed)
    assert load_model_research(tmp_path, actor="Atlas")["status"] == "error"


@pytest.mark.parametrize("mutation", [
    "symbol_item", "selection", "candidate_list", "candidate_item", "candidate_summary",
    "selected_list", "holdout", "catalog", "catalog_item", "coverage",
])
def test_malformed_nested_report_is_reported_without_projection_crash(tmp_path, legacy_report, mutation):
    report = deepcopy(legacy_report)
    selection = report["symbols"]["AAPL"]["selection"]
    if mutation == "symbol_item":
        report["symbols"]["AAPL"] = []
    elif mutation == "selection":
        report["symbols"]["AAPL"]["selection"] = []
    elif mutation == "candidate_list":
        selection["candidates"] = {}
    elif mutation == "candidate_item":
        selection["candidates"][0] = None
    elif mutation == "candidate_summary":
        selection["candidates"][0]["summary"] = []
    elif mutation == "selected_list":
        selection["selected_models"] = "not model specifications"
    elif mutation == "holdout":
        selection["holdout"] = []
    elif mutation == "catalog":
        selection["model_catalog"] = {}
    elif mutation == "catalog_item":
        selection["model_catalog"] = [None]
    elif mutation == "coverage":
        selection["candidate_coverage"] = {"attempted_variant_count": 21}
    write_report(tmp_path, NEWER, report)
    saved = load_model_research(tmp_path, actor="Atlas")
    assert saved["status"] == "error"
    assert research_view(saved, pc="Atlas", symbol="AAPL")["rows"] == ()


def test_report_size_limit_is_checked_before_reading(tmp_path, legacy_report, monkeypatch):
    write_report(tmp_path, OLDER, legacy_report)
    oversized = tmp_path / NEWER
    oversized.write_bytes(b"x" * 65)
    monkeypatch.setattr(intraday_models, "MAX_REPORT_BYTES", 64)
    original_read = type(oversized).read_text

    def guard_read(path, *args, **kwargs):
        if path == oversized:
            pytest.fail("An oversized report must not be read")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(type(oversized), "read_text", guard_read)
    saved = load_model_research(tmp_path, actor="Atlas")
    assert saved["status"] == "error" and "limit" in saved["message"]


def test_large_native_atlas_report_preserves_display_evidence_and_original_bytes(tmp_path, legacy_report):
    report = deepcopy(legacy_report)
    item = report["symbols"]["AAPL"]
    selection = item["selection"]
    selection["candidates"][0]["spec"]["parameters"] = {"alpha": 0.125}
    # Native Atlas reports duplicate detailed training/input provenance in
    # these two locations. Displaying their saved research must not refit it
    # or retain that bulk after loading the much smaller result evidence.
    source = {"source_history": {"fixture_provenance": "x" * (6 * 1024 * 1024)},
              "synthetic_policy": "observed_only"}
    item["training_snapshot"] = source
    selection["source"] = source
    path = write_report(tmp_path, NEWER, report)
    assert path.stat().st_size > 10 * 1024 * 1024
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    expected_view = research_view({"status": "loaded", "report": report}, pc="Atlas", symbol="AAPL")

    saved = load_model_research(tmp_path, actor="Atlas")

    assert saved["status"] == "loaded"
    expected = deepcopy(report)
    expected["symbols"]["AAPL"].pop("training_snapshot")
    expected["symbols"]["AAPL"]["selection"].pop("source")
    assert saved["report"] == expected
    assert research_view(saved, pc="Atlas", symbol="AAPL") == expected_view
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert set(tmp_path.iterdir()) == {path}


def test_large_newest_atlas_report_still_rejects_malformed_saved_scores(tmp_path, legacy_report):
    write_report(tmp_path, OLDER, legacy_report)
    report = deepcopy(legacy_report)
    item = report["symbols"]["AAPL"]
    item["training_snapshot"] = {"source_history": "x" * (11 * 1024 * 1024)}
    item["selection"]["candidates"][0]["summary"]["mean_brier"] = "bad"
    write_report(tmp_path, NEWER, report)

    saved = load_model_research(tmp_path, actor="Atlas")

    assert saved["status"] == "error" and "report" not in saved
    assert research_view(saved, pc="Atlas", symbol="AAPL")["rows"] == ()


def test_legacy_21_results_show_selection_and_holdout_separately(legacy_report):
    original = deepcopy(legacy_report)
    view = research_view({"status": "loaded", "report": legacy_report},
                         pc="Atlas", symbol="AAPL", source_dataset="EQUS.MINI")
    assert len(view["rows"]) == 21
    assert "21 saved candidate results" in view["summary"]
    assert "predates full-catalog coverage" in view["summary"]
    assert "4 selected for fresh fits" in view["summary"]
    assert "research results" in view["summary"]
    assert "consensus bars show only models fitted for that saved forecast" in view["summary"]
    rows = {row["name"]: row for row in view["rows"]}
    assert {name for name, row in rows.items() if row["selected"]} == set(SELECTED)
    assert all(rows[name]["reason"] == "selected" for name in SELECTED)
    assert rows["mlp_single_layer"]["status"] == "supported"
    assert rows["mlp_single_layer"]["selected"] is False
    assert rows["mlp_single_layer"]["reason"] == "Not selected by saved legacy policy"
    assert rows["gaussian_nb_smooth"]["reason"] == "training_validation_brier_gap"
    assert rows["knn_uniform"]["reason"] == "fixture fitting error"
    assert rows["knn_uniform"]["validation_brier"] is None
    assert rows["mlp_two_layers"]["validation_brier"] == 0.241
    assert rows["mlp_two_layers"]["holdout_brier"] == 0.255
    assert rows["mlp_single_layer"]["holdout_brier"] is None
    assert legacy_report == original


def test_new_catalog_exposes_unsupported_architectures_without_live_outputs(legacy_report):
    report = deepcopy(legacy_report)
    selection = report["symbols"]["AAPL"]["selection"]
    selection["candidate_coverage"] = {
        "attempted_variant_count": 21, "configured_variant_count": 21,
        "configured_family_count": 10, "implemented_family_count": 16,
    }
    selection["model_catalog"] = [
        {"family": "mlp", "status": "available", "configured": True},
        {"family": "adaboost", "status": "available", "configured": False,
         "nonselection_reason": "not_configured"},
        *({"family": family, "status": "adapter_not_implemented", "configured": False,
           "nonselection_reason": "adapter_not_implemented"} for family in NEURAL_FAMILIES),
    ]
    selection["candidates"][4]["selection_reason"] = "duplicate_family"
    view = research_view({"status": "loaded", "report": report}, pc="Atlas", symbol="AAPL")
    assert "21/21 configurations attempted" in view["summary"]
    assert "10/16 implemented families in research" in view["summary"]
    assert "predates" not in view["summary"]
    unsupported = [row for row in view["rows"] if row["status"] == "adapter_not_implemented"]
    assert {row["family"] for row in unsupported} == set(NEURAL_FAMILIES)
    assert len(unsupported) == len(NEURAL_FAMILIES) == 14
    assert all(row["selected"] is False and row["reason"] == "adapter_not_implemented"
               for row in unsupported)
    assert all("probability_not_down" not in row and "validation_brier" not in row for row in unsupported)
    assert sum(row["selected"] for row in view["rows"]) == 4
    assert next(row for row in view["rows"] if row["name"] == "mlp_single_layer")["reason"] == "duplicate_family"
    assert next(row for row in view["rows"] if row["family"] == "adaboost")["reason"] == "not_configured"


@pytest.mark.parametrize("status", ["error", "incumbent_retained", "no_selection"])
def test_research_failure_status_keeps_prior_live_selection_distinction(legacy_report, status):
    legacy_report["symbols"]["AAPL"]["status"] = status
    view = research_view({"status": "loaded", "report": legacy_report}, pc="Atlas", symbol="AAPL")
    assert f"Latest research status: {status}" in view["summary"]
    assert "earlier live selections may remain in use" in view["summary"]


@pytest.mark.parametrize("pc,dataset", [("Scout", "EQUS.MINI"), ("All", "EQUS.MINI"), ("Atlas", "OTHER.DATASET")])
def test_other_pc_or_source_report_is_not_admitted(legacy_report, pc, dataset):
    view = research_view({"status": "loaded", "report": legacy_report},
                         pc=pc, symbol="AAPL", source_dataset=dataset)
    assert view["rows"] == ()
    assert "selected for fresh fits" not in view["summary"]


def test_unknown_symbol_does_not_show_another_symbols_candidates(legacy_report):
    view = research_view({"status": "loaded", "report": legacy_report}, pc="Atlas", symbol="UNKNOWN")
    assert view["rows"] == ()


@pytest.mark.parametrize("value", ["bad", [], True, float("nan"), float("inf")])
def test_malformed_holdout_score_is_error_before_ui_numeric_formatting(tmp_path, legacy_report, value):
    write_report(tmp_path, OLDER, legacy_report)
    newest = deepcopy(legacy_report)
    newest["symbols"]["AAPL"]["selection"]["holdout"]["models"][0]["metrics"]["brier"] = value
    write_report(tmp_path, NEWER, newest)
    saved = load_model_research(tmp_path, actor="Atlas")
    assert saved["status"] == "error"
    assert "report" not in saved
    assert research_view(saved, pc="Atlas", symbol="AAPL")["rows"] == ()


def test_malformed_start_time_is_error_before_ui_date_formatting(tmp_path, legacy_report):
    write_report(tmp_path, OLDER, legacy_report)
    newest = deepcopy(legacy_report)
    newest["started_at"] = []
    write_report(tmp_path, NEWER, newest)
    saved = load_model_research(tmp_path, actor="Atlas")
    assert saved["status"] == "error" and "start time" in saved["message"]
    assert research_view(saved, pc="Atlas", symbol="AAPL")["rows"] == ()


def test_ui_unrecorded_forecast_cannot_show_different_source_research(legacy_report, monkeypatch):
    """Exercise the actual UI projection call with inert widgets, without Tk."""
    from types import SimpleNamespace
    from app.ui import intraday

    labels, rendered = {}, []
    view = SimpleNamespace(
        models_frame=SimpleNamespace(winfo_children=lambda: []),
        consensus_rows={"legacy": {"pc": "Atlas", "symbol": "AAPL", "models": [],
                                    "forecast_id": "legacy", "outcome_status": "pending"}},
        consensus_choice=SimpleNamespace(get=lambda: "legacy"), local_pc="Atlas",
        symbol=SimpleNamespace(get=lambda: "AAPL"),
        model_research={"status": "loaded", "report": legacy_report},
        research_status=SimpleNamespace(configure=lambda **values: labels.update(values)),
        research_table=object(), consensus_value=SimpleNamespace(configure=lambda **values: None),
        consensus_meta=SimpleNamespace(configure=lambda **values: None),
        consensus_context=SimpleNamespace(configure=lambda **values: None),
        snapshot=SimpleNamespace(loaded_at=intraday.timestamp("2026-10-16T08:00:00Z")),
    )
    monkeypatch.setattr(intraday, "_replace", lambda tree, rows: rendered.extend(rows))
    intraday.IntradayTab._render_consensus(view)
    assert rendered == []
    assert labels["text"] == "No local research report for the selected source dataset."


@pytest.mark.parametrize("mutation", ["candidate_name", "selected_name", "rejection_reason"])
def test_malformed_names_and_rejection_text_are_errors_before_set_or_join(tmp_path, legacy_report, mutation):
    selection = legacy_report["symbols"]["AAPL"]["selection"]
    if mutation == "candidate_name":
        selection["candidates"][0]["spec"]["name"] = []
    elif mutation == "selected_name":
        selection["selected_models"][0]["name"] = []
    else:
        selection["candidates"][0]["rejection_reasons"] = [None]
    write_report(tmp_path, NEWER, legacy_report)
    saved = load_model_research(tmp_path, actor="Atlas")
    assert saved["status"] == "error"
    assert research_view(saved, pc="Atlas", symbol="AAPL")["rows"] == ()


@pytest.fixture
def scout_report():
    """Saved producer-shaped evidence only; no fitting or private state."""
    return {
        "schema": "intraday-nightly-supervision-v1", "pc": "Scout",
        "as_of": "2026-10-15T09:00:00+00:00", "status": "incomplete",
        "refresh": {"source_dataset": "EQUS.MINI", "private_request": "DO_NOT_DISPLAY"},
        "symbols": {"ABCL": {
            "status": "complete", "data": {"source_dataset": "EQUS.MINI"},
            "installation": {"status": "installed", "policy_path": "DO_NOT_DISPLAY"},
            "selection": {
                "status": "promoted", "holdout_session": "2026-10-14",
                "selected_specs": [{"name": "selected", "family": "logistic"}],
                "frozen_shortlist": [{"name": "selected"}, {"name": "not_selected"}],
                "cv_baselines": {"training_prevalence": {"brier": .25}},
                "candidates": [
                    {"spec": {"name": "selected", "family": "logistic"},
                     "status": "eligible_cv", "selection_reason": "selected_validation_rank",
                     "metrics": {"brier": .20, "balanced_accuracy": .65,
                                 "calibration_bins": [{"count": 3}]}, "reasons": []},
                    {"spec": {"name": "not_selected", "family": "lda"},
                     "status": "rejected_cv", "metrics": {"brier": .30, "balanced_accuracy": .5},
                     "reasons": ["validation_rejected"]},
                    {"spec": {"name": "unavailable", "family": "catboost"},
                     "status": "failed", "metrics": None,
                     "selection_reason": "dependency_unavailable",
                     "error": {"message": "Fixture optional dependency missing"}},
                ],
                "model_catalog": [{"family": "tft", "status": "adapter_not_implemented",
                                   "configured": False, "nonselection_reason": "adapter_not_implemented"}],
                "candidate_coverage": {"attempted_variant_count": 3, "configured_variant_count": 3,
                                       "configured_family_count": 3, "implemented_family_count": 16},
                "holdout": {"models": [{"name": "selected", "metrics": {"brier": .22}}],
                            "proposal_metrics": {"brier": .123}},
                "reasons": [], "private_detail": "DO_NOT_DISPLAY",
            },
        }, "DBX": {"status": "deferred_budget", "resumes_next_wake": True}},
    }


def test_scout_checkpoint_is_authoritative_and_keeps_original_bytes(tmp_path, scout_report):
    latest = write_report(tmp_path, "latest.json", scout_report)
    before = latest.read_bytes()
    older = deepcopy(scout_report)
    older["status"] = "complete"
    write_report(tmp_path / "runs", "20261015T100000000000Z.json", older)
    saved = load_model_research(tmp_path, actor="Scout")
    assert saved["status"] == "loaded" and saved["report"] == scout_report
    assert latest.read_bytes() == before
    assert "DO_NOT_DISPLAY" not in str(research_view(saved, pc="Scout", symbol="ABCL"))


def test_scout_missing_checkpoint_reads_newest_recognized_run(tmp_path, scout_report):
    old = deepcopy(scout_report)
    old["as_of"] = "2026-10-15T08:00:00Z"
    write_report(tmp_path / "runs", "20261015T080000000001Z.json", old)
    write_report(tmp_path / "runs", "20261015T090000000001Z.json", scout_report)
    write_report(tmp_path / "runs", "latest.json", {})
    assert load_model_research(tmp_path, actor="Scout")["report"] == scout_report
    assert not (tmp_path / "latest.json").exists()


@pytest.mark.parametrize("payload", [b"{corrupt", b"[]", b"null", b"\xff"])
def test_scout_corrupt_checkpoint_cannot_fall_back_to_finished_run(tmp_path, scout_report, payload):
    write_report(tmp_path / "runs", "20261015T090000000001Z.json", scout_report)
    (tmp_path / "latest.json").write_bytes(payload)
    result = load_model_research(tmp_path, actor="Scout")
    assert result["status"] == "error" and "report" not in result


def test_scout_missing_path_never_creates_reports(tmp_path):
    path = tmp_path / "absent" / "supervision"
    assert load_model_research(path, actor="Scout")["status"] == "missing"
    assert not path.parent.exists()


def test_scout_scores_selection_and_ensemble_holdout_stay_separate(tmp_path, scout_report):
    write_report(tmp_path, "latest.json", scout_report)
    saved = load_model_research(tmp_path, actor="Scout")
    view = research_view(saved, pc="Scout", symbol="ABCL", source_dataset="EQUS.MINI")
    rows = {row["name"]: row for row in view["rows"]}
    assert rows["selected"]["validation_brier"] == .20
    assert rows["selected"]["validation_balanced_accuracy"] == .65
    assert rows["selected"]["baseline_improvement"] == pytest.approx(.05)
    assert rows["selected"]["holdout_brier"] == .22
    assert rows["selected"]["selected"] is True
    assert rows["not_selected"]["selected"] is False
    assert rows["not_selected"]["reason"] == "validation_rejected"
    assert rows["not_selected"]["holdout_brier"] is None
    assert rows["unavailable"]["validation_brier"] is None
    assert rows["unavailable"]["reason"] == "dependency_unavailable"
    assert all(row.get("seconds") is None for row in rows.values())
    assert all(row.get("holdout_brier") != .123 for row in rows.values())
    assert view["holdout_label"] == "Final holdout Brier"
    assert "Final holdout ensemble Brier: 0.123" in view["summary"]
    assert "saved installation: installed" in view["summary"]
    assert "do not establish activation in a running trader" in view["summary"]
    assert "1 selected for fresh fits" in view["summary"]


@pytest.mark.parametrize("status", ["retained", "failed", "budget_exhausted", "insufficient_data"])
def test_scout_assessment_completion_is_not_promotion(tmp_path, scout_report, status):
    item = scout_report["symbols"]["ABCL"]
    item["selection"].update(status=status, selected_specs=[])
    item["installation"]["status"] = "retained"
    write_report(tmp_path, "latest.json", scout_report)
    view = research_view(load_model_research(tmp_path, actor="Scout"), pc="Scout", symbol="ABCL")
    assert not any(row["selected"] for row in view["rows"])
    assert "Latest assessment: complete" in view["summary"]
    assert f"selection: {status}" in view["summary"]
    assert "saved installation: retained" in view["summary"]


def test_scout_promoted_specs_do_not_hide_failed_installation(tmp_path, scout_report):
    item = scout_report["symbols"]["ABCL"]
    item["status"] = "failed"
    item.pop("installation")
    write_report(tmp_path, "latest.json", scout_report)
    view = research_view(load_model_research(tmp_path, actor="Scout"), pc="Scout", symbol="ABCL")
    assert "Latest assessment: failed; selection: promoted; saved installation: unrecorded" in view["summary"]
    assert sum(row["selected"] for row in view["rows"]) == 1


def test_scout_sparse_deferred_symbol_does_not_borrow_other_results(tmp_path, scout_report):
    write_report(tmp_path, "latest.json", scout_report)
    saved = load_model_research(tmp_path, actor="Scout")
    view = research_view(saved, pc="Scout", symbol="DBX")
    assert view["rows"] == () and "deferred_budget" in view["summary"]
    assert "Unrecorded" in view["summary"]
    assert research_view(saved, pc="Scout", symbol="DBX", source_dataset="EQUS.MINI")["rows"] == ()
    assert intraday_models.research_choices(saved) == {
        "pcs": ("Scout",), "symbols": ("ABCL", "DBX"), "datasets": ("EQUS.MINI",)}


def test_scout_early_insufficiency_is_not_described_as_a_legacy_report(tmp_path, scout_report):
    scout_report["symbols"]["ABCL"]["selection"] = {
        "status": "insufficient_data", "selected_specs": [], "holdout_session": "2026-10-14",
        "reasons": ["intended_completed_session_has_no_matured_labels"], "holdout_rows": 0}
    write_report(tmp_path, "latest.json", scout_report)
    view = research_view(load_model_research(tmp_path, actor="Scout"), pc="Scout", symbol="ABCL")
    assert view["rows"] == ()
    assert "candidate coverage not recorded" in view["summary"]
    assert "predates" not in view["summary"]
    assert "intended_completed_session_has_no_matured_labels" in view["summary"]


def test_oversized_json_integer_is_a_report_error_not_a_ui_crash(tmp_path, scout_report):
    scout_report["symbols"]["ABCL"]["selection"]["candidates"][0]["metrics"]["brier"] = 10 ** 400
    write_report(tmp_path, "latest.json", scout_report)
    assert load_model_research(tmp_path, actor="Scout")["status"] == "error"


def test_earliest_parseable_time_does_not_crash_pacific_formatting(tmp_path, scout_report):
    scout_report["as_of"] = "0001-01-01T00:00:00+00:00"
    write_report(tmp_path, "latest.json", scout_report)
    view = research_view(load_model_research(tmp_path, actor="Scout"), pc="Scout", symbol="ABCL")
    assert "research unknown" in view["summary"]


@pytest.mark.parametrize("pc,source", [("Atlas", "EQUS.MINI"), ("All", "EQUS.MINI"),
                                       ("Scout", "XNAS.ITCH"), ("Scout", "Unrecorded")])
def test_scout_identity_and_source_filters_do_not_borrow_report(tmp_path, scout_report, pc, source):
    write_report(tmp_path, "latest.json", scout_report)
    saved = load_model_research(tmp_path, actor="Scout")
    assert research_view(saved, pc=pc, symbol="ABCL", source_dataset=source)["rows"] == ()


@pytest.mark.parametrize("mutation", ["identity", "schema", "time", "data", "dataset", "installation",
    "installation_status", "selection_status", "candidate_metrics", "candidate_reason", "candidate_error",
    "candidate_family", "selected_specs", "coverage", "baseline", "catalog_reason", "catalog_configured"])
def test_scout_malformed_display_fields_are_errors(tmp_path, scout_report, mutation):
    item = scout_report["symbols"]["ABCL"]
    selection = item["selection"]
    if mutation == "identity":
        scout_report["pc"] = "Atlas"
    elif mutation == "schema":
        scout_report["schema"] = "unknown"
    elif mutation == "time":
        scout_report["as_of"] = []
    elif mutation == "data":
        item["data"] = []
    elif mutation == "dataset":
        item["data"]["source_dataset"] = []
    elif mutation == "installation":
        item["installation"] = []
    elif mutation == "installation_status":
        item["installation"]["status"] = []
    elif mutation == "selection_status":
        selection["status"] = []
    elif mutation == "candidate_metrics":
        selection["candidates"][0]["metrics"] = []
    elif mutation == "candidate_reason":
        selection["candidates"][0]["selection_reason"] = []
    elif mutation == "candidate_error":
        selection["candidates"][2]["error"]["message"] = []
    elif mutation == "candidate_family":
        selection["candidates"][0]["spec"]["family"] = []
    elif mutation == "selected_specs":
        selection["selected_specs"] = "bad"
    elif mutation == "coverage":
        selection["candidate_coverage"]["attempted_variant_count"] = True
    elif mutation == "baseline":
        selection["cv_baselines"] = []
    elif mutation == "catalog_reason":
        selection["model_catalog"][0]["nonselection_reason"] = []
    else:
        selection["model_catalog"][0]["configured"] = "false"
    write_report(tmp_path, "latest.json", scout_report)
    saved = load_model_research(tmp_path, actor="Scout")
    assert saved["status"] == "error" and "report" not in saved
    assert research_view(saved, pc="Scout", symbol="ABCL")["rows"] == ()


@pytest.mark.parametrize("field", ["brier", "balanced_accuracy", "baseline", "holdout", "ensemble"])
@pytest.mark.parametrize("value", ["bad", [], True, float("nan"), float("inf")])
def test_scout_nonfinite_and_wrong_type_scores_never_reach_numeric_formatting(tmp_path, scout_report, field, value):
    selection = scout_report["symbols"]["ABCL"]["selection"]
    if field == "baseline":
        selection["cv_baselines"]["training_prevalence"]["brier"] = value
    elif field == "holdout":
        selection["holdout"]["models"][0]["metrics"]["brier"] = value
    elif field == "ensemble":
        selection["holdout"]["proposal_metrics"]["brier"] = value
    else:
        selection["candidates"][0]["metrics"][field] = value
    write_report(tmp_path, "latest.json", scout_report)
    assert load_model_research(tmp_path, actor="Scout")["status"] == "error"


def test_scout_size_limit_is_applied_to_checkpoint_before_read(tmp_path, scout_report, monkeypatch):
    write_report(tmp_path, "latest.json", scout_report)
    monkeypatch.setattr(intraday_models, "MAX_REPORT_BYTES", 1)
    monkeypatch.setattr(type(tmp_path), "read_text", lambda *a, **k: pytest.fail("Read oversized checkpoint"))
    saved = load_model_research(tmp_path, actor="Scout")
    assert saved["status"] == "error" and "limit" in saved["message"]


def test_research_ui_module_has_no_runtime_or_external_service_imports():
    import ast
    from pathlib import Path
    path = Path(__file__).parents[1] / "app/ui/intraday_models.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    imports += [item.name for node in ast.walk(tree) if isinstance(node, ast.Import) for item in node.names]
    assert not any(any(part in name for part in ("ml", "broker", "schwab", "hyperliquid", "databento",
                                                "threading", "subprocess", "socket", "requests")) for name in imports)


@pytest.mark.parametrize("pc,source,expected", [("All", "All", 4), ("Scout", "EQUS.MINI", 4),
    ("Atlas", "All", 0), ("Scout", "XNAS.ITCH", 0), ("Scout", "Unrecorded", 0)])
def test_no_forecast_projection_honors_pc_source_and_symbol(tmp_path, scout_report, monkeypatch, pc, source, expected):
    from types import SimpleNamespace
    from app.ui import intraday
    write_report(tmp_path, "latest.json", scout_report)
    rendered, labels = [], {}
    view = SimpleNamespace(models_frame=SimpleNamespace(winfo_children=lambda: []), consensus_rows={},
        consensus_choice=SimpleNamespace(get=lambda: ""), local_pc="Scout",
        pc=SimpleNamespace(get=lambda: pc), source_dataset=SimpleNamespace(get=lambda: source),
        symbol=SimpleNamespace(get=lambda: "ABCL"), model_research=load_model_research(tmp_path, actor="Scout"),
        research_status=SimpleNamespace(configure=lambda **v: labels.update(v)),
        research_table=SimpleNamespace(heading=lambda *a, **k: None),
        consensus_value=SimpleNamespace(configure=lambda **v: None),
        consensus_context=SimpleNamespace(configure=lambda **v: None),
        consensus_meta=SimpleNamespace(configure=lambda **v: None))
    monkeypatch.setattr(intraday, "_replace", lambda tree, rows: rendered.extend(rows))
    intraday.IntradayTab._render_consensus(view)
    assert len(rendered) == expected


@pytest.mark.parametrize("actor,suffix", [("Scout", "supervision"), ("Atlas", "model-supervision/logs")])
def test_tk_research_binding_derives_from_only_local_database(tmp_path, monkeypatch, actor, suffix):
    import tkinter as tk
    from tkinter import ttk
    from app.ui import intraday
    monkeypatch.setattr(intraday, "configured_actor", lambda: actor)
    monkeypatch.setattr(intraday, "load_model_research", lambda *a, **k: pytest.fail("Read reports before refresh"))
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk unavailable: {exc}")
    root.withdraw()
    try:
        frame = ttk.Frame(root)
        tab = intraday.IntradayTab(root, frame, database_paths={"Local": tmp_path / "local/shadow.sqlite3",
            "Peer receipt": tmp_path / "peer.json"}, auto_load=False)
        assert tab.local_pc == actor
        assert tab.model_research_directory == tmp_path / "local" / suffix
        assert not list(tmp_path.iterdir())
    finally:
        root.destroy()


def test_tk_report_symbols_are_selectable_before_any_manual_trader_start(tmp_path, scout_report):
    import tkinter as tk
    from tkinter import ttk
    from app.ui.intraday import IntradayTab
    directory = tmp_path / "explicit-research"
    report = write_report(directory, "latest.json", scout_report)
    before = report.read_bytes()
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk unavailable: {exc}")
    root.withdraw()
    try:
        frame = ttk.Frame(root)
        frame.pack(fill="both", expand=True)
        tab = IntradayTab(root, frame, datastore_root=tmp_path, local_pc="Scout",
                          model_research_directory=directory, auto_load=False)
        tab.refresh()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not tab.status.get().startswith(("Loaded ", "Refresh failed")):
            root.update()
            time.sleep(0.01)
        assert tab.status.get().startswith("Loaded "), tab.status.get()
        assert tab.snapshot.forecasts == () and tab.consensus_rows == {}
        assert set(tab.boxes["symbol"].cget("values")) == {"All", "ABCL", "DBX"}
        assert set(tab.boxes["pc"].cget("values")) == {"All", "Scout"}
        assert set(tab.boxes["source_dataset"].cget("values")) == {"All", "EQUS.MINI"}
        tab.symbol.set("ABCL")
        tab.render()
        root.update()
        assert len(tab.research_table.get_children()) == 4
        assert tab.research_table.heading("holdout", "text") == "Final holdout Brier"
        assert "0.123" in tab.research_status.cget("text")
        assert "No saved predictions" in tab.consensus_value.cget("text")
        assert report.read_bytes() == before
        assert not (tmp_path / "intraday").exists()
    finally:
        root.destroy()



def test_atlas_saved_research_is_selectable_without_a_forecast(legacy_report):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from app.ui.intraday import IntradayTab
    from app.ui.intraday_data import IntradaySnapshot
    options = {}
    class Variable:
        def __init__(self, value="All"): self.value = value
        def get(self): return self.value
        def set(self, value): self.value = value
    fields = ("pc", "symbol", "source_dataset", "session", "reference_slot")
    view = SimpleNamespace(model_research={"status": "loaded", "report": legacy_report},
        _closed=False, _view_revision=0,
        boxes={name: SimpleNamespace(configure=lambda *, values, name=name: options.update({name: values})) for name in fields},
        status=Variable(), render=lambda **kwargs: None, **{name: Variable() for name in fields})
    IntradayTab.set_snapshot(view, IntradaySnapshot(datetime.now(timezone.utc)))
    assert options["pc"] == ["All", "Atlas"]
    assert options["symbol"] == ["All", "AAPL"]
    assert options["source_dataset"] == ["All", "EQUS.MINI"]


@pytest.mark.parametrize("pc,source", [("Scout", "EQUS.MINI"), ("Atlas", "OTHER.DATASET")])
def test_atlas_research_without_forecasts_honors_filters(legacy_report, monkeypatch, pc, source):
    from types import SimpleNamespace
    from app.ui import intraday
    rendered = []
    view = SimpleNamespace(models_frame=SimpleNamespace(winfo_children=lambda: []), consensus_rows={},
        consensus_choice=SimpleNamespace(get=lambda: ""), local_pc="Atlas",
        pc=SimpleNamespace(get=lambda: pc), source_dataset=SimpleNamespace(get=lambda: source),
        symbol=SimpleNamespace(get=lambda: "AAPL"), model_research={"status": "loaded", "report": legacy_report},
        research_status=SimpleNamespace(configure=lambda **kw: None),
        research_table=SimpleNamespace(heading=lambda *a, **kw: None),
        consensus_value=SimpleNamespace(configure=lambda **kw: None),
        consensus_context=SimpleNamespace(configure=lambda **kw: None),
        consensus_meta=SimpleNamespace(configure=lambda **kw: None))
    monkeypatch.setattr(intraday, "_replace", lambda table, rows: rendered.extend(rows))
    intraday.IntradayTab._render_consensus(view)
    assert rendered == []
