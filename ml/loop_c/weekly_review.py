from __future__ import annotations

import argparse
import json
import math
import os
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Mapping, Sequence

import exchange_calendars as xcals
import pandas as pd

from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.artifacts import (
    create_timestamp_directory,
    file_checksum,
    semantic_metadata_fingerprint,
    utc_timestamp,
    verify_manifest,
    write_manifest,
)
from ml.loop_c.publication import LOOP_C_PUBLICATION_VERSION
from ml.loop_c.risk_proposal import build_pending_risk_proposal
from ml.loop_c.schwab_snapshot import (
    LOOP_C_SCHWAB_RECEIPT_SCHEMA_VERSION,
    capture_schwab_read_only_state,
)
from ml.option_pricing.strategy_outcomes import (
    read_current_strategy_outcome_evidence,
)
from ml.strategy_publication import STRATEGY_PUBLICATION_VERSION


LOOP_C_WEEKLY_REVIEW_SCHEMA_VERSION = "loop-c-weekly-operator-review-v1"
LOOP_C_WEEKLY_REVIEW_RECEIPT_VERSION = "loop-c-weekly-operator-review-receipt-v1"
LOOP_C_WEEKLY_REVIEW_POINTER_VERSION = "loop-c-weekly-operator-review-pointer-v1"


@dataclass(frozen=True)
class WeeklyReviewWindow:
    calendar_name: str
    first_session: date
    last_session: date
    calendar_week_start: date
    session_open: pd.Timestamp
    session_close: pd.Timestamp


@dataclass(frozen=True)
class LoopCWeeklyReviewResult:
    run_directory: Path
    report_path: Path
    markdown_path: Path
    receipt_path: Path
    status: str


def resolve_weekly_review_window(
    reviewed_at: object,
    *,
    week_ending: date | None = None,
) -> WeeklyReviewWindow:
    """Resolve the most recent completed XNYS calendar week without guessing."""

    cutoff = utc_timestamp(reviewed_at)
    local_cutoff = cutoff.tz_convert("America/New_York")
    search_end = week_ending or local_cutoff.date()
    search_start = search_end - timedelta(days=21)
    calendar = xcals.get_calendar("XNYS", start=search_start, end=search_end)
    sessions = calendar.sessions
    completed = [
        session
        for session in sessions
        if calendar.session_close(session) <= cutoff
        and (week_ending is None or session.date() <= week_ending)
    ]
    if not completed:
        raise ValueError("No completed XNYS session is available for weekly review")
    anchor = completed[-1].date()
    monday = anchor - timedelta(days=anchor.weekday())
    sunday = monday + timedelta(days=6)
    weekly_sessions = [
        session
        for session in sessions
        if monday <= session.date() <= sunday
        if calendar.session_close(session) <= cutoff
        and (week_ending is None or session.date() <= week_ending)
    ]
    if not weekly_sessions:
        raise ValueError("The requested week has no completed XNYS sessions")
    first = weekly_sessions[0]
    last = weekly_sessions[-1]
    return WeeklyReviewWindow(
        calendar_name="XNYS",
        first_session=first.date(),
        last_session=last.date(),
        calendar_week_start=monday,
        session_open=utc_timestamp(calendar.session_open(first)),
        session_close=utc_timestamp(calendar.session_close(last)),
    )


