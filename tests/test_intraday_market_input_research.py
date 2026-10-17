from datetime import datetime, timezone
import json

import pytest

from ml.intraday import market_input_research as m

NOW = datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc)


def fixture(source):
    row = {"symbol": "AAPL"}
    if source.startswith("fmp_estimates_"):
        row.update(date="2026-12-31", epsAvg=2.5, revenueAvg=100, numAnalystsEps=4)
    elif source.startswith("fmp_rating_"):
        row.update(date="2026-10-16", overallScore=3)
    elif source == "fmp_target_summary":
        row.update(lastMonthAvgPriceTarget=150, lastMonthCount=3)
    elif source == "fmp_target_consensus":
        row.update(targetConsensus=150, targetHigh=180, targetLow=120)
    elif source == "fmp_grade_changes":
        row.update(date="2026-10-16", action="upgrade")
    elif source in {"fmp_grade_history", "fmp_grade_consensus"}:
        for name, value in zip(("StrongBuy", "Buy", "Hold", "Sell", "StrongSell"), (1, 2, 3, 4, 5)):
            key = "analystRatings"+name if source == "fmp_grade_history" else name[0].lower()+name[1:]
            row[key] = value
        row["date"] = "2026-10-16"
    elif source.startswith("fmp_dcf"):
        row.update(date="2026-10-16", dcf=150, stockPrice=100)
    elif source in {"fmp_research_news", "fmp_press_releases", "fmp_sec_filings"}:
        row["acceptedDate" if source == "fmp_sec_filings" else "publishedDate"] = "2026-10-16 13:30:00"
    elif source.startswith("fmp_technical_"):
        name = source.removeprefix("fmp_technical_")
        row.update(date="2026-10-16 13:45:00")
        row[name] = -20 if name == "williams" else 20
    elif source.startswith("fmp_macro_"):
        row = {"name": m.MACROS[source.removeprefix("fmp_macro_")], "date": "2026-10-01", "value": 3.5}
    return row


@pytest.mark.parametrize("source", m.SOURCE_FIELDS)
def test_every_adapter_exposes_only_reviewed_finite_scalars_at_receipt(source):
    result = m.extract_context(source, [fixture(source)], NOW, symbol="AAPL")
    assert result["status"] == "ok"
    assert result["features"]
    assert set(result["features"]) <= set(m.SOURCE_FIELDS[source])
    assert result["evidence"]["available_at"] == NOW.isoformat()
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("source", m.SOURCE_FIELDS)
@pytest.mark.parametrize("marker", m.MARKERS)
def test_every_adapter_rejects_marked_records_before_projection(source, marker):
    result = m.extract_context(source, [fixture(source) | {marker: True}], NOW)
    assert result["features"] == {}
    assert result["evidence"]["marker_rejected"] == 1
    wrapper = {"data": [fixture(source)], marker: True}
    assert m.extract_features(source, wrapper, NOW) == {}


@pytest.mark.parametrize("value", [0, 1, "false", "true", None, [], {}])
def test_nonboolean_marker_is_not_treated_as_observed(value):
    assert not m.extract_features("fmp_rating_snapshot", [fixture("fmp_rating_snapshot") | {"synthetic": value}], NOW)
    assert m.extract_features("fmp_rating_snapshot", [fixture("fmp_rating_snapshot") | {"synthetic": False}], NOW)


@pytest.mark.parametrize("source", m.SOURCE_FIELDS)
def test_every_adapter_rejects_future_publication_but_preserves_receipt(source):
    result = m.extract_context(source, [fixture(source) | {"publishedDate": "2026-10-17T18:00:00Z"}], NOW)
    assert not result["features"]
    assert result["evidence"]["future_rejected"] == 1
    assert result["evidence"]["available_at"] == NOW.isoformat()


@pytest.mark.parametrize("indicator", m.INDICATORS)
def test_technical_unfinished_future_stale_conflict_and_finite_domains(indicator):
    source = f"fmp_technical_{indicator}"
    row = fixture(source)
    for date in ("2026-10-16 14:00:00", "2026-10-17 13:45:00"):
        assert not m.extract_features(source, [row | {"date": date}], NOW)
    stale = m.extract_context(source, [row | {"date": "2026-10-16 12:00:00"}], NOW)
    assert stale["status"] == "stale"
    assert not stale["features"]
    conflict = m.extract_context(source, [row, row | {indicator: 25}], NOW)
    assert not conflict["features"]
    assert conflict["status"] == "unavailable"
    for value in (float("nan"), float("inf"), True, "invalid"):
        assert not m.extract_features(source, [row | {indicator: value}], NOW)
    bad = 101 if indicator in {"rsi", "adx"} else 1 if indicator == "williams" else -1
    assert not m.extract_features(source, [row | {indicator: bad}], NOW)


