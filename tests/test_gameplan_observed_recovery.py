"""Offline fixture checks: no market requests, fitting, orders or live writes."""
import json
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.artifacts import file_checksum
from ml.gameplan_archive_integration import exclude_quality_intervals
from ml.gameplan_evaluation import evaluate_forecasts
from ml.gameplan_input_evidence import annotate_local_support, freeze_inference_inputs
from ml.independent_stock_targets import build_stock_training_groups
from ml.stock_observation_sources import SOURCE_ORDER, load_observed_stock_sources
from ml.stock_target_prices import (OBSERVED_MULTI_STOCK_PRICE_SOURCE,
    OBSERVED_MULTI_STOCK_PRICE_SOURCE_V2, XNAS_STOCK_PRICE_SOURCE)


def _sources():
    return pd.DataFrame([dict(symbol="TEST", action_date=date(2026, 10, 16),
        information_available_at=pd.Timestamp("2026-10-16T00:05Z"),
        decision_timestamp=pd.Timestamp("2026-10-16T00:05Z"), feature=1.)])


def _bar(source, timestamp, price, interval=60):
    return dict(symbol="TEST", timestamp=pd.Timestamp(timestamp), open=price, close=price,
        observation_source=source, observation_dataset="XNAS.ITCH" if source == SOURCE_ORDER[0] else "EQUS.MINI",
        observation_interval_seconds=interval, observation_input_sha256="a" * 64,
        observation_recorded_at=pd.Timestamp("2026-10-17T01:00Z"))


def _groups(rows):
    return build_stock_training_groups(_sources(), feature_columns=("feature",), minute_bars=pd.DataFrame(rows),
        available_at="2026-10-17T02:00Z", price_source_contract=OBSERVED_MULTI_STOCK_PRICE_SOURCE)


def test_fallback_uses_complete_same_source_pair_not_better_mixed_prices():
    groups = _groups([_bar(SOURCE_ORDER[0], "2026-10-16T11:00Z", 100),
                      _bar(SOURCE_ORDER[1], "2026-10-16T11:03Z", 200),
                      _bar(SOURCE_ORDER[1], "2026-10-16T23:59Z", 220)])
    row = groups["1d"].iloc[0]
    assert row.observed_return == pytest.approx(.1)
    assert row.target_observation_source == SOURCE_ORDER[1]
    assert row.target_start_gap_seconds == 180
    assert row.target_observation_entry_input_sha256 == "a" * 64
    assert row.target_observation_entry_record_id.endswith("TEST:2026-10-16T11:03:00+00:00")


def test_no_source_has_both_endpoints_stays_unavailable():
    groups = _groups([_bar(SOURCE_ORDER[0], "2026-10-16T11:00Z", 100),
                      _bar(SOURCE_ORDER[1], "2026-10-16T23:59Z", 220)])
    assert groups["1d"].empty


def test_fmp_requires_version_two_and_cannot_redefine_version_one_targets():
    from ml.stock_observation_sources import FMP_OBSERVATION_SOURCE, choose_target_source, source_order_for_contract
    rows = [_bar(FMP_OBSERVATION_SOURCE, "2026-10-16T11:00Z", 200, 300),
            _bar(FMP_OBSERVATION_SOURCE, "2026-10-16T23:55Z", 220, 300)]
    bars = pd.DataFrame(rows)
    old = _groups(rows)
    new = build_stock_training_groups(_sources(),feature_columns=("feature",),minute_bars=bars,
        available_at="2026-10-17T02:00Z",price_source_contract=OBSERVED_MULTI_STOCK_PRICE_SOURCE_V2)
    assert old['1d'].empty
    row = new['1d'].iloc[0]
    assert row.target_observation_source == FMP_OBSERVATION_SOURCE
    assert row.target_price_dataset == 'OBSERVED_SOURCE_SELECTION_V2'
    assert row.observed_close_timestamp == pd.Timestamp('2026-10-17T00:00Z')
    assert choose_target_source(bars,row.to_dict()).observation_source.eq(FMP_OBSERVATION_SOURCE).all()
    old_target={**row.to_dict(),'target_price_source_contract':OBSERVED_MULTI_STOCK_PRICE_SOURCE,
                'target_price_dataset':'OBSERVED_SOURCE_SELECTION_V1'}
    assert choose_target_source(bars,old_target).empty
    assert source_order_for_contract(OBSERVED_MULTI_STOCK_PRICE_SOURCE) == SOURCE_ORDER
    assert source_order_for_contract(OBSERVED_MULTI_STOCK_PRICE_SOURCE_V2) == (*SOURCE_ORDER,FMP_OBSERVATION_SOURCE)
    forecast={**old_target,'id':'historical-v1','source_gameplan_run':'fixture-v1','calibrated_probability':.6}
    scored=evaluate_forecasts(pd.DataFrame([forecast]),observed_groups=new,evaluated_at='2026-10-17T02:00Z')
    assert scored.iloc[0].evaluation_status=='MATURE_AWAITING_DATA'


