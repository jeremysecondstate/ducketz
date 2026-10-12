from __future__ import annotations

from dataclasses import asdict, replace
import json

import numpy as np
import pandas as pd
import pytest

from ml.loop_b_research import (
    PrivateExperimentStore, ResearchConfig, feature_coverage, fit_train_preprocessor,
    hash_files, ids_receipt, make_purged_folds, paired_uncertainty,
    probability_metrics, run_experiment, target_weights, validate_cohort,
)


def cohort(days=90, symbols=("A", "B")):
    rng = np.random.default_rng(41)
    records = []
    for day in range(days):
        start = pd.Timestamp("2020-01-01T14:30:00Z") + pd.Timedelta(days=day)
        for symbol in symbols:
            base, activity, path, interaction = rng.normal(size=4)
            records.append({"sample_id": f"{day}:{symbol}", "symbol": symbol,
                "decision_at": start - pd.Timedelta(hours=1), "target_start": start,
                "target_end": start + pd.Timedelta(hours=4),
                "label_available_at": start + pd.Timedelta(hours=4, minutes=5),
                "y": int(base + 0.7 * activity + rng.normal() > 0),
                "base": base, "activity": activity, "path": path, "interaction": interaction})
    return pd.DataFrame(records)


def config(**kwargs):
    return replace(ResearchConfig(), min_train_dates=6, min_validation_dates=3,
                   bootstrap_repetitions=100, **kwargs)


def store(tmp_path, name="run"):
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    return PrivateExperimentStore(scratch / name, private_parent=scratch)


def run_args(tmp_path):
    source = tmp_path / "source.py"
    source.write_text("# frozen synthetic source\n")
    data = tmp_path / "inputs.txt"
    data.write_text("synthetic input evidence only\n")
    return {"baseline_columns": ["base"],
            "families": {"activity": ["activity"], "path": ["path"], "interaction": ["interaction"]},
            "excluded_ids": ["previously-protected:sample"], "development_end": "2021-01-01T00:00:00Z",
            "source_hashes": hash_files([source]), "input_hashes": hash_files([data]),
            "reference": {"cohort": "synthetic research fixture, not an original fit reproduction"},
            "config": config()}


def test_cohort_rejects_protected_and_unknown_time_semantics():
    data = cohort()
    with pytest.raises(ValueError, match="protected"):
        validate_cohort(data, excluded_ids=[data.sample_id.iloc[0]], development_end="2021-01-01T00:00Z")
    naive = data.copy()
    naive["decision_at"] = naive.decision_at.dt.tz_localize(None)
    with pytest.raises(ValueError, match="timezone"):
        validate_cohort(naive, excluded_ids=[], development_end="2021-01-01T00:00Z")
    with pytest.raises(ValueError, match="boundary"):
        validate_cohort(data, excluded_ids=[], development_end=data.target_end.iloc[-1])
    with pytest.raises(ValueError, match="unique"):
        validate_cohort(pd.concat([data, data.iloc[:1]]), excluded_ids=[], development_end="2021-01-01T00:00Z")


def test_purging_uses_label_availability_and_groups_all_symbols_and_decisions():
    data = cohort()
    # Mimic a delayed observed label even though the outcome window ended.
    data["label_available_at"] += pd.Timedelta(days=4)
    folds = make_purged_folds(data, config())
    assert len(folds) == 3
    lookup = data.set_index("sample_id")
    for fold in folds:
        train = lookup.loc[list(fold.train_ids)]
        validation = lookup.loc[list(fold.validation_ids)]
        assert train.label_available_at.max() < validation.decision_at.min()
        assert not set(train.target_start) & set(validation.target_start)
        for start in validation.target_start.unique():
            assert set(data.loc[data.target_start == start, "sample_id"]) <= set(fold.validation_ids)
        assert fold.purged_ids
    assert len(folds[0].train_ids) < len(data) // 2  # Purged after nominal half, not min-train split.


def test_outcome_overlap_trimmed_between_validation_folds():
    data = cohort()
    data["target_end"] += pd.Timedelta(days=5)
    data["label_available_at"] += pd.Timedelta(days=5)
    folds = make_purged_folds(data, config())
    lookup = data.set_index("sample_id")
    for left, right in zip(folds, folds[1:]):
        earlier = lookup.loc[list(left.validation_ids)]
        later = lookup.loc[list(right.validation_ids)]
        assert earlier.target_end.max() < later.decision_at.min()
    assert len(folds) == 3
    assert folds[0].validation_purged_ids
    assert not set(folds[0].validation_purged_ids) & set(folds[0].validation_ids)
    omitted = lookup.loc[list(folds[0].validation_purged_ids)]
    next_validation = lookup.loc[list(folds[1].validation_ids)]
    assert (omitted.target_end >= next_validation.decision_at.min()).all()


