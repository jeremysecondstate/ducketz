from datetime import date
import json

import pandas as pd
import pytest

from ml.artifacts import write_manifest
from ml.gameplan_evaluation import evaluate_forecasts, evaluate_saved_gameplans, read_evaluation_history
from ml.gameplan_source_selection import GAMEPLAN_SOURCE_SELECTION_VERSION
from ml.independent_stock_targets import STOCK_TARGET_CONTRACT_VERSION
from ml.nightly_gameplan import _intraday_outcomes, _publish_gameplan


def saved_plan(root, name, action_date, *, end="2026-09-10T20:00:00Z"):
    run = root / "ml/nightly-gameplan-runs" / name
    run.mkdir(parents=True)
    frame = pd.DataFrame([{
        "id": f"{action_date}:AAPL:1d@D+5", "symbol": "AAPL", "route": "1d@D+5",
        "target_window_start": pd.Timestamp("2026-09-10T13:30:00Z"),
        "target_window_end": pd.Timestamp(end), "calibrated_probability": 0.7,
        "model_group": "1d", "model_status": "PROMOTED",
    }])
    frame.to_parquet(run / "forecasts.parquet", index=False)
    write_manifest(run, run_timestamp="2026-09-04T10:00:00Z", input_files=(),
                   output_files=("forecasts.parquet",), configuration={"action_date": action_date}, datastore_root=root)
    _publish_gameplan(root, run=run, action_date=date.fromisoformat(action_date),
                      published_at=pd.Timestamp("2026-09-04T10:01:00Z"), source_loop_b="test", source_strategy="test")
    return run


def outcomes():
    return {"1d": pd.DataFrame([{
        "symbol": "AAPL", "route": "1d@D+5", "target_window_start": pd.Timestamp("2026-09-10T13:30:00Z"),
        "target_window_end": pd.Timestamp("2026-09-10T20:00:00Z"), "target": 1, "observed_return": 0.02,
    }])}


def test_pending_forecast_survives_pointer_advance_and_scores_once(tmp_path):
    first = saved_plan(tmp_path, "first", "2026-09-04")
    initial = evaluate_saved_gameplans(tmp_path, observed_groups={}, evaluated_at="2026-09-05T16:00:00Z")
    assert initial.summary["review_week"]["forecasts"] == 1
    assert initial.evaluations.iloc[0].evaluation_status == "PENDING_MATURITY"
    saved_plan(tmp_path, "second", "2026-09-08")
    # A new current plan must not hide the older plan when its D+5 forecast matures.
    result = evaluate_saved_gameplans(tmp_path, observed_groups=outcomes(), evaluated_at="2026-09-11T01:00:00Z")
    assert len(result.evaluations) == 2
    old = result.evaluations.loc[result.evaluations.source_gameplan_run.eq(first.relative_to(tmp_path).as_posix())].iloc[0]
    assert old.evaluation_status == "EVALUATED"
    assert old.brier_score == pytest.approx(0.09)
    again = evaluate_saved_gameplans(tmp_path, observed_groups={}, evaluated_at="2026-09-12T16:00:00Z")
    pd.testing.assert_frame_equal(result.evaluations, again.evaluations)


def test_mature_missing_data_is_visible_and_later_retried(tmp_path):
    saved_plan(tmp_path, "first", "2026-09-04")
    missing = evaluate_saved_gameplans(tmp_path, observed_groups={}, evaluated_at="2026-09-11T01:00:00Z")
    assert missing.evaluations.iloc[0].evaluation_status == "MATURE_AWAITING_DATA"
    assert missing.summary["all_saved_gameplans"]["mature_awaiting_data"] == 1
    result = evaluate_saved_gameplans(tmp_path, observed_groups=outcomes(), evaluated_at="2026-09-12T16:00:00Z")
    assert result.evaluations.iloc[0].evaluation_status == "EVALUATED"


def test_only_september_four_onward_and_only_receipted_plans(tmp_path):
    saved_plan(tmp_path, "old", "2026-09-03")
    saved_plan(tmp_path, "first", "2026-09-04")
    incomplete = tmp_path / "ml/nightly-gameplan-runs/incomplete"
    incomplete.mkdir()
    (incomplete / "forecasts.parquet").write_text("failed training output")
    result = evaluate_saved_gameplans(tmp_path, observed_groups={}, evaluated_at="2026-09-05T16:00:00Z")
    assert result.evaluations.action_date.tolist() == ["2026-09-04"]


