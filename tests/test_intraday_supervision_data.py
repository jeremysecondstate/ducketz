from types import SimpleNamespace

import pandas as pd
import pytest

from ml.intraday import supervision_data as subject
from ml.intraday.data import History, equity_sessions
from ml.intraday.prediction import ContractError


def history(*dates, drop=()):
    sessions = equity_sessions(pd.Timestamp(min(dates), tz="UTC"), pd.Timestamp(max(dates), tz="UTC") + pd.Timedelta(days=1))
    rows = []
    for session in sessions:
        if session.session_id not in dates:
            continue
        for i, t in enumerate(pd.date_range(session.opened_at, pd.Timestamp(session.closed_at) - pd.Timedelta(minutes=15), freq="15min")):
            if t in {pd.Timestamp(value) for value in drop}:
                continue
            price = 100 + i * .1
            rows.append(dict(symbol="ABCL", opened_at=t, available_at=t+pd.Timedelta(minutes=15),
                availability_kind="assumed_historical_close", source_id="databento:EQUS.MINI:ohlcv-1m",
                source_dataset="EQUS.MINI", session_id=session.session_id,
                session_open=session.opened_at, session_close=session.closed_at,
                open=price, high=price+.2, low=price-.2, close=price+.1, volume=100+i,
                synthetic=False, no_trade=False, evidence_json="{}"))
    manifest = dict(source_dataset="EQUS.MINI", aggregation={"incomplete_intervals": len(drop)},
                    availability_limitation="Historical receipts unavailable")
    return History(pd.DataFrame(rows), manifest)


def test_overnight_audit_expects_previous_completed_session_without_closed_hours():
    h = history("2026-10-13")
    r = subject.audit_history(h, as_of="2026-10-14T08:00:00Z")
    assert r["freshness_ok"]
    assert r["expected_reference_at"] == "2026-10-13T23:45:00+00:00"
    assert r["completed_session"]["session_id"] == "2026-10-13"
    assert r["gaps"][0]["expected_count"] == 64
    assert r["missing_interval_count"] == 0
    assert r["terminal_reference_rows_excluded"] == 2


def test_current_session_completed_candle_is_required_and_holes_stay_explicit():
    h = history("2026-10-13", "2026-10-14", drop=("2026-10-13T22:00:00+00:00",))
    r = subject.audit_history(h, as_of="2026-10-14T08:30:00Z")
    assert r["latest_bar_fresh"]
    assert r["freshness_ok"]
    assert not r["coverage_complete"]
    assert r["expected_reference_at"] == "2026-10-14T08:15:00+00:00"
    assert r["missing_interval_count"] == 1
    assert r["gaps"][0]["missing_reference_at"] == ["2026-10-13T22:00:00+00:00"]
    assert r["active_session_ids"] == ["2026-10-14"]


def test_weekend_and_short_sessions_use_calendar():
    weekend = subject.audit_history(history("2026-10-16"), as_of="2026-10-18T09:00:00Z")
    assert weekend["freshness_ok"]
    assert weekend["completed_session"]["session_id"] == "2026-10-16"
    short = subject.audit_history(history("2026-11-27"), as_of="2026-11-28T09:00:00Z")
    assert short["freshness_ok"]
    assert short["expected_reference_at"] == "2026-11-27T17:45:00+00:00"
    assert short["gaps"][0]["expected_count"] == 14


def test_stale_prior_session_and_future_rows_do_not_pass_freshness():
    r = subject.audit_history(history("2026-10-12", "2026-10-14"), as_of="2026-10-14T08:00:00Z")
    assert not r["freshness_ok"]
    assert r["missing_interval_count"] == 64
    assert r["latest_reference_at"] == "2026-10-12T23:45:00+00:00"


def test_completed_assessment_uses_published_close_not_active_bar():
    session = {"session_id": "2026-10-13", "closed_at": "2026-10-14T00:00:00Z"}
    refresh = {"status": "partial", "source_dataset": "EQUS.MINI", "source_schema": "ohlcv-1m",
        "results": {"ABCL": {"error_files": 0}}, "requests": [{"symbol": "ABCL",
            "status": "fetched", "source_schema": "ohlcv-1m", "error_type": None,
            "provider_available_end": "2026-10-14T00:00:00Z"}]}
    audit = subject.audit_history(history("2026-10-13"), as_of="2026-10-14T10:30:00Z")
    assert not audit["freshness_ok"]
    assert audit["gaps"][1]["count"] == 10
    assert subject.assessment_readiness(refresh, symbol="ABCL", completed_session=session)["ready"]
    refresh["requests"][0]["provider_available_end"] = "2026-10-13T23:59:00Z"
    assert not subject.assessment_readiness(refresh, symbol="ABCL", completed_session=session)["ready"]
    refresh["requests"][0]["provider_available_end"] = "2026-10-14T00:00:00Z"
    refresh["results"]["ABCL"]["error_files"] = 1
    assert not subject.assessment_readiness(refresh, symbol="ABCL", completed_session=session)["ready"]


