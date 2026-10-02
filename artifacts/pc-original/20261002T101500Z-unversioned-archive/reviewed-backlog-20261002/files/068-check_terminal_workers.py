"""Read-only exact process-token and terminal receipt audit; no supervision calls."""
from pathlib import Path
from datetime import datetime,timezone
import hashlib
import json
import psutil

ROOT=Path('<LOCAL_DATASTORE>')
OUT=Path(__file__).resolve().parent
RUN=ROOT/'ml/overnight-runs/20261001T040644.667536Z'
TRADER={'ml.gameplan_stock_trader','ml.independent_stock_trader','ml.stock_trader.runtime'}
PIPELINE={'ml.overnight_runtime','datafetching.orchestrate','ml.prediction_runtime','ml.stock_target_history',
          'ml.gameplan_evaluation','ml.nightly_gameplan','ml.stock_trader.independent_training',
          'ml.gameplan_trade_planning','ml.gameplan_actuals_review'}
CONTROL={'--claim-supervision','--release-supervision','--status'}


def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def main():
    workers=[];unreadable=[];control_count=0
    for proc in psutil.process_iter(['pid','ppid','name']):
        name=(proc.info['name'] or '').lower()
        if name not in {'python.exe','pythonw.exe','cmd.exe','pwsh.exe','powershell.exe'}:continue
        try:
            args=proc.cmdline()
            module=args[args.index('-m')+1] if '-m' in args and args.index('-m')+1<len(args) else None
            control=module=='ml.overnight_runtime' and any(flag in args for flag in CONTROL)
            if control:control_count+=1
            explicit_launcher=any(Path(arg.strip('"')).name.lower() in {'start-gameplan-trader.cmd','start_stock_session.ps1'} for arg in args)
            if not control and (module in TRADER|PIPELINE or explicit_launcher):
                workers.append({'pid':proc.pid,'parent_pid':proc.ppid(),'name':name,'module':module,
                                'explicit_trader_launcher':explicit_launcher,'created_at_unix':proc.create_time()})
        except (psutil.NoSuchProcess,psutil.ZombieProcess):continue
        except psutil.AccessDenied:unreadable.append({'pid':proc.pid,'name':name})
    status_path=ROOT/'state/independent-stock-trader/session-status.json'
    status=json.loads(status_path.read_text())
    report=json.loads((RUN/'stage-report.json').read_text())
    receipt=json.loads((RUN/'receipt.json').read_text())
    preserve=json.loads((OUT/'preservation-final.json').read_text())
    checks={'no_native_pipeline_or_trader_worker':not workers,
            'all_relevant_process_commands_readable':not unreadable,
            'september30_trader_terminal':status.get('status')=='FINISHED' and status.get('action_date')=='2026-09-30',
            'no_native_trader_session_or_cycle_lock':not (ROOT/'locks/independent-stock-session.lock').exists() and not (ROOT/'locks/stock-trader-hourly.lock').exists(),
            'native_report_and_receipt_complete':report['status']==receipt['status']=='COMPLETE',
            'native_eight_stages_complete':len(report['stages'])==len(report['stage_order'])==8 and all(s['status']=='COMPLETE' and s['exit_code']==0 for s in report['stages']),
            'native_report_receipt_checksum':sha(RUN/'stage-report.json')==receipt['stage_report_checksum_sha256'] and (RUN/'stage-report.json').stat().st_size==receipt['stage_report_size'],
            'native_zero_order_authority':all(s['orders_placed']==0 and s['broker_orders_enabled'] is False for s in [report,receipt]),
            'final_preservation_passed':preserve['status']=='PASS' and all(preserve['checks'].values()),
            'repository_sources_unchanged_from_baseline':not preserve['repository_source_deltas_requiring_attribution']}
    result={'reviewed_at':datetime.now(timezone.utc).isoformat(),'status':'PASS' if all(checks.values()) else 'REVIEW_REQUIRED',
            'scope':'READ_ONLY_EXACT_MODULE_OR_LAUNCHER_TOKEN_MATCHING; full command text is neither persisted nor substring matched.',
            'checks':checks,'native_and_trader_workers':workers,'unreadable_relevant_processes':unreadable,
            'observed_supervision_status_command_count':control_count,
            'trader_status':status['status'],'trader_action_date':status['action_date'],
            'native_completed_at':report.get('completed_at'),
            'bindings':{str(p):{'sha256':sha(p),'bytes':p.stat().st_size} for p in [status_path,RUN/'stage-report.json',RUN/'receipt.json',OUT/'preservation-final.json']},
            'no_claim_or_lock_operations':True,'broker_calls':0,'provider_calls':0,'production_writes':0,'order_actions':0}
    with (OUT/'terminal-worker-verification.json').open('x',encoding='utf-8') as stream:json.dump(result,stream,indent=2);stream.write('\n')
    print(json.dumps({key:result[key] for key in ['reviewed_at','status','checks','native_and_trader_workers','unreadable_relevant_processes','native_completed_at']},indent=2))
    if not all(checks.values()):raise SystemExit(1)


if __name__=='__main__':main()
