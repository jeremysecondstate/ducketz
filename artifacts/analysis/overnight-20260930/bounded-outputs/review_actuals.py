"""Bounded saved-publication review; no price archive, broker, provider or model calls."""
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
import hashlib
import json

import numpy as np
import pandas as pd

from native_gate import resolve, frame as read_frame
ROOT = Path('C:/DATASTORE')
GAMEPLAN,PLAN,RUN,NATIVE_REPORT,NATIVE_POINTERS=resolve()
OUT = Path(__file__).resolve().parent


def read_json(path):
    assert path.stat().st_size <= 64*1024*1024, 'BOUNDED_JSON_SIZE_EXCEEDED'
    return json.loads(path.read_text(encoding='utf-8-sig'))


def sha(path):
    assert path.stat().st_size <= 64*1024*1024, 'BOUNDED_HASH_SIZE_EXCEEDED'
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
forecasts = read_frame(RUN / 'forecast-results.parquet')
prices = read_frame(RUN / 'price-results.parquet')
source_forecasts = read_frame(Path(report['source_gameplan_path']) / 'forecasts.parquet')
source_plan = read_frame(Path(report['source_trade_plan_path']) / 'trade-plan.parquet')
source_path = read_json(Path(report['source_trade_plan_path']) / 'planning-price-path.json')
source_gameplan_receipt = read_json(Path(report['source_gameplan_path']) / 'receipt.json')
source_trade_receipt = read_json(Path(report['source_trade_plan_path']) / 'receipt.json')
prior_open = pd.Timestamp('2026-09-29 04:00',tz='America/Los_Angeles')
prior_close = pd.Timestamp('2026-09-29 17:00',tz='America/Los_Angeles')

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
compared_prices = prices[prices.comparison_status.eq('COMPARED')]
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
    'price_dollar_difference_recomputed': bool(np.allclose(compared_prices.price_error, compared_prices.actual_price-compared_prices.planned_price_mid, rtol=0, atol=1e-12)),
    'price_percentage_difference_recomputed': bool(np.allclose(compared_prices.price_error_fraction, (compared_prices.actual_price-compared_prices.planned_price_mid)/compared_prices.planned_price_mid, rtol=0, atol=1e-12)),
    'price_in_range_flags_recomputed': bool(np.array_equal(compared_prices.in_planned_range.astype(bool), compared_prices.actual_price.ge(compared_prices.planned_price_low)&compared_prices.actual_price.le(compared_prices.planned_price_high))),
    'compared_price_timestamps_within_five_minutes': bool((compared_prices.actual_gap_seconds.abs()<=300).all()),
    'no_orders': receipt['orders_placed'] == report['orders_placed'] == 0 and not receipt['broker_orders_enabled'] and not report['broker_orders_enabled'],
    'prior_session_and_successor_dates':receipt['action_date']==report['action_date']==source_gameplan_receipt['action_date']==source_trade_receipt['action_date']=='2026-09-29' and report['successor_action_date']=='2026-09-30',
    'outcomes_stop_at_completed_prior_close':pd.Timestamp(report['outcomes_through'])==prior_close,
    'frozen_sources_saved_before_prior_opening':pd.Timestamp(source_gameplan_receipt['published_at'])<prior_open and pd.Timestamp(source_trade_receipt['completed_at'])<prior_open and pd.Timestamp(source_path['observed_at'])<prior_open,
    'source_trade_plan_matches_exact_prior_gameplan':source_trade_receipt['source_gameplan_run']==report['source_gameplan_run'] and source_trade_receipt['source_receipt_sha256']==sha(Path(report['source_gameplan_path'])/'receipt.json'),
    'source_receipts_bind_prior_manifests':source_gameplan_receipt['manifest_checksum_sha256']==sha(Path(report['source_gameplan_path'])/'manifest.json') and source_trade_receipt['manifest_sha256']==sha(Path(report['source_trade_plan_path'])/'manifest.json'),
    'prior_forecast_and_clock_counts':len(forecasts)==264 and len(prices)==154 and forecasts.groupby('symbol').size().to_dict()=={s:24 for s in symbols} and prices.groupby('symbol').size().to_dict()=={s:14 for s in symbols},
    'all_actual_prices_positive_and_finite':bool(np.isfinite(evaluated.actual_start_price).all() and np.isfinite(evaluated.actual_end_price).all() and evaluated.actual_start_price.gt(0).all() and evaluated.actual_end_price.gt(0).all() and compared_prices.actual_price.gt(0).all() and np.isfinite(compared_prices.actual_price).all()),
    'all_neutral_forecasts_excluded_from_direction_accuracy':bool(forecasts.loc[~forecasts.direction.isin(['BULLISH','BEARISH']),'direction_correct'].isna().all()),
    'pending_returns_and_model_scores_not_invented':bool(pending.actual_return.isna().all() and pending.model_brier_score.isna().all() and pending.model_observed_target.isna().all()),
    'cost_adjusted_outcomes_kept_separate':bool(np.allclose(evaluated.cost_adjusted_return,evaluated.actual_return-evaluated.assumed_round_trip_cost,rtol=0,atol=1e-12) and np.array_equal(evaluated.observed_cost_adjusted_positive,evaluated.actual_return.gt(evaluated.assumed_round_trip_cost).astype(int))),
    'raw_target_scores_recomputed_without_cost_substitution':bool(evaluated.probability_target_contract.eq('raw-price-direction-v1').all() and np.array_equal(evaluated.model_observed_target,evaluated.actual_return.gt(0).astype(int)) and np.allclose(evaluated.model_brier_score,(evaluated.calibrated_probability-evaluated.model_observed_target)**2,rtol=0,atol=1e-12)),
}

