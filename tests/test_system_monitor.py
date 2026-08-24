from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd

import ml.system_monitor as system_monitor
import ml.strategy_value_challenger as value_challenger
from ml.current_publication import CurrentPublication
from ml.horizons import INTERNAL_HORIZON_ORDER
from ml.strategy_publication import StrategyPublication
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


def test_monitor_pins_each_current_publication_once_across_checks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    loop_b_a = CurrentPublication(
        run_directory=tmp_path / "ml" / "runs" / "loop-b-a",
        manifest={"run_timestamp": "2026-08-20T21:00:00Z"},
        receipt={},
        pointer={},
    )
    loop_b_b = CurrentPublication(
        run_directory=tmp_path / "ml" / "runs" / "loop-b-b",
        manifest={"run_timestamp": "2026-08-20T21:01:00Z"},
        receipt={},
        pointer={},
    )
    strategy_a = StrategyPublication(
        run_directory=tmp_path / "ml" / "strategy-runs" / "strategy-a",
        manifest={"run_timestamp": "2026-08-20T21:02:00Z"},
        receipt={},
        pointer={},
    )
    strategy_b = StrategyPublication(
        run_directory=tmp_path / "ml" / "strategy-runs" / "strategy-b",
        manifest={"run_timestamp": "2026-08-20T21:03:00Z"},
        receipt={},
        pointer={},
    )
    reads = {"loop_b": 0, "strategy": 0}

    def read_loop_b(_root: Path) -> CurrentPublication:
        reads["loop_b"] += 1
        return loop_b_a if reads["loop_b"] == 1 else loop_b_b

    def read_strategy(_root: Path) -> StrategyPublication:
        reads["strategy"] += 1
        return strategy_a if reads["strategy"] == 1 else strategy_b

    def ok(name: str) -> dict[str, object]:
        return {"name": name, "status": "PASS", "summary": "ok", "details": {}}

    monkeypatch.setattr(system_monitor, "read_current_publication", read_loop_b)
    monkeypatch.setattr(
        system_monitor,
        "read_current_strategy_publication",
        read_strategy,
    )
    monkeypatch.setattr(system_monitor, "_process_checks", lambda _rows: [])
    for name in (
        "_lock_check",
        "_log_activity_check",
        "_loop_a_cycle_check",
        "_bar_readiness_check",
        "_cme_snapshot_check",
        "_alfred_pointer_check",
        "_options_check",
        "_pricing_check",
        "_strategy_value_shadow_check",
        "_storage_check",
        "_alfred_full_check",
    ):
        monkeypatch.setattr(
            system_monitor,
            name,
            lambda *_args, _name=name, **_kwargs: ok(_name),
        )

    observed: dict[str, object] = {}

    def loop_b_check(*_args, publication, **_kwargs):
        observed["loop_b_check"] = publication
        return ok("loop_b_publication")

    def strategy_check(*_args, publication, **_kwargs):
        observed["strategy_check"] = publication
        return ok("strategy_publication")

    def strategy_value_check(*_args, publication, **_kwargs):
        observed["strategy_values"] = publication
        return ok("strategy_candidate_value_sanity")

    def lineage_check(*_args, loop_b, strategy, **_kwargs):
        observed["lineage"] = (loop_b, strategy)
        return ok("cross_loop_lineage")

    def ui_check(*_args, loop_b, strategy_publication, **_kwargs):
        observed["ui"] = (loop_b, strategy_publication)
        return ok("ui_contracts")

    def directional_check(*_args, publication, **_kwargs):
        observed["directional"] = publication
        return ok("directional_prediction_quality")

    def strategy_quality_check(*_args, publication, **_kwargs):
        observed["strategy_quality"] = publication
        return ok("strategy_prediction_quality")

    def canary_check(*_args, strategy_publication, **_kwargs):
        observed["canary"] = strategy_publication
        return ok("pricing_strategy_canary")

    monkeypatch.setattr(system_monitor, "_loop_b_check", loop_b_check)
    monkeypatch.setattr(system_monitor, "_strategy_check", strategy_check)
    monkeypatch.setattr(
        system_monitor,
        "_strategy_candidate_value_check",
        strategy_value_check,
    )
    monkeypatch.setattr(system_monitor, "_lineage_check", lineage_check)
    monkeypatch.setattr(system_monitor, "_ui_check", ui_check)
    monkeypatch.setattr(
        system_monitor,
        "_directional_quality_check",
        directional_check,
    )
    monkeypatch.setattr(
        system_monitor,
        "_strategy_quality_check",
        strategy_quality_check,
    )
    monkeypatch.setattr(
        system_monitor,
        "_pricing_strategy_canary_check",
        canary_check,
    )

    report = system_monitor.build_monitor_report(
        tmp_path,
        mode="daily",
        observed_at="2026-08-20T22:00:00Z",
        process_rows=(),
        symbols=("AAPL",),
    )

    assert reads == {"loop_b": 1, "strategy": 1}
    assert observed == {
        "loop_b_check": loop_b_a,
        "strategy_check": strategy_a,
        "strategy_values": strategy_a,
        "lineage": (loop_b_a, strategy_a),
        "ui": (loop_b_a, strategy_a),
        "directional": loop_b_a,
        "strategy_quality": strategy_a,
        "canary": strategy_a,
    }
    assert report["publication_generations"] == {
        "capture_policy": "READ_EACH_CURRENT_POINTER_ONCE",
        "loop_b": {
            "status": "PINNED",
            "run_path": "ml/runs/loop-b-a",
            "run_timestamp": "2026-08-20T21:00:00+00:00",
        },
        "strategy": {
            "status": "PINNED",
            "run_path": "ml/strategy-runs/strategy-a",
            "run_timestamp": "2026-08-20T21:02:00+00:00",
        },
    }


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


