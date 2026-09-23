"""Bounded offline advisory audit called only after native Loop A completion.

Reads two compact diagnostic parquets, one retained FMP quote parquet and the
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
    prior_path = Path('C:/dev/ducketz/artifacts/analysis/overnight-20260919/provider-completion.json')
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
        'scope': 'Two bounded diagnostic parquets, one bounded FMP quote parquet and prior audit JSON. Pure local derivation only; no provider/broker calls or archive scan.',
    }
    return json.loads(json.dumps(result, default=str), parse_constant=lambda value: None)