def test_fmp_revision_cutoff_and_earlier_source_priority_are_preserved():
    from ml.stock_observation_sources import FMP_OBSERVATION_SOURCE, completed_observation_bars, choose_boundary_source
    rows=[_bar(FMP_OBSERVATION_SOURCE,'2026-10-16T11:00Z',100,300),
          _bar(FMP_OBSERVATION_SOURCE,'2026-10-16T11:00Z',101,300)]
    rows[0]['observation_recorded_at']=pd.Timestamp('2026-10-16T12:00Z')
    rows[1]['observation_recorded_at']=pd.Timestamp('2026-10-16T13:00Z')
    bars=pd.DataFrame(rows);bars.attrs['stock_price_source']={'source_contract':OBSERVED_MULTI_STOCK_PRICE_SOURCE_V2}
    assert completed_observation_bars(bars,pd.Timestamp('2026-10-16T12:30Z')).iloc[0].open==100
    assert completed_observation_bars(bars,pd.Timestamp('2026-10-16T13:30Z')).iloc[0].open==101
    assert choose_boundary_source(bars,pd.Timestamp('2026-10-16T11:00Z'),close=False,
                                  source_contract=OBSERVED_MULTI_STOCK_PRICE_SOURCE).empty
    pair=[_bar(s,t,p,i) for s,p,i in ((SOURCE_ORDER[2],100,60),(FMP_OBSERVATION_SOURCE,200,300))
          for t in ('2026-10-16T11:00Z','2026-10-16T23:55Z')]
    new=build_stock_training_groups(_sources(),feature_columns=('feature',),minute_bars=pd.DataFrame(pair),
        available_at='2026-10-17T02:00Z',price_source_contract=OBSERVED_MULTI_STOCK_PRICE_SOURCE_V2)
    assert new['1d'].iloc[0].target_observation_source==SOURCE_ORDER[2]


def test_version_one_loader_does_not_read_fmp_but_version_two_binds_it(tmp_path,monkeypatch):
    from ml.stock_observation_sources import FMP_OBSERVATION_SOURCE
    from ml.stock_target_prices import load_stock_target_prices
    monkeypatch.setattr('datafetching.databento_archive.discover_archive_partitions',lambda *a,**k:())
    calls=[]
    def fmp(root,*,symbol):
        calls.append(symbol)
        frame=pd.DataFrame([_bar(FMP_OBSERVATION_SOURCE,'2026-10-16T11:00Z',100,300)])
        return frame,(),[{'source':FMP_OBSERVATION_SOURCE,'rows':1}]
    monkeypatch.setattr('datafetching.fmp_price_history_receipts.load_native_five_minute_history',fmp)
    with pytest.raises(RuntimeError,match='No source-identified'):
        load_stock_target_prices(tmp_path,symbols=['TEST'],source_contract=OBSERVED_MULTI_STOCK_PRICE_SOURCE)
    assert calls==[]
    bars,files,report=load_stock_target_prices(tmp_path,symbols=['TEST'],source_contract=OBSERVED_MULTI_STOCK_PRICE_SOURCE_V2)
    assert calls==['TEST'] and len(bars)==1
    assert report['source_order'][-1]==FMP_OBSERVATION_SOURCE
    assert report['source_contract']==OBSERVED_MULTI_STOCK_PRICE_SOURCE_V2


