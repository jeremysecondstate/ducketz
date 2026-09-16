"""Bounded native provider completion audit; read metadata, never refetch or rehash archives."""
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
import hashlib
import json
import re

ROOT=Path('C:/DATASTORE')
OUT=Path(__file__).resolve().parent
OPRA=ROOT/'market-data/databento/opra/OPRA.PILLAR'
RUN=ROOT/'ml/overnight-runs/20260916T040757.217937Z'
schemas=['ohlcv-1h','cbbo-1m','definition']
symbols=[x.strip() for x in Path('C:/dev/ducketz/datafetching/watchlist.txt').read_text().splitlines() if x.strip() and not x.lstrip().startswith('#')]
cycle=json.loads((ROOT/'.ducketz-loop-a-cycle.json').read_text())
complete=json.loads((ROOT/'.ducketz-loop-a-complete.json').read_text())
assert cycle==complete and cycle['generation']=='20260916T040758.032086Z-pid29216'
assert cycle['symbols']==symbols and cycle['failure_count']==0 and cycle['status']=='COMPLETE'
stage=json.loads((RUN/'stage-report.json').read_text())
log_path=RUN/'loop_a_close_fetch.log'
log=log_path.read_text(encoding='utf-8')
lines=log.splitlines()
summary_pattern=r'^\[(\w+)\] changed parquet files: (\d+); blocking provider failures: (\d+); optional capture failures: (\d+); local advisories: (\d+)(.*)$'
provider_summaries=[{'symbol':m[1],'changed_files':int(m[2]),'blocking_failures':int(m[3]),'optional_capture_failures':int(m[4]),'local_advisories':int(m[5]),'advisory_detail':m[6].strip()} for line in lines if (m:=re.match(summary_pattern,line))]
assert {x['symbol'] for x in provider_summaries}==set(symbols)
assert all(x['blocking_failures']==x['optional_capture_failures']==0 for x in provider_summaries)
assert sum(x['local_advisories'] for x in provider_summaries)==2
feature_completions={kind:[line for line in lines if f'END   loop-a.{kind} ' in line] for kind in ['fundamentals','technicals','signals']}
for kind, events in feature_completions.items():
 assert {re.search(r'sym=(\w+)',line)[1] for line in events}==set(symbols)
 assert all('status=ok' in line for line in events)
technical_failures=[int(line.split(':')[1]) for line in lines if line.startswith('Failed calculations:')]
assert len(technical_failures)==11 and not any(technical_failures)
output_paths=sorted(set(match[0] for line in lines for match in re.finditer(r'C:\\DATASTORE\\stocks\\[^\r\n]+?\.parquet',line)))
missing_outputs=[p for p in output_paths if not Path(p).is_file()]
assert not missing_outputs
completed_scopes=[]
for line in lines:
 if line.startswith('OPRA symbol/schema history: '):
  item=dict(part.strip().split('=',1) for part in line.split(': ',1)[1].split(';'))
  item['log_line']=line
  completed_scopes.append(item)
assert len(completed_scopes)==33
assert {(x['symbol'],x['schema']) for x in completed_scopes}=={(s,c) for s in symbols for c in schemas}
assert all(x['status']=='COMPLETE' and x['end']=='2026-09-16' for x in completed_scopes)
scopes=[]
for symbol in symbols:
 for schema in schemas:
  cursor_path=OPRA/'state/symbol-history'/symbol/(schema+'.json')
  cursor=json.loads(cursor_path.read_text())
  assert cursor['completed_through']=='2026-09-16' and cursor['symbol']==symbol and cursor['schema']==schema
  assert cursor['provider_symbol']==symbol+'.OPT' and 'replay_coverage' not in cursor
  directory=OPRA/schema/(symbol+'.OPT')/'dates/2026-09-15/segments/full-day'
  manifest_path=directory/'manifest.json'
  manifest_bytes=manifest_path.read_bytes()
  manifest=json.loads(manifest_bytes)
  receipt=json.loads((directory/'receipt.json').read_text())
  manifest_hash=hashlib.sha256(manifest_bytes).hexdigest()
  assert receipt['manifest_checksum_sha256']==manifest_hash
  assert manifest['dataset']==receipt['dataset']=='OPRA.PILLAR'
  assert manifest['schema']==receipt['schema']==schema
  assert manifest['partition_date']==receipt['partition_date']=='2026-09-15'
  assert manifest['symbol_scope']==symbol+'.OPT'
  assert manifest['provider_delivery']['mode']=='timeseries-stream'
  req=manifest['request']
  assert req['dataset']=='OPRA.PILLAR' and req['schema']==schema and req['symbols']==[symbol+'.OPT'] and req['stype_in']=='parent'
  assert req['start']==manifest['partition_start']==receipt['partition_start']=='2026-09-15T00:00:00+00:00'
  assert req['end']==manifest['partition_end']==receipt['partition_end']=='2026-09-16T00:00:00+00:00'
  for field in ['raw','normalized']:
   saved=manifest[field]
   assert receipt[field+'_checksum_sha256']==saved['checksum_sha256']
   assert (directory/saved['path']).stat().st_size==saved['size_bytes']
  scopes.append({'symbol':symbol,'schema':schema,'cursor_path':str(cursor_path),'cursor_updated_at':cursor['updated_at'],
   'completed_through':cursor['completed_through'],'manifest_path':str(manifest_path),'manifest_checksum_verified':manifest_hash,
   'request':req,'delivery_mode':manifest['provider_delivery']['mode'],'published_at':receipt['published_at'],
   'row_count':manifest['normalized']['row_count'],'raw_bytes':manifest['raw']['size_bytes'],'normalized_bytes':manifest['normalized']['size_bytes'],
   'data_file_sizes_match':True,'data_file_checksums_recomputed':False})
