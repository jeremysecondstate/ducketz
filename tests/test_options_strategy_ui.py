from __future__ import annotations

import json
import inspect
import math
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from app.models.portfolio import PortfolioSnapshot
from app.services.schwab_strategy_orders import (
    DAY_ONLY,
    LIMIT_ORDER,
    MARKET_ORDER,
    NET_CREDIT_LIMIT,
    NET_DEBIT_LIMIT,
    build_strategy_order_draft,
    build_strategy_order_payload,
    schwab_position_context,
)
from app.services.strategy_portfolio_impact import (
    calculate_strategy_portfolio_impact,
)
from app.ui.options_strategy_data import (
    HORIZON_LABELS as STRATEGY_HORIZON_LABELS,
    _human_reason,
    load_strategy_candidates,
    portfolio_fit,
)
from app.ui.options_strategies import (
    _CANDIDATE_COLUMNS,
    OptionsStrategiesTab,
    _discover_sash_position,
    _decision_evidence,
    _inline_decision_summary,
    _max_loss_text,
    _profit_loss_text,
    _publication_notice_display,
    _risk_scope_text,
    _sort_candidate_views,
    _worst_case_text,
)
from app.ui.rolling_forecast_data import HORIZON_LABELS as FORECAST_HORIZON_LABELS
from app.ui.schwab_order_messages import order_confirmation_message
from ml.parquet_contracts import (
    STRATEGY_AUDIT_SCHEMA,
    STRATEGY_CANDIDATE_SCHEMA,
    empty_frame,
    write_parquet_with_schema,
)
from ml.strategy_selection.registry import STRATEGY_REGISTRY
from ml.strategy_selection.contracts import (
    BLACK_SCHOLES_CALIBRATED_MODEL_SCORE_BASIS,
    BSGP_CALIBRATED_MODEL_SCORE_BASIS,
    CALIBRATED_MODEL_SCORE_BASIS,
    OPRA_EXECUTION_CALIBRATED_MODEL_SCORE_BASIS,
    SCENARIO_PRIOR_SCORE_BASIS,
    STRATEGY_CANDIDATE_SCHEMA_VERSION,
    STRATEGY_MODEL_POLICY_VERSION,
    STRATEGY_RANKING_POLICY_VERSION,
)


@pytest.mark.parametrize(
    ("column", "attribute"),
    (
        ("direction_probability_up", "direction_probability_up"),
        ("calibrated_probability", "predictive_score"),
        ("scenario_coverage", "scenario_coverage"),
        ("expected_net_profit", "expected_net_profit"),
        ("expected_return", "expected_return"),
    ),
)
def test_ranked_candidate_metrics_sort_numerically_with_blanks_last(
    column: str,
    attribute: str,
) -> None:
    candidates = tuple(
        SimpleNamespace(candidate_id=candidate_id, **{attribute: value})
        for candidate_id, value in (
            ("middle", 12.04),
            ("missing", None),
            ("high", 53.44),
            ("low", -1.09),
        )
    )

    descending = _sort_candidate_views(
        candidates,
        column=column,
        descending=True,
    )
    ascending = _sort_candidate_views(
        candidates,
        column=column,
        descending=False,
    )

    assert [candidate.candidate_id for candidate in descending] == [
        "high",
        "middle",
        "low",
        "missing",
    ]
    assert [candidate.candidate_id for candidate in ascending] == [
        "low",
        "middle",
        "high",
        "missing",
    ]


def test_ranked_candidate_heading_click_defaults_metrics_to_highest_first() -> None:
    class _HeadingTable:
        def __init__(self) -> None:
            self.labels: dict[str, str] = {}

        def heading(self, column: str, *, text: str) -> None:
            self.labels[column] = text

    tab = OptionsStrategiesTab.__new__(OptionsStrategiesTab)
    table = _HeadingTable()
    tab.candidate_table = table
    tab._candidate_sort_column = None
    tab._candidate_sort_descending = False
    renders: list[bool] = []
    tab._render_candidates = lambda: renders.append(True)

    tab._sort_candidates("calibrated_probability")

    assert tab._candidate_sort_descending
    assert table.labels["calibrated_probability"] == "ML Profit Probability ↓"

    tab._sort_candidates("calibrated_probability")

    assert not tab._candidate_sort_descending
    assert table.labels["calibrated_probability"] == "ML Profit Probability ↑"

    tab._sort_candidates("strategy")

    assert not tab._candidate_sort_descending
    assert table.labels["calibrated_probability"] == "ML Profit Probability"
    assert table.labels["strategy"] == "Strategy ↑"
    assert renders == [True, True, True]


def test_options_strategy_weekly_labels_match_rolling_forecast_labels() -> None:
    assert STRATEGY_HORIZON_LABELS == FORECAST_HORIZON_LABELS


def test_candidate_table_preserves_declared_widths_for_horizontal_scroll() -> None:
    source = inspect.getsource(OptionsStrategiesTab._build_ranking)
    render_source = inspect.getsource(OptionsStrategiesTab._render_candidates)

    assert "minwidth=width" in source
    assert any(name == "expected_net_profit" for name, *_rest in _CANDIDATE_COLUMNS)
    assert "_money(candidate.expected_net_profit)" in render_source


def test_discover_layout_reserves_readable_ticket_at_canonical_width() -> None:
    width = 1120
    position = _discover_sash_position(width)

    assert position == 739
    assert width - position >= 360


def test_discover_resize_applies_the_readable_split() -> None:
    class _Panes:
        def __init__(self) -> None:
            self.position = 900

        def winfo_width(self) -> int:
            return 1120

        def sashpos(self, _index: int, position: int | None = None) -> int:
            if position is not None:
                self.position = position
            return self.position

    panes = _Panes()
    tab = OptionsStrategiesTab.__new__(OptionsStrategiesTab)
    tab._discover_panes = panes

    tab._resize_discover_panes()

    assert panes.position == 739


@pytest.mark.parametrize(
    (
        "basis",
        "model_status",
        "pricing_source",
        "calibrated",
        "label",
        "actionable",
    ),
    (
        (
            BSGP_CALIBRATED_MODEL_SCORE_BASIS,
            "MODEL_FIT",
            "BSGP",
            0.61,
            "BSGP + Strategy ML",
            True,
        ),
        (
            BLACK_SCHOLES_CALIBRATED_MODEL_SCORE_BASIS,
            "MODEL_FIT",
            "BLACK_SCHOLES",
            0.61,
            "Black-Scholes + ML",
            True,
        ),
        (
            OPRA_EXECUTION_CALIBRATED_MODEL_SCORE_BASIS,
            "MODEL_FIT",
            "UNAVAILABLE",
            0.61,
            "OPRA Execution + ML",
            False,
        ),
        (
            SCENARIO_PRIOR_SCORE_BASIS,
            "HEURISTIC_ONLY",
            "BLACK_SCHOLES",
            float("nan"),
            "Scenario Coverage",
            False,
        ),
    ),
)
def test_strategy_loader_displays_every_pricing_score_basis(
    tmp_path: Path,
    basis: str,
    model_status: str,
    pricing_source: str,
    calibrated: float,
    label: str,
    actionable: bool,
) -> None:
    candidate = _candidate(
        strategy_name="long_call",
        strategy_display_name="Long Call",
        legs=[
            _option_leg(
                side="LONG",
                option_type="CALL",
                strike=105,
                symbol="GOOG  260918C00105000",
                bid=2.40,
                ask=2.50,
            )
        ],
        score_basis=basis,
        model_status=model_status,
        pricing_source=pricing_source,
        calibrated_profit_probability=calibrated,
        raw_profit_probability=(
            0.61 if model_status == "MODEL_FIT" else float("nan")
        ),
        decision_score=(
            0.61 if model_status == "MODEL_FIT" else float("nan")
        ),
        scenario_coverage_score=0.61,
        pricing_status=(
            "Active"
            if pricing_source == "BSGP"
            else "Black-Scholes fallback"
            if pricing_source == "BLACK_SCHOLES"
            else "Unavailable"
        ),
    )
    path = tmp_path / f"{basis}.parquet"
    write_parquet_with_schema(
        pd.DataFrame([candidate]), path, STRATEGY_CANDIDATE_SCHEMA
    )
    view = load_strategy_candidates(
        path,
        snapshot=PortfolioSnapshot(
            source="schwab",
            account_label="Schwab",
            synced_at=datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc),
            account_facts={},
        ),
    )
    assert view.candidates[0].score_basis == label
    if model_status == "MODEL_FIT":
        assert view.candidates[0].predictive_score == pytest.approx(61.0)
    else:
        assert view.candidates[0].predictive_score is None
    assert view.candidates[0].manual_order_actionable is actionable
    assert view.candidates[0].scenario_coverage == pytest.approx(61.0)


