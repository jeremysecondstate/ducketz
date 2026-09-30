"""Capture baseline and adapt prior read-only helpers for Sep29/Sep30."""
from pathlib import Path
from datetime import datetime, timezone
import ast
import hashlib
import json
import subprocess

REPO = Path('C:/dev/ducketz')
OUT = Path(__file__).resolve().parent
OLD = REPO/'artifacts/analysis/overnight-20260929/verification'
NATIVE = '20260930T040723.733025Z'
CYCLE = '20260930T040724.527431Z-pid61720'
NAMES = ('verify_completed_run.py','verify_yg_completion.py','verify_archive_history.py',
    'audit_provider_completion.py','provider_advisories.py','review_models.py',
    'audit_environment.py','run_final_checks.py','validate_helpers.py',
    'audit_optional_pricing.py','preliminary_directional_review.py',
    'preliminary_sizing_review.py','watch_reviews.py','review_loop_b_weekly_prefix.py')

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

if (OUT/'audit-environment-baseline.json').exists():
    raise RuntimeError('Refusing to overwrite captured baseline')
paths = sorted([*REPO.glob('ml/**/*.py'), *REPO.glob('datafetching/**/*.py'),
    REPO/'docs/loops-system-analysis/NIGHTLY_GAMEPLAN.md',
    REPO/'docs/loops-system-analysis/INDEPENDENT_STOCK_HORIZONS.md',
    REPO/'datafetching/watchlist.txt'])
files = {p.relative_to(REPO).as_posix():sha(p) for p in paths if '__pycache__' not in p.parts}
snapshot = {'recorded_at':datetime.now(timezone.utc).isoformat(),'repository':str(REPO),
    'git_head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO,text=True).strip(),
    'capture_timing':'During first native Loop A stage, before all model/publication stages.',
    'scope':'Python ml/datafetching sources, native contracts and watchlist; concurrent edits preserved.',
    'files':files}
(OUT/'audit-environment-baseline.json').write_text(json.dumps(snapshot,indent=2)+'\n',encoding='utf-8')
adapted=[]
for name in NAMES:
    original=OLD/name
    value=original.read_text(encoding='utf-8')
    value=value.replace('overnight-20260929/verification','overnight-20260930/verification')
    value=value.replace('20260929T040727.779758Z',NATIVE)
    value=value.replace('20260929T040728.646690Z-pid62304',CYCLE)
    value=value.replace('20260926T061604.318956Z','20260929T061339.593647Z')
    value=value.replace('2026-09-29','__ACTION__').replace('2026-09-28','2026-09-29').replace('__ACTION__','2026-09-30')
    value=value.replace('September 29','__ACTION_TEXT__').replace('September 28','September 29').replace('__ACTION_TEXT__','September 30')
    value=value.replace('MONDAY_SOURCE_TUESDAY_PROVIDER_END_TUESDAY_ACTION','TUESDAY_SOURCE_WEDNESDAY_PROVIDER_END_WEDNESDAY_ACTION')
    if name=='provider_advisories.py':
        value=value.replace('overnight-20260926/verification/provider-completion.json','overnight-20260929/verification/provider-completion.json')
    if name=='audit_optional_pricing.py':
        value=value.replace('overnight-20260926/preflight/pricing-gate-review.json','overnight-20260929/verification/pricing-gate-review.json')
    ast.parse(value,filename=name)
    (OUT/name).write_text(value,encoding='utf-8')
    adapted.append({'name':name,'copied_from':str(original),'original_sha256':sha(original),'adapted_sha256':sha(OUT/name)})
prior=json.loads((OLD/'audit-environment-baseline.json').read_text(encoding='utf-8'))
changes=[{'path':p,'previous_sha256':prior['files'].get(p),'current_sha256':files.get(p)}
    for p in sorted(set(prior['files'])|set(files)) if prior['files'].get(p)!=files.get(p)]
result={'prepared_at':datetime.now(timezone.utc).isoformat(),'status':'PREPARED_NOT_EXECUTED',
    'native_original_run':'C:/DATASTORE/ml/overnight-runs/'+NATIVE,'expected_loop_a_cycle':CYCLE,
    'source_session':'2026-09-29','action_date':'2026-09-30','provider_exclusive_end':'2026-09-30',
    'deadline_at':'2026-09-30T11:00:00Z','target_completion_pacific':'2026-09-30T03:30:00-07:00',
    'baseline_publication':'C:/DATASTORE/ml/nightly-gameplan-runs/20260929T061339.593647Z',
    'expected_symbols':['AAPL','AMZN','GOOG','MU','NVDA','SNDK','COST','CROX','PATH','TWST','IONQ'],
    'expected_forecasts':264,'expected_intents':264,'expected_entry_forecasts':209,'expected_opra_cursors':33,
    'files':adapted,'code_inventory_files':len(files),'changes_from_prior_environment':changes,
    'production_mutations':0,'provider_calls':0,'pipeline_starts':0,'orders_placed':0}
(OUT/'preparation.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'status':result['status'],'helpers':len(adapted),'code_files':len(files),'changed_from_prior':changes}))
