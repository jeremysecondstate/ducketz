"""Offline tests of real collector integration; no provider or account calls."""
from datetime import datetime, timezone, timedelta
import json
import time
import pytest

from ml.intraday.market_inputs import MarketInputCollector, read_response, VERSION

T = datetime(2026, 10, 16, 18, tzinfo=timezone.utc)


def test_interrupted_immutable_publish_leaves_no_partial_target(tmp_path,monkeypatch):
    from ml.intraday.market_inputs import _write_immutable
    from pathlib import Path
    import os
    target=tmp_path/"retained.json"
    real_link=os.link
    def interrupted(source,destination):
        assert Path(source).read_bytes()==b'complete original bytes'
        assert not Path(destination).exists()
        raise OSError("simulated interruption before publication")
    monkeypatch.setattr("ml.intraday.market_inputs.os.link",interrupted)
    with pytest.raises(OSError):_write_immutable(target,b'complete original bytes')
    assert not target.exists() and not list(tmp_path.glob('.pending-*'))
    monkeypatch.setattr("ml.intraday.market_inputs.os.link",real_link)
    _write_immutable(target,b'complete original bytes')
    assert target.read_bytes()==b'complete original bytes'


def test_same_endpoint_failure_is_not_requested_twice_or_retimed(tmp_path):
    collector=MarketInputCollector(tmp_path,["AAPL"],clock=lambda:T)
    def denied():raise RuntimeError("HTTP 403 secret=DO_NOT_PERSIST")
    first=collector._capture("AAPL","fmp_analysts",{},denied,lambda p,n:{})
    collector.clock=lambda:T+timedelta(seconds=5)
    second=collector._capture("AAPL","fmp_target_consensus",{},
        lambda:pytest.fail("duplicate request"),lambda p,n:{},response_receipt=first)
    assert second["status"]=="unavailable" and second["received_at"]==first["received_at"]
    assert second["payload_ref"]==first["payload_ref"]


def test_extractor_failure_retains_original_response_before_projection(tmp_path):
    collector = MarketInputCollector(tmp_path, ["AAPL"], clock=lambda:T)
    def extract(payload, stamp):
        saved = list((collector.store.root / "raw").rglob("*.json"))
        assert len(saved) == 1
        assert json.loads(saved[0].read_text()) == {"price": 42, "opaque": [1, 2, 3]}
        payload["price"] = 999
        raise ValueError("secret=DO_NOT_PERSIST")
    row = collector._capture("AAPL", "fmp_quote", {},
        lambda:{"price":42,"opaque":[1,2,3],"apiKey":"DO_NOT_PERSIST"}, extract)
    original = read_response(collector.store.root, row["object_path"])
    assert row["status"] == "extraction_unavailable"
    assert original["payload"] == {"price":42,"opaque":[1,2,3]}
    assert "DO_NOT_PERSIST" not in json.dumps(original)


def test_malformed_schwab_quote_retains_native_adapter_response(tmp_path):
    class Schwab:
        def get_equity_quotes(self,symbols):return {"AAPL":["malformed native record"]}
    collector=MarketInputCollector(tmp_path,["AAPL"],schwab=Schwab(),clock=lambda:T)
    collector._deadline=0  # Only the one quote batch; no other requests.
    rows=collector._schwab_collect()
    assert len(rows)==1 and rows[0]["status"]=="extraction_unavailable"
    raw=read_response(collector.store.root,rows[0]["object_path"])
    assert raw["payload"]==["malformed native record"]
    batch=collector.store.root/raw["request"]["adapter_batch_payload_ref"]["path"]
    assert json.loads(batch.read_text())=={"AAPL":["malformed native record"]}


def test_research_global_requests_once_and_shared_response_preserves_clocks(tmp_path, monkeypatch):
    class Fmp:
        def __init__(self): self.calls=[]
        def _get_json(self, endpoint, params):
            self.calls.append((endpoint, params))
            if endpoint == "economic-indicators":
                return [{"name":params["name"], "date":"2026-10-01", "value":3.0}]
            if endpoint == "price-target-consensus":
                return [{"symbol":params["symbol"], "targetConsensus":100,"targetHigh":120,"targetLow":90}]
            return []
    fmp=Fmp()
    collector=MarketInputCollector(tmp_path,["AAPL","COST"],fmp=fmp,clock=lambda:T)
    collector._deadline=time.monotonic()+120
    monkeypatch.setattr("ml.intraday.market_inputs.time.sleep",lambda _:None)
    rows=collector._fmp_collect()
    assert len([x for x in fmp.calls if x[0]=="economic-indicators"]) == 4
    assert len([x for x in fmp.calls if x[0]=="price-target-consensus"]) == 2
    macro=[r for r in rows if r["source"]=="fmp_macro_cpi"]
    assert {r["symbol"] for r in macro} == {"*","AAPL","COST"}
    assert {r["received_at"] for r in macro} == {T.isoformat()}
    assert len({r["payload_ref"]["sha256"] for r in macro}) == 1
    assert all(r["features"]["value"] == 3 for r in macro)
    alias=next(r for r in rows if r["source"]=="fmp_target_consensus" and r["symbol"]=="AAPL")
    original=next(r for r in rows if r["source"]=="fmp_analysts" and r["symbol"]=="AAPL")
    assert alias["request"]["shared_response_receipt_id"] == original["id"]
    assert alias["payload_ref"] == original["payload_ref"]
    fmp.calls=[];collector._deadline=time.monotonic()+120
    collector._fmp_collect()
    assert [x[0] for x in fmp.calls]==["quote","quote","historical-chart/5min","historical-chart/5min"]