def test_strategy_loader_accepts_previous_model_policy_during_upgrade(
    tmp_path: Path,
) -> None:
    candidate = _candidate(
        strategy_name="long_call",
        strategy_display_name="Long Call",
        legs=[
            _option_leg(
                side="LONG",
                option_type="CALL",
                strike=105,
                symbol="GOOG  260918C00105000",
                bid=2.40,
                ask=2.50,
            )
        ],
        model_policy_version="pricing-market-state-hgb-platt-return-v6",
    )
    path = tmp_path / "strategy-candidates.parquet"
    write_parquet_with_schema(
        pd.DataFrame([candidate]), path, STRATEGY_CANDIDATE_SCHEMA
    )

    view = load_strategy_candidates(
        path,
        snapshot=PortfolioSnapshot(
            source="schwab",
            account_label="Schwab",
            synced_at=datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc),
            account_facts={},
        ),
    )

    assert view.candidates[0].predictive_score == pytest.approx(61.0)


def test_position_context_reads_equity_options_cash_and_working_orders() -> None:
    observed = datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc)
    context = schwab_position_context(
        {
            "positions": {
                "items": [
                    {
                        "asset_type": "EQUITY",
                        "symbol": "GOOG",
                        "net_quantity": 125,
                    },
                    {
                        "asset_type": "OPTION",
                        "symbol": "GOOG  260918C00110000",
                        "underlying_symbol": "GOOG",
                        "net_quantity": -2,
                    },
                ]
            },
            "working_orders": {
                "items": [
                    {
                        "asset_type": "OPTION",
                        "underlying_symbol": "GOOG",
                    },
                    {
                        "asset_type": "OPTION",
                        "underlying_symbol": "AAPL",
                    },
                ]
            },
            "account_values": {
                "status": "CURRENT",
                "available_funds": 15_000,
                "available_funds_non_marginable_trade": 8_000,
                "cash_balance": 6_000,
                "buying_power": 30_000,
                "liquidation_value": 45_000,
            },
        },
        symbol="goog",
        observed_at=observed,
    )

    assert context.symbol == "GOOG"
    assert context.observed_at == observed
    assert context.shares == 125
    assert context.option_contracts == 2
    assert context.working_option_orders == 1
    assert context.available_cash == 15_000
    assert context.available_cash_source == "Available Funds"
    assert context.non_marginable_funds == 8_000
    assert context.cash_balance == 6_000
    assert context.buying_power == 30_000
    assert context.liquidation_value == 45_000
    assert context.account_values_status == "CURRENT"


def test_vertical_candidate_builds_one_net_debit_schwab_order() -> None:
    candidate = _candidate(
        strategy_name="bear_put_spread",
        strategy_display_name="Bear Put Spread",
        legs=[
            _option_leg(
                side="LONG",
                option_type="PUT",
                strike=105,
                symbol="GOOG  260918P00105000",
                bid=2.40,
                ask=2.50,
            ),
            _option_leg(
                side="SHORT",
                option_type="PUT",
                strike=95,
                symbol="GOOG  260918P00095000",
                bid=1.20,
                ask=1.30,
            ),
        ],
    )
    position = schwab_position_context({}, symbol="GOOG")

    draft = build_strategy_order_draft(candidate, position=position)
    order = draft.orders[0]
    payload = build_strategy_order_payload(
        draft,
        strategy_quantity=2,
        order_method=NET_DEBIT_LIMIT,
        limit_price="1.30",
        duration=DAY_ONLY,
    )

    assert order.suggested_order_method == NET_DEBIT_LIMIT
    assert order.suggested_limit_price == pytest.approx(1.30)
    assert order.complex_order_strategy_type == "VERTICAL"
    assert payload == {
        "orderType": "NET_DEBIT",
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [
            {
                "instruction": "BUY_TO_OPEN",
                "quantity": 2,
                "instrument": {
                    "symbol": "GOOG  260918P00105000",
                    "assetType": "OPTION",
                },
            },
            {
                "instruction": "SELL_TO_OPEN",
                "quantity": 2,
                "instrument": {
                    "symbol": "GOOG  260918P00095000",
                    "assetType": "OPTION",
                },
            },
        ],
        "complexOrderStrategyType": "VERTICAL",
        "quantity": 2,
        "price": 1.30,
    }

    market = build_strategy_order_payload(
        draft,
        strategy_quantity=1,
        order_method=MARKET_ORDER,
        limit_price=None,
        duration=DAY_ONLY,
    )
    assert market["orderType"] == "MARKET"
    assert "price" not in market


def test_covered_call_uses_reported_shares_without_buying_them_again() -> None:
    candidate = _candidate(
        strategy_name="covered_call",
        strategy_display_name="Covered Call",
        stock_requirement="EXISTING_100_SHARES",
        legs=[
            _stock_leg(bid=104.90, ask=105.10),
            _option_leg(
                side="SHORT",
                option_type="CALL",
                strike=110,
                symbol="GOOG  260918C00110000",
                bid=2.00,
                ask=2.10,
            ),
        ],
    )
    position = schwab_position_context(
        {
            "positions": {
                "items": [
                    {
                        "asset_type": "EQUITY",
                        "symbol": "GOOG",
                        "net_quantity": 100,
                    }
                ]
            }
        },
        symbol="GOOG",
    )

    draft = build_strategy_order_draft(candidate, position=position)
    order = draft.orders[0]
    assert draft.uses_existing_shares is True
    assert draft.shares_required_per_strategy == 100
    assert len(order.legs) == 1
    assert order.legs[0].instruction == "SELL_TO_OPEN"

    payload = build_strategy_order_payload(
        draft,
        strategy_quantity=1,
        order_method=order.suggested_order_method,
        limit_price=order.suggested_limit_price,
        duration=DAY_ONLY,
    )
    assert len(payload["orderLegCollection"]) == 1
    assert payload["orderType"] == "LIMIT"
    assert "complexOrderStrategyType" not in payload

    with pytest.raises(ValueError, match="requires 200 GOOG shares"):
        build_strategy_order_payload(
            draft,
            strategy_quantity=2,
            order_method=order.suggested_order_method,
            limit_price=order.suggested_limit_price,
            duration=DAY_ONLY,
        )


def test_buy_write_keeps_stock_and_option_in_one_covered_order() -> None:
    candidate = _candidate(
        strategy_name="buy_write",
        strategy_display_name="Buy Write",
        stock_requirement="BUY_100_SHARES_ATOMICALLY",
        legs=[
            _stock_leg(bid=104.90, ask=105.10),
            _option_leg(
                side="SHORT",
                option_type="CALL",
                strike=110,
                symbol="GOOG  260918C00110000",
                bid=2.00,
                ask=2.10,
            ),
        ],
    )
    draft = build_strategy_order_draft(
        candidate,
        position=schwab_position_context({}, symbol="GOOG"),
    )
    order = draft.orders[0]

    assert [leg.asset_type for leg in order.legs] == ["EQUITY", "OPTION"]
    assert order.complex_order_strategy_type == "COVERED"
    assert order.suggested_limit_price == pytest.approx(103.10)

    payload = build_strategy_order_payload(
        draft,
        strategy_quantity=2,
        order_method=order.suggested_order_method,
        limit_price=order.suggested_limit_price,
        duration=DAY_ONLY,
    )
    assert payload["complexOrderStrategyType"] == "COVERED"
    assert payload["quantity"] == 2
    assert [
        leg["quantity"] for leg in payload["orderLegCollection"]
    ] == [200, 2]


def test_credit_spread_builds_a_net_credit_order() -> None:
    candidate = _candidate(
        strategy_name="bull_put_spread",
        strategy_display_name="Bull Put Spread",
        legs=[
            _option_leg(
                side="LONG",
                option_type="PUT",
                strike=95,
                symbol="GOOG  260918P00095000",
                bid=0.90,
                ask=1.00,
            ),
            _option_leg(
                side="SHORT",
                option_type="PUT",
                strike=105,
                symbol="GOOG  260918P00105000",
                bid=2.00,
                ask=2.10,
            ),
        ],
    )
    draft = build_strategy_order_draft(
        candidate,
        position=schwab_position_context({}, symbol="GOOG"),
    )
    order = draft.orders[0]

    assert order.suggested_order_method == NET_CREDIT_LIMIT
    assert order.suggested_limit_price == pytest.approx(1.00)
    payload = build_strategy_order_payload(
        draft,
        strategy_quantity=1,
        order_method=order.suggested_order_method,
        limit_price=order.suggested_limit_price,
        duration=DAY_ONLY,
    )
    assert payload["orderType"] == "NET_CREDIT"
    assert payload["complexOrderStrategyType"] == "VERTICAL"


