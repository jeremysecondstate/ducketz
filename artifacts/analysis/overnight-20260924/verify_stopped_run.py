"""Verify the stopped native attempt without fetching or changing production."""
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, 'C:/dev/ducketz')
from ml.overnight_runtime import _resume_configuration

root = Path('C:/DATASTORE')
run = root / 'ml/overnight-runs/20260924T040733.615371Z'
out = Path(__file__).parent
report = _resume_configuration(root, run)
receipt = json.loads((run / 'receipt.json').read_text())
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
checks = {
    'terminal_cancelled': report['status'] == receipt['status'] == 'CANCELLED',
    'fetch_is_failed_stage': report['failed_stage'] == 'loop_a_close_fetch',
    'no_completed_stages': report['completed_stages'] == [],
    'original_deadline': datetime.fromisoformat(report['deadline_at']) == datetime(2026, 9, 24, 11, tzinfo=timezone.utc),
    'no_deadline_exception': report.get('deadline_exception') is None,
    'saved_archive_policy': report['archive_history'] is True,
    'saved_native_stock_scope': report['stock_only'] is True and report['independent_stock_horizons'] is True,
    'saved_xnas_source': report['stock_price_source'] == 'xnas-itch-archive-v1',
    'saved_raw_direction_target': report['probability_target_contract'] == 'raw-price-direction-v1',
    'zero_order_authority': receipt['orders_placed'] == 0 and receipt['broker_orders_enabled'] is False,
    'receipt_report_size': receipt['stage_report_size'] == (run / 'stage-report.json').stat().st_size,
    'all_saved_log_sizes': all((run / name).stat().st_size == item['size'] for name, item in receipt['logs'].items()),
}
pointers = {}
for name, expected in (
    ('nightly-gameplan-latest', 'ml/nightly-gameplan-runs/20260923T054513.490247Z'),
    ('gameplan-trade-plan-latest', 'ml/gameplan-trade-plan-runs/20260923T054803.564321Z'),
    ('gameplan-actuals-review-latest', 'ml/gameplan-actuals-review-runs/20260923T055028.498627Z'),
):
    path = root / 'ml' / name / 'run.json'
    pointer = json.loads(path.read_text())
    checks[name + '_preserved'] = pointer['current']['run_path'] == expected
    pointers[name] = {'path': str(path), 'sha256': sha(path), 'current': pointer['current']}
result = {
    'verified_at': datetime.now(timezone.utc).isoformat(),
    'status': 'VERIFIED_STOPPED_EXTERNAL_AUTH_BLOCKER' if all(checks.values()) else 'VERIFICATION_FAILED',
    'run': str(run), 'checks': checks, 'receipt': receipt,
    'receipt_sha256': sha(run / 'receipt.json'), 'pointers': pointers,
    'unresolved_failure': 'Schwab OAuth HTTP400 invalid_grant; local reauthorization required',
    'new_action_date': '2026-09-24', 'new_publication_created': False,
    'opra_and_archive_extension_and_training_reached': False,
    'resume_policy': 'After new authorization, acquire own supervision claim and resume this attempt only, preserving archive policy, source and original deadline.',
    'orders_placed': 0,
}
(out / 'stopped-run-verification.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps({'status': result['status'], 'checks': checks, 'output': str(out / 'stopped-run-verification.json')}))
raise SystemExit(0 if all(checks.values()) else 1)
