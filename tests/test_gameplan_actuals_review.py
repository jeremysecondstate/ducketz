import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.artifacts import file_checksum, verify_manifest, write_manifest
from ml.gameplan_actuals_review import (
    _latest_saved_gameplan, compare_forecasts, compare_price_points,
    publish_actuals_review, render_actuals_review,
)
from ml.independent_stock_targets import stock_target_windows


def forecasts(day="2026-09-09"):
    return pd.DataFrame([{**window, "id": f"AAPL:{day}:{window['route']}", "symbol": "AAPL",
                          "action_date": day, "direction": "BULLISH", "calibrated_probability": .7,
                          "model_status": "PROMOTED", "target_price_source_contract": "xnas-itch-archive-v1",
                          "target_price_dataset": "XNAS.ITCH", "assumed_round_trip_cost": .001}
                         for window in stock_target_windows(pd.Timestamp(day).date())])


def prices(rows=None):
    rows = rows or [("2026-09-09T11:00Z", 100., 101.), ("2026-09-09T11:59Z", 104., 105.),
                    ("2026-09-09T12:00Z", 200., 201.), ("2026-09-09T23:59Z", 108., 110.),
                    ("2026-09-10T00:00Z", 999., 999.)]
    result = pd.DataFrame(rows, columns=["timestamp", "open", "close"])
    result["symbol"] = "AAPL"
    result.attrs["stock_price_source"] = {"source_contract": "xnas-itch-archive-v1", "dataset": "XNAS.ITCH"}
    return result


def path(day="2026-09-09"):
    return {"price_source_contract": "xnas-itch-archive-v1", "price_dataset": "XNAS.ITCH",
            "observed_at": (pd.Timestamp(day, tz="America/Los_Angeles") + pd.Timedelta(hours=1)).isoformat(),
            "points": {f"AAPL|{day}|{hour:02d}:00": {
                "symbol": "AAPL", "action_date": day, "clock_local": f"{hour:02d}:00",
                "timestamp": (pd.Timestamp(day, tz="America/Los_Angeles") + pd.Timedelta(hours=hour)).isoformat(),
                "endpoint_kind": "observed_close" if hour == 17 else "observed_open", "status": "AVAILABLE",
                "planned_price_low": 99., "planned_price_mid": 100., "planned_price_high": 101.}
                for hour in range(4, 18)}}


def test_same_clock_actuals_keep_original_prices_and_future_forecasts():
    source = forecasts()
    original = source.copy(deep=True)
    trades = source.assign(trade_price_low=99., trade_price_mid=100., trade_price_high=101.)
    result = compare_forecasts(source, prices(), observed_at="2026-09-10T00:00Z", trade_rows=trades)
    first = result.iloc[0]
    assert len(result) == 24
    assert first.actual_start_price == 100 and first.actual_end_price == 105
    assert first.actual_return == pytest.approx(.05)
    assert first.direction_correct is True or first.direction_correct == True
    assert first.trade_price_mid == 100 and first.entry_price_error == 0
    overnight = result.loc[result.route.eq("4h@16:00")].iloc[0]
    assert overnight.actuals_status == "PENDING_MATURITY"
    assert pd.isna(overnight.actual_return) and pd.isna(overnight.direction_correct)
    daily = result.loc[result.route.eq("1d@D+1")].iloc[0]
    assert daily.actual_end_price == 110  # Never the 17:00 minute's future open/close.
    pd.testing.assert_frame_equal(source, original)


def test_price_range_errors_use_saved_midpoint_and_completed_close():
    saved = path()
    original = copy.deepcopy(saved)
    result = compare_price_points(forecasts(), prices(), action_date="2026-09-09",
                                  observed_at="2026-09-10T00:00Z", planning_path=saved)
    assert len(result) == 14
    assert result.iloc[0].actual_price == 100
    second = result.iloc[1]
    assert second.actual_price == 200 and second.price_error == 100
    assert second.price_error_fraction == 1 and not second.in_planned_range
    assert result.iloc[-1].actual_price == 110
    assert pd.Timestamp(result.iloc[-1].actual_observed_at) == pd.Timestamp("2026-09-10T00:00Z")
    assert saved == original


