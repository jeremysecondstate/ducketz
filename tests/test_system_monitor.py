from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from ml.horizons import INTERNAL_HORIZON_ORDER
from ml.strategy_selection.contracts import (
    BSGP_CALIBRATED_MODEL_SCORE_BASIS,
    SCENARIO_COVERAGE_SCORE_BASIS,
)
from ml.system_monitor import (
    RUNTIMES,
    _log_activity_check,
    _loop_a_cycle_check,
    _overall_status,
    _process_checks,
    scheduled_monitor_mode,
    summarize_directional_quality,
    summarize_strategy_quality,
    summarize_weekly_evidence,
)


def _production_process_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    next_pid = 1000
    for spec in RUNTIMES:
        command = (
            f'"C:\\repo\\.venv\\Scripts\\python.exe" -u -m {spec.module} '
            + " ".join(spec.required_arguments)
        )
        rows.extend(
            (
                {
                    "pid": next_pid,
                    "ppid": 42,
                    "created_at": "2026-08-19T00:00:00Z",
                    "command_line": command,
                },
                {
                    "pid": next_pid + 1,
                    "ppid": next_pid,
                    "created_at": "2026-08-19T00:00:00Z",
                    "command_line": command,
                },
            )
        )
        next_pid += 10
    return rows


def test_process_checks_require_one_launcher_worker_pair_per_runtime() -> None:
    checks = _process_checks(_production_process_rows())

    assert len(checks) == len(RUNTIMES)
    assert {check["status"] for check in checks} == {"PASS"}


def test_process_checks_fail_a_missing_worker_without_affecting_other_owners() -> None:
    rows = _production_process_rows()
    rows = [
        row
        for row in rows
        if not (
            "-m ml.strategy_runtime " in str(row["command_line"])
            and int(row["ppid"]) != 42
        )
    ]

    checks = _process_checks(rows)
    by_name = {str(check["name"]): check for check in checks}

    assert by_name["process.strategy"]["status"] == "FAIL"
    assert by_name["process.loop_a"]["status"] == "PASS"


def test_loop_a_check_accepts_a_bounded_writing_cycle_and_last_complete_authority(
    tmp_path: Path,
) -> None:
    now = pd.Timestamp("2026-08-19T14:42:00Z")
    symbols = ("AAPL", "AMZN")
    complete = {
        "schema_version": "loop-a-cycle-v1",
        "status": "COMPLETE",
        "generation": "complete-generation",
        "started_at": "2026-08-19T14:25:00Z",
        "finished_at": "2026-08-19T14:35:00Z",
        "failure_count": 0,
        "symbols": list(symbols),
        "providers": ["databento"],
    }
    writing = {
        "schema_version": "loop-a-cycle-v1",
        "status": "WRITING",
        "generation": "active-generation",
        "started_at": "2026-08-19T14:40:00Z",
        "failure_count": 0,
        "symbols": list(symbols),
        "providers": ["databento"],
    }
    (tmp_path / ".ducketz-loop-a-complete.json").write_text(
        json.dumps(complete), encoding="utf-8"
    )
    (tmp_path / ".ducketz-loop-a-cycle.json").write_text(
        json.dumps(writing), encoding="utf-8"
    )

    check = _loop_a_cycle_check(tmp_path, now, symbols)

    assert check["status"] == "PASS"
    assert check["details"]["active_cycle_status"] == "WRITING"
    assert check["details"]["last_complete_generation"] == "complete-generation"


def _directional_frames(*, warning: bool) -> tuple[pd.DataFrame, pd.DataFrame]:
    monitoring_rows: list[dict[str, object]] = []
    for horizon in INTERNAL_HORIZON_ORDER:
        monitoring_rows.append(
            {
                "category": "performance",
                "metric_name": "calibration_gap",
                "scope_type": "horizon",
                "scope_value": horizon,
                "status": "WARNING" if warning and horizon == "1w" else "OK",
                "observed_value": 0.08 if warning and horizon == "1w" else 0.02,
                "reference_value": 0.05,
                "evidence_row_count": 100,
            }
        )
        monitoring_rows.append(
            {
                "category": "live_evidence",
                "metric_name": "completed_live_forecasts",
                "scope_type": "symbol_horizon",
                "scope_value": f"AAPL|{horizon}",
                "status": "NO_COMPLETED_DECISIONS",
                "observed_value": 0,
                "reference_value": 30,
                "evidence_row_count": 0,
            }
        )
    evaluations = pd.DataFrame(
        {"horizon": list(INTERNAL_HORIZON_ORDER), "evaluation_status": "EVALUATED"}
    )
    return pd.DataFrame(monitoring_rows), evaluations


def test_directional_quality_separates_offline_warning_from_missing_live_labels() -> None:
    monitoring, evaluations = _directional_frames(warning=True)

    summary = summarize_directional_quality(monitoring, evaluations)

    assert summary["status"] == "WARN"
    assert summary["quality_warnings"] == [
        {
            "horizon": "1w",
            "metric": "calibration_gap",
            "status": "WARNING",
            "observed": 0.08,
            "reference": 0.05,
            "evidence_rows": 100,
        }
    ]
    assert summary["live_evidence"]["interpretation"] == "INSUFFICIENT_LIVE_LABELS"


