"""Bounded read-only follow-up. Reads saved reports/small output tables; never fits or fetches."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import math
import pandas as pd

REPO = Path('<LOCAL_CHECKOUT>')
ROOT = Path('<LOCAL_DATASTORE>')
OUT = Path(__file__).parent
V = OUT.parent
GAME = ROOT/'ml/nightly-gameplan-runs/20260930T061501.586401Z'
SIZING = ROOT/'ml/stock-trader-model-runs/20260930T062446.604631Z'
ACTUAL = ROOT/'ml/gameplan-actuals-review-runs/20260930T063012.679198Z'
checks = []
bindings = {}

def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def load(p):
    return json.loads(Path(p).read_text(encoding='utf-8-sig'))

def check(name, passed):
    checks.append({'name': name, 'passed': bool(passed)})
    if not passed:
        raise AssertionError(name)

def bind(p):
    p = Path(p)
    bindings[str(p)] = {'sha256': sha(p), 'bytes': p.stat().st_size}

def verified_outputs(run, names):
    receipt, manifest = load(run/'receipt.json'), load(run/'manifest.json')
    expected_manifest = receipt.get('manifest_sha256', receipt.get('manifest_checksum_sha256'))
    check(f'{run.name}: manifest receipt', sha(run/'manifest.json') == expected_manifest)
    for name in names:
        expected = manifest['output_files'][name]
        p = run/name
        check(f'{run.name}: {name}', sha(p) == expected['checksum_sha256'] and p.stat().st_size == expected['size'])
        bind(p)
    bind(run/'manifest.json'); bind(run/'receipt.json')

verified_outputs(GAME, ['model-reports.json', 'forecasts.parquet'])
verified_outputs(SIZING, ['training-report.json'])
verified_outputs(ACTUAL, ['forecast-results.parquet', 'price-results.parquet', 'report.json'])
for name in ['completion-audit.json', 'model-review.json', 'yg-completion.json',
             'preliminary-sizing-review.json', 'preliminary-directional-review.json']:
    bind(V/name)
completed = load(V/'completion-audit.json')
check('original complete audit all 12 sections verified', len(completed['checks']) == 12 and
      all(s['status'] == 'VERIFIED' for s in completed['checks'].values()) and not completed['errors'])

sizing = load(V/'preliminary-sizing-review.json')
directional = load(V/'preliminary-directional-review.json')
native_sizing = load(SIZING/'training-report.json')
native_directional = load(GAME/'model-reports.json')
check('sizing same Gameplan source', native_sizing['source_gameplan_run'] == GAME.relative_to(ROOT).as_posix())
review_horizons = {}
for group, h in sizing['horizons'].items():
    n = native_sizing['horizons'][group]
    scores = h['assessment_scores']
    exact_scope = next(iter(n['scope_readiness'].values()))
    check(f'{group}: saved independent scores match native scope horizon', exact_scope['horizon_assessment_scores'] == scores)
    check(f'{group}: native development selection unchanged', n['head_development_selection'] == h['development_selection'])
    check(f'{group}: fitted but no qualified scopes', n['status'] == 'FITTED' and n['qualified_scope_count'] == 0)
    dev = h['development_selection']
    ordered = [dev['candidate_metrics'][str(float(p))] for p in dev['candidate_penalties']]
    check(f'{group}: return and probability development optima at upper grid edge',
          dev['selected_penalties']['expected_net_return'] == max(dev['candidate_penalties']) and
          dev['selected_penalties']['trade_probability'] == max(dev['candidate_penalties']) and
          all(ordered[i]['return_mse'] > ordered[i+1]['return_mse'] and
              ordered[i]['log_loss'] > ordered[i+1]['log_loss'] for i in range(len(ordered)-1)))
    review_horizons[group] = {k: h[k] for k in ['fit_status', 'fitted_scopes', 'qualified_scopes',
        'assessment_scores', 'quality_checks', 'failed_quality_checks', 'scope_reasons',
        'market_feature_admission', 'development_selection', 'iterative_optimizers_converged', 'thin_scope_evidence']}
    review_horizons[group]['return_mse_excess_percent'] = 100*(scores['return_mse']/scores['base_return_mse']-1)

direction_groups = {}
for group, d in directional['groups'].items():
    n = native_directional[group]
    # Prior heavy inference already proved these assessment scores and probabilities.
    check(f'{group}: directional assessment matches completed audit',
          d['assessment'] == completed['checks']['gameplan']['model_assessments'][group]['assessment'])
    direction_groups[group] = {k: d[k] for k in ['promotion_status', 'promotion_policy', 'metric_limits',
        'assessment', 'baseline', 'minimum_exact_route_support']}
    check(f'{group}: directional saved promotion has no failed checks', not d['failed_quality_checks'])

f = pd.read_parquet(ACTUAL/'forecast-results.parquet')
p = pd.read_parquet(ACTUAL/'price-results.parquet')
report = load(ACTUAL/'report.json')
cutoff = pd.Timestamp(report['outcomes_through'])
missing = f[f.actuals_status.eq('MATURE_AWAITING_DATA')]
pending = f[f.actuals_status.eq('PENDING_MATURITY')]
evaluated = f[f.actuals_status.eq('EVALUATED')]
slots = []
for side, column in [('start', 'target_window_start'), ('end', 'target_window_end')]:
    for row in missing.to_dict('records'):
        if row[f'actual_{side}_status'] != 'OBSERVED':
            slots.append({'forecast_id':row['id'], 'symbol':row['symbol'], 'route':row['route'],
                'side':side, 'timestamp':row[column], 'status':row[f'actual_{side}_status'],
                'candidate_observed_at':row[f'actual_{side}_candidate_observed_at'],
                'gap_seconds':None if pd.isna(row[f'actual_{side}_gap_seconds']) else row[f'actual_{side}_gap_seconds'],
                'source_coverage':row[f'actual_{side}_source_coverage'],
                'required_source_start':row[f'actual_{side}_required_source_start'],
                'required_source_end':row[f'actual_{side}_required_source_end']})
boundary = pd.DataFrame(slots)
check('actuals saved counts exactly reproduced', len(f)==264 and len(missing)==35 and len(pending)==66 and len(evaluated)==163)
check('all missing matured targets end by cutoff', (missing.target_window_end <= cutoff).all())
check('all missing mature boundary acquisition windows complete', boundary.source_coverage.eq('VERIFIED_COMPLETE').all())
check('all pending end after saved cutoff', (pending.target_window_end > cutoff).all())
check('all missing clock acquisition windows complete', p[p.comparison_status.ne('COMPARED')].actual_source_coverage.eq('VERIFIED_COMPLETE').all())
check('direction score is raw outcome, no synthetic substitutions', int(evaluated.direction_correct.sum())==84 and
      not completed['checks']['actuals_review']['synthetic_prices_used_for_actuals'])

actuals = {
    'source_action_date':report['action_date'], 'outcomes_through':report['outcomes_through'],
    'reviewed_at':report['reviewed_at'], 'counts':report['forecasts'],
    'missing_forecast_rows_by_symbol':missing.symbol.value_counts().sort_index().to_dict(),
    'missing_forecast_rows_by_group':missing.model_group.value_counts().to_dict(),
    'missing_boundary_slots':len(boundary),
    'distinct_missing_symbol_timestamps':len(boundary.drop_duplicates(['symbol','timestamp'])),
    'distinct_missing_symbol_timestamp_sides':len(boundary.drop_duplicates(['symbol','timestamp','side'])),
    'distinct_missing_symbol_request_windows':len(boundary.drop_duplicates(['symbol','required_source_start','required_source_end'])),
    'missing_boundary_details':slots,
    'missing_boundary_status_counts':[dict(side=a,status=b,count=int(c)) for (a,b),c in boundary.groupby(['side','status']).size().items()],
    'pending_routes':[{'route':route,'end':end,'end_pacific':end.tz_convert('America/Los_Angeles'), 'rows':int(n)}
                      for (route,end),n in pending.groupby(['route','target_window_end']).size().items()],
    'pending_with_already_ineligible_start':pending[~pending.actual_start_status.isin(['PENDING_MATURITY','OBSERVED'])][
        ['symbol','route','actual_start_status','actual_start_source_coverage']].to_dict('records'),
    'pending_caveat':'Maturity is necessary, not sufficient: later valid end data cannot repair an already ineligible observed start.',
    'price_clocks':report['prices'],
    'missing_price_details':p[p.comparison_status.ne('COMPARED')][['symbol','clock_local','actual_status',
        'actual_candidate_observed_at','actual_source_coverage']].to_dict('records'),
    'raw_direction_correct':84, 'raw_direction_scored':163, 'raw_direction_accuracy':84/163,
    'compared_clock_prices_in_range':int(p.in_planned_range.fillna(False).sum()),
    'limitation':'Prior full archive audit recomputed observations once. This follow-up rechecks immutable output hashes and counts only; no duplicate raw reconstruction or provider request.'}

source_paths = ['Start-Gameplan-Trader.cmd', 'ml/stock_trader/independent_runtime.py',
    'ml/stock_trader/gameplan_execution.py', 'ml/stock_trader/gameplan_direction_engine.py',
    'ml/stock_trader/independent_training.py', 'docs/loops-system-analysis/INDEPENDENT_STOCK_HORIZONS.md']
for name in source_paths:
    bind(REPO/name)
runtime = (REPO/'ml/stock_trader/independent_runtime.py').read_text()
check('manual launcher explicitly selects Gameplan policy', '-SizingPolicy gameplan-direction-current-market-v1' in (REPO/'Start-Gameplan-Trader.cmd').read_text())
check('runtime loads learned model only for learned policy', 'model = load_current_enrichment_model(root) if sizing_policy == LEARNED_SIZING_POLICY else None' in runtime)
check('Gameplan policy accepts saved signals without learned model readiness',
      'qualified = (dict(signals) if sizing_policy in {FIXED_SIZING_POLICY, GAMEPLAN_SIZING_POLICY}' in runtime)

output = {
    'reviewed_at':datetime.now(timezone.utc).isoformat(), 'status':'READ_ONLY_LIMITATIONS_REVIEW_COMPLETE',
    'scope':'Immutable completed September 29 source / September 30 action outputs and current selected manual policy code. No broker/production/provider actions; no fits or inference reruns.',
    'conclusion':'Operational verification passed. Prediction quality and thin venue-specific observations remain material limitations; no demonstrated code/data defect supports immediate native repair.',
    'checks':checks, 'bindings':bindings, 'sizing':review_horizons, 'directional':direction_groups,
    'TWST_directional_exact_route_support':directional['TWST_saved_support_focus'], 'actuals':actuals,
    'today_execution':{
        'policy':'gameplan-direction-current-market-v1',
        'learned_sizing_dependency':False,
        'direct_published_instruction_reader':True,
        'learned_model_failure_automatic_veto':False,
        'missing_historical_comparison_automatic_veto':False,
        'retained_checks':'Publication deployment/window/instruction identity, actual cash, ownership, pending orders, current quotes and execution controls still apply.',
        'caveat':'Code branch verification only; no trader started, and no claim that current live readiness is established by this review.'},
    'possible_future_development':{
        'basis':'Development error improves monotonically at penalties 1,5,20 for return and probability heads in all four horizons, with 20 selected at the fixed-grid boundary.',
        'study':'A separately versioned, preregistered stronger-regularization study with a training-mean return baseline candidate and purged chronological development evaluation.',
        'guard':'Do not tune to this exposed assessment set. Freeze candidate/protocol choices on development, then assess on untouched future data. Keep present strict qualification rules and saved publications.',
        'expectation':'Development boundary evidence justifies research, not a guaranteed pass. Hourly/four-hour return differences in development are tiny.',
        'route_evidence':'Accumulate newly observed exact-route samples. Do not fabricate bars, combine datasets, pool an unseen duration as observed, or lower sample gates.',
        'source_change':'If complete-market coverage is desired, study a separately identified consolidated source with explicit provider/license/cost authorization and fresh versioned training/evaluation; never splice its prices into existing XNAS publications.'},
    'primary_sources':[
        {'url':'https://databento.com/docs/examples/equities/equities-introduction','supports':'XNAS.ITCH is Nasdaq TotalView-ITCH.'},
        {'url':'https://nasdaqtrader.com/content/technicalsupport/specifications/dataproducts/NQTVITCHSpecification.pdf','supports':'Messages describe orders added, removed and executed on Nasdaq; this is a venue feed, not consolidated US execution coverage.'},
        {'url':'https://databento.com/docs/knowledge-base','supports':'OHLCV convention emits no bar when no trade occurs in an interval.'}],
    'venue_interpretation':'Complete native acquisition of XNAS.ITCH does not guarantee a bar at each requested clock or prove absence of all-market trading. No eligible XNAS observation remains missing, not zero return or a fabricated actual.'}
def clean(value):
    if isinstance(value, dict):
        return {str(k):clean(v) for k,v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return None if isinstance(value, float) and not math.isfinite(value) else value

(OUT/'limits-review.json').write_text(json.dumps(clean(output),indent=2,default=str,allow_nan=False)+'\n',encoding='utf-8')
print(json.dumps({'status':output['status'],'checks':len(checks),'path':str(OUT/'limits-review.json'),
                  'sha256':sha(OUT/'limits-review.json'),'missing_forecasts':len(missing),
                  'missing_boundary_slots':len(boundary),'pending':len(pending)}))
