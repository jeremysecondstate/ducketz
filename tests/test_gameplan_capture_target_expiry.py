"""Saved targets may expire before a first read; no broker error is implied."""
from dataclasses import replace
from copy import deepcopy
from types import SimpleNamespace
import inspect,json
from pathlib import Path
import pandas as pd
import pytest
import requests

from ml.stock_trader import independent_runtime as runtime
from ml.stock_trader.contracts import canonical_sha256
from ml.stock_trader.session_reporting import session_cycle_report
from test_independent_stock_runtime import environment, _fixed_signals


@pytest.fixture(autouse=True)
def isolate_fixture_publications(monkeypatch):
    # Canonical and locally composed fixtures exercise the same capture guard.
    # No installed publications, bindings or account are test inputs.
    monkeypatch.setattr(runtime, '_loaded_gameplan_run', lambda *a, **k: None)
    monkeypatch.setattr(runtime, '_assert_execution_deployment', lambda *a, **k: None)
    monkeypatch.setattr(runtime, '_loaded_fallback_policy', lambda *a, **k: (None, None))


def stub_local_adoption(monkeypatch):
    # The optional installed account composition is absent from canonical main.
    if (Path(runtime.__file__).resolve().parents[1] / 'joint_capital_adoption.py').is_file():
        monkeypatch.setattr('ml.joint_capital_adoption.assert_accepted_execution', lambda *a, **k: None)


def expire_during_selection(env, monkeypatch, *, policy='gameplan-direction-current-market-v1', mixed=False):
    original = dict(_fixed_signals())
    deadline=env.now+pd.Timedelta(seconds=1)
    env.signals={key:replace(signal,actionable_until=(deadline+pd.Timedelta(minutes=1) if mixed and key[1]!='1h' else deadline).isoformat()) for key,signal in original.items()}
    frozen={key:signal for key,signal in env.signals.items()}
    def load(*args,**kwargs):
        env.now=deadline
        return env.signals,()
    monkeypatch.setattr(runtime,'load_current_independent_gameplan_signals',load)
    return deadline,frozen


def run(env,**options):
    return runtime.run_independent_stock_trader_once(env.root,execute=True,session=env.broker,
        runtime_clock=lambda:env.now,session_managed=True,sizing_policy=options.pop('policy','gameplan-direction-current-market-v1'),
        **options)


def payload(result):
    return json.loads((result.run_directory/'decisions.json').read_bytes())


@pytest.mark.parametrize('mixed',[False,True])
def test_actual_expiry_before_first_read_is_normal_without_replaying_originals(environment,monkeypatch,mixed):
    env=environment;deadline,frozen=expire_during_selection(env,monkeypatch,mixed=mixed)
    result=run(env)
    assert result.status=='PREDICTION_TARGET_ELAPSED' and result.error is None
    assert not result.stopped_after_error and result.submitted_orders==0
    assert env.broker.calls==env.broker.submissions==env.broker.cancellations==[] and env.captures==0
    assert not (env.root/runtime.LEDGER_RELATIVE_PATH).exists() and env.signals==frozen
    saved=payload(result);proof=saved['prediction_handoff']['target_elapsed_before_broker_capture']
    assert proof['observed_at']==deadline.isoformat() and proof['stage']=='BEFORE_FIRST_BROKER_READ'
    assert len(proof['expired_signals'])==(7 if mixed else 28)
    assert len(proof['retained_signal_ids'])==(21 if mixed else 0)
    for row in proof['expired_signals']:
        original=frozen[row['symbol'],row['primary_horizon']]
        assert row['prediction_id']==original.prediction_id and row['actionable_until']==original.actionable_until
        assert row['target_window_start']==original.target_window_start and row['target_window_end']==original.target_window_end
    assert result.broker_state_capture['status']=='INTERRUPTED_PREDICTION_TARGET_ELAPSED'
    assert result.broker_state_capture['attempts']==1 and result.broker_state_capture['transient_failures']==0
    assert 'submission_stop' not in result.to_dict() and saved['decisions']==[]
    report=session_cycle_report(result.to_dict(),as_of=env.now,session_orders_submitted=0,session_cycles=1,
        session_failed_cycles=0,session_consecutive_failures=0)
    assert report['activity']=='TARGET ELAPSED'