def test_saved_forecast_corruption_fails_without_losing_history(tmp_path):
    first = saved_plan(tmp_path, "first", "2026-09-04")
    result = evaluate_saved_gameplans(tmp_path, observed_groups={}, evaluated_at="2026-09-05T16:00:00Z")
    saved_plan(tmp_path, "second", "2026-09-08")
    with (first / "forecasts.parquet").open("ab") as output:
        output.write(b"corrupt")
    with pytest.raises((ValueError, RuntimeError)):
        evaluate_saved_gameplans(tmp_path, observed_groups=outcomes(), evaluated_at="2026-09-11T01:00:00Z")
    assert read_evaluation_history(tmp_path).run_directory == result.run_directory


def test_evaluation_history_is_checksum_verified(tmp_path):
    saved_plan(tmp_path, "first", "2026-09-04")
    result = evaluate_saved_gameplans(tmp_path, observed_groups={}, evaluated_at="2026-09-05T16:00:00Z")
    (result.run_directory / "summary.json").write_text(json.dumps({"evaluated": 10000}))
    with pytest.raises((ValueError, RuntimeError)):
        read_evaluation_history(tmp_path)


def test_repeated_editions_have_separate_forecast_identities(tmp_path):
    saved_plan(tmp_path, "first", "2026-09-04")
    saved_plan(tmp_path, "second", "2026-09-04")
    result = evaluate_saved_gameplans(tmp_path, observed_groups={}, evaluated_at="2026-09-05T16:00:00Z")
    assert result.evaluations.id.nunique() == 2


def test_future_outcomes_are_not_scored_early(tmp_path):
    saved_plan(tmp_path, "first", "2026-09-04")
    result = evaluate_saved_gameplans(tmp_path, observed_groups=outcomes(), evaluated_at="2026-09-05T16:00:00Z")
    assert result.evaluations.iloc[0].evaluation_status == "PENDING_MATURITY"


def test_four_hour_evaluation_waits_for_an_actual_window_end_price(tmp_path):
    run = tmp_path / "ml/nightly-gameplan-runs/four-hour"
    run.mkdir(parents=True)
    forecasts = pd.DataFrame([{
        "id": "2026-09-04:AAPL:4h@16:00", "symbol": "AAPL", "route": "4h@16:00",
        "target_window_start": pd.Timestamp("2026-09-04T19:00:00Z"),
        "target_window_end": pd.Timestamp("2026-09-04T23:00:00Z"),
        "calibrated_probability": 0.5, "model_group": "4h", "model_status": "PROMOTED",
    }])
    forecasts.to_parquet(run / "forecasts.parquet", index=False)
    write_manifest(run, run_timestamp="2026-09-04T10:00:00Z", input_files=(),
                   output_files=("forecasts.parquet",), configuration={"action_date": "2026-09-04"}, datastore_root=tmp_path)
    _publish_gameplan(tmp_path, run=run, action_date=date(2026, 9, 4),
                      published_at=pd.Timestamp("2026-09-04T10:01:00Z"), source_loop_b="test", source_strategy="test")
    sources = pd.DataFrame([{
        "symbol": "AAPL", "action_date": date(2026, 9, 4),
        "decision_timestamp": pd.Timestamp("2026-09-04T00:05:00Z"),
    }])
    bars = pd.DataFrame({
        "symbol": "AAPL", "open": 100.0, "close": 101.0,
        "timestamp": pd.to_datetime(["2026-09-04T19:00:00Z", "2026-09-04T19:59:00Z"]),
    })
    _, incomplete = _intraday_outcomes(sources=sources, feature_columns=(), minute_bars=bars)
    waiting = evaluate_saved_gameplans(tmp_path, observed_groups={"4h": incomplete}, evaluated_at="2026-09-05T01:00:00Z")
    assert waiting.evaluations.iloc[0].evaluation_status == "MATURE_AWAITING_DATA"
    bars.loc[1, "timestamp"] = pd.Timestamp("2026-09-04T22:59:00Z")
    _, complete = _intraday_outcomes(sources=sources, feature_columns=(), minute_bars=bars)
    scored = evaluate_saved_gameplans(tmp_path, observed_groups={"4h": complete}, evaluated_at="2026-09-05T02:00:00Z")
    assert scored.evaluations.iloc[0].evaluation_status == "EVALUATED"
    assert scored.evaluations.iloc[0].brier_score == 0.25


