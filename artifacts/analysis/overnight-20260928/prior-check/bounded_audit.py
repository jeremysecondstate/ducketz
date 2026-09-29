"""Read-only bounded publication recheck; writes only this audit's evidence."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

OUT = Path(__file__).resolve().parent
STORE = Path('C:/DATASTORE')
REPO = Path('C:/dev/ducketz')
checks = []


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def check(name, valid, **evidence):
    checks.append({'check': name, 'pass': bool(valid), **evidence})


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def bound(path, expected, size=None, label='file_binding'):
    path = Path(path)
    actual = digest(path) if path.is_file() else None
    actual_size = path.stat().st_size if path.is_file() else None
    check(label, actual == expected.lower() and (size is None or actual_size == size),
          path=str(path), expected_sha256=expected, actual_sha256=actual,
          expected_size=size, actual_size=actual_size)


prior = read(REPO/'artifacts/analysis/overnight-20260927/noop-verification.json')
for entry in prior['pointers_watchlist_schedule']:
    if entry['path'].endswith('automation.toml'):
        continue
    bound(entry['path'], entry['sha256'], label='unchanged_since_previous_wake')

audit = read(REPO/'artifacts/analysis/overnight-20260926/final-verification.json')
check('previous_full_audit_complete', audit['status'] == 'VERIFIED_WITH_COVERAGE_NOTES'
      and audit['errors'] == [] and audit['all_eight_native_stages_complete']
      and audit['all_four_audits_exit_zero'])
for entry in audit['evidence'].values():
    bound(entry['path'], entry['sha256'], label='previous_full_audit_evidence')

native = STORE/'ml/overnight-runs/20260926T040723.834511Z'
receipt = read(native/'receipt.json')
bound(native/'stage-report.json', receipt['stage_report_checksum_sha256'],
      receipt['stage_report_size'], label='native_stage_report')
for name, binding in receipt['logs'].items():
    bound(native/name, binding['checksum_sha256'], binding['size'], label='native_log')
report = read(native/'stage-report.json')
check('native_complete_zero_orders', receipt['status'] == report['status'] == 'COMPLETE'
      and receipt['orders_placed'] == report['orders_placed'] == 0
      and not receipt['broker_orders_enabled'] and not report['broker_orders_enabled'])
check('all_eight_stages_complete', len(report['stages']) == 8
      and all(s['status'] == 'COMPLETE' and s['exit_code'] == 0 and s['error'] is None
              for s in report['stages']))
check('saved_configuration_preserved', report['archive_history']
      and report['stock_price_source'] == 'xnas-itch-archive-v1'
      and report['probability_target_contract'] == 'raw-price-direction-v1'
      and report['deadline_at'] == report['effective_deadline_at'] == '2026-09-28T11:00:00+00:00'
      and report['deadline_exception'] is None)

paths = {
    'gameplan': STORE/'ml/nightly-gameplan-runs/20260926T061604.318956Z',
    'trade_plan': STORE/'ml/gameplan-trade-plan-runs/20260926T062723.283372Z',
    'actuals': STORE/'ml/gameplan-actuals-review-runs/20260926T063044.178275Z',
    'sizing': STORE/'ml/stock-trader-model-runs/20260926T062539.552484Z',
}
pointers = {
    'gameplan': 'nightly-gameplan-latest',
    'trade_plan': 'gameplan-trade-plan-latest',
    'actuals': 'gameplan-actuals-review-latest',
    'evaluation': 'gameplan-evaluation-latest',
}
for kind, folder in pointers.items():
    p = read(STORE/'ml'/folder/'run.json')['current']
    directory = STORE/p['run_path']
    if kind == 'evaluation':
        paths[kind] = directory
    else:
        check(f'{kind}_expected_pointer', directory == paths[kind], current=p)
    bound(directory/'receipt.json', p.get('receipt_sha256', p.get('receipt_checksum_sha256')),
          label=f'{kind}_pointer_receipt')
    if 'manifest_checksum_sha256' in p:
        bound(directory/'manifest.json', p['manifest_checksum_sha256'], label=f'{kind}_pointer_manifest')

output_count = 0
for kind, directory in paths.items():
    r = read(directory/'receipt.json')
    m = read(directory/'manifest.json')
    bound(directory/'manifest.json', r.get('manifest_sha256', r.get('manifest_checksum_sha256')),
          label=f'{kind}_receipt_manifest')
    for name, binding in m['output_files'].items():
        bound(directory/name, binding['checksum_sha256'], binding['size'], label=f'{kind}_output')
        output_count += 1

symbols = [line.strip() for line in (REPO/'datafetching/watchlist.txt').read_text().splitlines()
           if line.strip() and not line.lstrip().startswith('#')]
frames = {
    'forecasts': pd.read_parquet(paths['gameplan']/'forecasts.parquet'),
    'intents': pd.read_parquet(paths['gameplan']/'option-strategy-intents.parquet'),
    'trade_plan': pd.read_parquet(paths['trade_plan']/'trade-plan.parquet'),
}
for kind, frame in frames.items():
    counts = {str(k): int(v) for k, v in frame.groupby('symbol').size().items()}
    check(f'{kind}_24_per_configured_symbol', set(counts) == set(symbols)
          and len(frame) == 24 * len(symbols) and set(counts.values()) == {24}, counts=counts)
check('stock_only_intents', set(frames['intents']['plan_status']) == {'NO_TRADE_STOCK_ONLY'})
check('current_forecasts_promoted', set(frames['forecasts']['model_status']) == {'PROMOTED'})
check('correct_source_action_dates', set(frames['forecasts']['source_session'].astype(str)) == {'2026-09-25'}
      and set(frames['forecasts']['action_date'].astype(str)) == {'2026-09-28'})
check('trade_plan_frozen_identities', set(frames['forecasts']['id']) == set(frames['trade_plan']['id']))

directional = read(paths['gameplan']/'model-reports.json')
directional_summary = {}
for horizon, model in directional.items():
    gate = model['promotion_gate']
    check(f'{horizon}_directional_qualification', gate['status'] == 'PROMOTED'
          and gate['policy_version'] == 'independent-stock-directional-promotion-v2'
          and all(gate['checks'].values()))
    directional_summary[horizon] = {
        'status': gate['status'], 'policy': gate['policy_version'],
        'assessment': model['assessment'], 'baseline': model['training_base_rate_assessment'],
        'assessment_beats_baseline': gate['assessment_beats_baseline'],
        'assessment_minus_baseline': gate['assessment_minus_baseline'],
    }
sizing = read(paths['sizing']/'training-report.json')
sizing_summary = {h: {key: m[key] for key in ['status', 'admitted_rows', 'excluded_context_rows',
                                              'fitted_scope_count', 'qualified_scope_count']}
                  for h, m in sizing['horizons'].items()}
check('sizing_fitted_but_unqualified', set(sizing_summary) == {'1h', '4h', '1d', '1w'}
      and all(m['status'] == 'FITTED' and m['qualified_scope_count'] == 0
              for m in sizing_summary.values()))
for path, checksum in sizing['source_files'].items():
    bound(STORE/path, checksum, label='sizing_pinned_source')

actuals = read(paths['actuals']/'report.json')
actuals_frame = pd.read_parquet(paths['actuals']/'forecast-results.parquet')
actuals_counts = {str(k): int(v) for k, v in actuals_frame['actuals_status'].value_counts().items()}
check('actuals_dates_source', actuals['action_date'] == '2026-09-25'
      and actuals['successor_action_date'] == '2026-09-28'
      and STORE/actuals['successor_gameplan_run'] == paths['gameplan']
      and actuals['target_price_source_contract'] == 'xnas-itch-archive-v1')
check('actuals_saved_coverage', sorted(actuals_counts.values()) == [38, 66, 160]
      and actuals['prices'] == {'COMPARED': 137, 'MATURE_AWAITING_DATA': 17}, counts=actuals_counts)

evaluation = read(paths['evaluation']/'summary.json')
evaluations = pd.read_parquet(paths['evaluation']/'evaluations.parquet')
evaluation_counts = {str(k): int(v) for k, v in evaluations['evaluation_status'].value_counts().items()}
check('latest_evaluation_saved_coverage', len(evaluations) == 5568
      and evaluation_counts == {'EVALUATED': 4245, 'MATURE_AWAITING_DATA': 894, 'PENDING_MATURITY': 429},
      counts=evaluation_counts)
monday = evaluations[evaluations['action_date'] == '2026-09-28']
check('monday_evaluation_pending', len(monday) == 264
      and set(monday['evaluation_status']) == {'PENDING_MATURITY'})

errors = [c for c in checks if not c['pass']]
result = {
    'checked_at': datetime.now(timezone.utc).isoformat(),
    'status': 'PASS_WITH_EXISTING_COVERAGE_NOTES' if not errors else 'FAIL',
    'scope': 'Bounded read-only saved artifact and audit-binding verification; no history replay, fitting, provider or broker calls.',
    'native_run': str(native), 'source_session': '2026-09-25', 'action_date': '2026-09-28',
    'manifest_output_files_verified': output_count,
    'previous_full_audit_evidence_files_verified': len(audit['evidence']),
    'checks_passed': len(checks)-len(errors), 'errors': errors, 'checks': checks,
    'paths': {k: str(v) for k,v in paths.items()},
    'directional': directional_summary, 'sizing': sizing_summary,
    'actuals': {'forecasts': actuals_counts, 'prices': actuals['prices']},
    'evaluation': {'run': str(paths['evaluation']), 'forecasts': len(evaluations),
                   'counts': evaluation_counts, 'monday_pending_rows': len(monday)},
    'limitations': ['Original deep source/cohort/inference audit was not rerun; its nine bound evidence files are unchanged.',
                    'Four sizing groups remain unqualified; 1h and 1w directional scores remain within v2 tolerances without beating both baselines.',
                    'Prior actuals retain 38 mature missing and 66 future forecasts; complete source coverage does not create observed prices.'],
}
OUT.mkdir(parents=True, exist_ok=True)
(OUT/'bounded-verification.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
print(json.dumps({k: result[k] for k in ['checked_at','status','manifest_output_files_verified',
                                       'previous_full_audit_evidence_files_verified','checks_passed','errors','actuals','evaluation']}, indent=2))
raise SystemExit(1 if errors else 0)
