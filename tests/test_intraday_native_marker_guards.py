"""All retained native paths reject explicit synthetic metadata before projection."""
from datetime import datetime,timezone
from types import SimpleNamespace

import pytest

from ml.intraday.market_inputs import _native_observation,_bar_features
from ml.market_gap_evidence import _native

MARKERS=('synthetic','is_synthetic','is_derived','no_trade','is_no_trade',
         'source_empty','is_source_empty','synthetic_open','synthetic_close')

@pytest.mark.parametrize('marker',MARKERS)
@pytest.mark.parametrize('value',[True,0,1,None,'false'])
def test_present_native_flags_are_strict_boolean_false(marker,value):
    record={marker:value}
    assert not _native(record)
    assert not _native_observation(record)
    assert not _native_observation(SimpleNamespace(**record))

@pytest.mark.parametrize('value',[True,False,1,-1,None,'0',float('nan'),float('inf')])
def test_synthetic_constituent_count_requires_numeric_zero(value):
    record={'synthetic_constituents':value}
    assert not _native(record)
    assert not _native_observation(record)
    assert not _native_observation(SimpleNamespace(**record))

@pytest.mark.parametrize('count',[0,0.0])
def test_explicit_native_metadata_remains_admissible(count):
    record=dict.fromkeys(MARKERS,False)|{'synthetic_constituents':count}
    assert _native(record) and _native_observation(record)

@pytest.mark.parametrize('change',[{'is_derived':True},{'synthetic_open':True},
    {'synthetic_close':True},{'synthetic_constituents':1}])
def test_auxiliary_bar_projection_does_not_erase_non_native_flags(change):
    row={'datetime':1791813600000,'open':100,'high':100,'low':100,'close':100,'volume':1}|change
    assert _bar_features([row],timestamp_key='datetime',receipt=datetime(2026,10,12,14,25,tzinfo=timezone.utc))=={}


def test_producer_freezes_native_validation_rules(tmp_path):
    import json
    from ml.intraday.market_inputs import MarketInputStore
    store=MarketInputStore(tmp_path)
    ref=store.capture_producer()
    producer=json.loads((store.root/ref['path']).read_bytes())
    assert producer['rules']['native_observation_markers']==list(MARKERS)
    assert producer['rules']['synthetic_constituents_rule']=='absent_or_numeric_zero_excluding_boolean'


@pytest.mark.parametrize("marker",MARKERS)
@pytest.mark.parametrize("value",[True,0,1,None,"false"])
@pytest.mark.parametrize("representation",["rows","dataframe"])
def test_primary_native_guard_rejects_every_shared_alias_before_projection(marker,value,representation):
    import pandas as pd
    from ml.intraday.data import _require_native_observations
    from ml.intraday.prediction import ContractError
    rows=[{marker:value}]
    with pytest.raises(ContractError,match="Native source observations"):
        _require_native_observations(pd.DataFrame(rows) if representation=="dataframe" else rows)


@pytest.mark.parametrize("representation",["rows","dataframe"])
@pytest.mark.parametrize("count",[0,0.0])
def test_primary_native_guard_accepts_explicit_false_metadata(representation,count):
    import pandas as pd
    from ml.intraday.data import _require_native_observations
    rows=[dict.fromkeys(MARKERS,False)|{"synthetic_constituents":count}]
    _require_native_observations(pd.DataFrame(rows) if representation=="dataframe" else rows)


@pytest.mark.parametrize("marker",MARKERS)
@pytest.mark.parametrize("value",[True,0,1,None,"false"])
@pytest.mark.parametrize("location",["candle","native_evidence"])
def test_frozen_primary_evidence_cannot_bypass_native_alias_guard(marker,value,location):
    import json
    from ml.intraday.coverage import verified_source_observation
    from ml.intraday.recovered_population import wrap_primary
    from ml.intraday.prediction import ContractError
    from tests.test_intraday_recovered_population import primary
    candle=primary()
    assert verified_source_observation(candle)
    if location=="candle":
        candle[marker]=value
    else:
        evidence=json.loads(candle["evidence_json"])
        evidence["observations"][0][marker]=value
        candle["evidence_json"]=json.dumps(evidence)
    assert not verified_source_observation(candle)
    with pytest.raises(ContractError):wrap_primary(candle)


@pytest.mark.parametrize("marker",MARKERS)
@pytest.mark.parametrize("rtype",[22,19])
def test_databento_marked_identity_is_retained_but_never_changes_mapping(tmp_path,marker,rtype):
    from datetime import timedelta
    from ml.intraday.market_input_stream import DatabentoMarketStreams
    from tests.test_intraday_market_inputs import FakeRecord,Client,T,stream_record
    stream=DatabentoMarketStreams(tmp_path,["AAPL"],[],client_factory=Client,clock=lambda:T+timedelta(seconds=1))
    try:
        record=FakeRecord(rtype=rtype,instrument_id=10,stype_in_symbol="AAPL",stype_out_symbol="AAPL",raw_symbol="AAPL",**{marker:True})
        stream.ingest("EQUS.MINI",record)
        assert ("EQUS.MINI","10") not in stream._mapping
        assert ("EQUS.MINI","10") not in stream._definitions
        stream.ingest("EQUS.MINI",stream_record())
        result=next(row for row in stream.snapshot() if row["source"]=="db_equity")
        assert result["features"]=={}
        assert result["payload"]["raw_segments"][0]["records"]==2
    finally:
        stream.close()


@pytest.mark.parametrize("rtype",[22,19])
def test_databento_genuine_identity_still_maps_native_data(tmp_path,rtype):
    from datetime import timedelta
    from ml.intraday.market_input_stream import DatabentoMarketStreams
    from tests.test_intraday_market_inputs import FakeRecord,Client,T,stream_record
    stream=DatabentoMarketStreams(tmp_path,["AAPL"],[],client_factory=Client,clock=lambda:T+timedelta(seconds=1))
    try:
        stream.ingest("EQUS.MINI",FakeRecord(rtype=rtype,instrument_id=10,stype_in_symbol="AAPL",stype_out_symbol="AAPL",raw_symbol="AAPL",**dict.fromkeys(MARKERS,False)))
        stream.ingest("EQUS.MINI",stream_record())
        result=next(row for row in stream.snapshot() if row["source"]=="db_equity")
        assert result["features"]["trade_volume"]==4
    finally:
        stream.close()
