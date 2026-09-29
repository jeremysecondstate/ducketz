"""Local-only post-reconciliation baseline and final preservation audit."""
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import argparse
import json
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import native_reconcile as native

ROOT, OUT = native.ROOT, native.OUT
REPO = Path('C:/dev/ducketz')
RUN = ROOT/'ml/overnight-runs/20260929T040727.779758Z'


def windows_tasks():
    command = r"""$items = @(Get-ScheduledTask | Where-Object { $_.TaskName -match 'Ducketz|Gameplan|Loops|OPRA|Directional' } | ForEach-Object {
      $info = $_ | Get-ScheduledTaskInfo
      [ordered]@{ name=$_.TaskName; state=[string]$_.State; task_path=$_.TaskPath; actions=@($_.Actions | Select-Object Execute,Arguments,WorkingDirectory); triggers=@($_.Triggers | Select-Object Enabled,StartBoundary,DaysOfWeek,WeeksInterval); settings=$_.Settings | Select-Object Enabled,StartWhenAvailable,WakeToRun,RestartCount,RestartInterval,MultipleInstances; last_run_time=$info.LastRunTime.ToString('o'); last_task_result=$info.LastTaskResult; next_run_time=$info.NextRunTime.ToString('o') }
    }); ConvertTo-Json -InputObject $items -Depth 8"""
    return json.loads(subprocess.run(['powershell.exe','-NoProfile','-Command',command],check=True,text=True,capture_output=True).stdout)


