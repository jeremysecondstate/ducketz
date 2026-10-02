"""Bind read-only CME warning diagnosis and selected model feature evidence."""
from pathlib import Path
from datetime import datetime,timezone
import hashlib,json
import pandas as pd
import pyarrow.parquet as pq

ROOT=Path('<LOCAL_DATASTORE>'); HERE=Path(__file__).resolve().parent
GP=ROOT/'ml/nightly-gameplan-runs/20260930T061501.586401Z'
LOOP=ROOT/'ml/runs/20260930T053953.049047Z'
def read(p):return json.loads(p.read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
metadata_path=HERE/'quality-metadata-20260930T072037Z.json'
metadata=read(metadata_path)
conditions=metadata['calls'][0]['response']; ranges=metadata['calls'][1]['response']
snapshot=read(HERE/'warning-review-20260930T054033Z.json')
manifest=read(GP/'manifest.json'); models=read(GP/'model-reports.json'); receipt=read(GP/'receipt.json')
features={key:{'admitted_count':item['features']['admitted_count'],'admitted_cme':[x for x in item['features']['admitted'] if x.startswith('cme__')],'admitted_features':item['features']['admitted']} for key,item in models.items()}
derived=ROOT/'pools/cme/features/cross-asset-context/databento/1h.parquet'
frame=pq.read_table(derived,columns=['window_start','window_end','available_at']).to_pandas()
loopmanifest=read(LOOP/'manifest.json')
source_binding=next(x for x in loopmanifest['input_files'] if x['path'].replace('\\','/')==str(derived.relative_to(ROOT)).replace('\\','/'))
sample_names=pq.read_schema(LOOP/'samples.parquet').names
cme_cols=[x for x in sample_names if x.startswith('cme__')]
samples=pq.read_table(LOOP/'samples.parquet',columns=['horizon','decision_timestamp',*cme_cols]).to_pandas()
current=samples[pd.to_datetime(samples.decision_timestamp,utc=True)>=pd.Timestamp('2026-09-29T00:00:00Z')]
partitions=[]
for schema in ['ohlcv-1m','bbo-1m','mbp-10']:
    paths=sorted((ROOT/'pools/cme/events/databento/context'/schema/'normalized').rglob('events.parquet'))
    partitions.append({'schema':schema,'count':len(paths),'last_partition_path':str(paths[-1]) if paths else None})
captures=[]
for item in snapshot['current_cme_captures']:
    req=item['current_request_metadata'][0]
    captures.append({'path':item['path'],'sha256_at_native_capture':item['sha256'],'schema':req['provider_schema'],'symbols':[r['symbol'] for r in item['symbols']],
        'rows':item['current_native_rows'],'effective_start':req['effective_range_start'],'effective_end':req['effective_range_end'],
        'limit_saturated':req['request_limit_saturated'],'rows_on_flagged_sep30_utc_day':sum(r['rows_after_stock_source_close'] for r in item['symbols'])})
checks={
    'metadata_gets_only':len(metadata['calls'])==2 and all(x['method']=='GET' and x['status']=='SUCCESS' for x in metadata['calls']),
    'gameplan_receipt_binds_manifest':receipt['manifest_checksum_sha256']==sha(GP/'manifest.json'),
    'model_reports_manifest_bound':manifest['output_files']['model-reports.json']['checksum_sha256']==sha(GP/'model-reports.json'),
    'all_four_models_exclude_cme':len(features)==4 and not any(x['admitted_cme'] for x in features.values()),
    'loop_b_derived_source_binding':source_binding['checksum_sha256']==sha(derived) and source_binding['size']==derived.stat().st_size,
    'loop_b_current_cme_values_missing':not current[cme_cols].notna().any().any(),
    'quality_flag_still_sep30_only':{r['date']:r['condition'] for r in conditions}=={'2026-09-27':'available','2026-09-28':'available','2026-09-29':'available','2026-09-30':'degraded'},
}
evidence_paths=[metadata_path,HERE/'warning-review-20260930T054033Z.json',HERE/'cme-derived-advisory.json',GP/'receipt.json',GP/'manifest.json',GP/'model-reports.json',LOOP/'publication.json',LOOP/'manifest.json',derived,
    Path('<LOCAL_CHECKOUT>/app/services/databento_cme_context.py'),Path('<LOCAL_CHECKOUT>/datafetching/databento_fetch.py'),Path('<LOCAL_CHECKOUT>/datafetching/cme_cross_asset_context.py'),Path('<LOCAL_CHECKOUT>/datafetching/cme_history.py')]
result={
    'reviewed_at':datetime.now(timezone.utc).isoformat(),'status':'CONFIRMED_VENDOR_FLAG_UNDISCLOSED_VENDOR_CAUSE_WITH_SEPARATE_LOCAL_ROUTING_DEFECT','checks':checks,
    'confirmed_warning_origin':'Databento SDK parses vendor HTTP X-Warning header in check_backend_warnings; native timeseries request surfaced the reduced-quality warning. It is not the locally generated stale-context advisory.',
    'provider_cause':'UNRESOLVED: dataset condition supplies date, condition and last_modified_date, with no reason, affected channel/symbol/schema or intraday interval. A still-open UTC day and delayed MBP publication are observations, not proof of cause.',
    'fresh_metadata':{'path':str(metadata_path),'observed_at':metadata['finished_at'],'conditions':conditions,'selected_schema_ranges':{x:ranges['schema'][x] for x in ['ohlcv-1m','bbo-1m','mbp-10','mbo']}},
    'flag_scope':'GLBX.MDP3 date2026-09-30UTC. Last night warnings occurred on OHLCV and BBO requests that overlap Sep30T00:00Z..04:10Z, after the completed Sep29 stock session close. The vendor flag does not identify which returned records are defective. MBP requests endedSep29 and did not emit this warning.',
    'warning_occurrences':9,'current_captures':captures,
    'gameplan_model_features':features,
    'loop_b_current_cme':{'samples_after_sep29_utc_start':len(current),'non_null_by_column':current[cme_cols].notna().sum().astype(int).to_dict(),'all_sample_non_null_by_column':samples[cme_cols].notna().sum().astype(int).to_dict(),'source_view_rows':len(frame),'source_first_window':str(frame.window_start.min()),'source_last_window':str(frame.window_end.max())},
    'local_routing_defect':{
        'confirmed':True,'provider_warning_cause':False,
        'description':'Inline fetch stores new CME rows in the hot macro files. The context reader chooses old partitioned events exclusively whenever any partition exists, so fresh hot captures are ignored. The old partition inventory ends September3/4 and repeatedly selects the September3 candidate.',
        'writer':'<LOCAL_CHECKOUT>/datafetching/databento_fetch.py:1241 (_fetch_cme_unlocked; save_macro_rows then materialize at1386; no persist_cme_event_history)',
        'reader':'<LOCAL_CHECKOUT>/datafetching/cme_cross_asset_context.py:444 (if partitioned ... else legacy hot rows)',
        'partition_inventory':partitions,
        'proposed_small_repair':'Merge and deterministically deduplicate both native normalized layouts in the shared reader, preserving receipt identity, source fields, freshness and saturation flags; alternatively use the common partition store from the inline writer. Prefer read-side repair only after confirming established natural keys and duplicate precedence.',
        'regression_test':'Temporary fixture with old partition plus newer hot file for each schema must include the newer rows without duplicate observations; saturated MBP and stale/future inputs must still fail. Test no-partition and no-hot-file compatibility.',
        'current_effect_limit':'Does not clear Sep30 vendor quality; even with fresh hot rows, nightly MBP is roughly8hours stale and limit-saturated. A corrected reader must still refuse that context. No model retraining or completed-run replay justified by this diagnosis alone.',
    },
    'safe_observability_cleanup':'On future existing nightly runs, save exact dataset-condition/schema-range metadata and selected feature-use evidence alongside each quality warning; label nonempty delivery separately from provider quality and model eligibility. This improves explanation without suppressing warnings or changing gates.',
    'official_sources':[
        {'url':'https://databento.com/docs/api-reference-historical/metadata/metadata-get-dataset-condition?historical=python&live=python','finding':'Degraded means data may be missing or incorrect. Response identifies UTCdate/condition/lastmodification only.'},
        {'url':'https://status.databento.com/','finding':'Read at current follow-up: all systems operational, no September30 incident reported; this does not supersede the dataset-quality endpoint.'},
        {'url':'https://issues.databento.com/','finding':'Official issue search found no September30-specific explanation; older GLBX issues do not establish this warning cause.'},
    ],
    'evidence':[{'path':str(p),'size':p.stat().st_size,'sha256':sha(p)} for p in evidence_paths],
    'production_writes':0,'code_or_configuration_edits':0,'provider_metadata_gets':2,'market_data_acquisitions':0,'broker_calls':0,'orders':0,
}
path=HERE/'rootcause-review.json'
path.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'path':str(path),'sha256':sha(path),'checks':checks,'loop_b_current_cme':result['loop_b_current_cme'],'model_admitted_cme':{k:v['admitted_cme'] for k,v in features.items()}},indent=2))
