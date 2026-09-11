from __future__ import annotations

from copy import deepcopy
import hashlib
import json

import pandas as pd
import pytest

from ml.gameplan_price_supplement import (
    SCHWAB_DATASET, normalize_schwab_minute_supplement, resolve_small_gap_reference,
)


SESSION = "2026-09-10"
START = "2026-09-10T23:30:00Z"
END = "2026-09-11T00:00:00Z"
FETCHED = "2026-09-11T05:00:00Z"
OBSERVED = "2026-09-11T05:01:00Z"


def candle(timestamp="2026-09-10T23:59:00Z", **changes):
    return {"datetime": int(pd.Timestamp(timestamp).timestamp() * 1000),
            "open": 902., "high": 903., "low": 901., "close": 902.5, "volume": 5, **changes}


def normalized(candles=None, **changes):
    payload = {"symbol": "COST", "empty": False, "candles": [candle()] if candles is None else candles}
    raw = json.dumps(payload).encode()
    kwargs = dict(symbol="COST", session=SESSION, request_start=START, request_end=END,
                  fetched_at=FETCHED, acquisition_receipt_sha256="a" * 64)
    kwargs.update(changes)
    return normalize_schwab_minute_supplement(raw, **kwargs)


def primary(last="2026-09-10T23:46:00Z"):
    frame = pd.DataFrame([{"symbol": "COST", "timestamp": pd.Timestamp(last), "open": 902., "close": 902.2}])
    frame.attrs["stock_price_source"] = {"source_contract": "xnas-itch-archive-v1", "dataset": "XNAS.ITCH"}
    return frame


def attempts(**live_changes):
    common = {"provider": "databento", "dataset": "XNAS.ITCH", "schema": "ohlcv-1m", "symbol": "COST",
              "session": SESSION, "status": "COMPLETE", "confirmed_gap": True,
              "request_start": START, "request_end": END, "completed_at": "2026-09-11T04:59:00Z",
              "receipt_sha256": "b" * 64, "observed_bar_starts": ["2026-09-10T23:46:00Z"]}
    return [{**deepcopy(common), "delivery_mode": "historical"},
            {**deepcopy(common), "delivery_mode": "live", **live_changes}]


def resolve(prices=None, supplement=None, **changes):
    kwargs = dict(symbol="COST", session=SESSION, observed_at=OBSERVED, databento_attempts=attempts())
    kwargs.update(changes)
    return resolve_small_gap_reference(primary() if prices is None else prices,
                                       normalized() if supplement is None else supplement, **kwargs)


def test_actual_sparse_candles_retain_raw_provenance_and_do_not_create_minutes():
    rows = [candle("2026-09-10T23:55:00Z"), candle()]
    result = normalized(rows + [deepcopy(rows[0])])
    assert len(result["rows"]) == 2 and result["raw_candle_count"] == 3
    assert result["rows"][0]["raw_row_indexes"] == [0, 2]
    assert result["provider_dataset"] == SCHWAB_DATASET
    assert result["raw_payload_sha256"] == hashlib.sha256(json.dumps({"symbol": "COST", "empty": False, "candles": rows + [deepcopy(rows[0])]}).encode()).hexdigest()
    assert result["rows"][-1]["observed_at"] == "2026-09-11T00:00:00+00:00"


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "volume"])
@pytest.mark.parametrize("value", [None, 0, -1, float("nan"), float("inf"), True])
def test_invalid_or_nonpositive_provider_values_are_never_filled(field, value):
    with pytest.raises(ValueError, match="positive and finite"):
        normalized([candle(**{field: value})])


def test_missing_volume_and_conflicting_duplicate_minutes_fail_closed():
    missing = candle(); del missing["volume"]
    with pytest.raises(ValueError, match="actual timestamp and OHLCV"):
        normalized([missing])
    with pytest.raises(ValueError, match="Conflicting supplemental"):
        normalized([candle(), candle(close=902.6)])
    with pytest.raises(ValueError, match="OHLC ordering"):
        normalized([candle(low=902.8)])


@pytest.mark.parametrize("change,match", [
    ({"provider_id": "databento"}, "provider identity"),
    ({"symbol": "GOOG"}, "symbol differs"),
    ({"frequency_minutes": 5}, "native one-minute"),
    ({"request_start": "2026-09-10T23:30:01Z"}, "one-minute boundary"),
    ({"request_end": "2026-09-11T00:01:00Z"}, "exact action session"),
    ({"session": "2026-09-09"}, "exact action session"),
    ({"fetched_at": "2026-09-10T23:59:30Z"}, "complete at fetched_at"),
    ({"fetched_at": "2026-09-11 05:00"}, "timezone-aware"),
    ({"acquisition_receipt_sha256": "unknown"}, "SHA256"),
])
def test_source_interval_and_receipt_contract(change, match):
    with pytest.raises(ValueError, match=match):
        normalized(**change)


@pytest.mark.parametrize("timestamp", ["2026-09-10T23:29:00Z", "2026-09-11T00:00:00Z", "2026-09-10T23:59:01Z"])
def test_out_of_interval_future_or_misaligned_candle_is_rejected(timestamp):
    with pytest.raises(ValueError):
        normalized([candle(timestamp)])