def test_insufficient_dates_are_not_rescued_by_pooled_rows():
    data = cohort(days=10, symbols=tuple(str(i) for i in range(100)))
    assert not make_purged_folds(data, ResearchConfig())


def test_preprocessor_is_train_only_including_semantic_cap_and_admission():
    data = pd.DataFrame({"volume": [0.0, 1.0, 3.0, 8.0, np.nan],
                         "future_only": [np.nan] * 5, "constant": [7] * 5,
                         "copy": [0.0, 1.0, 3.0, 8.0, np.nan]})
    versions = {"volume": "log1p-capped-training-v1", "future_only": "identity-v1",
                "constant": "identity-v1", "copy": "log1p-capped-training-v1"}
    fitted = fit_train_preprocessor(data, list(data), config(), versions)
    before = asdict(fitted)
    future = pd.DataFrame({"volume": [1e30], "future_only": [2], "constant": [9], "copy": [1e30]})
    transformed = fitted.transform(future)
    assert asdict(fitted) == before
    assert fitted.columns == ["volume"]
    assert fitted.semantic_caps["volume"] < 8
    assert np.isfinite(transformed).all()
    assert fitted.rejected == {"future_only": "insufficient_training_availability",
                               "constant": "constant_in_training", "copy": "exact_duplicate_in_training"}
    # Identical training prefixes yield identical transforms regardless of future.
    other = fit_train_preprocessor(pd.concat([data, future]).iloc[:len(data)], list(data), config(), versions)
    assert asdict(other) == before


def test_coverage_reports_empty_constant_redundant_and_per_period():
    data = cohort()
    data["empty"] = np.nan
    data["constant"] = 8.0
    data["copy"] = data.base
    report = feature_coverage(data, ["base", "empty", "constant", "copy"])
    assert report["empty"] == ["empty"]
    assert report["constant_nonmissing"] == ["constant"]
    assert report["exact_redundant"] == {"copy": "base"}
    assert report["rows_all_finite"] == 0
    assert sum(part["rows"] for part in report["by_symbol"].values()) == len(data)
    assert len(report["by_month"]) == 3


def test_weights_and_metrics_do_not_count_repeated_symbol_decisions_as_independent():
    data = cohort(days=1)
    data["probability"] = [0.8, 0.2]
    data["y"] = [1, 1]
    extra = pd.concat([data.iloc[[0]]] * 10, ignore_index=True)
    extra["sample_id"] = [f"extra:{i}" for i in range(len(extra))]
    repeated = pd.concat([data, extra], ignore_index=True)
    weights = target_weights(repeated)
    assert weights[repeated.symbol.eq("A")].sum() == pytest.approx(0.5)
    assert weights[repeated.symbol.eq("B")].sum() == pytest.approx(0.5)
    expected = probability_metrics(data)
    actual = probability_metrics(repeated)
    assert actual["logloss"] == pytest.approx(expected["logloss"])
    assert actual["brier"] == pytest.approx(expected["brier"])
    assert actual["target_clusters"] == 1


def test_uncertainty_is_paired_and_insufficient_short_history_is_explicit():
    baseline = cohort(days=60)
    baseline["fold"] = "fold-1"
    baseline["probability"] = 0.5
    candidate = baseline.copy()
    candidate["probability"] = np.where(candidate.y == 1, 0.6, 0.4)
    estimated = paired_uncertainty(baseline, candidate, config(), comparisons=36)
    assert estimated["uncertainty_status"] == "estimated"
    assert estimated["logloss_upper_bound"] < 0
    assert estimated["adjusted_one_sided_alpha"] == pytest.approx(0.05 / 36)
    assert estimated["observed_days"] == 60
    short = paired_uncertainty(baseline.iloc[:20], candidate.iloc[:20], config(), comparisons=36)
    assert short["logloss_upper_bound"] is None
    with pytest.raises(ValueError, match="identical sample"):
        paired_uncertainty(baseline, candidate.iloc[:-1], config(), comparisons=36)


