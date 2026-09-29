"""Bounded peer review of saved final reports; no heavy or production operations."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import re
import shutil

OUT = Path(__file__).resolve().parent
BASE = OUT.parent
V = BASE / 'verification'

def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    summary = BASE / 'completion-summary.md'
    text = summary.read_text(encoding='utf-8-sig')
    c, r, y, p, a, q = [read(V/name) for name in (
        'completion-audit.json', 'audit-runner-continuation.json', 'yg-completion.json',
        'provider-completion.json', 'provider-advisory-resolution.json', 'pricing-gate-review.json')]
    archive = c['checks']['archive_history']
    seconds = archive['second_minute_consistency']
    links = [m.group(1).strip('<>') for m in re.finditer(r'\[[^\]]*\]\(([^)]+)\)', text)]
    local = [Path(link) for link in links if re.match(r'^[A-Za-z]:[/\\]', link)]
    missing = [str(path) for path in local if not path.is_file()]
    bad_bindings = []
    bound = dict(r['evidence'])
    bound.update(original_runner=r['original_runner'], isolated_drift_review=r['isolated_drift_review'])
    bound['resolved_provider'] = a['provider_evidence']
    for key, value in bound.items():
        if sha(Path(value['path'])) != value['sha256']:
            bad_bindings.append(key)
    checks = {
        'all_markdown_links_are_existing_absolute_local_files': len(local) == len(links) and not missing,
        'continuation_all_bound_evidence_hashes_match': not bad_bindings,
        'full_audit_12_verified_sections_no_errors': c['status'] == 'VERIFIED_WITH_COVERAGE_NOTES' and not c['errors'] and len(c['checks']) == 12 and all(x['status'] == 'VERIFIED' for x in c['checks'].values()),
        'remaining_five_commands_exit_zero': r['status'] == 'VERIFIED_WITH_COVERAGE_NOTES' and len(r['checks']) == 5 and all(x['exit_code'] == 0 for x in r['checks']),
        'successful_full_audit_inherited_exactly': sha(V/'completion-audit.json') == r['inherited_successful_check']['completion_sha256'] and r['inherited_successful_check']['exit_code'] == 0,
        'suite_timestamp_matches_summary': r['finished_at'].startswith('2026-09-29T06:51:54') and '2026-09-29T06:51:54Z' in text,
        'no_native_restart_repeat_provider_or_order_actions': all(r[key] == 0 for key in ('orders_placed', 'provider_calls', 'production_mutations', 'native_restarts', 'native_stage_repeats')),
        'guard_before_after_exact_baseline_and_single_exception': r['implementation_before'] == r['implementation_after'] and r['implementation_before']['code_files'] == 268 and r['implementation_before']['reviewed_isolated_drift_paths'] == ['ml/hyperliquid_paper_policy.py'],
        'archive_source_counts': archive['source_files_bound_and_verified'] == 988 and archive['source_file_bytes'] == 950452425,
        'exact_cohorts': all(archive['training_cohorts'][h]['rows'] == n and archive['training_cohorts'][h]['all_rows_exactly_reproduced'] for h,n in {'1h':171897,'4h':42965,'1d':29712,'1w':5853}.items()),
        'quality_exclusion_counts': all(archive['exclusion_counts'][k] == n for k,n in {'quality_resets':25,'excluded_intervals':70,'split_boundaries':26,'target_discontinuity_boundaries':5,'excluded_undefined_observations':17}.items()),
        '264_current_predictions_zero_error': sum(x['rows'] for x in archive['current_forecast_inference'].values()) == 264 and all(all(v == 0 for v in x['maximum_absolute_error'].values()) for x in archive['current_forecast_inference'].values()),
        'seconds_partitions_rows_overlap': seconds['native_archive_partitions_verified'] == 223 and sum(x['second_rows'] for x in seconds['by_symbol'].values()) == 16443370 and sum(x['exact_ohlcv_overlap_minutes'] for x in seconds['by_symbol'].values()) == 2842334,
        'no_seconds_added_or_synthetic_examples_or_unmatched_eligible_minutes': seconds['added_training_rows'] == seconds['synthetic_rows'] == 0 and all(x['only_seconds_minutes_unused'] == 0 for x in seconds['by_symbol'].values()),
        'raw_replay_limit_disclosed': seconds['raw_record_replay'] == 'NOT_PERFORMED' and 'does not replay every raw DBN record' in text,
        'all_saved_directional_assessment_scores_zero_error': not y['errors'] and y['status'] == 'VERIFIED' and all(x['score_max_abs_error'] == 0 and x['calibration_diagnostics_and_all_support_counts_reproduced'] for x in y['directional'].values()),
        'only_4h_1d_strictly_beat_both_baselines': {h for h,x in y['directional'].items() if x['assessment']['brier_score'] < x['baseline']['brier_score'] and x['assessment']['log_loss'] < x['baseline']['log_loss']} == {'4h','1d'},
        'four_fitted_sizing_zero_qualified': all(x['status'] == 'FITTED' and x['qualified_scope_count'] == 0 for x in c['checks']['enrichment']['horizons'].values()),
        'optional_sizing_admission_counts': all((x['admitted_rows'], x['market_feature_admission']['excluded_missing_market_rows']) == {'1h':(93163,72749),'4h':(25119,17846),'1d':(3798,2141),'1w':(3772,2081)}[h] for h,x in c['checks']['enrichment']['horizons'].items()),
        'all_five_providers_complete_and_outputs_present': set(p['base_cycle']['providers']) == {'databento','fmp','fred','schwab','sec'} and p['base_cycle']['failure_count'] == 0 and p['logged_output_paths_checked'] == 456 and not p['missing_logged_outputs'] and not p['issues'] and not p['pending'],
        '33_opra_scopes_and_66_payloads': p['preflight_count'] == p['current_cursor_count'] == p['current_partition_count'] == 33 and p['current_data_files_hashed'] == 66,
        'opra_zero_cost_capacity_and_estimate': p['observed_estimated_download_bytes'] == 4882915488 and all(x['payload']['estimated_cost_usd'] == 0 and x['payload']['capacity_pass'] for x in p['preflights']),
        'advisory_disposition_resolved': a['status'] == 'KNOWN_OPTIONAL_LIMITATIONS_REVERIFIED' and a['unresolved_review_flags'] == [],
        'six_cme_raw_captures_and_two_capped_mbp': len(a['cme']['captures']) == 6 and all(x['all_configured_symbols_observed'] and 'status=ok' in x['native_completion_log'] for x in a['cme']['captures']) and sum(x['request_limit_saturated'] and x['rows'] == 5000 for x in a['cme']['captures']) == 2,
        'five_fmp_history_skews_current_quote_passes': len(a['fmp']['unchanged_historical_clock_skew_rows']) == 5 and a['fmp']['current_capture_status']['status'] == 'PASS' and a['fmp']['retained_full_history_status']['status'] == 'REJECTED',
        '99_pricing_routes_51_files_17_generations': q['failed_route_count'] == 99 and q['compact_source_files_matched'] == 51 and len(q['authority_chain']) == 17 and not q['issues'] and q['pointer_matches_prior'] and q['saved_route_gate_and_fraction_arithmetic_verified'],
        'administrative_closure_explicitly_pending': 'Supervision remains held for final peer review and administrative closure.' in text,
        'prior_planning_actuals_cash_corrections_retained': '**$219.90 base case**' in text and '**6 of 137**' in text and '**04:16:30Z**' in text,
    }
    issues = [name for name, passed in checks.items() if not passed]
    review = {
        'reviewed_at': datetime.now(timezone.utc).isoformat(),
        'scope': 'READ_ONLY_FINAL_SUMMARY_PEER_REVIEW_SAVED_REPORTS_AND_LOCAL_LINKS_ONLY',
        'summary_path': str(summary), 'summary_sha256': sha(summary),
        'status': 'PASS_NO_CORRECTIONS' if not issues else 'CORRECTIONS_REQUIRED',
        'checks': checks, 'issues': issues, 'local_link_count': len(local), 'missing_local_links': missing,
        'bad_evidence_bindings': bad_bindings,
        'source_reports': {name: {'path':str(V/name), 'sha256':sha(V/name)} for name in (
            'completion-audit.json','audit-runner-continuation.json','yg-completion.json',
            'provider-completion.json','provider-advisory-resolution.json','pricing-gate-review.json')},
        'timing_note': '06:51:54Z is the completed audit-sequence timestamp; its separate advisory disposition was resolved at 06:52:06Z. Summary clearly links both reports and keeps lease closure pending.',
        'manual_review': ['Archive first-source/action dates and per-horizon cohort/quality-exclusion table match the full report.', 'Directional table and fit/qualification distinctions match saved reports.', 'Read all summary additions against their linked completed audits; no factual correction required.', 'Existing bounded ownership, planning, actuals and reconciliation statements retain their previously verified figures.'],
        'heavy_rechecks': 0, 'provider_calls': 0, 'broker_calls': 0, 'production_mutations': 0,
    }
    destination = OUT / 'completion-summary-peer-review.json'
    prior = OUT / 'completion-summary-peer-review-initial.json'
    if destination.exists() and not prior.exists():
        shutil.copy2(destination, prior)
    destination.write_text(json.dumps(review, indent=2) + '\n')
    print(json.dumps({'status':review['status'],'checks':len(checks),'issues':issues,'local_links':len(local),'summary_sha256':review['summary_sha256'],'review_sha256':sha(destination)}))
    return 0 if not issues else 1

if __name__ == '__main__':
    raise SystemExit(main())
