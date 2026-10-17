from datetime import datetime, timedelta, timezone
import json
import threading
from types import SimpleNamespace

import pytest

from ml.intraday.provider_recovery import (
    ProviderRequestError, RetryLedger, fmp_request, retry_after_seconds, safe_failure,
)
from ml.intraday.market_input_schwab_stream import SchwabMarketStream

NOW = datetime(2026, 10, 17, 18, tzinfo=timezone.utc)


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_only_explicit_transient_http_retries(status):
    assert safe_failure(ProviderRequestError(status=status), NOW)["transient"]


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 405, 422, 501])
def test_rejected_request_does_not_assert_entitlement_or_retry(status):
    result = safe_failure(ProviderRequestError(status=status), NOW)
    assert result["category"] == "request_rejected" and not result["transient"]
    assert result["http_status"] == status


def test_retry_after_and_redaction():
    assert retry_after_seconds("Sat, 17 Oct 2026 18:01:00 GMT", NOW) == 60
    error = ProviderRequestError(status=429, cause="apikey=secret Bearer tok https://example.test/?apikey=secret", retry_after="7")
    result = safe_failure(error, NOW, secrets=("secret",))
    assert result["retry_after_seconds"] == 7
    assert "secret" not in result["cause"] and "example.test" not in result["cause"] and " tok" not in result["cause"]


def test_pending_survives_restart_and_continues_after_per_pass_bound(tmp_path):
    now, attempts, waits, events = [NOW], [], [], []
    def wait(seconds):
        waits.append(seconds)
        now[0] += timedelta(seconds=seconds)
    def fail():
        attempts.append(now[0])
        raise TimeoutError("temporary")
    ledger = RetryLedger(tmp_path / "state.json", clock=lambda: now[0], wait=wait)
    assert ledger.run("job", fail, events.append)["status"] == "retry_pending"
    assert len(attempts) == 2 and waits == [2]
    ledger = RetryLedger(tmp_path / "state.json", clock=lambda: now[0], wait=wait)
    assert ledger.run("job", lambda: [], events.append)["payload"] == []
    assert waits == [2, 2] and events[-1]["attempt_number"] == 3
    assert ledger.state("job")["status"] == "ok"


def test_retry_after_defers_with_durable_state_and_never_waits_past_budget(tmp_path):
    events, waits = [], []
    ledger = RetryLedger(tmp_path / "state.json", clock=lambda: NOW, wait=waits.append)
    def fail():
        raise ProviderRequestError(status=429, retry_after="300")
    result = ledger.run("job", fail, events.append, deadline=NOW + timedelta(seconds=20))
    assert result["status"] == "deferred" and len(events) == 1 and not waits
    assert ledger.state("job")["next_retry_at"] == (NOW + timedelta(seconds=300)).isoformat()


def test_single_request_worker_defers_early_retry_without_blocking_other_jobs(tmp_path):
    now,waits,events=[NOW],[],[]
    ledger=RetryLedger(tmp_path/"state.json",clock=lambda:now[0],wait=waits.append)
    ledger.defer("newer",seconds=3);ledger.defer("older",seconds=2)
    result=ledger.run("older",lambda:pytest.fail("early request"),events.append,max_attempts=1)
    assert result["status"]=="deferred" and not waits and not events
    now[0]+=timedelta(seconds=4)
    assert ledger.due_keys()==("older","newer")


def test_manual_stop_interrupts_wait_and_cannot_start_another_request(tmp_path):
    stop, events = threading.Event(), []
    def wait(seconds):
        assert seconds == 2
        stop.set()
        return True
    ledger = RetryLedger(tmp_path / "state.json", clock=lambda: NOW, wait=wait)
    def fail():
        raise TimeoutError()
    result = ledger.run("job", fail, events.append, stop_event=stop)
    assert result["status"] == "stopped" and len(events) == 1
    assert ledger.state("job")["status"] == "retry_pending"


def test_error_attempt_is_persisted_before_pending_or_success(tmp_path):
    path = tmp_path / "state.json"
    ledger = RetryLedger(path, clock=lambda: NOW)
    seen = []
    def retain(event):
        assert not path.exists()
        seen.append(event)
    result = ledger.run("job", lambda: {"rows": []}, retain)
    assert result["status"] == "ok" and seen[0]["received_at"] == NOW.isoformat()