def test_manual_stop_wins_even_when_target_also_expired(environment,monkeypatch):
    env=environment;expire_during_selection(env,monkeypatch)
    load=runtime.load_current_independent_gameplan_signals
    def stopped(*a,**k):
        result=load(*a,**k);env.active=False;return result
    monkeypatch.setattr(runtime,'load_current_independent_gameplan_signals',stopped)
    result=run(env)
    assert result.status=='HORIZON_BROKER_RECONCILIATION_UNAVAILABLE' and result.error=='ValueError: TRADER_INACTIVE_DURING_BROKER_CAPTURE'
    assert 'target_elapsed_before_broker_capture' not in payload(result)['prediction_handoff']
    assert not env.active and env.broker.calls==[]


def test_other_sizing_policy_preserves_original_expired_read_failure(environment,monkeypatch):
    env=environment;expire_during_selection(env,monkeypatch)
    result=run(env,policy='fixed-horizon-budget-v1')
    assert result.status=='HORIZON_BROKER_RECONCILIATION_UNAVAILABLE'
    assert result.error=='ValueError: BROKER_READ_EXECUTION_WINDOW_CLOSED' and env.broker.calls==[]


def test_broker_error_text_cannot_impersonate_local_cutoff(environment,monkeypatch):
    env=environment;env.signals=_fixed_signals()
    stub_local_adoption(monkeypatch)
    def broken():raise ValueError('BROKER_READ_EXECUTION_WINDOW_CLOSED')
    monkeypatch.setattr(env.broker,'stable_account_fingerprint',broken)
    result=run(env)
    assert result.status=='HORIZON_BROKER_RECONCILIATION_UNAVAILABLE'
    assert result.error=='ValueError: BROKER_READ_EXECUTION_WINDOW_CLOSED'
    assert 'target_elapsed_before_broker_capture' not in payload(result)['prediction_handoff']


def test_typed_broker_error_cannot_impersonate_callback_local_proof(environment,monkeypatch):
    env=environment;env.signals=_fixed_signals()
    stub_local_adoption(monkeypatch)
    impostor=runtime._BrokerReadTargetElapsed({'stage':'BEFORE_FIRST_BROKER_READ','fabricated':True})
    def broken():raise impostor
    monkeypatch.setattr(env.broker,'stable_account_fingerprint',broken)
    result=run(env)
    assert result.status=='HORIZON_BROKER_RECONCILIATION_UNAVAILABLE' and result.error
    assert 'target_elapsed_before_broker_capture' not in payload(result)['prediction_handoff']


@pytest.mark.parametrize('prior_failure',[True,False])
def test_prior_read_or_quote_refresh_not_reclassified_as_no_read_expiry(environment,monkeypatch,prior_failure):
    env=environment;env.signals=_fixed_signals();cutoff=env.now+pd.Timedelta(seconds=20)
    env.signals={k:replace(s,actionable_until=cutoff.isoformat())for k,s in env.signals.items()}
    stub_local_adoption(monkeypatch)
    original=runtime.capture_portfolio_state
    calls=[]
    def capture(*args,**kwargs):
        calls.append(env.now)
        if prior_failure:raise requests.ReadTimeout('real fixture read failure')
        result=original(*args,**{k:v for k,v in kwargs.items()if k in inspect.signature(original).parameters})
        return replace(result,quotes={s:replace(q,observed_at=None)for s,q in result.quotes.items()},source_fingerprint=canonical_sha256(result.source_fingerprint))
    monkeypatch.setattr(runtime,'capture_portfolio_state',capture)
    elapsed=[0.]
    def sleep(seconds):elapsed[0]+=seconds;env.now=cutoff
    result=run(env,broker_state_retry_max_seconds=120,broker_state_retry_sleep=sleep,broker_state_retry_clock=lambda:elapsed[0])
    assert result.status=='HORIZON_BROKER_RECONCILIATION_UNAVAILABLE' and result.error
    assert 'target_elapsed_before_broker_capture' not in payload(result)['prediction_handoff']
    assert len(calls)==1 and result.broker_state_capture['attempts']==2
    assert result.broker_state_capture['transient_failures']==int(prior_failure)
    assert env.broker.submissions==env.broker.cancellations==[]


