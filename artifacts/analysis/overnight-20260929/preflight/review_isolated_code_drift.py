"""Offline static review only: no imports from production, no provider/broker calls."""
from pathlib import Path
import ast
from datetime import datetime, timezone
import hashlib
import json
import subprocess

REPO = Path('C:/dev/ducketz')
OUT = Path(__file__).resolve().parent
VERIFY = OUT.parent / 'verification'
RUN = Path('C:/DATASTORE/ml/overnight-runs/20260929T040727.779758Z')
TARGET = 'ml.hyperliquid_paper_policy'
EXPECTED_OLD = 'a45db62db7895ddf18037e2431484ab5d27dc4ec13f046e12aa622fe18dd3cfc'
EXPECTED_NEW = '8493e54dfe1e9e89a2cedcdcecbbede4abb536a815c2295bd191f171ebdf6038'

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    baseline = json.loads((VERIFY / 'audit-environment-baseline.json').read_text())
    report = json.loads((RUN / 'stage-report.json').read_text())
    scopes = ('app', 'datafetching', 'fundamentals', 'ml', 'options', 'signals', 'technicals')
    modules = {}
    for scope in scopes:
        for path in (REPO / scope).rglob('*.py'):
            if '__pycache__' in path.parts:
                continue
            name = '.'.join(path.relative_to(REPO).with_suffix('').parts)
            if name.endswith('.__init__'):
                name = name[:-9]
            modules[name] = path
    graph, dynamic, errors = {}, {}, []
    for name, path in modules.items():
        try:
            tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        except Exception as exc:
            errors.append({'path': str(path), 'error': str(exc)})
            continue
        edges = set()
        def add(imported):
            pieces = imported.split('.')
            for n in range(1, len(pieces) + 1):
                prefix = '.'.join(pieces[:n])
                if prefix in modules:
                    edges.add(prefix)
        # Importing a submodule executes its package initializers too.
        for n in range(1, len(name.split('.'))):
            add('.'.join(name.split('.')[:n]))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ''
                if node.level:
                    package = name.split('.') if path.name == '__init__.py' else name.split('.')[:-1]
                    package = package[:len(package) - node.level + 1]
                    base = '.'.join(package + ([base] if base else []))
                add(base)
                for alias in node.names:
                    add(base + '.' + alias.name)
            elif isinstance(node, ast.Call):
                function = ast.unparse(node.func)
                if function == '__import__' or function.endswith('.import_module') or function in {'eval', 'exec'}:
                    dynamic.setdefault(name, []).append({'line': node.lineno, 'call': ast.unparse(node)})
        graph[name] = sorted(edges - {name})
    roots = ['ml.overnight_runtime']
    roots += [stage['command'][stage['command'].index('-m') + 1] for stage in report['stages']]
    reached, pending = set(), list(roots)
    while pending:
        name = pending.pop()
        if name in reached:
            continue
        reached.add(name)
        pending.extend(graph.get(name, []))
    actual_changes = []
    for relative, expected in baseline['files'].items():
        path = REPO / relative
        actual = sha(path) if path.is_file() else None
        if expected != actual:
            actual_changes.append({'path': relative, 'baseline_sha256': expected, 'current_sha256': actual})
    existing = {path.relative_to(REPO).as_posix() for scope in ('ml', 'datafetching')
                for path in (REPO / scope).rglob('*.py') if '__pycache__' not in path.parts}
    additions = sorted(existing - baseline['files'].keys())
    target_path = modules[TARGET]
    direct_importers = sorted(name for name, edges in graph.items() if TARGET in edges)
    current_diff = subprocess.run(['git', 'diff', '--', 'ml/hyperliquid_paper_policy.py'], cwd=REPO,
                                  capture_output=True, text=True, check=True).stdout
    checks = {
        'native_complete': report['status'] == 'COMPLETE',
        'target_expected_baseline': baseline['files']['ml/hyperliquid_paper_policy.py'] == EXPECTED_OLD,
        'target_exact_reviewed_hash': sha(target_path) == EXPECTED_NEW,
        'single_exact_drift': actual_changes == [{'path': 'ml/hyperliquid_paper_policy.py',
                                                'baseline_sha256': EXPECTED_OLD, 'current_sha256': EXPECTED_NEW}],
        'no_new_source_modules': not additions,
        'static_parse_complete': not errors,
        'target_not_reachable': TARGET not in reached,
        'no_target_importer_reachable': not any(name in reached for name in direct_importers),
        'no_unresolved_dynamic_import_exec_eval_reachable': not any(name in reached for name in dynamic),
    }
    result = {
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'status': 'EXACT_ISOLATED_AUDIT_EXCEPTION_JUSTIFIED' if all(checks.values()) else 'REQUIRES_REVIEW',
        'checks': checks, 'changes': actual_changes, 'additions': additions,
        'roots': sorted(set(roots)), 'scanned_modules': len(modules),
        'reachable_modules': sorted(reached), 'direct_importers': direct_importers,
        'other_hyperliquid_named_modules_reachable': sorted(name for name in reached if 'hyperliquid' in name),
        'parse_errors': errors,
        'dynamic_calls_in_reachable': {key: value for key, value in dynamic.items() if key in reached},
        'bound_reachable_sources': {str(modules[name].relative_to(REPO)): sha(modules[name]) for name in sorted(reached)},
        'guard_source_sha256': sha(VERIFY / 'audit_environment.py'),
        'baseline_sha256': sha(VERIFY / 'audit-environment-baseline.json'),
        'native_report_sha256': sha(RUN / 'stage-report.json'),
        'current_git_diff': current_diff,
        'assessment': 'The only observed source drift changes the Hyperliquid target_notionals hysteresis threshold to require matching gross exposure at least min_trade_notional. Conservative static import closure from the actual eight native stage commands plus overnight_runtime does not reach the changed policy or any of its four direct importers. Shared app account/info service modules are conservatively reachable, but do not reference the changed policy. This policy is also shared by the separate Hyperliquid powder runtime, so isolation is specific to the native stock pipeline, not a claim that all consumers are paper-only.',
        'conditions': ['Keep the original baseline unchanged.', 'Admit only this exact path and baseline/current SHA-256 pair.', 'Recheck all other source hashes and new modules; any further drift requires independent review.', 'Resume only remaining offline checks; preserve the already completed full audit and native run.', 'Do not modify or validate the unrelated Hyperliquid product change under this audit.'],
        'limitations': ['Static import closure is conservative over all lexical imports, including conditional imports; it does not execute production code or establish correctness of the unrelated Hyperliquid policy change.', 'Serialization artifact behavior is checked separately by the existing model/inference audit.'],
        'broker_calls': 0, 'provider_calls': 0, 'production_mutations': 0,
    }
    (OUT / 'isolated-code-drift-review.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'status': result['status'], 'checks': checks, 'reachable_modules': len(reached),
                      'direct_importers': direct_importers, 'artifact': str(OUT / 'isolated-code-drift-review.json')}))
    return 0 if all(checks.values()) else 1

if __name__ == '__main__':
    raise SystemExit(main())
