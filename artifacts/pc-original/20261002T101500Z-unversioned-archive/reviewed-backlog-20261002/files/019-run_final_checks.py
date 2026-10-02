"""Run saved-source completion and fitted-model audits only after native COMPLETE.

The root operator must retain and renew its own claim independently while this
read-only audit runs. This helper never starts or stops runtime work.
"""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import hashlib
import json
import subprocess
import sys
import time

OUT = Path(__file__).resolve().parent
REPO = Path('<LOCAL_CHECKOUT>')
read = lambda p: json.loads(Path(p).read_text(encoding='utf-8-sig'))
now = lambda: datetime.now(timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--overnight-run', type=Path, required=True)
    args = parser.parse_args()
    native = args.overnight_run.resolve()
    records = [read(native/name) if (native/name).exists() else {} for name in ('stage-report.json', 'receipt.json')]
    if any(item.get('status') != 'COMPLETE' for item in records):
        print(json.dumps({'status': 'NOT_READY_FOR_FINAL_VERIFICATION', 'heavy_checks_started': False,
                          'native_statuses': [item.get('status', 'MISSING') for item in records]}))
        return 2
    from audit_environment import verify_environment
    result = {'status': 'RUNNING', 'started_at': now(), 'native_run': str(native),
              'implementation_before': verify_environment(), 'checks': [], 'orders_placed': 0,
              'provider_calls': 0, 'production_mutations': 0}
    path = OUT/'audit-runner.json'
    if path.exists():
        raise RuntimeError('Refusing to overwrite a prior audit invocation; inspect its evidence first')

    def save():
        path.write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')

    try:
        for name, extra, output in [
            ('verify_completed_run', [], 'completion-audit.json'),
            ('verify_yg_completion', ['--output', str(OUT/'yg-completion.json')], 'model-audit-stdout.log')]:
            start = time.monotonic()
            command = [sys.executable, '-B', str(OUT/(name+'.py')), '--overnight-run', str(native), *extra]
            record = {'check': name, 'started_at': now(), 'command': command}
            result['checks'].append(record)
            save()
            print('AUDIT_START '+name, flush=True)
            with (OUT/output).open('w', encoding='utf-8') as out, (OUT/(name+'-stderr.log')).open('w', encoding='utf-8') as err:
                completed = subprocess.run(command, cwd=REPO, stdout=out, stderr=err, check=False)
            record.update(exit_code=completed.returncode, finished_at=now(), seconds=round(time.monotonic()-start, 3))
            save()
            print('AUDIT_END '+name+' '+str(completed.returncode), flush=True)
            if completed.returncode:
                raise RuntimeError('Audit failed: '+name+'; inspect saved stdout/stderr')
        completion, models = read(OUT/'completion-audit.json'), read(OUT/'yg-completion.json')
        if any(item.get('errors') or item.get('status') not in ('VERIFIED', 'VERIFIED_WITH_COVERAGE_NOTES')
               for item in (completion, models)):
            raise RuntimeError('Audit evidence contains verification failures')
        result['implementation_after'] = verify_environment()
        result['status'] = 'VERIFIED_WITH_COVERAGE_NOTES' if completion['coverage_notes'] or models['coverage_notes'] else 'VERIFIED'
        result['evidence'] = {name: {'path': str(OUT/name), 'sha256': hashlib.sha256((OUT/name).read_bytes()).hexdigest()}
                              for name in ('completion-audit.json', 'yg-completion.json')}
        result['limitations'] = ['Conditional planning does not establish live fallback baseline, fills or realized profit.',
            'Provider warning metadata follow-up and controls/schedule/ledger preservation are separate operator evidence.',
            'Archive native hashes/header/normalized consistency are verified; every raw DBN record is not replayed.',
            'All research statuses and missing actual outcomes remain explicitly reported.']
    except Exception as exc:
        result.update(status='FAILED_REQUIRES_DIAGNOSIS', error=type(exc).__name__+': '+str(exc))
    result['finished_at'] = now()
    save()
    print(json.dumps({'status': result['status'], 'report': str(path), 'error': result.get('error')}), flush=True)
    return 0 if result['status'] in ('VERIFIED', 'VERIFIED_WITH_COVERAGE_NOTES') else 1


if __name__ == '__main__':
    raise SystemExit(main())
