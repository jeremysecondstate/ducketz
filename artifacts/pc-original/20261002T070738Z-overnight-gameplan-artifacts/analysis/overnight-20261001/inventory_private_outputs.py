"""Inventory exact final local evidence without publishing or modifying it.

Run only after the final audits and notes have finished. The output must be outside
every inventoried tree. All generated data, models, account projections, runtime
records and task helpers stay private; this does not confer publication authority.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess


REPO = Path('C:/dev/ducketz')
DATASTORE = Path('C:/DATASTORE')
LOCAL = REPO/'artifacts/analysis/overnight-20261001'
NATIVE = DATASTORE/'ml/overnight-runs/20261002T040848.225355Z'
SCOPES = [
    ('invocation_local_evidence', LOCAL, 'PRIVATE_OPERATING_EVIDENCE',
     'Local audit helpers, receipts, account/ledger backup evidence, native IDs and supervision metadata.'),
    ('completed_session_fallback_review', REPO/'artifacts/analysis/fallback-session-reviews/20261002T064859Z-20261001',
     'PRIVATE_LEDGER_AND_ACCOUNT_REVIEW', 'Real ledger-recorded fills, reservations, donor ownership and native identities.'),
    ('native_overnight_reports', NATIVE, 'PRIVATE_OPERATING_EVIDENCE',
     'Native status, receipts, logs, process identities, provider/account diagnostics and operator notes.'),
    ('symbol_specific_gameplan', DATASTORE/'ml/nightly-gameplan-runs/20261002T063014.996205Z',
     'PRIVATE_SYMBOL_SPECIFIC_GAMEPLAN_AND_MODELS',
     'Local symbol forecasts, fitted models, training cohorts, source references and native publication identities.'),
    ('symbol_specific_trade_plan', DATASTORE/'ml/gameplan-trade-plan-runs/20261002T064252.251864Z',
     'PRIVATE_ACCOUNT_AND_PLANNING_PROJECTIONS',
     'Local Gameplan, account cash/holdings, donor allocation, conditional trade projection and source receipts.'),
    ('completed_session_actuals', DATASTORE/'ml/gameplan-actuals-review-runs/20261002T064641.608198Z',
     'PRIVATE_SYMBOL_SPECIFIC_RESULTS_AND_RECEIPTS',
     'Local frozen forecast outcomes, saved planning estimates and source-bound receipts.'),
    ('independent_sizing_models', DATASTORE/'ml/stock-trader-model-runs/20261002T064107.776476Z',
     'PRIVATE_FITTED_MODELS_AND_ASSESSMENTS',
     'Fitted local model artifacts, cohort identities, training and assessment evidence.'),
]
EXCLUDED_TESTS = [
    'tests/test_hyperliquid_model_config.py',
    'tests/test_hyperliquid_paper_policy.py',
    'tests/test_hyperliquid_paper_rebalance_threshold.py',
]


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def sha(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def beneath(path, root):
    return path == root or root in path.parents


def files_in_scope():
    result = {}
    for name, directory, classification, reason in SCOPES:
        root = directory.resolve(strict=True)
        require(root.is_dir(), 'Inventory root must be a directory')
        for path in sorted(root.rglob('*')):
            require(not path.is_symlink(), 'Symlink in local inventory scope; inspect instead of following')
            if not path.is_file():
                continue
            resolved = path.resolve(strict=True)
            require(beneath(resolved, root), 'Resolved file escaped its inventory root')
            require(resolved not in result, 'Overlapping inventory roots')
            info = path.stat()
            result[resolved] = {
                'scope': name,
                'classification': classification,
                'reason': reason,
                'relative_to_scope': path.relative_to(root).as_posix(),
                'size_bytes': info.st_size,
                'modified_ns': info.st_mtime_ns,
                'identity': (info.st_dev, info.st_ino),
            }
    require(len(result) <= 10000, 'Unexpected inventory size; inspect scope before proceeding')
    return result


def git(*args):
    result = subprocess.run(['git', *args], cwd=REPO, env=dict(os.environ, GIT_OPTIONAL_LOCKS='0'),
                            capture_output=True, check=True, text=True)
    return result.stdout.strip()


def validate_ready():
    native_report, native_receipt = (read(NATIVE/name) for name in ('stage-report.json', 'receipt.json'))
    require(native_report['status'] == native_receipt['status'] == 'COMPLETE', 'Native run is not COMPLETE')
    require(native_report['enrichment_gameplan']['run_path'] == 'ml/nightly-gameplan-runs/20261002T063014.996205Z',
            'Native pinned Gameplan changed')
    require(native_report['enrichment_gameplan']['action_date'] == '2026-10-02', 'Native action date changed')
    audit = read(LOCAL/'verification/audit-runner.json')
    require(audit['status'] in {'VERIFIED', 'VERIFIED_WITH_COVERAGE_NOTES'}, 'Final audit is not verified')
    require(all(sha(Path(item['path'])) == item['sha256'] for item in audit['evidence'].values()),
            'Final audit output binding changed')
    preservation = read(LOCAL/'final-preservation-review-complete.json')
    require(preservation['status'] == 'PASS_WITH_ATTRIBUTED_CONCURRENT_CHANGES'
            and all(preservation['checks'].values()), 'Final preservation review is not complete')
    require(all(sha(Path(path)) == digest for path, digest in preservation['evidence_bindings'].items()),
            'Final preservation evidence binding changed')
    source = read(LOCAL/'verification/audit-environment-baseline.json')
    require(all(sha(REPO/path) == digest for path, digest in source['files'].items()),
            'Preparation source changed after review')
    require(git('rev-parse', 'HEAD') == preservation['git_identity']['head']
            and git('branch', '--show-current') == preservation['git_identity']['branch'],
            'Git identity changed after attributed preservation review')
    require(not git('diff', '--cached', '--name-only'), 'Staged changes need separate attribution')
    excluded = []
    attribution = read(LOCAL/'concurrent-git-source-attribution.json')
    for relative in EXCLUDED_TESTS:
        path = REPO/relative
        digest = sha(path)
        require(digest == attribution['attribution'][relative]['sha256'], 'Concurrent test bytes changed after attribution')
        excluded.append({'path': str(path), 'sha256': digest,
                         'classification': 'EXCLUDED_UNRELATED_CONCURRENT_HYPERLIQUID_WORK',
                         'owned_by_this_invocation': False})
    bindings = [NATIVE/'receipt.json', NATIVE/'stage-report.json', LOCAL/'verification/audit-runner.json',
                LOCAL/'final-preservation-review-complete.json', LOCAL/'concurrent-git-source-attribution.json']
    return {'sha256': {str(path): sha(path) for path in bindings}, 'excluded': excluded,
            'checkout_head': git('rev-parse', 'HEAD'), 'checkout_branch': git('branch', '--show-current')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path,
                        help='New private JSON path outside every inventoried directory')
    args = parser.parse_args()
    target = args.output.resolve()
    require(not target.exists(), 'Refusing to replace a prior inventory')
    require(target.suffix.lower() == '.json', 'Inventory output must be JSON')
    require(not any(beneath(target, directory.resolve()) for _, directory, _, _ in SCOPES),
            'Output must be outside inventoried trees to avoid self-reference')
    started = datetime.now(timezone.utc).isoformat()
    validation_before = validate_ready()
    before = files_in_scope()
    inventory = []
    for path, item in before.items():
        inventory.append({'path': str(path), 'scope': item['scope'],
                          'relative_to_scope': item['relative_to_scope'],
                          'classification': item['classification'], 'reason': item['reason'],
                          'size_bytes': item['size_bytes'], 'sha256': sha(path),
                          'publishable_by_this_invocation': False})
    after = files_in_scope()
    validation_after = validate_ready()
    require(before == after, 'An inventoried file changed during hashing; wait for writers then use a fresh output path')
    require(validation_before == validation_after, 'Final audit or source identity changed during inventory')
    counts = Counter(item['classification'] for item in inventory)
    scope_counts = Counter(item['scope'] for item in inventory)
    result = {
        'schema_version': 'private-overnight-invocation-output-inventory-v1',
        'status': 'STABLE_LOCAL_ONLY_INVENTORY', 'started_at': started,
        'completed_at': datetime.now(timezone.utc).isoformat(),
        'producer': 'loops-hourly-operations', 'machine': 'pc-original',
        'source_date': '2026-10-01', 'action_date': '2026-10-02',
        'privacy': 'PRIVATE_LOCAL_ONLY_DO_NOT_QUEUE_UPLOAD_OR_PUBLISH',
        'authority': 'Inventory/classification only. Peer requests and namespacing grant no authority to publish local data, models, account state, receipts or operating evidence.',
        'shareable_source_operations': [], 'new_source_code_changes_owned_by_this_invocation': 0,
        'files': inventory, 'excluded_unrelated_concurrent_files': validation_before['excluded'],
        'scopes': [{'name': name, 'path': str(path), 'classification': category, 'reason': reason,
                    'file_count': scope_counts[name]} for name, path, category, reason in SCOPES],
        'file_count': len(inventory), 'total_bytes': sum(item['size_bytes'] for item in inventory),
        'classification_counts': dict(counts),
        'validation_bindings': validation_before['sha256'],
        'checkout': {key: validation_before[key] for key in ('checkout_head', 'checkout_branch')},
        'stability_checks': {'same_paths_sizes_mtimes_and_file_identities_before_after': True,
                             'same_verified_final_audit_and_source_bindings_before_after': True},
        'excluded_from_this_bounded_inventory': [
            'Bulk provider raw/normalized archives and other historical runs; remain private.',
            'Global mutable controls, credentials, accounts, ledgers and scheduler state outside the named roots; remain local.',
            'Automation memory and future edits after this capture; remain private.',
            'The inventory file itself; stored separately to avoid a self-referential hash.',
        ],
        'communication_guidance': 'Atlas on this PC may review the private local manifest by reference. Portable notices should contain only sanitized aggregate completion facts; do not include local paths, native IDs, account/holdings/projection values, receipt contents or task memory. No queue or new publication is authorized.',
        'source_edits': 0, 'git_or_index_mutations': 0, 'queue_operations': 0,
        'provider_calls': 0, 'broker_calls': 0, 'order_actions': 0,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('x', encoding='utf-8') as handle:
        json.dump(result, handle, indent=2)
        handle.write('\n')
    print(json.dumps({'status': result['status'], 'file_count': result['file_count'],
                      'classification_counts': result['classification_counts'],
                      'shareable_source_operations': 0, 'excluded_concurrent_files': len(EXCLUDED_TESTS),
                      'output': str(target), 'inventory_sha256': sha(target)}, indent=2))


if __name__ == '__main__':
    main()
