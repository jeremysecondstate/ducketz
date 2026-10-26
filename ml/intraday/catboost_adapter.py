"""One-worker CatBoost stages for the numeric sklearn intraday pipeline.

Imported only when CatBoost is selected and available. CatBoost's constructor
thread_count controls training, but its Pool preprocessing, automatic feature
importance and prediction methods have independent all-core defaults.
"""
from catboost import CatBoostClassifier, EFstrType, Pool


class SingleWorkerCatBoostClassifier(CatBoostClassifier):
    """Retain CatBoost parameters and sklearn identity while bounding each stage.

    The active pipeline fits an imputed numeric matrix and binary labels, with no
    CatBoost evaluation sets or categorical metadata. Keep that fit contract small;
    the inherited constructor, get_params and set_params preserve cloning/persistence.
    """

    def fit(self, X, y=None, sample_weight=None):
        pool = Pool(X, label=y, weight=sample_weight, thread_count=1)
        return super().fit(pool)

    def get_feature_importance(self, data=None, type=EFstrType.FeatureImportance,
                               prettified=False, thread_count=1, *args, **kwargs):
        # CatBoost also calls this method itself after native training.
        return super().get_feature_importance(data, type, prettified, 1, *args, **kwargs)

    def predict_proba(self, X, ntree_start=0, ntree_end=0, thread_count=1,
                      verbose=None, task_type="CPU"):
        return super().predict_proba(X, ntree_start=ntree_start, ntree_end=ntree_end,
                                    thread_count=1, verbose=verbose, task_type=task_type)

    def predict(self, data, prediction_type="Class", ntree_start=0, ntree_end=0,
                thread_count=1, verbose=None, task_type="CPU"):
        return super().predict(data, prediction_type=prediction_type,
                               ntree_start=ntree_start, ntree_end=ntree_end,
                               thread_count=1, verbose=verbose, task_type=task_type)
