"""Bounded saved-cohort diagnostics; no native price reload, fitting or writes."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import sys

REPO = Path('C:/dev/ducketz')
RUN = Path('C:/DATASTORE/ml/nightly-gameplan-runs/20261002T063014.996205Z')
OUT = Path(__file__).resolve().parent/'target-boundary-quality-review.json'
sys.path.insert(0, str(REPO))
from ml.gameplan_archive_integration import validate_archive_feature_clocks
import pandas as pd
import numpy as np

read = lambda p: json.loads(p.read_text())
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
assert not OUT.exists(), 'Preserve previous observation'
manifest, receipt, reports, archive = [read(RUN/n) for n in
    ('manifest.json','receipt.json','model-reports.json','archive-history.json')]
assert receipt['manifest_checksum_sha256'] == sha(RUN/'manifest.json')
columns = ['symbol','route','action_date','decision_timestamp','source_effective_cutoff',
    'source_feature_cutoff','source_action_start','source_bar_timestamp','source_bar_end_timestamp',
    'information_available_at','target_window_start','target_window_end','target_open','target_close',
    'observed_return','target_raw_price_direction','target','target_start_gap_seconds','target_end_gap_seconds',
    'target_boundary_aligned','target_price_source_contract','target_price_dataset',
    'probability_target_contract','target_role','execution_eligible']
result = {}
for group, report in reports.items():
    path = RUN/f'training-cohort-{group}.parquet'
    binding = manifest['output_files'][path.name]
    assert sha(path) == binding['checksum_sha256'] and path.stat().st_size == binding['size']
    frame = pd.read_parquet(path, columns=columns)
    boundary, cohort = report['target_boundary_quality'], archive['training_cohorts'][group]
    assert len(frame) == cohort['rows'] and cohort['rows']+cohort['quality_excluded_rows'] == boundary['aligned_rows']
    assert boundary['candidate_rows'] == boundary['aligned_rows']+boundary['excluded_rows']
    assert sum(boundary['excluded_rows_by_route'].values()) == boundary['excluded_rows']
    assert sum(boundary['admitted_rows_by_symbol'].values()) == sum(boundary['admitted_rows_by_route'].values()) == boundary['aligned_rows']
    assert boundary['enforced'] is True and boundary['maximum_boundary_gap_seconds'] == 300
    assert boundary['conflicting_minute_rows_excluded'] == 0
    assert frame.target_boundary_aligned.all()
    assert frame[['target_start_gap_seconds','target_end_gap_seconds']].abs().le(300).all().all()
    assert frame.target_price_source_contract.eq('xnas-itch-archive-v1').all()
    assert frame.target_price_dataset.eq('XNAS.ITCH').all()
    assert frame.probability_target_contract.eq('raw-price-direction-v1').all()
    assert np.isfinite(frame[['target_open','target_close','observed_return']]).all().all()
    assert frame[['target_open','target_close']].gt(0).all().all()
    assert np.allclose(frame.observed_return, frame.target_close/frame.target_open-1, rtol=1e-10, atol=1e-12)
    assert frame.target.eq(frame.target_raw_price_direction).all()
    assert frame.target.eq(frame.observed_return.gt(0).astype(int)).all()
    validate_archive_feature_clocks(frame)
    cutoff = pd.to_datetime(frame.source_effective_cutoff, utc=True)
    start, end = pd.to_datetime(frame.target_window_start, utc=True), pd.to_datetime(frame.target_window_end, utc=True)
    assert cutoff.loc[frame.execution_eligible].lt(start.loc[frame.execution_eligible]).all()
    late_to_start = cutoff.ge(start)
    assert frame.loc[late_to_start,'target_role'].eq('OPENING_GAP_RESEARCH').all()
    assert frame.loc[late_to_start,'route'].eq('1h@gap').all()
    assert not frame.loc[late_to_start,'execution_eligible'].any()
    assert start.lt(end).all() and end.le(pd.Timestamp(manifest['run_timestamp'])).all()
    assert not frame.duplicated(['symbol','action_date','route']).any()
    assert sha(path) == binding['checksum_sha256']
    result[group] = {'saved_rows':len(frame),'sha256':sha(path),'boundary_quality':boundary,
        'post_boundary_source_quality_excluded_rows':cohort['quality_excluded_rows'],
        'raw_direction_labels_and_price_returns_reproduced':True,'all_saved_boundaries_within_300_seconds':True,
        'native_causal_feature_clocks_verified':True,'execution_source_cutoff_before_target_start':True,
        'opening_gap_research_rows_starting_before_source_availability':int(late_to_start.sum()),
        'duplicate_natural_targets':0}
record = {'reviewed_at':datetime.now(timezone.utc).isoformat(),'status':'SAVED_COHORT_BOUNDARIES_VERIFIED',
    'publication':str(RUN),'action_date':receipt['action_date'],'groups':result,
    'finding':'The warnings describe intended five-minute endpoint exclusions. Saved source contracts, observed-price arithmetic, raw-direction labels, causal clocks and counts verify; no conflicting minute rows or concrete repair cause observed.',
    'helper_diagnosis':'The first unsaved probe incorrectly required source availability before every target start. Native non-entry opening-gap research spans prior17:00 through04:00 and can use causal prior daily evidence available17:05. This review applies the native causal-clock verifier to all rows and the target-start constraint only to execution rows.',
    'limitations':['Saved cohort columns, hashes and arithmetic verified; native observations/cohorts are not independently regenerated here.',
        'Full archive/model inference and complete native audit remain pending COMPLETE.'],
    'provider_calls':0,'production_mutations':0,'heavy_final_audit_started':False,
    'verifier':{'path':str(Path(__file__).resolve()),'sha256':sha(Path(__file__))}}
OUT.write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'status':record['status'],'rows':{g:x['saved_rows'] for g,x in result.items()},'output':str(OUT)}))
