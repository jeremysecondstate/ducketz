"""CatBoost stage dispatch and frozen contracts, with no numerical fitting."""
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sys

import joblib
import numpy as np
import pytest
from sklearn.base import clone

from ml.intraday import models
from ml.intraday.prediction import ContractError


NATIVE = "catboost.core.CatBoostClassifier"
ADAPTER = "ml.intraday.catboost_adapter.SingleWorkerCatBoostClassifier"


@pytest.fixture(autouse=True)
def forbid_numerical_fitting(monkeypatch):
    """Every fit in this module uses an explicit dispatch fake."""
    catboost = pytest.importorskip("catboost")
    def forbidden(*args, **kwargs):
        pytest.fail("This contract suite must never run numerical model fitting")
    monkeypatch.setattr(catboost.CatBoost, "_train", forbidden)
    for constructor in (models.LogisticRegression, models.RandomForestClassifier,
                        models.GaussianNB, models.SimpleImputer, models.StandardScaler):
        monkeypatch.setattr(constructor, "fit", forbidden)


def _native_spec(*, calibration="none"):
    """Reconstruct an original native constructor contract without fitting it."""
    catboost = pytest.importorskip("catboost")
    spec = models.ModelSpec("original-native", family="catboost", parameters={"iterations": 7},
                            probability_calibration=calibration)
    estimator = catboost.CatBoostClassifier(**models._parameters(spec))
    contract = dict(version=models.RUNTIME_CONTRACT_VERSION, estimator_class=NATIVE,
        preprocessing=dict(impute=models._resolved_step(models.SimpleImputer(
            strategy="median", keep_empty_features=True)), scale=None),
        calibrator=(models._resolved_step(models.LogisticRegression(C=1.0, max_iter=1000,
                    random_state=spec.random_state)) if calibration == "sigmoid" else None),
        dependency_versions=models._versions(spec))
    return replace(spec, parameters=models._encode_parameters(estimator.get_params(deep=False)),
                   runtime_contract=contract)


def _stage_fakes(monkeypatch):
    catboost = pytest.importorskip("catboost")
    events = []
    class FakePool:
        def __init__(self, data, label=None, weight=None, thread_count=-1):
            self.data, self.label, self.weight = data, label, weight
            events.append(("pool", thread_count))
    def fake_fit(self, X, y=None, sample_weight=None):
        pool = X if isinstance(X, FakePool) else FakePool(X, label=y, weight=sample_weight)
        assert np.asarray(pool.data).shape == (4, 2)
        assert list(pool.label) == [0, 0, 1, 1]
        events.append(("fit", self.get_params()["thread_count"]))
        self.get_feature_importance()
        return self
    def fake_importance(self, data=None, type=None, prettified=False, thread_count=-1,
                        *args, **kwargs):
        events.append(("importance", thread_count))
        return np.array([.75, .25])
    def fake_probability(self, X, ntree_start=0, ntree_end=0, thread_count=-1,
                         verbose=None, task_type="CPU"):
        events.append(("predict_proba", thread_count))
        return np.tile([.25, .75], (len(X), 1))
    def fake_predict(self, data, prediction_type="Class", ntree_start=0, ntree_end=0,
                     thread_count=-1, verbose=None, task_type="CPU"):
        events.append(("predict", thread_count))
        return np.ones(len(data), dtype=int)
    monkeypatch.setattr(catboost, "Pool", FakePool)
    monkeypatch.setattr(catboost.CatBoostClassifier, "fit", fake_fit)
    monkeypatch.setattr(catboost.CatBoostClassifier, "get_feature_importance", fake_importance)
    monkeypatch.setattr(catboost.CatBoostClassifier, "predict_proba", fake_probability)
    monkeypatch.setattr(catboost.CatBoostClassifier, "predict", fake_predict)
    adapter = sys.modules.get("ml.intraday.catboost_adapter")
    if adapter is not None:
        monkeypatch.setattr(adapter, "Pool", FakePool)
    return events


