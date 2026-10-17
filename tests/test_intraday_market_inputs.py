"""Offline receipt, plan and concrete transport contracts. Never call providers."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import struct

import pytest

from ml.intraday.market_inputs import (
    FEATURE_NAMES, MODEL_FEATURE_NAMES, MarketInputCollector, MarketInputStore,
    _bar_features, _option_features, augment_snapshot, fmp_specs,read_response,
)
from ml.intraday.market_input_stream import DatabentoMarketStreams, SCHEMAS

UTC = timezone.utc
T = datetime(2026,10,16,16,0,tzinfo=UTC)


def append(store, at=T, **kw):
    return store.append(symbol="AAPL",source="schwab_quote",started_at=at-timedelta(seconds=1),received_at=at,
                        payload=kw.pop("payload",{"bidPrice":100,"askPrice":101}),features={"last":100},**kw)


def column(result,name,row=0):
    return result["rows"][row][result["feature_names"].index(name)]


def test_receipts_never_backfill_earlier_training_and_expire(tmp_path):
    store = MarketInputStore(tmp_path)
    append(store)
    result = store.join("AAPL",[T-timedelta(microseconds=1),T,T+timedelta(minutes=31)],cutoff=T+timedelta(hours=1))
    assert [column(result,"market_schwab_quote_last__available",i) for i in range(3)] == [0,1,0]
    assert [column(result,"market_schwab_quote_last",i) for i in range(3)] == [0,100,0]
    assert result["provenance"][1]["sources"]["schwab_quote"]["received_at"] == T.isoformat()


def test_cutoff_and_future_event_rejected(tmp_path):
    store = MarketInputStore(tmp_path)
    append(store,at=T+timedelta(seconds=1))
    result = store.join("AAPL",[T+timedelta(minutes=5)],cutoff=T)
    assert column(result,"market_schwab_quote_last__available") == 0
    append(store,at=T,event_at=T+timedelta(seconds=5))
    result = store.join("AAPL",[T],cutoff=T)
    assert column(result,"market_schwab_quote_last__available") == 0
    assert result["provenance"][0]["sources"]["schwab_quote"]["status"] == "future_event"


def test_new_unavailable_receipt_does_not_silently_resurrect_old_quote(tmp_path):
    store = MarketInputStore(tmp_path)
    append(store)
    append(store,at=T+timedelta(seconds=1),status="unavailable")
    result = store.join("AAPL",[T+timedelta(seconds=2)],cutoff=T+timedelta(seconds=2))
    assert column(result,"market_schwab_quote_last__available") == 0


def test_all_same_clock_receipts_checked_even_when_last_two_agree(tmp_path):
    store=MarketInputStore(tmp_path)
    # Keep receipt ordering independent of the currently loaded source hash and
    # wall clock; this fixture tests same-clock conflicts, not producer capture.
    store.producer_runtime={"path":"offline-fixture.json","sha256":"a"*64}
    receipts=[store.append(symbol="AAPL",source="fmp_quote",started_at=T,received_at=T,
        payload={"fixture":i},features={"price":1 if i==0 else 2}) for i in range(30)]
    ordered=sorted(receipts,key=lambda row:row["id"])
    assert [row["features"]["price"] for row in ordered[-2:]]==[2,2]
    joined=store.join("AAPL",[T],cutoff=T)
    assert column(joined,"market_fmp_quote_price__available")==0
    assert joined["provenance"][0]["sources"]["fmp_quote"]["verification"]=="conflicting_same_receipt_time"


def test_immutable_objects_and_recursive_credentials_are_not_persisted(tmp_path):
    store = MarketInputStore(tmp_path)
    receipt = append(store,payload={"apikey":"DO_NOT_WRITE","nested":{"access_token":"HIDDEN","price":42}})
    raw = open(receipt["object_path"],"rb").read()
    assert b"DO_NOT_WRITE" not in raw and b"HIDDEN" not in raw
    assert hashlib.sha256(raw).hexdigest() == receipt["id"]
    decoded=read_response(store.root,receipt["object_path"])
    assert decoded["payload"]=={"nested":{"price":42}}
    same = append(store,payload={"apikey":"DO_NOT_WRITE","nested":{"access_token":"HIDDEN","price":42}})
    assert same["id"] == receipt["id"]


def test_missing_store_has_no_side_effect_and_augments_copy(tmp_path):
    store = MarketInputStore(tmp_path)
    snapshot={"X":[[1.]],"y":[1],"reference_at":[(T-timedelta(minutes=15)).isoformat()],"inference_X":[[2.]],"feature_names":["price_feature"],"manifest":{}}
    out = augment_snapshot(snapshot,store,symbol="AAPL",inference_at=T,cutoff=T)
    assert not store.root.exists()
    assert snapshot["X"] == [[1.]]
    assert len(out["X"][0]) == len(MODEL_FEATURE_NAMES)+1
    assert len(out["feature_names"]) == len(set(out["feature_names"]))
    assert out["X"][0][1:] == [0.]*len(MODEL_FEATURE_NAMES)
    assert out["market_inputs"]["training"][0]["decision_at"] == T.isoformat()
    assert out["manifest"]["feature_contract"]["market_inputs"]==out["market_inputs"]["version"]
    with pytest.raises(ValueError,match="already"):
        augment_snapshot(out,store,symbol="AAPL",inference_at=T,cutoff=T)


def test_snapshot_historical_and_inference_decisions_differ(tmp_path):
    store = MarketInputStore(tmp_path)
    append(store,at=T+timedelta(seconds=5))
    snapshot={"X":[[1]],"reference_at":[(T-timedelta(minutes=15)).isoformat()],"inference_X":[[2]],"feature_names":["price"],"manifest":{},"inference":{"information_available_at":T.isoformat()}}
    result=augment_snapshot(snapshot,store,symbol="AAPL",inference_at=T+timedelta(seconds=10),cutoff=T+timedelta(seconds=10))
    i=result["feature_names"].index("market_schwab_quote_last__available")
    assert result["X"][0][i]==0 and result["inference_X"][0][i]==1
    assert result["inference"]["information_available_at"]==(T+timedelta(seconds=5)).isoformat()
    assert result["market_inputs"]["training_nonmissing_rows_by_feature"]["market_schwab_quote_last"]==0


def test_bars_do_not_admit_partial_bad_or_duplicate_volume():
    stamp=int((T-timedelta(minutes=2)).timestamp()*1000)
    rows=[{"datetime":stamp,"open":100,"high":102,"low":99,"close":101,"volume":5},
          {"datetime":stamp,"open":100,"high":102,"low":99,"close":101,"volume":7},
          {"datetime":int(T.timestamp()*1000),"open":101,"high":103,"low":100,"close":102,"volume":90},
          {"datetime":stamp-60000,"open":100,"high":98,"low":99,"close":101,"volume":5}]
    result=_bar_features(rows,timestamp_key="datetime",receipt=T)
    assert result["observed_bars"]==1 and result["volume"]==7


@pytest.mark.parametrize("marker", ["synthetic","is_synthetic","no_trade","is_no_trade","source_empty","is_source_empty"])
@pytest.mark.parametrize("value", [True,1,0,"false",None])
def test_positive_volume_native_markers_rejected_before_bar_projection(marker,value):
    stamp=int((T-timedelta(minutes=2)).timestamp()*1000)
    row={"datetime":stamp,"open":100,"high":102,"low":99,"close":101,"volume":7}
    assert _bar_features([row|{marker:value}],timestamp_key="datetime",receipt=T)=={}
    assert _bar_features([row|{marker:False}],timestamp_key="datetime",receipt=T)["volume"]==7
    from ml.intraday.market_inputs import _fmp_features
    fmp={k:v for k,v in row.items() if k!="datetime"}|{"date":"2026-10-16 11:50:00",marker:value}
    assert _fmp_features("fmp_5min",[fmp],T)=={}


def test_native_marker_filter_applies_to_quote_contract_and_wrapper():
    from ml.intraday.market_inputs import _quote_features,_quote_status,_fmp_features
    quote={"quoteTime":int(T.timestamp()*1000),"lastPrice":100,"source_empty":True}
    assert _quote_features(quote)=={}
    assert _quote_status("schwab_quote",quote,T)[0]=="invalid_native_marker"
    assert _fmp_features("fmp_quote",{"data":[{"price":100}],"is_synthetic":True},T)=={}
    bad={"delta":0.5,"totalVolume":7,"synthetic":True}
    good={"delta":0.2,"totalVolume":3,"synthetic":False}
    payload={"callExpDateMap":{"date":{"100":[bad,good]}}}
    assert _option_features(payload)["contracts"]==1
    assert _option_features(payload)["delta"]==.2
    assert _option_features(payload|{"no_trade":True})=={}


def test_option_missing_volume_never_becomes_zero_and_invalid_greeks_rejected():
    payload={"callExpDateMap":{"date":{"100":[{"delta":-999,"volatility":-999,"bid":1,"ask":2}]}},"putExpDateMap":{}}
    result=_option_features(payload)
    assert result["delta"] is None and result["iv"] is None
    assert result["put_call_volume"] is None
    assert _option_features(payload,T)=={}


class FakeRecord(dict):
    def __bytes__(self):
        return json.dumps(self,sort_keys=True).encode()


class Client:
    def __init__(self,**kw):
        self.kw=kw;self.subscriptions=[];self.stopped=False
    def add_callback(self,*args): self.callback=args[0]
    def add_reconnect_callback(self,*args): pass
    def subscribe(self,**kw): self.subscriptions.append(kw)
    def start(self): pass
    def stop(self): self.stopped=True
    def block_for_close(self,**kw): pass
    def terminate(self): self.stopped=True


def stream_record(kind=1,**kw):
    return FakeRecord(rtype=kind,instrument_id=10,ts_event=int(T.timestamp()*1e9),sequence=1,price=100_000_000_000,size=4,action="T",side="B",levels=[{"bid_px":99_000_000_000,"ask_px":101_000_000_000,"bid_sz":3,"ask_sz":1}],**kw)


def test_concrete_live_subscriptions_are_plan_aware_and_owned(tmp_path):
    clients=[]
    def factory(**kw):
        client=Client(**kw);clients.append(client);return client
    stream=DatabentoMarketStreams(tmp_path,["AAPL"],["ES.c.0"],client_factory=factory,clock=lambda:T)
    assert stream.start()=={d:"starting" for d in SCHEMAS}
    requests=[r for c in clients for r in c.subscriptions]
    assert {r["dataset"] for r in requests}=={"EQUS.MINI","GLBX.MDP3","OPRA.PILLAR"}
    assert all(r["symbols"]!=["ALL_SYMBOLS"] for r in requests)
    assert next(r for r in requests if r["dataset"]=="OPRA.PILLAR")["symbols"]==("AAPL.OPT",)
    cme_requests=[r for r in requests if r["dataset"]=="GLBX.MDP3"]
    assert {r["schema"] for r in cme_requests}=={"definition","mbp-1","ohlcv-1m","statistics"}
    assert all(r["schema"] not in {"mbp-10","mbo"} for r in requests)
    assert all("snapshot" not in r for r in cme_requests)
    assert not any("1min" in endpoint for _,endpoint,_ in fmp_specs("AAPL",T))
    stream.close();assert all(c.stopped for c in clients)


def test_native_frames_hash_receipts_and_features_persist_without_future_data(tmp_path):
    clock=[T+timedelta(seconds=1)]
    stream=DatabentoMarketStreams(tmp_path,["AAPL"],["ES.c.0"],client_factory=Client,clock=lambda:clock[0])
    stream.ingest("EQUS.MINI",FakeRecord(rtype=22,instrument_id=10,stype_in_symbol="AAPL",stype_out_symbol="AAPL"))
    row=stream_record()
    stream.ingest("EQUS.MINI",row)
    result=next(x for x in stream.snapshot() if x["source"]=="db_equity")
    assert result["status"]=="ok"
    assert result["features"]["trade_volume"]==4
    assert result["features"]["quote_imbalance"]==.5
    manifest=result["payload"]["raw_segments"][0]
    raw=open(manifest["path"],"rb").read()
    assert hashlib.sha256(raw).hexdigest()==manifest["sha256"]
    receipt,length=struct.unpack(">QI",raw[:12])
    assert receipt==int(clock[0].timestamp()*1e9) and length>0
    clock[0]+=timedelta(seconds=1)
    result=next(x for x in stream.snapshot() if x["source"]=="db_equity")
    assert len(result["payload"]["raw_segments"])==1
    stream.close()


def test_stream_preserves_marked_raw_frame_but_never_projects_its_features(tmp_path):
    stream=DatabentoMarketStreams(tmp_path,["AAPL"],[],client_factory=Client,clock=lambda:T+timedelta(seconds=1))
    stream.ingest("EQUS.MINI",FakeRecord(rtype=22,instrument_id=10,stype_in_symbol="AAPL"))
    stream.ingest("EQUS.MINI",stream_record(synthetic=True))
    result=next(x for x in stream.snapshot() if x["source"]=="db_equity")
    assert result["status"]=="unavailable" and result["features"]=={}
    assert result["payload"]["raw_segments"][0]["records"]==2
    stream.ingest("EQUS.MINI",stream_record(synthetic=False))
    result=next(x for x in stream.snapshot() if x["source"]=="db_equity")
    assert result["status"]=="ok" and result["features"]["trade_volume"]==4
    stream.close()


def test_opra_cbbo_real_rtype192_maps_to_underlying(tmp_path):
    stream=DatabentoMarketStreams(tmp_path,["AAPL"],[],client_factory=Client,clock=lambda:T+timedelta(seconds=1))
    stream.ingest("OPRA.PILLAR",FakeRecord(rtype=19,instrument_id=10,raw_symbol="AAPL  261016C00100000"))
    stream.ingest("OPRA.PILLAR",stream_record(kind=192))
    result=next(x for x in stream.snapshot() if x["source"]=="db_options")
    assert result["status"]=="ok" and result["features"]["contracts"]==1


def test_overflow_is_explicit_and_does_not_discard_raw_records(tmp_path):
    stream=DatabentoMarketStreams(tmp_path,["AAPL"],[],client_factory=Client,clock=lambda:T+timedelta(seconds=1),maximum_records=1)
    stream.ingest("EQUS.MINI",FakeRecord(rtype=22,instrument_id=10,stype_in_symbol="AAPL"))
    stream.ingest("EQUS.MINI",stream_record())
    stream.ingest("EQUS.MINI",stream_record())
    result=next(x for x in stream.snapshot() if x["source"]=="db_equity")
    assert result["status"]=="unavailable"
    assert result["payload"]["raw_segments"][0]["records"]==3
    assert result["payload"]["derived_dropped_records"]>=1


def test_failure_text_with_secret_never_written_and_providers_are_independent(tmp_path):
    class Schwab:
        def get_equity_quotes(self,symbols):return {s:{"lastPrice":100,"quoteTime":int(T.timestamp()*1000)} for s in symbols}
        def get_price_history(self,*args,**kw):raise RuntimeError("https://example/?apikey=BAD_SECRET")
        def get_option_chain_snapshot(self,*args,**kw):return {}
    class Fmp:
        def _get_json(self,*args):raise RuntimeError("access_token=BAD_SECRET")
    collector=MarketInputCollector(tmp_path,["AAPL"],schwab=Schwab(),fmp=Fmp(),clock=lambda:T)
    result=collector.collect(T-timedelta(minutes=15))
    assert any(x["source"]=="schwab_quote" and x["status"]=="ok" for x in result["receipts"])
    assert any(x["source"]=="fmp_quote" and x["status"]=="unavailable" for x in result["receipts"])
    assert not any("BAD_SECRET" in p.read_text() for p in (collector.store.root/"objects").rglob("*.json"))
    collector.close()


def test_missing_credentials_does_not_construct_sdk_or_open_socket(tmp_path,monkeypatch):
    monkeypatch.delenv("DATABENTO_API_KEY",raising=False)
    stream=DatabentoMarketStreams(tmp_path,["AAPL"],[])
    assert set(stream.start().values())=={"missing_credentials"}
    assert not tmp_path.joinpath("streams").exists()


def test_catalog_tamper_or_deleted_receipt_masks_input_with_explicit_integrity_reason(tmp_path):
    import sqlite3
    store=MarketInputStore(tmp_path);receipt=append(store)
    db=sqlite3.connect(store.database)
    db.execute("UPDATE receipts SET features=?",(json.dumps({"last":999}),));db.commit();db.close()
    result=store.join("AAPL",[T],cutoff=T)
    assert column(result,"market_schwab_quote_last__available")==0
    assert result["provenance"][0]["sources"]["schwab_quote"]["verification"]=="receipt_integrity_unavailable"


def test_dense_flow_uses_sufficient_statistics_not_event_buffer(tmp_path):
    stream=DatabentoMarketStreams(tmp_path,["AAPL"],[],client_factory=Client,clock=lambda:T+timedelta(seconds=1),maximum_records=5)
    stream.ingest("EQUS.MINI",FakeRecord(rtype=22,instrument_id=10,stype_in_symbol="AAPL"))
    for i in range(1000):
        row=stream_record();row["sequence"]=i
        stream.ingest("EQUS.MINI",row)
    result=next(x for x in stream.snapshot() if x["source"]=="db_equity")
    assert result["status"]=="ok"
    assert result["features"]["trade_count"]==1000
    assert result["features"]["trade_volume"]==4000
    assert result["payload"]["derived_state_entries"]==3
    assert result["payload"]["raw_segments"][0]["records"]==1001


def test_stale_or_future_provider_quote_is_not_fresh_receipt(tmp_path):
    from ml.intraday.market_inputs import _quote_status
    assert _quote_status("schwab_quote",{"quoteTime":int((T-timedelta(hours=1)).timestamp()*1000)},T)[0]=="stale_event"
    assert _quote_status("fmp_quote",[{"timestamp":int((T+timedelta(seconds=2)).timestamp())}],T)[0]=="future_event"
    assert _quote_status("schwab_quote",{"lastPrice":100},T)[0]=="unverified_event_time"


def test_fmp_failed_fast_requests_rotate_without_exposing_provider_error(tmp_path,monkeypatch):
    import time
    class Fmp:
        def __init__(self):self.calls=[]
        def _get_json(self,endpoint,params):self.calls.append((endpoint,params));raise RuntimeError("HTTP 403: private-key")
    fmp=Fmp();collector=MarketInputCollector(tmp_path,["AAPL","COST"],fmp=fmp,clock=lambda:T)
    collector._deadline=time.monotonic()+120
    calls=[0]
    def may(symbol,source):
        calls[0]+=1
        return calls[0]==1
    collector._may_request=may
    collector._fmp_collect();calls[0]=0;collector._fmp_collect()
    assert [x[1]["symbol"] for x in fmp.calls]==["AAPL","COST"]
    payloads=[json.loads(p.read_text()) for p in (collector.store.root/"raw").rglob("*.json")]
    assert all(x["error"]["category"]=="request_rejected" for x in payloads)
    assert all(x["error"]["http_status"]==403 for x in payloads)


def test_actual_installed_dbn_record_admits_real_protocol_fields(tmp_path):
    d=pytest.importorskip("databento_dbn")
    stream=DatabentoMarketStreams(tmp_path,["AAPL"],[],client_factory=Client,clock=lambda:T+timedelta(seconds=1))
    mapping=d.SymbolMappingMsg(publisher_id=1,instrument_id=10,ts_event=int(T.timestamp()*1e9),stype_in=d.SType.RAW_SYMBOL,stype_in_symbol="AAPL",stype_out=d.SType.INSTRUMENT_ID,stype_out_symbol="10",start_ts=int(T.timestamp()*1e9),end_ts=int((T+timedelta(days=1)).timestamp()*1e9))
    record=d.MBP1Msg(publisher_id=1,instrument_id=10,ts_event=int(T.timestamp()*1e9),price=100_000_000_000,size=7,action=d.Action.TRADE,side=d.Side.BID,depth=0,ts_recv=int(T.timestamp()*1e9),sequence=10,levels=d.BidAskPair(bid_px=99_000_000_000,ask_px=101_000_000_000,bid_sz=3,ask_sz=1))
    stream.ingest("EQUS.MINI",mapping);stream.ingest("EQUS.MINI",record)
    result=next(x for x in stream.snapshot() if x["source"]=="db_equity")
    assert result["status"]=="ok" and result["features"]["trade_volume"]==7
    assert result["features"]["vwap"]==100


def test_optional_close_failure_does_not_escape_manual_stop(tmp_path):
    class BrokenStreams:
        def close(self):raise OSError("disk full")
    collector=MarketInputCollector(tmp_path,["AAPL"],streams=BrokenStreams())
    collector.close()
    assert collector._closed and collector.close_error_type=="OSError"
    assert collector._schwab_collect()==[]
    with pytest.raises(RuntimeError,match="closed"):
        collector.collect(T)


def test_same_receipt_time_conflicting_payloads_are_not_arbitrarily_selected(tmp_path):
    store=MarketInputStore(tmp_path);append(store)
    store.append(symbol="AAPL",source="schwab_quote",started_at=T,received_at=T,payload={"lastPrice":200},features={"last":200})
    result=store.join("AAPL",[T],cutoff=T)
    assert column(result,"market_schwab_quote_last__available")==0
    assert result["provenance"][0]["sources"]["schwab_quote"]["verification"]=="conflicting_same_receipt_time"


def test_mbo_snapshot_adds_are_not_organic_order_flow(tmp_path):
    stream=DatabentoMarketStreams(tmp_path,["AAPL"],["ES.c.0"],client_factory=Client,clock=lambda:T+timedelta(seconds=1))
    stream.ingest("GLBX.MDP3",FakeRecord(rtype=22,instrument_id=10,stype_in_symbol="ES.c.0"))
    snap=stream_record(kind=160);snap.update(action="A",flags=40)
    stream.ingest("GLBX.MDP3",snap)
    real=stream_record(kind=160);real.update(action="A",flags=0,sequence=2)
    stream.ingest("GLBX.MDP3",real)
    cancel=stream_record(kind=160);cancel.update(action="C",flags=0,sequence=3)
    stream.ingest("GLBX.MDP3",cancel)
    result=next(x for x in stream.snapshot() if x["source"]=="db_cme_es")
    assert result["features"]["cancel_add_ratio"]==1
    assert result["payload"]["raw_segments"][0]["records"]==4


def test_configured_json_futures_are_parsed_without_inventing_symbols(tmp_path,monkeypatch):
    monkeypatch.setenv("DATABENTO_CME_CONTEXT_SYMBOLS", '["ES.v.0", "NQ.v.0", "RTY.v.0", "CL.v.0", "GC.v.0"]')
    collector=MarketInputCollector(tmp_path,["AAPL"])
    assert collector.futures==("ES.v.0","NQ.v.0","RTY.v.0","CL.v.0","GC.v.0")
    stream=DatabentoMarketStreams(tmp_path,["AAPL"],collector.futures,client_factory=Client,clock=lambda:T,datasets=("GLBX.MDP3",))
    assert set(stream.start())=={"GLBX.MDP3"}
    rows=stream.snapshot()
    assert {r["source"] for r in rows}=={"db_cme_es","db_cme_nq","db_cme_rty","db_cme_cl","db_cme_gc"}
    assert all(r["status"]=="unavailable" for r in rows)
    stream.close()


def test_provider_error_diagnostic_is_durable_sanitized_and_attached_to_each_root(tmp_path,monkeypatch):
    d=pytest.importorskip("databento_dbn")
    monkeypatch.setenv("DATABENTO_API_KEY","TEST_PRIVATE_KEY")
    stream=DatabentoMarketStreams(tmp_path,["AAPL"],["ES.v.0","RTY.v.0"],client_factory=Client,clock=lambda:T,datasets=("GLBX.MDP3",))
    record=d.ErrorMsg(ts_event=int(T.timestamp()*1e9),err="Invalid subscription TEST_PRIVATE_KEY https://example/?apikey=TOKEN access_token=SECRET",code=d.ErrorCode.INVALID_SUBSCRIPTION)
    stream.ingest("GLBX.MDP3",record)
    rows=stream.snapshot()
    assert {r["source"] for r in rows}=={"db_cme_es","db_cme_rty"}
    assert all(r["payload"]["stream_status"]=="provider_error_record" for r in rows)
    assert all(r["payload"]["diagnostics"][0]["code"]==str(d.ErrorCode.INVALID_SUBSCRIPTION) for r in rows)
    paths=list((tmp_path/"stream-errors").rglob("*.json"))
    assert len(paths)==1
    value=paths[0].read_text()
    assert "Invalid subscription" in value
    assert all(secret not in value for secret in ("TEST_PRIVATE_KEY","TOKEN","SECRET","https://"))


def test_schema_rejection_is_removed_once_without_blocking_remaining_schemas(tmp_path):
    import threading
    ready=threading.Event();clients=[]
    class RejectStatistics(Client):
        def block_for_close(self,**kw):self.stopped=True
        def start(self):
            schemas={r["schema"] for r in self.subscriptions}
            rejected="statistics" if "statistics" in schemas else None
            if rejected:
                self.callback(FakeRecord(rtype=21,code="unset",err=f"Not authorized for {rejected} schema"))
            else:ready.set()
    def factory(**kw):
        client=RejectStatistics(**kw);clients.append(client);return client
    stream=DatabentoMarketStreams(tmp_path,["AAPL"],["ES.v.0"],client_factory=factory,clock=lambda:T,datasets=("GLBX.MDP3",))
    stream.start()
    assert ready.wait(timeout=3)
    row=stream.snapshot()[0]
    assert row["request"]["explicitly_rejected_schemas"]==["statistics"]
    assert row["request"]["observed_schemas"]==[]
    assert len(clients)==2 and clients[0].stopped
    assert all(c.kw["reconnect_policy"]=="none" for c in clients)
    assert sum(any(r["schema"]=="statistics" for r in c.subscriptions) for c in clients)==1
    assert {r["schema"] for r in clients[-1].subscriptions}=={"definition","mbp-1","ohlcv-1m"}
    stream.close()
    assert all(c.stopped for c in clients)


def test_cme_live_l1_keeps_depth_and_order_flow_unavailable(tmp_path):
    clients=[]
    def factory(**kw):
        client=Client(**kw);clients.append(client);return client
    stream=DatabentoMarketStreams(tmp_path,["AAPL"],["ES.v.0"],client_factory=factory,clock=lambda:T+timedelta(seconds=1),datasets=("GLBX.MDP3",))
    stream.start()
    stream.ingest("GLBX.MDP3",FakeRecord(rtype=22,instrument_id=10,stype_in_symbol="ES.v.0"))
    stream.ingest("GLBX.MDP3",stream_record())
    row=stream.snapshot()[0]
    assert row["status"]=="ok"
    assert row["features"]["trade_volume"]==4
    assert row["features"]["spread_relative"]==.02
    assert row["features"].get("depth_imbalance") is None
    assert row["features"].get("cancel_add_ratio") is None
    assert row["request"]["observed_schemas"]==["mbp-1"]
    assert row["request"]["explicitly_rejected_schemas"]==[]
    assert not {"mbp-10","mbo"}.intersection(row["request"]["schemas"])
    store=MarketInputStore(tmp_path/"joined")
    store.append(symbol="AAPL",source=row["source"],started_at=row["started_at"],received_at=row["received_at"],payload=row["payload"],features=row["features"],status=row["status"],request=row["request"])
    joined=store.join("AAPL",[T+timedelta(seconds=1)],cutoff=T+timedelta(seconds=1))
    assert column(joined,"market_db_cme_es_trade_volume__available")==1
    assert column(joined,"market_db_cme_es_depth_imbalance__available")==0
    assert column(joined,"market_db_cme_es_cancel_add_ratio__available")==0
    stream.close();assert all(c.stopped for c in clients)


def test_cme_l1_does_not_invent_l2_depth_and_l2_is_not_overwritten(tmp_path):
    stream=DatabentoMarketStreams(tmp_path,["AAPL"],["ES.v.0"],client_factory=Client,clock=lambda:T+timedelta(seconds=1),datasets=("GLBX.MDP3",))
    stream.ingest("GLBX.MDP3",FakeRecord(rtype=22,instrument_id=10,stype_in_symbol="ES.v.0"))
    stream.ingest("GLBX.MDP3",stream_record())
    row=stream.snapshot()[0]
    assert row["features"]["trade_volume"]==4
    assert row["features"]["depth_imbalance"] is None
    stream.ingest("GLBX.MDP3",stream_record(kind=10))
    next_top=stream_record();next_top["sequence"]=2;next_top["levels"][0]["bid_sz"]=1
    stream.ingest("GLBX.MDP3",next_top)
    assert stream.snapshot()[0]["features"]["depth_imbalance"]==.5


def test_corporate_cursor_never_skips_fast_quote_and_bar_lane(tmp_path,monkeypatch):
    import time
    class Fmp:
        def __init__(self):self.calls=[]
        def _get_json(self,endpoint,params):self.calls.append(endpoint);return []
    fmp=Fmp();collector=MarketInputCollector(tmp_path,["AAPL","COST"],fmp=fmp,clock=lambda:T)
    monkeypatch.setattr("ml.intraday.market_inputs.research_specs",lambda *a,**k:())
    collector._corporate_cursor=13
    collector._deadline=time.monotonic()+120
    monkeypatch.setattr("ml.intraday.market_inputs.time.sleep",lambda _:None)
    collector._fmp_collect()
    assert fmp.calls[:4]==["quote","quote","historical-chart/5min","historical-chart/5min"]
    assert len(fmp.calls)==24
    fmp.calls=[];collector._deadline=time.monotonic()+120
    collector._fmp_collect()
    assert fmp.calls==["quote","quote","historical-chart/5min","historical-chart/5min"]


def test_fast_budget_records_every_deferred_job_and_reserves_corporate_progress(tmp_path,monkeypatch):
    import time
    class Fmp:
        def __init__(self):self.calls=[]
        def _get_json(self,endpoint,params):self.calls.append(endpoint);return []
    fmp=Fmp();collector=MarketInputCollector(tmp_path,["AAPL","COST"],fmp=fmp,clock=lambda:T)
    monkeypatch.setattr("ml.intraday.market_inputs.research_specs",lambda *a,**k:())
    collector._deadline=time.monotonic()+20 # inside reserved corporate interval
    monkeypatch.setattr("ml.intraday.market_inputs.time.sleep",lambda _:None)
    collector._fmp_collect()
    assert len(collector._deferred)==4
    assert all(r["reason"]=="fast_lane_budget" for r in collector._deferred)
    assert len(fmp.calls)==20 and "quote" not in fmp.calls


def _price_snapshot():
    return {"X":[[1.]],"y":[1],"reference_at":[(T-timedelta(minutes=15)).isoformat()],"inference_X":[[2.]],"feature_names":["price_feature"],"manifest":{},"inference":{"information_available_at":T.isoformat()}}


def test_optional_query_bound_preserves_price_and_records_missing_context(tmp_path,monkeypatch):
    store=MarketInputStore(tmp_path);append(store,at=T-timedelta(seconds=1));append(store)
    monkeypatch.setattr("ml.intraday.market_inputs.MAX_JOIN_RECEIPTS",1)
    original=_price_snapshot()
    out=augment_snapshot(original,store,symbol="AAPL",inference_at=T,cutoff=T)
    assert original==_price_snapshot()
    assert out["X"][0]==[1.]+[0.]*len(MODEL_FEATURE_NAMES)
    assert out["inference_X"][0]==[2.]+[0.]*len(MODEL_FEATURE_NAMES)
    assert out["y"]==[1] and out["inference"]==original["inference"]
    assert out["market_inputs"]["read_status"]["reason"]=="catalog_query_limit"
    assert out["market_inputs"]["inference"]["sources"]=={}


def test_optional_database_read_failure_preserves_price_without_exception_text(tmp_path):
    store=MarketInputStore(tmp_path);store.root.mkdir(parents=True)
    store.database.write_bytes(b"unreadable catalog; secret=DO_NOT_EXPOSE")
    out=augment_snapshot(_price_snapshot(),store,symbol="AAPL",inference_at=T,cutoff=T)
    assert out["X"][0]==[1.]+[0.]*len(MODEL_FEATURE_NAMES)
    assert out["market_inputs"]["read_status"]=={"status":"unavailable","reason":"store_read_unavailable","error_type":"DatabaseError"}
    assert "DO_NOT_EXPOSE" not in json.dumps(out)


def test_optional_catalog_integrity_failure_preserves_price(tmp_path):
    import sqlite3
    store=MarketInputStore(tmp_path);append(store)
    with sqlite3.connect(store.database) as db:db.execute("UPDATE receipts SET features='invalid json'")
    out=augment_snapshot(_price_snapshot(),store,symbol="AAPL",inference_at=T,cutoff=T)
    assert out["market_inputs"]["read_status"]["reason"]=="catalog_integrity_unavailable"
    assert out["X"][0]==[1.]+[0.]*len(MODEL_FEATURE_NAMES)


def test_optional_fallback_does_not_swallow_malformed_snapshot_or_code_error(tmp_path,monkeypatch):
    store=MarketInputStore(tmp_path)
    bad=_price_snapshot();bad["X"]=[]
    with pytest.raises(ValueError,match="rows differ"):
        augment_snapshot(bad,store,symbol="AAPL",inference_at=T,cutoff=T)
    def broken(*a,**kw):raise ValueError("implementation error")
    monkeypatch.setattr(store,"join",broken)
    with pytest.raises(ValueError,match="implementation error"):
        augment_snapshot(_price_snapshot(),store,symbol="AAPL",inference_at=T,cutoff=T)


@pytest.mark.parametrize("stamp,expected",[("2026-01-17T00:15:00+00:00","2026-01-16"),("2026-07-17T00:15:00+00:00","2026-07-16"),("2026-07-17T04:15:00+00:00","2026-07-17")])
def test_fmp_chart_dates_follow_new_york_session_across_dst(stamp,expected):
    source,endpoint,params=next(s for s in fmp_specs("AAPL",datetime.fromisoformat(stamp)) if s[0]=="fmp_5min")
    assert params["from"]==params["to"]==expected
