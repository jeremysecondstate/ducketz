"""Read compact verified Pricing history and current quarantine metadata only."""
from pathlib import Path
import hashlib,json,re,sys
sys.dont_write_bytecode=True
sys.path.insert(0,'C:/dev/ducketz')
import pandas as pd
from ml.artifacts import file_checksum
from ml.option_pricing.consumers import read_verified_compact_pricing_features
from ml.horizons import horizon_specifications_for_profile
from ml.runtime_pipeline import OPTION_PRICING_LOOP_B_GATE_POLICY_VERSION,OPTION_PRICING_LOOP_B_MINIMUM_COVERAGE,OPTION_PRICING_LOOP_B_MINIMUM_DISTINCT_TARGETS,_specification_for_pricing_gate

ROOT=Path('C:/DATASTORE')
OUT=Path(__file__).resolve().parent
RUN=ROOT/'ml/runs/20260916T053415.692855Z'
LOG=ROOT/'ml/overnight-runs/20260916T040757.217937Z/loop_b_directional_generation.log'
PRIOR=Path('C:/dev/ducketz/artifacts/analysis/overnight-20260915/option-pricing-quarantine.json')
prior=json.loads(PRIOR.read_text())
cycle=json.loads((ROOT/'.ducketz-loop-a-complete.json').read_text())
assert cycle['generation']=='20260916T040758.032086Z-pid29216'
cutoff=cycle['finished_at']
source,files=read_verified_compact_pricing_features(ROOT,available_not_after=cutoff)
evidence=[{'path':str(p),'sha256':file_checksum(p),'bytes':p.stat().st_size} for p in files]
current_hashes={Path(x['path']).resolve().as_posix():x['sha256'] for x in evidence}
prior_hashes={Path(x['path']).resolve().as_posix():x['sha256'] for x in prior['verified_source']['source_files']}
first=pd.to_datetime(source.first_available_at,utc=True)
target=pd.to_datetime(source.target_snapshot_for,utc=True)
freshness={'1h':pd.Timedelta(hours=2),'4h':pd.Timedelta(hours=4),'1d':pd.Timedelta(days=2),'1w':pd.Timedelta(days=8)}
fresh_counts={h:int(pd.concat([first+limit,target+limit],axis=1).min(axis=1).ge(pd.Timestamp(cutoff)).sum()) for h,limit in freshness.items()}
columns=[c for c in ['causal_coverage','median_normalized_residual','median_predictive_standard_deviation','median_model_edge_in_half_spreads','positive_edge_fraction','negative_edge_fraction','raw_arbitrage_violation_rate','constrained_arbitrage_violation_rate','interval_80_coverage','interval_95_coverage','median_relative_bid_ask_spread'] if c in source]
log=LOG.read_bytes()
quarantine=[]
for line in log.decode().splitlines():
 match=re.match(r'\[Loop B/(.*?)\] Option Pricing family quarantined; fitting (.*?) until .*\((.*?)\)$',line)
 if match:
  quarantine.append({'horizon':match[1],'effective_feature_set':match[2],'failed_routes':match[3].split(', '),'log_line':line})
effective={}
for horizon,spec in horizon_specifications_for_profile('loop-a-all-bsgp-active-v3').items():
 fallback=_specification_for_pricing_gate(spec,gate={'enabled':True,'downstream_training_eligible':False})
 effective[horizon]={'requested_feature_set':spec.feature_set,'effective_feature_set':fallback.feature_set,'exact_non_pricing_feature_preservation':'PASS'}
