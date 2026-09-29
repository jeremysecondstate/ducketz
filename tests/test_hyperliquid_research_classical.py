from types import SimpleNamespace

import numpy as np
import pytest
from sklearn.base import clone
from sklearn.datasets import make_classification
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from ml import hyperliquid_research_classical as research
from ml.hyperliquid_models import ModelSettings, _make_estimators


OPTIONAL = {"lightgbm", "xgboost", "catboost"}
REQUIRED = {
    "logistic", "extra_trees", "hist_gradient_boosting", "mlp",
    "random_forest", "adaboost", "gradient_boosting",
}


def no_optional(monkeypatch):
    def absent(name):
        raise ModuleNotFoundError(f"No module named '{name}'", name=name)
    monkeypatch.setattr(research.importlib, "import_module", absent)


def test_existing_four_match_production_raw_parameters_and_scaling(monkeypatch):
    no_optional(monkeypatch)
    estimators, skipped = research.make_classical_estimators(seed=17, threads=2)
    baseline = _make_estimators(ModelSettings(random_state=17))
    for name, pipeline in baseline.items():
        assert estimators[name]["estimator"].get_params() == pipeline["model"].get_params()
        assert estimators[name]["scaled"] is ("scaler" in pipeline.named_steps)
    assert set(estimators) == REQUIRED
    assert set(skipped) == OPTIONAL
    assert all("not installed" in reason for reason in skipped.values())


def test_each_call_returns_fresh_cloneable_unfitted_estimators(monkeypatch):
    no_optional(monkeypatch)
    first, _ = research.make_classical_estimators()
    second, _ = research.make_classical_estimators()
    for name, row in first.items():
        assert set(row) == {"estimator", "scaled"}
        assert row["estimator"] is not second[name]["estimator"]
        assert clone(row["estimator"]).get_params(deep=False).keys() == row["estimator"].get_params(deep=False).keys()
        assert not hasattr(row["estimator"], "classes_")
    assert first["adaboost"]["estimator"].estimator is not second["adaboost"]["estimator"].estimator


@pytest.mark.parametrize("arguments", [
    {"seed": True}, {"seed": -1}, {"seed": 2**32}, {"seed": 1.5},
    {"threads": True}, {"threads": 0}, {"threads": -1}, {"threads": 2.5},
])
def test_invalid_budget_or_seed_fails_before_imports(monkeypatch, arguments):
    def unexpected(name):
        pytest.fail(f"optional import attempted for invalid settings: {name}")
    monkeypatch.setattr(research.importlib, "import_module", unexpected)
    with pytest.raises(ValueError):
        research.make_classical_estimators(**arguments)


def test_installed_optional_with_missing_transitive_dependency_is_not_silently_skipped(monkeypatch):
    def broken(name):
        raise ModuleNotFoundError("Missing package used by installed optional model", name="nested_dependency")
    monkeypatch.setattr(research.importlib, "import_module", broken)
    with pytest.raises(ModuleNotFoundError, match="installed optional model"):
        research.make_classical_estimators()


def test_optional_constructors_receive_bounded_workers_and_no_catboost_writes(monkeypatch):
    class Capture:
        def __init__(self, **params):
            self.params = params
    module = SimpleNamespace(LGBMClassifier=Capture, XGBClassifier=Capture, CatBoostClassifier=Capture)
    monkeypatch.setattr(research.importlib, "import_module", lambda name: module)
    estimators, skipped = research.make_classical_estimators(seed=73, threads=8)
    assert not skipped
    assert set(estimators) == REQUIRED | OPTIONAL
    for name in ("lightgbm", "xgboost"):
        params = estimators[name]["estimator"].params
        assert params["n_jobs"] == 1
        assert params["random_state"] == 73
        assert params["n_estimators"] == 100
    cat = estimators["catboost"]["estimator"].params
    assert cat["thread_count"] == 1
    assert cat["random_seed"] == 73
    assert cat["allow_writing_files"] is False


@pytest.mark.parametrize("name", sorted(REQUIRED | OPTIONAL))
def test_available_families_fit_and_produce_binary_probabilities(name):
    estimators, skipped = research.make_classical_estimators()
    if name in skipped:
        pytest.skip(skipped[name])
    row = estimators[name]
    estimator = clone(row["estimator"])
    params = estimator.get_params()
    if "n_estimators" in params:
        estimator.set_params(n_estimators=3)
    if name == "hist_gradient_boosting":
        estimator.set_params(max_iter=3)
    if name == "mlp":
        estimator.set_params(max_iter=5, batch_size=32)
    if name == "catboost":
        estimator.set_params(iterations=3)
    model = make_pipeline(StandardScaler(), estimator) if row["scaled"] else estimator
    x, y = make_classification(n_samples=128, n_features=8, n_informative=4, random_state=42)
    with threadpool_limits(limits=1):
        model.fit(x[:96], y[:96])
        p = model.predict_proba(x[96:])
    assert set(model.classes_) == {0, 1}
    assert p.shape == (32, 2)
    assert np.isfinite(p).all()
    assert ((p >= 0) & (p <= 1)).all()
    np.testing.assert_allclose(p.sum(axis=1), 1)