@pytest.mark.parametrize('bad',['','NaT','bad-date'])
def test_malformed_target_cannot_supply_positive_expiry_proof(environment,monkeypatch,bad):
    env=environment;expire_during_selection(env,monkeypatch)
    first=next(iter(env.signals));env.signals[first]=replace(env.signals[first],target_window_end=bad)
    result=run(env)
    assert result.status=='HORIZON_BROKER_RECONCILIATION_UNAVAILABLE' and result.error
    assert 'target_elapsed_before_broker_capture' not in payload(result)['prediction_handoff']
    assert env.broker.calls==[]


def test_existing_binding_or_deployment_failure_wins_over_expired_signal(environment,monkeypatch):
    env=environment;expire_during_selection(env,monkeypatch)
    def fail(*args, **kwargs):raise ValueError('fixture account binding changed')
    has_local_binding = hasattr(runtime, '_assert_account_runtime_binding')
    guard = '_assert_account_runtime_binding' if has_local_binding else '_assert_execution_deployment'
    monkeypatch.setattr(runtime, guard, fail)
    result=run(env)
    assert result.status==('HORIZON_BROKER_RECONCILIATION_UNAVAILABLE' if has_local_binding
                          else 'INDEPENDENT_TARGET_PLAN_UNAVAILABLE')
    assert result.error=='ValueError: fixture account binding changed' and env.broker.calls==[]
    assert 'target_elapsed_before_broker_capture' not in payload(result)['prediction_handoff']


@pytest.mark.parametrize('prior_failure',[False,True])
def test_own_session_no_read_expiry_does_not_add_or_clear_failures(environment,monkeypatch,tmp_path,prior_failure):
    from ml.stock_trader import independent_session as worker
    env=environment;expire_during_selection(env,monkeypatch);result=run(env)
    terminal=result.to_dict()
    assert terminal['status']=='PREDICTION_TARGET_ELAPSED'
    payloads=([{'status':'HORIZON_BROKER_RECONCILIATION_UNAVAILABLE','error':'original failure',
                'submitted_orders':0,'broker_state_capture':{'status':'UNAVAILABLE'}}]if prior_failure else[])+[terminal]
    session_root=tmp_path/'session';status=session_root/'state/independent-stock-trader/session-status.json'
    attempts=[];saved=[];reports=[];clock=[pd.Timestamp('2026-09-08T11:01:00Z')]
    monkeypatch.setattr(worker,'_has_inventory',lambda path:True)
    monkeypatch.setattr(worker,'read_gameplan_stock_activation_intent',lambda root:SimpleNamespace(active=len(attempts)<len(payloads)))
    def runner(root,**kwargs):
        assert root==session_root.resolve() and kwargs['execute']is False and kwargs['session_managed']is True
        record=deepcopy(payloads[len(attempts)]);attempts.append(clock[0])
        return SimpleNamespace(submitted_orders=0,to_dict=lambda:record)
    def sleep(seconds):saved.append(json.loads(status.read_bytes()));clock[0]+=pd.Timedelta(seconds=seconds)
    worker.run_independent_stock_session(session_root,execute=False,runner=runner,clock=lambda:clock[0],
        sleep=sleep,reporter=reports.append,sizing_policy='gameplan-direction-current-market-v1')
    assert len(saved)==len(payloads) and len(attempts)==len(payloads)
    assert saved[-1]['failed_cycles']==saved[-1]['consecutive_failures']==int(prior_failure)
    assert ('ATTENTION'in reports[-1])==prior_failure
    assert saved[-1]['last_cycle']==terminal
    assert env.broker.calls==env.broker.submissions==[]