def test_directional_uncertainty_is_clustered_and_deterministic() -> None:
    evaluations = pd.DataFrame(
        {
            "horizon": ["1h"] * 4,
            "evaluation_status": ["EVALUATED"] * 4,
            "target_window_start": pd.to_datetime(
                [
                    "2026-08-18T15:00:00Z",
                    "2026-08-18T15:00:00Z",
                    "2026-08-18T16:00:00Z",
                    "2026-08-18T16:00:00Z",
                ],
                utc=True,
            ),
            "observed_target": [0, 1, 0, 1],
            "calibrated_probability": [0.2, 0.8, 0.4, 0.6],
            "prediction_correct_0_5": [True, True, True, True],
            "brier_score": [0.04, 0.04, 0.16, 0.16],
            "log_loss": [0.22, 0.22, 0.51, 0.51],
        }
    )

    first = system_monitor._directional_uncertainty_by_horizon(
        evaluations,
        bootstrap_replicates=50,
    )
    second = system_monitor._directional_uncertainty_by_horizon(
        evaluations,
        bootstrap_replicates=50,
    )

    assert first == second
    assert first["1h"]["status"] == "AVAILABLE"
    assert first["1h"]["independent_target_clusters"] == 2
    assert first["1h"]["evaluated_rows"] == 4
    accuracy = first["1h"]["intervals"]["accuracy_at_0_5"]
    assert accuracy["point"] == 1.0
    assert accuracy["successful_replicates"] == 50


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
                "pricing_eligible_outcome_rows": 100 if calibrated else 0,
                "pricing_excluded_outcome_rows": 0,
                "usable_decision_clusters": 100 if calibrated else 0,
                "required_decision_clusters": 80,
                "pricing_exclusion_reason_counts": {},
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
    assert summary["model_evidence_interpretation"] == (
        "PRICING_ELIGIBLE_OBSERVED_OUTCOMES_AVAILABLE"
    )


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
    # A newer trainer log must not be mistaken for the shorter `strategy`
    # runtime stem.
    trainer_stdout = primary / "strategy-profit-training.stdout.log"
    trainer_stderr = primary / "strategy-profit-training.stderr.log"
    os.utime(trainer_stdout, (current_time + 30, current_time + 30))
    os.utime(trainer_stderr, (current_time + 30, current_time + 30))

    check = _log_activity_check(tmp_path, now=now, market_actionable=True)

    assert check["status"] == "PASS"
    cme = check["details"]["runtimes"]["cme"]
    assert cme["stdout"] == str(legacy_stdout)
    assert cme["stderr"] == str(legacy_stderr)
    assert cme["log_authority"] == "LEGACY_RUNTIME_LOGS"
    assert check["details"]["runtimes"]["strategy"]["stdout"] == str(
        primary / "strategy.stdout.log"
    )
    assert check["details"]["runtimes"]["strategy_profit_training"][
        "stdout"
    ] == str(trainer_stdout)


def _strategy_value_frame() -> pd.DataFrame:
    expected_returns = [-0.1, 0.05, 0.1, 0.2, 0.3]
    capital = [100.0] * len(expected_returns)
    return pd.DataFrame(
        {
            "symbol": ["AAPL"] * len(expected_returns),
            "horizon": ["1d"] * len(expected_returns),
            "candidate_key": [f"candidate-{index}" for index in range(5)],
            "candidate_rank": [1, 2, 3, 4, 5],
            "strategy_name": ["long_call"] * len(expected_returns),
            "strategy_display_name": ["Long Call"] * len(expected_returns),
            "model_status": ["MODEL_FIT"] * len(expected_returns),
            "scenario_coverage_score": [0.45, 0.48, 0.5, 0.52, 0.55],
            "calibrated_profit_probability": [0.4, 0.45, 0.5, 0.55, 0.6],
            "expected_net_profit": [
                value * required for value, required in zip(expected_returns, capital)
            ],
            "expected_return_on_risk": expected_returns,
            "capital_required": capital,
            "max_profit": [float("nan")] * len(expected_returns),
            "max_loss": capital,
        }
    )


