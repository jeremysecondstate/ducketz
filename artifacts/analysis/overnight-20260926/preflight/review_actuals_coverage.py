"""Bounded saved-publication review; no price archive, broker, provider or model calls."""
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
import hashlib
import json

import numpy as np
import pandas as pd

ROOT = Path('C:/DATASTORE')
RUN = ROOT / 'ml/gameplan-actuals-review-runs/20260926T063044.178275Z'
OUT = Path('C:/dev/ducketz/artifacts/analysis/overnight-20260926/preflight')


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def records(frame):
    return json.loads(frame.to_json(orient='records', date_format='iso'))


def equal_frames(left, right, columns, key='id'):
    try:
        pd.testing.assert_frame_equal(
            left.set_index(key).sort_index()[columns],
            right.set_index(key).sort_index()[columns],
            check_dtype=False, check_exact=True,
        )
        return {'equal': True, 'rows': len(left), 'columns': len(columns)}
    except AssertionError as exc:
        return {'equal': False, 'error': str(exc)}


report = read_json(RUN / 'report.json')
manifest = read_json(RUN / 'manifest.json')
receipt = read_json(RUN / 'receipt.json')
forecasts = pd.read_parquet(RUN / 'forecast-results.parquet')
prices = pd.read_parquet(RUN / 'price-results.parquet')
source_forecasts = pd.read_parquet(Path(report['source_gameplan_path']) / 'forecasts.parquet')
source_plan = pd.read_parquet(Path(report['source_trade_plan_path']) / 'trade-plan.parquet')
source_path = read_json(Path(report['source_trade_plan_path']) / 'planning-price-path.json')

bindings = []
for name, item in manifest['output_files'].items():
    p = RUN / name
    bindings.append({'kind': 'output', 'path': str(p), 'sha256': sha(p),
                     'verified': sha(p) == item['checksum_sha256'] and p.stat().st_size == item['size']})
for item in manifest['input_files']:
    if 'market-data' in item['path']:
        continue  # Root's full audit verifies raw/native archive inputs.
    p = ROOT / item['path']
    bindings.append({'kind': 'saved_publication_input', 'path': str(p), 'sha256': sha(p),
                     'verified': item['status'] == 'present' and sha(p) == item['checksum_sha256']
                     and p.stat().st_size == item['size']})

frozen_forecasts = equal_frames(source_forecasts, forecasts, [c for c in source_forecasts if c != 'id'])
frozen_plan = equal_frames(source_plan, forecasts, [c for c in source_plan if c != 'id'])
point_checks = []
for row in prices.itertuples(index=False):
    key = f'{row.symbol}|{row.action_date}|{row.clock_local}'
    point = source_path['points'][key]
    point_checks.append({'key': key, 'preserved': all(
        getattr(row, column) == point[column]
        for column in ('planned_price_low', 'planned_price_mid', 'planned_price_high')
    ) and pd.Timestamp(row.timestamp) == pd.Timestamp(point['timestamp'])})

missing = forecasts[forecasts.actuals_status.eq('MATURE_AWAITING_DATA')]
pending = forecasts[forecasts.actuals_status.eq('PENDING_MATURITY')]
evaluated = forecasts[forecasts.actuals_status.eq('EVALUATED')]
missing_prices = prices[prices.comparison_status.eq('MATURE_AWAITING_DATA')]
directional = evaluated[evaluated.direction.isin(['BULLISH', 'BEARISH'])]
raw_correct = (directional.direction.eq('BULLISH') & directional.actual_return.gt(0)) | (
    directional.direction.eq('BEARISH') & directional.actual_return.lt(0))
partitions = report['price_inventory']['partitions']
bound_paths = {str(ROOT / i['path']).replace('\\', '/').lower() for i in manifest['input_files']}

