"""Recipe persistence must keep assessment and deployed forecast arithmetic aligned."""
from dataclasses import asdict
import json

import joblib
import numpy as np
import pytest

from ml.hyperliquid_model_config import ModelConfig, load_config
from ml import hyperliquid_models as models
from tests.test_hyperliquid_models import snapshot, settings, small_models


@pytest.mark.parametrize('name', ['calibration_c', 'logistic_weight', 'extra_trees_weight',
                                 'hist_gradient_boosting_weight', 'mlp_weight'])
@pytest.mark.parametrize('value', [0, -1, True, None, '0.1', float('inf'), float('nan')])
def test_invalid_parameters_cannot_remove_a_family_or_enter_training(name, value):
    for constructor in (ModelConfig, models.ModelSettings):
        with pytest.raises(ValueError, match=name):
            constructor(**{name: value})


def test_json_parameters_reach_recipe_without_tuple_list_retraining_mismatch(tmp_path):
    values = dict(calibration_c=.1, logistic_weight=.4, extra_trees_weight=.2,
                  hist_gradient_boosting_weight=.2, mlp_weight=.2)
    path = tmp_path / 'models.json'
    path.write_text(json.dumps({'version': 1, **values}))
    config = load_config(path)
    recipe = config.model_settings(4)
    assert all(getattr(recipe, name) == value for name, value in values.items())
    assert json.loads(json.dumps(asdict(recipe))) == asdict(recipe)
    assert ModelConfig().calibration_c == 1.0
    assert models.ModelSettings().logistic_weight == 1.0


def test_deployed_bundle_preserves_selected_weights_calibration_and_legacy_fallback(tmp_path):
    from threadpoolctl import threadpool_limits
    source = snapshot()
    recipe = settings(calibration_c=.1, logistic_weight=.4, extra_trees_weight=.2,
                      hist_gradient_boosting_weight=.2, mlp_weight=.2)
    with threadpool_limits(limits=1):
        trained = models.train_candidate(source, recipe, model_factory=small_models)
        bundle = trained['bundle']
        assert set(bundle.estimators) == {'logistic', 'extra_trees', 'hist_gradient_boosting', 'mlp'}
        assert all(calibrator.C == .1 for calibrator in bundle.calibration.values())
        weights = trained['report']['ensemble_weights']
        expected = sum(trained['assessment'][name+'_p_not_down'] * weight for name, weight in weights.items())
        np.testing.assert_allclose(trained['assessment']['p_not_down'], expected, rtol=0, atol=1e-15)
        path = tmp_path / 'bundle.joblib'
        joblib.dump(bundle, path)
        loaded = joblib.load(path)
        forecast = models.predict_bundle(loaded, source)
        assert forecast['p_not_down'] == pytest.approx(sum(forecast['per_model'][name]['p_not_down'] * weight for name, weight in weights.items()))
        assert forecast['ensemble_weights'] == weights
        # A joblib created before the field existed must keep equal averaging.
        del loaded.ensemble_weights
        joblib.dump(loaded, path)
        legacy = models.predict_bundle(joblib.load(path), source)
        assert legacy['p_not_down'] == pytest.approx(np.mean([member['p_not_down'] for member in legacy['per_model'].values()]))
        assert legacy['ensemble_weights'] == {}


def test_corrupted_persisted_weights_fail_instead_of_silently_changing_membership():
    probabilities = {'logistic': np.array([.2]), 'mlp': np.array([.8])}
    with pytest.raises(ValueError, match='match'):
        models._ensemble_probability(probabilities, {'logistic': 1.0})
    with pytest.raises(ValueError, match='positive'):
        models._ensemble_probability(probabilities, {'logistic': 1.0, 'mlp': 0.0})
    np.testing.assert_allclose(models._ensemble_probability(probabilities, {'logistic': 1e308, 'mlp': 1e308}), [.5])
