"""Offline bounded output audit. No provider, broker or production writes."""
from __future__ import annotations
import hashlib
import argparse
import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path

import pandas as pd

REPO = Path('C:/dev/ducketz')
ROOT = Path('C:/DATASTORE')
OUT = Path(__file__).parent
RUN = ROOT / 'ml/overnight-runs/20260924T044352.904210Z'
ANCESTOR = ROOT / 'ml/overnight-runs/20260924T040733.615371Z'
sys.path.insert(0, str(REPO))
checks = {}

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def require(value, name):
    checks[name] = bool(value)
    if not value:
        raise AssertionError(name)

def same(a, b):
    if a is None or b is None or pd.isna(a) or pd.isna(b):
        return (a is None or pd.isna(a)) and (b is None or pd.isna(b))
    return math.isclose(float(a), float(b), rel_tol=1e-10, abs_tol=1e-7)

def equal_columns(before, after, fields=None):
    fields = fields or [c for c in before.columns if c != 'id']
    pd.testing.assert_frame_equal(before.set_index('id')[fields].sort_index(),
        after.set_index('id')[fields].sort_index(), check_dtype=False, check_exact=True)

def resolved(path):
    path = Path(path)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()

def input_bound(manifest, path):
    path = Path(path).resolve()
    candidates = [x for x in manifest['input_files'] if resolved(x['path']) == path]
    return len(candidates) == 1 and candidates[0]['checksum_sha256'] == digest(path)

def pointer(kind):
    current = read(ROOT / f'ml/{kind}-latest/run.json')['current']
    path = ROOT / current['run_path']
    receipt, manifest = read(path / 'receipt.json'), read(path / 'manifest.json')
    require(current['receipt_sha256'] == digest(path / 'receipt.json'), f'{kind}_pointer_receipt')
    require(receipt['manifest_sha256'] == digest(path / 'manifest.json'), f'{kind}_receipt_manifest')
    for name, meta in manifest['output_files'].items():
        target = path / name
        require(target.stat().st_size == meta['size'] and digest(target) == meta['checksum_sha256'],
                f'{kind}_output_{name}')
    require(receipt['status'] == 'COMPLETE' and receipt['orders_placed'] == 0
            and receipt['broker_orders_enabled'] is False, f'{kind}_complete_zero_orders')
    return path, receipt, manifest, current

