"""Renew the root task claim and observe; never control or release the pipeline."""
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

base = Path(__file__).resolve().parent
claim_id = '5c3cea33-8624-4dcc-9fa8-68928af785b1'
stop_file = base / 'stop-monitor'
while not stop_file.exists():
    started = time.monotonic()
    stamp = datetime.now(timezone.utc).isoformat()
    claim = subprocess.run([sys.executable, '-m', 'ml.overnight_runtime', '--datastore-target', 'pc', '--claim-supervision', claim_id], capture_output=True, text=True, timeout=55)
    record = {'observed_at': stamp, 'returncode': claim.returncode, 'stdout': claim.stdout.strip(), 'stderr': claim.stderr.strip()}
    with (base / 'claim-renewals.jsonl').open('a', encoding='utf-8') as history:
        history.write(json.dumps(record) + '\n')
    print('CLAIM ' + json.dumps(record), flush=True)
    try:
        status = json.loads(claim.stdout).get('status')
    except json.JSONDecodeError:
        status = 'UNREADABLE_CLAIM'
    if claim.returncode or status != 'ACQUIRED':
        print('CLAIM_FAILED ' + str(status), flush=True)
        break
    observation = subprocess.run([sys.executable, str(base / 'monitor_run.py')], capture_output=True, text=True, timeout=25)
    print('OBSERVATION ' + observation.stdout.strip(), flush=True)
    if observation.returncode:
        print('MONITOR_ERROR ' + observation.stderr.strip(), flush=True)
    remaining = max(0, 35 - (time.monotonic() - started))
    for _ in range(int(remaining)):
        if stop_file.exists():
            break
        time.sleep(1)
print('MONITOR_STOPPED (claim intentionally not released)', flush=True)
