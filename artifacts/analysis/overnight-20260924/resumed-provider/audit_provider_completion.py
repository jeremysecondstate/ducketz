"""Bounded read-only audit of the September 24 overnight provider stage.

Only the report/helper directory is writable. Reads the current native log,
small current metadata and the 33 exact source-session partitions; never calls
providers, reads unrelated retained partitions, or controls a process.
"""
from pathlib import Path
from datetime import datetime, timezone
import sys
from collections import Counter
import argparse
import hashlib
import json
import re

ROOT = Path('C:/DATASTORE')
OUT = Path(__file__).resolve().parent
RUN = ROOT / 'ml/overnight-runs/20260924T044352.904210Z'
EXPECTED_CYCLE_ID = '20260924T044353.640845Z-pid64532'
OPRA = ROOT / 'market-data/databento/opra/OPRA.PILLAR'
SOURCE, END, ACTION = '2026-09-23', '2026-09-24', '2026-09-24'
WATCHLIST = Path('C:/dev/ducketz/datafetching/watchlist.txt')
SYMBOLS = [line.strip().upper() for line in WATCHLIST.read_text().splitlines()
           if line.strip() and not line.lstrip().startswith('#')]
SCHEMAS = ['ohlcv-1h', 'cbbo-1m', 'definition']
SCOPE_COUNT = len(SYMBOLS) * len(SCHEMAS)
args = argparse.ArgumentParser()
args.add_argument('--hash-current', action='store_true')
args = args.parse_args()
issues, pending = [], []


