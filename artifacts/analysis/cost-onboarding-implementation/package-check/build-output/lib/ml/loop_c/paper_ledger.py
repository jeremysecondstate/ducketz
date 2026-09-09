from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.artifacts import (
    create_timestamp_directory,
    file_checksum,
    utc_timestamp,
    verify_manifest,
    write_manifest,
)
from ml.loop_c.engine import LOOP_C_OPTION_SHADOW_HORIZONS
from ml.loop_c.publication import LOOP_C_PUBLICATION_VERSION
from ml.option_pricing.strategy_outcomes import (
    read_current_strategy_outcome_evidence,
)
from ml.strategy_publication import STRATEGY_PUBLICATION_VERSION


LOOP_C_PAPER_LEDGER_SCHEMA_VERSION = "loop-c-options-strategy-paper-ledger-v1"
LOOP_C_PAPER_LEDGER_RECEIPT_VERSION = (
    "loop-c-options-strategy-paper-ledger-receipt-v1"
)
LOOP_C_PAPER_LEDGER_POINTER_VERSION = (
    "loop-c-options-strategy-paper-ledger-pointer-v1"
)

_HORIZON_LABELS = {
    "1d": "One-Session",
    "1w": "Remaining-Week Aggregate",
}


@dataclass(frozen=True)
class LoopCPaperLedgerPublication:
    run_directory: Path
    report_path: Path
    receipt_path: Path
    report: Mapping[str, object]


def paper_ledger_pointer_path(datastore_root: Path) -> Path:
    return Path(datastore_root) / "ml" / "loop-c-paper-ledger-latest" / "run.json"


