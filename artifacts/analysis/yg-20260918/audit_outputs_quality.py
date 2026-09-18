"""Read-only audit of YG saved outputs; writes only this audit directory."""
import argparse
import hashlib
from types import SimpleNamespace
import json
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd


ROOT = Path('C:/DATASTORE')
REPO = Path('C:/dev/ducketz')
OUT = Path(__file__).resolve().parent / 'quality-followup'
NATIVE = None
YG = None
BASELINE = OUT / 'immutable-baseline.json'
OG = ROOT / 'ml/nightly-gameplan-runs/20260918T054532.489998Z'
OLD_TRADE = ROOT / 'ml/gameplan-trade-plan-runs/20260918T054811.145288Z'
OG_RECEIPT_HASH = 'ba53f5f4b62443c426190da8cad96cf7e18644ee8c3f278b0fa5092c93b3de97'
CHECKS = []


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(name, condition):
    CHECKS.append({'check': name, 'passed': bool(condition)})
    if not condition:
        raise AssertionError(name)


def number(value):
    return Decimal(str(value))


def equal(a, b):
    return abs(number(a) - number(b)) <= Decimal('0.011')


def outputs(directory):
    manifest = read(directory / 'manifest.json')
    for name, info in manifest['output_files'].items():
        path = directory / name
        check(f'{directory.name}/{name}: output hash and size',
              path.is_file() and path.stat().st_size == info['size']
              and sha(path) == info['checksum_sha256'])
    return manifest


