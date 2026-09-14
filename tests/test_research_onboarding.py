from datetime import date
import json
from pathlib import Path
import pytest

from datafetching.history_scope import filter_dated_payload, policy_digest, read_history_policy, price_floor
from datafetching.research_onboarding import history_start, load_batch, activate_batch, effective_listing_date


def test_included_history_bounds_listing_provider_and_auction_imbalance():
    args=dict(listing='2006-02-08',available='2013-04-01T00:00:00Z',as_of=date(2026,9,14))
    assert history_start('ohlcv-1s',**args)=='2018-01-01'
    assert history_start('trades',**args)=='2025-09-14'
    assert history_start('imbalance',**args)=='2026-08-14'
    assert history_start('mbo',**args)=='2026-08-14'
    assert history_start('ohlcv-1d',**{**args,'listing':'2021-10-01'})=='2021-10-01'
    assert history_start('status',**{**args,'available':'2024-07-17'})=='2024-07-17'
    with pytest.raises(ValueError,match='before 2018'):
        history_start('ohlcv-1d',floor='2017-01-01',**args)


def test_calendar_month_boundaries_are_not_approximated_with_fixed_days():
    assert history_start('mbp-10',listing='2006-02-08',available='2018-05-01',as_of=date(2028,3,31))=='2028-02-29'


def test_current_ticker_boundary_excludes_spac_but_preserves_older_company_history():
    assert effective_listing_date('2021-01-04','2018-05-01','2021-10-01')=='2021-10-01'
    assert effective_listing_date('2006-02-08','2018-05-01','2018-05-01')=='2006-02-08'


def test_history_filter_preserves_current_snapshots_and_removes_older_periods():
    rows=[{'date':'2017-12-31','value':1},{'date':'2018-01-01','value':2}]
    assert filter_dated_payload(rows,'2018-01-01')==[rows[1]]
    assert filter_dated_payload({'historical':rows,'symbol':'CROX'},'2018-01-01')['historical']==[rows[1]]
    profile={'symbol':'CROX','ipoDate':'2006-02-08'}
    assert filter_dated_payload(profile,'2018-01-01')==profile


def test_persistent_history_policy_rejects_tampering_and_does_not_affect_existing_symbols(tmp_path):
    assert read_history_policy(tmp_path,'AAPL') is None
    p=tmp_path/'state/symbol-history-policy/IONQ.json';p.parent.mkdir(parents=True)
    payload={'symbol':'IONQ','history_floor':'2018-01-01','listing_date':'2021-10-01'}
    p.write_text(json.dumps({**payload,'sha256':policy_digest(payload)}))
    assert price_floor(read_history_policy(tmp_path,'IONQ')).date()==date(2021,10,1)
    tampered={**payload,'history_floor':'2010-01-01','sha256':policy_digest(payload)}
    p.write_text(json.dumps(tampered))
    with pytest.raises(ValueError,match='checksum'):read_history_policy(tmp_path,'IONQ')


def test_batch_checksum_rejects_changed_candidate_or_data_scope(tmp_path):
    p=tmp_path/'plan.json'
    p.write_text(json.dumps({'schema_version':'research-symbol-onboarding-v1','selected_symbols':['IONQ'],'plan_id':'wrong'}))
    with pytest.raises(ValueError,match='checksum'):load_batch(p)


