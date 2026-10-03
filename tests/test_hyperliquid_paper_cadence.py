"""Duration credits require reliable timed results and one verified successor."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json

from filelock import FileLock, Timeout
import pytest

from ml import hyperliquid_paper_cadence as module
from ml import hyperliquid_paper_review as review
from tests.test_hyperliquid_paper_review import make_paper


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    review.write_json(path, value)


def native_baseline(root, identity='run-1', seed='2026-09-29T02:00:00+00:00', supersedes='old'):
    paper = root / '_paper'
    if paper.exists():
        retained = root / ('retained-before-' + identity)
        assert retained.parent == root and not retained.exists()
        paper.rename(retained)
    make_paper(root, stamp=seed, experiment=identity, mirror=True)
    evidence = root / '_operations/paper-improvement' / identity
    info = review.seed_info(paper)
    owners = {name: str(index)*64 for index,name in enumerate(sorted(module.ACCOUNTS))}
    opening = {'result': 'passed', 'seed_at_utc': seed,
               'immutable_opening_hashes': info['immutable_opening_hashes'], 'public_owner_sha256': owners}
    write(evidence / 'opening-verification.json', opening)
    hashes = {}
    for name, recipe in {
        'hyperliquid-markets.json': {'interval': '5m', 'symbols': ['BTC','ETH','HYPE','ZEC']},
        'hyperliquid-models.json': {'horizons_bars': [1]},
        'hyperliquid-paper.json': {'require_qualified_forecasts': True},
    }.items():
        path = evidence / 'accepted-source/configs' / name
        write(path, recipe)
        hashes[name] = module.file_digest(path)
    record = {'experiment_id': identity, 'seed_at_utc': seed, 'status': 'running', 'analysis_eligible': True,
              'superseded_experiment_id': supersedes, 'opening_equity': 6000.,
              'opening_policy_id': 'test-policy', 'evidence_directory': str(evidence),
              'opening_verification_path': str(evidence / 'opening-verification.json'),
              'immutable_opening_hashes': info['immutable_opening_hashes'], 'config_sha256': hashes}
    for path in (paper/'experiment.json', root/'_operations/paper-current-accepted.json', evidence/'deployment.json'):
        write(path,record)
    write(evidence/'operation.json', {'phase': 'completed', 'cycle_id': identity})
    write(root/'_operations/paper-maintenance.json', {'status': 'completed', 'phase': 'completed', 'cycle_id': identity})
    return record


@pytest.fixture
def cadence(tmp_path):
    native_baseline(tmp_path)
    instance = module.Cadence(tmp_path)
    instance.init()
    return instance


def scored_active(cadence, duration=2):
    state = cadence.state()
    state['active']['carry_in_unscored'] = False
    state['active']['evaluation_hours'] = state['current_evaluation_hours'] = duration
    state['active']['due_at_utc'] = (module.stamp(state['active']['seed_at_utc'])+timedelta(hours=duration)).isoformat()
    write(cadence.state_path,state)
    return state['active']


def set_rule(cadence, version):
    state = cadence.state()
    state['cadence_rule_version'] = version
    state['active']['cadence_rule_version'] = version
    state['policy'] = module.policy(version)
    write(cadence.state_path, state)
    return state


def comparison(active, edge=10., offset=30):
    due = module.stamp(active['due_at_utc'])
    observed = due+timedelta(seconds=offset)
    completed = observed+timedelta(seconds=10)
    start_ms = int(module.stamp(active['seed_at_utc']).timestamp()*1000)
    end_ms = int(completed.timestamp()*1000)
    accounts, reads = {}, []
    for name,owner in active['public_owner_sha256'].items():
        accounts[name] = {'observed_at_utc': completed.isoformat(), 'flows': {
            'range_complete': True, 'zero_external_flows_verified': True, 'event_count': 0,
            'net_external_flow_usd': 0., 'start_time_ms': start_ms, 'end_time_ms': end_ms}}
        reads.append({'account': name, 'request': {'type': 'userNonFundingLedgerUpdates',
            'user': 'wallet-sha256:'+owner, 'startTime': start_ms, 'endTime': end_ms}, 'response': []})
    result = {'experiment_id': active['experiment_id'], 'seed_at_utc': active['seed_at_utc'],
            'experiment_sha256': active['experiment_sha256'],
            'paper': {'observed_at_utc': observed.isoformat(), 'opening_equity': 6000., 'common_mark_equity': 6000.+edge},
            'actual': {'completed_at_utc': completed.isoformat(), 'common_mark_equity': 6000., 'accounts': accounts},
            'common_marks': {'observed_at_utc': (observed+timedelta(seconds=1)).isoformat()},
            'comparison': {'performance_comparable': True, 'mirror_baseline_verified': True,
                'zero_external_flows_verified': True, 'paper_beating_actual': edge>0,
                'observation_skew_seconds': 10., 'common_mark_equity_edge': edge, 'excess_return_fraction': edge/6000.},
            'source_reads': reads}
    if active.get('win_forecast_rule'):
        result['paper']['forecast_evidence'] = eligible_forecast_evidence(active, observed.isoformat())
    return result


def eligible_forecast_evidence(active, endpoint):
    from ml import hyperliquid_paper_forecast_evidence as proof
    seed = module.stamp(active['seed_at_utc'])
    close = seed + timedelta(minutes=5)
    created, observed = close + timedelta(seconds=5), close + timedelta(seconds=10)
    model_id, data_id = '20260929T020000Z-abcdef12', '20260929T020500Z-1234abcd'
    prediction = {'prediction_id': 'qualified-hold', 'coin': 'BTC', 'interval': '5m', 'horizon_bars': 1,
                  'qualified': True, 'role': 'active', 'model_id': model_id, 'data_run_id': data_id,
                  'p_not_down': .51, 'p_down': .49, 'created_at_utc': created.isoformat(),
                  'decision_close_utc': close.isoformat(), 'target_close_utc': (close+timedelta(minutes=5)).isoformat(),
                  '_valid_until_epoch': (close+timedelta(minutes=5)).timestamp()}
    detail = {'coin': 'BTC', 'forecast_id': prediction['prediction_id'], 'model_id': model_id,
              'data_run_id': data_id, 'qualified': True, 'forecast_created_at_utc': created.isoformat(),
              'p_not_down': .51, 'action': 'hold', 'reason': 'neutral_signal',
              'forecast_observation': {'schema_version': 1, 'prediction': prediction,
                  'observed_at_utc': observed.isoformat(), 'sigma': .01,
                  'max_forecast_age_seconds': 300, 'max_model_age_seconds': 86400}}
    row = {'decision_id': 'decision-1', 'cycle_id': 'forecast:qualified-hold',
           'timestamp_utc': observed.isoformat(), 'committed_at_utc': observed.isoformat(),
           'coin': 'BTC', 'forecast_id': prediction['prediction_id'], 'model_id': model_id,
           'details_json': json.dumps(detail)}
    model = json.dumps({'coin': 'BTC', 'interval': '5m', 'horizon_bars': 1,
                        'model_id': model_id, 'trained_at_utc': seed.isoformat(), 'eligible': True})
    return {'schema_version': 1, 'rule': proof.WIN_FORECAST_RULE,
            'experiment_id': active['experiment_id'], 'seed_at_utc': active['seed_at_utc'],
            'endpoint_at_utc': endpoint, 'valid_qualified_forecast_count': 1,
            'first_valid_witness': {'decision': row, 'decision_sha256': proof._hash(row['details_json']),
                                    'model_record_json': model, 'model_record_sha256': proof._hash(model)}}


def save_comparison(cadence, value, name='closing.json'):
    path=cadence.operations/name
    write(path,value)
    return path


def test_bootstrap_is_unscored_preserves_live_opening_and_is_idempotent(cadence):
    before=(cadence.root/'_paper/opening_snapshot.json').read_bytes()
    first=cadence.status();second=cadence.init()
    assert first==second and first['active']['carry_in_unscored'] is True
    assert first['cadence_rule_version'] == first['active']['cadence_rule_version'] == module.LATEST_RULE
    assert first['next_due_at_utc']=='2026-09-29T04:00:00+00:00'
    assert before==(cadence.root/'_paper/opening_snapshot.json').read_bytes()
    decision=module.classify(first['active'],comparison(first['active'],offset=3600))
    assert decision['outcome']=='carry_in_unscored' and decision['next_evaluation_hours']==2


@pytest.mark.parametrize('duration,edge,outcome,next_hours', [(2,10,'win',3),(3,10,'win',4),
    (4,-10,'loss',3),(3,-10,'loss',2),(2,-10,'loss',2),
    (4,0,'tie',4),(2,0,'tie',2),(8,-1,'loss',7)])
def test_one_step_forward_one_back_two_hour_floor_ladder(cadence,duration,edge,outcome,next_hours):
    active=scored_active(cadence,duration)
    result=module.classify(active,comparison(active,edge=edge))
    assert result['outcome']==outcome and result['next_evaluation_hours']==next_hours


@pytest.mark.parametrize('duration,edge,next_duration', [(4,-10,2),(3,-10,1),(2,-10,1),(1,-10,1),(1,10,2)])
def test_historical_v2_ladder_remains_immutable(cadence,duration,edge,next_duration):
    active = scored_active(cadence, 2).copy()
    active['cadence_rule_version'] = module.CURRENT_RULE
    active['evaluation_hours'] = duration
    active['due_at_utc'] = (module.stamp(active['seed_at_utc']) + timedelta(hours=duration)).isoformat()
    assert module.classify(active, comparison(active, edge=edge))['next_evaluation_hours'] == next_duration


def test_two_hour_floor_rule_rejects_one_hour_active(cadence):
    active = scored_active(cadence, 2).copy()
    active['evaluation_hours'] = 1
    active['due_at_utc'] = (module.stamp(active['seed_at_utc']) + timedelta(hours=1)).isoformat()
    with pytest.raises(ValueError, match='Two-hour-floor cadence'):
        module.classify(active, comparison(active, edge=-1))


@pytest.mark.parametrize('problem', ['missing', 'zero', 'unqualified', 'nan', 'bool_probability',
    'stale', 'future', 'before_seed', 'after_endpoint', 'wrong_recipe', 'ineligible_model',
    'wrong_model', 'model_published_later', 'model_digest', 'decision_digest', 'uncommitted',
    'prediction_identity', 'wrong_round', 'string_count', 'target_horizon'])
def test_positive_edge_requires_valid_qualified_in_round_forecast(cadence, problem):
    from ml import hyperliquid_paper_forecast_evidence as proof
    active = scored_active(cadence, 2)
    active['win_forecast_rule'] = proof.WIN_FORECAST_RULE
    value = comparison(active)
    evidence = value['paper']['forecast_evidence']
    witness = evidence['first_valid_witness']
    row = witness['decision']
    detail = json.loads(row['details_json'])
    prediction = detail['forecast_observation']['prediction']
    model = json.loads(witness['model_record_json'])
    if problem == 'missing': del value['paper']['forecast_evidence']
    elif problem == 'zero': evidence['valid_qualified_forecast_count'] = 0
    elif problem == 'unqualified': prediction['qualified'] = False
    elif problem == 'nan': prediction['p_not_down'] = float('nan')
    elif problem == 'bool_probability': prediction['p_not_down'] = True
    elif problem == 'stale':
        row['timestamp_utc'] = row['committed_at_utc'] = prediction['target_close_utc']
    elif problem == 'future': prediction['created_at_utc'] = value['paper']['observed_at_utc']
    elif problem == 'before_seed': prediction['created_at_utc'] = (module.stamp(active['seed_at_utc'])-timedelta(seconds=1)).isoformat()
    elif problem == 'after_endpoint': row['timestamp_utc'] = row['committed_at_utc'] = (module.stamp(value['paper']['observed_at_utc'])+timedelta(seconds=1)).isoformat()
    elif problem == 'wrong_recipe': active['recipe']['symbols'] = ['ETH']
    elif problem == 'ineligible_model': model['eligible'] = False
    elif problem == 'wrong_model': model['model_id'] = '20260929T020000Z-12345678'
    elif problem == 'model_published_later': model['trained_at_utc'] = value['paper']['observed_at_utc']
    elif problem == 'uncommitted': row['committed_at_utc'] = active['seed_at_utc']
    elif problem == 'prediction_identity': row['forecast_id'] = 'another-prediction'
    elif problem == 'wrong_round': evidence['experiment_id'] = 'another-round'
    elif problem == 'string_count': evidence['valid_qualified_forecast_count'] = '1'
    elif problem == 'target_horizon': prediction['target_close_utc'] = value['paper']['observed_at_utc']
    row['details_json'] = json.dumps(detail)
    witness['decision_sha256'] = proof._hash(row['details_json']) if problem != 'decision_digest' else '0'*64
    witness['model_record_json'] = json.dumps(model)
    witness['model_record_sha256'] = proof._hash(witness['model_record_json']) if problem != 'model_digest' else '0'*64
    result = module.classify(active, value)
    assert result['outcome'] == 'unavailable' and result['winning'] is False
    assert result['reason'] == 'no_eligible_forecast_evidence' and result['next_evaluation_hours'] == 2
    assert result['common_mark_equity_edge'] == 10


def test_valid_qualified_abstention_allows_win_but_loss_and_tie_do_not_require_it(cadence):
    from ml import hyperliquid_paper_forecast_evidence as proof
    active = scored_active(cadence, 4)
    active['win_forecast_rule'] = proof.WIN_FORECAST_RULE
    value = comparison(active)
    assert module.classify(active, value)['outcome'] == 'win'
    for edge, outcome, duration in [(-10, 'loss', 3), (0, 'tie', 4)]:
        value = comparison(active, edge=edge)
        del value['paper']['forecast_evidence']
        result = module.classify(active, value)
        assert result['outcome'] == outcome and result['next_evaluation_hours'] == duration


def test_forecast_collector_verifies_model_source_and_deduplicates_account_decisions(cadence, tmp_path):
    from ml import hyperliquid_paper_forecast_evidence as proof
    active = scored_active(cadence)
    endpoint = comparison(active)['paper']['observed_at_utc']
    evidence = eligible_forecast_evidence(active, endpoint)
    witness = evidence['first_valid_witness']
    row = witness['decision']
    source = tmp_path / '_models/BTC/5m/h1/runs/20260929T020000Z-abcdef12/record.json'
    source.parent.mkdir(parents=True)
    source.write_text(witness['model_record_json'])
    before = source.read_bytes()
    duplicate = {**row, 'decision_id': 'second-account'}
    collected = proof.collect_forecast_evidence(tmp_path, active, active['seed_at_utc'], endpoint, [row, duplicate])
    assert collected['valid_qualified_forecast_count'] == 1 and collected['qualified_decision_count'] == 2
    assert proof.has_eligible_forecast(collected, active, endpoint)
    assert source.read_bytes() == before
    source.unlink()
    # Retained proof remains independently checkable after source archival.
    assert proof.has_eligible_forecast(collected, active, endpoint)
    missing = proof.collect_forecast_evidence(tmp_path, active, active['seed_at_utc'], endpoint, [row])
    assert missing['valid_qualified_forecast_count'] == 0 and missing['invalid_qualified_decision_count'] == 1
    assert not source.exists()
    for malformed in ('[]', 'null', '"invalid"', '{"qualified":true,"forecast_observation":[]}'):
        invalid = proof.collect_forecast_evidence(tmp_path, active, active['seed_at_utc'], endpoint,
                                                 [{**row, 'details_json': malformed}])
        assert invalid['valid_qualified_forecast_count'] == 0


def test_forecast_rule_is_prospective_and_historical_positive_assessment_stays_idempotent(cadence):
    active = scored_active(cadence)
    assert 'win_forecast_rule' not in active
    path = save_comparison(cadence, comparison(active))
    first = cadence.assess(path)
    assert first == cadence.assess(path)
    native_baseline(cadence.root, 'run-2', '2026-09-29T04:10:00+00:00', supersedes='run-1')
    result = cadence.advance()
    assert result['active']['win_forecast_rule'] == module.WIN_FORECAST_RULE


def test_native_read_only_comparison_collects_only_committed_endpoint_bounded_decisions(cadence):
    import sqlite3
    from ml.hyperliquid_paper_comparison import read_paper_snapshot
    active = scored_active(cadence)
    endpoint = comparison(active)['paper']['observed_at_utc']
    witness = eligible_forecast_evidence(active, endpoint)['first_valid_witness']
    row = witness['decision']
    source = cadence.root / '_models/BTC/5m/h1/runs/20260929T020000Z-abcdef12/record.json'
    source.parent.mkdir(parents=True)
    source.write_text(witness['model_record_json'])
    with sqlite3.connect(cadence.root / '_paper/ledger.sqlite3') as connection:
        opening = json.loads(connection.execute("SELECT result_json FROM cycles WHERE cycle_id='opening'").fetchone()[0])
        for identity, timestamp in [(row['cycle_id'], row['timestamp_utc']), ('endpoint', endpoint)]:
            connection.execute('INSERT INTO cycles VALUES (?,?,?)',
                               (identity, timestamp, json.dumps({**opening, 'cycle_id': identity, 'timestamp_utc': timestamp})))
        for identity, cycle_id, timestamp in [('valid', row['cycle_id'], row['timestamp_utc']),
                                             ('orphan', 'missing-cycle', row['timestamp_utc']),
                                             ('future', row['cycle_id'], '2026-09-30T00:00:00+00:00')]:
            connection.execute('INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?)',
                (identity, cycle_id, timestamp, 'alex', 'BTC', 'hold', 'neutral_signal',
                 row['forecast_id'], row['model_id'], row['details_json']))
    before = (cadence.root / '_paper/ledger.sqlite3').read_bytes()
    snapshot = read_paper_snapshot(cadence.root)
    evidence = snapshot['forecast_evidence']
    assert evidence['committed_decision_count'] == evidence['valid_qualified_forecast_count'] == 1
    assert evidence['first_valid_witness']['decision']['decision_id'] == 'valid'
    assert (cadence.root / '_paper/ledger.sqlite3').read_bytes() == before


@pytest.mark.parametrize('change', ['flows','capped','owner','flow_interval','source_event','flag_string',
    'edge_nan','edge_bool','excess_mismatch','baseline_mismatch','winning_flag','missing_account'])
def test_unreliable_comparisons_never_earn_duration_credit(cadence,change):
    active=scored_active(cadence,3);value=comparison(active)
    if change=='flows':value['comparison']['zero_external_flows_verified']=False
    elif change=='capped':value['actual']['accounts']['alex']['flows']['range_complete']=False
    elif change=='owner':value['source_reads'][0]['request']['user']='wallet-sha256:'+'f'*64
    elif change=='flow_interval':value['actual']['accounts']['alex']['flows']['start_time_ms']+=1
    elif change=='source_event':value['source_reads'][0]['response']=[{'delta':{'type':'deposit'}}]
    elif change=='flag_string':value['comparison']['performance_comparable']='true'
    elif change=='edge_nan':value['comparison']['common_mark_equity_edge']=float('nan')
    elif change=='edge_bool':value['comparison']['common_mark_equity_edge']=True
    elif change=='excess_mismatch':value['comparison']['excess_return_fraction']=.7
    elif change=='baseline_mismatch':value['paper']['opening_equity']=7000
    elif change=='winning_flag':value['comparison']['paper_beating_actual']=False
    else:del value['actual']['accounts']['alex']
    result=module.classify(active,value)
    assert result['outcome']=='unavailable' and result['winning'] is False and result['next_evaluation_hours']==3


def test_early_wake_does_not_finalize_and_late_run_is_not_called_a_two_hour_win(cadence):
    active=scored_active(cadence)
    path=save_comparison(cadence,comparison(active,offset=-1))
    before=cadence.state_path.read_bytes()
    result=cadence.assess(path)
    assert result['decision']['outcome']=='early' and result['decision']['finalizable'] is False
    assert cadence.state_path.read_bytes()==before and not cadence.receipts.exists()
    result=module.classify(active,comparison(active,offset=291))
    assert result['outcome']=='late_unscored' and result['next_evaluation_hours']==2
    assert module.classify(active,comparison(active,offset=290))['outcome']=='win'


@pytest.mark.parametrize('endpoint', [None, 'bad-timestamp', '2026-09-29T04:00:00', 1])
def test_invalid_paper_endpoint_cannot_finalize_unavailable_before_due(cadence,endpoint):
    active=scored_active(cadence);value=comparison(active,offset=-60)
    value['paper']['observed_at_utc']=endpoint
    path=save_comparison(cadence,value);before=cadence.state_path.read_bytes()
    with pytest.raises(ValueError,match='valid committed Paper endpoint'):
        cadence.assess(path)
    assert cadence.state_path.read_bytes()==before and not cadence.receipts.exists()


def test_registry_exclusion_is_checked_before_baseline_ledger_read(cadence,monkeypatch):
    write(cadence.operations/'excluded-paper-runs.json',{'excluded_runs':[{'experiment_id':'run-1'}]})
    def forbidden(*args,**kwargs):raise AssertionError('Excluded ledger was opened')
    monkeypatch.setattr(module,'seed_info',forbidden)
    with pytest.raises(ValueError,match='exclusion registry'):
        cadence.baseline()


@pytest.mark.parametrize('field', ['experiment_id','seed_at_utc','experiment_sha256'])
def test_different_experiment_identity_is_an_error_not_a_loss(cadence,field):
    active=scored_active(cadence);value=comparison(active);value[field]='other'
    with pytest.raises(ValueError,match='different|provenance'):
        module.classify(active,value)


def test_advance_consumes_assessment_once_after_completed_successor(cadence):
    active=scored_active(cadence);path=save_comparison(cadence,comparison(active))
    first=cadence.assess(path);assert first==cadence.assess(path)
    assert first['current_evaluation_hours']==2 and first['planned_successor_evaluation_hours']==3
    with pytest.raises(ValueError,match='directly supersede'):
        cadence.advance()
    native_baseline(cadence.root,'run-2','2026-09-29T04:10:00+00:00',supersedes='run-1')
    advanced=cadence.advance()
    assert advanced==cadence.advance()
    assert advanced['current_evaluation_hours']==3 and advanced['next_due_at_utc']=='2026-09-29T07:10:00+00:00'
    assert advanced['schedule_reanchor_deadline_at_utc']=='2026-09-29T04:13:00+00:00'
    assert advanced['active']['carry_in_unscored'] is False
    assert len(cadence.state()['history'])==1


def test_receipt_first_interruption_recovers_without_reclassifying_or_double_credit(cadence,monkeypatch):
    active=scored_active(cadence);path=save_comparison(cadence,comparison(active))
    original=module.write_json
    def interrupted(path,value,**kwargs):
        if path==cadence.state_path:raise OSError('interrupted state write')
        return original(path,value,**kwargs)
    monkeypatch.setattr(module,'write_json',interrupted)
    with pytest.raises(OSError,match='interrupted'):cadence.assess(path)
    receipt=next(cadence.receipts.glob('*.json'));before=receipt.read_bytes()
    monkeypatch.setattr(module,'write_json',original)
    cadence.assess(path)
    assert receipt.read_bytes()==before and cadence.state()['pending_assessment']['sha256']==module.file_digest(receipt)


@pytest.mark.parametrize('problem', ['wrong_parent','incomplete','opening_changed','comparison_changed','receipt_changed'])
def test_incomplete_or_tampered_successor_cannot_consume_assessment(cadence,problem):
    active=scored_active(cadence);path=save_comparison(cadence,comparison(active));cadence.assess(path)
    native_baseline(cadence.root,'run-2','2026-09-29T04:10:00+00:00',supersedes='other' if problem=='wrong_parent' else 'run-1')
    if problem=='incomplete':write(cadence.operations/'paper-maintenance.json',{'status':'in_progress','cycle_id':'run-2'})
    elif problem=='opening_changed':
        snapshot=cadence.root/'_paper/opening_snapshot.json';value=review.read_json(snapshot);value['cash']['alex']+=1;write(snapshot,value)
    elif problem=='comparison_changed':write(path,comparison(active,edge=20))
    elif problem=='receipt_changed':
        receipt=next(cadence.receipts.glob('*.json'));value=review.read_json(receipt);value['decision']['next_evaluation_hours']=9;write(receipt,value)
    before=cadence.state_path.read_bytes()
    with pytest.raises(ValueError):cadence.advance()
    assert cadence.state_path.read_bytes()==before


def test_a_second_comparison_cannot_rewrite_an_ending_result(cadence):
    active=scored_active(cadence);cadence.assess(save_comparison(cadence,comparison(active,edge=-1)))
    with pytest.raises(ValueError,match='immutable assessment'):
        cadence.assess(save_comparison(cadence,comparison(active,edge=1),'second.json'))


def test_lock_external_path_and_invalid_duration_are_rejected(cadence,tmp_path):
    with FileLock(str(cadence.operations/'.paper-cadence.lock'),timeout=0):
        with pytest.raises(Timeout):cadence.init()
    with pytest.raises(ValueError,match='within the datastore'):
        cadence.assess(tmp_path.parent/'outside.json')
    state=cadence.state();state['active']['evaluation_hours']=True;write(cadence.state_path,state)
    with pytest.raises(ValueError,match='integer'):cadence.status()


def legacy_state(cadence):
    state = cadence.state()
    state.pop('cadence_rule_version')
    state['active'].pop('cadence_rule_version')
    state['policy'] = module.policy(module.LEGACY_RULE)
    write(cadence.state_path, state)
    return state


@pytest.fixture
def consumed_legacy_loss(cadence, monkeypatch):
    set_rule(cadence, module.CURRENT_RULE)
    scored_active(cadence, 4)
    active = legacy_state(cadence)['active']
    path = save_comparison(cadence, comparison(active, edge=-10))
    assert cadence.assess(path)['decision']['next_evaluation_hours'] == 4
    native_baseline(cadence.root, 'run-2', '2026-09-29T06:10:00+00:00', supersedes='run-1')
    cadence.advance()
    monkeypatch.setattr(module, 'utc', lambda: '2026-09-29T06:30:00+00:00')
    return cadence


def adopt(cadence, change_id='rule-change', ending='run-1', active='run-2'):
    return cadence.adopt_loss_penalty(change_id, ending, active)


def test_legacy_immutable_classifications_and_pending_receipts_keep_old_meaning(cadence):
    active = scored_active(cadence, 4)
    active.pop('cadence_rule_version')
    assert module.classify(active, comparison(active, edge=-10))['next_evaluation_hours'] == 4
    legacy_state(cadence)
    path = save_comparison(cadence, comparison(active, edge=-10))
    result = cadence.assess(path)
    immutable = next(cadence.receipts.glob('*.json'))
    before = immutable.read_bytes()
    assert result == cadence.assess(path)
    assert immutable.read_bytes() == before
    assert result['cadence_rule_version'] == module.LEGACY_RULE
    assert result['planned_successor_evaluation_hours'] == 4
    native_baseline(cadence.root, 'run-2', '2026-09-29T06:10:00+00:00', supersedes='run-1')
    result = cadence.advance()
    assert result['current_evaluation_hours'] == 4
    assert result['cadence_rule_version'] == module.LEGACY_RULE


def test_adoption_preserves_history_opening_configs_and_consumed_receipts(consumed_legacy_loss, monkeypatch):
    cadence = consumed_legacy_loss
    before = cadence.state()
    files = {path: path.read_bytes() for path in cadence.root.rglob('*') if path.is_file()
             and path != cadence.state_path and not path.name.endswith('.lock')}
    result = adopt(cadence)
    after = cadence.state()
    assert after['history'] == before['history']
    assert after['active']['seed_at_utc'] == before['active']['seed_at_utc']
    assert after['active']['immutable_opening_hashes'] == before['active']['immutable_opening_hashes']
    assert result['current_evaluation_hours'] == 2
    assert result['next_due_at_utc'] == '2026-09-29T08:10:00+00:00'
    assert result['cadence_rule_version'] == module.CURRENT_RULE
    assert all(path.read_bytes() == data for path, data in files.items())
    receipt = review.read_json(result['policy_amendment']['path'])
    assert receipt['before_state'] == before
    assert receipt['after_active'] == after['active']
    committed = cadence.state_path.read_bytes()
    assert adopt(cadence) == result
    assert cadence.state_path.read_bytes() == committed
    assert len(list(cadence.amendments.glob('*.json'))) == 1
    with pytest.raises(ValueError, match='another change ID'):
        adopt(cadence, change_id='second-change')
    # Successful idempotent retry is allowed after due; no new deadline is set.
    monkeypatch.setattr(module, 'utc', lambda: '2026-09-29T08:15:00+00:00')
    assert adopt(cadence) == result
    assert cadence.state_path.read_bytes() == committed
    assert len(after['policy_amendments']) == 1


def test_adopted_policy_scores_and_advances_four_to_two_to_one(consumed_legacy_loss):
    cadence = consumed_legacy_loss
    adopt(cadence)
    active = cadence.state()['active']
    result = cadence.assess(save_comparison(cadence, comparison(active, edge=-10), 'new-closing.json'))
    assert result['planned_successor_evaluation_hours'] == 1
    native_baseline(cadence.root, 'run-3', '2026-09-29T08:20:00+00:00', supersedes='run-2')
    result = cadence.advance()
    assert result['current_evaluation_hours'] == 1
    assert result['next_due_at_utc'] == '2026-09-29T09:20:00+00:00'
    assert result['active']['cadence_rule_version'] == module.CURRENT_RULE
    active = result['active']
    assert module.classify(active, comparison(active, edge=-10))['next_evaluation_hours'] == 1
    assert module.classify(active, comparison(active, edge=10))['next_evaluation_hours'] == 2


def pending_v2_successor(cadence, edge=-10.):
    set_rule(cadence, module.CURRENT_RULE)
    active = scored_active(cadence, 1)
    assessment = cadence.assess(save_comparison(cadence, comparison(active, edge=edge)))
    assessment_path = cadence.path(assessment['pending_assessment']['path'])
    native_baseline(cadence.root, 'run-2', '2026-09-29T04:10:00+00:00', supersedes='run-1')
    return assessment, assessment_path


def test_advance_adopts_two_hour_floor_prospectively_and_idempotently(cadence, monkeypatch):
    assessment, assessment_path = pending_v2_successor(cadence)
    before_assessment = assessment_path.read_bytes()
    monkeypatch.setattr(module, 'utc', lambda: '2026-09-29T04:11:00+00:00')
    result = cadence.advance(change_id='two-hour-floor-20261003', expected_ending_id='run-1',
                             expected_active_id='run-2')
    assert result['policy_amendment']
    assert result['cadence_rule_version'] == module.TWO_HOUR_FLOOR_RULE
    assert result['current_evaluation_hours'] == 2
    assert result['active']['seed_at_utc'] == '2026-09-29T04:10:00+00:00'
    assert result['next_due_at_utc'] == '2026-09-29T06:10:00+00:00'
    assert cadence.state()['history'][-1]['decision'] == assessment['decision']
    assert assessment_path.read_bytes() == before_assessment
    assert module.classify(result['active'], comparison(result['active'], edge=-10))['next_evaluation_hours'] == 2
    assert module.classify(result['active'], comparison(result['active'], edge=10))['next_evaluation_hours'] == 3
    assert result == cadence.advance(change_id='two-hour-floor-20261003', expected_ending_id='run-1',
                                     expected_active_id='run-2')


@pytest.mark.parametrize('ending,active', [('other', 'run-2'), ('run-1', 'other')])
def test_two_hour_floor_transition_rejects_wrong_identity_without_writes(cadence, monkeypatch, ending, active):
    pending_v2_successor(cadence)
    monkeypatch.setattr(module, 'utc', lambda: '2026-09-29T04:11:00+00:00')
    before = cadence.state_path.read_bytes()
    with pytest.raises(ValueError, match='Expected identities'):
        cadence.advance(change_id='two-hour-floor-20261003', expected_ending_id=ending, expected_active_id=active)
    assert cadence.state_path.read_bytes() == before and not cadence.amendments.exists()


def test_two_hour_floor_transition_rejects_a_late_successor(cadence, monkeypatch):
    pending_v2_successor(cadence)
    monkeypatch.setattr(module, 'utc', lambda: '2026-09-29T05:10:00+00:00')
    before = cadence.state_path.read_bytes()
    with pytest.raises(ValueError, match='Existing successor deadline is already due'):
        cadence.advance(change_id='two-hour-floor-20261003', expected_ending_id='run-1', expected_active_id='run-2')
    assert cadence.state_path.read_bytes() == before and not cadence.amendments.exists()


def test_two_hour_floor_transition_honors_paper_review_owner(cadence, monkeypatch):
    pending_v2_successor(cadence)
    monkeypatch.setattr(module, 'utc', lambda: '2026-09-29T04:11:00+00:00')
    before = cadence.state_path.read_bytes()
    with FileLock(str(cadence.operations / '.paper-review.lock'), timeout=0):
        with pytest.raises(Timeout):
            cadence.advance(change_id='two-hour-floor-20261003', expected_ending_id='run-1', expected_active_id='run-2')
    assert cadence.state_path.read_bytes() == before and not cadence.amendments.exists()


def test_two_hour_floor_transition_recovers_receipt_first_interruption(cadence, monkeypatch):
    pending_v2_successor(cadence)
    monkeypatch.setattr(module, 'utc', lambda: '2026-09-29T04:11:00+00:00')
    before = cadence.state_path.read_bytes()
    original = module.write_json

    def interrupted(path, value, **kwargs):
        if path == cadence.state_path:
            raise OSError('interrupted')
        return original(path, value, **kwargs)

    monkeypatch.setattr(module, 'write_json', interrupted)
    with pytest.raises(OSError, match='interrupted'):
        cadence.advance(change_id='two-hour-floor-20261003', expected_ending_id='run-1', expected_active_id='run-2')
    receipt = next(cadence.amendments.glob('*.json'))
    immutable = receipt.read_bytes()
    assert cadence.state_path.read_bytes() == before
    monkeypatch.setattr(module, 'write_json', original)
    monkeypatch.setattr(module, 'utc', lambda: '2026-09-29T05:11:00+00:00')
    result = cadence.advance(change_id='two-hour-floor-20261003', expected_ending_id='run-1', expected_active_id='run-2')
    assert result['current_evaluation_hours'] == 2 and receipt.read_bytes() == immutable


def test_two_hour_floor_transition_receipt_tampering_is_detected(cadence, monkeypatch):
    pending_v2_successor(cadence)
    monkeypatch.setattr(module, 'utc', lambda: '2026-09-29T04:11:00+00:00')
    result = cadence.advance(change_id='two-hour-floor-20261003', expected_ending_id='run-1', expected_active_id='run-2')
    path = cadence.path(result['policy_amendment']['path'])
    receipt = review.read_json(path)
    receipt['after_active']['evaluation_hours'] = 1
    write(path, receipt)
    with pytest.raises(ValueError, match='amendment changed'):
        cadence.status()


def test_two_hour_floor_transition_preserves_historical_v2_amendment(consumed_legacy_loss, monkeypatch):
    cadence = consumed_legacy_loss
    v2 = adopt(cadence)
    original_reference = v2['policy_amendment']
    original_receipt = cadence.path(original_reference['path']).read_bytes()
    active = cadence.state()['active']
    assessment = cadence.assess(save_comparison(cadence, comparison(active, edge=-10), 'v2-closing.json'))
    native_baseline(cadence.root, 'run-3', '2026-09-29T08:20:00+00:00', supersedes='run-2')
    monkeypatch.setattr(module, 'utc', lambda: '2026-09-29T08:21:00+00:00')
    result = cadence.advance(change_id='two-hour-floor-20261003', expected_ending_id='run-2', expected_active_id='run-3')
    state = cadence.state()
    assert result['cadence_rule_version'] == module.TWO_HOUR_FLOOR_RULE
    assert state['policy_amendments'][0] == original_reference
    assert cadence.path(original_reference['path']).read_bytes() == original_receipt
    assert len(state['policy_amendments']) == 2
    assert state['history'][-1]['decision'] == assessment['decision']


def test_policy_amendment_receipt_first_interruption_recovers_once(consumed_legacy_loss, monkeypatch):
    cadence = consumed_legacy_loss
    before = cadence.state_path.read_bytes()
    original = module.write_json
    def interrupted(path, value, **kwargs):
        if path == cadence.state_path:
            raise OSError('interrupted policy state write')
        return original(path, value, **kwargs)
    monkeypatch.setattr(module, 'write_json', interrupted)
    with pytest.raises(OSError, match='interrupted'):
        adopt(cadence)
    receipt = next(cadence.amendments.glob('*.json'))
    immutable = receipt.read_bytes()
    assert cadence.state_path.read_bytes() == before
    monkeypatch.setattr(module, 'write_json', original)
    monkeypatch.setattr(module, 'utc', lambda: '2026-09-29T06:35:00+00:00')
    result = adopt(cadence)
    assert result['current_evaluation_hours'] == 2
    assert receipt.read_bytes() == immutable
    assert len(cadence.state()['history']) == 1
    assert result == adopt(cadence)


@pytest.mark.parametrize('problem', ['pending', 'maintenance', 'wrong_active', 'wrong_ending',
    'active_provenance', 'assessment', 'comparison', 'history', 'opening', 'config', 'excluded'])
def test_adoption_rejects_unverified_or_conflicting_inputs_without_writes(consumed_legacy_loss, problem):
    cadence = consumed_legacy_loss
    state = cadence.state()
    ending, active = 'run-1', 'run-2'
    if problem == 'pending':
        state['pending_assessment'] = state['history'][-1]['assessment']
        write(cadence.state_path, state)
    elif problem == 'maintenance':
        write(cadence.operations / 'paper-maintenance.json', {'status': 'in_progress', 'cycle_id': 'another-owner'})
    elif problem == 'wrong_active': active = 'other'
    elif problem == 'wrong_ending': ending = 'other'
    elif problem == 'active_provenance':
        state['active']['opening_equity'] += 1
        write(cadence.state_path, state)
    elif problem == 'assessment':
        path = next(cadence.receipts.glob('*.json'))
        value = review.read_json(path); value['decision']['outcome'] = 'win'; write(path, value)
    elif problem == 'comparison':
        path = cadence.operations / 'closing.json'
        value = review.read_json(path); value['comparison']['common_mark_equity_edge'] = -11; write(path, value)
    elif problem == 'history':
        state['history'][-1]['decision']['next_evaluation_hours'] = 10
        write(cadence.state_path, state)
    elif problem == 'opening':
        path = cadence.root / '_paper/opening_snapshot.json'
        value = review.read_json(path); value['cash']['alex'] += 1; write(path, value)
    elif problem == 'config':
        path = cadence.operations / 'paper-improvement/run-2/accepted-source/configs/hyperliquid-models.json'
        value = review.read_json(path); value['horizons_bars'] = [4]; write(path, value)
    elif problem == 'excluded':
        write(cadence.operations / 'excluded-paper-runs.json', {'excluded_runs': [{'experiment_id': 'run-1'}]})
    before = cadence.state_path.read_bytes()
    with pytest.raises(ValueError):
        adopt(cadence, ending=ending, active=active)
    assert cadence.state_path.read_bytes() == before
    assert not cadence.amendments.exists()


@pytest.mark.parametrize('seconds', [0, 1])
def test_adoption_cannot_shorten_a_round_to_an_already_due_deadline(consumed_legacy_loss, monkeypatch, seconds):
    cadence = consumed_legacy_loss
    due = module.stamp('2026-09-29T08:10:00+00:00') + timedelta(seconds=seconds)
    monkeypatch.setattr(module, 'utc', lambda: due.isoformat())
    before = cadence.state_path.read_bytes()
    with pytest.raises(ValueError, match='already due'):
        adopt(cadence)
    assert cadence.state_path.read_bytes() == before and not cadence.amendments.exists()


@pytest.mark.parametrize('lock_name', ['.paper-cadence.lock', '.paper-review.lock'])
def test_policy_adoption_honors_both_owners(consumed_legacy_loss, lock_name):
    cadence = consumed_legacy_loss
    before = cadence.state_path.read_bytes()
    with FileLock(str(cadence.operations / lock_name), timeout=0):
        with pytest.raises(Timeout):
            adopt(cadence)
    assert cadence.state_path.read_bytes() == before and not cadence.amendments.exists()


def test_committed_policy_amendment_tampering_is_detected(consumed_legacy_loss):
    cadence = consumed_legacy_loss
    result = adopt(cadence)
    path = cadence.path(result['policy_amendment']['path'])
    receipt = review.read_json(path)
    receipt['after_active']['evaluation_hours'] = 1
    write(path, receipt)
    with pytest.raises(ValueError, match='amendment changed'):
        cadence.status()
    with pytest.raises(ValueError, match='amendment changed'):
        adopt(cadence)


def test_uncommitted_policy_amendment_tampering_blocks_recovery(consumed_legacy_loss, monkeypatch):
    cadence = consumed_legacy_loss
    original = module.write_json
    def interrupted(path, value, **kwargs):
        if path == cadence.state_path: raise OSError('interrupted')
        return original(path, value, **kwargs)
    monkeypatch.setattr(module, 'write_json', interrupted)
    with pytest.raises(OSError): adopt(cadence)
    monkeypatch.setattr(module, 'write_json', original)
    path = next(cadence.amendments.glob('*.json'))
    receipt = review.read_json(path); receipt['after_active']['opening_equity'] += 1; write(path, receipt)
    before = cadence.state_path.read_bytes()
    with pytest.raises(ValueError, match='does not match'):
        adopt(cadence)
    assert cadence.state_path.read_bytes() == before


def test_rehashed_false_assessment_decision_cannot_authorize_amendment(consumed_legacy_loss):
    cadence = consumed_legacy_loss
    state = cadence.state()
    reference = state['history'][-1]['assessment']
    receipt = review.read_json(reference['path'])
    receipt['decision']['common_mark_equity_edge'] = -11
    state['history'][-1]['decision'] = deepcopy(receipt['decision'])
    write(cadence.path(reference['path']), receipt)
    reference['sha256'] = module.file_digest(reference['path'])
    write(cadence.state_path, state)
    before = cadence.state_path.read_bytes()
    with pytest.raises(ValueError, match='verified consumed legacy LOSS'):
        adopt(cadence)
    assert cadence.state_path.read_bytes() == before and not cadence.amendments.exists()


@pytest.mark.parametrize('duration', [2, 4])
def test_ties_and_unscored_results_hold_under_two_hour_floor_rule(cadence, duration):
    active = scored_active(cadence, duration)
    assert module.classify(active, comparison(active, edge=0))['next_evaluation_hours'] == duration
    assert module.classify(active, comparison(active, offset=301))['next_evaluation_hours'] == duration
    value = comparison(active); value['comparison']['performance_comparable'] = False
    assert module.classify(active, value)['next_evaluation_hours'] == duration
    active['carry_in_unscored'] = True
    assert module.classify(active, comparison(active))['next_evaluation_hours'] == duration