def test_twin_peak_fly_builds_two_complete_butterfly_orders() -> None:
    candidate = _candidate(
        strategy_name="twin_peak_fly",
        strategy_display_name="Twin-Peak Fly",
        legs=[
            _option_leg(
                side="LONG",
                option_type="CALL",
                strike=90,
                symbol="GOOG  260918C00090000",
                bid=6.90,
                ask=7.00,
            ),
            _option_leg(
                side="SHORT",
                option_type="CALL",
                strike=95,
                symbol="GOOG  260918C00095000",
                bid=4.90,
                ask=5.00,
                quantity=2,
            ),
            _option_leg(
                side="LONG",
                option_type="CALL",
                strike=100,
                symbol="GOOG  260918C00100000",
                bid=3.90,
                ask=4.00,
                quantity=2,
            ),
            _option_leg(
                side="SHORT",
                option_type="CALL",
                strike=105,
                symbol="GOOG  260918C00105000",
                bid=2.90,
                ask=3.00,
                quantity=2,
            ),
            _option_leg(
                side="LONG",
                option_type="CALL",
                strike=110,
                symbol="GOOG  260918C00110000",
                bid=1.90,
                ask=2.00,
            ),
        ],
    )
    draft = build_strategy_order_draft(
        candidate,
        position=schwab_position_context({}, symbol="GOOG"),
    )

    assert [order.display_name for order in draft.orders] == [
        "Lower-price butterfly",
        "Higher-price butterfly",
    ]
    assert [order.complex_order_strategy_type for order in draft.orders] == [
        "BUTTERFLY",
        "BUTTERFLY",
    ]
    assert [[leg.quantity for leg in order.legs] for order in draft.orders] == [
        [1, 2, 1],
        [1, 2, 1],
    ]
    for order_index, order in enumerate(draft.orders):
        payload = build_strategy_order_payload(
            draft,
            order_index=order_index,
            strategy_quantity=1,
            order_method=order.suggested_order_method,
            limit_price=order.suggested_limit_price,
            duration=DAY_ONLY,
        )
        assert payload["complexOrderStrategyType"] == "BUTTERFLY"
        assert len(payload["orderLegCollection"]) == 3


def test_range_to_trend_relay_builds_condor_and_strangle_orders() -> None:
    candidate = _candidate(
        strategy_name="range_to_trend_relay",
        strategy_display_name="Range-to-Trend Relay",
        legs=[
            _option_leg(
                side="LONG",
                option_type="PUT",
                strike=90,
                symbol="GOOG  260918P00090000",
                bid=0.90,
                ask=1.00,
            ),
            _option_leg(
                side="SHORT",
                option_type="PUT",
                strike=95,
                symbol="GOOG  260918P00095000",
                bid=1.90,
                ask=2.00,
            ),
            _option_leg(
                side="SHORT",
                option_type="CALL",
                strike=105,
                symbol="GOOG  260918C00105000",
                bid=1.90,
                ask=2.00,
            ),
            _option_leg(
                side="LONG",
                option_type="CALL",
                strike=110,
                symbol="GOOG  260918C00110000",
                bid=0.90,
                ask=1.00,
            ),
            _option_leg(
                side="LONG",
                option_type="PUT",
                strike=95,
                symbol="GOOG  261218P00095000",
                bid=2.90,
                ask=3.00,
                expiration="2026-12-18T00:00:00Z",
            ),
            _option_leg(
                side="LONG",
                option_type="CALL",
                strike=105,
                symbol="GOOG  261218C00105000",
                bid=2.90,
                ask=3.00,
                expiration="2026-12-18T00:00:00Z",
            ),
        ],
    )
    draft = build_strategy_order_draft(
        candidate,
        position=schwab_position_context({}, symbol="GOOG"),
    )

    assert [order.display_name for order in draft.orders] == [
        "Near-expiration iron condor",
        "Later-expiration long strangle",
    ]
    assert [order.complex_order_strategy_type for order in draft.orders] == [
        "IRON_CONDOR",
        "STRANGLE",
    ]
    assert [len(order.legs) for order in draft.orders] == [4, 2]


@pytest.mark.parametrize("strategy_name", tuple(STRATEGY_REGISTRY))
def test_every_registered_strategy_builds_complete_schwab_orders(
    strategy_name: str,
) -> None:
    definition = STRATEGY_REGISTRY[strategy_name]
    legs: list[dict[str, object]] = []
    for leg in definition.legs:
        if leg.asset == "STOCK":
            legs.append(_stock_leg(bid=104.90, ask=105.10))
            continue
        strike = 105.0 + 5.0 * float(leg.strike_offset or 0)
        expiration = (
            "2026-12-18T00:00:00Z"
            if leg.expiration_role == "BACK"
            else "2026-09-18T00:00:00Z"
        )
        expiration_code = "261218" if leg.expiration_role == "BACK" else "260918"
        option_code = "C" if leg.option_type == "CALL" else "P"
        legs.append(
            _option_leg(
                side=leg.side,
                option_type=str(leg.option_type),
                strike=strike,
                symbol=(
                    f"GOOG  {expiration_code}{option_code}"
                    f"{int(round(strike * 1000)):08d}"
                ),
                bid=1.00,
                ask=1.10,
                quantity=leg.quantity,
                expiration=expiration,
            )
        )
    candidate = _candidate(
        strategy_name=definition.name,
        strategy_display_name=definition.display_name,
        stock_requirement=definition.stock_requirement,
        legs=legs,
    )
    position = schwab_position_context(
        {
            "positions": {
                "items": [
                    {
                        "asset_type": "EQUITY",
                        "symbol": "GOOG",
                        "net_quantity": 100,
                    }
                ]
            },
            "account_values": {"available_funds": 100_000},
        },
        symbol="GOOG",
    )

    draft = build_strategy_order_draft(candidate, position=position)

    assert draft.orders
    assert sum(len(order.legs) for order in draft.orders) >= 1
    for order_index, order in enumerate(draft.orders):
        assert 1 <= len(order.legs) <= 4
        payload = build_strategy_order_payload(
            draft,
            order_index=order_index,
            strategy_quantity=1,
            order_method=order.suggested_order_method,
            limit_price=order.suggested_limit_price,
            duration=DAY_ONLY,
        )
        assert payload["orderLegCollection"]


def test_strategy_loader_combines_model_output_with_current_position(
    tmp_path: Path,
) -> None:
    candidate = _candidate(
        strategy_name="bear_put_spread",
        strategy_display_name="Bear Put Spread",
        legs=[
            _option_leg(
                side="LONG",
                option_type="PUT",
                strike=105,
                symbol="GOOG  260918P00105000",
                bid=2.40,
                ask=2.50,
            ),
            _option_leg(
                side="SHORT",
                option_type="PUT",
                strike=95,
                symbol="GOOG  260918P00095000",
                bid=1.20,
                ask=1.30,
            ),
        ],
        decision_score=0.62,
        calibrated_profit_probability=0.62,
        expected_return_on_risk=0.08,
        net_delta=-25.0,
        candidate_rank=1,
    )
    path = tmp_path / "strategy-candidates.parquet"
    write_parquet_with_schema(
        pd.DataFrame([candidate]),
        path,
        STRATEGY_CANDIDATE_SCHEMA,
    )
    snapshot = PortfolioSnapshot(
        source="schwab",
        account_label="Schwab",
        synced_at=datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc),
        account_facts={
            "positions": {
                "items": [
                    {
                        "asset_type": "EQUITY",
                        "symbol": "GOOG",
                        "net_quantity": 100,
                    }
                ]
            }
        },
    )

    view = load_strategy_candidates(path, snapshot=snapshot)

    assert view.symbols == ("GOOG",)
    assert view.horizons_by_symbol == {"GOOG": ("1d",)}
    assert len(view.candidates) == 1
    result = view.candidates[0]
    assert result.exact_legs == "Buy 1 $105 Put · Sell 1 $95 Put"
    assert result.portfolio_fit.label == "Downside Hedge"
    assert result.direction_probability_up == pytest.approx(55.0)
    assert result.predictive_score == pytest.approx(62.0)
    assert 0.0 <= result.predictive_score <= 100.0
    assert result.score_basis == "BSGP + Strategy ML"
    assert (
        result.order_draft.orders[0].complex_order_strategy_type
        == "VERTICAL"
    )