def test_activation_refuses_partial_history_without_changing_watchlist(tmp_path,monkeypatch):
    import datafetching.research_onboarding as onboarding
    from ml.artifacts import file_checksum
    watchlist=tmp_path/'watchlist.txt';watchlist.write_text('AAPL\n')
    monkeypatch.setattr(onboarding,'REPOSITORY_WATCHLIST',watchlist)
    batch={'plan_id':'batch','previous_symbols':['AAPL'],'candidate_symbols':['AAPL','IONQ'],
           'selected_symbols':['IONQ'],'symbol_plan_ids':{'IONQ':'symbol-plan'},'research_lineage':{}}
    gameplan=tmp_path/'gameplan';gameplan.mkdir();(gameplan/'receipt.json').write_text('{}')
    validation={'plan_id':'batch','symbols':batch['candidate_symbols'],'gameplan_run':str(gameplan),
                'gameplan_receipt_sha256':file_checksum(gameplan/'receipt.json')}
    (tmp_path/'validation.json').write_text(json.dumps(validation))
    child=tmp_path/'IONQ';child.mkdir()
    (child/'progress.json').write_text(json.dumps({'plan_id':'symbol-plan','status':'FETCHING'}))
    monkeypatch.setattr(onboarding,'load_plan',lambda _: {'plan_id':'symbol-plan'})
    with pytest.raises(ValueError,match='incomplete'):activate_batch(tmp_path/'plan.json',batch)
    assert watchlist.read_text()=='AAPL\n'
    assert not (tmp_path/'activation.json').exists()


def _activation_fixture(tmp_path, monkeypatch):
    import datafetching.research_onboarding as onboarding
    from ml.artifacts import file_checksum
    previous = ['AAPL','AMZN','GOOG','MU','NVDA','SNDK','COST']
    selected = ['CROX','PATH','TWST','IONQ']
    watchlist = tmp_path/'watchlist.txt'
    watchlist.write_text('# Production symbols\n'+'\n'.join(previous)+'\n')
    monkeypatch.setattr(onboarding,'REPOSITORY_WATCHLIST',watchlist)
    batch = {'plan_id':'batch','datastore_root':str(tmp_path),'previous_symbols':previous,
             'candidate_symbols':previous+selected,'selected_symbols':selected,
             'symbol_plan_ids':{s:s+'-plan' for s in selected},'research_lineage':{'week':'2026-09-13'}}
    gameplan = tmp_path/'gameplan';gameplan.mkdir();(gameplan/'receipt.json').write_text('{}')
    pointers = {}
    for folder in ('nightly-gameplan-latest','stock-trader-model-latest','gameplan-trade-plan-latest'):
        pointer = tmp_path/'ml'/folder/'run.json'
        pointer.parent.mkdir(parents=True);pointer.write_text('{"run_path":"original"}')
        pointers[pointer.relative_to(tmp_path).as_posix()] = file_checksum(pointer)
    validation = {'plan_id':'batch','symbols':batch['candidate_symbols'],'gameplan_run':str(gameplan),
                  'gameplan_receipt_sha256':file_checksum(gameplan/'receipt.json'),'current_pointer_hashes':pointers}
    (tmp_path/'validation.json').write_text(json.dumps(validation))
    for symbol in selected:
        child = tmp_path/symbol;child.mkdir()
        for name in ('progress.json','corporate-history.json','operational-history.json','secondary-history-quality.json'):
            (child/name).write_text(json.dumps({'plan_id':symbol+'-plan',
                'status':'HISTORY_FETCHED' if name=='progress.json' else 'COMPLETE'}))
    monkeypatch.setattr(onboarding,'load_plan',lambda p:{'plan_id':p.parent.name+'-plan'})
    return batch, watchlist


def test_activation_adds_all_four_once_and_preserves_watchlist_comments(tmp_path,monkeypatch):
    batch, watchlist = _activation_fixture(tmp_path,monkeypatch)
    result = activate_batch(tmp_path/'plan.json',batch)
    assert result['status']=='ACTIVE'
    assert watchlist.read_text().startswith('# Production symbols\n')
    assert watchlist.read_text().splitlines()[1:]==batch['candidate_symbols']
    activate_batch(tmp_path/'plan.json',batch)
    assert len(watchlist.read_text().splitlines()[1:])==11
    for symbol in batch['selected_symbols']:
        assert json.loads((tmp_path/symbol/'activation.json').read_text())['batch_plan_id']=='batch'