def test_dataset_reports_terminal_label_limitation_then_recovers_with_current_reference():
    h = history("2026-10-12", "2026-10-13", "2026-10-14")
    before = subject.build_supervision_dataset(h, as_of="2026-10-14T08:00:00Z", completed_session="2026-10-13")
    assert before.manifest["supervision_reference_at"] == "2026-10-13T23:15:00+00:00"
    assert before.manifest["terminal_reference_rows_excluded"] == 2
    assert before.manifest["terminal_matured_labels_excluded"] == 2
    assert before.training_reference_at.max() == pd.Timestamp("2026-10-13T22:45:00Z")
    after = subject.build_supervision_dataset(h, as_of="2026-10-14T08:15:00Z", completed_session={"session_id": "2026-10-13"})
    assert after.manifest["terminal_matured_labels_excluded"] == 0
    assert after.training_reference_at.max() == pd.Timestamp("2026-10-13T23:15:00Z")
    assert (after.target_close_at <= pd.Timestamp("2026-10-14T08:15:00Z")).all()
    assert (after.label_available_at <= pd.Timestamp("2026-10-14T08:15:00Z")).all()


def test_later_wake_retains_all_prior_labels_and_excludes_incomplete_current_holdout():
    h = history("2026-10-12", "2026-10-13", "2026-10-14")
    data = subject.build_supervision_dataset(h, as_of="2026-10-14T10:30:00Z",
        completed_session={"session_id": "2026-10-13", "closed_at": "2026-10-14T00:00:00Z"})
    assert data.manifest["supervision_reference_at"] == "2026-10-14T10:15:00+00:00"
    assert data.training_reference_at.max() == pd.Timestamp("2026-10-13T23:15:00Z")
    assert data.manifest["terminal_matured_labels_excluded"] == 0
    assert data.manifest["incomplete_session_labels_excluded"] == 8
    assert data.manifest["training_rows"] == len(data.X) == len(data.y)
    assert len(data.y) == len(data.reference_at) == len(data.target_close_at) == len(data.label_available_at)
    assert data.target_close_at.max() == pd.Timestamp("2026-10-14T00:00:00Z")
    assert data.manifest["selection_label_scope"] == "completed_sessions_only"


def test_builder_rejects_an_active_session_as_completed_holdout():
    with pytest.raises(ContractError, match="not completed"):
        subject.build_supervision_dataset(history("2026-10-13", "2026-10-14"),
            as_of="2026-10-14T10:30:00Z", completed_session="2026-10-14")


def test_refresh_reuses_native_batch_persistence_with_exact_scope(tmp_path, monkeypatch):
    import app.services.databento_market_data as provider_module
    import datafetching.databento_fetch as fetching
    from datafetching import FetchResult

    calls = []
    spec = SimpleNamespace(schema="ohlcv-1m", frequency="1m")
    class Provider:
        def __init__(self, **kwargs):
            calls.append(("provider", kwargs))
            self.dataset = kwargs["dataset"]
        def native_specs(self):
            return (spec,)
    available = SimpleNamespace(start=pd.Timestamp("2026-10-01T00:00:00Z"), end=pd.Timestamp("2026-10-14T08:00:00Z"))
    def fetch(provider, symbols, profile, store):
        calls.append(("fetch", symbols, profile))
        return {s: [(spec, [], None, available, None)] for s in symbols}
    def persist(symbol, store, **kwargs):
        calls.append(("persist", symbol, kwargs))
        return FetchResult("databento", 2, 0)
    monkeypatch.setattr(provider_module, "DatabentoMarketDataProvider", Provider)
    monkeypatch.setattr(fetching, "_fetch_native_results_many", fetch)
    monkeypatch.setattr(fetching, "_persist_native_results", persist)
    receipt = subject.refresh_training_data(tmp_path, tmp_path/"datastore", ["ABCL", "DOCU"], "2026-10-14T08:00:00Z")
    assert calls[0] == ("provider", dict(dataset="EQUS.MINI", schema="ohlcv-1m", native_schemas=("ohlcv-1m",)))
    assert calls[1] == ("fetch", ("ABCL", "DOCU"), "continuation")
    assert all(c[2]["run_derived"] is False for c in calls if c[0] == "persist")
    assert receipt["status"] == "complete"
    assert receipt["freshness_requires_history_audit"]
    assert receipt["continuation_overlap_minutes"] == 60
    assert receipt["results"]["ABCL"]["data_files"] == 2


def test_refresh_rejects_invalid_scope_before_provider_access(tmp_path):
    with pytest.raises(ContractError, match="unique explicit"):
        subject.refresh_training_data(tmp_path, tmp_path, ["ABCL", "abcl"], "2026-10-14T08:00:00Z")
