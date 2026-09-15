"""Combine the completed full audit with its bounded, provenance-preserving recheck."""
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

root = Path(__file__).resolve().parent
initial_path = root / 'final-verification-initial.json'
recheck_path = root / 'fetch-scope-recheck.json'
initial = json.loads(initial_path.read_text(encoding='utf-8-sig'))
recheck = json.loads(recheck_path.read_text(encoding='utf-8-sig'))
expected_error = {'check': 'fetch_scope_log', 'error': 'ValueError: OPRA per-scope completions differ'}
assert initial['errors'] == [expected_error]
assert recheck['status'] == 'VERIFIED' and not recheck['errors']
assert recheck['overnight_run'] == initial['overnight_run']
assert recheck['checks']['overnight'] == initial['checks']['overnight']
assert recheck['checks']['fetch_scope_log']['status'] == 'VERIFIED'
assert all(v['status'] == 'VERIFIED' for k, v in initial['checks'].items() if k != 'fetch_scope_log')

result = copy.deepcopy(initial)
result['checks']['fetch_scope_log'] = recheck['checks']['fetch_scope_log']
result['errors'] = []
result['status'] = 'VERIFIED_WITH_COVERAGE_NOTES'
result['reconciled_at'] = datetime.now(timezone.utc).isoformat()
result['verification_provenance'] = {
    'full_audit': {'path': str(initial_path), 'sha256': hashlib.sha256(initial_path.read_bytes()).hexdigest(), 'verified_at': initial['verified_at']},
    'bounded_recheck': {'path': str(recheck_path), 'sha256': hashlib.sha256(recheck_path.read_bytes()).hexdigest(), 'verified_at': recheck['verified_at'], 'scope': recheck['verification_scope']},
    'resolution': 'Artifact verifier corrected to validate the exact union of successful OPRA Historical and Live replay scopes. All 33 required scopes completed via Live replay. The recheck confirmed unchanged overnight ancestry and log hashes. Ten previously passing checks were retained; no native pipeline output was changed and no heavy checks were repeated.',
    'resolved_initial_error': expected_error,
    'reconciler_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    'verifier_sha256': hashlib.sha256((root / 'verify_completed_run.py').read_bytes()).hexdigest(),
}
assert len(result['checks']) == 11
assert all(v['status'] == 'VERIFIED' for v in result['checks'].values())
(root / 'final-verification.json').write_text(json.dumps(result, indent=2, sort_keys=True) + '\n', encoding='utf-8')
print(json.dumps({'status': result['status'], 'verified_checks': len(result['checks']), 'errors': result['errors'], 'reconciled_at': result['reconciled_at']}))