coverage = []
for kind, rows, sides in [('forecast', missing, ['start', 'end']), ('clock_price', missing_prices, [''])]:
    for _, row in rows.iterrows():
        for side in sides:
            prefix = 'actual_' + (side + '_' if side else '')
            start = pd.Timestamp(row[prefix + 'required_source_start'])
            end = pd.Timestamp(row[prefix + 'required_source_end'])
            matching = [p for p in partitions if p['symbol'] == row['symbol']
                        and pd.Timestamp(p['start'], tz='UTC') <= start
                        and pd.Timestamp(p['end'], tz='UTC') >= end]
            coverage.append({
                'kind': kind, 'id': row.get('id', row.get('clock_local')), 'symbol': row['symbol'], 'side': side,
                'status': row[prefix + 'status'], 'recorded_coverage': row[prefix + 'source_coverage'],
                'required_start': start.isoformat(), 'required_end': end.isoformat(),
                'matching_saved_verified_partition_count': len(matching),
                'matching_partition_manifest_paths': [p['manifest_path'] for p in matching],
                'partition_metadata_manifest_bound': bool(matching) and all(
                    p['manifest_path'].replace('\\', '/').lower() in bound_paths for p in matching),
            })

symbols = sorted(forecasts.symbol.unique())
per_symbol = []
for symbol in symbols:
    f = forecasts[forecasts.symbol.eq(symbol)]
    x = prices[prices.symbol.eq(symbol)]
    d = directional[directional.symbol.eq(symbol)]
    per_symbol.append({
        'symbol': symbol, 'forecasts': len(f),
        'evaluated': int(f.actuals_status.eq('EVALUATED').sum()),
        'mature_missing': int(f.actuals_status.eq('MATURE_AWAITING_DATA').sum()),
        'pending_maturity': int(f.actuals_status.eq('PENDING_MATURITY').sum()),
        'raw_direction_correct': int(d.direction_correct.eq(True).sum()),
        'raw_direction_evaluable': len(d),
        'prices_compared': int(x.comparison_status.eq('COMPARED').sum()),
        'prices_missing': int(x.comparison_status.eq('MATURE_AWAITING_DATA').sum()),
        'missing_price_clocks': x.loc[x.comparison_status.eq('MATURE_AWAITING_DATA'), 'clock_local'].tolist(),
    })

checks = {
    'complete_receipt_binds_manifest': receipt['status'] == 'COMPLETE' and receipt['manifest_sha256'] == sha(RUN / 'manifest.json'),
    'saved_input_output_bindings': all(x['verified'] for x in bindings),
    'frozen_forecasts_preserved': frozen_forecasts['equal'],
    'frozen_trade_plan_columns_preserved': frozen_plan['equal'],
    'all_original_hourly_low_mid_high_estimates_preserved': all(x['preserved'] for x in point_checks),
    'forecast_report_counts_match': report['forecasts'] == {'total': len(forecasts), 'evaluated': len(evaluated),
        'mature_awaiting_data': len(missing), 'pending_maturity': len(pending)},
    'price_report_counts_match': report['prices'] == {str(k): int(v) for k, v in prices.comparison_status.value_counts().items()},
    'every_mature_missing_endpoint_has_complete_saved_coverage': all(x['recorded_coverage'] == 'VERIFIED_COMPLETE' for x in coverage),
    'every_mature_missing_required_interval_in_saved_manifest_bound_partition': all(x['partition_metadata_manifest_bound'] for x in coverage),
    'raw_return_recomputed': bool(np.allclose(evaluated.actual_return, evaluated.actual_end_price / evaluated.actual_start_price - 1, rtol=0, atol=1e-12)),
    'raw_direction_correct_recomputed': bool(np.array_equal(raw_correct.to_numpy(), directional.direction_correct.astype(bool).to_numpy())),
    'raw_price_direction_target_recomputed': bool(np.array_equal(evaluated.actual_return.gt(0).astype(float), evaluated.observed_raw_price_direction)),
    'missing_and_pending_excluded_from_direction_accuracy': bool(forecasts.loc[~forecasts.actuals_status.eq('EVALUATED'), 'direction_correct'].isna().all()),
    'all_evaluated_endpoints_within_five_minutes': bool((evaluated.actual_start_gap_seconds.abs() <= 300).all() and (evaluated.actual_end_gap_seconds.abs() <= 300).all()),
    'all_outside_tolerance_candidates_exceed_five_minutes': all(
        (forecasts.loc[forecasts[f'actual_{side}_status'].eq('OUTSIDE_TOLERANCE'), f'actual_{side}_gap_seconds'].abs() > 300).all()
        for side in ('start', 'end')) and bool((missing_prices.loc[missing_prices.actual_status.eq('OUTSIDE_TOLERANCE'), 'actual_gap_seconds'].abs() > 300).all()),
    'missing_actual_values_not_filled': bool(missing.actual_return.isna().all() and missing_prices.actual_price.isna().all()),
    'no_orders': receipt['orders_placed'] == report['orders_placed'] == 0 and not receipt['broker_orders_enabled'] and not report['broker_orders_enabled'],
}

