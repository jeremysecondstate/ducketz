"""Additional family must survive calibration, serialization and live arithmetic."""
from dataclasses import replace
import joblib
import numpy as np
import pytest
from sklearn.ensemble import RandomForestClassifier
from threadpoolctl import threadpool_limits

from ml.hyperliquid_model_config import ModelConfig
from ml import hyperliquid_models as models
from tests.test_hyperliquid_models import snapshot, settings


@pytest.mark.parametrize('value', [-1, True, None, '0.2', float('nan'), float('inf')])
def test_optional_family_rejects_invalid_weight(value):
    for constructor in (ModelConfig, models.ModelSettings):
        with pytest.raises(ValueError, match='random_forest_weight'):
            constructor(random_forest_weight=value)


def test_default_retains_four_and_positive_weight_adds_distinct_fifth():
    core = models._make_estimators(models.ModelSettings())
    recipe = ModelConfig(random_forest_weight=.2).model_settings(4)
    expanded = models._make_estimators(recipe)
    assert set(core) == {'logistic', 'extra_trees', 'hist_gradient_boosting', 'mlp'}
    assert set(expanded) == set(core) | {'random_forest'}
    forest = expanded['random_forest'].named_steps['model']
    assert isinstance(forest, RandomForestClassifier)
    assert forest.bootstrap and forest.n_estimators == 128 and forest.n_jobs == 1
    assert forest.max_depth == 10 and forest.min_samples_leaf == 12 and forest.max_features == .7
    assert len({id(x) for x in expanded.values()}) == 5
    assert recipe.random_forest_weight == .2


def test_five_fitted_families_keep_positive_contribution_after_publication(tmp_path):
    source = snapshot()
    recipe = settings(logistic_weight=.32, extra_trees_weight=.16,
                      hist_gradient_boosting_weight=.16, mlp_weight=.16,
                      random_forest_weight=.2, calibration_c=.1)
    with threadpool_limits(limits=1):
        result = models.train_candidate(source, recipe)
        weights = result['report']['ensemble_weights']
        assert len(weights) == 5 and all(w > 0 for w in weights.values())
        assert weights['random_forest'] == pytest.approx(.2)
        assert set(result['bundle'].calibration) == set(weights)
        expected = sum(result['assessment'][name + '_p_not_down'] * weight
                       for name, weight in weights.items())
        np.testing.assert_allclose(result['assessment']['p_not_down'], expected, atol=1e-15)
        path = tmp_path / 'bundle.joblib'
        joblib.dump(result['bundle'], path)
        forecast = models.predict_bundle(joblib.load(path), source)
        assert forecast['ensemble_weights'] == weights
        assert forecast['p_not_down'] == pytest.approx(sum(
            forecast['per_model'][name]['p_not_down'] * weight for name, weight in weights.items()))
        assert result['report']['refit_after_assessment'] is False
        assert result['report']['splits']['fit']['last_label_end_utc'] < result['report']['splits']['calibration']['first_decision_close_utc']
        assert result['report']['splits']['calibration']['last_label_end_utc'] < result['report']['splits']['assessment']['first_decision_close_utc']