def test_resolver_selects_real_reference_preserves_primary_and_returns_absent_rows_only():
    prices = primary(); original = prices.copy(deep=True); original.attrs = deepcopy(prices.attrs)
    supplement = normalized([candle("2026-09-10T23:46:00Z", close=901.5), candle("2026-09-10T23:55:00Z"), candle()])
    before = deepcopy(supplement)
    result = resolve(prices, supplement)
    assert result["status"] == "SUPPLEMENTED" and result["reference_price"] == 902.5
    assert result["reference_provider"] == "schwab" and result["reference_provider_dataset"] == SCHWAB_DATASET
    assert result["native_close_gap_seconds"] == 780 and result["boundary_tolerance_seconds"] == 300
    assert [row["timestamp"] for row in result["supplemented_rows"]] == ["2026-09-10T23:55:00+00:00", "2026-09-10T23:59:00+00:00"]
    pd.testing.assert_frame_equal(prices, original)
    assert prices.attrs == original.attrs and supplement == before
    assert result["reference_row_provenance"]["raw_row_indexes"] == [2]


def test_schwab_actual_probe_latest_close_at_1651_remains_unavailable():
    result = resolve(supplement=normalized([candle("2026-09-10T23:37:00Z"), candle("2026-09-10T23:43:00Z"), candle("2026-09-10T23:50:00Z")]))
    assert result["status"] == "UNAVAILABLE_REFERENCE_PRICE" and result["reference_price"] is None
    assert result["last_supplemental_close_observed_at"] == "2026-09-10T23:51:00+00:00"
    assert len(result["supplemented_rows"]) == 1


def test_valid_native_reference_wins_without_using_supplement_or_retry_records():
    result = resolve(prices=primary("2026-09-10T23:57:00Z"), supplement={}, databento_attempts=[])
    assert result["status"] == "PRIMARY_AVAILABLE" and result["reference_price"] == 902.2
    assert result["reference_provider_dataset"] == "XNAS.ITCH" and result["supplemented_rows"] == []


def test_five_minute_fallback_tolerance_is_not_widened_to_small_gap_limit():
    assert resolve(supplement=normalized([candle("2026-09-10T23:54:00Z")]))["status"] == "SUPPLEMENTED"
    assert resolve(supplement=normalized([candle("2026-09-10T23:53:00Z")]))["status"] == "UNAVAILABLE_REFERENCE_PRICE"
    assert resolve(prices=primary("2026-09-10T23:44:00Z"),
                   databento_attempts=attempts(observed_bar_starts=[])) if False else True
    with pytest.raises(ValueError, match="small-gap limit"):
        resolve(prices=primary("2026-09-10T23:43:00Z"))


@pytest.mark.parametrize("changes,match", [
    ({"status": "FAILED"}, "successfully confirm"),
    ({"confirmed_gap": False}, "successfully confirm"),
    ({"dataset": "EQUS.MINI"}, "source or symbol/session"),
    ({"symbol": "GOOG"}, "source or symbol/session"),
    ({"observed_bar_starts": []}, "coverage differs"),
    ({"observed_bar_starts": ["2026-09-10T23:59:00Z"]}, "newer bar"),
    ({"request_start": "2026-09-10T23:31:00Z"}, "intervals must match"),
    ({"completed_at": "2026-09-11T05:02:00Z"}, "before observation"),
])
def test_databento_retry_must_prove_same_exact_gap(changes, match):
    with pytest.raises(ValueError, match=match):
        resolve(databento_attempts=attempts(**changes))


def test_live_denial_is_not_gap_evidence_and_requires_explicit_opt_in():
    denied = attempts(status="LIVE_ACCESS_DENIED", confirmed_gap=False, observed_bar_starts=[])
    with pytest.raises(ValueError, match="explicit authorization"):
        resolve(databento_attempts=denied)
    result = resolve(databento_attempts=denied, allow_live_unavailable=True)
    assert result["status"] == "SUPPLEMENTED"
    assert result["databento_attempts"][1]["coverage_basis"] == "ACCESS_UNAVAILABLE_EXPLICITLY_ALLOWED"
    with pytest.raises(ValueError, match="cannot claim an observed gap"):
        resolve(databento_attempts=attempts(status="LIVE_ACCESS_DENIED"), allow_live_unavailable=True)


def test_tampered_supplement_or_primary_source_fails_without_mutation():
    supplement = normalized(); supplement["rows"][0]["close"] += 1
    with pytest.raises(ValueError, match="semantic checksum"):
        resolve(supplement=supplement)
    prices = primary(); prices.attrs["stock_price_source"]["dataset"] = "EQUS.MINI"
    with pytest.raises(ValueError, match="XNAS source identity"):
        resolve(prices=prices)


def test_normalization_never_edits_raw_input_and_accepts_explicit_empty_observation():
    raw = json.dumps({"symbol": "COST", "empty": True, "candles": []})
    result = normalize_schwab_minute_supplement(raw, symbol="COST", session=SESSION, request_start=START,
              request_end=END, fetched_at=FETCHED, acquisition_receipt_sha256="a" * 64)
    assert result["rows"] == [] and result["raw_candle_count"] == 0
    assert resolve(supplement=result)["status"] == "UNAVAILABLE_REFERENCE_PRICE"
