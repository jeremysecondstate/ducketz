"""Focused point-in-time tests; callback fixtures establish contracts only."""
from datetime import datetime, timedelta, timezone

import pytest

from ml.intraday.evaluation import (
    EvaluationPoint, RETRAIN_INTERVALS, build_walk_forward_plan, compare_schedules,
    eligible_training_rows, purged_validation_rows, run_walk_forward,
)
from ml.intraday.prediction import ContractError, TARGET_VERSION


def points(n=8):
    base = datetime(2026, 10, 8, 17, tzinfo=timezone.utc)
    return [EvaluationPoint(reference_at=base + timedelta(minutes=15 * i),
                            issue_at=base + timedelta(minutes=15 * (i + 1), seconds=30),
                            target_available_at=base + timedelta(minutes=15 * i + 45, seconds=30),
                            target_not_down=i % 2 == 0, synthetic_target=i == 0) for i in range(n)]


def callbacks():
    fits, predictions = [], []
    def fit(*, training_cutoff, window_start):
        fits.append((training_cutoff, window_start))
        return {"cutoff": training_cutoff}, {"target_version": TARGET_VERSION,
                                           "latest_label_available_at": training_cutoff}
    def predict(*, fitted, point):
        # Evaluation must not leak realized target data through callback arguments.
        assert point.target_not_down is None and point.target_available_at is None
        assert point.synthetic_target is False
        predictions.append(point.issue_at)
        return {"probability_not_down": 0.5, "complete": True, "configured_count": 10, "successful_count": 10}
    return fit, predict, fits, predictions


def test_same_timestamp_grid_all_schedules_and_full_quarter_hour_benchmark():
    observed = points()
    plans = {name: build_walk_forward_plan(observed, schedule=name) for name in RETRAIN_INTERVALS}
    assert all([entry["point"].issue_at for entry in plan] == [p.issue_at for p in observed] for plan in plans.values())
    assert sum(entry["retrain"] for entry in plans["15m"]) == 8
    assert sum(entry["retrain"] for entry in plans["30m"]) == 4
    assert sum(entry["retrain"] for entry in plans["1h"]) == 2
    assert sum(entry["retrain"] for entry in plans["weekly"]) == 1
    assert all(entry["window_start"] is None for entry in plans["15m"])


def test_full_retraining_is_per_reference_cycle_even_when_issue_offsets_change():
    observed = points(3)
    observed[1] = EvaluationPoint(reference_at=observed[1].reference_at,
                                  issue_at=observed[1].issue_at - timedelta(seconds=20))
    assert all(p["retrain"] for p in build_walk_forward_plan(observed))
    plan = build_walk_forward_plan(observed, schedule="30m")
    assert [p["retrain"] for p in plan] == [True, False, True]


def test_matured_training_latest_reference_is_0930_at_101530_and_no_lookahead():
    base = datetime(2026, 10, 8, 9, tzinfo=timezone.utc)
    refs = [base + timedelta(minutes=15 * i) for i in range(5)]
    targets = [v + timedelta(minutes=45) for v in refs]
    available = [v + timedelta(seconds=30) for v in targets]
    cutoff = base + timedelta(minutes=75, seconds=30)
    selected = eligible_training_rows(refs, targets, available, training_cutoff=cutoff)
    assert refs[selected[-1]].strftime("%H:%M") == "09:30"
    # A late known target's event time does not override its true availability.
    available[2] += timedelta(minutes=1)
    selected = eligible_training_rows(refs, targets, available, training_cutoff=cutoff)
    assert refs[selected[-1]].strftime("%H:%M") == "09:15"


def test_synthetic_training_policy_is_explicit_and_lookback_does_not_extend_coverage():
    observed = points(4)
    refs = [p.reference_at for p in observed]
    targets = [p.target_close_at for p in observed]
    availability = [p.target_available_at for p in observed]
    flags = [True, False, False, False]
    with pytest.raises(ContractError, match="explicit"):
        eligible_training_rows(refs, targets, availability, training_cutoff=availability[-1], synthetic_targets=flags)
    included = eligible_training_rows(refs, targets, availability, training_cutoff=availability[-1],
                                      synthetic_targets=flags, synthetic_policy="include")
    excluded = eligible_training_rows(refs, targets, availability, training_cutoff=availability[-1],
                                      synthetic_targets=flags, synthetic_policy="exclude")
    assert included.tolist() == [0, 1, 2, 3] and excluded.tolist() == [1, 2, 3]
    shortened = eligible_training_rows(refs, targets, availability, training_cutoff=availability[-1],
                                       window_start=refs[2], synthetic_targets=flags, synthetic_policy="include")
    assert shortened.tolist() == [2, 3]


