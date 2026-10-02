"""One-time artifact-only adaptation; refuses existing destination helpers."""
from pathlib import Path
from datetime import datetime, timezone
import ast
import hashlib
import json

REPO = Path('<LOCAL_CHECKOUT>')
OUT = Path(__file__).resolve().parent
SOURCE = REPO/'artifacts/analysis/overnight-20260930/verification'
PACKAGE = REPO/'artifacts/analysis/cross-horizon-fallback-20260930'
sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
read = lambda p: json.loads(Path(p).read_text(encoding='utf-8-sig'))


def replace_once(value, old, new):
    assert value.count(old) == 1, (old, value.count(old))
    return value.replace(old, new)


files = []
for name in ('verify_completed_run.py', 'verify_archive_history.py', 'verify_yg_completion.py'):
    destination = OUT/name
    assert not destination.exists(), 'Refusing to overwrite a prior helper: '+name
    value = (SOURCE/name).read_text(encoding='utf-8')
    value = value.replace('20260930T040723.733025Z', '20261001T040644.667536Z')
    value = value.replace('2026-09-30T11:00:00Z', '2026-10-01T11:00:00Z')
    value = value.replace('default="2026-09-30"', 'default="2026-10-01"')
    value = value.replace("default='2026-09-30'", "default='2026-10-01'")
    value = value.replace('default="2026-09-29"', 'default="2026-09-30"')
    value = value.replace('artifacts/analysis/overnight-20260930/verification',
                          'artifacts/analysis/overnight-20260930/fallback-enabled-verification')
    if name == 'verify_completed_run.py':
        start = value.index('"""')
        end = value.index('"""', start+3)+3
        value = ('"""Offline audit of September 30 source / October 1 action, including the\n'
                 'approved prospective fallback. Uses manifest-bound saved snapshots and local\n'
                 'archives only; no providers, broker, trading, claims, training or production writes.\n'
                 'Inherited full checks preserve explicit research and unavailable statuses.\n"""'+value[end:])
        value = replace_once(value, '    context.update(publication=pub, forecasts=forecasts, symbols=symbols)',
            '    from fallback_checks import verify_fallback_binding\n'
            '    fallback_policy = verify_fallback_binding(pub, read, require)\n'
            '    context.update(publication=pub, forecasts=forecasts, symbols=symbols, fallback_policy=fallback_policy)')
        value = replace_once(value, '            "forecast_rows": len(forecasts), "intent_rows": len(intents),',
            '            "cross_horizon_fallback_policy": fallback_policy,\n'
            '            "forecast_rows": len(forecasts), "intent_rows": len(intents),')
        value = replace_once(value, '    ledger = read(run / "direction-ledger.json")',
            '    ledger = read(run / "direction-ledger.json")\n'
            '    from fallback_checks import verify_planning_policy\n'
            '    verify_planning_policy(context["fallback_policy"], report, receipt, manifest, ledger, require)')
        value = replace_once(value, '                                                        signal_driven=True)',
            '                                                        signal_driven=True,\n'
            '                                                        cross_horizon_fallback_policy=context["fallback_policy"])')
        value = replace_once(value, '    signal_holdings = check_signal_driven_holdings(rows, snapshot, ledger)',
            '    from fallback_checks import verify_holdings_and_fallback\n'
            '    signal_holdings = verify_holdings_and_fallback(rows, snapshot, ledger,\n'
            '                        symbols=context["symbols"], require=require, same_number=same_number)')
        value = replace_once(value, '              or column.startswith("projected_available_shares_after")]',
            '              or column.startswith("projected_available_shares_after") or column.startswith("fallback_")]')
    ast.parse(value, filename=str(destination))
    destination.write_text(value, encoding='utf-8')
    files.append({'name': name, 'source_path': str(SOURCE/name), 'source_sha256': sha(SOURCE/name),
                  'adapted_sha256': sha(destination)})

manifest = read(PACKAGE/'candidate-manifest.json')
manifest_hash = sha(PACKAGE/'candidate-manifest.json')
for name in ('apply-progress.json', 'apply-receipt.json'):
    record = read(PACKAGE/name)
    assert record['status'] == 'APPLIED' and record['manifest_sha256'] == manifest_hash
    assert record['source_dependency_fingerprints'] == manifest['source_dependency_fingerprints']
expected = dict(manifest['source_dependency_fingerprints'])
expected.update({f['path']: f['sha256'] for f in manifest['files']})
assert all(sha(REPO/name) == digest for name, digest in expected.items())
baseline = {path.relative_to(REPO).as_posix(): sha(path) for scope in ('ml', 'datafetching')
            for path in (REPO/scope).rglob('*.py') if '__pycache__' not in path.parts}
baseline.update(expected)
now = datetime.now(timezone.utc).isoformat()
(OUT/'audit-environment-baseline.json').write_text(json.dumps({
    'recorded_at': now, 'repository': str(REPO), 'files': baseline,
    'candidate_manifest_sha256': manifest_hash,
    'deployment_record_sha256': {name: sha(PACKAGE/name) for name in ('apply-progress.json', 'apply-receipt.json')},
    'source': 'Fresh read-only snapshot after exact installed candidate/dependency validation; no production writes.'
}, indent=2)+'\n', encoding='utf-8')
(OUT/'preparation.json').write_text(json.dumps({'prepared_at': now, 'files': files,
    'original_run': '20261001T040644.667536Z', 'source_date': '2026-09-30', 'action_date': '2026-10-01',
    'deadline': '2026-10-01T11:00:00Z', 'source_files_in_baseline': len(baseline),
    'policy': 'hierarchical-bearish-fallback-v1', 'production_mutations': 0,
    'limitations': ['Heavy publication/archive/model checks require native COMPLETE and have not run.',
                    'Provider-warning read-only metadata follow-up and controls/schedule/ledger preservation are separate operator evidence.']
}, indent=2)+'\n', encoding='utf-8')
print(json.dumps({'status': 'PREPARED_NOT_RUN', 'output': str(OUT), 'helpers': len(files), 'source_files': len(baseline)}))
