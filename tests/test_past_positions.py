from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models.past_positions import PastPositionFilters, PositionOutcome
from app.services.schwab_past_positions import (
    ALL_STRATEGIES,
    SchwabPastPositionsService,
    filter_closed_positions,
    group_closed_positions,
    normalize_history,
    performance_summary,
    positions_csv,
    reconstruct_closed_positions,
    snapshot_from_history,
)
from app.ui.options_strategies import OPTIONS_COMMAND_TABS
from app.ui.options_management import OptionsManagementView
from app.ui.past_positions import (
    route_related_orders,
    selected_position_detail_state,
    statement_text,
)


UTC = timezone.utc
OPENED = datetime(2025, 5, 2, 14, 14, tzinfo=UTC)
CLOSED = datetime(2025, 5, 12, 15, 2, tzinfo=UTC)
NVDA_120P = "NVDA  250516P00120000"
NVDA_115P = "NVDA  250516P00115000"


def test_raw_order_and_transaction_normalization_uses_exact_execution_evidence() -> None:
    order = _single_order("10", "BUY_TO_OPEN", NVDA_120P, 1, 2.25, OPENED, "exec-10")
    transaction = {
        "activityId": "trade-11",
        "orderId": "11",
        "time": CLOSED.isoformat(),
        "transactionItem": {
            "instruction": "SELL_TO_CLOSE",
            "quantity": 1,
            "price": 3.40,
            "instrument": _instrument(NVDA_120P),
        },
    }

    history = normalize_history([order], [transaction])

    assert [fill.instruction for fill in history.fills] == ["BUY_TO_OPEN", "SELL_TO_CLOSE"]
    assert history.fills[0].contract.occ_symbol == NVDA_120P
    assert history.fills[0].contract.strike == 120.0
    assert history.fills[0].contract.multiplier == 100.0
    assert history.fills[1].executed_at == CLOSED


def test_non_option_records_and_non_execution_activities_are_filtered() -> None:
    equity = _single_order(
        "10", "BUY_TO_OPEN", "NVDA", 1, 2.25, OPENED, "equity", asset_type="EQUITY"
    )
    equity["orderActivityCollection"][0]["activityType"] = "ORDER_ACTION"
    option = _single_order("11", "BUY_TO_OPEN", NVDA_120P, 1, 2.25, OPENED, "option")
    option["orderLegCollection"].append(
        {
            "legId": 2,
            "instruction": "BUY",
            "quantity": 10,
            "instrument": {"assetType": "EQUITY", "symbol": "NVDA"},
        }
    )
    option["orderActivityCollection"][0]["executionLegs"].append(
        {"legId": 2, "quantity": 10, "price": 120, "time": OPENED.isoformat()}
    )

    history = normalize_history([equity, option])

    assert len(history.fills) == 1
    assert history.coverage.non_option_count == 1


def test_duplicate_broker_execution_results_are_removed_across_windows() -> None:
    order = _single_order("10", "BUY_TO_OPEN", NVDA_120P, 1, 2.25, OPENED, "same-fill")
    history = normalize_history([order, order])
    assert len(history.fills) == 1
    assert history.coverage.duplicate_fill_count == 1


def test_execution_fills_are_sorted_deterministically() -> None:
    late = _single_order("20", "SELL_TO_CLOSE", NVDA_120P, 1, 3.25, CLOSED, "late")
    early = _single_order("10", "BUY_TO_OPEN", NVDA_120P, 1, 2.25, OPENED, "early")
    history = normalize_history([late, early])
    assert [fill.execution_id for fill in history.fills] == ["early", "late"]


def test_single_leg_debit_round_trip_realized_pnl_and_return() -> None:
    positions, coverage = _round_trip(
        _single_order("10", "BUY_TO_OPEN", NVDA_120P, 1, 2.00, OPENED, "open"),
        _single_order("11", "SELL_TO_CLOSE", NVDA_120P, 1, 3.00, CLOSED, "close"),
    )
    position = positions[0]
    assert position.opening_cash_flow == -200.0
    assert position.closing_cash_flow == 300.0
    assert position.realized_pnl == 100.0
    assert position.return_fraction == pytest.approx(0.5)
    assert position.outcome == PositionOutcome.WIN
    assert coverage.unmatched_open_quantity == 0


