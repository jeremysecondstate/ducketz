from __future__ import annotations

import argparse
import json
import math
import os
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from app.models.past_positions import ClosedPosition, PositionOutcome
from app.services.schwab import (
    SchwabSession,
    _net_quantity as _schwab_net_quantity,
    _schwab_day_pnl,
    _securities_account,
)
from app.services.schwab_past_positions import (
    SchwabPastPositionsService,
    performance_summary,
)
from app.services.schwab_policy_inputs import (
    SCHWAB_POLICY_INPUTS_SCHEMA_VERSION,
    normalize_schwab_policy_inputs,
)
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.artifacts import (
    create_timestamp_directory,
    file_checksum,
    utc_timestamp,
    write_manifest,
)
from ml.loop_c.inputs import (
    BROKER_SOURCE_POLICY_VERSION,
    LOOP_C_BROKER_SNAPSHOT_SCHEMA_VERSION,
    LOOP_C_PORTFOLIO_SNAPSHOT_SCHEMA_VERSION,
    PORTFOLIO_SOURCE_POLICY_VERSION,
)


LOOP_C_SCHWAB_FACTS_SCHEMA_VERSION = "loop-c-schwab-sanitized-facts-v1"
LOOP_C_TRADE_HISTORY_SCHEMA_VERSION = "loop-c-account-trade-history-summary-v1"
LOOP_C_SCHWAB_RECEIPT_SCHEMA_VERSION = "loop-c-schwab-read-only-receipt-v1"
LOOP_C_SCHWAB_POINTER_SCHEMA_VERSION = "loop-c-schwab-read-only-pointer-v1"
LOOP_C_SCHWAB_SOURCE_POLICY_VERSION = "loop-c-schwab-read-only-snapshot-v1"


@dataclass(frozen=True)
class SchwabReadOnlySnapshotResult:
    run_directory: Path
    receipt_path: Path
    portfolio_snapshot_path: Path
    broker_snapshot_path: Path
    history_summary_path: Path
    observed_at: pd.Timestamp
    reconciled: bool
    history_status: str


