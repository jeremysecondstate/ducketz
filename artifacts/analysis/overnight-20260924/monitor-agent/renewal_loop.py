"""Renew the existing task claim and observe; never control the pipeline."""
import json
import subprocess
import sys
import time
from pathlib import Path

base = Path(__file__).parent
while True:
    claim = subprocess.run([sys.executable, '-m', 'ml.overnight_runtime', '--datastore-target', 'pc', '--claim-supervision', '63df5b6c-2987-4603-b8af-f745d38a0ec2'], capture_output=True, text=True)
    print('CLAIM ' + claim.stdout.strip(), flush=True)
    if claim.returncode or json.loads(claim.stdout).get('status') != 'ACQUIRED':
        print('CLAIM_FAILED ' + claim.stderr.strip(), flush=True)
        break
    observation = subprocess.run([sys.executable, str(base / 'monitor_run.py')], capture_output=True, text=True)
    print('OBSERVATION ' + observation.stdout.strip(), flush=True)
    if observation.returncode:
        print('MONITOR_ERROR ' + observation.stderr.strip(), flush=True)
    time.sleep(35)