def test_strategy_loader_uses_market_state_prior_until_calibration_exists(
    tmp_path: Path,
) -> None:
    candidate = _candidate(
        strategy_name="long_call",
        strategy_display_name="Long Call",
        legs=[
            _option_leg(
                side="LONG",
                option_type="CALL",
                strike=105,
                symbol="GOOG  260918C00105000",
                bid=2.40,
                ask=2.50,
            )
        ],
        raw_profit_probability=float("nan"),
        calibrated_profit_probability=float("nan"),
        expected_net_profit=12.0,
        expected_return_on_risk=0.04,
        decision_score=float("nan"),
        scenario_coverage_score=0.57,
        candidate_rank=1,
        model_status="HEURISTIC_ONLY",
        model_version="pricing-greek-bbo-scenario-coverage-v4",
        score_basis=SCENARIO_PRIOR_SCORE_BASIS,
    )
    path = tmp_path / "strategy-candidates.parquet"
    write_parquet_with_schema(
        pd.DataFrame([candidate]),
        path,
        STRATEGY_CANDIDATE_SCHEMA,
    )

    view = load_strategy_candidates(
        path,
        snapshot=PortfolioSnapshot(
            source="schwab",
            account_label="Schwab",
            synced_at=datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc),
            account_facts={},
        ),
    )

    assert view.candidates[0].predictive_score is None
    assert view.candidates[0].scenario_coverage == pytest.approx(57.0)
    assert not view.candidates[0].manual_order_actionable
    assert view.candidates[0].expected_return == pytest.approx(0.04)
    assert view.candidates[0].score_basis == "Scenario Coverage"


def test_strategy_loader_marks_heuristic_quality_failures_research_only(
    tmp_path: Path,
) -> None:
    candidate = _candidate(
        strategy_name="long_call",
        strategy_display_name="Long Call",
        legs=[
            _option_leg(
                side="LONG",
                option_type="CALL",
                strike=105,
                symbol="GOOG  260918C00105000",
                bid=2.40,
                ask=2.50,
            )
        ],
        score_basis=SCENARIO_PRIOR_SCORE_BASIS,
        model_status="HEURISTIC_ONLY",
        raw_profit_probability=float("nan"),
        calibrated_profit_probability=float("nan"),
        decision_score=float("nan"),
        scenario_coverage_score=1.0,
        pricing_status="Unavailable",
        pricing_source="UNAVAILABLE",
        pricing_leg_coverage=0.0,
        pricing_missing_reason="PREDICTION_MISSING",
        surface_quality_pass=False,
        liquidity_policy_pass=False,
        all_option_quotes_valid=True,
        max_relative_spread=0.50,
    )
    path = tmp_path / "strategy-candidates.parquet"
    write_parquet_with_schema(
        pd.DataFrame([candidate]),
        path,
        STRATEGY_CANDIDATE_SCHEMA,
    )

    view = load_strategy_candidates(
        path,
        snapshot=PortfolioSnapshot(
            source="schwab",
            account_label="Schwab",
            account_facts={},
        ),
    )
    result = view.candidates[0]
    assert result.predictive_score is None
    assert result.scenario_coverage == 100.0
    assert result.pricing_summary == "Unavailable pricing · Unavailable · Prediction Missing"
    assert "surface policy failed" in result.quality_warning
    assert "liquidity policy failed" in result.quality_warning
    assert "50.00% exceeds the 35.00% OPRA execution gate" in (
        result.quality_warning
    )
    assert not result.manual_order_actionable
    assert result.manual_actionability.startswith(
        "Publication model-pricing checks are incomplete"
    )
    assert "current account facts" in result.manual_actionability


def test_opra_model_evidence_uses_its_own_quality_gate_and_shows_ev(
    tmp_path: Path,
) -> None:
    candidate = _candidate(
        strategy_name="long_call",
        strategy_display_name="Long Call",
        legs=[
            _option_leg(
                side="LONG",
                option_type="CALL",
                strike=105,
                symbol="GOOG  260918C00105000",
                bid=2.40,
                ask=2.50,
            )
        ],
        score_basis=OPRA_EXECUTION_CALIBRATED_MODEL_SCORE_BASIS,
        pricing_source="UNAVAILABLE",
        pricing_status="Unavailable",
        pricing_leg_coverage=0.0,
        surface_quality_pass=False,
        liquidity_policy_pass=False,
        all_option_quotes_valid=True,
        expected_net_profit=12.34,
    )
    path = tmp_path / "strategy-candidates.parquet"
    write_parquet_with_schema(
        pd.DataFrame([candidate]),
        path,
        STRATEGY_CANDIDATE_SCHEMA,
    )

    result = load_strategy_candidates(
        path,
        snapshot=PortfolioSnapshot(
            source="schwab",
            account_label="Schwab",
            account_facts={},
        ),
    ).candidates[0]
    evidence = _decision_evidence(result)

    assert result.quality_warning == "OPRA execution evidence gate passed"
    assert not result.manual_order_actionable
    assert evidence.expected_net_profit == pytest.approx(12.34)
    assert evidence.publication_checks == (
        ("Contract quote fields", True),
        ("OPRA execution evidence gate", True),
    )


def test_discover_empty_state_surfaces_matching_publication_audit_reason(
    tmp_path: Path,
) -> None:
    candidates_path = tmp_path / "strategy-candidates.parquet"
    audit_path = tmp_path / "strategy-audit.parquet"
    write_parquet_with_schema(
        empty_frame(STRATEGY_CANDIDATE_SCHEMA),
        candidates_path,
        STRATEGY_CANDIDATE_SCHEMA,
    )
    dominant_reason = (
        "ValueError: No standard contracts contain a numerically usable BBO"
    )
    audit_rows = []
    for index, reason in enumerate(
        (dominant_reason, dominant_reason, "insufficient strike depth")
    ):
        strategy_name = f"strategy_{index}"
        audit_rows.append(
            {
                "id": f"GOOG|1d|2026-08-01T15:00:00Z|{strategy_name}",
                "symbol": "GOOG",
                "horizon": "1d",
                "decision_timestamp": pd.Timestamp("2026-08-01T15:00:00Z"),
                "strategy_name": strategy_name,
                "strategy_display_name": strategy_name.title(),
                "strategy_family": "TEST",
                "account_approval": "SPREADS",
                "authorization_status": "AUTHORIZED_SPREADS",
                "construction_status": "CANDIDATE_CONSTRUCTION_FAILED",
                "candidate_count": 0,
                "reason": reason,
                "registry_version": "test-registry",
                "candidate_policy_version": "test-candidates",
            }
        )
    write_parquet_with_schema(
        pd.DataFrame(audit_rows),
        audit_path,
        STRATEGY_AUDIT_SCHEMA,
    )
    view = load_strategy_candidates(
        candidates_path,
        snapshot=PortfolioSnapshot(
            source="schwab",
            account_label="Schwab",
            account_facts={},
        ),
    )

    assert view.audit_source_path == audit_path
    assert view.symbols == ("GOOG",)
    assert view.horizons_by_symbol == {"GOOG": ("1d",)}
    assert view.route_diagnoses == {("GOOG", "1d"): dominant_reason}
    assert view.empty_diagnosis == dominant_reason

    tab = OptionsStrategiesTab.__new__(OptionsStrategiesTab)
    tab.view = view
    tab.candidate_table = object()
    tab.symbol = _Value("GOOG")
    tab.horizon_label = _Value("1 Day")
    tab.position_summary = _Value("")
    tab.candidate_summary = _Value("")
    tab.visible_candidates = ()
    tab._clear_table = lambda _table: None
    tab._clear_ticket = lambda: None

    tab._render_candidates()

    assert tab.position_summary.get() == f"No candidates: {dominant_reason}"
    assert tab.candidate_summary.get() == "0 Candidates"


