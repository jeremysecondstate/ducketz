"""Read-only native stage observer and completed-artifact audit dispatcher.

Root separately owns supervision. This helper never starts/resumes/stops a
pipeline or calls a provider/broker; it runs only the reviewed offline audits.
"""
from pathlib import Path
from datetime import datetime, timezone
import json
import subprocess
import sys
import time

OUT=Path(__file__).resolve().parent
REPO=Path('C:/dev/ducketz')
NATIVE=Path('C:/DATASTORE/ml/overnight-runs/20260930T040723.733025Z')
STAGES=['loop_a_close_fetch','loop_b_directional_generation','stock_target_history',
        'gameplan_evaluation','gameplan_publication','stock_enrichment_training',
        'gameplan_trade_planning','gameplan_actuals_review']
result={'started_at':datetime.now(timezone.utc).isoformat(),'native_run':str(NATIVE),
        'status':'WAITING_FOR_PINNED_STAGES','checks':[], 'stage_observations':[],
        'provider_calls':0,'broker_calls':0,'production_mutations':0,'orders_placed':0}


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def save():
    result['updated_at']=datetime.now(timezone.utc).isoformat()
    (OUT/'review-watch.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')


def audit(script,arguments=()):
    record={'script':script,'started_at':datetime.now(timezone.utc).isoformat()}
    result['checks'].append(record)
    result['status']='RUNNING_'+script
    save()
    print('REVIEW_START '+json.dumps(record),flush=True)
    process=subprocess.run([sys.executable,'-B',str(OUT/script),*map(str,arguments)],cwd=REPO,check=False)
    record.update(exit_code=process.returncode,finished_at=datetime.now(timezone.utc).isoformat())
    save()
    print('REVIEW_END '+json.dumps(record),flush=True)
    if process.returncode:
        raise RuntimeError('REVIEW_FAILED '+script)


done=set()
try:
    while True:
        report=read(NATIVE/'stage-report.json')
        if report.get('stage_order')!=STAGES or report.get('resumed_from') is not None:
            raise RuntimeError('NATIVE_STAGE_OR_ANCESTRY_CHANGED_REQUIRES_ROOT_REVIEW')
        key=(report.get('status'),report.get('current_stage'))
        if not result['stage_observations'] or key!=tuple(result['stage_observations'][-1]['state']):
            item={'at':datetime.now(timezone.utc).isoformat(),'state':list(key)}
            result['stage_observations'].append(item)
            print('NATIVE_STAGE '+json.dumps(item),flush=True)
        result['native_heartbeat_at']=report.get('heartbeat_at')
        completed={item['stage'] for item in report.get('stages',[]) if item.get('status')=='COMPLETE' and item.get('exit_code')==0}
        if report.get('enrichment_gameplan'):
            for stage,script in [('gameplan_publication','preliminary_directional_review.py'),
                                 ('stock_enrichment_training','preliminary_sizing_review.py')]:
                if stage in completed and stage not in done:
                    audit(script)
                    done.add(stage)
        if report.get('status')=='COMPLETE':
            receipt=read(NATIVE/'receipt.json') if (NATIVE/'receipt.json').exists() else {}
            if receipt.get('status')=='COMPLETE':
                if completed!=set(STAGES):
                    raise RuntimeError('COMPLETE_NATIVE_MISSING_SUCCESSFUL_STAGE')
                audit('run_final_checks.py',['--overnight-run',NATIVE])
                result['status']='ALL_COMPLETED_ARTIFACT_REVIEWS_FINISHED'
                save()
                break
        elif report.get('status') in {'FAILED','CANCELLED'}:
            raise RuntimeError('NATIVE_TERMINAL_FAILURE_REQUIRES_ROOT_REVIEW')
        result['status']='WAITING_FOR_PINNED_STAGES'
        save()
        time.sleep(20)
except Exception as exc:
    result.update(status='STOPPED_REQUIRES_REVIEW',error=type(exc).__name__+': '+str(exc))
    save()
    print(json.dumps(result),flush=True)
    raise SystemExit(1) from None
