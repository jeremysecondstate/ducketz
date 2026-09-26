"""Bounded Loop A stage review while downstream training runs.

Reads completed stage/cycle/logs, small OPRA metadata and file stats, and the small
saved FMP diagnostic/quote file. No raw archive payload reads or provider calls.
"""
from pathlib import Path
from datetime import datetime,timezone
from collections import Counter
import hashlib
import json
import re
import sys

sys.dont_write_bytecode=True
sys.path.insert(0,'C:/dev/ducketz')
import pandas as pd
import pyarrow.parquet as pq
from datafetching.fmp_energy_context import calculate_fmp_energy_context

ROOT=Path('C:/DATASTORE')
OUT=Path(__file__).resolve().parent
RUN=ROOT/'ml/overnight-runs/20260925T040822.112032Z'
OPRA=ROOT/'market-data/databento/opra/OPRA.PILLAR'
SOURCE='2026-09-24'
END='2026-09-25'


def read(path):
    if path.stat().st_size>5*1024*1024:
        raise RuntimeError('SMALL_METADATA_BOUND_EXCEEDED')
    return json.loads(path.read_text(encoding='utf-8-sig'))


def sha(path):
    if path.stat().st_size>5*1024*1024:
        raise RuntimeError('SMALL_HASH_BOUND_EXCEEDED')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    report=read(RUN/'stage-report.json')
    stage=next(item for item in report['stages'] if item['stage']=='loop_a_close_fetch')
    if stage['status']!='COMPLETE' or stage['exit_code']!=0:
        raise RuntimeError('LOOP_A_STAGE_NOT_COMPLETE')
    start,finish=pd.Timestamp(stage['started_at']),pd.Timestamp(stage['finished_at'])
    fresh=lambda value:start<=pd.Timestamp(value)<=finish
    logpath=RUN/'loop_a_close_fetch.log'
    lines=logpath.read_text(encoding='utf-8').splitlines()
    symbols=[line.strip() for line in Path('C:/dev/ducketz/datafetching/watchlist.txt').read_text().splitlines() if line.strip() and not line.lstrip().startswith('#')]
    cycle,complete=read(ROOT/'.ducketz-loop-a-cycle.json'),read(ROOT/'.ducketz-loop-a-complete.json')
    issues=[]
    def check(condition,message):
        if not condition:issues.append(message)
    check(cycle==complete,'Cycle and complete records differ')
    check(cycle['generation']=='20260925T040822.974281Z-pid22132','Native cycle identity differs')
    check(cycle['status']=='COMPLETE' and cycle['failure_count']==0,'Cycle failure/completion differs')
    check(cycle['symbols']==symbols,'Production universe differs')
    check(cycle['providers']==['databento','fmp','fred','schwab','sec'],'Provider scopes differ')
    check(fresh(cycle['started_at']) and fresh(cycle['finished_at']),'Cycle outside native stage')
    pattern=r'^\[(\w+)\] changed parquet files: (\d+); blocking provider failures: (\d+); optional capture failures: (\d+); local advisories: (\d+)(.*)$'
    summaries=[{'symbol':m[1],'changed_parquet_files':int(m[2]),'blocking_provider_failures':int(m[3]),'optional_capture_failures':int(m[4]),'local_advisories':int(m[5]),'detail':m[6].strip()} for line in lines if (m:=re.match(pattern,line))]
    check(len(summaries)==len(symbols) and {item['symbol'] for item in summaries}==set(symbols),'Missing/duplicate symbol summary')
    check(all(item['blocking_provider_failures']==item['optional_capture_failures']==0 for item in summaries),'Capture failures reported')
    feature_completions={kind:[line for line in lines if f'END   loop-a.{kind} ' in line] for kind in ['fundamentals','technicals','signals']}
    for kind,events in feature_completions.items():
        check(len(events)==len(symbols) and all('status=ok' in event for event in events),kind+' completion mismatch')
    calculations={'fundamental_outputs':sum(int(m[1]) for line in lines if (m:=re.search(r'Fundamental outputs: (\d+); failures: (\d+)',line))),
        'fundamental_failures':sum(int(m[2]) for line in lines if (m:=re.search(r'Fundamental outputs: (\d+); failures: (\d+)',line))),
        'technical_outputs':sum(int(m[1]) for line in lines if (m:=re.search(r'Output parquet files: (\d+)',line))),
        'technical_not_ready':sum(int(m[1]) for line in lines if (m:=re.search(r'Not-ready calculations skipped: (\d+)',line))),
        'technical_failures':sum(int(m[1]) for line in lines if (m:=re.search(r'Failed calculations: (\d+)',line)))}
    check(calculations['fundamental_failures']==calculations['technical_failures']==0,'Derived calculation failures')
    outputs=sorted(set(match[0] for line in lines for match in re.finditer(r'C:\\DATASTORE\\stocks\\[^\r\n]+?\.parquet',line)))
    missing=[path for path in outputs if not Path(path).is_file()]
    check(not missing,'Logged output files missing')
    cursors=[];partitions=[];preflights=[]
    for symbol in symbols:
        for schema in ['ohlcv-1h','cbbo-1m','definition']:
            label=symbol+'/'+schema
            cursorpath=OPRA/'state/symbol-history'/symbol/(schema+'.json')
            cursor=read(cursorpath)
            tests={'identity':cursor['symbol']==symbol and cursor['schema']==schema and cursor['provider_symbol']==symbol+'.OPT' and cursor['dataset']=='OPRA.PILLAR' and cursor['provider']=='databento-opra',
                   'exact_exclusive_end':cursor['completed_through']==END,'updated_in_stage':fresh(cursor['updated_at']),
                   'historical_no_replay':'replay_coverage' not in cursor}
            check(all(tests.values()),'OPRA cursor failed: '+label)
            cursors.append({'path':str(cursorpath),'payload':cursor,'checks':tests})
            directory=OPRA/schema/(symbol+'.OPT')/'dates'/SOURCE/'segments/full-day'
            receiptpath,manifestpath=directory/'receipt.json',directory/'manifest.json'
            receipt,manifest=read(receiptpath),read(manifestpath)
            tests={'manifest_hash':sha(manifestpath)==receipt['manifest_checksum_sha256'],
                'identity':receipt['dataset']=='OPRA.PILLAR' and receipt['schema']==schema and receipt['partition_date']==SOURCE and manifest['symbol_scope']==symbol+'.OPT',
                'full_request_window':manifest['request']=={'dataset':'OPRA.PILLAR','end':END+'T00:00:00+00:00','schema':schema,'start':SOURCE+'T00:00:00+00:00','stype_in':'parent','symbols':[symbol+'.OPT']},
                'published_in_stage':fresh(receipt['published_at']) and fresh(manifest['published_at'])}
            files={}
            for kind in ['raw','normalized']:
                item=manifest[kind];path=directory/item['path']
                files[kind]={'path':str(path),'present':path.is_file(),'size_bytes':path.stat().st_size if path.is_file() else None,
                    'manifest_size_bytes':item['size_bytes'],'metadata_hash_identity_matches':item['checksum_sha256']==receipt[kind+'_checksum_sha256'],
                    'payload_checksum_recomputed':False}
                tests[kind+'_present_size_metadata_hash']=files[kind]['present'] and files[kind]['size_bytes']==item['size_bytes'] and files[kind]['metadata_hash_identity_matches']
            footer_rows=pq.read_metadata(Path(files['normalized']['path'])).num_rows
            tests['normalized_footer_rows']=footer_rows==manifest['normalized']['row_count']
            check(all(tests.values()),'OPRA source metadata failed: '+label)
            partitions.append({'symbol':symbol,'schema':schema,'directory':str(directory),'receipt_sha256':sha(receiptpath),
                'rows':footer_rows,'event_start':manifest['normalized']['earliest_event_timestamp'],
                'event_end':manifest['normalized']['latest_event_timestamp'],'checks':tests,'files':files})
            candidates=[]
            for path in (OPRA/'metadata/preflights'/schema/(symbol+'.OPT')).glob('*_to_'+END+'/preflight.json'):
                preflight=read(path)
                if fresh(preflight['generated_at']):candidates.append((path,preflight))
            check(len(candidates)==1,'Expected one fresh exact preflight: '+label)
            if len(candidates)==1:
                path,preflight=candidates[0]
                unsigned={key:value for key,value in preflight.items() if key!='semantic_checksum_sha256'}
                nested=preflight['estimates'][schema]
                tests={'semantic_hash':hashlib.sha256(json.dumps(unsigned,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()==preflight['semantic_checksum_sha256'],
                    'scope':preflight['scope']['symbols']==[symbol+'.OPT'] and preflight['scope']['schemas']==[schema] and preflight['scope']['start']<=SOURCE and preflight['scope']['end']==END,
                    'zero_cost':preflight['cost_estimates_complete'] and preflight['estimated_cost_usd']==0 and nested['estimated_cost_usd']==0,
                    'capacity':preflight['capacity_pass'] and preflight['shortfall_bytes']==0 and preflight['available_free_bytes']>=preflight['required_free_bytes']}
                check(all(tests.values()),'Fresh preflight failed: '+label)
                preflights.append({'path':str(path),'generated_at':preflight['generated_at'],'estimated_download_bytes':preflight['estimated_download_size_bytes'],'checks':tests})
    diagnostic=ROOT/'pools/macro/ENERGY_CONTEXT/energy-context/fmp/diagnostics/ENERGY_CONTEXT_energy-context.parquet'
    quote=ROOT/'pools/macro/CLUSD/quote/fmp/normalized/CLUSD_quote.parquet'
    check(pq.read_metadata(diagnostic).num_rows<=10000 and pq.read_metadata(quote).num_rows<=100000,'FMP bounded row maximum exceeded')
    diagnostics=pd.read_parquet(diagnostic)
    frame=pd.read_parquet(quote)
    fetched=pd.to_datetime(frame['fetched_at'],utc=True)
    market=pd.to_datetime(frame['timestamp'],utc=True,format='mixed')
    current=frame.loc[fetched.ge(start)&fetched.le(finish)].copy()
    def pure(data):
        try:return {'status':'PASS','rows':len(calculate_fmp_energy_context(data))}
        except Exception as exc:return {'status':'REJECTED','error':type(exc).__name__+': '+str(exc)}
    skew=(market-fetched).dt.total_seconds()
    bad=frame.loc[skew.gt(5),['symbol','provider_symbol','timestamp','fetched_at']].copy()
    bad['skew_seconds']=skew[skew.gt(5)]
    safe_columns=[name for name in ['symbol','provider_symbol','proxy_fallback_for','is_proxy_fallback','timestamp','fetched_at','available_at','price','unit','macro_authority'] if name in current]
    fmp={'diagnostic_path':str(diagnostic),'diagnostic_sha256':sha(diagnostic),'saved_diagnostic':diagnostics.to_dict('records'),
        'quote_path':str(quote),'quote_sha256':sha(quote),'quote_rows':len(frame),'current_rows':current[safe_columns].to_dict('records'),
        'current_only_pure_calculation':pure(current),'retained_whole_history_pure_calculation':pure(frame),
        'retained_clock_skew_rows':bad.to_dict('records'),'new_current_clock_skew_count':int((skew.gt(5)&fetched.ge(start)&fetched.le(finish)).sum()),
        'interpretation':'The current native USO proxy capture is not a WTI oil price. Current-only pure derivation does not publish a repaired historical feature. Retained whole-history future-timestamp evidence still violates the existing five-second gate and remains preserved/excluded.'}
    cme=read(OUT/'cme-advisory-review.json')
    check(all(not item['missing_symbols'] and not item['unexpected_symbols'] and item['stype_matches'] for item in cme['captures']),'Fresh CME source coverage differs')
    maintenance=[line for line in lines if line.startswith('Options history maintenance finished:')]
    result={'reviewed_at':datetime.now(timezone.utc).isoformat(),'status':'STAGE_METADATA_PASS_WITH_OPTIONAL_ADVISORIES' if not issues else 'STAGE_METADATA_REVIEW_REQUIRED',
        'scope':'Completed Loop A stage only. No full-native-completion assertion. No heavy raw/normalized archive payload hashing, provider calls or production writes.',
        'issues':issues,'stage':stage,'cycle':cycle,'cycle_matches_complete':cycle==complete,'native_current_stage':report['current_stage'],
        'provider_summaries':summaries,'reported_changed_parquet_files':sum(item['changed_parquet_files'] for item in summaries),
        'blocking_provider_failures':sum(item['blocking_provider_failures'] for item in summaries),
        'optional_capture_failures':sum(item['optional_capture_failures'] for item in summaries),
        'local_advisories':sum(item['local_advisories'] for item in summaries),'feature_calculations':calculations,
        'feature_completion_counts':{kind:len(events) for kind,events in feature_completions.items()},
        'logged_stock_output_paths':outputs,'logged_stock_output_path_count':len(outputs),'missing_logged_stock_outputs':missing,
        'output_count_interpretation':'Reported changed provider parquet count and distinct logged derived-output paths are separate populations; no one-to-one equivalence is assumed.',
        'opra':{'cursor_count':len(cursors),'source_date':SOURCE,'exclusive_end':END,'cursors':cursors,'preflights':preflights,'source_partitions':partitions,
            'row_counts_by_schema':{schema:sum(item['rows'] for item in partitions if item['schema']==schema) for schema in ['ohlcv-1h','cbbo-1m','definition']},
            'raw_bytes':sum(item['files']['raw']['size_bytes'] for item in partitions),'normalized_bytes':sum(item['files']['normalized']['size_bytes'] for item in partitions),
            'native_maintenance_summary':maintenance,'payload_hash_audit':'DEFERRED_TO_FULL_FINAL_PROVIDER_HELPER'},
        'fmp_optional_caveat':fmp,'cme_review_path':str(OUT/'cme-advisory-review.json'),'cme_review_sha256':sha(OUT/'cme-advisory-review.json'),
        'native_log_sha256':sha(logpath),'orders_placed_recorded':report['orders_placed'],'broker_orders_enabled':report['broker_orders_enabled']}
    (OUT/'provider-stage-review.json').write_text(json.dumps(result,indent=2,default=str)+'\n',encoding='utf-8')
    print(json.dumps({key:result[key] for key in ['status','issues','reported_changed_parquet_files','logged_stock_output_path_count','blocking_provider_failures','optional_capture_failures','local_advisories','feature_calculations']},default=str))
    print(json.dumps({'opra_cursor_count':len(cursors),'opra_rows':result['opra']['row_counts_by_schema'],'fmp':fmp},default=str))


if __name__=='__main__':main()