def independent_forecast_and_outcome():
    common = {
        "symbol": "COST", "route": "1h@13:00",
        "target_window_start": pd.Timestamp("2026-09-10T20:00:00Z"),
        "target_window_end": pd.Timestamp("2026-09-10T21:00:00Z"),
        "target_contract_version": STOCK_TARGET_CONTRACT_VERSION,
        "target_price_source_contract": "xnas-itch-archive-v1",
        "target_price_dataset": "XNAS.ITCH",
    }
    forecast = {**common, "id": "COST:1h@13:00", "source_gameplan_run": "old-selector-plan",
                "action_date": "2026-09-10", "model_group": "1h",
                "model_status": "PROMOTED", "calibrated_probability": 0.7}
    return forecast, {**common, "target": 1, "observed_return": 0.02}


@pytest.mark.parametrize("forecast_selection,outcome_selection", [
    (None, GAMEPLAN_SOURCE_SELECTION_VERSION),
    (GAMEPLAN_SOURCE_SELECTION_VERSION, None),
])
def test_independent_outcomes_do_not_cross_match_feature_selection_contracts(
    forecast_selection, outcome_selection,
):
    forecast, outcome = independent_forecast_and_outcome()
    forecast["source_selection_contract"] = forecast_selection
    outcome["source_selection_contract"] = outcome_selection
    result = evaluate_forecasts(pd.DataFrame([forecast]),
        observed_groups={"same-physical-clocks": pd.DataFrame([outcome])},
        evaluated_at="2026-09-11T01:00:00Z")
    assert result.iloc[0].evaluation_status == "MATURE_AWAITING_DATA"
    assert pd.isna(result.iloc[0].observed_target)
    assert pd.isna(result.iloc[0].brier_score)


def test_independent_contracts_keep_separate_outcomes_at_identical_clocks():
    legacy, old_outcome = independent_forecast_and_outcome()
    current = {**legacy, "source_gameplan_run": "prior-session-plan",
               "source_selection_contract": GAMEPLAN_SOURCE_SELECTION_VERSION}
    # Deliberately opposing fixture labels make accidental lookup overwrites
    # visible even though the source, symbol, route and clocks are identical.
    new_outcome = {**old_outcome, "source_selection_contract": GAMEPLAN_SOURCE_SELECTION_VERSION,
                   "target": 0, "observed_return": -0.01}
    result = evaluate_forecasts(pd.DataFrame([legacy, current]),
        observed_groups={"legacy": pd.DataFrame([old_outcome]),
                         "prior-session": pd.DataFrame([new_outcome])},
        evaluated_at="2026-09-11T01:00:00Z").set_index("source_gameplan_run")
    assert result.evaluation_status.tolist() == ["EVALUATED", "EVALUATED"]
    assert result.loc["old-selector-plan", "observed_target"] == 1
    assert result.loc["old-selector-plan", "brier_score"] == pytest.approx(0.09)
    assert result.loc["prior-session-plan", "observed_target"] == 0
    assert result.loc["prior-session-plan", "brier_score"] == pytest.approx(0.49)


@pytest.mark.parametrize("null_selection", [None, float("nan"), pd.NA])
def test_absent_and_null_legacy_feature_selection_preserve_matching(null_selection):
    forecast, outcome = independent_forecast_and_outcome()
    # Concatenating old publications with versioned publications produces a
    # null column for old rows; it must match the original absent-column form.
    forecast["source_selection_contract"] = null_selection
    result = evaluate_forecasts(pd.DataFrame([forecast]),
        observed_groups={"legacy": pd.DataFrame([outcome])},
        evaluated_at="2026-09-11T01:00:00Z")
    assert result.iloc[0].evaluation_status == "EVALUATED"
    assert result.iloc[0].brier_score == pytest.approx(0.09)