def test_source_priority_frozen_and_tolerance_not_widened():
    rows = [_bar(s, t, p) for s, p in ((SOURCE_ORDER[0], 100), (SOURCE_ORDER[1], 200))
            for t in ("2026-10-16T11:00Z", "2026-10-16T23:59Z")]
    assert _groups(rows)["1d"].iloc[0].target_observation_source == SOURCE_ORDER[0]
    late = [_bar(SOURCE_ORDER[1], "2026-10-16T11:06Z", 200),
            _bar(SOURCE_ORDER[1], "2026-10-16T23:59Z", 220)]
    assert _groups(late)["1d"].empty


def test_native_five_minute_close_and_future_bar_availability():
    rows = [_bar(SOURCE_ORDER[3], "2026-10-16T11:00Z", 200, 300),
            _bar(SOURCE_ORDER[3], "2026-10-16T23:55Z", 220, 300)]
    row = _groups(rows)["1d"].iloc[0]
    assert row.observed_close_timestamp == pd.Timestamp("2026-10-17T00:00Z")
    assert row.target_end_gap_seconds == 0
    incomplete = build_stock_training_groups(_sources(), feature_columns=(), minute_bars=pd.DataFrame(rows),
        available_at="2026-10-16T23:59Z", price_source_contract=OBSERVED_MULTI_STOCK_PRICE_SOURCE)
    assert incomplete["1d"].empty


def test_late_receipt_needs_explicit_actual_evidence_clock_without_backdating_label():
    rows = [_bar(SOURCE_ORDER[1], "2026-10-16T11:00Z", 200),
            _bar(SOURCE_ORDER[1], "2026-10-16T23:59Z", 220)]
    arguments = dict(feature_columns=(), minute_bars=pd.DataFrame(rows),
        available_at="2026-10-17T00:30Z", price_source_contract=OBSERVED_MULTI_STOCK_PRICE_SOURCE)
    assert build_stock_training_groups(_sources(), **arguments)["1d"].empty
    recovered = build_stock_training_groups(_sources(), **arguments, evidence_available_at="2026-10-17T01:30Z")["1d"]
    assert len(recovered) == 1
    assert recovered.attrs["target_boundary_quality"]["label_cutoff"] == "2026-10-17T00:30:00+00:00"
    assert recovered.attrs["target_boundary_quality"]["evidence_cutoff"] == "2026-10-17T01:30:00+00:00"


def test_new_outcome_keeps_receipt_and_cannot_score_historical_xnas_contract():
    groups = _groups([_bar(SOURCE_ORDER[1], "2026-10-16T11:00Z", 200),
                      _bar(SOURCE_ORDER[1], "2026-10-16T23:59Z", 220)])
    row = groups["1d"].iloc[0].to_dict()
    forecast = {**row, "id": "new", "source_gameplan_run": "fixture-new", "calibrated_probability": .6}
    old = {**forecast, "id": "old", "target_price_source_contract": XNAS_STOCK_PRICE_SOURCE,
           "target_price_dataset": "XNAS.ITCH"}
    result = evaluate_forecasts(pd.DataFrame([forecast, old]), observed_groups=groups, evaluated_at="2026-10-17T02:00Z")
    assert result.evaluation_status.tolist() == ["EVALUATED", "MATURE_AWAITING_DATA"]
    assert result.iloc[0].target_observation_source == SOURCE_ORDER[1]
    retained = evaluate_forecasts(pd.DataFrame([forecast, old]), observed_groups={}, evaluated_at="2026-10-18T02:00Z", previous=result)
    assert retained.iloc[0].target_observation_entry_record_id == result.iloc[0].target_observation_entry_record_id


