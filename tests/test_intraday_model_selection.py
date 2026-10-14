"""Offline synthetic research contracts; fixture success is not market evidence."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import itertools
import json

import numpy as np
import pandas as pd
import pytest

from ml.intraday.data import Dataset
from ml.intraday.models import ModelSpec, UnsupportedModelError, registry_status
from ml.intraday.model_selection import SelectionConfig, default_candidates, evaluate_candidates, probability_metrics
from ml.intraday.prediction import ContractError, TARGET_VERSION, instant


def fixture_data(days=9, rows=32, *, continuous=False):
    rng = np.random.default_rng(708)
    references, labels, sessions, features = [], [], [], []
    start = datetime(2026, 9, 1, 14, tzinfo=timezone.utc)
    for day in range(days):
        for row in range(rows):
            references.append(start + (timedelta(minutes=15 * (day * rows + row)) if continuous
                                       else timedelta(days=day, minutes=15 * row)))
            label = row % 2
            labels.append(label)
            sessions.append(f"session-{day:02d}")
            features.append([(-2 if label == 0 else 2) + rng.normal(0, 0.1), day, day * rows + row])
    targets = [reference + timedelta(minutes=45) for reference in references]
    available = [target + timedelta(seconds=1) for target in targets]
    return Dataset(np.array(features), np.array(labels, dtype=np.uint8), np.array([features[-1]]),
                   ("signal", "day", "row_id"), pd.DatetimeIndex(available), pd.DatetimeIndex(references),
                   pd.DatetimeIndex(targets), {}, {"target_version": TARGET_VERSION,
                   "training_cutoff": (available[-1] + timedelta(hours=2)).isoformat(),
                   "availability_kinds": ["actual"], "synthetic_policy": "exclude"}), sessions


def small_config(**changes):
    values = dict(cv_folds=2, min_training_sessions=2, min_training_rows=64,
                  min_validation_rows=16, min_holdout_rows=16, min_class_count=3,
                  min_calibration_rows=12, max_selected=1)
    values.update(changes)
    return replace(SelectionConfig(), **values)


def spec(name="candidate", family="logistic", **parameters):
    return ModelSpec(name, family=family, parameters=parameters, calibration_fraction=0.25,
                     probability_calibration="sigmoid")


class SyntheticEstimator:
    classes_ = np.array([0, 1])

    def __init__(self, probability, reverse_from=None, invalid=False):
        self.probability = probability
        self.reverse_from = reverse_from
        self.invalid = invalid

    def predict_proba(self, X):
        p = np.where(X[:, 0] > 0, self.probability, 1 - self.probability)
        if self.reverse_from is not None:
            p = np.where(X[:, 1] >= self.reverse_from, 1 - p, p)
        if self.invalid:
            p[:] = np.nan
        return np.column_stack([1 - p, p])


def fake_fitter(monkeypatch):
    calls = []

    def fit(model, X, y, **evidence):
        calls.append({"name": model.name, "X": X.copy(), "y": y.copy(), **evidence})
        return SyntheticEstimator(model.parameters.get("fixture_probability", 0.9),
                                  model.parameters.get("reverse_from"), model.parameters.get("invalid", False)), {
            "calibration": {"method": "fixture_chronological_sigmoid"}}

    monkeypatch.setattr("ml.intraday.model_selection.fit_calibrated_estimator", fit)
    return calls


def test_default_search_has_real_diverse_calibrated_families_and_neural_shapes():
    candidates = default_candidates()
    implemented = {item["family"] for item in registry_status() if item["status"] != "adapter_not_implemented"}
    assert len(candidates) == 21 and len(implemented) == 16
    assert {candidate.family for candidate in candidates} == implemented
    assert SelectionConfig().max_selected == 16 and SelectionConfig().max_candidates == 21
    assert all(candidate.probability_calibration == "sigmoid" for candidate in candidates)
    assert {candidate.parameters["hidden_layer_sizes"] for candidate in candidates if candidate.family == "mlp"} == {(16,), (32, 8), (8, 4)}
    assert all(candidate.parameters.get("warm_start", False) is False for candidate in candidates)


def test_full_catalog_research_records_missing_dependency_and_selects_more_than_four_families(monkeypatch):
    import ml.intraday.model_selection as selection

    dataset, sessions = fixture_data()
    catalog = registry_status()
    for item in catalog:
        if item["status"] != "adapter_not_implemented":
            item["status"] = "dependency_unavailable" if item["family"] == "catboost" else "available"
    monkeypatch.setattr(selection, "registry_status", lambda: [dict(item) for item in catalog])
    calls = []

    def fit(model, X, y, **evidence):
        calls.append(model.name)
        if model.family == "catboost":
            raise UnsupportedModelError("catboost fixture dependency is unavailable")
        return SyntheticEstimator(.9), {"calibration": {"method": "fixture_chronological_sigmoid"}}

    monkeypatch.setattr(selection, "fit_calibrated_estimator", fit)
    report = evaluate_candidates(dataset, session_ids=sessions)
    assert {candidate.name for candidate in default_candidates()} <= set(calls)
    assert report["status"] == "promoted", report["reasons"]
    assert len(report["selected_specs"]) == len({item["family"] for item in report["selected_specs"]}) == 15
    coverage = report["candidate_coverage"]
    assert coverage["configured_variant_count"] == coverage["attempted_variant_count"] == 21
    assert coverage["configured_family_count"] == coverage["attempted_family_count"] == 16
    assert coverage["implemented_family_count"] == 16 and coverage["available_family_count"] == 15
    assert coverage["unconfigured_implemented_families"] == []
    assert coverage["dependency_unavailable_families"] == ["catboost"]
    failed = next(row for row in report["candidates"] if row["spec"]["family"] == "catboost")
    assert failed["status"] == "failed" and failed["selection_reason"] == "dependency_unavailable"
    assert failed["error"]["type"] == "UnsupportedModelError" and not failed["selected"]
    unsupported = [row for row in report["model_catalog"] if row["status"] == "adapter_not_implemented"]
    assert len(unsupported) == 14
    assert all(row["nonselection_reason"] == "adapter_not_implemented" and not row["configured"] for row in unsupported)
    assert report["holdout_policy"] == "one_final_gate_after_frozen_cv_shortlist_no_replacements"
    json.dumps(report, allow_nan=False)


def test_family_duplicates_and_explicit_cap_have_distinct_nonselection_reasons(monkeypatch):
    dataset, sessions = fixture_data()
    fake_fitter(monkeypatch)
    choices = [spec("a_logistic"), spec("b_logistic"), spec("c_nb", family="gaussian_nb"), spec("d_lda", family="lda")]
    report = evaluate_candidates(dataset, candidates=choices, config=small_config(max_selected=2), session_ids=sessions)
    rows = {row["spec"]["name"]: row for row in report["candidates"]}
    assert [item["name"] for item in report["selected_specs"]] == ["a_logistic", "c_nb"]
    assert rows["a_logistic"]["selected"] and rows["a_logistic"]["selection_reason"] == "selected_validation_rank"
    assert rows["b_logistic"]["selection_reason"] == "duplicate_family" and not rows["b_logistic"]["selected"]
    assert rows["d_lda"]["selection_reason"] == "configured_cap" and not rows["d_lda"]["selected"]
    assert report["candidate_coverage"]["attempted_variant_count"] == 4
    assert "catboost" in report["candidate_coverage"]["unconfigured_implemented_families"]


def test_explicit_candidate_budget_still_rejects_an_oversized_catalog_before_fitting(monkeypatch):
    dataset, sessions = fixture_data()
    calls = fake_fitter(monkeypatch)
    with pytest.raises(ContractError, match="Candidate count exceeds"):
        evaluate_candidates(dataset, config=small_config(max_candidates=20), session_ids=sessions)
    assert calls == []


def test_budget_skips_are_reported_without_inventing_candidate_fits(monkeypatch):
    dataset, sessions = fixture_data()
    calls = fake_fitter(monkeypatch)
    ticks = itertools.count()
    monkeypatch.setattr("ml.intraday.model_selection.time.monotonic", lambda: next(ticks))
    report = evaluate_candidates(dataset, candidates=[spec("first"), spec("second", family="lda")],
                                 config=small_config(max_search_seconds=.5), session_ids=sessions)
    assert report["status"] == "budget_exhausted" and calls == []
    assert report["selected_specs"] == [] and len(report["candidates"]) == 2
    assert report["candidates"][1]["status"] == "not_run"
    assert report["candidates"][1]["selection_reason"] == "search_budget_exhausted"
    assert not report["candidates"][1]["selected"]


def test_probability_scores_reveal_extreme_wrong_predictions_and_include_one_in_bins():
    scores = probability_metrics([0, 1, 0, 1], [1.0, 0.0, 0.99, 0.01])
    assert scores["accuracy"] == 0
    assert scores["brier"] > 0.98
    assert scores["log_loss"] > 10
    assert scores["extreme_error_rate"] == 1
    assert scores["expected_calibration_error"] > 0.98
    assert sum(row["count"] for row in scores["calibration_bins"]) == 4
    assert scores["calibration_bins"][-1]["count"] == 2
    json.dumps(scores, allow_nan=False)


@pytest.mark.parametrize("labels,probabilities", [([], []), ([0, 2], [.2, .8]), ([0], [np.nan]),
                                                ([0], [1.1]), ([0, 1], [.2])])
def test_invalid_metric_inputs_are_rejected(labels, probabilities):
    with pytest.raises(ContractError):
        probability_metrics(labels, probabilities)


def test_success_freezes_before_latest_session_and_fits_only_prior_available_labels(monkeypatch):
    dataset, sessions = fixture_data()
    calls = fake_fitter(monkeypatch)
    report = evaluate_candidates(dataset, candidates=[spec()], config=small_config(), session_ids=sessions)
    assert report["status"] == "promoted", report["reasons"]
    assert report["holdout_session"] == "session-08"
    assert report["selected_specs"] == report["frozen_shortlist"]
    assert len(calls) == 3
    for call in calls:
        cutoff = instant(call["training_cutoff"])
        assert all(instant(value) <= cutoff for value in call["target_close_at"])
        assert all(instant(value) <= cutoff for value in call["label_available_at"])
        assert all(instant(value) < cutoff for value in call["reference_at"])
        assert call["X"][:, 1].max() < 8
    assert [fold["session"] for fold in report["folds"]] == ["session-06", "session-07"]
    assert report["proposal_cv"]["brier"] < report["cv_baselines"]["training_prevalence"]["brier"]
    assert report["holdout"]["proposal_metrics"]["brier"] < .02
    json.dumps(report, allow_nan=False)


def test_exact_45_minute_overlap_and_delayed_receipt_are_both_purged(monkeypatch):
    dataset, sessions = fixture_data(continuous=True)
    available = list(dataset.label_available_at)
    # CV starts at session-06. This otherwise old label arrived ten minutes later.
    delayed_index = 4 * 32
    available[delayed_index] = dataset.reference_at[6 * 32] + timedelta(minutes=10)
    dataset = replace(dataset, label_available_at=pd.DatetimeIndex(available))
    calls = fake_fitter(monkeypatch)
    report = evaluate_candidates(dataset, candidates=[spec()], config=small_config(), session_ids=sessions)
    assert report["status"] == "promoted", report
    first = calls[0]
    assert delayed_index not in first["X"][:, 2]
    assert all(instant(value) <= instant(first["training_cutoff"]) for value in first["label_available_at"])
    # Receipt of the target that closes exactly at the boundary is one second late.
    assert report["folds"][0]["overlap_or_unavailable_rows_purged"] == 4
    assert report["folds"][0]["training_latest_target_close_at"] <= report["folds"][0]["training_cutoff"]


def test_training_row_cap_and_session_evidence_are_explicit(monkeypatch):
    dataset, sessions = fixture_data(days=12)
    fake_fitter(monkeypatch)
    report = evaluate_candidates(dataset, candidates=[spec()], config=small_config(max_training_rows=96), session_ids=sessions)
    assert report["status"] == "promoted"
    assert all(fold["training_rows"] == 96 for fold in report["folds"])
    assert all(fold["row_budget_excluded"] > 0 for fold in report["folds"])
    assert report["selected_specs"][0]["training_max_rows"] == 96


def test_custom_smaller_training_cap_is_the_actual_fit_and_gated_window(monkeypatch):
    dataset, sessions = fixture_data()
    calls = fake_fitter(monkeypatch)
    candidate = replace(spec(), training_max_rows=80)
    report = evaluate_candidates(dataset, candidates=[candidate], config=small_config(), session_ids=sessions)
    assert report["status"] == "promoted"
    assert all(len(call["X"]) == 80 for call in calls)
    assert report["selected_specs"][0]["training_max_rows"] == 80
    below_minimum = replace(candidate, training_max_rows=32)
    report = evaluate_candidates(dataset, candidates=[below_minimum], config=small_config(), session_ids=sessions)
    assert report["status"] == "insufficient_data" and report["selected_specs"] == []


def test_rejected_cv_never_evaluates_holdout(monkeypatch):
    dataset, sessions = fixture_data()
    calls = fake_fitter(monkeypatch)
    report = evaluate_candidates(dataset, candidates=[spec(fixture_probability=.5)], config=small_config(), session_ids=sessions)
    assert report["status"] == "retained"
    assert len(calls) == 2
    assert report["holdout"] is None and report["selected_specs"] == []
    assert report["candidates"][0]["status"] == "rejected_cv"


def test_failed_holdout_never_tries_lower_ranked_replacement(monkeypatch):
    dataset, sessions = fixture_data()
    calls = fake_fitter(monkeypatch)
    candidates = [spec("cv_winner", fixture_probability=.97, reverse_from=8),
                  spec("reserve", family="random_forest", fixture_probability=.9)]
    report = evaluate_candidates(dataset, candidates=candidates, config=small_config(), session_ids=sessions)
    assert report["status"] == "retained"
    assert report["frozen_shortlist"][0]["name"] == "cv_winner"
    assert report["selected_specs"] == []
    assert [call["name"] for call in calls] == ["cv_winner", "cv_winner", "reserve", "reserve", "cv_winner"]
    assert report["holdout"]["proposal_metrics"]["extreme_error_rate"] == 1


def test_one_failing_member_rejects_whole_frozen_ensemble_without_holdout_reranking(monkeypatch):
    dataset, sessions = fixture_data()
    fake_fitter(monkeypatch)
    candidates = [spec("stable", fixture_probability=.97),
                  spec("unstable", family="random_forest", fixture_probability=.9, reverse_from=8)]
    config = replace(small_config(), max_selected=2)
    report = evaluate_candidates(dataset, candidates=candidates, config=config, session_ids=sessions)
    assert report["status"] == "retained"
    assert len(report["frozen_shortlist"]) == 2 and report["selected_specs"] == []
    assert len(report["holdout"]["models"]) == 2
    assert any(reason.startswith("unstable:") for reason in report["reasons"])


def test_incumbent_comparison_rejects_inferior_proposal_before_touching_holdout(monkeypatch):
    dataset, sessions = fixture_data()
    calls = fake_fitter(monkeypatch)
    incumbent = ModelSpec("incumbent", family="logistic", parameters={"fixture_probability": .97})
    report = evaluate_candidates(dataset, candidates=[spec()], incumbent_specs=[incumbent],
                                 config=small_config(), session_ids=sessions)
    assert report["status"] == "retained"
    assert report["holdout"] is None
    assert "cv_brier_worse_than_incumbent" in report["reasons"]
    assert len(calls) == 4


def test_initial_ten_uncapped_incumbents_can_be_compared_to_one_bounded_candidate(monkeypatch):
    dataset, sessions = fixture_data(days=140)
    calls = fake_fitter(monkeypatch)
    incumbents = [ModelSpec(f"old_{index}", family="logistic", parameters={"fixture_probability": .8}) for index in range(10)]
    report = evaluate_candidates(dataset, candidates=[spec()], incumbent_specs=incumbents,
                                 config=small_config(), session_ids=sessions)
    assert report["status"] == "promoted", report["reasons"]
    assert all(len(call["X"]) == 4096 for call in calls if call["name"] == "candidate")
    assert all(len(call["X"]) > 4096 for call in calls if call["name"].startswith("old_"))
    assert len(report["incumbent"]["specs"]) == 10


def test_latest_session_has_no_influence_on_cv_rank_or_shortlist(monkeypatch):
    dataset, sessions = fixture_data()
    fake_fitter(monkeypatch)
    candidates = [spec("first", fixture_probability=.95), spec("second", family="random_forest", fixture_probability=.9)]
    first = evaluate_candidates(dataset, candidates=candidates, config=small_config(), session_ids=sessions)
    changed_labels = dataset.y.copy()
    changed_labels[-32:] = 1 - changed_labels[-32:]
    changed = evaluate_candidates(replace(dataset, y=changed_labels), candidates=candidates, config=small_config(), session_ids=sessions)
    assert first["frozen_shortlist"] == changed["frozen_shortlist"]
    assert first["proposal_cv"] == changed["proposal_cv"]
    assert first["status"] == "promoted" and changed["status"] == "retained"


def test_minimum_sessions_classes_and_calibration_counts_block_without_substitution(monkeypatch):
    dataset, sessions = fixture_data(days=4)
    calls = fake_fitter(monkeypatch)
    report = evaluate_candidates(dataset, candidates=[spec()], config=small_config(), session_ids=sessions)
    assert report["status"] == "insufficient_data" and calls == []
    dataset, sessions = fixture_data()
    one_class = replace(dataset, y=np.ones(len(dataset.y), dtype=np.uint8))
    report = evaluate_candidates(one_class, candidates=[spec()], config=small_config(), session_ids=sessions)
    assert report["status"] == "insufficient_data" and calls == []
    report = evaluate_candidates(dataset, candidates=[spec()], config=small_config(min_calibration_rows=200), session_ids=sessions)
    assert report["status"] == "insufficient_data" and calls == []
    assert report["candidates"][0]["error"]["type"] == "InsufficientEvidenceError"


def test_failed_probability_adapter_is_retryable_and_never_promoted(monkeypatch):
    dataset, sessions = fixture_data()
    fake_fitter(monkeypatch)
    report = evaluate_candidates(dataset, candidates=[spec(invalid=True)], config=small_config(), session_ids=sessions)
    assert report["status"] == "failed"
    assert report["selected_specs"] == [] and report["holdout"] is None
    assert report["candidates"][0]["error"]["type"] == "ContractError"


def test_search_deadline_stops_before_next_fit_and_is_retryable(monkeypatch):
    dataset, sessions = fixture_data()
    calls = fake_fitter(monkeypatch)
    ticks = itertools.count()
    monkeypatch.setattr("ml.intraday.model_selection.time.monotonic", lambda: next(ticks))
    report = evaluate_candidates(dataset, candidates=[spec()], config=small_config(max_search_seconds=.5), session_ids=sessions)
    assert report["status"] == "budget_exhausted" and calls == []
    assert report["selected_specs"] == []


def test_completed_holdout_fit_that_overruns_budget_remains_retryable(monkeypatch):
    import ml.intraday.model_selection as selection

    dataset, sessions = fixture_data()
    calls = fake_fitter(monkeypatch)
    wrapped_fit = selection.fit_calibrated_estimator
    elapsed = [0.0]

    def fit(*args, **kwargs):
        result = wrapped_fit(*args, **kwargs)
        if instant(kwargs["training_cutoff"]) == instant(dataset.reference_at[-32]):
            elapsed[0] = 200.0
        return result

    monkeypatch.setattr(selection, "fit_calibrated_estimator", fit)
    monkeypatch.setattr(selection.time, "monotonic", lambda: elapsed[0])
    report = evaluate_candidates(dataset, candidates=[spec()], config=small_config(), session_ids=sessions)
    assert len(calls) == 3 and report["holdout"] is not None
    assert report["status"] == "budget_exhausted" and report["selected_specs"] == []


@pytest.mark.parametrize("field,value", [("cv_folds", 6), ("max_candidates", 25), ("min_class_count", True),
                                         ("max_selected", 17),
                                         ("max_search_seconds", np.nan), ("max_ece", 1.1),
                                         ("extreme_probability", .5)])
def test_invalid_search_budget_and_probability_gates_rejected(field, value):
    with pytest.raises(ContractError):
        SelectionConfig(**{field: value})


def test_target_order_availability_and_calibration_contracts_reject_bad_inputs():
    dataset, sessions = fixture_data()
    with pytest.raises(ContractError, match="sigmoid"):
        evaluate_candidates(dataset, candidates=[ModelSpec("raw")], config=small_config(), session_ids=sessions)
    with pytest.raises(ContractError, match="after selection"):
        evaluate_candidates(dataset, as_of=dataset.reference_at[-1], candidates=[spec()], config=small_config(), session_ids=sessions)
    closes = list(dataset.target_close_at)
    closes[0] += timedelta(minutes=15)
    with pytest.raises(ContractError, match="45-minute"):
        evaluate_candidates(replace(dataset, target_close_at=closes), candidates=[spec()], config=small_config(), session_ids=sessions)
    mixed = list(sessions)
    mixed[40] = mixed[0]
    with pytest.raises(ContractError, match="contiguous"):
        evaluate_candidates(dataset, candidates=[spec()], config=small_config(), session_ids=mixed)


def test_real_offline_sigmoid_pipeline_produces_auditable_selection_evidence():
    dataset, sessions = fixture_data()
    report = evaluate_candidates(dataset, candidates=[spec()],
                                 config=small_config(), session_ids=sessions)
    assert report["status"] == "promoted", report
    calibration = report["candidates"][0]["folds"][0]["fit_evidence"]["calibration"]
    assert calibration["method"] == "purged_chronological_sigmoid"
    assert instant(calibration["base_latest_label_available_at"]) <= instant(calibration["first_calibration_reference"])
    assert instant(calibration["base_latest_target"]) <= instant(calibration["first_calibration_reference"])
    assert len(report["selection_id"]) == len(report["dataset_sha256"]) == 64
    json.dumps(report, allow_nan=False)