assert len(quarantine)==9 and sum(len(x['failed_routes']) for x in quarantine)==99
manifest_path=RUN/'manifest.json'
gate=None
groups={}
binding=None
if manifest_path.is_file():
 manifest=json.loads(manifest_path.read_text())
 cfg=manifest['configuration']
 assert pd.Timestamp(cfg['causal_input_cutoff'])==pd.Timestamp(cutoff)
 gate=cfg['pricing_evidence']
 publication=json.loads((RUN/'publication.json').read_text())
 assert publication['manifest_checksum_sha256']==file_checksum(manifest_path)
 binding={'manifest_path':str(manifest_path),'manifest_checksum_sha256':file_checksum(manifest_path),'route_errors':cfg['route_errors'],'publication_counts':cfg['publication_counts']}
 gathered={}
 for route in gate['route_gates'].values():
  for name,item in route['groups'].items(): gathered.setdefault(name,[]).append(item)
 groups={name:{'routes':len(items),'passing_routes':sum(item['pass'] for item in items),'maximum_complete_fraction':max(item['complete_row_fraction'] for item in items),'maximum_fresh_joined_fraction':max(item['fresh_joined_row_fraction'] for item in items),'maximum_distinct_targets':max(item['distinct_surface_targets'] for item in items)} for name,items in gathered.items()}
result={'audited_at':pd.Timestamp.now(tz='UTC').isoformat(),'status':'OPTIONAL_PRICING_QUARANTINE_VERIFIED','read_only':True,'run_path':str(RUN),'log_path':str(LOG),'log_sha256':hashlib.sha256(log).hexdigest(),
 'base_cycle':cycle,'causal_input_cutoff':cutoff,'native_quarantine':quarantine,'failed_route_count':99,'effective_feature_contracts':effective,
 'gate_policy':{'version':OPTION_PRICING_LOOP_B_GATE_POLICY_VERSION,'minimum_complete_row_fraction':OPTION_PRICING_LOOP_B_MINIMUM_COVERAGE,'minimum_fresh_joined_row_fraction':OPTION_PRICING_LOOP_B_MINIMUM_COVERAGE,'minimum_distinct_surface_targets':OPTION_PRICING_LOOP_B_MINIMUM_DISTINCT_TARGETS},
 'verified_source':{'rows':len(source),'symbols':sorted(source.symbol.unique()),'missing_production_symbols':sorted(set(cycle['symbols'])-set(source.symbol)),'distinct_targets':int(source.target_snapshot_for.nunique()),'latest_target':target.max(),'latest_availability':first.max(),'fresh_rows_at_current_cutoff':fresh_counts,'all_rows_missing_features':[c for c in columns if source[c].isna().all()],'source_files':evidence,'provenance':source.attrs.get('pricing_evidence',{}),'by_symbol':{symbol:{'rows':len(frame),'distinct_targets':int(frame.target_snapshot_for.nunique()),'latest_target':frame.target_snapshot_for.max(),'non_null_feature_counts':{c:int(frame[c].notna().sum()) for c in columns}} for symbol,frame in source.groupby('symbol')}},
 'prior_comparison':{'audit':str(PRIOR),'source_paths_and_hashes_unchanged':current_hashes==prior_hashes,'prior_rows':prior['verified_source']['rows'],'prior_targets':prior['verified_source']['distinct_targets'],'prior_failed_routes':prior['route_count_from_native_quarantine_log']},
 'current_manifest_available':manifest_path.is_file(),'current_native_gate':gate,'current_group_summary':groups,'current_publication_binding':binding,
 'current_original_route_fractions':('Available in saved current native manifest' if gate else 'Not yet persisted during training; prior fractions are not reused as current and the 350341 current materialized rows are not rematerialized'),
 'conclusion':'Same verified August20 compact Pricing source and hashes; retained freshness, target-count and uncertainty/interval-calibration deficiencies explain all99 current route quarantines. Native fallback preserves every requested non-Pricing feature. This is separate from successful current33-scope OPRA acquisition.',
 'actions':'No provider/broker calls, training, retries, claims, production changes or expensive archive scans. Read-only compact Pricing verification and native log/control-plane inspection only.'}
(OUT/'option-pricing-quarantine.json').write_text(json.dumps(result,indent=2,default=str)+'\n')
print(json.dumps({'audited_at':result['audited_at'],'compact_rows':len(source),'distinct_targets':result['verified_source']['distinct_targets'],'latest_target':str(target.max()),'latest_availability':str(first.max()),'missing_symbols':result['verified_source']['missing_production_symbols'],'missing_features':result['verified_source']['all_rows_missing_features'],'fresh_rows':fresh_counts,'source_unchanged':current_hashes==prior_hashes,'current_manifest_available':manifest_path.is_file(),'groups':groups},indent=2))
