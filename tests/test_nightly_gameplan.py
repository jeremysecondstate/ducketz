from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pandas as pd
import numpy as np
import pytest

from ml.gameplan_executor import _routes_for_action_anchor, run_gameplan_decision_once
from ml.artifacts import file_checksum

from ml.nightly_gameplan import (
    FOUR_HOUR_ANCHORS,
    HOURLY_ANCHORS,
    _build_current_groups,
    _candidate_exposure_matches_direction,
    _candidate_uses_latest_completed_session,
    _chronological_partitions,
    _latest_candidate_session_by_symbol,
    _option_session_route_is_tradable,
    _select_option_candidate_for_route,
    _verify_opra_history,
    _calibration_signal_diagnostics,
    _fit_group_model,
)
from ml.overnight_runtime import (
    STAGE_ORDER,
    record_scheduled_noop,
    run_overnight_pipeline,
    scheduled_session_eligibility,
)


def test_current_gameplan_grid_has_exact_requested_routes() -> None:
    symbols = ("AAPL", "AMZN", "GOOG", "MU", "NVDA", "SNDK")
    action_date = date(2026, 9, 4)
    current_sources = pd.DataFrame(
        [
            {
                "symbol": symbol,
                "decision_timestamp": pd.Timestamp("2026-09-04T00:05:00Z"),
                "information_available_at": pd.Timestamp("2026-09-04T00:05:00Z"),
                "bar_end_timestamp": pd.Timestamp("2026-09-04T00:00:00Z"),
                "mr__x": float(index),
            }
            for index, symbol in enumerate(symbols)
        ]
    )
    components = []
    for index, horizon in enumerate(("1w-d1", "1w-d2", "1w-d3", "1w-d4", "1w-d5"), 1):
        for symbol_index, symbol in enumerate(symbols):
            start = pd.Timestamp("2026-09-04T13:30:00Z") + pd.Timedelta(days=index - 1)
            components.append(
                {
                    "symbol": symbol,
                    "horizon": horizon,
                    "label_status": "INCOMPLETE_LABEL",
                    "decision_timestamp": pd.Timestamp("2026-09-03T20:05:00Z"),
                    "information_available_at": pd.Timestamp("2026-09-03T20:05:00Z"),
                    "target_window_start": start,
                    "target_window_end": start + pd.Timedelta(hours=6, minutes=30),
                    "mr__x": float(symbol_index),
                }
            )
    current = _build_current_groups(
        pd.DataFrame(components),
        current_sources=current_sources,
        action_date=action_date,
        as_of=pd.Timestamp("2026-09-04T08:00:00Z"),
        symbols=symbols,
        feature_columns=("mr__x",),
    )

    assert len(current["1h"]) == 6 * 14
    assert len(current["4h"]) == 6 * 4
    assert len(current["1d"]) == 6 * 5
    assert len(current["1w"]) == 6
    assert set(current["1h"]["forecast_anchor_local"]) == {
        f"{hour:02d}:00" for hour in HOURLY_ANCHORS
    }
    assert set(current["4h"]["forecast_anchor_local"]) == {
        f"{hour:02d}:00" for hour in FOUR_HOUR_ANCHORS
    }


def test_completed_session_quotes_are_valid_planning_evidence() -> None:
    legs = json.dumps(
        [
            {
                "asset": "OPTION",
                "target_snapshot_for": "2026-09-03T20:00:00Z",
            },
            {
                "asset": "OPTION",
                "target_snapshot_for": "2026-09-03T20:00:00Z",
            },
        ]
    )
    candidates = pd.DataFrame(
        [{"symbol": "AAPL", "legs_json": legs}]
    )
    latest = _latest_candidate_session_by_symbol(candidates)

    assert latest["AAPL"] == pd.Timestamp("2026-09-03T20:00:00Z")
    assert _candidate_uses_latest_completed_session(
        candidates.iloc[0].to_dict(), latest["AAPL"]
    )


