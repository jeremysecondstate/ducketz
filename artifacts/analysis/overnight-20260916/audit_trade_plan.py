"""Bounded saved-current-trade-plan inspection; no recomputation or broker calls."""
from pathlib import Path
from datetime import datetime,timezone
from collections import Counter
import json,hashlib
import pandas as pd

RUN=Path('C:/DATASTORE/ml/gameplan-trade-plan-runs/20260916T060421.076865Z')
OUT=Path(__file__).resolve().parent
report=json.loads((RUN/'report.json').read_text())
receipt=json.loads((RUN/'receipt.json').read_text())
snapshot=json.loads((RUN/'account-snapshot.json').read_text())
path=json.loads((RUN/'planning-price-path.json').read_text())
completion=json.loads((RUN/'planning-reference-completion.json').read_text())
ledger=json.loads((RUN/'direction-ledger.json').read_text())
manifest=json.loads((RUN/'manifest.json').read_text())
assert receipt['action_date']=='2026-09-16' and receipt['source_gameplan_run']=='ml/nightly-gameplan-runs/20260916T060149.932707Z'
assert receipt['manifest_sha256']==hashlib.sha256((RUN/'manifest.json').read_bytes()).hexdigest()
assert receipt['orders_placed']==report['orders_placed']==ledger['orders_placed']==snapshot['orders_placed']==0
assert not receipt['broker_orders_enabled'] and not report['broker_orders_enabled'] and not ledger['broker_orders_enabled'] and not snapshot['orders_enabled']
trades=pd.read_parquet(RUN/'trade-plan.parquet',columns=['symbol','model_group','model_status','direction','trade_planning_reason','trade_quantity','projected_trade_quantity','price_band_status'])
synthetic=pd.read_parquet(RUN/'synthetic-reference-bars.parquet')
assert len(trades)==264 and trades.groupby('symbol').size().eq(24).all()
symbols=sorted(trades.symbol.unique())
availability={symbol:dict(Counter(p['status'] for p in path['points'].values() if p['symbol']==symbol)) for symbol in symbols}
reference_fields=['symbol','status','reason','price','observed_at','effective_at','gap_minutes','fill_count','is_synthetic','session','action_date','source_contract','dataset','source_coverage']
references={key:{k:value.get(k) for k in reference_fields} for key,value in completion['references'].items()}
historical=completion['historical_references']
historical_summary=dict(Counter(x['status'] for x in historical.values()))
assert len(synthetic)==len(completion['synthetic_bars'])==312
assert synthetic.volume.eq(0).all() and synthetic.is_synthetic.eq(True).all()
assert synthetic.open.eq(synthetic.close).all() and synthetic.high.eq(synthetic.close).all() and synthetic.low.eq(synthetic.close).all()
assert ledger['status']==report['direction_projection_status']=='UNAVAILABLE_PRICE_REFERENCES'
assert not ledger['events'] and not ledger['hourly'] and not ledger['summary'] and not ledger['ending_positions']
assert snapshot['observed_at']==report['snapshot']['observed_at']
weekly=trades.loc[trades.model_group.eq('1w')]
result={'audited_at':datetime.now(timezone.utc).isoformat(),'status':'CURRENT_SAVED_TRADE_PLAN_BOUNDED_AUDIT_PASSED','run_path':str(RUN),
 'receipt':receipt,'original_deadline':report['deadline_at'],'effective_deadline':report['effective_deadline_at'],
 'manifest_receipt_binding':'PASS','snapshot':{k:snapshot[k] for k in ['observed_at','authority','status','cash_status','cash_policy','available_cash','broker_available_cash','reserved_cash','working_order_count','held_shares','pending_buy_shares','pending_sell_shares','account_equity','gross_exposure','broker_data_http_methods','broker_state_capture','ownership','reason_codes','cash_reason_codes','orders_placed','orders_enabled']},
 'rows_by_symbol':trades.groupby('symbol').size().to_dict(),'option_intent_rows':report['option_intent_rows'],
 'path_contract':path['contract_version'],'price_observation_time':path['observed_at'],'source_contract':path['price_source_contract'],'dataset':path['price_dataset'],
 'hourly_price_point_count':len(path['points']),'point_availability_by_symbol':availability,'price_band_status_counts':report['price_band_status_counts'],
 'reference_completion_contract':completion['contract_version'],'current_references':references,'historical_reference_status_counts':historical_summary,
 'synthetic_minute_rows':len(synthetic),'synthetic_rows_by_symbol':synthetic.groupby('symbol').size().to_dict(),
 'synthetic_basic_saved_row_checks':{'all_explicitly_synthetic':True,'all_zero_volume':True,'all_ohlc_equal':True,'recomputed_price_path':False},
 'projection':{'status':ledger['status'],'reason':ledger['reason'],'unavailable_point_count':len(ledger['unavailable_points']),'events':len(ledger['events']),'hourly_summaries':len(ledger['hourly']),'has_ending_projection':bool(ledger['summary'] or ledger['ending_positions']),'interpretation':'Unavailable chronological projection, not zero projected trades or a zero ending balance.'},
 'scheduled_preview_reason_counts':report['trade_reason_counts'],'provisional_buy_rows':report['provisional_buy_rows'],
 'weekly_saved_rows':weekly.to_dict('records'),'weekly_direction_counts':weekly.direction.value_counts().to_dict(),
 'manual_policy_limit':'The saved scheduled-entry preview exclusions are not evidence that the separately selected manual Gameplan policy skips its 11 weekly instructions. Model assessment status is preserved.',
 'planning_limitations':report['limitations'],'native_prices_modified':completion['native_prices_modified'],'model_training_prices_modified':completion['model_training_prices_modified'],
 'scope':'Saved artifact inspection only; no full price-path recomputation, raw-source verification or final-helper duplication. Root owns full final verification. No provider/broker calls, claims, process, pipeline or control mutations.'}
(OUT/'trade-plan-audit.json').write_text(json.dumps(result,indent=2,default=str)+'\n')
print(json.dumps({'audited_at':result['audited_at'],'status':result['status'],'snapshot_observed_at':snapshot['observed_at'],'cash':snapshot['available_cash'],'availability':availability,'synthetic_by_symbol':result['synthetic_rows_by_symbol'],'historical_reference_status_counts':historical_summary,'projection_status':ledger['status'],'weekly_directions':result['weekly_direction_counts']},indent=2))
