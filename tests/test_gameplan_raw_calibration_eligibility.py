from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from ml.gameplan_development_selection import (
    RAW_DIRECTION_DEVELOPMENT_SELECTION_POLICY, select_development_calibrator,
)
from ml.gameplan_probability_target import RAW_DIRECTION_TARGET


def rows():
    clocks = pd.date_range("2026-01-01", periods=40, freq="D", tz="UTC")
    return pd.DataFrame({"decision_timestamp": clocks, "target_window_start": clocks + pd.Timedelta(hours=1),
                         "target_window_end": clocks + pd.Timedelta(hours=2), "target": np.arange(40) % 2})


def test_raw_selection_excludes_flat_platt_even_when_it_wins_development_log_loss():
    data = rows()
    raw = np.where(data.target, .2, .8)
    calibrator, report = select_development_calibrator(data, raw, probability_target=RAW_DIRECTION_TARGET)
    assert report["policy"] == RAW_DIRECTION_DEVELOPMENT_SELECTION_POLICY
    assert report["candidate_metrics"]["platt"]["log_loss"] < report["candidate_metrics"]["identity"]["log_loss"]
    assert report["candidate_eligibility"]["platt"]["eligible"] is False
    assert report["candidate_eligibility"]["platt"]["nondecreasing_constraint_active"] is True
    assert report["candidate_eligibility"]["identity"]["eligible"] is True
    assert report["selected_family"] == "identity"
    assert report["assessment_used_for_selection"] is False
    np.testing.assert_allclose(calibrator.predict(raw), raw)
    # Historical cost-target models retain the original selector and semantics.
    legacy, legacy_report = select_development_calibrator(data, raw)
    assert legacy_report["selected_family"] == "platt"
    assert np.ptp(legacy.predict(raw)) == 0


def test_eligible_platt_full_refit_cannot_replace_signal_with_constant(monkeypatch):
    data = rows()
    raw = np.where(data.target, .8, .2)

    class Candidate:
        method = "platt"

        def __init__(self, flat):
            self.nondecreasing_constraint_active = flat
            self.model = SimpleNamespace(coef_=np.asarray([[0. if flat else 2.]]))
            self.flat = flat

        def predict(self, probabilities):
            p = np.asarray(probabilities)
            return np.full(len(p), .5) if self.flat else np.where(p > .5, .9, .1)

    candidates = iter([Candidate(False), Candidate(True)])
    monkeypatch.setattr("ml.gameplan_development_selection._fit_platt", lambda *args: next(candidates))
    calibrator, report = select_development_calibrator(data, raw, probability_target=RAW_DIRECTION_TARGET)
    assert report["candidate_eligibility"]["platt"]["eligible"] is True
    assert report["full_refit_eligibility"]["eligible"] is False
    assert report["selected_family"] == "identity"
    assert report["selection_status"] == "IDENTITY_AFTER_INELIGIBLE_FULL_DEVELOPMENT_REFIT"
    np.testing.assert_allclose(calibrator.predict(raw), raw)


def test_constant_raw_predictions_are_never_manufactured_into_varying_probabilities():
    raw = np.full(40, .5)
    calibrator, report = select_development_calibrator(rows(), raw, probability_target=RAW_DIRECTION_TARGET)
    assert report["selection_status"] == "NO_INFORMATION_RETAINING_DEVELOPMENT_CANDIDATE"
    assert not any(item["eligible"] for item in report["candidate_eligibility"].values())
    assert np.ptp(calibrator.predict(raw)) == 0


def test_ineligible_full_refit_cannot_fall_back_to_development_ineligible_identity(monkeypatch):
    data = rows()
    raw = np.where(data.target, .5 + 2e-13, .5 - 2e-13)

    class Candidate:
        method = "platt"

        def __init__(self, flat):
            self.nondecreasing_constraint_active = flat
            self.model = SimpleNamespace(coef_=np.asarray([[0. if flat else 1e12]]))
            self.flat = flat

        def predict(self, probabilities):
            p = np.asarray(probabilities)
            return np.full(len(p), .5) if self.flat else np.where(p > .5, .9, .1)

    candidates = iter([Candidate(False), Candidate(True)])
    monkeypatch.setattr("ml.gameplan_development_selection._fit_platt", lambda *args: next(candidates))
    with pytest.raises(ValueError, match="identity is ineligible"):
        select_development_calibrator(data, raw, probability_target=RAW_DIRECTION_TARGET)