def test_directional_quality_passes_when_published_references_pass() -> None:
    monitoring, evaluations = _directional_frames(warning=False)

    summary = summarize_directional_quality(monitoring, evaluations)

    assert summary["status"] == "PASS"
    assert summary["missing_horizons"] == []


def _strategy_candidates(*, calibrated: bool) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["AAPL"] * len(INTERNAL_HORIZON_ORDER),
            "horizon": list(INTERNAL_HORIZON_ORDER),
            "score_basis": [
                (
                    BSGP_CALIBRATED_MODEL_SCORE_BASIS
                    if calibrated
                    else SCENARIO_COVERAGE_SCORE_BASIS
                )
            ]
            * len(INTERNAL_HORIZON_ORDER),
            "pricing_status": ["Active" if calibrated else "Delayed"]
            * len(INTERNAL_HORIZON_ORDER),
            "pricing_source": ["BSGP" if calibrated else "UNAVAILABLE"]
            * len(INTERNAL_HORIZON_ORDER),
            "pricing_leg_coverage": [1.0 if calibrated else 0.0]
            * len(INTERNAL_HORIZON_ORDER),
            "surface_quality_pass": [calibrated] * len(INTERNAL_HORIZON_ORDER),
            "liquidity_policy_pass": [calibrated] * len(INTERNAL_HORIZON_ORDER),
            "all_option_quotes_valid": [calibrated] * len(INTERNAL_HORIZON_ORDER),
        }
    )


def _strategy_reports(*, calibrated: bool) -> dict[str, object]:
    return {
        "model_reports": {
            horizon: {
                "status": "MODEL_FIT" if calibrated else "MODEL_NOT_FIT",
                "complete_outcome_rows": 100 if calibrated else 0,
            }
            for horizon in INTERNAL_HORIZON_ORDER
        }
    }


def test_strategy_quality_calls_heuristic_output_insufficient_not_calibrated() -> None:
    summary = summarize_strategy_quality(
        _strategy_candidates(calibrated=False),
        _strategy_reports(calibrated=False),
    )

    assert summary["status"] == "WARN"
    assert summary["calibrated_candidate_rows"] == 0
    assert summary["model_evidence_interpretation"] == (
        "INSUFFICIENT_OBSERVED_OPTION_OUTCOMES"
    )


def test_strategy_quality_passes_calibrated_fully_priced_quality_rows() -> None:
    summary = summarize_strategy_quality(
        _strategy_candidates(calibrated=True),
        _strategy_reports(calibrated=True),
    )

    assert summary["status"] == "PASS"
    assert summary["fully_priced_rows"] == len(INTERNAL_HORIZON_ORDER)
    assert summary["quality_passing_rows"] == len(INTERNAL_HORIZON_ORDER)


def test_overall_status_ignores_info_but_escalates_warning_and_failure() -> None:
    assert _overall_status([{"status": "PASS"}, {"status": "INFO"}]) == "HEALTHY"
    assert _overall_status([{"status": "WARN"}]) == "DEGRADED"
    assert _overall_status([{"status": "WARN"}, {"status": "FAIL"}]) == "UNHEALTHY"


def test_scheduled_mode_uses_xnys_post_close_daily_and_final_session_weekly() -> None:
    pacific = ZoneInfo("America/Los_Angeles")

    assert scheduled_monitor_mode(datetime(2026, 8, 19, 14, 42, tzinfo=pacific)) == "daily"
    assert scheduled_monitor_mode(datetime(2026, 8, 21, 14, 42, tzinfo=pacific)) == "weekly"
    assert scheduled_monitor_mode(datetime(2026, 8, 19, 13, 42, tzinfo=pacific)) == "hourly"
    assert scheduled_monitor_mode(datetime(2026, 8, 22, 14, 42, tzinfo=pacific)) == "hourly"


def test_scheduled_weekly_mode_uses_holiday_week_final_session_not_friday() -> None:
    pacific = ZoneInfo("America/Los_Angeles")

    assert scheduled_monitor_mode(datetime(2026, 4, 1, 14, 42, tzinfo=pacific)) == "daily"
    assert scheduled_monitor_mode(datetime(2026, 4, 2, 14, 42, tzinfo=pacific)) == "weekly"
    assert scheduled_monitor_mode(datetime(2026, 4, 3, 14, 42, tzinfo=pacific)) == "hourly"


