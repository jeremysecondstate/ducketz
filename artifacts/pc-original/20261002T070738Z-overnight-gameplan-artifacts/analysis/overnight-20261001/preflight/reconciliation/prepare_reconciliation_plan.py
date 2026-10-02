"""Bind tonight's local-only reconciliation review to exact current sources."""
from pathlib import Path
from dataclasses import asdict
from datetime import datetime, timezone
import json
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import native_reconcile as native

ROOT = native.ROOT
OUT = native.OUT
REPO = Path('C:/dev/ducketz')
OWNER = '84e5b37e-311c-4627-8e24-6c8e4bfd8ae8'


def main():
    status = native.read(native.STATUS)
    native.require(status['action_date'] == '2026-10-01' and status['status'] == 'FINISHED_WITH_ERRORS', 'EXPECTED_TERMINAL_SESSION_CHANGED')
    saved = native.table_state(native.DB)
    with native.connect(native.DB) as connection:
        account = dict(connection.execute('SELECT key,value FROM metadata'))['account']
        snapshot = native.HorizonLedger._snapshot(connection)
    pending = [asdict(row) for row in snapshot.reservations if row.status in native.OPEN]
    native.require({(row['symbol'],row['horizon'],row['side'],row['quantity'],row['filled_quantity']) for row in pending}
                   == {('CROX','1h','SELL',26,0),('TWST','1w','SELL',1,0),('CROX','4h','BUY',26,0)},
                   'OBSERVED_PENDING_SCOPE_CHANGED')
    native.require(len(pending) == 3 and not snapshot.persistent_blocks, 'PENDING_COUNT_OR_BLOCKS_CHANGED')
    last = sorted(saved['snapshots'], key=lambda row:row['observed_at'])[-1]
    native.require(last['ready'] == 1 and json.loads(last['reasons']) == [], 'SAVED_BASELINE_NOT_READY')
    decision = Path(status['last_cycle']['run_directory'])
    receipt = native.read(decision/'receipt.json')
    native.require(receipt['manifest_sha256'] == native.sha(decision/'manifest.json')
                   and receipt['decisions_sha256'] == native.sha(decision/'decisions.json'), 'LAST_DECISION_BINDING_FAILED')
    gameplan = ROOT/receipt['prediction_handoff']['source_gameplan_run']
    claims = sorted(p for folder in ('entry-slots','quote-recovery-slots')
                    for p in (ROOT/'state/independent-stock-trader'/folder).rglob('*') if p.is_file())
    guarded = [native.STATUS, ROOT/'controls/stock-trader/operator-intent.txt',
        ROOT/'controls/gameplan-stock-trader/operator-intent.txt', ROOT/'controls/loop-c/current/halt-control.json',
        ROOT/'controls/loop-c/current/risk-approval.json', REPO/'Start-Gameplan-Trader.cmd',
        REPO/'docs/datafetch-ml/start_stock_session.ps1', *claims]
    sources = [REPO/path for path in [
        'datafetching/watchlist.txt', 'datafetching/runtime_lock.py', 'app/services/schwab.py',
        'app/services/schwab_retry.py', 'app/services/schwab_policy_inputs.py',
        'ml/gameplan_trade_snapshot.py', 'ml/stock_trader/horizon_broker.py', 'ml/stock_trader/horizon_ledger.py',
        'ml/stock_trader/contracts.py', 'ml/stock_trader/cross_horizon_fallback.py', 'ml/stock_trader/state.py', 'ml/stock_trader/runtime.py',
        'ml/stock_trader/independent_runtime.py', 'ml/stock_trader/independent_session.py',
        'tests/test_cross_horizon_fallback_ledger.py', 'tests/test_stock_horizon_broker.py', 'tests/test_stock_horizon_ledger.py', 'tests/test_gameplan_trade_snapshot.py']]
    sources += [decision/name for name in ['receipt.json','manifest.json','decisions.json']]
    sources += [gameplan/name for name in ['receipt.json','manifest.json','gameplan.json']]
    sources += [OUT/name for name in ['native_reconcile.py','verify_native_reconciliation.py',
                                    'prepare_reconciliation_plan.py','native-reconciliation-tests.xml']]
    plan = {'schema_version':'source-bound-postclose-reconciliation-preparation-v2',
        'prepared_at':datetime.now(timezone.utc).isoformat(),'source_date':'2026-10-01',
        'supervision_owner':OWNER,'terminal_status_sha256':native.sha(native.STATUS),
        'ledger_sha256':native.sha(native.DB),'ledger_logical_sha256':native.state_hash(saved),
        'latest_saved_snapshot_id':last['id'],'latest_saved_snapshot_at':last['observed_at'],
        'account_fingerprint':account,'symbols':list(native.STOCK_TRADER_SYMBOLS),'pending':pending,
        'guarded_artifact_sha256':{str(path):native.sha(path) for path in guarded},
        'source_artifact_sha256':{str(path):native.sha(path) for path in sources},
        'claim_paths':[str(path) for path in claims],
        'root_authorized_supervised_execution':True,
        'execution_scope':'Native terminal-order reconciliation only; zero budgets; no order actions',
        'fallback_scope':'Preserve exact frozen fallback_days and all reservation request metadata; release only terminal-confirmed unfilled reservation quantity',
        'snapshot_limit':'Saved local state establishes identities, not current broker terminal outcomes'}
    native.check_owner(plan)
    native.check_terminal(plan)
    native.check_ledger(plan)
    native.dump(OUT/'reconciliation-plan.json',plan)
    print(json.dumps({'status':'PREPARED_LOCAL_ONLY','pending_reservations':len(pending),
        'guarded_artifacts':len(guarded),'bound_sources':len(sources),'broker_calls':0,'production_writes':0,
        'plan_sha256':native.sha(OUT/'reconciliation-plan.json')}))


if __name__ == '__main__':
    main()