def test_single_leg_credit_round_trip_uses_buy_to_close_cash_flow() -> None:
    positions, _coverage = _round_trip(
        _single_order("10", "SELL_TO_OPEN", NVDA_120P, 2, 2.00, OPENED, "open"),
        _single_order("11", "BUY_TO_CLOSE", NVDA_120P, 2, 0.50, CLOSED, "close"),
    )
    assert positions[0].opening_cash_flow == 400.0
    assert positions[0].closing_cash_flow == -100.0
    assert positions[0].realized_pnl == 300.0
    assert positions[0].return_fraction == pytest.approx(0.75)


def test_exact_multi_leg_round_trip_preserves_package_and_derives_supported_structure() -> None:
    opening = _multi_order(
        "20",
        (("SELL_TO_OPEN", NVDA_120P, 1, 2.25), ("BUY_TO_OPEN", NVDA_115P, 1, 1.10)),
        OPENED,
        strategy="VERTICAL",
    )
    closing = _multi_order(
        "21",
        (("BUY_TO_CLOSE", NVDA_120P, 1, 0.60), ("SELL_TO_CLOSE", NVDA_115P, 1, 0.25)),
        CLOSED,
        strategy="VERTICAL",
    )

    positions, coverage = _round_trip(opening, closing)
    position = positions[0]

    assert position.strategy_label == "Bull Put Spread"
    assert [leg.contract.occ_symbol for leg in position.legs] == [NVDA_115P, NVDA_120P]
    assert position.realized_pnl == 80.0
    assert position.return_fraction == pytest.approx(80 / 115)
    assert position.max_profit == 115.0
    assert position.max_loss == 385.0
    assert position.order_ids == ("20", "21")
    assert coverage.ambiguous_package_count == 0


def test_partial_execution_fills_aggregate_within_one_order_package() -> None:
    opening = _single_order("10", "BUY_TO_OPEN", NVDA_120P, 1, 2.00, OPENED, "open-a")
    opening["orderActivityCollection"][0]["executionLegs"][0]["quantity"] = 0.4
    opening["orderActivityCollection"].append(
        {
            "activityType": "EXECUTION",
            "activityId": "activity-b",
            "executionLegs": [
                {
                    "legId": 1,
                    "quantity": 0.6,
                    "price": 2.10,
                    "time": (OPENED + timedelta(minutes=2)).isoformat(),
                    "executionId": "open-b",
                }
            ],
        }
    )
    closing = _single_order("11", "SELL_TO_CLOSE", NVDA_120P, 1, 3.00, CLOSED, "close")

    positions, coverage = _round_trip(opening, closing)

    assert len(positions) == 1
    assert positions[0].legs[0].entry_price == pytest.approx(2.06)
    assert positions[0].holding_days == pytest.approx((CLOSED - OPENED).total_seconds() / 86400)
    assert coverage.unmatched_open_quantity == 0


def test_partial_close_emits_only_closed_quantity_and_omits_residual_inventory() -> None:
    positions, coverage = _round_trip(
        _single_order("10", "BUY_TO_OPEN", NVDA_120P, 2, 2.00, OPENED, "open"),
        _single_order("11", "SELL_TO_CLOSE", NVDA_120P, 1, 3.00, CLOSED, "close"),
    )
    assert len(positions) == 1
    assert positions[0].quantity == 1
    assert positions[0].legs[0].quantity == 1
    assert positions[0].realized_pnl == 100.0
    assert coverage.unmatched_open_quantity == 1


def test_unmatched_opening_and_closing_evidence_is_not_invented_as_closed_positions() -> None:
    history = normalize_history(
        [
            _single_order("10", "BUY_TO_OPEN", NVDA_120P, 1, 2, OPENED, "open"),
            _single_order("11", "SELL_TO_CLOSE", NVDA_115P, 1, 3, CLOSED, "close"),
        ]
    )
    positions, coverage = reconstruct_closed_positions(history.fills)
    assert positions == ()
    assert coverage.unmatched_open_quantity == 1
    assert coverage.unmatched_close_quantity == 1