@pytest.mark.parametrize("day,bar,receipt", [("2026-01-05", "09:45:00", "15:00:00"), ("2026-07-06", "09:45:00", "14:00:00")])
def test_technical_new_york_winter_summer_bar_end(day, bar, receipt):
    now = datetime.fromisoformat(day+"T"+receipt+"+00:00")
    result = m.extract_context("fmp_technical_sma", [{"date": day+" "+bar, "sma": 20}], now)
    assert result["features"] == {"value": 20}
    assert result["event_at"] == now.isoformat()
    assert result["evidence"]["available_at"] == now.isoformat()


@pytest.mark.parametrize("clock", ["2026-03-08 02:15:00", "2026-11-01 01:15:00"])
def test_ambiguous_and_nonexistent_exchange_clocks_are_unavailable(clock):
    assert m._provider_time(clock) is None


def test_request_catalog_is_bounded_and_global_jobs_are_optional():
    jobs = m.research_specs("AAPL", NOW)
    assert len(jobs) == 23
    global_jobs = m.research_specs("AAPL", NOW, include_global=True)[23:]
    assert len(global_jobs) == 4
    assert len({endpoint for source, endpoint, _ in jobs if not source.startswith("fmp_technical_")}) == 13
    assert {source for source, _, _ in (*jobs, *global_jobs)} == set(m.SOURCE_FIELDS)
    for source, _, params in (*jobs, *global_jobs):
        assert "apikey" not in params
        if source.startswith("fmp_macro_"):
            assert "symbol" not in params
            assert (datetime.fromisoformat(params["to"])-datetime.fromisoformat(params["from"])).days == 89
        else:
            assert params.get("symbol", params.get("symbols")) == "AAPL"
        assert params.get("page", 0) == 0
        assert params.get("limit", 100) <= 100


def test_estimate_period_and_macro_period_never_become_availability():
    estimate = m.extract_context("fmp_estimates_quarter", [fixture("fmp_estimates_quarter")], NOW)
    assert estimate["event_at"] is None
    assert estimate["evidence"]["estimate_period_end"] == "2026-12-31"
    macro = m.extract_context("fmp_macro_gdp", [fixture("fmp_macro_gdp")], NOW)
    assert macro["event_at"] is None
    assert macro["evidence"]["period_date"] == "2026-10-01"
    assert estimate["evidence"]["available_at"] == macro["evidence"]["available_at"] == NOW.isoformat()


@pytest.mark.parametrize("source", m.SOURCE_FIELDS)
def test_empty_denied_payload_and_publication_unknown_are_explicit(source):
    assert m.extract_context(source, [], NOW)["status"] == "empty"
    denied = m.extract_context(source, {"Error Message": "apikey=PRIVATE; denied"}, NOW)
    assert denied["status"] == "unavailable"
    assert "PRIVATE" not in json.dumps(denied)
    malformed = m.extract_context(source, [fixture(source) | {"publishedDate": "not-a-date"}], NOW)
    assert not malformed["features"]


def test_safe_error_never_copies_provider_text_or_url():
    error = RuntimeError("HTTP 403 https://example.com/?apikey=PRIVATE authorization=SECRET")
    result = m.safe_error(error)
    assert result == {"error_type": "RuntimeError", "http_status": 403, "category": "entitlement_or_permission"}
    assert "PRIVATE" not in json.dumps(result) and "SECRET" not in json.dumps(result)


def test_counts_are_bounded_observations_not_fake_empty_zero():
    source = "fmp_research_news"
    row = fixture(source)
    result = m.extract_context(source, [row, row], NOW, request_params={"limit": 2})
    assert result["features"]["items_7d"] == 1
    assert result["evidence"]["duplicate_rows"] == 1
    assert result["evidence"]["page_bound_reached"]
    assert m.extract_context(source, [], NOW)["features"] == {}