def build_loop_c_weekly_review(
    datastore_root: Path,
    *,
    reviewed_at: object | None = None,
    week_ending: date | None = None,
    schwab_run_directory: Path | None = None,
    pending_risk_proposal: Mapping[str, object] | None = None,
) -> LoopCWeeklyReviewResult:
    """Publish one review-only weekly artifact from receipt-verified evidence."""

    root = Path(datastore_root).resolve()
    timestamp = utc_timestamp(reviewed_at)
    window = resolve_weekly_review_window(timestamp, week_ending=week_ending)
    schwab_run = Path(
        schwab_run_directory or _current_schwab_run(root)
    ).resolve()
    schwab = _verify_schwab_run(
        root,
        schwab_run,
        reviewed_at=timestamp,
        required_review_start=window.calendar_week_start,
        required_review_end=window.last_session,
    )
    decisions, decision_sources = _load_loop_c_decisions(
        root,
        start=window.session_open,
        end=window.session_close,
    )
    outcome_frame, outcome_sources, outcome_status = _load_strategy_outcomes(
        root,
        reviewed_at=timestamp,
    )
    shadow, shadow_sources = _summarize_shadow_performance(
        root,
        decisions,
        outcome_frame=outcome_frame,
        outcome_evidence_status=outcome_status,
    )
    equity_bridge, equity_sources = _equity_bridge(
        root,
        start=window.session_open,
        last_session_close=window.session_close,
        end=timestamp,
    )
    cohorts = _cohort_summary(decisions)
    account_period = schwab["history"]["review_period"]
    actual_performance = dict(account_period.get("performance", {}))
    status = _review_status(decisions, shadow, cohorts)
    proposal_summary, proposal_sources = _proposal_summary(
        root,
        pending_risk_proposal,
    )
    report: dict[str, object] = {
        "schema_version": LOOP_C_WEEKLY_REVIEW_SCHEMA_VERSION,
        "status": status,
        "reviewed_at": timestamp.isoformat(),
        "window": {
            "calendar": window.calendar_name,
            "calendar_week_start": window.calendar_week_start.isoformat(),
            "first_completed_session": window.first_session.isoformat(),
            "last_completed_session": window.last_session.isoformat(),
            "session_window_open": window.session_open.isoformat(),
            "session_window_close": window.session_close.isoformat(),
        },
        "actual_account_context": {
            "attribution": "ACCOUNT_OPTIONS_CONTEXT_NOT_LOOP_C_ATTRIBUTED",
            "performance_scope": "RECONSTRUCTABLE_CLOSED_OPTION_POSITIONS",
            "performance": actual_performance,
            "per_underlying": account_period.get("per_underlying", []),
            "per_strategy": account_period.get("per_strategy", []),
            "equity_bridge": equity_bridge,
            "usable_to_claim_loop_c_effectiveness": False,
            "limitations": account_period.get("limitations", []),
        },
        "loop_c": {
            "authority": "OBSERVE_ONLY",
            "actual_broker_performance": {
                "status": "NOT_APPLICABLE_OBSERVE_ONLY",
                "attributed_trade_count": 0,
                "attributed_realized_pnl": 0.0,
                "reason": "Loop C has no broker submission path and every verified run placed zero orders.",
            },
            "shadow_counterfactual_performance": shadow,
            "decision_summary": _decision_summary(decisions),
            "model_and_risk_cohorts": cohorts,
        },
        "pending_risk_proposal": proposal_summary,
        "operator_review": {
            "automatic_change_allowed": False,
            "scheduler_self_approval_allowed": False,
            "threshold_change_allowed_from_this_report_alone": False,
            "risk_increase_allowed_from_this_report_alone": False,
            "model_retraining_allowed_from_this_report": False,
            "decision": "HOLD_CURRENT_VALUES_PENDING_OPERATOR_DISCUSSION",
            "discussion_sequence": [
                "Separate actual account results from Loop C counterfactual results.",
                "Review deterministic risk-limit utilization and any halt or reconciliation events.",
                "Review only causally mature, receipt-matched Loop C outcomes by horizon.",
                "If a change is worth testing, preregister one threshold or risk hypothesis against a frozen baseline.",
                "Issue a new explicit time-bounded approval only after the operator accepts exact values.",
            ],
        },
        "safety": {
            "authority": "REVIEW_ONLY",
            "broker_data_http_methods": ["GET"],
            "model_or_threshold_mutation_performed": False,
            "risk_or_halt_control_mutation_performed": False,
            "broker_mutation_performed": False,
            "orders_enabled": False,
            "orders_placed": 0,
        },
    }
    run = create_timestamp_directory(
        root / "ml" / "loop-c-weekly-reviews",
        timestamp=timestamp,
    )
    report_path = run / "report.json"
    markdown_path = run / "review.md"
    _write_json_atomic(report_path, report)
    markdown_path.write_text(_render_markdown(report), encoding="utf-8")
    input_files = tuple(
        dict.fromkeys(
            (
                schwab["receipt_path"],
                schwab["facts_path"],
                schwab["history_path"],
                *decision_sources,
                *outcome_sources,
                *shadow_sources,
                *equity_sources,
                *proposal_sources,
            )
        )
    )
    manifest_path = write_manifest(
        run,
        run_timestamp=timestamp,
        input_files=input_files,
        output_files=(report_path.name, markdown_path.name),
        configuration={
            "authority": "REVIEW_ONLY",
            "window": report["window"],
            "automatic_change_allowed": False,
            "orders_enabled": False,
            "orders_placed": 0,
        },
        datastore_root=root,
    )
    receipt = {
        "schema_version": LOOP_C_WEEKLY_REVIEW_RECEIPT_VERSION,
        "authority": "REVIEW_ONLY",
        "run_path": run.relative_to(root).as_posix(),
        "reviewed_at": timestamp.isoformat(),
        "status": status,
        "manifest_sha256": file_checksum(manifest_path),
        "report_sha256": file_checksum(report_path),
        "markdown_sha256": file_checksum(markdown_path),
        "safety": report["safety"],
    }
    receipt_path = run / "receipt.json"
    _write_json_atomic(receipt_path, receipt)
    pointer = {
        "schema_version": LOOP_C_WEEKLY_REVIEW_POINTER_VERSION,
        "current": {
            "run_path": receipt["run_path"],
            "reviewed_at": receipt["reviewed_at"],
            "status": status,
            "receipt_sha256": file_checksum(receipt_path),
        },
    }
    _write_json_atomic(
        root / "ml" / "loop-c-weekly-review-latest" / "review.json",
        pointer,
    )
    return read_current_loop_c_weekly_review(root)


