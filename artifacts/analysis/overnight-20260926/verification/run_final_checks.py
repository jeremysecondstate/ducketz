"""Sequential offline final checks, executed only after native COMPLETE.

The supervising operator separately retains/renews the claim and reads native
health. This helper never acquires a claim, starts a pipeline or retries a stage.
"""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import hashlib
import json
import subprocess
import sys
import time

from audit_environment import verify_environment

OUT = Path(__file__).resolve().parent
REPO = Path('C:/dev/ducketz')


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--overnight-run', required=True, type=Path)
    args = parser.parse_args()
    native = args.overnight_run.resolve()
    report, receipt = (read(native/name) if (native/name).is_file() else {}
                       for name in ('stage-report.json', 'receipt.json'))
    if report.get('status') != 'COMPLETE' or receipt.get('status') != 'COMPLETE':
        print(json.dumps({'status': 'NOT_READY_FOR_FINAL_VERIFICATION', 'heavy_checks_started': False,
                          'native_statuses': [report.get('status'), receipt.get('status')]}), flush=True)
        return 2
    result = {'started_at': datetime.now(timezone.utc).isoformat(), 'native_run': str(native),
              'implementation_before': verify_environment(), 'checks': [], 'status': 'RUNNING',
              'orders_placed': 0, 'provider_calls': 0, 'production_mutations': 0}
    output = OUT/'audit-runner.json'

    def save():
        output.write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')

    def run(name, arguments, stdout_name=None):
        destination = OUT/(stdout_name or name+'-stdout.log')
        errors = OUT/(name+'-stderr.log')
        command = [sys.executable, '-B', str(OUT/(name+'.py')), *map(str, arguments)]
        started = time.monotonic()
        record = {'check': name, 'started_at': datetime.now(timezone.utc).isoformat(),
                  'command': command, 'stdout': str(destination), 'stderr': str(errors)}
        result['checks'].append(record)
        save()
        print('AUDIT_START '+name, flush=True)
        with destination.open('w', encoding='utf-8') as out, errors.open('w', encoding='utf-8') as err:
            completed = subprocess.run(command, cwd=REPO, stdout=out, stderr=err, check=False)
        record.update(exit_code=completed.returncode, seconds=round(time.monotonic()-started, 3),
                      finished_at=datetime.now(timezone.utc).isoformat())
        save()
        print('AUDIT_END '+name+' '+json.dumps(record), flush=True)
        if completed.returncode:
            raise RuntimeError('Final audit failed: '+name+'; inspect '+str(errors))

    try:
        run('verify_completed_run', ['--overnight-run', native], 'completion-audit.json')
        completion = read(OUT/'completion-audit.json')
        if completion.get('errors') or completion.get('status') not in ('VERIFIED', 'VERIFIED_WITH_COVERAGE_NOTES'):
            raise RuntimeError('Full completion audit did not verify')
        run('verify_yg_completion', ['--overnight-run', native, '--output', OUT/'yg-completion.json'])
        run('audit_provider_completion', ['--terminal-run', native, '--hash-current'])
        game = Path(completion['checks']['gameplan']['run'])
        enrichment = Path(completion['checks']['enrichment']['run'])
        run('review_models', ['--overnight-run', native, '--gameplan-run', game, '--enrichment-run', enrichment])
        result['implementation_after'] = verify_environment()
        result['status'] = 'VERIFIED_WITH_COVERAGE_NOTES'
        result['evidence'] = {name: {'path': str(OUT/name), 'sha256': hashlib.sha256((OUT/name).read_bytes()).hexdigest()}
                              for name in ('completion-audit.json', 'yg-completion.json', 'provider-completion.json',
                                           'model-review.json', 'model-metric-summary.json')}
        result['limitations'] = ['Model quality and unavailable actual outcomes retain their saved status.',
            'Controls, holdings ledger and schedule pre/post verification remain a separate root/preflight audit.',
            'Archive API validates raw/normalized hashes and native request headers; second/minute check does not replay every raw DBN record.']
    except Exception as exc:
        result.update(status='FAILED_REQUIRES_DIAGNOSIS', error=type(exc).__name__+': '+str(exc))
    result['finished_at'] = datetime.now(timezone.utc).isoformat()
    save()
    print(json.dumps({'status': result['status'], 'report': str(output), 'error': result.get('error')}), flush=True)
    return 0 if result['status'] == 'VERIFIED_WITH_COVERAGE_NOTES' else 1


if __name__ == '__main__':
    raise SystemExit(main())