@pytest.mark.parametrize('change',['current-publication','production-membership'])
def test_activation_rejects_stale_validation_and_concurrent_membership(tmp_path,monkeypatch,change):
    batch, watchlist = _activation_fixture(tmp_path,monkeypatch)
    if change=='current-publication':
        (tmp_path/'ml/nightly-gameplan-latest/run.json').write_text('{"run_path":"newer"}')
    else:
        watchlist.write_text(watchlist.read_text()+'MSFT\n')
    original = watchlist.read_text()
    with pytest.raises(ValueError,match='advanced|independently'):
        activate_batch(tmp_path/'plan.json',batch)
    assert watchlist.read_text()==original
    assert not (tmp_path/'activation.json').exists()


def test_schwab_policy_clamps_request_and_filters_provider_boundary_candle(tmp_path,monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from datafetching import schwab_fetch
    from app.services.market_fetch_specs import SchwabPriceHistorySpec
    policy = {'symbol':'IONQ','history_floor':'2018-01-01','listing_date':'2021-10-01'}
    policy_path = tmp_path/'state/symbol-history-policy/IONQ.json'
    policy_path.parent.mkdir(parents=True)
    policy_path.write_text(json.dumps({**policy,'sha256':policy_digest(policy)}))
    spec = SchwabPriceHistorySpec(key='year_20_daily_1',period_type='year',period=20,
                                 frequency_type='daily',frequency=1,need_extended_hours_data=False)
    monkeypatch.setattr(schwab_fetch,'_specs_for_profile',lambda _: (spec,))
    monkeypatch.setattr(schwab_fetch,'_latest_stored_bar_timestamp',lambda *_:None)
    monkeypatch.setattr(schwab_fetch,'persist_quote_liquidity',lambda *_:None)
    before = datetime(2021,9,30,tzinfo=timezone.utc)
    first = datetime(2021,10,1,tzinfo=timezone.utc)
    calls, saved = [], {}
    class Provider:
        def fetch_quote(self,symbol): return object(), {}
        def fetch_bars_for_spec(self,symbol,spec,**kwargs):
            calls.append(kwargs)
            return [SimpleNamespace(timestamp=t) for t in (before,first)], {
                'candles':[{'datetime':int(t.timestamp()*1000)} for t in (before,first)]}
    class Store:
        root_dir=tmp_path
        def save_quote(self,*args): return None
        def save_error(self,**kwargs): pytest.fail(str(kwargs))
        def save_raw_payload(self,**kwargs):
            if kwargs['category']=='bars':saved['raw']=kwargs['payload']
        def save_bars(self,source,symbol,key,bars,**kwargs):saved['bars']=bars
    schwab_fetch.fetch('IONQ',Store(),provider=Provider(),session=object(),include_options=False)
    assert calls[0]['start_datetime']==first
    assert [bar.timestamp for bar in saved['bars']]==[first]
    assert saved['raw']['candles']==[{'datetime':int(first.timestamp()*1000)}]


def test_fmp_policy_filters_statement_and_raw_archive_together(tmp_path):
    from datafetching import fmp_fetch
    from app.services.fmp_corporate_data import FmpCorporateDataSpec
    policy = {'symbol':'CROX','history_floor':'2018-01-01','listing_date':'2006-02-08'}
    path = tmp_path/'state/symbol-history-policy/CROX.json';path.parent.mkdir(parents=True)
    path.write_text(json.dumps({**policy,'sha256':policy_digest(policy)}))
    saved = {}
    class Provider:
        base_url='https://financialmodelingprep.com/stable'
        def corporate_specs(self,symbol):
            return (FmpCorporateDataSpec('income_statement_annual','income-statement',{'symbol':symbol}),)
        def fetch_corporate_data(self,symbol,spec):
            return [], [{'date':'2017-12-31','revenue':1},{'date':'2018-12-31','revenue':2}], spec.endpoint
        def _get_json(self,*args):return []
    class Store:
        root_dir=tmp_path
        def save_error(self,**kwargs):pytest.fail(str(kwargs))
        def save_corporate_rows(self,source,symbol,key,rows,**kwargs):saved[key]=rows
        def save_raw_payload(self,**kwargs):saved[kwargs['endpoint']+'_raw']=kwargs['payload']
    result = fmp_fetch.fetch('CROX',Store(),include_macro=False,corporate_provider=Provider())
    assert result.error_files==0
    assert [row['date'] for row in saved['income_statement_annual']]==['2018-12-31']
    assert [row['date'] for row in saved['income_statement_annual_raw']]==['2018-12-31']


@pytest.mark.parametrize('change',['different-gameplan','missing-stage','report-tamper','different-source'])
def test_overnight_completion_must_pin_this_gameplan_and_include_the_full_tail(tmp_path,change):
    from datafetching.research_onboarding import verify_overnight_completion
    from ml.artifacts import file_checksum
    from ml.overnight_runtime import OVERNIGHT_RUNTIME_VERSION
    run=tmp_path/'ml/overnight-runs/attempt';run.mkdir(parents=True)
    gameplan=tmp_path/'ml/nightly-gameplan-runs/expected'
    stages=['loop_a_close_fetch','loop_b_directional_generation','stock_target_history',
            'gameplan_evaluation','gameplan_publication','stock_enrichment_training',
            'gameplan_trade_planning','gameplan_actuals_review']
    report={'status':'COMPLETE','independent_stock_horizons':True,'preparation_scope':'STOCK_ONLY',
            'stock_price_source':'xnas-itch-archive-v1',
            'enrichment_gameplan':{'run_path':gameplan.relative_to(tmp_path).as_posix(),'receipt_sha256':'expected'},
            'stages':[{'stage':s,'status':'COMPLETE'} for s in stages]}
    if change=='different-gameplan':report['enrichment_gameplan']['run_path']='ml/nightly-gameplan-runs/other'
    if change=='different-source':report['stock_price_source']='different-dataset'
    if change=='missing-stage':report['stages']=report['stages'][:-1]
    (run/'stage-report.json').write_text(json.dumps(report))
    receipt={'schema_version':OVERNIGHT_RUNTIME_VERSION,'status':'COMPLETE','orders_placed':0,
             'broker_orders_enabled':False,'run_path':run.relative_to(tmp_path).as_posix(),
             'stage_report_checksum_sha256':file_checksum(run/'stage-report.json')}
    (run/'receipt.json').write_text(json.dumps(receipt))
    if change=='report-tamper':(run/'stage-report.json').write_text(json.dumps({**report,'status':'FAILED'}))
    with pytest.raises(ValueError,match='completion|stages'):
        verify_overnight_completion(tmp_path,{'plan_id':'batch','run_path':str(run)},'batch',gameplan,'expected')


def test_stock_batch_preparation_rejects_a_new_charge_before_submitting(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import threading
    import datafetching.research_onboarding as onboarding
    import datafetching.databento_cold_start as cold
    request={'dataset':'XNAS.ITCH','schema':'mbo','request_id':'scope'}
    monkeypatch.setattr(onboarding,'load_plan',lambda _: {'requests':[request]})
    monkeypatch.setattr(cold,'_generic_entry_paths',lambda *_:(tmp_path/'not-published',tmp_path/'staging'))
    monkeypatch.setattr(cold,'_request_kwargs',lambda r:r)
    monkeypatch.setattr(cold,'_load_or_submit_batch_fallback',lambda *a,**k:pytest.fail('Charged request was submitted'))
    client=SimpleNamespace(metadata=SimpleNamespace(get_cost=lambda **_:1.25),batch=object())
    batch={'datastore_root':str(tmp_path),'selected_symbols':['IONQ'],'budget':{'required_free_bytes':1}}
    with pytest.raises(ValueError,match='quotes a charge'):
        onboarding.prepare_stock_batches(tmp_path/'plan.json',batch,client,threading.Event())
    assert not (tmp_path/'stock-batch-preparation.json').exists()
