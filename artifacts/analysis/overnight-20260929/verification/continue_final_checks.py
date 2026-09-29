"""Continue only unfinished offline checks after exact isolated source review."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import subprocess
import sys
import time
from audit_environment import verify_environment

OUT = Path(__file__).resolve().parent
REPO = Path('C:/dev/ducketz')
read = lambda p: json.loads(p.read_text(encoding='utf-8-sig'))
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
review = read(OUT/'reviewed-isolated-code-drift.json')
for filename, evidence in review['preserved_failed_attempt'].items():
    assert sha(Path(evidence['path'])) == evidence['sha256'], filename
    assert sha(OUT/filename) == evidence['sha256'], 'Original evidence changed: '+filename
old = read(OUT/'first-audit-attempt/audit-runner.json')
assert old['status'] == 'FAILED_REQUIRES_DIAGNOSIS'
assert len(old['checks']) == 2
assert old['checks'][0]['check'] == 'verify_completed_run' and old['checks'][0]['exit_code'] == 0
assert old['checks'][1]['check'] == 'verify_yg_completion' and old['checks'][1]['exit_code'] == 1
assert 'AUDIT_IMPLEMENTATION_DRIFT_REQUIRES_REVIEW' in (OUT/'verify_yg_completion-stderr.log').read_text()
native = Path(old['native_run'])
assert native == Path('C:/DATASTORE/ml/overnight-runs/20260929T040727.779758Z')
for name in ('stage-report.json', 'receipt.json'):
    assert read(native/name)['status'] == 'COMPLETE'
completion = read(OUT/'completion-audit.json')
assert completion['status'] in ('VERIFIED', 'VERIFIED_WITH_COVERAGE_NOTES') and not completion['errors']
assert len(completion['checks']) == 12
result = {'status': 'RUNNING', 'started_at': datetime.now(timezone.utc).isoformat(),
    'native_run': str(native), 'implementation_before': verify_environment(),
    'original_runner': {'path': str(OUT/'audit-runner.json'), 'sha256': sha(OUT/'audit-runner.json')},
    'isolated_drift_review': {'path': str(OUT/'reviewed-isolated-code-drift.json'), 'sha256': sha(OUT/'reviewed-isolated-code-drift.json')},
    'inherited_successful_check': {**old['checks'][0], 'completion_sha256': sha(OUT/'completion-audit.json'),
        'stderr_sha256': sha(OUT/'verify_completed_run-stderr.log'), 'sections_verified': sorted(completion['checks'])},
    'checks': [], 'orders_placed': 0, 'provider_calls': 0, 'production_mutations': 0,
    'native_restarts': 0, 'native_stage_repeats': 0}
output = OUT/'audit-runner-continuation.json'
assert not output.exists(), 'Do not overwrite a previous continuation'
def save():
    output.write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
def run(name, arguments):
    stdout, stderr = OUT/(name+'-continuation-stdout.log'), OUT/(name+'-continuation-stderr.log')
    assert not stdout.exists() and not stderr.exists()
    command = [sys.executable, '-B', str(OUT/(name+'.py')), *map(str, arguments)]
    entry = {'check': name, 'started_at': datetime.now(timezone.utc).isoformat(),
             'command': command, 'stdout': str(stdout), 'stderr': str(stderr), 'helper_sha256': sha(OUT/(name+'.py'))}
    result['checks'].append(entry)
    save()
    print('AUDIT_CONTINUE_START '+name, flush=True)
    start = time.monotonic()
    with stdout.open('w', encoding='utf-8') as out, stderr.open('w', encoding='utf-8') as err:
        done = subprocess.run(command, cwd=REPO, stdout=out, stderr=err, check=False)
    entry.update(exit_code=done.returncode, seconds=round(time.monotonic()-start, 3),
        finished_at=datetime.now(timezone.utc).isoformat(), stdout_sha256=sha(stdout), stderr_sha256=sha(stderr))
    save()
    print('AUDIT_CONTINUE_END '+name+' '+json.dumps(entry), flush=True)
    if done.returncode:
        raise RuntimeError('Remaining check failed: '+name+'; inspect '+str(stderr))
try:
    run('verify_yg_completion', ['--overnight-run', native, '--output', OUT/'yg-completion.json'])
    run('audit_provider_completion', ['--terminal-run', native, '--hash-current'])
    run('review_models', ['--overnight-run', native, '--gameplan-run', completion['checks']['gameplan']['run'],
                          '--enrichment-run', completion['checks']['enrichment']['run']])
    run('audit_optional_pricing', ['--overnight-run', native])
    run('review_loop_b_weekly_prefix', [])
    result['implementation_after'] = verify_environment()
    assert sha(OUT/'completion-audit.json') == result['inherited_successful_check']['completion_sha256']
    assert sha(OUT/'audit-runner.json') == result['original_runner']['sha256']
    result['evidence'] = {name: {'path': str(OUT/name), 'sha256': sha(OUT/name)} for name in
        ('completion-audit.json', 'yg-completion.json', 'provider-completion.json', 'model-review.json',
         'model-metric-summary.json', 'pricing-gate-review.json', 'loop-b-weekly-prefix-review.json',
         'reviewed-isolated-code-drift.json')}
    result['limitations'] = ['Saved model quality and unavailable outcomes remain explicit.',
        'Provider advisory disposition is separately reviewed using the bounded provider evidence.',
        'Controls, holdings and schedules are checked by root/preflight.',
        'Archive audit verifies hashes/request headers; seconds/minutes check does not replay all raw DBN records.']
    result['status'] = 'VERIFIED_WITH_COVERAGE_NOTES'
except Exception as exc:
    result.update(status='FAILED_REQUIRES_DIAGNOSIS', error=type(exc).__name__+': '+str(exc))
result['finished_at'] = datetime.now(timezone.utc).isoformat()
save()
print(json.dumps({'status': result['status'], 'report': str(output), 'error': result.get('error')}), flush=True)
raise SystemExit(0 if result['status'] == 'VERIFIED_WITH_COVERAGE_NOTES' else 1)
