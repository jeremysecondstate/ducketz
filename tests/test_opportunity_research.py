from __future__ import annotations

import copy
import json
from datetime import date

import pandas as pd
import pytest

from ml.opportunity_research.policy import GATES, WEIGHTS, assess, sources_valid, timestamp, valuation_case
from ml.opportunity_research.providers import request
from ml.opportunity_research.storage import ResearchStore, atomic_write, encode
from ml.opportunity_research.tracking import calendar, evaluate, latest_session, windows
from ml.opportunity_research.workflow import build_publication, render_report

NOW = "2025-01-05T18:00:00Z"
CUTOFF = "2025-01-05T17:59:00Z"


def candidate():
    sources = [{"id": kind, "title": f"Evidence from {kind}", "kind": kind,
                "url": f"https://example.com/{kind}", "published_at": "2025-01-02T12:00:00Z",
                "accessed_at": "2025-01-05T17:00:00Z", "supports": "Verifiable company evidence."}
               for kind in ("filing", "company", "independent", "market_data")]
    rationale = "Supported by the archived financial evidence."
    item = {"symbol": "TEST", "security_id": "CIK0000000001:COMMON", "company_name": "Fictional Test Company",
            "track": "established", "currency": "USD", "decision": "recommend", "reference_price": 20,
            "reference_session": "2025-01-03", "median_daily_dollar_volume_20d": 4_000_000,
            "sector_benchmark": "XLK", "sources": sources,
            "scores": {key: {"rating": 4, "rationale": rationale, "source_ids": ["filing"]} for key in WEIGHTS},
            "gates": {key: {"passed": True, "rationale": rationale, "source_ids": ["filing"]} for key in GATES},
            "catalysts": [{"due_date": "2025-06-01", "milestone": "Observed commercial deployment.",
                           "success_test": "Paid deployments reach the stated volume.",
                           "failure_consequence": "Reduce the margin and revenue forecast.", "source_ids": ["company"]}],
            "valuation": {name: {"method": "comparables", "metric": "revenue", "forward_metric": 100,
                                 "multiple": multiple, "net_debt": 0, "diluted_shares": 10, "assumptions": rationale}
                          for name, multiple in (("bear", 1.5), ("base", 3), ("bull", 4))},
            "price_targets": {h: {"bear": 15, "base": 30, "bull": 40, "rationale": rationale} for h in ("6m", "12m")}}
    for field in ("business", "product_evidence", "mispricing", "reverse_valuation", "countercase", "invalidation",
                  "financing", "benchmark_rationale", "accounting_adjustments"):
        item[field] = rationale
    return item


def dossier(item=None, week="2025-01-05", cutoff=CUTOFF):
    item = candidate() if item is None else item
    return {"week": week, "research_cutoff": cutoff, "summary": "Fictional test edition, never production research.",
            "candidates": [item], "screening": [{"symbol": "TEST", "track": "established",
                "disposition": "investigate", "reason": "Fictional integration-test company."}],
            "updates": [], "limitations": ["A single synthetic company for the test fixture."],
            "no_new_recommendations_reason": "The fixture contains no qualifying new recommendations."}


def company_snapshot(store):
    cal = calendar(NOW)
    sessions = [s for s in cal.sessions if s.date() <= date(2025, 1, 3)][-20:]
    payload = {"symbol": "TEST", "fetched_at": "2025-01-05T16:00:00Z", "datasets": {
        "profile": {"rows": [{"symbol": "TEST", "cik": "0000000001", "currency": "USD", "exchange": "NASDAQ"}]},
        "daily_prices": {"rows": [{"date": s.date().isoformat(), "open": 20, "close": 20, "volume": 200_000}
                                    for s in sessions]}}}
    return {"TEST": store.snapshot(payload)}


def call():
    return {"call_id": "2025-01-05/CIK0000000001:COMMON", "security_id": "CIK0000000001:COMMON",
            "symbol": "TEST", "sector_benchmark": "XLK", "published_at": NOW, "windows": windows(NOW)}


def market(end="2025-07-07", scale=1.0):
    cal = calendar(NOW)
    sessions = [s for s in cal.sessions if "2025-01-06" <= s.date().isoformat() <= end]
    result = {}
    for ticker, final in (("TEST", 121), ("SPY", 110), ("XLK", 105)):
        rows = [{"date": s.date().isoformat(), "adjOpen": scale * 100,
                 "adjClose": scale * (100 + (final - 100) * i / max(len(sessions) - 1, 1))}
                for i, s in enumerate(sessions)]
        result[ticker] = {"symbol": ticker, "rows": rows, "basis": "fmp_dividend_adjusted_ohlc",
                          "error": None, "sha256": ticker, "path": f"snapshots/{ticker}.json"}
    return result