def test_ambiguous_multi_leg_package_is_excluded_without_inventing_a_strategy() -> None:
    history = normalize_history(
        [
            _multi_order(
                "20",
                (("SELL_TO_OPEN", NVDA_120P, 1, 2.25), ("BUY_TO_OPEN", NVDA_115P, 1, 1.10)),
                OPENED,
                strategy=None,
            )
        ]
    )
    positions, coverage = reconstruct_closed_positions(history.fills)
    assert positions == ()
    assert coverage.ambiguous_package_count == 1
    assert "lacked broker complex-order linkage" in coverage.messages[0]


def test_fifo_matches_the_oldest_exact_contract_lot_first() -> None:
    orders = [
        _single_order("10", "BUY_TO_OPEN", NVDA_120P, 1, 2.00, OPENED, "first"),
        _single_order(
            "11",
            "BUY_TO_OPEN",
            NVDA_120P,
            1,
            3.00,
            OPENED + timedelta(days=1),
            "second",
        ),
        _single_order("12", "SELL_TO_CLOSE", NVDA_120P, 1, 4.00, CLOSED, "close"),
    ]
    history = normalize_history(orders)
    positions, coverage = reconstruct_closed_positions(history.fills)
    assert len(positions) == 1
    assert positions[0].opening_cash_flow == -200
    assert positions[0].realized_pnl == 200
    assert coverage.unmatched_open_quantity == 1


def test_explicit_execution_fees_are_included_in_realized_pnl() -> None:
    opening = _single_order("10", "BUY_TO_OPEN", NVDA_120P, 1, 2.00, OPENED, "open", fee=1)
    closing = _single_order("11", "SELL_TO_CLOSE", NVDA_120P, 1, 3.00, CLOSED, "close", fee=1)
    positions, _coverage = _round_trip(opening, closing)
    position = positions[0]
    assert position.opening_cash_flow == -201
    assert position.closing_cash_flow == 299
    assert position.realized_pnl == 98
    assert position.return_fraction == pytest.approx(98 / 201)
    assert position.fees == 2
    assert position.fees_complete is True


@pytest.mark.parametrize(
    ("close_price", "expected"),
    ((3.0, PositionOutcome.WIN), (1.0, PositionOutcome.LOSS), (2.0, PositionOutcome.BREAKEVEN)),
)
def test_win_loss_and_breakeven_classification(close_price: float, expected: PositionOutcome) -> None:
    positions, _coverage = _round_trip(
        _single_order("10", "BUY_TO_OPEN", NVDA_120P, 1, 2.00, OPENED, "open"),
        _single_order("11", "SELL_TO_CLOSE", NVDA_120P, 1, close_price, CLOSED, "close"),
    )
    assert positions[0].outcome == expected


def test_performance_summary_profit_factor_average_holding_cumulative_and_strategy() -> None:
    win, _ = _round_trip(
        _single_order("10", "BUY_TO_OPEN", NVDA_120P, 1, 2, OPENED, "open-win"),
        _single_order("11", "SELL_TO_CLOSE", NVDA_120P, 1, 3, CLOSED, "close-win"),
    )
    loss, _ = _round_trip(
        _single_order("12", "BUY_TO_OPEN", NVDA_115P, 1, 2, OPENED, "open-loss"),
        _single_order(
            "13",
            "SELL_TO_CLOSE",
            NVDA_115P,
            1,
            1.5,
            CLOSED + timedelta(days=1),
            "close-loss",
        ),
    )
    summary = performance_summary((*win, *loss))
    assert summary.net_realized_pnl == 50
    assert summary.win_rate == 0.5
    assert summary.profit_factor == 2
    expected_days = (
        win[0].holding_days + loss[0].holding_days
    ) / 2
    assert summary.average_days_held == pytest.approx(expected_days)
    assert [point.value for point in summary.cumulative_pnl] == [100, 50]
    assert summary.strategy_performance[0].strategy_label == "Long Put"
    assert summary.strategy_performance[0].realized_pnl == 50


