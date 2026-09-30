"""Research admission is prospective and never fabricates qualification."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json

import pytest

from ml import hyperliquid_paper_cadence as cadence_module
from ml import hyperliquid_paper_forecast_evidence as proof
from tests.test_hyperliquid_paper_cadence import (
    cadence, comparison, eligible_forecast_evidence, native_baseline,
    save_comparison, scored_active, write,
)


def exploratory(active):
    active = deepcopy(active)
    active['win_forecast_rule'] = proof.EXPLORATORY_WIN_FORECAST_RULE
    active['recipe']['require_qualified_forecasts'] = False
    return active


def research_evidence(active, endpoint):
    evidence = eligible_forecast_evidence(active, endpoint)
    evidence.update(rule=proof.EXPLORATORY_WIN_FORECAST_RULE,
                    valid_forecast_count=1, valid_qualified_forecast_count=0)
    witness = evidence['first_valid_witness']
    detail = json.loads(witness['decision']['details_json'])
    detail.update(qualified=False, policy_id=active['recipe']['paper_policy_id'])
    detail['forecast_observation']['prediction'].update(qualified=False, role='research_candidate')
    witness['decision']['details_json'] = json.dumps(detail)
    witness['decision_sha256'] = proof._hash(witness['decision']['details_json'])
    model = json.loads(witness['model_record_json'])
    model['eligible'] = False
    witness['model_record_json'] = json.dumps(model)
    witness['model_record_sha256'] = proof._hash(witness['model_record_json'])
    return evidence


def closing_with_research(cadence):
    active = exploratory(scored_active(cadence))
    closing = comparison(active)
    closing['paper']['forecast_evidence'] = research_evidence(active, closing['paper']['observed_at_utc'])
    return active, closing


def test_fresh_consumed_research_hold_can_support_prospective_win(cadence):
    active, closing = closing_with_research(cadence)
    decision = cadence_module.classify(active, closing)
    assert decision['outcome'] == 'win' and decision['next_evaluation_hours'] == 3
    evidence = closing['paper']['forecast_evidence']
    assert evidence['valid_qualified_forecast_count'] == 0
    assert json.loads(evidence['first_valid_witness']['model_record_json'])['eligible'] is False


@pytest.mark.parametrize('edge,outcome', [(-10, 'loss'), (0, 'tie')])
def test_research_rule_does_not_require_a_forecast_for_loss_or_tie(cadence, edge, outcome):
    active = exploratory(scored_active(cadence))
    closing = comparison(active, edge=edge)
    closing['paper'].pop('forecast_evidence')
    assert cadence_module.classify(active, closing)['outcome'] == outcome


@pytest.mark.parametrize('problem', ['stale', 'policy', 'qualified_lie', 'model_lie', 'role',
                                   'missing_observation', 'nonfinite', 'publication_before_seed'])
def test_research_witness_keeps_freshness_truth_and_consumption_checks(cadence, problem):
    active, closing = closing_with_research(cadence)
    witness = closing['paper']['forecast_evidence']['first_valid_witness']
    detail = json.loads(witness['decision']['details_json'])
    prediction = detail['forecast_observation']['prediction']
    if problem == 'stale': prediction['_valid_until_epoch'] = 0
    elif problem == 'policy': detail['policy_id'] = 'other-policy'
    elif problem == 'qualified_lie': detail['qualified'] = True
    elif problem == 'model_lie':
        model = json.loads(witness['model_record_json']); model['eligible'] = True
        witness['model_record_json'] = json.dumps(model)
        witness['model_record_sha256'] = proof._hash(witness['model_record_json'])
    elif problem == 'role': prediction['role'] = 'active'
    elif problem == 'missing_observation': del detail['forecast_observation']
    elif problem == 'nonfinite': prediction['p_not_down'] = float('nan')
    elif problem == 'publication_before_seed': prediction['created_at_utc'] = '2026-01-01T00:00:00Z'
    witness['decision']['details_json'] = json.dumps(detail)
    witness['decision_sha256'] = proof._hash(witness['decision']['details_json'])
    decision = cadence_module.classify(active, closing)
    assert decision['outcome'] == 'unavailable'
    assert decision['next_evaluation_hours'] == active['evaluation_hours']


@pytest.mark.parametrize('problem', ['late', 'flow', 'account', 'skew'])
def test_research_does_not_relax_endpoint_comparability(cadence, problem):
    active, closing = closing_with_research(cadence)
    if problem == 'late': closing['actual']['completed_at_utc'] = (cadence_module.stamp(active['due_at_utc']) + timedelta(minutes=6)).isoformat()
    elif problem == 'flow': closing['actual']['accounts']['alex']['flows']['event_count'] = 1
    elif problem == 'account': del closing['actual']['accounts']['alex']
    else: closing['comparison']['observation_skew_seconds'] = 121
    assert cadence_module.classify(active, closing)['outcome'] in ('unavailable', 'late_unscored')


def test_qualified_v1_and_historical_rounds_keep_their_meaning(cadence):
    active, closing = closing_with_research(cadence)
    active['win_forecast_rule'] = proof.WIN_FORECAST_RULE
    active['recipe']['require_qualified_forecasts'] = True
    assert cadence_module.classify(active, closing)['outcome'] == 'unavailable'
    del active['win_forecast_rule']
    assert cadence_module.classify(active, closing)['outcome'] == 'win'


@pytest.mark.parametrize('rule,admission', [(proof.EXPLORATORY_WIN_FORECAST_RULE, True),
                                         (proof.WIN_FORECAST_RULE, False)])
def test_rule_must_match_committed_admission_policy(cadence, rule, admission):
    active = scored_active(cadence)
    active['win_forecast_rule'] = rule
    active['recipe']['require_qualified_forecasts'] = admission
    with pytest.raises(ValueError, match='admission'):
        cadence_module.validate_active(active)


def test_collector_uses_hashed_accepted_config_and_retains_research_label(cadence):
    active, closing = closing_with_research(cadence)
    witness = closing['paper']['forecast_evidence']['first_valid_witness']
    model = json.loads(witness['model_record_json'])
    path = cadence.root / '_models/BTC/5m/h1/runs' / model['model_id'] / 'record.json'
    path.parent.mkdir(parents=True); path.write_text(witness['model_record_json'])
    accepted = cadence.root / '_operations/paper-improvement' / active['experiment_id'] / 'accepted-source/configs/hyperliquid-paper.json'
    write(accepted, {'require_qualified_forecasts': False})
    experiment = {'experiment_id': active['experiment_id'], 'opening_policy_id': active['recipe']['paper_policy_id'],
                  'config_sha256': {'hyperliquid-paper.json': hashlib.sha256(accepted.read_bytes()).hexdigest()}}
    evidence = proof.collect_forecast_evidence(cadence.root, experiment, active['seed_at_utc'],
                                              closing['paper']['observed_at_utc'], [witness['decision']])
    assert evidence['rule'] == proof.EXPLORATORY_WIN_FORECAST_RULE
    assert evidence['valid_forecast_count'] == 1 and evidence['valid_qualified_forecast_count'] == 0
    assert evidence['qualified_decision_count'] == 0
    assert proof.has_eligible_forecast(evidence, active, closing['paper']['observed_at_utc'])
    write(accepted, {'require_qualified_forecasts': True})
    with pytest.raises(ValueError, match='configuration changed'):
        proof.collect_forecast_evidence(cadence.root, experiment, active['seed_at_utc'],
                                        closing['paper']['observed_at_utc'], [witness['decision']])


def test_native_advance_commits_exploratory_rule_only_for_new_baseline(cadence):
    active = scored_active(cadence)
    assessed = cadence.assess(save_comparison(cadence, comparison(active)))
    immutable = cadence_module.Path(assessed['pending_assessment']['path']).read_bytes()
    record = native_baseline(cadence.root, 'run-2', '2026-09-29T05:00:00+00:00', 'run-1')
    accepted = cadence_module.Path(record['evidence_directory']) / 'accepted-source/configs/hyperliquid-paper.json'
    write(accepted, {'require_qualified_forecasts': False})
    record['config_sha256']['hyperliquid-paper.json'] = cadence_module.file_digest(accepted)
    for path in (cadence.root / '_paper/experiment.json', cadence.root / '_operations/paper-current-accepted.json',
                 cadence_module.Path(record['evidence_directory']) / 'deployment.json'):
        write(path, record)
    result = cadence.advance()
    assert result['active']['win_forecast_rule'] == proof.EXPLORATORY_WIN_FORECAST_RULE
    assert result['active']['recipe']['require_qualified_forecasts'] is False
    assert cadence_module.Path(assessed['pending_assessment']['path']).read_bytes() == immutable