@pytest.mark.parametrize("frozen", [False, True], ids=["new-spec", "original-native-spec"])
def test_all_selected_native_stages_obey_existing_single_worker_policy(monkeypatch, frozen):
    events = _stage_fakes(monkeypatch)
    spec = _native_spec() if frozen else models.ModelSpec("fresh", family="catboost")
    estimator = models.build_estimator(spec).named_steps["model"]
    X, y = np.arange(8).reshape(4, 2), [0, 0, 1, 1]
    estimator.fit(X, y, sample_weight=np.ones(4))
    np.testing.assert_array_equal(estimator.predict_proba(X[:2], thread_count=-1),
                                  [[.25, .75], [.25, .75]])
    np.testing.assert_array_equal(estimator.predict(X[:2], thread_count=-1), [1, 1])
    estimator.get_feature_importance(None, thread_count=-1)
    assert {stage for stage, count in events} == {
        "pool", "fit", "importance", "predict_proba", "predict"}
    assert all(count == 1 for stage, count in events), events


@pytest.mark.parametrize("calibration", ["none", "sigmoid"])
def test_old_native_contract_parameters_calibrator_and_identity_are_preserved(calibration, tmp_path):
    original = _native_spec(calibration=calibration)
    before = models._json(asdict(original))
    pipeline = models.build_estimator(original)
    assert models._constructor_identity(pipeline.named_steps["model"]) == ADAPTER
    assert original.runtime_contract["estimator_class"] == NATIVE
    assert models._json(asdict(models.freeze_model_spec(original))) == before
    assert models._json(asdict(original)) == before
    copied = clone(pipeline)
    assert type(copied.named_steps["model"]) is type(pipeline.named_steps["model"])
    assert copied.named_steps["model"].get_params() == pipeline.named_steps["model"].get_params()
    assert not copied.named_steps["model"].is_fitted()
    path = tmp_path / "untrained-original-contract.joblib"
    joblib.dump(dict(spec=original, estimator=pipeline), path)
    restored = joblib.load(path)
    assert models._json(asdict(restored["spec"])) == before
    assert models._constructor_identity(restored["estimator"].named_steps["model"]) == ADAPTER
    assert [(name, models._resolved_step(step)) for name, step in restored["estimator"].steps] == [
        (name, models._resolved_step(step)) for name, step in pipeline.steps]


def test_new_frozen_contract_records_actual_adapter_identity_and_roundtrips(tmp_path):
    frozen = models.freeze_model_spec(models.ModelSpec("new", family="catboost",
                                                       parameters={"iterations": 7}))
    assert frozen.runtime_contract["estimator_class"] == ADAPTER
    pipeline = models.build_estimator(frozen)
    assert models._constructor_identity(pipeline.named_steps["model"]) == ADAPTER
    path = tmp_path / "untrained-new-contract.joblib"
    joblib.dump(dict(spec=frozen, estimator=clone(pipeline)), path)
    restored = joblib.load(path)
    assert asdict(restored["spec"]) == asdict(frozen)
    assert type(restored["estimator"].named_steps["model"]) is type(pipeline.named_steps["model"])


def test_loading_untouched_historical_native_artifact_keeps_native_class(tmp_path):
    catboost = pytest.importorskip("catboost")
    path = tmp_path / "historical-native-untrained.joblib"
    native = catboost.CatBoostClassifier(**models._parameters(_native_spec()))
    joblib.dump(native, path)
    original_bytes = path.read_bytes()
    restored = joblib.load(path)
    assert models._constructor_identity(restored) == NATIVE
    assert restored.get_params() == native.get_params()
    assert path.read_bytes() == original_bytes


@pytest.mark.parametrize("calibration", ["none", "sigmoid"])
def test_valid_market_projection_preserves_original_native_contract(calibration, monkeypatch):
    from ml.intraday.feature_support import PROJECTION_POLICY
    original = _native_spec(calibration=calibration)
    before = deepcopy(asdict(original))
    projected = models.with_market_feature_projection(original)
    expected = deepcopy(before)
    expected["runtime_contract"]["market_feature_projection"] = PROJECTION_POLICY
    assert asdict(projected) == expected
    assert asdict(original) == before
    monkeypatch.setitem(models.DEFAULT_PARAMETERS, "catboost", {"iterations": 999,
        "thread_count": -1, "depth": 10})
    pipeline = models.build_estimator(projected)
    assert models._constructor_identity(pipeline.named_steps["model"]) == ADAPTER
    assert pipeline.named_steps["model"].get_params()["iterations"] == 7
    assert pipeline.named_steps["model"].get_params()["thread_count"] == 1
    assert asdict(models.freeze_model_spec(projected)) == expected