def test_filters_and_grouping_drive_the_same_closed_position_set() -> None:
    may, _ = _round_trip(
        _single_order("10", "BUY_TO_OPEN", NVDA_120P, 1, 2, OPENED, "open"),
        _single_order("11", "SELL_TO_CLOSE", NVDA_120P, 1, 3, CLOSED, "close"),
    )
    april_position = replace(
        may[0],
        position_id="april",
        underlying_symbol="SPY",
        close_time=datetime(2025, 4, 20, tzinfo=UTC),
        strategy_label="Iron Condor",
    )
    filtered = filter_closed_positions(
        (*may, april_position),
        PastPositionFilters(
            date_range="YTD",
            symbol="nv",
            strategy=ALL_STRATEGIES,
            group_by="Month",
        ),
        today=date(2025, 5, 31),
    )
    assert filtered == may
    month_groups = group_closed_positions((*may, april_position), "Month")
    assert [label for label, _rows in month_groups] == ["May 2025", "April 2025"]
    assert [label for label, _rows in group_closed_positions((*may, april_position), "Symbol")] == [
        "NVDA",
        "SPY",
    ]


def test_unavailable_values_are_blank_in_csv_and_named_in_statement() -> None:
    positions, _coverage = _round_trip(
        _single_order("10", "BUY_TO_OPEN", NVDA_120P, 1, 2, OPENED, "open"),
        _single_order("11", "SELL_TO_CLOSE", NVDA_120P, 1, 3, CLOSED, "close"),
    )
    unavailable = replace(
        positions[0],
        position_id="unavailable",
        realized_pnl=None,
        return_fraction=None,
        holding_days=None,
        outcome=None,
        eligible=False,
        unavailable_reasons=("Opening cash flow unavailable",),
    )
    csv_text = positions_csv((unavailable,))
    row = csv_text.splitlines()[1]
    assert ",,,Unavailable," in row
    summary = performance_summary((unavailable,))
    text = statement_text(PastPositionFilters(), summary, (unavailable,))
    assert "Net Realized P/L: Unavailable" in text
    assert "$0.00" not in text


def test_last_valid_snapshot_is_preserved_when_refresh_fails() -> None:
    orders = [
        _single_order("10", "BUY_TO_OPEN", NVDA_120P, 1, 2, OPENED, "open"),
        _single_order("11", "SELL_TO_CLOSE", NVDA_120P, 1, 3, CLOSED, "close"),
    ]

    class Session:
        fail = False

        def get_orders(self, **_kwargs: object) -> list[dict[str, object]]:
            if self.fail:
                raise ConnectionError("offline")
            return orders

        def get_transactions(self, **_kwargs: object) -> list[object]:
            return []

    session = Session()
    service = SchwabPastPositionsService(
        session_factory=lambda: session,
        today=lambda: date(2025, 5, 31),
        now=lambda: datetime(2025, 5, 31, tzinfo=UTC),
    )
    first = service.load()
    session.fail = True
    stale = service.load()
    assert stale.positions == first.positions
    assert stale.stale is True
    assert stale.refresh_error == "ConnectionError: offline"


def test_past_positions_tab_is_immediately_after_templates() -> None:
    assert OPTIONS_COMMAND_TABS == (
        "Discover",
        "Positions",
        "Orders",
        "Templates",
        "Past Positions",
    )


def test_selected_position_detail_and_related_order_callback_routing() -> None:
    positions, _coverage = _round_trip(
        _single_order("10", "BUY_TO_OPEN", NVDA_120P, 1, 2, OPENED, "open"),
        _single_order("11", "SELL_TO_CLOSE", NVDA_120P, 1, 3, CLOSED, "close"),
    )
    state = selected_position_detail_state(positions[0])
    routed: list[tuple[str, ...]] = []
    assert state.title == "NVDA · Long Put"
    assert state.status == "Closed — Win"
    assert state.related_orders_enabled is True
    assert state.duplicate_template_enabled is False
    assert "policies" in state.duplicate_template_reason
    assert route_related_orders(positions[0], routed.append) is True
    assert routed == [("10", "11")]


def test_related_order_workspace_loads_recent_history_when_order_is_not_visible() -> None:
    view = OptionsManagementView.__new__(OptionsManagementView)
    selected_tabs: list[str] = []
    loaded: list[tuple[str, ...]] = []
    view.on_show_orders = lambda: selected_tabs.append("Orders")
    view._visible_order_by_iid = {
        "row": SimpleNamespace(order_id="different-order")
    }
    view.order_table = None
    view.order_detail = SimpleNamespace(set=lambda _value: None)
    view._load_recent_orders = lambda select_order_ids=(): loaded.append(select_order_ids)

    view.show_related_orders(("10", "11"))

    assert selected_tabs == ["Orders"]
    assert loaded == [("10", "11")]


