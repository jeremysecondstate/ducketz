"""Read-only exact baseline and current APPLIED deployment guard."""
from pathlib import Path
import hashlib
import json

OUT = Path(__file__).resolve().parent
sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
read = lambda p: json.loads(Path(p).read_text(encoding='utf-8-sig'))


def verify_environment():
    saved = read(OUT/'audit-environment-baseline.json')
    repo = Path(saved['repository'])
    package = repo/'artifacts/analysis/cross-horizon-fallback-20260930'
    manifest_hash = sha(package/'candidate-manifest.json')
    if manifest_hash != saved['candidate_manifest_sha256']:
        raise RuntimeError('Candidate manifest changed after verifier preparation')
    manifest = read(package/'candidate-manifest.json')
    for name, expected in saved['deployment_record_sha256'].items():
        record = read(package/name)
        if (sha(package/name) != expected or record.get('status') != 'APPLIED'
                or record.get('manifest_sha256') != manifest_hash or record.get('locks_released') is not True
                or record.get('source_dependency_fingerprints') != manifest['source_dependency_fingerprints']):
            raise RuntimeError('Current deployment progress/receipt must exactly match APPLIED baseline: '+name)
    changes = [{'path': name, 'expected_sha256': expected, 'actual_sha256': sha(repo/name) if (repo/name).is_file() else None}
               for name, expected in saved['files'].items()
               if not (repo/name).is_file() or sha(repo/name) != expected]
    existing = {p.relative_to(repo).as_posix() for scope in ('ml', 'datafetching')
                for p in (repo/scope).rglob('*.py') if '__pycache__' not in p.parts}
    changes += [{'path': name, 'status': 'ADDED'} for name in sorted(existing-set(saved['files']))]
    if changes:
        raise RuntimeError('AUDIT_IMPLEMENTATION_DRIFT_REQUIRES_REVIEW: '+json.dumps(changes))
    return {'status': 'UNCHANGED', 'source_files': len(saved['files']), 'baseline_recorded_at': saved['recorded_at'],
            'candidate_manifest_sha256': manifest_hash, 'current_apply_progress_and_receipt': 'EXACT_APPLIED'}


if __name__ == '__main__':
    print(json.dumps(verify_environment()))
