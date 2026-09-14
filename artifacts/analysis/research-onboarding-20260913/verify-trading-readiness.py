"""Verify configured forecasts and live prerequisites without entering a slot."""
from datetime import date, datetime, timezone
import json
from pathlib import Path
from datafetching.symbol_universe import read_symbols
from datafetching.symbol_onboarding import _write
from ml.stock_trader.independent_session import _independent_forecast_preflight
from ml.stock_trader.independent_signals import load_current_independent_gameplan_signals
from ml.stock_trader.gameplan import read_gameplan_stock_activation_intent
from ml.gameplan_trade_snapshot import capture_trade_planning_snapshot

folder=Path(__file__).resolve().parent
root=Path('C:/DATASTORE')
symbols=read_symbols()
expected=json.loads((folder/'plan.json').read_text(encoding='utf-8'))['candidate_symbols']
assert list(symbols)==expected
ready=_independent_forecast_preflight(root,action_date=date(2026,9,14))
assert ready['status']=='READY', ready
now=datetime.now(timezone.utc)
signals,_=load_current_independent_gameplan_signals(root, as_of=now,
    require_promoted_model_reports=True, late_opening_date='2026-09-14')
controls=read_gameplan_stock_activation_intent(root)
snapshot=capture_trade_planning_snapshot(root,symbols=symbols)
assert controls.active
assert snapshot['status']=='OBSERVED' and snapshot['cash_status']=='CASH_ONLY_BOUNDED'
assert snapshot['ownership']['safe_for_planning'] is True
slot=root/'state/independent-stock-trader/entry-slots/20260914T110000Z.json'
assert not slot.exists(), 'The opening slot has already been consumed; never replay it'
assert not (root/'locks/independent-stock-session.lock').exists(), 'An existing session owner must be adopted'
result={'status':'READY_FOR_USER_MANUAL_START','observed_at':now.isoformat(),'symbols':list(symbols),
        'forecast_readiness':ready, 'late_opening_signals':[{'symbol':s,'horizon':h,'probability':signal.calibrated_probability,
        'forecast_id':signal.prediction_id,'target_end':signal.target_window_end,'actionable_until':signal.actionable_until}
        for (s,h),signal in signals.items()], 'controls_active':controls.active,'account_snapshot_status':snapshot['status'],
        'ownership_safe':snapshot['ownership']['safe_for_planning'],'opening_slot_unconsumed':True,
        'orders_placed':0,'trader_started':False}
_write(folder/'trading-readiness.json',result)
print(json.dumps({'status':result['status'],'symbols':len(symbols),'qualified_windows':len(ready['qualified_forecast_windows']),
                  'late_opening_signals':len(signals),'orders_placed':0}))
