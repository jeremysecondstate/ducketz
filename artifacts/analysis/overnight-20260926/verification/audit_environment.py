"""Read-only verification environment drift guard; never silently rebaseline."""
from pathlib import Path
import hashlib
import json

OUT = Path(__file__).resolve().parent


def verify_environment():
    saved = json.loads((OUT/'audit-environment-baseline.json').read_text(encoding='utf-8'))
    repo = Path(saved['repository'])
    changes = []
    for relative, expected in saved['files'].items():
        path = repo/relative
        if not path.is_file():
            changes.append({'path': relative, 'status': 'MISSING'})
        else:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != expected:
                changes.append({'path': relative, 'status': 'CHANGED', 'expected': expected, 'actual': actual})
    existing = {path.relative_to(repo).as_posix() for scope in ('ml', 'datafetching')
                for path in (repo/scope).rglob('*.py') if '__pycache__' not in path.parts}
    changes.extend({'path': path, 'status': 'ADDED'} for path in sorted(existing-set(saved['files'])))
    if changes:
        raise RuntimeError('AUDIT_IMPLEMENTATION_DRIFT_REQUIRES_REVIEW: '+json.dumps(changes))
    return {'status': 'UNCHANGED', 'code_files': len(saved['files']), 'baseline_recorded_at': saved['recorded_at']}


if __name__ == '__main__':
    print(json.dumps(verify_environment()))