@pytest.mark.parametrize("damage", ["unrecognized-class", "lookalike-class", "null-class",
    "false-class", "list-class", "dict-class", "dependency",
    "extra-parameter", "worker-count", "calibrator", "preprocessing", "projection"])
def test_native_compatibility_does_not_weaken_other_frozen_checks(damage):
    original = _native_spec()
    contract = deepcopy(original.runtime_contract)
    params = deepcopy(original.parameters)
    if damage == "unrecognized-class":
        contract["estimator_class"] = "catboost.core.CatBoost"
    elif damage == "lookalike-class":
        contract["estimator_class"] = "other.SingleWorkerCatBoostClassifier"
    elif damage in {"null-class", "false-class", "list-class", "dict-class"}:
        contract["estimator_class"] = {"null-class": None, "false-class": False,
                                       "list-class": [], "dict-class": {}}[damage]
    elif damage == "dependency":
        contract["dependency_versions"]["catboost"] = "unreviewed-version"
    elif damage == "extra-parameter":
        params["new_unreviewed_parameter"] = 1
    elif damage == "worker-count":
        params["thread_count"] = -1
    elif damage == "calibrator":
        contract["calibrator"] = models._resolved_step(models.LogisticRegression())
    elif damage == "preprocessing":
        contract["preprocessing"]["impute"]["class"] = "unreviewed.Imputer"
    else:
        contract["market_feature_projection"] = "unreviewed-projection"
    with pytest.raises((ContractError, TypeError)):
        models.build_estimator(replace(original, parameters=params, runtime_contract=contract))


def test_exact_adapter_type_required_and_native_identity_is_never_spoofed():
    adapter = importlib.import_module("ml.intraday.catboost_adapter")
    original = _native_spec()
    class UnreviewedSubclass(adapter.SingleWorkerCatBoostClassifier):
        pass
    with pytest.raises(ContractError):
        models._frozen_pipeline(original, UnreviewedSubclass(**models._parameters(original)))
    assert models._constructor_identity(adapter.SingleWorkerCatBoostClassifier()) == ADAPTER
    assert models._constructor_identity(importlib.import_module("catboost").CatBoostClassifier()) == NATIVE


@pytest.mark.parametrize("family", ["logistic", "gaussian_nb", "random_forest"])
def test_other_frozen_family_rejects_catboost_native_class(family):
    original = models.freeze_model_spec(models.ModelSpec("unchanged", family=family))
    contract = {**original.runtime_contract, "estimator_class": NATIVE}
    with pytest.raises(ContractError):
        models.build_estimator(replace(original, runtime_contract=contract))


