"""Read-only metadata and saved-sample audit. Never requests timeseries data."""
import hashlib
import importlib.metadata
import json
import math
import os
import shutil
import sys
from pathlib import Path

import databento as db
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path.cwd()))
from datafetching.cme_runtime import load_repository_environment
from datafetching.databento_xnas_replay import _external_failure

OUT = Path(__file__).parent
SYMBOLS = ['AAPL', 'AMZN', 'COST', 'GOOG', 'MU', 'NVDA', 'SNDK']
SAMPLES = Path('C:/DATASTORE/ml/runs/20260912T045017.509319Z/samples.parquet')
START = '2023-12-29T00:00:00Z'  # Prior session supplies Jan 2 opening-gap reference.
END = '2025-01-13T00:00:00Z'  # Existing selected source begins Jan 13.
report = {
    'started_at': pd.Timestamp.now(tz='UTC').isoformat(),
    'status': 'RUNNING', 'metadata_only': True, 'timeseries_requests': 0,
    'production_writes': False, 'training_runs': 0,
    'sdk_version': importlib.metadata.version('databento'),
    'preflights': [],
}

def save():
    (OUT / 'history-2024-feasibility.json').write_text(
        json.dumps(report, indent=2, default=str, allow_nan=False) + '\n', encoding='utf-8')

