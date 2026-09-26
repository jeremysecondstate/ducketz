"""Read-only bounded publication observer; never supervises or changes the owner.

Watches small native status/logs and invokes saved-report review once publication
is complete and pinned. The root task separately maintains native supervision.
"""
from pathlib import Path
from datetime import datetime, timezone
import json
import subprocess
import sys
import time

OUT = Path(__file__).resolve().parent
NATIVE = Path('C:/DATASTORE/ml/overnight-runs/20260926T040723.834511Z')
REPO = Path('C:/dev/ducketz')
observations = []
seen_events = set()
started = datetime.now(timezone.utc).isoformat()

while True:
    report = json.loads((NATIVE/'stage-report.json').read_text(encoding='utf-8'))
    observation = {k: report.get(k) for k in ('status', 'current_stage', 'heartbeat_at', 'cpu_seconds', 'io_bytes', 'log_bytes', 'recent_issues')}
    observation['observed_at'] = datetime.now(timezone.utc).isoformat()
    observations.append(observation)
    log = NATIVE/'gameplan_publication.log'
    if log.exists():
        for line in log.read_text(encoding='utf-8', errors='replace').splitlines():
            if ('FIT_WARNING' in line or 'FIT_END' in line or 'FIT_COMPLETE' in line) and line not in seen_events:
                seen_events.add(line)
                print(line, flush=True)
    stages = [s for s in report.get('stages', []) if s.get('stage') == 'gameplan_publication']
    ready = (len(stages) == 1 and stages[0].get('status') == 'COMPLETE'
             and stages[0].get('exit_code') == 0 and bool(report.get('enrichment_gameplan')))
    result = {'started_at': started, 'updated_at': observation['observed_at'], 'native_run': str(NATIVE),
              'status': 'PINNED_PUBLICATION_READY' if ready else 'WAITING_FOR_PINNED_PUBLICATION',
              'observations': observations, 'model_fit_events': sorted(seen_events),
              'provider_calls': 0, 'orders_placed': 0, 'production_mutations': 0,
              'native_supervision_owner_changed': False}
    (OUT/'preliminary-review-watch.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    if ready:
        completed = subprocess.run([sys.executable, '-B', str(OUT/'preliminary_directional_review.py')], cwd=REPO, check=False)
        raise SystemExit(completed.returncode)
    if report.get('status') in ('FAILED', 'CANCELLED', 'COMPLETE'):
        print(json.dumps({'status': 'TERMINAL_WITHOUT_COMPLETED_PUBLICATION', **observation}), flush=True)
        raise SystemExit(1)
    time.sleep(30)
