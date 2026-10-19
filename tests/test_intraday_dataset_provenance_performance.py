"""Dataset provenance stays intact without copying it into every pandas slice."""
from copy import deepcopy
from dataclasses import fields

import numpy as np
import pandas as pd
import pytest

from ml.intraday.coverage import POPULATION_CONTRACT, aggregate_source_minutes
from ml.intraday.data import SessionBounds, build_dataset
from ml.intraday.prediction import STEP
from tests.test_intraday_data import candles


class CopyTrackedList(list):
    copies = 0

    def __deepcopy__(self, memo):
        type(self).copies += 1
        result = type(self)()
        memo[id(self)] = result
        result.extend(deepcopy(item, memo) for item in self)
        return result


def assert_same_dataset(actual, expected):
    for field in fields(expected):
        left, right = getattr(actual, field.name), getattr(expected, field.name)
        if isinstance(right, np.ndarray):
            np.testing.assert_array_equal(left, right)
        elif isinstance(right, pd.Index):
            pd.testing.assert_index_equal(left, right)
        else:
            assert left == right, field.name


@pytest.mark.parametrize("count", [32, 44])
def test_history_provenance_copies_are_bounded_and_dataset_unchanged(count):
    frame = candles(count)
    reference = frame.opened_at.iloc[-3]
    cutoff = reference + STEP
    expected = build_dataset(frame, cutoff=cutoff, reference_at=reference)
    provenance = {"source_dataset": "EQUS.MINI", "source_coverage": CopyTrackedList(
        {"request": str(index), "sha256": str(index).zfill(64)} for index in range(64))}
    frame.attrs = {"intraday_history": provenance, "unrelated": {"retain": [1, 2, 3]}}
    original = frame.copy(deep=True)
    original_attrs = deepcopy(frame.attrs)
    expected.manifest["source_history"] = provenance

    CopyTrackedList.copies = 0
    actual = build_dataset(frame, cutoff=cutoff, reference_at=reference)
    copies_during_build = CopyTrackedList.copies

    assert_same_dataset(actual, expected)
    pd.testing.assert_frame_equal(frame, original)
    assert frame.attrs == original_attrs
    # Permit a small fixed detachment cost, independent of training-row count.
    # Count actual metadata copies instead of using a machine-dependent timer.
    assert copies_during_build <= 2


def test_population_from_history_attrs_and_native_provenance_are_preserved():
    minutes = candles(44 * 15, minutes=True)
    start = minutes.opened_at.iloc[0]
    end = start + pd.Timedelta(hours=12)
    session = SessionBounds("2026-05-11", start.isoformat(), end.isoformat())
    frame, aggregation = aggregate_source_minutes(minutes, sessions=[session], source_coverage=[{
        "opened_at": start.isoformat(), "closed_at": end.isoformat(), "basis": "offline native fixture"}])
    reference = frame.opened_at.iloc[-3]
    cutoff = reference + STEP
    expected = build_dataset(frame, cutoff=cutoff, reference_at=reference,
                             population_contract=POPULATION_CONTRACT)
    provenance = {"population_contract": POPULATION_CONTRACT, "aggregation": aggregation,
                  "source_objects": [{"path": "retained-original", "sha256": "a" * 64}]}
    frame.attrs["intraday_history"] = provenance
    original = frame.copy(deep=True)
    original_attrs = deepcopy(frame.attrs)
    expected.manifest["source_history"] = provenance

    actual = build_dataset(frame, cutoff=cutoff, reference_at=reference)

    assert_same_dataset(actual, expected)
    assert actual.manifest["population_contract"] == POPULATION_CONTRACT
    pd.testing.assert_frame_equal(frame, original)
    assert frame.attrs == original_attrs