def read_current_loop_c_weekly_review(
    datastore_root: Path,
) -> LoopCWeeklyReviewResult:
    """Read the latest weekly review only after pointer and receipt verification."""

    root = Path(datastore_root).resolve()
    pointer_path = root / "ml" / "loop-c-weekly-review-latest" / "review.json"
    pointer = _read_object(pointer_path, "Loop C weekly review pointer")
    current = pointer.get("current")
    if (
        pointer.get("schema_version") != LOOP_C_WEEKLY_REVIEW_POINTER_VERSION
        or not isinstance(current, Mapping)
    ):
        raise ValueError("Loop C weekly review pointer is malformed")
    raw_run = current.get("run_path")
    if not isinstance(raw_run, str) or not raw_run:
        raise ValueError("Loop C weekly review pointer run_path is missing")
    run = (root / raw_run).resolve()
    if run.parent != (root / "ml" / "loop-c-weekly-reviews").resolve():
        raise ValueError("Loop C weekly review pointer escapes immutable reviews")
    verify_manifest(run)
    report_path = run / "report.json"
    markdown_path = run / "review.md"
    receipt_path = run / "receipt.json"
    report = _read_object(report_path, "Loop C weekly report")
    receipt = _read_object(receipt_path, "Loop C weekly receipt")
    safety = receipt.get("safety")
    report_safety = report.get("safety")
    expected_current = {
        "run_path": raw_run,
        "reviewed_at": receipt.get("reviewed_at"),
        "status": receipt.get("status"),
        "receipt_sha256": file_checksum(receipt_path),
    }
    if (
        dict(current) != expected_current
        or receipt.get("schema_version")
        != LOOP_C_WEEKLY_REVIEW_RECEIPT_VERSION
        or receipt.get("authority") != "REVIEW_ONLY"
        or receipt.get("manifest_sha256") != file_checksum(run / "manifest.json")
        or receipt.get("report_sha256") != file_checksum(report_path)
        or receipt.get("markdown_sha256") != file_checksum(markdown_path)
        or report.get("schema_version") != LOOP_C_WEEKLY_REVIEW_SCHEMA_VERSION
        or report.get("status") != receipt.get("status")
        or not isinstance(safety, Mapping)
        or safety.get("authority") != "REVIEW_ONLY"
        or safety.get("broker_mutation_performed") is not False
        or safety.get("model_or_threshold_mutation_performed") is not False
        or safety.get("risk_or_halt_control_mutation_performed") is not False
        or safety.get("orders_enabled") is not False
        or int(safety.get("orders_placed", -1)) != 0
        or not isinstance(report_safety, Mapping)
        or dict(report_safety) != dict(safety)
    ):
        raise ValueError("Loop C weekly review receipt verification failed")
    return LoopCWeeklyReviewResult(
        run_directory=run,
        report_path=report_path,
        markdown_path=markdown_path,
        receipt_path=receipt_path,
        status=str(receipt.get("status")),
    )


def _verify_schwab_run(
    root: Path,
    run: Path,
    *,
    reviewed_at: pd.Timestamp,
    required_review_start: date | None = None,
    required_review_end: date | None = None,
) -> dict[str, object]:
    allowed = (root / "accounts" / "schwab" / "loop-c-read-only-runs").resolve()
    if run.parent != allowed:
        raise ValueError("Weekly review Schwab run escapes immutable read-only evidence")
    manifest = verify_manifest(run)
    receipt_path = run / "receipt.json"
    facts_path = run / "sanitized-account-facts.json"
    history_path = run / "trade-history-summary.json"
    receipt = _read_object(receipt_path, "Schwab receipt")
    facts = _read_object(facts_path, "Schwab facts")
    history = _read_object(history_path, "Schwab history")
    safety = receipt.get("safety")
    sanitization = receipt.get("sanitization")
    observed = _required_utc(receipt.get("observed_at"), "Schwab observed_at")
    if (
        receipt.get("schema_version") != LOOP_C_SCHWAB_RECEIPT_SCHEMA_VERSION
        or receipt.get("authority") != "OBSERVED_READ_ONLY"
        or receipt.get("manifest_sha256") != file_checksum(run / "manifest.json")
        or receipt.get("facts_sha256") != file_checksum(facts_path)
        or receipt.get("history_sha256") != file_checksum(history_path)
        or observed > reviewed_at
        or not isinstance(safety, Mapping)
        or safety.get("broker_data_http_methods") != ["GET"]
        or safety.get("order_submission_called") is not False
        or safety.get("order_replacement_called") is not False
        or safety.get("order_cancellation_called") is not False
        or safety.get("orders_enabled") is not False
        or int(safety.get("orders_placed", -1)) != 0
        or not isinstance(sanitization, Mapping)
        or any(
            sanitization.get(name) is not False
            for name in (
                "account_identifiers_persisted",
                "raw_order_identifiers_persisted",
                "raw_transaction_identifiers_persisted",
                "oauth_tokens_persisted",
            )
        )
    ):
        raise ValueError("Weekly review Schwab evidence failed its read-only receipt")
    if not isinstance(manifest.get("configuration"), Mapping):
        raise ValueError("Weekly review Schwab manifest configuration is missing")
    if required_review_start is not None:
        period = history.get("review_period")
        if (
            not isinstance(period, Mapping)
            or period.get("schema_version")
            != "loop-c-account-weekly-review-period-v1"
            or period.get("range_start") != required_review_start.isoformat()
            or required_review_end is None
            or str(period.get("range_end", "")) < required_review_end.isoformat()
            or period.get("attribution")
            != "ACCOUNT_OPTIONS_CONTEXT_NOT_LOOP_C_ATTRIBUTED"
            or period.get("automatic_threshold_or_risk_change_allowed") is not False
        ):
            raise ValueError("Schwab evidence does not contain the exact weekly review period")
    return {
        "manifest": manifest,
        "receipt": receipt,
        "facts": facts,
        "history": history,
        "receipt_path": receipt_path,
        "facts_path": facts_path,
        "history_path": history_path,
    }