def test_csv_export_formats_current_rows_without_a_save_dialog() -> None:
    positions, _coverage = _round_trip(
        _single_order("10", "BUY_TO_OPEN", NVDA_120P, 1, 2, OPENED, "open"),
        _single_order("11", "SELL_TO_CLOSE", NVDA_120P, 1, 3, CLOSED, "close"),
    )
    exported: list[str] = []
    exporter = exported.append
    exporter(positions_csv(positions))
    assert exported[0].startswith("close_time_utc,underlying,strategy")
    assert "NVDA,Long Put" in exported[0]
    assert "100,50" in exported[0]


def test_snapshot_contract_reports_coverage_without_account_number_exposure() -> None:
    snapshot = snapshot_from_history(
        [
            _single_order(
                "10",
                "BUY_TO_OPEN",
                NVDA_120P,
                1,
                2,
                OPENED,
                "open",
                account_number="123456789",
            ),
            _single_order(
                "11",
                "SELL_TO_CLOSE",
                NVDA_120P,
                1,
                3,
                CLOSED,
                "close",
                account_number="123456789",
            ),
        ],
        range_start=date(2025, 1, 1),
        range_end=date(2025, 5, 31),
        observed_at=datetime(2025, 5, 31, tzinfo=UTC),
    )
    assert snapshot.positions[0].account_label == "Schwab ••••6789"
    assert "123456789" not in repr(snapshot)
    assert snapshot.coverage.fill_count == 2


def _round_trip(
    opening: dict[str, object],
    closing: dict[str, object],
) -> tuple[tuple[object, ...], object]:
    history = normalize_history([opening, closing])
    return reconstruct_closed_positions(history.fills)


def _single_order(
    order_id: str,
    instruction: str,
    symbol: str,
    quantity: float,
    price: float,
    when: datetime,
    execution_id: str,
    *,
    fee: float | None = None,
    asset_type: str = "OPTION",
    account_number: str | None = None,
) -> dict[str, object]:
    execution: dict[str, object] = {
        "legId": 1,
        "quantity": quantity,
        "price": price,
        "time": when.isoformat(),
        "executionId": execution_id,
    }
    if fee is not None:
        execution["fees"] = fee
    order: dict[str, object] = {
        "orderId": order_id,
        "enteredTime": (when - timedelta(minutes=4)).isoformat(),
        "orderLegCollection": [
            {
                "legId": 1,
                "instruction": instruction,
                "quantity": quantity,
                "instrument": _instrument(symbol, asset_type=asset_type),
            }
        ],
        "orderActivityCollection": [
            {
                "activityType": "EXECUTION",
                "activityId": f"activity-{execution_id}",
                "executionLegs": [execution],
            }
        ],
    }
    if account_number:
        order["accountNumber"] = account_number
    return order


def _multi_order(
    order_id: str,
    legs: tuple[tuple[str, str, float, float], ...],
    when: datetime,
    *,
    strategy: str | None,
) -> dict[str, object]:
    order_legs = []
    execution_legs = []
    for index, (instruction, symbol, quantity, price) in enumerate(legs, 1):
        order_legs.append(
            {
                "legId": index,
                "instruction": instruction,
                "quantity": quantity,
                "instrument": _instrument(symbol),
            }
        )
        execution_legs.append(
            {
                "legId": index,
                "quantity": quantity,
                "price": price,
                "time": when.isoformat(),
                "executionId": f"{order_id}-{index}",
            }
        )
    result: dict[str, object] = {
        "orderId": order_id,
        "enteredTime": (when - timedelta(minutes=4)).isoformat(),
        "orderLegCollection": order_legs,
        "orderActivityCollection": [
            {
                "activityType": "EXECUTION",
                "activityId": f"activity-{order_id}",
                "executionLegs": execution_legs,
            }
        ],
    }
    if strategy is not None:
        result["complexOrderStrategyType"] = strategy
    return result


def _instrument(symbol: str, *, asset_type: str = "OPTION") -> dict[str, object]:
    result: dict[str, object] = {"assetType": asset_type, "symbol": symbol}
    if asset_type == "OPTION":
        result["multiplier"] = 100
    return result
