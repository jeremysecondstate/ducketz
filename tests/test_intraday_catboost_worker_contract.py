"""Synthetic CatBoost native-stage checks, without production data or orders."""
from datetime import datetime, timedelta, timezone
import functools
import hashlib
import inspect
import os
from pathlib import Path
import subprocess
import sys

import joblib
import numpy as np
import pytest
from sklearn.base import clone

from ml.intraday.models import ModelSpec, build_estimator, fit_calibrated_estimator, fit_ensemble


def _catboost():
    return pytest.importorskip("catboost")


def _dataset():
    rng = np.random.default_rng(20261021)
    X = rng.normal(size=(160, 4))
    y = (X[:, 0] + .2 * X[:, 1] > 0).astype(np.uint8)
    start = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    references = [start + i * timedelta(minutes=15) for i in range(len(y))]
    targets = [t + timedelta(minutes=45) for t in references]
    available = [t + timedelta(seconds=5) for t in targets]
    return X, y, {"reference_at": references, "target_close_at": targets,
                  "label_available_at": available, "training_cutoff": available[-1]}


def _observe_native_stages(monkeypatch):
    catboost = _catboost()
    events = []
    for cls, name in ((catboost.Pool, "__init__"), (catboost.CatBoost, "_train"),
                      (catboost.CatBoost, "_calc_fstr"), (catboost.CatBoost, "_base_predict")):
        original = getattr(cls, name)
        signature = inspect.signature(original)

        def observe(original, signature, name):
            @functools.wraps(original)
            def wrapped(*args, **kwargs):
                bound = signature.bind(*args, **kwargs)
                bound.apply_defaults()
                count = (bound.arguments["params"]["thread_count"] if name == "_train"
                         else bound.arguments["thread_count"])
                events.append((name, count))
                return original(*args, **kwargs)
            return wrapped

        monkeypatch.setattr(cls, name, observe(original, signature, name))
    return events


def _assert_bounded(events):
    assert {name for name, _ in events} == {"__init__", "_train", "_calc_fstr", "_base_predict"}
    assert all(count == 1 for _, count in events), events


@pytest.mark.parametrize("calibration", ["none", "sigmoid"])
def test_native_pool_fit_feature_importance_and_inference_use_one_worker(monkeypatch, calibration):
    events = _observe_native_stages(monkeypatch)
    X, y, timestamps = _dataset()
    spec = ModelSpec("bounded", family="catboost", parameters={"iterations": 5},
                     probability_calibration=calibration)
    fitted, evidence = fit_calibrated_estimator(spec, X, y, **timestamps)
    probabilities = fitted.predict_proba(X[-3:])
    assert probabilities.shape == (3, 2)
    assert evidence["effective_parameters"]["thread_count"] == 1
    np.testing.assert_allclose(probabilities.sum(axis=1), 1)
    _assert_bounded(events)


def test_parameters_probabilities_classes_and_joblib_identity_are_preserved(tmp_path):
    catboost = _catboost()
    X, y, _ = _dataset()
    spec = ModelSpec("retained", family="catboost", parameters={"iterations": 7})
    candidate = build_estimator(spec)
    copied = clone(candidate)
    assert copied.named_steps["model"].get_params() == candidate.named_steps["model"].get_params()
    assert type(copied.named_steps["model"]) is type(candidate.named_steps["model"])
    assert not copied.named_steps["model"].is_fitted()
    original = catboost.CatBoostClassifier(**candidate.named_steps["model"].get_params())
    expected = original.fit(X, y).predict_proba(X[-8:], thread_count=1)
    candidate.fit(X, y)
    np.testing.assert_array_equal(candidate.predict_proba(X[-8:]), expected)
    np.testing.assert_array_equal(candidate.classes_, original.classes_)
    np.testing.assert_array_equal(candidate.named_steps["model"].feature_importances_, original.feature_importances_)
    artifact = tmp_path / "synthetic.joblib"
    joblib.dump(candidate, artifact)
    restored = joblib.load(artifact)
    assert type(restored.named_steps["model"]) is type(candidate.named_steps["model"])
    np.testing.assert_array_equal(restored.predict_proba(X[-8:]), expected)


def test_explicit_all_core_arguments_do_not_escape_existing_worker_contract(monkeypatch):
    catboost = _catboost()
    events = _observe_native_stages(monkeypatch)
    X, y, _ = _dataset()
    estimator = build_estimator(ModelSpec("explicit", family="catboost", parameters={"iterations": 5}))
    model = estimator.named_steps["model"].fit(X, y, sample_weight=np.ones(len(y)))
    probabilities = model.predict_proba(X[-3:], 0, 0, -1)
    classes = model.predict(X[-3:], thread_count=-1)
    np.testing.assert_array_equal(classes, model.classes_[probabilities.argmax(axis=1)])
    model.get_feature_importance(None, catboost.EFstrType.FeatureImportance, False, -1)
    _assert_bounded(events)


