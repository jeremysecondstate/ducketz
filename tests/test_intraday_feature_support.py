from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from ml.intraday.feature_support import MarketVariationSupport, attach_market_support, VERSION
from ml.intraday.models import ModelSpec, build_estimator, fit_calibrated_estimator


NAMES = ('price', 'market_value', 'market_value__available', 'market_varying')


def test_training_constants_stay_inert_without_modifying_raw_values():
    X = np.array([[1, 0, 0, 3], [1, 0, 0, 4]], dtype=float)
    transform = MarketVariationSupport(NAMES).fit(X)
    raw = np.array([[7, 1e15, 1, 9]], dtype=float)
    got = transform.transform(raw)
    assert got.tolist() == [[7, 0, 0, 9]]
    assert raw.tolist() == [[7, 1e15, 1, 9]]
    assert transform.evidence()['constant_columns'] == list(NAMES[1:3])
    assert transform.evidence()['version'] == VERSION


def test_observed_constant_market_values_are_model_constants_not_market_fills():
    transform = MarketVariationSupport(('price','market_value','market_value__available')).fit(
        [[1, 55, 1], [2, 55, 1]])
    assert transform.transform([[3, 0, 0]]).tolist() == [[3, 55, 1]]
    assert transform.evidence()['raw_snapshot_preserved']


def test_price_only_recipes_keep_original_pipeline():
    base = build_estimator(ModelSpec('test',family='logistic'))
    assert attach_market_support(base, ('price','volume')) is base
    assert 'market_support' not in base.named_steps


def test_mlp_cannot_react_to_an_auxiliary_field_never_varied_in_training():
    X = np.column_stack([np.arange(80) % 7, np.zeros(80), np.zeros(80)])
    y = (np.arange(80) % 2).astype(int)
    spec = ModelSpec('test',family='mlp',parameters={'hidden_layer_sizes':(4,), 'max_iter':60})
    estimator, evidence = fit_calibrated_estimator(spec,X,y,feature_names=NAMES[:3])
    missing = estimator.predict_proba([[2,0,0]])
    fresh = estimator.predict_proba([[2,1e15,1]])
    assert np.array_equal(missing,fresh)
    assert evidence['market_feature_support']['base_training_rows'] == 80


def test_calibration_cannot_supply_base_feature_variation():
    rows=100
    references=[datetime(2026,1,1,tzinfo=timezone.utc)+timedelta(hours=i) for i in range(rows)]
    targets=[value+timedelta(minutes=45) for value in references]
    X=np.column_stack([np.arange(rows)%7,np.zeros(rows),np.zeros(rows)])
    X[80:,1]=np.arange(20)+1
    X[80:,2]=1
    y=(np.arange(rows)%2).astype(int)
    estimator,evidence=fit_calibrated_estimator(ModelSpec('test',family='logistic',probability_calibration='sigmoid'),
        X,y,reference_at=references,target_close_at=targets,label_available_at=targets,
        training_cutoff=targets[-1],feature_names=NAMES[:3])
    guard=estimator.base.named_steps['market_support']
    assert guard.training_rows_==80
    assert guard.evidence()['constant_columns']==list(NAMES[1:3])
    assert np.array_equal(estimator.predict_proba([[2,0,0]]),estimator.predict_proba([[2,999999,1]]))


def test_new_base_training_variation_is_admitted_on_the_next_fresh_fit():
    transform=MarketVariationSupport(NAMES[:3]).fit([[1,0,0],[2,5,1],[3,8,1]])
    assert transform.indices_.size==0
    assert transform.transform([[4,13,1]]).tolist()==[[4,13,1]]


def test_frozen_recipe_core_preprocessing_is_preserved_when_available():
    from ml.intraday import models
    if not hasattr(models,'freeze_model_spec'):
        pytest.skip('Atlas adapter predates frozen core recipe API; candidate exercises it')
    spec=models.freeze_model_spec(ModelSpec('test',family='logistic'))
    original=models.build_estimator(spec)
    guarded=attach_market_support(models.build_estimator(spec),NAMES[:3])
    assert guarded.named_steps['impute'].get_params()==original.named_steps['impute'].get_params()
    assert guarded.named_steps['scale'].get_params()==original.named_steps['scale'].get_params()
    assert guarded.named_steps['model'].get_params()==original.named_steps['model'].get_params()
