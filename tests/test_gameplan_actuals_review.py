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
    assert "Saved price range" in rendered and "Actual price" in rendered and "Waiting for data" in rendered
    assert "Pending target end" in rendered and "$999.00" not in rendered


def test_missing_successor_trade_plan_or_expired_deadline_cannot_publish(publication_case):
    c = publication_case
    with pytest.raises(ValueError, match="original 04:00 deadline"):
        publish_actuals_review(c.root, gameplan_run=c.successor.run_directory, clock=lambda: pd.Timestamp("2026-09-10T11:00Z"))
    (c.successor_trade / "receipt.json").unlink()
    with pytest.raises(ValueError, match="must finish"):
        publish_actuals_review(c.root, gameplan_run=c.successor.run_directory, clock=c.clock)
    assert not (c.root / "ml/gameplan-actuals-review-latest/run.json").exists()


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
