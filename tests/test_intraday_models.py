"""Offline contract tests; fixture fit probabilities are not performance evidence."""
from datetime import datetime, timedelta, timezone
import importlib.util

import joblib
import numpy as np
import pytest

from ml.intraday.models import (
    DEFAULT_MODELS, DEFAULT_PARAMETERS, ModelSpec, build_estimator, configured_models, fit_ensemble,
    probability_not_down, purged_chronological_split, registry_status,
)
from ml.intraday.prediction import ContractError, TARGET_VERSION


def dataset(n=160):
    rng = np.random.default_rng(42)
    X = rng.normal(size=(n, 4))
    y = (X[:, 0] + 0.25 * X[:, 1] >= 0).astype(np.uint8)
    references = [datetime(2026, 10, 1, 12, tzinfo=timezone.utc) + i * timedelta(minutes=15) for i in range(n)]
    targets = [v + timedelta(minutes=45) for v in references]
    available = [v + timedelta(seconds=10) for v in targets]
    return X, y, X[-1:].copy(), {"training_cutoff": available[-1], "label_available_at": available,
                                "reference_at": references, "target_close_at": targets}


def test_initial_ten_are_real_independent_fits_and_equal_mean():
    if importlib.util.find_spec("xgboost") is None:
        pytest.skip("Initial ten include explicit optional XGBoost dependency")
    X, y, inference, kwargs = dataset()
    result = fit_ensemble(X, y, inference, **kwargs)
    assert result["configured_count"] == result["successful_count"] == 10
    assert tuple(o["name"] for o in result["outputs"]) == DEFAULT_MODELS
    assert result["complete"] and result["consensus_status"] == "full"
    assert result["probability_not_down"] == pytest.approx(np.mean([o["probability_not_down"] for o in result["outputs"]]))
    for output in result["outputs"]:
        assert 0 <= output["probability_not_down"] <= 1
        assert output["metadata"]["target_version"] == TARGET_VERSION
        assert output["metadata"]["full_retrain"] is True
        assert output["metadata"]["internal_workers"] == 1
        assert output["metadata"]["training_rows"] == len(y)
        assert output["training_seconds"] > 0 and output["inference_seconds"] > 0
        assert len(output["version"]) == 64


@pytest.mark.parametrize("family", ["knn", "lda", "qda", "gaussian_nb", "lightgbm", "mlp"])
def test_additional_real_probability_adapters(family):
    if family == "lightgbm" and importlib.util.find_spec("lightgbm") is None:
        pytest.skip("LightGBM absent")
    X, y, inference, kwargs = dataset()
    result = fit_ensemble(X, y, inference, config=[family], **kwargs)
    assert result["complete"], result["outputs"]
    assert 0 <= result["probability_not_down"] <= 1


@pytest.mark.parametrize("rows", [2, 7, 8, 9, 10, 14, 15, 16])
def test_knn_uses_available_causal_rows_and_records_effective_neighbors(rows):
    X, _, inference, kwargs = dataset(rows)
    y = np.arange(rows) % 2
    result = fit_ensemble(X, y, inference + 0.1, config=["knn"], **kwargs)
    output = result["outputs"][0]
    assert result["complete"], output
    assert 0 <= output["probability_not_down"] <= 1
    assert output["metadata"]["training_rows"] == rows
    assert output["metadata"]["requested_n_neighbors"] == 15
    assert output["metadata"]["neighbor_count_policy"] == "cap_to_training_rows"
    assert output["metadata"]["effective_parameters"]["n_neighbors"] == min(15, rows)
    assert output["metadata"]["spec"]["parameters"] == {}
    assert DEFAULT_PARAMETERS["knn"]["n_neighbors"] == 15