def test_xnas_warning_does_not_exclude_other_source_but_split_does():
    groups = _groups([_bar(SOURCE_ORDER[1], "2026-10-16T11:00Z", 200),
                      _bar(SOURCE_ORDER[1], "2026-10-16T23:59Z", 220)])
    interval = dict(symbol="TEST", start="2026-10-16T12:00Z", end="2026-10-16T13:00Z")
    assert len(exclude_quality_intervals(groups, [interval])["1d"]) == 1
    assert exclude_quality_intervals(groups, [], split_boundaries=[dict(symbol="TEST", at="2026-10-16T12:00Z")])["1d"].empty


def test_raw_provider_identity_zero_volume_and_conflicts(tmp_path, monkeypatch):
    monkeypatch.setattr("datafetching.databento_archive.discover_archive_partitions", lambda *a, **k: ())
    folder = tmp_path / "stocks/TEST/bars/1m/databento/raw"
    folder.mkdir(parents=True)
    path = folder / "TEST_ohlcv-1m_1m_raw.parquet"
    raw = pd.DataFrame([dict(ts_event=pd.Timestamp("2026-10-16T11:00Z"), symbol="TEST", open=10., high=11.,
        low=9., close=10., volume=v, provider_dataset="EQUS.MINI", source_schema="ohlcv-1m",
        aggregation_method="native", corporate_action_adjustment="none") for v in (0, 100)])
    raw.to_parquet(path, index=False)
    bars, files, report = load_observed_stock_sources(tmp_path, symbols=["TEST"])
    assert len(bars) == 1 and bars.iloc[0].observation_input_sha256 == file_checksum(path)
    assert report["inventory"][0]["invalid_or_zero_volume_rows_excluded"] == 1
    raw["provider_dataset"] = "OTHER"
    raw.to_parquet(path, index=False)
    with pytest.raises(ValueError, match="raw identity"):
        load_observed_stock_sources(tmp_path, symbols=["TEST"])


def test_raw_schwab_receipts_survive_snapshot_replacement_and_asof_revision(tmp_path, monkeypatch):
    from datafetching.parquet_store import ParquetStore
    from datafetching.schwab_price_history_receipts import preserve_price_history_raw
    from ml.stock_observation_sources import completed_observation_bars
    monkeypatch.setattr("datafetching.databento_archive.discover_archive_partitions", lambda *a, **k: ())
    store = ParquetStore(tmp_path)
    metadata = dict(provider_frequency_type='minute', provider_frequency=1)
    def save(price, fetched):
        payload = {'candles':[dict(datetime=int(pd.Timestamp('2026-10-16T11:00Z').timestamp()*1000),
            open=price,high=price,low=price,close=price,volume=100)]}
        path = store.save_raw_payload(source='schwab',category='bars',symbol='TEST',
            endpoint='pricehistory_day_10_minute_1',timeframe='1m',payload=payload,
            metadata={**metadata,'fetched_at':fetched})
        return path, preserve_price_history_raw(path)
    path, first = save(100., '2026-10-16T12:00Z')
    original = first.read_bytes()
    _, second = save(101., '2026-10-16T13:00Z')
    assert first != second and first.read_bytes() == original
    bars, files, _ = load_observed_stock_sources(tmp_path,symbols=['TEST'])
    assert len(bars) == 2
    early = completed_observation_bars(bars,pd.Timestamp('2026-10-16T12:30Z'))
    later = completed_observation_bars(bars,pd.Timestamp('2026-10-16T13:30Z'))
    assert early.iloc[0].open == 100. and later.iloc[0].open == 101.
    assert early.iloc[0].observation_input_sha256 == file_checksum(first)
    first.write_bytes(b'altered')
    with pytest.raises(RuntimeError,match='verification'):
        load_observed_stock_sources(tmp_path,symbols=['TEST'])