def _current_schwab_run(root: Path) -> Path:
    pointer_path = root / "accounts" / "schwab" / "loop-c-read-only-latest" / "run.json"
    pointer = _read_object(pointer_path, "current Schwab pointer")
    current = pointer.get("current")
    if not isinstance(current, Mapping):
        raise ValueError("Current Schwab pointer has no current record")
    raw = current.get("run_path")
    if not isinstance(raw, str) or not raw:
        raise ValueError("Current Schwab pointer run_path is missing")
    return (root / raw).resolve()


def _load_loop_c_decisions(
    root: Path,
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> tuple[list[dict[str, object]], tuple[Path, ...]]:
    runs_root = root / "ml" / "loop-c-runs"
    if not runs_root.is_dir():
        return [], ()
    decisions: list[dict[str, object]] = []
    sources: list[Path] = []
    for run in sorted(path for path in runs_root.iterdir() if path.is_dir()):
        receipt_path = run / "publication.json"
        report_path = run / "report.json"
        decision_path = run / "decisions.parquet"
        if not receipt_path.is_file():
            continue
        manifest = verify_manifest(run)
        receipt = _read_object(receipt_path, "Loop C publication receipt")
        report = _read_object(report_path, "Loop C report")
        safety = receipt.get("safety")
        report_safety = report.get("safety")
        configuration = manifest.get("configuration")
        decision = report.get("decision")
        if (
            receipt.get("schema_version") != LOOP_C_PUBLICATION_VERSION
            or receipt.get("authority") != "OBSERVE_ONLY"
            or receipt.get("manifest_checksum_sha256")
            != file_checksum(run / "manifest.json")
            or not isinstance(safety, Mapping)
            or safety.get("orders_enabled") is not False
            or int(safety.get("orders_placed", -1)) != 0
            or safety.get("broker_submission_path_present") is not False
            or not isinstance(report_safety, Mapping)
            or report_safety.get("orders_enabled") is not False
            or int(report_safety.get("orders_placed", -1)) != 0
            or not isinstance(configuration, Mapping)
            or configuration.get("authority") != "OBSERVE_ONLY"
            or configuration.get("orders_enabled") is not False
            or int(configuration.get("orders_placed", -1)) != 0
            or not isinstance(decision, Mapping)
            or decision.get("automated_action_allowed") is not False
            or decision.get("orders_enabled") is not False
            or int(decision.get("orders_placed", -1)) != 0
        ):
            raise ValueError(f"Loop C weekly source failed zero-order verification: {run}")
        decision_at = _required_utc(
            decision.get("decision_timestamp"),
            "Loop C decision_timestamp",
        )
        if not start <= decision_at <= end:
            continue
        decisions.append(
            {
                "run_path": run.relative_to(root).as_posix(),
                "manifest": manifest,
                "report": report,
                "decision": dict(decision),
                "decision_at": decision_at,
            }
        )
        sources.extend((run / "manifest.json", receipt_path, report_path, decision_path))
    decisions.sort(key=lambda row: (row["decision_at"], row["run_path"]))
    return decisions, tuple(dict.fromkeys(sources))


def _load_strategy_outcomes(
    root: Path,
    *,
    reviewed_at: pd.Timestamp,
) -> tuple[pd.DataFrame, tuple[Path, ...], str]:
    report = read_current_strategy_outcome_evidence(root)
    if report is None:
        return pd.DataFrame(), (), "NOT_PUBLISHED"
    evaluated = _required_utc(report.get("evaluated_at"), "Strategy evaluated_at")
    if evaluated > reviewed_at:
        raise ValueError("Strategy outcome evidence is future-dated")
    pointer_path = root / "ml" / "option-pricing-strategy-latest" / "report.json"
    pointer = _read_object(pointer_path, "Strategy outcome pointer")
    current = pointer.get("current")
    if not isinstance(current, Mapping):
        raise ValueError("Strategy outcome pointer has no current record")
    run = (root / str(current.get("run_path", ""))).resolve()
    observations_path = run / "observations.parquet"
    observations = pd.read_parquet(observations_path)
    return (
        observations,
        (pointer_path, run / "receipt.json", run / "report.json", observations_path),
        str(report.get("status", "UNKNOWN")),
    )


def _summarize_shadow_performance(
    root: Path,
    decisions: Sequence[Mapping[str, object]],
    *,
    outcome_frame: pd.DataFrame,
    outcome_evidence_status: str,
) -> tuple[dict[str, object], tuple[Path, ...]]:
    proposal_rows = [
        row
        for row in decisions
        if isinstance(row.get("decision"), Mapping)
        and row["decision"].get("action") == "RESEARCH_PROPOSAL"
    ]
    observations: list[dict[str, object]] = []
    sources: list[Path] = []
    for row in proposal_rows:
        decision = row["decision"]
        assert isinstance(decision, Mapping)
        matched, matched_sources = _match_strategy_outcome(root, row, outcome_frame)
        sources.extend(matched_sources)
        realized = _finite(matched.get("realized_net_profit_usd")) if matched else None
        quantity = _nonnegative_integer(decision.get("quantity")) or 0
        scaled = realized * quantity if realized is not None else None
        observations.append(
            {
                "decision_timestamp": _required_utc(
                    decision.get("decision_timestamp"),
                    "Loop C decision_timestamp",
                ).isoformat(),
                "candidate_id": decision.get("candidate_id"),
                "symbol": decision.get("symbol"),
                "horizon": decision.get("horizon"),
                "quantity": quantity,
                "calibrated_probability": decision.get("calibrated_probability"),
                "sequence_directional_probability": decision.get(
                    "sequence_directional_probability"
                ),
                "expected_return_on_risk": decision.get("expected_return_on_risk"),
                "total_uncertainty": decision.get("total_uncertainty"),
                "modeled_maximum_loss": decision.get("modeled_maximum_loss"),
                "outcome_status": (
                    "MATURE_RECEIPT_MATCHED"
                    if scaled is not None
                    else "PENDING_OR_UNAVAILABLE"
                ),
                "counterfactual_realized_net_pnl": _money(scaled),
            }
        )
    mature = [row for row in observations if row["counterfactual_realized_net_pnl"] is not None]
    values = [float(row["counterfactual_realized_net_pnl"]) for row in mature]
    wins = sum(value > 0.0 for value in values)
    losses = sum(value < 0.0 for value in values)
    breakeven = len(values) - wins - losses
    per_horizon: dict[str, dict[str, object]] = {}
    for horizon in ("1h", "4h", "1d", "1w"):
        rows = [row for row in mature if row.get("horizon") == horizon]
        horizon_values = [float(row["counterfactual_realized_net_pnl"]) for row in rows]
        per_horizon[horizon] = {
            "mature_proposals": len(rows),
            "net_counterfactual_pnl": _money(sum(horizon_values)) or 0.0,
            "wins": sum(value > 0.0 for value in horizon_values),
            "losses": sum(value < 0.0 for value in horizon_values),
        }
    return (
        {
            "attribution": "LOOP_C_COUNTERFACTUAL_NOT_BROKER_EXECUTIONS",
            "outcome_evidence_status": outcome_evidence_status,
            "proposal_count": len(proposal_rows),
            "mature_receipt_matched_proposals": len(mature),
            "pending_or_unavailable_proposals": len(observations) - len(mature),
            "net_counterfactual_pnl": _money(sum(values)) or 0.0,
            "gross_counterfactual_profit": _money(sum(value for value in values if value > 0.0)) or 0.0,
            "gross_counterfactual_loss": _money(sum(value for value in values if value < 0.0)) or 0.0,
            "wins": wins,
            "losses": losses,
            "breakeven": breakeven,
            "win_rate": wins / max(wins + losses, 1) if values else None,
            "maximum_closed_pnl_drawdown": _money(_maximum_drawdown(values)) or 0.0,
            "per_horizon": per_horizon,
            "observations": observations,
            "execution_assumption": "Receipt-matched Strategy outcomes use their checked-in conservative exit, fees, and spread policy.",
            "automatic_change_allowed": False,
        },
        tuple(dict.fromkeys(sources)),
    )


def _match_strategy_outcome(
    root: Path,
    loop_row: Mapping[str, object],
    outcomes: pd.DataFrame,
) -> tuple[Mapping[str, object] | None, tuple[Path, ...]]:
    if outcomes.empty:
        return None, ()
    manifest = loop_row.get("manifest")
    decision = loop_row.get("decision")
    if not isinstance(manifest, Mapping) or not isinstance(decision, Mapping):
        return None, ()
    configuration = manifest.get("configuration")
    if not isinstance(configuration, Mapping):
        return None, ()
    strategy_source = configuration.get("strategy_source")
    if not isinstance(strategy_source, Mapping):
        return None, ()
    raw_run = strategy_source.get("run_path")
    candidate_id = decision.get("candidate_id")
    if not isinstance(raw_run, str) or not raw_run or candidate_id is None:
        return None, ()
    run = (root / raw_run).resolve()
    if run.parent != (root / "ml" / "strategy-runs").resolve():
        raise ValueError("Loop C Strategy source escapes immutable strategy runs")
    strategy_manifest = verify_manifest(run)
    publication = _read_object(run / "publication.json", "Strategy publication")
    if (
        publication.get("schema_version") != STRATEGY_PUBLICATION_VERSION
        or publication.get("manifest_checksum_sha256")
        != file_checksum(run / "manifest.json")
    ):
        raise ValueError("Loop C Strategy source publication failed verification")
    candidates = pd.read_parquet(run / "strategy-candidates.parquet")
    sources = (
        run / "manifest.json",
        run / "publication.json",
        run / "strategy-candidates.parquet",
    )
    matches = candidates.loc[candidates["id"].astype("string").eq(str(candidate_id))]
    if len(matches) != 1:
        return None, sources
    candidate = matches.iloc[0]
    required = {
        "strategy_run_path",
        "symbol",
        "horizon",
        "decision_timestamp",
        "candidate_key",
        "realized_net_profit_usd",
    }
    if missing := required.difference(outcomes.columns):
        raise ValueError(
            "Strategy outcome evidence is missing Loop C matching fields: "
            + ", ".join(sorted(missing))
        )
    decision_time = _required_utc(
        candidate.get("decision_timestamp"),
        "Strategy candidate decision_timestamp",
    )
    outcome_times = pd.to_datetime(outcomes["decision_timestamp"], utc=True, errors="coerce")
    mask = (
        outcomes["strategy_run_path"].astype("string").eq(raw_run)
        & outcomes["symbol"].astype("string").str.upper().eq(str(candidate.get("symbol", "")).upper())
        & outcomes["horizon"].astype("string").str.lower().eq(str(candidate.get("horizon", "")).lower())
        & outcomes["candidate_key"].astype("string").eq(str(candidate.get("candidate_key", "")))
        & outcome_times.eq(decision_time)
    )
    matches = outcomes.loc[mask]
    if len(matches) > 1:
        raise ValueError("Strategy outcome evidence duplicated a Loop C candidate")
    return (matches.iloc[0].to_dict() if len(matches) == 1 else None), sources


def _equity_bridge(
    root: Path,
    *,
    start: pd.Timestamp,
    last_session_close: pd.Timestamp,
    end: pd.Timestamp,
) -> tuple[dict[str, object], tuple[Path, ...]]:
    runs_root = root / "accounts" / "schwab" / "loop-c-read-only-runs"
    observations: list[tuple[pd.Timestamp, float, Path]] = []
    if runs_root.is_dir():
        for run in sorted(path for path in runs_root.iterdir() if path.is_dir()):
            try:
                verified = _verify_schwab_run(root, run, reviewed_at=end)
                observed = _required_utc(
                    verified["receipt"].get("observed_at"),
                    "Schwab observed_at",
                )
                facts = verified["facts"]
                account_values = facts.get("account_values")
                equity = (
                    _finite(account_values.get("liquidation_value"))
                    if isinstance(account_values, Mapping)
                    else None
                )
            except Exception:
                continue
            if equity is not None and start <= observed <= end:
                observations.append((observed, equity, run))
    if len(observations) < 2:
        return (
            {
                "status": "INSUFFICIENT_VERIFIED_SNAPSHOTS",
                "snapshot_count": len(observations),
                "attribution": "UNATTRIBUTED_ACCOUNT_EQUITY_CHANGE",
                "cash_flow_adjusted": False,
            },
            tuple(
                path / "receipt.json" for _, _, path in observations
            ),
        )
    observations.sort(key=lambda row: row[0])
    opening = observations[0]
    closing = observations[-1]
    opening_covers_first_session = (
        opening[0].tz_convert("America/New_York").date()
        == start.tz_convert("America/New_York").date()
    )
    closing_covers_last_session = closing[0] >= last_session_close
    if not opening_covers_first_session or not closing_covers_last_session:
        return (
            {
                "status": "INSUFFICIENT_WINDOW_COVERAGE",
                "snapshot_count": len(observations),
                "first_observed_at": opening[0].isoformat(),
                "last_observed_at": closing[0].isoformat(),
                "first_session_covered": opening_covers_first_session,
                "last_session_close_covered": closing_covers_last_session,
                "attribution": "UNATTRIBUTED_ACCOUNT_EQUITY_CHANGE",
                "cash_flow_adjusted": False,
                "usable_to_claim_loop_c_effectiveness": False,
            },
            tuple(
                item
                for _, _, path in observations
                for item in (
                    path / "receipt.json",
                    path / "sanitized-account-facts.json",
                )
            ),
        )
    return (
        {
            "status": "OBSERVED_NOT_CASH_FLOW_ADJUSTED",
            "snapshot_count": len(observations),
            "opening_observed_at": opening[0].isoformat(),
            "opening_equity": _money(opening[1]),
            "closing_observed_at": closing[0].isoformat(),
            "closing_equity": _money(closing[1]),
            "equity_change": _money(closing[1] - opening[1]),
            "attribution": "UNATTRIBUTED_ACCOUNT_EQUITY_CHANGE",
            "cash_flow_adjusted": False,
            "usable_to_claim_loop_c_effectiveness": False,
        },
        tuple(
            item
            for _, _, path in observations
            for item in (
                path / "receipt.json",
                path / "sanitized-account-facts.json",
            )
        ),
    )


def _decision_summary(decisions: Sequence[Mapping[str, object]]) -> dict[str, object]:
    actions: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    horizons: Counter[str] = Counter()
    symbols: Counter[str] = Counter()
    sessions: set[date] = set()
    modeled_loss = 0.0
    for row in decisions:
        decision = row.get("decision")
        if not isinstance(decision, Mapping):
            continue
        actions[str(decision.get("action", "UNKNOWN"))] += 1
        statuses[str(decision.get("status", "UNKNOWN"))] += 1
        horizon = str(decision.get("horizon") or "NONE")
        symbol = str(decision.get("symbol") or "NONE")
        horizons[horizon] += 1
        symbols[symbol] += 1
        sessions.add(
            _required_utc(
                decision.get("decision_timestamp"),
                "Loop C decision_timestamp",
            )
            .tz_convert("America/New_York")
            .date()
        )
        for reason in decision.get("reason_codes", []):
            reasons[str(reason)] += 1
        value = _finite(decision.get("modeled_maximum_loss"))
        if value is not None:
            modeled_loss += value
    return {
        "verified_run_count": len(decisions),
        "completed_session_count": len(sessions),
        "actions": dict(sorted(actions.items())),
        "statuses": dict(sorted(statuses.items())),
        "reason_codes": dict(sorted(reasons.items())),
        "horizons": dict(sorted(horizons.items())),
        "symbols": dict(sorted(symbols.items())),
        "aggregate_modeled_maximum_loss_if_each_proposal_entered": _money(modeled_loss) or 0.0,
        "orders_placed": 0,
    }


def _cohort_summary(decisions: Sequence[Mapping[str, object]]) -> dict[str, object]:
    fingerprints: Counter[str] = Counter()
    approval_ids: Counter[str] = Counter()
    policy_versions: Counter[str] = Counter()
    expiries: set[str] = set()
    risk_limit_sets: dict[str, dict[str, object]] = {}
    model_binding_sets: dict[str, dict[str, object]] = {}
    for row in decisions:
        report = row.get("report")
        manifest = row.get("manifest")
        if isinstance(report, Mapping):
            contracts = report.get("input_contracts")
            if isinstance(contracts, Mapping):
                model_fingerprint = str(
                    contracts.get("sequence_configuration_fingerprint", "MISSING")
                )
                fingerprints[model_fingerprint] += 1
                approval_ids[str(contracts.get("risk_approval_id", "MISSING"))] += 1
                expiry = contracts.get("risk_approval_expires_at")
                if expiry is not None:
                    expiries.add(str(expiry))
            consumer = report.get("sequence_consumer")
            if isinstance(consumer, Mapping) and isinstance(
                consumer.get("model_binding"), Mapping
            ):
                binding = dict(consumer["model_binding"])
                binding_id = semantic_metadata_fingerprint(binding)
                entry = model_binding_sets.setdefault(
                    binding_id,
                    {"use_count": 0, "binding": binding},
                )
                entry["use_count"] = int(entry["use_count"]) + 1
        if isinstance(manifest, Mapping):
            configuration = manifest.get("configuration")
            if isinstance(configuration, Mapping):
                limits = configuration.get("risk_limits")
                if isinstance(limits, Mapping):
                    policy_versions[str(limits.get("policy_version", "MISSING"))] += 1
                    normalized_limits = dict(limits)
                    limit_id = semantic_metadata_fingerprint(normalized_limits)
                    entry = risk_limit_sets.setdefault(
                        limit_id,
                        {"use_count": 0, "limits": normalized_limits},
                    )
                    entry["use_count"] = int(entry["use_count"]) + 1
    compatible = (
        len(fingerprints) <= 1
        and len(approval_ids) <= 1
        and len(policy_versions) <= 1
        and "MISSING" not in fingerprints
        and "MISSING" not in approval_ids
        and "MISSING" not in policy_versions
    )
    return {
        "status": "COMPARABLE" if compatible else "INCOMPATIBLE_COHORT_DEFINITIONS",
        "sequence_configuration_fingerprints": dict(sorted(fingerprints.items())),
        "risk_approval_ids": dict(sorted(approval_ids.items())),
        "risk_policy_versions": dict(sorted(policy_versions.items())),
        "approval_expiries": sorted(expiries),
        "exact_model_binding_sets": dict(sorted(model_binding_sets.items())),
        "exact_risk_limit_sets": dict(sorted(risk_limit_sets.items())),
    }


def _review_status(
    decisions: Sequence[Mapping[str, object]],
    shadow: Mapping[str, object],
    cohorts: Mapping[str, object],
) -> str:
    if not decisions:
        return "INSUFFICIENT_LOOP_C_OBSERVATIONS"
    if cohorts.get("status") != "COMPARABLE":
        return "INCOMPATIBLE_COHORT_DEFINITIONS"
    if int(shadow.get("mature_receipt_matched_proposals", 0)) == 0:
        return "INSUFFICIENT_MATURE_LOOP_C_OUTCOMES"
    return "WEEKLY_OPERATOR_DISCUSSION_READY"


def _proposal_summary(
    root: Path,
    proposal: Mapping[str, object] | None,
) -> tuple[dict[str, object], tuple[Path, ...]]:
    if not isinstance(proposal, Mapping):
        return ({"status": "NOT_GENERATED"}, ())
    paths: list[Path] = []
    rendered: dict[str, object] = {
        "status": proposal.get("status"),
        "resolved_limits": proposal.get("resolved_limits"),
        "model_configuration_fingerprint": proposal.get(
            "model_configuration_fingerprint"
        ),
        "automatic_approval_allowed": False,
    }
    for key in (
        "proposal_path",
        "risk_approval_path",
        "calculus_path",
        "halt_control_path",
    ):
        raw = proposal.get(key)
        if not isinstance(raw, str) or not raw:
            continue
        path = Path(raw).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError(f"Pending weekly proposal source is invalid: {key}")
        paths.append(path)
        rendered[key] = path.relative_to(root).as_posix()
        rendered[f"{key}_sha256"] = file_checksum(path)
    return rendered, tuple(paths)


def _render_markdown(report: Mapping[str, object]) -> str:
    window = report["window"]
    account = report["actual_account_context"]
    loop_c = report["loop_c"]
    assert isinstance(window, Mapping)
    assert isinstance(account, Mapping)
    assert isinstance(loop_c, Mapping)
    performance = account.get("performance", {})
    shadow = loop_c.get("shadow_counterfactual_performance", {})
    decisions = loop_c.get("decision_summary", {})
    proposal = report.get("pending_risk_proposal", {})
    assert isinstance(performance, Mapping)
    assert isinstance(shadow, Mapping)
    assert isinstance(decisions, Mapping)
    assert isinstance(proposal, Mapping)
    return (
        "# Loop C weekly operator review\n\n"
        f"Status: `{report['status']}`  \n"
        f"XNYS sessions: `{window['first_completed_session']}` through "
        f"`{window['last_completed_session']}`  \n"
        "Authority: `REVIEW_ONLY`; orders placed: `0`\n\n"
        "## Actual account context (not Loop C-attributed)\n\n"
        f"- Reconstructed closed option positions: `{performance.get('included_closed_positions', 0)}`\n"
        f"- Net realized P/L: `{_currency(performance.get('net_realized_pnl'))}`\n"
        f"- Wins / losses / breakeven: `{performance.get('win_count', 0)} / "
        f"{performance.get('loss_count', 0)} / {performance.get('breakeven_count', 0)}`\n"
        f"- Maximum closed-P/L drawdown: `{_currency(performance.get('maximum_closed_pnl_drawdown'))}`\n\n"
        "## Loop C observe-only counterfactuals\n\n"
        f"- Verified hourly runs: `{decisions.get('verified_run_count', 0)}`\n"
        f"- Research proposals: `{shadow.get('proposal_count', 0)}`\n"
        f"- Mature receipt-matched proposals: `{shadow.get('mature_receipt_matched_proposals', 0)}`\n"
        f"- Pending outcomes: `{shadow.get('pending_or_unavailable_proposals', 0)}`\n"
        f"- Counterfactual net P/L after modeled execution: `{_currency(shadow.get('net_counterfactual_pnl'))}`\n\n"
        "## Decision boundary\n\n"
        "No threshold, risk limit, model binding, halt control, or broker state was changed. "
        "Any next-period values require discussion, a frozen proposal, and explicit operator approval.\n\n"
        f"Pending risk proposal: `{proposal.get('status', 'NOT_GENERATED')}`\n"
    )


def _maximum_drawdown(values: Sequence[float]) -> float:
    cumulative = 0.0
    peak = 0.0
    maximum = 0.0
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        maximum = max(maximum, peak - cumulative)
    return maximum


def _read_object(path: Path, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _required_utc(value: object, label: str) -> pd.Timestamp:
    if value is None or not str(value).strip():
        raise ValueError(f"{label} is missing")
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"{label} is invalid")
    return pd.Timestamp(parsed)


def _finite(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _nonnegative_integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _money(value: object) -> float | None:
    parsed = _finite(value)
    return round(parsed + 1.0e-12, 2) if parsed is not None else None


def _currency(value: object) -> str:
    parsed = _money(value)
    return "unavailable" if parsed is None else f"${parsed:,.2f}"


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + f".tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a receipt-verified Loop C weekly operator review."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--root-dir", type=Path)
    group.add_argument("--datastore-target", choices=sorted(DATASTORE_TARGETS))
    parser.add_argument("--reviewed-at")
    parser.add_argument("--week-ending", type=date.fromisoformat)
    parser.add_argument("--schwab-run", type=Path)
    parser.add_argument("--capture-schwab", action="store_true")
    parser.add_argument("--build-risk-proposal", action="store_true")
    parser.add_argument("--compact", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = resolve_datastore_dir(root_dir=args.root_dir, target=args.datastore_target)
        timestamp = utc_timestamp(args.reviewed_at)
        window = resolve_weekly_review_window(
            timestamp,
            week_ending=args.week_ending,
        )
        with exclusive_runtime_lock(
            root / ".ducketz-loop-c-weekly-review.lock",
            process_name="Duckets Loop C weekly review",
        ):
            if args.capture_schwab and args.schwab_run is not None:
                raise ValueError("--capture-schwab and --schwab-run are mutually exclusive")
            if args.capture_schwab:
                with exclusive_runtime_lock(
                    root / ".ducketz-loop-c-schwab-read-only.lock",
                    process_name="Duckets Loop C Schwab read-only snapshot",
                ):
                    schwab_result = capture_schwab_read_only_state(
                        root,
                        observed_at=timestamp,
                        review_period_start=window.calendar_week_start,
                    )
                schwab_run = schwab_result.run_directory
            else:
                schwab_run = args.schwab_run
            proposal = (
                build_pending_risk_proposal(root, as_of=timestamp)
                if args.build_risk_proposal
                else None
            )
            result = build_loop_c_weekly_review(
                root,
                reviewed_at=timestamp,
                week_ending=args.week_ending,
                schwab_run_directory=schwab_run,
                pending_risk_proposal=proposal,
            )
        output = {
            "status": result.status,
            "run_directory": str(result.run_directory),
            "report": str(result.report_path),
            "review": str(result.markdown_path),
            "receipt": str(result.receipt_path),
            "authority": "REVIEW_ONLY",
            "automatic_change_allowed": False,
            "orders_enabled": False,
            "orders_placed": 0,
        }
        exit_code = 0
    except Exception as exc:
        output = {
            "status": "ERROR",
            "error": f"{type(exc).__name__}: {exc}",
            "authority": "NONE",
            "automatic_change_allowed": False,
            "orders_enabled": False,
            "orders_placed": 0,
        }
        exit_code = 2
    print(json.dumps(output, separators=(",", ":") if args.compact else None))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "LOOP_C_WEEKLY_REVIEW_SCHEMA_VERSION",
    "LoopCWeeklyReviewResult",
    "WeeklyReviewWindow",
    "build_loop_c_weekly_review",
    "main",
    "read_current_loop_c_weekly_review",
    "resolve_weekly_review_window",
]