@pytest.mark.parametrize("failure", [False, True], ids=["mock-success", "mock-failure"])
def test_adapter_evidence_preserves_original_spec_and_actual_implementation(monkeypatch, tmp_path, failure):
    original = _native_spec()
    before = models._json(asdict(original))
    class FakeFitted:
        classes_ = np.array([0, 1])
        def predict_proba(self, X):
            return np.tile([.25, .75], (len(X), 1))
    def fake_fit(spec, X, y, **kwargs):
        assert models._json(asdict(spec)) == before
        if failure:
            raise RuntimeError("explicit fake model failure")
        return FakeFitted(), {"calibration": None}
    monkeypatch.setattr(models, "fit_calibrated_estimator", fake_fit)
    # Avoid pickling a local test class; verify the exact artifact metadata input.
    saved = {}
    def fake_export(path, payload):
        saved.update(payload)
        return "f" * 64
    monkeypatch.setattr(models, "_export_artifact", fake_export)
    cutoff = "2026-10-01T00:00:00Z"
    result = models.fit_ensemble([[-2.], [-1.], [1.], [2.]], [0, 0, 1, 1], [[.5]],
        config=[original], training_cutoff=cutoff, label_available_at=[cutoff] * 4,
        artifact_dir=tmp_path, run_id="mock-artifact")
    output = result["outputs"][0]
    assert output["status"] == ("failed" if failure else "success")
    assert models._json(output["metadata"]["spec"]) == before
    evidence = output["metadata"]["model_adapter"]
    assert evidence["version"] == "intraday-catboost-single-worker-v1"
    assert evidence["estimator_class"] == ADAPTER
    assert evidence["frozen_estimator_class"] == NATIVE
    assert evidence["compatibility"] == "recorded-native-catboost-single-worker-adapter"
    adapter = importlib.import_module("ml.intraday.catboost_adapter")
    assert evidence["source_sha256"] == hashlib.sha256(Path(adapter.__file__).read_bytes()).hexdigest()
    assert evidence["source_hash_basis"] == "current_disk_bytes"
    assert "loaded_functions_match_disk" not in evidence
    if failure:
        assert output["error"]["message"] == "explicit fake model failure" and not saved
    else:
        assert saved["metadata"]["model_adapter"] == evidence
        assert saved["metadata"]["spec"] == asdict(original)


def test_optional_dependency_or_adapter_failure_stays_inside_affected_model(monkeypatch):
    import builtins
    original_find = models.importlib.util.find_spec
    original_import = builtins.__import__
    def fake_fit(spec, X, y, **kwargs):
        estimator = models.build_estimator(spec)
        class FakeFitted:
            classes_ = np.array([0, 1])
            def predict_proba(self, X):
                return np.tile([.25, .75], (len(X), 1))
        return FakeFitted(), {}
    monkeypatch.setattr(models, "fit_calibrated_estimator", fake_fit)
    cutoff = "2026-10-01T00:00:00Z"
    kwargs = dict(config=[models.ModelSpec("optional", family="catboost"), "logistic"],
                  training_cutoff=cutoff, label_available_at=[cutoff] * 4)
    monkeypatch.setattr(models.importlib.util, "find_spec",
        lambda name: None if name == "catboost" else original_find(name))
    result = models.fit_ensemble([[-2.], [-1.], [1.], [2.]], [0, 0, 1, 1], [[.5]], **kwargs)
    assert result["outputs"][0]["error"]["type"] == "UnsupportedModelError"
    assert result["outputs"][1]["status"] == "success"
    monkeypatch.setattr(models.importlib.util, "find_spec", original_find)
    def failed_adapter(name, *args, **kwargs):
        if name.endswith("catboost_adapter"):
            raise ImportError("explicit optional adapter import failure")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", failed_adapter)
    result = models.fit_ensemble([[-2.], [-1.], [1.], [2.]], [0, 0, 1, 1], [[.5]], **kwargs)
    assert result["outputs"][0]["error"]["message"] == "explicit optional adapter import failure"
    assert result["outputs"][1]["status"] == "success"


def test_adapter_hash_failure_is_local_to_affected_model(monkeypatch):
    original_read = Path.read_bytes
    def unavailable_source(path):
        if path.name == "catboost_adapter.py":
            raise OSError("explicit adapter disk hash unavailable")
        return original_read(path)
    def fake_fit(spec, X, y, **kwargs):
        class FakeFitted:
            classes_ = np.array([0, 1])
            def predict_proba(self, X):
                return np.tile([.25, .75], (len(X), 1))
        return FakeFitted(), {}
    monkeypatch.setattr(Path, "read_bytes", unavailable_source)
    monkeypatch.setattr(models, "fit_calibrated_estimator", fake_fit)
    cutoff = "2026-10-01T00:00:00Z"
    result = models.fit_ensemble([[-2.], [-1.], [1.], [2.]], [0, 0, 1, 1], [[.5]],
        config=[models.ModelSpec("optional", family="catboost"), "logistic"],
        training_cutoff=cutoff, label_available_at=[cutoff] * 4)
    assert result["outputs"][0]["error"]["message"] == "explicit adapter disk hash unavailable"
    assert result["outputs"][1]["status"] == "success"