def test_log_discovery_prefers_current_legacy_runtime_log_over_old_primary(
    tmp_path: Path,
) -> None:
    now = pd.Timestamp("2026-08-19T18:42:00Z")
    primary = tmp_path / "logs" / "ducketz"
    legacy = tmp_path / "runtime-logs"
    primary.mkdir(parents=True)
    legacy.mkdir(parents=True)
    old_time = pd.Timestamp("2026-08-19T15:00:00Z").timestamp()
    current_time = pd.Timestamp("2026-08-19T18:40:00Z").timestamp()
    for spec in RUNTIMES:
        stdout = primary / f"{spec.log_aliases[0]}.stdout.log"
        stderr = primary / f"{spec.log_aliases[0]}.stderr.log"
        stdout.write_text("current\n", encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        os.utime(stdout, (current_time, current_time))
        os.utime(stderr, (current_time, current_time))
    cme_primary = primary / "cme-l2.stdout.log"
    os.utime(cme_primary, (old_time, old_time))
    legacy_stdout = legacy / "20260819T183900Z-cme.out.log"
    legacy_stderr = legacy / "20260819T183900Z-cme.err.log"
    legacy_stdout.write_text("live\n", encoding="utf-8")
    legacy_stderr.write_text("", encoding="utf-8")
    os.utime(legacy_stdout, (current_time, current_time))
    os.utime(legacy_stderr, (current_time, current_time))

    check = _log_activity_check(tmp_path, now=now, market_actionable=True)

    assert check["status"] == "PASS"
    cme = check["details"]["runtimes"]["cme"]
    assert cme["stdout"] == str(legacy_stdout)
    assert cme["stderr"] == str(legacy_stderr)
    assert cme["log_authority"] == "LEGACY_RUNTIME_LOGS"


def _weekly_rows(
    *,
    week_start: str,
    count: int,
    accuracy: float,
    definition: str = "definition-v1",
) -> list[dict[str, object]]:
    start = pd.Timestamp(week_start, tz="UTC") + pd.Timedelta(hours=14)
    correct_count = round(count * accuracy)
    rows: list[dict[str, object]] = []
    for index in range(count):
        decision = start + pd.Timedelta(minutes=index)
        correct = index < correct_count
        rows.append(
            {
                "symbol": f"S{index % 6}",
                "provider": "databento",
                "horizon": "1h",
                "decision_timestamp": decision,
                "target_window_start": decision + pd.Timedelta(hours=1),
                "target_window_end": decision + pd.Timedelta(hours=2),
                "prediction_created_at": decision + pd.Timedelta(minutes=5),
                "model_name": "logistic",
                "model_version": f"model-{week_start}",
                "prediction_mode": "LIVE",
                "evaluation_status": "EVALUATED",
                "target_definition_version": definition,
                "target_specification": "same-target-contract",
                "assumed_round_trip_cost": 0.001,
                "observed_target": int(correct),
                "calibrated_probability": 0.6 if correct else 0.4,
                "log_loss": 0.5 if correct else 0.8,
                "brier_score": 0.16 if correct else 0.36,
                "prediction_correct_0_5": correct,
            }
        )
    return rows


def test_weekly_rollup_compares_only_sufficient_compatible_live_evidence() -> None:
    evaluations = pd.DataFrame(
        _weekly_rows(week_start="2026-08-03", count=30, accuracy=0.4)
        + _weekly_rows(week_start="2026-08-10", count=30, accuracy=0.6)
    )

    summary = summarize_weekly_evidence(
        evaluations,
        previous_week_start="2026-08-03",
        current_week_start="2026-08-10",
    )

    assert summary["status"] == "PASS"
    assert summary["comparable_horizons"] == ["1h"]
    route = summary["routes"]["1h"]
    assert route["evidence_state"] == "COMPARABLE_WEEKLY_EVIDENCE"
    assert route["comparison"]["accuracy_at_0_5_delta"] == 0.2
    assert summary["routes"]["1w"]["evidence_state"] == (
        "INSUFFICIENT_WEEKLY_EVIDENCE"
    )


def test_weekly_rollup_refuses_incompatible_definitions() -> None:
    evaluations = pd.DataFrame(
        _weekly_rows(week_start="2026-08-03", count=30, accuracy=0.4)
        + _weekly_rows(
            week_start="2026-08-10",
            count=30,
            accuracy=0.6,
            definition="definition-v2",
        )
    )

    summary = summarize_weekly_evidence(
        evaluations,
        previous_week_start="2026-08-03",
        current_week_start="2026-08-10",
    )

    assert summary["status"] == "INFO"
    assert summary["evidence_state"] == "INSUFFICIENT_WEEKLY_EVIDENCE"
    assert summary["routes"]["1h"]["evidence_state"] == (
        "INCOMPATIBLE_WEEKLY_DEFINITIONS"
    )
    assert summary["routes"]["1h"]["comparison"] is None


def test_weekly_rollup_labels_immature_outcomes_instead_of_making_a_trend() -> None:
    evaluations = pd.DataFrame(
        _weekly_rows(week_start="2026-08-03", count=6, accuracy=0.5)
        + _weekly_rows(week_start="2026-08-10", count=6, accuracy=0.5)
    )

    summary = summarize_weekly_evidence(
        evaluations,
        previous_week_start="2026-08-03",
        current_week_start="2026-08-10",
    )

    assert summary["status"] == "INFO"
    assert summary["evidence_state"] == "INSUFFICIENT_WEEKLY_EVIDENCE"
    assert summary["routes"]["1h"]["comparison"] is None