try:
    samples = pd.read_parquet(SAMPLES)
    features = [c for c in samples if c.startswith(('mr__', 'bp__', 'bar__', 'life__', 'weekly__'))]
    stamps = pd.to_datetime(samples.information_available_at, utc=True, errors='coerce')
    local = stamps.dt.tz_convert('America/Los_Angeles')
    hist = samples.loc[local.dt.year.eq(2024)].copy()
    hist['source_date_pt'] = local.loc[hist.index].dt.date
    hist['core_feature_count'] = hist[features].apply(pd.to_numeric, errors='coerce').notna().sum(axis=1)
    feature_results = []
    for sym in SYMBOLS:
        part = hist.loc[hist.symbol.eq(sym)]
        hourly = part.loc[part.horizon.eq('1h')]
        vals = {c: int(hourly[c].notna().sum()) for c in ['mr__trend_atr', 'mr__technical_score', 'bp__compression_score', 'bp__breakout_readiness_score']}
        feature_results.append({
            'symbol': sym, 'rows_all_horizons': len(part),
            'hourly_rows': len(hourly),
            'hourly_source_dates': int(hourly.source_date_pt.nunique()),
            'first_hourly_information_at': str(hourly.information_available_at.min()) if len(hourly) else None,
            'last_hourly_information_at': str(hourly.information_available_at.max()) if len(hourly) else None,
            'hourly_rows_with_any_core_features': int(hourly.core_feature_count.gt(0).sum()),
            'hourly_rows_with_at_least_20_core_features': int(hourly.core_feature_count.ge(20).sum()),
            'selected_feature_non_null_rows': vals,
            'horizon_rows': {str(k): int(v) for k, v in part.horizon.value_counts().items()},
        })
    report['saved_feature_evidence'] = {
        'samples_path': str(SAMPLES),
        'samples_sha256': hashlib.sha256(SAMPLES.read_bytes()).hexdigest(),
        'row_time_basis': 'information_available_at converted to America/Los_Angeles; year 2024',
        'note': 'Potential historical feature inputs; does not assert admissible independent target labels or complete optional features.',
        'core_feature_columns_counted': features,
        'symbols': feature_results,
    }
    save()
    load_repository_environment()
    key = os.environ.get('DATABENTO_API_KEY', '').strip()
    if not key:
        raise ValueError('Existing Databento credential unavailable')
    client = db.Historical(key)
    schemas = list(client.metadata.list_schemas(dataset='XNAS.ITCH'))
    range_ = client.metadata.get_dataset_range(dataset='XNAS.ITCH')
    conditions = client.metadata.get_dataset_condition(dataset='XNAS.ITCH', start_date='2023-12-29', end_date='2025-01-13')
    mappings = client.symbology.resolve(dataset='XNAS.ITCH', symbols=SYMBOLS, stype_in='raw_symbol', stype_out='instrument_id', start_date='2023-12-29', end_date='2025-01-13')
    report['dataset_metadata'] = {'schemas': schemas, 'range': range_, 'conditions': conditions, 'symbology': mappings}
    save()
    available = range_.get('schema', {}).get('ohlcv-1m', range_)
    range_pass = pd.Timestamp(available.get('start', range_['start'])) <= pd.Timestamp(START) < pd.Timestamp(END) <= pd.Timestamp(available.get('end', range_['end']))
    if 'ohlcv-1m' not in schemas or not range_pass:
        raise ValueError('Exact requested one-minute schema/range not available')
    for symbol in SYMBOLS:
        request = {'dataset': 'XNAS.ITCH', 'schema': 'ohlcv-1m', 'symbols': [symbol], 'stype_in': 'raw_symbol', 'start': START, 'end': END}
        if symbol in mappings.get('not_found', []) and not mappings.get('result', {}).get(symbol):
            report['preflights'].append({'observed_at': pd.Timestamp.now(tz='UTC').isoformat(), 'request': request, 'status': 'NOT_AVAILABLE_SYMBOL_UNMAPPED', 'estimated_cost_usd': None, 'estimated_billable_bytes': 0, 'estimated_record_count': 0, 'zero_cost': None, 'range_pass': bool(range_pass), 'reason': 'Native symbology has no mapping for this symbol during the requested period; no data request is appropriate.'})
            save()
            continue
        cost = float(client.metadata.get_cost(**request))
        size = int(client.metadata.get_billable_size(**request))
        count = int(client.metadata.get_record_count(**request))
        report['preflights'].append({'observed_at': pd.Timestamp.now(tz='UTC').isoformat(), 'request': request, 'estimated_cost_usd': cost, 'estimated_billable_bytes': size, 'estimated_record_count': count, 'zero_cost': math.isfinite(cost) and cost == 0, 'range_pass': bool(range_pass)})
        save()
        print(json.dumps({'symbol': symbol, 'cost_usd': cost, 'billable_bytes': size, 'records': count}), flush=True)
    # Resolving dates around the existing first SNDK record documents the exception.
    report['sndk_early_2025_mapping'] = client.symbology.resolve(dataset='XNAS.ITCH', symbols=['SNDK'], stype_in='raw_symbol', stype_out='instrument_id', start_date='2025-01-01', end_date='2025-03-15')
    total = sum(x['estimated_billable_bytes'] for x in report['preflights'])
    free = shutil.disk_usage('C:/DATASTORE').free
    report['capacity'] = {'datastore_path': 'C:/DATASTORE', 'available_bytes': free, 'total_billable_bytes': total, 'required_free_bytes': 5 * 1024**3 + 2 * total, 'rule': '5 GiB reserve plus 2 times sum of estimated uncompressed billable bytes', 'capacity_pass': free > 5 * 1024**3 + 2 * total, 'note': 'Billable bytes are neither compressed download bytes nor final disk size.'}
    report['total_estimated_cost_usd'] = sum(x['estimated_cost_usd'] or 0 for x in report['preflights'])
    probe = OUT / 'source-probe' / 'validation-120'
    native_proof = []
    for sym in SYMBOLS:
        manifest = json.loads((probe / sym / 'manifest.json').read_text())
        preflight = json.loads((probe / sym / 'preflight.json').read_text())
        metadata = json.loads((probe / sym / 'native-metadata.json').read_text())
        payload_checks = []
        for payload in manifest['payloads']:
            path = probe / sym / payload['path']
            payload_checks.append(path.stat().st_size == payload['bytes'] and hashlib.sha256(path.read_bytes()).hexdigest() == payload['sha256'])
        native_proof.append({'symbol': sym, 'completed_at': manifest['completed_at'], 'dataset': manifest['dataset'], 'rows': manifest['rows'], 'quote_cost_usd': preflight['estimated_cost_usd'], 'native_dataset': metadata['dataset'], 'native_schema': metadata['schema'], 'payload_hashes_verified': all(payload_checks), 'request': manifest['request']})
    report['existing_basic_historical_access_evidence'] = native_proof
    report['status'] = 'METADATA_AND_SAVED_DATA_VERIFIED_NO_ACQUISITION'
except Exception as exc:
    report['status'] = 'INCOMPLETE_METADATA_AUDIT'
    report['error'] = _external_failure(exc)
finally:
    report['completed_at'] = pd.Timestamp.now(tz='UTC').isoformat()
    save()
    print(json.dumps({'status': report['status'], 'preflights': len(report['preflights']), 'error': report.get('error')}, indent=2))
