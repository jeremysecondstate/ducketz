"""Peer-check final prose and evidence against saved reports only."""
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
import hashlib
import json
import re

OUT=Path(__file__).resolve().parent
BASE=OUT.parent
VERIFY=BASE/'verification'

def read(path):return json.loads(path.read_text(encoding='utf-8-sig'))
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def evidence_valid(entries):
    return all(Path(e['path']).is_file() and sha(Path(e['path']))==e['sha256'] and Path(e['path']).stat().st_size==e.get('bytes',e.get('size')) for e in entries)

def main():
    summary_path=BASE/'completion-summary.md';text=summary_path.read_text()
    final_path=BASE/'final-verification.json';final=read(final_path)
    audit=read(VERIFY/'completion-audit.json');findings=read(VERIFY/'audit-findings.json')
    runner=read(VERIFY/'audit-runner.json');metrics=read(VERIFY/'model-metric-summary.json')
    models=read(VERIFY/'model-review.json');yg=read(VERIFY/'yg-completion.json')
    provider=read(VERIFY/'provider-completion.json')
    bounded=read(BASE/'bounded-outputs/bounded-output-summary.json')
    trade=read(BASE/'bounded-outputs/final-output-review.json')
    actual=read(BASE/'bounded-outputs/actuals-coverage-review.json')
    preservation=read(OUT/'final-preservation.json');authority=read(OUT/'final-authorities-peer-review.json')
    recon=read(OUT/'reconciliation/native-reconciliation-verification.json');peer=read(OUT/'reconciliation-peer-review.json')
    native=read(Path(final['native_run'])/'stage-report.json')
    native_receipt=read(Path(final['native_run'])/'receipt.json')
    loop_b=(Path(final['native_run'])/'loop_b_directional_generation.log').read_text()
    archive=findings['archive'];coverage=findings['cumulative_evaluation']['coverage_counts']
    cash=bounded['cash'];forecasts=bounded['actual_forecasts'];prices=bounded['actual_prices']
    checks={
        'final_external_evidence_sizes_and_hashes':evidence_valid([e for e in final['evidence'] if Path(e['path']).resolve()!=(OUT/'completion-summary-peer-review.json').resolve()]),
        'bounded_evidence_sizes_and_hashes':evidence_valid(bounded['evidence']),
        'native_complete_eight_stages_no_resume':native['status']=='COMPLETE' and len(native['stages'])==8 and all(r['status']=='COMPLETE' and r['exit_code']==0 for r in native['stages']) and native.get('resumed_from') is None,
        'final_source_action_deadline':final['source_session']=='2026-09-29' and final['action_date']=='2026-09-30' and final['original_deadline']==native['deadline_at']=='2026-09-30T11:00:00+00:00',
        'reported_pacific_completion_clock':datetime.fromisoformat(native['completed_at']).astimezone(ZoneInfo('America/Los_Angeles')).strftime('%Y-%m-%d %H:%M:%S')=='2026-09-29 23:30:57',
        'twelve_sections_verified':len(audit['checks'])==final['full_audit_sections']==12 and all(v['status']=='VERIFIED' for v in audit['checks'].values()) and not audit['errors'],
        'seven_audit_commands_passed':len(runner['checks'])==final['audit_commands_passed']==7 and all(r['exit_code']==0 for r in runner['checks']) and runner['status']=='VERIFIED_WITH_COVERAGE_NOTES',
        'zero_overnight_orders':all(r['orders_placed']==0 for r in [final,native,native_receipt,findings,yg]),
        'expected_current_rows':bounded['trade_plan_rows']=={'forecasts':264,'intents':264,'tradeplan':264,'price_points':154,'synthetic_planning_bars':306},
        'all_promoted_v2_with_explicit_baseline_limits':all(r['promotion_status']=='PROMOTED' and r['promotion_policy']=='independent-stock-directional-promotion-v2' and r['metric_limits']['brier_score']['allowed_baseline_excess']==.005 and r['metric_limits']['log_loss']['allowed_baseline_excess']==.01 for r in metrics['groups'].values()),
        'only_four_hour_beats_both':{h for h,r in metrics['groups'].items() if r['strictly_beats_both_baselines']}=={'4h'},
        'no_future_accuracy_claim':'does not establish improved future accuracy' in text,
        'all_sizing_fitted_zero_qualified':all(r['fit_status']=='FITTED' and r['qualified_scopes']==0 for r in models['enrichment']['groups'].values()),
        'sizing_failure_inventory':{h:r['failed_sizing_quality_checks'] for h,r in models['enrichment']['groups'].items()}=={'1h':['return_mse_below_baseline'],'4h':['return_mse_below_baseline'],'1d':['brier_below_baseline','log_loss_below_baseline','return_mse_below_baseline'],'1w':['brier_below_baseline','log_loss_below_baseline','return_mse_below_baseline','adverse_mse_at_most_baseline']},
        'sizing_failures_fully_described':'All four failed return-MSE checks; daily and weekly also failed Brier/log-loss checks, and weekly failed adverse-MSE.' in text,
        'archive_inventory_counts':{k:archive[k] for k in ['source_files','source_bytes','second_minute_partitions','second_rows','exact_ohlcv_overlap_minutes','second_added_training_rows']}=={'source_files':1032,'source_bytes':951337351,'second_minute_partitions':234,'second_rows':16443370,'exact_ohlcv_overlap_minutes':2842334,'second_added_training_rows':0},
        'current_probabilities_264_zero_error':sum(r['rows'] for r in archive['current_probabilities_reproduced'].values())==264 and all(all(e==0 for e in r['maximum_absolute_error'].values()) for r in archive['current_probabilities_reproduced'].values()),
        'no_prefix_acquisition_and_cursor_preservation':archive['prefix_acquisition']['preflight']=={'new_acquisition_required':False,'requests':0} and archive['prefix_acquisition']['current_cursors_not_regressed'] is True,
        'eleven_zero_cost_minute_acquisitions':audit['checks']['stock_history']['acquisition']['requests']==11 and audit['checks']['stock_history']['acquisition']['cost_usd']==0,
        'opra_counts_zero_cost_and_byte_estimates':provider['current_cursor_count']==33 and provider['current_data_files_hashed']==66 and provider['observed_estimated_download_bytes']==2424616064 and all(r['payload']['estimated_cost_usd']==0 for r in provider['preflights']),
        'loop_b_model_and_sample_counts_saved':all(t in loop_b for t in ['rows=356061','samples=342035;','models_trained=9; models_reused=0']),
        'loop_b_remaining_week_counts':findings['loop_b_weekly']['publication_counts']['fresh_live_rows']==77 and findings['loop_b_weekly']['intelligence_rows']==99 and len(findings['loop_b_weekly']['omitted_suffix']['horizons'])*len(findings['loop_b_weekly']['omitted_suffix']['symbols'])==22,
        'cash_event_arithmetic':cash['buy_events']==19 and cash['sell_events']==13 and cash['trade_events']==cash['buy_events']+cash['sell_events']==32 and Decimal(str(cash['starting_cash']))+Decimal(str(cash['cash_change_low']))==Decimal(str(cash['ending_cash_low'])) and Decimal(str(cash['starting_cash']))+Decimal(str(cash['cash_change_high']))==Decimal(str(cash['ending_cash_high'])),
        'cash_summary_values':(cash['starting_cash'],cash['ending_cash_low'],cash['ending_cash_base'],cash['ending_cash_high'])==(54.85,27.17,180.87,335.15),
        'synthetic_anchor_details':[(r['symbol'],r['price'],r['gap_minutes'],r['fill_count']) for r in bounded['current_synthetic_anchors']]==[('CROX',123.0,200.0,200),('PATH',12.31,106.0,106)],
        'synthetic_observation_clocks':[(r['symbol'],datetime.fromisoformat(r['observed_at']).astimezone(ZoneInfo('America/Los_Angeles')).strftime('%H:%M')) for r in bounded['current_synthetic_anchors']]==[('CROX','13:40'),('PATH','15:14')],
        'current_saved_policy_distinction_disclosed':all(t in text for t in ['240 minutes','older 15-minute policy','50% direction threshold','signal-driven holdings','five-minute observed-price rules']),
        'conditional_fill_and_no_realized_pl_claim':all(t in text for t in ['planning scenarios, not fills or realized P/L','no-fill baseline remains unchanged']),
        'actuals_forecast_totals':forecasts=={'evaluated':163,'mature_awaiting_data':35,'pending_maturity':66,'total':264} and sum(forecasts[k] for k in ['evaluated','mature_awaiting_data','pending_maturity'])==264,
        'raw_direction_accuracy_math':bounded['raw_direction']['correct']==84 and bounded['raw_direction']['evaluable']==163 and abs(bounded['raw_direction']['accuracy']-84/163)<1e-14 and f"{100*84/163:.2f}%"=='51.53%',
        'actual_price_totals':prices=={'COMPARED':135,'MATURE_AWAITING_DATA':19} and sum(prices.values())==154 and bounded['price_range_result']=={'in_range':17,'compared':135,'missing_excluded':19},
        'mature_missing_symbol_counts':bounded['mature_missing_forecasts_by_symbol']=={'COST':5,'CROX':11,'GOOG':2,'IONQ':1,'PATH':8,'TWST':8} and sum(bounded['mature_missing_forecasts_by_symbol'].values())==35,
        'cumulative_coverage_totals':{k:coverage[k] for k in ['forecasts','evaluated','mature_awaiting_data','pending_maturity']}=={'forecasts':5832,'evaluated':4669,'mature_awaiting_data':998,'pending_maturity':165} and 4669+998+165==5832,
        'preservation_ten_checks':preservation['status']=='PASS' and len(preservation['checks'])==10 and all(preservation['checks'].values()),
        'authority_twenty_nine_checks':authority['status']=='PASS' and len(authority['checks'])==29 and all(authority['checks'].values()),
        'reconciliation_756_and_42_checks':recon['status']=='PASS' and len(recon['checks'])==756 and all(recon['checks'].values()) and peer['status']=='PASS' and len(peer['checks'])==42 and all(peer['checks'].values()),
        'raw_decode_and_cursor_hash_limits_disclosed':all(t in text for t in ['does not independently decode every raw second-level DBN record','no separate outer-file checksum']),
        'unresolved_vendor_quality_disclosed':'degraded without a verified cause' in text and 'do not prove the original full requested range' in text,
        'supervision_held_not_claimed_released':final['supervision']=='HELD_FOR_FINAL_REVIEW' and 'Supervision closure is recorded separately after report review' in text,
    }
    for h,r in metrics['groups'].items():
        b,l=r['metric_limits']['brier_score'],r['metric_limits']['log_loss'];support=r['minimum_exact_route_support']
        expected=f"| {h} | {b['assessment']:.9f} / {b['training_rate_baseline']:.9f} | {l['assessment']:.9f} / {l['training_rate_baseline']:.9f} | {support['symbol']} {support['route']}: {support['fitted_rows']} |"
        checks['metric_table_'+h]=expected in text
    for h,r in archive['cohorts'].items():
        checks['cohort_table_'+h]=f"| {h} | {r['rows']:,} | {r['first_action_date']} | {r['last_action_date']} |" in text
    links=[]
    for md in [summary_path,BASE/'bounded-outputs/review.md']:
        for target in re.findall(r'\]\(([^)]+)\)',md.read_text()):
            target=target.strip('<>')
            links.append({'source':str(md),'target':target,'exists':Path(target).is_file(),'absolute':Path(target).is_absolute()})
    checks['all_summary_and_bounded_links_exist_absolute']=all(r['exists'] and r['absolute'] for r in links)
    result={'reviewed_at':datetime.now(timezone.utc).isoformat(),'status':'PASS' if all(checks.values()) else 'FAIL',
        'scope':'SAVED_REPORTS_EVIDENCE_HASHES_LINKS_AND_SUMMARY_ARITHMETIC_ONLY',
        'checks':checks,'issues':[k for k,v in checks.items() if not v],'links':links,
        'reviewed_summary':{'path':str(summary_path),'sha256':sha(summary_path),'phase':'PRE_CLOSURE_REVIEWED_VERSION'},
        'reviewed_final_verification':{'path':str(final_path),'sha256':sha(final_path),'phase':'PRE_CLOSURE_REVIEWED_VERSION'},
        'bounded_review':{'path':str(BASE/'bounded-outputs/bounded-output-summary.json'),'sha256':sha(BASE/'bounded-outputs/bounded-output-summary.json')},
        'quality_interpretation':'All four directional groups satisfy explicit v2 tolerances; only4h beats both baselines. Sizing remains fitted with zero qualified scopes. No predictive improvement, observed-price invention or realized-P/L claim found.',
        'supervision_closure':'Intentionally pending; subsequent closure append needs its own evidence/hash refresh.',
        'self_reference_handling':'Only this peer report is excluded from checking its own final-index entry to avoid circular hashes. Root may bind the final peer output when appending closure; reviewed summary/index hashes identify the preclosure versions.',
        'notes':['Sizing failure prose now explicitly lists all four return-MSE, daily/weekly Brier/log-loss, and weekly adverse-MSE failures. Initial peer diagnostic retained: expected summary-hash drift while the reviewed prose was edited, resolved by refreshing only the evidence index.'],
        'production_writes':0,'provider_calls':0,'broker_calls':0,'heavy_audits_repeated':0}
    (OUT/'completion-summary-peer-review.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'status':result['status'],'checks':len(checks),'issues':result['issues']}))
    if result['status']!='PASS':raise SystemExit(1)

if __name__=='__main__':main()
