"""Offline October 1 fallback policy and conditional planning assertions.

Receives already manifest-verified saved inputs. Does not read live holdings,
initialize a ledger, call a provider/broker, change controls or submit orders.
"""
from decimal import Decimal
import re


def verify_fallback_binding(publication, read, require):
    from ml.stock_trader.cross_horizon_fallback import policy_for_action_date, verify_policy_metadata
    day = publication.receipt['action_date']
    policy = verify_policy_metadata(publication.run_directory, publication.manifest, publication.receipt)
    require(day >= '2026-10-01' and policy == policy_for_action_date(day) and policy is not None,
            'Fresh October 1+ publication lacks the exact approved fallback policy')
    return policy


def verify_planning_policy(policy, report, receipt, manifest, ledger, require):
    for label, item in [('report', report), ('receipt', receipt), ('manifest', manifest['configuration']), ('ledger', ledger)]:
        require(item.get('cross_horizon_fallback_policy') == policy,
                'Planning fallback binding differs: ' + label)


def verify_holdings_and_fallback(rows, snapshot, ledger, *, symbols, require, same_number):
    """Independently roll horizon lots forward and check frozen caps and slots.

Exact individual-lot selection is also checked by the caller's complete native
recomputation. This rollforward independently attributes each fallback sale to
the donor horizon, applies no cap to normal sales, and freezes initial budgets.
"""
    import pandas as pd
    from ml.stock_direction_policy import stock_direction
    from ml.gameplan_cash_ledger import SIGNAL_DRIVEN_HOLDING_POLICY
    zero = Decimal(0)
    number = lambda value: Decimal(str(value))
    horizons = ('1h', '4h', '1d', '1w')
    weights = {'1h': 1, '4h': 2, '1d': 3}
    slots = [(hour, horizon) for hour in range(4, 17) for horizon in ('1h', '4h', '1d')
             if horizon == '1h' or (horizon == '4h' and hour in (4, 8, 12, 16))
             or (horizon == '1d' and hour == 4)]
    require(len(slots) == 18 and sum(weights[h] for _, h in slots) == 24, 'Invalid audit slot grid')
    buckets = {(symbol, horizon): zero for symbol in symbols for horizon in horizons}
    reserved = buckets.copy()
    free = {symbol: number(snapshot['held_shares'][symbol]) for symbol in symbols}
    donors, pending = {}, set()
    for allocation in snapshot['ownership'].get('active_allocations', []):
        symbol, horizon = allocation['symbol'], allocation['horizon']
        if symbol not in free:
            continue
        key = symbol, horizon
        owned, held_reserved = number(allocation['owned_shares']), number(allocation.get('reserved_sell_shares', 0))
        buy_reserved = number(allocation.get('reserved_buy_shares', 0))
        require(key in buckets and zero <= held_reserved <= owned and buy_reserved >= zero, 'Invalid saved ownership')
        buckets[key] += owned - held_reserved
        reserved[key] += held_reserved
        free[symbol] -= owned
        if held_reserved or buy_reserved:
            pending.add(key)
        if owned:
            identity = allocation.get('allocation_id_sha256')
            require(isinstance(identity, str) and re.fullmatch('[0-9a-f]{64}', identity) is not None
                    and identity not in donors, 'Missing or duplicate saved native allocation hash')
            donors[identity] = {'symbol': symbol, 'horizon': horizon, 'initial_shares': float(owned),
                                'daily_cap': int(owned / 2), 'used': 0}
    unallocated_reserved = {}
    for symbol in symbols:
        owned_reserved = sum((v for key, v in reserved.items() if key[0] == symbol), zero)
        unallocated_reserved[symbol] = number(snapshot['pending_sell_shares'].get(symbol, 0)) - owned_reserved
        free[symbol] -= unallocated_reserved[symbol]
        require(free[symbol] >= zero and unallocated_reserved[symbol] >= zero, 'Initial holdings overdrawn')
    caps = {symbol: int(sum((number(d['initial_shares']) for d in donors.values()
                            if d['symbol'] == symbol and d['horizon'] != '1h'), zero) / 2) for symbol in symbols}
    used = {symbol: 0 for symbol in symbols}
    saved = ledger.get('cross_horizon_fallback', {})
    require(saved.get('symbol_daily_caps') == caps and set(saved.get('donors', {})) == set(donors),
            'Frozen fallback budgets or donor hash set differ from initial snapshot')
    policy = ledger['cross_horizon_fallback_policy']
    day = policy['effective_action_date'] if not len(rows) else str(rows.iloc[0]['action_date'])
    quota_by_id, slot_rows = {}, {}
    for row in rows.to_dict('records'):
        require(row.get('fallback_policy_version') == policy['policy_version']
                and row.get('fallback_symbol_daily_cap') == caps[row['symbol']], 'Trade row fallback binding differs')
        if not row['execution_eligible']:
            continue
        stamp = pd.Timestamp(row['target_window_start']).tz_convert('America/Los_Angeles')
        require(str(stamp.date()) == day and stamp.minute == stamp.second == stamp.microsecond == stamp.nanosecond == 0,
                'Fallback quota clock differs from action date')
        horizon, symbol = row['model_group'], row['symbol']
        if horizon == '1w':
            quota = 0
        else:
            key = stamp.hour, horizon
            require(key in slots, 'Fallback opportunity outside approved grid')
            index = slots.index(key)
            before = sum(weights[h] for _, h in slots[:index])
            quota = (before + weights[horizon]) * caps[symbol] // 24 - before * caps[symbol] // 24
            slot_rows.setdefault(symbol, []).append((key, quota))
        require(row.get('fallback_slot_quota') == quota, 'Saved slot quota differs from independent integer formula')
        quota_by_id[row['id']] = quota
    for symbol in symbols:
        present = slot_rows.get(symbol, [])
        require(len(present) == 18 and {key for key, _ in present} == set(slots)
                and sum(quota for _, quota in present) == caps[symbol], 'Incomplete, duplicate or rolling slot budget')
    by_id = rows.set_index('id').to_dict('index')
    seen, fallback_events, normal_sales = set(), [], 0
    before_clock = {}
    for event in ledger['events']:
        identity = event['forecast_id']
        require(identity in by_id and identity not in seen, 'Repeated or missing planning instruction')
        seen.add(identity)
        row = by_id[identity]
        key = event['symbol'], event['horizon']
        symbol, horizon = key
        quantity = number(event['quantity'])
        clock = event['timestamp']
        if clock not in before_clock:
            before_clock[clock] = {'free': free.copy(), 'buckets': buckets.copy()}
        require(row['execution_eligible'] and key == (row['symbol'], row['model_group'])
                and pd.Timestamp(clock) == pd.Timestamp(row['target_window_start'])
                and quantity > zero and quantity == int(quantity), 'Event changed forecast identity, clock or quantity')
        direction = stock_direction(row['calibrated_probability'])
        if event['action'] == 'BUY':
            require(event['reason'] == 'BULLISH_BUY' and direction == 'BULLISH'
                    and not event.get('cross_horizon_fallback'), 'Invalid bullish purchase')
            buckets[key] += quantity
        elif event['reason'] == 'BEARISH_SELL':
            require(event['action'] == 'SELL' and direction == 'BEARISH' and not event.get('cross_horizon_fallback'),
                    'Normal sale has wrong direction or fallback attribution')
            require(quantity <= free[symbol] + buckets[key], 'Normal sale takes another horizon or reserved shares')
            assigned = min(free[symbol], quantity)
            free[symbol] -= assigned
            buckets[key] -= quantity - assigned
            normal_sales += 1
        else:
            require(event['action'] == 'SELL' and event['reason'] == 'BEARISH_CROSS_HORIZON_FALLBACK'
                    and direction == 'BEARISH', 'Unexpected expiry or other sale')
            attribution = event['cross_horizon_fallback']
            donor_id = attribution.get('donor_allocation_id_sha256')
            require(donor_id in donors, 'Fallback donor is not an initial saved allocation hash')
            donor = donors[donor_id]
            donor_key = symbol, donor['horizon']
            require(donor['symbol'] == symbol and horizons.index(donor['horizon']) > horizons.index(horizon),
                    'Fallback takes a non-longer or different-symbol donor')
            before = before_clock[clock]
            require(before['free'][symbol] == 0 and before['buckets'][key] + reserved[key] == 0
                    and unallocated_reserved[symbol] == 0 and key not in pending,
                    'Fallback requires genuine pre-clock normal-route absence')
            same_clock = [r for r in by_id.values() if r['symbol'] == symbol and r['execution_eligible']
                          and pd.Timestamp(r['target_window_start']) == pd.Timestamp(clock)]
            require(not any(r['model_group'] == donor['horizon'] and stock_direction(r['calibrated_probability']) == 'BULLISH'
                            for r in same_clock) and donor_key not in pending, 'Fallback uses bullish or pending donor')
            require(not any(e['timestamp'] == clock and e['symbol'] == symbol and e['horizon'] == donor['horizon']
                            and e['reason'] == 'BEARISH_SELL' for e in ledger['events']), 'Fallback uses normal-sale donor')
            quota = quota_by_id[identity]
            require(quantity <= quota and used[symbol] + quantity <= caps[symbol]
                    and donor['used'] + quantity <= donor['daily_cap'] and quantity <= buckets[donor_key],
                    'Fallback exceeds slot, frozen symbol, donor, or available share ceiling')
            expected = {'policy_version': policy['policy_version'], 'action_date': day,
                        'trigger_forecast_id': identity, 'trigger_horizon': horizon, 'donor_horizon': donor['horizon'],
                        'donor_initial_shares': donor['initial_shares'], 'slot_quota': quota,
                        'symbol_daily_cap': caps[symbol], 'symbol_used_before': used[symbol],
                        'symbol_used_after': used[symbol] + int(quantity), 'donor_daily_cap': donor['daily_cap'],
                        'donor_used_before': donor['used'], 'donor_used_after': donor['used'] + int(quantity),
                        'quantity': int(quantity), 'basis': 'CONDITIONAL_PLANNING_FILLS'}
            require(all(attribution.get(k) == v for k, v in expected.items()), 'Fallback event attribution/cap use differs')
            require(same_number(number(attribution['donor_shares_before']) - quantity, attribution['donor_shares_after']),
                    'Donor share change fails conservation')
            used[symbol] += int(quantity)
            donor['used'] += int(quantity)
            buckets[donor_key] -= quantity
            fallback_events.append(identity)
    require(saved['symbol_used'] == used and saved['donors'] == donors, 'Final fallback cap use differs')
    ending, ending_reserved = {key: zero for key in buckets}, {key: zero for key in buckets}
    for lot in ledger['ending_allocations']:
        key = lot['symbol'], lot['horizon']
        require(key in ending, 'Unknown ending allocation horizon')
        ending[key] += number(lot['quantity'])
        ending_reserved[key] += number(lot['reserved'])
    require(ending == buckets and ending_reserved == reserved, 'Donor-attributed horizon share rollforward differs')
    for symbol in symbols:
        total = free[symbol] + unallocated_reserved[symbol] + sum(
            (buckets[key] + reserved[key] for key in buckets if key[0] == symbol), zero)
        require(same_number(total, ledger['ending_positions'][symbol]), 'Ending symbol ownership fails conservation')
    return {'policy': SIGNAL_DRIVEN_HOLDING_POLICY, 'scheduled_expiry_sales': 0,
            'horizon_ownership_rollforward': 'VERIFIED', 'fallback_policy_version': policy['policy_version'],
            'planning_donor_hashes': len(donors), 'independent_quota_rows': len(quota_by_id),
            'weighted_fallback_slots_per_symbol': 18, 'symbol_daily_caps': caps, 'symbol_used': used,
            'fallback_event_count': len(fallback_events), 'normal_sale_event_count': normal_sales,
            'basis': 'CONDITIONAL_PLANNING_FILLS_ONLY', 'unique_instruction_events': len(seen),
            'live_baseline_or_live_fills_verified': False,
            'model_assessment_separate_from_manual_instructions': True}
