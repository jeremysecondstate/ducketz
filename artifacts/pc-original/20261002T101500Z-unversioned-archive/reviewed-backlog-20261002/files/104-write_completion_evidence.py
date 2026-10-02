"""Summarize completed local verification without account balances or holdings."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json

ROOT = Path('<LOCAL_CHECKOUT>')
OUT = Path(__file__).resolve().parent
VERIFY = OUT.parent / 'fallback-enabled-verification'
read = lambda p: json.loads(Path(p).read_text(encoding='utf-8-sig'))
audit = read(VERIFY / 'audit-runner.json')
completion = read(VERIFY / 'completion-audit.json')
models = read(VERIFY / 'yg-completion.json')
assert audit['status'] == 'VERIFIED_WITH_COVERAGE_NOTES'
assert not completion['errors'] and not models['errors']
checks = completion['checks']
signals = checks['trade_plan']['signal_driven_holdings']
refs = checks['reference_completion']
evidence_paths = [
    VERIFY / 'audit-runner.json', VERIFY / 'completion-audit.json', VERIFY / 'yg-completion.json',
    VERIFY / 'fresh-directional-quality-review.json', VERIFY / 'fresh-sizing-and-direction-review.json',
    OUT / 'preservation-final.json', OUT / 'terminal-worker-verification.json',
    OUT / 'cumulative-coverage-diagnosis.json',
    OUT / 'provider-warning/quality-metadata-20261001T041634Z.json',
    OUT / 'provider-warning/loop-b-model-trace-20261001T063444Z.json',
    OUT / 'provider-warning/gameplan-model-trace-20261001T065512Z.json',
    ROOT / 'artifacts/analysis/fallback-session-reviews/2026-09-30-observed-20261001T065840Z/report.json',
    ROOT / 'artifacts/analysis/fallback-session-reviews/2026-09-30-observed-20261001T065840Z/manifest.json',
    ROOT / 'artifacts/analysis/overnight-20260930/fallback-preparation-preflight.json',
    ROOT / 'artifacts/analysis/cross-horizon-fallback-20260930/deployment-status.json',
    ROOT / 'artifacts/analysis/cross-horizon-fallback-20260930/post-deployment-verification-attempt2.json',
    ROOT / 'artifacts/analysis/cross-horizon-fallback-20260930/git-push-receipt.json',
]
result = {
    'schema_version': 'loops-overnight-completion-status-v1',
    'created_at_utc': datetime.now(timezone.utc).isoformat(),
    'status': audit['status'], 'source_session': '2026-09-30', 'action_date': '2026-10-01',
    'native_run': audit['native_run'], 'native_completed_at': '2026-10-01T06:57:46.465661+00:00',
    'original_deadline': '2026-10-01T11:00:00Z', 'native_stages_completed': 8, 'overnight_orders': 0,
    'gameplan': '<LOCAL_DATASTORE>/ml/nightly-gameplan-runs/20261001T064119.779051Z',
    'readable_gameplan': checks['trade_plan']['readable_gameplan'],
    'readable_prior_actuals': checks['actuals_review']['readable_results'],
    'counts': {'symbols': 11, 'forecasts': 264, 'stock_only_intents': 264, 'planning_rows': 264,
               'opra_scopes': 33, 'price_points': 154, 'hourly_portfolio_rows': 14},
    'directional_models': {'fresh_groups_promoted': ['1h', '4h', '1d', '1w'],
        'rows_promoted': 264, 'bullish': 124, 'bearish': 140, 'neutral': 0,
        'policy': 'stock-direction-50-v2', 'quality_policy': 'independent-stock-directional-promotion-v2',
        'baseline_outperformance_claimed': False, 'sparse_exact_route_support_disclosed': True},
    'learned_sizing': {'fitted_groups': 4, 'qualified_scopes': 0, 'status': 'RESEARCH_ONLY',
        'review': 'Saved development selection, convergence, calibration and qualification logic agree; no concrete repair identified.'},
    'fallback_rollout': {'status': 'DEPLOYED_VERIFIED_AND_OCTOBER1_PUBLICATION_BOUND',
        'policy': 'hierarchical-bearish-fallback-v1', 'manifest_sha256': audit['implementation_after']['candidate_manifest_sha256'],
        'current_apply_progress_and_receipt': 'EXACT_APPLIED', 'sealed_installed_files': 23,
        'dependency_files_verified': 176, 'successful_deployment_tests': 678,
        'final_audit_source_bindings_unchanged': 341, 'full_preservation_source_bindings_unchanged': 737,
        'daily_symbol_and_donor_fraction': '1/2', 'weighted_slots_per_symbol': 18, 'slot_weight_total': 24,
        'integer_quotas_donor_hashes_and_conservation': 'VERIFIED',
        'scheduled_expiry_sales': signals['scheduled_expiry_sales'],
        'live_baseline_or_fills_established_by_planning': False},
    'archive_history': {'bound_source_files_verified': checks['archive_history']['source_files_bound_and_verified'],
        'cohort_rows': {k: v['rows'] for k, v in checks['archive_history']['training_cohorts'].items()},
        'all_cohorts_exactly_reproduced': True, 'seconds_minutes_consistency_verified': True,
        'raw_dbn_every_record_replayed': False},
    'planning_references': {'contract': refs['contract_version'], 'maximum_carry_minutes': 240,
        'current_carried': {v['symbol']: {'minutes': v['gap_minutes'], 'observed_at': v['observed_at'],
            'effective_at': v['effective_at']} for v in refs['synthetic_references'].values()},
        'historical_reference_status_counts': refs['historical_reference_status_counts'],
        'historical_carried_references': len(refs['historical_synthetic_anchors']),
        'current_synthetic_bar_rows': refs['synthetic_rows'],
        'disclosure': 'Planning carry-forward uses synthetic zero-volume ASSUMED_NO_TRADES bars; these are not observed trades, training labels, actuals or live quotes.'},
    'cumulative_evaluation': {'run': checks['cumulative_evaluation']['run'],
        **checks['cumulative_evaluation']['coverage_counts']},
    'prior_actuals_coverage': checks['actuals_review']['coverage'],
    'september30_fallback_review': {'status': 'NOT_ENABLED', 'baseline': None,
        'sales_inferred': False, 'profit_inferred': False, 'reason': 'Expected absence of fallback baseline for prior publication; not proof of zero sales.'},
    'provider_advisories': {'GLBX_MDP3_20260930': 'Degraded cause undisclosed; schema lag separate; saturated MBP captures are not full coverage.',
        'fresh_model_usage': 'Flagged September30 captures excluded from bound LoopB CME inputs; all four independent Gameplan models admit zero CME features.',
        'FMP': 'Existing September2 energy timestamp-skew advisory retained; no new acquisition defect.'},
    'preservation': 'Controls, holdings ledger, prior publications, schedules, Git HEAD/branch/index and source hashes unchanged; no native or trader workers remain.',
    'source_publication': {'commit': 'be760388063a0fa3099c1cc445ba758be69ceb23',
        'branch': 'codex/cross-horizon-bearish-fallback-20260930', 'already_pushed_verified': True,
        'new_source_changes': False, 'new_ready_record': False, 'duplicate_publication': False},
    'authority': 'Preparation and read-only verification only; manual trader start, controls and all execution gates remain unchanged. Peer notice grants no deployment or order authority.',
    'atlas_handoff': 'Send this sanitized status against the existing commit; raw account, holdings, ledger, model and datastore evidence remain local.',
    'evidence': [{'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in evidence_paths],
}
destination = OUT / 'completion-status-sanitized.json'
with destination.open('x', encoding='utf-8') as stream:
    json.dump(result, stream, indent=2)
    stream.write('\n')
print(json.dumps({'status': result['status'], 'path': str(destination),
    'sha256': hashlib.sha256(destination.read_bytes()).hexdigest()}))