def build_paper_trade_snapshot(
    candidate: Mapping[str, object],
    *,
    decision: Mapping[str, object],
    strategy_run_path: str,
    loop_c_run_path: str | None = None,
) -> dict[str, object]:
    """Freeze one exact generated Options Strategy as a paper entry."""

    candidate_id = str(candidate.get("id") or "").strip()
    candidate_key = str(candidate.get("candidate_key") or "").strip()
    symbol = str(candidate.get("symbol") or "").strip().upper()
    horizon = str(candidate.get("horizon") or "").strip().lower()
    quantity = _positive_integer(decision.get("quantity"))
    if not candidate_id or not candidate_key or not symbol:
        raise ValueError("Loop C paper candidate identity is incomplete")
    if horizon not in LOOP_C_OPTION_SHADOW_HORIZONS:
        raise ValueError("Loop C paper candidate must use the 1d or 1w horizon")
    if quantity is None:
        raise ValueError("Loop C paper quantity must be a positive integer")
    if str(decision.get("candidate_id") or "") != candidate_id:
        raise ValueError("Loop C paper candidate differs from its decision")
    decision_timestamp = _iso_utc(
        decision.get("decision_timestamp"), "Loop C decision_timestamp"
    )
    candidate_decision_timestamp = _iso_utc(
        candidate.get("decision_timestamp"), "Strategy decision_timestamp"
    )
    target_window_start = _iso_utc(
        candidate.get("target_window_start"), "Strategy target_window_start"
    )
    target_window_end = _iso_utc(
        candidate.get("target_window_end"), "Strategy target_window_end"
    )
    entry_available_at = _iso_utc(
        candidate.get("entry_available_at"), "Strategy entry_available_at"
    )
    legs_text = str(candidate.get("legs_json") or "")
    try:
        legs = json.loads(legs_text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Loop C paper candidate has unreadable exact legs") from exc
    if not isinstance(legs, list) or not legs or not all(
        isinstance(leg, Mapping) for leg in legs
    ):
        raise ValueError("Loop C paper candidate has invalid exact legs")
    option_obligations: list[dict[str, object]] = []
    integral_stock_shares = 0.0
    target_end_timestamp = _utc(
        candidate.get("target_window_end"), "Strategy target_window_end"
    )
    for leg in legs:
        asset = str(leg.get("asset") or "").strip().upper()
        side = str(leg.get("side") or "").strip().upper()
        quantity_value = _positive_integer(leg.get("quantity"))
        multiplier = _finite(leg.get("multiplier"))
        if quantity_value is None or multiplier is None or multiplier <= 0.0:
            raise ValueError("Loop C paper candidate has invalid leg quantity/multiplier")
        if side not in {"LONG", "SHORT"}:
            raise ValueError("Loop C paper candidate has an invalid leg side")
        if asset == "STOCK":
            signed = quantity_value if side == "LONG" else -quantity_value
            integral_stock_shares += signed * multiplier
            continue
        if asset != "OPTION":
            raise ValueError("Loop C paper candidate has an unsupported leg asset")
        option_type = str(leg.get("option_type") or "").strip().upper()
        if option_type not in {"CALL", "PUT"}:
            raise ValueError("Loop C paper candidate has an invalid option type")
        expiration = _utc(
            leg.get("expiration_date"), "Strategy option expiration_date"
        )
        if target_end_timestamp.date() > expiration.date():
            raise ValueError("Loop C paper target extends beyond an option expiration")
        share_change_sign = 1.0 if (
            (side == "LONG" and option_type == "CALL")
            or (side == "SHORT" and option_type == "PUT")
        ) else -1.0
        share_obligation = quantity_value * multiplier
        option_obligations.append(
            {
                "contract_symbol": _optional_text(leg.get("contract_symbol")),
                "side": side,
                "option_type": option_type,
                "expiration_date": expiration.isoformat(),
                "contracts_per_strategy": quantity_value,
                "shares_per_contract": multiplier,
                "exercise_or_assignment_event": (
                    "EXERCISE" if side == "LONG" else "ASSIGNMENT"
                ),
                "potential_share_change_direction": (
                    "BUY_SHARES" if share_change_sign > 0.0 else "SELL_SHARES"
                ),
                "potential_share_obligation_per_strategy": share_obligation,
                "potential_signed_share_change_per_strategy": (
                    share_change_sign * share_obligation
                ),
                "potential_signed_share_change_total": (
                    share_change_sign * share_obligation * quantity
                ),
            }
        )
    if not option_obligations:
        raise ValueError("Loop C paper candidate has no option legs")
    legs_checksum = hashlib.sha256(legs_text.encode("utf-8")).hexdigest()
    identity = {
        "schema_version": LOOP_C_PAPER_LEDGER_SCHEMA_VERSION,
        "strategy_run_path": strategy_run_path,
        "candidate_id": candidate_id,
        "loop_c_decision_timestamp": decision_timestamp,
        "quantity": quantity,
    }
    paper_trade_id = hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()
    cluster = {
        "symbol": symbol,
        "horizon": horizon,
        "target_window_start": target_window_start,
        "target_window_end": target_window_end,
    }
    cluster_id = hashlib.sha256(_canonical_json(cluster).encode("utf-8")).hexdigest()
    per_unit_max_loss = _finite(candidate.get("max_loss"))
    per_unit_capital = _finite(candidate.get("capital_required"))
    per_unit_entry_fees = _finite(candidate.get("entry_fees"))
    return {
        "paper_trade_id": paper_trade_id,
        "independent_decision_cluster_id": cluster_id,
        "loop_c_run_path": loop_c_run_path,
        "loop_c_decision_timestamp": decision_timestamp,
        "strategy_run_path": strategy_run_path,
        "candidate_id": candidate_id,
        "candidate_key": candidate_key,
        "candidate_decision_timestamp": candidate_decision_timestamp,
        "symbol": symbol,
        "horizon": horizon,
        "horizon_label": _HORIZON_LABELS[horizon],
        "target_window_start": target_window_start,
        "target_window_end": target_window_end,
        "entry_available_at": entry_available_at,
        "strategy_name": _optional_text(candidate.get("strategy_name")),
        "strategy_display_name": _optional_text(
            candidate.get("strategy_display_name")
        ),
        "strategy_family": _optional_text(candidate.get("strategy_family")),
        "candidate_rank": _nonnegative_integer(candidate.get("candidate_rank")),
        "quantity": quantity,
        "exact_legs": [_json_mapping(leg) for leg in legs],
        "legs_checksum_sha256": legs_checksum,
        "entry_assumptions": {
            "entry_cash_flow_per_strategy": _finite(
                candidate.get("entry_cash_flow")
            ),
            "entry_net_credit_per_strategy": _finite(
                candidate.get("entry_net_credit")
            ),
            "entry_net_debit_per_strategy": _finite(
                candidate.get("entry_net_debit")
            ),
            "entry_fees_per_strategy": per_unit_entry_fees,
            "capital_required_per_strategy": per_unit_capital,
            "modeled_maximum_loss_per_strategy": per_unit_max_loss,
            "total_entry_fees": _scaled(per_unit_entry_fees, quantity),
            "total_capital_required": _scaled(per_unit_capital, quantity),
            "total_modeled_maximum_loss": _scaled(per_unit_max_loss, quantity),
            "fill_policy": (
                "Exact generated Strategy leg BBOs and checked-in fees; no broker fill."
            ),
        },
        "selection_evidence": {
            "calibrated_profit_probability": _finite(
                decision.get("calibrated_probability")
            ),
            "sequence_directional_probability": _finite(
                decision.get("sequence_directional_probability")
            ),
            "sequence_expected_return": _finite(
                decision.get("sequence_expected_return")
            ),
            "sequence_adverse_return": _finite(
                decision.get("sequence_adverse_return")
            ),
            "expected_return_on_risk": _finite(
                decision.get("expected_return_on_risk")
            ),
            "total_uncertainty": _finite(decision.get("total_uncertainty")),
            "expected_utility": _finite(decision.get("expected_utility")),
            "score_basis": _optional_text(candidate.get("score_basis")),
            "pricing_source": _optional_text(candidate.get("pricing_source")),
            "pricing_status": _optional_text(candidate.get("pricing_status")),
            "pricing_leg_coverage": _finite(
                candidate.get("pricing_leg_coverage")
            ),
            "model_version": _optional_text(candidate.get("model_version")),
            "candidate_policy_version": _optional_text(
                candidate.get("candidate_policy_version")
            ),
            "model_policy_version": _optional_text(
                candidate.get("model_policy_version")
            ),
            "ranking_policy_version": _optional_text(
                candidate.get("ranking_policy_version")
            ),
        },
        "expiration_and_assignment": {
            "paper_only_no_assignment_occurs": True,
            "planned_exit_policy": (
                "Close every exact leg using the receipt-proven target-window exit BBO."
            ),
            "planned_exit_at": target_window_end,
            "earliest_option_expiration": min(
                str(row["expiration_date"]) for row in option_obligations
            ),
            "missing_exit_policy": (
                "Keep the outcome pending or unavailable; never assume an option expires harmlessly."
            ),
            "planned_exit_no_later_than_each_expiration_session": True,
            "future_live_exit_buffer_required": True,
            "future_live_options_authority_present": False,
            "option_leg_obligations": option_obligations,
            "gross_potential_share_obligation_per_strategy": sum(
                float(row["potential_share_obligation_per_strategy"])
                for row in option_obligations
            ),
            "gross_potential_share_obligation_total": sum(
                float(row["potential_share_obligation_per_strategy"])
                for row in option_obligations
            )
            * quantity,
            "net_share_change_if_every_leg_exercised_or_assigned_per_strategy": sum(
                float(row["potential_signed_share_change_per_strategy"])
                for row in option_obligations
            ),
            "net_share_change_if_every_leg_exercised_or_assigned_total": sum(
                float(row["potential_signed_share_change_total"])
                for row in option_obligations
            ),
            "integral_stock_shares_per_strategy": integral_stock_shares,
            "assignment_or_exercise_would_require_broker_controls": True,
        },
        "attribution": "LOOP_C_OPTIONS_STRATEGY_PAPER_ENTRY_NOT_BROKER_EXECUTION",
        "orders_enabled": False,
        "orders_placed": 0,
    }


def paper_candidate_has_bounded_exit(candidate: Mapping[str, object]) -> bool:
    """Return whether the exact strategy can use the stateless target-exit label."""

    if bool(candidate.get("lifecycle", False)):
        return False
    target_end = pd.to_datetime(
        candidate.get("target_window_end"), utc=True, errors="coerce"
    )
    if pd.isna(target_end):
        return False
    try:
        legs = json.loads(str(candidate.get("legs_json") or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(legs, list) or not legs:
        return False
    option_count = 0
    for leg in legs:
        if not isinstance(leg, Mapping):
            return False
        if str(leg.get("asset") or "").strip().upper() != "OPTION":
            continue
        option_count += 1
        expiration = pd.to_datetime(
            leg.get("expiration_date"), utc=True, errors="coerce"
        )
        if pd.isna(expiration) or pd.Timestamp(target_end).date() > pd.Timestamp(
            expiration
        ).date():
            return False
    return option_count > 0


def track_options_strategy_paper_trades(
    datastore_root: Path,
    *,
    tracked_at: object | None = None,
) -> LoopCPaperLedgerPublication:
    """Publish a receipt-backed daily lifecycle snapshot for Loop C paper trades."""

    root = Path(datastore_root).resolve()
    timestamp = utc_timestamp(tracked_at)
    entries, run_summary, source_files = _load_verified_paper_entries(
        root, tracked_at=timestamp
    )
    outcomes, outcome_status, outcome_sources = _load_outcomes(
        root, tracked_at=timestamp
    )
    tracked = [
        _attach_outcome(entry, outcomes=outcomes, tracked_at=timestamp)
        for entry in entries
    ]
    summary = _summarize(tracked, run_summary=run_summary)
    report: dict[str, object] = {
        "schema_version": LOOP_C_PAPER_LEDGER_SCHEMA_VERSION,
        "status": "TRACKING" if tracked else "NO_PAPER_TRADES_YET",
        "tracked_at": timestamp.isoformat(),
        "scope": {
            "asset_class": "OPTIONS_STRATEGY",
            "selection_source": "Loop C deterministic selection from generated Options Strategies",
            "eligible_horizons": list(LOOP_C_OPTION_SHADOW_HORIZONS),
            "horizon_labels": dict(_HORIZON_LABELS),
            "weekly_semantics": (
                "1w is the current dynamic Remaining-Week Aggregate, not a fixed five-session window."
            ),
            "interim_mark_to_market_used_as_final_outcome": False,
            "mature_outcome_source": (
                "Receipt-matched Strategy exit evidence using exact future leg BBOs, fees, and spreads."
            ),
        },
        "outcome_evidence_status": outcome_status,
        "summary": summary,
        "paper_trades": tracked,
        "safety": {
            "authority": "OBSERVE_ONLY",
            "broker_contact_performed": False,
            "broker_submission_path_present": False,
            "automated_action_allowed": False,
            "orders_enabled": False,
            "orders_placed": 0,
        },
    }
    run = create_timestamp_directory(
        root / "ml" / "loop-c-paper-ledger-runs", timestamp=timestamp
    )
    report_path = run / "ledger.json"
    _write_json_atomic(report_path, report)
    manifest_path = write_manifest(
        run,
        run_timestamp=timestamp,
        input_files=tuple(
            dict.fromkeys((*source_files, *outcome_sources))
        ),
        output_files=(report_path.name,),
        configuration={
            "authority": "OBSERVE_ONLY",
            "asset_class": "OPTIONS_STRATEGY",
            "eligible_horizons": list(LOOP_C_OPTION_SHADOW_HORIZONS),
            "paper_trade_count": len(tracked),
            "automated_action_allowed": False,
            "orders_enabled": False,
            "orders_placed": 0,
        },
        datastore_root=root,
    )
    receipt = {
        "schema_version": LOOP_C_PAPER_LEDGER_RECEIPT_VERSION,
        "run_path": run.relative_to(root).as_posix(),
        "tracked_at": timestamp.isoformat(),
        "status": report["status"],
        "manifest_sha256": file_checksum(manifest_path),
        "ledger_sha256": file_checksum(report_path),
        "safety": report["safety"],
    }
    receipt_path = run / "receipt.json"
    _write_json_atomic(receipt_path, receipt)
    pointer = {
        "schema_version": LOOP_C_PAPER_LEDGER_POINTER_VERSION,
        "current": {
            "run_path": receipt["run_path"],
            "tracked_at": receipt["tracked_at"],
            "status": receipt["status"],
            "receipt_sha256": file_checksum(receipt_path),
        },
    }
    _write_json_atomic(paper_ledger_pointer_path(root), pointer)
    return read_current_paper_ledger(root)


def read_current_paper_ledger(
    datastore_root: Path,
) -> LoopCPaperLedgerPublication:
    root = Path(datastore_root).resolve()
    pointer_path = paper_ledger_pointer_path(root)
    pointer = _read_object(pointer_path, "Loop C paper-ledger pointer")
    current = pointer.get("current")
    if (
        pointer.get("schema_version") != LOOP_C_PAPER_LEDGER_POINTER_VERSION
        or not isinstance(current, Mapping)
    ):
        raise ValueError("Loop C paper-ledger pointer is malformed")
    raw_run = current.get("run_path")
    if not isinstance(raw_run, str) or not raw_run:
        raise ValueError("Loop C paper-ledger pointer has no run path")
    run = (root / raw_run).resolve()
    if run.parent != (root / "ml" / "loop-c-paper-ledger-runs").resolve():
        raise ValueError("Loop C paper-ledger pointer escapes immutable runs")
    verify_manifest(run)
    report_path = run / "ledger.json"
    receipt_path = run / "receipt.json"
    report = _read_object(report_path, "Loop C paper ledger")
    receipt = _read_object(receipt_path, "Loop C paper-ledger receipt")
    safety = report.get("safety")
    expected = {
        "run_path": raw_run,
        "tracked_at": receipt.get("tracked_at"),
        "status": receipt.get("status"),
        "receipt_sha256": file_checksum(receipt_path),
    }
    if (
        dict(current) != expected
        or receipt.get("schema_version") != LOOP_C_PAPER_LEDGER_RECEIPT_VERSION
        or report.get("schema_version") != LOOP_C_PAPER_LEDGER_SCHEMA_VERSION
        or report.get("status") != receipt.get("status")
        or receipt.get("manifest_sha256") != file_checksum(run / "manifest.json")
        or receipt.get("ledger_sha256") != file_checksum(report_path)
        or not isinstance(safety, Mapping)
        or safety.get("authority") != "OBSERVE_ONLY"
        or safety.get("broker_contact_performed") is not False
        or safety.get("broker_submission_path_present") is not False
        or safety.get("automated_action_allowed") is not False
        or safety.get("orders_enabled") is not False
        or int(safety.get("orders_placed", -1)) != 0
        or receipt.get("safety") != safety
    ):
        raise ValueError("Loop C paper-ledger receipt verification failed")
    return LoopCPaperLedgerPublication(run, report_path, receipt_path, report)


def _load_verified_paper_entries(
    root: Path,
    *,
    tracked_at: pd.Timestamp,
) -> tuple[list[dict[str, object]], dict[str, object], tuple[Path, ...]]:
    runs_root = root / "ml" / "loop-c-runs"
    if not runs_root.is_dir():
        return [], {"verified_observe_runs": 0, "no_trade_runs": 0}, ()
    by_id: dict[str, dict[str, object]] = {}
    sources: list[Path] = []
    verified_runs = 0
    no_trade_runs = 0
    duplicate_entries = 0
    for run in sorted(path for path in runs_root.iterdir() if path.is_dir()):
        receipt_path = run / "publication.json"
        if not receipt_path.is_file():
            continue
        manifest = verify_manifest(run)
        receipt = _read_object(receipt_path, "Loop C publication receipt")
        report_path = run / "report.json"
        report = _read_object(report_path, "Loop C observe report")
        decision = report.get("decision")
        configuration = manifest.get("configuration")
        safety = receipt.get("safety")
        report_safety = report.get("safety")
        published_at = _utc(receipt.get("published_at"), "Loop C published_at")
        if published_at > tracked_at:
            continue
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
            raise ValueError(f"Loop C paper source violates zero-order safety: {run}")
        verified_runs += 1
        sources.extend(
            (
                run / "manifest.json",
                receipt_path,
                report_path,
                run / "decisions.parquet",
            )
        )
        if decision.get("action") != "RESEARCH_PROPOSAL":
            no_trade_runs += 1
            continue
        strategy_source = configuration.get("strategy_source")
        if not isinstance(strategy_source, Mapping):
            raise ValueError("Loop C paper source has no exact Strategy binding")
        raw_strategy_run = strategy_source.get("run_path")
        if not isinstance(raw_strategy_run, str) or not raw_strategy_run:
            raise ValueError("Loop C paper source Strategy run path is missing")
        strategy_run = (root / raw_strategy_run).resolve()
        if strategy_run.parent != (root / "ml" / "strategy-runs").resolve():
            raise ValueError("Loop C paper Strategy source escapes immutable runs")
        verify_manifest(strategy_run)
        strategy_receipt_path = strategy_run / "publication.json"
        strategy_receipt = _read_object(
            strategy_receipt_path, "Strategy publication receipt"
        )
        if (
            strategy_receipt.get("schema_version") != STRATEGY_PUBLICATION_VERSION
            or strategy_receipt.get("run_path") != raw_strategy_run
            or strategy_receipt.get("manifest_checksum_sha256")
            != file_checksum(strategy_run / "manifest.json")
        ):
            raise ValueError("Loop C paper Strategy publication failed verification")
        candidates_path = strategy_run / "strategy-candidates.parquet"
        candidates = pd.read_parquet(candidates_path)
        candidate_id = str(decision.get("candidate_id") or "")
        matches = candidates.loc[candidates["id"].astype("string").eq(candidate_id)]
        if len(matches) != 1:
            raise ValueError("Loop C paper decision has no unique exact Strategy candidate")
        entry = build_paper_trade_snapshot(
            matches.iloc[0].to_dict(),
            decision=decision,
            strategy_run_path=raw_strategy_run,
            loop_c_run_path=run.relative_to(root).as_posix(),
        )
        reported = report.get("paper_trade")
        if reported is not None and (
            not isinstance(reported, Mapping) or dict(reported) != entry
        ):
            raise ValueError("Loop C reported paper trade differs from its exact candidate")
        sources.extend(
            (
                strategy_run / "manifest.json",
                strategy_receipt_path,
                candidates_path,
            )
        )
        paper_id = str(entry["paper_trade_id"])
        existing = by_id.get(paper_id)
        if existing is not None:
            duplicate_entries += 1
            if existing != entry:
                raise ValueError("Duplicate Loop C paper identity has conflicting evidence")
            continue
        by_id[paper_id] = entry
    entries = sorted(
        by_id.values(),
        key=lambda row: (
            str(row.get("loop_c_decision_timestamp")),
            str(row.get("paper_trade_id")),
        ),
    )
    return (
        entries,
        {
            "verified_observe_runs": verified_runs,
            "no_trade_runs": no_trade_runs,
            "duplicate_paper_entries_deduplicated": duplicate_entries,
        },
        tuple(dict.fromkeys(sources)),
    )


def _load_outcomes(
    root: Path,
    *,
    tracked_at: pd.Timestamp,
) -> tuple[pd.DataFrame, str, tuple[Path, ...]]:
    report = read_current_strategy_outcome_evidence(root)
    if report is None:
        return pd.DataFrame(), "NOT_PUBLISHED", ()
    evaluated_at = _utc(report.get("evaluated_at"), "Strategy evaluated_at")
    if evaluated_at > tracked_at:
        raise ValueError("Strategy outcome evidence is future-dated")
    pointer_path = root / "ml" / "option-pricing-strategy-latest" / "report.json"
    pointer = _read_object(pointer_path, "Strategy outcome pointer")
    current = pointer.get("current")
    if not isinstance(current, Mapping):
        raise ValueError("Strategy outcome pointer has no current record")
    run = (root / str(current.get("run_path") or "")).resolve()
    allowed = (root / "ml" / "option-pricing-strategy-evidence").resolve()
    if run.parent != allowed:
        raise ValueError("Strategy outcome pointer escapes immutable evidence")
    observations_path = run / "observations.parquet"
    observations = pd.read_parquet(observations_path)
    return (
        observations,
        str(report.get("status") or "UNKNOWN"),
        (
            pointer_path,
            run / "receipt.json",
            run / "report.json",
            observations_path,
        ),
    )


def _attach_outcome(
    entry: Mapping[str, object],
    *,
    outcomes: pd.DataFrame,
    tracked_at: pd.Timestamp,
) -> dict[str, object]:
    output = dict(entry)
    matched: Mapping[str, object] | None = None
    if not outcomes.empty:
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
                "Strategy outcome evidence is missing paper matching fields: "
                + ", ".join(sorted(missing))
            )
        outcome_times = pd.to_datetime(
            outcomes["decision_timestamp"], utc=True, errors="coerce"
        )
        mask = (
            outcomes["strategy_run_path"]
            .astype("string")
            .eq(str(entry.get("strategy_run_path")))
            & outcomes["symbol"]
            .astype("string")
            .str.upper()
            .eq(str(entry.get("symbol")))
            & outcomes["horizon"]
            .astype("string")
            .str.lower()
            .eq(str(entry.get("horizon")))
            & outcomes["candidate_key"]
            .astype("string")
            .eq(str(entry.get("candidate_key")))
            & outcome_times.eq(
                _utc(
                    entry.get("candidate_decision_timestamp"),
                    "paper candidate decision_timestamp",
                )
            )
        )
        matches = outcomes.loc[mask]
        if len(matches) > 1:
            raise ValueError("Strategy outcome evidence duplicated a paper trade")
        if len(matches) == 1:
            matched = matches.iloc[0].to_dict()
    quantity = _positive_integer(entry.get("quantity")) or 0
    realized = _finite(matched.get("realized_net_profit_usd")) if matched else None
    target_end = _utc(entry.get("target_window_end"), "paper target_window_end")
    if realized is not None:
        lifecycle_status = "MATURE_RECEIPT_MATCHED"
    elif tracked_at <= target_end:
        lifecycle_status = "OPEN_PENDING_TARGET"
    else:
        lifecycle_status = "PENDING_MATURE_OUTCOME_EVIDENCE"
    output["tracking"] = {
        "tracked_at": tracked_at.isoformat(),
        "lifecycle_status": lifecycle_status,
        "mature_outcome_available": realized is not None,
        "realized_net_profit_per_strategy": realized,
        "counterfactual_realized_net_pnl": _scaled(realized, quantity),
        "exit_available_at": (
            _optional_iso_utc(matched.get("exit_available_at"))
            if matched
            else None
        ),
        "outcome_policy_version": (
            _optional_text(matched.get("outcome_policy_version"))
            if matched
            else None
        ),
        "round_trip_contract_fees_per_strategy": (
            _finite(matched.get("round_trip_contract_fees_usd"))
            if matched
            else None
        ),
        "total_bid_ask_spread_per_strategy": (
            _finite(matched.get("total_bid_ask_spread_usd"))
            if matched
            else None
        ),
        "attribution": "LOOP_C_COUNTERFACTUAL_NOT_BROKER_EXECUTION",
    }
    return output


def _summarize(
    entries: Sequence[Mapping[str, object]],
    *,
    run_summary: Mapping[str, object],
) -> dict[str, object]:
    statuses: Counter[str] = Counter()
    horizons: Counter[str] = Counter()
    symbols: Counter[str] = Counter()
    families: Counter[str] = Counter()
    clusters: set[str] = set()
    mature_values: list[float] = []
    open_gross_share_obligation = 0.0
    open_buy_share_obligation = 0.0
    open_sell_share_obligation = 0.0
    open_trade_obligations: list[float] = []
    open_expirations: list[pd.Timestamp] = []
    per_horizon_values: dict[str, list[float]] = {
        horizon: [] for horizon in LOOP_C_OPTION_SHADOW_HORIZONS
    }
    for entry in entries:
        tracking = entry.get("tracking")
        tracking_row = tracking if isinstance(tracking, Mapping) else {}
        status = str(tracking_row.get("lifecycle_status") or "UNKNOWN")
        horizon = str(entry.get("horizon") or "UNKNOWN")
        statuses[status] += 1
        horizons[horizon] += 1
        symbols[str(entry.get("symbol") or "UNKNOWN")] += 1
        families[str(entry.get("strategy_family") or "UNKNOWN")] += 1
        clusters.add(str(entry.get("independent_decision_cluster_id") or "MISSING"))
        assignment = entry.get("expiration_and_assignment")
        assignment_row = assignment if isinstance(assignment, Mapping) else {}
        if status == "OPEN_PENDING_TARGET":
            gross = _finite(
                assignment_row.get("gross_potential_share_obligation_total")
            ) or 0.0
            open_gross_share_obligation += gross
            open_trade_obligations.append(gross)
            expiration = pd.to_datetime(
                assignment_row.get("earliest_option_expiration"),
                utc=True,
                errors="coerce",
            )
            if not pd.isna(expiration):
                open_expirations.append(pd.Timestamp(expiration))
            obligations = assignment_row.get("option_leg_obligations")
            for obligation in obligations if isinstance(obligations, list) else []:
                if not isinstance(obligation, Mapping):
                    continue
                signed = _finite(
                    obligation.get("potential_signed_share_change_total")
                ) or 0.0
                if signed > 0.0:
                    open_buy_share_obligation += signed
                elif signed < 0.0:
                    open_sell_share_obligation += abs(signed)
        value = _finite(tracking_row.get("counterfactual_realized_net_pnl"))
        if value is not None:
            mature_values.append(value)
            per_horizon_values.setdefault(horizon, []).append(value)
    by_horizon = {
        horizon: {
            "paper_trade_count": horizons.get(horizon, 0),
            "mature_trade_count": len(per_horizon_values.get(horizon, ())),
            "counterfactual_realized_net_pnl": _money(
                sum(per_horizon_values.get(horizon, ()))
            ),
        }
        for horizon in LOOP_C_OPTION_SHADOW_HORIZONS
    }
    return {
        **dict(run_summary),
        "paper_trade_count": len(entries),
        "independent_decision_cluster_count": len(clusters) if entries else 0,
        "lifecycle_status_counts": dict(sorted(statuses.items())),
        "horizon_counts": dict(sorted(horizons.items())),
        "symbol_counts": dict(sorted(symbols.items())),
        "strategy_family_counts": dict(sorted(families.items())),
        "mature_trade_count": len(mature_values),
        "pending_trade_count": len(entries) - len(mature_values),
        "open_paper_trade_count": statuses.get("OPEN_PENDING_TARGET", 0),
        "open_gross_potential_share_obligation": _money(
            open_gross_share_obligation
        ),
        "maximum_single_open_trade_gross_share_obligation": _money(
            max(open_trade_obligations, default=0.0)
        ),
        "open_potential_buy_share_obligation": _money(
            open_buy_share_obligation
        ),
        "open_potential_sell_share_obligation": _money(
            open_sell_share_obligation
        ),
        "earliest_open_option_expiration": (
            min(open_expirations).isoformat() if open_expirations else None
        ),
        "future_live_exit_buffer_required": True,
        "wins": sum(value > 0.0 for value in mature_values),
        "losses": sum(value < 0.0 for value in mature_values),
        "breakeven": sum(value == 0.0 for value in mature_values),
        "counterfactual_realized_net_pnl": _money(sum(mature_values)),
        "gross_counterfactual_profit": _money(
            sum(value for value in mature_values if value > 0.0)
        ),
        "gross_counterfactual_loss": _money(
            sum(value for value in mature_values if value < 0.0)
        ),
        "by_horizon": by_horizon,
        "orders_placed": 0,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Track exact Loop C Options Strategy paper trades."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--root-dir", type=Path)
    group.add_argument("--datastore-target", choices=sorted(DATASTORE_TARGETS))
    parser.add_argument("--tracked-at")
    parser.add_argument("--compact", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = resolve_datastore_dir(
            root_dir=args.root_dir, target=args.datastore_target
        )
        with exclusive_runtime_lock(
            root / "locks" / "loop-c-options-paper-ledger.lock",
            process_name="Loop C Options Strategy paper ledger",
        ):
            result = track_options_strategy_paper_trades(
                root, tracked_at=args.tracked_at
            )
        summary = result.report.get("summary")
        summary_row = summary if isinstance(summary, Mapping) else {}
        output = {
            "status": result.report.get("status"),
            "run_directory": str(result.run_directory),
            "ledger": str(result.report_path),
            "receipt": str(result.receipt_path),
            "paper_trade_count": summary_row.get("paper_trade_count", 0),
            "mature_trade_count": summary_row.get("mature_trade_count", 0),
            "pending_trade_count": summary_row.get("pending_trade_count", 0),
            "authority": "OBSERVE_ONLY",
            "orders_enabled": False,
            "orders_placed": 0,
        }
        exit_code = 0
    except Exception as exc:
        output = {
            "status": "ERROR",
            "error": f"{type(exc).__name__}: {exc}",
            "authority": "NONE",
            "orders_enabled": False,
            "orders_placed": 0,
        }
        exit_code = 2
    print(json.dumps(output, separators=(",", ":") if args.compact else None))
    return exit_code


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    )


def _json_mapping(value: Mapping[str, object]) -> dict[str, object]:
    return {str(key): _json_value(item) for key, item in value.items()}


def _json_value(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return _json_mapping(value)
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            return _json_value(value.item())
        except (TypeError, ValueError):
            pass
    return str(value)


def _finite(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _positive_integer(value: object) -> int | None:
    parsed = _finite(value)
    if parsed is None or parsed < 1.0 or not parsed.is_integer():
        return None
    return int(parsed)


def _nonnegative_integer(value: object) -> int | None:
    parsed = _finite(value)
    if parsed is None or parsed < 0.0 or not parsed.is_integer():
        return None
    return int(parsed)


def _scaled(value: float | None, quantity: int) -> float | None:
    return value * quantity if value is not None else None


def _money(value: object) -> float:
    parsed = _finite(value)
    return round((parsed or 0.0) + 1.0e-12, 2)


def _optional_text(value: object) -> str | None:
    rendered = str(value or "").strip()
    return rendered or None


def _utc(value: object, label: str) -> pd.Timestamp:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"{label} is invalid")
    return pd.Timestamp(parsed)


def _iso_utc(value: object, label: str) -> str:
    return _utc(value, label).isoformat()


def _optional_iso_utc(value: object) -> str | None:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    return None if pd.isna(parsed) else pd.Timestamp(parsed).isoformat()


def _read_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


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


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "LOOP_C_PAPER_LEDGER_SCHEMA_VERSION",
    "LoopCPaperLedgerPublication",
    "build_paper_trade_snapshot",
    "main",
    "paper_ledger_pointer_path",
    "paper_candidate_has_bounded_exit",
    "read_current_paper_ledger",
    "track_options_strategy_paper_trades",
]
