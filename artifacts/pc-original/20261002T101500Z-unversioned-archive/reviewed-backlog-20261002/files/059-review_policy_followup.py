import hashlib
import json
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path('<LOCAL_CHECKOUT>')
OUT = ROOT / 'artifacts/analysis/overnight-20260930/preflight'
FOLLOWUP = ROOT / 'artifacts/analysis/overnight-20260930/limitations-followup'
PLAN = Path('<LOCAL_DATASTORE>/ml/gameplan-trade-plan-runs/20260930T062633.629638Z')
GAMEPLAN = Path('<LOCAL_DATASTORE>/ml/nightly-gameplan-runs/20260930T061501.586401Z')

def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))

def evidence(path):
    return {'path': str(path).replace('\\', '/'), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}

report = read_json(PLAN / 'report.json')
completion = read_json(PLAN / 'planning-reference-completion.json')
ledger = read_json(PLAN / 'direction-ledger.json')
forecasts = pd.read_parquet(GAMEPLAN / 'forecasts.parquet')
proposal_path = FOLLOWUP / 'automation-proposal-v2.json'
proposal = read_json(proposal_path)
before = tomllib.loads((FOLLOWUP / 'automation-before.toml').read_text(encoding='utf-8-sig'))
checks = {}
checks['proposal_preserves_every_declared_field'] = all(before[key] == value for key, value in proposal['preserved'].items())
checks['proposal_preserves_id'] = before['id'] == proposal['id']
checks['saved_direction_version'] = report['direction_policy_version'] == 'stock-direction-50-v2'
checks['saved_direction_thresholds'] = report['direction_up_threshold'] == report['direction_down_threshold'] == .5
checks['all_forecast_policy_versions'] = forecasts.direction_policy_version.eq('stock-direction-50-v2').all().item()
checks['all_forecast_thresholds'] = (forecasts.direction_up_threshold.eq(.5) & forecasts.direction_down_threshold.eq(.5)).all().item()
expected = forecasts.calibrated_probability.map(lambda p: 'BULLISH' if p > .5 else 'BEARISH' if p < .5 else 'NO_EDGE')
checks['all_264_saved_directions_match_probability'] = len(forecasts) == 264 and forecasts.direction.eq(expected).all().item()
checks['signal_driven_saved_holding_policy'] = ledger['holding_policy'] == 'accumulate_bullish_sell_on_bearish_no_scheduled_expiry_v1'
checks['no_saved_expiry_sale'] = all('EXPIR' not in x['reason'].upper() for x in ledger['events'])
checks['saved_sparse240_contract'] = completion['contract_version'] == 'sparse-session-planning-reference-completion-v2' and completion['max_gap_minutes'] == 240
checks['saved_native_and_training_prices_unmodified'] = completion['native_prices_modified'] is False and completion['model_training_prices_modified'] is False
checks['source_and_action_identity'] = report['action_date'] == '2026-09-30' and report['source_gameplan_run'] == 'ml/nightly-gameplan-runs/20260930T061501.586401Z'
checks['zero_planning_orders'] = report['orders_placed'] == 0 and report['broker_orders_enabled'] is False
references = [v for v in completion['references'].values() if v['is_synthetic']]
checks['exact_carried_current_anchors'] = {(r['symbol'], r['price'], r['gap_minutes']) for r in references} == {('CROX',123.0,200.0),('PATH',12.31,106.0)}
checks['current_synthetic_volume_zero'] = len(completion['synthetic_bars']) == 306 and all(x['volume'] == 0 and x['reason'] == 'ASSUMED_NO_TRADES' for x in completion['synthetic_bars'])

