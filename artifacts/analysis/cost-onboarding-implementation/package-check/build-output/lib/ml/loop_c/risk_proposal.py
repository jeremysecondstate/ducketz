from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import asdict
from pathlib import Path
from typing import Mapping, Sequence

from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from ml.artifacts import create_timestamp_directory, file_checksum, utc_timestamp, write_manifest
from ml.loop_c.inputs import (
    LOOP_C_APPROVAL_SCOPE,
    LOOP_C_BROKER_SNAPSHOT_SCHEMA_VERSION,
    LOOP_C_HALT_CONTROL_SCHEMA_VERSION,
    LOOP_C_PORTFOLIO_SNAPSHOT_SCHEMA_VERSION,
    LOOP_C_RISK_APPROVAL_SCHEMA_VERSION,
)
from ml.loop_c.policy import (
    LOOP_C_POLICY_VERSION,
    LoopCPredictiveThresholds,
    LoopCRiskLimits,
    expected_sequence_model_binding,
)


LOOP_C_RISK_PROPOSAL_SCHEMA_VERSION = "loop-c-equity-risk-proposal-v1"
LOOP_C_RISK_CALCULUS_VERSION = "loop-c-account-equity-calculus-v1"


def build_pending_risk_proposal(
    datastore_root: Path,
    *,
    as_of: object | None = None,
    portfolio_snapshot_path: Path | None = None,
    broker_snapshot_path: Path | None = None,
) -> dict[str, object]:
    """Build, but never approve, a risk proposal from verified read-only state."""

    root = Path(datastore_root).resolve()
    timestamp = utc_timestamp(as_of)
    current = root / "controls" / "loop-c" / "current"
    portfolio_path = Path(portfolio_snapshot_path or current / "portfolio-snapshot.json")
    broker_path = Path(broker_snapshot_path or current / "broker-snapshot.json")
    portfolio = _read_object(portfolio_path, "portfolio snapshot")
    broker = _read_object(broker_path, "broker snapshot")
    if portfolio.get("schema_version") != LOOP_C_PORTFOLIO_SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("Pending risk proposal requires the current Loop C portfolio schema")
    if broker.get("schema_version") != LOOP_C_BROKER_SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("Pending risk proposal requires the current Loop C broker schema")
    if portfolio.get("reconciled") is not True or broker.get("reconciled") is not True:
        raise ValueError("Pending risk proposal requires reconciled read-only Schwab state")
    if portfolio.get("observed_at") != broker.get("observed_at"):
        raise ValueError("Portfolio and broker snapshots are not from one atomic observation")

    receipt_path, receipt = _source_receipt(root, portfolio, broker)
    history_path = _history_path(root, receipt)
    history = _read_object(history_path, "trade-history summary")
    equity = _positive(portfolio.get("account_equity"), "account equity")
    gross = _nonnegative(portfolio.get("gross_exposure"), "gross exposure")
    available = _nonnegative(portfolio.get("available_cash"), "available cash")
    open_positions = _nonnegative_integer(
        portfolio.get("open_positions"), "open positions"
    )
    working_orders = _nonnegative_integer(
        broker.get("working_orders"), "working orders"
    )
    reserved_cash = _nonnegative(broker.get("reserved_cash"), "reserved cash")

    history_multiplier, history_reasons = _history_risk_multiplier(history, equity)
    base_trade_loss = equity * 0.0025
    effective_trade_loss = base_trade_loss * history_multiplier
    maximum_gross_exposure = max(gross, equity * 1.30)
    limits = LoopCRiskLimits(
        maximum_snapshot_age_seconds=300.0,
        maximum_model_age_seconds=5_400.0,
        maximum_daily_loss=_money(equity * 0.0050),
        maximum_gross_exposure=_money(maximum_gross_exposure),
        maximum_symbol_exposure=_money(equity * 0.15),
        maximum_trade_loss=_money(effective_trade_loss),
        maximum_open_positions=max(open_positions + 1, 1),
        maximum_working_orders=max(1, working_orders + 1),
        maximum_candidate_quantity=1,
        predictive_thresholds_by_horizon=_initial_predictive_thresholds(),
        policy_version=LOOP_C_POLICY_VERSION,
    )
    binding = expected_sequence_model_binding()
    approval_id = f"pending-{timestamp.strftime('%Y%m%dT%H%M%SZ').lower()}"
    risk_approval = {
        "schema_version": LOOP_C_RISK_APPROVAL_SCHEMA_VERSION,
        "approval": {
            "status": "PENDING_OPERATOR_APPROVAL",
            "approval_id": approval_id,
            "approved_by": None,
            "approved_at": None,
            "expires_at": None,
            "scope": LOOP_C_APPROVAL_SCOPE,
            "rationale": None,
        },
        "model_binding": {
            **asdict(binding),
            "horizons": list(binding.horizons),
        },
        "limits": asdict(limits),
    }
    calculus = {
        "schema_version": LOOP_C_RISK_CALCULUS_VERSION,
        "status": "PENDING_OPERATOR_APPROVAL",
        "calculated_at": timestamp.isoformat(),
        "source": {
            "portfolio_snapshot": str(portfolio_path.resolve()),
            "portfolio_snapshot_sha256": file_checksum(portfolio_path),
            "broker_snapshot": str(broker_path.resolve()),
            "broker_snapshot_sha256": file_checksum(broker_path),
            "schwab_receipt": str(receipt_path),
            "schwab_receipt_sha256": file_checksum(receipt_path),
            "trade_history_summary": str(history_path),
            "trade_history_summary_sha256": file_checksum(history_path),
        },
        "observed_account_state": {
            "account_equity": equity,
            "gross_exposure": gross,
            "available_cash_after_working_order_reserves": available,
            "available_cash_source": portfolio.get("available_cash_source"),
            "open_positions": open_positions,
            "working_orders": working_orders,
            "reserved_cash": reserved_cash,
            "daily_pnl": portfolio.get("daily_pnl"),
        },
        "portfolio_limit_formulas": {
            "maximum_daily_loss": "account_equity * 0.0050",
            "base_maximum_trade_loss": "account_equity * 0.0025",
            "history_multiplier": (
                "0.50 for severe account-context loss/drawdown/streak; "
                "0.75 for moderate context; otherwise 1.00; never above 1.00"
            ),
            "maximum_trade_loss": "base_maximum_trade_loss * history_multiplier",
            "maximum_gross_exposure": "max(current_gross_exposure, account_equity * 1.30)",
            "maximum_symbol_exposure": "account_equity * 0.15",
            "maximum_open_positions": "current_open_positions + 1",
            "maximum_working_orders": "max(1, current_working_orders + 1)",
            "maximum_candidate_quantity": "1 during observe-only pilot",
        },
        "resolved_portfolio_limits": {
            "daily_loss_fraction": 0.0050,
            "base_trade_loss_fraction": 0.0025,
            "base_maximum_trade_loss": _money(base_trade_loss),
            "history_multiplier": history_multiplier,
            "history_multiplier_reasons": history_reasons,
            "maximum_trade_loss": limits.maximum_trade_loss,
            "maximum_daily_loss": limits.maximum_daily_loss,
            "maximum_gross_exposure": limits.maximum_gross_exposure,
            "gross_headroom": _money(maximum_gross_exposure - gross),
            "maximum_symbol_exposure": limits.maximum_symbol_exposure,
            "maximum_open_positions": limits.maximum_open_positions,
            "maximum_working_orders": limits.maximum_working_orders,
            "maximum_candidate_quantity": limits.maximum_candidate_quantity,
        },
        "candidate_calculus": {
            "hard_gates": [
                "fresh exact-bound shadow sequence publication",
                "open XNYS session",
                "fresh reconciled account and working-order snapshots",
                "unexpired explicit observe-only risk approval",
                "unexpired halt control with halt_requested=false",
                "daily loss, total gross, per-symbol, position-count, and working-order limits",
                "horizon-specific Strategy probability, sequence direction, expected-return-on-risk, and uncertainty gates",
            ],
            "quantity": (
                "floor(min(max_trade_loss/loss_per_unit, available_cash/capital_per_unit, "
                "gross_headroom/capital_per_unit, symbol_headroom/capital_per_unit, "
                "maximum_candidate_quantity))"
            ),
            "expected_utility": (
                "expected_return_on_risk - horizon_uncertainty_penalty * total_uncertainty"
            ),
            "selection": "highest positive expected_utility; deterministic candidate id breaks ties",
            "modeled_loss": "loss_per_unit * proposed_quantity",
            "stop_loss_policy": (
                "NOT_CONFIGURED: observe-only records modeled maximum loss and does not create, "
                "stage, submit, replace, or cancel any stop or other broker order"
            ),
        },
        "history_governance": {
            "current_attribution": history.get("attribution"),
            "current_history_status": history.get("status"),
            "usable_to_increase_risk": False,
            "usable_to_claim_loop_c_effectiveness": False,
            "automatic_adjustment": "DOWNWARD_ONLY_IN_THIS_PENDING_PROPOSAL",
            "future_changes": (
                "After prospective Loop C outcomes mature, any threshold or risk increase requires "
                "a preregistered cohort-level comparison, immutable before/after evidence, rollback "
                "criteria, and a new explicit operator approval. No per-trade reactive tuning."
            ),
        },
        "predictive_threshold_governance": {
            "status": "PREDECLARED_OBSERVE_ONLY_SEED_VALUES",
            "values": asdict(limits)["predictive_thresholds_by_horizon"],
            "rule": (
                "These horizon-specific seed gates may filter research observations immediately "
                "after approval, but they gain no trading authority and cannot be silently retuned."
            ),
        },
        "safety": {
            "authority": "PROPOSAL_ONLY",
            "automated_action_allowed": False,
            "orders_enabled": False,
            "orders_placed": 0,
        },
    }
    proposal = {
        "schema_version": LOOP_C_RISK_PROPOSAL_SCHEMA_VERSION,
        "status": "PENDING_OPERATOR_APPROVAL",
        "proposal_id": approval_id,
        "calculated_at": timestamp.isoformat(),
        "risk_approval": risk_approval,
        "calculus": calculus,
        "operator_actions_required": [
            "Review every model-binding, portfolio-limit, and horizon-threshold value.",
            "Choose an approval identity, approval timestamp, expiration, and rationale.",
            "Issue a separate unexpired halt control; do not infer unhalt from this proposal.",
            "Copy an explicitly approved risk record to controls/loop-c/current/risk-approval.json.",
        ],
        "safety": {
            "authority": "PROPOSAL_ONLY",
            "automated_action_allowed": False,
            "orders_enabled": False,
            "orders_placed": 0,
        },
    }
    run = create_timestamp_directory(
        root / "controls" / "loop-c" / "proposals",
        timestamp=timestamp,
    )
    proposal_path = run / "proposal.json"
    approval_path = run / "risk-approval.pending.json"
    calculus_path = run / "calculus.json"
    halt_path = run / "halt-control.pending.json"
    halt_control = {
        "schema_version": LOOP_C_HALT_CONTROL_SCHEMA_VERSION,
        "control_id": f"halt-{timestamp.strftime('%Y%m%dT%H%M%SZ').lower()}",
        "issued_at": None,
        "expires_at": None,
        "halt_requested": True,
        "set_by": None,
    }
    _write_json_atomic(approval_path, risk_approval)
    _write_json_atomic(calculus_path, calculus)
    _write_json_atomic(halt_path, halt_control)
    _write_json_atomic(proposal_path, proposal)
    write_manifest(
        run,
        run_timestamp=timestamp,
        input_files=(portfolio_path, broker_path, receipt_path, history_path),
        output_files=(
            approval_path.name,
            calculus_path.name,
            halt_path.name,
            proposal_path.name,
        ),
        configuration={
            "schema_version": LOOP_C_RISK_PROPOSAL_SCHEMA_VERSION,
            "status": "PENDING_OPERATOR_APPROVAL",
            "model_configuration_fingerprint": binding.configuration_fingerprint,
            "authority": "PROPOSAL_ONLY",
            "orders_enabled": False,
            "orders_placed": 0,
        },
        datastore_root=root,
    )
    pointer = root / "controls" / "loop-c" / "latest-proposal" / "run.json"
    _write_json_atomic(
        pointer,
        {
            "schema_version": "loop-c-risk-proposal-pointer-v1",
            "current": {
                "run_path": run.relative_to(root).as_posix(),
                "proposal_sha256": file_checksum(proposal_path),
                "status": "PENDING_OPERATOR_APPROVAL",
            },
        },
    )
    return {
        "status": "PENDING_OPERATOR_APPROVAL",
        "run_directory": str(run),
        "proposal_path": str(proposal_path),
        "risk_approval_path": str(approval_path),
        "calculus_path": str(calculus_path),
        "halt_control_path": str(halt_path),
        "model_configuration_fingerprint": binding.configuration_fingerprint,
        "resolved_limits": calculus["resolved_portfolio_limits"],
        "safety": proposal["safety"],
    }


