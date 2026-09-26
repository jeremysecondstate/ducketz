"""Record the root-authorized, exact-byte Hyperliquid drift relevance review."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import subprocess

OUT = Path(__file__).resolve().parent
REPO = Path('C:/dev/ducketz')
baseline = json.loads((OUT/'audit-environment-baseline.json').read_text(encoding='utf-8'))
allowed = {'ml/hyperliquid_paper_runtime.py', 'ml/hyperliquid_forecast_reader.py',
    'ml/hyperliquid_powder_exchange.py', 'ml/hyperliquid_powder_ledger.py',
    'ml/hyperliquid_powder_lock.py', 'ml/hyperliquid_powder_runtime.py'}
current = {path.relative_to(REPO).as_posix(): path for scope in ('ml', 'datafetching')
           for path in (REPO/scope).rglob('*.py') if '__pycache__' not in path.parts}
sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
changed = {name for name, path in current.items() if baseline['files'].get(name) != sha(path)}
assert changed == allowed, 'Unexpected module drift: '+str(changed)
refs = []
examined = []
for name, path in current.items():
    if path.name.startswith('hyperliquid'):
        continue
    examined.append(name)
    for number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
        if 'hyperliquid' in line.lower():
            refs.append({'path': name, 'line': number, 'text': line.strip()})
assert not refs, 'A native non-Hyperliquid module references Hyperliquid code'
head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip()
diff = subprocess.check_output(['git', 'diff', baseline['git_head'], head, '--', *sorted(allowed)], cwd=REPO, text=True)
(OUT/'reviewed-isolated-code-drift.diff').write_text(diff, encoding='utf-8')
result = {'reviewed_at': datetime.now(timezone.utc).isoformat(),
    'status': 'REVIEWED_ISOLATED_FROM_NATIVE_STOCK_PIPELINE',
    'authorization': 'Root explicitly authorized read-only relevance analysis and an artifact-only exact-hash exception after the preliminary drift guard stopped.',
    'baseline_preserved': True, 'baseline_git_head': baseline['git_head'], 'reviewed_git_head': head,
    'changes': [{'path': name, 'baseline_sha256': baseline['files'].get(name), 'reviewed_sha256': sha(current[name]),
                 'reason': 'Separate Hyperliquid consumer module. No non-Hyperliquid ml/datafetching Python source references the Hyperliquid namespace; all captured stock-pipeline, model, archive, policy, watchlist and native contract bytes remain unchanged.'}
                for name in sorted(allowed)],
    'reference_scan': {'non_hyperliquid_python_files': len(examined), 'reference_count': len(refs),
                       'files': sorted(examined), 'references': refs},
    'source_diff': str(OUT/'reviewed-isolated-code-drift.diff'),
    'limits': ['Code-reference review does not authorize running any Hyperliquid module.',
               'Only these exact reviewed bytes are admitted; subsequent changes or any stock-pipeline drift fail the guard.',
               'Native commands remain source-bound stock-only eight-stage commands; independent root controls/schedule comparison remains required.'],
    'production_writes': 0, 'provider_calls': 0, 'orders_placed': 0}
(OUT/'reviewed-isolated-code-drift.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
print(json.dumps({'status': result['status'], 'exact_exception_files': len(allowed), 'non_hyperliquid_sources_examined': len(examined), 'references': len(refs)}))