def test_route_candidate_matches_direction_and_options_session() -> None:
    candidates = pd.DataFrame(
        [
            {
                "symbol": "AAPL",
                "horizon": "1h",
                "candidate_key": "bear",
                "candidate_rank": 1,
                "score_basis": "OPRA_EXECUTION_CALIBRATED_MODEL",
                "decision_score": 0.80,
                "expected_return_on_risk": 0.20,
                "net_delta": -0.30,
            },
            {
                "symbol": "AAPL",
                "horizon": "1h",
                "candidate_key": "bull",
                "candidate_rank": 2,
                "score_basis": "OPRA_EXECUTION_CALIBRATED_MODEL",
                "decision_score": 0.70,
                "expected_return_on_risk": 0.10,
                "net_delta": 0.25,
            },
        ]
    )
    selected = _select_option_candidate_for_route(
        candidates,
        symbol="AAPL",
        horizon="1h",
        direction="BULLISH",
    )

    assert selected is not None
    assert selected["candidate_key"] == "bull"
    assert _candidate_exposure_matches_direction(
        selected, direction="BULLISH"
    )
    assert not _option_session_route_is_tradable("1h", "1h@07:00")
    assert _option_session_route_is_tradable("1h", "1h@08:00")
    assert _option_session_route_is_tradable("1h", "1h@13:00")
    assert not _option_session_route_is_tradable("1h", "1h@04:00")
    assert not _option_session_route_is_tradable("1h", "1h@14:00")
    assert not _option_session_route_is_tradable("4h", "4h@08:00")
    assert _option_session_route_is_tradable("4h", "4h@12:00")


def test_action_clock_consumes_forward_checkpoint_routes() -> None:
    assert _routes_for_action_anchor(4) == {
        "1h@04:00",
        "4h@04:00",
        "1h@05:00",
        "4h@08:00",
    }
    assert _routes_for_action_anchor(5) == {"1h@06:00"}
    assert _routes_for_action_anchor(8) == {"1h@09:00", "4h@12:00"}
    assert _routes_for_action_anchor(12) == {"1h@13:00", "4h@16:00"}
    assert _routes_for_action_anchor(16) == {"1h@17:00"}
    assert _routes_for_action_anchor(17) == set()


def test_chronological_partitions_never_share_decision_clusters() -> None:
    rows = []
    for index, timestamp in enumerate(
        pd.date_range("2026-01-01T00:00:00Z", periods=64, freq="1D")
    ):
        for route_index, route in enumerate(("1h@04:00", "1h@05:00")):
            rows.append(
                {
                    "symbol": "AAPL",
                    "route": route,
                    "decision_timestamp": timestamp,
                    "target_window_start": timestamp + pd.Timedelta(hours=11 + route_index),
                    "target_window_end": timestamp + pd.Timedelta(hours=12 + route_index),
                    "target": (index + route_index) % 2,
                    "observed_return": 0.01,
                    "model_group": "1h",
                    "mr__x": float(index),
                }
            )
    partitions = _chronological_partitions(pd.DataFrame(rows), group="1h")
    cluster_sets = [
        set(frame["decision_timestamp"]) for frame in partitions.values()
    ]

    assert all(frame["target"].nunique() == 2 for frame in partitions.values())
    for index, left in enumerate(cluster_sets):
        for right in cluster_sets[index + 1 :]:
            assert left.isdisjoint(right)


def test_daytime_reader_consumes_frozen_routes_without_orders(
    tmp_path, monkeypatch
) -> None:
    run = tmp_path / "ml" / "nightly-gameplan-runs" / "run-1"
    run.mkdir(parents=True)
    forecasts = pd.DataFrame(
        [
            {
                "id": f"forecast-{route}",
                "symbol": "AAPL",
                "route": route,
                "action_anchor_local": "04:00",
                "model_status": "PROMOTED",
                "direction": "BULLISH",
                "calibrated_probability": 0.61,
            }
            for route in ("1h@04:00", "4h@04:00", "1h@05:00", "4h@08:00")
        ]
    )
    intents = pd.DataFrame(
        [
            {
                "id": f"intent-{route}",
                "symbol": "AAPL",
                "route": route,
                "candidate_key": "bull_call_spread|w1",
                "plan_status": "FROZEN_REVALIDATE_SAME_LEGS",
            }
            for route in ("1h@04:00", "4h@04:00", "1h@05:00", "4h@08:00")
        ]
    )
    forecasts.to_parquet(run / "forecasts.parquet", index=False)
    intents.to_parquet(run / "option-strategy-intents.parquet", index=False)
    publication = SimpleNamespace(
        run_directory=run,
        receipt={"action_date": "2026-09-04"},
        pointer={"current": {"receipt_checksum_sha256": "abc"}},
    )
    monkeypatch.setattr(
        "ml.gameplan_executor.read_current_gameplan", lambda _root: publication
    )

    decision_run = run_gameplan_decision_once(
        tmp_path,
        decision_at=pd.Timestamp("2026-09-04T11:01:00Z"),
    )
    payload = json.loads((decision_run / "decision.json").read_text())
    receipt = json.loads((decision_run / "receipt.json").read_text())

    assert payload["anchor_local"] == "04:00"
    assert payload["forecast_routes_consumed"] == [
        "1h@04:00",
        "1h@05:00",
        "4h@04:00",
        "4h@08:00",
    ]
    assert len(payload["stock_direction_decisions"]) == 4
    assert len(payload["option_strategy_decisions"]) == 4
    assert payload["broker_adapter_loaded"] is False
    assert payload["orders_submitted"] == 0
    assert receipt["decision_checksum_sha256"] == file_checksum(
        decision_run / "decision.json"
    )
    assert receipt["orders_submitted"] == 0