def test_exact_legs_match_whether_protective_stock_is_held_or_bought(
    tmp_path: Path,
) -> None:
    candidate = _candidate(
        strategy_name="protective_put",
        strategy_display_name="Protective / Married Put",
        stock_requirement="EXISTING_OR_ATOMIC_100_SHARES",
        legs=[
            _stock_leg(bid=104.90, ask=105.10),
            _option_leg(
                side="LONG",
                option_type="PUT",
                strike=95,
                symbol="GOOG  260918P00095000",
                bid=1.20,
                ask=1.30,
            ),
        ],
    )
    path = tmp_path / "strategy-candidates.parquet"
    write_parquet_with_schema(
        pd.DataFrame([candidate]),
        path,
        STRATEGY_CANDIDATE_SCHEMA,
    )
    without_shares = PortfolioSnapshot(
        source="schwab",
        account_label="Schwab",
        account_facts={"positions": {"items": []}},
    )
    with_shares = PortfolioSnapshot(
        source="schwab",
        account_label="Schwab",
        account_facts={
            "positions": {
                "items": [
                    {
                        "asset_type": "EQUITY",
                        "symbol": "GOOG",
                        "net_quantity": 100,
                    }
                ]
            }
        },
    )

    bought = load_strategy_candidates(path, snapshot=without_shares)
    held = load_strategy_candidates(path, snapshot=with_shares)

    assert bought.candidates[0].exact_legs.startswith("Buy 100 shares")
    assert held.candidates[0].exact_legs.startswith("Use 100 shares")
    assert bought.candidates[0].order_draft.orders[0].legs[0].instruction == "BUY"
    assert [
        leg.asset_type for leg in held.candidates[0].order_draft.orders[0].legs
    ] == ["OPTION"]


def test_portfolio_state_changes_fit_text_but_not_predictive_score_or_rank(
    tmp_path: Path,
) -> None:
    candidate = _candidate(
        strategy_name="covered_strangle",
        strategy_display_name="Covered Strangle / Combination",
        stock_requirement="EXISTING_100_SHARES",
        cash_requirement="PUT_STRIKE_TIMES_MULTIPLIER",
        legs=[
            _stock_leg(bid=104.90, ask=105.10),
            _option_leg(
                side="SHORT",
                option_type="PUT",
                strike=95,
                symbol="GOOG  260918P00095000",
                bid=1.20,
                ask=1.30,
            ),
            _option_leg(
                side="SHORT",
                option_type="CALL",
                strike=110,
                symbol="GOOG  260918C00110000",
                bid=2.00,
                ask=2.10,
            ),
        ],
        decision_score=0.73,
        calibrated_profit_probability=0.73,
        candidate_rank=1,
    )
    path = tmp_path / "strategy-candidates.parquet"
    write_parquet_with_schema(
        pd.DataFrame([candidate]),
        path,
        STRATEGY_CANDIDATE_SCHEMA,
    )
    constrained = PortfolioSnapshot(
        source="schwab",
        account_label="Schwab",
        account_facts={
            "positions": {"items": []},
            "account_values": {"available_funds": 5_000},
        },
    )
    covered = PortfolioSnapshot(
        source="schwab",
        account_label="Schwab",
        account_facts={
            "positions": {
                "items": [
                    {
                        "asset_type": "EQUITY",
                        "symbol": "GOOG",
                        "net_quantity": 100,
                    }
                ]
            },
            "account_values": {"available_funds": 15_000},
        },
    )

    constrained_view = load_strategy_candidates(path, snapshot=constrained)
    covered_view = load_strategy_candidates(path, snapshot=covered)
    constrained_candidate = constrained_view.candidates[0]
    covered_candidate = covered_view.candidates[0]

    assert constrained_candidate.portfolio_fit.label != covered_candidate.portfolio_fit.label
    assert constrained_candidate.predictive_score == covered_candidate.predictive_score == 73.0
    assert constrained_candidate.rank == covered_candidate.rank == 1
    assert constrained_candidate.row["candidate_rank"] == covered_candidate.row["candidate_rank"] == 1


def test_strategy_loader_preserves_complete_deterministic_market_ranks(
    tmp_path: Path,
) -> None:
    rank_two = _candidate(
        strategy_name="long_put",
        strategy_display_name="Long Put",
        legs=[
            _option_leg(
                side="LONG",
                option_type="PUT",
                strike=95,
                symbol="GOOG  260918P00095000",
                bid=1.20,
                ask=1.30,
            )
        ],
        decision_score=0.40,
        calibrated_profit_probability=0.40,
        candidate_rank=2,
    )
    rank_one = _candidate(
        strategy_name="long_call",
        strategy_display_name="Long Call",
        legs=[
            _option_leg(
                side="LONG",
                option_type="CALL",
                strike=105,
                symbol="GOOG  260918C00105000",
                bid=2.40,
                ask=2.50,
            )
        ],
        decision_score=0.80,
        calibrated_profit_probability=0.80,
        candidate_rank=1,
    )
    path = tmp_path / "strategy-candidates.parquet"
    write_parquet_with_schema(
        pd.DataFrame([rank_two, rank_one]),
        path,
        STRATEGY_CANDIDATE_SCHEMA,
    )

    view = load_strategy_candidates(
        path,
        snapshot=PortfolioSnapshot(
            source="schwab",
            account_label="Schwab",
            account_facts={},
        ),
    )

    assert [candidate.strategy_name for candidate in view.candidates] == [
        "long_call",
        "long_put",
    ]
    assert [candidate.rank for candidate in view.candidates] == [1, 2]
    assert all(
        math.isfinite(candidate.predictive_score)
        and 0.0 <= candidate.predictive_score <= 100.0
        for candidate in view.candidates
    )


def test_portfolio_fit_combines_held_shares_and_available_funds() -> None:
    candidate = _candidate(
        strategy_name="covered_strangle",
        strategy_display_name="Covered Strangle / Combination",
        stock_requirement="EXISTING_100_SHARES",
        cash_requirement="PUT_STRIKE_TIMES_MULTIPLIER",
        legs=[
            _stock_leg(bid=104.90, ask=105.10),
            _option_leg(
                side="SHORT",
                option_type="PUT",
                strike=95,
                symbol="GOOG  260918P00095000",
                bid=1.20,
                ask=1.30,
            ),
            _option_leg(
                side="SHORT",
                option_type="CALL",
                strike=110,
                symbol="GOOG  260918C00110000",
                bid=2.00,
                ask=2.10,
            ),
        ],
    )
    position = schwab_position_context(
        {
            "positions": {
                "items": [
                    {
                        "asset_type": "EQUITY",
                        "symbol": "GOOG",
                        "net_quantity": 100,
                    }
                ]
            },
            "account_values": {"available_funds": 15_000},
        },
        symbol="GOOG",
    )

    result = portfolio_fit(candidate, position=position)

    assert result.label == "Uses Shares and Funds"
    assert "$15,000.00" in result.detail
    assert "$9,500.00" in result.detail


def test_portfolio_fit_reports_funds_below_cash_secured_estimate() -> None:
    candidate = _candidate(
        strategy_name="cash_secured_put",
        strategy_display_name="Cash-Secured Put",
        cash_requirement="STRIKE_TIMES_MULTIPLIER",
        legs=[
            _option_leg(
                side="SHORT",
                option_type="PUT",
                strike=95,
                symbol="GOOG  260918P00095000",
                bid=1.20,
                ask=1.30,
            ),
        ],
    )
    position = schwab_position_context(
        {
            "account_values": {
                "available_funds": 50_000,
                "available_funds_non_marginable_trade": 5_000,
            }
        },
        symbol="GOOG",
    )

    result = portfolio_fit(candidate, position=position)

    assert result.label == "Funds Below Estimate"
    assert "$5,000.00" in result.detail
    assert "$9,500.00" in result.detail
    assert "non-marginable funds" in result.detail


def test_cash_secured_impact_uses_current_non_marginable_schwab_funds() -> None:
    candidate = _candidate(
        strategy_name="cash_secured_put",
        strategy_display_name="Cash-Secured Put",
        cash_requirement="STRIKE_TIMES_MULTIPLIER",
        capital_required=30_755.0,
        legs=[
            _option_leg(
                side="SHORT",
                option_type="PUT",
                strike=307.5,
                symbol="GOOG  260918P00307500",
                bid=3.25,
                ask=3.35,
            )
        ],
    )
    position = schwab_position_context(
        {
            "account_values": {
                "status": "CURRENT",
                "available_funds": 103_046.94,
                "available_funds_non_marginable_trade": 50_000.0,
                "cash_balance": 24_000.0,
                "buying_power": 206_093.88,
            }
        },
        symbol="GOOG",
    )
    draft = build_strategy_order_draft(candidate, position=position)

    impact = calculate_strategy_portfolio_impact(
        candidate,
        order_draft=draft,
        position=position,
        order_index=0,
        strategy_quantity=2,
        order_method=LIMIT_ORDER,
        limit_price=3.25,
        account_label="Schwab",
    )

    assert impact.applicable_funds_label == "Non-Marginable Funds"
    assert impact.applicable_funds == 50_000.0
    assert impact.available_funds == 103_046.94
    assert impact.estimated_funds_required == 61_510.0
    assert impact.funds_after_estimate == -11_510.0
    assert impact.estimated_opening_cash_flow == 650.0
    assert impact.has_funds_shortfall