@pytest.mark.parametrize("rows", [
    [("2026-09-09T11:06Z", 100., 100.), ("2026-09-09T11:59Z", 105., 105.)],
    [("2026-09-09T11:00Z", 100., 100.), ("2026-09-09T11:50Z", 105., 105.)],
    [("2026-09-09T11:00Z", 0., 100.), ("2026-09-09T11:59Z", 105., 105.)],
])
def test_missing_or_invalid_endpoints_are_not_scored_or_filled(rows):
    result = compare_forecasts(forecasts().iloc[:1], prices(rows), observed_at="2026-09-10T00:00Z").iloc[0]
    assert result.actuals_status == "MATURE_AWAITING_DATA"
    assert result.actual_return is None and result.direction_correct is None


def test_boundary_tolerance_records_real_observation_time():
    bars = prices([("2026-09-09T11:05Z", 100., 101.), ("2026-09-09T11:54Z", 105., 105.)])
    result = compare_forecasts(forecasts().iloc[:1], bars, observed_at="2026-09-10T00:00Z").iloc[0]
    assert result.actuals_status == "EVALUATED"
    assert pd.Timestamp(result.actual_start_observed_at) == pd.Timestamp("2026-09-09T11:05Z")
    assert pd.Timestamp(result.actual_end_observed_at) == pd.Timestamp("2026-09-09T11:55Z")


def test_future_rows_and_unfinished_minutes_cannot_become_actuals():
    result = compare_forecasts(forecasts().iloc[:1], prices(), observed_at="2026-09-09T11:59:30Z").iloc[0]
    assert result.actuals_status == "PENDING_MATURITY"
    assert result.actual_end_price is None and result.model_brier_score is None
    clocks = compare_price_points(forecasts(), prices(), action_date="2026-09-09",
                                   observed_at="2026-09-09T11:00:30Z", planning_path=path())
    assert pd.isna(clocks.iloc[0].actual_price)
    assert clocks.iloc[1].comparison_status == "PENDING_MATURITY"


def test_neutral_calls_and_cost_adjusted_model_scores_are_distinct():
    data = forecasts().iloc[:1].copy()
    bars = prices([("2026-09-09T11:00Z", 100., 100.), ("2026-09-09T11:59Z", 100., 100.05)])
    bullish = compare_forecasts(data, bars, observed_at="2026-09-10T00:00Z").iloc[0]
    assert bullish.direction_correct == True
    assert bullish.model_observed_target == 0 and bullish.model_brier_score == pytest.approx(.49)
    data["direction"] = "NO_EDGE"
    neutral = compare_forecasts(data, bars, observed_at="2026-09-10T00:00Z").iloc[0]
    assert neutral.direction_correct is None and neutral.direction_result == "No directional call"
    data["direction"] = "BEARISH"
    assert compare_forecasts(data, bars, observed_at="2026-09-10T00:00Z").iloc[0].direction_correct == False


def test_opening_gap_uses_prior_close_and_current_open():
    data = forecasts().loc[lambda df: df.route.eq("1h@gap")]
    bars = prices([("2026-09-08T23:59Z", 900., 100.), ("2026-09-09T11:00Z", 105., 950.)])
    result = compare_forecasts(data, bars, observed_at="2026-09-10T00:00Z").iloc[0]
    assert result.actual_start_price == 100 and result.actual_end_price == 105


def test_source_conflicts_and_changed_frozen_rows_are_rejected():
    bars = prices()
    bars.attrs["stock_price_source"]["dataset"] = "EQUS.MINI"
    with pytest.raises(ValueError, match="source identity"):
        compare_forecasts(forecasts(), bars, observed_at="2026-09-10T00:00Z")
    bars = prices([("2026-09-09T11:00Z", 100., 100.), ("2026-09-09T11:00Z", 200., 200.)])
    with pytest.raises(ValueError, match="conflicting"):
        compare_forecasts(forecasts(), bars, observed_at="2026-09-10T00:00Z")
    trades = forecasts()
    trades.loc[0, "calibrated_probability"] = .9
    with pytest.raises(ValueError, match="changed a frozen"):
        compare_forecasts(forecasts(), prices(), observed_at="2026-09-10T00:00Z", trade_rows=trades)


