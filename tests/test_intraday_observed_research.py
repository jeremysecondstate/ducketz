"""Offline fixture contracts; fitting success is not evidence of market skill."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import numpy as np
import pandas as pd
import pytest

from ml.intraday.data import Dataset
from ml.intraday.observed_research import (
    INCUMBENT, PROPOSAL, ObservedResearchConfig, evaluate_observed_experiment, fixed_specs,
)
from ml.intraday.prediction import ContractError, TARGET_VERSION, instant
from ml.intraday.observed_data import FEATURE_POLICY, FEATURE_VERSION, LABEL_PROVENANCE


def fixture_data(days=26, rows=32):
    references, values, labels = [], [], []
    start = datetime(2026, 8, 1, 14, tzinfo=timezone.utc)
    for day in range(days):
        for row in range(rows):
            references.append(start + timedelta(days=day, minutes=15 * row))
            label = row % 2
            labels.append(label)
            values.append([0.01 if label else -0.01, float(day), float(row)])
    targets = [value + timedelta(minutes=45) for value in references]
    receipts = [value + timedelta(seconds=1) for value in targets]
    return Dataset(np.array(values), np.array(labels, dtype=np.uint8), np.array([values[-1]]),
                   ("body_return", "day", "row"), pd.DatetimeIndex(receipts), pd.DatetimeIndex(references),
                   pd.DatetimeIndex(targets), {"symbol": "FIXTURE", "source": "fixture", "source_dataset": "fixture-observed"}, {"target_version": TARGET_VERSION,
                   "training_cutoff": (receipts[-1] + timedelta(hours=1)).isoformat(),
                   "availability_kinds": ["actual"], "synthetic_policy": "exclude", "synthetic_labels_included": 0,
                   "feature_version": FEATURE_VERSION, "feature_policy": FEATURE_POLICY, "label_provenance": LABEL_PROVENANCE})


class FixtureEstimator:
    classes_ = np.array([1, 0])  # Exercise explicit class identity, not column assumptions.

    def predict_proba(self, X):
        p = np.where(X[:, 0] >= 0, 0.8, 0.2)
        return np.column_stack([p, 1 - p])


def fake_fitter(monkeypatch, *, failed=None):
    calls = []

    def fit(spec, X, y, **kwargs):
        calls.append({"spec": spec, "X": X.copy(), "y": y.copy(), **kwargs})
        if spec.name == failed:
            raise RuntimeError("intentional fixture fit failure")
        return FixtureEstimator(), {"calibration": "fixture_only"}

    monkeypatch.setattr("ml.intraday.observed_research.fit_calibrated_estimator", fit)
    return calls


def subset(dataset, keep):
    return replace(dataset, X=dataset.X[keep], y=dataset.y[keep],
                   label_available_at=dataset.label_available_at[keep],
                   training_reference_at=dataset.reference_at[keep], target_close_at=dataset.target_close_at[keep])


def test_fixed_specs_and_four_right_aligned_temporal_blocks(monkeypatch):
    calls = fake_fitter(monkeypatch)
    dataset = fixture_data()
    report = evaluate_observed_experiment(dataset)
    assert report["status"] == "scored"
    assert len(calls) == 8
    assert [block["role"] for block in report["blocks"]] == ["development"] * 3 + ["holdout"]
    assert [len(block["dates"]) for block in report["blocks"]] == [5] * 4
    assert report["blocks"][0]["date_start"] == "2026-08-07"
    assert report["holdout"]["date_end"] == "2026-08-26"
    assert report["blocks"][0]["partition"]["training"]["actual_date_count"] == 6
    assert report["holdout"]["comparators"][PROPOSAL]["metrics"]["scored"] == 160
    assert report["holdout"]["comparators"]["reference_direction"]["fitting"]["hard_direction_accuracy"] == 1
    assert report["development"]["comparators"][PROPOSAL]["metrics"]["scored"] == 480
    assert report["promotion_allowed"] is False and report["production_writes"] is False
    assert report["selected_specs"] == []
    assert [row["spec"] for row in calls[:2]] == list(fixed_specs())
    assert fixed_specs()[0].parameters == {"C": .05}
    assert fixed_specs()[1].parameters == {"n_estimators": 64, "max_depth": 3, "min_samples_leaf": 16}
    for call in calls:
        cutoff = instant(call["training_cutoff"])
        assert all(instant(value) < cutoff for value in call["reference_at"])
        assert all(instant(value) <= cutoff for value in call["target_close_at"])
        assert all(instant(value) <= cutoff for value in call["label_available_at"])
    assert all(call["X"][:, 1].max() < 21 for call in calls)
    json.dumps(report, allow_nan=False)


def test_holdout_labels_do_not_change_fits_or_baseline_predictions(monkeypatch):
    calls = fake_fitter(monkeypatch)
    dataset = fixture_data()
    first = evaluate_observed_experiment(dataset)
    old_calls = [(call["X"].copy(), call["y"].copy()) for call in calls]
    calls.clear()
    changed = dataset.y.copy()
    changed[-5 * 32:] = 1 - changed[-5 * 32:]
    second = evaluate_observed_experiment(replace(dataset, y=changed))
    assert first["frozen_specs_sha256"] == second["frozen_specs_sha256"]
    for (old_X, old_y), new in zip(old_calls, calls):
        np.testing.assert_array_equal(old_X, new["X"])
        np.testing.assert_array_equal(old_y, new["y"])
    assert first["development"] == second["development"]
    assert [row["probabilities"] for row in first["holdout"]["predictions"]] == [row["probabilities"] for row in second["holdout"]["predictions"]]
    assert first["holdout"]["comparators"][PROPOSAL]["metrics"]["accuracy"] == 1
    assert second["holdout"]["comparators"][PROPOSAL]["metrics"]["accuracy"] == 0
    assert second["holdout"]["status"] == "scored"  # A poor score never hides the heldout result.


def test_incumbent_preserves_original_population_and_common_evaluation_keys(monkeypatch):
    calls = fake_fitter(monkeypatch)
    original = fixture_data()
    observed_keep = np.ones(len(original.y), dtype=bool)
    observed_keep[::8] = False
    observed = subset(original, observed_keep)
    incumbent = replace(original, manifest={**original.manifest, "synthetic_policy": "include", "synthetic_labels_included": 104})
    report = evaluate_observed_experiment(observed, incumbent_dataset=incumbent)
    assert report["status"] == "scored"
    assert len(calls) == 48
    assert report["comparison_coverage"]["incumbent_observable_rows_absent_from_candidate"] == 104
    assert report["incumbent_manifest"]["synthetic_policy"] == "include"
    block = report["holdout"]
    assert block["incumbent_partition"]["training"]["rows"] > block["partition"]["training"]["rows"]
    assert len(block["comparators"][INCUMBENT]["members"]) == 10
    for row in block["predictions"]:
        assert set(row["probabilities"]) == set(block["comparators"])
        assert all(value is not None for value in row["probabilities"].values())
    assert len({row["evaluation_key"] for row in block["predictions"]}) == len(block["predictions"])
    assert all(call["spec"].probability_calibration == "none" for call in calls if call["spec"].name not in {spec.name for spec in fixed_specs()})


def test_unmatched_early_block_rows_never_move_cutoff_and_late_receipts_are_purged(monkeypatch):
    calls = fake_fitter(monkeypatch)
    dataset = fixture_data()
    receipts = list(dataset.label_available_at)
    first_date = 6
    delayed_index = first_date * 32 - 1
    cutoff = dataset.reference_at[first_date * 32]
    receipts[delayed_index] = cutoff + timedelta(minutes=2)
    dataset = replace(dataset, label_available_at=pd.DatetimeIndex(receipts))
    keep = np.ones(len(dataset.y), dtype=bool)
    keep[first_date * 32:first_date * 32 + 3] = False
    keep[21 * 32:21 * 32 + 3] = False  # Repeat at the final heldout boundary.
    incumbent = subset(dataset, keep)
    report = evaluate_observed_experiment(dataset, incumbent_dataset=incumbent)
    partition = report["blocks"][0]["partition"]
    assert partition["training_cutoff"] == instant(cutoff).isoformat()
    assert partition["target_overlap_rows"] == 0
    assert partition["unavailable_prior_rows"] == 1
    assert partition["overlap_or_unavailable_rows_purged"] == 1
    assert report["comparison_coverage"]["candidate_rows_missing_incumbent_reference"] == 6
    assert report["holdout"]["partition"]["training_cutoff"] == instant(dataset.reference_at[21 * 32]).isoformat()
    assert report["holdout"]["partition"]["training"]["last_reference"] < report["holdout"]["partition"]["training_cutoff"]
    for call in calls:
        assert all(instant(value) <= instant(call["training_cutoff"]) for value in call["label_available_at"])
    assert all(partition["proof"].values())
    assert all(report["blocks"][0]["calibration"]["proof"].values())
    assert report["blocks"][0]["calibration"]["purged_rows"] > 0


def test_no_survivor_ensemble_and_no_baseline_substitution_for_model_failure(monkeypatch):
    fake_fitter(monkeypatch, failed="forest_shallow")
    report = evaluate_observed_experiment(fixture_data())
    held = report["holdout"]
    assert held["comparators"]["logistic_strong"]["status"] == "scored"
    assert held["comparators"]["forest_shallow"]["status"] == "failed"
    assert held["comparators"][PROPOSAL]["metrics"] is None
    assert held["comparators"]["neutral_0_5"]["status"] == "scored"
    assert all(row["probabilities"][PROPOSAL] is None for row in held["predictions"])
    assert report["development"]["comparators"][PROPOSAL]["metrics"] is None


def test_insufficient_training_calibration_or_blocks_are_truthful(monkeypatch):
    calls = fake_fitter(monkeypatch)
    report = evaluate_observed_experiment(fixture_data(days=19))
    assert report["status"] == "insufficient_data" and not calls
    report = evaluate_observed_experiment(fixture_data(days=20))
    assert report["blocks"][0]["status"] == "insufficient_data"
    assert "actual_training_dates_below_minimum" in report["blocks"][0]["reasons"]
    calls.clear()
    report = evaluate_observed_experiment(fixture_data(), config={"min_calibration_rows": 999})
    assert not calls
    assert "calibration_rows_below_minimum" in report["holdout"]["reasons"]
    assert report["holdout"]["comparators"][PROPOSAL]["metrics"] is None


def test_deadline_prevents_fits_without_disguising_budget_exhaustion(monkeypatch):
    calls = fake_fitter(monkeypatch)
    report = evaluate_observed_experiment(fixture_data(), config={"max_seconds": 1e-9})
    assert not calls
    assert all(block["status"] == "budget_exhausted" for block in report["blocks"])
    assert report["holdout"]["comparators"][PROPOSAL]["metrics"] is None


@pytest.mark.parametrize("mutation", [
    lambda d: replace(d, y=d.y[:-1]),
    lambda d: replace(d, X=d.X[:, :-1]),
    lambda d: replace(d, inference_X=d.inference_X[:, :-1]),
    lambda d: replace(d, feature_names=("body_return", "day", "day")),
    lambda d: replace(d, feature_names=("other", "day", "row")),
    lambda d: replace(d, training_reference_at=d.reference_at[::-1]),
    lambda d: replace(d, target_close_at=d.target_close_at + timedelta(minutes=15)),
    lambda d: replace(d, label_available_at=d.target_close_at - timedelta(seconds=1)),
    lambda d: replace(d, manifest={**d.manifest, "target_version": "wrong"}),
    lambda d: replace(d, manifest={**d.manifest, "synthetic_policy": "include"}),
    lambda d: replace(d, manifest={**d.manifest, "feature_version": "incumbent"}),
    lambda d: replace(d, manifest={**d.manifest, "synthetic_labels_included": 1}),
])
def test_mismatched_inputs_are_rejected_before_fitting(monkeypatch, mutation):
    calls = fake_fitter(monkeypatch)
    with pytest.raises(ContractError):
        evaluate_observed_experiment(mutation(fixture_data()))
    assert not calls


def test_shared_incumbent_label_disagreement_is_rejected_even_before_split(monkeypatch):
    calls = fake_fitter(monkeypatch)
    dataset = fixture_data()
    wrong = dataset.y.copy()
    wrong[0] = 1 - wrong[0]
    with pytest.raises(ContractError, match="disagree"):
        evaluate_observed_experiment(dataset, incumbent_dataset=replace(dataset, y=wrong))
    assert not calls


@pytest.mark.parametrize("field", ["symbol", "source", "source_dataset"])
def test_mismatched_source_identity_is_rejected_before_any_fit(monkeypatch, field):
    calls = fake_fitter(monkeypatch)
    dataset = fixture_data()
    with pytest.raises(ContractError, match="identities"):
        evaluate_observed_experiment(dataset, incumbent_dataset=replace(dataset, inference={**dataset.inference, field: "different"}))
    assert not calls


@pytest.mark.parametrize("inference_only", [False, True])
def test_nonzero_actual_synthetic_fraction_column_is_rejected(monkeypatch, inference_only):
    calls = fake_fitter(monkeypatch)
    dataset = fixture_data()
    X = np.column_stack([dataset.X, np.zeros(len(dataset.X))])
    inference = np.column_stack([dataset.inference_X, np.zeros(1)])
    if inference_only:
        inference[0, -1] = .05
    else:
        X[0, -1] = .05
    dataset = replace(dataset, X=X, inference_X=inference,
                      feature_names=(*dataset.feature_names, "synthetic_input_fraction20"))
    with pytest.raises(ContractError, match="synthetic feature history"):
        evaluate_observed_experiment(dataset)
    assert not calls


def test_real_fitting_uses_disjoint_calibration_and_serializable_probability_scores():
    report = evaluate_observed_experiment(fixture_data(days=25))
    assert report["status"] == "scored", report["reasons"]
    assert report["elapsed_seconds"] < 120
    for block in report["blocks"]:
        for spec in fixed_specs():
            row = block["comparators"][spec.name]
            evidence = row["fitting"]["calibration"]
            assert evidence["calibration_rows"] >= 32
            assert instant(evidence["base_latest_target"]) <= instant(evidence["first_calibration_reference"])
            assert instant(evidence["base_latest_label_available_at"]) <= instant(evidence["first_calibration_reference"])
            assert 0 <= row["metrics"]["brier"] <= 1
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("config", [{"max_seconds": 121}, {"min_training_rows": True}, {"max_training_rows": 1}, {"max_training_rows": 4097}])
def test_invalid_budget_config_is_rejected(config):
    with pytest.raises(ContractError):
        ObservedResearchConfig(**config)
