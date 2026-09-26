"""Bounded read-only September 25 session receipt review, never a trader call."""
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import sys

sys.dont_write_bytecode = True
REPO = Path('C:/dev/ducketz')
ROOT = Path('C:/DATASTORE')
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
from ml.stock_trader.session import stock_execution_window


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


status_path = ROOT/'state/independent-stock-trader/session-status.json'
status = read(status_path)
assert status['action_date'] == '2026-09-25' and status['status'] == 'FINISHED_WITH_ERRORS'
start = datetime.fromisoformat(status['started_at']).strftime('%Y%m%dT%H%M%S')
end = Path(status['last_cycle']['run_directory']).name
paths = sorted(p/'receipt.json' for p in (ROOT/'ml/stock-trader-decision-runs').glob('202609*')
               if p.is_dir() and start <= p.name <= end)
assert len(paths) == status['calls'] == 1639 and len(paths) <= 2500
rows, inventory = [], []
for path in paths:
    assert path.stat().st_size < 1_000_000
    receipt = read(path)
    assert receipt['prediction_handoff']['session_managed'] is True
    assert receipt['prediction_handoff']['sizing_policy'] == status['sizing_policy']
    rows.append((path, receipt))
    inventory.append({'run': path.parent.name, 'receipt_sha256': sha(path), 'status': receipt['status']})
counts = dict(Counter(row['status'] for _, row in rows))
assert counts == {'ORDERS_SELECTED': 21, 'NO_TRADE': 1616,
                  'EXECUTION_WINDOW_CLOSED_AFTER_BROKER_CAPTURE': 2}


def detail(path, receipt):
    run = path.parent
    manifest, decisions = read(run/'manifest.json'), read(run/'decisions.json')
    checks = {'receipt_manifest_binding': receipt['manifest_sha256'] == sha(run/'manifest.json'),
              'receipt_decisions_binding': receipt['decisions_sha256'] == sha(run/'decisions.json'),
              'manifest_decisions_binding': manifest['output_files']['decisions.json']['checksum_sha256'] == sha(run/'decisions.json'),
              'manifest_decisions_size': manifest['output_files']['decisions.json']['size'] == (run/'decisions.json').stat().st_size,
              'receipt_decision_status_match': receipt['status'] == decisions['status'],
              'zero_selected_orders': receipt['orders_selected'] == decisions['orders_selected'] == 0,
              'empty_decision_ids_and_rows': receipt['decision_ids'] == decisions['decisions'] == [],
              'current_broker_capture': receipt['broker_state_capture']['status'] == 'CURRENT',
              'ready_inventory': receipt['prediction_handoff']['inventory_reconciliation'] == {'ready': True, 'reasons': []}}
    assert all(checks.values())
    window = stock_execution_window(receipt['decided_at'])
    return {'run': str(run), 'receipt': receipt, 'checks': checks,
            'source_sha256': {name: sha(run/name) for name in ['receipt.json', 'manifest.json', 'decisions.json']},
            'native_window_at_saved_decision_timestamp': {'executable': window.executable, 'mode': window.mode, 'reason': window.reason},
            'error_evidence_note': 'Receipt saves the exact status but not StockTraderRunResult.error. The error reason here is a pure native calendar derivation at its saved decision time; the final session last_cycle additionally saves that reason.'}


failed = []
for index, (path, receipt) in enumerate(rows):
    if receipt['status'] == 'EXECUTION_WINDOW_CLOSED_AFTER_BROKER_CAPTURE':
        item = detail(path, receipt)
        if index + 1 < len(rows):
            item['next_cycle'] = detail(*rows[index+1])
        else:
            item['next_cycle'] = None
        failed.append(item)
assert len(failed) == status['failed_cycles']
assert all(not item['native_window_at_saved_decision_timestamp']['executable'] for item in failed)
assert failed[0]['next_cycle']['receipt']['status'] == 'NO_TRADE'
assert failed[0]['next_cycle']['native_window_at_saved_decision_timestamp']['executable']
source_files = [status_path, REPO/'ml/stock_trader/independent_session.py',
                REPO/'ml/stock_trader/independent_runtime.py', REPO/'ml/stock_trader/session.py',
                OUT/'local-preflight.json']
report = {'reviewed_at': datetime.now(timezone.utc).isoformat(),
          'scope': 'BOUNDED_LOCAL_SAVED_SESSION_RECEIPTS_ONLY_NO_TRADER_OR_BROKER_CALLS',
          'status': 'TWO_BOUNDARY_FAILURES_IDENTIFIED_NO_NEW_OVERNIGHT_OWNERSHIP_BLOCKER',
          'session_status': status, 'receipt_inventory_count': len(rows), 'receipt_status_counts': counts,
          'receipt_inventory': inventory, 'failed_cycles': failed,
          'source_sha256': {str(path): sha(path) for path in source_files},
          'interpretation': 'The earlier failure crossed the 13:00 Pacific transition during broker capture. The next recorded cycle at 13:05:10 Pacific was NO_TRADE with CURRENT broker capture and ready inventory, satisfying the native branch that clears prior consecutive failures. The later 17:00 failure remained terminal FINISHED_WITH_ERRORS; no status was changed.',
          'overnight_implication': 'These two cycles selected no orders and both reconciled inventory ready before the execution-window gate. The independent preflight found no pending reservations or persistent blocks. No ledger maintenance or trader repair is indicated for overnight planning by this evidence. Planning still requires its normal fresh account/ownership snapshot.',
          'limitations': ['The session is not relabeled healthy.', 'This bounded review does not assess all daytime order outcomes or performance.', 'Exact historical stdout for the earlier StockTraderRunResult.error was not found; saved status and pure native reason are distinguished.'],
          'production_mutations': 0, 'provider_calls': 0, 'broker_calls': 0, 'trader_starts': 0}
(OUT/'daytime-failures.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
print(json.dumps({'status': report['status'], 'receipt_count': len(rows), 'failed_cycles': [item['run'] for item in failed], 'output': str(OUT/'daytime-failures.json')}))