@pytest.mark.parametrize("field,value", [("symbol", "MU"), ("endpoint_kind", "observed_close"), ("planned_price_high", 90.)])
def test_bad_saved_price_point_is_not_used(field, value):
    saved = path()
    saved["points"]["AAPL|2026-09-09|04:00"][field] = value
    with pytest.raises(ValueError):
        compare_price_points(forecasts(), prices(), action_date="2026-09-09", observed_at="2026-09-10T00:00Z", planning_path=saved)


def test_missing_saved_estimates_are_explicit_and_not_rebuilt():
    result = compare_price_points(forecasts(), prices(), action_date="2026-09-09", observed_at="2026-09-10T00:00Z")
    assert result.iloc[0].comparison_status == "NO_SAVED_ESTIMATE"
    assert result.iloc[0].actual_price == 100 and result.iloc[0].planned_price_mid is None


@pytest.mark.parametrize("stale_actual", [False, True])
def test_native_sparse_planning_close_keeps_observed_actuals_strict(stale_actual):
    from ml.gameplan_price_bands import build_entry_price_bands, build_planning_price_path

    history = _with_source_partitions(prices([
        ("2026-09-02T23:11Z", 100., 100.),
        ("2026-09-03T11:00Z", 95., 95.), ("2026-09-03T23:11Z", 100., 100.),
        ("2026-09-04T11:00Z", 105., 105.), ("2026-09-04T23:11Z", 100., 100.),
        ("2026-09-08T11:00Z", 110., 110.), ("2026-09-08T23:11Z", 200., 200.),
    ]), [("2026-09-01", "2026-09-09")])
    history.attrs["stock_price_source"]["partitions"][0]["published_at"] = "2026-09-09T01:00Z"
    bands = build_entry_price_bands(history, forecasts(), observed_at="2026-09-09T04:00Z",
                                    lookback_sessions=3, minimum_samples=2,
                                    allow_reference_forward_fill=True, allow_sparse_session_references=True)
    saved = build_planning_price_path(history, forecasts(), observed_at=bands["observed_at"],
                                     entry_bands=bands,
                                     allow_reference_forward_fill=True, allow_sparse_session_references=True)
    point = saved["points"]["AAPL|2026-09-09|17:00"]
    assert point["endpoint_kind"] == "planning_close" and point["status"] == "AVAILABLE"
    assert point["reference_is_synthetic"] and point["synthetic_close_sample_count"] == 3
    original = copy.deepcopy(saved)
    bars = prices([("2026-09-09T11:00Z", 100., 100.),
                   ("2026-09-09T23:53Z" if stale_actual else "2026-09-09T23:59Z", 108., 110.)])
    result = compare_price_points(forecasts(), bars, action_date="2026-09-09",
                                  observed_at="2026-09-10T00:00Z", planning_path=saved).iloc[-1]
    assert result.planned_price_mid == point["planned_price_mid"]
    if stale_actual:
        assert result.comparison_status == "MATURE_AWAITING_DATA" and pd.isna(result.actual_price)
    else:
        assert result.comparison_status == "COMPARED" and result.actual_price == 110
        assert result.price_error == 110 - point["planned_price_mid"]
    assert saved == original


@pytest.mark.parametrize("status", ["UNAVAILABLE_REFERENCE_PRICE", "UNAVAILABLE_MINIMUM_SAMPLES"])
def test_sparse_unavailable_close_keeps_no_saved_estimate(status):
    saved = path()
    saved["contract_version"] = "conditional-hourly-planning-price-path-v3"
    point = saved["points"]["AAPL|2026-09-09|17:00"]
    point.update(endpoint_kind="planning_close", status=status,
                 planned_price_low=None, planned_price_mid=None, planned_price_high=None)
    result = compare_price_points(forecasts(), prices(), action_date="2026-09-09",
                                  observed_at="2026-09-10T00:00Z", planning_path=saved).iloc[-1]
    assert result.comparison_status == "NO_SAVED_ESTIMATE" and result.actual_price == 110
    assert pd.isna(result.planned_price_mid) and pd.isna(result.price_error)


@pytest.mark.parametrize("version,hour,endpoint", [
    (None, 17, "planning_close"),
    ("conditional-hourly-planning-price-path-v1", 17, "planning_close"),
    ("conditional-hourly-planning-price-path-v2", 17, "planning_close"),
    ("conditional-hourly-planning-price-path-v3", 17, "observed_close"),
    ("conditional-hourly-planning-price-path-v3", 4, "planning_close"),
])
def test_saved_close_kind_is_bound_to_version_and_clock(version, hour, endpoint):
    saved = path()
    saved["contract_version"] = version
    saved["points"][f"AAPL|2026-09-09|{hour:02d}:00"]["endpoint_kind"] = endpoint
    with pytest.raises(ValueError, match="declared market clock"):
        compare_price_points(forecasts(), prices(), action_date="2026-09-09",
                             observed_at="2026-09-10T00:00Z", planning_path=saved)