@pytest.mark.parametrize("requested", [3, 7, 20])
def test_knn_custom_request_matches_saved_estimator_and_metadata(tmp_path, requested):
    X, _, inference, kwargs = dataset(7)
    y = np.arange(7) % 2
    spec = ModelSpec("custom_knn", family="knn", parameters={"n_neighbors": requested})
    result = fit_ensemble(X, y, inference + 0.1, config=[spec], artifact_dir=tmp_path,
                          run_id="knn-small-snapshot", **kwargs)
    output = result["outputs"][0]
    assert result["complete"], output
    saved = joblib.load(output["artifact_path"])
    assert saved["estimator"].named_steps["model"].n_neighbors == min(requested, 7)
    assert saved["metadata"] == output["metadata"]
    assert saved["version"] == output["version"]
    assert saved["metadata"]["effective_parameters"]["n_neighbors"] == min(requested, 7)
    assert saved["metadata"]["requested_n_neighbors"] == requested
    assert saved["metadata"]["spec"]["parameters"] == spec.parameters == {"n_neighbors": requested}
    assert probability_not_down(saved["estimator"], inference + 0.1) == pytest.approx(output["probability_not_down"])


@pytest.mark.parametrize("requested", [0, -1, 1.5, 20.5, True, "15", None])
def test_knn_invalid_neighbor_request_is_not_repaired_by_clamping(requested):
    X, _, inference, kwargs = dataset(7)
    spec = ModelSpec("invalid_knn", family="knn", parameters={"n_neighbors": requested})
    result = fit_ensemble(X, np.arange(7) % 2, inference, config=[spec], **kwargs)
    output = result["outputs"][0]
    assert not result["complete"] and result["probability_not_down"] is None
    assert output["error"]["phase"] == "training"
    assert output["error"]["type"] == "ContractError"
    assert "positive integer" in output["error"]["message"]
    assert output["artifact_path"] is None


def test_learned_preprocessing_is_fitted_only_on_training_rows(tmp_path):
    X, y, inference, kwargs = dataset()
    X[0, 0] = np.nan
    inference[:] = 1e6
    result = fit_ensemble(X, y, inference, config=["logistic"], artifact_dir=tmp_path, run_id="fresh-cycle", **kwargs)
    fitted = joblib.load(result["outputs"][0]["artifact_path"])["estimator"]
    assert fitted.named_steps["impute"].statistics_[0] == pytest.approx(np.nanmedian(X[:, 0]))
    imputed = fitted.named_steps["impute"].transform(X)
    assert np.allclose(fitted.named_steps["scale"].mean_, imputed.mean(axis=0))
    assert not np.allclose(fitted.named_steps["scale"].mean_, inference[0])


def test_artifacts_and_versions_bind_exact_training_snapshot_and_cannot_overwrite(tmp_path):
    X, y, inference, kwargs = dataset()
    first = fit_ensemble(X, y, inference, config=["logistic"], artifact_dir=tmp_path, run_id="cycle-a", **kwargs)
    out = first["outputs"][0]
    artifact = joblib.load(out["artifact_path"])
    assert artifact["version"] == out["version"]
    assert probability_not_down(artifact["estimator"], inference) == pytest.approx(out["probability_not_down"])
    repeated = fit_ensemble(X, y, inference, config=["logistic"], artifact_dir=tmp_path, run_id="cycle-a", **kwargs)
    assert not repeated["complete"]
    assert repeated["outputs"][0]["error"]["type"] == "FileExistsError"
    assert repeated["probability_not_down"] is None
    X[0, 0] += 0.25
    changed = fit_ensemble(X, y, inference, config=["logistic"], artifact_dir=tmp_path, run_id="cycle-b", **kwargs)
    assert changed["outputs"][0]["version"] != out["version"]


def test_training_cutoff_rejects_unavailable_label_and_wrong_target():
    X, y, inference, kwargs = dataset()
    kwargs["training_cutoff"] -= timedelta(seconds=1)
    with pytest.raises(ContractError, match="observable"):
        fit_ensemble(X, y, inference, config=["logistic"], **kwargs)
    kwargs["training_cutoff"] += timedelta(seconds=1)
    with pytest.raises(ContractError, match="not-down target"):
        fit_ensemble(X, y, inference, config=["logistic"], target_version="legacy-strict-positive", **kwargs)


