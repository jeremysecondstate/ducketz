"""Read-only overnight ownership, activation and CME source preflight.

No provider calls, ledger writes, supervision operations, or trading actions.
Only sanitized audit files in this script's directory are written.
"""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import os
import sqlite3
import subprocess
import sys

sys.dont_write_bytecode = True
REPO = Path('C:/dev/ducketz')
ROOT = Path('C:/DATASTORE')
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    from ml.gameplan_trade_snapshot import _ownership
    import pandas as pd
    import pyarrow.parquet as pq

    now = datetime.now(timezone.utc).isoformat()
    watchlist = REPO/'datafetching/watchlist.txt'
    symbols = [s.strip() for s in watchlist.read_text().splitlines()
               if s.strip() and not s.lstrip().startswith('#')]
    sources = [watchlist]
    batches = []
    for registry in sorted((ROOT/'state/research-onboarding').glob('*.json')):
        registered = read(registry)
        plan = Path(registered['plan_path'])
        activation_path = plan.parent/'activation.json'
        progress_path = plan.parent/'progress.json'
        activation = read(activation_path) if activation_path.exists() else {}
        progress = read(progress_path) if progress_path.exists() else {}
        checks = {'watchlist_matches_activation': symbols == activation.get('symbols'),
                  'selected_symbols_in_production': set(registered['selected_symbols']) <= set(symbols)}
        for name, run, digest in [
            ('gameplan_receipt', activation.get('gameplan_run'), activation.get('gameplan_receipt_sha256')),
            ('trade_plan_receipt', activation.get('trade_plan', {}).get('run_path'), activation.get('trade_plan', {}).get('receipt_sha256')),
            ('overnight_receipt', activation.get('overnight_run'), activation.get('overnight_receipt_sha256')),
        ]:
            checks[name] = bool(run and digest and sha(Path(run)/'receipt.json') == digest)
        batches.append({'registry': str(registry), 'activation': str(activation_path),
            'selected_symbols': registered['selected_symbols'], 'status': activation.get('status'),
            'activated_at': activation.get('activated_at'), 'progress': progress,
            'checks': checks, 'unfinished': activation.get('status') != 'ACTIVE'})
        sources.extend([registry, activation_path, progress_path])

    status_path = ROOT/'state/independent-stock-trader/session-status.json'
    status = read(status_path)
    decision_run = Path(status['last_cycle']['run_directory'])
    receipt = read(decision_run/'receipt.json')
    decision_checks = {name: sha(decision_run/(name+'.json')) == receipt[name+'_sha256']
                       for name in ['manifest', 'decisions']}
    sources.extend([status_path, decision_run/'receipt.json', decision_run/'manifest.json', decision_run/'decisions.json'])

    # Filter real launchers; exclude this audit's own PowerShell process.
    command = "Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('python.exe','pythonw.exe','cmd.exe') -and $_.CommandLine -match 'ml\\.(gameplan_stock_trader|independent_stock_trader)|Start-Gameplan-Trader' } | Select-Object ProcessId,ParentProcessId,CreationDate,Name,CommandLine | ConvertTo-Json -Depth 3"
    processes_text = subprocess.run(['powershell.exe', '-NoProfile', '-Command', command],
                                    check=True, text=True, capture_output=True).stdout.strip()
    processes = json.loads(processes_text) if processes_text else []
    if isinstance(processes, dict):
        processes = [processes]

    ledger = ROOT/'state/independent-stock-trader/holdings.sqlite3'
    if Path(str(ledger)+'-wal').exists() and not Path(str(ledger)+'-shm').exists():
        raise RuntimeError('Ledger WAL without SHM: cannot perform read-only preflight')
    connection = sqlite3.connect(ledger.as_uri()+'?mode=ro', uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        saved = dict(connection.execute('SELECT id,observed_at,ready,reasons,owned,payload FROM snapshots ORDER BY observed_at DESC LIMIT 1').fetchone())
        payload = json.loads(saved.pop('payload'))
        saved['reasons'] = json.loads(saved['reasons'])
        saved['owned'] = json.loads(saved['owned'])
        counts = [dict(row) for row in connection.execute('SELECT status,count(*) AS count FROM reservations GROUP BY status')]
        pending = [dict(row) for row in connection.execute("SELECT a.symbol,a.horizon,r.side,r.quantity,r.filled,(r.quantity-r.filled) AS remaining,r.status,r.last_evidence_at,a.start,a.end FROM reservations r JOIN allocations a ON a.id=r.allocation WHERE r.status IN ('RESERVED','SUBMITTED','UNKNOWN','WORKING','PARTIAL') ORDER BY a.symbol,a.horizon")]
        blocks = [dict(row) for row in connection.execute('SELECT symbol,reason FROM blocks')]
    finally:
        connection.close()
    ownership = _ownership(ROOT, symbols, payload['account_fingerprint'], payload['held_shares'], now)

    config_keys = ['DATABENTO_CME_CONTEXT_SYMBOLS', 'DATABENTO_CME_CONTEXT_STYPE_IN',
                   'DATABENTO_CME_CONTRACT_SYMBOLS', 'DATABENTO_CME_CONTRACT_STYPE_IN']
    config = {}
    for number, line in enumerate((REPO/'.env').read_text(encoding='utf-8-sig').splitlines(), 1):
        key, separator, value = line.partition('=')
        if separator and key.strip() in config_keys:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ['"', "'"]:
                value = value[1:-1]
            config[key.strip()] = {'line': number, 'value': json.loads(value) if key.strip().endswith('_SYMBOLS') else value}
    # Only nonsecret CME keys are selected, never other environment values.
    overrides = {key: os.environ[key] for key in config_keys if key in os.environ}
    captures = []
    mappings = {}
    for scope in ['CME_CONTEXT', 'CME_CONTRACTS']:
        period = scope.lower()+'_bbo-1m'
        path = ROOT/'pools/cme'/scope/period/'databento/raw'/(scope+'_'+period+'_raw.parquet')
        rows = pq.read_metadata(path).num_rows
        if rows > 300000:
            raise RuntimeError('CME audit bounded row limit exceeded')
        frame = pd.read_parquet(path, columns=['ts_event','instrument_id','symbol','provider_stype_in','provider_dataset','range_start','range_end'])
        frame['_timestamp'] = pd.to_datetime(frame['ts_event'], utc=True)
        latest_date = frame['_timestamp'].max().date()
        latest = frame.loc[frame['_timestamp'].dt.date.eq(latest_date)]
        groups = []
        for symbol, data in latest.groupby('symbol'):
            item = {'symbol': str(symbol), 'instrument_ids': sorted(int(x) for x in data['instrument_id'].unique()),
                    'rows': len(data), 'first_event': data['_timestamp'].min().isoformat(),
                    'last_event': data['_timestamp'].max().isoformat(),
                    'provider_stypes': sorted(str(x) for x in data['provider_stype_in'].unique()),
                    'provider_datasets': sorted(str(x) for x in data['provider_dataset'].unique())}
            groups.append(item)
            mappings[str(symbol)] = item
        captures.append({'path': str(path), 'sha256': sha(path), 'rows': rows,
                         'latest_observed_utc_date': str(latest_date), 'latest_date_groups': groups})
    pairs = []
    for continuous, raw in [('ES.v.0','ESZ6'),('NQ.v.0','NQZ6'),('CL.v.0','CLX6'),('GC.v.0','GCZ6')]:
        left, right = mappings.get(continuous, {}), mappings.get(raw, {})
        pairs.append({'continuous': continuous, 'raw': raw,
            'instrument_ids_match_in_latest_saved_capture': bool(left and right and left['instrument_ids'] == right['instrument_ids']),
            'continuous_last_event': left.get('last_event'), 'raw_last_event': right.get('last_event'),
            'instrument_ids': right.get('instrument_ids')})

    # Reverify the retained native GC definition, whose recorded expiry remains after today.
    definition = ROOT/'market-data/databento/cme/GLBX.MDP3/definition/GC.V.0/windows/2026-05-07_to_2026-08-15'
    dr, dm = read(definition/'receipt.json'), read(definition/'manifest.json')
    checks = {'manifest': sha(definition/'manifest.json') == dr['manifest_checksum_sha256']}
    defpaths = {}
    for name in ['raw','normalized']:
        entry = dm[name]
        path = Path(entry['path'])
        if not path.is_absolute():
            path = definition/path
        defpaths[name] = path
        checks[name] = sha(path) == dr[name+'_checksum_sha256'] == entry['checksum_sha256']
    defs = pd.read_parquet(defpaths['normalized'], columns=['raw_symbol','instrument_id','expiration','activation'])
    gold = defs.loc[defs.raw_symbol.eq('GCZ6')].drop_duplicates().to_dict('records')
    cme = {'selected_nonsecret_configuration': config, 'process_overrides': overrides,
           'captures': captures, 'same_capture_instrument_pairs': pairs,
           'retained_GCZ6_definition': {'path': str(definition), 'checks': checks, 'records': gold},
           'limitation': 'Saved latest captures verify instrument identity at their recorded times. They do not prove tonight native acquisition completion or a future roll.'}

    result = {'reviewed_at': now, 'scope': 'LOCAL_READ_ONLY_PRECHECK_NO_PROVIDER_OR_BROKER_CALLS_NO_PRODUCTION_WRITES',
        'status': 'LOCAL_PLANNING_RECONCILIATION_BLOCKER' if not ownership['safe_for_planning'] else 'NO_LOCAL_RECONCILIATION_BLOCKER',
        'symbols': symbols, 'expected_forecasts': 24*len(symbols), 'expected_intents': 24*len(symbols),
        'expected_production_opra_cursors': 3*len(symbols), 'batches': batches,
        'unfinished_authorized_batches': [batch for batch in batches if batch['unfinished']],
        'terminal_session': status, 'matching_trader_processes': processes,
        'last_decision_receipt_checks': decision_checks,
        'last_decision_inventory_reconciliation': receipt.get('prediction_handoff', {}).get('inventory_reconciliation'),
        'latest_saved_ownership_reconciliation': saved, 'saved_held_shares': payload['held_shares'],
        'reservation_status_counts': counts, 'pending_reservations': pending, 'persistent_blocks': blocks,
        'planning_ownership_check_using_saved_snapshot': ownership, 'cme': cme,
        'source_sha256': {str(path): sha(path) for path in sources if path.is_file()},
        'broker_status_now': 'UNKNOWN_NO_PROVIDER_CALLS',
        'next_step': 'Root supervisor should inspect exact current terminal order evidence and a newer matching-account portfolio snapshot through the documented locked native reconciliation path before trade planning. Preserve unresolved reservations; do not infer cancellations.' if pending else 'Native trade planning must capture its normal fresh account snapshot.'}
    (OUT/'local-preflight.json').write_text(json.dumps(result, indent=2, default=str)+'\n', encoding='utf-8')
    print(json.dumps({'status': result['status'], 'symbols': len(symbols), 'pending_reservations': pending,
                      'ownership_reasons': ownership['reason_codes'], 'trader_processes': len(processes),
                      'activation_checks': [batch['checks'] for batch in batches], 'cme_pairs': pairs,
                      'output': str(OUT/'local-preflight.json')}, indent=2))


if __name__ == '__main__':
    main()