def test_empty_gdp_fallback_survives_budget_without_reissuing_empty_window(tmp_path,monkeypatch):
    from ml.intraday.market_input_research import research_specs
    jobs=research_specs("AAPL",T,include_global=True)
    source,endpoint,initial=next(j for j in jobs if j[0]=="fmp_macro_gdp")
    monkeypatch.setattr("ml.intraday.market_inputs.research_specs",
        lambda *a,**kw:((source,endpoint,initial),) if kw.get("include_global") else ())
    monkeypatch.setattr("ml.intraday.market_inputs.time.sleep",lambda _:None)
    class Fmp:
        def __init__(self):self.calls=[]
        def _get_json(self,end,params):
            if end==endpoint:
                self.calls.append(dict(params))
                return [] if params==initial else [{"name":"GDP","date":params["to"],"value":30000}]
            return []
    tick=[T]
    def clock():tick[0]+=timedelta(milliseconds=1);return tick[0]
    fmp=Fmp();first=MarketInputCollector(tmp_path,["AAPL"],fmp=fmp,clock=clock)
    monkeypatch.setattr(first,"_schwab_collect",lambda:[])
    allowed=first._may_request
    def limited(symbol,src):
        if src==source and first._pending_macro:
            first._deferred.append({"symbol":symbol,"source":src,"reason":"cycle_request_budget"})
            return False
        return allowed(symbol,src)
    monkeypatch.setattr(first,"_may_request",limited)
    first.collect(T)
    assert len(fmp.calls)==1 and first._pending_macro
    parent=first._pending_macro_parent
    resumed=MarketInputCollector(tmp_path,["AAPL"],fmp=fmp,clock=clock)
    monkeypatch.setattr(resumed,"_schwab_collect",lambda:[])
    cycle=resumed.collect(T)
    assert len(fmp.calls)==2 and fmp.calls[0]==initial and fmp.calls[1]!=initial
    assert not resumed._pending_macro
    receipts=[r for r in cycle["receipts"] if r["source"]==source and r["symbol"]=="AAPL"]
    assert len(receipts)==1 and receipts[0]["status"]=="ok"
    raw=read_response(resumed.store.root,receipts[0]["object_path"])
    assert raw["features"]["value"]==30000 and raw["request"]["empty_receipt_id"]==parent


def test_schwab_failure_does_not_prevent_independent_stream_close(tmp_path):
    class Streams:
        def __init__(self, broken=False):self.started=0;self.closed=0;self.broken=broken
        def start(self):
            self.started+=1
            if self.broken:raise OSError("fixture")
            return {"status":"started"}
        def close(self):self.closed+=1
    db,sch=Streams(),Streams(True)
    collector=MarketInputCollector(tmp_path,["AAPL"],streams=db,schwab_stream=sch)
    result=collector.start()
    assert result["databento"]["status"]=="started"
    assert result["schwab"]=={"status":"unavailable","error_type":"OSError"}
    collector.close()
    assert db.closed==sch.closed==1


def test_later_snapshot_does_not_retimestamp_native_receipt_or_broadcast_foreign_symbol(tmp_path,monkeypatch):
    native=T-timedelta(minutes=1)
    class Streams:
        def snapshot(self):
            return [{"symbol":s,"source":"schwab_stream_quote","started_at":native,
                "received_at":native,"event_at":native,"payload":{"native":s},
                "status":"ok","features":{"last":100}} for s in ["AAPL","MSFT"]]
    collector=MarketInputCollector(tmp_path,["AAPL","COST"],clock=lambda:T,schwab_stream=Streams())
    monkeypatch.setattr(collector,"_schwab_collect",lambda:[])
    monkeypatch.setattr(collector,"_fmp_collect",lambda:[])
    result=collector.collect(T)
    assert VERSION=="intraday-receipt-market-inputs-v2"
    assert len(result["receipts"])==1
    assert result["receipts"][0]["symbol"]=="AAPL"
    assert result["receipts"][0]["received_at"]==native.isoformat()
