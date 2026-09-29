"""Bounded saved Loop B live-count diagnosis; no sample scan, fitting or fetch."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import sys

sys.dont_write_bytecode=True
REPO=Path('C:/dev/ducketz')
sys.path.insert(0,str(REPO))
import pandas as pd
from ml.runtime_pipeline import _coherent_weekly_live_bundle_horizons
from ml.calendars import ExchangeSessionCalendar

ROOT=Path('C:/DATASTORE')
OUT=Path(__file__).resolve().parent
RUN=ROOT/'ml/runs/20260929T053846.934376Z'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
expected_weekly=('1w','1w-d1','1w-d2','1w-d3','1w-d4')
assert set(bundles)==set(symbols) and all(tuple(v)==expected_weekly for v in bundles.values())
assert len(live)==88 and live.groupby('symbol').size().eq(8).all()
assert live.groupby('horizon').size().to_dict()=={h:11 for h in ('1h','4h','1d',*expected_weekly)}
omitted=intelligence.loc[intelligence.horizon.eq('1w-d5')]
assert len(intelligence)==99 and intelligence.groupby('symbol').size().eq(9).all()
assert len(omitted)==11 and set(omitted.symbol)==set(symbols)
assert omitted.intelligence_status.eq('NOT_APPLICABLE_TO_REMAINING_WEEK').all()
assert omitted.operational_status.eq('OPERATIONALLY_CURRENT').all()
assert omitted.actionability_status.eq('NOT_ACTIONABLE').all() and omitted.probability_up.isna().all()
assert not intelligence.automated_action_allowed.any()
calendar=ExchangeSessionCalendar('XNYS',start=pd.Timestamp('2026-09-21'),end=pd.Timestamp('2026-10-09'))
sessions=['2026-09-29','2026-09-30','2026-10-01','2026-10-02']
for offset,session in enumerate(sessions,1):
    rows=live.loc[live.horizon.eq('1w-d'+str(offset))]
    assert pd.to_datetime(rows.target_window_start,utc=True).eq(calendar.session_open(pd.Timestamp(session))).all()
    assert pd.to_datetime(rows.target_window_end,utc=True).eq(calendar.session_close(pd.Timestamp(session))).all()
aggregate=live.loc[live.horizon.eq('1w')]
assert pd.to_datetime(aggregate.target_window_start,utc=True).eq(calendar.session_open(pd.Timestamp(sessions[0]))).all()
assert pd.to_datetime(aggregate.target_window_end,utc=True).eq(calendar.session_close(pd.Timestamp(sessions[-1]))).all()
assert pd.to_datetime(omitted.target_window_start,utc=True).eq(calendar.session_open(pd.Timestamp('2026-10-05'))).all()
counts=manifest['configuration']['publication_counts']
assert counts['fresh_live_rows']==88 and counts['actionable_ordinary_routes']==33
assert all(counts[k]==0 for k in ('expired_fresh_live_rows_pruned','carried_active_live_rows','retained_frozen_weekly_live_rows'))
assert manifest['configuration']['route_errors']=={}
result={'reviewed_at':datetime.now(timezone.utc).isoformat(),'status':'VERIFIED_EXPECTED_REMAINING_WEEK_PREFIX',
    'run':str(RUN),'manifest_sha256':sha(RUN/'manifest.json'),'publication_sha256':sha(RUN/'publication.json'),
    'bounded_output_sha256':hashes,'publication_counts':counts,'intelligence_rows':len(intelligence),
    'live_rows_per_horizon':live.groupby('horizon').size().to_dict(),'live_rows_per_symbol':live.groupby('symbol').size().to_dict(),
    'coherent_weekly_prefix_by_symbol':bundles,'remaining_week_sessions':sessions,
    'omitted_suffix':{'horizon':'1w-d5','symbols':sorted(omitted.symbol),'next_target_session':'2026-10-05',
        'status':'NOT_APPLICABLE_TO_REMAINING_WEEK','probabilities_missing_explicitly':True},
    'conclusion':'Monday-source remaining week has four future sessions (Tue–Fri); 55 weekly plus33ordinary LIVE rows equals88. All99intelligence routes remain present. No missing-source or model defect found.',
    'scope':'Saved manifest/publication and two sub-1MB output parquets; pure bundle/calendar checks. No samples, model inference, fitting, provider/broker calls or production mutations.'}
(OUT/'loop-b-weekly-prefix-review.json').write_text(json.dumps(result,indent=2,default=str)+'\n',encoding='utf-8')
print(json.dumps({k:result[k] for k in ('status','intelligence_rows','live_rows_per_horizon','remaining_week_sessions')}))