def _publication(root, day, name, publications, *, published_hour=1):
    run = root / "ml/nightly-gameplan-runs" / name
    run.mkdir(parents=True)
    frame = forecasts(day)
    frame.to_parquet(run / "forecasts.parquet", index=False)
    receipt = {"action_date": day, "published_at": (pd.Timestamp(day, tz="America/Los_Angeles") + pd.Timedelta(hours=published_hour)).isoformat()}
    (run / "receipt.json").write_text(json.dumps(receipt))
    config = {"target_contract_version": "independent-stock-targets-v1", "target_price_source_contract": "xnas-itch-archive-v1"}
    (run / "manifest.json").write_text(json.dumps({"configuration": config}))
    pub = SimpleNamespace(run_directory=run, receipt=receipt, manifest={"configuration": config})
    publications[run] = pub
    return pub


def _trade_plan(root, publication, *, name=None):
    day = publication.receipt["action_date"]
    run = root / "ml/gameplan-trade-plan-runs" / (name or day)
    run.mkdir(parents=True)
    trades = pd.read_parquet(publication.run_directory / "forecasts.parquet").assign(trade_price_low=99., trade_price_mid=100., trade_price_high=101.)
    trades.to_parquet(run / "trade-plan.parquet", index=False)
    (run / "planning-price-path.json").write_text(json.dumps(path(day)))
    source = publication.run_directory.relative_to(root).as_posix()
    checksum = file_checksum(publication.run_directory / "receipt.json")
    write_manifest(run, run_timestamp=pd.Timestamp(day, tz="UTC"), input_files=[],
                   output_files=["trade-plan.parquet", "planning-price-path.json"],
                   configuration={"source_gameplan_run": source, "source_receipt_sha256": checksum})
    receipt = {"status": "COMPLETE", "run_path": run.relative_to(root).as_posix(), "action_date": day,
               "completed_at": (pd.Timestamp(day, tz="America/Los_Angeles") + pd.Timedelta(hours=2)).isoformat(),
               "source_gameplan_run": source, "source_receipt_sha256": checksum,
               "manifest_sha256": file_checksum(run / "manifest.json"), "orders_placed": 0, "broker_orders_enabled": False}
    (run / "receipt.json").write_text(json.dumps(receipt))
    return run


@pytest.fixture
def publication_case(tmp_path, monkeypatch):
    publications = {}
    original = _publication(tmp_path, "2026-09-09", "original", publications)
    successor = _publication(tmp_path, "2026-09-10", "successor", publications)
    original_trade = _trade_plan(tmp_path, original)
    successor_trade = _trade_plan(tmp_path, successor)
    monkeypatch.setattr("ml.nightly_gameplan.read_gameplan_run", lambda root, run: publications[Path(run)])
    loader = lambda *a, **kw: (prices(), (), {"dataset": "XNAS.ITCH"})
    return SimpleNamespace(root=tmp_path, original=original, successor=successor, original_trade=original_trade,
                           successor_trade=successor_trade, publications=publications, loader=loader,
                           clock=lambda: pd.Timestamp("2026-09-10T10:00Z"))


