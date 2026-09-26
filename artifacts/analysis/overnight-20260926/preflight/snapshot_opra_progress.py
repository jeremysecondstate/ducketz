"""Bounded local metadata progress snapshot for this native OPRA maintenance.

No imports of provider/runtime modules, archive payload scans or native writes.
Only small entitlement/preflight/cursor/receipt JSONs and current native log read.
"""
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
import hashlib
import json
import math

REPO=Path('C:/dev/ducketz')
ROOT=Path('C:/DATASTORE')
OUT=Path(__file__).resolve().parent
OPRA=ROOT/'market-data/databento/opra/OPRA.PILLAR'
RUN=ROOT/'ml/overnight-runs/20260926T040723.834511Z'
CYCLE='20260926T040724.729021Z-pid46624'
SOURCE='2026-09-25'
END='2026-09-26'
SCHEMAS=('ohlcv-1h','cbbo-1m','definition')
symbols=[s.strip() for s in (REPO/'datafetching/watchlist.txt').read_text().splitlines() if s.strip() and not s.lstrip().startswith('#')]
now=datetime.now(timezone.utc)
issues=[]


def read(path):
    assert path.stat().st_size<2_000_000,str(path)
    return json.loads(path.read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def semantic(value):
    body={k:v for k,v in value.items() if k!='semantic_checksum_sha256'}
    return hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':')).encode()).hexdigest()==value.get('semantic_checksum_sha256')


def stamp(value):
    return datetime.fromisoformat(value.replace('Z','+00:00'))