def _initial_predictive_thresholds() -> Mapping[str, LoopCPredictiveThresholds]:
    return {
        "1h": LoopCPredictiveThresholds(0.60, 0.56, 0.03, 0.020, 1.25),
        "4h": LoopCPredictiveThresholds(0.60, 0.57, 0.04, 0.035, 1.35),
        "1d": LoopCPredictiveThresholds(0.62, 0.58, 0.06, 0.060, 1.50),
        "1w": LoopCPredictiveThresholds(0.65, 0.60, 0.10, 0.120, 1.75),
    }


def _history_risk_multiplier(
    history: Mapping[str, object],
    equity: float,
) -> tuple[float, list[str]]:
    performance = history.get("performance")
    if not isinstance(performance, Mapping):
        return 0.50, ["History is unavailable; the fail-closed proposal uses 0.50."]
    count = _optional_nonnegative_integer(performance.get("included_closed_positions"))
    net = _optional_number(performance.get("net_realized_pnl"))
    drawdown = _optional_number(performance.get("maximum_closed_pnl_drawdown"))
    loss_streak = _optional_nonnegative_integer(performance.get("maximum_loss_streak"))
    reasons: list[str] = []
    if count is None or count < 12:
        reasons.append("Fewer than 12 reconstructed closed positions; context is immature.")
    if net is None or net < 0.0:
        reasons.append("Observed account-context net realized P/L is negative or unavailable.")
    drawdown_fraction = drawdown / equity if drawdown is not None else None
    if drawdown_fraction is None or drawdown_fraction >= 0.03:
        reasons.append("Observed closed-P/L drawdown is unavailable or at least 3% of equity.")
    if loss_streak is None or loss_streak >= 5:
        reasons.append("Observed maximum loss streak is unavailable or at least five.")
    severe = (
        net is None
        or net < 0.0
        or drawdown_fraction is None
        or drawdown_fraction >= 0.03
        or loss_streak is None
        or loss_streak >= 5
    )
    moderate = count is None or count < 30 or (drawdown_fraction or 0.0) >= 0.015
    return (0.50 if severe else 0.75 if moderate else 1.00), reasons or [
        "No account-context drawdown throttle was triggered; multiplier remains capped at 1.00."
    ]