def test_holiday_entry_and_exact_calendar_month_maturity():
    result = windows("2025-01-17T22:00:00Z")
    assert result["entry_session"] == "2025-01-21"  # Monday is a holiday.
    assert result["entry_at"] == "2025-01-21T14:30:00+00:00"
    assert result["6m_at"] == "2025-07-21T20:00:00+00:00"  # DST.
    assert windows(NOW)["6m_session"] == "2025-07-07"  # Sunday anniversary.


def test_strictly_after_publication_and_no_partial_session():
    assert windows("2025-01-06T14:30:00Z")["entry_session"] == "2025-01-07"
    assert latest_session("2025-01-06T20:59:00Z") == "2025-01-03"
    assert latest_session("2025-01-06T21:00:00Z") == "2025-01-06"
    assert latest_session("2025-11-28T18:00:00Z") == "2025-11-28"  # Early close.


def test_does_not_score_future_outcomes_even_when_rows_exist():
    result = evaluate(call(), market(), "2025-07-07T19:59:00Z")
    assert result["windows"]["6m"]["status"] == "PENDING_MATURITY"
    assert result["windows"]["12m"]["status"] == "PENDING_MATURITY"
    assert evaluate(call(), market(), NOW)["windows"]["6m"]["status"] == "PENDING_ENTRY"


def test_total_return_benchmarks_and_consistent_adjustment_vintage():
    first = evaluate(call(), market(), "2025-07-07T20:01:00Z")["windows"]["6m"]
    scaled = evaluate(call(), market(scale=0.5), "2025-07-07T20:01:00Z")["windows"]["6m"]
    assert first["status"] == "EVALUATED"
    assert first["series"]["stock"]["total_return"] == pytest.approx(0.21)
    assert first["excess_vs_spy"] == pytest.approx(0.11)
    assert first["excess_vs_sector"] == pytest.approx(0.16)
    assert scaled["series"]["stock"]["total_return"] == first["series"]["stock"]["total_return"]


@pytest.mark.parametrize("problem", ["missing_entry", "missing_exit", "interior_gap", "duplicate", "raw_prices"])
def test_missing_or_ambiguous_prices_never_become_returns(problem):
    prices = market()
    if problem == "missing_entry":
        prices["TEST"]["rows"].pop(0)
    elif problem == "missing_exit":
        prices["TEST"]["rows"].pop()
    elif problem == "interior_gap":
        prices["TEST"]["rows"].pop(12)
    elif problem == "duplicate":
        prices["TEST"]["rows"].append(prices["TEST"]["rows"][0])
    else:
        prices["TEST"]["basis"] = "raw"
    result = evaluate(call(), prices, "2025-07-07T21:00:00Z")["windows"]["6m"]
    assert result["status"] == "MISSING_DATA"
    assert "excess_vs_spy" not in result


def test_drawdown_uses_peak_wealth_and_weekly_uses_prior_close():
    prices = market()
    prices["TEST"]["rows"][3]["adjClose"] = 150
    prices["TEST"]["rows"][4]["adjClose"] = 75
    result = evaluate(call(), prices, "2025-07-07T21:00:00Z")
    assert result["windows"]["6m"]["series"]["stock"]["max_close_drawdown"] == pytest.approx(-0.5)
    assert result["week"]["basis"] == "previous_week_close"
    assert result["week"]["start_session"] == "2025-06-30"


def test_mature_result_is_frozen_despite_missing_later_provider():
    original = evaluate(call(), market(), "2025-07-07T21:00:00Z")["windows"]["6m"]
    subsequent = evaluate(call(), {}, "2025-07-14T21:00:00Z", {"6m": original})
    assert subsequent["windows"]["6m"] == original


def test_scorecard_and_cashflow_math():
    result = assess(candidate(), timestamp(CUTOFF), "2025-01-03")
    assert result["score"] == 80
    assert result["fair_values"]["base"] == 30
    assert result["discount_to_base_value"] == pytest.approx(1 / 3)
    case = {"method": "dcf", "fcff": [10, 10, 10], "discount_rate": 0.1, "terminal_growth": 0,
            "net_debt": 20, "diluted_shares": 10, "assumptions": "Constant annual cash flows in perpetuity."}
    assert valuation_case(case) == pytest.approx(8)
    case["terminal_growth"] = 0.1
    with pytest.raises(ValueError):
        valuation_case(case)


