from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from app.models.portfolio import PortfolioSnapshot
from app.services.schwab_strategy_orders import (
    DAY_ONLY,
    MARKET_ORDER,
    NET_CREDIT_LIMIT,
    NET_DEBIT_LIMIT,
    build_strategy_order_draft,
    build_strategy_order_payload,
    schwab_position_context,
)
from app.ui.options_strategy_data import load_strategy_candidates, portfolio_fit
from app.ui.options_strategies import OptionsStrategiesTab
from app.ui.schwab_order_messages import order_confirmation_message
from ml.parquet_contracts import (
    STRATEGY_AUDIT_SCHEMA,
    STRATEGY_CANDIDATE_SCHEMA,
    empty_frame,
    write_parquet_with_schema,
)
from ml.strategy_selection.registry import STRATEGY_REGISTRY
from ml.strategy_selection.contracts import (
    CALIBRATED_MODEL_SCORE_BASIS,
    SCENARIO_PRIOR_SCORE_BASIS,
    STRATEGY_CANDIDATE_SCHEMA_VERSION,
    STRATEGY_MODEL_POLICY_VERSION,
    STRATEGY_RANKING_POLICY_VERSION,
)


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
            "account_values": {"available_funds": 15_000},
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
    assert result.predictive_score == pytest.approx(62.0)
    assert 0.0 <= result.predictive_score <= 100.0
    assert result.score_basis == "Calibrated ML"
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
        raw_profit_probability=0.57,
        calibrated_profit_probability=float("nan"),
        expected_net_profit=12.0,
        expected_return_on_risk=0.04,
        decision_score=0.57,
        candidate_rank=1,
        model_status="MARKET_STATE_PRIOR",
        model_version="greek-bbo-scenario-prior-v2",
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

    assert view.candidates[0].predictive_score == pytest.approx(57.0)
    assert 0.0 <= view.candidates[0].predictive_score <= 100.0
    assert view.candidates[0].expected_return == pytest.approx(0.04)
    assert view.candidates[0].score_basis == "Scenario Prior"


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
        {"account_values": {"available_funds": 5_000}},
        symbol="GOOG",
    )

    result = portfolio_fit(candidate, position=position)

    assert result.label == "Funds Below Estimate"
    assert "$5,000.00" in result.detail
    assert "$9,500.00" in result.detail


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
        }
    )

    assert "Order type: Net debit" in message
    assert "Duration: Good until canceled" in message
    assert "Buy to open 1 contract(s)" in message
    assert "NET_DEBIT" not in message
    assert "BUY_TO_OPEN" not in message


def test_options_strategy_submit_uses_the_existing_schwab_session(
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
    tab.selected_candidate = SimpleNamespace(order_draft=draft)
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
    tab._render_order_part = lambda: None
    background_calls: list[object] = []
    confirmations: list[str] = []
    receipts: list[str] = []
    monkeypatch.setattr(
        "app.ui.options_strategies.messagebox.askyesno",
        lambda _title, message: confirmations.append(message) or True,
    )
    monkeypatch.setattr(
        "app.ui.options_strategies.messagebox.showinfo",
        lambda _title, message: receipts.append(message),
    )
    monkeypatch.setattr(
        "app.ui.options_strategies.messagebox.showerror",
        lambda _title, message: pytest.fail(message),
    )
    monkeypatch.setattr(
        "app.ui.options_strategies.run_in_background",
        lambda root, work, on_success, _on_error: (
            background_calls.append(root),
            on_success(work()),
        ),
    )

    tab._submit_order()

    assert len(confirmations) == 1
    assert background_calls == [tab.root]
    assert len(submitted) == 1
    assert submitted[0]["orderType"] == "NET_DEBIT"
    assert submitted[0]["complexOrderStrategyType"] == "VERTICAL"
    assert len(receipts) == 1
    assert "Order ID: 12345" in receipts[0]


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
        "raw_profit_probability": 0.60,
        "calibrated_profit_probability": 0.61,
        "direction_probability_up": 0.55,
        "direction_alignment": 0.0,
        "expected_net_profit": 10.0,
        "expected_return_on_risk": 0.07,
        "decision_score": 0.61,
        "score_basis": CALIBRATED_MODEL_SCORE_BASIS,
        "candidate_rank": 1,
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
