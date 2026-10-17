"""Partial receipt coverage never suppresses a valid row or admits an invalid row."""
from copy import deepcopy

import pytest

from ml.intraday.market_context import (
    FEATURE_VERSION, LEGACY_FEATURE_VERSION, build_market_context,
    market_context_contract, validate_market_context_contract,
)
from ml.intraday.market_context_cache import ContextCache
from ml.intraday.prediction import ContractError
from tests.test_intraday_market_context import CUTOFF, context, quote, sealed


@pytest.mark.parametrize('schema,fields,expected', [
    ('shares_float', {'float_shares': 50, 'outstanding_shares': 100}, {'free_float_fraction': .5}),
    ('income_statement_quarterly', {'eps': -2, 'eps_diluted': -2.2, 'net_income': -10, 'revenue': 100},
     {'eps': -2, 'diluted_eps': -2.2, 'profit_margin': -.1}),
    ('balance_sheet_statement_annual', {'total_debt': 20, 'total_stockholders_equity': 10,
                                      'cash_and_cash_equivalents': 10, 'total_assets': 100},
     {'debt_equity': 2, 'cash_assets_fraction': .1}),
    ('cash_flow_statement_annual', {'operating_cash_flow': -99, 'free_cash_flow': 0},
     {'signed_log_operating_cash_flow': -4.605170185988092, 'signed_log_free_cash_flow': 0}),
    ('income_statement_growth', {'growth_revenue': -.2}, {'revenue_growth': -.2}),
    ('cash_flow_statement_growth', {'growth_operating_cash_flow': -.2, 'growth_free_cash_flow': .3},
     {'operating_cash_flow_growth': -.2, 'free_cash_flow_growth': .3}),
    ('ratings-historical', {'overall_score': 3, 'price_to_book_score': 2},
     {'overall_rating_score': 3, 'pb_rating_score': 2}),
    ('ratings-snapshot', {'return_on_equity_score': 4, 'discounted_cash_flow_score': 1},
     {'roe_rating_score': 4, 'dcf_rating_score': 1}),
    ('price-target-summary', {'last_month_count': 3, 'last_month_avg_price_target': 125},
     {'target_month_count': 3, 'target_month_average': 125}),
    ('sec_filings_search_symbol', {}, {'log_observed_filing_count': .6931471805599453}),
])
def test_previously_unprojected_fundamental_families(schema, fields, expected):
    row = {'symbol': 'DOCU', 'event_at': '2026-10-16T14:14:00Z',
           'published_at': '2026-10-16T14:14:00Z', **fields}
    snapshot = sealed('fundamentals', [row], schema=schema, status='PARTIAL')
    value = context(snapshot)
    for name, number in expected.items():
        assert value.features['ctx_fundamentals_' + name] == pytest.approx(number)
        assert value.features['ctx_fundamentals_' + name + '_missing'] == 0
    assert next(e for e in value.source_evidence if e['source_kind']=='fundamentals')['records_used']==1
    old=context(snapshot,contract=market_context_contract(version=LEGACY_FEATURE_VERSION))
    assert next(e for e in old.source_evidence if e['source_kind']=='fundamentals')['records_used']==0
    assert build_market_context([snapshot],symbol='DOCU',cutoff='2026-10-16T14:15:04Z') == build_market_context(
        [],symbol='DOCU',cutoff='2026-10-16T14:15:04Z')


def test_fundamental_v2_ratios_are_same_record_and_missing_invalid_fields():
    rows=[{'symbol':'DOCU','event_at':'2026-10-16T14:13:00Z','net_income':5},
          {'symbol':'DOCU','event_at':'2026-10-16T14:14:00Z','revenue':100,
           'float_shares':200,'outstanding_shares':100,'last_month_count':0,'last_month_avg_price_target':100}]
    rows=[{**row,'published_at':row['event_at']} for row in rows]
    value=context(sealed('fundamentals',rows,schema='income_statement_annual'))
    for field in ('profit_margin','free_float_fraction','target_month_average'):
        assert value.features['ctx_fundamentals_'+field+'_missing']==1
    assert value.features['ctx_fundamentals_target_month_count']==0


def test_fundamental_v2_latest_eligible_period_keeps_receipt_clocks():
    rows=[{'symbol':'DOCU','event_at':'2026-10-16T14:14:00Z','period_end':'2026-09-30','overall_score':4},
          {'symbol':'DOCU','event_at':'2026-10-16T14:13:00Z','period_end':'2026-06-30','overall_score':2}]
    rows=[{**row,'published_at':row['event_at']} for row in rows]
    assert context(sealed('fundamentals',rows,schema='ratings-historical')).features['ctx_fundamentals_overall_rating_score']==4


