"""Read-only post-session observations of cross-horizon fallback orders.

No broker/account IDs, account fingerprints, profit estimates, or imagined
counterfactual outcomes are emitted. This reader never initializes the ledger.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo


_OPEN = ('RESERVED', 'SUBMITTED', 'UNKNOWN', 'WORKING', 'PARTIAL')
_PACIFIC = ZoneInfo('America/Los_Angeles')


def _ref(kind, identity):
    return hashlib.sha256(json.dumps([kind, identity], separators=(',', ':')).encode()).hexdigest()


def _donor_ref(identity):
    # Same native-allocation hash as the planning ledger, permitting joins
    # without exposing the actual account-bound allocation identity.
    return hashlib.sha256(str(identity).encode('utf-8')).hexdigest()


def _finished(result):
    return {**result, 'read_completed_at':datetime.now(timezone.utc).isoformat()}


def review_fallback_day(ledger_path: Path, action_date: str) -> dict:
    """Observe one Pacific action date in one SQLite read-only transaction.

    Includes older fallback reservations only while still open or if filled on
    the requested date, because those shares consume the requested day's cap.
    Missing baseline is an explicit unavailable state, not evidence of no sales.
    """
    day = date.fromisoformat(action_date)
    if day.isoformat() != action_date:
        raise ValueError('Action date must be an exact ISO date')
    start = datetime.combine(day, time(), _PACIFIC).astimezone(timezone.utc).isoformat()
    end = datetime.combine(day+timedelta(days=1), time(), _PACIFIC).astimezone(timezone.utc).isoformat()
    path = Path(ledger_path).resolve()
    result = {'schema_version':'hierarchical-bearish-fallback-review-v1', 'action_date':action_date,
        'reviewed_at':datetime.now(timezone.utc).isoformat(), 'fill_date_timezone':'America/Los_Angeles',
        'observation_scope':'Ledger-recorded orders and broker-confirmed fills; no broker requests or counterfactual profit.',
        'read_only':True, 'baseline':None, 'fallback_orders':None, 'daily_budget_usage':None,
        'sales_counts':None, 'read_started_at':datetime.now(timezone.utc).isoformat(),
        'query_snapshot':'One SQLite read-only BEGIN transaction; recorded evidence, not a fresh broker observation.',
        'latest_recorded_snapshot_at':None}
    if not path.is_file():
        return _finished({**result, 'status':'LEDGER_UNAVAILABLE', 'reason':'No existing ledger; execution is not inferred.'})
    db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'snapshots' in tables:
            result['latest_recorded_snapshot_at'] = db.execute('SELECT MAX(observed_at) FROM snapshots').fetchone()[0]
        if 'fallback_days' not in tables:
            return _finished({**result, 'status':'NOT_ENABLED', 'reason':'Ledger has no fallback baseline table; execution is not inferred.'})
        baselines = db.execute('SELECT payload FROM fallback_days WHERE action_date=?', (action_date,)).fetchall()
        if not baselines:
            return _finished({**result, 'status':'NO_BASELINE', 'reason':'No frozen baseline for this date; execution is not inferred.'})
        if len(baselines) != 1:
            raise ValueError('Review requires exactly one account-bound day baseline')
        baseline = json.loads(baselines[0]['payload'])
        if baseline['action_date'] != action_date:
            raise ValueError('Fallback day row and payload dates differ')
        result['baseline'] = {'baseline_ref':_ref('baseline',baseline['baseline_id']),
            'observed_at':baseline['observed_at'], 'source_fingerprint':baseline['source_fingerprint'],
            'policy_version':baseline['policy']['policy_version'],
            'symbols':baseline['symbols'],
            'donors':[{'donor_allocation_id_sha256':_donor_ref(identity),'symbol':record['symbol'],
                       'horizon':record['horizon'], 'initial_owned_shares':record['initial_owned_shares'],
                       'daily_cap':record['daily_cap']} for identity,record in sorted(baseline['donors'].items())]}
        # Bound the query by requested-day reservations/fills and live carryovers.
        # snapshot timestamps are normalized UTC by the ledger writer.
        rows = db.execute("""SELECT r.*,a.symbol,a.horizon,s.observed_at AS reserved_snapshot_at
            FROM reservations r JOIN allocations a ON a.id=r.allocation
            LEFT JOIN snapshots s ON s.id=json_extract(r.request,'$.snapshot')
            WHERE r.side='SELL' AND (
                (s.observed_at>=? AND s.observed_at<?)
                OR EXISTS(SELECT 1 FROM fills f WHERE f.reservation=r.id AND f.executed_at>=? AND f.executed_at<?)
                OR (json_extract(r.request,'$.kind')='fallback-direction-exit' AND
                    (json_extract(r.request,'$.action_date')=? OR r.status IN ('RESERVED','SUBMITTED','UNKNOWN','WORKING','PARTIAL'))))
            ORDER BY r.id""", (start,end,start,end,action_date)).fetchall()
        orders, symbol_use, donor_use = [], {}, {}
        counts = {'fallback_orders_requested':0, 'normal_sell_orders_requested':0,
            'fallback_orders_with_fills':0, 'normal_sell_orders_with_fills':0,
            'fallback_shares_filled':0, 'normal_sell_shares_filled':0,
            'outstanding_fallback_orders':0, 'outstanding_fallback_shares':0}
        for row in rows:
            request = json.loads(row['request'])
            fallback = request.get('kind') == 'fallback-direction-exit'
            fills = db.execute('SELECT quantity,price,executed_at FROM fills WHERE reservation=? AND executed_at>=? AND executed_at<? ORDER BY executed_at,id',
                               (row['id'],start,end)).fetchall()
            filled_today = sum(fill['quantity'] for fill in fills)
            requested_today = (request.get('action_date') == action_date if fallback else
                row['reserved_snapshot_at'] is not None and start <= row['reserved_snapshot_at'] < end)
            kind = 'fallback' if fallback else 'normal_sell'
            counts[kind+'_orders_requested'] += int(requested_today)
            counts[kind+'_orders_with_fills'] += int(bool(fills))
            counts[kind+'_shares_filled'] += filled_today
            if not fallback:
                continue
            remaining = row['quantity']-row['filled'] if row['status'] in _OPEN else 0
            counts['outstanding_fallback_orders'] += int(remaining > 0)
            counts['outstanding_fallback_shares'] += remaining
            used = filled_today+remaining
            symbol_use[row['symbol']] = symbol_use.get(row['symbol'],0)+used
            donor_use[row['allocation']] = donor_use.get(row['allocation'],0)+used
            orders.append({'reservation_ref':_ref('reservation',row['id']), 'symbol':row['symbol'],
                'origin_action_date':request['action_date'], 'trigger_forecast_id':request['trigger_forecast'],
                'trigger_horizon':request['trigger_horizon'], 'donor_horizon':row['horizon'],
                'donor_allocation_id_sha256':_donor_ref(row['allocation']), 'requested_quantity':row['quantity'],
                'slot_quota':request['slot_quota'], 'status':row['status'],
                'filled_quantity_all_dates':row['filled'], 'filled_quantity_on_action_date':filled_today,
                'outstanding_reserved_quantity':remaining,
                'unknown_reserved_quantity':remaining if row['status']=='UNKNOWN' else 0,
                'terminal_unfilled_released_quantity':row['quantity']-row['filled'] if row['status'] in {'CANCELLED','REJECTED'} else 0,
                'fills_on_action_date':[{'quantity':fill['quantity'],'price':fill['price'],
                                         'executed_at':fill['executed_at']} for fill in fills]})
        result.update({'status':'OBSERVED', 'fallback_orders':orders, 'sales_counts':counts,
            'daily_budget_usage':{
                'definition':'Confirmed fallback fills on this Pacific date plus all outstanding fallback reservations, including prior-day carryovers.',
                'symbols':{symbol:{'daily_cap':baseline['symbols'].get(symbol,{}).get('daily_cap'),
                    'filled_plus_reserved':symbol_use.get(symbol,0),
                    'remaining':max(0,record['daily_cap']-symbol_use.get(symbol,0))}
                    for symbol,record in sorted(baseline['symbols'].items())},
                'donors':[{'donor_allocation_id_sha256':_donor_ref(identity), 'symbol':record['symbol'],
                    'horizon':record['horizon'], 'daily_cap':record['daily_cap'],
                    'filled_plus_reserved':donor_use.get(identity,0),
                    'remaining':max(0,record['daily_cap']-donor_use.get(identity,0))}
                    for identity,record in sorted(baseline['donors'].items())]}})
        return _finished(result)
    finally:
        db.rollback()
        db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--datastore-root',type=Path,required=True)
    parser.add_argument('--action-date',required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    args = parser.parse_args(argv)
    root, output = args.datastore_root.resolve(), args.output_dir.resolve()
    if output == root or output.is_relative_to(root):
        parser.error('Review output must be outside the datastore; native state is read-only')
    if output.exists():
        parser.error('Review output directory must be new; existing observations cannot be replaced')
    review = review_fallback_day(root/'state/independent-stock-trader/holdings.sqlite3', args.action_date)
    output.parent.mkdir(parents=True,exist_ok=True)
    output.mkdir()
    destination = output/'report.json'
    with destination.open('x',encoding='utf-8') as handle:
        json.dump(review,handle,indent=2,allow_nan=False)
        handle.write('\n')
    source = {'source_fingerprint':(review.get('baseline') or {}).get('source_fingerprint'),
        'read_started_at':review['read_started_at'], 'read_completed_at':review['read_completed_at'],
        'latest_recorded_snapshot_at':review['latest_recorded_snapshot_at'],
        'query_snapshot':review['query_snapshot']}
    manifest = {'schema_version':'hierarchical-bearish-fallback-review-manifest-v1',
        'action_date':args.action_date, 'status':review['status'], 'source_observation':source,
        'output_files':{'report.json':{'checksum_sha256':hashlib.sha256(destination.read_bytes()).hexdigest(),
                                      'size':destination.stat().st_size}}}
    manifest_path = output/'manifest.json'
    with manifest_path.open('x',encoding='utf-8') as handle:
        json.dump(manifest,handle,indent=2,allow_nan=False)
        handle.write('\n')
    print(json.dumps({'status':review['status'],'review_path':str(destination),'manifest_path':str(manifest_path)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
