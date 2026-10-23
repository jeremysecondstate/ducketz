"""Offline source-empty reference projections retain genuine training data."""
from copy import deepcopy
from dataclasses import replace
import json

import numpy as np
import pandas as pd
import pytest

from ml.intraday.data import IntervalResponse, SessionBounds, STEP, completed_15m_observation
from ml.intraday.forward_fill import (CONTRACT, PRICE_BASIS, build_forward_fill_dataset,
                                     make_reference, policy_contract, verified_reference)
from ml.intraday.native_interval import build_native_interval_dataset, candle_matches_forecast
from ml.intraday.prediction import ContractError
from tests.test_intraday_market_recipe import frame


def empty_pair(reference, *, symbol="TEST"):
    opened = pd.Timestamp(reference)
    close = opened + STEP
    first = IntervalResponse(f"{opened}-empty1", symbol, "databento:EQUS.MINI:ohlcv-1m",
        "EQUS.MINI", opened.isoformat(), close.isoformat(),
        (close + pd.Timedelta(seconds=1)).isoformat(),
        (close + pd.Timedelta(seconds=2)).isoformat(), "success", (), opened.isoformat(), close.isoformat())
    second = replace(first, attempt_id=f"{opened}-empty2",
        request_started_at=(close + pd.Timedelta(seconds=6)).isoformat(),
        request_ended_at=(close + pd.Timedelta(seconds=7)).isoformat())
    return first, second


@pytest.fixture
def population():
    genuine = frame()
    final_day = genuine.session_id.iloc[-1]
    day_first = pd.Timestamp(genuine.loc[genuine.session_id.eq(final_day), "opened_at"].iloc[0])
    reference = day_first + 30 * STEP
    history = genuine.loc[pd.to_datetime(genuine.opened_at, utc=True).lt(reference)].copy()
    last = history.iloc[-1]
    session = SessionBounds(last.session_id, last.session_open, last.session_close)
    return history, reference, session


def projected(population, *, history=None, reference=None):
    original, at, session = population
    history = original if history is None else history
    at = at if reference is None else reference
    candle = make_reference(history, empty_pair(at), session=session)
    return pd.concat([history, pd.DataFrame([candle])], ignore_index=True), candle


def build(candles, reference):
    return build_forward_fill_dataset(candles, cutoff=reference + STEP + pd.Timedelta(seconds=8),
                                      reference_at=reference)


def test_reference_is_explicit_flat_derived_price_and_leaves_originals_unchanged(population):
    history, reference, session = population
    original = history.copy(deep=True)
    reference_row = make_reference(history, empty_pair(reference), session=session)
    assert verified_reference(reference_row)
    assert reference_row["forward_fill_contract"] == CONTRACT
    assert reference_row["reference_price_basis"] == PRICE_BASIS
    assert "native_interval_contract" not in reference_row
    assert not completed_15m_observation(reference_row)
    price = history.close.iloc[-1]
    assert [reference_row[k] for k in ("open", "high", "low", "close")] == [price] * 4
    assert reference_row["volume"] == 0 and reference_row["synthetic"] is True
    assert reference_row["no_trade"] is False and reference_row["observed_constituents"] == 0
    assert reference_row["anchor_age_seconds"] == 900
    assert reference_row["anchor_observation_age_seconds"] == 1620
    evidence = json.loads(reference_row["evidence_json"])
    assert evidence["policy"] == policy_contract()
    assert evidence["source_empty_is_not_market_wide_no_trade"] is True
    assert len(evidence["responses"]) == 2 and all(r["rows"] == [] for r in evidence["responses"])
    pd.testing.assert_frame_equal(history, original)


@pytest.mark.parametrize("change", [
    {"close": 999.0}, {"volume": 100.0}, {"synthetic": False}, {"no_trade": True},
    {"symbol": "OTHER"}, {"source_id": "other"}, {"source_dataset": "OTHER"},
    {"anchor_age_seconds": 0}, {"anchor_sha256": "a" * 64},
    {"available_at": "2026-05-01T00:00:00Z"}, {"native_interval_contract": "forged"},
    {"open": True}, {"interval_seconds": True},
])
def test_copied_or_tampered_reference_is_rejected(population, change):
    _, value = projected(population)
    value.update(change)
    assert not verified_reference(value)


