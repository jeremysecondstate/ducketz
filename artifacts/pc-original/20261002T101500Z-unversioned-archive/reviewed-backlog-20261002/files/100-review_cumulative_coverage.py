"""Bounded local read-only diagnosis of cumulative saved-forecast coverage.

Reads immutable forecast/evaluation outputs and only their checksum-bound recent
XNAS minute partitions. No training, evaluator rerun, provider or broker calls.
"""
from __future__ import annotations
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
import hashlib
import json
import sys

sys.dont_write_bytecode=True
REPO=Path('<LOCAL_CHECKOUT>');ROOT=Path('<LOCAL_DATASTORE>')
OUT=Path(__file__).resolve().parent
RUN=ROOT/'ml/gameplan-evaluation-runs/20261001T064116.865175Z'
sys.path.insert(0,str(REPO))
import pandas as pd
from ml.gameplan_actuals_review import _boundary_evidence
from ml.stock_target_prices import independent_price_identity

bindings={};checks={}


def sha(p):
    with Path(p).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))


def bind(p,expected=None):
    p=Path(p); value={'sha256':sha(p),'bytes':p.stat().st_size}
    if expected is not None:
        assert value['sha256']==expected['checksum_sha256'] and value['bytes']==expected['size'],str(p)
    bindings[str(p)]=value
    return value


def verify_run(run,names):
    m,r=read(run/'manifest.json'),read(run/'receipt.json')
    assert sha(run/'manifest.json')==r.get('manifest_checksum_sha256',r.get('manifest_sha256'))
    bind(run/'manifest.json');bind(run/'receipt.json')
    for name in names:bind(run/name,m['output_files'][name])
    return m,r


def grouped(frame,cols):
    return [{**{k:str(v) for k,v in zip(cols,key if isinstance(key,tuple) else (key,))},'rows':int(value)}
            for key,value in frame.groupby(cols,dropna=False).size().items()]


