"""Read-only end-to-end directional readiness and app adapter verification."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from app.ui.gameplan_data import load_gameplan
from app.ui.gameplan_stats_data import load_gameplan_stats
from ml.artifacts import file_checksum, verify_manifest
from ml.gameplan_deployment import assert_execution_gameplan, read_deployment
from ml.gameplan_promotion import validate_promoted_report
from ml.nightly_gameplan import read_gameplan_run
from ml.stock_trader.independent_signals import load_current_independent_gameplan_signals


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--gameplan-run', type=Path, required=True)
    parser.add_argument('--require-active', action='store_true')
    args = parser.parse_args()
    root = Path('C:/DATASTORE')
    out = Path(__file__).parent
    run = args.gameplan_run.resolve()
    publication = read_gameplan_run(root, run)
    forecasts = pd.read_parquet(run/'forecasts.parquet')
    reports = json.loads((run/'model-reports.json').read_text())
    checks = {}
    failures = {}
    for group in ('1h', '4h', '1d', '1w'):
        try:
            validate_promoted_report(reports[group])
            checks[f'{group}_assessment'] = True
        except ValueError as exc:
            checks[f'{group}_assessment'] = False
            failures[group] = str(exc)
    checks.update(
        exact_date=publication.receipt['action_date'] == '2026-09-18',
        YG_target=publication.manifest['configuration']['probability_target_contract'] == 'raw-price-direction-v1',
        all_forecasts_promoted=bool(forecasts.model_status.eq('PROMOTED').all()),
        exact_fitted_history=bool(forecasts.symbol_fitted_target_rows.gt(0).all()
            and forecasts.symbol_route_fitted_target_rows.gt(0).all()),
        complete_grid=len(forecasts) == 264 and forecasts.symbol.nunique() == 11
            and forecasts.groupby('symbol').size().eq(24).all(),
        valid_probabilities=bool(forecasts.calibrated_probability.between(0, 1).all()),
    )
    baseline = json.loads((out/'immutable-baseline.json').read_text())
    for name, evidence in baseline['runs'].items():
        checks[f'unchanged_{name}'] = all(
            file_checksum(Path(evidence['run'])/file) == checksum
            for file, checksum in evidence['files'].items())
    plan = load_gameplan(root, '2026-09-18')
    verify_manifest(plan.run_directory)
    trade_receipt = json.loads((plan.run_directory/'receipt.json').read_text())
    checks['app_exact_YG_source'] = trade_receipt['source_gameplan_run'] == run.relative_to(root).as_posix()
    checks['app_YG_name'] = plan.display_name == 'Yung Gameplan (YG)'
    checks['app_264_qualified_rows'] = len(plan.forecasts) == 264 and all(row.model_status == 'PROMOTED' for row in plan.forecasts)
    filter_counts = {}
    for symbol in (None, *plan.symbols):
        for horizon in ('all', '1h', '4h', '1d', '1w'):
            rows = plan.rows(horizon, symbol)
            expected = forecasts
            if symbol is not None:
                expected = expected[expected.symbol.eq(symbol)]
            if horizon != 'all':
                expected = expected[expected.model_group.eq(horizon)]
            filter_counts[f'{symbol or "all"}/{horizon}'] = len(rows)
            if len(rows) != len(expected):
                raise AssertionError(f'UI filter mismatch: {symbol}/{horizon}')
    checks['all_60_app_filters'] = len(filter_counts) == 60
    metrics = load_gameplan_stats(root, '2026-09-17').metrics()
    checks['prior_OG_stats_preserved'] = metrics.correct == 62 and metrics.scored == 164 and abs(metrics.brier-.24128772600598308) < 1e-12
    active_details = {}
    if args.require_active:
        deployment = read_deployment(root, '2026-09-18')
        checks['active_exact_YG'] = deployment['status'] == 'ACTIVE' and deployment['YG']['run_path'] == run.relative_to(root).as_posix()
        checks['all_directional_quality_required'] = deployment.get('require_all_directional_models_promoted') is True
        assert_execution_gameplan(root, run, action_date='2026-09-18')
        for name in ('OG', 'YG_first'):
            try:
                assert_execution_gameplan(root, Path(baseline['runs'][name]['run']), action_date='2026-09-18')
            except ValueError as exc:
                active_details[f'{name}_rejection'] = str(exc)
            else:
                raise AssertionError(f'Superseded {name} accepted')
        signals, _ = load_current_independent_gameplan_signals(root,
            as_of=pd.Timestamp('2026-09-18T11:01:00Z'), execution_ready_plan=True)
        checks['opening_instructions_exact_YG'] = len(signals) == 44 and all(s.source_fingerprint == run.name for s in signals.values())
        active_details['opening_instructions'] = len(signals)
    checks = {name: bool(passed) for name, passed in checks.items()}
    result = {
        'status': 'VERIFIED' if all(checks.values()) else 'NOT_READY',
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'run': str(run), 'readable_gameplan': str(plan.report_path),
        'checks': checks, 'model_failure_details': failures, 'filter_counts': filter_counts,
        'active_details': active_details, 'broker_calls': 0, 'orders_placed': 0, 'entry_slots_claimed': 0,
    }
    name = 'active-readiness.json' if args.require_active else 'candidate-readiness.json'
    (out/name).write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k != 'filter_counts'}, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