def capture_schwab_read_only_state(
    datastore_root: Path,
    *,
    observed_at: object | None = None,
    history_start: date | None = None,
    review_period_start: date | None = None,
    session_factory: Callable[[], object] = SchwabSession,
) -> SchwabReadOnlySnapshotResult:
    """Capture GET-only Schwab evidence and publish identifier-free Loop C inputs."""

    root = Path(datastore_root).resolve()
    observed = utc_timestamp(observed_at)
    local_date = observed.tz_convert("America/Los_Angeles").date()
    start = history_start or date(local_date.year, 1, 1)
    if start > local_date:
        raise ValueError("Schwab history_start cannot be after the observed date")
    if review_period_start is not None and not start <= review_period_start <= local_date:
        raise ValueError(
            "Schwab review_period_start must be within the requested history range"
        )

    session = session_factory()
    account_payload = _required_call(session, "get_account")
    orders_payload = _required_call(session, "get_open_orders")
    if not isinstance(orders_payload, list):
        raise RuntimeError("Schwab working-order read returned a non-list payload")
    normalized = normalize_schwab_policy_inputs(
        account_payload,
        orders_payload,
        observed_at=observed.to_pydatetime(),
    )
    portfolio, broker, sanitized = _current_state(
        normalized,
        account_payload=account_payload,
    )

    history, history_status = _history_context(
        session,
        start=start,
        end=local_date,
        observed_at=observed,
        review_period_start=review_period_start,
    )
    portfolio["trade_history_status"] = history_status
    sanitized["trade_history_status"] = history_status

    run = create_timestamp_directory(
        root / "accounts" / "schwab" / "loop-c-read-only-runs",
        timestamp=observed,
    )
    facts_path = run / "sanitized-account-facts.json"
    history_path = run / "trade-history-summary.json"
    _write_json_atomic(facts_path, sanitized)
    _write_json_atomic(history_path, history)
    manifest_path = write_manifest(
        run,
        run_timestamp=observed,
        input_files=(),
        output_files=(facts_path.name, history_path.name),
        configuration={
            "authority": "OBSERVED_READ_ONLY",
            "source_policy_version": LOOP_C_SCHWAB_SOURCE_POLICY_VERSION,
            "upstream_policy_version": SCHWAB_POLICY_INPUTS_SCHEMA_VERSION,
            "broker_data_http_methods": ["GET"],
            "orders_enabled": False,
            "orders_placed": 0,
            "account_identifiers_persisted": False,
            "raw_order_or_transaction_identifiers_persisted": False,
            "history_start": start.isoformat(),
            "history_end": local_date.isoformat(),
            "review_period_start": (
                review_period_start.isoformat()
                if review_period_start is not None
                else None
            ),
        },
        datastore_root=root,
    )
    receipt = {
        "schema_version": LOOP_C_SCHWAB_RECEIPT_SCHEMA_VERSION,
        "authority": "OBSERVED_READ_ONLY",
        "source_policy_version": LOOP_C_SCHWAB_SOURCE_POLICY_VERSION,
        "observed_at": observed.isoformat(),
        "run_path": run.relative_to(root).as_posix(),
        "manifest_sha256": file_checksum(manifest_path),
        "facts_sha256": file_checksum(facts_path),
        "history_sha256": file_checksum(history_path),
        "portfolio_reconciled": bool(portfolio["reconciled"]),
        "broker_reconciled": bool(broker["reconciled"]),
        "history_status": history_status,
        "sanitization": {
            "account_identifiers_persisted": False,
            "raw_order_identifiers_persisted": False,
            "raw_transaction_identifiers_persisted": False,
            "oauth_tokens_persisted": False,
        },
        "safety": {
            "broker_data_http_methods": ["GET"],
            "order_submission_called": False,
            "order_replacement_called": False,
            "order_cancellation_called": False,
            "orders_enabled": False,
            "orders_placed": 0,
        },
    }
    receipt_path = run / "receipt.json"
    _write_json_atomic(receipt_path, receipt)
    source = {
        "policy_version": LOOP_C_SCHWAB_SOURCE_POLICY_VERSION,
        "receipt_path": receipt_path.relative_to(root).as_posix(),
        "receipt_sha256": file_checksum(receipt_path),
    }
    portfolio_snapshot = {
        "schema_version": LOOP_C_PORTFOLIO_SNAPSHOT_SCHEMA_VERSION,
        "authority": "OBSERVED_READ_ONLY",
        "observed_at": observed.isoformat(),
        **portfolio,
        "source": source,
    }
    broker_snapshot = {
        "schema_version": LOOP_C_BROKER_SNAPSHOT_SCHEMA_VERSION,
        "authority": "OBSERVED_READ_ONLY",
        "observed_at": observed.isoformat(),
        **broker,
        "source": source,
    }
    current = root / "controls" / "loop-c" / "current"
    portfolio_path = current / "portfolio-snapshot.json"
    broker_path = current / "broker-snapshot.json"
    _write_json_atomic(portfolio_path, portfolio_snapshot)
    _write_json_atomic(broker_path, broker_snapshot)
    pointer_path = root / "accounts" / "schwab" / "loop-c-read-only-latest" / "run.json"
    _write_json_atomic(
        pointer_path,
        {
            "schema_version": LOOP_C_SCHWAB_POINTER_SCHEMA_VERSION,
            "current": {
                "run_path": run.relative_to(root).as_posix(),
                "observed_at": observed.isoformat(),
                "receipt_sha256": file_checksum(receipt_path),
            },
        },
    )
    return SchwabReadOnlySnapshotResult(
        run_directory=run,
        receipt_path=receipt_path,
        portfolio_snapshot_path=portfolio_path,
        broker_snapshot_path=broker_path,
        history_summary_path=history_path,
        observed_at=observed,
        reconciled=bool(portfolio["reconciled"] and broker["reconciled"]),
        history_status=history_status,
    )


