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
    return {'experiment_id': active['experiment_id'], 'seed_at_utc': active['seed_at_utc'],
            'experiment_sha256': active['experiment_sha256'],
            'paper': {'observed_at_utc': observed.isoformat(), 'opening_equity': 6000., 'common_mark_equity': 6000.+edge},
            'actual': {'completed_at_utc': completed.isoformat(), 'common_mark_equity': 6000., 'accounts': accounts},
            'common_marks': {'observed_at_utc': (observed+timedelta(seconds=1)).isoformat()},
            'comparison': {'performance_comparable': True, 'mirror_baseline_verified': True,
                'zero_external_flows_verified': True, 'paper_beating_actual': edge>0,
                'observation_skew_seconds': 10., 'common_mark_equity_edge': edge, 'excess_return_fraction': edge/6000.},
            'source_reads': reads}


def save_comparison(cadence, value, name='closing.json'):
    path=cadence.operations/name
    write(path,value)
    return path


def test_bootstrap_is_unscored_preserves_live_opening_and_is_idempotent(cadence):
    before=(cadence.root/'_paper/opening_snapshot.json').read_bytes()
    first=cadence.status();second=cadence.init()
    assert first==second and first['active']['carry_in_unscored'] is True
    assert first['next_due_at_utc']=='2026-09-29T04:00:00+00:00'
    assert before==(cadence.root/'_paper/opening_snapshot.json').read_bytes()
    decision=module.classify(first['active'],comparison(first['active'],offset=3600))
    assert decision['outcome']=='carry_in_unscored' and decision['next_evaluation_hours']==2


@pytest.mark.parametrize('duration,edge,outcome,next_hours', [(2,10,'win',3),(3,10,'win',4),
    (3,-10,'loss',3),(4,0,'tie',4),(8,-1,'loss',8)])
def test_monotonic_duration_ladder(cadence,duration,edge,outcome,next_hours):
    active=scored_active(cadence,duration)
    result=module.classify(active,comparison(active,edge=edge))
    assert result['outcome']==outcome and result['next_evaluation_hours']==next_hours


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