def test_debit_impact_recalculates_from_ticket_quantity_and_limit() -> None:
    candidate = _candidate(
        strategy_name="long_put",
        strategy_display_name="Long Put",
        capital_required=130.0,
        legs=[
            _option_leg(
                side="LONG",
                option_type="PUT",
                strike=105,
                symbol="GOOG  260918P00105000",
                bid=2.40,
                ask=2.50,
            )
        ],
    )
    position = schwab_position_context(
        {"account_values": {"available_funds": 10_000.0}},
        symbol="GOOG",
    )
    draft = build_strategy_order_draft(candidate, position=position)

    impact = calculate_strategy_portfolio_impact(
        candidate,
        order_draft=draft,
        position=position,
        order_index=0,
        strategy_quantity=3,
        order_method=LIMIT_ORDER,
        limit_price=4.0,
        account_label="Schwab",
    )

    assert impact.estimated_opening_cash_flow == -1_200.0
    assert impact.estimated_capital_at_risk == 390.0
    assert impact.estimated_funds_required == 1_200.0
    assert impact.funds_after_estimate == 8_800.0
    assert impact.requirement_basis == "Configured opening debit"


def test_held_share_impact_does_not_recount_stock_as_new_cash() -> None:
    candidate = _candidate(
        strategy_name="covered_call",
        strategy_display_name="Covered Call",
        stock_requirement="EXISTING_100_SHARES",
        capital_required=10_500.0,
        legs=[
            _stock_leg(bid=104.90, ask=105.10),
            _option_leg(
                side="SHORT",
                option_type="CALL",
                strike=110,
                symbol="GOOG  260918C00110000",
                bid=2.00,
                ask=2.10,
            ),
        ],
    )
    position = schwab_position_context(
        {
            "positions": {
                "items": [
                    {
                        "asset_type": "EQUITY",
                        "symbol": "GOOG",
                        "net_quantity": 100,
                    }
                ]
            },
            "account_values": {"available_funds": 20_000.0},
        },
        symbol="GOOG",
    )
    draft = build_strategy_order_draft(candidate, position=position)

    impact = calculate_strategy_portfolio_impact(
        candidate,
        order_draft=draft,
        position=position,
        order_index=0,
        strategy_quantity=1,
        order_method=LIMIT_ORDER,
        limit_price=2.0,
        account_label="Schwab",
    )

    assert draft.uses_existing_shares
    assert impact.shares_required == 100
    assert impact.shares_after_estimate == 0
    assert impact.estimated_funds_required == 0.0
    assert impact.funds_after_estimate == 20_000.0
    assert impact.estimated_opening_cash_flow == 200.0
    assert not impact.has_share_shortfall


def test_ratio_backspread_risk_combines_ticket_with_current_schwab_shares() -> None:
    candidate = _candidate(
        strategy_name="call_ratio_backspread",
        strategy_display_name="Call Ratio Backspread",
        underlying_price=1_596.08,
        max_loss=6_890.0,
        capital_required=6_890.0,
        legs=[
            _option_leg(
                side="SHORT",
                option_type="CALL",
                strike=1_595.0,
                symbol="GOOG  260918C01595000",
                bid=66.30,
                ask=66.50,
            ),
            _option_leg(
                side="LONG",
                option_type="CALL",
                strike=1_600.0,
                symbol="GOOG  260918C01600000",
                bid=63.40,
                ask=63.60,
                quantity=2,
            ),
        ],
    )
    position = schwab_position_context(
        {
            "positions": {
                "items": [
                    {
                        "asset_type": "EQUITY",
                        "symbol": "GOOG",
                        "net_quantity": 20,
                        "price": 1_596.08,
                        "market_value": 31_921.60,
                    }
                ]
            },
            "account_values": {"available_funds": 102_866.24},
        },
        symbol="GOOG",
    )
    draft = build_strategy_order_draft(candidate, position=position)

    impact = calculate_strategy_portfolio_impact(
        candidate,
        order_draft=draft,
        position=position,
        order_index=0,
        strategy_quantity=1,
        order_method=NET_DEBIT_LIMIT,
        limit_price=63.90,
        account_label="Schwab",
    )

    assert position.underlying_price == 1_596.08
    assert impact.reference_price == 1_596.08
    assert impact.reference_price_basis == "Current Schwab position price"
    assert impact.risk_status == "SINGLE_EXPIRATION_PAYOFF"
    assert impact.ticket_max_loss == 6_890.0
    assert impact.ticket_worst_case_price == 1_600.0
    assert impact.combined_max_loss == 38_311.60
    assert impact.combined_worst_case_price == 0.0
    assert len(impact.price_scenarios) == 5
    unchanged = impact.price_scenarios[2]
    assert unchanged.label == "Unchanged"
    assert unchanged.existing_shares_profit_loss == 0.0
    assert unchanged.ticket_profit_loss == -6_498.0
    assert unchanged.combined_profit_loss == -6_498.0
    up_ten = impact.price_scenarios[3]
    assert up_ten.existing_shares_profit_loss == 3_192.16
    assert up_ten.ticket_profit_loss == 8_678.80
    assert up_ten.combined_profit_loss == 11_870.96
    assert "Existing option positions and working orders are excluded" in (
        impact.risk_basis
    )


def test_short_call_risk_reports_unlimited_ticket_and_combined_loss() -> None:
    candidate = _candidate(
        strategy_name="short_call",
        strategy_display_name="Short Call",
        underlying_price=100.0,
        legs=[
            _option_leg(
                side="SHORT",
                option_type="CALL",
                strike=110.0,
                symbol="GOOG  260918C00110000",
                bid=2.0,
                ask=2.1,
            )
        ],
    )
    position = schwab_position_context(
        {
            "positions": {
                "items": [
                    {
                        "asset_type": "EQUITY",
                        "symbol": "GOOG",
                        "net_quantity": 20,
                        "price": 100.0,
                    }
                ]
            },
            "account_values": {"available_funds": 10_000.0},
        },
        symbol="GOOG",
    )
    draft = build_strategy_order_draft(candidate, position=position)

    impact = calculate_strategy_portfolio_impact(
        candidate,
        order_draft=draft,
        position=position,
        order_index=0,
        strategy_quantity=1,
        order_method=LIMIT_ORDER,
        limit_price=2.0,
        account_label="Schwab",
    )

    assert impact.ticket_max_loss is None
    assert impact.ticket_max_loss_unbounded
    assert impact.combined_max_loss is None
    assert impact.combined_max_loss_unbounded


def test_multi_expiration_ticket_uses_conservative_front_expiration_floor() -> None:
    candidate = _candidate(
        strategy_name="bear_put_diagonal",
        strategy_display_name="Bear Put Diagonal",
        expiration_structure="MULTI",
        underlying_price=342.88,
        entry_cash_flow=-431.30,
        max_loss=34_181.30,
        capital_required=34_181.30,
        risk_calculation_status="PATH_DEPENDENT_CONSERVATIVE_ASSIGNMENT_BOUND",
        legs=[
            _option_leg(
                side="SHORT",
                option_type="PUT",
                strike=337.5,
                symbol="GOOG  260904P00337500",
                bid=2.35,
                ask=2.60,
                expiration="2026-09-04T00:00:00Z",
            ),
            _option_leg(
                side="LONG",
                option_type="PUT",
                strike=342.5,
                symbol="GOOG  260911P00342500",
                bid=5.90,
                ask=6.65,
                expiration="2026-09-11T00:00:00Z",
            ),
        ],
    )
    position = schwab_position_context(
        {
            "positions": {
                "items": [
                    {
                        "asset_type": "EQUITY",
                        "symbol": "GOOG",
                        "net_quantity": 120,
                        "price": 342.88,
                    }
                ]
            },
            "account_values": {"available_funds": 65_702.90},
        },
        symbol="GOOG",
    )
    draft = build_strategy_order_draft(candidate, position=position)

    impact = calculate_strategy_portfolio_impact(
        candidate,
        order_draft=draft,
        position=position,
        order_index=0,
        strategy_quantity=1,
        order_method=NET_DEBIT_LIMIT,
        limit_price=4.30,
        account_label="Schwab",
    )

    assert impact.risk_status == "MULTI_EXPIRATION_INTRINSIC_FLOOR"
    assert impact.risk_expiration == "2026-09-04"
    assert impact.ticket_max_loss == 34_180.0
    assert not impact.ticket_max_loss_unbounded
    assert impact.ticket_worst_case_price is None
    assert impact.combined_max_loss == 75_325.60
    assert impact.combined_worst_case_price is None
    assert impact.published_strategy_max_loss == 34_181.30
    assert len(impact.price_scenarios) == 5
    assert impact.price_scenarios[0].ticket_profit_loss == 70.0
    assert impact.price_scenarios[0].combined_profit_loss == -8_159.12
    assert impact.price_scenarios[2].ticket_profit_loss == -430.0
    assert impact.price_scenarios[2].combined_profit_loss == -430.0
    assert "Later-dated long options are valued at intrinsic only" in impact.risk_basis
    assert "assignment-risk bound of $34,180.00" in impact.risk_basis
    assert "buying-power requirement input" in impact.risk_basis
    assert _max_loss_text(
        impact.ticket_max_loss,
        unbounded=False,
        upper_bound=True,
    ) == "≤ $34,180.00"
    assert _profit_loss_text(-430.0, lower_bound=True) == "≥ -$430.00"
    assert _worst_case_text(
        impact.combined_worst_case_price,
        unbounded=False,
        path_dependent=True,
    ) == "Path-dependent"
    assert _risk_scope_text(impact) == (
        "First-expiry intrinsic floor · Sep 4, 2026 · Reference $342.88"
    )


