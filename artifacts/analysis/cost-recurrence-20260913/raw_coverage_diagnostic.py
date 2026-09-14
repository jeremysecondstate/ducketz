"""Read-only native archive diagnosis; writes analysis artifacts only."""
from pathlib import Path
import json
import sys
import pandas as pd
import databento as db
from pandas.testing import assert_frame_equal

sys.path.insert(0, str(Path.cwd()))
from ml.stock_target_prices import load_stock_target_prices
from ml.gameplan_price_bands import _observation
from ml.artifacts import file_checksum

root = Path('C:/DATASTORE')
out = Path('artifacts/analysis/cost-recurrence-20260913')
out.mkdir(parents=True, exist_ok=True)
syms = ('AAPL', 'AMZN', 'GOOG', 'MU', 'NVDA', 'SNDK', 'COST')
bars, files, inv = load_stock_target_prices(root, symbols=syms, source_contract='xnas-itch-archive-v1')
bars.loc[bars.timestamp.ge(pd.Timestamp('2026-09-04', tz='UTC'))].to_parquet(
    out / 'raw-coverage-recent-prices.parquet', index=False)
(out / 'raw-coverage-source-inventory.json').write_text(json.dumps(inv, indent=2))
recent = []
for p in inv['partitions']:
    if p.get('end', '') <= '2026-09-08':
        continue
    mpath = Path(p['manifest_path'])
    m = json.loads(mpath.read_text())
    native = db.DBNStore.from_file(mpath.parent / m['raw']['path'])
    raw = native.to_df().reset_index()
    norm = pd.read_parquet(mpath.parent / m['normalized']['path'])
    if 'ts_event' not in norm.columns:
        norm = norm.reset_index()
    cols = list(raw.columns)
    assert_frame_equal(raw.loc[:, cols], norm.loc[:, cols], check_dtype=False)
    recent.append({
        'symbol': p['symbol'], 'start': p['start'], 'end': p['end'], 'manifest_path': str(mpath),
        'native_rows': len(raw), 'normalized_rows': len(norm), 'raw_normalized_exact_values_equal': True,
        'raw_sha256': file_checksum(mpath.parent / m['raw']['path']),
        'normalized_sha256': file_checksum(mpath.parent / m['normalized']['path']),
        'provider_warnings': m['provider_warnings'], 'native_metadata': str(native.metadata),
    })
density, clocks = [], []
for sym in syms:
    sb = bars[bars.symbol.eq(sym)].copy()
    sb['pacific'] = sb.timestamp.dt.tz_convert('America/Los_Angeles')
    sb['session_date'] = sb.pacific.dt.strftime('%Y-%m-%d')
    for day in ('2026-09-08', '2026-09-09', '2026-09-10', '2026-09-11'):
        daybars = sb[sb.session_date.eq(day)]
        minute = daybars.pacific.dt.hour * 60 + daybars.pacific.dt.minute
        segments = {'premarket_0400_0630': (240, 390), 'regular_0630_1300': (390, 780),
                    'afterhours_1300_1700': (780, 1020), 'late_1400_1700': (840, 1020)}
        d = {'symbol': sym, 'session': day, 'session_bar_rows': len(daybars),
             'first_pacific': str(daybars.pacific.min()), 'last_pacific': str(daybars.pacific.max())}
        for k, (a, b) in segments.items():
            d[k + '_rows'] = int(((minute >= a) & (minute < b)).sum())
            d[k + '_possible_minutes'] = b - a
        density.append(d)
        for hour in range(4, 18):
            bound = pd.Timestamp(f'{day} {hour:02}:00', tz='America/Los_Angeles').tz_convert('UTC')
            for close in (False, True):
                data = daybars.loc[daybars.timestamp.add(pd.Timedelta(minutes=1)).le(bound)] if close else daybars.loc[daybars.timestamp.ge(bound)]
                nearest = (data.iloc[-1] if close else data.iloc[0]) if len(data) else None
                observed = (nearest.timestamp + pd.Timedelta(minutes=1 if close else 0)) if nearest is not None else None
                gap = (bound - observed if close else observed - bound).total_seconds() / 60 if observed is not None else None
                val = _observation(daybars, bound, close=close)
                clocks.append({'symbol': sym, 'session': day, 'hour_pacific': hour,
                               'boundary_kind': 'close_before' if close else 'open_after',
                               'nearest_observation_pacific': str(observed.tz_convert('America/Los_Angeles')) if observed is not None else None,
                               'nearest_gap_minutes': gap, 'within_5m': val is not None,
                               'observation_price': float(nearest['close' if close else 'open']) if nearest is not None else None})
review = root / 'ml/gameplan-actuals-review-runs/20260912T051439.995563Z'
fr = pd.read_parquet(review / 'forecast-results.parquet')
pr = pd.read_parquet(review / 'price-results.parquet')
fr.loc[fr.symbol.eq('COST')].to_json(out / 'raw-coverage-cost-forecast-results.json', orient='records', date_format='iso', indent=2)
pr.loc[pr.symbol.eq('COST')].to_json(out / 'raw-coverage-cost-price-results.json', orient='records', date_format='iso', indent=2)
result = {'generated_at': pd.Timestamp.now(tz='UTC').isoformat(), 'source_contract': inv['source_contract'],
          'source_dataset': inv['dataset'], 'archive_inventory': inv['by_symbol'], 'recent_raw_verification': recent,
          'session_density': density, 'boundary_observations': clocks,
          'interpretation': 'Identical stored raw DBN and normalized rows establish no observed normalization loss, not proof of no trading. Missing observations remain missing under actuals five-minute policy.'}
(out / 'raw-coverage.json').write_text(json.dumps(result, indent=2))
pd.DataFrame(density).to_csv(out / 'raw-coverage-density.csv', index=False)
pd.DataFrame(clocks).to_csv(out / 'raw-coverage-boundaries.csv', index=False)
print('Verified raw-normalized partitions:', len(recent))
print(pd.DataFrame(density).to_string(index=False))
c = pd.DataFrame(clocks)
used = c[(c.hour_pacific.lt(17) & c.boundary_kind.eq('open_after')) |
         (c.hour_pacific.eq(17) & c.boundary_kind.eq('close_before'))]
summary = used.groupby(['session', 'symbol']).agg(hourly_points=('within_5m', 'size'),
                                                available=('within_5m', 'sum')).reset_index()
summary['missing'] = summary.hourly_points - summary.available
summary.to_csv(out / 'raw-coverage-hourly-summary.csv', index=False)
print('\nCOST hourly opens failing five-minute gate:')
print(c[c.symbol.eq('COST') & c.boundary_kind.eq('open_after') & ~c.within_5m].to_string(index=False))
print('\nCOST Friday close observations:')
print(c[c.symbol.eq('COST') & c.session.eq('2026-09-11') & c.hour_pacific.ge(13)].to_string(index=False))
print('\nSaved report:', out.resolve() / 'raw-coverage.json')
