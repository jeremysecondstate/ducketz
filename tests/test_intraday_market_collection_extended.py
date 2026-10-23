from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ml.intraday.market_collection import CollectionResult, SourceRequest
from ml.intraday.market_collection_adapters import FmpAdapter, default_requests
from ml.intraday.market_collection_extended import (
    canonical_fmp_fields, collect_extended, extended_requests, fetch_full_schwab_chain, handles,
)

START = datetime(2026, 10, 16, 19, 45, tzinfo=timezone.utc)
END = START + timedelta(minutes=15)
SYMBOLS = ("DOCU", "DBX", "SDGR", "QBTS", "PYPL", "GLOB", "OUST", "ABCL", "MRNA", "RR", "PDYN")


def request(endpoint="news/stock"):
    return next(r for r in extended_requests(["SDGR"]) if r.endpoint == endpoint)


def row(n, **kwargs):
    return {"symbol": "SDGR", "raw": {"symbol": "SDGR", "id": n, **kwargs},
            "published_at": None, "publication_time_basis": "receipt_only"}


def collect(fetch, endpoint="news/stock", bound=1000):
    return collect_extended(request(endpoint), START, END, bound, fmp_fetch_page=fetch)


def test_exact_symbol_inventory_and_unique_semantic_sources():
    requests = extended_requests(SYMBOLS)
    assert len(requests) == len(SYMBOLS)*14
    assert {r.symbol for r in requests} == set(SYMBOLS)
    assert all(r.capability == "UNPROBED" for r in requests)
    assert len({r.identity for r in requests}) == len(requests)
    for symbol in SYMBOLS:
        subset = [r for r in requests if r.symbol == symbol]
        assert len({r.source_id for r in subset}) == 14
        assert all(handles(r) for r in subset)
    combined = default_requests(["SDGR"])
    assert all(r in combined for r in extended_requests(["SDGR"]))
    chains = [r for r in combined if r.provider == "schwab" and r.endpoint == "chains"]
    assert len(chains) == 1 and chains[0].params == {"surface": "full"}
    assert not handles(chains[0])


@pytest.mark.parametrize("symbol", ["SDGR,OUST", "*", "../SDGR", ""])
def test_inventory_rejects_broad_or_malformed_symbols(symbol):
    with pytest.raises(ValueError):
        extended_requests([symbol])


def test_dcf_ratio_requires_valid_same_row_price_and_finite_values():
    fields = canonical_fmp_fields({"dcf": 150, "Stock Price": 100}, "discounted-cash-flow")
    assert fields == {"dcf_value": 150, "dcf_upside_fraction": .5, "dcf_reference_price": 100}
    for bad in (0, -1, None, float("inf"), "bad", True):
        fields = canonical_fmp_fields({"dcf": 150, "Stock Price": bad}, "levered-discounted-cash-flow")
        assert fields["dcf_upside_fraction"] is None
    assert canonical_fmp_fields({"dcf": float("inf"), "stockPrice": 10}, "discounted-cash-flow")["dcf_value"] is None
    assert canonical_fmp_fields({"dcf": 1e308, "stockPrice": 1e-308}, "discounted-cash-flow")["dcf_upside_fraction"] is None


def test_analyst_counts_are_nonnegative_integers_and_missing_stays_missing():
    raw = {"strongBuy": 3, "buy": 2, "hold": 4, "sell": 1, "strongSell": 0}
    fields = canonical_fmp_fields(raw, "grades-consensus")
    assert fields["analyst_count"] == 10 and fields["analyst_bullish_fraction"] == .5
    for bad in (-1, 1.5, None, True, float("inf")):
        fields = canonical_fmp_fields({**raw, "buy": bad}, "grades-consensus")
        assert fields["analyst_buy"] is None
        assert fields["analyst_count"] is None and fields["analyst_bullish_fraction"] is None
    historic = {"analystRatings"+k[0].upper()+k[1:]: v for k, v in raw.items()}
    assert canonical_fmp_fields(historic, "grades-historical")["analyst_count"] == 10
    overflow = canonical_fmp_fields({key: 1e308 for key in raw}, "grades-consensus")
    assert overflow["analyst_count"] is None and overflow["analyst_bullish_fraction"] is None