preflight_snapshot=json.loads((OUT/'opra-preflight-progress.json').read_text())
preflights=[]
for item in preflight_snapshot['current_preflights']:
 path=Path(item['path'])
 p=json.loads(path.read_text())
 saved=p.pop('semantic_checksum_sha256')
 calculated=hashlib.sha256(json.dumps(p,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()
 assert saved==calculated
 assert p['capacity_pass'] and p['estimated_cost_usd']==0 and p['cost_estimates_complete']
 assert p['scope']['end']=='2026-09-16'
 assert p['available_free_bytes']>=p['required_free_bytes'] and p['shortfall_bytes']==0
 preflights.append({'path':str(path),'scope':p['scope'],'generated_at':p['generated_at'],'semantic_checksum_verified':saved,
  'estimated_cost_usd':p['estimated_cost_usd'],'estimated_download_size_bytes':p['estimated_download_size_bytes'],'capacity_pass':p['capacity_pass']})
assert {(x['scope']['symbols'][0].removesuffix('.OPT'),x['scope']['schemas'][0]) for x in preflights}=={(s,c) for s in symbols for c in schemas}
health=json.loads((OPRA/'health/current.json').read_text())
assert health['observed_at']>cycle['started_at']
maintenance=[line for line in lines if line.startswith('Options history maintenance finished:')]
assert len(maintenance)==1
assert 'requested_scopes=33; completed_scopes=33; capacity_blocked_scopes=0; failed_scopes=0; bootstrap_required_scopes=0;' in maintenance[0]
assert 'deferred_scopes=0; live_replay_completed_scopes=0; live_replay_bytes=0;' in maintenance[0]
result={'audited_at':datetime.now(timezone.utc).isoformat(),'status':'NATIVE_LOOP_A_AND_33_OPRA_SCOPES_COMPLETE',
 'scope':'Bounded source-bound metadata/manifest receipt check, native logs and current small cursors. Raw/normalized archive hashes and native validation not repeated.',
 'source_session':'2026-09-15','exclusive_coverage_end':'2026-09-16','production_symbols':symbols,'base_cycle':cycle,
 'matching_complete_receipt':str(ROOT/'.ducketz-loop-a-complete.json'),'stage_report_snapshot':stage,
 'provider_summaries':provider_summaries,'feature_completions':feature_completions,'technical_failure_counts':technical_failures,
 'logged_output_paths_checked':len(output_paths),'missing_logged_outputs':missing_outputs,
 'schwab_options_capture_lines':[line for line in lines if '/schwab/options]' in line],
 'provider_request_completed_lines':[line for line in lines if 'END   provider.request' in line],
 'completed_opra_scope_logs':completed_scopes,'current_session_scope_receipts':scopes,
 'preflights':preflights,'selected_estimated_download_bytes':sum(x['estimated_download_size_bytes'] for x in preflights),
 'selected_estimated_cost_usd':sum(x['estimated_cost_usd'] for x in preflights),
 'maintenance_summary':maintenance[0],'health':health,
 'health_limitation':'Health contains the selected verified inventory, not a count/list of invalid partitions skipped by iter_verified_partitions; historical full-day can supersede retained Live replay. Completion is not a claim that every retained archive directory is valid.',
 'known_advisories':{'cme':'Repeated stale optional partition-source context rejection; see cme-advisory-audit.json','fmp':'Repeated retained historical 9.471-second quote clock skew; current-cycle quote passes; see provider-advisories.json'},
 'limits':['Provider success and feature completion do not prove every optional source family/model qualifies.','Operational EQUS.MINI acquisition is separate from subsequent XNAS.ITCH stock target history.','No provider/broker calls, claims, process actions, production changes or training were performed.'],
 'log_path':str(log_path),'log_sha256':hashlib.sha256(log_path.read_bytes()).hexdigest()}
(OUT/'provider-completion.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({k:result[k] for k in ['audited_at','status','logged_output_paths_checked','selected_estimated_download_bytes','selected_estimated_cost_usd','maintenance_summary']},indent=2))
print(json.dumps({'health_observed_at':health['observed_at'],'health_total_partitions':health['total_partitions'],'health_total_rows':health['total_rows'],'health_production_schema_counts':{s:health['schemas'][s]['partition_count'] for s in schemas}},indent=2))