def test_invalid_symbol_domain_and_response_bounds():
    with pytest.raises(ValueError):
        m.research_specs("*", NOW)
    with pytest.raises(ValueError):
        m.extract_context("unknown", [], NOW)
    with pytest.raises(ValueError):
        m.extract_context("fmp_dcf", [], datetime(2026, 10, 16))
    assert not m.extract_features("fmp_dcf", [fixture("fmp_dcf") | {"symbol": "OTHER"}], NOW, symbol="AAPL")
    assert m.extract_context("fmp_dcf", [fixture("fmp_dcf")]*(m.MAX_RESPONSE_ROWS+1), NOW)["status"] == "unavailable"


def test_macro_source_identity_and_period_bounds():
    row = fixture("fmp_macro_gdp")
    assert not m.extract_features("fmp_macro_cpi", [row], NOW)
    assert not m.extract_features("fmp_macro_gdp", [row], NOW, request_params={"from": "2026-10-02", "to": "2026-10-16"})
    assert not m.extract_features("fmp_macro_gdp", [row | {"date": "2027-01-01"}], NOW)


def test_nonnegative_integer_count_and_safe_ratio_require_operands():
    source = "fmp_grade_consensus"
    assert not m.extract_features(source, [fixture(source) | {"buy": -1}], NOW)
    assert not m.extract_features(source, [fixture(source) | {"buy": 1.5}], NOW)
    dcf = m.extract_features("fmp_dcf", [fixture("fmp_dcf") | {"stockPrice": 0}], NOW)
    assert dcf == {"value": 150}
    targets = m.extract_features("fmp_target_consensus", [fixture("fmp_target_consensus") | {"targetLow": 200}], NOW)
    assert targets == {}


def test_empty_gdp_fallback_is_exact_nonoverlapping_preceding_ninety_days():
    params = {"name": "GDP", "from": "2026-07-20", "to": "2026-10-17"}
    source, endpoint, fallback = m.empty_macro_fallback_spec("fmp_macro_gdp", params, [])
    assert (source, endpoint) == ("fmp_macro_gdp", "economic-indicators")
    assert fallback == {"name": "GDP", "from": "2026-04-21", "to": "2026-07-19"}
    assert (datetime.fromisoformat(fallback["to"])-datetime.fromisoformat(fallback["from"])).days == 89
    assert datetime.fromisoformat(fallback["to"]) < datetime.fromisoformat(params["from"])
    assert m.empty_macro_fallback_spec(source, params, {"data": [], "synthetic": False}) == (source, endpoint, fallback)
    assert params == {"name": "GDP", "from": "2026-07-20", "to": "2026-10-17"}


@pytest.mark.parametrize("payload", [{}, {"Error Message": "denied"}, {"data": [], "error": "denied"},
    {"data": [], "synthetic": True}, {"data": [], "source_empty": True}, {"data": [], "no_trade": None},
    [{"name": "GDP", "date": "2026-07-01", "value": None}], [{"synthetic": True}], "[]", None])
def test_gdp_fallback_rejects_error_marked_nonempty_and_malformed_payloads(payload):
    assert m.empty_macro_fallback_spec("fmp_macro_gdp", {"name": "GDP", "from": "2026-07-20", "to": "2026-10-17"}, payload) is None


@pytest.mark.parametrize("params", [{"name": "CPI", "from": "2026-07-20", "to": "2026-10-17"},
    {"name": "GDP", "from": "2026-07-21", "to": "2026-10-17"}, {"name": "GDP", "from": "invalid", "to": "2026-10-17"},
    {"name": "GDP", "from": "2026-10-17", "to": "2026-07-20"}, {"name": "GDP", "from": "2026-07-20", "to": "2026-10-17", "extra": 1},
    {"name": "GDP", "from": "0001-01-01", "to": "0001-03-31"}])
def test_gdp_fallback_requires_reviewed_exact_initial_window(params):
    assert m.empty_macro_fallback_spec("fmp_macro_gdp", params, []) is None


def test_gdp_fallback_is_not_a_generic_macro_history_expander():
    params={"name": "GDP", "from": "2026-07-20", "to": "2026-10-17"}
    assert m.empty_macro_fallback_spec("fmp_macro_cpi", params, []) is None
