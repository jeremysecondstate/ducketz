from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from io import StringIO
from typing import Any

from app.models.past_positions import (
    ClosedPosition,
    ClosedPositionLeg,
    CumulativePnlPoint,
    ExecutionFill,
    HistoryCoverage,
    OptionContract,
    PastPositionFilters,
    PastPositionsSnapshot,
    PerformanceSummary,
    PositionOutcome,
    PositionTimelineEvent,
    StrategyPerformance,
)
from app.services.schwab import SchwabSession


HISTORY_WINDOW_DAYS = 60
HISTORY_MAX_RESULTS = 3_000
OPTION_INSTRUCTIONS = {
    "BUY_TO_OPEN",
    "SELL_TO_OPEN",
    "BUY_TO_CLOSE",
    "SELL_TO_CLOSE",
}
ALL_ACCOUNTS = "All Accounts"
ALL_STRATEGIES = "All Strategies"
GROUP_BY_CHOICES = ("Month", "Strategy", "Symbol")
DATE_RANGE_CHOICES = ("YTD", "Last 30 Days", "Last 90 Days", "All Loaded")

_OCC_PATTERN = re.compile(
    r"^(?P<root>[A-Z0-9.]{1,6})\s*(?P<date>\d{6})(?P<right>[CP])(?P<strike>\d{8})$"
)


@dataclass(frozen=True)
class NormalizedHistory:
    fills: tuple[ExecutionFill, ...]
    coverage: HistoryCoverage


@dataclass(frozen=True)
class _PackageLeg:
    contract: OptionContract
    instruction: str
    ratio: float
    quantity: float
    weighted_price: float
    gross_cash_flow: float
    known_fees: float
    has_reported_fee: bool


@dataclass(frozen=True)
class _ExecutionPackage:
    package_id: str
    account_label: str
    order_id: str
    package_strategy: str | None
    opening: bool
    units: float
    legs: tuple[_PackageLeg, ...]
    first_execution_at: datetime
    final_execution_at: datetime
    order_entered_at: datetime | None
    fills: tuple[ExecutionFill, ...]

    @property
    def cash_flow(self) -> float:
        return sum(leg.gross_cash_flow - leg.known_fees for leg in self.legs)

    @property
    def known_fees(self) -> float:
        return sum(leg.known_fees for leg in self.legs)

    @property
    def fees_complete(self) -> bool:
        return all(leg.has_reported_fee for leg in self.legs)

    @property
    def contract_quantity(self) -> float:
        return sum(leg.quantity for leg in self.legs)


@dataclass
class _OpenLot:
    package: _ExecutionPackage
    remaining_units: float