def test_weekend_gap_requires_prior_session_completion_not_action_date(
    tmp_path,
) -> None:
    symbols = ("AAPL", "AMZN", "GOOG", "MU", "NVDA", "SNDK")
    root = (
        tmp_path
        / "market-data"
        / "databento"
        / "opra"
        / "OPRA.PILLAR"
        / "state"
        / "symbol-history"
    )
    for symbol in symbols:
        folder = root / symbol
        folder.mkdir(parents=True)
        for schema in ("ohlcv-1h", "cbbo-1m", "definition"):
            (folder / f"{schema}.json").write_text(
                json.dumps({"completed_through": "2026-09-05"}),
                encoding="utf-8",
            )

    _files, report = _verify_opra_history(
        tmp_path,
        symbols=symbols,
        action_date=date(2026, 9, 7),
        required_completed_through=date(2026, 9, 5),
    )

    assert report["completed_through"] == "2026-09-05"
    assert report["required_completed_through"] == "2026-09-05"


def test_overnight_owner_runs_stages_in_order_and_receipts_report(
    tmp_path, monkeypatch
) -> None:
    calls: list[tuple[str, ...]] = []

    def completed(command, **_kwargs):
        calls.append(tuple(command))
        return 0

    monkeypatch.setattr("ml.overnight_runtime._run_stage", completed)
    run = run_overnight_pipeline(
        tmp_path,
        datastore_argument=("--datastore", str(tmp_path)),
        repository_root=tmp_path,
        reporter=None,
    )
    report_path = run / "stage-report.json"
    report = json.loads(report_path.read_text())
    receipt = json.loads((run / "receipt.json").read_text())

    assert [row["stage"] for row in report["stages"]] == list(STAGE_ORDER)
    assert len(calls) == len(STAGE_ORDER)
    assert report["status"] == "COMPLETE"
    assert receipt["stage_report_checksum_sha256"] == file_checksum(report_path)
    assert receipt["orders_placed"] == 0


def test_scheduled_overnight_accepts_completed_session_and_skips_holiday() -> None:
    completed = scheduled_session_eligibility("2026-09-05T00:05:00Z")
    holiday = scheduled_session_eligibility("2026-09-08T00:05:00Z")

    assert completed["eligible"] is True
    assert completed["local_date"] == "2026-09-04"
    assert holiday["eligible"] is False
    assert holiday["status"] == "NOOP_NON_SESSION_DATE"
    assert holiday["local_date"] == "2026-09-07"
    assert holiday["next_exchange_session"] == "2026-09-08"


def test_scheduled_overnight_fails_closed_before_action_day_close() -> None:
    early = scheduled_session_eligibility("2026-09-04T23:59:00Z")

    assert early["eligible"] is False
    assert early["status"] == "FAIL_ACTION_DAY_NOT_CLOSED"


def test_scheduled_holiday_noop_preserves_pointer_and_runs_no_stage(tmp_path) -> None:
    eligibility = scheduled_session_eligibility("2026-09-08T00:05:00Z")

    run = record_scheduled_noop(tmp_path, eligibility=eligibility)
    report = json.loads((run / "stage-report.json").read_text())
    receipt = json.loads((run / "receipt.json").read_text())

    assert report["status"] == "NOOP_NON_SESSION_DATE"
    assert report["stages"] == []
    assert report["prior_gameplan_pointer_preserved"] is True
    assert receipt["stage_report_checksum_sha256"] == file_checksum(
        run / "stage-report.json"
    )
    assert receipt["orders_placed"] == 0


