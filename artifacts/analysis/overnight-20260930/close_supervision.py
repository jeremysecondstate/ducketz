from pathlib import Path
from datetime import datetime, timezone
import json
import subprocess

ROOT = Path(__file__).resolve().parent
REPO = Path('C:/dev/ducketz')
TOKEN = '3e1f4906-750c-46d5-8e0b-5dfdad3b651a'
PY = REPO / '.venv/Scripts/python.exe'

def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')

def claim(argument):
    result = subprocess.run([str(PY), '-m', 'ml.overnight_runtime', '--datastore-target', 'pc', argument, TOKEN], cwd=REPO, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)

renewal = claim('--claim-supervision')
assert renewal['status'] == 'ACQUIRED', renewal
save(ROOT/'monitor/closure-claim.json', renewal)
(ROOT/'monitor/STOP').write_text('Final supervised work and independent audits complete.\n', encoding='utf-8')
check = subprocess.run(['powershell.exe', '-NoProfile', '-Command', "@(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python(w)?\\.exe$' -and ($_.CommandLine -like '*overnight-20260930*monitor.py*' -or $_.CommandLine -match '-m ml\\.overnight_runtime') } | Select-Object ProcessId,ParentProcessId,CommandLine) | ConvertTo-Json -Compress"], capture_output=True, text=True, check=True)
processes = json.loads(check.stdout) if check.stdout.strip() else []
assert not processes, processes
save(ROOT/'monitor/final-process-check.json', {'checked_at': datetime.now(timezone.utc).isoformat(), 'matching_monitor_or_native_python_processes': processes})
renewals = [json.loads(line) for line in (ROOT/'monitor/renewals.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
assert all(row['status'] == 'ACQUIRED' and row['owner_token'] == TOKEN for row in renewals)
times = [datetime.fromisoformat(row['updated_at']) for row in renewals]
maximum_gap = max((b-a).total_seconds() for a,b in zip(times,times[1:]))
release = claim('--release-supervision')
save(ROOT/'supervision-release.json', release)
assert release['status'] == 'RELEASED', release
now = datetime.now(timezone.utc).isoformat()
closure = {
    'closed_at': now, 'status': 'RELEASED', 'owner_token': TOKEN,
    'helper_exit_code': 0, 'helper_terminal_status': 'AGENT_KEEPALIVE_EXPIRED',
    'helper_process_absent': True, 'native_process_absent': True,
    'automated_renewal_count': len(renewals), 'all_automated_renewals_acquired': True,
    'maximum_automated_renewal_gap_seconds': maximum_gap,
    'last_automated_renewal': renewals[-1],
    'post_audit_gap': {
        'cause': 'Root context compaction exceeded helper keepalive; helper self-stopped after all native work and audits completed.',
        'lease_expired_at': renewals[-1]['expires_at'],
        'same_owner_reacquired_at': '2026-09-30T06:56:52.866791+00:00',
        'reacquisition_status': 'ACQUIRED',
        'reacquisition_evidence': 'Root native claim tool result; subsequent saved final-claim.json and closure-claim.json.',
        'native_completed_at': '2026-09-30T06:30:57.383549+00:00',
        'production_actions_during_gap': 0,
    },
    'final_process_check': str(ROOT/'monitor/final-process-check.json'),
    'final_claim': renewal, 'release': release,
}
save(ROOT/'monitor/monitor-closure.json', closure)
summary = ROOT/'completion-summary.md'
text = summary.read_text(encoding='utf-8')
old = 'Supervision closure is recorded separately after report review. Do not rerun the completed September 29 source session.'
assert old in text
new = (f'Supervision was released at {now}. The monitor recorded {len(renewals)} ACQUIRED renewals with a maximum automated interval of {maximum_gap:.3f} seconds while active. '
       'After native work and all audits completed, root context compaction exceeded the helper keepalive: it self-stopped and the lease expired at 06:54:51 UTC. '
       'The same owner reacquired the claim at 06:56:52 UTC before closing; no production action occurred during that gap. '
       'Both native and monitor processes were confirmed absent before release. '
       '[Closure evidence](C:/dev/ducketz/artifacts/analysis/overnight-20260930/monitor/monitor-closure.json). Do not rerun the completed September 29 source session.')
summary.write_text(text.replace(old,new), encoding='utf-8')
print(json.dumps({'status': 'RELEASED', 'closed_at': now, 'renewals': len(renewals), 'maximum_gap_seconds': maximum_gap, 'processes_absent': True}))