def processes():
    command = r"""$items = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('python.exe','pythonw.exe','cmd.exe') -and ($_.CommandLine -match 'ml\.|datafetching\.|Start-Gameplan-Trader') } | Select-Object ProcessId,ParentProcessId,CreationDate,Name,CommandLine); ConvertTo-Json -InputObject $items -Depth 4"""
    return json.loads(subprocess.run(['powershell.exe','-NoProfile','-Command',command],check=True,text=True,capture_output=True).stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--final',action='store_true')
    args = parser.parse_args()
    plan = native.read(OUT/'reconciliation-plan.json')
    receipt = native.read(OUT/'native-reconciliation.json')
    verification = native.read(OUT/'native-reconciliation-verification.json')
    protected = native.read(OUT/'controls-and-schedules.json')
    tables = native.table_state(native.DB)
    with native.connect(native.DB) as connection:
        snapshot = native.HorizonLedger._snapshot(connection)
    latest = sorted(tables['snapshots'],key=lambda row:row['observed_at'])[-1]
    portfolio = json.loads(latest['payload'])
    ownership = native._ownership(ROOT,plan['symbols'],plan['account_fingerprint'],portfolio['held_shares'],native.utc().isoformat())
    controls = {path: {'exists':Path(path).is_file(),'sha256':native.sha(Path(path)) if Path(path).is_file() else None}
                for path in protected['controls_and_launcher_hashes']}
    automations = {item['path']:native.sha(Path(item['path'])) for item in protected['automations']}
    claims = {str(path):native.sha(path) for folder in ('entry-slots','quote-recovery-slots')
              for path in sorted((ROOT/'state/independent-stock-trader'/folder).rglob('*')) if path.is_file()}
    current = {'captured_at':datetime.now(timezone.utc).isoformat(),
        'scope':'LOCAL_READ_ONLY_NO_BROKER_PROVIDER_LOCK_OR_PRODUCTION_WRITE',
        'ledger_logical_sha256':native.state_hash(tables),
        'ledger_table_sha256':{name:native.state_hash(rows) for name,rows in tables.items()},
        'ledger_table_counts':{name:len(rows) for name,rows in tables.items()},
        'latest_saved_snapshot':latest, 'native_snapshot':json.loads(json.dumps(asdict(snapshot))), 'planning_ownership':ownership,
        'reservation_counts':dict(Counter(row['status'] for row in tables['reservations'])),
        'controls_environment_launcher_hashes':controls,'automation_sha256':automations,
        'entry_and_recovery_claim_hashes':claims,'windows_tasks':windows_tasks(),
        'terminal_session_sha256':native.sha(native.STATUS),'production_watchlist_sha256':native.sha(REPO/'datafetching/watchlist.txt'),
        'processes':processes(),
        'reconciliation_evidence_sha256':{str(OUT/name):native.sha(OUT/name) for name in
            ['reconciliation-plan.json','native-reconciliation.json','native-reconciliation-verification.json','native-reconciliation-tests.xml']},
        'production_writes':0,'broker_calls':0,'provider_calls':0,'order_actions':0}
    checks = {
        'reconciliation_receipt_completed':receipt['status']=='RECONCILED' and receipt['result']['ready'] is True,
        'independent_reconciliation_verification':verification['status']=='PASS' and len(verification['checks'])==685 and all(verification['checks'].values()),
        'exact_reconciled_ledger':current['ledger_logical_sha256']==receipt['after_logical_sha256']==receipt['rehearsal_logical_sha256'],
        'terminal_three_orders_zero_fills':len(receipt['order_evidence'])==3 and all(row['broker_status']=='CANCELED' and row['status']=='CANCELLED' and row['cumulative_filled_quantity']==0 and not row['fills'] for row in receipt['order_evidence']),
        'no_fill_or_allocation_deltas':receipt['verified_changes']=={'new_fills':0,'new_assignment_releases':0,'owned_share_deltas_by_allocation':{}},
        'zero_order_actions':all(receipt[key]==0 for key in ['orders_submitted','orders_cancelled','orders_replaced']),
        'saved_ownership_ready':latest['ready']==1 and not json.loads(latest['reasons']) and ownership['safe_for_planning'] is True,
        'no_pending_reservations_or_blocks':not snapshot.persistent_blocks and not any(row.status in native.OPEN for row in snapshot.reservations),
        'terminal_session_unchanged':current['terminal_session_sha256']==plan['terminal_status_sha256'],
        'guarded_artifacts_preserved':all(native.sha(Path(path))==digest for path,digest in plan['guarded_artifact_sha256'].items()),
        'reviewed_sources_preserved':all(native.sha(Path(path))==digest for path,digest in plan['source_artifact_sha256'].items()),
        'controls_environment_and_launchers_preserved':all(controls[path]=={key:item[key] for key in ['exists','sha256']} for path,item in protected['controls_and_launcher_hashes'].items()),
        'automations_preserved':automations=={item['path']:item['sha256'] for item in protected['automations']},
        'entry_claims_preserved':claims==protected['entry_and_recovery_claim_hashes'],
        'windows_schedule_preserved':current['windows_tasks']==native.read(OUT/'windows-schedules.json')['tasks'],
        'no_live_or_waiting_stock_trader':not any(any(name in (row.get('CommandLine') or '') for name in ['-m ml.gameplan_stock_trader','-m ml.stock_trader.runtime','-m ml.independent_stock_trader','Start-Gameplan-Trader.cmd']) for row in current['processes']),
    }
    if args.final:
        baseline = native.read(OUT/'post-reconciliation-baseline.json')
        for key in ['ledger_logical_sha256','ledger_table_sha256','ledger_table_counts','latest_saved_snapshot','native_snapshot',
                    'reservation_counts','controls_environment_launcher_hashes','automation_sha256','entry_and_recovery_claim_hashes',
                    'windows_tasks','terminal_session_sha256','production_watchlist_sha256','reconciliation_evidence_sha256']:
            checks['final_unchanged_'+key] = current[key]==baseline[key]
        report_path, receipt_path = RUN/'stage-report.json', RUN/'receipt.json'
        report, overnight = native.read(report_path),native.read(receipt_path)
        checks['native_complete']=report['status']==overnight['status']=='COMPLETE'
        checks['native_report_receipt_binding']=overnight['stage_report_checksum_sha256']==native.sha(report_path) and overnight['stage_report_size']==report_path.stat().st_size
        checks['all_eight_stages_complete']=len(report['stages'])==len(report['stage_order'])==8 and all(s['status']=='COMPLETE' and s['exit_code']==0 for s in report['stages'])
        checks['zero_overnight_orders']=all(p.get('orders_placed')==0 and p.get('broker_orders_enabled') is False for p in [report,overnight])
        checks['original_deadline_retained']=report['deadline_at']==report['effective_deadline_at']=='2026-09-29T11:00:00+00:00' and report['deadline_exception'] is None
        logs = []
        for name,item in overnight['logs'].items():
            path = RUN/name
            matched = native.sha(path)==item['checksum_sha256'] and path.stat().st_size==item['size']
            logs.append({'path':str(path),'verified':matched,'sha256':native.sha(path)})
        checks['native_log_bindings']=bool(logs) and all(row['verified'] for row in logs)
        modules = ['ml.overnight_runtime','datafetching.orchestrate','ml.prediction_runtime','ml.stock_target_history','ml.gameplan_evaluation','ml.nightly_gameplan','ml.stock_trader.independent_training','ml.gameplan_trade_planning','ml.gameplan_actuals_review']
        active = [row for row in current['processes'] if any('-m '+m in (row.get('CommandLine') or '') for m in modules) and not any(flag in (row.get('CommandLine') or '') for flag in ['--claim-supervision','--release-supervision','--status'])]
        checks['no_live_pipeline']=not active
        last_decision = Path(native.read(native.STATUS)['last_cycle']['run_directory']).name
        new_decisions = [str(p) for p in (ROOT/'ml/stock-trader-decision-runs').glob('202609*') if p.is_dir() and p.name>last_decision]
        checks['no_new_stock_decision_runs']=not new_decisions
        current.update(native_completion={'run':str(RUN),'completed_at':report['completed_at'],'receipt_sha256':native.sha(receipt_path),
            'stage_report_sha256':native.sha(report_path),'stage_logs':logs},new_stock_decision_runs=new_decisions,
            baseline_sha256=native.sha(OUT/'post-reconciliation-baseline.json'))
    current.update(status='PASS' if all(checks.values()) else 'REVIEW_REQUIRED',checks=checks,
                   issues=[key for key,value in checks.items() if not value])
    output = OUT/('final-preservation.json' if args.final else 'post-reconciliation-baseline.json')
    native.dump(output,current)
    print(json.dumps({'status':current['status'],'checks':len(checks),'issues':current['issues'],
        'ledger_logical_sha256':current['ledger_logical_sha256'],'reservations':current['reservation_counts'],'output':str(output)}))
    if not all(checks.values()):
        raise SystemExit(1)


if __name__=='__main__':
    main()
