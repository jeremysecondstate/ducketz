import json
import sqlite3
from types import SimpleNamespace
import pytest

from datafetching.research_ownership import prepare_ownership
from ml.stock_trader.horizon_ledger import HorizonLedger, PortfolioEvidence


@pytest.fixture
def ownership_case(tmp_path):
    identity = 'a'*64
    path = tmp_path/'state/independent-stock-trader/holdings.sqlite3'
    ledger = HorizonLedger(path, identity)
    ledger.reconcile(PortfolioEvidence('old',identity,'2026-09-11T23:00:00+00:00',
        {'AAPL':2.5},{'AAPL':10},{'AAPL':0},'old-source'))
    snapshot = {'status':'OBSERVED','cash_status':'CASH_ONLY_BOUNDED','observed_at':'2026-09-14T10:00:00+00:00',
        'held_shares':{'AAPL':2.5,'CROX':3},'working_order_count':0,
        'quotes':{'AAPL':{'price_reference':10},'CROX':{'price_reference':20}},
        'ownership':{'safe_for_planning':False,'reason_codes':['SAVED_RECONCILIATION_SYMBOL_MISSING'],
                     'account_matches':True,'last_saved_reconciliation_ready':True}}
    return tmp_path,path,identity,snapshot


def test_records_observed_new_holdings_without_claiming_manual_inventory(ownership_case):
    root,path,identity,snapshot = ownership_case
    result = prepare_ownership(root,symbols=('AAPL','CROX'),plan_id='batch',output=root/'ownership.json',
        session=SimpleNamespace(stable_account_fingerprint=lambda:identity),snapshot_loader=lambda *a,**k:snapshot)
    assert result['recorded_observed_holdings'] and result['orders_placed']==0
    with sqlite3.connect(path) as db:
        payload,owned = db.execute('SELECT payload,owned FROM snapshots ORDER BY observed_at DESC LIMIT 1').fetchone()
        assert db.execute('SELECT COUNT(*) FROM allocations').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM reservations').fetchone()[0]==0
    assert json.loads(payload)['held_shares']=={'AAPL':'2.5','CROX':'3'}
    assert set(json.loads(payload)['symbol_budgets'].values())=={'0'}
    assert json.loads(owned)=={}


@pytest.mark.parametrize('problem',['discrepancy','working-order','account-mismatch','missing-holding'])
def test_does_not_bypass_existing_ownership_guards(ownership_case,problem):
    root,path,identity,snapshot = ownership_case
    if problem=='discrepancy':snapshot['ownership']['reason_codes'].append('UNEXPLAINED_SHARE_REDUCTION_SINCE_SAVED_RECONCILIATION')
    if problem=='working-order':snapshot['working_order_count']=1
    if problem=='account-mismatch':snapshot['ownership']['account_matches']=False
    if problem=='missing-holding':snapshot['held_shares'].pop('CROX')
    with pytest.raises(ValueError):
        prepare_ownership(root,symbols=('AAPL','CROX'),plan_id='batch',output=root/'ownership.json',
            session=SimpleNamespace(stable_account_fingerprint=lambda:identity),snapshot_loader=lambda *a,**k:snapshot)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0]==1