def test_chronological_validation_purges_future_window_overlap():
    observed = points(8)
    refs = [p.reference_at for p in observed]
    targets = [p.target_close_at for p in observed]
    train, validation = purged_validation_rows(refs, targets, validation_start=refs[4],
                                               validation_end=refs[-1] + timedelta(minutes=15))
    assert train.tolist() == [0, 1]
    assert validation.tolist() == [4, 5, 6, 7]
    assert targets[train[-1]] <= refs[validation[0]]


def test_real_callback_runs_measure_only_observable_outcomes_with_synthetic_breakdown():
    fit, predict, fits, predictions = callbacks()
    observed = points(4)
    result = run_walk_forward(observed, fit=fit, predict=predict,
                               evaluation_as_of=observed[1].target_available_at + timedelta(seconds=1),
                               synthetic_policy="include")
    assert len(fits) == len(predictions) == 4
    assert result["headline"]["scored"] == 2
    assert result["headline"]["accuracy"] == 0.5
    assert result["headline"]["brier"] == 0.25
    assert result["observed"]["scored"] == result["synthetic"]["scored"] == 1
    assert result["rows"][-1]["outcome_status"] == "pending"
    assert result["rows"][-1]["target_not_down"] is None
    assert result["rows"][-1]["synthetic_target"] is None
    assert all(r["training_seconds"] > 0 and r["inference_seconds"] > 0 for r in result["rows"])
    assert all(r["availability_kind"] == "assumed_historical_close" for r in result["rows"])


