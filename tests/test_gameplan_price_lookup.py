"""Regression checks for call-local native-price lookup metadata."""
from dataclasses import FrozenInstanceError

import numpy as np
import pandas as pd
import pytest

from ml.gameplan_price_bands import (
    _ObservationLookup, _observation, _observation_interval, build_entry_price_bands,
)
from ml.independent_stock_targets import STOCK_TARGET_BOUNDARY_TOLERANCE
from ml.stock_target_prices import XNAS_STOCK_PRICE_SOURCE


def _frame(seconds=60):
    return pd.DataFrame({'symbol': 'TEST',
        'timestamp': pd.date_range('2026-10-15T12:00Z', periods=33, freq=f'{seconds}s').as_unit('ns'),
        'open': np.arange(33, dtype=float)+100, 'close': np.arange(33, dtype=float)+100.5,
        'observation_interval_seconds': seconds})


def _uncached_reference(bars, boundary, *, close):
    """Pre-optimization endpoint algorithm; retain independent positional lookup."""
    if bars.empty:
        return None
    timestamps = pd.DatetimeIndex(bars.timestamp)
    interval = _observation_interval(bars)
    position = (timestamps.searchsorted(boundary-interval, side='right')-1
                if close else timestamps.searchsorted(boundary, side='left'))
    if position < 0 or position >= len(bars):
        return None
    row = bars.iloc[position]
    observed = row.timestamp + (interval if close else pd.Timedelta(0))
    gap = boundary-observed if close else observed-boundary
    price = float(row['close' if close else 'open'])
    if not pd.Timedelta(0) <= gap <= STOCK_TARGET_BOUNDARY_TOLERANCE or not np.isfinite(price) or price <= 0:
        return None
    return price, observed.isoformat()


@pytest.mark.parametrize('seconds', [1, 60, 300])
@pytest.mark.parametrize('damage', ['none', 'duplicates', 'nan', 'zero', 'negative', 'infinite', 'empty'])
def test_cached_endpoint_matches_uncached_native_rule(seconds, damage):
    bars = _frame(seconds)
    if damage == 'duplicates':
        bars = pd.concat([bars, bars.iloc[11:12]]).sort_values('timestamp', kind='stable')
    elif damage == 'empty':
        bars = bars.iloc[:0]
    elif damage != 'none':
        bars.loc[11, ['open', 'close']] = {'nan': np.nan, 'zero': 0, 'negative': -1, 'infinite': np.inf}[damage]
    before = bars.copy(deep=True)
    lookup = _ObservationLookup.from_frame(bars)
    for i in (-400, -5, 0, 1, 11, 12, 32, 33, 38, 400):
        for delta in (-1, 0, 1):
            boundary = pd.Timestamp('2026-10-15T12:00Z') + pd.Timedelta(seconds=seconds*i, nanoseconds=delta)
            for close in (False, True):
                expected = _uncached_reference(bars, boundary, close=close)
                assert _observation(lookup, boundary, close=close) == expected
                assert _observation(bars, boundary, close=close) == expected
    pd.testing.assert_frame_equal(before, bars)


@pytest.mark.parametrize('values', [[60, 300], [60, None], [60, 2], [60, 'bad'], [60, np.inf], [True, 60]])
def test_lookup_validates_full_interval_column_before_search(values):
    bars = _frame().astype({'observation_interval_seconds': object})
    bars.loc[[0, len(bars)-1], 'observation_interval_seconds'] = values
    with pytest.raises(ValueError):
        _ObservationLookup.from_frame(bars)


def test_lookup_is_immutable_and_not_reused_after_input_change():
    bars = _frame()
    lookup = _ObservationLookup.from_frame(bars)
    boundary = bars.timestamp.iloc[11]
    expected = _observation(lookup, boundary, close=False)
    bars.loc[11, 'open'] = 999
    bars.loc[11, 'timestamp'] += pd.Timedelta(seconds=1)
    assert _observation(lookup, boundary, close=False) == expected
    assert _observation(bars, boundary, close=False) != expected
    with pytest.raises(ValueError):
        lookup.opens[0] = 5
    with pytest.raises(ValueError):
        lookup.timestamps.values[0] = 0
    with pytest.raises(FrozenInstanceError):
        lookup.interval = pd.Timedelta(seconds=300)
    assert not bars.attrs


def test_empty_lookup_and_one_sided_direct_frame_keep_existing_behavior():
    boundary = pd.Timestamp('2026-10-15T12:00Z')
    assert _observation(pd.DataFrame(), boundary, close=False) is None
    empty = pd.DataFrame(columns=['timestamp', 'open', 'close'])
    assert _observation(_ObservationLookup.from_frame(empty), boundary, close=True) is None
    assert _observation(_frame().drop(columns='close'), boundary, close=False) == (100.0, boundary.isoformat())
    end = boundary + pd.Timedelta(minutes=1)
    assert _observation(_frame().drop(columns='open'), end, close=True) == (100.5, end.isoformat())


def test_builder_scans_interval_once_per_used_symbol_and_keeps_report(monkeypatch):
    import ml.gameplan_price_bands as module
    prices = _frame()
    prices.attrs['stock_price_source'] = {'source_contract': XNAS_STOCK_PRICE_SOURCE, 'dataset': 'XNAS.ITCH'}
    forecasts = pd.DataFrame([{'symbol': 'TEST', 'action_date': '2026-10-16', 'route': f'1h@{hour:02d}:00',
        'target_role': 'EXECUTION', 'target_window_start': pd.Timestamp(f'2026-10-16T{hour:02d}:00', tz='America/Los_Angeles'),
        'target_price_source_contract': XNAS_STOCK_PRICE_SOURCE, 'target_price_dataset': 'XNAS.ITCH'}
        for hour in (4, 8, 12, 16)])
    counts = []
    original = module._observation_interval
    def counted(frame):
        counts.append(len(frame))
        return original(frame)
    monkeypatch.setattr(module, '_observation_interval', counted)
    result = build_entry_price_bands(prices, forecasts, observed_at='2026-10-16T04:00Z', lookback_sessions=4)
    assert counts == [len(prices)]
    assert len(result['statistics']) == 4
    assert all(row['price_band_status'] == 'UNAVAILABLE_REFERENCE_PRICE' for row in result['rows'])
    assert result['new_synthetic_prices_allowed'] is False