def test_permanent_request_does_not_loop_in_this_or_later_pass(tmp_path):
    events = []
    ledger = RetryLedger(tmp_path / "state.json", clock=lambda: NOW)
    def fail():
        raise ProviderRequestError(status=404)
    assert ledger.run("job", fail, events.append)["status"] == "request_rejected"
    assert ledger.run("job", fail, events.append)["attempts"] == 0
    assert len(events) == 1


def test_fmp_transport_preserves_retry_after_without_disclosing_key():
    provider = SimpleNamespace(_validate_config=lambda: None, base_url="https://example.test", api_key="topsecret", timeout_seconds=10)
    def get(url, **kwargs):
        assert kwargs["params"] == {"name": "GDP", "apikey": "topsecret"}
        return SimpleNamespace(status_code=503, headers={"Retry-After": "4"}, json=lambda: {"error": "apikey=topsecret"})
    with pytest.raises(ProviderRequestError) as caught:
        fmp_request(provider, "economic-indicators", {"name": "GDP"}, http_get=get)
    result = safe_failure(caught.value, NOW)
    assert result["transient"] and result["retry_after_seconds"] == 4
    assert "topsecret" not in str(caught.value) and "topsecret" not in json.dumps(result)


def test_chain_retry_continues_in_same_quarter_without_blocking_other_symbols(tmp_path):
    now, events, calls = [NOW], [], []
    value = SchwabMarketStream(tmp_path, ["MU", "SNDK"], clock=lambda: now[0], persist_raw=lambda payload, metadata: events.append((payload, metadata)))
    def request(endpoint, params):
        calls.append(params["symbol"])
        if len(calls) == 1:
            raise TimeoutError("temporary")
        return {"symbol": params["symbol"], "status": "SUCCESS", "callExpDateMap": {}, "putExpDateMap": {}}
    value._http = request
    assert not value._refresh_one_chain()
    assert value._refresh_one_chain()
    assert calls == ["MU", "SNDK"]
    assert not value._refresh_one_chain()
    now[0] += timedelta(seconds=2)
    assert value._refresh_one_chain() and calls == ["MU", "SNDK", "MU"]
    assert events[0][1]["kind"] == "option_chain_attempt"
    assert events[0][0]["error"]["transient"]
    value.close()


def test_restarted_chain_worker_recovers_due_job_and_stop_preserved(tmp_path):
    now = [NOW]
    first = SchwabMarketStream(tmp_path, ["MU"], clock=lambda: now[0], persist_raw=lambda *a: None)
    first._http = lambda *a: (_ for _ in ()).throw(TimeoutError())
    assert not first._refresh_one_chain()
    first.close()
    now[0] += timedelta(seconds=2)
    second = SchwabMarketStream(tmp_path, ["MU"], clock=lambda: now[0], persist_raw=lambda *a: None)
    second._http = lambda *a: {"symbol": "MU", "status": "SUCCESS", "callExpDateMap": {}, "putExpDateMap": {}}
    assert second._refresh_one_chain()
    second.close()
    assert not second._refresh_one_chain()


def test_body_overflow_is_explicit_scope_error_then_all_expirations_are_partitioned(tmp_path):
    now, calls, retained = [NOW], [], []
    stream=SchwabMarketStream(tmp_path,["MU"],clock=lambda:now[0],persist_raw=lambda p,m:retained.append((p,m)))
    def request(endpoint,params):
        calls.append((endpoint,dict(params)))
        if len(calls)==1:
            raise ProviderRequestError(status=502,cause="protocol.http.TooBigBody Body buffer overflow")
        if endpoint=="expirationchain":
            return {"expirationList":[{"expirationDate":"2026-10-23"},{"expirationDate":"2026-10-30"}]}
        key="MU 261023C00100000" if params["fromDate"]=="2026-10-23" else "MU 261030C00100000"
        return {"symbol":"MU","status":"SUCCESS","callExpDateMap":{"date":{"strike":[{"symbol":key}]}},"putExpDateMap":{}}
    stream._http=request
    for _ in range(3):
        assert not stream._refresh_one_chain()
        now[0]+=timedelta(seconds=2)
    assert stream._refresh_one_chain()
    assert len(stream._chains["MU"])==2 and stream._chain_details["MU"]["partition_count"]==2
    assert calls[2][1]["fromDate"]==calls[2][1]["toDate"]=="2026-10-23"
    assert len(retained)==4
    assert retained[0][0]["error"]["category"]=="request_scope_too_large"
    assert not retained[0][0]["error"]["transient"]
    stream.close()