paths = [proposal_path, FOLLOWUP/'automation-before.toml', OUT/'sparse-policy-direct-thread-evidence.json', ROOT/'artifacts/analysis/overnight-20260915/policy-provenance.md', ROOT/'artifacts/gameplans/2026-09-14/direction-policy-review.md', ROOT/'docs/loops-system-analysis/INDEPENDENT_STOCK_HORIZONS.md', ROOT/'docs/loops-system-analysis/NIGHTLY_GAMEPLAN.md', ROOT/'ml/stock_direction_policy.py', ROOT/'ml/gameplan_cash_ledger.py', ROOT/'ml/gameplan_trade_planning.py', ROOT/'ml/gameplan_price_completion.py', ROOT/'ml/stock_trader/gameplan_direction_engine.py', PLAN/'report.json',PLAN/'direction-ledger.json',PLAN/'planning-reference-completion.json', GAMEPLAN/'forecasts.parquet']
result = {
    'status': 'PASS' if all(checks.values()) else 'FAIL',
    'observed_at': datetime.now(timezone.utc).isoformat(),
    'scope': 'Read-only provenance, current saved policy, shared implementation, and root-owned prompt proposal peer review; no code, automation, production, provider or broker action.',
    'checks': checks,
    'direct_authority': {
        'sparse_session_policy': {'thread_id':'REDACTED_NATIVE_ID_0126','thread_title':'Brainstorm weekly stock research','user_turn':'REDACTED_NATIVE_ID_0127','user_message':'REDACTED_NATIVE_ID_0128','disclosed_four_hour_cap_message':'msg_009c2702dbdac558016aa7e9dd3d3c87d0a9e7c93121eab217','acknowledgment_user_message':'REDACTED_NATIVE_ID_0129','qualification':'User requested appropriate sparse OHLCV filling. Four hours was the assistant-disclosed implementation, subsequently acknowledged, not a numeric cap dictated by the user.'},
        'neutral_band_removal': {'status':'ORIGINAL_USER_TURN_NOT_RETRIEVED','documented_authority':'<LOCAL_USER>/.codex/automations/REDACTED_NATIVE_ID_0121/memory.md:3309-3312 explicitly records September14 21:04 user request and implementation. Current guide:144-150 and committed direction-policy-review corroborate. Do not claim direct re-reading of original user message.'}
    },
    'git_policy_origins': {'sparse_session':'f80176ccf49590b53f6ff56230293b5798680548','neutral_band_removal':'c6886f1154f7a008d96a7e91054b99924b73e42f','signal_driven_holdings':'1d3f724'},
    'implementation_citations': {
        'strict_above_below_50_shared_function':'<LOCAL_CHECKOUT>/ml/stock_direction_policy.py:6-19',
        'planner_shared_import_and_signal_driven_call':'<LOCAL_CHECKOUT>/ml/gameplan_trade_planning.py:21,440,463-464',
        'cash_direction_calls_and_no_expiry_branch':'<LOCAL_CHECKOUT>/ml/gameplan_cash_ledger.py:13,214,232,237,307-315',
        'manual_direction_calls':'<LOCAL_CHECKOUT>/ml/stock_trader/gameplan_direction_engine.py:15,249-263',
        'synthetic_bound_coverage_original_timestamp':'<LOCAL_CHECKOUT>/ml/gameplan_price_completion.py:157-186,223-261,282-290',
        'manual_current_operator_contract':'<LOCAL_CHECKOUT>/docs/loops-system-analysis/INDEPENDENT_STOCK_HORIZONS.md:37-79,144-150',
    },
    'saved_policy': {'direction_version':report['direction_policy_version'],'direction_counts':forecasts.direction.value_counts().to_dict(),'holding_policy':ledger['holding_policy'],'synthetic_current_anchors':references,'synthetic_current_bars':len(completion['synthetic_bars'])},
    'recommendation': 'Apply only the reviewed prompt synchronization through the native automation tool, preserving all fields and auditing read-back. V2 resolves identified no-expiry and live-quote wording conflicts. No stock execution, forecast, model gate or synthetic training change is justified by this provenance review.',
    'limitations': ['The 50% direction rule does not prove improved forecast accuracy or profitability.','A carried close is an explicit planning assumption; missing XNAS bars do not prove no trades or a flat consolidated market.','Published artifacts remain immutable; existing full final audit is reused, not repeated.'],
    'evidence': [evidence(p) for p in paths]
}
(OUT/'policy-provenance-followup.json').write_text(json.dumps(result,indent=2,sort_keys=True)+'\n',encoding='utf-8')
print(json.dumps({'status':result['status'],'checks':len(checks),'failed':[k for k,v in checks.items() if not v],'artifact':str(OUT/'policy-provenance-followup.json')}))
assert all(checks.values())
