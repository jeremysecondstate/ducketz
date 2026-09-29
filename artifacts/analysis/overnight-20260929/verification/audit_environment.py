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
    approved = {}
    review_path = OUT/'reviewed-isolated-code-drift.json'
    if review_path.exists():
        review = json.loads(review_path.read_text(encoding='utf-8'))
        if review.get('status') != 'REVIEWED_ISOLATED_FROM_NATIVE_STOCK_PIPELINE':
            raise RuntimeError('Invalid explicit code drift review status')
        approved = {item['path']: item for item in review['changes']}
    unreviewed, admitted = [], []
    for change in changes:
        item = approved.get(change['path'])
        actual = hashlib.sha256((repo/change['path']).read_bytes()).hexdigest() if (repo/change['path']).is_file() else None
        if (item and item.get('baseline_sha256') == saved['files'].get(change['path'])
                and item.get('reviewed_sha256') == actual and item.get('reason')):
            admitted.append(change['path'])
        else:
            unreviewed.append(change)
    if unreviewed:
        raise RuntimeError('AUDIT_IMPLEMENTATION_DRIFT_REQUIRES_REVIEW: '+json.dumps(unreviewed))
    return {'status': 'UNCHANGED_EXCEPT_EXACT_REVIEWED_ISOLATED_MODULES' if admitted else 'UNCHANGED',
            'code_files': len(saved['files']), 'baseline_recorded_at': saved['recorded_at'],
            'reviewed_isolated_drift_paths': admitted,
            'explicit_review': str(review_path) if admitted else None}


if __name__ == '__main__':
    print(json.dumps(verify_environment()))
