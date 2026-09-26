"""Bounded CME advisory evidence only; no provider calls or source mutations."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import re
import pandas as pd
import pyarrow.parquet as pq

REPO = Path('C:/dev/ducketz')
ROOT = Path('C:/DATASTORE')
OUT = Path(__file__).resolve().parent
RUN = ROOT/'ml/overnight-runs/20260926T040723.834511Z'
START = pd.Timestamp('2026-09-26T04:07:24Z')


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bounded(path, columns=None, maximum=300000):
    assert pq.read_metadata(path).num_rows <= maximum, str(path)
    return pd.read_parquet(path, columns=columns)


diag_path = ROOT/'pools/cme/CME_CONTEXT/cross-asset-context/databento/diagnostics/CME_CONTEXT_cross-asset-context.parquet'
diag = bounded(diag_path, maximum=10000)
current = diag.loc[pd.to_datetime(diag.fetched_at, utc=True).ge(START)]
assert len(current) == 1
record = current.to_dict('records')[0]
prior_path = REPO/'artifacts/analysis/overnight-20260925/verification/provider-completion.json'
prior = read(prior_path)['optional_advisory_review']
previous = prior['diagnostics']['cme']['current_rows'][-1]
fields = ('severity','advisory_type','input_policy','provider_rows_preserved')
same_policy = all(record[key] == previous[key] for key in fields)
assert same_policy and record['severity'] == 'advisory' and record['provider_rows_preserved'] is True
pattern = r'CME cross-asset context rejected every candidate window: (.+?): CME BBO (\w+) is stale by (.+); maximum is (.+)'
parts, old_parts = re.fullmatch(pattern,record['advisory_message']), re.fullmatch(pattern,previous['advisory_message'])
assert parts and old_parts and (parts[1],parts[2],parts[4]) == (old_parts[1],old_parts[2],old_parts[4])

keys = ('DATABENTO_CME_CONTEXT_SYMBOLS','DATABENTO_CME_CONTEXT_STYPE_IN',
        'DATABENTO_CME_CONTRACT_SYMBOLS','DATABENTO_CME_CONTRACT_STYPE_IN')
config = {}
for line in (REPO/'.env').read_text(encoding='utf-8-sig').splitlines():
    key,sep,value = line.partition('=')
    if sep and key.strip() in keys:
        value=value.strip()
        if len(value)>1 and value[0] == value[-1] and value[0] in ('"',"'"):
            value=value[1:-1]
        config[key.strip()] = json.loads(value) if key.strip().endswith('_SYMBOLS') else value
old_config = prior['cme_scope_coverage']['selected_nonsecret_configuration']
config_unchanged = all(config[k] == (json.loads(old_config[k]['value']) if k.endswith('_SYMBOLS') else old_config[k]['value']) for k in keys)
assert config_unchanged

captures=[]
for scope,prefix in [('CME_CONTEXT','DATABENTO_CME_CONTEXT'),('CME_CONTRACTS','DATABENTO_CME_CONTRACT')]:
    for schema in ['ohlcv-1m','bbo-1m','mbp-10']:
        period=scope.lower()+'_'+schema
        path=ROOT/'pools/cme'/scope/period/'databento/normalized'/(scope+'_'+period+'.parquet')
        available=pq.read_schema(path).names
        cols=[k for k in ('symbol','timestamp','fetched_at','provider_dataset','provider_stype_in','request_limit_saturated','initial_range_start','initial_range_end','effective_range_start','effective_range_end','latest_window_shrink_count','empty_window_expansion_count') if k in available]
        frame=bounded(path,cols)
        now=frame.loc[pd.to_datetime(frame.fetched_at,utc=True).ge(START)]
        expected=config[prefix+'_SYMBOLS']
        checks={'all_requested_symbols_observed':set(now.symbol)==set(expected),
                'stype_matches':now.provider_stype_in.eq(config[prefix+'_STYPE_IN']).all(),
                'same_dataset':now.provider_dataset.eq('GLBX.MDP3').all()}
        assert all(checks.values())
        groups=[{'symbol':str(s),'rows':len(g),'first_market_time':str(pd.to_datetime(g.timestamp,utc=True).min()),'last_market_time':str(pd.to_datetime(g.timestamp,utc=True).max())} for s,g in now.groupby('symbol')]
        captures.append({'path':str(path),'sha256':sha(path),'scope':scope,'schema':schema,'file_rows':len(frame),'current_rows':len(now),
                         'checks':{k:bool(v) for k,v in checks.items()},'symbol_observations':groups,
                         'request_metadata':now[[k for k in cols if k not in ('symbol','timestamp','fetched_at')]].drop_duplicates().to_dict('records')})

mappings={}
raw_sources=[]
for scope in ['CME_CONTEXT','CME_CONTRACTS']:
    period=scope.lower()+'_bbo-1m'
    path=ROOT/'pools/cme'/scope/period/'databento/raw'/(scope+'_'+period+'_raw.parquet')
    frame=bounded(path,['ts_event','instrument_id','symbol','provider_stype_in','provider_dataset'])
    frame=frame.loc[pd.to_datetime(frame.ts_event,utc=True).ge(pd.Timestamp('2026-09-25T19:59:00Z'))]
    for symbol,group in frame.groupby('symbol'):
        mappings[str(symbol)]={'instrument_ids':sorted(int(v) for v in group.instrument_id.unique()),'rows':len(group),'last_event':str(pd.to_datetime(group.ts_event,utc=True).max())}
    raw_sources.append({'path':str(path),'sha256':sha(path),'recent_rows':len(frame)})
pairs=[]
for continuous,raw in [('ES.v.0','ESZ6'),('NQ.v.0','NQZ6'),('CL.v.0','CLX6'),('GC.v.0','GCZ6')]:
    match=mappings[continuous]['instrument_ids']==mappings[raw]['instrument_ids']
    assert match
    pairs.append({'continuous':continuous,'raw':raw,'instrument_ids_match':match,'continuous_evidence':mappings[continuous],'raw_evidence':mappings[raw]})

inventory={}
for schema in ('ohlcv-1m','bbo-1m','mbp-10'):
    folder=ROOT/'pools/cme/events/databento/context'/schema/'normalized'
    paths=sorted(folder.rglob('events.parquet'))
    assert len(paths)<1000
    inventory[schema]={'count':len(paths),'newest_mtime_utc':datetime.fromtimestamp(max(p.stat().st_mtime for p in paths),timezone.utc).isoformat() if paths else None,
        'path_inventory_sha256':hashlib.sha256('\n'.join(str(p) for p in paths).encode()).hexdigest(),
        'review_scope':'Path/count/mtime only; partition payloads not scanned.'}

log_path=RUN/'loop_a_close_fetch.log'
log_bytes=log_path.read_bytes()
lines=log_bytes.decode('utf-8',errors='replace').splitlines()
cme_lines=[line for line in lines if 'CME' in line or '504' in line]
successes=[line for line in cme_lines if 'END   provider.request name="CME ' in line and 'status=ok' in line]
assert len(successes)==6
result={'reviewed_at':datetime.now(timezone.utc).isoformat(),'status':'KNOWN_OPTIONAL_CME_LIMITATION_REVERIFIED_NO_NEW_ACTIONABLE_DEFECT',
        'scope':'Current compact projected CME captures, diagnostics, raw BBO mapping, code and partition path metadata only; no retained archive payload scan.',
        'current_diagnostic':record,'previous_diagnostic':previous,'diagnostic_comparison':{'same_category_policy':same_policy,'same_candidate_root_and_staleness_threshold':True,'exact_message_identical':record['advisory_message']==previous['advisory_message'],'staleness_age_increased':True},
        'configuration':config,'configuration_unchanged_from_prior':config_unchanged,'captures':captures,'raw_mapping_sources':raw_sources,'raw_continuous_pairs':pairs,'persisted_partition_inventory':inventory,
        'native_cme_log_lines':cme_lines,'native_log_prefix':{'path':str(log_path),'bytes_at_read':len(log_bytes),'prefix_sha256':hashlib.sha256(log_bytes).hexdigest(),'note':'LoopA is still running; this hashes only bytes read at review time.'},
        'optional_input_disposition':'Preserve optional cross-asset derivation rejection. The partition-first context reader still selects existing normalized event partitions ahead of fresh flat captures. Both new MBP captures retain request_limit_saturated=true and do not establish complete book inputs. No change to model admission, stock source labels, prices or promotion gates is justified.',
        'prior_comparison':{'path':str(prior_path),'sha256':sha(prior_path)},'source_sha256':{str(p):sha(p) for p in [diag_path,REPO/'datafetching/cme_cross_asset_context.py',REPO/'datafetching/cme_history.py',REPO/'datafetching/databento_fetch.py']},
        'limitations':['No successful cross-asset derivation is claimed.','No full provider or overnight completion is claimed while LoopA remains running.','Fresh successful requests do not remove stale-partition or capped-book exclusions.','No full retained archive validation was repeated.'],
        'production_writes':0,'provider_calls':0,'broker_calls':0}
(OUT/'cme-advisory-review.json').write_text(json.dumps(result,indent=2,default=str)+'\n',encoding='utf-8')
print(json.dumps({'status':result['status'],'captures':[(i['scope'],i['schema'],i['current_rows']) for i in captures],'partition_counts':{k:v['count'] for k,v in inventory.items()},'output':str(OUT/'cme-advisory-review.json')}))