def test_strategy_candidate_value_sanity_passes_coherent_values() -> None:
    summary = system_monitor.summarize_strategy_candidate_values(
        _strategy_value_frame()
    )

    assert summary["status"] == "PASS"
    assert summary["integrity_failure_rows"] == 0
    assert summary["alert_rows"] == 0
    assert summary["formula_max_absolute_error"] == 0.0
    assert summary["automated_action"] == (
        "REPORT_ONLY_NO_MODEL_OR_CANDIDATE_MUTATION"
    )


def test_strategy_candidate_value_sanity_flags_screenshot_style_tail_profile() -> None:
    frame = _strategy_value_frame()
    frame.loc[0, "calibrated_profit_probability"] = 0.01954728995211606
    frame.loc[0, "scenario_coverage_score"] = 0.636602272261187
    frame.loc[0, "expected_return_on_risk"] = 5.269765070487699
    frame.loc[0, "expected_net_profit"] = 526.9765070487699

    summary = system_monitor.summarize_strategy_candidate_values(frame)

    assert summary["status"] == "WARN"
    assert summary["integrity_failure_rows"] == 0
    assert summary["alert_rows"] == 1
    assert summary["alert_counts"] == {
        "positive_return_with_zero_probability": 0,
        "tail_payoff_dependency": 1,
        "high_return_low_probability": 1,
        "extreme_expected_return": 1,
        "route_return_outlier": 1,
    }
    finding = summary["top_findings"][0]
    assert finding["ml_profit_probability_percent"] == 1.9547
    assert finding["expected_return_percent"] == 526.977
    assert finding["implied_profitable_return_floor_x"] == 269.591
    assert finding["rules"] == [
        "tail_payoff_dependency",
        "high_return_low_probability",
        "extreme_expected_return",
        "route_return_outlier",
    ]


def test_strategy_candidate_value_sanity_fails_formula_corruption() -> None:
    frame = _strategy_value_frame()
    frame.loc[2, "expected_net_profit"] = 999.0

    summary = system_monitor.summarize_strategy_candidate_values(frame)

    assert summary["status"] == "FAIL"
    assert summary["integrity_failure_rows"] == 1
    assert summary["integrity_failure_counts"][
        "expected_return_formula_mismatch"
    ] == 1


def test_strategy_candidate_value_sanity_fails_closed_on_missing_fields() -> None:
    summary = system_monitor.summarize_strategy_candidate_values(
        _strategy_value_frame().drop(columns=["capital_required"])
    )

    assert summary["status"] == "FAIL"
    assert summary["missing_columns"] == ["capital_required"]


def test_strategy_value_shadow_monitor_verifies_current_shadow_receipt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run = tmp_path / "ml" / "strategy-value-challenger-runs" / "run"
    receipt = run / "receipt.json"
    shadow = SimpleNamespace(
        directory=run,
        receipt_path=receipt,
        report={
            "created_at": "2026-08-24T17:00:00Z",
            "source_fingerprint_sha256": "d" * 64,
            "decision": "BLOCKED_KEEP_CURRENT_AUTHORITY",
            "promotion_eligible": False,
            "horizons": {},
        },
    )
    publication = StrategyPublication(
        run_directory=tmp_path / "ml" / "strategy-runs" / "current",
        manifest={},
        receipt={},
        pointer={},
    )
    monkeypatch.setattr(
        value_challenger,
        "strategy_value_source_fingerprint",
        lambda *_args, **_kwargs: "d" * 64,
    )
    monkeypatch.setattr(
        value_challenger,
        "read_current_strategy_value_challenger",
        lambda _root: shadow,
    )

    check = system_monitor._strategy_value_shadow_check(
        tmp_path,
        now=pd.Timestamp("2026-08-24T17:30:00Z"),
        publication=publication,
    )

    assert check["status"] == "PASS"
    assert check["details"]["source_fingerprint_current"] is True
    assert check["details"]["production_authority"] is False
    assert check["details"]["orders_placed"] == 0


def test_strategy_value_shadow_monitor_warns_when_scheduler_is_behind(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run = tmp_path / "ml" / "strategy-value-challenger-runs" / "run"
    shadow = SimpleNamespace(
        directory=run,
        receipt_path=run / "receipt.json",
        report={
            "created_at": "2026-08-24T15:00:00Z",
            "source_fingerprint_sha256": "e" * 64,
            "decision": "BLOCKED_KEEP_CURRENT_AUTHORITY",
            "promotion_eligible": False,
            "horizons": {},
        },
    )
    publication = StrategyPublication(
        run_directory=tmp_path / "ml" / "strategy-runs" / "current",
        manifest={},
        receipt={},
        pointer={},
    )
    monkeypatch.setattr(
        value_challenger,
        "strategy_value_source_fingerprint",
        lambda *_args, **_kwargs: "f" * 64,
    )
    monkeypatch.setattr(
        value_challenger,
        "read_current_strategy_value_challenger",
        lambda _root: shadow,
    )

    check = system_monitor._strategy_value_shadow_check(
        tmp_path,
        now=pd.Timestamp("2026-08-24T17:00:00Z"),
        publication=publication,
    )

    assert check["status"] == "WARN"
    assert check["details"]["source_fingerprint_current"] is False


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
