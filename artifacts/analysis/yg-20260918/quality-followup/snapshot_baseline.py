"""Save immutable publication evidence before the authorized quality revision."""
import json
from datetime import datetime, timezone
from pathlib import Path
from ml.artifacts import file_checksum, verify_manifest

root = Path('C:/DATASTORE')
runs = {
    'OG': root/'ml/nightly-gameplan-runs/20260918T054532.489998Z',
    'YG_first': root/'ml/nightly-gameplan-runs/20260918T072555.813034Z',
    'YG_first_trade_plan': root/'ml/gameplan-trade-plan-runs/20260918T072910.923004Z',
    'YG_first_deployment': root/'ml/gameplan-deployment-runs/20260918T073523.985203Z',
}
output = {'saved_at': datetime.now(timezone.utc).isoformat(), 'runs': {}}
for name, run in runs.items():
    verify_manifest(run)
    manifest = json.loads((run/'manifest.json').read_text())
    files = {'receipt.json', 'manifest.json', *manifest['output_files']}
    output['runs'][name] = {
        'run': str(run),
        'files': {item: file_checksum(run/item) for item in sorted(files)},
    }
destination = Path(__file__).with_name('immutable-baseline.json')
if destination.exists():
    raise RuntimeError('Baseline already exists; preserve it')
destination.write_text(json.dumps(output, indent=2), encoding='utf-8')
print(json.dumps({'status': 'SAVED', 'path': str(destination), 'runs': list(runs)}))