class SchwabPastPositionsService:
    """Read-only, injected Schwab history loader with last-good snapshot retention."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], object] = SchwabSession,
        today: Callable[[], date] = date.today,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        window_days: int = HISTORY_WINDOW_DAYS,
        max_results: int = HISTORY_MAX_RESULTS,
    ) -> None:
        self.session_factory = session_factory
        self.today = today
        self.now = now
        self.window_days = window_days
        self.max_results = max_results
        self._last_snapshot: PastPositionsSnapshot | None = None

    def load(
        self,
        range_start: date | None = None,
        range_end: date | None = None,
    ) -> PastPositionsSnapshot:
        end = range_end or self.today()
        start = range_start or date(end.year, 1, 1)
        if start > end:
            raise ValueError("Past Positions range start must not be after range end.")
        try:
            session = self.session_factory()
            orders, transactions = self._load_payloads(session, start, end)
            snapshot = snapshot_from_history(
                orders,
                transactions,
                range_start=start,
                range_end=end,
                observed_at=self.now(),
            )
        except Exception as exc:
            if self._last_snapshot is None:
                raise
            return replace(
                self._last_snapshot,
                status="Refresh failed; showing the last valid Past Positions snapshot",
                stale=True,
                refresh_error=f"{type(exc).__name__}: {exc}",
            )
        self._last_snapshot = snapshot
        return snapshot

    def _load_payloads(
        self,
        session: object,
        start: date,
        end: date,
    ) -> tuple[list[object], list[object]]:
        get_orders = getattr(session, "get_orders", None)
        get_transactions = getattr(session, "get_transactions", None)
        if not callable(get_orders):
            raise TypeError("Schwab session does not provide bounded order history.")
        if not callable(get_transactions):
            raise TypeError("Schwab session does not provide bounded transaction history.")

        orders: list[object] = []
        transactions: list[object] = []
        for window_start, window_end in _history_windows(start, end, self.window_days):
            raw_orders = get_orders(
                from_entered_time=window_start,
                to_entered_time=window_end,
                max_results=self.max_results,
            )
            raw_transactions = get_transactions(
                start_date=window_start,
                end_date=window_end,
                transaction_types="TRADE",
            )
            if not isinstance(raw_orders, list):
                raise RuntimeError("Schwab order-history window returned a non-list payload.")
            if not isinstance(raw_transactions, list):
                raise RuntimeError("Schwab transaction-history window returned a non-list payload.")
            if len(raw_orders) >= self.max_results:
                raise RuntimeError(
                    "Schwab order-history window reached its result cap; history may be truncated."
                )
            orders.extend(raw_orders)
            transactions.extend(raw_transactions)
        return (
            _deduplicate_rows(orders, ("orderId", "order_id")),
            _deduplicate_rows(
                transactions,
                ("activityId", "transactionId", "transaction_id"),
            ),
        )


def snapshot_from_history(
    orders_payload: object,
    transactions_payload: object = (),
    *,
    range_start: date,
    range_end: date,
    observed_at: datetime,
) -> PastPositionsSnapshot:
    normalized = normalize_history(orders_payload, transactions_payload)
    positions, reconstruction = reconstruct_closed_positions(normalized.fills)
    coverage = _merge_coverage(normalized.coverage, reconstruction)
    return PastPositionsSnapshot(
        positions=positions,
        coverage=coverage,
        range_start=range_start,
        range_end=range_end,
        observed_at=_aware_utc(observed_at),
        status=f"Schwab execution history · {len(positions):,} eligible closed position(s)",
    )


def normalize_history(
    orders_payload: object,
    transactions_payload: object = (),
) -> NormalizedHistory:
    orders = list(orders_payload) if _is_sequence(orders_payload) else []
    transactions = list(transactions_payload) if _is_sequence(transactions_payload) else []
    raw_fills: list[ExecutionFill] = []
    counters: defaultdict[str, int] = defaultdict(int)
    messages: list[str] = []

    for order_index, raw_order in enumerate(orders):
        if not isinstance(raw_order, Mapping):
            counters["invalid"] += 1
            continue
        for order, source_ref in _walk_orders(raw_order, f"orders[{order_index}]"):
            fills, row_counts, row_messages = _normalize_order(order, source_ref)
            raw_fills.extend(fills)
            for key, value in row_counts.items():
                counters[key] += value
            messages.extend(row_messages)

    for transaction_index, transaction in enumerate(transactions):
        if not isinstance(transaction, Mapping):
            counters["invalid"] += 1
            continue
        fills, row_counts, row_messages = _normalize_transaction(
            transaction,
            f"transactions[{transaction_index}]",
        )
        raw_fills.extend(fills)
        for key, value in row_counts.items():
            counters[key] += value
        messages.extend(row_messages)

    deduplicated: list[ExecutionFill] = []
    by_execution_id: set[str] = set()
    by_fingerprint: dict[tuple[object, ...], str] = {}
    duplicate_count = 0
    for fill in sorted(raw_fills, key=_execution_sort_key):
        fingerprint = _fill_fingerprint(fill)
        if fill.execution_id and fill.execution_id in by_execution_id:
            duplicate_count += 1
            continue
        prior_source = by_fingerprint.get(fingerprint)
        source_kind = fill.provenance[0].split("[", 1)[0] if fill.provenance else ""
        if prior_source is not None and prior_source != source_kind:
            duplicate_count += 1
            continue
        if fill.execution_id:
            by_execution_id.add(fill.execution_id)
        by_fingerprint[fingerprint] = source_kind
        deduplicated.append(fill)

    return NormalizedHistory(
        fills=tuple(deduplicated),
        coverage=HistoryCoverage(
            order_count=len(orders),
            transaction_count=len(transactions),
            fill_count=len(deduplicated),
            duplicate_fill_count=duplicate_count,
            non_option_count=counters["non_option"],
            invalid_execution_count=counters["invalid"],
            messages=tuple(dict.fromkeys(messages)),
        ),
    )


def reconstruct_closed_positions(
    fills: Sequence[ExecutionFill],
) -> tuple[tuple[ClosedPosition, ...], HistoryCoverage]:
    packages, ambiguous_count, package_messages = _execution_packages(fills)
    inventory: dict[tuple[object, ...], deque[_OpenLot]] = defaultdict(deque)
    closed: list[ClosedPosition] = []
    unmatched_close = 0.0

    for package in sorted(packages, key=lambda item: (item.final_execution_at, item.package_id)):
        key = _package_inventory_key(package)
        if package.opening:
            inventory[key].append(_OpenLot(package=package, remaining_units=package.units))
            continue
        remaining_close = package.units
        lots = inventory[key]
        while remaining_close > 1e-9 and lots:
            lot = lots[0]
            matched_units = min(lot.remaining_units, remaining_close)
            closed.append(_closed_position(lot.package, package, matched_units, len(closed)))
            lot.remaining_units -= matched_units
            remaining_close -= matched_units
            if lot.remaining_units <= 1e-9:
                lots.popleft()
        if remaining_close > 1e-9:
            unmatched_close += remaining_close * sum(leg.ratio for leg in package.legs)

    unmatched_open = sum(
        lot.remaining_units * sum(leg.ratio for leg in lot.package.legs)
        for lots in inventory.values()
        for lot in lots
    )
    fees_unavailable = sum(not position.fees_complete for position in closed)
    coverage = HistoryCoverage(
        ambiguous_package_count=ambiguous_count,
        unmatched_open_quantity=_rounded(unmatched_open, 8),
        unmatched_close_quantity=_rounded(unmatched_close, 8),
        fees_unavailable_count=fees_unavailable,
        messages=package_messages,
    )
    return (
        tuple(sorted(closed, key=lambda item: (item.close_time or datetime.min.replace(tzinfo=timezone.utc), item.position_id), reverse=True)),
        coverage,
    )


def performance_summary(positions: Sequence[ClosedPosition]) -> PerformanceSummary:
    eligible = tuple(
        position
        for position in positions
        if position.eligible and position.realized_pnl is not None
    )
    excluded_count = len(positions) - len(eligible)
    wins = tuple(position for position in eligible if position.outcome == PositionOutcome.WIN)
    losses = tuple(position for position in eligible if position.outcome == PositionOutcome.LOSS)
    breakevens = tuple(
        position for position in eligible if position.outcome == PositionOutcome.BREAKEVEN
    )
    decided = len(wins) + len(losses)
    gross_profit = sum(float(position.realized_pnl) for position in wins if position.realized_pnl is not None)
    gross_loss = sum(float(position.realized_pnl) for position in losses if position.realized_pnl is not None)
    held = tuple(
        float(position.holding_days)
        for position in eligible
        if position.holding_days is not None
    )
    cumulative = 0.0
    cumulative_points: list[CumulativePnlPoint] = []
    for position in sorted(
        eligible,
        key=lambda item: (item.close_time or datetime.max.replace(tzinfo=timezone.utc), item.position_id),
    ):
        if position.close_time is None or position.realized_pnl is None:
            continue
        cumulative += position.realized_pnl
        cumulative_points.append(CumulativePnlPoint(position.close_time, _rounded(cumulative, 2)))
    by_strategy: defaultdict[str, list[float]] = defaultdict(list)
    for position in eligible:
        by_strategy[position.strategy_label or "Custom"].append(float(position.realized_pnl))
    strategy = tuple(
        StrategyPerformance(label, _rounded(sum(values), 2), len(values))
        for label, values in sorted(
            by_strategy.items(),
            key=lambda item: (-sum(item[1]), item[0]),
        )
    )
    return PerformanceSummary(
        net_realized_pnl=(
            _rounded(sum(float(position.realized_pnl) for position in eligible), 2)
            if eligible
            else None
        ),
        win_count=len(wins),
        loss_count=len(losses),
        breakeven_count=len(breakevens),
        win_rate=(len(wins) / decided if decided else None),
        gross_profit=_rounded(gross_profit, 2),
        gross_loss=_rounded(gross_loss, 2),
        profit_factor=(gross_profit / abs(gross_loss) if gross_loss < 0 else None),
        average_days_held=(sum(held) / len(held) if held else None),
        holding_time_count=len(held),
        included_position_count=len(eligible),
        excluded_position_count=excluded_count,
        cumulative_pnl=tuple(cumulative_points),
        strategy_performance=strategy,
    )


def filter_closed_positions(
    positions: Sequence[ClosedPosition],
    filters: PastPositionFilters,
    *,
    today: date | None = None,
) -> tuple[ClosedPosition, ...]:
    observed = today or date.today()
    start: date | None = None
    if filters.date_range == "YTD":
        start = date(observed.year, 1, 1)
    elif filters.date_range == "Last 30 Days":
        start = observed - timedelta(days=29)
    elif filters.date_range == "Last 90 Days":
        start = observed - timedelta(days=89)
    symbol = filters.symbol.strip().upper()
    visible: list[ClosedPosition] = []
    for position in positions:
        if filters.account != ALL_ACCOUNTS and position.account_label != filters.account:
            continue
        if symbol and symbol not in position.underlying_symbol.upper():
            continue
        if filters.strategy != ALL_STRATEGIES and position.strategy_label != filters.strategy:
            continue
        if position.close_time is not None:
            closed_on = position.close_time.date()
            if start is not None and closed_on < start:
                continue
            if closed_on > observed:
                continue
        elif start is not None:
            continue
        visible.append(position)
    return tuple(
        sorted(
            visible,
            key=lambda item: (
                item.close_time or datetime.min.replace(tzinfo=timezone.utc),
                item.position_id,
            ),
            reverse=True,
        )
    )


def group_closed_positions(
    positions: Sequence[ClosedPosition],
    group_by: str,
) -> tuple[tuple[str, tuple[ClosedPosition, ...]], ...]:
    grouped: dict[str, list[ClosedPosition]] = {}
    for position in positions:
        if group_by == "Strategy":
            label = position.strategy_label or "Custom"
        elif group_by == "Symbol":
            label = position.underlying_symbol or "Unavailable"
        else:
            label = position.close_time.strftime("%B %Y") if position.close_time else "Date unavailable"
        grouped.setdefault(label, []).append(position)
    if group_by == "Month":
        keys = sorted(
            grouped,
            key=lambda label: max(
                (
                    position.close_time or datetime.min.replace(tzinfo=timezone.utc)
                    for position in grouped[label]
                ),
                default=datetime.min.replace(tzinfo=timezone.utc),
            ),
            reverse=True,
        )
    else:
        keys = sorted(grouped)
    return tuple((label, tuple(grouped[label])) for label in keys)


def positions_csv(positions: Sequence[ClosedPosition]) -> str:
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        (
            "close_time_utc",
            "underlying",
            "strategy",
            "expiration",
            "quantity",
            "realized_pnl",
            "return_percent",
            "days_held",
            "outcome",
            "opening_cash_flow",
            "closing_cash_flow",
            "order_ids",
            "fees",
            "fees_complete",
            "coverage",
        )
    )
    for position in positions:
        writer.writerow(
            (
                _iso_utc(position.close_time),
                position.underlying_symbol,
                position.strategy_label,
                position.expiration.isoformat() if position.expiration else "",
                _csv_number(position.quantity),
                _csv_number(position.realized_pnl),
                _csv_number(
                    position.return_fraction * 100
                    if position.return_fraction is not None
                    else None
                ),
                _csv_number(position.holding_days),
                position.outcome.value if position.outcome else "Unavailable",
                _csv_number(position.opening_cash_flow),
                _csv_number(position.closing_cash_flow),
                "|".join(position.order_ids),
                _csv_number(position.fees),
                "true" if position.fees_complete else "false",
                "Eligible" if position.eligible else "; ".join(position.unavailable_reasons),
            )
        )
    return output.getvalue()


def _normalize_order(
    order: Mapping[str, object],
    source_ref: str,
) -> tuple[list[ExecutionFill], dict[str, int], list[str]]:
    counts: defaultdict[str, int] = defaultdict(int)
    messages: list[str] = []
    order_id = _clean_text(order.get("orderId") or order.get("order_id"))
    package_id = order_id or f"unidentified-{_payload_digest(order)}"
    strategy = _clean_text(
        order.get("complexOrderStrategyType") or order.get("complex_order_strategy_type")
    ).upper() or None
    if strategy in {"NONE", "SINGLE"}:
        strategy = None
    account_label = _account_label(order.get("accountNumber") or order.get("accountId"))
    entered_at = _parse_datetime(order.get("enteredTime") or order.get("entered_time"))
    raw_legs = order.get("orderLegCollection") or order.get("order_legs")
    if not _is_sequence(raw_legs):
        return [], counts, messages
    leg_by_id: dict[str, Mapping[str, object]] = {}
    ratios: dict[str, float] = {}
    leg_quantities: list[float] = []
    for index, raw_leg in enumerate(raw_legs):
        if not isinstance(raw_leg, Mapping):
            continue
        leg_id = _clean_text(raw_leg.get("legId") or raw_leg.get("leg_id") or index)
        leg_by_id[leg_id] = raw_leg
        quantity = _number(raw_leg.get("quantity"))
        if quantity is not None and quantity > 0:
            leg_quantities.append(quantity)
            ratios[leg_id] = quantity
    ratio_base = min(leg_quantities) if leg_quantities else 1.0
    ratios = {key: value / ratio_base for key, value in ratios.items()}

    activities = order.get("orderActivityCollection") or order.get("order_activities")
    if not _is_sequence(activities):
        return [], counts, messages
    fills: list[ExecutionFill] = []
    for activity_index, raw_activity in enumerate(activities):
        if not isinstance(raw_activity, Mapping):
            counts["invalid"] += 1
            continue
        activity_type = _clean_text(
            raw_activity.get("activityType") or raw_activity.get("activity_type")
        ).upper()
        if activity_type not in {"EXECUTION", "FILL"}:
            continue
        activity_id = _clean_text(
            raw_activity.get("activityId") or raw_activity.get("activity_id")
        ) or None
        execution_rows = raw_activity.get("executionLegs") or raw_activity.get("execution_legs")
        if not _is_sequence(execution_rows):
            counts["invalid"] += 1
            messages.append(f"{source_ref} execution activity had no structured execution legs.")
            continue
        activity_fee = _fee_total(raw_activity)
        activity_quantities = sum(
            value
            for value in (_number(row.get("quantity")) for row in execution_rows if isinstance(row, Mapping))
            if value is not None and value > 0
        )
        for execution_index, raw_execution in enumerate(execution_rows):
            if not isinstance(raw_execution, Mapping):
                counts["invalid"] += 1
                continue
            leg_id = _clean_text(
                raw_execution.get("legId") or raw_execution.get("leg_id") or execution_index
            )
            leg = leg_by_id.get(leg_id)
            if leg is None and len(leg_by_id) == 1:
                leg = next(iter(leg_by_id.values()))
                leg_id = next(iter(leg_by_id))
            if leg is None:
                counts["invalid"] += 1
                messages.append(f"{source_ref} execution leg did not map to an exact order leg.")
                continue
            instrument = leg.get("instrument") if isinstance(leg.get("instrument"), Mapping) else {}
            if _clean_text(instrument.get("assetType") or instrument.get("asset_type")).upper() != "OPTION":
                counts["non_option"] += 1
                continue
            instruction = _clean_text(leg.get("instruction")).upper()
            quantity = _number(raw_execution.get("quantity"))
            price = _number(raw_execution.get("price"))
            executed_at = _parse_datetime(
                raw_execution.get("time")
                or raw_execution.get("executionTime")
                or raw_activity.get("executionTime")
                or raw_activity.get("time")
            )
            contract = _option_contract(instrument, leg)
            if (
                instruction not in OPTION_INSTRUCTIONS
                or quantity is None
                or quantity <= 0
                or price is None
                or price < 0
                or executed_at is None
                or contract is None
            ):
                counts["invalid"] += 1
                messages.append(f"{source_ref} contained an incomplete option execution.")
                continue
            fee = _fee_total(raw_execution)
            if fee is None and activity_fee is not None and activity_quantities > 0:
                fee = activity_fee * quantity / activity_quantities
            execution_id = _clean_text(
                raw_execution.get("executionId")
                or raw_execution.get("execution_id")
                or raw_execution.get("activityId")
            )
            if not execution_id:
                execution_id = _synthetic_execution_id(
                    order_id,
                    activity_id,
                    leg_id,
                    executed_at,
                    quantity,
                    price,
                    contract.occ_symbol,
                )
            fills.append(
                ExecutionFill(
                    execution_id=execution_id,
                    account_label=account_label,
                    order_id=order_id or package_id,
                    package_id=package_id,
                    activity_id=activity_id,
                    executed_at=executed_at,
                    instruction=instruction,
                    quantity=quantity,
                    price=price,
                    contract=contract,
                    fees=fee,
                    package_strategy=strategy,
                    package_leg_ratio=ratios.get(leg_id, 1.0),
                    order_entered_at=entered_at,
                    provenance=(
                        f"{source_ref}.orderActivityCollection[{activity_index}].executionLegs[{execution_index}]",
                    ),
                )
            )
    return fills, counts, messages


def _normalize_transaction(
    transaction: Mapping[str, object],
    source_ref: str,
) -> tuple[list[ExecutionFill], dict[str, int], list[str]]:
    counts: defaultdict[str, int] = defaultdict(int)
    messages: list[str] = []
    transaction_id = _clean_text(
        transaction.get("activityId")
        or transaction.get("transactionId")
        or transaction.get("transaction_id")
    )
    order_id = _clean_text(transaction.get("orderId") or transaction.get("order_id"))
    account_label = _account_label(
        transaction.get("accountNumber") or transaction.get("accountId")
    )
    occurred_at = _parse_datetime(
        transaction.get("time")
        or transaction.get("tradeDate")
        or transaction.get("transactionDate")
    )
    rows = (
        transaction.get("transferItems")
        or transaction.get("transactionItems")
        or transaction.get("transactionItem")
    )
    if isinstance(rows, Mapping):
        rows = [rows]
    if not _is_sequence(rows):
        return [], counts, messages
    package_strategy = _clean_text(transaction.get("complexOrderStrategyType")).upper() or None
    if package_strategy in {"NONE", "SINGLE"}:
        package_strategy = None
    parent_fee = _fee_total(transaction)
    total_quantity = sum(
        value
        for value in (
            _number(row.get("quantity") or row.get("amount"))
            for row in rows
            if isinstance(row, Mapping)
        )
        if value is not None and abs(value) > 0
    )
    total_quantity = abs(total_quantity)
    fills: list[ExecutionFill] = []
    for index, raw_item in enumerate(rows):
        if not isinstance(raw_item, Mapping):
            counts["invalid"] += 1
            continue
        instrument = raw_item.get("instrument") if isinstance(raw_item.get("instrument"), Mapping) else {}
        if _clean_text(instrument.get("assetType") or instrument.get("asset_type")).upper() != "OPTION":
            counts["non_option"] += 1
            continue
        instruction = _clean_text(
            raw_item.get("instruction") or raw_item.get("positionEffect")
        ).upper()
        quantity = _number(raw_item.get("quantity") or raw_item.get("amount"))
        price = _number(raw_item.get("price"))
        item_time = _parse_datetime(raw_item.get("time")) or occurred_at
        contract = _option_contract(instrument, raw_item)
        if quantity is not None:
            quantity = abs(quantity)
        if (
            instruction not in OPTION_INSTRUCTIONS
            or quantity is None
            or quantity <= 0
            or price is None
            or price < 0
            or item_time is None
            or contract is None
        ):
            counts["invalid"] += 1
            messages.append(f"{source_ref} contained an incomplete option transaction item.")
            continue
        fee = _fee_total(raw_item)
        if fee is None and parent_fee is not None and total_quantity > 0:
            fee = parent_fee * quantity / total_quantity
        execution_id = _clean_text(
            raw_item.get("executionId") or raw_item.get("execution_id")
        )
        if not execution_id:
            execution_id = _synthetic_execution_id(
                order_id,
                transaction_id,
                str(index),
                item_time,
                quantity,
                price,
                contract.occ_symbol,
            )
        fills.append(
            ExecutionFill(
                execution_id=execution_id,
                account_label=account_label,
                order_id=order_id or transaction_id or f"transaction-{_payload_digest(transaction)}",
                package_id=order_id or transaction_id or f"transaction-{_payload_digest(transaction)}",
                activity_id=transaction_id or None,
                executed_at=item_time,
                instruction=instruction,
                quantity=quantity,
                price=price,
                contract=contract,
                fees=fee,
                package_strategy=package_strategy,
                package_leg_ratio=float(_number(raw_item.get("legRatio")) or 1.0),
                order_entered_at=None,
                provenance=(f"{source_ref}.transferItems[{index}]",),
            )
        )
    return fills, counts, messages


def _execution_packages(
    fills: Sequence[ExecutionFill],
) -> tuple[tuple[_ExecutionPackage, ...], int, tuple[str, ...]]:
    grouped: dict[tuple[str, str], list[ExecutionFill]] = defaultdict(list)
    for fill in fills:
        grouped[(fill.account_label, fill.package_id)].append(fill)
    packages: list[_ExecutionPackage] = []
    ambiguous = 0
    messages: list[str] = []
    for (_account, package_id), package_fills in grouped.items():
        opening_states = {fill.is_opening for fill in package_fills}
        if len(opening_states) != 1 or not all(fill.is_opening or fill.is_closing for fill in package_fills):
            ambiguous += 1
            messages.append(f"Package {package_id} mixed opening and closing evidence and was excluded.")
            continue
        by_contract: dict[tuple[str, str], list[ExecutionFill]] = defaultdict(list)
        for fill in package_fills:
            by_contract[(_occ_key(fill.contract.occ_symbol), fill.instruction)].append(fill)
        if len({_occ_key(fill.contract.occ_symbol) for fill in package_fills}) > 1 and not any(
            fill.package_strategy for fill in package_fills
        ):
            ambiguous += 1
            messages.append(f"Multi-leg package {package_id} lacked broker complex-order linkage and was excluded.")
            continue
        package_legs: list[_PackageLeg] = []
        unit_values: list[float] = []
        malformed = False
        for contract_fills in by_contract.values():
            ratios = {round(fill.package_leg_ratio, 8) for fill in contract_fills}
            if len(ratios) != 1 or next(iter(ratios)) <= 0:
                malformed = True
                break
            ratio = next(iter(ratios))
            quantity = sum(fill.quantity for fill in contract_fills)
            unit_values.append(quantity / ratio)
            gross = sum(fill.gross_cash_flow for fill in contract_fills)
            known_fees = sum(fill.fees or 0.0 for fill in contract_fills)
            package_legs.append(
                _PackageLeg(
                    contract=contract_fills[0].contract,
                    instruction=contract_fills[0].instruction,
                    ratio=ratio,
                    quantity=quantity,
                    weighted_price=sum(fill.price * fill.quantity for fill in contract_fills) / quantity,
                    gross_cash_flow=gross,
                    known_fees=known_fees,
                    has_reported_fee=all(fill.fees is not None for fill in contract_fills),
                )
            )
        if malformed or not unit_values or max(unit_values) - min(unit_values) > 1e-8:
            ambiguous += 1
            messages.append(f"Package {package_id} had incompatible executed leg ratios and was excluded.")
            continue
        package_fills.sort(key=_execution_sort_key)
        packages.append(
            _ExecutionPackage(
                package_id=package_id,
                account_label=package_fills[0].account_label,
                order_id=package_fills[0].order_id,
                package_strategy=next(
                    (fill.package_strategy for fill in package_fills if fill.package_strategy),
                    None,
                ),
                opening=next(iter(opening_states)),
                units=unit_values[0],
                legs=tuple(sorted(package_legs, key=lambda leg: _occ_key(leg.contract.occ_symbol))),
                first_execution_at=package_fills[0].executed_at,
                final_execution_at=package_fills[-1].executed_at,
                order_entered_at=min(
                    (fill.order_entered_at for fill in package_fills if fill.order_entered_at),
                    default=None,
                ),
                fills=tuple(package_fills),
            )
        )
    return tuple(packages), ambiguous, tuple(messages)


def _package_inventory_key(package: _ExecutionPackage) -> tuple[object, ...]:
    signature = []
    for leg in package.legs:
        opening_instruction = (
            leg.instruction
            if package.opening
            else "BUY_TO_OPEN"
            if leg.instruction == "SELL_TO_CLOSE"
            else "SELL_TO_OPEN"
        )
        signature.append(
            (
                _occ_key(leg.contract.occ_symbol),
                round(leg.ratio, 8),
                opening_instruction,
            )
        )
    return package.account_label, tuple(sorted(signature))


def _closed_position(
    opening: _ExecutionPackage,
    closing: _ExecutionPackage,
    units: float,
    sequence: int,
) -> ClosedPosition:
    open_fraction = units / opening.units
    close_fraction = units / closing.units
    opening_cash = opening.cash_flow * open_fraction
    closing_cash = closing.cash_flow * close_fraction
    realized = opening_cash + closing_cash
    return_fraction = realized / abs(opening_cash) if abs(opening_cash) > 1e-12 else None
    holding_days = max(
        0.0,
        (closing.final_execution_at - opening.first_execution_at).total_seconds() / 86_400,
    )
    outcome = _outcome(realized)
    closing_by_occ = {_occ_key(leg.contract.occ_symbol): leg for leg in closing.legs}
    legs: list[ClosedPositionLeg] = []
    for open_leg in opening.legs:
        close_leg = closing_by_occ[_occ_key(open_leg.contract.occ_symbol)]
        legs.append(
            ClosedPositionLeg(
                contract=open_leg.contract,
                opening_instruction=open_leg.instruction,
                closing_instruction=close_leg.instruction,
                quantity=open_leg.ratio * units,
                entry_price=open_leg.weighted_price,
                exit_price=close_leg.weighted_price,
                opening_cash_flow=(
                    open_leg.gross_cash_flow - open_leg.known_fees
                ) * open_fraction,
                closing_cash_flow=(
                    close_leg.gross_cash_flow - close_leg.known_fees
                ) * close_fraction,
            )
        )
    strategy = _strategy_label(opening)
    max_profit, max_loss = _max_profit_loss(
        strategy,
        tuple(legs),
        units,
        opening_cash,
    )
    timeline = _timeline(opening, closing)
    order_ids = tuple(dict.fromkeys((opening.order_id, closing.order_id)))
    fees_complete = opening.fees_complete and closing.fees_complete
    known_fees = opening.known_fees * open_fraction + closing.known_fees * close_fraction
    digest = hashlib.sha256(
        f"{opening.package_id}|{closing.package_id}|{units:.8f}|{sequence}".encode("utf-8")
    ).hexdigest()[:16]
    provenance = tuple(
        dict.fromkeys(ref for fill in (*opening.fills, *closing.fills) for ref in fill.provenance)
    )
    return ClosedPosition(
        position_id=f"closed-{digest}",
        account_label=opening.account_label,
        underlying_symbol=legs[0].contract.underlying_symbol,
        strategy_label=strategy,
        open_time=opening.first_execution_at,
        close_time=closing.final_execution_at,
        quantity=units,
        opening_cash_flow=_rounded(opening_cash, 2),
        closing_cash_flow=_rounded(closing_cash, 2),
        realized_pnl=_rounded(realized, 2),
        return_fraction=return_fraction,
        holding_days=holding_days,
        outcome=outcome,
        legs=tuple(legs),
        timeline=timeline,
        order_ids=order_ids,
        fees=_rounded(known_fees, 2) if known_fees or fees_complete else None,
        fees_complete=fees_complete,
        close_reason=None,
        notes=None,
        max_profit=max_profit,
        max_loss=max_loss,
        eligible=True,
        unavailable_reasons=(),
        provenance=provenance,
    )


def _timeline(
    opening: _ExecutionPackage,
    closing: _ExecutionPackage,
) -> tuple[PositionTimelineEvent, ...]:
    events: list[PositionTimelineEvent] = []
    if opening.order_entered_at and opening.order_entered_at <= opening.first_execution_at:
        events.append(
            PositionTimelineEvent(
                "Opening order entered",
                opening.order_entered_at,
                f"Order {opening.order_id}",
                "Schwab order enteredTime",
            )
        )
    events.append(
        PositionTimelineEvent(
            "Opened",
            opening.first_execution_at,
            "First matched opening execution",
            "Schwab execution fill",
        )
    )
    if opening.final_execution_at > opening.first_execution_at:
        events.append(
            PositionTimelineEvent(
                "Opening filled",
                opening.final_execution_at,
                "Final execution for the displayed opening quantity",
                "Schwab execution fill",
            )
        )
    if (
        closing.order_entered_at
        and opening.final_execution_at <= closing.order_entered_at <= closing.final_execution_at
    ):
        events.append(
            PositionTimelineEvent(
                "Closing order entered",
                closing.order_entered_at,
                f"Order {closing.order_id}",
                "Schwab order enteredTime",
            )
        )
    events.append(
        PositionTimelineEvent(
            "Closed",
            closing.final_execution_at,
            "Final execution closing the displayed quantity",
            "Schwab execution fill",
        )
    )
    return tuple(sorted(events, key=lambda item: (item.occurred_at, item.label)))


def _strategy_label(package: _ExecutionPackage) -> str:
    broker_label = {
        "IRON_CONDOR": "Iron Condor",
        "CONDOR": "Iron Condor",
        "BUTTERFLY": "Butterfly",
        "IRON_BUTTERFLY": "Iron Butterfly",
        "STRADDLE": "Straddle",
        "STRANGLE": "Strangle",
        "COLLAR_WITH_STOCK": "Collar",
        "COVERED": "Covered Call",
    }.get((package.package_strategy or "").upper())
    if broker_label:
        return broker_label
    legs = package.legs
    if len(legs) == 1:
        side = "Long" if legs[0].instruction == "BUY_TO_OPEN" else "Short"
        right = "Call" if legs[0].contract.option_type == "CALL" else "Put"
        return f"{side} {right}"
    if len(legs) == 2:
        expirations = {leg.contract.expiration for leg in legs}
        rights = {leg.contract.option_type for leg in legs}
        if len(expirations) == 1 and len(rights) == 1:
            low, high = sorted(legs, key=lambda leg: leg.contract.strike)
            if low.contract.option_type == "PUT":
                return (
                    "Bull Put Spread"
                    if high.instruction == "SELL_TO_OPEN" and low.instruction == "BUY_TO_OPEN"
                    else "Bear Put Spread"
                    if high.instruction == "BUY_TO_OPEN" and low.instruction == "SELL_TO_OPEN"
                    else "Custom"
                )
            return (
                "Bull Call Spread"
                if low.instruction == "BUY_TO_OPEN" and high.instruction == "SELL_TO_OPEN"
                else "Bear Call Spread"
                if low.instruction == "SELL_TO_OPEN" and high.instruction == "BUY_TO_OPEN"
                else "Custom"
            )
    return "Custom"


def _max_profit_loss(
    strategy: str,
    legs: tuple[ClosedPositionLeg, ...],
    units: float,
    opening_cash: float,
) -> tuple[float | None, float | None]:
    if not legs:
        return None, None
    multiplier = legs[0].contract.multiplier
    if len(legs) == 1:
        leg = legs[0]
        premium = abs(opening_cash)
        if leg.opening_instruction == "BUY_TO_OPEN":
            max_loss = premium
            if leg.contract.option_type == "PUT":
                max_profit = leg.contract.strike * multiplier * leg.quantity - premium
                return _rounded(max_profit, 2), _rounded(max_loss, 2)
            return None, _rounded(max_loss, 2)
        max_profit = max(opening_cash, 0.0)
        if leg.contract.option_type == "PUT":
            max_loss = leg.contract.strike * multiplier * leg.quantity - max_profit
            return _rounded(max_profit, 2), _rounded(max_loss, 2)
        return _rounded(max_profit, 2), None
    if len(legs) == 2 and "Spread" in strategy:
        width = abs(legs[0].contract.strike - legs[1].contract.strike)
        width_value = width * multiplier * units
        if opening_cash < 0:
            return _rounded(width_value + opening_cash, 2), _rounded(abs(opening_cash), 2)
        return _rounded(opening_cash, 2), _rounded(max(width_value - opening_cash, 0.0), 2)
    if strategy == "Iron Condor" and len(legs) == 4 and opening_cash > 0:
        puts = sorted(
            (leg.contract.strike for leg in legs if leg.contract.option_type == "PUT")
        )
        calls = sorted(
            (leg.contract.strike for leg in legs if leg.contract.option_type == "CALL")
        )
        if len(puts) == 2 and len(calls) == 2:
            width = max(puts[1] - puts[0], calls[1] - calls[0])
            return (
                _rounded(opening_cash, 2),
                _rounded(max(width * multiplier * units - opening_cash, 0.0), 2),
            )
    return None, None


def _option_contract(
    instrument: Mapping[str, object],
    fallback: Mapping[str, object],
) -> OptionContract | None:
    symbol = _clean_text(instrument.get("symbol") or fallback.get("symbol")).upper()
    match = _OCC_PATTERN.match(symbol)
    multiplier = _number(
        instrument.get("multiplier")
        or instrument.get("contractMultiplier")
        or fallback.get("multiplier")
        or fallback.get("contractMultiplier")
    )
    if match is None or multiplier is None or multiplier <= 0:
        return None
    try:
        expiration = datetime.strptime(match.group("date"), "%y%m%d").date()
    except ValueError:
        return None
    underlying = _clean_text(
        instrument.get("underlyingSymbol")
        or instrument.get("underlying_symbol")
        or match.group("root")
    ).upper()
    return OptionContract(
        occ_symbol=symbol,
        underlying_symbol=underlying,
        expiration=expiration,
        strike=int(match.group("strike")) / 1000,
        option_type="CALL" if match.group("right") == "C" else "PUT",
        multiplier=multiplier,
    )


def _fee_total(row: Mapping[str, object]) -> float | None:
    direct_keys = (
        "fees",
        "fee",
        "commission",
        "commissionAmount",
        "regFee",
        "secFee",
        "optionRegulatoryFee",
    )
    values: list[float] = []
    explicit = False
    for key in direct_keys:
        if key not in row:
            continue
        raw = row.get(key)
        if isinstance(raw, Mapping):
            nested = [_number(value) for value in raw.values()]
            values.extend(abs(value) for value in nested if value is not None)
            explicit = True
            continue
        value = _number(raw)
        if value is not None:
            values.append(abs(value))
            explicit = True
    fee_rows = row.get("feesCollection") or row.get("feeCollection")
    if _is_sequence(fee_rows):
        explicit = True
        for fee_row in fee_rows:
            if isinstance(fee_row, Mapping):
                value = _number(fee_row.get("amount") or fee_row.get("value"))
                if value is not None:
                    values.append(abs(value))
    return sum(values) if explicit else None


def _history_windows(
    start: date,
    end: date,
    window_days: int,
) -> Iterable[tuple[datetime, datetime]]:
    if window_days < 1:
        raise ValueError("History window must be at least one day.")
    cursor = datetime.combine(start, time.min, tzinfo=timezone.utc)
    final = datetime.combine(end, time.max, tzinfo=timezone.utc)
    while cursor < final:
        window_end = min(cursor + timedelta(days=window_days), final)
        yield cursor, window_end
        cursor = window_end


def _deduplicate_rows(
    rows: Sequence[object],
    id_keys: Sequence[str],
) -> list[object]:
    unique: dict[tuple[str, str], object] = {}
    for row in rows:
        key_value = ""
        if isinstance(row, Mapping):
            key_value = next(
                (_clean_text(row.get(key)) for key in id_keys if _clean_text(row.get(key))),
                "",
            )
        key = (
            ("broker_id", key_value)
            if key_value
            else ("payload", json.dumps(row, sort_keys=True, default=str, separators=(",", ":")))
        )
        unique[key] = row
    return list(unique.values())


def _walk_orders(
    order: Mapping[str, object],
    source_ref: str,
) -> Iterable[tuple[Mapping[str, object], str]]:
    yield order, source_ref
    children = order.get("childOrderStrategies")
    if _is_sequence(children):
        for index, child in enumerate(children):
            if isinstance(child, Mapping):
                yield from _walk_orders(child, f"{source_ref}.childOrderStrategies[{index}]")


def _merge_coverage(left: HistoryCoverage, right: HistoryCoverage) -> HistoryCoverage:
    return HistoryCoverage(
        order_count=left.order_count,
        transaction_count=left.transaction_count,
        fill_count=left.fill_count,
        duplicate_fill_count=left.duplicate_fill_count,
        non_option_count=left.non_option_count,
        invalid_execution_count=left.invalid_execution_count,
        ambiguous_package_count=right.ambiguous_package_count,
        unmatched_open_quantity=right.unmatched_open_quantity,
        unmatched_close_quantity=right.unmatched_close_quantity,
        excluded_position_count=left.excluded_position_count + right.excluded_position_count,
        fees_unavailable_count=right.fees_unavailable_count,
        messages=tuple(dict.fromkeys((*left.messages, *right.messages))),
    )


def _account_label(raw: object) -> str:
    value = _clean_text(raw)
    if not value:
        return "Schwab"
    if "•" in value or "*" in value:
        return value
    suffix = re.sub(r"\D", "", value)[-4:]
    return f"Schwab ••••{suffix}" if suffix else "Schwab"


def _outcome(realized_pnl: float) -> PositionOutcome:
    if math.isclose(realized_pnl, 0.0, abs_tol=0.005):
        return PositionOutcome.BREAKEVEN
    return PositionOutcome.WIN if realized_pnl > 0 else PositionOutcome.LOSS


def _execution_sort_key(fill: ExecutionFill) -> tuple[datetime, str, str]:
    return fill.executed_at, fill.order_id, fill.execution_id


def _fill_fingerprint(fill: ExecutionFill) -> tuple[object, ...]:
    return (
        fill.account_label,
        fill.order_id,
        _occ_key(fill.contract.occ_symbol),
        fill.instruction,
        fill.executed_at.isoformat(),
        round(fill.quantity, 8),
        round(fill.price, 8),
    )


def _synthetic_execution_id(*parts: object) -> str:
    value = "|".join(str(part) for part in parts)
    return f"synthetic-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:20]}"


def _payload_digest(payload: Mapping[str, object]) -> str:
    serialized = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def _occ_key(symbol: str) -> str:
    return re.sub(r"\s+", "", symbol).upper()


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _aware_utc(value)
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = _clean_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _aware_utc(parsed)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _clean_text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _rounded(value: float, digits: int) -> float:
    return round(float(value), digits)


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _iso_utc(value: datetime | None) -> str:
    return value.astimezone(timezone.utc).isoformat() if value else ""


def _csv_number(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.8f}".rstrip("0").rstrip(".")


__all__ = [
    "ALL_ACCOUNTS",
    "ALL_STRATEGIES",
    "DATE_RANGE_CHOICES",
    "GROUP_BY_CHOICES",
    "HISTORY_MAX_RESULTS",
    "HISTORY_WINDOW_DAYS",
    "NormalizedHistory",
    "SchwabPastPositionsService",
    "filter_closed_positions",
    "group_closed_positions",
    "normalize_history",
    "performance_summary",
    "positions_csv",
    "reconstruct_closed_positions",
    "snapshot_from_history",
]
