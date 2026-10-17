from datetime import date
import json
import pandas as pd
from ml.independent_stock_targets import build_stock_training_groups
from ml.stock_observation_sources import SOURCE_ORDER_V2,target_failure_codes,target_exclusion_evidence


def test_forward_diagnostics_preserve_missing_endpoints_and_same_source_rule():
    sources=pd.DataFrame([{'symbol':'TEST','action_date':date(2026,10,12),
        'decision_timestamp':pd.Timestamp('2026-10-10T00:05Z'),'information_available_at':pd.Timestamp('2026-10-10T00:05Z')}])
    # One feed has only the entry and another only the exit. They cannot form
    # a return, and future metadata explains why each source pair was rejected.
    rows=[]
    for source,stamp in ((SOURCE_ORDER_V2[0],'2026-10-12T11:00Z'),(SOURCE_ORDER_V2[-1],'2026-10-12T23:55Z')):
        rows.append({'symbol':'TEST','timestamp':pd.Timestamp(stamp),'open':100.,'close':101.,
          'observation_source':source,'observation_dataset':'TEST','observation_interval_seconds':300 if source.startswith('fmp:') else 60,
          'observation_input_sha256':'a'*64,'observation_recorded_at':pd.Timestamp('2026-10-13T01:00Z')})
    bars=pd.DataFrame(rows)
    groups=build_stock_training_groups(sources,feature_columns=(),minute_bars=bars,available_at='2026-10-13T02:00Z',price_source_contract='observed-multi-source-stock-v2')
    quality=groups['1d'].attrs['target_boundary_quality']
    assert quality['candidate_rows']==1 and quality['aligned_rows']==0
    detail=quality['exclusion_evidence']
    assert detail['examples_exhaustive'] and detail['excluded_target_count']==1
    assert len(detail['examples'])==5 and all(x['causes'] for x in detail['examples'])
    assert detail['examples'][0]['symbol']=='TEST' and detail['examples'][0]['action_date']=='2026-10-12'
    json.dumps(detail,allow_nan=False)


def test_exhaustive_counts_are_separate_from_bounded_examples():
    targets=pd.DataFrame({'symbol':['TEST']*120,'action_date':[date(2026,10,12)]*120,'route':['1h@04:00']*120,
       'target_role':['ENTRY']*120,'target_window_start':[pd.Timestamp('2026-10-12T11:00Z')]*120,
       'target_window_end':[pd.Timestamp('2026-10-12T12:00Z')]*120})
    from ml.independent_stock_targets import _target_observations
    observations=_target_observations(targets,by_symbol={})
    codes=target_failure_codes(targets,observations)
    result=target_exclusion_evidence(targets,pd.Series(False,index=targets.index),{'test':codes},by_symbol={},example_limit=3)
    assert result['excluded_target_count']==120 and result['example_target_count']==3
    assert result['cause_counts_by_source']['test']['native_endpoint_pair_unavailable_or_invalid']==120
    assert not result['examples_exhaustive'] and len(result['examples'])==3
