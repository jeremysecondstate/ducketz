"""Prepare this operation's read-only verification helpers; no production writes."""
from pathlib import Path
from datetime import datetime, timezone
import ast
import hashlib
import json
import subprocess

REPO = Path('C:/dev/ducketz')
OUT = Path(__file__).resolve().parent
OLD = REPO / 'artifacts/analysis/overnight-20260925/verification'
ROOT_RUN = '20260926T040723.834511Z'
NAMES = ('verify_completed_run.py', 'verify_yg_completion.py', 'verify_archive_history.py',
         'audit_provider_completion.py', 'provider_advisories.py', 'review_models.py')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError('Expected one adaptation point: ' + old)
    return text.replace(old, new)


files = []
if (OUT/'audit-environment-baseline.json').exists():
    raise RuntimeError('Preparation already recorded; review and edit existing helpers without silently rebaselining code.')
for name in NAMES:
    path = OLD/name
    value = path.read_text(encoding='utf-8')
    value = value.replace('overnight-20260925/verification', 'overnight-20260926/verification')
    value = value.replace('20260925T040822.112032Z', ROOT_RUN)
    value = value.replace('20260925T040822.974281Z-pid22132', '20260926T040724.729021Z-pid46624')
    value = value.replace('2026-09-25T11:00:00Z', '2026-09-28T11:00:00Z')
    # Map action and source dates separately; Friday's exclusive provider end is Saturday.
    value = value.replace('2026-09-25', '__NEW_ACTION__').replace('2026-09-24', '2026-09-25').replace('__NEW_ACTION__', '2026-09-28')
    value = value.replace('September 24 source / September 25 action', 'September 25 source / September 28 action')
    value = value.replace('September 25', 'September 28')
    value = value.replace('September 28 source / September 28 action', 'September 25 source / September 28 action')
    if name == 'audit_provider_completion.py':
        value = replace_once(value, "SOURCE, END, ACTION = '2026-09-25', '2026-09-28', '2026-09-28'",
                             "SOURCE, END, ACTION = '2026-09-25', '2026-09-26', '2026-09-28'")
        value = replace_once(value, "if (terminal_receipt.get('stage_report_checksum_sha256')", 
                             "from audit_environment import verify_environment\nverify_environment()\nif (terminal_receipt.get('stage_report_checksum_sha256')")
    if name == 'provider_advisories.py':
        value = replace_once(value, 'overnight-20260924/resumed-provider/provider-completion.json',
                             'overnight-20260925/verification/provider-completion.json')
    if name == 'review_models.py':
        value = value.replace('20260924T062615.050242Z', '20260925T060654.631426Z')
        value = replace_once(value, '    require(terminal_receipt.get("stage_report_checksum_sha256")',
                             '    from audit_environment import verify_environment\n    verify_environment()\n    require(terminal_receipt.get("stage_report_checksum_sha256")')
    if name == 'verify_completed_run.py':
        value = replace_once(value, 'sys.path.insert(0, str(args.repository.resolve()))',
                             'from audit_environment import verify_environment\nverify_environment()\nsys.path.insert(0, str(args.repository.resolve()))')
    if name == 'verify_yg_completion.py':
        value = replace_once(value, '    # Imports happen after the small native completion guard.',
                             '    from audit_environment import verify_environment\n    verify_environment()\n    # Imports happen after the small native completion guard.')
    ast.parse(value, filename=name)
    (OUT/name).write_text(value, encoding='utf-8')
    files.append({'name': name, 'copied_from': str(path), 'original_sha256': sha(path), 'adapted_sha256': sha(OUT/name)})

# Inventory code only. No credentials, runtime settings, market files or account state.
code_paths = sorted([*REPO.glob('ml/**/*.py'), *REPO.glob('datafetching/**/*.py'),
    REPO/'docs/loops-system-analysis/NIGHTLY_GAMEPLAN.md',
    REPO/'docs/loops-system-analysis/INDEPENDENT_STOCK_HORIZONS.md',
    REPO/'datafetching/watchlist.txt'])
code = {path.relative_to(REPO).as_posix(): sha(path) for path in code_paths if '__pycache__' not in path.parts}
snapshot = {'recorded_at': datetime.now(timezone.utc).isoformat(), 'repository': str(REPO),
            'git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip(),
            'scope': 'Python sources under ml/datafetching plus native contract documents and watchlist. No secrets or configuration values read.',
            'files': code}
(OUT/'audit-environment-baseline.json').write_text(json.dumps(snapshot, indent=2)+'\n', encoding='utf-8')
preparation = {'prepared_at': datetime.now(timezone.utc).isoformat(), 'status': 'PREPARED_NOT_EXECUTED',
    'native_original_run': 'C:/DATASTORE/ml/overnight-runs/'+ROOT_RUN,
    'source_session': '2026-09-25', 'action_date': '2026-09-28',
    'provider_exclusive_end': '2026-09-26', 'deadline_at': '2026-09-28T11:00:00Z',
    'target_completion_pacific': '2026-09-26T03:30:00-07:00',
    'expected_loop_a_cycle': '20260926T040724.729021Z-pid46624',
    'baseline_publication': 'C:/DATASTORE/ml/nightly-gameplan-runs/20260925T060654.631426Z',
    'syntax_validation': 'ALL_SIX_AST_PARSE', 'files': files,
    'code_inventory_files': len(code), 'code_drift_policy': 'Explicit failure before audit if captured Python/doc/watchlist bytes changed; inspect cause and revise only with evidence. No automatic rebaseline.',
    'production_mutations': 0, 'pipeline_starts': 0, 'provider_calls': 0, 'orders_placed': 0}
(OUT/'preparation.json').write_text(json.dumps(preparation, indent=2)+'\n', encoding='utf-8')
print(json.dumps({'status': preparation['status'], 'helpers': len(files), 'code_files': len(code)}))