def integer(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>=0 and value==int(value)


cycle_path=ROOT/'.ducketz-loop-a-cycle.json'
cycle=read(cycle_path)
assert cycle['generation']==CYCLE and cycle['symbols']==symbols
started=stamp(cycle['started_at'])
entitlement_path=OPRA/'metadata/entitlement.json'
entitlement=read(entitlement_path)
entitlement_checks={'semantic_checksum':semantic(entitlement),
    'current_observation':started<=stamp(entitlement['observed_at'])<=now,
    'provider_dataset':entitlement['provider']=='databento-opra' and entitlement['dataset']=='OPRA.PILLAR',
    'required_schema_end_available':all(entitlement['entitlements'][s]['entitled_end']>=END for s in SCHEMAS)}
if not all(entitlement_checks.values()):issues.append('ENTITLEMENT_CHECK_FAILED')
preflights=[]
cursors=[]
missing=[]
partitions=[]
for schema in SCHEMAS:
    for symbol in symbols:
        folder=OPRA/'metadata/preflights'/schema/(symbol+'.OPT')
        candidates=sorted(folder.glob('*_to_'+END+'/preflight.json'))
        assert len(candidates)<10
        found=[]
        for path in candidates:
            value=read(path)
            if started<=stamp(value['generated_at'])<=now:found.append((path,value))
        if len(found)>1:issues.append('DUPLICATE_CURRENT_PREFLIGHT:'+symbol+'/'+schema)
        if not found:missing.append({'symbol':symbol,'schema':schema})
        for path,value in found:
            scope=value['scope']
            rows=value['estimates']
            expected=rows.get(schema,{})
            bounds=entitlement['provider_dataset_range']['schema'][schema]
            included=entitlement['entitlements'][schema]
            checks={'semantic_checksum':semantic(value),
                    'single_exact_scope':scope['schemas']==[schema] and scope['symbols']==[symbol+'.OPT'] and set(rows)=={schema},
                    'source_session_covered':scope['start']<=SOURCE and scope['end']==END,
                    'native_path_scope':path.parent.name==scope['start']+'_to_'+scope['end'],
                    'nested_scope_matches':all(expected.get(k)==scope[k] for k in ('start','end','symbols')),
                    'provider_dataset':value['provider']=='databento-opra' and value['dataset']=='OPRA.PILLAR',
                    'provider_range':stamp(bounds['start'])<=stamp(scope['start']+'T00:00:00Z')<stamp(scope['end']+'T00:00:00Z')<=stamp(bounds['end']),
                    'included_range':included['entitled_start']<=scope['start']<scope['end']<=included['entitled_end'],
                    'exact_zero_cost':value['estimated_cost_usd']==0 and expected.get('estimated_cost_usd')==0 and value['cost_estimates_complete'] is True,
                    'finite_nonnegative_size_records':all(integer(value[k]) and integer(expected.get(k)) for k in ('estimated_download_size_bytes','record_count')),
                    'size_record_sums':all(value[k]==sum(item[k] for item in rows.values()) for k in ('estimated_download_size_bytes','record_count')),
                    'capacity_pass':value['capacity_pass'] is True and value['shortfall_bytes']==0 and value['available_free_bytes']>=value['required_free_bytes'],
                    'capacity_formula':value['storage_reserve_bytes']==5*1024**3 and value['storage_expansion_factor']==2 and value['required_free_bytes']==5*1024**3+2*value['estimated_download_size_bytes']}
            if not all(checks.values()):issues.append('PREFLIGHT_CHECK_FAILED:'+symbol+'/'+schema+':'+','.join(k for k,v in checks.items() if not v))
            preflights.append({'path':str(path),'sha256':sha(path),'symbol':symbol,'schema':schema,'scope':scope,'generated_at':value['generated_at'],'estimated_cost_usd':value['estimated_cost_usd'],'estimated_download_size_bytes':value['estimated_download_size_bytes'],'record_count':value['record_count'],'required_free_bytes':value['required_free_bytes'],'available_free_bytes':value['available_free_bytes'],'provider_range':bounds,'checks':checks})
        cp=OPRA/'state/symbol-history'/symbol/(schema+'.json')
        cursor=read(cp)
        identity=cursor['symbol']==symbol and cursor['schema']==schema and cursor['dataset']=='OPRA.PILLAR' and cursor['provider']=='databento-opra'
        if not identity:issues.append('CURSOR_IDENTITY_FAILED:'+symbol+'/'+schema)
        cursors.append({'path':str(cp),'sha256':sha(cp),'symbol':symbol,'schema':schema,'completed_through':cursor['completed_through'],'updated_at':cursor['updated_at'],'identity_verified':identity,'required_source_session_covered':cursor['completed_through']>=END})
        pp=OPRA/schema/(symbol+'.OPT')/'dates'/SOURCE/'segments/full-day'
        rp,mp=pp/'receipt.json',pp/'manifest.json'
        summary={'symbol':symbol,'schema':schema,'path':str(pp),'receipt_exists':rp.is_file(),'manifest_exists':mp.is_file()}
        if rp.is_file() and mp.is_file():
            receipt,manifest=read(rp),read(mp)
            summary.update(receipt_sha256=sha(rp),manifest_sha256=sha(mp),receipt=receipt)
        partitions.append(summary)

log_path=RUN/'loop_a_close_fetch.log'
log=log_path.read_bytes()
lines=log.decode('utf-8',errors='replace').splitlines()
progress_lines=[line for line in lines if line.startswith(('OPRA history guarded preflight:','OPRA symbol/schema history:','OPRA symbol/schema history preflight failed:','OPRA symbol/schema history deferred','Options history maintenance finished:'))]
total_bytes=sum(item['estimated_download_size_bytes'] for item in preflights)
total_cost=sum(item['estimated_cost_usd'] for item in preflights)
if total_bytes>20_000_000_000:issues.append('OBSERVED_PREFLIGHT_TOTAL_EXCEEDS_NATIVE_20GB_SELECTION_BUDGET')
covered=sum(item['required_source_session_covered'] for item in cursors)
result={'observed_at':now.isoformat(),'status':'ISSUE_REQUIRES_REVIEW' if issues else 'IN_PROGRESS_METADATA_VERIFIED',
    'scope':'Read-only current exact-scope metadata snapshot; no acquisition/full-payload completion claim.',
    'native_run':str(RUN),'loop_a_cycle':CYCLE,'source_session':SOURCE,'required_exclusive_end':END,'expected_scopes':len(symbols)*len(SCHEMAS),
    'base_fetch_cycle':cycle,'base_fetch_cycle_sha256':sha(cycle_path),'entitlement':{'path':str(entitlement_path),'sha256':sha(entitlement_path),'observed_at':entitlement['observed_at'],'checks':entitlement_checks,'provider_schema_ranges':{s:entitlement['provider_dataset_range']['schema'][s] for s in SCHEMAS}},
    'observed_preflight_count':len(preflights),'observed_by_schema':dict(Counter(x['schema'] for x in preflights)),
    'observed_estimated_cost_usd':total_cost,'observed_estimated_download_size_bytes':total_bytes,'observed_estimate_within_native_20GB_ceiling':total_bytes<=20_000_000_000,
    'missing_preflight_scopes':missing,'preflights':preflights,'cursors':cursors,'cursors_covering_required_source_session':covered,
    'current_source_partition_metadata':partitions,'current_source_partition_receipt_count':sum(x['receipt_exists'] for x in partitions),
    'native_progress_lines':progress_lines,'native_log_prefix':{'path':str(log_path),'bytes_at_read':len(log),'prefix_sha256':hashlib.sha256(log).hexdigest()},
    'native_progress_sources':{'log':str(log_path),'entitlement':str(entitlement_path),'scope_preflights_pattern':str(OPRA/'metadata/preflights/{schema}/{symbol}.OPT/*_to_2026-09-26/preflight.json'),'production_cursors_pattern':str(OPRA/'state/symbol-history/{symbol}/{schema}.json'),'sync_lock':str(OPRA/'state/sync.lock')},
    'issues':issues,'limitations':['Native state/status.json is a retained schema status, not the current33-scope progress report.','Preflight observations are sequential and this snapshot is not atomic.','The log prints aggregate guarded-preflight progress only after all scope metadata requests.','Provider entitlement/preflight checks do not prove download/normalization/source-receipt completeness.','Actual observed costs must remain zero despite the native CLI displaying its legacy1-dollar ceiling.'],
    'production_mutations':0,'provider_calls':0,'broker_calls':0}
name='opra-progress-'+now.strftime('%Y%m%dT%H%M%S.%fZ')+'.json'
(OUT/name).write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
(OUT/'opra-progress-latest.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
print(json.dumps({k:result[k] for k in ['observed_at','status','observed_preflight_count','observed_by_schema','observed_estimated_cost_usd','observed_estimated_download_size_bytes','cursors_covering_required_source_session','current_source_partition_receipt_count','issues']},indent=2))