def test_multi_expiration_ticket_with_later_short_exposure_stays_unavailable() -> None:
    candidate = _candidate(
        strategy_name="unsupported_reverse_calendar",
        strategy_display_name="Unsupported Reverse Calendar",
        expiration_structure="MULTI",
        max_loss=500.0,
        capital_required=500.0,
        risk_calculation_status="PATH_DEPENDENT_CONSERVATIVE_ASSIGNMENT_BOUND",
        legs=[
            _option_leg(
                side="LONG",
                option_type="CALL",
                strike=105.0,
                symbol="GOOG  260918C00105000",
                bid=2.0,
                ask=2.1,
                expiration="2026-09-18T00:00:00Z",
            ),
            _option_leg(
                side="SHORT",
                option_type="CALL",
                strike=105.0,
                symbol="GOOG  261218C00105000",
                bid=4.5,
                ask=4.7,
                expiration="2026-12-18T00:00:00Z",
            ),
        ],
    )
    position = schwab_position_context(
        {"account_values": {"available_funds": 10_000.0}},
        symbol="GOOG",
    )
    draft = build_strategy_order_draft(candidate, position=position)

    impact = calculate_strategy_portfolio_impact(
        candidate,
        order_draft=draft,
        position=position,
        order_index=0,
        strategy_quantity=1,
        order_method=NET_DEBIT_LIMIT,
        limit_price=2.50,
        account_label="Schwab",
    )

    assert impact.risk_status == "MULTI_EXPIRATION_REQUIRES_TIME_MODEL"
    assert impact.ticket_max_loss is None
    assert impact.combined_max_loss is None
    assert impact.price_scenarios == ()
    assert "short option exposure after its first expiration" in impact.risk_basis


def test_decision_evidence_separates_contract_and_quality_notices() -> None:
    legs = (
        SimpleNamespace(bid=4.10, ask=4.30),
        SimpleNamespace(bid=3.65, ask=3.85),
    )
    candidate = SimpleNamespace(
        model_summary="Calibrated profit model active",
        direction_probability_up=53.44,
        predictive_score=37.88,
        scenario_coverage=55.09,
        expected_return=0.0367,
        order_draft=SimpleNamespace(
            legs=legs,
            orders=(SimpleNamespace(legs=legs),),
        ),
        row={
            "decision_timestamp": pd.Timestamp("2026-08-21T20:05:00Z"),
            "all_option_quotes_valid": True,
            "surface_quality_pass": False,
            "liquidity_policy_pass": False,
        },
        pricing_summary=(
            "Unavailable pricing · Unavailable · "
            "AAPL 260828P00310000: Target Event Stale;"
            "AAPL 260828C00310000: Target Event Stale"
        ),
        quality_warning=(
            "Quality warning: surface policy failed, liquidity policy failed"
        ),
    )

    evidence = _decision_evidence(candidate)

    assert evidence.quote_legs_available == 2
    assert evidence.quote_legs_total == 2
    assert evidence.candidate_snapshot != "Timestamp unavailable"
    assert evidence.publication_notices == (
        "AAPL 260828P00310000: Target Event Stale",
        "AAPL 260828C00310000: Target Event Stale",
    )
    assert evidence.publication_checks == (
        ("Contract quote fields", True),
        ("Volatility surface screen", False),
        ("Liquidity screen", False),
    )
    assert evidence.execution_status == "Schwab Review & Confirm"
    assert _publication_notice_display(
        evidence.publication_notices[0]
    ) == "AAPL 260828P00310000 — Model prediction window expired"
    assert _inline_decision_summary(candidate) == (
        "2/2 snapshot quotes populated • 2 publication checks incomplete • "
        "Live Schwab validation"
    )


def test_decision_summary_reports_complete_publication_checks() -> None:
    leg = SimpleNamespace(bid=2.40, ask=2.50)
    candidate = SimpleNamespace(
        model_summary="Calibrated profit model active",
        direction_probability_up=55.0,
        predictive_score=61.0,
        scenario_coverage=60.0,
        expected_return=0.07,
        order_draft=SimpleNamespace(
            legs=(leg,),
            orders=(SimpleNamespace(legs=(leg,)),),
        ),
        row={
            "decision_timestamp": pd.Timestamp("2026-08-24T15:00:00Z"),
            "all_option_quotes_valid": True,
            "surface_quality_pass": True,
            "liquidity_policy_pass": True,
        },
        pricing_summary="Active pricing · BSGP",
    )

    evidence = _decision_evidence(candidate)

    assert evidence.publication_notices == ()
    assert _inline_decision_summary(candidate) == (
        "1/1 snapshot quotes populated • Publication checks passed • "
        "Live Schwab validation"
    )


def test_human_pricing_reason_preserves_each_contract_symbol() -> None:
    assert _human_reason(
        "AAPL 260828P00310000:TARGET_EVENT_STALE;"
        "AAPL 260828C00310000:TARGET_EVENT_STALE"
    ) == (
        "AAPL 260828P00310000: Target Event Stale · "
        "AAPL 260828C00310000: Target Event Stale"
    )


def test_confirmation_copy_is_human_readable() -> None:
    message = order_confirmation_message(
        {
            "orderType": "NET_DEBIT",
            "session": "NORMAL",
            "duration": "GOOD_TILL_CANCEL",
            "price": 1.25,
            "quantity": 1,
            "orderLegCollection": [
                {
                    "instruction": "BUY_TO_OPEN",
                    "quantity": 1,
                    "instrument": {
                        "symbol": "GOOG  260918P00105000",
                        "assetType": "OPTION",
                    },
                }
            ],
        },
        account_label="Schwab ••••5678",
        strategy_label="Long Put",
        acknowledgment_copy="I reviewed the contract, limit, and warnings.",
    )

    assert "Account: Schwab ••••5678" in message
    assert "Strategy: Long Put" in message
    assert "Order type: Net debit" in message
    assert "Duration: Good until canceled" in message
    assert "Buy to open 1 contract(s)" in message
    assert "NET_DEBIT" not in message
    assert "BUY_TO_OPEN" not in message
    assert "By choosing Yes, you confirm:" in message


def test_options_strategy_submit_opens_review_without_direct_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _candidate(
        strategy_name="bear_put_spread",
        strategy_display_name="Bear Put Spread",
        legs=[
            _option_leg(
                side="LONG",
                option_type="PUT",
                strike=105,
                symbol="GOOG  260918P00105000",
                bid=2.40,
                ask=2.50,
            ),
            _option_leg(
                side="SHORT",
                option_type="PUT",
                strike=95,
                symbol="GOOG  260918P00095000",
                bid=1.20,
                ask=1.30,
            ),
        ],
    )
    draft = build_strategy_order_draft(
        candidate,
        position=schwab_position_context({}, symbol="GOOG"),
    )
    submitted: list[dict[str, object]] = []

    class _Session:
        def submit_order(self, payload: dict[str, object]) -> str:
            submitted.append(payload)
            return "/accounts/test/orders/12345"

    tab = OptionsStrategiesTab.__new__(OptionsStrategiesTab)
    tab.root = object()
    tab.selected_candidate = SimpleNamespace(
        candidate_id=str(candidate["id"]),
        row=candidate,
        order_draft=draft,
        manual_order_actionable=True,
        manual_actionability="Manual review eligible.",
        position=SimpleNamespace(
            available_cash=10_000.0,
            working_option_orders=0,
        ),
        portfolio_fit=SimpleNamespace(detail="Fits current account limits."),
        model_summary="Calibrated profit model active",
        pricing_summary="Active pricing",
        quality_warning="Quality policies passed",
    )
    tab.snapshot = PortfolioSnapshot(
        source="schwab",
        account_label="Schwab account 12345678",
        synced_at=datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc),
    )
    tab.selected_order_index = 0
    tab.ticket_quantity = _Value("1")
    tab.ticket_order_method = _Value(
        draft.orders[0].suggested_order_method
    )
    tab.ticket_limit_price = _Value(
        f"{draft.orders[0].suggested_limit_price:.2f}"
    )
    tab.ticket_duration = _Value(DAY_ONLY)
    tab.ticket_order_part = _Value(draft.orders[0].display_name)
    tab.session_factory = _Session
    dialogs: list[object] = []
    monkeypatch.setattr(
        "app.ui.options_strategies.messagebox.showerror",
        lambda _title, message: pytest.fail(message),
    )
    monkeypatch.setattr(
        "app.ui.options_strategies.OptionOrderReviewDialog",
        lambda *, root, controller: dialogs.append((root, controller)),
    )

    tab._submit_order()

    assert len(dialogs) == 1
    root, controller = dialogs[0]
    assert root is tab.root
    assert controller.review.title == "Review Strategy Order"
    assert controller.review.account_display_label.endswith("•••5678")
    assert submitted == []


