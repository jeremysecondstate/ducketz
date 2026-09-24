"""Renew the existing task claim and observe; never control the pipeline."""
import json
import subprocess
import sys
import time
from pathlib import Path

base = Path(__file__).parent
while True:
    claim = subprocess.run([sys.executable, '-m', 'ml.overnight_runtime', '--datastore-target', 'pc', '--claim-supervision', '996a651c-933c-498b-9b1a-1acb47945bb8'], capture_output=True, text=True)
    print('CLAIM ' + claim.stdout.strip(), flush=True)
    if claim.returncode or json.loads(claim.stdout).get('status') != 'ACQUIRED':
        print('CLAIM_FAILED ' + claim.stderr.strip(), flush=True)
        break
    observation = subprocess.run([sys.executable, str(base / 'monitor_run.py')], capture_output=True, text=True)
    print('OBSERVATION ' + observation.stdout.strip(), flush=True)
    if observation.returncode:
        print('MONITOR_ERROR ' + observation.stderr.strip(), flush=True)
    time.sleep(35)

