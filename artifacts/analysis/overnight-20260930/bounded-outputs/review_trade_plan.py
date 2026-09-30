"""Bounded immutable output review; no native archives, inference or broker calls."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import hashlib
import json

import numpy as np
import pandas as pd

from native_gate import resolve, frame as read_frame
ROOT = Path('C:/DATASTORE')
OUT = Path(__file__).resolve().parent
GAMEPLAN, PLAN, ACTUALS, NATIVE_REPORT, NATIVE_POINTERS = resolve()


def read(path):
    assert path.stat().st_size <= 64*1024*1024, 'BOUNDED_JSON_SIZE_EXCEEDED'
    return json.loads(path.read_text(encoding='utf-8-sig'))


def sha(path):
    assert path.stat().st_size <= 64*1024*1024, 'BOUNDED_HASH_SIZE_EXCEEDED'
    return hashlib.sha256(path.read_bytes()).hexdigest()


def close(a,b,tolerance='0.00000001'):
    return abs(Decimal(str(a))-Decimal(str(b))) <= Decimal(tolerance)


def main():
    gp, report, receipt, manifest = read(GAMEPLAN/'gameplan.json'),read(PLAN/'report.json'),read(PLAN/'receipt.json'),read(PLAN/'manifest.json')
    snapshot, ledger = read(PLAN/'account-snapshot.json'),read(PLAN/'direction-ledger.json')
    path, ref = read(PLAN/'planning-price-path.json'),read(PLAN/'planning-reference-completion.json')
    forecasts,intents,table = (read_frame(p) for p in [GAMEPLAN/'forecasts.parquet',GAMEPLAN/'option-strategy-intents.parquet',PLAN/'trade-plan.parquet'])
    synthetic = read_frame(PLAN/'synthetic-reference-bars.parquet')
    readable = (PLAN/'Gameplan.md').read_text(encoding='utf-8')
    symbols = gp['symbols']
    output_bindings=[]
    for name,item in manifest['output_files'].items():
        p = PLAN/name
        output_bindings.append({'path':str(p),'sha256':sha(p),'verified':sha(p)==item['checksum_sha256'] and p.stat().st_size==item['size']})
    gp_receipt,gp_manifest=read(GAMEPLAN/'receipt.json'),read(GAMEPLAN/'manifest.json')
    publication_bindings=[]
    for name in ['gameplan.json','forecasts.parquet','option-strategy-intents.parquet']:
        p=GAMEPLAN/name; item=gp_manifest['output_files'][name]
        publication_bindings.append({'path':str(p),'sha256':sha(p),'verified':sha(p)==item['checksum_sha256'] and p.stat().st_size==item['size']})
    for item in manifest['input_files']:
        if 'market-data' in item['path']:
            continue
        p=ROOT/item['path']
        publication_bindings.append({'path':str(p),'sha256':sha(p),'verified':item['status']=='present' and sha(p)==item['checksum_sha256'] and p.stat().st_size==item['size']})
    frozen_equal=True
    try:
        pd.testing.assert_frame_equal(forecasts.set_index('id').sort_index(),table.set_index('id').sort_index()[[c for c in forecasts if c!='id']],check_dtype=False,check_exact=True)
    except AssertionError:
        frozen_equal=False
    pointers={name:read(ROOT/f'ml/{name}-latest/run.json')['current'] for name in ['nightly-gameplan','gameplan-trade-plan','gameplan-actuals-review']}
    expected_points={f'{s}|2026-09-30|{hour:02}:00' for s in symbols for hour in range(4,18)}
    checks={
        'exact_264_rows_each':len(forecasts)==len(intents)==len(table)==24*len(symbols)==264,
        'exact_universe_24_rows_per_symbol':all(set(frame.symbol)==set(symbols) and frame.groupby('symbol').size().to_dict()=={s:24 for s in symbols} for frame in [forecasts,intents,table]),
        'forecast_ids_unique_frozen_preserved':forecasts.id.is_unique and table.id.is_unique and frozen_equal,
        'stock_only_intents_nonexecuting':set(intents.plan_status)=={'NO_TRADE_STOCK_ONLY'} and not intents.broker_orders_enabled.any() and intents.strategy_source_run.isna().all() and all(json.loads(value or '[]')==[] for value in intents.legs_json),
        'complete_pinned_plan':receipt['status']==report['status']=='COMPLETE' and receipt['action_date']==report['action_date']==gp['action_date']=='2026-09-30' and receipt['source_gameplan_run']==report['source_gameplan_run']==str(GAMEPLAN.relative_to(ROOT)).replace('\\','/'),
        'gameplan_receipt_binding':receipt['source_receipt_sha256']==report['source_receipt_sha256']==sha(GAMEPLAN/'receipt.json'),
        'manifest_receipt_and_outputs':receipt['manifest_sha256']==sha(PLAN/'manifest.json') and all(row['verified'] for row in output_bindings),
        'selected_publication_files_manifest_bound':gp_receipt['manifest_checksum_sha256']==sha(GAMEPLAN/'manifest.json') and all(row['verified'] for row in publication_bindings),
        'latest_gameplan_pointer':pointers['nightly-gameplan']['run_path']==str(GAMEPLAN.relative_to(ROOT)).replace('\\','/') and pointers['nightly-gameplan']['receipt_checksum_sha256']==sha(GAMEPLAN/'receipt.json'),
        'latest_tradeplan_pointer':pointers['gameplan-trade-plan']['run_path']==str(PLAN.relative_to(ROOT)).replace('\\','/') and pointers['gameplan-trade-plan']['receipt_sha256']==sha(PLAN/'receipt.json'),
        'latest_actuals_pointer':pointers['gameplan-actuals-review']['run_path']==str(ACTUALS.relative_to(ROOT)).replace('\\','/') and pointers['gameplan-actuals-review']['receipt_sha256']==sha(ACTUALS/'receipt.json'),
        'fresh_readonly_account_snapshot':pd.Timestamp(report['observed_at'])<=pd.Timestamp(snapshot['observed_at'])<=pd.Timestamp(report['observed_at'])+pd.Timedelta(seconds=60) and snapshot['orders_enabled'] is False and snapshot['orders_placed']==0 and snapshot['broker_data_http_methods']==['GET'],
        'snapshot_ownership_safe_no_working_orders':snapshot['ownership']['safe_for_planning'] is True and snapshot['working_order_count']==0 and snapshot['reserved_cash']==0 and not any(snapshot['pending_buy_shares'].values()) and not any(snapshot['pending_sell_shares'].values()),
        'literal_cash_not_broker_capacity':snapshot['available_cash']==snapshot['balances']['cash_balance']-snapshot['reserved_cash'] and snapshot['available_cash']<=snapshot['broker_available_cash'],
        'report_embedded_sources_match':report['snapshot']==snapshot and report['direction_based_projection']==ledger and report['reference_completion']=={k:v for k,v in ref.items() if k!='synthetic_bars'},
        'synthetic_reference_json_matches_parquet':synthetic.to_dict('records')==ref['synthetic_bars'],
        'complete_projection_and_prices':ledger['status']==report['direction_projection_status']=='COMPLETE' and set(path['points'])==expected_points and all(point['status']=='AVAILABLE' and point['sample_count']>=2 for point in path['points'].values()),
        'one_price_observation_clock':path['observed_at']==ref['observed_at'],
        'source_contract_retained':gp['target_price_dataset']==path['price_dataset']==ref['price_dataset']=='XNAS.ITCH' and gp['target_price_source_contract']==path['price_source_contract']==ref['price_source_contract']=='xnas-itch-archive-v1',
        'quantity_columns_adjacent_readable':readable.count('Projected Trade Quantity | Direction Based Trade Qty')==len(symbols),
        'nonentry_rows_have_no_direction_quantity':table.loc[~table.execution_eligible,'direction_based_trade_quantity'].isna().all(),
        'zero_orders_in_all_saved_outputs':all(d['orders_placed']==0 and not d['broker_orders_enabled'] for d in [gp,report,receipt,read(ACTUALS/'report.json'),read(ACTUALS/'receipt.json')]),
        'explicit_review_only_authority':report['execution_authority']=='REVIEW_ONLY_REVALIDATE_AT_ENTRY' and gp_receipt['execution_authority']=='ADVISORY_PAPER_ONLY',
        'original_deadline_retained_without_exception':pd.Timestamp(report['deadline_at'])==pd.Timestamp(report['effective_deadline_at'])==pd.Timestamp('2026-09-30T11:00:00Z') and report['deadline_exception'] is None,
        'recorded_current_policies_preserved':report['direction_policy_version']=='stock-direction-50-v2' and report['direction_down_threshold']==report['direction_up_threshold']==0.5 and ledger['holding_policy']=='accumulate_bullish_sell_on_bearish_no_scheduled_expiry_v1' and path['contract_version']=='conditional-hourly-planning-price-path-v3' and ref['contract_version']=='sparse-session-planning-reference-completion-v2' and ref['max_gap_minutes']==240,
    }
    baseline=read(Path('C:/dev/ducketz/artifacts/analysis/overnight-20260930/preflight/post-reconciliation-baseline.json'))
    before_held=json.loads(baseline['ledger_tables']['snapshots']['latest']['payload'])['held_shares']
    checks['snapshot_holdings_match_reconciled_baseline']={s:Decimal(str(v)) for s,v in snapshot['held_shares'].items()}=={s:Decimal(str(v)) for s,v in before_held.items()}
    checks['no_fill_baseline_preserved']=ledger['no_fill_baseline']=={'cash':snapshot['available_cash'],'held_shares':snapshot['held_shares']} and ledger['starting_positions']==snapshot['held_shares']
    cash={k:Decimal(str(snapshot['available_cash'])) for k in ['low','base','high']}
    held={s:Decimal(str(v)) for s,v in snapshot['held_shares'].items()}
    events_by_clock=defaultdict(list)
    event_checks=[]
    for sequence,event in enumerate(ledger['events'],1):
        valid=event['sequence']==sequence and event['quantity']>0 and int(event['quantity'])==event['quantity']
        symbol=event['symbol']; quantity=Decimal(str(event['quantity']))
        valid=valid and close(event['shares_before'],held[symbol])
        for case,price_case in [('low','high' if event['action']=='BUY' else 'low'),('base','base'),('high','low' if event['action']=='BUY' else 'high')]:
            change=quantity*Decimal(str(event['price_'+price_case]))*(-1 if event['action']=='BUY' else 1)
            valid=valid and close(event['cash_before_'+case],cash[case]) and close(event['cash_change_'+case],change,'0.0051')
            cash[case]+=change
            valid=valid and close(event['cash_'+case],cash[case],'0.0051')
        held[symbol]+=quantity*(1 if event['action']=='BUY' else -1)
        valid=valid and close(event['shares_after'],held[symbol]) and held[symbol]>=0 and cash['low']>=0
        event_row=table.loc[table.id.eq(event['forecast_id'])].iloc[0]
        point=path['points'][event_row.planning_price_point_key]
        valid=valid and close(event_row.direction_based_trade_quantity,quantity*(1 if event['action']=='BUY' else -1))
        valid=valid and all(close(event['price_'+a],point['planned_price_'+b]) for a,b in [('low','low'),('base','mid'),('high','high')])
        event_checks.append({'sequence':sequence,'verified':bool(valid)})
        events_by_clock[event['timestamp']].append(event)
    checks['every_event_conserves_cash_shares_and_price']=all(row['verified'] for row in event_checks)
    checks['event_sequence_chronological']=[e['timestamp'] for e in ledger['events']]==sorted(e['timestamp'] for e in ledger['events'])
    checks['each_hour_sells_before_buys']=all([e['action'] for e in rows]==sorted([e['action'] for e in rows],key=lambda a:0 if a=='SELL' else 1) for rows in events_by_clock.values())
    checks['ending_cash_matches_ledger']=all(close(ledger['summary']['ending_cash_'+case],cash[case],'0.0051') for case in cash)
    checks['ending_positions_match_events']={s:Decimal(str(v)) for s,v in ledger['ending_positions'].items()}==held
    allocation_shares=defaultdict(Decimal)
    for lot in ledger['ending_allocations']:
        allocation_shares[lot['symbol']]+=Decimal(str(lot['quantity']))
    checks['ending_lots_conserve_owned_shares']=set(allocation_shares).issubset(set(held)) and all(allocation_shares[s]==held[s] for s in held)
    running_cash={k:snapshot['available_cash'] for k in ['low','base','high']}
    running_held=dict(snapshot['held_shares'])
    hour_checks=[]
    for hour in ledger['hourly']:
        events=events_by_clock[hour['timestamp']]
        if events:
            running_cash={k:events[-1]['cash_'+k] for k in running_cash}
            for event in events:running_held[event['symbol']]=event['shares_after']
        okay=all(close(hour['cash_'+k],v) for k,v in running_cash.items()) and hour['held_shares']==running_held and hour['event_sequences']==[e['sequence'] for e in events]
        clock=pd.Timestamp(hour['timestamp']).tz_convert('America/Los_Angeles').strftime('%H:%M')
        matching=table.loc[table.execution_eligible & table.action_anchor_local.eq(clock)]
        for row in matching.itertuples():
            okay=okay and all(close(getattr(row,'projected_cash_after_'+k),hour['cash_'+k]) for k in running_cash) and close(row.projected_shares_after,hour['held_shares'][row.symbol])
        hour_checks.append({'clock':clock,'rows':len(matching),'verified':bool(okay)})
    checks['all_fourteen_hour_balances_and_forecast_rows_match']=len(hour_checks)==14 and all(row['verified'] for row in hour_checks)
    checks['event_counts_match_summary']=Counter(e['action'] for e in ledger['events'])=={'BUY':ledger['summary']['buy_events'],'SELL':ledger['summary']['sell_events']} and len(ledger['events'])==ledger['summary']['trade_events']
    anchors=[]
    for reference in ref['references'].values():
        symbol=reference['symbol']; is_synthetic=reference['is_synthetic']
        rows=synthetic.loc[synthetic.symbol.eq(symbol)]
        observed,boundary=pd.Timestamp(reference['observed_at']),pd.Timestamp(reference['boundary_at'])
        valid=reference['session']=='2026-09-29' and close((boundary-observed).total_seconds()/60,reference['gap_minutes'])
        if is_synthetic:
            valid=valid and reference['status']=='AVAILABLE_SYNTHETIC' and reference['reason']=='ASSUMED_NO_TRADES' and 5<reference['gap_minutes']<=reference['max_gap_minutes']
            valid=valid and observed>=pd.Timestamp(reference['regular_session_close_at']) and pd.Timestamp(reference['effective_at'])==boundary and len(rows)==reference['fill_count']==reference['gap_minutes']
            expected_times=pd.date_range(observed,boundary-pd.Timedelta(minutes=1),freq='min')
            valid=valid and list(pd.to_datetime(rows.timestamp,utc=True))==list(expected_times)
            valid=valid and rows.is_synthetic.all() and rows.volume.eq(0).all() and rows.reason.eq('ASSUMED_NO_TRADES').all() and all(rows[col].eq(reference['price']).all() for col in ['open','high','low','close'])
            valid=valid and rows.original_observed_at.eq(reference['observed_at']).all()
            coverage=reference['source_coverage']
            valid=valid and coverage['native_partition_verified'] is True and pd.Timestamp(coverage['start'])<=observed and pd.Timestamp(coverage['end'])>=boundary and pd.Timestamp(coverage['published_at'])<=pd.Timestamp(ref['observed_at'])
            valid=valid and f'| {symbol} | ${reference["price"]:.2f} |' in readable and f'| {reference["fill_count"]} |' in readable
            observed_label=observed.tz_convert('America/Los_Angeles').strftime('%b %d, %Y %H:%M %Z')
            boundary_label=boundary.tz_convert('America/Los_Angeles').strftime('%b %d, %Y %H:%M %Z')
            valid=valid and f'| {symbol} | ${reference["price"]:.2f} | {observed_label} | {boundary_label} | {reference["fill_count"]} |' in readable
        else:
            valid=valid and reference['status']=='AVAILABLE_OBSERVED' and reference['gap_minutes']<=5 and len(rows)==0
        anchors.append({k:reference[k] for k in ['symbol','status','price','observed_at','effective_at','gap_minutes','fill_count'] }|{'verified':bool(valid)})
    checks['all_current_reference_anchors_verified']=len(anchors)==11 and all(a['verified'] for a in anchors) and len(synthetic)==sum(a['fill_count'] for a in anchors)
    checks['synthetics_confined_to_planning_contract']=ref['native_prices_modified'] is False and ref['model_training_prices_modified'] is False and ref['regular_session_prices_filled'] is False and set(synthetic.origin_source_contract)=={'xnas-itch-archive-v1'}
    checks['original_reference_observation_retained_all_prices']=all(point['reference_observed_at']==ref['references'][point['symbol']+'|2026-09-30']['observed_at'] and point['reference_is_synthetic']==ref['references'][point['symbol']+'|2026-09-30']['is_synthetic'] for point in path['points'].values())
    result={'reviewed_at':datetime.now(timezone.utc).isoformat(),'status':'PASS' if all(checks.values()) else 'REVIEW_REQUIRED',
        'scope':'BOUNDED_SAVED_PUBLICATION_OUTPUTS_ONLY_NO_ARCHIVE_RELOAD_MODEL_INFERENCE_BROKER_OR_PROVIDER_CALLS',
        'checks':checks,'issues':[key for key,value in checks.items() if not value],
        'paths':{'gameplan':str(GAMEPLAN),'tradeplan':str(PLAN),'actuals':str(ACTUALS),'readable':str(PLAN/'Gameplan.md')},
        'symbols':symbols,'rows':{'forecasts':len(forecasts),'intents':len(intents),'tradeplan':len(table),'price_points':len(path['points']),'synthetic_planning_bars':len(synthetic)},
        'forecast_roles':dict(Counter(forecasts.target_role)),'saved_direction_policy':report['direction_policy_version'],
        'saved_holding_policy':ledger['holding_policy'],'saved_planning_reference_contract':ref['contract_version'],
        'snapshot_observed_at':snapshot['observed_at'],'literal_available_cash':snapshot['available_cash'],'broker_capacity_separate':snapshot['broker_available_cash'],
        'held_shares':snapshot['held_shares'],'ledger_summary':ledger['summary'],'ending_holdings':ledger['ending_positions'],
        'event_checks':event_checks,'hour_checks':hour_checks,'current_reference_anchors':anchors,
        'reference_unavailability':dict(Counter(a['status'] for a in anchors)),
        'source_output_bindings':output_bindings,'selected_publication_bindings':publication_bindings,'pointers':pointers,
        'limitations':['Saved outputs are audited here; peer native audit independently verifies source bytes, cohorts, models and derivation.',
            'Cash/holdings are the planning snapshot and hypothetical conditional fills; no broker recapture or realized P/L is claimed.',
            'Synthetic current anchors and historical closing carries are planning assumptions only; native labels/actuals retain observed endpoint rules.'],
        'saved_policy_note':'Audits the current documented YG 50-percent direction policy, signal-driven holding and v3/240-minute sparse-session planning policy. These are not the older 54/46 thresholds, fixed expiry or v2/15-minute current-anchor policy quoted in the automation history.',
        'provider_calls':0,'broker_calls':0,'production_writes':0,
        'ownership_mutation_scope':'Bounded saved ownership snapshot compared with post-reconciliation saved holdings; root preservation audit separately checks operational database bytes/tables.'}
    (OUT/'final-output-review.json').write_text(json.dumps(result,indent=2,default=str)+'\n',encoding='utf-8')
    print(json.dumps({'status':result['status'],'checks':len(checks),'issues':result['issues'],'rows':result['rows'],'cash':ledger['summary'],'anchors':anchors},indent=2))


if __name__=='__main__':
    main()
