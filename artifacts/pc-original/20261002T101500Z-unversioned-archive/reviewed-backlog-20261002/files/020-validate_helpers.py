"""Bounded helper validation using synthetic in-memory projections only."""
from pathlib import Path
from datetime import datetime, timezone
from copy import deepcopy
import ast
import hashlib
import json
import math
import subprocess
import sys

OUT = Path(__file__).resolve().parent
REPO = Path('<LOCAL_CHECKOUT>')
sys.path.insert(0, str(REPO))
from audit_environment import verify_environment
from fallback_checks import verify_holdings_and_fallback
from ml.gameplan_cash_ledger import project_direction_trades
from ml.stock_trader.cross_horizon_fallback import policy_for_action_date
import pandas as pd


def require(condition, message):
    if not condition:
        raise ValueError(message)


checks = []
for path in OUT.glob('*.py'):
    ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
checks.append({'name': 'ALL_HELPERS_AST_PARSE', 'status': 'PASS'})
checks.append({'name': 'EXACT_DEPLOYMENT_AND_SOURCE_BASELINE', **verify_environment()})
native = REPO/'artifacts/analysis/overnight-20260930/fallback-enabled-verification/nonexistent-native-run'
assert not native.exists()
for script in ('run_final_checks.py', 'verify_completed_run.py'):
    process = subprocess.run([sys.executable, '-B', str(OUT/script), '--overnight-run', str(native)],
                             capture_output=True, text=True, cwd=REPO, check=False)
    payload = json.loads(process.stdout)
    assert process.returncode == 2 and payload['status'] == 'NOT_READY_FOR_FINAL_VERIFICATION'
    assert payload.get('heavy_checks_started', payload.get('archive_checks_started')) is False
    checks.append({'name': script+' completion guard', 'status': 'PASS', 'exit_code': process.returncode})

day = '2026-10-01'
policy = policy_for_action_date(day)
grid = [(hour, horizon) for hour in range(4, 17) for horizon in ('1h', '4h', '1d', '1w')
        if horizon == '1h' or (horizon == '4h' and hour in (4, 8, 12, 16))
        or (horizon in ('1d', '1w') and hour == 4)]
raw = []
for hour, horizon in grid:
    stamp = pd.Timestamp(f'{day} {hour:02d}:00', tz='America/Los_Angeles').tz_convert('UTC')
    raw.append({'id': f'AAPL:{horizon}:{hour}', 'action_date': day, 'symbol': 'AAPL', 'model_group': horizon,
                'execution_eligible': True, 'target_window_start': stamp,
                'target_window_end': stamp + pd.Timedelta(hours=1), 'model_status': 'PROMOTED',
                'calibrated_probability': .4 if horizon == '1h' else .5, 'projected_trade_quantity': 1})
snapshot = {'status': 'OBSERVED', 'cash_status': 'CASH_ONLY_BOUNDED', 'account_equity': 10000,
            'available_cash': 1000, 'reserved_cash': 0, 'held_shares': {'AAPL': 300},
            'symbol_exposure': {'AAPL': 3000}, 'stock_market_value_by_symbol': {'AAPL': 3000},
            'other_symbol_exposure': {'AAPL': 0}, 'gross_exposure': 3000,
            'pending_buy_shares': {}, 'pending_sell_shares': {}, 'quotes': {'AAPL': {'ask': 11}},
            'ownership': {'safe_for_planning': True, 'blocked_symbols': [], 'active_allocations': [
                {'symbol': 'AAPL', 'horizon': '1w', 'owned_shares': 300,
                 'reserved_sell_shares': 0, 'allocation_id_sha256': 'a'*64,
                 'target_end': '2026-10-07T00:00:00Z', 'prediction_id': 'FROZEN_WEEKLY'}]}}
prices = {'working_half_width_bps': 20, 'points': {
    f'AAPL|{day}|{hour:02d}:00': {'status': 'AVAILABLE', 'symbol': 'AAPL', 'action_date': day,
        'clock_local': f'{hour:02d}:00', 'timestamp': f'{day}T{hour:02d}:00:00-07:00',
        'planned_price_low': 10, 'planned_price_mid': 10.5, 'planned_price_high': 11}
    for hour in range(4, 18)}}
rows, ledger = project_direction_trades(pd.DataFrame(raw), snapshot, prices,
                                        signal_driven=True, cross_horizon_fallback_policy=policy)
same = lambda left, right: math.isclose(float(left), float(right), rel_tol=1e-10, abs_tol=1e-7)
audit = lambda r, s, l: verify_holdings_and_fallback(r, s, l, symbols=('AAPL',), require=require, same_number=same)
detail = audit(rows, snapshot, ledger)
assert detail['fallback_event_count'] == 13 and detail['symbol_daily_caps'] == {'AAPL': 150}
assert detail['normal_sale_event_count'] == 0
checks.append({'name': 'INDEPENDENT_DONOR_CAP_SLOT_AND_SHARE_AUDIT', 'status': 'PASS', 'result': detail})

for name, mutate in [
    ('quota changed', lambda r, s, l: r.loc.__setitem__((0, 'fallback_slot_quota'), 999)),
    ('donor hash changed', lambda r, s, l: l['events'][0]['cross_horizon_fallback'].__setitem__('donor_allocation_id_sha256', 'b'*64)),
    ('daily cap inflated', lambda r, s, l: l['cross_horizon_fallback']['symbol_daily_caps'].__setitem__('AAPL', 151)),
    ('usage reset', lambda r, s, l: l['events'][1]['cross_horizon_fallback'].__setitem__('symbol_used_before', 0)),
    ('wrong donor horizon', lambda r, s, l: l['events'][0]['cross_horizon_fallback'].__setitem__('donor_horizon', '1h')),
    ('expiry inserted', lambda r, s, l: l['events'][0].__setitem__('reason', 'HORIZON_EXIT')),
]:
    r, s, l = rows.copy(deep=True), deepcopy(snapshot), deepcopy(ledger)
    mutate(r, s, l)
    try:
        audit(r, s, l)
    except ValueError:
        checks.append({'name': 'REJECTS '+name, 'status': 'PASS'})
    else:
        raise AssertionError('Audit accepted '+name)

normal_raw = deepcopy(raw)
for row in normal_raw:
    if row['model_group'] == '1w':
        row['calibrated_probability'] = .4
normal_rows, normal_ledger = project_direction_trades(pd.DataFrame(normal_raw), snapshot, prices,
    signal_driven=True, cross_horizon_fallback_policy=policy)
detail = audit(normal_rows, snapshot, normal_ledger)
assert detail['normal_sale_event_count'] == 1 and detail['fallback_event_count'] == 0
assert normal_ledger['events'][0]['quantity'] == 300 and detail['symbol_used'] == {'AAPL': 0}
checks.append({'name': 'NORMAL_SALE_UNCAPPED_AND_EXCLUDED_AS_DONOR', 'status': 'PASS'})

record = {'checked_at': datetime.now(timezone.utc).isoformat(), 'status': 'PASS', 'checks': checks,
          'scope': 'AST, early completion guards, exact source hashes and synthetic in-memory planning arithmetic.',
          'source_archive_checks_started': False, 'provider_calls': 0, 'account_calls': 0,
          'production_mutations': 0, 'orders_placed': 0,
          'helper_hashes': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(OUT.glob('*.py'))}}
(OUT/'helper-review-tests.json').write_text(json.dumps(record, indent=2)+'\n', encoding='utf-8')
print(json.dumps({'status': 'PASS', 'checks': len(checks), 'output': str(OUT/'helper-review-tests.json')}))
