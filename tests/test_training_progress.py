import warnings

import numpy as np
import pandas as pd
import pytest

from ml.training_progress import fit_with_progress


class Estimator:
    def get_params(self, deep=True):
        return {}

    def fit(self, features, target):
        warnings.warn("test convergence warning", UserWarning)
        self.loss_curve_ = [1.0, 0.5]
        return self


def test_warnings_and_fit_progress_are_visible(monkeypatch, capsys):
    monkeypatch.setenv("DUCKETZ_TRAINING_PROGRESS", "1")
    fit_with_progress(Estimator(), np.zeros((2, 2)), [0, 1], label="test")
    output = capsys.readouterr().out
    assert "FIT_START" in output and "FIT_WARNING" in output and "FIT_COMPLETE" in output
    assert "test convergence warning" in output


def test_nonfinite_loss_fails_and_is_reported(monkeypatch, capsys):
    monkeypatch.setenv("DUCKETZ_TRAINING_PROGRESS", "1")
    class Broken(Estimator):
        def fit(self, features, target):
            self.loss_curve_ = [1.0, float("nan")]
            return self
    with pytest.raises(RuntimeError, match="non-finite"):
        fit_with_progress(Broken(), np.zeros((2, 2)), [0, 1], label="test")
    assert "FIT_FAILED" in capsys.readouterr().out


@pytest.mark.parametrize("family", ["tree", "neural"])
def test_real_gameplan_estimators_emit_progress_and_restore_parameters(family, monkeypatch, capsys):
    from ml.nightly_gameplan import _estimator
    monkeypatch.setenv("DUCKETZ_TRAINING_PROGRESS", "1")
    estimator = _estimator(family, ("x",), ("symbol",))
    model_key = estimator.steps[-1][0]
    estimator.set_params(**{f"{model_key}__max_iter": 4})
    originals = {k: v for k, v in estimator.get_params().items() if k.endswith("verbose")}
    features = pd.DataFrame({"x": np.linspace(-1, 1, 80), "symbol": ["AAPL"] * 80})
    fit_with_progress(estimator, features, np.arange(80) % 2, label=f"test/{family}")
    assert np.isfinite(estimator.predict_proba(features)).all()
    assert {k: v for k, v in estimator.get_params().items() if k.endswith("verbose")} == originals
    assert "FIT_COMPLETE" in capsys.readouterr().out
