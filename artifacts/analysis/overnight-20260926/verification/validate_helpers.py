"""Bounded preparation checks only; does not audit a publication or source archive."""
from pathlib import Path
from datetime import datetime, timezone
import ast
import hashlib
import json
import subprocess
import sys

OUT = Path(__file__).resolve().parent
REPO = Path('C:/dev/ducketz')
sys.path.insert(0, str(OUT))
from audit_environment import verify_environment

checks = []
trees = {}
for path in sorted(OUT.glob('*.py')):
    trees[path.name] = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
checks.append({'check': 'ALL_HELPER_AST_PARSE', 'status': 'PASS', 'files': len(trees)})
checks.append({'check': 'CODE_ENVIRONMENT_UNCHANGED', **verify_environment()})

fake = 'C:/DATASTORE/ml/overnight-runs/NOT_AN_EXISTING_NATIVE_RUN_PREPARATION_GUARD'
if Path(fake).exists():
    raise RuntimeError('Preparation guard path unexpectedly exists')
for script, arguments in (
    ('verify_completed_run.py', ['--overnight-run', fake]),
    ('audit_provider_completion.py', ['--terminal-run', fake]),
    ('run_final_checks.py', ['--overnight-run', fake]),
):
    completed = subprocess.run([sys.executable, '-B', str(OUT/script), *arguments], cwd=REPO,
                               capture_output=True, text=True, check=False)
    payload = json.loads(completed.stdout)
    assert completed.returncode == 2 and payload['status'] == 'NOT_READY_FOR_FINAL_VERIFICATION', script
    assert payload.get('heavy_checks_started', payload.get('archive_checks_started')) is False, script
    checks.append({'check': script+' missing COMPLETE guard', 'status': 'PASS',
                   'exit_code': completed.returncode, 'result': payload})

yg = (OUT/'verify_yg_completion.py').read_text(encoding='utf-8')
assert yg.index('if not ready:') < yg.index('verify_environment()') < yg.index('    import joblib')
checks.append({'check': 'YG_GUARD_PRECEDES_ENVIRONMENT_AND_MODEL_IMPORTS', 'status': 'PASS'})
value = (OUT/'verify_completed_run.py').read_text(encoding='utf-8')
assert 'default="2026-09-25"' in value and 'default="2026-09-28"' in value
assert 'default="2026-09-28T11:00:00Z"' in value and 'default="20260926T040723.834511Z"' in value
provider = (OUT/'audit_provider_completion.py').read_text(encoding='utf-8')
assert "SOURCE, END, ACTION = '2026-09-25', '2026-09-26', '2026-09-28'" in provider
checks.append({'check': 'FRIDAY_SOURCE_SATURDAY_PROVIDER_END_MONDAY_ACTION', 'status': 'PASS'})

numeric = next(n for n in trees['verify_yg_completion.py'].body if isinstance(n, ast.FunctionDef) and n.name == 'compare_saved_numbers')
require = next(n for n in trees['verify_yg_completion.py'].body if isinstance(n, ast.FunctionDef) and n.name == 'require')
namespace = {'math': __import__('math')}
exec(compile(ast.Module(body=[require, numeric], type_ignores=[]), '<bounded-numeric-guard>', 'exec'), namespace)
compare = namespace['compare_saved_numbers']
compare({'score': .25+1e-13}, {'score': .25}, 'bounded numeric check')
for candidate in ({}, {'score': .25001}, {'score': float('nan')}):
    try:
        compare(candidate, {'score': .25}, 'negative numeric check')
    except ValueError:
        pass
    else:
        raise AssertionError('Missing/changed/nonfinite saved metric was accepted')
checks.append({'check': 'SAVED_METRIC_GUARD_REJECTS_MISSING_CHANGED_NONFINITE', 'status': 'PASS'})

result = {'checked_at': datetime.now(timezone.utc).isoformat(), 'status': 'PASS', 'checks': checks,
          'scope': 'AST, source constants, missing-completion refusal, numeric report guard and code hashes only; no market/model/account reads.',
          'source_archive_checks_started': False, 'production_mutations': 0, 'orders_placed': 0}
(OUT/'helper-review-tests.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
preparation = json.loads((OUT/'preparation.json').read_text(encoding='utf-8'))
for entry in preparation['files']:
    entry['adapted_sha256'] = hashlib.sha256((OUT/entry['name']).read_bytes()).hexdigest()
preparation['additional_helpers'] = {name: hashlib.sha256((OUT/name).read_bytes()).hexdigest()
    for name in sorted(path.name for path in OUT.glob('*.py'))
    if name not in {entry['name'] for entry in preparation['files']}}
reviewed_drift = OUT/'reviewed-isolated-code-drift.json'
if reviewed_drift.exists():
    preparation['explicit_isolated_drift_review'] = {
        'path': str(reviewed_drift), 'sha256': hashlib.sha256(reviewed_drift.read_bytes()).hexdigest(),
        'original_environment_baseline_preserved': True}
preparation['reviewed_at'] = result['checked_at']
preparation['syntax_validation'] = 'ALL_HELPERS_AST_PARSE'
preparation['bounded_checks'] = 'helper-review-tests.json'
(OUT/'preparation.json').write_text(json.dumps(preparation, indent=2)+'\n', encoding='utf-8')
print(json.dumps({'status': result['status'], 'checks': len(checks), 'source_archive_checks_started': False}))