@pytest.mark.parametrize("problem", ["gate", "cash", "liquidity", "price", "score", "source", "target", "valuation"])
def test_recommendations_cannot_bypass_gates(problem):
    item = candidate()
    if problem == "gate":
        item["gates"]["countercase_complete"]["passed"] = False
    elif problem == "cash":
        item.update(track="emerging", cash_runway_months=6)
    elif problem == "liquidity":
        item["median_daily_dollar_volume_20d"] = 100
    elif problem == "price":
        item["reference_session"] = "2025-01-02"
    elif problem == "score":
        item["scores"]["product_economics"]["rating"] = 1
    elif problem == "source":
        item["sources"] = item["sources"][:2]
    elif problem == "target":
        item["price_targets"]["12m"]["base"] = 20
    else:
        item["valuation"] = None
    with pytest.raises(ValueError):
        assess(item, timestamp(CUTOFF), "2025-01-03")


def test_watch_can_admit_unknown_valuation_without_fabrication():
    item = candidate()
    item.update(decision="watch", valuation=None, price_targets=None)
    result = assess(item, timestamp(CUTOFF), "2025-01-03")
    assert result["fair_values"] is None
    assert "valuation_unavailable" in result["failed_gates"]


def test_sources_enforce_cutoff_and_do_not_accept_secret_urls():
    sources = candidate()["sources"]
    sources[0]["accessed_at"] = "2025-01-06T00:00:00Z"
    with pytest.raises(ValueError, match="cutoff"):
        sources_valid(sources, timestamp(CUTOFF))
    sources[0]["accessed_at"] = "2025-01-04T00:00:00Z"
    sources[0]["url"] += "?apikey=secret"
    with pytest.raises(ValueError, match="credentials"):
        sources_valid(sources, timestamp(CUTOFF))