def main():
    pointer = read(ROOT / 'ml/gameplan-trade-plan-latest/run.json')['current']
    trade_dir = ROOT / pointer['run_path']
    tr = read(trade_dir / 'receipt.json')
    if tr.get('source_gameplan_run') != YG.relative_to(ROOT).as_posix():
        raise SystemExit('PENDING: matching YG trade-plan pointer has not been published')
    if not (NATIVE / 'receipt.json').exists():
        raise SystemExit('PENDING: native three-stage terminal receipt has not been published')
    native = read(NATIVE / 'stage-report.json')
    terminal = read(NATIVE / 'receipt.json')
    yr = read(YG / 'receipt.json')
    ym = outputs(YG)
    baseline = read(BASELINE)
    required_baselines = {'OG', 'YG_first', 'YG_first_trade_plan', 'YG_first_deployment'}
    check('all four immutable baselines present', required_baselines.issubset(baseline['runs']))
    check('quality revision is a new YG', YG != Path(baseline['runs']['YG_first']['run']).resolve())
    baseline_files = 0
    for label, saved in baseline['runs'].items():
        for name, expected in saved['files'].items():
            saved_path = Path(saved['run']) / name
            check(f'preserved {label}/{name}', saved_path.is_file() and sha(saved_path) == expected)
            baseline_files += 1
    from ml.gameplan_deployment import _require_all_directional_models_promoted
    _require_all_directional_models_promoted(SimpleNamespace(run_directory=YG, manifest=ym, receipt=yr))
    check('all four directional models and all264 exact-supported forecasts promoted', True)
    check('native receipt binds final report', terminal['stage_report_checksum_sha256'] == sha(NATIVE/'stage-report.json')
          and terminal['stage_report_size'] == (NATIVE/'stage-report.json').stat().st_size)
    for name, evidence in terminal['logs'].items():
        check(f'native log {name}', Path(name).name == name and sha(NATIVE/name) == evidence['checksum_sha256']
              and (NATIVE/name).stat().st_size == evidence['size'])
    tm = outputs(trade_dir)
    report = read(trade_dir / 'report.json')
    ledger = read(trade_dir / 'direction-ledger.json')
    price_path = read(trade_dir / 'planning-price-path.json')
    completion = read(trade_dir / 'planning-reference-completion.json')
    snapshot = read(trade_dir / 'account-snapshot.json')
    forecasts = pd.read_parquet(YG / 'forecasts.parquet')
    intents = pd.read_parquet(YG / 'option-strategy-intents.parquet')
    rows = pd.read_parquet(trade_dir / 'trade-plan.parquet')
    synthetic = pd.read_parquet(trade_dir / 'synthetic-reference-bars.parquet')
    configured = sorted({line.split('#', 1)[0].strip() for line in
                         (REPO / 'datafetching/watchlist.txt').read_text().splitlines()
                         if line.split('#', 1)[0].strip()})
    check('three-stage native endpoint preserved', native['stage_order'] == [
        'gameplan_publication', 'stock_enrichment_training', 'gameplan_trade_planning'])
    check('all three native stages complete', native['status'] == terminal['status'] == 'COMPLETE'
          and len(native['stages']) == 3 and all(s['status'] == 'COMPLETE' and s['exit_code'] == 0 for s in native['stages']))
    check('original deadline and no exception', native['deadline_at'] == native['effective_deadline_at'] == '2026-09-18T11:00:00+00:00'
          and native.get('deadline_exception') is None)
    check('published before original deadline', pd.Timestamp(tr['completed_at']) < pd.Timestamp(native['deadline_at']))
    check('YG raw-price identity', yr['gameplan_variant'] == 'YG' and yr['probability_target_contract'] == 'raw-price-direction-v1'
          and ym['configuration']['gameplan_variant'] == 'YG')
    check('YG receipt binds manifest', sha(YG / 'manifest.json') == yr['manifest_checksum_sha256'])
    check('trade receipt binds manifest', sha(trade_dir / 'manifest.json') == tr['manifest_sha256'])
    check('pointer and native source bind YG', pointer['receipt_sha256'] == sha(trade_dir / 'receipt.json')
          and tr['source_receipt_sha256'] == sha(YG / 'receipt.json')
          and native['enrichment_gameplan']['receipt_sha256'] == sha(YG / 'receipt.json')
          and tm['configuration']['source_gameplan_run'] == YG.relative_to(ROOT).as_posix())
    check('action date Sep18', yr['action_date'] == tr['action_date'] == report['action_date'] == '2026-09-18')
    for name, frame in [('forecasts', forecasts), ('intents', intents), ('augmented rows', rows)]:
        check(f'{name}: 264 rows and exact eleven-symbol universe', len(frame) == 264
              and sorted(frame.symbol.unique()) == configured and len(configured) == 11
              and frame.groupby('symbol').size().eq(24).all())
    check('forecast identities unchanged in augmented rows', forecasts.id.is_unique and rows.id.is_unique
          and set(forecasts.id) == set(rows.id))
    source_columns = list(forecasts.columns)
    pd.testing.assert_frame_equal(forecasts.sort_values('id').reset_index(drop=True),
                                  rows[source_columns].sort_values('id').reset_index(drop=True), check_dtype=False)
    check('all frozen forecast columns retained exactly', True)
    check('all option intents are stock-only placeholders', intents.plan_status.eq('NO_TRADE_STOCK_ONLY').all()
          and not intents.broker_orders_enabled.any()
          and intents[['legs_json', 'candidate_key', 'strategy_source_run']].isna().all().all())
    points = price_path['points']
    check('154 available hourly price points', len(points) == 154 and all(p['status'] == 'AVAILABLE' for p in points.values()))
    for symbol in configured:
        check(f'{symbol}: fourteen hourly clocks', sorted(p['clock_local'] for p in points.values() if p['symbol'] == symbol)
              == [f'{hour:02d}:00' for hour in range(4, 18)])
    check('ledger projection complete', ledger['status'] == 'COMPLETE')
    starting = ledger['starting_positions']
    positions = {k: number(v) for k, v in starting.items()}
    cash = {band: number(ledger['summary']['starting_cash']) for band in ('low', 'base', 'high')}
    check('no-fill baseline and snapshot preserved', ledger['no_fill_baseline']['held_shares'] == snapshot['held_shares']
          and starting == snapshot['held_shares'] and equal(ledger['no_fill_baseline']['cash'], snapshot['available_cash'])
          and equal(ledger['summary']['starting_cash'], snapshot['available_cash']))
    events = ledger['events']
    event_snapshots = {}
    for sequence, event in enumerate(events, 1):
        symbol = event['symbol']
        quantity = number(event['quantity'])
        sign = -1 if event['action'] == 'BUY' else 1
        check(f'event {sequence}: identity and nonnegative whole shares', event['sequence'] == sequence
              and event['action'] in ('BUY', 'SELL') and quantity > 0 and quantity == int(quantity))
        check(f'event {sequence}: shares before', equal(event['shares_before'], positions[symbol]))
        for band in ('low', 'base', 'high'):
            price_band = {'low': 'high', 'base': 'base', 'high': 'low'}[band] if sign == -1 else band
            change = sign * quantity * number(event['price_' + price_band])
            check(f'event {sequence}: {band} cash conservation', equal(event['cash_before_' + band], cash[band])
                  and equal(event['cash_change_' + band], change)
                  and equal(event['cash_' + band], cash[band] + change))
            cash[band] = number(event['cash_' + band])
        positions[symbol] -= sign * quantity
        check(f'event {sequence}: shares after', positions[symbol] >= 0 and equal(event['shares_after'], positions[symbol]))
        event_snapshots[event['timestamp']] = (cash.copy(), positions.copy())
    check('ending holdings conserve all events', all(equal(v, ledger['ending_positions'][s]) for s, v in positions.items()))
    check('ending cash conserves all events', all(equal(v, ledger['summary']['ending_cash_' + b]) for b, v in cash.items()))
    running_cash = {b: number(ledger['summary']['starting_cash']) for b in cash}
    running_positions = {s: number(v) for s, v in starting.items()}
    for hour in ledger['hourly']:
        if hour['timestamp'] in event_snapshots:
            running_cash, running_positions = event_snapshots[hour['timestamp']]
        check(f"hour {hour['timestamp']}: shared cash and shares", all(equal(hour['cash_' + b], v) for b, v in running_cash.items())
              and all(equal(hour['held_shares'][s], v) for s, v in running_positions.items()))
    check('fourteen portfolio clocks', len(ledger['hourly']) == 14)
    hourly_by_clock = {pd.Timestamp(h['timestamp']).tz_convert('America/Los_Angeles').strftime('%H:%M'): h
                       for h in ledger['hourly']}
    eligible_rows = rows.loc[~rows.direction_based_action.eq('CONTEXT')]
    for row in eligible_rows.to_dict('records'):
        hour = hourly_by_clock[row['action_anchor_local']]
        check(f"forecast {row['id']}: post-entire-hour balances", all(
            equal(row['projected_cash_after_' + band], hour['cash_' + band]) for band in ('low', 'base', 'high'))
            and equal(row['projected_shares_after'], hour['held_shares'][row['symbol']]))
    check('55 non-entry context rows omit projection quantities and balances',
          len(rows.loc[rows.direction_based_action.eq('CONTEXT')]) == 55
          and rows.loc[rows.direction_based_action.eq('CONTEXT'), [
              'direction_based_trade_quantity', 'projected_cash_after_low', 'projected_cash_after_base',
              'projected_cash_after_high', 'projected_shares_after']].isna().all().all())
    og_receipt = read(OG / 'receipt.json')
    check('original OG receipt unchanged', sha(OG / 'receipt.json') == OG_RECEIPT_HASH)
    check('original OG manifest unchanged', sha(OG / 'manifest.json') == og_receipt['manifest_checksum_sha256'])
    old_completion = read(OLD_TRADE / 'planning-reference-completion.json')
    fields = ['symbol', 'session', 'status', 'observed_at', 'effective_at', 'price', 'gap_minutes', 'is_synthetic', 'fill_count']
    check('all original current price observations preserved', {
        key: {f: row[f] for f in fields} for key, row in completion['references'].items()} == {
        key: {f: row[f] for f in fields} for key, row in old_completion['references'].items()})
    pd.testing.assert_frame_equal(synthetic, pd.read_parquet(OLD_TRADE / 'synthetic-reference-bars.parquet'))
    check('same seven disclosed synthetic CROX bars', len(synthetic) == 7 and synthetic.symbol.eq('CROX').all()
          and synthetic.volume.eq(0).all() and synthetic.is_synthetic.all())
    check('native prices and training unmodified', not completion['native_prices_modified'] and not completion['model_training_prices_modified'])
    check('all receipts zero orders', all(payload['orders_placed'] == 0 and not payload['broker_orders_enabled']
          for payload in [native, terminal, yr, tr]))
    check('snapshot read-only zero orders', snapshot['orders_placed'] == 0 and not snapshot['orders_enabled'])
    summary = {
        'reviewed_at': datetime.now(timezone.utc).isoformat(), 'status': 'PASS',
        'scope': 'Read-only saved-output audit. No provider/broker calls, fitting, trading, claim or publication.',
        'native_run': str(NATIVE), 'gameplan': str(YG), 'trade_plan': str(trade_dir),
        'original_deadline': native['deadline_at'], 'stage_order': native['stage_order'],
        'publication_identity': {k: yr[k] for k in ['gameplan_variant', 'probability_target_contract', 'action_date', 'published_at']},
        'symbols': configured, 'forecast_rows': len(forecasts), 'intent_rows': len(intents),
        'augmented_rows': len(rows), 'price_points': len(points),
        'model_status_counts': forecasts.model_status.value_counts().to_dict(),
        'model_review_scope': 'Status counts only; separate model-review agent audits fitting/inference.',
        'direction_policy': {k: report.get(k) for k in ['direction_policy_version', 'direction_up_threshold', 'direction_down_threshold']},
        'holding_policy': ledger['holding_policy'], 'cash_summary': ledger['summary'],
        'event_reasons': dict(Counter(e['reason'] for e in events)),
        'ending_positions': ledger['ending_positions'], 'no_fill_baseline': ledger['no_fill_baseline'],
        'snapshot_at': snapshot['observed_at'], 'snapshot_working_orders': snapshot['working_order_count'],
        'snapshot_reserved_cash': snapshot['reserved_cash'],
        'synthetic_anchors': [r for r in completion['references'].values() if r['is_synthetic']],
        'original_receipt_sha256': sha(OG / 'receipt.json'), 'original_manifest_sha256': sha(OG / 'manifest.json'),
        'all_checks_passed': True, 'checks': CHECKS, 'orders_placed': 0,
        'immutable_baseline': str(BASELINE), 'immutable_baseline_files_checked': baseline_files,
    }
    (OUT / 'output-review.json').write_text(json.dumps(summary, indent=2, default=str) + '\n', encoding='utf-8')
    s = ledger['summary']
    lines = [
        '# September 18 YG quality-revision output audit', '',
        f"Reviewed {summary['reviewed_at']}. All {len(CHECKS)} bounded saved-output checks passed.", '',
        f"The native three-stage publication, enrichment and trade-planning run is COMPLETE, retaining the original September 18 04:00 Pacific deadline (11:00 UTC) without an exception. The [YG Gameplan]({(trade_dir / 'Gameplan.md').as_posix()}) binds publication `{YG.name}`, variant YG and `raw-price-direction-v1`.", '',
        'The same eleven configured stocks have 264 forecasts, 264 stock-only intents and 264 augmented trade-plan rows. Every original forecast column is retained exactly. All 154 hourly price points are AVAILABLE. Output hashes, receipt/manifest links, source bindings and current pointers passed.', '',
        f"The conditional scenario has {len(events)} events ({s['buy_events']} buys, {s['sell_events']} bearish sales). Starting cash is ${s['starting_cash']:,.2f}; ending cash is ${s['ending_cash_low']:,.2f}–${s['ending_cash_high']:,.2f} (base ${s['ending_cash_base']:,.2f}). Every event's before/change/after cash and shares, all fourteen hourly balances and ending balances conserve. Holding policy: `{ledger['holding_policy']}`.", '',
        'Ending conditional shares: ' + ', '.join(f'{k} {v:g}' for k, v in ledger['ending_positions'].items()) + '.', '',
        f"The no-fill baseline remains ${ledger['no_fill_baseline']['cash']:,.2f} and the snapshot holdings. Snapshot time: {snapshot['observed_at']}; working orders: {snapshot['working_order_count']}; reserved cash: ${snapshot['reserved_cash']:,.2f}. Projected fills and sale proceeds remain conditional; these are not broker executions.", '',
        'Original price observations match the earlier September 18 review. CROX alone retains the explicit $123.19 close observed September 17 at 16:53 Pacific, carried seven minutes through 17:00. The seven synthetic bars are identical, zero volume, and disclosed as assumptions within verified same-session coverage. Native prices and training prices remain unmodified.', '',
        f"The OG receipt and manifest are unchanged (receipt SHA-256 `{OG_RECEIPT_HASH}`). OG remains preserved for evaluation. This explicitly narrower run ends after trade planning; it does not add or rerun the prior-session actuals review.", '',
        f"All four directional model gates and all 264 forecast promotion/exact-route-support checks pass. Frozen model statuses are {json.dumps(summary['model_status_counts'], sort_keys=True)}. Optional learned enrichment qualification remains separate. Current direction policy is {summary['direction_policy']['direction_policy_version']}. {baseline_files} immutable baseline file hashes match OG, first YG, first YG trade plan and first ACTIVE deployment.", '',
        'All native, publication and trade-plan receipts report zero orders. No provider calls, broker fetches, trading operations, training, claims or production writes were made by this audit.', '',
        f"Structured checks: [output-review.json]({(OUT / 'output-review.json').as_posix()}).",
    ]
    (OUT / 'output-review.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(json.dumps({k: summary[k] for k in ['status', 'trade_plan', 'forecast_rows', 'intent_rows', 'augmented_rows', 'price_points', 'cash_summary', 'model_status_counts']}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-run', type=Path, required=True)
    parser.add_argument('--gameplan-run', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, default=BASELINE)
    args = parser.parse_args()
    NATIVE, YG, BASELINE = args.native_run.resolve(), args.gameplan_run.resolve(), args.baseline.resolve()
    if NATIVE.parent != (ROOT/'ml/overnight-runs').resolve() or YG.parent != (ROOT/'ml/nightly-gameplan-runs').resolve():
        parser.error('Run paths must be exact immutable native/Gameplan directories')
    if YG == Path('C:/DATASTORE/ml/nightly-gameplan-runs/20260918T072555.813034Z').resolve():
        parser.error('The first YG is preserved evidence, not a quality revision')
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        main()
    except Exception as exc:
        (OUT / 'output-review-failure.json').write_text(json.dumps({
            'reviewed_at': datetime.now(timezone.utc).isoformat(), 'error': str(exc), 'checks': CHECKS,
        }, indent=2) + '\n', encoding='utf-8')
        raise
