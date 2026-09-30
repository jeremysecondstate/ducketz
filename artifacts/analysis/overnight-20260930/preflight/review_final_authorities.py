"""Bounded independent final receipt, pointer and no-trader preservation review."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import subprocess

ROOT=Path('C:/DATASTORE')
OUT=Path(__file__).resolve().parent
RUN=ROOT/'ml/overnight-runs/20260930T040723.733025Z'

def read(path):return json.loads(path.read_text(encoding='utf-8-sig'))
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    baseline=read(OUT/'post-reconciliation-baseline.json')
    preservation=read(OUT/'final-preservation.json')
    native=read(RUN/'stage-report.json');receipt=read(RUN/'receipt.json')
    pin=native['enrichment_gameplan']
    game=ROOT/pin['run_path'];game_receipt=read(game/'receipt.json')
    gp=read(ROOT/'ml/nightly-gameplan-latest/run.json')['current']
    tp=read(ROOT/'ml/gameplan-trade-plan-latest/run.json')['current']
    ap=read(ROOT/'ml/gameplan-actuals-review-latest/run.json')['current']
    ep=read(ROOT/'ml/stock-trader-model-latest/run.json')
    trade=ROOT/tp['run_path'];actuals=ROOT/ap['run_path'];enrichment=ROOT/ep['run_path']
    tr=read(trade/'receipt.json');ar=read(actuals/'receipt.json');er=read(enrichment/'receipt.json')
    actual_report=read(actuals/'report.json');training=read(enrichment/'training-report.json')
    command=r"""$items = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('python.exe','pythonw.exe','cmd.exe') -and ($_.CommandLine -match 'ml\.|datafetching\.|Start-Gameplan-Trader') } | Select-Object ProcessId,ParentProcessId,CreationDate,Name,CommandLine); ConvertTo-Json -InputObject $items -Depth 4"""
    processes=json.loads(subprocess.run(['powershell.exe','-NoProfile','-Command',command],check=True,text=True,capture_output=True).stdout)
    trading=['-m ml.gameplan_stock_trader','-m ml.stock_trader.runtime','-m ml.independent_stock_trader','Start-Gameplan-Trader.cmd']
    pipeline=['-m '+name for name in ['ml.overnight_runtime','datafetching.orchestrate','ml.prediction_runtime','ml.stock_target_history','ml.gameplan_evaluation','ml.nightly_gameplan','ml.stock_trader.independent_training','ml.gameplan_trade_planning','ml.gameplan_actuals_review']]
    current_stock=[p for p in processes if any(m.lower() in (p.get('CommandLine') or '').lower() for m in trading)]
    active_pipeline=[p for p in processes if any(m in (p.get('CommandLine') or '') for m in pipeline) and not any(flag in (p.get('CommandLine') or '') for flag in ['--claim-supervision','--release-supervision','--status'])]
    last=Path(baseline['last_stock_decision_run']).name
    new_decisions=[str(p) for p in (ROOT/'ml/stock-trader-decision-runs').iterdir() if p.is_dir() and p.name>last]
    expected=['loop_a_close_fetch','loop_b_directional_generation','stock_target_history','gameplan_evaluation','gameplan_publication','stock_enrichment_training','gameplan_trade_planning','gameplan_actuals_review']
    checks={
        'preservation_pass':preservation['status']=='PASS' and all(preservation['checks'].values()),
        'native_complete':native['status']==receipt['status']=='COMPLETE',
        'native_report_binding':sha(RUN/'stage-report.json')==receipt['stage_report_checksum_sha256'] and (RUN/'stage-report.json').stat().st_size==receipt['stage_report_size'],
        'all_eight_required_stages_complete':native['stage_order']==expected and [r['stage'] for r in native['stages']]==expected and all(r['status']=='COMPLETE' and r['exit_code']==0 for r in native['stages']),
        'original_deadline_unchanged':native['deadline_at']==native['effective_deadline_at']=='2026-09-30T11:00:00+00:00' and native['deadline_exception'] is None,
        'completed_before_target':datetime.fromisoformat(native['completed_at'])<datetime.fromisoformat('2026-09-30T10:30:00+00:00'),
        'zero_native_order_authority':all(r['orders_placed']==0 and r['broker_orders_enabled'] is False for r in [native,receipt,game_receipt,tr,ar,training]),
        'archive_independent_stock_source_unchanged':native['archive_history'] is True and native['stock_only'] is True and native['independent_stock_horizons'] is True and native['stock_price_source']=='xnas-itch-archive-v1',
        'no_current_stock_trader':not current_stock,
        'no_new_stock_decisions':not new_decisions,
        'no_active_native_pipeline':not active_pipeline,
        'native_pinned_gameplan':sha(game/'receipt.json')==pin['receipt_sha256']==gp['receipt_checksum_sha256'] and pin['run_path']==gp['run_path'] and pin['action_date']=='2026-09-30',
        'gameplan_manifest_binding':sha(game/'manifest.json')==game_receipt['manifest_checksum_sha256']==gp['manifest_checksum_sha256'],
        'gameplan_action_date':game_receipt['action_date']==gp['action_date']=='2026-09-30',
        'trade_receipt_binding':sha(trade/'receipt.json')==tp['receipt_sha256'],
        'trade_manifest_binding':sha(trade/'manifest.json')==tr['manifest_sha256'],
        'trade_pinned_source':tr['source_gameplan_run']==pin['run_path'] and tr['source_receipt_sha256']==tp['source_receipt_sha256']==pin['receipt_sha256'],
        'trade_action_and_rows':tr['action_date']==tp['action_date']=='2026-09-30' and tr['forecast_rows']==264,
        'enrichment_receipt_binding':sha(enrichment/'receipt.json')==ep['receipt_sha256'],
        'enrichment_manifest_binding':sha(enrichment/'manifest.json')==er['manifest_sha256']==ep['manifest_sha256'],
        'enrichment_report_binding':sha(enrichment/'training-report.json')==er['training_report_sha256'],
        'enrichment_pinned_source':training['source_gameplan_run']==pin['run_path'],
        'actuals_receipt_binding':sha(actuals/'receipt.json')==ap['receipt_sha256'],
        'actuals_manifest_binding':sha(actuals/'manifest.json')==ar['manifest_sha256'],
        'actuals_prior_session_date':ar['action_date']==actual_report['action_date']==ap['action_date']=='2026-09-29',
        'actuals_pinned_successor':actual_report['successor_gameplan_run']==pin['run_path'] and actual_report['successor_action_date']=='2026-09-30',
        'actuals_prior_immutable_source':actual_report['source_gameplan_run']=='ml/nightly-gameplan-runs/20260929T061339.593647Z',
        'actuals_dated_pointer':read(ROOT/'ml/gameplan-actuals-review-by-date/2026-09-29/run.json')['current']==ap,
    }
    logs=[]
    for name,record in receipt['logs'].items():
        path=RUN/name
        matches=sha(path)==record['checksum_sha256'] and path.stat().st_size==record['size']
        logs.append({'name':name,'verified':matches,'sha256':sha(path),'bytes':path.stat().st_size})
    checks['all_native_logs_bound']=len(logs)==8 and all(row['verified'] for row in logs)
    result={'reviewed_at':datetime.now(timezone.utc).isoformat(),'status':'PASS' if all(checks.values()) else 'FAIL',
        'scope':'BOUNDED_INDEPENDENT_RECEIPT_POINTER_PROCESS_AND_PRESERVATION_REVIEW_NO_RECONSTRUCTION',
        'checks':checks,'issues':[k for k,v in checks.items() if not v],'native_run':str(RUN),'completed_at':native['completed_at'],
        'gameplan':str(game),'trade_plan':str(trade),'actuals':str(actuals),'enrichment':str(enrichment),
        'readable_gameplan':str(trade/'Gameplan.md'),'native_log_bindings':logs,'processes':processes,
        'new_stock_decision_runs':new_decisions,'active_stock_traders':current_stock,'active_native_pipeline':active_pipeline,
        'concurrent_changes':preservation['concurrent_change_deltas'],
        'limitation':'Archive reconstruction, model metrics, row/source quality, cash conservation and actuals outcomes are verified separately by final_audit.',
        'source_sha256':{str(p):sha(p) for p in [OUT/'final-preservation.json',RUN/'receipt.json',game/'receipt.json',trade/'receipt.json',actuals/'receipt.json',enrichment/'receipt.json']},
        'orders_placed':0,'broker_calls':0,'provider_calls':0,'production_writes':0}
    (OUT/'final-authorities-peer-review.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'status':result['status'],'checks':len(checks),'issues':result['issues'],'completed_at':native['completed_at']}))
    if result['status']!='PASS':raise SystemExit(1)

if __name__=='__main__':main()