def test_model_failure_is_saved_and_incomplete_consensus_is_explicit():
    X, y, inference, kwargs = dataset()
    result = fit_ensemble(X, y, inference, config=["logistic", "garn"], **kwargs)
    assert result["configured_count"] == 2 and result["successful_count"] == 1
    assert result["consensus_status"] == "incomplete" and not result["complete"]
    assert result["probability_not_down"] == result["outputs"][0]["probability_not_down"]
    assert result["outputs"][1]["error"]["type"] == "UnsupportedModelError"
    assert result["outputs"][1]["probability_not_down"] is None
    assert result["execution_eligibility"] == "unresolved_shadow_only"


def test_single_class_has_failure_without_constant_model_fallback():
    X, y, inference, kwargs = dataset()
    result = fit_ensemble(X, np.ones_like(y), inference, config=["logistic", "gaussian_nb", "knn"], **kwargs)
    assert result["consensus_status"] == "failed" and result["successful_count"] == 0
    assert result["probability_not_down"] is None
    assert all(o["error"]["type"] == "ContractError" for o in result["outputs"])


@pytest.mark.parametrize("family", ["ridge", "passive_aggressive"])
def test_non_probability_adapters_use_purged_chronological_calibration(family):
    X, y, inference, kwargs = dataset()
    result = fit_ensemble(X, y, inference, config=[family], **kwargs)
    assert result["complete"], result["outputs"]
    calibration = result["outputs"][0]["metadata"]["calibration"]
    assert calibration["method"] == "purged_chronological_sigmoid"
    assert calibration["purged_rows"] == 2
    assert calibration["base_latest_target"] <= calibration["first_calibration_reference"]
    kwargs.pop("target_close_at")
    failed = fit_ensemble(X, y, inference, config=[family], **kwargs)
    assert failed["outputs"][0]["error"]["type"] == "ContractError"


def test_split_preserves_chronology_and_purges_overlapping_future_windows():
    _, _, _, kwargs = dataset(40)
    train, calibration = purged_chronological_split(kwargs["reference_at"], kwargs["target_close_at"])
    assert kwargs["target_close_at"][train[-1]] <= kwargs["reference_at"][calibration[0]]
    assert train[-1] < calibration[0]
    kwargs["reference_at"].reverse()
    with pytest.raises(ContractError, match="chronological"):
        purged_chronological_split(kwargs["reference_at"], kwargs["target_close_at"])


def test_probability_validation_uses_class_identity_and_rejects_bad_probabilities():
    class Reversed:
        classes_ = np.array([1, 0])

        def predict_proba(self, X):
            return np.array([[0.7, 0.3]])

    assert probability_not_down(Reversed(), [[0]]) == 0.7
    class Invalid(Reversed):
        def predict_proba(self, X):
            return np.array([[np.nan, 1]])
    with pytest.raises(ContractError, match="invalid probability"):
        probability_not_down(Invalid(), [[0]])


def test_unsupported_architectures_are_explicit_and_configuration_forbids_warm_start():
    status = {s["family"]: s for s in registry_status()}
    assert status["lstm_attention"]["status"] == "adapter_not_implemented"
    assert status["garn"]["status"] == "adapter_not_implemented"
    assert status["catboost"]["status"] in {"available", "dependency_unavailable"}
    with pytest.raises(ContractError, match="Warm-start"):
        build_estimator(ModelSpec("warm", family="random_forest", parameters={"warm_start": True}))
    with pytest.raises(ContractError, match="unique"):
        configured_models(["lda", "lda"])


def test_registry_import_has_no_broker_dependencies():
    from pathlib import Path
    import ml.intraday.models as models
    source = Path(models.__file__).read_text(encoding="utf-8")
    assert "ml.account_gameplan" not in source
    assert "stock_trader" not in source
    assert "schwab" not in source.lower()