boundary_checks=[]
for row in forecasts.to_dict('records'):
    opening_gap=row['target_role']=='OPENING_GAP_RESEARCH'
    for side,is_close in [('start',opening_gap),('end',not opening_gap)]:
        prefix='actual_'+side
        boundary=pd.Timestamp(row['target_window_'+side])
        observed=row[prefix+'_observed_at']
        actual=row[prefix+'_price']
        expected_start=boundary-pd.Timedelta(minutes=6) if is_close else boundary
        expected_end=boundary if is_close else boundary+pd.Timedelta(minutes=6)
        okay=pd.Timestamp(row[prefix+'_required_source_start'])==expected_start and pd.Timestamp(row[prefix+'_required_source_end'])==expected_end
        if row[prefix+'_status']=='OBSERVED':
            stamp=pd.Timestamp(observed)
            delta=(boundary-stamp if is_close else stamp-boundary).total_seconds()
            okay=okay and 0<=delta<=300 and abs(delta-row[prefix+'_gap_seconds'])<1e-9
            okay=okay and stamp==pd.Timestamp(row[prefix+'_candidate_observed_at']) and actual==row[prefix+'_candidate_price'] and stamp<=prior_close
        else:
            okay=okay and pd.isna(actual) and pd.isna(observed)
        boundary_checks.append({'id':row['id'],'side':side,'verified':bool(okay)})
checks['saved_actual_endpoint_clocks_and_unfilled_missing_values']=all(item['verified'] for item in boundary_checks)
price_clock_checks=[]
for row in prices.to_dict('records'):
    boundary=pd.Timestamp(row['timestamp']); is_close=row['clock_local']=='17:00'
    if row['actual_status']=='OBSERVED':
        stamp=pd.Timestamp(row['actual_observed_at'])
        delta=(boundary-stamp if is_close else stamp-boundary).total_seconds()
        okay=0<=delta<=300 and abs(delta-row['actual_gap_seconds'])<1e-9 and stamp<=prior_close and row['actual_price']==row['actual_candidate_price'] and stamp==pd.Timestamp(row['actual_candidate_observed_at'])
    else:
        okay=pd.isna(row['actual_price']) and pd.isna(row['actual_observed_at'])
    price_clock_checks.append({'symbol':row['symbol'],'clock':row['clock_local'],'verified':bool(okay)})
checks['saved_hourly_actual_clocks_and_unfilled_missing_values']=all(item['verified'] for item in price_clock_checks)
dated_readable=ROOT/'ml/gameplan-actuals-review-by-date/2026-09-29/Gameplan-results.md'
plan_readable=(PLAN/'Gameplan.md').read_text(encoding='utf-8')
checks['dated_actuals_readable_matches_immutable_result']=dated_readable.is_file() and sha(dated_readable)==sha(RUN/'Gameplan-results.md')
checks['successor_readable_links_prior_dated_actuals']=dated_readable.as_posix() in plan_readable

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
    'price_range_result': {'in_range': int(compared_prices.in_planned_range.eq(True).sum()), 'compared': len(compared_prices), 'missing_excluded': len(missing_prices)},
    'mature_missing_forecasts': records(missing[detail_columns]),
    'missing_clock_prices': records(missing_prices),
    'saved_coverage_interval_checks': coverage,
    'frozen_forecast_comparison': frozen_forecasts, 'frozen_trade_plan_comparison': frozen_plan,
    'frozen_hourly_price_point_count': len(point_checks), 'bindings': bindings,
    'native_archive_partitions_recorded_verified': report['price_inventory']['native_archive_partitions_verified'],
    'source_policy': report['price_inventory']['source_policy'],
    'source_gameplan_published_at':source_gameplan_receipt['published_at'],
    'source_trade_plan_completed_at':source_trade_receipt['completed_at'],
    'endpoint_clock_checks':boundary_checks,'hourly_price_clock_checks':price_clock_checks,
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