def main():
    native = read(RUN / 'stage-report.json')
    require(native['status'] == 'COMPLETE', 'explicit_resumed_native_complete')
    ancestry = []
    cursor = RUN
    while cursor.resolve() != ANCESTOR.resolve():
        require(cursor.resolve() not in ancestry, 'acyclic_ancestry')
        ancestry.append(cursor.resolve())
        cursor = Path(read(cursor / 'stage-report.json')['resumed_from'])
    require((ROOT / 'ml/overnight-runs/20260924T044352.904210Z').resolve() in ancestry, 'explicit_ancestry')
    completed = set(native.get('completed_stages_from_previous_attempt', []))
    completed.update(x['stage'] for x in native['stages'] if x['status'] == 'COMPLETE')
    original_stage_order = read(ANCESTOR / 'stage-report.json')['stage_order']
    require(all(x['status'] == 'COMPLETE' for x in native['stages'])
            and completed == set(original_stage_order) and len(completed) == 8, 'all_eight_native_stages')
    deadline = pd.Timestamp(native['deadline_at'])
    pin = native['enrichment_gameplan']
    pub = ROOT / pin['run_path']
    require(digest(pub / 'receipt.json') == pin['receipt_sha256'], 'pinned_gameplan_receipt')
    forecasts = pd.read_parquet(pub / 'forecasts.parquet')
    symbols = sorted(forecasts.symbol.unique())
    action = pin['action_date']
    require(len(symbols) == 11 and action == '2026-09-24', 'current_universe_and_action')
    plan, receipt, manifest, _ = pointer('gameplan-trade-plan')
    report, snapshot = read(plan / 'report.json'), read(plan / 'account-snapshot.json')
    require(report['orders_placed'] == 0 and report['broker_orders_enabled'] is False, 'trade_report_zero_orders')
    ledger, prices = read(plan / 'direction-ledger.json'), read(plan / 'planning-price-path.json')
    refs = read(plan / 'planning-reference-completion.json')
    rows = pd.read_parquet(plan / 'trade-plan.parquet')
    text = (plan / 'Gameplan.md').read_text(encoding='utf-8')
    require(receipt['source_gameplan_run'] == pin['run_path'] and receipt['source_receipt_sha256'] == pin['receipt_sha256']
            and receipt['action_date'] == action, 'tradeplan_exact_source_pin')
    require(input_bound(manifest, pub / 'receipt.json'), 'tradeplan_manifest_source_pin')
    require(report['snapshot'] == snapshot and report['direction_based_projection'] == ledger, 'report_saved_evidence_equal')
    require(report.get('planning_override') is None, 'original_safe_snapshot_preserved')
    require(len(rows) == 264 and rows.groupby('symbol').size().eq(24).all() and set(rows.symbol) == set(symbols), 'trade_rows_264')
    equal_columns(forecasts, rows)
    checks['all_original_forecast_columns_preserved'] = True
    require(snapshot['status'] == 'OBSERVED' and snapshot['cash_status'] == 'CASH_ONLY_BOUNDED'
            and snapshot['ownership']['safe_for_planning'] is True and snapshot['orders_placed'] == 0
            and snapshot['orders_enabled'] is False and snapshot['broker_data_http_methods'] == ['GET'], 'safe_readonly_snapshot')
    require(set(snapshot['held_shares']) == set(snapshot['quotes']) == set(symbols), 'snapshot_all_symbols')
    require(pd.Timestamp(report['observed_at']) <= pd.Timestamp(snapshot['observed_at'])
            <= pd.Timestamp(report['completed_at']) < deadline, 'fresh_snapshot_before_deadline')
    require(pd.Timestamp(report['observed_at']) <= pd.Timestamp(prices['observed_at'])
            <= pd.Timestamp(report['completed_at']), 'fixed_price_observation_clock')
    balances = snapshot['balances']
    require(any(balances.get(k) is not None for k in ('cash_balance', 'settled_cash')), 'literal_cash_present')
    cash = float(max(Decimal(0), min(min(Decimal(str(v)) for v in balances.values() if v is not None)
        - Decimal(str(snapshot['reserved_cash'])), Decimal(str(snapshot['broker_available_cash']))))
        .quantize(Decimal('.01'), rounding=ROUND_FLOOR))
    require(same(cash, snapshot['available_cash']), 'literal_cash_independently_recomputed')
    points = list(prices['points'].values())
    require(len(points) == 154 and {(p['symbol'], p['action_date'], p['clock_local']) for p in points}
        == {(s, action, f'{h:02}:00') for s in symbols for h in range(4, 18)}, 'hourly_price_identities_154')
    require(prices['reference_completion'] == refs and prices['price_source_contract'] == 'xnas-itch-archive-v1', 'saved_reference_contract')
    require(prices['minimum_samples'] == 2 and prices['lookback_sessions'] == 120, 'saved_path_sample_policy')
    for p in points:
        if p['status'] == 'AVAILABLE':
            require(p['sample_count'] >= 2 and 0 < p['planned_price_low'] <= p['planned_price_mid'] <= p['planned_price_high'], 'available_price_ranges_valid')
            ref = refs['references'][p['symbol'] + '|' + action]
            require(all(p['reference_' + k] == ref[k] for k in ('observed_at', 'effective_at', 'is_synthetic', 'fill_count', 'gap_minutes', 'price', 'session')), 'point_reference_lineage')
        else:
            require(p['status'] in ('UNAVAILABLE_REFERENCE_PRICE', 'UNAVAILABLE_MINIMUM_SAMPLES'), 'explicit_price_unavailability')
    require(text.count('| Projected Trade Quantity |') == 11, 'readable_all_stock_tables')
    synthetic = [r for r in refs['references'].values() if r.get('is_synthetic')]
    require(refs['native_prices_modified'] is False and refs['model_training_prices_modified'] is False
            and refs['regular_session_prices_filled'] is False, 'synthetic_reference_scope_limited')
    bars = pd.read_parquet(plan / 'synthetic-reference-bars.parquet')
    # The saved bars are current reference completions; historical planning pairs are separate.
    current_bars = bars.loc[bars.action_date.eq(action)] if len(bars) else bars
    expected_bars = pd.DataFrame(refs['synthetic_bars'])
    if len(expected_bars):
        pd.testing.assert_frame_equal(bars[sorted(bars.columns)], expected_bars[sorted(expected_bars.columns)], check_dtype=False, check_exact=True)
        require(not bars.duplicated(['symbol', 'timestamp']).any(), 'synthetic_no_duplicate_symbol_minutes')
    require(len(current_bars) == sum(r['fill_count'] for r in synthetic), 'synthetic_bar_count')
    for ref in synthetic:
        frame = current_bars.loc[current_bars.symbol.eq(ref['symbol'])]
        expect = pd.date_range(ref['observed_at'], ref['effective_at'], freq='min', inclusive='left')
        require(list(pd.to_datetime(frame.timestamp, utc=True).sort_values()) == list(expect), 'synthetic_contiguous_minutes')
        require(frame.volume.eq(0).all() and frame.is_synthetic.eq(True).all()
                and frame.reason.eq('ASSUMED_NO_TRADES').all()
                and all(frame[c].eq(ref['price']).all() for c in ('open', 'high', 'low', 'close')), 'synthetic_zero_volume_fixed_price')
        require(frame.original_observed_at.eq(ref['observed_at']).all(), 'synthetic_original_time_preserved')
        coverage = ref['source_coverage']
        require(coverage['native_partition_verified'] and pd.Timestamp(coverage['start']) <= pd.Timestamp(ref['observed_at'])
                and pd.Timestamp(coverage['end']) >= pd.Timestamp(ref['effective_at']), 'synthetic_saved_source_coverage')
        from ml.gameplan_trade_review import _money, _pacific
        disclosure = f"| {ref['symbol']} | {_money(ref['price'])} | {_pacific(ref['observed_at'])} | {_pacific(ref['effective_at'])} | {ref['fill_count']} |"
        require(disclosure in text, 'synthetic_readable_disclosure')

    from ml.gameplan_trade_planning import _plan_working_price_rows
    from ml.gameplan_cash_ledger import project_direction_trades, UnavailablePlanningPricePath
    from ml.stock_trader.contracts import StockTraderPolicy
    policy = StockTraderPolicy(**report['sizing_policy'])
    capacity = _plan_working_price_rows(forecasts, snapshot, read(plan / 'price-bands.json'), prices, policy=policy)
    equal_columns(capacity, rows)
    checks['standalone_capacity_native_recomputed'] = True
    try:
        rebuilt_rows, rebuilt = project_direction_trades(capacity, snapshot, prices, policy=policy, signal_driven=True)
    except UnavailablePlanningPricePath as error:
        require(ledger['status'] == 'UNAVAILABLE_PRICE_REFERENCES' and ledger['unavailable_points'] == error.points
                and ledger['events'] == [] and ledger['hourly'] == [] and ledger['summary'] == {}
                and ledger['ending_positions'] == {}, 'unavailable_projection_honest')
    else:
        require(ledger == rebuilt, 'full_native_ledger_recomputed')
        equal_columns(rebuilt_rows, rows)
        require(text.count('| Projected Trade Quantity | Direction Based Trade Qty |') == 11, 'quantity_columns_adjacent')
        require('Holdings have no scheduled expiry sale.' in text and 'Forecast boundaries do not sell shares.' in text,
                'readable_saved_holding_policy')
        running = {k: cash for k in ('low', 'base', 'high')}
        held = dict(snapshot['held_shares'])
        require(ledger['starting_positions'] == held and len(ledger['hourly']) == 14, 'ledger_initial_positions_and_clocks')
        require([e['sequence'] for e in ledger['events']] == list(range(1, len(ledger['events']) + 1)), 'event_sequence_complete')
        seen = []
        for hour in ledger['hourly']:
            events = [e for e in ledger['events'] if e['timestamp'] == hour['timestamp']]
            require(hour['event_sequences'] == [e['sequence'] for e in events], 'hour_event_binding')
            seen.extend(hour['event_sequences'])
            for e in events:
                require(e['action'] in ('BUY', 'SELL') and int(e['quantity']) == e['quantity'] > 0, 'whole_positive_quantities')
                sign = 1 if e['action'] == 'SELL' else -1
                for label in running:
                    price_label = label if sign == 1 or label == 'base' else {'low':'high','high':'low'}[label]
                    change = sign * e['quantity'] * e['price_' + price_label]
                    require(same(e['cash_change_' + label], change) and same(e['cash_before_' + label], running[label]), 'cash_event_cost_and_before')
                    running[label] += change
                    require(same(e['cash_' + label], running[label]), 'cash_event_after')
                require(same(held[e['symbol']], e['shares_before']), 'share_event_before')
                held[e['symbol']] -= sign * e['quantity']
                require(held[e['symbol']] >= 0 and same(held[e['symbol']], e['shares_after']), 'share_event_after_nonnegative')
            require(all(same(hour['cash_' + k], running[k]) for k in running) and hour['held_shares'] == held, 'hour_entire_batch_conservation')
            sameclock = rows.loc[pd.to_datetime(rows.target_window_start, utc=True).eq(pd.Timestamp(hour['timestamp'])) & rows.execution_eligible.eq(True)]
            require(all(sameclock['projected_cash_after_' + k].map(lambda n: same(n, running[k])).all() for k in running), 'same_clock_forecast_posthour_cash')
        require(seen == [e['sequence'] for e in ledger['events']], 'every_event_applied_once')
        require(all(same(ledger['summary']['ending_cash_' + k], running[k]) for k in running)
                and ledger['ending_positions'] == held, 'ending_cash_shares_conserved')
        require(ledger['no_fill_baseline'] == {'cash': snapshot['available_cash'], 'held_shares': ledger['starting_positions']}, 'no_fill_baseline_preserved')
        # Independent lot accounting protects other horizons and pending reservations.
        from ml.stock_direction_policy import stock_direction
        buckets = {(s, h): Decimal(0) for s in symbols for h in ('1h', '4h', '1d', '1w')}
        reserved = buckets.copy()
        free = {s: Decimal(str(snapshot['held_shares'][s])) for s in symbols}
        for allocation in snapshot['ownership'].get('active_allocations', []):
            s = allocation['symbol']
            if s not in free:
                continue
            key = s, allocation['horizon']
            owned, held_reserved = Decimal(str(allocation['owned_shares'])), Decimal(str(allocation.get('reserved_sell_shares', 0)))
            require(key in buckets and 0 <= held_reserved <= owned, 'valid_initial_horizon_ownership')
            buckets[key] += owned - held_reserved
            reserved[key] += held_reserved
            free[s] -= owned
        unallocated_reserved = {}
        for s in symbols:
            unallocated_reserved[s] = Decimal(str(snapshot['pending_sell_shares'].get(s, 0))) - sum(v for key, v in reserved.items() if key[0] == s)
            free[s] -= unallocated_reserved[s]
            require(free[s] >= 0 and unallocated_reserved[s] >= 0, 'pending_shares_protected')
        byid, seen_ids = rows.set_index('id').to_dict('index'), set()
        for event in ledger['events']:
            identity = event['forecast_id']
            require(identity in byid and identity not in seen_ids, 'unique_saved_instruction_event')
            seen_ids.add(identity)
            row, key = byid[identity], (event['symbol'], event['horizon'])
            quantity = Decimal(str(event['quantity']))
            require(row['execution_eligible'] and key == (row['symbol'], row['model_group'])
                    and pd.Timestamp(event['timestamp']) == pd.Timestamp(row['target_window_start']), 'event_frozen_instruction_binding')
            direction = stock_direction(row['calibrated_probability'])
            if event['action'] == 'BUY':
                require(event['reason'] == 'BULLISH_BUY' and direction == 'BULLISH', 'only_bullish_buys')
                buckets[key] += quantity
            else:
                require(event['reason'] == 'BEARISH_SELL' and direction == 'BEARISH', 'only_bearish_sales_no_expiry')
                require(quantity <= free[key[0]] + buckets[key], 'no_cross_horizon_or_reserved_share_sale')
                assigned = min(free[key[0]], quantity)
                free[key[0]] -= assigned
                buckets[key] -= quantity - assigned
        ending, ending_reserved = {k: Decimal(0) for k in buckets}, {k: Decimal(0) for k in buckets}
        for lot in ledger['ending_allocations']:
            key = lot['symbol'], lot['horizon']
            ending[key] += Decimal(str(lot['quantity']))
            ending_reserved[key] += Decimal(str(lot['reserved']))
        require(ending == buckets and ending_reserved == reserved, 'horizon_ownership_and_reservations_rollforward')
        require(all(same(free[s] + unallocated_reserved[s] + sum(buckets[k] + reserved[k] for k in buckets if k[0] == s),
                         ledger['ending_positions'][s]) for s in symbols), 'horizon_allocations_match_ending_holdings')

    baseline = read(OUT / 'prior-session-output-baseline.json')
    require(all(Path(p).stat().st_size == meta['size'] and digest(p) == meta['sha256'] for p, meta in baseline['files'].items()), 'prior_files_unchanged_since_pre_tail_baseline')
    actual, ar, am, ac = pointer('gameplan-actuals-review')
    actual_report = read(actual / 'report.json')
    require(actual_report['orders_placed'] == 0 and actual_report['broker_orders_enabled'] is False, 'actuals_report_zero_orders')
    actual_readable = (actual / 'Gameplan-results.md').read_text(encoding='utf-8')
    require('not broker fills or trading P/L' in actual_readable and 'not broker fills or realized profit' in actual_readable,
            'actuals_readable_market_observations_not_fills')
    prior_pub, prior_plan = Path(baseline['prior_gameplan']), Path(baseline['prior_trade_plan'])
    require(actual_report['action_date'] == '2026-09-23' and actual_report['successor_action_date'] == action
            and actual_report['successor_gameplan_run'] == pin['run_path']
            and resolved(actual_report['source_gameplan_run']) == prior_pub.resolve()
            and Path(actual_report['source_trade_plan_path']).resolve() == prior_plan.resolve(), 'actuals_exact_original_and_successor')
    require(pd.Timestamp(receipt['completed_at']) <= pd.Timestamp(actual_report['reviewed_at']) < deadline
            and pd.Timestamp(actual_report['deadline_at']) == deadline, 'actuals_after_successor_before_deadline')
    require(input_bound(am, prior_pub / 'receipt.json') and input_bound(am, prior_pub / 'forecasts.parquet')
            and input_bound(am, prior_plan / 'receipt.json') and input_bound(am, prior_plan / 'planning-price-path.json')
            and input_bound(am, pub / 'receipt.json') and input_bound(am, plan / 'receipt.json'), 'actuals_all_frozen_source_bindings')
    dated = ROOT / 'ml/gameplan-actuals-review-by-date/2026-09-23'
    require(read(dated / 'run.json')['current'] == ac and digest(dated / 'Gameplan-results.md') == digest(actual / 'Gameplan-results.md')
            and Path(report['previous_session_results_path']).resolve() == (dated / 'Gameplan-results.md').resolve(), 'actuals_dated_reader_and_successor_link')
    fr, pr = pd.read_parquet(actual / 'forecast-results.parquet'), pd.read_parquet(actual / 'price-results.parquet')
    equal_columns(pd.read_parquet(prior_pub / 'forecasts.parquet'), fr)
    equal_columns(pd.read_parquet(prior_plan / 'trade-plan.parquet'), fr)
    require(len(fr) == 264 and len(pr) == 154 and fr.id.is_unique, 'actuals_all_frozen_rows')
    checks['all_original_actuals_forecast_trade_columns_preserved'] = True
    pp = read(prior_plan / 'planning-price-path.json')
    bypoint = {(p['symbol'], p['clock_local']): p for p in pp['points'].values()}
    for r in pr.to_dict('records'):
        p = bypoint[(r['symbol'], r['clock_local'])]
        require(all(same(r['planned_price_' + k], p['planned_price_' + k]) for k in ('low', 'mid', 'high')), 'frozen_price_estimates_preserved')
        if r['comparison_status'] == 'COMPARED':
            require(same(r['price_error'], r['actual_price'] - r['planned_price_mid'])
                and same(r['price_error_fraction'], r['price_error'] / r['planned_price_mid'])
                and bool(r['in_planned_range']) == (r['planned_price_low'] <= r['actual_price'] <= r['planned_price_high']), 'price_comparison_arithmetic')
        if r['actual_status'] == 'OBSERVED':
            require(0 <= r['actual_gap_seconds'] <= 300 and r['actual_source_coverage'] == 'VERIFIED_COMPLETE', 'accepted_clock_native_boundary')
    for r in fr.to_dict('records'):
        for side in ('start', 'end'):
            if r['actual_' + side + '_status'] == 'OBSERVED':
                require(0 <= r['actual_' + side + '_gap_seconds'] <= 300 and r['actual_' + side + '_source_coverage'] == 'VERIFIED_COMPLETE', 'accepted_forecast_native_boundary')
        if r['actuals_status'] == 'EVALUATED':
            raw = r['actual_end_price'] / r['actual_start_price'] - 1
            require(same(raw, r['actual_return']), 'raw_actual_return_arithmetic')
            if r['direction'] in ('BULLISH', 'BEARISH'):
                require(bool(r['direction_correct']) == (raw > 0 if r['direction'] == 'BULLISH' else raw < 0), 'direction_outcome_arithmetic')
        if r['actuals_status'] != 'EVALUATED' or r['direction'] not in ('BULLISH', 'BEARISH'):
            require(pd.isna(r['direction_correct']), 'unscored_neutral_missing_future')
    missing = fr.loc[fr.actuals_status.ne('EVALUATED')]
    calls = fr.direction_correct.dropna()
    result = {'reviewed_at': datetime.now(timezone.utc).isoformat(), 'scope': 'OFFLINE_SAVED_OUTPUTS_AND_PURE_CASH_SHARE_RECOMPUTATION_NO_PROVIDER_BROKER_OR_RAW_ARCHIVE_RELOAD',
        'status': 'PASS', 'native_run': str(RUN), 'original_run': str(ANCESTOR), 'checks': checks,
        'tradeplan': {'path': str(plan), 'readable': str(plan / 'Gameplan.md'), 'action_date': action, 'rows': len(rows),
            'price_points': len(points), 'price_status_counts': dict(Counter(p['status'] for p in points)),
            'fixed_price_observed_at': prices['observed_at'], 'snapshot_observed_at': snapshot['observed_at'],
            'literal_available_cash': cash, 'reserved_cash': snapshot['reserved_cash'], 'working_orders': snapshot['working_order_count'],
            'held_shares': snapshot['held_shares'], 'projection_status': ledger['status'], 'summary': ledger['summary'],
            'no_fill_baseline': ledger.get('no_fill_baseline'), 'ending_positions': ledger.get('ending_positions'),
            'synthetic_anchors': synthetic, 'synthetic_rows': len(current_bars),
            'policies': {'direction': report['direction_policy_version'], 'holding': ledger.get('holding_policy'),
                'reference': refs['contract_version'], 'maximum_reference_gap_minutes': refs['max_gap_minutes'], 'path': prices['contract_version']}},
        'actuals': {'path': str(actual), 'readable': str(actual / 'Gameplan-results.md'), 'reviewed_action_date': '2026-09-23',
            'forecast_rows': len(fr), 'price_rows': len(pr), 'forecast_status': fr.actuals_status.value_counts().to_dict(),
            'price_status': pr.comparison_status.value_counts().to_dict(), 'direction_scored': len(calls),
            'direction_correct': int(calls.sum()), 'direction_accuracy': float(calls.mean()) if len(calls) else None,
            'missing_by_symbol_status': missing.groupby(['symbol','actuals_status']).size().to_dict(),
            'price_missing_by_symbol': pr.loc[pr.comparison_status.ne('COMPARED')].groupby('symbol').size().to_dict(),
            'original_estimates_and_forecasts_unchanged': True, 'raw_archive_reverification': 'Separate full-verification agent; not duplicated here'}}
    result['actuals']['missing_by_symbol_status'] = {f'{s}:{status}': int(n) for (s,status),n in result['actuals']['missing_by_symbol_status'].items()}
    (OUT / 'output-review.json').write_text(json.dumps(result, indent=2, default=str) + '\n', encoding='utf-8')
    lines = [f"# Output review: {result['status']}", '', f"Read-only review at {result['reviewed_at']} of resumed native {RUN.name}.",
        '', f"Trade plan: {plan}. All 264 original forecasts are preserved, with 24 rows per stock and 154 hourly price identities.",
        f"Price availability: {result['tradeplan']['price_status_counts']}. Projection status: {ledger['status']}.",
        f"Literal available starting cash ${cash:,.2f}; pending reserved cash ${snapshot['reserved_cash']:,.2f}; working orders {snapshot['working_order_count']}.",
        "Saved capacity and the entire shared-cash ledger were reconstructed with pure native functions. Event costs, chronological cash/shares, whole quantities, hourly balances, ending totals and the no-fill baseline were independently checked.",
        f"Saved policies: {result['tradeplan']['policies']}.", '', 'Current synthetic reference anchors (planning only, ASSUMED_NO_TRADES):']
    lines += [f"- {r['symbol']}: ${r['price']:,.2f}; observed {r['observed_at']}, effective {r['effective_at']}; {r['fill_count']} zero-volume minutes; disclosed in readable plan." for r in synthetic]
    lines += ['', f"Actuals review: {actual}. The original Sep23 preopening Gameplan, trade plan and hourly estimates match both pre-tail baseline hashes and all saved result columns.",
        f"264 forecasts: {result['actuals']['forecast_status']}. 154 same-clock prices: {result['actuals']['price_status']}.",
        f"Directional calls: {len(calls)} scored, {int(calls.sum())} correct; neutral, missing and future outcomes excluded.",
        "Accepted observations retain the five-minute native boundary and verified source-coverage status. The separate full audit verifies raw archive observations; no raw archive reloading was duplicated here.",
        "Market observations are not broker fills or realized P/L. This audit submitted no orders, fetched no data and changed no production artifacts."]
    (OUT / 'output-review.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('reviewed_at','status','native_run')}))
    print(json.dumps({'checks': len(checks), 'tradeplan': str(plan), 'actuals': str(actual), 'forecast_status': result['actuals']['forecast_status'], 'price_status': result['actuals']['price_status']}))

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', type=Path, default=RUN)
    RUN = parser.parse_args().run
    main()