def test_flat_calibration_cannot_be_promoted_as_a_directional_model(tmp_path, monkeypatch, capsys) -> None:
    from sklearn.dummy import DummyClassifier
    monkeypatch.setattr("ml.nightly_gameplan._estimator", lambda *args: DummyClassifier(strategy="prior"))
    starts = pd.date_range("2026-01-01", periods=120, freq="D", tz="UTC")
    samples = pd.DataFrame({
        "symbol": "AAPL", "model_group": "4h", "route": "4h@08:00",
        "forecast_anchor_local": "08:00", "decision_timestamp": starts,
        "information_available_at": starts, "target_window_start": starts,
        "target_window_end": starts + pd.Timedelta(hours=4),
        "target_semantics": "test", "target": np.arange(120) % 2,
        "mr__x": np.arange(120, dtype=float),
    })
    samples.attrs["target_boundary_quality"] = {"excluded_rows": 7, "enforced": True}
    result = _fit_group_model(samples, current=samples.tail(2), feature_columns=("mr__x",),
        group="4h", model_directory=tmp_path / "models/4h", trained_at=starts[-1])
    gate = result["report"]["promotion_gate"]
    assert gate["status"] == "RESEARCH_NOT_PROMOTED"
    assert gate["checks"]["calibration_retains_directional_information"] is False
    assert gate["checks"]["brier_beats_training_base_rate"] is False
    assert gate["checks"]["log_loss_beats_training_base_rate"] is False
    assert set(result["forecasts"].model_status) == {"RESEARCH_NOT_PROMOTED"}
    assert set(result["forecasts"].calibration_status) == {"FLAT_CALIBRATION"}
    report = result["report"]
    assert report["assessment_raw_scores"]["brier_score"] == 0.25
    assert report["calibration_assessment_change"]["brier_score"] == pytest.approx(1 / 300)
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]["fit"] == "gameplan/4h/target-quality"
    assert events[0]["target_boundary_quality"]["excluded_rows"] == 7
    assert report["target_boundary_quality"] == samples.attrs["target_boundary_quality"]
    warning = next(event for event in events if event.get("fit") == "gameplan/4h/assessment")
    assert "brier_beats_training_base_rate" in warning["failed_checks"]


def test_calibration_diagnostics_distinguish_reversed_and_supported_rankings() -> None:
    from ml.calibration import fit_probability_calibrator
    raw = np.array([0.1, 0.2, 0.8, 0.9])
    for target, expected in [([1, 1, 0, 0], False), ([0, 0, 1, 1], True)]:
        calibrator = fit_probability_calibrator("platt", raw, target, require_nondecreasing=True)
        report = _calibration_signal_diagnostics(calibrator, raw, target, calibrator.predict(raw))
        assert report["information_available"] is expected
        assert report["nondecreasing_constraint_active"] is (not expected)


def test_varying_calibration_still_needs_to_beat_a_baseline(tmp_path, monkeypatch) -> None:
    from sklearn.compose import ColumnTransformer
    from sklearn.pipeline import Pipeline
    from sklearn.tree import DecisionTreeClassifier

    def estimator(*args):
        return Pipeline([
            ("features", ColumnTransformer([("x", "passthrough", ["mr__x"])])),
            ("classifier", DecisionTreeClassifier(max_depth=1, random_state=0)),
        ])

    monkeypatch.setattr("ml.nightly_gameplan._estimator", estimator)
    starts = pd.date_range("2026-01-01", periods=120, freq="D", tz="UTC")
    signal = np.arange(120) % 2
    target = signal.copy()
    target[-15:] = 1 - target[-15:]  # The later assessment regime reverses.
    samples = pd.DataFrame({
        "symbol": "AAPL", "model_group": "4h", "route": "4h@08:00",
        "forecast_anchor_local": "08:00", "decision_timestamp": starts,
        "information_available_at": starts, "target_window_start": starts,
        "target_window_end": starts + pd.Timedelta(hours=4),
        "target_semantics": "test", "target": target, "mr__x": signal.astype(float),
    })
    result = _fit_group_model(samples, current=samples.tail(2), feature_columns=("mr__x",),
        group="4h", model_directory=tmp_path / "models/4h", trained_at=starts[-1])
    checks = result["report"]["promotion_gate"]["checks"]
    assert checks["calibration_retains_directional_information"] is True
    assert checks["brier_beats_training_base_rate"] is False
    assert checks["log_loss_beats_training_base_rate"] is False
    assert set(result["forecasts"].model_status) == {"RESEARCH_NOT_PROMOTED"}


def test_flat_assessment_does_not_claim_the_calibrator_itself_is_constant() -> None:
    from ml.calibration import IdentityCalibrator

    report = _calibration_signal_diagnostics(
        IdentityCalibrator(), [0.2, 0.8], [0, 1], [0.8, 0.8],
    )
    assert report["information_available"] is False
    assert report["status"] == "FLAT_ASSESSMENT"