@pytest.mark.parametrize("field", ["anchor", "responses", "policy"])
def test_evidence_tampering_is_rejected(population, field):
    _, value = projected(population)
    evidence = json.loads(value["evidence_json"])
    if field == "anchor":
        evidence[field]["close"] += 1
    elif field == "responses":
        evidence[field][-1]["coverage_close"] = evidence[field][-1]["interval_open"]
    else:
        evidence[field]["volume"] = "copied"
    value["evidence_json"] = json.dumps(evidence)
    assert not verified_reference(value)


def test_future_anchor_never_becomes_a_known_price(population):
    history, reference, session = population
    later = history.iloc[[-1]].copy()
    value = json.loads(later.evidence_json.iloc[0])
    received = reference + STEP + pd.Timedelta(minutes=1)
    value["coverage"]["request_ended_at"] = received.isoformat()
    later.loc[:, "available_at"] = received.isoformat()
    later.loc[:, "evidence_json"] = json.dumps(value)
    with pytest.raises(ContractError, match="No genuinely known"):
        make_reference(later, empty_pair(reference), session=session)


@pytest.mark.parametrize("kind", ["temporary_error", "unavailable_coverage", "permanent_error", "uncovered", "unstable", "preclose"])
def test_only_stable_successful_empty_coverage_authorizes_projection(population, kind):
    history, reference, session = population
    one, two = empty_pair(reference)
    if kind in {"temporary_error", "unavailable_coverage", "permanent_error"}:
        two = replace(two, status=kind)
    elif kind == "uncovered":
        two = replace(two, coverage_close=reference.isoformat())
    elif kind == "unstable":
        two = replace(two, request_started_at=one.request_ended_at)
    else:
        one = replace(one, request_started_at=reference.isoformat())
    with pytest.raises(ContractError):
        make_reference(history, (one, two), session=session)


def test_wrong_source_symbol_and_derived_only_history_cannot_supply_anchor(population):
    history, reference, session = population
    with pytest.raises(ContractError, match="source scope"):
        make_reference(history, empty_pair(reference, symbol="OTHER"), session=session)
    wrong = history.copy()
    wrong.loc[wrong.index[-1], "source_id"] = "other"
    with pytest.raises(ContractError, match="source scope"):
        make_reference(wrong, empty_pair(reference), session=session)
    _, candle = projected(population)
    with pytest.raises(ContractError, match="No genuinely known"):
        make_reference(pd.DataFrame([candle]), empty_pair(reference + STEP), session=session)


def test_genuine_training_rows_values_labels_and_receipts_remain_exact(population):
    history, reference, _ = population
    full, candle = projected(population)
    baseline_reference = pd.Timestamp(history.opened_at.iloc[-1])
    baseline = build_native_interval_dataset(history, cutoff=reference + STEP + pd.Timedelta(seconds=8),
                                             reference_at=baseline_reference)
    result = build(full, reference)
    np.testing.assert_array_equal(result.X, baseline.X)
    np.testing.assert_array_equal(result.y, baseline.y)
    pd.testing.assert_index_equal(result.reference_at, baseline.reference_at)
    pd.testing.assert_index_equal(result.target_close_at, baseline.target_close_at)
    pd.testing.assert_index_equal(result.label_available_at, baseline.label_available_at)
    assert result.manifest["synthetic_labels_included"] == 0
    assert result.manifest["reference_fill_reference"] == candle
    assert result.inference["reference_at"] == reference.isoformat()
    assert result.inference["reference_open"] == history.close.iloc[-1]
    fraction = result.feature_names.index("synthetic_input_fraction20")
    assert (result.X[:, fraction] == 0).all() and result.inference_X[0, fraction] == 1 / 20
    assert np.isfinite(result.inference_X).all()
    assert sum(row["synthetic"] for row in result.inference["snapshot"]["candles"]) == 1
    # Genuine future targets remain compatible, whereas projections are never targets.
    assert candle_matches_forecast(history.iloc[-1].to_dict(), result.inference)
    assert not candle_matches_forecast(candle, result.inference)