def test_private_writer_rejects_production_roots_traversal_and_overwrite(tmp_path):
    private = store(tmp_path)
    private.write_json("evidence.json", {"ok": True})
    with pytest.raises(FileExistsError):
        private.write_json("evidence.json", {"ok": False})
    with pytest.raises(ValueError, match="contained"):
        private.write_json("../escape.json", {})
    with pytest.raises(ValueError, match="production/pointer"):
        PrivateExperimentStore(tmp_path / "scratch/latest", private_parent=tmp_path / "scratch")
    with pytest.raises(ValueError, match="forbidden"):
        PrivateExperimentStore(tmp_path / "scratch/production-data", private_parent=tmp_path / "scratch",
                               forbidden_roots=[tmp_path / "scratch/production-data"])
    with pytest.raises(ValueError, match="private parent"):
        PrivateExperimentStore(tmp_path / "models", private_parent=tmp_path)


def test_private_writer_rejects_symlink_escape(tmp_path):
    private = store(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (private.root / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Windows symlink privilege unavailable")
    with pytest.raises(ValueError, match="symlink"):
        private.write_json("link/escape.json", {})
    assert not (outside / "escape.json").exists()


def test_run_freezes_exact_membership_and_persists_reproducible_matched_models(tmp_path):
    args = run_args(tmp_path)
    private = store(tmp_path)
    args["transform_versions"] = {name: "identity-v1" for name in ["base", "activity", "path", "interaction"]}
    results = run_experiment(cohort(), store=private, **args)
    assert results["input_source_stability"] == "verified_before_and_after"
    assert len(results["attempts"]) == 15
    assert all(attempt["status"] == "succeeded" for attempt in results["attempts"])
    spec = json.loads((private.root / "specification.json").read_text())
    assert spec["config"]["total_comparisons"] == 36
    assert spec["eligible_membership"] == ids_receipt(cohort().sample_id)
    assert spec["protected_exclusions"]["ids"] == ["previously-protected:sample"]
    predictions = pd.read_csv(private.root / "predictions.csv")
    for _, rows in predictions.groupby("fold"):
        memberships = [set(part.sample_id) for _, part in rows.groupby("variant")]
        assert all(ids == memberships[0] for ids in memberships)
    model = json.loads((private.root / "models/fold-1-all.json").read_text())
    assert model["kind"] == "logistic_regression"
    assert set(model["preprocessor"]) >= {"median", "lower", "upper", "mean", "scale", "semantic_caps"}
    assert len(model["coefficients"][0]) == len(model["preprocessor"]["columns"])
    # Frozen specification is written before the first started attempt.
    assert (private.root / "specification.json").stat().st_mtime_ns <= (private.root / "attempts/fold-1-baseline-started.json").stat().st_mtime_ns


def test_changed_input_invalidates_completed_predictions(tmp_path, monkeypatch):
    import ml.loop_b_research as module
    args = run_args(tmp_path)
    private = store(tmp_path)
    original = module.LogisticRegression.fit
    changed = False

    def fit_and_change(self, *fit_args, **kwargs):
        nonlocal changed
        result = original(self, *fit_args, **kwargs)
        if not changed:
            (tmp_path / "inputs.txt").write_text("unexpected concurrent input mutation")
            changed = True
        return result

    monkeypatch.setattr(module.LogisticRegression, "fit", fit_and_change)
    with pytest.raises(ValueError, match="changed"):
        run_experiment(cohort(), store=private, **args)
    assert (private.root / "invalidated.json").exists()
    assert not (private.root / "results.json").exists()


def test_future_target_feature_is_rejected_before_fitting(tmp_path):
    data = cohort()
    data["future_return"] = data.y
    args = run_args(tmp_path)
    args["families"] = {"activity": ["future_return"]}
    with pytest.raises(ValueError, match="prohibited"):
        run_experiment(data, store=store(tmp_path), **args)


def test_frozen_boundary_is_exclusive_even_without_label_delay():
    data = cohort().drop(columns="label_available_at")
    with pytest.raises(ValueError, match="boundary"):
        validate_cohort(data, excluded_ids=[], development_end=data.target_end.max())


def test_insufficient_history_records_each_predeclared_disposition(tmp_path):
    args = run_args(tmp_path)
    args["config"] = ResearchConfig(bootstrap_repetitions=100, total_comparisons=4)
    private = store(tmp_path)
    results = run_experiment(cohort(days=10), store=private, **args)
    assert results["status"] == "insufficient_history"
    assert results["attempts"] == []
    assert set(results["comparisons"]) == {"activity", "path", "interaction", "all"}
    assert all(value["disposition"] == "inconclusive" for value in results["comparisons"].values())
    specification = json.loads((private.root / "specification.json").read_text())
    assert "all 4 predeclared comparisons" in specification["acceptance_rule"]