def test_simultaneous_schwab_revision_conflict_stays_unavailable():
    from ml.stock_observation_sources import completed_observation_bars
    frame = pd.DataFrame([_bar(SOURCE_ORDER[2],'2026-10-16T11:00Z',100),
                          _bar(SOURCE_ORDER[2],'2026-10-16T11:00Z',101)])
    frame.attrs['stock_price_source']={'source_contract':OBSERVED_MULTI_STOCK_PRICE_SOURCE}
    assert completed_observation_bars(frame,pd.Timestamp('2026-10-17T02:00Z')).empty


@pytest.mark.parametrize('marker',['synthetic','is_synthetic','no_trade','is_no_trade','source_empty','is_source_empty'])
def test_native_markers_checked_before_projection(marker):
    from ml.stock_observation_sources import _valid,completed_observation_bars
    from ml.gameplan_price_completion import _native_minutes
    from ml.stock_target_prices import _verify_native_markers
    from ml.gameplan_archive_features import _normalize
    frame=pd.DataFrame([dict(symbol='TEST',timestamp=pd.Timestamp('2026-10-16T11:00Z'),
        open=100.,high=101.,low=99.,close=100.,volume=100,**{marker:value})
        for value in (False,True,0,'false',None)])
    admitted,excluded=_valid(frame,source=SOURCE_ORDER[1],symbol='TEST',digest='a'*64,interval=60,dataset='EQUS.MINI')
    assert len(admitted)==1 and excluded==4
    assert len(_native_minutes(frame.iloc[:1]))==1
    archive=frame.assign(source_coverage_start=pd.Timestamp('2026-10-16T00:00Z'),
        source_coverage_end=pd.Timestamp('2026-10-17T00:00Z'),source_kind='verified-native-ohlcv-1m')
    assert len(_normalize(archive.iloc[:1],pd.Timedelta(minutes=1),as_of='2026-10-17T00:00Z'))==1
    _groups([{**_bar(SOURCE_ORDER[0],'2026-10-16T11:00Z',100),**frame.iloc[0].to_dict()}])
    for i in range(1,len(frame)):
        with pytest.raises(ValueError,match='markers'):
            _native_minutes(frame.iloc[i:i+1])
        with pytest.raises(ValueError,match='markers'):
            completed_observation_bars(frame.iloc[i:i+1],pd.Timestamp('2026-10-17T00:00Z'))
        with pytest.raises(RuntimeError,match='markers'):
            _verify_native_markers(frame.iloc[i:i+1])
        with pytest.raises(ValueError,match='markers'):
            _normalize(archive.iloc[i:i+1],pd.Timedelta(minutes=1),as_of='2026-10-17T00:00Z')
        with pytest.raises(ValueError,match='markers'):
            _groups(frame.iloc[i:i+1].to_dict('records'))


@pytest.mark.parametrize('marker',['synthetic','is_synthetic','no_trade','is_no_trade','source_empty','is_source_empty'])
def test_archive_loader_does_not_erase_native_markers(tmp_path,monkeypatch,marker):
    from ml import gameplan_archive_features as archive
    directory=tmp_path/'market-data/databento/us-equities/XNAS.ITCH/fixture'
    directory.mkdir(parents=True)
    raw=directory/'provider.dbn.zst';raw.write_bytes(b'fixture retained raw')
    normal=directory/'normalized.parquet'
    pd.DataFrame([dict(symbol='TEST',ts_event=pd.Timestamp('2026-10-16T11:00Z'),
        open=100.,high=101.,low=99.,close=100.,volume=100,**{marker:True})]).to_parquet(normal,index=False)
    request={'dataset':'XNAS.ITCH','schema':'ohlcv-1m','stype_in':'raw_symbol','symbol_scope':['TEST'],
             'start':'2026-10-16','end':'2026-10-17'}
    part=SimpleNamespace(directory=directory,normalized_path=normal,manifest_path=directory/'manifest.json',request=request,
        manifest={'published_at':'2026-10-17T01:00Z','provider_warnings':[],
            'raw':{'path':raw.name,'size_bytes':raw.stat().st_size,'checksum_sha256':file_checksum(raw)},
            'normalized':{'timestamp_column':'ts_event'}},
        receipt={'raw_checksum_sha256':file_checksum(raw)},source_files=(normal,))
    monkeypatch.setattr(archive,'discover_archive_partitions',lambda *a,**k:(part,))
    with pytest.raises(ValueError,match='markers'):
        archive._load_schema(tmp_path,['TEST'],'ohlcv-1m',as_of=pd.Timestamp('2026-10-17T02:00Z'))