def test_new_output_and_saved_artifact_bind_loaded_adapter_source(tmp_path):
    _catboost()
    from ml.intraday import catboost_adapter
    X, y, timestamps = _dataset()
    spec = ModelSpec("provenance", family="catboost", parameters={"iterations": 5})
    result = fit_ensemble(X, y, X[-1:], config=[spec], artifact_dir=tmp_path,
                          run_id="synthetic", **timestamps)
    assert result["complete"], result["outputs"]
    output = result["outputs"][0]
    runtime = output["metadata"]["model_adapter_runtime"]
    source = runtime["modules"][catboost_adapter.__name__]
    assert source["status"] == "captured"
    assert source["loaded_functions_match_disk"] is True
    assert source["disk_sha256_at_capture"] == hashlib.sha256(Path(catboost_adapter.__file__).read_bytes()).hexdigest()
    assert any("SingleWorkerCatBoostClassifier.fit" in name for name in source["loaded_functions"])
    saved = joblib.load(output["artifact_path"])
    assert saved["metadata"]["model_adapter_runtime"] == runtime


def test_failed_fit_retains_loaded_adapter_source_evidence(monkeypatch):
    _catboost()
    from ml.intraday import models
    X, y, timestamps = _dataset()

    def fail_fit(*args, **kwargs):
        raise RuntimeError("synthetic native fit failure")

    monkeypatch.setattr(models, "fit_calibrated_estimator", fail_fit)
    result = models.fit_ensemble(X, y, X[-1:], config=["catboost"], **timestamps)
    output = result["outputs"][0]
    assert output["status"] == "failed"
    assert output["error"]["message"] == "synthetic native fit failure"
    runtime = output["metadata"]["model_adapter_runtime"]
    source = runtime["modules"]["ml.intraday.catboost_adapter"]
    assert source["status"] == "captured"
    assert source["loaded_functions_match_disk"] is True


def test_registry_import_stays_lazy_and_missing_optional_dependency_has_no_substitute():
    # A fresh interpreter establishes that another earlier test did not preload
    # the optional adapter and mask an eager CatBoost import.
    candidate = Path(__file__).resolve().parents[1]
    code = """
import sys
import builtins
import socket
from pathlib import Path
def deny_network(*args, **kwargs):
    raise RuntimeError('optional adapter regression forbids network calls')
socket.create_connection = deny_network
socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network
sys.addaudithook(lambda event, values: deny_network() if event in {
    'socket.connect', 'socket.getaddrinfo', 'socket.bind'} else None)
from ml.intraday import models
assert Path(models.__file__).resolve().parents[2] == Path.cwd().resolve()
assert not any(name == 'catboost' or name.startswith('catboost.') for name in sys.modules)
original = models.importlib.util.find_spec
models.importlib.util.find_spec = lambda name: None if name == 'catboost' else original(name)
try:
    models.build_estimator(models.ModelSpec('missing', family='catboost'))
except models.UnsupportedModelError as error:
    assert 'not installed; no substitute was fitted' in str(error)
else:
    raise AssertionError('missing dependency must fail without substitution')
models.build_estimator(models.ModelSpec('available', family='logistic'))
assert 'ml.intraday.catboost_adapter' not in sys.modules
models.importlib.util.find_spec = lambda name: object() if name == 'catboost' else original(name)
original_import = builtins.__import__
def import_with_failed_optional_adapter(name, *args, **kwargs):
    if name.endswith('catboost_adapter'):
        raise ImportError('optional native adapter import fixture')
    return original_import(name, *args, **kwargs)
builtins.__import__ = import_with_failed_optional_adapter
cutoff = '2026-10-01T00:00:00+00:00'
result = models.fit_ensemble([[-2.], [-1.], [1.], [2.]], [0, 0, 1, 1], [[.5]],
    config=[models.ModelSpec('native_failed', family='catboost'), 'logistic'],
    training_cutoff=cutoff, label_available_at=[cutoff] * 4)
assert result['successful_count'] == 1
assert result['outputs'][0]['error']['type'] == 'ImportError'
assert result['outputs'][0]['error']['message'] == 'optional native adapter import fixture'
assert result['outputs'][1]['status'] == 'success'
"""
    environment = {**os.environ, "PYTHONPATH": str(candidate), "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=candidate,
                            env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
