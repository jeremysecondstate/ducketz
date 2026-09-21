"""Bounded saved-artifact audit; no broker, training, publication or ledger writes."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd

ROOT = Path('C:/DATASTORE')
OUT = Path(__file__).resolve().parent
RUN = ROOT/'ml/overnight-runs/20260919T040748.657031Z'
def read(path):
    return json.loads(path.read_text(encoding='utf-8'))
def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
def checked(path, evidence):
    assert path.stat().st_size == evidence['size'], str(path)
    assert sha(path) == evidence['checksum_sha256'], str(path)

report, receipt = read(RUN/'stage-report.json'), read(RUN/'receipt.json')
checked(RUN/'stage-report.json', {'size': receipt['stage_report_size'], 'checksum_sha256': receipt['stage_report_checksum_sha256']})
for name, evidence in receipt['logs'].items():
    checked(RUN/name, evidence)
assert report['status'] == receipt['status'] == 'FAILED'
assert report['failed_stage'] == 'gameplan_trade_planning'
assert report['orders_placed'] == receipt['orders_placed'] == 0
assert report['deadline_at'] == '2026-09-21T11:00:00+00:00'
gp = ROOT/report['enrichment_gameplan']['run_path']
assert sha(gp/'receipt.json') == report['enrichment_gameplan']['receipt_sha256']
gr, manifest = read(gp/'receipt.json'), read(gp/'manifest.json')
assert sha(gp/'manifest.json') == gr['manifest_checksum_sha256']
for name, evidence in manifest['output_files'].items():
    checked(gp/name, evidence)
symbols = [line.strip() for line in Path('C:/dev/ducketz/datafetching/watchlist.txt').read_text().splitlines() if line.strip() and not line.startswith('#')]
activation = read(Path('C:/dev/ducketz/artifacts/analysis/research-onboarding-20260913/activation.json'))
assert activation['status'] == 'ACTIVE' and set(activation['symbols']) == set(symbols)
counts = {}
for name in ('forecasts.parquet', 'option-strategy-intents.parquet'):
    frame = pd.read_parquet(gp/name)
    by_symbol = frame.groupby('symbol').size().to_dict()
    assert set(by_symbol) == set(symbols) and set(by_symbol.values()) == {24}
    counts[name] = {'rows': len(frame), 'per_symbol': by_symbol}
forecasts = pd.read_parquet(gp/'forecasts.parquet')
models = forecasts.groupby('model_group')['model_status'].value_counts().to_dict()
model_statuses = {group: forecasts.loc[forecasts.model_group.eq(group), 'model_status'].value_counts().to_dict() for group in sorted(forecasts.model_group.unique())}
enrichment_run = ROOT/'ml/stock-trader-model-runs/20260919T061839.948292Z'
enrichment = read(enrichment_run/'training-report.json')
assert enrichment['source_gameplan_run'] == report['enrichment_gameplan']['run_path']
for name, checksum in enrichment['source_files'].items():
    assert sha(ROOT/name) == checksum
pointer_names = ('overnight-latest','nightly-gameplan-latest','stock-trader-model-latest','gameplan-trade-plan-latest','gameplan-actuals-review-latest','gameplan-evaluation-latest')
pointers = {name: {'sha256':sha(ROOT/'ml'/name/'run.json'), 'value':read(ROOT/'ml'/name/'run.json')} for name in pointer_names}
evaluation = ROOT/pointers['gameplan-evaluation-latest']['value']['current']['run_path']
evsummary = read(evaluation/'summary.json')
snap = read(OUT/'account-snapshot.json')
result = {
    'verified_at':datetime.now(timezone.utc).isoformat(),
    'status':'SAVED_UPSTREAM_VERIFIED_NATIVE_TAIL_BLOCKED',
    'full_native_completion_verified':False,
    'native_run':str(RUN), 'native_status':report['status'],
    'completed_stages':[s['stage'] for s in report['stages'] if s['status']=='COMPLETE'],
    'failed_stage':report['failed_stage'], 'pending_stage':'gameplan_actuals_review',
    'source_session':'2026-09-18', 'action_date':gr['action_date'], 'deadline_at':report['deadline_at'],
    'publication':str(gp),'counts':counts,'model_statuses':model_statuses,
    'model_assessment_note':'All four pass recorded v2 tolerances, none beats its baseline; prior exact score reproduction retained.',
    'enrichment':{k:{n:v.get(n) for n in ('status','fitted_scope_count','qualified_scope_count')} for k,v in enrichment['horizons'].items()},
    'publication_opra_coverage':manifest['configuration']['opra_history'],
    'current_cumulative_summary':evsummary['all_saved_gameplans'],
    'current_snapshot':{k:snap[k] for k in ('observed_at','available_cash','held_shares','working_order_count','ownership')},
    'production_pointers':pointers,
    'orders_placed':0,'ledger_mutations':0,'errors':[],
}
(OUT/'verification.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
print(json.dumps({k:result[k] for k in ('status','completed_stages','failed_stage','pending_stage','model_statuses','enrichment','errors')}))