def test_support_describes_pooled_not_local_qualification():
    frame = pd.DataFrame([dict(symbol="TEST", route="1d@D+1", model_status="PROMOTED")])
    support = {"TEST": dict(fitted_rows_by_route={"1d@D+1": 2}, assessment_rows_by_route={"1d@D+1": 5},
        assessment_class_counts_by_route={"1d@D+1": {"0": 3, "1": 2}}, assessment_decision_clusters_by_route={"1d@D+1": 5})}
    annotated = annotate_local_support(frame, support)
    assert annotated.iloc[0].model_status == "PROMOTED"
    assert not annotated.iloc[0].symbol_route_separately_qualified
    assert annotated.iloc[0].symbol_route_support_status == "LOCAL_ASSESSMENT_FEWER_THAN_TEN_DECISIONS"
    assert annotate_local_support(frame, {"TEST": {"fitted_rows_by_route": {"1d@D+1": 2}}}).iloc[0].symbol_route_support_status == "NO_LOCAL_ASSESSMENT"


def test_prospective_receipt_freezes_values_and_does_not_claim_past_recovery(tmp_path):
    directory = tmp_path / "models/1d"
    directory.mkdir(parents=True)
    frame = pd.DataFrame([dict(symbol="TEST", route="1d@D+1", feature=.3)])
    evidence, files = freeze_inference_inputs(frame, numeric=("feature",), categorical=("symbol", "route"),
        directory=directory, report={"model_file": {"path": "model.joblib", "checksum_sha256": "a" * 64}})
    original = file_checksum(tmp_path / evidence["path"])
    frame["feature"] = 999
    assert file_checksum(tmp_path / evidence["path"]) == original
    assert pd.read_parquet(tmp_path / evidence["path"]).iloc[0].feature == .3
    receipt = json.loads((tmp_path / evidence["receipt"]).read_text())
    assert receipt["runtime"]["historical_receipts_recovered"] is False
    assert "historical_original_inputs_not_recovered" in receipt["receipt_scope"]
    assert all((tmp_path / path).is_file() for path in files)


def test_future_published_partition_is_skipped_before_reading(tmp_path, monkeypatch):
    from ml import gameplan_archive_features as module
    part = SimpleNamespace(request={"symbol_scope": ["TEST"]}, manifest={"published_at": "2026-10-18T00:00Z"})
    monkeypatch.setattr(module, "discover_archive_partitions", lambda *a, **k: [part])
    bars, files, coverage, exclusions = module._load_schema(tmp_path, ("TEST",), "ohlcv-1d", as_of="2026-10-17T00:00Z")
    assert bars.empty and not files and not exclusions and coverage == {"TEST": []}


def test_feature_lineage_binds_only_the_actual_twenty_one_day_window():
    from ml.gameplan_archive_features import build_archive_feature_sources, _empty
    times = pd.bdate_range("2026-09-14", periods=25, tz="UTC")
    frame = pd.DataFrame([dict(symbol="TEST", timestamp=t, open=100+i, high=103+i, low=99+i,
        close=102+i, volume=1000+i, source_coverage_start=times[0], source_coverage_end=times[-1]+pd.Timedelta(days=1),
        source_kind="verified-native-ohlcv-1d", source_raw_input_hashes_json=json.dumps([str(i)]))
        for i,t in enumerate(times)])
    source = build_archive_feature_sources(frame, _empty(), symbols=["TEST"], available_at="2026-10-20T02:00Z").sources
    assert json.loads(source.iloc[0].feature_raw_input_hashes_json) == sorted(map(str, range(21)))
    assert set(json.loads(source.iloc[-1].feature_raw_input_hashes_json)) == set(map(str, range(4, 25)))
    assert "not_historical_receipt" in source.iloc[-1].feature_availability_basis


