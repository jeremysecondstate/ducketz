"""Resolve only completed current native output identities; bounded local reads."""
from pathlib import Path
import hashlib
import json
import pandas as pd
import pyarrow.parquet as pq

ROOT=Path('C:/DATASTORE')
NATIVE=ROOT/'ml/overnight-runs/20260930T040723.733025Z'
GAMEPLAN=ROOT/'ml/nightly-gameplan-runs/20260930T061501.586401Z'
ACTION='2026-09-30'
PRIOR='2026-09-29'
LIMIT=64*1024*1024


def sha(path):
    assert path.stat().st_size<=LIMIT, 'BOUND_FILE_SIZE_EXCEEDED'
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    assert path.stat().st_size<=LIMIT, 'BOUND_JSON_SIZE_EXCEEDED'
    return json.loads(path.read_text(encoding='utf-8-sig'))


def frame(path, maximum_rows=10000):
    assert path.stat().st_size<=LIMIT, 'BOUND_PARQUET_SIZE_EXCEEDED'
    assert pq.read_metadata(path).num_rows<=maximum_rows, 'BOUND_PARQUET_ROW_COUNT_EXCEEDED'
    return pd.read_parquet(path)


def relative(path):
    return path.relative_to(ROOT).as_posix()


def resolve():
    report=read(NATIVE/'stage-report.json')
    receipt=read(NATIVE/'receipt.json')
    assert report['status']==receipt['status']=='COMPLETE', 'NATIVE_NOT_COMPLETE'
    assert receipt['stage_report_checksum_sha256']==sha(NATIVE/'stage-report.json')
    assert receipt['stage_report_size']==(NATIVE/'stage-report.json').stat().st_size
    assert report['enrichment_gameplan']['run_path']==relative(GAMEPLAN)
    assert report['enrichment_gameplan']['action_date']==ACTION
    assert report['enrichment_gameplan']['receipt_sha256']==sha(GAMEPLAN/'receipt.json')
    stages={row['stage']:row for row in report['stages']}
    for name in ['gameplan_trade_planning','gameplan_actuals_review']:
        assert stages[name]['status']=='COMPLETE' and stages[name]['exit_code']==0
    pointers={name:read(ROOT/f'ml/{name}-latest/run.json')['current']
              for name in ['nightly-gameplan','gameplan-trade-plan','gameplan-actuals-review']}
    assert pointers['nightly-gameplan']['run_path']==relative(GAMEPLAN)
    paths={name:ROOT/pointers[name]['run_path'] for name in ['gameplan-trade-plan','gameplan-actuals-review']}
    for name,path in paths.items():
        assert path.resolve().is_relative_to((ROOT/'ml').resolve())
        saved=read(path/'receipt.json')
        assert saved['status']=='COMPLETE' and saved['manifest_sha256']==sha(path/'manifest.json')
        assert pointers[name]['receipt_sha256']==sha(path/'receipt.json')
        child=read(path/'report.json')
        stage=stages[name.replace('-','_').replace('gameplan_trade_plan','gameplan_trade_planning')]
        stamp=child['observed_at'] if name=='gameplan-trade-plan' else child['reviewed_at']
        assert pd.Timestamp(stage['started_at'])<=pd.Timestamp(stamp)<=pd.Timestamp(stage['finished_at'])
    plan=paths['gameplan-trade-plan']; actuals=paths['gameplan-actuals-review']
    assert read(plan/'report.json')['source_gameplan_run']==relative(GAMEPLAN)
    ar=read(actuals/'report.json')
    assert ar['successor_gameplan_run']==relative(GAMEPLAN) and ar['successor_action_date']==ACTION and ar['action_date']==PRIOR
    return GAMEPLAN,plan,actuals,report,pointers
