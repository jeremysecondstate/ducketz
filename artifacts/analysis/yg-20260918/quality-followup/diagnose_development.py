"""Descriptive train/development audit; never fit or score final assessment."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import joblib
import numpy as np
import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[4]))
from ml.nightly_gameplan import _model_frame, _proper_scores

RUN=Path('C:/DATASTORE/ml/nightly-gameplan-runs/20260918T072555.813034Z')
OUT=Path(__file__).resolve().parent
GROUPS=('1h','1w')
DEV=('train','selection','calibration')


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bounded_parts(path):
    # Only label-free clocks are read for the global partition boundaries.
    clocks=pd.read_parquet(path,columns=['decision_timestamp','target_window_start'])
    clusters=pd.Index(clocks.decision_timestamp.unique()).sort_values()
    holdout=min(63,max(10,len(clusters)//8))
    if len(clusters)-3*holdout<20:
        holdout=max(5,(len(clusters)-20)//3)
    ends=[len(clusters)-3*holdout,len(clusters)-2*holdout,len(clusters)-holdout]
    ranges={'train':clusters[:ends[0]],'selection':clusters[ends[0]:ends[1]],
        'calibration':clusters[ends[1]:ends[2]],'assessment':clusters[ends[2]:]}
    assessment_first=ranges['assessment'][0]
    # No assessment labels or feature values are admitted to the audit frame.
    frame=pd.read_parquet(path,filters=[('decision_timestamp','<',assessment_first)])
    result={}
    for left,right in zip(DEV,('selection','calibration','assessment')):
        boundary=clocks.loc[clocks.decision_timestamp.isin(ranges[right]),'target_window_start'].min()
        result[left]=frame[frame.decision_timestamp.isin(ranges[left]) & frame.target_window_end.lt(boundary)].copy().reset_index(drop=True)
    return result,assessment_first


def timing(frame):
    end=pd.to_datetime(frame.source_bar_end_timestamp,utc=True)
    regular=pd.to_datetime(frame.source_regular_close,utc=True)
    action=pd.to_datetime(frame.source_action_start,utc=True)
    return pd.DataFrame({'source__hours_after_regular_close':(end-regular).dt.total_seconds()/3600,
        'source__hours_until_action_start':(action-end).dt.total_seconds()/3600},index=frame.index)


def grouped(frame,key):
    return {str(k):{'rows':int(len(v)),'positive_rate':float(v.target.mean()),
        'action_sessions':int(v.action_date.nunique())}
        for k,v in frame.groupby(key,observed=True)}


reports=read(RUN/'model-reports.json')
result={'created_at':datetime.now(timezone.utc).isoformat(),'run_path':str(RUN),
    'scope':'Train/selection/calibration only. No assessment feature values, labels, predictions, or scores admitted; no fit, fetch, broker, control or production changes.',
    'input_hashes':{},'groups':{},
    'proposed_timing_family':{'status':'Design proposal only; not implemented or fitted',
        'names':['source__hours_after_regular_close','source__hours_until_action_start'],
        'derivations':{'source__hours_after_regular_close':'(source_bar_end_timestamp - source_regular_close).total_seconds()/3600',
            'source__hours_until_action_start':'(source_action_start - source_bar_end_timestamp).total_seconds()/3600'},
        'availability':'All three timestamps are already saved in every cohort and current forecast source row. Identical deterministic derivation can run before fitting and inference, without another fetch. Target outcomes and prices are not read.',
        'method':'Descriptive association and saved-estimator calibration predictions only. No claim of measured candidate improvement.'}}
for group in GROUPS:
    path=RUN/f'training-cohort-{group}.parquet'
    result['input_hashes'][path.name]=sha(path)
    parts,assessment_first=bounded_parts(path)
    report=reports[group]
    features=report['features']['admitted']
    record={'assessment_first_source_timestamp_excluded':assessment_first.isoformat(),
        'numeric_feature_count':len(features),'numeric_features':features,
        'categorical_features':['symbol','route'],'partitions':{},
        'saved_development_selection_metrics':report['selection_metrics'],
        'saved_calibration_selection':report['calibration_selection']}
    for name,frame in parts.items():
        unique=frame.drop_duplicates(['symbol','action_date'])
        end_hour=pd.to_datetime(unique.source_bar_end_timestamp,utc=True).dt.tz_convert('America/Los_Angeles').dt.hour
        missing=frame[features].isna().mean()
        proposed=timing(frame)
        record['partitions'][name]={'rows':len(frame),'recorded_rows_match':len(frame)==report['partitions'][f'{name}_rows'],
            'source_decision_clusters':int(frame.decision_timestamp.nunique()),'action_sessions':int(frame.action_date.nunique()),
            'unique_symbol_source_sessions':len(unique),'action_date_start':str(frame.action_date.min()),'action_date_end':str(frame.action_date.max()),
            'positive_rate':float(frame.target.mean()),'by_symbol':grouped(frame,'symbol'),'by_route':grouped(frame,'route'),
            'source_end_hour_counts_unique_snapshots':{str(k):int(v) for k,v in end_hour.value_counts().sort_index().items()},
            '17_Pacific_snapshot_fraction':float(end_hour.eq(17).mean()),
            'feature_missing_fraction':{str(k):float(v) for k,v in missing.items()},
            'missing_close_location_exactly_zero_range':bool(frame.bar__close_location.isna().eq(frame.bar__intrabar_range_atr.eq(0)).all()),
            'proposed_timing_features':{k:{'minimum':float(proposed[k].min()),'maximum':float(proposed[k].max()),'unique_values':int(proposed[k].nunique()),'finite':bool(np.isfinite(proposed[k]).all())} for k in proposed},
            'raw_label_integrity':bool(frame.target.eq(frame.observed_return.gt(0).astype(int)).all()),
            'missing_assessment':bool(frame.decision_timestamp.lt(assessment_first).all())}
    corr=parts['train'][features].corr()
    record['train_feature_correlations_abs_above_0_95']=[{'left':left,'right':right,'correlation':float(corr.loc[left,right])}
        for i,left in enumerate(features) for right in features[i+1:] if abs(corr.loc[left,right])>.95]
    # Existing final model is trained only on train+selection; calibration remains development.
    payload=joblib.load(RUN/report['model_file']['path'])
    frame=parts['calibration'].copy()
    raw=payload['estimator'].predict_proba(_model_frame(frame,payload['feature_columns'],payload['categorical_columns']))[:,1]
    frame['raw_probability']=raw
    frame['source_end_hour']=pd.to_datetime(frame.source_bar_end_timestamp,utc=True).dt.tz_convert('America/Los_Angeles').dt.hour
    record['unfitted_calibration_diagnostics_by_source_clock']={}
    for hour,rows in frame.groupby('source_end_hour'):
        probability=rows.raw_probability.to_numpy()
        logits=np.log(probability/(1-probability))
        target=rows.target.to_numpy()
        record['unfitted_calibration_diagnostics_by_source_clock'][str(hour)]={'rows':len(rows),
            'unique_symbol_source_sessions':int(rows[['symbol','action_date']].drop_duplicates().shape[0]),
            'action_sessions':int(rows.action_date.nunique()),'positive_rate':float(target.mean()),
            'raw_probability_mean':float(probability.mean()),
            'logit_label_covariance':float(np.mean((logits-logits.mean())*(target-target.mean()))),
            'saved_estimator_raw_log_loss':_proper_scores(target,probability)['log_loss']}
    result['groups'][group]=record
current=pd.read_parquet(RUN/'forecasts.parquet',columns=['symbol','source_bar_end_timestamp','source_regular_close','source_action_start'])
current=current.drop_duplicates('symbol').reset_index(drop=True)
current=pd.concat([current,timing(current)],axis=1)
result['proposed_timing_family']['current_forecast_derivation']=json.loads(current.to_json(orient='records',date_format='iso'))
result['source_definition']={'path':'C:/dev/ducketz/technicals/calculations/bar_shape.py:85',
    'close_location':'(close-low)/(high-low), with zero range made NaN; exact matches zero intrabar_range_atr in all inspected development partitions.',
    'imputation':'Native estimator applies median imputation with add_indicator=True, trained within fitting data, then scaling for logistic/MLP. Missingness is retained, not fabricated as a price or removed.'}
result['conclusions']=[
    'No corrupted labels, causal price-boundary defect, lost categorical route, or unhandled missing-feature defect identified in the inspected train/development scope.',
    'Weekly has only 1561 train rows across 350 action sessions, 174 selection rows across 22 sessions and 198 calibration rows across 26 sessions. Six TWST fit rows and no TWST selection/calibration rows limit symbol-specific evidence. Overlapping weekly returns and multiple symbols reduce effective independence further; feature-time cluster counts are not session counts.',
    'Strong exact and near-exact train feature redundancy plus the small weekly cohort justify the same fixed lower-C logistic grid already used in other horizons. The observed train-to-development base-rate change supports conservative complexity; it does not authorize threshold weakening or selecting by final assessment.',
    'Flat calibration is an explicit development candidate eligibility mismatch with a downstream varying-probability requirement. Aligning candidate eligibility with that existing requirement before development score comparison is a fixed design repair; preserve the held-out gate and fail honestly if no eligible varying candidate exists.',
    'Source-bar timing is omitted explicit causal context. Training and development use a different mix of regular-close versus 17:00 features, with zero-range feature missingness following that shift. The proposed two fixed timing features are fully derivable from saved data; no acquisition is needed.',
    'Calibration-only source-clock associations are mixed and thin in early-hour strata; they do not establish that adding source timing improves unseen results. Do not add a late assessment-driven feature search to the present minimal repair. A prospective development experiment may be justified later.',
    'A promise that every model will pass cannot be justified from these data. Keep the source/price/sample/promotion gates intact and preserve a failing result if the predeclared development-selected repair still fails.'
]
(OUT/'model-diagnosis.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n',encoding='utf-8')
lines=['# YG train/development model diagnosis','',f"Created {result['created_at']}. `{RUN}`.",'',result['scope'],'',
    '| Group / partition | Rows | Feature-time clusters | Action sessions | Positive rate | Source snapshots at 17:00 PT | Missing close-location |',
    '|---|---:|---:|---:|---:|---:|---:|']
for group,record in result['groups'].items():
    for name,v in record['partitions'].items():
        lines.append(f"| {group} / {name} | {v['rows']} | {v['source_decision_clusters']} | {v['action_sessions']} | {v['positive_rate']:.2%} | {v['17_Pacific_snapshot_fraction']:.2%} | {v['feature_missing_fraction']['bar__close_location']:.2%} |")
lines+=['','## Source and feature findings','',
    'All other admitted numeric features are complete in the audited partitions. Missing bar close-location exactly equals zero intrabar range in every audited partition; the source formula deliberately leaves zero-range division undefined. Native median imputation preserves an explicit missingness indicator. This is observable market-feature behavior, not a broken fetch or invented price.', '',
    'Target entry-clock, weekday, elapsed, trading and closed-market hours are already included. Source bar end-time and age relative to the forecast action are not explicit estimator inputs. Both proposed source timing quantities use already-frozen source timestamps and can be computed identically at training and inference without prices, labels, or a new fetch.', '',
    '`source__hours_after_regular_close = (source_bar_end_timestamp - source_regular_close) / 1 hour`', '',
    '`source__hours_until_action_start = (source_action_start - source_bar_end_timestamp) / 1 hour`', '',
    'The saved-estimator hourly calibration covariance is positive for 14:00/15:00 source bars and negative for 16:00/17:00 bars; the early strata have only 13/11 unique symbol-session snapshots. Weekly 17:00 source bars have a 40% positive rate while predicted raw probability averages 57.0%, but earlier strata are very small. These are development diagnostics, not a demonstrated new-feature model gain.', '',
    'Exact train redundancies include range-position/range-score and the three direction/upside/downside-pressure variables. Weekly trend and momentum score pairs correlate above 0.97; elapsed duration and closed-market hours correlate 0.9974. Features are causal, but the data do not justify interpreting all 32 numeric weekly inputs as independent information.', '',
    '## Recommendation','']
lines += [f'- {text}' for text in result['conclusions']]
lines += ['','Evidence and all per-symbol/route/clock counts are retained in `model-diagnosis.json`. This script performed prediction only on saved calibration development rows; it never fitted a candidate or evaluated final assessment.']
(OUT/'model-diagnosis.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
print(json.dumps({'written':[str(OUT/'model-diagnosis.md'),str(OUT/'model-diagnosis.json')],
    'partition_counts_match':all(p['recorded_rows_match'] for g in result['groups'].values() for p in g['partitions'].values()),
    'assessment_excluded':all(p['missing_assessment'] for g in result['groups'].values() for p in g['partitions'].values())}))
