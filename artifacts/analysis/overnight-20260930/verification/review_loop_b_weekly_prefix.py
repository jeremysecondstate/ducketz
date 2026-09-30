"""Bounded saved Loop B live-count diagnosis; no sample scan, fitting or fetch."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import argparse
import sys

sys.dont_write_bytecode=True
REPO=Path('C:/dev/ducketz')
sys.path.insert(0,str(REPO))
import pandas as pd
from ml.runtime_pipeline import _coherent_weekly_live_bundle_horizons
from ml.calendars import ExchangeSessionCalendar

ROOT=Path('C:/DATASTORE')
OUT=Path(__file__).resolve().parent
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--overnight-run',type=Path,default=ROOT/'ml/overnight-runs/20260930T040723.733025Z')
args=parser.parse_args()


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


native=read(args.overnight_run/'stage-report.json')
receipt=read(args.overnight_run/'receipt.json') if (args.overnight_run/'receipt.json').exists() else {}
if native.get('status')!='COMPLETE' or receipt.get('status')!='COMPLETE':
    print(json.dumps({'status':'NOT_READY_FOR_FINAL_VERIFICATION','heavy_checks_started':False}))
    raise SystemExit(2)
from audit_environment import verify_environment
verify_environment()
assert receipt['stage_report_checksum_sha256']==sha(args.overnight_run/'stage-report.json')
assert pd.Timestamp(native['deadline_at'])==pd.Timestamp('2026-09-30T11:00:00Z')
assert pd.Timestamp(native.get('effective_deadline_at',native['deadline_at']))==pd.Timestamp(native['deadline_at'])
assert native.get('deadline_exception') is None
game=ROOT/native['enrichment_gameplan']['run_path']
assert sha(game/'receipt.json')==native['enrichment_gameplan']['receipt_sha256']
game_receipt=read(game/'receipt.json')
assert game_receipt['manifest_checksum_sha256']==sha(game/'manifest.json')
game_manifest=read(game/'manifest.json')
assert game_receipt['action_date']==game_manifest['configuration']['action_date']=='2026-09-30'
assert pd.Timestamp(game_receipt['published_at'])<pd.Timestamp(native['deadline_at'])
assert game_receipt['source_loop_b_run']==game_manifest['configuration']['source_loop_b_run']
RUN=(ROOT/game_manifest['configuration']['source_loop_b_run']).resolve()
assert RUN.parent==(ROOT/'ml/runs').resolve()
manifest,publication=read(RUN/'manifest.json'),read(RUN/'publication.json')
assert publication['manifest_checksum_sha256']==sha(RUN/'manifest.json')
frames={}
hashes={}
for name in ('predictions.parquet','intelligence.parquet'):
    path=RUN/name
    assert path.stat().st_size<1000000
    saved=manifest['output_files'][name]
    assert saved['checksum_sha256']==sha(path) and saved['size']==path.stat().st_size
    frames[name]=pd.read_parquet(path)
    hashes[name]=sha(path)
predictions,intelligence=frames['predictions.parquet'],frames['intelligence.parquet']
symbols=manifest['configuration']['symbols']
live=predictions.loc[predictions.prediction_mode.eq('LIVE')]
bundles=_coherent_weekly_live_bundle_horizons(predictions)
calendar=ExchangeSessionCalendar('XNYS',start=pd.Timestamp('2026-09-21'),end=pd.Timestamp('2026-10-09'))
source=pd.Timestamp('2026-09-29')
future=[pd.Timestamp(s) for s in calendar.sessions if s>source][:5]
sessions=[s.date().isoformat() for s in future if s.isocalendar().week==source.isocalendar().week]
assert sessions==['2026-09-30','2026-10-01','2026-10-02']
expected_weekly=('1w',*('1w-d'+str(i) for i in range(1,len(sessions)+1)))
assert set(bundles)==set(symbols) and all(tuple(v)==expected_weekly for v in bundles.values())
expected_live=len(symbols)*(3+len(expected_weekly))
assert len(live)==expected_live and live.groupby('symbol').size().eq(3+len(expected_weekly)).all()
assert live.groupby('horizon').size().to_dict()=={h:11 for h in ('1h','4h','1d',*expected_weekly)}
omitted_horizons=['1w-d'+str(i) for i in range(len(sessions)+1,6)]
omitted=intelligence.loc[intelligence.horizon.isin(omitted_horizons)]
assert len(intelligence)==99 and intelligence.groupby('symbol').size().eq(9).all()
assert len(omitted)==len(omitted_horizons)*len(symbols) and set(omitted.symbol)==set(symbols)
assert omitted.intelligence_status.eq('NOT_APPLICABLE_TO_REMAINING_WEEK').all()
assert omitted.operational_status.eq('OPERATIONALLY_CURRENT').all()
assert omitted.actionability_status.eq('NOT_ACTIONABLE').all() and omitted.probability_up.isna().all()
assert not intelligence.automated_action_allowed.any()
for offset,session in enumerate(sessions,1):
    rows=live.loc[live.horizon.eq('1w-d'+str(offset))]
    assert pd.to_datetime(rows.target_window_start,utc=True).eq(calendar.session_open(pd.Timestamp(session))).all()
    assert pd.to_datetime(rows.target_window_end,utc=True).eq(calendar.session_close(pd.Timestamp(session))).all()
aggregate=live.loc[live.horizon.eq('1w')]
assert pd.to_datetime(aggregate.target_window_start,utc=True).eq(calendar.session_open(pd.Timestamp(sessions[0]))).all()
assert pd.to_datetime(aggregate.target_window_end,utc=True).eq(calendar.session_close(pd.Timestamp(sessions[-1]))).all()
for offset in range(len(sessions)+1,6):
    rows=omitted.loc[omitted.horizon.eq('1w-d'+str(offset))]
    assert pd.to_datetime(rows.target_window_start,utc=True).eq(calendar.session_open(future[offset-1])).all()
    assert pd.to_datetime(rows.target_window_end,utc=True).eq(calendar.session_close(future[offset-1])).all()
counts=manifest['configuration']['publication_counts']
assert counts['fresh_live_rows']==expected_live and counts['actionable_ordinary_routes']==33
assert all(counts[k]==0 for k in ('expired_fresh_live_rows_pruned','carried_active_live_rows','retained_frozen_weekly_live_rows'))
assert manifest['configuration']['route_errors']=={}
result={'reviewed_at':datetime.now(timezone.utc).isoformat(),'status':'VERIFIED_EXPECTED_REMAINING_WEEK_PREFIX',
    'run':str(RUN),'manifest_sha256':sha(RUN/'manifest.json'),'publication_sha256':sha(RUN/'publication.json'),
    'bounded_output_sha256':hashes,'publication_counts':counts,'intelligence_rows':len(intelligence),
    'live_rows_per_horizon':live.groupby('horizon').size().to_dict(),'live_rows_per_symbol':live.groupby('symbol').size().to_dict(),
    'coherent_weekly_prefix_by_symbol':bundles,'remaining_week_sessions':sessions,
    'omitted_suffix':{'horizons':omitted_horizons,'symbols':sorted(omitted.symbol.unique()),'target_sessions':[s.date().isoformat() for s in future[len(sessions):]],
        'status':'NOT_APPLICABLE_TO_REMAINING_WEEK','probabilities_missing_explicitly':True},
    'conclusion':'Tuesday-source remaining week has three future sessions (Wed–Fri); 44 weekly plus 33 ordinary LIVE rows equals 77. All 99 intelligence routes remain present. No missing-source or model defect found.',
    'scope':'Saved manifest/publication and two sub-1MB output parquets; pure bundle/calendar checks. No samples, model inference, fitting, provider/broker calls or production mutations.'}
(OUT/'loop-b-weekly-prefix-review.json').write_text(json.dumps(result,indent=2,default=str)+'\n',encoding='utf-8')
print(json.dumps({k:result[k] for k in ('status','intelligence_rows','live_rows_per_horizon','remaining_week_sessions')}))