def test_retraining_failure_has_no_older_model_execution_fallback():
    fit, predict, fits, predictions = callbacks()
    observed = points(4)
    count = 0
    def failing_fit(**kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("genuine training failed")
        return fit(**kwargs)
    result = run_walk_forward(observed, fit=failing_fit, predict=predict, schedule="30m",
                               evaluation_as_of=observed[-1].target_available_at, synthetic_policy="include")
    assert [r["outcome_status"] for r in result["rows"]] == ["scored", "scored", "unscorable", "unscorable"]
    assert len(predictions) == 2
    assert result["rows"][2]["probability_not_down"] is None
    assert result["rows"][3]["probability_not_down"] is None
    assert result["rows"][2]["issued_at"] is None and result["rows"][3]["issued_at"] is None


def test_fit_evidence_rejects_future_labels():
    _, predict, _, _ = callbacks()
    def invalid_fit(*, training_cutoff, window_start):
        future = datetime.fromisoformat(training_cutoff) + timedelta(seconds=1)
        return object(), {"target_version": TARGET_VERSION, "latest_label_available_at": future}
    result = run_walk_forward(points(1), fit=invalid_fit, predict=predict, evaluation_as_of=points(1)[0].target_available_at,
                               synthetic_policy="include")
    assert result["headline"]["scored"] == 0
    assert result["rows"][0]["error"]["type"] == "ContractError"


def test_compare_schedules_and_lookbacks_retains_same_grid_without_invented_results():
    fit, predict, _, _ = callbacks()
    observed = points(4)
    results = compare_schedules(observed, fit=fit, predict=predict, evaluation_as_of=observed[-1].target_available_at,
                                 synthetic_policy="exclude", schedules=("15m", "30m"), lookbacks=(None, 30))
    assert len(results) == 4
    assert [r["full_training_count"] for r in results] == [4, 2, 4, 2]
    assert all(r["headline"]["scored"] == 3 and r["synthetic"]["scored"] == 0 for r in results)
    assert all([row["scheduled_issue_at"] for row in r["rows"]] == [p.issue_at.isoformat() for p in observed] for r in results)
    assert all(datetime.fromisoformat(row["issued_at"]) >= datetime.fromisoformat(row["decision_cutoff_at"])
               for result in results for row in result["rows"])


def test_issue_never_moves_target_and_duplicates_are_rejected():
    observed = points(1)[0]
    late = EvaluationPoint(reference_at=observed.reference_at, issue_at=observed.target_close_at)
    with pytest.raises(ContractError, match="fixed target"):
        late.validate()
    with pytest.raises(ContractError, match="chronological"):
        build_walk_forward_plan([observed, observed])


class MeasuredClock:
    """Callback-controlled real-work clock for deterministic latency contracts."""
    def __init__(self):
        self.seconds = 0.0
        self.actual_start = datetime(2026, 10, 12, 20, tzinfo=timezone.utc)

    def advance(self, seconds):
        self.seconds += seconds

    def perf_counter(self):
        return self.seconds

    def utc(self):
        return self.actual_start + timedelta(seconds=self.seconds)


def timed_callbacks(clock, *, fit_seconds, predict_seconds, fail_fit=False, fail_predict=False):
    def fit(*, training_cutoff, window_start):
        clock.advance(fit_seconds)
        if fail_fit:
            raise RuntimeError("fit failed after measured work")
        return object(), {"target_version": TARGET_VERSION, "latest_label_available_at": training_cutoff}

    def predict(*, fitted, point):
        clock.advance(predict_seconds)
        if fail_predict:
            raise RuntimeError("inference failed after measured work")
        return {"probability_not_down": .6, "complete": True, "configured_count": 10, "successful_count": 10}
    return fit, predict


def test_simulated_issue_follows_measured_training_inference_with_separate_actual_utc_clock():
    clock = MeasuredClock()
    fit, predict = timed_callbacks(clock, fit_seconds=7, predict_seconds=3)
    point = points(1)[0]
    result = run_walk_forward([point], fit=fit, predict=predict, evaluation_as_of=point.target_available_at,
                               synthetic_policy="include", clock=clock.utc, perf_counter=clock.perf_counter)
    row = result["rows"][0]
    assert row["scheduled_issue_at"] == row["decision_cutoff_at"] == point.issue_at.isoformat()
    assert row["training_cutoff"] == point.issue_at.isoformat()
    assert row["issued_at"] == (point.issue_at + timedelta(seconds=10)).isoformat()
    assert row["compute_started_at"] == clock.actual_start.isoformat()
    assert row["compute_completed_at"] == row["recorded_at"] == (clock.actual_start + timedelta(seconds=10)).isoformat()
    assert row["training_seconds"] == 7 and row["inference_seconds"] == 3 and row["compute_seconds"] == 10
    assert row["queue_delay_seconds"] == 0 and not row["issue_after_target"]
    assert result["timely"]["scored"] == 1 and result["late"]["scored"] == 0


def test_overrun_queue_delay_carries_to_later_cycles_without_moving_fixed_targets():
    clock = MeasuredClock()
    fit, predict = timed_callbacks(clock, fit_seconds=1800, predict_seconds=10)
    observed = points(2)
    result = run_walk_forward(observed, fit=fit, predict=predict,
                               evaluation_as_of=observed[-1].target_close_at + timedelta(hours=2),
                               synthetic_policy="include", clock=clock.utc, perf_counter=clock.perf_counter)
    first, second = result["rows"]
    assert first["issued_at"] == (observed[0].issue_at + timedelta(seconds=1810)).isoformat()
    assert second["simulated_started_at"] == first["issued_at"]
    assert second["queue_delay_seconds"] == 910
    assert second["issued_at"] == (observed[0].issue_at + timedelta(seconds=3620)).isoformat()
    assert second["decision_cutoff_at"] == second["training_cutoff"] == observed[1].issue_at.isoformat()
    assert [row["target_close_at"] for row in result["rows"]] == [point.target_close_at.isoformat() for point in observed]
    assert first["issue_after_target"] and second["issue_after_target"]
    assert result["late_issue_count"] == result["late"]["scored"] == 2
    assert result["timely"]["scored"] == 0


@pytest.mark.parametrize("phase", ["training", "inference"])
def test_failed_callback_consumption_is_recorded_without_an_issued_fake_forecast(phase):
    clock = MeasuredClock()
    fit, predict = timed_callbacks(clock, fit_seconds=7, predict_seconds=3,
                                   fail_fit=phase == "training", fail_predict=phase == "inference")
    point = points(1)[0]
    result = run_walk_forward([point], fit=fit, predict=predict, evaluation_as_of=point.target_available_at,
                               synthetic_policy="include", clock=clock.utc, perf_counter=clock.perf_counter)
    row = result["rows"][0]
    assert row["issued_at"] is None and row["issue_after_target"] is None
    assert row["probability_not_down"] is None and row["outcome_status"] == "unscorable"
    assert row["compute_seconds"] == (7 if phase == "training" else 10)
    assert row["compute_completed_at"] is not None and row["simulated_completed_at"] is not None
    assert result["issued_count"] == result["headline"]["scored"] == 0


def test_work_not_issued_by_evaluation_cutoff_does_not_get_a_retroactive_score():
    clock = MeasuredClock()
    fit, predict = timed_callbacks(clock, fit_seconds=1900, predict_seconds=1)
    point = points(1)[0]
    result = run_walk_forward([point], fit=fit, predict=predict, evaluation_as_of=point.target_available_at,
                               synthetic_policy="include", clock=clock.utc, perf_counter=clock.perf_counter)
    row = result["rows"][0]
    assert row["issue_after_target"] is True
    assert row["outcome_status"] == "pending_issue" and row["target_not_down"] is None
    assert result["headline"]["scored"] == 0