def test_fundamental_v2_conflicting_latest_revision_stays_missing():
    common={'symbol':'DOCU','event_at':'2026-10-16T14:14:00Z','published_at':'2026-10-16T14:14:00Z'}
    rows=[{**common,'period_end':'2026-06-30','overall_score':1},
          {**common,'period_end':'2026-09-30','overall_score':3},
          {**common,'period_end':'2026-09-30','overall_score':4}]
    for order in (rows,list(reversed(rows))):
        value=context(sealed('fundamentals',order,schema='ratings-historical'))
        assert value.features['ctx_fundamentals_overall_rating_score_missing']==1


def test_negative_equity_is_preserved_but_fractional_target_count_is_missing():
    row={'symbol':'DOCU','event_at':'2026-10-16T14:14:00Z','published_at':'2026-10-16T14:14:00Z',
         'total_debt':20,'total_stockholders_equity':-10,'last_month_count':1.5}
    value=context(sealed('fundamentals',[row]))
    assert value.features['ctx_fundamentals_debt_equity']==-2
    assert value.features['ctx_fundamentals_target_month_count_missing']==1


def test_partial_keeps_causal_record_and_missing_coverage():
    snapshot = sealed(status='PARTIAL', rows=[quote(), quote(event_at='2026-10-16T14:16:00Z'),
        quote(symbol='WRONG'), quote(event_at='2026-10-16T13:00:00Z'), quote(synthetic=True)],
        evidence={'omitted_scope_count': 100, 'coverage_claim': 'bounded observed subset'})
    result = context(snapshot)
    assert result.feature_version == FEATURE_VERSION
    assert result.features['ctx_stock_quote_relative_spread'] == pytest.approx(.02)
    assert result.features['ctx_stock_quote_source_available'] == 1
    assert result.features['ctx_stock_quote_source_partial'] == 1
    entry = result.source_evidence[0]
    assert entry['records_used'] == 1
    assert entry['records_received'] == 5
    assert entry['coverage_complete'] is False
    assert entry['coverage_status'] == 'PARTIAL'
    assert entry['coverage_evidence']['omitted_scope_count'] == 100
    assert entry['exclusions'] == {'future_row_clock': 1, 'wrong_or_missing_row_symbol': 1,
        'stale_market_event': 1, 'synthetic_or_unverified_observation_marker': 1}
    assert context(snapshot, contract=market_context_contract(version=LEGACY_FEATURE_VERSION)).features[
        'ctx_stock_quote_relative_spread_missing'] == 1


@pytest.mark.parametrize('status', ['ERROR', 'CAPACITY_BLOCKED', 'NOT_AUTHORIZED', 'UNPROBED'])
def test_failure_with_rows_stays_unavailable(status):
    result = context(sealed(status=status))
    assert result.features['ctx_stock_quote_relative_spread_missing'] == 1
    assert result.source_evidence[0]['records_used'] == 0


def test_partial_no_rows_is_missing_not_no_trade_evidence():
    result = context(sealed(status='PARTIAL', rows=[]))
    assert result.features['ctx_stock_quote_source_available'] == 0
    assert result.features['ctx_stock_quote_relative_spread_missing'] == 1
    assert result.source_evidence[0]['coverage_complete'] is False


def test_partial_bar_dedup_and_completed_window_eligibility():
    row = {'symbol': 'DOCU', 'event_at': '2026-10-16T14:14:00Z',
           'open': 100, 'high': 102, 'low': 99, 'close': 101, 'volume': 7}
    snapshot = sealed('stock_bars', [row, deepcopy(row), {**row, 'event_at': '2026-10-16T14:15:00Z'}],
                      status='PARTIAL', schema='ohlcv-1m')
    result = context(snapshot)
    entry = next(e for e in result.source_evidence if e['source_kind'] == 'stock_bars')
    assert entry['records_used'] == 1
    assert entry['exclusions'] == {'identical_duplicate': 1, 'outside_requested_interval': 1}
    assert result.features['ctx_stock_bars_price_return'] == pytest.approx(.01)