def _identified_prices(rows):
    frame = pd.DataFrame(rows)
    frame.attrs["stock_price_source"] = {"source_contract": OBSERVED_MULTI_STOCK_PRICE_SOURCE,
                                        "dataset": "OBSERVED_SOURCE_SELECTION_V1"}
    return frame


def test_actuals_and_point_review_accept_multi_source_without_mixing_endpoints():
    from ml.gameplan_actuals_review import compare_forecasts, compare_price_points
    rows = [_bar(SOURCE_ORDER[0], "2026-10-16T11:00Z", 100),
            _bar(SOURCE_ORDER[1], "2026-10-16T11:03Z", 200),
            _bar(SOURCE_ORDER[1], "2026-10-16T23:59Z", 220)]
    forecasts = _groups(rows)["1d"].assign(id="fixture", calibrated_probability=.7, direction="BULLISH")
    result = compare_forecasts(forecasts, _identified_prices(rows), observed_at="2026-10-17T02:00Z")
    assert result.iloc[0].actual_return == pytest.approx(.1)
    assert result.iloc[0].actual_observation_source == SOURCE_ORDER[1]
    points = compare_price_points(forecasts, _identified_prices(rows), action_date="2026-10-16", observed_at="2026-10-17T02:00Z")
    assert points.iloc[-1].actual_price == 220
    assert points.iloc[-1].actual_observation_source == SOURCE_ORDER[1]


def test_planning_uses_one_source_for_history_and_anchor_with_native_five_minutes():
    from ml.gameplan_price_bands import build_entry_price_bands, build_planning_price_path
    from ml.stock_observation_sources import planning_source_view
    rows = [_bar(SOURCE_ORDER[0], "2026-10-15T23:30Z", 100),
            *[_bar(SOURCE_ORDER[3], day + "T23:55Z", 200, 300) for day in ("2026-10-13", "2026-10-14", "2026-10-15")],
            *[_bar(SOURCE_ORDER[3], day + "T11:00Z", 202, 300) for day in ("2026-10-14", "2026-10-15")]]
    for row in rows:
        row["observation_recorded_at"] = pd.Timestamp("2026-10-16T01:00Z")
    forecast = pd.DataFrame([dict(symbol="TEST", id="fixture", action_date="2026-10-16", route="1d@D+1",
        target_role="EXECUTION", target_window_start=pd.Timestamp("2026-10-16T11:00Z"),
        target_price_source_contract=OBSERVED_MULTI_STOCK_PRICE_SOURCE, target_price_dataset="OBSERVED_SOURCE_SELECTION_V1")])
    prices = _identified_prices(rows)
    view = planning_source_view(prices, forecast, now=pd.Timestamp("2026-10-16T02:00Z"))
    assert set(view.observation_source) == {SOURCE_ORDER[3]}
    bands = build_entry_price_bands(prices, forecast, observed_at="2026-10-16T02:00Z", lookback_sessions=2,
                                   allow_reference_forward_fill=True, allow_sparse_session_references=True)
    assert bands["rows"][0]["price_band_status"] == "AVAILABLE"
    assert bands["rows"][0]["price_reference"] == 200
    assert bands["reference_completion"]["synthetic_bars"] == []
    path = build_planning_price_path(prices, forecast, observed_at="2026-10-16T02:00Z", entry_bands=bands,
                                    allow_reference_forward_fill=True, allow_sparse_session_references=True)
    assert path["points"]["TEST|2026-10-16|04:00"]["planned_price_mid"] == 202
    assert path["observation_sources_by_symbol"] == {"TEST": SOURCE_ORDER[3]}
