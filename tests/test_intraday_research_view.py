"""Offline saved-report fixtures; no model fitting or private state is read."""
from __future__ import annotations

from copy import deepcopy
import json

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