detail_columns = ['id', 'symbol', 'route', 'target_role', 'direction', 'target_window_start', 'target_window_end']
detail_columns += [c for c in forecasts if c.startswith('actual_start_') or c.startswith('actual_end_')]
result = {
    'reviewed_at': datetime.now(timezone.utc).isoformat(), 'status': 'SAVED_ACTUALS_COVERAGE_VERIFIED' if all(checks.values()) else 'CHECK_FAILED',
    'run_path': str(RUN), 'checks': checks, 'issues': [k for k,v in checks.items() if not v],
    'report_summary': {k:v for k,v in report.items() if k != 'price_inventory'},
    'per_symbol': per_symbol,
    'raw_direction': {'correct': int(raw_correct.sum()), 'evaluable': len(directional),
        'accuracy': float(raw_correct.mean()), 'evaluated_neutral_excluded': len(evaluated) - len(directional),
        'pending_or_missing_excluded': len(forecasts)-len(evaluated),
        'saved_direction_policy_versions': sorted(forecasts.direction_policy_version.unique()),
        'interpretation': 'Raw observed price direction under the frozen publication; includes evaluated opening-gap research, not broker fills or realized P/L.'},
    'evaluated_by_target_role': {str(k): int(v) for k,v in evaluated.target_role.value_counts().items()},
    'mature_missing_by_target_role': {str(k): int(v) for k,v in missing.target_role.value_counts().items()},
    'pending_route_counts': {str(k): int(v) for k,v in pending.route.value_counts().items()},
    'mature_missing_start_status': dict(Counter(missing.actual_start_status)),
    'mature_missing_end_status': dict(Counter(missing.actual_end_status)),
    'missing_clock_price_status': dict(Counter(missing_prices.actual_status)),
    'mature_missing_forecasts': records(missing[detail_columns]),
    'missing_clock_prices': records(missing_prices),
    'saved_coverage_interval_checks': coverage,
    'frozen_forecast_comparison': frozen_forecasts, 'frozen_trade_plan_comparison': frozen_plan,
    'frozen_hourly_price_point_count': len(point_checks), 'bindings': bindings,
    'native_archive_partitions_recorded_verified': report['price_inventory']['native_archive_partitions_verified'],
    'source_policy': report['price_inventory']['source_policy'],
    'limitations': [
        'Uses saved actuals report, parquets and manifest plus their referenced frozen publication artifacts; no native price archives reloaded.',
        'Source coverage means verified request intervals. It does not assert an observation at every boundary or prove no trading occurred.',
        'Root full audit separately verifies source payloads and reproduces actual endpoint selection.',
        'Frozen directions and original estimates are preserved; no new estimates, synthetic actuals, retraining or model selection.',
    ],
    'production_writes': 0, 'broker_calls': 0, 'provider_calls': 0, 'native_price_reload': False,
}
(OUT/'actuals-coverage-review.json').write_text(json.dumps(result, indent=2, default=str)+'\n', encoding='utf-8')
print(json.dumps({'status':result['status'], 'issues':result['issues'], 'per_symbol':per_symbol, 'raw_direction':result['raw_direction']}, indent=2))