def load(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def check(condition, message):
    if not condition:
        issues.append(message)
    return bool(condition)


def fresh(value):
    return bool(value) and START <= datetime.fromisoformat(value.replace('Z', '+00:00')) <= FINISH


def digest_file(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


stage = load(RUN / 'stage-report.json')
native_stage = next((item for item in stage.get('stages', []) if item.get('stage') == 'loop_a_close_fetch'), None)
if native_stage is None or native_stage.get('status') != 'COMPLETE' or native_stage.get('exit_code') != 0:
    print(json.dumps({'status': 'NOT_READY_FOR_PROVIDER_COMPLETION_AUDIT',
                      'native_run': str(RUN), 'loop_a_stage': native_stage,
                      'archive_checks_started': False, 'provider_calls': 0}))
    raise SystemExit(2)
START = datetime.fromisoformat(native_stage['started_at'].replace('Z', '+00:00'))
FINISH = datetime.fromisoformat(native_stage['finished_at'].replace('Z', '+00:00'))
check(len(SYMBOLS) == len(set(SYMBOLS)) and bool(SYMBOLS), 'Invalid production watchlist')
check(stage.get('resumed_from', '').replace('\\', '/') == 'C:/DATASTORE/ml/overnight-runs/20260924T040733.615371Z', 'Expected explicit original-attempt ancestry differs')
check(stage.get('archive_history') is True and stage.get('stock_price_source') == 'xnas-itch-archive-v1' and stage.get('probability_target_contract') == 'raw-price-direction-v1', 'Inherited source/archive/target policy differs')
check(stage.get('deadline_at') == ACTION + 'T11:00:00+00:00'
      and stage.get('deadline_exception') is None, 'Original deadline differs or exception applied')
check(stage.get('orders_placed') == 0 and stage.get('broker_orders_enabled') is False,
      'Native report does not explicitly record zero orders')
log_path = RUN / 'loop_a_close_fetch.log'
log_bytes = log_path.read_bytes()
lines = log_bytes.decode('utf-8', errors='replace').splitlines()
cycle = load(ROOT / '.ducketz-loop-a-cycle.json')
complete = load(ROOT / '.ducketz-loop-a-complete.json')
cycle_times = [line.removeprefix('CYCLE ').strip() for line in lines if line.startswith('CYCLE ')]
check(len(cycle_times) == 1, 'Expected exactly one native fetch cycle')
logged_cycle_time = datetime.fromisoformat(cycle_times[0].replace('Z', '+00:00')) if len(cycle_times) == 1 else None
cycle_started = datetime.fromisoformat(cycle['started_at'].replace('Z', '+00:00'))
cycle_prefix = logged_cycle_time.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ-pid') if logged_cycle_time else None
check(logged_cycle_time == cycle_started and START <= cycle_started <= FINISH,
      'Base cycle does not match the exact native log timestamp')
check(cycle_prefix is not None and re.fullmatch(re.escape(cycle_prefix) + r'\d+', cycle['generation']) is not None,
      'Base cycle generation does not bind the logged cycle timestamp and a process ID')
CYCLE_ID = cycle['generation']
check(CYCLE_ID == EXPECTED_CYCLE_ID, 'Base cycle differs from observed original native cycle')
log_watchlists = [line.removeprefix('Watchlist: ').split(', ') for line in lines if line.startswith('Watchlist: ')]
check(log_watchlists == [SYMBOLS], 'Native log universe differs from production watchlist')
check(cycle['symbols'] == SYMBOLS, 'Base cycle saved universe differs')
check(cycle['providers'] == ['databento', 'fmp', 'fred', 'schwab', 'sec'], 'Base provider scopes differ')
watchlist = SYMBOLS
base_done = cycle['status'] == 'COMPLETE'
if base_done:
    check(cycle == complete, 'Base complete receipt differs from cycle')
    check(cycle['failure_count'] == 0, 'Base cycle reports failures')
else:
    pending.append('Base Loop A cycle completion')
native_stage = next((s for s in stage['stages'] if s['stage'] == 'loop_a_close_fetch'), None)
native_done = native_stage is not None and native_stage['status'] == 'COMPLETE'
if native_done:
    check(native_stage['exit_code'] == 0, 'Native Loop A stage exit not zero')
else:
    pending.append('Native Loop A stage completion')

pattern = r'^\[(\w+)\] changed parquet files: (\d+); blocking provider failures: (\d+); optional capture failures: (\d+); local advisories: (\d+)(.*)$'
summaries = [{'symbol': m[1], 'changed_parquet_files': int(m[2]), 'blocking_provider_failures': int(m[3]), 'optional_capture_failures': int(m[4]), 'local_advisories': int(m[5]), 'advisory_detail': m[6].strip()} for line in lines if (m := re.match(pattern, line))]
check(all(s['blocking_provider_failures'] == s['optional_capture_failures'] == 0 for s in summaries), 'Provider summary has capture failures')
if base_done:
    check(len(summaries) == len(SYMBOLS) and {s['symbol'] for s in summaries} == set(SYMBOLS), 'Missing or duplicate final symbol capture summary')
features = {kind: [line for line in lines if f'END   loop-a.{kind} ' in line] for kind in ['fundamentals', 'technicals', 'signals']}
for kind, events in features.items():
    check(all('status=ok' in e for e in events), f'{kind} has unsuccessful completion')
    if base_done:
        check(len(events) == len(SYMBOLS) and {re.search(r'sym=(\w+)', e)[1] for e in events} == set(SYMBOLS), f'{kind} completion coverage mismatch')
calculation_counts = []
full_log = '\n'.join(lines)
for symbol in SYMBOLS:
    counts = {'symbol': symbol}
    for kind in ['fundamentals', 'technicals']:
        match = re.search(r'START loop-a\.' + kind + r' sym=' + symbol + r'\s.*?(?=END   loop-a\.' + kind + r' sym=' + symbol + r'\s)', full_log, re.S)
        if not match:
            if base_done:
                check(False, f'Missing {kind} calculation block {symbol}')
            continue
        block = match[0]
        if kind == 'fundamentals':
            summary = re.search(r'Fundamental outputs: (\d+); failures: (\d+)', block)
            if summary:
                counts.update(fundamental_outputs=int(summary[1]), fundamental_failures=int(summary[2]))
                check(int(summary[2]) == 0, f'Fundamental calculation failures {symbol}')
        else:
            for label, key in [('Output parquet files', 'technical_outputs'), ('Not-ready calculations skipped', 'technical_not_ready'), ('Failed calculations', 'technical_failures')]:
                found = re.search(label + r': (\d+)', block)
                if found:
                    counts[key] = int(found[1])
            check(counts.get('technical_failures', 0) == 0, f'Technical calculation failures {symbol}')
        if base_done:
            check(('fundamental_outputs' if kind == 'fundamentals' else 'technical_outputs') in counts, f'Missing {kind} calculation counts {symbol}')
    calculation_counts.append(counts)
output_paths = sorted(set(m[0] for line in lines for m in re.finditer(r'C:\\DATASTORE\\stocks\\[^\r\n]+?\.parquet', line)))
missing_outputs = [p for p in output_paths if not Path(p).is_file()]
check(not missing_outputs, 'Logged stock output path is missing')

preflights, cursors, partitions, scope_logs = [], [], [], []
for symbol in SYMBOLS:
    for schema in SCHEMAS:
        label = f'{symbol}/{schema}'
        current = []
        for path in (OPRA / 'metadata/preflights' / schema / (symbol + '.OPT')).glob(f'*_to_{END}/preflight.json'):
            p = load(path)
            if fresh(p.get('generated_at')):
                current.append((path, p))
        if not current:
            pending.append(f'Fresh preflight {label}')
        else:
            check(len(current) == 1, f'Multiple current preflights {label}')
            path, p = sorted(current, key=lambda x: x[1]['generated_at'])[-1]
            unsigned = {k: v for k, v in p.items() if k != 'semantic_checksum_sha256'}
            h = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()
            nested = p['estimates'].get(schema, {})
            tests = {
                'semantic_checksum': h == p['semantic_checksum_sha256'],
                'exact_scope': p['scope']['symbols'] == [symbol + '.OPT'] and p['scope']['schemas'] == [schema],
                'provider_dataset': p['provider'] == 'databento-opra' and p['dataset'] == 'OPRA.PILLAR',
                'source_session_covered': p['scope']['start'] <= SOURCE and p['scope']['end'] == END,
                'zero_cost_complete': p['cost_estimates_complete'] and p['estimated_cost_usd'] == 0 and nested.get('estimated_cost_usd') == 0,
                'nested_scope': list(p['estimates']) == [schema] and all(nested.get(k) == p['scope'][k] for k in ['symbols', 'start', 'end']),
                'capacity': p['capacity_pass'] and p['shortfall_bytes'] == 0 and p['available_free_bytes'] >= p['required_free_bytes'],
                'capacity_formula': p['storage_expansion_factor'] == 2 and p['storage_reserve_bytes'] == 5 * 1024**3 and p['required_free_bytes'] == 5 * 1024**3 + 2 * p['estimated_download_size_bytes'],
                'size_sum': p['estimated_download_size_bytes'] == sum(e['estimated_download_size_bytes'] for e in p['estimates'].values()),
            }
            check(all(tests.values()), f'Failed preflight checks {label}: {[k for k, v in tests.items() if not v]}')
            preflights.append({'path': str(path), 'payload': p, 'checks': tests})
        cursor_path = OPRA / 'state/symbol-history' / symbol / (schema + '.json')
        cursor = load(cursor_path)
        identity = cursor['symbol'] == symbol and cursor['schema'] == schema and cursor['provider_symbol'] == symbol + '.OPT' and cursor['provider'] == 'databento-opra' and cursor['dataset'] == 'OPRA.PILLAR'
        check(identity, f'Cursor identity mismatch {label}')
        ready = cursor['completed_through'] == END and fresh(cursor['updated_at'])
        if not ready:
            pending.append(f'Current cursor {label}')
        else:
            check('replay_coverage' not in cursor, f'Unexpected Live replay cursor {label}')
        cursors.append({'path': str(cursor_path), 'payload': cursor, 'current_run_completion': ready})
        directory = OPRA / schema / (symbol + '.OPT') / 'dates' / SOURCE / 'segments/full-day'
        if not (directory / 'receipt.json').is_file():
            pending.append(f'Current partition {label}')
            continue
        manifest_bytes = (directory / 'manifest.json').read_bytes()
        m = json.loads(manifest_bytes)
        receipt = load(directory / 'receipt.json')
        mh = hashlib.sha256(manifest_bytes).hexdigest()
        request = m['request']
        tests = {
            'manifest_checksum': receipt['manifest_checksum_sha256'] == mh,
            'provider_dataset': m['dataset'] == receipt['dataset'] == 'OPRA.PILLAR' and m['provider'] == receipt['provider'] == 'databento-opra',
            'schema_date': m['schema'] == receipt['schema'] == schema and m['partition_date'] == receipt['partition_date'] == SOURCE,
            'symbol_request': m['symbol_scope'] == symbol + '.OPT' and request['symbols'] == [symbol + '.OPT'] and request['stype_in'] == 'parent' and request['schema'] == schema and request['dataset'] == 'OPRA.PILLAR',
            'start': request['start'] == m['partition_start'] == receipt['partition_start'] == SOURCE + 'T00:00:00+00:00',
            'end': request['end'] == m['partition_end'] == receipt['partition_end'] == END + 'T00:00:00+00:00',
            'native_historical': m['provider_delivery']['mode'] == 'timeseries-stream',
            'published_current_run': fresh(receipt['published_at']) and m['published_at'] == receipt['published_at'],
        }
        hashes = {}
        for field in ['raw', 'normalized']:
            saved = m[field]
            file = directory / saved['path']
            tests[field + '_binding_size'] = receipt[field + '_checksum_sha256'] == saved['checksum_sha256'] and file.stat().st_size == saved['size_bytes']
            if args.hash_current:
                actual = digest_file(file)
                hashes[field] = actual
                tests[field + '_actual_hash'] = actual == saved['checksum_sha256']
        check(all(tests.values()), f'Partition verification failed {label}: {[k for k, v in tests.items() if not v]}')
        partitions.append({'symbol': symbol, 'schema': schema, 'manifest_path': str(directory / 'manifest.json'), 'manifest_sha256': mh, 'receipt': receipt, 'request': request, 'normalized_rows': m['normalized']['row_count'], 'raw_bytes': m['raw']['size_bytes'], 'normalized_bytes': m['normalized']['size_bytes'], 'checks': tests, 'actual_data_hashes': hashes})

for line in lines:
    if line.startswith('OPRA symbol/schema history: '):
        scope_logs.append(dict(part.strip().split('=', 1) for part in line.split(': ', 1)[1].split(';')))
check(all(s['status'] == 'COMPLETE' and s['end'] == END for s in scope_logs), 'OPRA scope log reports incomplete/failing range')
for entry in preflights:
    scope = entry['payload']['scope']
    symbol = scope['symbols'][0].removesuffix('.OPT')
    schema = scope['schemas'][0]
    matches = [row for row in scope_logs if row.get('symbol') == symbol and row.get('schema') == schema]
    check(len(matches) == 1 and all(matches[0].get(key) == scope[key] for key in ('start', 'end')),
          f'Native completion range differs from exact preflight: {symbol}/{schema}')
maintenance = [line for line in lines if line.startswith('Options history maintenance finished:')]
selection = [line for line in lines if line.startswith('OPRA history guarded preflight:')]
estimated = sum(p['payload']['estimated_download_size_bytes'] for p in preflights)
check(estimated <= 20_000_000_000, 'Observed OPRA preflight estimates exceed 20GB limit')
if maintenance:
    check(len(maintenance) == 1, 'Multiple maintenance summaries')
    expected_summary = [f'requested_scopes={SCOPE_COUNT};', f'completed_scopes={SCOPE_COUNT};', 'capacity_blocked_scopes=0;', 'failed_scopes=0;', 'bootstrap_required_scopes=0;', f'preflighted_scopes={SCOPE_COUNT};', 'deferred_scopes=0;', 'live_replay_completed_scopes=0;', 'live_replay_bytes=0;', f'selected_estimated_download_bytes={estimated};', 'selected_estimated_cost_usd=0.0']
    check(all(text in maintenance[0] for text in expected_summary), 'OPRA maintenance summary counts/cost differ')
    check(len(scope_logs) == SCOPE_COUNT and {(s['symbol'], s['schema']) for s in scope_logs} == {(s, c) for s in SYMBOLS for c in SCHEMAS}, 'OPRA complete scope log coverage differs')
else:
    pending.append('Native OPRA maintenance terminal summary')
if native_done and pending:
    issues.extend('Completed native stage missing: ' + p for p in pending)

health_path = OPRA / 'health/current.json'
health = load(health_path)
health_fresh = fresh(health['observed_at'])
health_totals = all(health[top] == sum(s.get(field, 0) for s in health['schemas'].values()) for top, field in [('total_partitions', 'partition_count'), ('total_rows', 'row_count'), ('total_raw_bytes', 'raw_bytes'), ('total_parquet_bytes', 'parquet_bytes')])
if native_done:
    check(health_fresh and health_totals, 'Native health inventory freshness/totals do not verify')
    check(health.get('provider') == 'databento-opra' and health.get('dataset') == 'OPRA.PILLAR', 'Native health provider identity differs')
    check(any(line.startswith('REFRESHED_OPRA_HEALTH ') for line in lines), 'Native log lacks completed health publication')
    check([line for line in lines if line.startswith('Loop A OPRA Strategy history maintenance:')] == [f'Loop A OPRA Strategy history maintenance: exit_code=0; attempt_date={END}'], 'Native OPRA maintenance exit differs')

from provider_advisories import audit_advisories
try:
    advisory_review = audit_advisories(ROOT, START, FINISH, lines)
    issues.extend(advisory_review.get('issues', []))
except Exception as exc:
    advisory_review = {'disposition': 'ADVISORY_AUDIT_FAILED', 'error': type(exc).__name__ + ': ' + str(exc)}
    issues.append('Bounded optional-advisory audit failed; inspect report')
result = {
    'audited_at': datetime.now(timezone.utc).isoformat(),
    'status': 'ISSUES_FOUND' if issues else (f'PASS_NATIVE_LOOP_A_AND_{SCOPE_COUNT}_OPRA_SCOPES_COMPLETE' if native_done and not pending else 'PENDING_NATIVE_PROVIDER_COMPLETION'),
    'issues': issues, 'pending': pending, 'source_session': SOURCE, 'required_exclusive_end': END, 'action_date': ACTION,
    'production_symbols': SYMBOLS, 'native_run': str(RUN), 'base_cycle': cycle, 'base_complete_receipt_matches': cycle == complete,
    'stage_snapshot': stage, 'loop_a_stage_completion': native_stage,
    'provider_summaries': summaries, 'feature_completions': features,
    'calculation_counts': calculation_counts,
    'provider_scope_log_lines': [line for line in lines if ('fetching' in line and any('[' + p in line for p in ['fmp', 'schwab', 'sec'])) or 'fetching shared macro series' in line or 'END   loop-a.databento-watchlist' in line],
    'logged_output_paths_checked': len(output_paths), 'missing_logged_outputs': missing_outputs,
    'provider_request_completed_lines': [line for line in lines if 'END   provider.request' in line],
    'schwab_options_capture_lines': [line for line in lines if '/schwab/options]' in line],
    'preflight_count': len(preflights), 'preflights': preflights, 'observed_estimated_download_bytes': estimated,
    'current_cursor_count': sum(c['current_run_completion'] for c in cursors), 'cursors': cursors,
    'current_partition_count': len(partitions), 'current_partitions': partitions,
    'current_partition_rows': sum(p['normalized_rows'] for p in partitions),
    'current_partition_bytes': sum(p['raw_bytes'] + p['normalized_bytes'] for p in partitions),
    'current_data_files_hashed': 2 * len(partitions) if args.hash_current else 0,
    'native_scope_completion_logs': scope_logs, 'native_selection_summary': selection, 'native_maintenance_summary': maintenance,
    'native_exit_summary': [line for line in lines if line.startswith('Loop A OPRA Strategy history maintenance:')],
    'health_inventory': {'path': str(health_path), 'sha256': digest_file(health_path), 'payload': health, 'fresh_current_run': health_fresh, 'inventory_totals_consistent': health_totals},
    'prior_advisory_evidence': 'C:/dev/ducketz/artifacts/analysis/overnight-20260918/provider-preflight-review.md',
    'known_advisory_policy': 'Prior CME stale partition-derived context and retained FMP historical quote clock skew are optional limitations. Current-cycle identity/quality evidence must be compared before describing them as unchanged.',
    'optional_advisory_review': advisory_review,
    'optional_advisory_markdown': None,
    'log_path': str(log_path), 'log_sha256_at_snapshot': hashlib.sha256(log_bytes).hexdigest(),
    'scope_limit': f'Bounded current base cycle, native log, {SCOPE_COUNT} small production cursors, fresh exact preflights and only {SOURCE} native partitions. Data file hashing is explicitly counted. No provider/broker calls, production mutation, process/lock/claim actions, training, or retained-archive scan. Native health inventories selected verified partitions and does not establish validity of every retained directory.',
}
(OUT / 'provider-completion.json').write_text(json.dumps(result, indent=2) + '\n')
md = [
    f'# {ACTION} provider evidence audit', '',
    f"Observed **{result['audited_at']}**. Status: **{result['status']}**.", '',
    f"Native run `{RUN.as_posix()}` prepares **{ACTION}** from source session **{SOURCE}**, with original deadline `{stage['deadline_at']}`. Its saved/current universe agrees on {len(SYMBOLS)} symbols: {', '.join(SYMBOLS)}.", '',
    f"Base cycle `{cycle['generation']}` is **{cycle['status']}**, with all five configured providers. Current log has {len(summaries)}/{len(SYMBOLS)} symbol summaries, {len(features['fundamentals'])}/{len(SYMBOLS)} fundamental completions, {len(features['technicals'])}/{len(SYMBOLS)} technical completions and {len(features['signals'])}/{len(SYMBOLS)} signal completions. {len(output_paths)} logged stock Parquet paths were checked; {len(missing_outputs)} are missing.", '',
    f"Provider changes total **{sum(s['changed_parquet_files'] for s in summaries)} Parquets**. Calculations produced **{sum(c.get('fundamental_outputs', 0) for c in calculation_counts)} fundamental** and **{sum(c.get('technical_outputs', 0) for c in calculation_counts)} technical outputs**, with **{sum(c.get('fundamental_failures', 0) + c.get('technical_failures', 0) for c in calculation_counts)} failures** and **{sum(c.get('technical_not_ready', 0) for c in calculation_counts)} technical not-ready skips**. Every observed symbol capture summary has zero blocking and optional capture failures.", '',
    f"Production OPRA: **{len(preflights)}/{SCOPE_COUNT}** fresh exact symbol/schema preflights, **{result['current_cursor_count']}/{SCOPE_COUNT}** current-run exclusive `{END}` cursors, **{len(partitions)}/{SCOPE_COUNT}** source-session Historical partition receipts, and **{len(scope_logs)}/{SCOPE_COUNT}** native COMPLETE scope log entries. Available receipts are checked for exact source/range, checksum binding, zero-dollar nested/total cost and native capacity formula. Observed estimated download size: **{estimated:,} bytes** (20,000,000,000-byte ceiling). Current partition raw/normalized files hashed: **{result['current_data_files_hashed']}**.", '',
    f"Native Loop A stage completion: **{native_stage['status'] if native_stage else 'PENDING'}**. Native OPRA terminal summary: **{'present' if maintenance else 'PENDING'}**. Health inventory freshness: **{health_fresh}**; inventory totals consistent: **{health_totals}**. Pending native work is not a provider failure.", '',
    f"Discrepancies: **{len(issues)}**. Outstanding evidence checks: **{len(pending)}**.", '',
    'Prior optional advisory evidence is retained at [provider-preflight-review.md](C:/dev/ducketz/artifacts/analysis/overnight-20260918/provider-preflight-review.md). The previous CME stale partition context and FMP historical clock-skew rejections do not establish a new defect; current evidence must still be compared before claiming unchanged status.', '',
    'This is bounded provider evidence only. Operational EQUS.MINI captures do not replace the later XNAS.ITCH target-history stage. Selected native health inventory is not proof that every retained historical directory is valid. No provider/broker requests, source writes, process changes, supervision claims or training were performed.', '',
    'Full evidence: [provider-completion.json](C:/dev/ducketz/artifacts/analysis/overnight-20260924/resumed-provider/provider-completion.json).', '',
]
if issues:
    md += ['## Discrepancies', ''] + ['- ' + issue for issue in issues] + ['']
md += ['## Current and retained optional advisories', '',
       f"Disposition: **{advisory_review['disposition']}**. Exact current/retained diagnostics and pure FMP derivation are saved in the JSON.", '']
md += ['- ' + note for note in advisory_review.get('coverage_notes', [])] + ['']
md += ['## Explicit CME scope coverage', '',
       'Successful requests do not prove every requested symbol returned observations. Current capture rows are distinguished from retained history; sparse observed bars are not labeled as download failures.', '',
       '| Scope | Schema | Current rows | Missing configured symbols |',
       '|---|---|---:|---|']
for scope in advisory_review.get('cme_scope_coverage', {}).get('captures', []):
    md.append(f"| {scope['scope']} | {scope['schema']} | {scope['current_stage_rows']:,} | {', '.join(scope['missing_requested_symbols']) or 'None'} |")
md += ['', 'CME MBP request limits remain explicit bounded evidence. No missing raw contract is substituted with continuous or another contract price.', '']
(OUT / 'provider-completion.md').write_text('\n'.join(md))
print(json.dumps({k: result[k] for k in ['audited_at', 'status', 'issues', 'preflight_count', 'current_cursor_count', 'current_partition_count', 'current_data_files_hashed', 'observed_estimated_download_bytes']}, indent=2))
print(json.dumps({'pending_count': len(pending), 'base_status': cycle['status'], 'symbol_capture_summaries': len(summaries), 'native_loop_a_complete': native_done}, indent=2))

raise SystemExit(1 if issues or pending else 0)
