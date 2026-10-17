"""Reject known synthetic provenance even when a normalizer retained it raw."""
from copy import deepcopy

import pytest

from ml.intraday.market_context import build_market_context, snapshot_digest

CUTOFF = "2026-10-16T14:15:10Z"


def snapshot(kind, schema, row):
    value = {"schema_version": "intraday-market-snapshot-v1", "snapshot_id": "quality",
        "source_kind": kind, "source_id": "quality:" + schema, "symbol": "DOCU",
        "provider": "fixture", "dataset": "fixture", "schema": schema,
        "quarter_start": "2026-10-16T14:00:00Z", "quarter_end": "2026-10-16T14:15:00Z",
        "requested_start": "2026-10-16T14:00:00Z", "requested_end": "2026-10-16T14:15:00Z",
        "receipt_at": "2026-10-16T14:15:05Z", "available_at": "2026-10-16T14:15:05Z",
        "status": "COMPLETE", "records": [row]}
    value["snapshot_sha256"] = snapshot_digest(value)
    return value


def context(value):
    return build_market_context([value], symbol="DOCU", cutoff=CUTOFF)


@pytest.mark.parametrize("field", ["synthetic", "is_synthetic", "no_trade", "is_no_trade", "source_empty", "is_source_empty"])
@pytest.mark.parametrize("value", [True, 0, "false", None])
@pytest.mark.parametrize("location", ["projection", "raw"])
def test_known_or_ambiguous_marker_never_enters_quote_feature(field, value, location):
    row = {"symbol": "DOCU", "event_at": "2026-10-16T14:14:00Z", "bid": 99, "ask": 101}
    if location == "raw":
        row.update(synthetic=False, raw={field: value})
    else:
        row[field] = value
    saved = snapshot("stock_quote", "quote", row)
    original = deepcopy(saved)
    result = context(saved)
    assert result.features["ctx_stock_quote_source_available"] == 0
    assert result.features["ctx_stock_quote_relative_spread_missing"] == 1
    assert result.source_evidence[0]["exclusions"] == {"synthetic_or_unverified_observation_marker": 1}
    assert saved == original  # native evidence is never rewritten


def test_zero_trade_volume_does_not_remove_a_real_quote():
    row = {"symbol": "DOCU", "event_at": "2026-10-16T14:14:00Z", "bid": 99, "ask": 101,
        "volume": 0, "synthetic": False, "raw": {"is_synthetic": False}}
    assert context(snapshot("stock_quote", "quote", row)).features["ctx_stock_quote_relative_spread"] == .02


@pytest.mark.parametrize("volume", [0, -1, None, float("nan")])
def test_bar_without_positive_observed_volume_stays_unavailable(volume):
    row = {"symbol": "DOCU", "event_at": "2026-10-16T14:14:00Z",
        "open": 100, "high": 101, "low": 99, "close": 100, "volume": volume}
    result = context(snapshot("stock_bars", "ohlcv-1m", row))
    assert result.features["ctx_stock_bars_source_available"] == 0
    assert result.features["ctx_stock_bars_price_return_missing"] == 1


def test_invalid_bar_geometry_is_not_observed_support():
    row = {"symbol": "DOCU", "event_at": "2026-10-16T14:14:00Z",
        "open": 100, "high": 99, "low": 98, "close": 100, "volume": 1}
    result = context(snapshot("stock_bars", "ohlcv-1m", row))
    assert any(item.get("exclusions") == {"invalid_observed_ohlc": 1} for item in result.source_evidence)


@pytest.mark.parametrize("kind,schema,row", [
    ("cross_asset", "ohlcv-1m", {"symbol": "ESZ6", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 0, "size": 10}),
    ("cross_asset", "trades", {"symbol": "ESZ6", "price": 100, "size": 0}),
])
def test_native_cross_asset_zero_volume_is_not_observed_support(kind, schema, row):
    row["event_at"] = "2026-10-16T14:14:00Z"
    result = context(snapshot(kind, schema, row))
    assert result.features["ctx_cross_asset_source_available"] == 0
    assert any(item.get("exclusions") == {"missing_or_nonpositive_observed_volume": 1} for item in result.source_evidence)


def test_corrected_cache_identity_cannot_hit_legacy_vectors(tmp_path, monkeypatch):
    import ml.intraday.market_context_cache as cache
    row = {"symbol": "DOCU", "event_at": "2026-10-16T14:14:00Z", "bid": 99, "ask": 101}
    saved = snapshot("stock_quote", "quote", row)
    path = tmp_path / "cache.sqlite3"
    current = cache.VERSION
    monkeypatch.setattr(cache, "VERSION", "intraday-exact-causal-context-cache-v1")
    with cache.ContextCache(path) as old:
        old.build([saved], symbol="DOCU", cutoff=CUTOFF)
        assert old.misses == 1
    monkeypatch.setattr(cache, "VERSION", current)
    with cache.ContextCache(path) as new:
        new.build([saved], symbol="DOCU", cutoff=CUTOFF)
        assert new.misses == 1 and new.hits == 0