def _current_state(
    normalized: Mapping[str, object],
    *,
    account_payload: object,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    account_values = _mapping(normalized.get("account_values"), "account_values")
    positions = _mapping(normalized.get("positions"), "positions")
    working = _mapping(normalized.get("working_orders"), "working_orders")
    raw_positions = positions.get("items")
    raw_orders = working.get("items")
    if not isinstance(raw_positions, list) or not isinstance(raw_orders, list):
        raise RuntimeError("Normalized Schwab positions or working orders are unavailable")

    equity = _positive(account_values.get("liquidation_value"))
    reserved_cash = _nonnegative(working.get("reserved_cash"))
    available_cash, available_source = _available_capital(account_values, reserved_cash)
    symbol_exposure: defaultdict[str, float] = defaultdict(float)
    sanitized_positions: list[dict[str, object]] = []
    exposure_complete = True
    daily_pnl_complete = True
    gross_exposure = 0.0
    daily_pnl = 0.0
    open_positions = 0
    account = _securities_account(account_payload)
    schwab_position_rows = account.get("positions")
    if not isinstance(schwab_position_rows, list):
        schwab_position_rows = []
    for index, raw in enumerate(raw_positions):
        if not isinstance(raw, Mapping):
            exposure_complete = False
            continue
        row = _sanitize_position(raw)
        if index < len(schwab_position_rows) and isinstance(
            schwab_position_rows[index], dict
        ):
            source_row = schwab_position_rows[index]
            row["day_pnl"] = _schwab_day_pnl(
                source_row,
                _schwab_net_quantity(source_row),
            )
        sanitized_positions.append(row)
        symbol = str(row.get("underlying_symbol") or row.get("symbol") or "").strip().upper()
        market_value = _finite(row.get("market_value"))
        quantity = _finite(row.get("net_quantity"))
        row_daily_pnl = _finite(row.get("day_pnl"))
        if not symbol or market_value is None or quantity is None:
            exposure_complete = False
        else:
            absolute_value = abs(market_value)
            gross_exposure += absolute_value
            symbol_exposure[symbol] += absolute_value
            if abs(quantity) > 1.0e-8:
                open_positions += 1
        if row_daily_pnl is None:
            daily_pnl_complete = False
        else:
            daily_pnl += row_daily_pnl

    positions_complete = (
        positions.get("stock_policy_row_set_complete") is True
        and positions.get("option_row_set_complete") is True
        and exposure_complete
        and daily_pnl_complete
    )
    broker_reconciled = working.get("status") == "CURRENT" and reserved_cash is not None
    portfolio_reconciled = (
        equity is not None
        and available_cash is not None
        and positions_complete
        and broker_reconciled
    )
    sanitized_orders = [
        _sanitize_order(row) for row in raw_orders if isinstance(row, Mapping)
    ]
    portfolio = {
        "reconciled": portfolio_reconciled,
        "account_equity": _rounded(equity, 2),
        "daily_pnl": _rounded(daily_pnl, 2) if daily_pnl_complete else None,
        "gross_exposure": _rounded(gross_exposure, 2) if exposure_complete else None,
        "symbol_exposure": {
            symbol: _rounded(value, 2)
            for symbol, value in sorted(symbol_exposure.items())
        },
        "open_positions": open_positions if exposure_complete else None,
        "available_cash": _rounded(available_cash, 2),
        "available_cash_source": available_source,
        "trade_history_status": "PENDING",
    }
    broker = {
        "reconciled": broker_reconciled,
        "working_orders": len(raw_orders),
        "reserved_cash": _rounded(reserved_cash, 2),
        "unknown_submission_status": not broker_reconciled,
    }
    sanitized = {
        "schema_version": LOOP_C_SCHWAB_FACTS_SCHEMA_VERSION,
        "authority": "OBSERVED_READ_ONLY",
        "observed_at": normalized.get("observed_at"),
        "source": "Schwab Duckets read-only integration",
        "upstream_policy_version": normalized.get("schema_version"),
        "account_values": {
            key: account_values.get(key)
            for key in (
                "status",
                "liquidation_value",
                "cash_balance",
                "short_balance",
                "available_funds",
                "available_funds_non_marginable_trade",
                "buying_power",
                "buying_power_non_marginable_trade",
                "day_trading_buying_power",
                "margin_balance",
                "maintenance_requirement",
                "maintenance_call",
            )
        },
        "positions": {
            "row_set_reconciled": positions_complete,
            "count": len(raw_positions),
            "items": sanitized_positions,
        },
        "working_orders": {
            "reconciled": broker_reconciled,
            "count": len(raw_orders),
            "reserved_cash": _rounded(reserved_cash, 2),
            "items": sanitized_orders,
        },
        "derived_portfolio": portfolio,
        "derived_broker": broker,
        "identifiers_removed": [
            "accountNumber",
            "hashValue",
            "orderId",
            "activityId",
            "transactionId",
            "executionId",
            "oauth_tokens",
        ],
    }
    return portfolio, broker, sanitized


def _history_context(
    session: object,
    *,
    start: date,
    end: date,
    observed_at: pd.Timestamp,
    review_period_start: date | None = None,
) -> tuple[dict[str, object], str]:
    try:
        service = SchwabPastPositionsService(
            session_factory=lambda: session,
            today=lambda: end,
            now=lambda: observed_at.to_pydatetime(),
        )
        snapshot = service.load(range_start=start, range_end=end)
    except Exception as exc:
        status = "UNAVAILABLE"
        return (
            {
                "schema_version": LOOP_C_TRADE_HISTORY_SCHEMA_VERSION,
                "authority": "OBSERVED_READ_ONLY",
                "status": status,
                "observed_at": observed_at.isoformat(),
                "range_start": start.isoformat(),
                "range_end": end.isoformat(),
                "reason": f"{type(exc).__name__}: {exc}",
                "attribution": "ACCOUNT_CONTEXT_NOT_LOOP_C_ATTRIBUTED",
                "usable_for_loop_c_effectiveness": False,
                "automatic_risk_increase_allowed": False,
            },
            status,
        )
    summary = performance_summary(snapshot.positions)
    eligible = tuple(
        position
        for position in snapshot.positions
        if position.eligible
        and position.realized_pnl is not None
        and position.close_time is not None
    )
    maximum_drawdown = _maximum_drawdown(eligible)
    current_loss_streak, maximum_loss_streak = _loss_streaks(eligible)
    per_underlying = _aggregate_positions(eligible, key="underlying")
    per_strategy = _aggregate_positions(eligible, key="strategy")
    status = "CURRENT_CONTEXT_ONLY" if not snapshot.stale else "STALE_CONTEXT_ONLY"
    payload = {
        "schema_version": LOOP_C_TRADE_HISTORY_SCHEMA_VERSION,
        "authority": "OBSERVED_READ_ONLY",
        "status": status,
        "observed_at": snapshot.observed_at.astimezone(timezone.utc).isoformat(),
        "range_start": snapshot.range_start.isoformat(),
        "range_end": snapshot.range_end.isoformat(),
        "attribution": "ACCOUNT_OPTIONS_CONTEXT_NOT_LOOP_C_ATTRIBUTED",
        "usable_for_loop_c_effectiveness": False,
        "automatic_risk_increase_allowed": False,
        "performance": {
            "included_closed_positions": summary.included_position_count,
            "excluded_closed_positions": summary.excluded_position_count,
            "net_realized_pnl": summary.net_realized_pnl,
            "win_count": summary.win_count,
            "loss_count": summary.loss_count,
            "breakeven_count": summary.breakeven_count,
            "win_rate": summary.win_rate,
            "gross_profit": summary.gross_profit,
            "gross_loss": summary.gross_loss,
            "profit_factor": summary.profit_factor,
            "maximum_closed_pnl_drawdown": maximum_drawdown,
            "current_loss_streak": current_loss_streak,
            "maximum_loss_streak": maximum_loss_streak,
            "fees_unavailable_count": snapshot.coverage.fees_unavailable_count,
        },
        "coverage": {
            "order_count": snapshot.coverage.order_count,
            "transaction_count": snapshot.coverage.transaction_count,
            "fill_count": snapshot.coverage.fill_count,
            "duplicate_fill_count": snapshot.coverage.duplicate_fill_count,
            "invalid_execution_count": snapshot.coverage.invalid_execution_count,
            "ambiguous_package_count": snapshot.coverage.ambiguous_package_count,
            "unmatched_open_quantity": snapshot.coverage.unmatched_open_quantity,
            "unmatched_close_quantity": snapshot.coverage.unmatched_close_quantity,
        },
        "per_underlying": per_underlying,
        "per_strategy": per_strategy,
        "limitations": [
            "The history is account context, not prospectively attributed Loop C evidence.",
            "Only normalized closed option positions with reconstructable fills are summarized.",
            "This history may ratchet a pending risk proposal down but cannot raise risk or tune a model.",
        ],
    }
    if review_period_start is not None:
        review_positions = tuple(
            position
            for position in eligible
            if position.close_time is not None
            and review_period_start
            <= position.close_time.astimezone(timezone.utc).date()
            <= end
        )
        payload["review_period"] = _history_period_summary(
            review_positions,
            start=review_period_start,
            end=end,
        )
    return payload, status


def _history_period_summary(
    positions: Sequence[ClosedPosition],
    *,
    start: date,
    end: date,
) -> dict[str, object]:
    summary = performance_summary(positions)
    current_loss_streak, maximum_loss_streak = _loss_streaks(positions)
    return {
        "schema_version": "loop-c-account-weekly-review-period-v1",
        "status": "CURRENT_CONTEXT_ONLY",
        "range_start": start.isoformat(),
        "range_end": end.isoformat(),
        "attribution": "ACCOUNT_OPTIONS_CONTEXT_NOT_LOOP_C_ATTRIBUTED",
        "usable_for_loop_c_effectiveness": False,
        "automatic_threshold_or_risk_change_allowed": False,
        "performance": {
            "included_closed_positions": summary.included_position_count,
            "excluded_closed_positions": summary.excluded_position_count,
            "net_realized_pnl": summary.net_realized_pnl,
            "win_count": summary.win_count,
            "loss_count": summary.loss_count,
            "breakeven_count": summary.breakeven_count,
            "win_rate": summary.win_rate,
            "gross_profit": summary.gross_profit,
            "gross_loss": summary.gross_loss,
            "profit_factor": summary.profit_factor,
            "maximum_closed_pnl_drawdown": _maximum_drawdown(positions),
            "current_loss_streak": current_loss_streak,
            "maximum_loss_streak": maximum_loss_streak,
            "fees_unavailable_count": sum(
                not position.fees_complete for position in positions
            ),
        },
        "per_underlying": _aggregate_positions(positions, key="underlying"),
        "per_strategy": _aggregate_positions(positions, key="strategy"),
        "limitations": [
            "This period contains actual account option closes, but they are not Loop C-attributed trades.",
            "The summary excludes non-option activity and any close that cannot be reconstructed.",
        ],
    }


def _aggregate_positions(
    positions: Sequence[ClosedPosition],
    *,
    key: str,
) -> list[dict[str, object]]:
    groups: defaultdict[str, list[ClosedPosition]] = defaultdict(list)
    for position in positions:
        label = (
            position.underlying_symbol
            if key == "underlying"
            else position.strategy_label or "Custom"
        )
        groups[label].append(position)
    output: list[dict[str, object]] = []
    for label, rows in groups.items():
        pnl = sum(float(row.realized_pnl or 0.0) for row in rows)
        wins = sum(row.outcome == PositionOutcome.WIN for row in rows)
        losses = sum(row.outcome == PositionOutcome.LOSS for row in rows)
        output.append(
            {
                "label": label,
                "closed_positions": len(rows),
                "net_realized_pnl": _rounded(pnl, 2),
                "wins": wins,
                "losses": losses,
                "win_rate": wins / max(wins + losses, 1),
            }
        )
    return sorted(output, key=lambda row: (str(row["label"])))


def _maximum_drawdown(positions: Sequence[ClosedPosition]) -> float:
    cumulative = 0.0
    peak = 0.0
    maximum = 0.0
    for position in sorted(positions, key=lambda row: row.close_time or datetime.min.replace(tzinfo=timezone.utc)):
        cumulative += float(position.realized_pnl or 0.0)
        peak = max(peak, cumulative)
        maximum = max(maximum, peak - cumulative)
    return _rounded(maximum, 2) or 0.0


def _loss_streaks(positions: Sequence[ClosedPosition]) -> tuple[int, int]:
    current = 0
    maximum = 0
    for position in sorted(positions, key=lambda row: row.close_time or datetime.min.replace(tzinfo=timezone.utc)):
        if float(position.realized_pnl or 0.0) < 0.0:
            current += 1
            maximum = max(maximum, current)
        else:
            current = 0
    return current, maximum


def _sanitize_position(row: Mapping[str, object]) -> dict[str, object]:
    allowed = (
        "symbol",
        "asset_type",
        "underlying_symbol",
        "option_type",
        "strike",
        "expiration",
        "contract_multiplier",
        "long_quantity",
        "short_quantity",
        "net_quantity",
        "settled_quantity",
        "price",
        "market_value",
        "cost_basis",
        "unrealized_pnl",
        "day_pnl",
        "delta",
        "underlying_price",
        "status",
    )
    return {name: row.get(name) for name in allowed}


def _sanitize_order(row: Mapping[str, object]) -> dict[str, object]:
    denied = {
        "account_id",
        "account_number",
        "order_id",
        "activity_id",
        "transaction_id",
        "execution_id",
        "source_ref",
        "source_refs",
    }
    return {
        str(name): _sanitize_nested(value)
        for name, value in row.items()
        if str(name).lower() not in denied
        and not str(name).lower().endswith("_id")
    }


def _sanitize_nested(value: object) -> object:
    if isinstance(value, Mapping):
        return _sanitize_order(value)
    if isinstance(value, list):
        return [_sanitize_nested(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_nested(item) for item in value]
    return value


def _available_capital(
    account_values: Mapping[str, object],
    reserved_cash: float | None,
) -> tuple[float | None, str]:
    nonmarginable = [
        value
        for value in (
            _nonnegative(account_values.get("available_funds_non_marginable_trade")),
            _nonnegative(account_values.get("buying_power_non_marginable_trade")),
        )
        if value is not None
    ]
    if nonmarginable:
        gross = min(nonmarginable)
        source = "MIN_NONMARGINABLE_AVAILABLE"
    else:
        cash_values = [
            value
            for value in (
                _nonnegative(account_values.get("cash_available_for_trading")),
                _nonnegative(account_values.get("available_funds")),
                _nonnegative(account_values.get("cash_balance")),
            )
            if value is not None
        ]
        if not cash_values:
            return None, "UNAVAILABLE"
        gross = min(cash_values)
        source = "MIN_REPORTED_AVAILABLE"
    if reserved_cash is None:
        return None, "UNAVAILABLE"
    return max(0.0, gross - reserved_cash), source


def _required_call(session: object, name: str) -> object:
    method = getattr(session, name, None)
    if not callable(method):
        raise TypeError(f"Schwab session does not provide read-only {name}")
    return method()


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"Normalized Schwab {label} is unavailable")
    return value


def _finite(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _nonnegative(value: object) -> float | None:
    parsed = _finite(value)
    return parsed if parsed is not None and parsed >= 0.0 else None


def _positive(value: object) -> float | None:
    parsed = _finite(value)
    return parsed if parsed is not None and parsed > 0.0 else None


def _rounded(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else None


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
        description="Capture sanitized GET-only Schwab evidence for Loop C."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--root-dir", type=Path)
    group.add_argument("--datastore-target", choices=sorted(DATASTORE_TARGETS))
    parser.add_argument("--observed-at")
    parser.add_argument("--history-start", type=date.fromisoformat)
    parser.add_argument("--review-period-start", type=date.fromisoformat)
    parser.add_argument("--compact", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = resolve_datastore_dir(root_dir=args.root_dir, target=args.datastore_target)
        with exclusive_runtime_lock(
            root / ".ducketz-loop-c-schwab-read-only.lock",
            process_name="Duckets Loop C Schwab read-only snapshot",
        ):
            result = capture_schwab_read_only_state(
                root,
                observed_at=args.observed_at,
                history_start=args.history_start,
                review_period_start=args.review_period_start,
            )
        output = {
            "status": "CURRENT" if result.reconciled else "INCOMPLETE",
            "run_directory": str(result.run_directory),
            "portfolio_snapshot": str(result.portfolio_snapshot_path),
            "broker_snapshot": str(result.broker_snapshot_path),
            "history_summary": str(result.history_summary_path),
            "history_status": result.history_status,
            "authority": "OBSERVED_READ_ONLY",
            "orders_enabled": False,
            "orders_placed": 0,
        }
        exit_code = 0 if result.reconciled else 2
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


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "LOOP_C_SCHWAB_RECEIPT_SCHEMA_VERSION",
    "LOOP_C_SCHWAB_SOURCE_POLICY_VERSION",
    "SchwabReadOnlySnapshotResult",
    "capture_schwab_read_only_state",
    "main",
]
