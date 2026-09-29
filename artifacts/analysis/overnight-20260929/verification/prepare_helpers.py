"""Prepare read-only Sep28-source/Sep29-action audits; no production writes."""
from pathlib import Path
from datetime import datetime, timezone
import ast
import hashlib
import json
import subprocess

REPO = Path('C:/dev/ducketz')
OUT = Path(__file__).resolve().parent
OLD = REPO/'artifacts/analysis/overnight-20260926/verification'
ROOT_RUN = '20260929T040727.779758Z'
CYCLE = '20260929T040728.646690Z-pid62304'
BASELINE = '20260926T061604.318956Z'
NAMES = ('verify_completed_run.py', 'verify_yg_completion.py', 'verify_archive_history.py',
         'audit_provider_completion.py', 'provider_advisories.py', 'review_models.py',
         'audit_environment.py', 'run_final_checks.py', 'validate_helpers.py')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


if (OUT/'audit-environment-baseline.json').exists():
    raise RuntimeError('Baseline already exists: never silently rebaseline code.')

# Snapshot current source bytes before any audit begins. This is not a clean-tree
# assertion: concurrent edits remain untouched and are included exactly as seen.
code_paths = sorted([*REPO.glob('ml/**/*.py'), *REPO.glob('datafetching/**/*.py'),
    REPO/'docs/loops-system-analysis/NIGHTLY_GAMEPLAN.md',
    REPO/'docs/loops-system-analysis/INDEPENDENT_STOCK_HORIZONS.md',
    REPO/'datafetching/watchlist.txt'])
code = {p.relative_to(REPO).as_posix(): sha(p) for p in code_paths if '__pycache__' not in p.parts}
snapshot = {'recorded_at': datetime.now(timezone.utc).isoformat(), 'repository': str(REPO),
    'git_head': subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO,text=True).strip(),
    'scope': 'Python sources under ml/datafetching, native contract documents and watchlist. No secrets or runtime configuration values.',
    'capture_timing': 'After native launch during first Loop A stage, before all model/publication stages.',
    'files': code}
(OUT/'audit-environment-baseline.json').write_text(json.dumps(snapshot,indent=2)+'\n',encoding='utf-8')
files = []
for name in NAMES:
    original = OLD/name
    value = original.read_text(encoding='utf-8')
    value = value.replace('overnight-20260926/verification', 'overnight-20260929/verification')
    value = value.replace('20260926T040723.834511Z', ROOT_RUN)
    value = value.replace('20260926T040724.729021Z-pid46624', CYCLE)
    value = value.replace('20260925T060654.631426Z', BASELINE)
    value = value.replace('2026-09-28T11:00:00Z', '2026-09-29T11:00:00Z')
    value = value.replace('2026-09-28', '__ACTION__').replace('2026-09-25', '2026-09-28').replace('__ACTION__', '2026-09-29')
    value = value.replace('September 28', '__ACTION_TEXT__').replace('September 25', 'September 28').replace('__ACTION_TEXT__', 'September 29')
    if name == 'audit_provider_completion.py':
        old = "SOURCE, END, ACTION = '2026-09-28', '2026-09-26', '2026-09-29'"
        assert value.count(old) == 1
        value = value.replace(old, "SOURCE, END, ACTION = '2026-09-28', '2026-09-29', '2026-09-29'")
    if name == 'provider_advisories.py':
        value = value.replace('overnight-20260925/verification/provider-completion.json',
                              'overnight-20260926/verification/provider-completion.json')
    if name == 'validate_helpers.py':
        value = value.replace("SOURCE, END, ACTION = '2026-09-28', '2026-09-26', '2026-09-29'",
                              "SOURCE, END, ACTION = '2026-09-28', '2026-09-29', '2026-09-29'")
        value = value.replace('FRIDAY_SOURCE_SATURDAY_PROVIDER_END_MONDAY_ACTION',
                              'MONDAY_SOURCE_TUESDAY_PROVIDER_END_TUESDAY_ACTION')
    ast.parse(value, filename=name)
    (OUT/name).write_text(value,encoding='utf-8')
    files.append({'name':name,'copied_from':str(original),'original_sha256':sha(original),'adapted_sha256':sha(OUT/name)})

previous = json.loads((OLD/'audit-environment-baseline.json').read_text(encoding='utf-8'))
changes = [{'path':p,'previous_sha256':previous['files'].get(p),'current_sha256':code.get(p)}
           for p in sorted(set(previous['files'])|set(code)) if previous['files'].get(p)!=code.get(p)]
preparation = {'prepared_at':datetime.now(timezone.utc).isoformat(), 'status':'PREPARED_NOT_EXECUTED',
    'native_original_run':'C:/DATASTORE/ml/overnight-runs/'+ROOT_RUN,
    'expected_loop_a_cycle':CYCLE, 'source_session':'2026-09-28','action_date':'2026-09-29',
    'provider_exclusive_end':'2026-09-29','deadline_at':'2026-09-29T11:00:00Z',
    'target_completion_pacific':'2026-09-29T03:30:00-07:00',
    'baseline_publication':'C:/DATASTORE/ml/nightly-gameplan-runs/'+BASELINE,
    'expected_symbols':['AAPL','AMZN','GOOG','MU','NVDA','SNDK','COST','CROX','PATH','TWST','IONQ'],
    'expected_forecasts':264,'expected_intents':264,'expected_entry_forecasts':209,'expected_opra_cursors':33,
    'syntax_validation':'ALL_COPIED_HELPERS_AST_PARSE','files':files,'code_inventory_files':len(code),
    'changes_from_prior_environment':changes,
    'code_drift_policy':'Fail on uncaptured source drift; inspect and record exact reviewed bytes before any exception.',
    'production_mutations':0,'pipeline_starts':0,'provider_calls':0,'orders_placed':0}
(OUT/'preparation.json').write_text(json.dumps(preparation,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'status':preparation['status'],'helpers':len(files),'code_files':len(code),'changed_from_prior':len(changes)}))
