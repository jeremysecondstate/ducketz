"""Read-only control/schedule hash capture; outputs stay in this evidence folder."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import tomllib

OUT = Path(__file__).resolve().parent
REPO = Path('C:/dev/ducketz')
ROOT = Path('C:/DATASTORE')

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

controls = {}
paths = [ROOT/'controls/stock-trader/operator-intent.txt',
         ROOT/'controls/gameplan-stock-trader/operator-intent.txt',
         ROOT/'controls/loop-c/current/halt-control.json',
         ROOT/'controls/loop-c/current/risk-approval.json',
         REPO/'.env', REPO/'Start-Gameplan-Trader.cmd',
         REPO/'docs/datafetch-ml/start_stock_session.ps1']
for path in paths:
    controls[str(path)] = {'exists': path.is_file(), 'sha256': sha(path) if path.is_file() else None}
    if path.name == 'operator-intent.txt' and path.is_file():
        controls[str(path)]['value'] = path.read_text(encoding='utf-8').strip()

automation_paths = sorted(Path('C:/Users/7980X/.codex/automations').glob('*/automation.toml'))
automations = []
for path in automation_paths:
    data = tomllib.loads(path.read_text(encoding='utf-8-sig'))
    if not any(term in data.get('name', '').lower() for term in ['loop', 'gameplan', 'stock']):
        continue
    selected = {key:data[key] for key in ['id','kind','name','status','rrule','model','reasoning_effort','notification_policy','timezone'] if key in data}
    selected.update(path=str(path), sha256=sha(path))
    automations.append(selected)

entry_claims = {}
for name in ['entry-slots', 'quote-recovery-slots']:
    for path in sorted((ROOT/'state/independent-stock-trader'/name).rglob('*')):
        if path.is_file():
            entry_claims[str(path)] = sha(path)

result = {'reviewed_at': datetime.now(timezone.utc).isoformat(),
          'scope': 'READ_ONLY_LOCAL_CONTROL_AND_SCHEDULE_CAPTURE',
          'controls_and_launcher_hashes': controls,
          'entry_and_recovery_claim_hashes': entry_claims,
          'automations': automations,
          'windows_schedule_evidence': str(OUT/'windows-schedules.json'),
          'windows_schedule_sha256': sha(OUT/'windows-schedules.json')}
(OUT/'controls-and-schedules.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
print(json.dumps({'controls': len(controls), 'claims': len(entry_claims), 'automations': len(automations), 'output': str(OUT/'controls-and-schedules.json')}))