def clean(value):
    if isinstance(value,dict):return {str(k):clean(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [clean(v) for v in value]
    if isinstance(value,pd.Timestamp):return value.isoformat()
    if hasattr(value,'item'):return clean(value.item())
    return value


def main():
    m,r=verify_run(RUN,['evaluations.parquet','summary.json'])
    summary=read(RUN/'summary.json');now=pd.Timestamp(summary['observed_at'])
    frame=pd.read_parquet(RUN/'evaluations.parquet')
    missing=frame[frame.evaluation_status.eq('MATURE_AWAITING_DATA')].copy()
    pending=frame[frame.evaluation_status.eq('PENDING_MATURITY')].copy()
    checks['saved_totals_reproduced']=len(frame)==6096 and len(missing)==1063 and len(pending)==165 and int(frame.evaluation_status.eq('EVALUATED').sum())==4868
    checks['all_missing_matured_by_saved_clock']=bool((missing.target_window_end<=now).all())
    checks['all_pending_are_future_at_saved_clock']=bool((pending.target_window_end>now).all())
    inputs={str((ROOT/item['path']).resolve()):item for item in m['input_files']}
    forecasts=[];universes=[]
    for source,group in frame.groupby('source_gameplan_run'):
        directory=ROOT/source
        fm,fr=verify_run(directory,['forecasts.parquet'])
        for name in ['manifest.json','receipt.json','forecasts.parquet']:bind(directory/name,inputs[str((directory/name).resolve())])
        f=pd.read_parquet(directory/'forecasts.parquet')
        symbols=fm['configuration']['symbols']
        count=f.symbol.value_counts().to_dict()
        valid=set(count)==set(symbols) and set(count.values())=={24} and len(f)==len(group)==24*len(symbols)
        valid=valid and set(f.id)==set(group.source_forecast_id)
        assert valid,source
        selected=group.set_index('source_forecast_id')
        for row in f.itertuples():
            e=selected.loc[row.id]
            assert row.symbol==e.symbol and row.route==e.route
            assert pd.Timestamp(row.target_window_start)==e.target_window_start and pd.Timestamp(row.target_window_end)==e.target_window_end
            assert float(row.calibrated_probability)==float(e.predicted_probability)
        universe={'source_gameplan_run':source,'action_date':fr['action_date'],'symbols':symbols,
                  'forecast_rows':len(f),'counts_by_symbol':count,'verified':True,
                  'source_selection_contract':fm['configuration'].get('source_selection_contract'),
                  'target_contract_version':fm['configuration'].get('target_contract_version'),
                  'target_price_source_contract':fm['configuration'].get('target_price_source_contract','LEGACY_UNSPECIFIED'),
                  'target_price_dataset':fm['configuration'].get('target_price_dataset','LEGACY_UNSPECIFIED')}
        universes.append(universe)
        f['source_gameplan_run']=source
        forecasts.append(f)
    allf=pd.concat(forecasts,ignore_index=True)
    checks['all_saved_universes_and_exact_forecast_identities_verified']=all(u['verified'] for u in universes)
    meta=allf[['source_gameplan_run','id','target_role','source_selection_contract','target_contract_version']].rename(columns={'id':'source_forecast_id'})
    missing=missing.merge(meta,on=['source_gameplan_run','source_forecast_id'],how='left',validate='one_to_one')
    def resolved_family(row):
        if row['target_contract_version']=='independent-stock-targets-v1':
            contract,dataset=independent_price_identity(row)
            return dataset+'/'+contract+('/HISTORICAL_DEFAULT' if pd.isna(row['target_price_source_contract']) else '/EXPLICIT')
        return 'LEGACY/'+str(row['target_contract_version'])
    missing['resolved_outcome_source_family']=missing.apply(resolved_family,axis=1)
    prior_entry=next(item for item in m['input_files'] if 'gameplan-evaluation-runs' in item['path'] and item['path'].endswith('evaluations.parquet'))
    prior_path=ROOT/prior_entry['path'];bind(prior_path,prior_entry)
    pm,pr=verify_run(prior_path.parent,['evaluations.parquet','summary.json'])
    previous=pd.read_parquet(prior_path)
    transition=frame.merge(previous[['id','evaluation_status']],on='id',how='left',suffixes=('','_previous'))
    old_eval=previous[previous.evaluation_status.eq('EVALUATED')].set_index('id')
    new_eval=frame.set_index('id').loc[old_eval.index]
    score_cols=['evaluation_status','observed_target','observed_return','brier_score','predicted_probability','evaluated_at']
    checks['all_prior_evaluations_preserved']=all(old_eval[c].equals(new_eval[c]) for c in score_cols)
    checks['no_prior_evaluated_rows_became_missing']=not ((transition.evaluation_status_previous=='EVALUATED')&(transition.evaluation_status!='EVALUATED')).any()
    new_missing_ids=set(transition[(transition.evaluation_status=='MATURE_AWAITING_DATA')&(transition.evaluation_status_previous!='MATURE_AWAITING_DATA')].id)
    # The missing XNAS forecasts begin September 4 or later; retained small
    # overlap partitions beginning September 3 cover those exact clock windows.
    partitions=[];bars=[]
    for item in m['input_files']:
        p=ROOT/item['path']; text=p.as_posix()
        if '/XNAS.ITCH/ohlcv-1m/' not in text or p.name!='manifest.json':continue
        manifest=read(p);request=manifest['request']
        if request['start']<'2026-09-03':continue
        bind(p,item)
        normalized=p.parent/manifest['normalized']['path']
        bind(normalized,inputs[str(normalized.resolve())])
        assert sha(normalized)==manifest['normalized']['checksum_sha256']
        receipt=read(p.parent/'receipt.json');bind(p.parent/'receipt.json')
        assert receipt['manifest_checksum_sha256']==sha(p)
        assert receipt['normalized_checksum_sha256']==sha(normalized)
        assert request['dataset']=='XNAS.ITCH' and request['schema']=='ohlcv-1m' and request['stype_in']=='raw_symbol'
        assert len(request['symbol_scope'])==1
        symbol=request['symbol_scope'][0]
        f=pd.read_parquet(normalized)
        timestamp=manifest['normalized']['timestamp_column']
        if timestamp not in f and f.index.name==timestamp:f=f.reset_index()
        f['timestamp']=pd.to_datetime(f[timestamp],utc=True)
        assert len(f)==manifest['normalized']['row_count'] and set(f.symbol)=={symbol}
        assert f.timestamp.ge(pd.Timestamp(request['start'],tz='UTC')).all() and f.timestamp.lt(pd.Timestamp(request['end'],tz='UTC')).all()
        bars.append(f[['symbol','timestamp','open','close']])
        partitions.append({'symbol':symbol,'start':request['start'],'end':request['end'],'manifest_path':str(p)})
    price=pd.concat(bars,ignore_index=True).drop_duplicates(['symbol','timestamp','open','close'])
    assert not price.duplicated(['symbol','timestamp']).any()
    price=price[~(price.open.isna()&price.close.isna())]
    price=price[price.timestamp.add(pd.Timedelta(minutes=1)).le(now)]
    inventory={'partitions':partitions,'schema':'ohlcv-1m','native_archive_partitions_verified':len(partitions)}
    by_symbol={symbol:f.sort_values('timestamp') for symbol,f in price.groupby('symbol')}
    diagnostics=[]
    xnas=missing[missing.target_price_source_contract.eq('xnas-itch-archive-v1')]
    for row in xnas.to_dict('records'):
        gap=row['target_role']=='OPENING_GAP_RESEARCH'
        sides={}
        for side,close in [('start',gap),('end',not gap)]:
            sides[side]=_boundary_evidence(by_symbol[row['symbol']],row['target_window_'+side],close=close,now=now,inventory=inventory,symbol=row['symbol'])
        bad=[side for side,value in sides.items() if value['status']!='OBSERVED']
        diagnostics.append({'evaluation_id':row['id'],'action_date':row['action_date'],'symbol':row['symbol'],
                            'horizon':row['model_group'],'route':row['route'],'target_role':row['target_role'],
                            'source_selection_contract':row['source_selection_contract'],
                            'newly_missing':row['id'] in new_missing_ids,
                            'classification':'MISSING_NATIVE_BOUNDARY_OBSERVATION' if bad else 'BOUNDARIES_PRESENT_LOOKUP_ADMISSION_REQUIRES_INSPECTION',
                            'missing_sides':bad,'boundaries':sides})
    bad_slots=[{'evaluation_id':d['evaluation_id'],'symbol':d['symbol'],'action_date':d['action_date'],
                'horizon':d['horizon'],'side':side,'newly_missing':d['newly_missing'],**d['boundaries'][side]}
               for d in diagnostics for side in d['missing_sides']]
    all_slots=[value for d in diagnostics for value in d['boundaries'].values()]
    checks['all_xnas_missing_required_windows_have_complete_bound_saved_acquisition']=all(value['source_coverage']=='VERIFIED_COMPLETE' for value in all_slots)
    current=missing[missing.action_date.eq('2026-09-30')]
    unresolved=[d for d in diagnostics if not d['missing_sides']]
    result={'reviewed_at':datetime.now(timezone.utc).isoformat(),'status':'COMPLETE' if all(checks.values()) and not unresolved else 'REVIEW_REQUIRED',
            'scope':'Bounded immutable outputs and small checksum-bound recent XNAS normalized partition inspection; no source reload, raw DBN replay, model fit, evaluator rerun, production writes, broker/provider calls.',
            'evaluation_run':str(RUN),'saved_observation_cutoff':summary['observed_at'],'totals':summary['all_saved_gameplans'],
            'checks':checks,'prior_evaluation':str(prior_path.parent),
            'transition_counts':grouped(transition,['evaluation_status_previous','evaluation_status']),
            'saved_universes':universes,'missing_by_source':grouped(missing,['target_price_source_contract','target_price_dataset']),
            'missing_by_resolved_outcome_source':grouped(missing,['resolved_outcome_source_family']),
            'missing_by_resolved_source_symbol_horizon':grouped(missing,['resolved_outcome_source_family','symbol','model_group']),
            'missing_by_source_symbol_horizon':grouped(missing,['target_price_source_contract','symbol','model_group']),
            'missing_by_action_date':grouped(missing,['action_date']),
            'missing_by_history_selection':grouped(missing,['target_contract_version','source_selection_contract']),
            'current_september30_missing':grouped(current,['symbol','model_group']),
            'new_missing_by_symbol_horizon':grouped(missing[missing.id.isin(new_missing_ids)],['action_date','symbol','model_group']),
            'pending_by_date_horizon_target_end':grouped(pending,['action_date','model_group','target_window_end']),
            'historical_non_xnas_missing_rows':len(missing)-len(xnas),
            'legacy_missing_rows':int(missing.resolved_outcome_source_family.str.startswith('LEGACY/').sum()),
            'historical_default_canonical_independent_missing_rows':int(missing.resolved_outcome_source_family.str.startswith('EQUS.MINI/').sum()),
            'legacy_note':'156 retained historical v1/v2 outcomes use their original legacy endpoint/feature matching. A further 87 original independent forecasts have absent explicit source metadata and resolve through the documented native historical default to canonical-equity-minute-v1 / EQUS.MINI. No saved source field is rewritten. All 243 non-XNAS rows were already missing in the preceding evaluation; no prior evaluated outcome was lost.',
            'xnas_missing_rows':len(xnas),'xnas_distinct_missing_windows':len({(d['symbol'],d['route'],str(d['boundaries']['start']['required_source_start']),str(d['boundaries']['end']['required_source_end'])) for d in diagnostics}),
            'xnas_missing_boundary_slot_count':len(bad_slots),'xnas_missing_boundary_status_counts':dict(Counter(s['status'] for s in bad_slots)),
            'xnas_missing_boundary_coverage_counts':dict(Counter(s['source_coverage'] for s in bad_slots)),
            'new_xnas_missing_boundary_slot_count':sum(s['newly_missing'] for s in bad_slots),
            'xnas_missing_row_diagnostics':diagnostics,'unresolved_xnas_rows_with_observed_boundaries':unresolved,
            'bounded_partition_count':len(partitions),'bounded_distinct_minute_rows':len(price),
            'interpretation':'Verified complete native XNAS request intervals can contain no observation within the required five-minute boundary. This proves an observation gap under the saved source contract, not unfinished acquisition or absence of all-market trading. Future targets remain pending; synthetic planning references never supply actual prices.',
            'universe_note':'Every immutable publication is matched to its own saved symbol universe with exactly 24 forecasts per saved symbol. Pre-onboarding absence is not counted as missing coverage.',
            'bindings':bindings}
    path=OUT/'cumulative-coverage-diagnosis.json'
    path.write_text(json.dumps(clean(result),indent=2)+'\n',encoding='utf-8')
    print(json.dumps(clean({key:result[key] for key in ['status','checks','missing_by_source','legacy_missing_rows','xnas_missing_rows','xnas_missing_boundary_slot_count','xnas_missing_boundary_status_counts','xnas_missing_boundary_coverage_counts','new_xnas_missing_boundary_slot_count','current_september30_missing','bounded_partition_count','bounded_distinct_minute_rows']}),indent=2))
    print('Unresolved rows with present boundaries:',len(unresolved))


if __name__=='__main__':main()
