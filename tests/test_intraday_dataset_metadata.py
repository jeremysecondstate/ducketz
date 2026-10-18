"""Large provenance stays in the manifest without repeated per-row copies."""
from copy import deepcopy

import numpy as np
import pandas as pd

from ml.intraday.data import build_dataset
from tests.test_intraday_data import candles


class CountedEvidence(dict):
    copies = 0

    def __deepcopy__(self, memo):
        type(self).copies += 1
        return type(self)(deepcopy(dict(self), memo))


def prepared(frame):
    reference = frame.opened_at.iloc[-1]
    return build_dataset(frame, reference_at=reference,
                         cutoff=reference + pd.Timedelta(minutes=16), synthetic_policy="exclude")


def test_large_source_manifest_preserves_dataset_and_caller_without_per_row_copying():
    counts = []
    for size in (32, 44):
        frame = candles(size)
        # Input gaps and a late receipt exercise row positions after filtering.
        frame = frame.drop(index=5).reset_index(drop=True)
        frame.loc[10, "available_at"] += pd.Timedelta(minutes=30)
        expected = prepared(frame)
        evidence = CountedEvidence(receipts=[{"id": str(i), "scope": "fixture"} for i in range(100)])
        original = {"intraday_history": {"source_evidence": evidence, "contract": "fixture-v1"}}
        frame.attrs = original
        CountedEvidence.copies = 0
        actual = prepared(frame)
        counts.append(CountedEvidence.copies)
        np.testing.assert_array_equal(actual.X, expected.X)
        np.testing.assert_array_equal(actual.y, expected.y)
        np.testing.assert_array_equal(actual.inference_X, expected.inference_X)
        assert actual.reference_at.equals(expected.reference_at)
        assert actual.target_close_at.equals(expected.target_close_at)
        assert actual.label_available_at.equals(expected.label_available_at)
        assert actual.inference == expected.inference
        assert actual.manifest == {**expected.manifest, "source_history": original["intraday_history"]}
        assert frame.attrs == original
    # Adding rows must not add full provenance copies in target/history lookup.
    assert counts[0] == counts[1]
