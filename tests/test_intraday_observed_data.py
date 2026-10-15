"""Observed-only research preserves causality, exact labels, and source bytes."""
from copy import deepcopy
import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from ml.intraday.data import build_dataset
from ml.intraday.observed_data import FEATURE_POLICY, FEATURE_VERSION, build_observed_dataset
from ml.intraday.prediction import ContractError, STEP


def candles(count=45):
    first = pd.Timestamp("2026-05-11T13:30:00Z")
    rows = []
    for index in range(count):
        opened = first + index * STEP
        price = 100 + (index % 7) / 10
        rows.append(dict(
            symbol="AAPL", opened_at=opened, available_at=opened + STEP,
            availability_kind="actual", open=price, high=price + 1,
            low=price - 1, close=price + 0.1, volume=100 + index,
            source_id="test:EQUS.MINI", source_dataset="EQUS.MINI",
            session_id="2026-05-11", session_open=first.isoformat(),
            session_close=(first + pd.Timedelta(hours=12)).isoformat(),
            synthetic=False, no_trade=False, evidence_json="{}",
        ))
    frame = pd.DataFrame(rows)
    frame.attrs["intraday_history"] = {"source": "immutable_fixture", "files": [{"sha256": "fixture"}]}
    return frame


def build(frame, reference_index=39):
    reference = candles().opened_at.iloc[reference_index]
    return build_observed_dataset(frame, cutoff=reference + STEP, reference_at=reference)


def test_synthetic_insertions_do_not_change_observed_feature_inputs():
    original = candles()
    holes = [2, 6, 12, 18]
    observed = original.drop(index=holes).reset_index(drop=True)
    inserted = original.copy(deep=True)
    inserted.loc[holes, "synthetic"] = True
    inserted.loc[holes, ["open", "close", "high", "low", "volume"]] = [200, 200, 201, 199, 0]
    baseline, trial = build(observed), build(inserted)

    pd.testing.assert_index_equal(baseline.reference_at, trial.reference_at)
    np.testing.assert_array_equal(baseline.X, trial.X)
    np.testing.assert_array_equal(baseline.y, trial.y)
    np.testing.assert_array_equal(baseline.inference_X, trial.inference_X)
    fraction = trial.feature_names.index("synthetic_input_fraction20")
    assert np.all(trial.X[:, fraction] == 0) and trial.inference_X[0, fraction] == 0
    assert not any(row["synthetic"] for row in trial.inference["snapshot"]["candles"])
    assert trial.manifest["synthetic_candles_excluded"] == len(holes)
    assert trial.manifest["source_history"] == original.attrs["intraday_history"]


def test_synthetic_targets_and_references_are_excluded_without_shifting_labels():
    frame = candles()
    frame.loc[30, "synthetic"] = True
    trial = build(frame)
    assert frame.opened_at.iloc[28] not in trial.reference_at  # Exact target30 was synthetic.
    assert frame.opened_at.iloc[30] not in trial.reference_at
    assert frame.opened_at.iloc[29] in trial.reference_at  # Exact target31 is observed.
    assert trial.manifest["synthetic_labels_included"] == 0
    assert trial.manifest["label_provenance"] == "observed_reference_and_exact_observed_target_only"
    lookup = frame.set_index("opened_at")
    for position, reference in enumerate(trial.reference_at):
        target = lookup.loc[reference + 2 * STEP]
        assert not target.synthetic
        assert trial.y[position] == int(target.close >= lookup.loc[reference, "open"])
        assert trial.target_close_at[position] == reference + 3 * STEP
    assert trial.inference["target_close"] == (pd.Timestamp(trial.inference["reference_at"]) + 3 * STEP).isoformat()


@pytest.mark.parametrize("state", ["synthetic", "missing"])
def test_exact_reference_must_exist_and_be_observed(state):
    frame = candles()
    if state == "synthetic":
        frame.loc[39, "synthetic"] = True
    else:
        frame = frame.drop(index=39)
    with pytest.raises(ContractError, match="Exact.*reference.*" + state):
        build(frame)


def test_removed_interval_is_an_explicit_elapsed_gap():
    frame = candles()
    frame.loc[29, "synthetic"] = True
    trial = build(frame)
    row = trial.reference_at.get_loc(frame.opened_at.iloc[30])
    assert trial.X[row, trial.feature_names.index("elapsed_minutes_previous")] == 30
    assert trial.X[row, trial.feature_names.index("elapsed_minutes20")] == 315
    assert trial.inference_X[0, trial.feature_names.index("elapsed_minutes20")] == 315


def test_late_input_and_target_receipts_are_not_admitted_early():
    frame = candles()
    reference = frame.opened_at.iloc[39]
    correction_receipt = frame.opened_at.iloc[36] + pd.Timedelta(seconds=5)
    frame.loc[22, "available_at"] = correction_receipt
    frame.loc[31, "available_at"] = reference + 2 * STEP
    trial = build(frame)
    assert frame.opened_at.iloc[23] not in trial.reference_at
    assert frame.opened_at.iloc[29] not in trial.reference_at  # Target31 is unavailable.
    row = trial.reference_at.get_loc(frame.opened_at.iloc[22])
    assert trial.label_available_at[row] == correction_receipt
    assert trial.manifest["label_availability_policy"] == "max_target_and_feature_receipt"
    assert max(trial.label_available_at) <= reference + STEP
    frame.loc[39, "available_at"] = reference + 2 * STEP
    with pytest.raises(ContractError, match="Exact completed reference is unavailable"):
        build(frame)


def test_observed_snapshot_has_distinct_canonical_identity_and_preserves_originals():
    frame = candles()
    before, attrs = frame.copy(deep=True), deepcopy(frame.attrs)
    reference = frame.opened_at.iloc[39]
    trial = build_observed_dataset(frame, cutoff=reference + STEP, reference_at=reference, lookback_days=1)
    default = build_dataset(frame, cutoff=reference + STEP, reference_at=reference, lookback_days=1, synthetic_policy="exclude")
    np.testing.assert_array_equal(trial.X, default.X)
    np.testing.assert_array_equal(trial.inference_X, default.inference_X)
    for metadata in (trial.manifest, trial.inference, trial.inference["snapshot"]):
        assert metadata["feature_version"] == FEATURE_VERSION
        assert metadata["feature_policy"] == FEATURE_POLICY
    encoded = json.dumps(trial.inference["snapshot"], sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    assert trial.inference["snapshot_sha256"] == hashlib.sha256(encoded).hexdigest()
    assert trial.inference["snapshot_sha256"] != default.inference["snapshot_sha256"]
    pd.testing.assert_frame_equal(frame, before)
    assert frame.attrs == attrs


def test_future_synthetic_candle_does_not_change_causal_snapshot():
    frame = candles()
    original = build(frame)
    frame.loc[43, "synthetic"] = True
    changed = build(frame)
    assert original.inference == changed.inference
    np.testing.assert_array_equal(original.X, changed.X)
    assert changed.manifest["synthetic_candles_excluded"] == 1
    assert changed.manifest["synthetic_candles_excluded_at_cutoff"] == 0


def test_invalid_synthetic_rows_cannot_be_hidden_by_exclusion():
    frame = candles()
    frame.loc[10, "synthetic"] = True
    frame.loc[10, "high"] = 1
    with pytest.raises(ContractError, match="Invalid OHLC range"):
        build(frame)
