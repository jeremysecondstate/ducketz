from datetime import datetime, timedelta, timezone
from dataclasses import replace

import pytest
import requests

from ml.intraday.market_collection import CollectionFailure, QuarterHourMarketCollector, SourceRequest
from ml.intraday.market_collection_fmp_commodities import collect, native_commodity_requests

START = datetime(2026, 10, 16, 19, 45, tzinfo=timezone.utc)
END = START + timedelta(minutes=15)


def test_legacy_batch_never_initializes_provider():
    request = SourceRequest("fmp", "cross_asset", "*", "stable", "commodity-quote",
        "batch-commodity-quotes", {"configured_symbols": ["CLUSD", "BZUSD"]})
    def forbidden():
        pytest.fail("Legacy bulk request must not call the provider")
    result = collect(request, START, END, 100, provider_factory=forbidden)
    assert result.status == "UNSUPPORTED" and result.records == []
    assert result.evidence["provider_called"] is False


def test_native_scope_ids_are_distinct_and_clock_is_not_rewritten():
    scope = native_commodity_requests(["CLUSD", "BZUSD", "NGUSD"])
    assert len({request.source_id for request in scope}) == 3
    stamp = int((START - timedelta(days=1)).timestamp())
    calls = []
    class Provider:
        def _get_json(self, endpoint, params):
            calls.append((endpoint, params))
            return [{"symbol": "BZUSD", "timestamp": stamp, "price": 100, "previousClose": 98}]
    result = collect(scope[1], START, END, 100, provider_factory=Provider)
    assert calls == [("quote", {"symbol": "BZUSD"})]
    assert result.records[0]["event_at"] == (START - timedelta(days=1)).isoformat()
    assert result.records[0]["raw"]["timestamp"] == stamp
    assert result.evidence["configured_symbols"] == ["BZUSD"]


def test_symbol_denial_cannot_block_other_symbols_or_leak_authenticated_url(tmp_path):
    calls = []
    class Provider:
        def _get_json(self, endpoint, params):
            calls.append(params["symbol"])
            if params["symbol"] == "CLUSD":
                response = requests.Response()
                response.status_code = 402
                raise requests.HTTPError("https://example.invalid/?apikey=SECRET", response=response)
            return [{"symbol": params["symbol"], "timestamp": int(END.timestamp()), "price": 100}]
    now = [END + timedelta(seconds=10)]
    collector = QuarterHourMarketCollector(tmp_path, symbols=["SDGR"],
        requests=native_commodity_requests(["CLUSD", "BZUSD"]), clock=lambda: now[0],
        adapters={"fmp": lambda *args: collect(*args, provider_factory=Provider)})
    collector.enqueue(START)
    receipts = collector.step(max_requests=2)
    assert sorted(row["status"] for row in receipts) == ["COMPLETE", "NOT_AUTHORIZED"]
    now[0] += timedelta(minutes=15)
    collector.enqueue(END)
    collector.step(max_requests=2)
    assert calls.count("CLUSD") == 1 and calls.count("BZUSD") == 2
    assert all(b"SECRET" not in path.read_bytes() for path in tmp_path.rglob("*.json"))


@pytest.mark.parametrize("payload", [{"price": 1}, [{"symbol": "USO", "price": 1}], [None]])
def test_invalid_or_substituted_identity_fails_closed(payload):
    class Provider:
        def _get_json(self, *args):
            return payload
    with pytest.raises(CollectionFailure):
        collect(native_commodity_requests(["CLUSD"])[0], START, END, 100, provider_factory=Provider)


def test_empty_or_undated_result_does_not_invent_an_event():
    class Provider:
        payload = []
        def _get_json(self, *args):
            return self.payload
    request = native_commodity_requests(["BZUSD"])[0]
    assert collect(request, START, END, 100, provider_factory=Provider).records == []
    Provider.payload = [{"symbol": "BZUSD", "price": 1, "synthetic": True}]
    result = collect(request, START, END, 100, provider_factory=Provider)
    assert result.records[0]["event_at"] is None
    assert result.records[0]["raw"]["synthetic"] is True


def test_malformed_scope_is_rejected_before_io():
    request = replace(native_commodity_requests(["BZUSD"])[0], params={"symbol": "BZUSD"})
    with pytest.raises(ValueError):
        collect(request, START, END, 100, provider_factory=lambda: pytest.fail("unexpected I/O"))
