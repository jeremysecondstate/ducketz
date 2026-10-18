"""Offline checks: fixture scores are never evidence of market qualification."""
from dataclasses import replace

from ml.intraday.model_selection import assess_frozen_ensemble
from ml.intraday.prediction import ContractError
import pytest
from tests.test_intraday_model_selection import fixture_data, small_config, spec, fake_fitter


def test_exact_members_are_assessed_without_supervised_shortlisting(monkeypatch):
    calls = fake_fitter(monkeypatch)
    dataset, sessions = fixture_data()
    members = [replace(spec("strong", fixture_probability=.9), training_max_rows=4096),
               replace(spec("weak", fixture_probability=.51), training_max_rows=4096)]
    report = assess_frozen_ensemble(dataset, models=members, config=small_config(), session_ids=sessions)
    assert len(calls) == 6
    assert all([m["name"] for m in p["models"]] == ["strong", "weak"] for p in report["partitions"])
    assert all(p["complete"] for p in report["partitions"])
    assert report["selected_specs"] == []
    assert report["holdout_use"] == "diagnostic_only"
    assert report["qualification"] != "fixed_recipe_gates_passed"
    assert report["policy_installation_authorized"] is False


def test_sparse_validation_keeps_descriptive_metrics_but_cannot_pass(monkeypatch):
    fake_fitter(monkeypatch)
    dataset, sessions = fixture_data()
    y = dataset.y.copy()
    y[-32:] = 1
    dataset = replace(dataset, y=y)
    report = assess_frozen_ensemble(dataset, models=[replace(spec(), training_max_rows=4096)],
        config=small_config(), session_ids=sessions, holdout_already_examined=False)
    held = report["partitions"][-1]
    assert held["validation_class_counts"] == {"0": 0, "1": 32}
    assert held["gate_reasons"] == ["holdout_class_0_below_3"]
    assert held["ensemble_metrics"] is not None
    assert held["scores_are_descriptive_only"]
    assert report["status"] == "insufficient_data"
    assert report["qualification"] == "not_qualified"


def test_missing_training_support_prevents_fits(monkeypatch):
    calls = fake_fitter(monkeypatch)
    dataset, sessions = fixture_data()
    report = assess_frozen_ensemble(dataset, models=[replace(spec(), training_max_rows=4096)],
        config=small_config(min_training_rows=1000), session_ids=sessions)
    assert calls == []
    assert report["status"] == "insufficient_data"
    assert all(not p["complete"] for p in report["partitions"])


def test_source_identity_changes_assessment_even_when_matrix_is_equal(monkeypatch):
    fake_fitter(monkeypatch)
    dataset, sessions = fixture_data()
    kwargs = dict(models=[replace(spec(), training_max_rows=4096)], config=small_config(), session_ids=sessions)
    first = assess_frozen_ensemble(dataset, **kwargs)
    changed = replace(dataset, manifest={**dataset.manifest, "source_history": {"receipt_sha256": "different"}})
    second = assess_frozen_ensemble(changed, **kwargs)
    assert first["dataset_sha256"] != second["dataset_sha256"]
    assert first["assessment_id"] != second["assessment_id"]
    assert first["source_binding"]["source_history_sha256"] != second["source_binding"]["source_history_sha256"]


def test_exact_manual_assessment_cannot_claim_another_member_selection(monkeypatch):
    fake_fitter(monkeypatch)
    dataset, sessions = fixture_data()
    dataset = replace(dataset, manifest={**dataset.manifest,
        "manual_model_recipe": {"sha256": "fixture", "models": [{"name": "other", "family": "logistic"}]}})
    with pytest.raises(ContractError, match="dataset's frozen recipe"):
        assess_frozen_ensemble(dataset, models=[replace(spec(), training_max_rows=4096)],
            config=small_config(), session_ids=sessions)
