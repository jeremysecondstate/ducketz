"""Stats retain original direction rules through nightly and peer publications."""
import json

import pandas as pd
import pytest

from app.ui.gameplan_stats_data import load_gameplan_stats
from ml.artifacts import file_checksum, verify_manifest
from ml.gameplan_actuals_review import compare_forecasts, render_actuals_review
from ml.gameplan_evaluation import evaluate_forecasts
from ml.gameplan_stats_handoff import adopt_combined_stats, export_stats_package, read_stats_package
from ml.stock_direction_policy import STOCK_DIRECTION_POLICY_VERSION, saved_direction_policy_summary
from test_gameplan_actuals_review import forecasts, prices
from tests.gameplan_stats_fixture import forecast, write_review


DAY = "2026-10-19"


@pytest.mark.parametrize("version,probability,direction", [
    (STOCK_DIRECTION_POLICY_VERSION, .5001, "BULLISH"),
    (STOCK_DIRECTION_POLICY_VERSION, .4999, "BEARISH"),
    (STOCK_DIRECTION_POLICY_VERSION, .50005, "NO_EDGE"),
    ("stock-direction-50-v2", .50005, "BULLISH"),
    (None, .52, "NO_EDGE"),
])
def test_actuals_keep_the_saved_policy_call_probability_and_score(version, probability, direction):
    source = forecasts(DAY).loc[lambda frame: frame.route.eq("1h@04:00")].copy()
    source["calibrated_probability"], source["direction"] = probability, direction
    if version is not None:
        source["direction_policy_version"] = version
    original = source.copy(deep=True)
    bars = prices([("2026-10-19T11:00Z", 100., 101.), ("2026-10-19T11:59Z", 104., 105.)])
    reviewed = compare_forecasts(source, bars, observed_at="2026-10-20T00:00Z")
    row = reviewed.iloc[0]
    assert row.direction == direction and row.calibrated_probability == probability
    assert row.model_brier_score == pytest.approx((probability - 1) ** 2)
    if direction == "NO_EDGE":
        assert row.direction_correct is None
    else:
        assert bool(row.direction_correct) == (direction == "BULLISH")
    pd.testing.assert_frame_equal(source, original)
    summary = saved_direction_policy_summary(reviewed.to_dict("records"))
    assert summary["direction_policy_versions"] == ([version] if version else [])
    assert summary["unrecorded_direction_policy_forecasts"] == (0 if version else 1)


def test_unknown_and_unrecorded_historical_cutoffs_are_never_inferred():
    summary = saved_direction_policy_summary([{}, {"direction_policy_version": float("nan")},
        {"direction_policy_version": "older-original-rule"}])
    assert summary["direction_policy_cutoffs"] == {}
    assert summary["direction_policy_versions"] == ["older-original-rule"]
    assert summary["unrecorded_direction_policy_forecasts"] == 2


