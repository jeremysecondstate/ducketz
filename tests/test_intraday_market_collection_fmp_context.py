"""Offline FMP scope, date, source identity, pagination and causal-clock checks."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from ml.intraday.market_collection_fmp_context import (
    INDICATORS, MACRO_NAMES, collect_context, context_catalog, context_requests, handles,
)

START = datetime(2026, 10, 16, 19, 45, tzinfo=timezone.utc)
END = START + timedelta(minutes=15)


class Provider:
    def __init__(self, response):
        self.response, self.calls = response, []
    def _get_json(self, endpoint, params):
        self.calls.append((endpoint, dict(params)))
        return self.response(endpoint, params) if callable(self.response) else self.response


def request(name="sma"):
    return next(r for r in context_requests(["SDGR"]) if
        r.endpoint == "technical-indicators/"+name or r.params.get("name") == name)


def collect(provider, name="sma", bound=1000, start=START, end=END):
    return collect_context(request(name), start, end, bound, fmp_provider=provider)


def raw(name="sma", value=10.5, **extra):
    return {"date": "2026-10-16 15:45:00", "open": 10, "high": 12, "low": 9,
            "close": 11, "volume": 100, name: value, **extra}


def test_inventory_exact_scope_and_semantic_source_separation():
    symbols = ("DOCU", "DBX", "SDGR", "QBTS", "PYPL", "GLOB", "OUST", "ABCL", "MRNA", "RR", "PDYN")
    requests = context_requests(symbols)
    assert len(requests) == 9*11+4 and all(handles(r) for r in requests)
    assert {r.symbol for r in requests if r.source_kind == "technical"} == set(symbols)
    assert {r.params["name"] for r in requests if r.source_kind == "macro"} == set(MACRO_NAMES)
    assert all(r.symbol == "*" for r in requests if r.source_kind == "macro")
    assert len({r.identity for r in requests}) == len(requests)
    assert len({r.source_id for r in requests}) == 13
    assert all(r.capability == "UNPROBED" for r in requests)
    changed = replace(request(), params={**request().params, "periodLength": 20})
    assert changed.source_id != request().source_id


@pytest.mark.parametrize("symbols", [[], ["*"], ["SDGR,OUST"], ["SDGR", "SDGR"]])
def test_inventory_rejects_unbounded_or_duplicate_symbol_scope(symbols):
    with pytest.raises(ValueError):
        context_requests(symbols)


def test_technical_exact_request_and_completed_open_close_clock():
    provider = Provider([raw(), raw(date="2026-10-16 16:00:00")])
    result = collect(provider)
    assert result.status == "COMPLETE" and len(result.records) == 1
    row = result.records[0]
    assert row["event_at"] == START.isoformat() and row["bar_end_at"] == END.isoformat()
    assert row["available_at"] == END.isoformat()
    assert row["technical_value"] == 10.5 and row["technical_reference_close"] == 11
    assert row["period_length"] == 10 and row["timeframe"] == "15min"
    assert provider.calls == [("technical-indicators/sma", {"symbol": "SDGR", "periodLength": 10,
        "timeframe": "15min", "from": "2026-10-02", "to": "2026-10-16"})]
    assert result.evidence["excluded_unfinished_records"] == 1
    assert result.evidence["excluded_unfinished_raw"][0]["date"] == "2026-10-16 16:00:00"


def test_winter_exchange_clock_and_aware_provider_clock_are_not_fixed_offset():
    start = datetime(2026, 12, 16, 20, 45, tzinfo=timezone.utc)
    result = collect(Provider([raw(date="2026-12-16 15:45:00")]), start=start, end=start+timedelta(minutes=15))
    assert result.records[0]["event_at"] == start.isoformat()
    result = collect(Provider([raw(date=START.isoformat())]))
    assert result.records[0]["event_at"] == START.isoformat()


@pytest.mark.parametrize("value", [None, "NaT", "2026-10-16", "2026-10-16 15:46:00", 1792179900])
def test_bad_or_wrong_frequency_technical_timestamp_fails_closed(value):
    with pytest.raises(ValueError):
        collect(Provider([raw(date=value)]))


@pytest.mark.parametrize("name,value", [("rsi", 101), ("adx", -1), ("williams", 1),
    ("williams", -101), ("standarddeviation", -1), ("ema", float("inf")), ("sma", True)])
def test_invalid_values_remain_raw_but_are_explicitly_missing(name, value):
    result = collect(Provider([raw(name, value)]), name)
    assert result.records[0]["technical_value"] is None
    assert result.evidence["missing_or_invalid_values"] == 1


def test_standard_deviation_provider_alias_is_normalized():
    result = collect(Provider([raw("standardDeviation", 2)]), "standarddeviation")
    assert result.records[0]["technical_name"] == "standard_deviation"
    assert result.records[0]["technical_value"] == 2


def test_duplicate_records_removed_and_same_bar_conflicts_are_partial():
    result = collect(Provider([raw(), raw()]))
    assert len(result.records) == 1 and result.status == "COMPLETE"
    assert result.evidence["duplicates_removed"] == 1
    result = collect(Provider([raw(), raw(value=12)]))
    assert len(result.records) == 2 and result.status == "PARTIAL"
    assert result.evidence["conflicting_bar_records"] == 1


def test_cap_retains_newest_completed_values_and_marks_partial():
    result = collect(Provider([raw(date="2026-10-16 15:30:00"), raw()]), bound=1)
    assert result.status == "PARTIAL" and len(result.records) == 1
    assert result.records[0]["event_at"] == START.isoformat()


@pytest.mark.parametrize("payload", [{}, {"error": "denied"}, {"data": {}}, [None], "error"])
def test_malformed_provider_payload_never_becomes_successful_empty(payload):
    with pytest.raises(ValueError):
        collect(Provider(payload))


def test_symbol_and_date_window_are_validated():
    with pytest.raises(ValueError, match="another symbol"):
        collect(Provider([raw(symbol="OUST")]))
    with pytest.raises(ValueError, match="date bounds"):
        collect(Provider([raw(date="2025-01-02 15:45:00")]))


def test_macro_slices_are_bounded_and_period_is_not_publication():
    def response(endpoint, params):
        assert endpoint == "economic-indicators"
        return [{"name": "GDP", "date": "2026-07-01", "value": 31422.526}] if params["to"] < "2026-08-01" else []
    provider = Provider(response)
    result = collect(provider, "GDP")
    assert result.status == "COMPLETE" and len(result.records) == 1
    assert [params for _, params in provider.calls] == [
        {"name": "GDP", "from": "2026-07-19", "to": "2026-10-16"},
        {"name": "GDP", "from": "2026-04-20", "to": "2026-07-18"}]
    row = result.records[0]
    assert row["macro_value"] == 31422.526 and row["macro_period_date"] == "2026-07-01"
    assert row["macro_unit"] == "billion USD"
    assert row["event_at"] is None and row["published_at"] is None
    assert row["publication_time_basis"] == "receipt_only"


def test_macro_explicit_publication_clock_is_retained_separately():
    def response(endpoint, params):
        return [{"name": "CPI", "date": params["from"], "value": 320,
                 "publishedDate": "2026-10-15T12:30:00Z"}]
    result = collect(Provider(response), "CPI")
    assert all(r["event_at"] == "2026-10-15T12:30:00+00:00" for r in result.records)
    assert all(r["macro_period_date"] != r["event_at"][:10] for r in result.records)


def test_macro_later_failure_keeps_received_values_and_sanitized_reason():
    def response(endpoint, params):
        if params["to"] < "2026-08-01":
            raise TimeoutError("https://example.test?apikey=PRIVATE_KEY")
        return [{"name": "CPI", "date": "2026-09-01", "value": 320}]
    result = collect(Provider(response), "CPI")
    assert len(result.records) == 1 and result.status == "PARTIAL"
    assert result.evidence["slices"][-1]["status"] == "UNAVAILABLE"
    assert "PRIVATE_KEY" not in str(result)


def test_denial_is_propagated_and_no_extra_calls_occur():
    def denied(*args):
        raise PermissionError("not authorized")
    provider = Provider(denied)
    with pytest.raises(PermissionError):
        collect(provider, "GDP")
    assert len(provider.calls) == 1


@pytest.mark.parametrize("row", [{"name": "CPI", "date": "2026-09-01", "value": 320},
    {"name": "GDP", "date": "2027-01-01", "value": 320}, {"name": "GDP", "date": None, "value": 320}])
def test_macro_wrong_series_future_period_or_invalid_date_rejected(row):
    with pytest.raises(ValueError):
        collect(Provider([row]), "GDP")


def test_parameters_cannot_silently_change_model_population():
    for params in ({**request().params, "periodLength": 14}, {**request().params, "timeframe": "1min"},
                   {**request().params, "symbol": "OUST"}):
        with pytest.raises(ValueError):
            collect_context(replace(request(), params=params), START, END, 100, fmp_provider=Provider([]))
    with pytest.raises(ValueError):
        collect_context(request(), START, END+timedelta(minutes=15), 100, fmp_provider=Provider([]))


def test_empty_response_is_observed_empty_not_history_or_market_coverage():
    result = collect(Provider([]))
    assert result.records == [] and result.status == "COMPLETE"
    assert "no complete-market" in result.evidence["coverage_claim"]


def test_catalog_explicitly_identifies_nonconfigured_and_redundant_families():
    catalog = context_catalog()
    assert catalog["implemented"]["technical"] == list(INDICATORS)
    assert catalog["not_configured"] == ["treasury-rates", "economic-calendar", "market-risk-premium"]
    assert "independent source selectors" in catalog["redundancy"]


def test_real_adapter_collector_and_features_use_only_actual_receipts(tmp_path):
    from ml.intraday.market_collection import QuarterHourMarketCollector
    from ml.intraday.market_collection_adapters import FmpAdapter, default_requests
    from ml.intraday.market_context import build_market_context
    requests = [request("sma"), request("GDP")]
    assert all(r in default_requests(["SDGR"]) for r in requests)
    def response(endpoint, params):
        if endpoint.startswith("technical-indicators/"):
            return [raw(), raw(date="2026-10-16 16:00:00")]
        return [{"name": "GDP", "date": "2026-07-01", "value": 31422.526}] if params["to"] < "2026-08-01" else []
    provider = Provider(response)
    receipt = END + timedelta(seconds=10)
    collector = QuarterHourMarketCollector(tmp_path, symbols=["SDGR"], requests=requests,
        adapters={"fmp": FmpAdapter(provider_factory=lambda: provider)}, clock=lambda: receipt)
    collector.enqueue(START)
    snapshots = collector.step(max_requests=2)
    assert len(snapshots) == 2 and all(s["status"] == "COMPLETE" for s in snapshots)
    selectors = {r.source_kind: r.source_id for r in requests}
    context = build_market_context(snapshots, symbol="SDGR", cutoff=receipt, source_ids=selectors)
    assert context.features["ctx_technical_sma"] == 10.5
    assert context.features["ctx_macro_gdp_value"] == 31422.526
    before = build_market_context(snapshots, symbol="SDGR", cutoff=END, source_ids=selectors)
    assert before.features["ctx_technical_sma_missing"] == 1
    assert before.features["ctx_macro_gdp_value_missing"] == 1
    macro = next(s for s in snapshots if s["source_kind"] == "macro")["records"][0]
    assert macro["macro_period_date"] == "2026-07-01"
    assert macro["event_at"] == macro["published_at"] == receipt.isoformat()
