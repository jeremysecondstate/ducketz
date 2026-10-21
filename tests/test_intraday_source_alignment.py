"""Native source proofs retain exact alignment without repeated frequency parsing."""
from copy import deepcopy
from datetime import timedelta
import json

import numpy as np
import pandas as pd
import pytest

from ml.intraday import coverage
from ml.intraday.prediction import ContractError
from ml.intraday.recovered_population import verified_recovery_candle, wrap_primary


def source_candle(indices=tuple(range(15)), *, opened_at="2026-10-12T17:00:00Z"):
    """Construct explicit native evidence, without providers or private records."""
    opened = pd.Timestamp(opened_at)
    observations = [dict(opened_at=(opened + timedelta(minutes=i)).isoformat(),
        available_at=(opened + timedelta(minutes=i + 1)).isoformat(),
        source_id="offline-native", synthetic=False, open=100. + i,
        high=101. + i, low=99. + i, close=100.5 + i, volume=float(i + 1))
        for i in indices]
    evidence = dict(aggregation=coverage.POPULATION_CONTRACT,
        source_id="offline-native", source_dataset="OFFLINE_NATIVE",
        source_coverage=dict(opened_at=opened.isoformat(),
            closed_at=(opened + timedelta(minutes=15)).isoformat(), basis="explicit offline fixture"),
        observations=observations)
    return dict(symbol="AAPL", opened_at=opened.isoformat(),
        available_at=(opened + timedelta(minutes=15)).isoformat(),
        open=observations[0]["open"], high=max(row["high"] for row in observations),
        low=min(row["low"] for row in observations), close=observations[-1]["close"],
        volume=sum(row["volume"] for row in observations),
        source_id="offline-native", source_dataset="OFFLINE_NATIVE",
        session_id="offline-session", session_open=opened.isoformat(),
        session_close=(opened + timedelta(hours=1)).isoformat(),
        synthetic=False, no_trade=False, synthetic_open=False, synthetic_close=False,
        interval_seconds=900, synthetic_constituents=0,
        observed_constituents=len(indices), source_empty_constituents=15-len(indices),
        native_first_opened_at=observations[0]["opened_at"],
        native_last_opened_at=observations[-1]["opened_at"],
        population_contract=coverage.POPULATION_CONTRACT, evidence_json=evidence)


@pytest.mark.parametrize("indices", [tuple(range(15)), (0, 7, 14), (8,)])
@pytest.mark.parametrize("opened_at", ["2026-10-12T17:00:00Z", "1969-12-31T23:45:00Z",
    "2026-11-01T01:00:00-08:00"])
def test_dense_sparse_and_utc_converted_native_proofs_preserved(indices, opened_at):
    candle = source_candle(indices, opened_at=opened_at)
    original = deepcopy(candle)
    assert coverage.verified_source_observation(candle)
    wrapped = wrap_primary(candle)
    assert verified_recovery_candle(wrapped)
    retained = json.loads(wrapped["evidence_json"])["native_primary"]
    assert retained == original
    assert candle == original


@pytest.mark.parametrize("indices", [tuple(range(15)), (0, 7, 14), (8,)])
def test_verified_source_alignment_reuses_fixed_offsets(monkeypatch, indices):
    candle = source_candle(indices)
    original = np.isclose
    calls = []
    def observed(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)
    monkeypatch.setattr(np, "isclose", observed)
    assert coverage.verified_source_observation(candle)
    # Scalar Timestamp.floor string-frequency normalization otherwise calls
    # NumPy once per native observation, even when the minute is already exact.
    assert calls == []


@pytest.mark.parametrize("field", ["opened_at", "native_first_opened_at", "native_last_opened_at"])
@pytest.mark.parametrize("delta", [-1, 1])
def test_nanosecond_alignment_changes_remain_invalid(field, delta):
    candle = source_candle()
    candle[field] = pd.Timestamp(candle[field]) + pd.Timedelta(nanoseconds=delta)
    assert not coverage.verified_source_observation(candle)


@pytest.mark.parametrize("delta", [-1, 1])
def test_native_minute_nanosecond_alignment_changes_remain_invalid(delta):
    candle = source_candle()
    row = candle["evidence_json"]["observations"][4]
    row["opened_at"] = pd.Timestamp(row["opened_at"]) + pd.Timedelta(nanoseconds=delta)
    assert not coverage.verified_source_observation(candle)


@pytest.mark.parametrize("bad", ["not-a-time", "2026-10-12T17:00:00", None])
def test_malformed_or_naive_native_clock_rejected(bad):
    candle = source_candle()
    candle["evidence_json"]["observations"][0]["opened_at"] = bad
    assert not coverage.verified_source_observation(candle)


@pytest.mark.parametrize("case", ["nan_price", "inf_volume", "bad_ohlc", "aggregate_changed",
    "synthetic", "boolean_observed_count", "missing_observation", "duplicate_observation",
    "wrong_source", "wrong_aggregation", "coverage_starts_late", "coverage_ends_early",
    "native_available_early", "native_available_late", "invalid_evidence_json"])
def test_native_coverage_value_and_evidence_guards_remain(case):
    candle = source_candle()
    evidence = candle["evidence_json"]
    observations = evidence["observations"]
    if case == "nan_price": candle["open"] = float("nan")
    elif case == "inf_volume": candle["volume"] = float("inf")
    elif case == "bad_ohlc": observations[0]["high"] = 1.
    elif case == "aggregate_changed": candle["volume"] += 1.
    elif case == "synthetic": observations[0]["synthetic"] = True
    elif case == "boolean_observed_count": candle["observed_constituents"] = True
    elif case == "missing_observation": observations.pop()
    elif case == "duplicate_observation": observations[1] = deepcopy(observations[0])
    elif case == "wrong_source": observations[0]["source_id"] = "other-source"
    elif case == "wrong_aggregation": evidence["aggregation"] = "other-population"
    elif case == "coverage_starts_late": evidence["source_coverage"]["opened_at"] = "2026-10-12T17:01:00Z"
    elif case == "coverage_ends_early": evidence["source_coverage"]["closed_at"] = "2026-10-12T17:14:00Z"
    elif case == "native_available_early": observations[0]["available_at"] = "2026-10-12T17:00:00Z"
    elif case == "native_available_late": observations[0]["available_at"] = "2026-10-12T17:16:00Z"
    else: candle["evidence_json"] = "{invalid-json"
    assert not coverage.verified_source_observation(candle)
    with pytest.raises(ContractError, match="verified genuine primary"):
        wrap_primary(candle)


def test_mapping_and_serialized_evidence_have_identical_validation():
    candle = source_candle()
    serialized = dict(candle, evidence_json=json.dumps(candle["evidence_json"]))
    assert coverage.verified_source_observation(candle)
    assert coverage.verified_source_observation(serialized)