def test_publication_follows_successor_and_preserves_saved_artifacts(publication_case):
    c = publication_case
    preserved = {p: file_checksum(p) for folder in (c.original.run_directory, c.original_trade, c.successor_trade)
                 for p in folder.iterdir() if p.is_file()}
    run = publish_actuals_review(c.root, gameplan_run=c.successor.run_directory, price_loader=c.loader, clock=c.clock)
    verify_manifest(run)
    report = json.loads((run / "report.json").read_text())
    assert report["action_date"] == "2026-09-09" and report["successor_action_date"] == "2026-09-10"
    assert report["forecasts"]["total"] == 24 and report["forecasts"]["pending_maturity"] == 6
    assert report["orders_placed"] == 0 and report["broker_orders_enabled"] is False
    assert all(file_checksum(p) == checksum for p, checksum in preserved.items())
    pointer = json.loads((c.root / "ml/gameplan-actuals-review-latest/run.json").read_text())
    assert pointer["current"]["receipt_sha256"] == file_checksum(run / "receipt.json")
    assert (c.root / "ml/gameplan-actuals-review-by-date/2026-09-09/run.json").is_file()
    assert (c.root / "ml/gameplan-actuals-review-by-date/2026-09-09/Gameplan-results.md").read_bytes() == (run / "Gameplan-results.md").read_bytes()
    rendered = (run / "Gameplan-results.md").read_text(encoding="utf-8")
    assert "Saved price range" in rendered and "Actual price" in rendered and "No observed price" in rendered
    assert "Pending target end" in rendered and "$999.00" not in rendered


def test_missing_successor_trade_plan_or_expired_deadline_cannot_publish(publication_case):
    c = publication_case
    with pytest.raises(ValueError, match="original 04:00 deadline"):
        publish_actuals_review(c.root, gameplan_run=c.successor.run_directory, clock=lambda: pd.Timestamp("2026-09-10T11:00Z"))
    (c.successor_trade / "receipt.json").unlink()
    with pytest.raises(ValueError, match="must finish"):
        publish_actuals_review(c.root, gameplan_run=c.successor.run_directory, clock=c.clock)
    assert not (c.root / "ml/gameplan-actuals-review-latest/run.json").exists()


def test_explicit_late_successor_does_not_admit_late_historical_estimates(publication_case):
    from tests.test_preparation_deadline import exception_record
    from ml.gameplan_actuals_review import _saved_trade_plan
    c = publication_case
    receipt_path = c.successor_trade/'receipt.json'
    receipt = json.loads(receipt_path.read_text())
    receipt['completed_at'] = '2026-09-10T11:10:00Z'
    receipt_path.write_text(json.dumps(receipt))
    assert _saved_trade_plan(c.root, c.successor) is None
    exception, _ = exception_record(c.root, c.successor.run_directory, session='2026-09-10')
    run = publish_actuals_review(c.root, gameplan_run=c.successor.run_directory, deadline_exception=exception,
                                 price_loader=c.loader, clock=lambda:pd.Timestamp('2026-09-10T11:20:00Z'))
    verify_manifest(run)
    report = json.loads((run/'report.json').read_text())
    assert report['source_trade_plan_path'] == c.original_trade.as_posix()
    assert report['deadline_at'] == '2026-09-10T11:00:00+00:00'
    assert _saved_trade_plan(c.root, c.successor) is None


def test_tampered_saved_estimate_leaves_previous_results_pointer(publication_case):
    c = publication_case
    run = publish_actuals_review(c.root, gameplan_run=c.successor.run_directory, price_loader=c.loader, clock=c.clock)
    pointer = c.root / "ml/gameplan-actuals-review-latest/run.json"
    preserved = pointer.read_bytes()
    (c.original_trade / "planning-price-path.json").write_text("{}")
    with pytest.raises(RuntimeError):
        publish_actuals_review(c.root, gameplan_run=c.successor.run_directory, price_loader=c.loader, clock=c.clock)
    assert pointer.read_bytes() == preserved
    verify_manifest(run)


def test_source_selection_uses_last_saved_preopen_plan_not_current_pointer(publication_case):
    c = publication_case
    earlier = _publication(c.root, "2026-09-09", "earlier", c.publications, published_hour=0)
    after_open = _publication(c.root, "2026-09-09", "too-late", c.publications, published_hour=5)
    assert _latest_saved_gameplan(c.root, "2026-09-09").run_directory == c.original.run_directory
    assert earlier.run_directory != after_open.run_directory


def test_holiday_predecessor_is_last_exchange_session(publication_case):
    c = publication_case
    successor = _publication(c.root, "2026-09-08", "after-holiday", c.publications)
    _trade_plan(c.root, successor)
    run = publish_actuals_review(c.root, gameplan_run=successor.run_directory, clock=lambda: pd.Timestamp("2026-09-08T10:00Z"))
    report = json.loads((run / "report.json").read_text())
    assert report["action_date"] == "2026-09-04"
    assert report["coverage_status"] == "NO_SAVED_INDEPENDENT_GAMEPLAN"