def test_short_page_complete_and_news_window_is_bounded_and_recorded():
    calls = []
    def fetch(req, *args):
        calls.append(req)
        return CollectionResult([row(1), row(2)])
    result = collect(fetch)
    assert result.status == "COMPLETE" and len(calls) == 1
    assert result.evidence["pagination_exhausted_within_request"] is True
    assert calls[0].params == {"symbols": "SDGR", "page": 0, "limit": 100,
                               "from": "2026-10-09", "to": "2026-10-16"}
    assert result.evidence["pages"][0]["request_params"] == calls[0].params
    assert all(r["event_type"] == "stock_news" for r in result.records)
    assert "no claim to complete history" in result.evidence["history_claim"]


def test_three_full_pages_are_partial_even_without_local_record_cap():
    calls = []
    def fetch(req, *args):
        calls.append(req.params["page"])
        return CollectionResult([row(req.params["page"]*100+i) for i in range(100)])
    result = collect(fetch)
    assert calls == [0, 1, 2] and len(result.records) == 300
    assert result.status == "PARTIAL" and not result.evidence["pagination_exhausted_within_request"]


def test_overlap_deduplicated_but_repeated_full_page_is_partial():
    def fetch(req, *args):
        return CollectionResult([row(i) for i in range(100)])
    result = collect(fetch)
    assert result.status == "PARTIAL" and len(result.records) == 100
    assert "repeated" in result.reason
    def overlap(req, *args):
        return CollectionResult([row(i) for i in (range(100) if req.params["page"] == 0 else range(99, 102))])
    result = collect(overlap)
    assert result.status == "COMPLETE" and len(result.records) == 102


def test_later_page_failure_preserves_received_rows_without_raw_error_secrets():
    def fetch(req, *args):
        if req.params["page"]:
            raise TimeoutError("https://example.test?apikey=PRIVATE_SECRET")
        return CollectionResult([row(i) for i in range(100)])
    result = collect(fetch)
    assert result.status == "PARTIAL" and len(result.records) == 100
    assert result.evidence["pages"][1]["status"] == "UNAVAILABLE"
    assert "PRIVATE_SECRET" not in str(result)


def test_first_failure_is_not_claimed_as_successful_empty():
    def fetch(*args):
        raise PermissionError("not authorized")
    with pytest.raises(PermissionError):
        collect(fetch)


def test_local_bound_and_limited_endpoint_are_truthful_partial():
    result = collect(lambda *args: CollectionResult([row(i) for i in range(9)]), bound=5)
    assert result.status == "PARTIAL" and len(result.records) == 5
    result = collect(lambda *args: CollectionResult([row(i) for i in range(100)]), "ratings-historical")
    assert result.status == "PARTIAL" and len(result.records) == 100


def test_symbol_mismatch_and_invalid_page_schema_fail_closed():
    with pytest.raises(ValueError, match="symbol/raw"):
        collect(lambda *args: CollectionResult([{**row(0), "symbol": "OUST"}]))
    with pytest.raises(ValueError, match="symbol/raw"):
        collect(lambda *args: CollectionResult([{"symbol": "SDGR"}]))
    with pytest.raises(TypeError):
        collect(lambda *args: [])


def test_real_fmp_normalizer_keeps_estimate_horizon_separate_from_publication():
    class Provider:
        def _get_json(self, endpoint, params):
            assert endpoint == "analyst-estimates" and params["symbol"] == "SDGR"
            return [{"symbol": "SDGR", "date": "2028-12-31", "epsAvg": -1.25,
                     "revenueAvg": 100000, "publishedDate": "2026-10-16 12:00:00"}]
    result = FmpAdapter(provider_factory=Provider)(request("analyst-estimates"), START, END, 1000)
    record = result.records[0]
    assert record["analyst_estimate_period_end"] == "2028-12-31"
    assert "period_end" not in record
    assert record["analyst_eps_estimate"] == -1.25 and record["analyst_revenue_estimate"] == 100000
    assert record["published_at"] is None and record["event_at"] is None
    assert record["publication_time_basis"] == "receipt_only"