def test_options_strategy_submit_routes_research_only_candidate_to_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _candidate(
        strategy_name="long_put",
        strategy_display_name="Long Put",
        legs=[
            _option_leg(
                side="LONG",
                option_type="PUT",
                strike=105,
                symbol="GOOG  260918P00105000",
                bid=2.40,
                ask=2.50,
            )
        ],
    )
    draft = build_strategy_order_draft(
        candidate,
        position=schwab_position_context({}, symbol="GOOG"),
    )
    tab = OptionsStrategiesTab.__new__(OptionsStrategiesTab)
    tab.root = object()
    tab.selected_candidate = SimpleNamespace(
        candidate_id=str(candidate["id"]),
        row=candidate,
        order_draft=draft,
        manual_order_actionable=False,
        manual_actionability=(
            "Publication model-pricing checks are incomplete. Strategy Order "
            "Review refreshes exact Schwab quotes before any placement."
        ),
        position=SimpleNamespace(
            available_cash=10_000.0,
            working_option_orders=0,
        ),
        portfolio_fit=SimpleNamespace(detail="Adds downside protection."),
        model_summary="Scenario coverage only",
        pricing_summary="Pricing unavailable",
        quality_warning="Surface policy failed",
    )
    tab.snapshot = PortfolioSnapshot(
        source="schwab",
        account_label="Schwab account 12345678",
        synced_at=datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc),
    )
    tab.selected_order_index = 0
    tab.ticket_quantity = _Value("1")
    tab.ticket_order_method = _Value(draft.orders[0].suggested_order_method)
    tab.ticket_limit_price = _Value(
        f"{draft.orders[0].suggested_limit_price:.2f}"
    )
    tab.ticket_duration = _Value(DAY_ONLY)
    tab.session_factory = lambda: pytest.fail("review must not submit directly")
    dialogs: list[object] = []
    monkeypatch.setattr(
        "app.ui.options_strategies.messagebox.showerror",
        lambda _title, message: pytest.fail(message),
    )
    monkeypatch.setattr(
        "app.ui.options_strategies.OptionOrderReviewDialog",
        lambda *, root, controller: dialogs.append((root, controller)),
    )

    tab._submit_order()

    assert len(dialogs) == 1
    review = dialogs[0][1].review
    assert review.title == "Review Strategy Order"
    assert any(
        notice.title == "Publication Evidence Requires Validation"
        for notice in review.notices
    )


def _candidate(
    *,
    strategy_name: str,
    strategy_display_name: str,
    legs: list[dict[str, object]],
    stock_requirement: str = "NONE",
    **values: object,
) -> dict[str, object]:
    return {
        "id": f"GOOG|1d|2026-08-01T15:00:00Z|{strategy_name}",
        "symbol": "GOOG",
        "horizon": "1d",
        "decision_timestamp": pd.Timestamp("2026-08-01T15:00:00Z"),
        "information_available_at": pd.Timestamp("2026-08-01T15:00:00Z"),
        "target_window_start": pd.Timestamp("2026-08-01T15:30:00Z"),
        "target_window_end": pd.Timestamp("2026-08-01T21:00:00Z"),
        "entry_available_at": pd.Timestamp("2026-08-01T15:05:00Z"),
        "strategy_name": strategy_name,
        "strategy_display_name": strategy_display_name,
        "strategy_family": "TEST",
        "candidate_key": f"{strategy_name}|front=2026-09-18",
        "account_approval": "SPREADS",
        "authorization_status": "AUTHORIZED_SPREADS",
        "construction_status": "CONSTRUCTED",
        "risk_form": "DEFINED_RISK",
        "expiration_structure": "SINGLE",
        "stock_requirement": stock_requirement,
        "cash_requirement": "NORMAL_BUYING_POWER",
        "lifecycle": False,
        "front_expiration": pd.Timestamp("2026-09-18T00:00:00Z"),
        "front_days_to_expiration": 48.0,
        "target_elapsed_hours": 5.5,
        "width_steps": 1,
        "leg_count": len(legs),
        "legs_json": json.dumps(legs),
        "underlying_price": 105.0,
        "entry_cash_flow": -130.0,
        "entry_fees": 1.30,
        "entry_net_credit": 0.0,
        "entry_net_debit": 130.0,
        "max_profit": 870.0,
        "max_loss": 130.0,
        "capital_required": 130.0,
        "risk_calculation_status": "EXPIRATION_PAYOFF_EXACT",
        "net_delta": 0.0,
        "net_gamma": 0.0,
        "net_theta": 0.0,
        "net_vega": 0.0,
        "mean_relative_spread": 0.05,
        "max_relative_spread": 0.05,
        "minimum_open_interest": 100,
        "total_volume": 50,
        "entry_debit_to_underlying": 0.012,
        "max_loss_to_underlying": 0.012,
        "net_delta_per_share": 0.0,
        "surface_quality_pass": True,
        "all_option_quotes_valid": True,
        "liquidity_policy_pass": True,
        "maximum_quote_staleness_seconds": 5.0,
        "scenario_coverage_score": 0.60,
        "raw_profit_probability": 0.60,
        "calibrated_profit_probability": 0.61,
        "direction_probability_up": 0.55,
        "direction_alignment": 0.0,
        "expected_net_profit": 10.0,
        "expected_return_on_risk": 0.07,
        "decision_score": 0.61,
        "score_basis": BSGP_CALIBRATED_MODEL_SCORE_BASIS,
        "candidate_rank": 1,
        "pricing_mode": "ACTIVE",
        "pricing_status": "Active",
        "pricing_leg_coverage": 1.0,
        "pricing_missing_reason": "",
        "pricing_candidate_edge": 10.0,
        "pricing_conservative_edge": 2.0,
        "pricing_edge_to_friction": 0.5,
        "pricing_uncertainty": 12.0,
        "pricing_probability_favorable": 0.70,
        "pricing_relative_edge": 0.001,
        "pricing_model_age_seconds": 30.0,
        "pricing_residual_shrinkage": 0.8,
        "pricing_source": "BSGP",
        "model_version": "test-model",
        "model_status": "MODEL_FIT",
        "registry_version": "test-registry",
        "candidate_policy_version": "test-candidates",
        "model_policy_version": STRATEGY_MODEL_POLICY_VERSION,
        "ranking_policy_version": STRATEGY_RANKING_POLICY_VERSION,
        "schema_version": STRATEGY_CANDIDATE_SCHEMA_VERSION,
        **values,
    }


def _option_leg(
    *,
    side: str,
    option_type: str,
    strike: float,
    symbol: str,
    bid: float,
    ask: float,
    quantity: int = 1,
    expiration: str = "2026-09-18T00:00:00Z",
) -> dict[str, object]:
    return {
        "asset": "OPTION",
        "side": side,
        "quantity": quantity,
        "contract_symbol": symbol,
        "option_type": option_type,
        "expiration_date": expiration,
        "strike": strike,
        "bid": bid,
        "ask": ask,
        "multiplier": 100,
    }


def _stock_leg(*, bid: float, ask: float) -> dict[str, object]:
    return {
        "asset": "STOCK",
        "side": "LONG",
        "quantity": 100,
        "bid": bid,
        "ask": ask,
        "multiplier": 1,
    }


class _Value:
    def __init__(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value