def test_standard_time_keeps_local_price_clocks():
    data = forecasts("2027-01-12")
    bars = prices([("2027-01-12T12:00Z", 100., 100.), ("2027-01-13T00:59Z", 101., 105.)])
    result = compare_price_points(data, bars, action_date="2027-01-12", observed_at="2027-01-13T01:00Z", planning_path=path("2027-01-12"))
    assert result.iloc[0].actual_price == 100 and result.iloc[-1].actual_price == 105


def _with_source_partitions(bars, intervals, *, symbol="AAPL"):
    bars.attrs["stock_price_source"].update(native_archive_partitions_verified=len(intervals), schema="ohlcv-1m",
        partitions=[{"symbol": symbol, "start": start, "end": end, "manifest_path": f"verified/{symbol}/{index}/manifest.json"}
                    for index, (start, end) in enumerate(intervals)])
    return bars


def test_complete_source_still_retains_unscored_stale_boundary_with_exact_evidence():
    data = forecasts().iloc[:1]
    original = data.copy(deep=True)
    bars = _with_source_partitions(prices([("2026-09-09T11:00Z", 100., 100.),
                                          ("2026-09-09T11:53Z", 105., 105.)]),
                                    [("2026-09-09", "2026-09-10")])
    result = compare_forecasts(data, bars, observed_at="2026-09-10T00:00Z").iloc[0]
    assert result.actuals_status == "MATURE_AWAITING_DATA"
    assert result.actual_return is None and result.actual_end_price is None
    assert result.actual_end_status == "OUTSIDE_TOLERANCE"
    assert result.actual_end_source_coverage == "VERIFIED_COMPLETE"
    assert result.actual_end_candidate_price == 105
    assert result.actual_end_candidate_observed_at == "2026-09-09T11:54:00+00:00"
    assert result.actual_end_gap_seconds == 360
    assert result.actual_end_required_source_start == "2026-09-09T11:54:00+00:00"
    assert result.actual_end_required_source_end == "2026-09-09T12:00:00+00:00"
    pd.testing.assert_frame_equal(data, original)


@pytest.mark.parametrize("intervals,expected", [
    (None, "UNKNOWN"),
    ([("2026-09-09T11:54Z", "2026-09-09T11:57Z"), ("2026-09-09T11:58Z", "2026-09-09T12:00Z")], "INCOMPLETE"),
    ([("2026-09-09T11:54Z", "2026-09-09T11:57Z"), ("2026-09-09T11:57Z", "2026-09-09T12:00Z")], "VERIFIED_COMPLETE"),
])
def test_source_interval_coverage_requires_unbroken_same_symbol_window(intervals, expected):
    bars = prices([("2026-09-09T11:00Z", 100., 100.), ("2026-09-09T11:53Z", 105., 105.)])
    if intervals is not None:
        _with_source_partitions(bars, intervals)
    result = compare_forecasts(forecasts().iloc[:1], bars, observed_at="2026-09-10T00:00Z").iloc[0]
    assert result.actual_end_source_coverage == expected
    assert result.actual_end_status == "OUTSIDE_TOLERANCE"
    if intervals is not None:
        for item in bars.attrs["stock_price_source"]["partitions"]:
            item["symbol"] = "COST"
        result = compare_forecasts(forecasts().iloc[:1], bars, observed_at="2026-09-10T00:00Z").iloc[0]
        assert result.actual_end_source_coverage == "INCOMPLETE"


@pytest.mark.parametrize("field,value", [
    ("native_archive_partitions_verified", None), ("native_archive_partitions_verified", 0),
    ("native_archive_partitions_verified", True), ("native_archive_partitions_verified", 1.0),
    ("native_archive_partitions_verified", 2), ("schema", "ohlcv-1h"), ("partitions", []),
    ("partitions", [None]), ("partitions", [{"symbol": "AAPL", "start": "2026-09-09", "end": "2026-09-10"}]),
    ("partitions", [{"symbol": "AAPL", "start": "2026-09-09T11:54", "end": "2026-09-10", "manifest_path": "verified"}]),
])
def test_unproven_native_partition_inventory_is_unknown_not_incomplete(field, value):
    bars = _with_source_partitions(prices(), [("2026-09-09", "2026-09-10")])
    bars.attrs["stock_price_source"][field] = value
    result = compare_forecasts(forecasts().iloc[:1], bars, observed_at="2026-09-10T00:00Z").iloc[0]
    assert result.actual_start_source_coverage == "UNKNOWN"
    assert result.actual_end_source_coverage == "UNKNOWN"
    assert result.actuals_status == "EVALUATED"