def test_real_fmp_normalizer_retains_explicit_aware_news_publication():
    class Provider:
        def _get_json(self, endpoint, params):
            assert endpoint == "news/press-releases" and params["symbols"] == "SDGR"
            return [{"symbol": "SDGR", "title": "Fixture", "publishedDate": "2026-10-16T18:00:00Z"}]
    result = FmpAdapter(provider_factory=Provider)(request("news/press-releases"), START, END, 1000)
    record = result.records[0]
    assert record["published_at"] == "2026-10-16T18:00:00+00:00"
    assert record["event_at"] == record["published_at"] and record["event_type"] == "press_release"


def mock_http(monkeypatch, payload):
    calls = []
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return payload
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()
    monkeypatch.setattr("requests.get", get)
    return SimpleNamespace(_headers=lambda: {"Authorization": "fixture-only"}), calls


def test_full_schwab_surface_has_no_local_strike_or_expiry_filters(monkeypatch):
    payload = {"symbol": "SDGR", "status": "SUCCESS", "callExpDateMap": {}, "putExpDateMap": {}}
    session, calls = mock_http(monkeypatch, payload)
    assert fetch_full_schwab_chain(session, "SDGR") is payload
    url, options = calls[0]
    assert url.endswith("/marketdata/v1/chains") and "accounts" not in url
    assert options["headers"] == {"Authorization": "fixture-only"}
    assert options["params"] == {"symbol": "SDGR", "contractType": "ALL", "includeUnderlyingQuote": "true",
                                  "strategy": "SINGLE", "range": "ALL"}
    assert options["timeout"] == 20


def test_schwab_fundamental_keeps_unknown_units_raw_and_uses_receipt_time(monkeypatch):
    raw = {"symbol": "SDGR", "fundamental": {"peRatio": 12.3, "marketCap": 4000, "netProfitMarginTTM": 7}}
    session, calls = mock_http(monkeypatch, {"instruments": [raw]})
    result = collect_extended(request("instruments"), START, END, 100, schwab_session=session)
    record = result.records[0]
    assert result.status == "COMPLETE" and record["raw"] == raw
    assert record["pe_ratio"] == 12.3 and record["market_cap"] is None and record["profit_margin"] is None
    assert record["publication_time_basis"] == "receipt_only" and record["published_at"] is None
    assert calls[0][0].endswith("/marketdata/v1/instruments")
    assert calls[0][1]["params"] == {"symbol": "SDGR", "projection": "fundamental"}


@pytest.mark.parametrize("change", [{"status": "FAILED"}, {"symbol": "OUST"}, {"putExpDateMap": []}])
def test_full_chain_errors_wrong_symbols_or_invalid_maps_fail_closed(monkeypatch, change):
    payload = {"symbol": "SDGR", "status": "SUCCESS", "callExpDateMap": {}, "putExpDateMap": {}, **change}
    session, _ = mock_http(monkeypatch, payload)
    with pytest.raises(ValueError):
        fetch_full_schwab_chain(session, "SDGR")


@pytest.mark.parametrize("payload", [{}, [], {"instruments": [{}]}, {"instruments": [{"symbol": "OUST", "fundamental": {}}]}])
def test_schwab_invalid_instrument_schema_is_not_successful_empty(monkeypatch, payload):
    session, _ = mock_http(monkeypatch, payload)
    with pytest.raises(ValueError):
        collect_extended(request("instruments"), START, END, 100, schwab_session=session)


def test_unrelated_requests_and_invalid_bounds_are_rejected():
    unrelated = SourceRequest("fmp", "fundamentals", "SDGR", "stable", "x", "x", {})
    with pytest.raises(ValueError):
        collect_extended(unrelated, START, END, 100)
    for bound in (0, -1, 1.5, True):
        with pytest.raises(ValueError):
            collect_extended(request(), START, END, bound)