def _source_receipt(
    root: Path,
    portfolio: Mapping[str, object],
    broker: Mapping[str, object],
) -> tuple[Path, Mapping[str, object]]:
    left = portfolio.get("source")
    right = broker.get("source")
    if not isinstance(left, Mapping) or not isinstance(right, Mapping) or dict(left) != dict(right):
        raise ValueError("Portfolio and broker snapshots do not share one source receipt")
    raw_path = left.get("receipt_path")
    raw_checksum = left.get("receipt_sha256")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("Schwab source receipt path is missing")
    path = (root / raw_path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Schwab source receipt is missing or escapes the datastore")
    if raw_checksum != file_checksum(path):
        raise ValueError("Schwab source receipt checksum changed")
    receipt = _read_object(path, "Schwab source receipt")
    return path, receipt


def _history_path(root: Path, receipt: Mapping[str, object]) -> Path:
    raw_run = receipt.get("run_path")
    if not isinstance(raw_run, str) or not raw_run:
        raise ValueError("Schwab receipt run_path is missing")
    path = (root / raw_run / "trade-history-summary.json").resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Trade-history summary is missing or escapes the datastore")
    if receipt.get("history_sha256") != file_checksum(path):
        raise ValueError("Trade-history summary checksum changed")
    return path


def _read_object(path: Path, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _positive(value: object, label: str) -> float:
    parsed = _number(value, label)
    if parsed <= 0.0:
        raise ValueError(f"{label} must be positive")
    return parsed


def _nonnegative(value: object, label: str) -> float:
    parsed = _number(value, label)
    if parsed < 0.0:
        raise ValueError(f"{label} cannot be negative")
    return parsed


def _number(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{label} must be finite")
    return parsed


def _nonnegative_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _optional_number(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _optional_nonnegative_integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _money(value: float) -> float:
    return round(float(value) + 1.0e-12, 2)


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + f".tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a pending, never self-approved Loop C risk proposal."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--root-dir", type=Path)
    group.add_argument("--datastore-target", choices=sorted(DATASTORE_TARGETS))
    parser.add_argument("--as-of")
    parser.add_argument("--portfolio-snapshot", type=Path)
    parser.add_argument("--broker-snapshot", type=Path)
    parser.add_argument("--compact", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = resolve_datastore_dir(root_dir=args.root_dir, target=args.datastore_target)
        result = build_pending_risk_proposal(
            root,
            as_of=args.as_of,
            portfolio_snapshot_path=args.portfolio_snapshot,
            broker_snapshot_path=args.broker_snapshot,
        )
        exit_code = 0
    except Exception as exc:
        result = {
            "status": "ERROR",
            "error": f"{type(exc).__name__}: {exc}",
            "orders_enabled": False,
            "orders_placed": 0,
        }
        exit_code = 2
    print(json.dumps(result, separators=(",", ":") if args.compact else None))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "LOOP_C_RISK_CALCULUS_VERSION",
    "LOOP_C_RISK_PROPOSAL_SCHEMA_VERSION",
    "build_pending_risk_proposal",
    "main",
]