def test_no_permitted_candidate_invalid_price_and_future_boundary_remain_distinct():
    bars = prices([("2026-09-09T11:00Z", 0., 100.)])
    result = compare_forecasts(forecasts().iloc[:1], bars, observed_at="2026-09-09T11:30Z").iloc[0]
    assert result.actual_start_status == "INVALID_OBSERVATION"
    assert result.actual_start_candidate_price == 0
    assert result.actual_end_status == "PENDING_MATURITY"
    assert result.actual_end_candidate_observed_at is None and result.actual_end_gap_seconds is None
    bars = _with_source_partitions(prices([("2026-09-09T12:01Z", 100., 100.)]),
                                    [("2026-09-09", "2026-09-10")])
    result = compare_forecasts(forecasts().iloc[:1], bars, observed_at="2026-09-10T00:00Z").iloc[0]
    assert result.actual_end_status == "NO_OBSERVATION"
    assert result.actual_end_candidate_observed_at is None
    assert result.actual_end_source_coverage == "VERIFIED_COMPLETE"


def test_diagnostic_preserves_exact_five_minute_rules_and_opening_gap_sides():
    bars = prices([("2026-09-08T23:53Z", 900., 100.), ("2026-09-09T11:05Z", 105., 950.),
                   ("2026-09-09T11:54Z", 106., 110.)])
    result = compare_forecasts(forecasts(), bars, observed_at="2026-09-10T00:00Z").set_index("route")
    assert result.loc["1h@04:00", "actual_start_status"] == "OBSERVED"
    assert result.loc["1h@04:00", "actual_end_status"] == "OBSERVED"
    assert result.loc["1h@04:00", "actuals_status"] == "EVALUATED"
    assert result.loc["1h@04:00", "actual_start_gap_seconds"] == 300
    assert result.loc["1h@04:00", "actual_end_gap_seconds"] == 300
    assert result.loc["1h@gap", "actual_start_status"] == "OUTSIDE_TOLERANCE"
    assert result.loc["1h@gap", "actual_start_candidate_price"] == 100
    assert result.loc["1h@gap", "actual_end_candidate_price"] == 105
    assert result.loc["1h@gap", "actual_start_candidate_observed_at"] == "2026-09-08T23:54:00+00:00"


def test_render_explains_verified_missing_boundary_and_preserves_legacy_rows():
    data = forecasts()
    bars = _with_source_partitions(prices([("2026-09-09T11:00Z", 100., 100.),
                                          ("2026-09-09T11:53Z", 105., 105.)]),
                                    [("2026-09-09", "2026-09-10")])
    results = compare_forecasts(data, bars, observed_at="2026-09-10T00:00Z")
    clocks = compare_price_points(data, bars, action_date="2026-09-09", observed_at="2026-09-10T00:00Z")
    assert clocks.loc[clocks.clock_local.eq("05:00"), "actual_status"].iloc[0] == "NO_OBSERVATION"
    assert clocks.loc[clocks.clock_local.eq("05:00"), "actual_source_coverage"].iloc[0] == "VERIFIED_COMPLETE"
    report = {"action_date": "2026-09-09", "successor_action_date": "2026-09-10",
              "forecasts": {"evaluated": 0, "pending_maturity": 6, "mature_awaiting_data": 18}}
    rendered = render_actuals_review(results, clocks, report)
    assert "No observed price within five minutes" in rendered
    assert "nearest Sep 09 04:54; 6 minutes away" in rendered
    assert "source request covers the full tolerance window" in rendered
    assert "missing eligible price observations" in rendered
    legacy = results.drop(columns=[name for name in results if name.startswith(("actual_start_", "actual_end_"))
                                 and name not in {"actual_start_price", "actual_start_observed_at", "actual_end_price", "actual_end_observed_at"}])
    legacy_clocks = clocks.drop(columns=[name for name in clocks if name.startswith("actual_")
                                        and name not in {"actual_price", "actual_observed_at"}])
    assert "Waiting for data" in render_actuals_review(legacy, legacy_clocks, report)
