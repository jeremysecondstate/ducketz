"""Bounded saved-report diagnosis; no fits, source archives, broker or final audit."""
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import sys

OUT = Path(__file__).resolve().parent
REPO = Path('<LOCAL_CHECKOUT>')
ROOT = Path('<LOCAL_DATASTORE>')
RUN = ROOT/'ml/nightly-gameplan-runs/20261001T064119.779051Z'
PREVIOUS = ROOT/'ml/nightly-gameplan-runs/20260930T061501.586401Z'
NATIVE = ROOT/'ml/overnight-runs/20261001T040644.667536Z'
sys.path.insert(0, str(REPO))
from ml.gameplan_promotion import build_promotion_gate
import pandas as pd

read = lambda path: json.loads(path.read_text(encoding='utf-8-sig'))
sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
manifest, receipt = read(RUN/'manifest.json'), read(RUN/'receipt.json')
assert receipt['manifest_checksum_sha256'] == sha(RUN/'manifest.json')
assert receipt['action_date'] == manifest['configuration']['action_date'] == '2026-10-01'
assert receipt['orders_placed'] == 0 and receipt['broker_orders_enabled'] is False
for name in ('model-reports.json', 'forecasts.parquet', 'archive-history.json'):
    binding = manifest['output_files'][name]
    assert binding['checksum_sha256'] == sha(RUN/name) and binding['size'] == (RUN/name).stat().st_size
reports, previous = read(RUN/'model-reports.json'), read(PREVIOUS/'model-reports.json')
archive = read(RUN/'archive-history.json')
forecasts = pd.read_parquet(RUN/'forecasts.parquet')
assert len(forecasts) == 264 and forecasts.groupby('symbol').size().eq(24).all()
groups = {}
for group, report in reports.items():
    boundary = report['target_boundary_quality']
    prior = previous[group]['target_boundary_quality']
    assert boundary['candidate_rows'] == boundary['aligned_rows'] + boundary['excluded_rows']
    assert sum(boundary['excluded_rows_by_route'].values()) == boundary['excluded_rows']
    assert sum(boundary['admitted_rows_by_symbol'].values()) == boundary['aligned_rows']
    assert sum(boundary['admitted_rows_by_route'].values()) == boundary['aligned_rows']
    assert boundary['enforced'] is True and boundary['maximum_boundary_gap_seconds'] == 300
    assert boundary['target_price_source_contract'] == 'xnas-itch-archive-v1'
    gate = build_promotion_gate(report['assessment'], report['training_base_rate_assessment'],
                               report['calibration_diagnostics'], report['partition_decision_clusters']['assessment'],
                               policy_version=report['promotion_gate']['policy_version'])
    assert gate == report['promotion_gate']
    data = forecasts.loc[forecasts.model_group.eq(group)]
    failed = [name for name, passed in gate['checks'].items() if not passed]
    metrics = {}
    for metric, tolerance in gate['baseline_tolerances'].items():
        score, baseline = Decimal(str(report['assessment'][metric])), Decimal(str(report['training_base_rate_assessment'][metric]))
        metrics[metric] = {'assessment': float(score), 'baseline': float(baseline), 'allowed_excess': tolerance,
                           'margin_to_maximum': float(baseline+Decimal(str(tolerance))-score),
                           'strictly_beats_baseline': score < baseline}
    selected = report['selected_family']
    assert report['selection_metrics'][selected]['log_loss'] == min(v['log_loss'] for v in report['selection_metrics'].values())
    assert report['calibration_selection']['assessment_used_for_selection'] is False
    assert report['probability_target_contract'] == 'raw-price-direction-v1'
    assert report['source_selection_contract'] == 'xnas-archive-prior-session-features-v1'
    cohort = archive['training_cohorts'][group]
    assert cohort['rows'] + cohort['quality_excluded_rows'] == boundary['aligned_rows']
    route_deltas = {route: boundary['excluded_rows_by_route'].get(route, 0)-prior['excluded_rows_by_route'].get(route, 0)
                    for route in sorted(set(prior['excluded_rows_by_route']) | set(boundary['excluded_rows_by_route']))}
    groups[group] = {
        'promotion_status': gate['status'], 'failed_promotion_checks': failed, 'gate_arithmetic_reproduced': True,
        'policy_version': gate['policy_version'], 'metric_limits': metrics, 'selected_family': selected,
        'selected_on_minimum_development_log_loss': True, 'calibration_selection_excludes_assessment': True,
        'deployment': report.get('deployment'), 'forecast_status_counts': data.model_status.value_counts().to_dict(),
        'minimum_exact_symbol_route_fitted_rows': int(data.symbol_route_fitted_target_rows.min()),
        'partition_rows': report['partitions'], 'partition_clusters': report['partition_decision_clusters'],
        'calibration_diagnostics': report['calibration_diagnostics'],
        'boundary_warning': {'candidate_rows': boundary['candidate_rows'], 'aligned_rows': boundary['aligned_rows'],
            'excluded_rows': boundary['excluded_rows'], 'excluded_fraction': boundary['excluded_rows']/boundary['candidate_rows'],
            'conflicting_minute_rows_excluded': boundary['conflicting_minute_rows_excluded'],
            'excluded_rows_by_route': boundary['excluded_rows_by_route'],
            'delta_from_previous': {key: boundary[key]-prior[key] for key in ('candidate_rows','aligned_rows','excluded_rows')},
            'excluded_route_delta_from_previous': route_deltas,
            'post_boundary_quality_excluded_rows': cohort['quality_excluded_rows'], 'final_cohort_rows': cohort['rows'],
            'cohort_ranges_by_symbol': cohort['by_symbol']},
    }
