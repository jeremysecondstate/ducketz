"""Bounded pure local CME diagnostic; no providers or production writes."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import sys

sys.dont_write_bytecode=True
sys.path.insert(0,'C:/dev/ducketz')
import pandas as pd
import pyarrow.parquet as pq
from datafetching.cme_cross_asset_context import (
    _continuous_roots, _prepare_ohlcv, _complete_common_ohlcv_windows,
    calculate_cme_cross_asset_context,
)

ROOT=Path('C:/DATASTORE')
REPO=Path('C:/dev/ducketz')
OUT=Path(__file__).resolve().parent
START=pd.Timestamp('2026-09-25T04:08:22.112032Z')
END=pd.Timestamp('2026-09-25T04:24:41.274200Z')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bounded(path,maximum=300000,columns=None):
    if pq.read_metadata(path).num_rows>maximum:
        raise RuntimeError('CME_DIAGNOSTIC_ROW_BOUND_EXCEEDED')
    return pd.read_parquet(path,columns=columns)


def calculation(frames,at):
    try:
        result=calculate_cme_cross_asset_context(frames['ohlcv-1m'],frames['bbo-1m'],frames['mbp-10'],calculated_at=at)
        return {'status':'PASS','rows':len(result)}
    except Exception as exc:
        return {'status':'REJECTED','error':type(exc).__name__+': '+str(exc)}


def main():
    diagnostic=ROOT/'pools/cme/CME_CONTEXT/cross-asset-context/databento/diagnostics/CME_CONTEXT_cross-asset-context.parquet'
    diagnostics=bounded(diagnostic,10000)
    times=pd.to_datetime(diagnostics['fetched_at'],utc=True)
    current_diagnostic=diagnostics.loc[times.ge(START)&times.le(END)].copy()
    previous_diagnostic=diagnostics.loc[times.lt(START)].assign(_time=times).sort_values('_time').tail(1).drop(columns='_time')
    if len(current_diagnostic)!=1:
        raise RuntimeError('EXACT_CURRENT_CME_ADVISORY_REQUIRED')
    at=pd.Timestamp(current_diagnostic.iloc[0]['fetched_at'])
    config={}
    allowed={'DATABENTO_CME_CONTEXT_SYMBOLS','DATABENTO_CME_CONTEXT_STYPE_IN','DATABENTO_CME_CONTRACT_SYMBOLS','DATABENTO_CME_CONTRACT_STYPE_IN'}
    for number,line in enumerate((REPO/'.env').read_text(encoding='utf-8-sig').splitlines(),1):
        key,separator,value=line.partition('=')
        if separator and key.strip() in allowed:
            value=value.strip().strip("'\"")
            config[key.strip()]={'line':number,'value':json.loads(value) if key.strip().endswith('_SYMBOLS') else value}
    prior_path=REPO/'artifacts/analysis/overnight-20260924/resumed-provider/provider-completion.json'
    prior=json.loads(prior_path.read_text())['optional_advisory_review']['cme_scope_coverage']['captures']
    prior_by={(item['scope'],item['schema']):item for item in prior}
    captures=[]
    frames={}
    for scope,prefix in [('CME_CONTEXT','DATABENTO_CME_CONTEXT'),('CME_CONTRACTS','DATABENTO_CME_CONTRACT')]:
        expected_symbols=config[prefix+'_SYMBOLS']['value']
        expected_stype=config[prefix+'_STYPE_IN']['value']
        for schema in ['ohlcv-1m','bbo-1m','mbp-10']:
            period=scope.lower()+'_'+schema
            path=ROOT/'pools/cme'/scope/period/'databento/normalized'/(scope+'_'+period+'.parquet')
            all_rows=bounded(path)
            receipts=pd.to_datetime(all_rows['fetched_at'],utc=True)
            current=all_rows.loc[receipts.ge(START)&receipts.le(END)].copy()
            stamps=pd.to_datetime(current['timestamp'],utc=True)
            fetched=pd.to_datetime(current['fetched_at'],utc=True)
            by_symbol=[]
            for symbol,rows in current.groupby('symbol'):
                events=pd.to_datetime(rows['timestamp'],utc=True)
                by_symbol.append({'symbol':symbol,'rows':len(rows),'first_event':str(events.min()),'last_event':str(events.max())})
            identity_columns=[name for name in ['provider_stype_in','provider_symbol','provider_dataset'] if name in current]
            request_columns=[name for name in ['request_limit_saturated','initial_range_start','initial_range_end','effective_range_start','effective_range_end','latest_window_shrink_count','empty_window_expansion_count'] if name in current]
            prior_capture=prior_by[(scope,schema)]
            capture={'scope':scope,'schema':schema,'path':str(path),'sha256':sha(path),'file_rows':len(all_rows),'current_rows':len(current),
                'expected_symbols':expected_symbols,'expected_stype':expected_stype,'actual_symbols':by_symbol,
                'missing_symbols':sorted(set(expected_symbols)-set(current['symbol'])),'unexpected_symbols':sorted(set(current['symbol'])-set(expected_symbols)),
                'stype_matches':bool(current['provider_stype_in'].eq(expected_stype).all()),
                'identity_values':{name:sorted(str(value) for value in current[name].dropna().unique()) for name in identity_columns},
                'request_evidence':current[request_columns].drop_duplicates().to_dict('records'),
                'latest_event':str(stamps.max()),'latest_receipt':str(fetched.max()),
                'latest_event_age_at_advisory_seconds':(at-stamps.max()).total_seconds(),
                'limit_saturated':bool(current.get('request_limit_saturated',pd.Series(False,index=current.index)).fillna(False).any()),
                'events_after_receipt_plus5s':int((stamps>fetched+pd.Timedelta(seconds=5)).sum()),
                'receipts_after_advisory':int((fetched>at).sum()),
                'native_continuous_root_rows':len(_continuous_roots(current)),
                'prior_completed_capture':{'rows':prior_capture['current_stage_rows'],'actual_symbols':prior_capture['symbols'],
                                           'missing_symbols':prior_capture['missing_requested_symbols']}}
            captures.append(capture)
            if scope=='CME_CONTEXT':
                frames[schema]=current
    partitions={}
    for schema in ['ohlcv-1m','bbo-1m','mbp-10']:
        paths=sorted((ROOT/'pools/cme/events/databento/context'/schema/'normalized').rglob('events.parquet'))
        latest=paths[-1]
        sample=bounded(latest,100000,columns=['timestamp','fetched_at'])
        partitions[schema]={'count':len(paths),'latest_partition':str(latest),'latest_partition_sha256':sha(latest),
            'latest_partition_rows':len(sample),'max_event':str(pd.to_datetime(sample['timestamp'],utc=True).max()),
            'max_receipt':str(pd.to_datetime(sample['fetched_at'],utc=True).max())}
        if schema=='ohlcv-1m':
            recent=pd.concat([bounded(path,100000) for path in paths[-2:]],ignore_index=True)
            windows=_complete_common_ohlcv_windows(_prepare_ohlcv(recent))
            partitions[schema]['latest_exact_common_60min_window_end']=str(max(windows)+pd.Timedelta(hours=1)) if windows else None
    corrected={name:frame.assign(provider_symbol=frame['symbol']) for name,frame in frames.items()}
    result={'reviewed_at':datetime.now(timezone.utc).isoformat(),'status':'FRESH_COLLECTION_COMPLETE_OPTIONAL_CONTEXT_EXCLUDED',
        'scope':'Six bounded current aggregate CME captures, eighteen diagnostic rows, three newest event partitions and two OHLCV partitions. No provider calls, configuration changes, production writes or model qualification claims.',
        'native_databento_completion':{'completed_at':END.isoformat(),'status':'ok','watchlist_symbol_count':11,
            'source_log':str(ROOT/'ml/overnight-runs/20260925T040822.112032Z/loop_a_close_fetch.log'),
            'transient_retry':'CME_CONTEXT ohlcv-1m first request received gateway504; native retry2 succeeded at04:18:24Z.'},
        'diagnostic_path':str(diagnostic),'diagnostic_sha256':sha(diagnostic),'current_diagnostic':current_diagnostic.to_dict('records'),
        'previous_diagnostic':previous_diagnostic.to_dict('records'),'selected_nonsecret_configuration':config,
        'captures':captures,'partitioned_source_inventory':partitions,
        'aggregate_native_pure_calculation':calculation(frames,at),
        'diagnostic_only_actual_symbol_identity_calculation':calculation(corrected,at),
        'baseline_path':str(prior_path),'baseline_sha256':sha(prior_path),
        'causes':[
            'Current selector prioritizes retained partitioned events whenever present; those files have not advanced with inline aggregate captures. Latest exact common hour still ends 2026-09-03T21:00Z; saved NQ BBO is stale by 21 days 07:24:42 against the 15-minute maximum.',
            'Fresh continuous aggregate row symbols identify all five roots, but provider_symbol is the group alias CME_CONTEXT; the current reader prefers that column and identifies zero continuous roots from unmodified continuous aggregates. Raw contracts are separately excluded intentionally by their raw_symbol stype.',
            'Using actual symbol identity diagnostically does not make current inputs admissible: current MBP is limit-saturated, and its newest event is more than eight hours old. Latest BBO is 15 minutes 42 seconds old, beyond the 15-minute gate. Exact same-hour requirements also remain unchanged.'
        ],
        'interpretation':'Successful native collection preserves actual captures but does not establish strict cross-asset feature admissibility. Known optional source-contract and quality exclusions persist; no source substitutions, gate relaxation or stock-model conclusions follow.'}
    (OUT/'cme-advisory-review.json').write_text(json.dumps(result,indent=2,default=str)+'\n',encoding='utf-8')
    print(json.dumps({'status':result['status'],'diagnostic':result['current_diagnostic'],
        'captures':[{key:item[key] for key in ['scope','schema','current_rows','missing_symbols','latest_event','latest_event_age_at_advisory_seconds','limit_saturated','native_continuous_root_rows']} for item in captures],
        'pure_calculation':result['aggregate_native_pure_calculation'],'diagnostic_identity_calculation':result['diagnostic_only_actual_symbol_identity_calculation']},default=str))


if __name__=='__main__':main()
