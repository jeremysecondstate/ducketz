import json
import subprocess
import sys

import joblib
import numpy as np
import pandas as pd
import pytest
from sklearn.linear_model import LogisticRegression

from ml.gameplan_estimators import PriorProbabilityShrinkage
from ml.gameplan_probability_target import RAW_DIRECTION_TARGET, probability_target_metadata
from ml.gameplan_development_selection import WEEKLY_PROBABILITY_SHRINKAGE_POLICY
from ml.nightly_gameplan import _chronological_partitions, _fit_group_model, _model_frame
from ml.independent_stock_targets import with_stock_calendar_features


def fitted_model():
    x = np.asarray([[-2.], [-1.], [1.], [2.]])
    return x, LogisticRegression().fit(x, [0, 0, 1, 1])


@pytest.mark.parametrize("weight", [.25, .5, .75, 1.])
def test_probability_shrinkage_preserves_exact_nonzero_model_mix(weight):
    x, model = fitted_model()
    base = model.predict_proba(x)[:, 1]
    fitted = PriorProbabilityShrinkage(model, prior_probability=.6, weight=weight)
    result = fitted.predict_proba(x)
    np.testing.assert_allclose(result[:, 1], weight*base+(1-weight)*.6)
    np.testing.assert_allclose(result.sum(axis=1), 1.)
    assert np.ptp(result[:, 1]) > 0
    if weight == 1:
        np.testing.assert_allclose(result, model.predict_proba(x))


@pytest.mark.parametrize("prior,weight", [(0,.5), (1,.5), (np.nan,.5), (.5,0), (.5,.00001), (.5,.2), (.5,np.nan)])
def test_probability_shrinkage_rejects_invalid_or_unregistered_values(prior, weight):
    _, model = fitted_model()
    with pytest.raises(ValueError):
        PriorProbabilityShrinkage(model, prior_probability=prior, weight=weight)


def test_shrinkage_joblib_reloads_in_fresh_python_process(tmp_path):
    x, model = fitted_model()
    fitted = PriorProbabilityShrinkage(model, prior_probability=.6, weight=.5)
    path = tmp_path / "model.joblib"
    joblib.dump(fitted, path)
    script = "import joblib,json,numpy as np,sys; print(json.dumps(joblib.load(sys.argv[1]).predict_proba(np.array([[-2.],[-1.],[1.],[2.]])).tolist()))"
    result = subprocess.run([sys.executable, "-c", script, str(path)], check=True, capture_output=True, text=True)
    np.testing.assert_allclose(np.asarray(json.loads(result.stdout)), fitted.predict_proba(x))


def test_weekly_fit_records_all_fixed_candidates_and_priors_without_assessment_selection(tmp_path):
    clocks = pd.date_range("2025-01-01", periods=240, freq="D", tz="UTC")
    x = np.arange(240) % 2
    target = x.copy()
    # Development selection intentionally weakens the strong training relation.
    target[150:180] = np.arange(30) % 4 // 2
    samples = pd.DataFrame({"decision_timestamp": clocks, "information_available_at": clocks,
        "target_window_start": clocks + pd.Timedelta(hours=1),
        "target_window_end": clocks + pd.Timedelta(days=5),
        "symbol": "AAPL", "model_group": "1w", "route": "1w@D+5", "forecast_anchor_local": "D+5",
        "target_semantics": "independent_test", "target_contract_version": "independent-stock-targets-v1",
        "trading_hours": 65., "mr__x": x, "observed_return": np.where(target, .0005, -.0005),
        "assumed_round_trip_cost": .001, "target": target, "target_raw_price_direction": target,
        "target_cost_adjusted_positive": 0, **probability_target_metadata(RAW_DIRECTION_TARGET)})
    parts = _chronological_partitions(samples, group="1w")
    results = []
    for reverse in (False, True):
        changed = samples.copy()
        if reverse:
            mask = changed.decision_timestamp.isin(parts['assessment'].decision_timestamp)
            changed.loc[mask, 'target'] = 1-changed.loc[mask, 'target']
            changed.loc[mask, 'target_raw_price_direction'] = changed.loc[mask, 'target']
            changed.loc[mask, 'observed_return'] *= -1
        results.append(_fit_group_model(changed, current=samples.tail(2), feature_columns=("mr__x",),
            group="1w", model_directory=tmp_path / str(reverse) / "models/1w",
            trained_at=pd.Timestamp("2026-01-01T00:00Z")))
    first, second = [item['report'] for item in results]
    assert first['probability_shrinkage_policy'] == WEEKLY_PROBABILITY_SHRINKAGE_POLICY
    assert len(first['selection_metrics']) == 36
    assert first['selection_metrics'] == second['selection_metrics']
    assert first['selected_family'] == second['selected_family']
    assert first['calibration_selection'] == second['calibration_selection']
    assert first['selected_probability_shrinkage_weight'] in (.25,.5,.75)
    assert first['selection_shrinkage_prior'] == parts['train'].target.mean()
    assert first['fitted_shrinkage_prior'] == pd.concat([parts['train'],parts['selection']]).target.mean()
    payload = joblib.load(tmp_path / "False/models/1w/model.joblib")
    for key in ('selected_family', 'selected_base_family', 'selected_neural_weight',
                'selected_logistic_regularization_c', 'probability_shrinkage_policy',
                'selected_probability_shrinkage_weight', 'selection_shrinkage_prior',
                'fitted_shrinkage_prior', 'probability_shrinkage_weights', 'selection_metrics'):
        assert payload[key] == first[key]
    if first['selected_probability_shrinkage_weight'] != 1:
        assert isinstance(payload['estimator'], PriorProbabilityShrinkage)
        assert payload['estimator'].prior_probability == first['fitted_shrinkage_prior']
    matrix = _model_frame(with_stock_calendar_features(samples.tail(2)),
                          payload['feature_columns'], payload['categorical_columns'])
    reproduced_raw = payload['estimator'].predict_proba(matrix)[:, 1]
    np.testing.assert_array_equal(reproduced_raw, results[0]['forecasts'].raw_probability.to_numpy())
    np.testing.assert_array_equal(payload['calibrator'].predict(reproduced_raw),
                                  results[0]['forecasts'].calibrated_probability.to_numpy())
