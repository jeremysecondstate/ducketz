"""Bounded offline advisory audit called only after native Loop A completion.

Reads two compact diagnostic parquets, one retained FMP quote parquet, six
compact projected CME captures, four nonsecret configuration values and the
prior audit JSON. Never calls a provider or scans a retained market archive.
"""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import sys


def audit_advisories(root, started_at, finished_at, native_lines):
    import pandas as pd
    import pyarrow.parquet as pq
    sys.path.insert(0, 'C:/dev/ducketz')
    from datafetching.fmp_energy_context import calculate_fmp_energy_context
    start, finish = pd.Timestamp(started_at), pd.Timestamp(finished_at)
    prior_path = Path('C:/dev/ducketz/artifacts/analysis/overnight-20260925/verification/provider-completion.json')
    prior = json.loads(prior_path.read_text(encoding='utf-8')).get('optional_advisory_review', {})
    issues, notes, diagnostics = [], [], {}
    scopes = {
        'cme': 'pools/cme/CME_CONTEXT/cross-asset-context/databento/diagnostics/CME_CONTEXT_cross-asset-context.parquet',
        'fmp': 'pools/macro/ENERGY_CONTEXT/energy-context/fmp/diagnostics/ENERGY_CONTEXT_energy-context.parquet',
    }

    def bounded_frame(path, maximum_rows):
        metadata = pq.read_metadata(path)
        if metadata.num_rows > maximum_rows:
            raise ValueError('Bounded advisory row limit exceeded: ' + str(path))
        return pd.read_parquet(path)

    fields = ('severity', 'advisory_type', 'input_policy', 'provider_rows_preserved')
    for name, relative in scopes.items():
        path = root/relative
        frame = bounded_frame(path, 10000)
        times = pd.to_datetime(frame['fetched_at'], utc=True)
        current = frame.loc[times.ge(start) & times.le(finish)]
        latest = frame.assign(_time=times).sort_values('_time').tail(2).drop(columns='_time')
        previous = prior.get('diagnostics', {}).get(name, {})
        previous_rows = previous.get('current_rows', []) or previous.get('latest_rows', [])
        previous_signatures = [{k: row.get(k) for k in fields} for row in previous_rows]
        relevant = current.to_dict('records') or latest.tail(1).to_dict('records')
        unknown = [row for row in relevant if {k: row.get(k) for k in fields} not in previous_signatures]
        retained_same_message = bool(relevant) and all(any(
            {k: row.get(k) for k in (*fields, 'advisory_message')} ==
            {k: saved.get(k) for k in (*fields, 'advisory_message')}
            for saved in previous_rows) for row in relevant)
        for row in relevant:
            if row.get('severity') != 'advisory' or row.get('input_policy') != 'persisted_rows_only' or row.get('provider_rows_preserved') is not True:
                issues.append(name + ' diagnostic changed optional severity or provider-preservation policy')
        if unknown:
            notes.append(name + ' has a diagnostic category/policy absent from prior verified audit; review exact saved message')
        elif not retained_same_message:
            notes.append(name + ' retains prior diagnostic category/policy, but exact message differs; no unchanged-source claim')
        diagnostics[name] = {
            'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'total_rows': len(frame), 'current_rows': current.to_dict('records'),
            'latest_rows': latest.to_dict('records'), 'matches_prior_diagnostic_category_policy': not unknown,
            'exact_message_matches_prior': retained_same_message,
            'evidence_age': 'CURRENT_NATIVE_STAGE' if len(current) else 'RETAINED_DIAGNOSTIC',
        }

    quote_path = root/'pools/macro/CLUSD/quote/fmp/normalized/CLUSD_quote.parquet'
    quote = bounded_frame(quote_path, 100000)
    receipt_times = pd.to_datetime(quote['fetched_at'], utc=True)
    current_quote = quote.loc[receipt_times.ge(start) & receipt_times.le(finish)]

    def validation(frame):
        if frame.empty:
            return {'status': 'NO_CURRENT_ROWS', 'rows': 0}
        try:
            result = calculate_fmp_energy_context(frame)
            return {'status': 'PASS', 'rows': len(result)}
        except Exception as exc:
            return {'status': 'REJECTED', 'error': type(exc).__name__ + ': ' + str(exc)}

    full_result, current_result = validation(quote), validation(current_quote)
    previous_full = prior.get('fmp_full_source_pure_validation')
    if full_result != previous_full:
        notes.append('FMP whole-history pure validation differs from previous audit; review recorded result')
    if current_result['status'] != 'PASS':
        notes.append('FMP current capture does not independently pass optional pure derivation')
    market_times = pd.to_datetime(quote['timestamp'], utc=True, format='mixed')
    skew = (market_times - receipt_times).dt.total_seconds()
    offenders = quote.loc[skew.gt(5), ['fetched_at', 'timestamp', 'symbol', 'provider_symbol']].copy()
    offenders['skew_seconds'] = skew.loc[skew.gt(5)]
    # Inspect only the four explicitly nonsecret configuration keys. No other
    # environment values are retained in the audit or emitted to output.
    env_path = Path('C:/dev/ducketz/.env')
    config_keys = ('DATABENTO_CME_CONTEXT_SYMBOLS', 'DATABENTO_CME_CONTEXT_STYPE_IN',
                   'DATABENTO_CME_CONTRACT_SYMBOLS', 'DATABENTO_CME_CONTRACT_STYPE_IN')
    config = {}
    for number, line in enumerate(env_path.read_text(encoding='utf-8-sig').splitlines(), 1):
        key, separator, value = line.partition('=')
        if separator and key.strip() in config_keys:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            config[key.strip()] = {'line': number, 'value': value}
    expected_configuration = {
        'DATABENTO_CME_CONTEXT_SYMBOLS': ['ES.v.0', 'NQ.v.0', 'RTY.v.0', 'CL.v.0', 'GC.v.0'],
        'DATABENTO_CME_CONTEXT_STYPE_IN': 'continuous',
        'DATABENTO_CME_CONTRACT_SYMBOLS': ['ESZ6', 'NQZ6', 'CLX6', 'GCZ6'],
        'DATABENTO_CME_CONTRACT_STYPE_IN': 'raw_symbol',
    }
    configuration_checks = {}
    for key, expected in expected_configuration.items():
        saved = config.get(key, {}).get('value')
        observed = json.loads(saved) if key.endswith('_SYMBOLS') and saved else saved
        configuration_checks[key] = observed == expected
        if observed != expected:
            issues.append('Explicit corrected CME configuration differs: ' + key)
    captures = []
    for scope, prefix in [('CME_CONTEXT', 'DATABENTO_CME_CONTEXT'),
                          ('CME_CONTRACTS', 'DATABENTO_CME_CONTRACT')]:
        expected_symbols = json.loads(config[prefix + '_SYMBOLS']['value'])
        expected_stype = config[prefix + '_STYPE_IN']['value'].strip('"\'')
        for schema in ('ohlcv-1m', 'bbo-1m', 'mbp-10'):
            period = scope.lower() + '_' + schema
            path = root / 'pools/cme' / scope / period / 'databento/normalized' / (scope + '_' + period + '.parquet')
            metadata = pq.read_metadata(path)
            if metadata.num_rows > 300000:
                raise ValueError('Bounded CME capture row limit exceeded: ' + str(path))
            available = set(pq.read_schema(path).names)
            required = {'fetched_at', 'timestamp', 'symbol', 'provider_stype_in'}
            if not required.issubset(available):
                raise ValueError('CME capture lacks required source identity fields: ' + str(path))
            request_columns = [key for key in (
                'provider_stype_in', 'request_limit_saturated', 'initial_range_start',
                'initial_range_end', 'effective_range_start', 'effective_range_end',
                'latest_window_shrink_count', 'empty_window_expansion_count') if key in available]
            frame = pd.read_parquet(path, columns=sorted(required | set(request_columns)))
            receipts = pd.to_datetime(frame['fetched_at'], utc=True)
            current = frame.loc[receipts.ge(start) & receipts.le(finish)].copy()
            present = sorted(current['symbol'].dropna().unique().tolist())
            missing = sorted(set(expected_symbols) - set(present))
            unexpected = sorted(set(present) - set(expected_symbols))
            if unexpected or not current['provider_stype_in'].eq(expected_stype).all():
                issues.append(scope + '/' + schema + ' current capture source identity differs from explicit configuration')
            if missing:
                notes.append(scope + '/' + schema + ' has no current observations for configured symbols: ' + ', '.join(missing))
            groups = []
            for symbol, rows in current.groupby('symbol'):
                market = pd.to_datetime(rows['timestamp'], utc=True)
                groups.append({'symbol': symbol, 'rows': len(rows),
                               'first_market_time': str(market.min()), 'last_market_time': str(market.max())})
            captures.append({'path': str(path), 'scope': scope, 'schema': schema,
                             'file_rows': metadata.num_rows, 'current_stage_rows': len(current),
                             'expected_symbols': expected_symbols, 'expected_stype_in': expected_stype,
                             'symbols': groups, 'missing_requested_symbols': missing,
                             'unexpected_symbols': unexpected,
                             'request_metadata': current[request_columns].drop_duplicates().to_dict('records')})
    cme_scope_coverage = {
        'configuration_path': str(env_path), 'selected_nonsecret_configuration': config,
        'captures': captures,
        'expected_configuration_checks': configuration_checks,
        'native_unresolved_symbol_warnings': [line for line in native_lines if 'did not resolve' in line],
        'interpretation': 'Current fetched_at rows only. Missing configured raw contracts are an explicit source limitation, not silently replaced. Continuous scopes remain separate. Sparse venue observations alone do not prove a failed download. MBP captures may be capped.',
    }
    result = {
        'observed_at': datetime.now(timezone.utc).isoformat(),
        'disposition': 'ADVISORY_REVIEW_REQUIRED' if issues or notes else 'KNOWN_OPTIONAL_LIMITATIONS_REVERIFIED',
        'issues': issues, 'coverage_notes': notes, 'diagnostics': diagnostics,
        'fmp_source_path': str(quote_path), 'fmp_source_sha256': hashlib.sha256(quote_path.read_bytes()).hexdigest(),
        'fmp_total_source_rows': len(quote), 'fmp_current_source_rows': current_quote.to_dict('records'),
        'fmp_full_source_pure_validation': full_result, 'fmp_current_source_pure_validation': current_result,
        'fmp_full_validation_matches_prior': full_result == previous_full,
        'fmp_historical_clock_skew_rows': offenders.to_dict('records'),
        'prior_evidence': str(prior_path), 'prior_evidence_sha256': hashlib.sha256(prior_path.read_bytes()).hexdigest(),
        'native_advisory_lines': [line for line in native_lines if any(key in line.lower() for key in ('advisory', 'cme', 'energy-context', 'clock skew'))],
        'cme_scope_coverage': cme_scope_coverage,
        'scope': 'Two bounded diagnostic parquets, one bounded FMP quote parquet, six compact projected CME captures, four nonsecret configuration values and prior audit JSON. Pure local derivation only; no provider/broker calls or archive scan.',
    }
    return json.loads(json.dumps(result, default=str), parse_constant=lambda value: None)
