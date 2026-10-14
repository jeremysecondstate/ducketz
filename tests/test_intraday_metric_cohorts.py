"""Offline saved-forecast cohorts; no training, provider or trading paths."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from app.ui.intraday_data import (
    DEFAULT_METRIC_SCOPE, METRIC_SCOPES, forecast_metrics, metric_rows,
    prediction_timing, window_rows,
)


def row(identity="before", **changes):
    value = {
        "forecast_id": identity,
        "issued_at": "2026-10-14T20:15:30Z",
        "target_open": "2026-10-14T20:30:00Z",
        "target_close": "2026-10-14T20:45:00Z",
        "reference_open": 100,
        "probability_not_down": .7,
        "outcome_status": "scored",
        "outcome": {"target_not_down": True, "target_close_price": 101},
        "models": [
            {"name": "success", "status": "success", "probability_not_down": .7},
            {"name": "failed", "status": "failed", "probability_not_down": None},
        ],
    }
    value.update(changes)
    return value


@pytest.mark.parametrize(("issued", "expected"), [
    ("2026-10-14T20:29:59.999999Z", "Before target open"),
    ("2026-10-14T20:30:00Z", "During target"),
    ("2026-10-14T20:44:59.999999Z", "During target"),
    ("2026-10-14T20:45:00Z", "At or after target close"),
    ("2026-10-14T21:04:01Z", "At or after target close"),
    ("2026-10-14T13:30:00-07:00", "During target"),
    (datetime(2026, 10, 14, 20, 45, tzinfo=timezone.utc), "At or after target close"),
])
def test_timing_uses_exact_aware_instant_boundaries(issued, expected):
    assert prediction_timing(row(issued_at=issued)) == expected


@pytest.mark.parametrize("changes", [
    {"issued_at": None}, {"issued_at": "bad"},
    {"issued_at": "2026-10-14T20:15:30"},
    {"target_open": None}, {"target_open": "bad"},
    {"target_close": "2026-10-14T20:45:00"},
    {"target_close": "2026-10-14T20:30:00Z"},
    {"target_close": "2026-10-14T20:00:00Z"},
])
def test_incomplete_or_malformed_timing_never_enters_known_cohort(changes):
    value = row(**changes)
    assert prediction_timing(value) == "Timing unknown"
    assert metric_rows([value]) == ()
    assert metric_rows([value], scope="Timing unknown · all outcomes") == (value,)
    assert metric_rows([value], scope="All saved outcomes") == (value,)


def test_timing_ignores_misleading_flags_and_execution_dispositions():
    before = row(issue_after_target=True, execution_blocked_reason="recovered_older_cycle")
    after = row("after", issued_at="2026-10-14T21:04:01Z", issue_after_target=False,
                execution_blocked_reason=None)
    assert prediction_timing(before) == "Before target open"
    assert prediction_timing(after) == "At or after target close"
    assert metric_rows([before, after]) == (before,)


def test_observed_cohort_keeps_unfinished_counts_but_excludes_synthetic_scores():
    observed = row()
    synthetic = row("synthetic", probability_not_down=.1, outcome_status="synthetic")
    held = [row(status, outcome_status=status, outcome={})
            for status in ("pending", "missing", "unscorable")]
    late = row("late", issued_at="2026-10-14T21:04:01Z")
    rows = [observed, synthetic, *held, late]
    original = deepcopy(rows)
    admitted = metric_rows(rows)
    assert DEFAULT_METRIC_SCOPE == METRIC_SCOPES[0]
    assert admitted == (observed, *held)
    assert admitted[0] is observed
    assert rows == original
    metrics = forecast_metrics(admitted)
    assert metrics["issued"] == 4 and metrics["denominator"] == 1
    assert metrics["observed_denominator"] == 1 and metrics["synthetic_denominator"] == 0
    assert metrics["accuracy"] == 1 and metrics["brier"] == pytest.approx(.09)
    assert metrics["counts"] == dict(scored=1, pending=1, missing=1, unscorable=1, synthetic=0)
    assert sum(bucket["count"] for bucket in metrics["calibration"]) == 1
    included = forecast_metrics(metric_rows(rows, scope="Before target open · all outcomes"))
    assert included["denominator"] == 2 and included["accuracy"] == .5
    assert included["observed_denominator"] == included["synthetic_denominator"] == 1


@pytest.mark.parametrize(("issued", "scope"), [
    ("2026-10-14T20:30:00Z", "During target"),
    ("2026-10-14T20:45:00Z", "After target close"),
])
def test_during_and_after_target_scopes_have_explicit_outcome_policies(issued, scope):
    observed, synthetic = row(issued_at=issued), row("synthetic", issued_at=issued,
                                                  outcome_status="synthetic")
    assert metric_rows([observed, synthetic], scope=f"{scope} · observed") == (observed,)
    assert metric_rows([observed, synthetic], scope=f"{scope} · all outcomes") == (observed, synthetic)


def test_last_n_is_selected_from_admitted_cohort_and_original_rows_remain_available():
    before = row("first", issued_at="2026-10-14T20:15:30Z")
    second = row("second", issued_at="2026-10-14T20:20:00Z", outcome_status="pending", outcome={})
    late = row("late", issued_at="2026-10-14T20:50:00Z")
    now = datetime(2026, 10, 14, 21, tzinfo=timezone.utc)
    assert window_rows(metric_rows([before, second, late]), "Last 1", now) == (second,)
    assert forecast_metrics(window_rows(metric_rows([before, second, late]), "Last 1", now))["denominator"] == 0
    assert metric_rows([before, second, late], scope="All saved outcomes") == (before, second, late)


def test_same_cohort_majority_baseline_and_prevalence_use_scored_model_denominator():
    rows = [row(str(index), outcome={"target_not_down": index < 3, "target_close_price": 101})
            for index in range(4)]
    rows.append(row("pending", outcome_status="pending", outcome={}))
    metrics = forecast_metrics(rows)
    assert metrics["positive_label_fraction"] == .75
    assert metrics["majority_class_baseline_accuracy"] == .75
    assert "not_a_forecast_or_trade_return" in metrics["baseline_basis"]
    rows[0]["models"][0]["status"] = "failed"
    per_model = forecast_metrics(rows, model="success")
    assert per_model["denominator"] == 3
    assert per_model["positive_label_fraction"] == pytest.approx(2/3)
    assert per_model["majority_class_baseline_accuracy"] == pytest.approx(2/3)
    failed = forecast_metrics(rows, model="failed")
    assert failed["denominator"] == 0
    assert failed["positive_label_fraction"] is failed["majority_class_baseline_accuracy"] is None


def test_one_label_and_empty_cohorts_do_not_imply_balanced_accuracy():
    metrics = forecast_metrics(metric_rows([row(probability_not_down=.2)]))
    assert metrics["denominator"] == 1 and metrics["accuracy"] == 0
    assert metrics["balanced_accuracy"] is None
    assert metrics["positive_label_fraction"] == metrics["majority_class_baseline_accuracy"] == 1
    empty = forecast_metrics(metric_rows([row(issued_at="2026-10-14T21:00:00Z")]))
    assert empty["denominator"] == 0 and empty["accuracy"] is None
    assert empty["positive_label_fraction"] is empty["majority_class_baseline_accuracy"] is None


def test_unknown_scope_fails_explicitly():
    with pytest.raises(ValueError, match="Unknown metric scope"):
        metric_rows([row()], scope="unknown")
