"""Preparation shortcuts preserve causal admission and native evidence checks."""
from copy import deepcopy
import json

import numpy as np
import pandas as pd
import pytest

from ml.intraday import recovered_population as recovered
from ml.intraday.coverage import aggregate_source_minutes
from ml.intraday.data import SessionBounds, build_dataset, causal_features, exact_target
from ml.intraday.prediction import ContractError, STEP, UnresolvedPolicy
from tests.test_intraday_data import candles
from tests.test_intraday_dataset_provenance_performance import assert_same_dataset


def reference_samples(frame, reference, cutoff, synthetic_policy, lookback_days):
    """Independent label/receipt oracle using timestamp selection, not positions."""
    eligible = frame.loc[(frame.opened_at <= reference) & (frame.available_at <= cutoff)
                         & (frame.opened_at + STEP <= cutoff)].sort_values("opened_at")
    features = causal_features(eligible)
    start = reference - pd.Timedelta(days=lookback_days) if lookback_days else eligible.opened_at.min()
    records, synthetic_count = [], 0
    for row in eligible.itertuples():
        target, close = exact_target(row.opened_at)
        matches = eligible.loc[eligible.opened_at.eq(target)]
        if row.opened_at < start or matches.empty or close > cutoff:
            continue
        outcome = matches.iloc[0]
        if str(row.session_id) != str(outcome.session_id):
            continue
        history = eligible.loc[eligible.opened_at.le(row.opened_at)].tail(21)
        values = features.loc[row.opened_at].to_numpy(dtype=float)
        if history.available_at.gt(row.available_at).any() or not np.isfinite(values).all():
            continue
        if row.synthetic or outcome.synthetic:
            synthetic_count += 1
            if synthetic_policy is None:
                raise UnresolvedPolicy("Synthetic")
            if synthetic_policy == "exclude":
                continue
        records.append((row.opened_at, close, max(row.available_at, outcome.available_at),
                        int(outcome.close >= row.open), values))
    return records, synthetic_count


@pytest.mark.parametrize("policy", [None, "include", "exclude"])
@pytest.mark.parametrize("lookback", [None, 0.125, 0.25])
@pytest.mark.parametrize("case", ["sparse", "late_receipt", "revised_receipt", "synthetic", "sessions"])
def test_causal_rows_match_timestamp_oracle(case, policy, lookback):
    frame = candles(46)
    reference = frame.opened_at.iloc[42]
    cutoff = reference + STEP
    if case == "sparse":
        frame = frame.drop(index=[5, 24, 32])
    elif case in {"late_receipt", "revised_receipt"}:
        # One nanosecond beyond row30's receipt must exclude row30. The
        # revised reference itself keeps its later joint label receipt.
        frame["available_at"] = frame.available_at.astype("datetime64[ns, UTC]")
        frame.loc[22, "available_at"] = frame.available_at.iloc[30] + pd.Timedelta(nanoseconds=1)
        if case == "revised_receipt":
            frame.loc[22, ["open", "high", "low", "close"]] *= 1.01
    elif case == "synthetic":
        frame.loc[30, "synthetic"] = True
    elif case == "sessions":
        frame.loc[31:, "session_id"] = "second-segment"
    # Unordered, non-contiguous external indices cannot shift feature rows.
    frame = frame.sample(frac=1, random_state=18)
    original = frame.copy(deep=True)
    if case == "synthetic" and policy is None:
        with pytest.raises(UnresolvedPolicy, match="Synthetic"):
            build_dataset(frame, cutoff=cutoff, reference_at=reference, synthetic_policy=policy, lookback_days=lookback)
        return
    expected, count = reference_samples(frame, reference, cutoff, policy, lookback)
    actual = build_dataset(frame, cutoff=cutoff, reference_at=reference, synthetic_policy=policy, lookback_days=lookback)
    assert list(actual.reference_at) == [row[0] for row in expected]
    assert list(actual.target_close_at) == [row[1] for row in expected]
    assert list(actual.label_available_at) == [row[2] for row in expected]
    np.testing.assert_array_equal(actual.y, [row[3] for row in expected])
    np.testing.assert_array_equal(actual.X, [row[4] for row in expected])
    assert actual.manifest["synthetic_labels_excluded"] == (count if policy == "exclude" else 0)
    assert actual.manifest["synthetic_labels_included"] == (count if policy == "include" else 0)
    ordered = build_dataset(frame.sort_values("opened_at").reset_index(drop=True), cutoff=cutoff,
                            reference_at=reference, synthetic_policy=policy, lookback_days=lookback)
    assert_same_dataset(actual, ordered)
    pd.testing.assert_frame_equal(frame, original)


def test_duplicate_revision_and_session_closure_remain_errors():
    frame = candles(45)
    reference = frame.opened_at.iloc[-1]
    duplicate = pd.concat([frame, frame.iloc[[22]]], ignore_index=True)
    with pytest.raises(ContractError, match="immutable candle revision"):
        build_dataset(duplicate, cutoff=reference + STEP, reference_at=reference)
    frame["session_close"] = reference + 2 * STEP
    with pytest.raises(UnresolvedPolicy, match="closure"):
        build_dataset(frame, cutoff=reference + STEP, reference_at=reference)


def primary_wrapper():
    minutes = candles(15, minutes=True)
    start = minutes.opened_at.iloc[0]
    end = start + STEP
    frame, _ = aggregate_source_minutes(minutes,
        sessions=[SessionBounds("2026-05-11", start, end)],
        source_coverage=[{"opened_at": start.isoformat(), "closed_at": end.isoformat(), "basis": "offline fixture"}])
    return recovered.wrap_primary(frame.iloc[0].to_dict())


@pytest.mark.parametrize("damage", [None, "price", "receipt", "digest"])
def test_primary_wrapper_validates_native_evidence_once(monkeypatch, damage):
    wrapped = primary_wrapper()
    evidence = json.loads(wrapped["evidence_json"])
    original = evidence["native_primary"]
    if damage == "price":
        original["high"] = original["low"] - 1
    elif damage == "receipt":
        original["available_at"] = original["opened_at"]
    if damage in {"price", "receipt"}:
        evidence["native_primary_sha256"] = recovered._digest(original)
    elif damage == "digest":
        evidence["native_primary_sha256"] = "0" * 64
    wrapped["evidence_json"] = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    validator = recovered.verified_source_observation
    calls = []
    def verify(value):
        calls.append(deepcopy(value))
        return validator(value)
    monkeypatch.setattr(recovered, "verified_source_observation", verify)
    assert recovered.verified_recovery_candle(wrapped) is (damage is None)
    assert len(calls) == (0 if damage == "digest" else 1)
