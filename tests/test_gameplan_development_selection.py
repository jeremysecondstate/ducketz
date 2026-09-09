import json

import numpy as np
import pandas as pd
import pytest

from ml.gameplan_development_selection import DEVELOPMENT_SELECTION_POLICY, select_development_calibrator
from ml.nightly_gameplan import _estimator, _fit_group_model
from ml.stock_trader.market_features import INDEPENDENT_MARKET_FEATURE_NAMES, INDEPENDENT_MARKET_FEATURE_CONTRACT


def development_rows(count=20):
    start = pd.date_range("2026-01-01", periods=count, freq="D", tz="UTC")
    return pd.DataFrame({"decision_timestamp": start, "target_window_start": start + pd.Timedelta(hours=1),
                         "target_window_end": start + pd.Timedelta(hours=2), "target": np.arange(count) % 2})


def test_identity_wins_when_platt_degrades_later_development_probabilities():
    rows = development_rows()
    raw = np.where(rows.target.eq(1), 0.8, 0.2)
    calibrator, report = select_development_calibrator(rows, raw)
    assert calibrator.method == "none"
    assert report["selected_family"] == "identity"
    assert report["candidate_metrics"]["identity"]["log_loss"] < report["candidate_metrics"]["platt"]["log_loss"]
    assert report["assessment_used_for_selection"] is False
    assert pd.Timestamp(report["fit_last_target_end"]) < pd.Timestamp(report["validation_first_decision"])
    np.testing.assert_array_equal(calibrator.predict(raw), raw)


def test_platt_may_win_on_development_and_is_refit_on_full_calibration():
    rows = development_rows()
    calibrator, report = select_development_calibrator(rows, np.full(len(rows), 0.9))
    assert calibrator.method == "platt"
    assert report["selected_family"] == "platt"
    assert report["full_calibration_refit_rows"] == len(rows)
    assert report["candidate_metrics"]["platt"]["log_loss"] < report["candidate_metrics"]["identity"]["log_loss"]


def test_mature_label_purging_can_leave_identity_only_without_searching_another_split(monkeypatch):
    rows = development_rows(10)
    rows["target_window_end"] += pd.Timedelta(days=10)
    def forbidden(*args):
        raise AssertionError("Unsupported development calibration must not fit")
    monkeypatch.setattr("ml.gameplan_development_selection._fit_platt", forbidden)
    calibrator, report = select_development_calibrator(rows, np.linspace(0.2, 0.8, len(rows)))
    assert calibrator.method == "none"
    assert report["fit_rows"] == 0 and report["purged_rows"] == 5
    assert report["selection_status"] == "IDENTITY_INSUFFICIENT_PURGED_DEVELOPMENT_SUPPORT"
    assert report["candidate_metrics"] == {}


def test_logistic_baseline_is_fixed_regularized_and_scaled():
    pipeline = _estimator("logistic", ("mr__x",), ("symbol", "route"))
    classifier = pipeline.named_steps["classifier"]
    assert classifier.C == 1.0 and classifier.solver == "lbfgs" and classifier.class_weight is None
    numeric = pipeline.named_steps["preprocess"].transformers[0][1]
    assert "scale" in numeric.named_steps