def test_og_and_yg_score_their_own_targets_and_compare_same_observed_window():
    from ml.gameplan_evaluation import compare_gameplan_variants
    from ml.gameplan_probability_target import RAW_DIRECTION_TARGET
    og, outcome = independent_forecast_and_outcome()
    og.update(calibrated_probability=.3, direction="BEARISH")
    yg = {**og, "source_gameplan_run": "yg-plan", "calibrated_probability": .7,
          "direction": "BULLISH", "probability_target_contract": RAW_DIRECTION_TARGET, "gameplan_variant": "YG"}
    # The physical observation can come from a YG cohort; OG must still score
    # the small gain below cost as class zero, and YG as raw class one.
    outcome.update(observed_return=.0005, target=1, probability_target_contract=RAW_DIRECTION_TARGET)
    result = evaluate_forecasts(pd.DataFrame([og, yg]), observed_groups={"verified": pd.DataFrame([outcome])},
                                evaluated_at="2026-09-11T01:00:00Z")
    assert result.observed_target.tolist() == [0, 1]
    assert result.gameplan_variant.tolist() == ["OG", "YG"]
    assert result.brier_score.tolist() == pytest.approx([.09, .09])
    assert result.raw_direction_correct.tolist() == [False, True]
    assert result.observed_cost_adjusted_positive.tolist() == [0, 0]
    assert result.cost_adjusted_return.tolist() == pytest.approx([-.0005, -.0005])
    comparison = compare_gameplan_variants(result)["pairs"][0]
    assert comparison["evaluated_same_windows"] == 1
    assert comparison["og_direction_accuracy"] == 0 and comparison["yg_direction_accuracy"] == 1
    assert compare_gameplan_variants(result.loc[result.gameplan_variant.eq("YG")])["pairs"] == []
    preserved = evaluate_forecasts(pd.DataFrame([og, yg]), observed_groups={}, previous=result,
                                   evaluated_at="2026-09-12T01:00:00Z")
    pd.testing.assert_frame_equal(result, preserved)


@pytest.mark.parametrize("contract", ["future-unknown-target", ""])
def test_unknown_probability_contract_rejected_even_for_pending_forecast(contract):
    row, _ = independent_forecast_and_outcome()
    row["probability_target_contract"] = contract
    with pytest.raises(ValueError, match="Unknown Gameplan probability target"):
        evaluate_forecasts(pd.DataFrame([row]), observed_groups={}, evaluated_at="2026-09-09T01:00:00Z")


def test_an_evaluated_og_cannot_be_reinterpreted_as_yg():
    from ml.gameplan_probability_target import RAW_DIRECTION_TARGET
    row, outcome = independent_forecast_and_outcome()
    original = evaluate_forecasts(pd.DataFrame([row]), observed_groups={"verified": pd.DataFrame([outcome])},
                                 evaluated_at="2026-09-11T01:00:00Z")
    row["probability_target_contract"] = RAW_DIRECTION_TARGET
    with pytest.raises(RuntimeError, match="immutable forecast changed"):
        evaluate_forecasts(pd.DataFrame([row]), observed_groups={}, previous=original,
                           evaluated_at="2026-09-12T01:00:00Z")


def test_og_yg_comparison_never_pairs_other_sources_or_counts_pending():
    from ml.gameplan_evaluation import compare_gameplan_variants
    from ml.gameplan_probability_target import RAW_DIRECTION_TARGET
    og, outcome = independent_forecast_and_outcome()
    yg = {**og, "source_gameplan_run": "yg-plan", "probability_target_contract": RAW_DIRECTION_TARGET}
    pending = evaluate_forecasts(pd.DataFrame([og, yg]), observed_groups={}, evaluated_at="2026-09-09T01:00:00Z")
    pair = compare_gameplan_variants(pending)["pairs"][0]
    assert pair["status"] == "AWAITING_MATCHED_OUTCOMES"
    assert pair["evaluated_same_windows"] == 0 and pair["yg_direction_accuracy"] is None
    pending.loc[pending.gameplan_variant.eq("YG"), "target_price_dataset"] = "EQUS.MINI"
    assert compare_gameplan_variants(pending)["pairs"][0]["matched_windows"] == 0
