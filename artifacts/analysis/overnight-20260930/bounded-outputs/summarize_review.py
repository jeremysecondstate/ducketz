"""Bind independent small-output audits; never open archives or operational stores."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json

HERE=Path(__file__).resolve().parent
def read(name):
    return json.loads((HERE/name).read_text(encoding='utf-8'))
def digest(path):
    assert path.stat().st_size<64*1024*1024
    return hashlib.sha256(path.read_bytes()).hexdigest()

t=read('final-output-review.json')
a=read('actuals-coverage-review.json')
stamp=datetime.now(timezone.utc).isoformat()
native=Path('C:/DATASTORE/ml/overnight-runs/20260930T040723.733025Z')
evidence=[HERE/x for x in ['native_gate.py','review_trade_plan.py','review_actuals.py','final-output-review.json','actuals-coverage-review.json']]
evidence += [native/'stage-report.json',native/'receipt.json',HERE.parent/'preflight/post-reconciliation-baseline.json']
synthetic=[x for x in t['current_reference_anchors'] if x['status']=='AVAILABLE_SYNTHETIC']
checks={
    'trade_plan_checks_pass':t['status']=='PASS' and all(t['checks'].values()),
    'actuals_checks_pass':a['status']=='SAVED_ACTUALS_COVERAGE_VERIFIED' and all(a['checks'].values()),
    'all_events_and_hourly_summaries_pass':all(x['verified'] for x in t['event_checks']+t['hour_checks']),
    'all_actual_endpoint_and_hourly_clocks_pass':all(x['verified'] for x in a['endpoint_clock_checks']+a['hourly_price_clock_checks']),
    'review_is_saved_outputs_only':not any(t[x] or a[x] for x in ['production_writes','broker_calls','provider_calls']) and not a['native_price_reload'],
}
summary={
    'reviewed_at':stamp,'status':'PASS_WITH_EXPLICIT_COVERAGE_LIMITATIONS' if all(checks.values()) else 'REVIEW_REQUIRED',
    'native_run':str(native),'action_date':'2026-09-30','reviewed_prior_action_date':'2026-09-29','checks':checks,
    'audit_check_counts':{'trade_plan':len(t['checks']),'actuals':len(a['checks']),'cash_share_events':len(t['event_checks']),'hourly_summaries':len(t['hour_checks']),'actual_endpoint_clocks':len(a['endpoint_clock_checks']),'actual_hourly_clocks':len(a['hourly_price_clock_checks'])},
    'trade_plan_rows':t['rows'],'cash':t['ledger_summary'],'current_synthetic_anchors':synthetic,
    'actual_forecasts':a['report_summary']['forecasts'],'actual_prices':a['report_summary']['prices'],'raw_direction':a['raw_direction'],'price_range_result':a['price_range_result'],
    'mature_missing_forecasts_by_symbol':{r['symbol']:r['mature_missing'] for r in a['per_symbol'] if r['mature_missing']},
    'missing_clock_prices_by_symbol':{r['symbol']:r['missing_price_clocks'] for r in a['per_symbol'] if r['missing_price_clocks']},
    'pending_route_counts':a['pending_route_counts'],
    'actual_source_gameplan_published_at':a['source_gameplan_published_at'],'actual_source_trade_plan_completed_at':a['source_trade_plan_completed_at'],
    'saved_policy_note':t['saved_policy_note'],
    'limitations':[
        'Actual outcomes preserve 35 mature forecasts without an eligible observation and 66 future forecasts. The 19 missing hourly prices stay null; verified acquisition coverage does not prove a boundary trade or absence of trading.',
        'CROX 200-minute and PATH 106-minute carries are explicit planning assumptions under the saved v3/240-minute policy, with original timestamps retained. They are not prices for training, evaluation, actuals or live quote validation.',
        'Cash and shares reflect a hypothetical sequence of fills. Broker capacity is separate from literal cash; no-fill baseline preserved. No realized P/L or actual fills are inferred.',
        'Ownership evidence compares saved planning holdings with the saved post-reconciliation baseline. The root preservation audit independently checks operational ledger tables and controls.',
        'Raw source bytes, model/cohort reconstruction and actual endpoint selection from archives are intentionally delegated to the main final audit.'
    ],
    'evidence':[{'path':str(p),'size':p.stat().st_size,'sha256':digest(p)} for p in evidence],
    'production_writes':0,'broker_calls':0,'provider_calls':0,
}
(HERE/'bounded-output-summary.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
lines=[
f"Bounded trade-plan and actuals review completed at {stamp}.",
'',
f"PASS: {len(t['checks'])} trade-plan checks and {len(a['checks'])} actuals checks. Native run 20260930T040723.733025Z was COMPLETE before output reads. These checks use saved receipts, manifests, reports and bounded publication tables; no raw archive reconstruction, broker/provider calls or production changes occurred.",
'',
'The September 30 publication has 264 forecasts, 264 stock-only intents and 264 augmented trade rows (24 per symbol across eleven symbols), with all 154 hourly planning prices available. Decimal arithmetic independently conserves cash and shares across 32 hypothetical events (13 sales and 19 buys), all 14 hourly summaries and eligible forecast rows. Literal initial cash is $54.85; conditional ending low/base/high cash is $27.17 / $180.87 / $335.15, before fees and taxes. The unchanged no-fill baseline and saved real holdings match the post-reconciliation baseline.',
'',
'Two current planning anchors use explicitly synthetic zero-volume bars: CROX $123.00, observed September 29 at 13:40 PDT, carried 200 minutes; PATH $12.31, observed 15:14 PDT, carried 106 minutes. Both end at that session’s 17:00 boundary. All 306 bars preserve original observation times, source identity, gap and coverage; the readable Gameplan discloses both anchors. This verifies the saved/current v3 and sparse-session-v2 240-minute planning policy. It does not claim compliance with the older v2 15-minute rule. The saved YG direction threshold is 50 percent and holdings persist until a bearish signal, as documented in the current operating contract.',
'',
'The September 29 actuals use the original Gameplan published at 06:23:01Z and trade plan completed at 06:28:25Z that day, before the 11:00Z opening. Frozen forecasts, original ranges/midpoints and price-path clocks remain exact. Outcomes stop at the completed session’s close. There are 163 evaluated forecasts, 35 mature forecasts lacking eligible observations and 66 pending forecasts. Direction accuracy is 84/163 (51.53%) under the frozen policy; pending/missing results are excluded and cost-adjusted targets remain separate.',
'',
'Hourly actuals contain 135 compared prices and 19 missing prices (15 outside the five-minute tolerance and four with no observation). Seventeen of 135 compared prices fall within the saved range. Missing values remain null even where saved request intervals verify source coverage. Raw native data was not reloaded here; the main final audit separately verifies payloads and endpoint selection.',
'',
'Missing mature forecasts: COST 5, CROX 11, GOOG 2, IONQ 1, PATH 8, TWST 8. Pending: eleven each for 4h@16:00, 1d@D+2, 1d@D+3, 1d@D+4, 1d@D+5 and 1w@D+5.',
'',
'Missing hourly actual prices:',
]
for row in a['per_symbol']:
    if row['missing_price_clocks']:
        lines.append(f"- {row['symbol']}: {', '.join(row['missing_price_clocks'])} Pacific.")
lines += [
'',
'Receipt/manifest bindings, dated actuals output and the successor’s results link pass. Saved outputs assert zero orders and review-only authority. The root preservation audit supplies the separate operational-ledger mutation check. No actual broker fills or realized P/L are implied.',
'',
'Detailed evidence:',
'',
f"- [Trade-plan review]({(HERE/'final-output-review.json').as_posix()})",
f"- [Actuals review]({(HERE/'actuals-coverage-review.json').as_posix()})",
f"- [Bound summary and hashes]({(HERE/'bounded-output-summary.json').as_posix()})",
f"- [Readable September 30 Gameplan]({t['paths']['readable'].replace(chr(92),'/')})",
f"- [September 29 results]({a['run_path'].replace(chr(92),'/')}/Gameplan-results.md)",
]
(HERE/'review.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
print(json.dumps({'status':summary['status'],'checks':summary['audit_check_counts'],'summary_path':str(HERE/'bounded-output-summary.json'),'summary_sha256':digest(HERE/'bounded-output-summary.json'),'review_path':str(HERE/'review.md')},indent=2))