def test_both_stats_packages_retain_policy_versions_and_score_original_labels(tmp_path):
    paths, owners, originals = {}, {"Atlas": ("AAPL",), "Scout": ("ABCL",)}, {}
    for actor, symbols in owners.items():
        rows = []
        for hour, probability, direction, version in (
            (4, .5001, "BULLISH", STOCK_DIRECTION_POLICY_VERSION),
            (5, .50005, "NO_EDGE", STOCK_DIRECTION_POLICY_VERSION),
            (6, .50005, "BULLISH", "stock-direction-50-v2"),
            (7, .52, "NO_EDGE", None),
        ):
            row = forecast(symbols[0], hour=hour, probability=probability, direction=direction, change=.01, session=DAY)
            if version:
                row["direction_policy_version"] = version
            rows.append(row)
        root = tmp_path / actor
        run = write_review(root, rows, session=DAY)
        originals[run / "forecast-results.parquet"] = (run / "forecast-results.parquet").read_bytes()
        paths[actor] = export_stats_package(root, producer=actor, symbols=symbols,
            destination=tmp_path / (actor + "-stats.json"))
        _, packaged = read_stats_package(paths[actor], expected_sha256=file_checksum(paths[actor]))
        assert packaged.direction_policy_version.iloc[:3].tolist() == [STOCK_DIRECTION_POLICY_VERSION,
            STOCK_DIRECTION_POLICY_VERSION, "stock-direction-50-v2"]
    combined_root = tmp_path / "combined"
    run = adopt_combined_stats(combined_root, packages=paths, expected_symbols=owners,
        expected_sha256={actor: file_checksum(path) for actor, path in paths.items()}, reviewed_at="2026-10-20T07:00Z")
    manifest = verify_manifest(run)
    report = json.loads((run / "report.json").read_text())
    assert report["direction_policy_versions"] == [STOCK_DIRECTION_POLICY_VERSION, "stock-direction-50-v2"]
    assert report["unrecorded_direction_policy_forecasts"] == 2
    assert report["direction_policy_cutoffs"][STOCK_DIRECTION_POLICY_VERSION]["bullish_probability"] == .5001
    assert manifest["configuration"]["direction_policy_versions"] == report["direction_policy_versions"]
    review = load_gameplan_stats(combined_root)
    assert review.metrics().scored == review.metrics().correct == 4
    assert review.metrics().neutral == 4
    assert {row.direction_policy_version for row in review.outcomes} == {None, STOCK_DIRECTION_POLICY_VERSION, "stock-direction-50-v2"}
    rendered = (run / "Gameplan-results.md").read_text(encoding="utf-8")
    assert "50.01%" in rendered and "49.99%" in rendered and "original saved call" in rendered
    assert all(path.read_bytes() == original for path, original in originals.items())


@pytest.mark.parametrize("version,expected", [(STOCK_DIRECTION_POLICY_VERSION, "NO_EDGE"),
                                              ("stock-direction-50-v2", "BULLISH"), (None, "BULLISH")])
def test_missing_evaluation_label_uses_only_the_explicit_saved_policy(version, expected):
    start, end = pd.Timestamp("2026-10-19T11:00Z"), pd.Timestamp("2026-10-19T12:00Z")
    source = {"id": "saved", "source_gameplan_run": "original", "symbol": "AAPL", "action_date": DAY,
              "route": "1h@04:00", "model_group": "1h", "model_status": "PROMOTED",
              "target_window_start": start, "target_window_end": end, "calibrated_probability": .50005}
    if version is not None:
        source["direction_policy_version"] = version
    observed = pd.DataFrame([{**source, "target": 1, "observed_return": .01}])
    first = evaluate_forecasts(pd.DataFrame([source]), observed_groups={"1h": observed}, evaluated_at=end)
    assert first.iloc[0].direction == expected
    # The historical binary-target accuracy is distinct from saved-call accuracy.
    assert bool(first.iloc[0].direction_correct) is True
    assert pd.isna(first.iloc[0].raw_direction_correct) == (expected == "NO_EDGE")
    again = evaluate_forecasts(pd.DataFrame([source]), observed_groups={}, evaluated_at=end, previous=first)
    pd.testing.assert_frame_equal(first, again)


def test_stats_report_displays_actual_boundaries_without_estimate_price_tables():
    source = forecasts(DAY).loc[lambda frame: frame.route.eq("1h@04:00")].copy()
    source["direction_policy_version"] = STOCK_DIRECTION_POLICY_VERSION
    bars = prices([("2026-10-19T11:00Z", 100., 101.), ("2026-10-19T11:59Z", 104., 105.)])
    reviewed = compare_forecasts(source, bars, observed_at="2026-10-20T00:00Z")
    report = {"action_date": DAY, "successor_action_date": "2026-10-20",
              "forecasts": {"evaluated": 1, "pending_maturity": 0, "mature_awaiting_data": 0}}
    rendered = render_actuals_review(reviewed, pd.DataFrame(), report)
    assert "Actual start" in rendered and "Actual end" in rendered and "$100.00" in rendered
    assert "50.01%" in rendered and "49.99%" in rendered
    for removed in ("Planning price", "Execution mid", "Saved price range", "Saved midpoint", "Mean absolute price error"):
        assert removed not in rendered