def test_long_empty_stretch_never_accumulates_zero_volume_training_or_features(population):
    history, reference, session = population
    first_empty = reference - 25 * STEP
    history = history.loc[pd.to_datetime(history.opened_at, utc=True).lt(first_empty)].copy()
    carried = [make_reference(history, empty_pair(first_empty + n * STEP), session=session)
               for n in range(26)]
    all_rows = pd.concat([history, pd.DataFrame(carried)], ignore_index=True)
    current_only = pd.concat([history, pd.DataFrame([carried[-1]])], ignore_index=True)
    result, baseline = build(all_rows, reference), build(current_only, reference)
    np.testing.assert_array_equal(result.X, baseline.X)
    np.testing.assert_array_equal(result.y, baseline.y)
    np.testing.assert_array_equal(result.inference_X, baseline.inference_X)
    assert np.isfinite(result.inference_X).all()
    assert result.manifest["older_derived_references_excluded"] == 25
    assert all(c["anchor_opened_at"] == carried[0]["anchor_opened_at"] for c in carried)


def test_observed_reference_uses_original_dataset_and_excludes_old_projections(population):
    history, reference, session = population
    last = pd.Timestamp(history.opened_at.iloc[-1])
    # Earlier carried records can coexist in supplied immutable audit history.
    gap = reference - 2 * STEP
    earlier = history.loc[pd.to_datetime(history.opened_at, utc=True).lt(gap)]
    old = make_reference(earlier, empty_pair(gap), session=session)
    combined = pd.concat([history, pd.DataFrame([old])], ignore_index=True)
    result = build(combined, last)
    baseline = build_native_interval_dataset(history, cutoff=last + STEP + pd.Timedelta(seconds=8), reference_at=last)
    np.testing.assert_array_equal(result.X, baseline.X)
    np.testing.assert_array_equal(result.y, baseline.y)
    np.testing.assert_array_equal(result.inference_X, baseline.inference_X)
    assert result.manifest["reference_price_basis"] == "observed_native"
    assert result.manifest["reference_fill_reference"] is None


def test_dataset_rejects_old_anchor_if_newer_genuine_price_is_available(population):
    history, reference, session = population
    old = make_reference(history.iloc[:-1], empty_pair(reference), session=session)
    with pytest.raises(ContractError, match="latest genuine"):
        build(pd.concat([history, pd.DataFrame([old])], ignore_index=True), reference)


def test_dataset_rejects_invalid_mixed_rows_and_unavailable_current_reference(population):
    history, reference, _ = population
    full, candle = projected(population)
    invalid = history.copy()
    invalid.loc[invalid.index[0], "native_interval_contract"] = "other"
    with pytest.raises(ContractError, match="genuine native"):
        build(pd.concat([invalid, pd.DataFrame([candle])], ignore_index=True), reference)
    with pytest.raises(ContractError, match="unavailable"):
        build_forward_fill_dataset(full, cutoff=reference + STEP, reference_at=reference)


def test_policy_value_is_not_mutable_global_state():
    changed = policy_contract()
    changed["price"] = "fake"
    assert policy_contract()["price"] == "latest_genuine_native_close"


def test_prior_session_anchor_is_explicit_and_never_moved_to_current_source_time(population):
    history, _, _ = population
    reference = pd.Timestamp("2026-05-12T13:30Z")
    session = SessionBounds("2026-05-12", reference.isoformat(), (reference + pd.Timedelta(hours=10)).isoformat())
    value = make_reference(history, empty_pair(reference), session=session)
    assert verified_reference(value)
    assert value["session_id"] != json.loads(value["evidence_json"])["anchor"]["session_id"]
    assert value["anchor_opened_at"] == pd.Timestamp(history.opened_at.iloc[-1]).isoformat()
    assert value["anchor_observation_age_seconds"] > 12 * 3600
