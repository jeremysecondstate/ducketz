"""One-time local audit preparation; never changes source or production state."""
from pathlib import Path
from datetime import datetime, timezone
import ast
import hashlib
import json
import subprocess

REPO = Path('C:/dev/ducketz')
OUT = Path(__file__).resolve().parent
SOURCE = REPO/'artifacts/analysis/overnight-20260930/fallback-enabled-verification'
PACKAGE = REPO/'artifacts/analysis/cross-horizon-fallback-20260930'
ORIGINAL = '20261002T040848.225355Z'
sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
read = lambda p: json.loads(Path(p).read_text(encoding='utf-8-sig'))
now = datetime.now(timezone.utc).isoformat()

names = ('verify_completed_run.py', 'verify_archive_history.py', 'verify_yg_completion.py',
         'fallback_checks.py', 'audit_environment.py', 'run_final_checks.py', 'validate_helpers.py')
assert not any((OUT/name).exists() for name in names), 'Refusing to replace prior audit helpers'
assert not (OUT/'preparation.json').exists(), 'Preparation already recorded'
head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip()
ownership = {'recorded_at': now, 'producer': 'loops-hourly-operations/final_verification',
    'reviewed_base_commit': head, 'owned_scope': str(OUT),
    'operations': [{'path': str(OUT/name), 'operation': 'add', 'owned': True} for name in names],
    'source_helpers': {name: sha(SOURCE/name) for name in names},
    'scope': 'Local verification helpers and evidence only; no shareable application change.'}
(OUT/'ownership.json').write_text(json.dumps(ownership, indent=2)+'\n', encoding='utf-8')

native = read(Path('C:/DATASTORE/ml/overnight-runs')/ORIGINAL/'stage-report.json')
assert native['deadline_at'].replace('+00:00', 'Z') == '2026-10-02T11:00:00Z'
assert native['archive_history'] is True and native['stock_price_source'] == 'xnas-itch-archive-v1'
assert native['gameplan_variant'] == 'YG' and native['probability_target_contract'] == 'raw-price-direction-v1'
manifest = read(PACKAGE/'candidate-manifest.json')
manifest_hash = sha(PACKAGE/'candidate-manifest.json')
for name in ('apply-progress.json', 'apply-receipt.json'):
    record = read(PACKAGE/name)
    assert record['status'] == 'APPLIED' and record['manifest_sha256'] == manifest_hash
    assert record['source_dependency_fingerprints'] == manifest['source_dependency_fingerprints']
    assert record.get('locks_released') is True
expected = dict(manifest['source_dependency_fingerprints'])
expected.update({item['path']: item['sha256'] for item in manifest['files']})
assert all(sha(REPO/name) == digest for name, digest in expected.items()), 'Installed package/source mismatch'

files = []
for name in names:
    value = (SOURCE/name).read_text(encoding='utf-8')
    value = value.replace('20261001T040644.667536Z', ORIGINAL)
    value = value.replace('2026-10-01T11:00:00Z', '2026-10-02T11:00:00Z')
    value = value.replace('default="2026-10-01"', 'default="2026-10-02"')
    value = value.replace("default='2026-10-01'", "default='2026-10-02'")
    value = value.replace('default="2026-09-30"', 'default="2026-10-01"')
    value = value.replace('artifacts/analysis/overnight-20260930/fallback-enabled-verification',
                          'artifacts/analysis/overnight-20261001/verification')
    value = value.replace('September 30 source / October 1 action', 'October 1 source / October 2 action')
    value = value.replace('September 30 full-run verifier', 'October 1 full-run verifier')
    value = value.replace('Offline October 1 fallback policy', 'Offline October 2 fallback policy')
    if name == 'validate_helpers.py':
        value = value.replace("day = '2026-10-01'", "day = '2026-10-02'")
    ast.parse(value, filename=str(OUT/name))
    (OUT/name).write_text(value, encoding='utf-8')
    files.append({'name': name, 'source_path': str(SOURCE/name), 'source_sha256': sha(SOURCE/name),
                  'adapted_sha256': sha(OUT/name)})

baseline = {path.relative_to(REPO).as_posix(): sha(path) for scope in ('ml', 'datafetching')
            for path in (REPO/scope).rglob('*.py') if '__pycache__' not in path.parts}
baseline.update(expected)
prior = read(SOURCE/'audit-environment-baseline.json')['files']
changes = [{'path': name, 'prior_sha256': prior.get(name), 'current_sha256': baseline.get(name)}
           for name in sorted(set(prior)|set(baseline)) if prior.get(name) != baseline.get(name)]
(OUT/'audit-environment-baseline.json').write_text(json.dumps({
    'recorded_at': now, 'repository': str(REPO), 'files': baseline,
    'candidate_manifest_sha256': manifest_hash,
    'deployment_record_sha256': {name: sha(PACKAGE/name) for name in ('apply-progress.json', 'apply-receipt.json')},
    'source': 'Fresh read-only fingerprints after exact APPLIED package and dependency verification.'
}, indent=2)+'\n', encoding='utf-8')
(OUT/'preparation.json').write_text(json.dumps({'prepared_at': now, 'files': files,
    'original_run': ORIGINAL, 'source_date': '2026-10-01', 'action_date': '2026-10-02',
    'deadline': '2026-10-02T11:00:00Z', 'reviewed_base_commit': head,
    'source_files_in_baseline': len(baseline), 'source_changes_since_previous_audit': changes,
    'policy': 'hierarchical-bearish-fallback-v1', 'production_mutations': 0,
    'limitations': ['Heavy audits require native COMPLETE and are not run during preparation.',
        'Provider metadata warning follow-up and controls/schedule/ledger preservation are separate root evidence.',
        'Current all-ml/datafetching source baseline detects concurrent changes; drift requires explicit review.']
}, indent=2)+'\n', encoding='utf-8')
print(json.dumps({'status': 'PREPARED_NOT_RUN', 'output': str(OUT), 'helpers': len(files),
                  'source_files': len(baseline), 'source_changes_since_previous_audit': changes}))
