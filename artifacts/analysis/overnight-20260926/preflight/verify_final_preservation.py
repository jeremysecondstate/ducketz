"""Final bounded read-only preservation check; writes only this audit directory."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import subprocess
import sys

sys.dont_write_bytecode=True
REPO=Path('C:/dev/ducketz')
ROOT=Path('C:/DATASTORE')
OUT=Path(__file__).resolve().parent
RUN=ROOT/'ml/overnight-runs/20260926T040723.834511Z'
sys.path.insert(0,str(REPO))
from ml.gameplan_trade_snapshot import _ownership


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


now=datetime.now(timezone.utc).isoformat()
before=read(OUT/'local-preflight.json')
protected=read(OUT/'controls-and-schedules.json')
checks={}
controls=[]
for raw,item in protected['controls_and_launcher_hashes'].items():
    path=Path(raw)
    actual=sha(path) if path.is_file() else None
    controls.append({'path':raw,'before_sha256':item['sha256'],'after_sha256':actual,'unchanged':actual==item['sha256'] and path.is_file()==item['exists']})
checks['controls_environment_and_launchers_unchanged']=all(x['unchanged'] for x in controls)
automations=[]
for item in protected['automations']:
    path=Path(item['path'])
    actual=sha(path) if path.is_file() else None
    automations.append({'name':item['name'],'path':str(path),'before_sha256':item['sha256'],'after_sha256':actual,'unchanged':actual==item['sha256']})
checks['automation_files_unchanged']=all(x['unchanged'] for x in automations)
claims={}
for name in ('entry-slots','quote-recovery-slots'):
    claims.update({str(p):sha(p) for p in sorted((ROOT/'state/independent-stock-trader'/name).rglob('*')) if p.is_file()})
checks['entry_and_recovery_claims_unchanged']=claims==protected['entry_and_recovery_claim_hashes']
checks['windows_schedule_unchanged']=read(OUT/'windows-schedules.json')['tasks']==read(OUT/'final-windows-schedules.json')['tasks']
status_path=ROOT/'state/independent-stock-trader/session-status.json'
checks['terminal_stock_session_bytes_unchanged']=sha(status_path)==before['source_sha256'][str(status_path)]
checks['production_watchlist_bytes_unchanged']=sha(REPO/'datafetching/watchlist.txt')==before['source_sha256'][str(REPO/'datafetching/watchlist.txt')]

db=ROOT/'state/independent-stock-trader/holdings.sqlite3'
assert not Path(str(db)+'-wal').exists() or Path(str(db)+'-shm').exists()
connection=sqlite3.connect(db.as_uri()+'?mode=ro',uri=True,timeout=2)
connection.row_factory=sqlite3.Row
try:
    connection.execute('PRAGMA query_only=ON')
    connection.execute('BEGIN')
    saved=dict(connection.execute('SELECT id,observed_at,ready,reasons,owned,payload FROM snapshots ORDER BY observed_at DESC LIMIT 1').fetchone())
    payload=json.loads(saved.pop('payload'))
    saved['reasons']=json.loads(saved['reasons'])
    saved['owned']=json.loads(saved['owned'])
    counts=[dict(row) for row in connection.execute('SELECT status,count(*) AS count FROM reservations GROUP BY status')]
    pending=[dict(row) for row in connection.execute("SELECT a.symbol,a.horizon,r.side,r.quantity,r.filled,(r.quantity-r.filled) AS remaining,r.status,r.last_evidence_at,a.start,a.end FROM reservations r JOIN allocations a ON a.id=r.allocation WHERE r.status IN ('RESERVED','SUBMITTED','UNKNOWN','WORKING','PARTIAL') ORDER BY a.symbol,a.horizon")]
    blocks=[dict(row) for row in connection.execute('SELECT symbol,reason FROM blocks')]
finally:
    connection.close()
ownership=_ownership(ROOT,before['symbols'],payload['account_fingerprint'],payload['held_shares'],now)
checks.update(saved_ownership_snapshot_unchanged=saved==before['latest_saved_ownership_reconciliation'],
    saved_held_shares_unchanged=payload['held_shares']==before['saved_held_shares'],
    reservation_counts_unchanged=counts==before['reservation_status_counts'],
    pending_reservations_unchanged_empty=pending==before['pending_reservations']==[],
    persistent_blocks_unchanged_empty=blocks==before['persistent_blocks']==[],
    read_only_planning_ownership_consistent=ownership['safe_for_planning'] is True,
    active_horizon_allocations_unchanged=ownership['active_allocations']==before['planning_ownership_check_using_saved_snapshot']['active_allocations'])

process_command="Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('python.exe','pythonw.exe','cmd.exe') -and ($_.CommandLine -match 'ml\\.|datafetching\\.|overnight-20260926|Start-Gameplan-Trader') } | Select-Object ProcessId,ParentProcessId,CreationDate,Name,CommandLine | ConvertTo-Json -Depth 4"
raw=subprocess.run(['powershell.exe','-NoProfile','-Command',process_command],check=True,text=True,capture_output=True).stdout.strip()
processes=json.loads(raw) if raw else []
if isinstance(processes,dict):processes=[processes]
native_modules=['ml.overnight_runtime','datafetching.orchestrate','ml.prediction_runtime','ml.stock_target_history','ml.gameplan_evaluation','ml.nightly_gameplan','ml.stock_trader.independent_training','ml.gameplan_trade_planning','ml.gameplan_actuals_review','ml.gameplan_stock_trader','ml.stock_trader.runtime']
active_native=[]
for process in processes:
    command=process.get('CommandLine') or ''
    if any('-m '+module in command for module in native_modules) and not any(option in command for option in ('--claim-supervision','--release-supervision','--status')):
        active_native.append(process)
checks['no_active_stock_trader_or_overnight_pipeline']=not active_native
unrelated=[p for p in processes if 'ml.hyperliquid_' in (p.get('CommandLine') or '')]

report_path,receipt_path=RUN/'stage-report.json',RUN/'receipt.json'
report,receipt=read(report_path),read(receipt_path)
checks['native_complete']=report['status']==receipt['status']=='COMPLETE'
checks['native_report_receipt_binding']=receipt['stage_report_checksum_sha256']==sha(report_path) and receipt['stage_report_size']==report_path.stat().st_size
checks['all_eight_stages_complete']=len(report['stages'])==len(report['stage_order'])==8 and all(s['status']=='COMPLETE' and s['exit_code']==0 for s in report['stages'])
checks['zero_overnight_orders']=all(p.get('orders_placed')==0 and p.get('broker_orders_enabled') is False for p in (report,receipt))
logs=[]
for name,item in receipt['logs'].items():
    path=RUN/name
    logs.append({'path':str(path),'sha256':sha(path),'matches':sha(path)==item['checksum_sha256'] and path.stat().st_size==item['size']})
checks['native_all_stage_log_bindings_verified']=all(x['matches'] for x in logs)
checks['deadline_unchanged']=report['deadline_at']==report['effective_deadline_at']=='2026-09-28T11:00:00+00:00' and report['deadline_exception'] is None
last_decision=Path(before['terminal_session']['last_cycle']['run_directory']).name
new_decisions=[str(p) for p in (ROOT/'ml/stock-trader-decision-runs').glob('202609*') if p.is_dir() and p.name>last_decision]
checks['no_new_stock_decision_runs_since_terminal_session']=not new_decisions

result={'reviewed_at':now,'status':'PRESERVATION_VERIFIED' if all(checks.values()) else 'PRESERVATION_REVIEW_REQUIRED','checks':checks,'issues':[k for k,v in checks.items() if not v],
    'controls_environment_launchers':controls,'automations':automations,'entry_and_recovery_claim_count':len(claims),'entry_and_recovery_claim_hashes':claims,
    'saved_ownership_snapshot':saved,'saved_held_shares':payload['held_shares'],'reservation_counts':counts,'pending_reservations':pending,'persistent_blocks':blocks,'ownership':ownership,
    'process_snapshot':processes,'active_stock_or_pipeline_processes':active_native,'unrelated_hyperliquid_processes_preserved':unrelated,'new_stock_decision_runs':new_decisions,
    'native_completion':{'run':str(RUN),'completed_at':report['completed_at'],'orders_placed':report['orders_placed'],'broker_orders_enabled':report['broker_orders_enabled'],'receipt_sha256':sha(receipt_path),'stage_report_sha256':sha(report_path),'stage_logs':logs},
    'baseline_evidence':{str(p):sha(p) for p in [OUT/'local-preflight.json',OUT/'controls-and-schedules.json',OUT/'windows-schedules.json']},
    'current_windows_schedule_evidence':{'path':str(OUT/'final-windows-schedules.json'),'sha256':sha(OUT/'final-windows-schedules.json')},
    'limitations':['Unchanged ledger evidence is logical state comparison, not a claim that every SQLite byte was hashed before the run.','Existing unrelated Hyperliquid runtimes were observed and preserved, not audited or controlled.','Root final verification separately checks publication/source/model/planning/actuals correctness.'],
    'production_writes':0,'provider_calls':0,'broker_calls':0,'claim_operations':0}
(OUT/'final-preservation.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'status':result['status'],'checks':checks,'issues':result['issues'],'unrelated_hyperliquid_process_count':len(unrelated),'output':str(OUT/'final-preservation.json')},indent=2))
