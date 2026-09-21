"""Inspect exact tracked order history with GETs; never open ledger writable."""
import json
import sqlite3
import sys
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, 'C:/dev/ducketz')
from app.services.schwab import SchwabSession
from ml.stock_trader.horizon_ledger import HorizonLedger
from ml.stock_trader.horizon_broker import normalize_order_evidence, _walk_orders, ORDER_HISTORY_LIMIT

out = Path(__file__).resolve().parent
path = Path('C:/DATASTORE/state/independent-stock-trader/holdings.sqlite3')
result = {'observed_at': datetime.now(timezone.utc).isoformat(), 'scope': 'READ_ONLY_EXACT_TRACKED_ORDER_HISTORY',
          'ledger_mutations': 0, 'orders_submitted': 0, 'orders_cancelled': 0, 'orders_replaced': 0,
          'broker_data_http_methods': ['GET'], 'orders': []}
try:
    with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        account = db.execute("SELECT value FROM metadata WHERE key='account'").fetchone()[0]
        ids = [row[0] for row in db.execute("SELECT id FROM reservations WHERE status IN ('RESERVED','SUBMITTED','UNKNOWN','WORKING','PARTIAL')")]
        reservations = [HorizonLedger._reservation(db, rid) for rid in ids]
    broker = SchwabSession()
    auth_identity = broker.prepare_read_snapshot()
    if broker.stable_account_fingerprint() != account:
        raise ValueError('ACCOUNT_MISMATCH')
    now = datetime.now(timezone.utc)
    raw = broker.get_orders(from_entered_time=now-timedelta(days=14), to_entered_time=now, max_results=ORDER_HISTORY_LIMIT)
    broker.verify_read_snapshot(auth_identity)
    if broker.stable_account_fingerprint() != account:
        raise ValueError('ACCOUNT_CHANGED')
    if not isinstance(raw, list) or len(raw) >= ORDER_HISTORY_LIMIT:
        raise ValueError('INCOMPLETE_ORDER_HISTORY')
    rows = list(_walk_orders(raw))
    observed = datetime.now(timezone.utc).isoformat()
    result.update(account_matches=True, observed_at=observed, history_rows=len(raw))
    for reservation in reservations:
        if not reservation.broker_order_id:
            raise ValueError('UNKNOWN_SUBMISSION_ID_PRESERVED')
        matches = [row for row in rows if str(row.get('orderId', '')) == reservation.broker_order_id]
        if len(matches) != 1:
            raise ValueError('TRACKED_ID_NOT_UNIQUE_IN_HISTORY')
        row = matches[0]
        fields = ('status','quantity','filledQuantity','remainingQuantity','cancelable','enteredTime','closeTime','orderType','price','duration','session')
        safe = {key: row.get(key) for key in fields}
        safe.update(symbol=reservation.symbol, horizon=reservation.horizon, ledger_status=reservation.status,
                    reserved_quantity=reservation.quantity, ledger_filled_quantity=reservation.filled_quantity,
                    exact_broker_order_id_matched=True, present_fields=sorted(row.keys()),
                    order_legs=[{key: leg.get(key) for key in ('instruction','quantity','legId')} | {'instrument': {key: leg.get('instrument',{}).get(key) for key in ('symbol','assetType')}} for leg in row.get('orderLegCollection',[])],
                    activity_collection=row.get('orderActivityCollection', []))
        try:
            evidence = normalize_order_evidence(row, reservation, account_fingerprint=account, observed_at=observed)
            safe.update(native_normalization='VERIFIED', native_status=evidence.status,
                        native_filled=evidence.cumulative_filled_quantity, native_remaining=evidence.remaining_quantity)
        except Exception as exc:
            safe.update(native_normalization='REJECTED', native_failure_type=type(exc).__name__, native_failure_code=str(exc) if str(exc).isupper() else 'UNCLASSIFIED')
        result['orders'].append(safe)
    result['status'] = 'READ_COMPLETE'
except Exception as exc:
    result.update(status='READ_FAILED', error_type=type(exc).__name__)
(out/'broker-order-status-readonly.json').write_text(json.dumps(result, indent=2, default=str)+'\n', encoding='utf-8')
print(json.dumps(result, default=str))
