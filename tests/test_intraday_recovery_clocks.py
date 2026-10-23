"""Synthetic clock proofs; no recovered market records in shareable fixtures."""
import pandas as pd
import pytest

from ml.intraday.data import build_dataset, validate_candles
from ml.intraday.models import purged_chronological_split
from ml.intraday.prediction import ContractError
from tests.test_intraday_data import candles


def recovered_frame():
    from ml.intraday.native_interval import RECOVERY_CONTRACT
    frame = candles(42)
    frame["availability_kind"] = "actual_historical_recovery"
    frame["native_recovery_contract"] = RECOVERY_CONTRACT
    frame["feature_cutoff_at"] = frame.opened_at + pd.Timedelta(minutes=15)
    frame["available_at"] = pd.Timestamp("2026-05-12T12:00Z")
    # A preceding overlap request completed after the reference's own request.
    frame.loc[10, "available_at"] = pd.Timestamp("2026-05-12T12:01Z")
    return frame


def test_recovered_training_waits_for_actual_latest_feature_receipt():
    frame = recovered_frame()
    reference = frame.opened_at.iloc[-1]
    dataset = build_dataset(frame, reference_at=reference, cutoff="2026-05-12T12:02Z", synthetic_policy="exclude")
    position = list(dataset.reference_at).index(frame.opened_at.iloc[25])
    assert dataset.label_available_at[position] == pd.Timestamp("2026-05-12T12:01Z")
    assert dataset.label_available_at.min() >= pd.Timestamp("2026-05-12T12:00Z")
    with pytest.raises(ContractError, match="Exact completed reference"):
        build_dataset(frame, reference_at=reference, cutoff="2026-05-11T23:59Z", synthetic_policy="exclude")


def test_new_receipts_cannot_enter_historical_calibration_base():
    frame = recovered_frame()
    dataset = build_dataset(frame, reference_at=frame.opened_at.iloc[-1], cutoff="2026-05-12T12:02Z", synthetic_policy="exclude")
    with pytest.raises(ContractError, match="No label windows remain"):
        purged_chronological_split(dataset.reference_at, dataset.target_close_at,
            label_available_at=dataset.label_available_at, fraction=.25)


@pytest.mark.parametrize("field,value", [("native_recovery_contract", "unreviewed"),
    ("feature_cutoff_at", "2026-05-12T00:00Z")])
def test_reconstruction_clock_needs_exact_reviewed_contract(field, value):
    frame = recovered_frame()
    frame[field] = value
    with pytest.raises(ContractError, match="Historical recovery"):
        validate_candles(frame)