def test_collector_recovers_due_transient_job_after_restart_without_ttl_wait(tmp_path):
    from ml.intraday.market_inputs import MarketInputCollector
    now=[NOW]
    class Fmp:
        def __init__(self,fail):self.fail=fail;self.calls=0
        def _get_json(self,endpoint,params):
            self.calls+=1
            if self.fail:raise TimeoutError("temporary")
            return [{"price":10,"timestamp":int(now[0].timestamp())}]
    first=MarketInputCollector(tmp_path,["MU"],clock=lambda:now[0],fmp=Fmp(True))
    first._capture("MU","fmp_quote",{"endpoint":"quote","params":{"symbol":"MU"}},lambda:first._fmp_request("quote",{"symbol":"MU"}),lambda p,n:{"last":10})
    first.close()
    now[0]+=timedelta(seconds=2)
    second=MarketInputCollector(tmp_path,["MU"],clock=lambda:now[0],fmp=Fmp(False))
    result=second.recover_pending()
    assert second.fmp.calls==1 and result["receipts"][0]["status"]=="ok"
    assert not second._retry_jobs
    second.close()


def test_quote_batch_recovery_preserves_native_payload_and_actual_receipt(tmp_path):
    from ml.intraday.market_inputs import MarketInputCollector,read_response
    now=[NOW]
    class Schwab:
        calls=0
        def get_equity_quotes(self,symbols):
            self.calls+=1
            if self.calls==1:raise TimeoutError("temporary")
            return {"MU":{"quote":{"lastPrice":100,"quoteTime":int(now[0].timestamp()*1000)},"realtime":True,"quoteType":"NBBO"}}
    schwab=Schwab();collector=MarketInputCollector(tmp_path,["MU"],schwab=schwab,clock=lambda:now[0])
    initial=collector._schwab_collect()
    assert initial[0]["status"]=="unavailable"
    assert collector.recover_pending() is None
    now[0]+=timedelta(seconds=2)
    result=collector.recover_pending()
    row=next(r for r in result["receipts"] if r["symbol"]=="MU")
    assert row["status"]=="ok" and row["features"]["last"]==100
    assert row["received_at"]==now[0].isoformat()
    assert read_response(collector.store.root,row["object_path"])["payload"]["realtime"] is True
    collector.close()
    assert collector.recover_pending() is None and schwab.calls==2


def test_gdp_period_lag_uses_three_bounded_windows_until_genuine_record(tmp_path,monkeypatch):
    import time
    from ml.intraday.market_inputs import MarketInputCollector
    from ml.intraday.market_input_research import research_specs
    job=next(j for j in research_specs("MU",NOW,include_global=True) if j[0]=="fmp_macro_gdp")
    monkeypatch.setattr("ml.intraday.market_inputs.research_specs",lambda *a,**kw:(job,) if kw.get("include_global") else ())
    monkeypatch.setattr("ml.intraday.market_inputs.time.sleep",lambda _:None)
    class Fmp:
        calls=[]
        def _get_json(self,endpoint,params):
            if endpoint!="economic-indicators":return []
            self.calls.append(dict(params))
            return [{"name":"GDP","date":"2026-04-01","value":30000}] if len(self.calls)==3 else []
    fmp=Fmp();collector=MarketInputCollector(tmp_path,["MU"],fmp=fmp,clock=lambda:NOW)
    collector._deadline=time.monotonic()+120
    rows=collector._fmp_collect()
    assert len(fmp.calls)==3
    for params in fmp.calls:
        assert (datetime.fromisoformat(params["to"])-datetime.fromisoformat(params["from"])).days==89
    assert all(a["from"]>b["to"] for a,b in zip(fmp.calls,fmp.calls[1:]))
    final=[row for row in rows if row["source"]=="fmp_macro_gdp" and row["symbol"]=="MU"][-1]
    assert final["status"]=="ok" and final["features"]["value"]==30000
    assert final["received_at"]==NOW.isoformat()
    collector.close()


def test_idle_recovery_poll_does_not_emit_producer_or_make_datastore(tmp_path,monkeypatch):
    from ml.intraday.market_inputs import MarketInputCollector
    collector=MarketInputCollector(tmp_path,["MU"],clock=lambda:NOW)
    monkeypatch.setattr(collector.store,"capture_producer",lambda:pytest.fail("idle poll writes producer"))
    assert collector.recover_pending() is None
    assert not collector.store.root.exists()