def test_publish_verifies_sources_and_report_and_is_idempotent(tmp_path):
    store = ResearchStore(tmp_path)
    refs = company_snapshot(store)
    publication = build_publication(store, dossier(), refs, {}, NOW)
    report = render_report(publication)
    path = store.publish(publication, report)
    assert path.read_text(encoding="utf-8") == report
    assert store.publish(publication, report) == path
    assert len(store.calls()) == 1
    assert store.calls()[0]["windows"]["entry_session"] == "2025-01-06"
    assert "PENDING_ENTRY" in report
    path.write_text(report + "tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="verification"):
        store.history()


def test_provider_snapshot_corruption_cannot_disappear_from_history(tmp_path):
    store = ResearchStore(tmp_path)
    refs = company_snapshot(store)
    publication = build_publication(store, dossier(), refs, {}, NOW)
    store.publish(publication, render_report(publication))
    (store.root / refs["TEST"]["path"]).write_bytes(b"{}")
    with pytest.raises(ValueError, match="checksum"):
        store.history()


def test_identity_and_reference_price_are_independently_reconciled(tmp_path):
    store = ResearchStore(tmp_path)
    refs = company_snapshot(store)
    for key, value in (("reference_price", 19), ("security_id", "CIK0000000002:COMMON"),
                       ("median_daily_dollar_volume_20d", 5_000_000)):
        item = candidate()
        item[key] = value
        with pytest.raises(ValueError):
            build_publication(store, dossier(item), refs, {}, NOW)


def test_repeated_call_keeps_inception_and_requires_prior_review(tmp_path):
    store = ResearchStore(tmp_path)
    refs = company_snapshot(store)
    first = build_publication(store, dossier(), refs, {}, NOW)
    store.publish(first, render_report(first))
    item = candidate()
    item.update(decision="watch", reference_session="2025-01-10")
    doc = dossier(item, week="2025-01-12", cutoff="2025-01-12T17:00:00Z")
    now = "2025-01-12T18:00:00Z"
    with pytest.raises(ValueError, match="Every unresolved"):
        build_publication(store, doc, refs, {}, now)
    original = store.calls()[0]
    doc["updates"] = [{"call_id": original["call_id"], "status": "withdrawn",
                       "thesis_update": "The original thesis has materially weakened.",
                       "catalyst_update": "The relevant original catalyst remains unresolved.", "sources": item["sources"]}]
    second = build_publication(store, doc, refs, {}, now)
    assert second["new_calls"] == []
    assert len(second["tracking"]) == 1  # Withdrawn calls still tracked.
    store.publish(second, render_report(second))
    assert store.calls()[0] == original
    assert len(store.history()) == 2
    changed = copy.deepcopy(second)
    changed["input_sha256"] = "different"
    with pytest.raises(ValueError, match="already published"):
        store.publish(changed, "changed report")


def test_snapshot_path_cannot_escape_and_late_data_is_rejected(tmp_path):
    store = ResearchStore(tmp_path)
    with pytest.raises(ValueError, match="path"):
        store.read_snapshot({"path": "../../elsewhere.json", "sha256": "0" * 64})
    refs = company_snapshot(store)
    data = store.read_snapshot(refs["TEST"])
    data["fetched_at"] = "2025-01-05T19:00:00Z"
    refs["TEST"] = store.snapshot(data)
    with pytest.raises(ValueError, match="after the research cutoff"):
        build_publication(store, dossier(), refs, {}, NOW)


def test_no_findings_edition_requires_reason_and_renders(tmp_path):
    store = ResearchStore(tmp_path)
    doc = dossier()
    doc["candidates"] = []
    result = build_publication(store, doc, {}, {}, NOW)
    assert result["new_calls"] == []
    assert "no qualifying" in render_report(result)
    doc["no_new_recommendations_reason"] = ""
    with pytest.raises(ValueError):
        build_publication(store, doc, {}, {}, NOW)


def test_provider_failure_never_exposes_authenticated_url():
    class Unavailable:
        def _get_json(self, *args):
            raise RuntimeError("Request failed https://example.com/?apikey=TOPSECRET")
    payload = request(Unavailable(), "profile", {"symbol": "TEST"})
    assert payload["error"]
    assert "TOPSECRET" not in json.dumps(payload)


def test_cli_prepare_status_and_refresh_empty_history(tmp_path, capsys):
    from ml.opportunity_research.__main__ import main
    for command in ("prepare", "refresh", "status", "verify"):
        assert main([command, "--datastore-dir", str(tmp_path)]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result
    assert ResearchStore(tmp_path).calls() == []


def test_missing_latest_edition_is_detected_from_publication_index(tmp_path):
    store = ResearchStore(tmp_path)
    refs = company_snapshot(store)
    publication = build_publication(store, dossier(), refs, {}, NOW)
    path = store.publish(publication, render_report(publication))
    path.parent.rename(store.root / "misplaced-edition")
    with pytest.raises(ValueError, match="missing"):
        store.history()


def test_publication_recovers_after_index_export_failure(tmp_path, monkeypatch):
    store = ResearchStore(tmp_path)
    publication = build_publication(store, dossier(), company_snapshot(store), {}, NOW)
    original_export = store.export_ledger
    def unavailable(*args):
        raise OSError("Interrupted export")
    monkeypatch.setattr(store, "export_ledger", unavailable)
    with pytest.raises(OSError):
        store.publish(publication, render_report(publication))
    assert len(store.calls()) == 1
    monkeypatch.setattr(store, "export_ledger", original_export)
    store.publish(publication, render_report(publication))
    assert len(store.calls()) == 1
    assert json.loads((store.root / "latest.json").read_bytes())["edition_count"] == 1


def test_watch_report_with_unresolved_valuation(tmp_path):
    store = ResearchStore(tmp_path)
    item = candidate()
    item.update(decision="watch", valuation=None, price_targets=None)
    publication = build_publication(store, dossier(item), company_snapshot(store), {}, NOW)
    report = render_report(publication)
    assert "valuation is unresolved" in report
    assert "WATCH" in report
    assert not publication["new_calls"]


def test_wrong_ticker_in_provider_rows_is_not_scored():
    prices = market()
    prices["TEST"]["rows"][0]["symbol"] = "OTHER"
    result = evaluate(call(), prices, "2025-07-07T21:00:00Z")["windows"]["6m"]
    assert result["series"]["stock"]["status"] == "WRONG_SECURITY"


def test_cli_publish_retry_preserves_original_time(tmp_path, capsys, monkeypatch):
    from ml.opportunity_research.__main__ import main
    monkeypatch.setattr(pd.Timestamp, "now", classmethod(lambda cls, tz=None: pd.Timestamp(NOW)))
    store = ResearchStore(tmp_path)
    refs = company_snapshot(store)
    draft = store.root / "drafts/2025-01-05"
    atomic_write(draft / "research.json", encode(dossier()))
    atomic_write(draft / "companies.json", encode(refs))
    args = ["publish", "--datastore-dir", str(tmp_path), "--week", "2025-01-05"]
    assert main(args) == 0
    capsys.readouterr()
    original = store.calls()[0]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["already_published"]
    assert store.calls()[0] == original


def test_company_fetch_includes_required_filing_window(tmp_path, monkeypatch):
    from ml.opportunity_research import providers
    requests = []
    class FakeProvider:
        def __init__(self, **kwargs):
            pass
        def _get_json(self, endpoint, params):
            requests.append((endpoint, params))
            return []
    monkeypatch.setattr(providers, "FmpCorporateDataProvider", FakeProvider)
    store = ResearchStore(tmp_path)
    refs = providers.fetch_companies(store, ["TEST"], "2024-11-05", "2025-01-03")
    params = next(p for e, p in requests if e == "sec-filings-search/symbol")
    assert params["from"] == "2023-12-30"
    assert params["to"] == "2025-01-03"
    assert params["page"] == 0
    assert store.read_snapshot(refs["TEST"])["symbol"] == "TEST"