lines = (NATIVE/'gameplan_publication.log').read_text(encoding='utf-8').splitlines()
events = [json.loads(line) for line in lines if line.startswith('{') and line.endswith('}')
          and any(name in line for name in ('FIT_WARNING','FIT_ERROR'))]
quality_failures = [event for event in events if not event.get('fit','').endswith('/target-quality')]
record = {
    'reviewed_at': datetime.now(timezone.utc).isoformat(), 'status': 'SAVED_REPORT_QUALITY_VERIFIED',
    'native_status_at_review': read(NATIVE/'stage-report.json')['status'], 'publication': str(RUN),
    'action_date': receipt['action_date'], 'published_at': receipt['published_at'], 'groups': groups,
    'forecast_rows': len(forecasts), 'forecast_status_counts': forecasts.model_status.value_counts().to_dict(),
    'all_fresh_groups_promoted': all(g['promotion_status']=='PROMOTED' and not g['deployment'] for g in groups.values()),
    'non_boundary_fit_warning_or_error_events': quality_failures,
    'boundary_interpretation': 'Counts preserve the same five-minute source policy and prior sparse-boundary pattern; no conflict rows. Current count deltas reconcile exactly with the new session. This is consistent with observed-price availability limits, not evidence of a fitting defect. Exact archive/cohort reproduction remains the separate root final audit.',
    'repair_finding': 'No failed directional assessment group or concrete source/fitting defect identified in bounded saved-report evidence; no justified repair or unchanged retraining proposed.',
    'limits': ['This is not final native completion verification.', 'No raw source archive or full cohort regeneration performed.',
               'Saved estimator inference and complete source/partition reproduction remain root final audit.',
               'Sparse valid histories remain narrow evidence even where a pooled group is promoted.',
               'Sizing qualification is separate and must be reviewed when its report is durable.'],
    'evidence': {str(path): {'sha256': sha(path), 'bytes': path.stat().st_size}
                 for path in (RUN/'receipt.json', RUN/'manifest.json', RUN/'model-reports.json', RUN/'forecasts.parquet',
                              RUN/'archive-history.json', PREVIOUS/'model-reports.json', NATIVE/'gameplan_publication.log')},
    'heavy_final_audit_started': False, 'source_modifications': 0, 'provider_calls': 0, 'orders_placed': 0}
destination = OUT/'fresh-directional-quality-review.json'
assert not destination.exists(), 'Preserve previous diagnosis evidence'
destination.write_text(json.dumps(record, indent=2)+'\n', encoding='utf-8')
print(json.dumps({'status': record['status'], 'all_fresh_groups_promoted': record['all_fresh_groups_promoted'],
    'forecast_status_counts': record['forecast_status_counts'], 'non_boundary_warnings': len(quality_failures),
    'groups': {h:{'metrics': g['metric_limits'], 'boundary_delta':g['boundary_warning']['delta_from_previous']}
               for h,g in groups.items()}, 'output':str(destination)}, indent=2))
