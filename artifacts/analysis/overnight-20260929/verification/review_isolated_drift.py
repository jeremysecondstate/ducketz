"""Root-authorized exact-byte review of a separate concurrent Hyperliquid edit."""
from pathlib import Path
from datetime import datetime, timezone
import difflib
import hashlib
import json
import subprocess

OUT = Path(__file__).resolve().parent
REPO = Path('C:/dev/ducketz')
sha = lambda value: hashlib.sha256(value).hexdigest()
baseline_bytes = (OUT/'audit-environment-baseline.json').read_bytes()
baseline = json.loads(baseline_bytes)
allowed = {'ml/hyperliquid_paper_policy.py'}
current = {p.relative_to(REPO).as_posix(): p for scope in ('ml', 'datafetching')
           for p in (REPO/scope).rglob('*.py') if '__pycache__' not in p.parts}
changes = {name for name, expected in baseline['files'].items()
           if not (REPO/name).is_file() or sha((REPO/name).read_bytes()) != expected}
changes.update(set(current)-set(baseline['files']))
assert changes == allowed, 'Unexpected module drift: '+str(changes)
name = next(iter(allowed))
old = subprocess.check_output(['git', 'show', baseline['git_head']+':'+name], cwd=REPO)
new = (REPO/name).read_bytes()
assert sha(old) == baseline['files'][name] == 'a45db62db7895ddf18037e2431484ab5d27dc4ec13f046e12aa622fe18dd3cfc'
assert sha(new) == '8493e54dfe1e9e89a2cedcdcecbbede4abb536a815c2295bd191f171ebdf6038'
refs, examined = [], []
for relative, path in current.items():
    if path.name.startswith('hyperliquid'):
        continue
    examined.append(relative)
    for number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
        if 'hyperliquid' in line.lower():
            refs.append({'path': relative, 'line': number, 'text': line.strip()})
assert not refs, 'Non-Hyperliquid native source reference found: '+str(refs)
preserve = OUT/'first-audit-attempt'
preserve.mkdir(exist_ok=False)
preserved = {}
for filename in ('audit-runner.json', 'review-watch.json', 'verify_yg_completion-stdout.log',
                 'verify_yg_completion-stderr.log', 'completion-audit.json', 'verify_completed_run-stderr.log'):
    data = (OUT/filename).read_bytes()
    (preserve/filename).write_bytes(data)
    preserved[filename] = {'path': str(preserve/filename), 'sha256': sha(data)}
diff = ''.join(difflib.unified_diff(old.decode().splitlines(True), new.decode().splitlines(True),
    fromfile='baseline/'+name, tofile='reviewed/'+name))
(OUT/'reviewed-isolated-code-drift.diff').write_text(diff, encoding='utf-8')
result = {
    'reviewed_at': datetime.now(timezone.utc).isoformat(),
    'status': 'REVIEWED_ISOLATED_FROM_NATIVE_STOCK_PIPELINE',
    'authorization': 'Root explicitly authorized exact-hash isolated review and remaining-check continuation after guard failure; no native rerun or source rebaseline.',
    'baseline_preserved': True, 'baseline_file_sha256': sha(baseline_bytes),
    'baseline_git_head': baseline['git_head'],
    'reviewed_git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip(),
    'changes': [{'path': name, 'baseline_sha256': sha(old), 'reviewed_sha256': sha(new),
        'baseline_exact_git_object_verified': True,
        'reason': 'Separate Hyperliquid paper exposure hysteresis: matching exposure must reach min_trade_notional to use the hold threshold; sub-minimum residuals retain accounting. No non-Hyperliquid ml/datafetching Python file references Hyperliquid. All other captured Python sources, stock contracts and watchlist hashes remain identical.'}],
    'reference_scan': {'non_hyperliquid_python_files': len(examined), 'reference_count': len(refs),
                       'files': sorted(examined), 'references': refs},
    'source_diff': {'path': str(OUT/'reviewed-isolated-code-drift.diff'), 'sha256': sha(diff.encode())},
    'preserved_failed_attempt': preserved,
    'limits': ['Static source-reference review, not a whole-environment import proof.',
               'This exact-byte exception authorizes no Hyperliquid execution or production change.',
               'Any subsequent byte drift or native stock source drift fails the guard.',
               'Preflight independently verifies controls, schedule, holdings and claim preservation.'],
    'production_writes': 0, 'provider_calls': 0, 'orders_placed': 0}
assert (OUT/'audit-environment-baseline.json').read_bytes() == baseline_bytes
(OUT/'reviewed-isolated-code-drift.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
print(json.dumps({'status': result['status'], 'reviewed_files': list(allowed), 'reference_count': len(refs),
                  'native_sources_examined': len(examined), 'baseline_file_sha256': sha(baseline_bytes)}))
