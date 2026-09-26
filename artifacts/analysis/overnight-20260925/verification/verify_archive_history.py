"""Read-only archive audit extension for the September 25 full-run verifier.

Called only after native COMPLETE. Rebuilds feature/target cohorts and seconds
consistency once from existing local archives; never fits or acquires data.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path


def verify_archive_history(*, root, context, read, require, input_matches,
                           local_prices, completed_history):
    import pandas as pd
    import numpy as np
    import joblib
    from ml.artifacts import file_checksum
    from ml.gameplan_archive_features import load_archive_feature_sources, ARCHIVE_FEATURE_CONTRACT
    from ml.gameplan_archive_integration import (
        combine_archive_sources, exclude_quality_intervals, validate_archive_feature_clocks,
    )
    from ml.gameplan_archive_seconds import verify_second_minute_overlap
    from ml.gameplan_source_selection import select_prior_session_sources
    from ml.independent_stock_targets import build_stock_training_groups, build_stock_current_groups
    from ml.nightly_gameplan import _feature_columns, _current_overnight_sources, _model_frame

    pub, symbols = context['publication'], context['symbols']
    run, manifest = pub.run_directory, pub.manifest
    cfg = manifest['configuration']
    require(cfg.get('archive_history') is True
            and cfg.get('source_selection_contract') == ARCHIVE_FEATURE_CONTRACT,
            'Fresh full publication lacks its archive-history feature contract')
    require('archive-history.json' in manifest['output_files'], 'Archive report is not manifest bound')
    saved = read(run / 'archive-history.json')
    require(saved.get('orders_placed') == 0 and saved.get('synthetic_feature_bars') == 0
            and saved.get('label_policy_changed') is False
            and saved.get('minimum_core_daily_observations') == 21,
            'Archive report changed warmup, label policy, synthetic rows, or order authority')
    require(set(saved.get('by_symbol', {})) == set(symbols), 'Archive report universe differs')
    require(all(saved.get(k) == v for k, v in cfg['source_selection'].items()),
            'Archive report disagrees with manifest source selection')
    as_of = pd.Timestamp(saved['as_of'])

    # Native readers verify source payloads against their acquisition manifests.
    # The second pass below binds those same verified paths to this publication.
    archive = load_archive_feature_sources(root, symbols=symbols, available_at=as_of)
    loop_b = (root / cfg['source_loop_b_run']).resolve()
    loop_manifest = read(loop_b / 'manifest.json')
    samples = pd.read_parquet(loop_b / 'samples.parquet')
    optional = _feature_columns(loop_manifest, samples)
    operational = select_prior_session_sources(samples, symbols=symbols,
                                               available_at=as_of, feature_columns=optional)
    combined = combine_archive_sources(archive, operational, feature_columns=optional)
    require(all(saved.get(k) == json.loads(json.dumps(v, default=str))
                for k, v in combined.report.items()),
            'Recomputed archive feature dates, warmup/exclusions, or optional inputs differ')
    validate_archive_feature_clocks(combined.sources)
    require(not combined.sources.duplicated(['symbol', 'action_date']).any(),
            'Archive source symbol/action identity is duplicated')
    seconds, seconds_files = verify_second_minute_overlap(root, symbols=symbols, available_at=as_of)
    require(seconds == saved.get('second_minute_consistency'),
            'Exact local second/minute consistency report differs')
    require(seconds['status'] == 'VERIFIED' and seconds['synthetic_rows'] == 0
            and seconds['added_training_rows'] == 0, 'Seconds created training rows or assumptions')
    for symbol, evidence in seconds['by_symbol'].items():
        require(evidence['overlap_minutes'] == evidence['exact_ohlcv_overlap_minutes']
                and all(n == evidence['overlap_minutes'] for n in evidence['exact_field_matches'].values())
                and evidence['synthetic_rows'] == evidence['added_training_rows'] == 0,
                'Seconds/minute exact overlap or no-duplication contract differs: ' + symbol)
    files = tuple(dict.fromkeys([*combined.source_files, *seconds_files,
                               loop_b / 'samples.parquet', loop_b / 'manifest.json']))
    for path in files:
        input_matches(manifest, path)

    prices, minute_files, _ = local_prices(symbols, 'xnas-itch-archive-v1')
    for path in minute_files:
        if Path(path).resolve() not in {Path(item).resolve() for item in files}:
            input_matches(manifest, path)
    files = tuple(dict.fromkeys([*files, *minute_files]))
    # Reuse the full verifier's already-loaded minute price inventory. Seconds
    # contribute only consistency evidence and never feed the target builder.
    groups = build_stock_training_groups(combined.sources,
        feature_columns=manifest['feature_columns'], minute_bars=prices, available_at=as_of,
        price_source_contract='xnas-itch-archive-v1',
        probability_target=cfg['probability_target_contract'])
    groups = exclude_quality_intervals(groups, combined.report.get('excluded_intervals', ()),
        split_boundaries=(*combined.report.get('split_boundaries', ()),
                          *combined.report.get('target_discontinuity_boundaries', ())))
    cohort_summary = {}
    provider_quality_intervals = {}
    for interval in saved['excluded_intervals']:
        if interval['reason'] == 'PROVIDER_QUALITY_DEGRADED':
            key = (interval['symbol'], interval['start'], interval['end'])
            provider_quality_intervals.setdefault(key, set()).add(interval['schema'])
    for group, regenerated in groups.items():
        name = f'training-cohort-{group}.parquet'
        require(name in manifest['output_files'], 'Cohort is not manifest bound: ' + group)
        actual = pd.read_parquet(run / name)
        keys = ['symbol', 'action_date', 'route']
        require(not actual.duplicated(keys).any(), 'Duplicate archive training target identity: ' + group)
        validate_archive_feature_clocks(actual)
        # Exact equality verifies every eligible sample, causal optional feature,
        # source clock, target endpoint, return, and excluded-quality decision.
        pd.testing.assert_frame_equal(actual.reset_index(drop=True), regenerated.reset_index(drop=True),
                                      check_dtype=False, check_exact=True)
        counts = {'rows': len(actual), 'first_action_date': str(actual.action_date.min()),
            'last_action_date': str(actual.action_date.max()),
            'by_symbol': {str(symbol): {'rows': len(rows),
                'first_action_date': str(rows.action_date.min()),
                'last_action_date': str(rows.action_date.max())}
                for symbol, rows in actual.groupby('symbol')},
            'quality_excluded_rows': regenerated.attrs.get('archive_quality_excluded_rows', 0)}
        require(counts == saved['training_cohorts'][group], 'Archive cohort range/count evidence differs: ' + group)
        require(actual.target_price_source_contract.eq('xnas-itch-archive-v1').all()
                and actual.target_price_dataset.eq('XNAS.ITCH').all()
                and actual.target_boundary_aligned.all()
                and actual[['target_start_gap_seconds', 'target_end_gap_seconds']].le(300).all().all(),
                'Archive targets changed minute source or five-minute boundary policy: ' + group)
        starts = pd.to_datetime(actual.target_window_start, utc=True)
        ends = pd.to_datetime(actual.target_window_end, utc=True)
        for interval in saved['excluded_intervals']:
            crossing = (actual.symbol.eq(interval['symbol'])
                        & starts.lt(pd.Timestamp(interval['end']))
                        & ends.ge(pd.Timestamp(interval['start'])))
            require(not crossing.any(), 'A saved target crosses an excluded source-quality interval: ' + group)
        cohort_summary[group] = {**counts, 'all_rows_exactly_reproduced': True,
            'targets_crossing_excluded_source_quality_intervals': 0,
            'boundary_exclusions': regenerated.attrs['target_boundary_quality']}
    current_sources, current_action_date = _current_overnight_sources(combined.sources, symbols=symbols, as_of=as_of)
    require(str(current_action_date) == context['action_date'], 'Rebuilt current archive source action date differs')
    current_groups = build_stock_current_groups(current_sources,
        feature_columns=manifest['feature_columns'], price_source_contract='xnas-itch-archive-v1',
        probability_target=cfg['probability_target_contract'])
    model_reports = read(run / 'model-reports.json')
    current_forecast_inference = {}
    for group, current in current_groups.items():
        report = model_reports[group]
        model_name = report['model_file']['path']
        require(model_name in manifest['output_files'] and (run / model_name).resolve().is_relative_to(run.resolve()),
                'Current forecast model is not manifest-bound: ' + group)
        payload = joblib.load(run / model_name)
        raw = payload['estimator'].predict_proba(_model_frame(current, payload['feature_columns'], payload['categorical_columns']))[:, 1]
        probability = payload['calibrator'].predict(raw)
        rebuilt = current[['symbol', 'route']].assign(raw_probability=raw, calibrated_probability=probability).set_index(['symbol', 'route']).sort_index()
        actual = context['forecasts'].loc[context['forecasts'].model_group.eq(group)].set_index(['symbol', 'route']).sort_index()
        require(rebuilt.index.equals(actual.index), 'Current forecast exact symbol/route grid differs: ' + group)
        errors = {}
        for column in ('raw_probability', 'calibrated_probability'):
            require(np.isfinite(rebuilt[column]).all() and np.isfinite(actual[column]).all()
                    and np.allclose(rebuilt[column], actual[column], rtol=0, atol=1e-12),
                    'Frozen forecast probabilities do not reproduce from verified current archive features: ' + group + '/' + column)
            errors[column] = float(np.max(np.abs(rebuilt[column].to_numpy() - actual[column].to_numpy())))
        current_forecast_inference[group] = {'rows': len(actual), 'model_artifact': model_name,
            'current_features_rebuilt_from_manifest_bound_archive': True, 'maximum_absolute_error': errors}
    features_by_symbol = {}
    for symbol, frame in combined.sources.groupby('symbol'):
        features_by_symbol[symbol] = {'rows': len(frame),
            'first_action_date': str(frame.action_date.min()), 'last_action_date': str(frame.action_date.max()),
            'first_source_session': str(frame.source_session.min()),
            'last_source_session': str(frame.source_session.max())}
    extension = _verify_extension(root, Path(completed_history['run']), read=read,
                                  require=require, symbols=symbols, file_checksum=file_checksum)
    return {'report': str(run / 'archive-history.json'), 'source_selection_contract': ARCHIVE_FEATURE_CONTRACT,
        'available_at': as_of.isoformat(), 'source_files_bound_and_verified': len(files),
        'source_file_bytes': sum(Path(path).stat().st_size for path in files),
        'feature_dates_by_symbol': features_by_symbol, 'current_forecast_inference': current_forecast_inference,
        'training_cohorts': cohort_summary, 'feature_history_extension': extension,
        'provider_quality_intervals': [{'symbol': symbol, 'start': start, 'end': end,
            'schemas': sorted(schemas)} for (symbol, start, end), schemas
            in sorted(provider_quality_intervals.items())],
        'exclusion_counts': {'quality_resets': len(saved['quality_resets']),
            'excluded_intervals': len(saved['excluded_intervals']),
            'excluded_undefined_observations': saved['excluded_undefined_observations'],
            'split_boundaries': len(saved['split_boundaries']),
            'target_discontinuity_boundaries': len(saved['target_discontinuity_boundaries']),
            'optional_rows_excluded_future': saved['operational_rows_excluded_as_future'],
            'optional_rows_excluded_unknown_availability': saved['operational_rows_excluded_unknown_availability']},
        'second_minute_consistency': {'status': seconds['status'],
            'native_archive_partitions_verified': seconds['native_archive_partitions_verified'],
            'by_symbol': seconds['by_symbol'], 'synthetic_rows': 0, 'added_training_rows': 0,
            'raw_record_replay': seconds.get('raw_record_replay'), 'verification': seconds.get('verification')},
        'all_eligible_local_archive_cohorts_reproduced': True, 'orders_placed': 0}


def _verify_extension(root, run, *, read, require, symbols, file_checksum):
    from datafetching.databento_cold_start import _verify_generic_partition, _validate_execution_request_identity
    from ml.stock_target_history import FEATURE_HISTORY_MAX_BILLABLE_BYTES, _checksum, _verified_target_cursor
    from datafetching.databento_cold_start import history_cursor_path, MARKET_US_EQUITIES

    receipt = read(run / 'receipt.json')
    binding = receipt.get('feature_history_extension', {})
    path = run / 'feature-history-extension.json'
    require(binding.get('required') is True and binding.get('status') == 'VERIFIED'
            and Path(binding.get('path', '')).resolve() == path.resolve()
            and binding.get('sha256') == file_checksum(path), 'History receipt does not bind verified extension')
    evidence = read(path)
    require(evidence.get('status') == 'VERIFIED' and evidence.get('current_cursors_preserved') is True
            and evidence.get('orders_placed') == 0 and evidence.get('estimated_cost_usd') == 0,
            'Prefix extension preservation, zero cost, or completion evidence differs')
    manifest_path = run / 'feature-history-manifest.json'
    require(evidence['manifest_sha256'] == file_checksum(manifest_path), 'Extension manifest hash differs')
    manifest = read(manifest_path)
    body = {k: v for k, v in manifest.items() if k not in {'manifest_id', 'semantic_checksum_sha256'}}
    digest = _checksum(body)
    require(manifest['semantic_checksum_sha256'] == digest and manifest['manifest_id'] == digest[:24],
            'Extension semantic identity differs')
    require(set(manifest['symbols']) == set(symbols)
            and manifest['maximum_cost_usd'] == 0
            and manifest['maximum_billable_bytes'] == FEATURE_HISTORY_MAX_BILLABLE_BYTES
            and manifest['source_contract'] == 'xnas-itch-archive-v1'
            and manifest['cursor_policy'] == 'preserve_exact_existing_cursor_no_prefix_cursor_write',
            'Extension universe, source, capacity limit or cursor contract differs')
    requests = manifest['requests']
    require(evidence['requests'] == len(requests), 'Extension request count differs')
    snapshots = read(run / 'feature-history-original-cursors.json')
    require(set(snapshots) == set(symbols), 'Original minute-cursor snapshot universe differs')
    for symbol, saved in snapshots.items():
        original = json.loads(saved['content'])
        expected_cursor_path = history_cursor_path(root, market=MARKET_US_EQUITIES, dataset='XNAS.ITCH',
                                                   schema='ohlcv-1m', symbol=symbol)
        require(Path(saved['path']).resolve() == expected_cursor_path.resolve(),
                'Original cursor snapshot path differs from the configured symbol: ' + symbol)
        require(hashlib.sha256(saved['content'].encode('utf-8')).hexdigest() == saved['sha256'],
                'Saved original cursor bytes differ: ' + symbol)
        current = _verified_target_cursor(root, symbol)
        require(current is not None and original == manifest['symbols'][symbol]['cursor_before']
                and current['completed_through'] >= original['completed_through'],
                'Prefix original/current cursor regressed or provenance differs: ' + symbol)
    completed = {item['request_id']: item for item in evidence['completed_requests']}
    require(set(completed) == {r['request_id'] for r in requests}, 'Extension incomplete or duplicated request set')
    details = []
    for request in requests:
        symbol = request['symbol_scope'][0]
        prior = manifest['symbols'][symbol]
        require(request['dataset'] == 'XNAS.ITCH' and request['schema'] == 'ohlcv-1m'
                and request['symbol_scope'] == [symbol] and symbol in symbols
                and request['start'] == prior['feature_start'] and request['end'] == prior['minute_start']
                and request['start'] < request['end'] and prior['status'] == 'PREFIX_REQUIRED',
                'Extension is not the exact observed-feature missing minute prefix')
        _validate_execution_request_identity(root, request)
        directory = Path(request['storage_path']).resolve()
        _verify_generic_partition(directory, request)
        record = completed[request['request_id']]
        require(Path(record['manifest_path']).resolve() == directory / 'manifest.json'
                and record['manifest_sha256'] == file_checksum(directory / 'manifest.json'),
                'Extension completed native source binding differs')
        details.append({'symbol': symbol, 'start': request['start'], 'end': request['end'],
                        'request_id': request['request_id'], 'native_partition': str(directory)})
    preflight_summary = {'requests': 0, 'new_acquisition_required': False}
    if requests:
        preflight_path = run / 'feature-history-preflight.json'
        require(evidence['preflight_sha256'] == file_checksum(preflight_path), 'Extension preflight hash differs')
        preflight = read(preflight_path)
        require(evidence['started_at'] <= preflight['generated_at'] <= evidence['completed_at'],
                'Prefix metadata preflight was not generated within this extension attempt')
        estimates = {row['request_id']: row for row in preflight['estimates']}
        require(set(estimates) == set(completed), 'Extension exact metadata request scope differs')
        bounds = preflight['dataset_range']['ohlcv-1m']
        size = 0
        for request in requests:
            row = estimates[request['request_id']]
            require(row['estimated_cost_usd'] == 0 and bounds['start'] <= request['start'] < request['end'] <= bounds['end'],
                    'Extension exact provider-range or zero-dollar gate differs')
            for key in ('estimated_download_size_bytes', 'record_count'):
                value = row[key]
                require(not isinstance(value, bool) and math.isfinite(value) and value == int(value) and value >= 0,
                        'Extension metadata count/size is not finite and nonnegative')
            require(row['record_count'] > 0, 'Extension required prefix has zero estimated records')
            size += row['estimated_download_size_bytes']
        require(preflight['maximum_cost_usd'] == 0 and preflight['capacity_pass'] is True
                and preflight['maximum_billable_bytes'] == FEATURE_HISTORY_MAX_BILLABLE_BYTES
                and size == preflight['total_estimated_download_size_bytes'] <= FEATURE_HISTORY_MAX_BILLABLE_BYTES
                and preflight['available_free_bytes'] >= preflight['required_free_bytes'],
                'Extension zero-dollar or bounded-capacity preflight differs')
        preflight_summary = {'requests': len(requests), 'new_acquisition_required': True,
            'estimated_cost_usd': 0, 'estimated_download_size_bytes': size,
            'provider_range': bounds, 'capacity_pass': True,
            'preflight_generated_at': preflight['generated_at'], 'estimates': preflight['estimates']}
    return {'run': str(run), 'status': evidence['status'], 'prefixes': details,
            'cursor_snapshots_verified': len(snapshots), 'current_cursors_not_regressed': True,
            'native_extension_reported_exact_cursor_preservation': True,
            'cursor_preservation_evidence': 'Saved cursor content hashes verified and semantic values bound to the native extension manifest. Current native cursors are source-verified and nonregressing; ordinary daily acquisition may advance them after the prefix stage.',
            'original_cursor_file_outer_hash_binding': 'NOT_RECORDED_BY_NATIVE_RECEIPT',
            'raw_record_replay_performed': False, 'preflight': preflight_summary}