def test_estimator_and_calibration_choices_do_not_depend_on_final_assessment(tmp_path, monkeypatch):
    from sklearn.compose import ColumnTransformer
    from sklearn.dummy import DummyClassifier
    from sklearn.pipeline import Pipeline
    from sklearn.tree import DecisionTreeClassifier

    def estimator(family, *args):
        if family != "logistic":
            return DummyClassifier(strategy="prior")
        return Pipeline([("features", ColumnTransformer([("x", "passthrough", ["mr__x"])])),
                         ("classifier", DecisionTreeClassifier(max_depth=1, random_state=0))])
    monkeypatch.setattr("ml.nightly_gameplan._estimator", estimator)
    samples = development_rows(120)
    samples = samples.assign(symbol="AAPL", model_group="1h", route="1h@04:00", forecast_anchor_local="04:00",
        information_available_at=samples.decision_timestamp, target_semantics="independent_test",
        target_contract_version="independent-stock-targets-v1", trading_hours=1.0, mr__x=samples.target.astype(float))
    current = samples.tail(2).copy()
    for index, name in enumerate(INDEPENDENT_MARKET_FEATURE_NAMES):
        current[name] = [index + 0.25, index + 0.5]
    results = []
    for reverse in [False, True]:
        altered = samples.copy()
        if reverse:
            altered.loc[altered.index[-15:], "target"] = 1 - altered.loc[altered.index[-15:], "target"]
        results.append(_fit_group_model(altered, current=current, feature_columns=("mr__x",), group="1h",
            model_directory=tmp_path / str(reverse) / "models/1h", trained_at=pd.Timestamp("2026-06-01T00:00Z")))
    first, changed = [r["report"] for r in results]
    assert first["selected_family"] == changed["selected_family"] == "regularized-logistic-c1"
    assert first["selection_metrics"] == changed["selection_metrics"]
    assert first["calibration_selection"] == changed["calibration_selection"]
    assert first["calibration_selection"]["selected_family"] == "identity"
    assert first["development_selection_policy"] == DEVELOPMENT_SELECTION_POLICY
    assert first["target_calendar_feature_contract"] == "independent-stock-known-calendar-inputs-v1"
    assert "target__weekday_sin" in first["features"]["admitted"]
    assert first["promotion_gate"]["status"] == "PROMOTED"
    assert changed["promotion_gate"]["status"] == "RESEARCH_NOT_PROMOTED"
    assert changed["promotion_gate"]["checks"]["brier_within_baseline_tolerance"] is False
    assert changed["promotion_gate"]["checks"]["log_loss_within_baseline_tolerance"] is False
    for result in results:
        forecasts = result["forecasts"]
        assert forecasts.enrichment_feature_contract.eq(INDEPENDENT_MARKET_FEATURE_CONTRACT).all()
        for position, payload in enumerate(forecasts.enrichment_feature_values_json):
            assert json.loads(payload) == {name: float(current.iloc[position][name]) for name in INDEPENDENT_MARKET_FEATURE_NAMES}
            assert "target" not in json.loads(payload)


@pytest.mark.parametrize("bad", [np.nan, -0.1, 1.1])
def test_invalid_development_probabilities_fail_instead_of_falling_back(bad):
    rows = development_rows()
    raw = np.full(len(rows), 0.5)
    raw[0] = bad
    with pytest.raises(ValueError, match="probabilities are invalid"):
        select_development_calibrator(rows, raw)


def test_daily_regularization_grid_is_selected_without_assessment_labels(tmp_path, monkeypatch):
    from sklearn.dummy import DummyClassifier
    from ml.gameplan_development_selection import DAILY_LOGISTIC_REGULARIZATION_POLICY
    original = _estimator
    def estimator(family, numeric, categorical):
        return original(family, numeric, categorical) if family == "logistic" else DummyClassifier(strategy="prior")
    monkeypatch.setattr("ml.nightly_gameplan._estimator", estimator)
    samples = development_rows(120)
    samples = samples.assign(symbol="AAPL", model_group="1d", route="1d@D+1", forecast_anchor_local="D+1",
        information_available_at=samples.decision_timestamp, target_semantics="independent_test",
        target_contract_version="independent-stock-targets-v1", trading_hours=13., mr__x=samples.target.astype(float))
    samples["target_window_end"] = samples.target_window_start + pd.Timedelta(hours=13)
    current = samples.tail(2).copy()
    for index, name in enumerate(INDEPENDENT_MARKET_FEATURE_NAMES):
        current[name] = [index + .25, index + .5]
    reports = []
    for flip_assessment in (False, True):
        altered = samples.copy()
        if flip_assessment:
            altered.loc[altered.index[-15:], "target"] = 1 - altered.loc[altered.index[-15:], "target"]
        value = _fit_group_model(altered, current=current, feature_columns=("mr__x",), group="1d",
            model_directory=tmp_path / str(flip_assessment) / "models/1d", trained_at=pd.Timestamp("2026-06-01T00:00Z"))
        reports.append(value["report"])
    first, second = reports
    assert first["logistic_regularization_policy"] == DAILY_LOGISTIC_REGULARIZATION_POLICY
    assert first["logistic_regularization_candidates"] == [.001, .01, .1, 1.]
    assert first["selection_metrics"] == second["selection_metrics"]
    assert first["selected_logistic_regularization_c"] == second["selected_logistic_regularization_c"]
    assert first["calibration_selection"] == second["calibration_selection"]
    assert first["promotion_gate"]["status"] == "PROMOTED"
    assert second["promotion_gate"]["status"] == "RESEARCH_NOT_PROMOTED"
