from __future__ import annotations

import numpy as np
import pytest

from ml.nightly_target_source_ablation import (
    _aggregate_records,
    _probability_metrics,
    _transition_matrix,
)


def test_aggregate_records_is_sorted_lf_without_trailing_lf() -> None:
    assert _aggregate_records(["b|2", "a|1"]) == _aggregate_records(
        ["a|1", "b|2"]
    )
    assert _aggregate_records(["a|1", "b|2"]) != _aggregate_records(
        ["a|1", "b|2", ""]
    )


def test_probability_metrics_preserve_binary_calibration_contract() -> None:
    metrics = _probability_metrics(
        [0, 1, 0, 1],
        [0.1, 0.9, 0.2, 0.8],
        sample_weight=np.array([0.5, 0.5, 0.5, 0.5]),
    )

    assert metrics["rows"] == 4
    assert metrics["target_base_rate"] == 0.5
    assert metrics["brier_score"] == pytest.approx(0.025)
    assert metrics["roc_auc"] == 1.0
    assert len(metrics["reliability_bins"]) == 10


def test_transition_matrix_reports_all_four_cells() -> None:
    assert _transition_matrix([0, 0, 1, 1], [0, 1, 0, 1]) == {
        "0_to_0": 1,
        "0_to_1": 1,
        "1_to_0": 1,
        "1_to_1": 1,
    }