def test_partial_receipt_cannot_enter_earlier_cutoff():
    snapshot = sealed(status='PARTIAL')
    before = '2026-10-16T14:15:04Z'
    assert build_market_context([snapshot], symbol='DOCU', cutoff=before) == build_market_context(
        [], symbol='DOCU', cutoff=before)


def test_cache_contract_separation_survives_restart(tmp_path):
    snapshot = sealed(status='PARTIAL')
    old = market_context_contract(version=LEGACY_FEATURE_VERSION)
    new = market_context_contract()
    assert validate_market_context_contract(old) == old
    assert set(old['feature_names']) < set(new['feature_names'])
    with ContextCache(tmp_path / 'cache.sqlite3') as cache:
        legacy = cache.build([snapshot], symbol='DOCU', cutoff=CUTOFF, contract=old)
        current = cache.build([snapshot], symbol='DOCU', cutoff=CUTOFF, contract=new)
        assert cache.misses == 2 and cache.hits == 0
    with ContextCache(tmp_path / 'cache.sqlite3') as cache:
        assert cache.build([snapshot], symbol='DOCU', cutoff=CUTOFF, contract=old) == legacy
        assert cache.build([snapshot], symbol='DOCU', cutoff=CUTOFF, contract=new) == current
        assert cache.hits == 2 and cache.misses == 0
    assert legacy.features['ctx_stock_quote_relative_spread_missing'] == 1
    assert current.features['ctx_stock_quote_relative_spread_missing'] == 0
    changed = deepcopy(new)
    changed['partial_policy'] = 'admit_everything'
    with pytest.raises(ContractError):
        validate_market_context_contract(changed)


def test_option_book_economics_reach_versioned_features_without_fabricated_greeks():
    row = {'symbol': 'DOCU', 'underlying_symbol': 'DOCU', 'contract_symbol': 'DOCU-C100',
           'event_at': '2026-10-16T14:14:59Z', 'bid': 2., 'ask': 2.2, 'bid_size': 30,
           'ask_size': 10, 'bid_depth_volume': 70, 'ask_depth_volume': 30}
    snapshot = sealed('options', [row], status='PARTIAL', schema='OPTIONS_BOOK')
    result = context(snapshot)
    assert result.features['ctx_options_relative_spread_median'] == pytest.approx(.2 / 2.1)
    assert result.features['ctx_options_top_depth_imbalance_median'] == .5
    assert result.features['ctx_options_book_depth_imbalance_median'] == .4
    assert result.features['ctx_options_iv_median_missing'] == 1
    assert result.features['ctx_options_put_call_volume_log_ratio_missing'] == 1
    assert result.features['ctx_options_source_partial'] == 1
    legacy = context(snapshot, contract=market_context_contract(version=LEGACY_FEATURE_VERSION))
    assert 'ctx_options_relative_spread_median' not in legacy.features


def test_saved_option_stream_native_side_and_cumulative_volume_are_used_once():
    base = {'symbol': 'DOCU', 'underlying_symbol': 'DOCU', 'event_at': '2026-10-16T14:14:59Z'}
    call = {**base, 'contract_symbol': 'DOCU-C100', 'option_type': 'c', 'day_volume': 10}
    put = {**base, 'contract_symbol': 'DOCU-P100', 'option_type': 'p', 'day_volume': 30}
    older = {**call, 'event_at': '2026-10-16T14:14:58Z', 'day_volume': 9}
    snapshot = sealed('options', [older, call, put], status='PARTIAL', schema='LEVELONE_OPTIONS')
    import math
    result = context(snapshot)
    assert result.features['ctx_options_put_call_volume_log_ratio'] == pytest.approx(math.log1p(30) - math.log1p(10))


def test_previous_derivation_cache_revision_is_not_reused(tmp_path, monkeypatch):
    from ml.intraday import market_context_cache as module
    path=tmp_path/'cache.sqlite3'
    snapshot=sealed(status='PARTIAL')
    current=module.VERSION
    monkeypatch.setattr(module,'VERSION','intraday-exact-causal-context-cache-v2')
    with ContextCache(path) as cache:
        cache.build([snapshot],symbol='DOCU',cutoff=CUTOFF)
    monkeypatch.setattr(module,'VERSION',current)
    with ContextCache(path) as cache:
        cache.build([snapshot],symbol='DOCU',cutoff=CUTOFF)
        assert cache.hits==0 and cache.misses==1
        cache.build([snapshot],symbol='DOCU',cutoff=CUTOFF)
        assert cache.hits==1
