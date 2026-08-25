from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from app.models.portfolio import CashBalance, Holding, PortfolioSnapshot
from app.services.options_chat import OptionsChatMessage, OptionsChatService
from app.services.schwab_strategy_orders import (
    DAY_ONLY,
    LIMIT_ORDER,
    SchwabPositionContext,
    StrategyOrderComponent,
    StrategyOrderDraft,
    StrategyOrderLeg,
)
from app.services.strategy_portfolio_impact import (
    StrategyPortfolioImpact,
    StrategyPriceScenario,
)
from app.ui.options_chat import (
    MAX_DISCOVER_CANDIDATES_IN_CONTEXT,
    build_options_chat_context,
    options_context_summary,
)


class _FakeResponses:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(output_text="Direct answer from the Options Desk.")


class _FakeOpenAI:
    def __init__(self) -> None:
        self.responses = _FakeResponses()


def test_options_chat_service_sends_fresh_context_and_local_history_statelessly() -> None:
    client = _FakeOpenAI()
    service = OptionsChatService(client=client, model="gpt-test")

    answer = service.reply(
        "Yo, is this risky?",
        history=(
            OptionsChatMessage(role="user", content="What is theta?"),
            OptionsChatMessage(role="assistant", content="Time decay sensitivity."),
        ),
        context={
            "screen": {"symbol": "SNDK"},
            "portfolio": {"total_value": 100_705.68},
        },
    )

    assert answer == "Direct answer from the Options Desk."
    assert len(client.responses.calls) == 1
    request = client.responses.calls[0]
    assert request["model"] == "gpt-test"
    assert request["store"] is False
    assert request["text"] == {"verbosity": "medium"}
    assert request["input"] == [
        {"role": "user", "content": "What is theta?"},
        {"role": "assistant", "content": "Time decay sensitivity."},
        {"role": "user", "content": "Yo, is this risky?"},
    ]
    instructions = str(request["instructions"])
    assert "Do not add generic financial\nadvice disclaimers" in instructions
    prefix = "<CURRENT_DUCKETS_CONTEXT>"
    suffix = "</CURRENT_DUCKETS_CONTEXT>"
    encoded_context = instructions.split(prefix, 1)[1].split(suffix, 1)[0]
    assert json.loads(encoded_context) == {
        "portfolio": {"total_value": 100_705.68},
        "screen": {"symbol": "SNDK"},
    }


def test_options_chat_context_contains_balances_candidate_ticket_and_risk() -> None:
    observed_at = datetime(2026, 8, 25, 9, 15, tzinfo=timezone.utc)
    snapshot = PortfolioSnapshot(
        source="schwab",
        account_label="Schwab",
        cash=[
            CashBalance(
                symbol="USD",
                amount=41_627.24,
                value=41_627.24,
                source="schwab",
                bucket="Cash balance",
            )
        ],
        holdings=[
            Holding(
                symbol="SNDK",
                quantity=20.0,
                price=1_475.0,
                value=29_500.0,
                source="schwab",
                bucket="Equity",
                unrealized_pnl=2_100.0,
                day_pnl=125.0,
            )
        ],
        synced_at=observed_at,
        status="Schwab synced Individual",
        reported_total_value=100_705.68,
        account_facts={
            "account_values": {
                "status": "CURRENT",
                "cash_balance": 41_627.24,
                "available_funds": 100_705.68,
                "buying_power": 214_170.0,
            },
            "positions": {
                "status": "CURRENT",
                "option_row_set_complete": True,
                "items": [
                    {
                        "status": "CURRENT",
                        "symbol": "SNDK",
                        "asset_type": "EQUITY",
                        "net_quantity": 20.0,
                        "price": 1_475.0,
                        "market_value": 29_500.0,
                    }
                ],
            },
            "working_orders": {
                "status": "CURRENT",
                "option_row_set_complete": True,
                "items": [
                    {
                        "order_id": "should-not-leave-the-curated-context",
                        "status": "CURRENT",
                        "order_status": "WORKING",
                        "asset_type": "OPTION",
                        "underlying_symbol": "SNDK",
                        "remaining_quantity": 1.0,
                    }
                ],
            },
        },
    )
    candidate = _candidate(rank=16)
    impact = _portfolio_impact(observed_at)
    visible = tuple(_candidate(rank=rank) for rank in range(1, 36))

    context = build_options_chat_context(
        snapshot=snapshot,
        view=SimpleNamespace(loaded_at=observed_at),
        visible_candidates=visible,
        selected_candidate=candidate,
        selected_order_index=0,
        portfolio_impact=impact,
        screen={
            "chat_entry_point": "Decision Details",
            "active_workspace": "Discover",
            "symbol": "SNDK",
            "horizon": "1 Day",
        },
        ticket={
            "strategy_quantity": "1",
            "order_method": "Limit",
            "limit_price": "56.00",
            "duration": "Day only",
        },
        impact_display={
            "status": "Adds Exposure",
            "ticket_max_loss": "$5,600.00",
            "position_plus_ticket_max_loss": "$36,326.00",
        },
    )

    portfolio = context["portfolio"]
    assert isinstance(portfolio, dict)
    assert portfolio["total_value"] == 100_705.68
    assert portfolio["account_values"] == {
        "status": "CURRENT",
        "cash_balance": 41_627.24,
        "available_funds": 100_705.68,
        "buying_power": 214_170.0,
    }
    assert portfolio["normalized_positions"][0]["net_quantity"] == 20.0
    assert "order_id" not in portfolio["working_orders"][0]

    discover = context["discover"]
    assert discover["visible_candidate_count"] == 35
    assert discover["included_candidate_count"] == MAX_DISCOVER_CANDIDATES_IN_CONTEXT
    assert discover["candidate_list_truncated"] is True

    selected = context["selected_strategy"]
    assert selected["strategy"] == "Long Call"
    assert selected["exact_legs"] == "Buy 1 SNDK 2026-08-28 1495 Call"
    assert selected["position_context"]["shares"] == 20.0
    assert selected["published_snapshot"]["net_delta"] == 0.42
    assert "internal_only" not in selected["published_snapshot"]
    assert selected["order_draft"]["selected_order"]["legs"][0]["strike"] == 1495.0

    calculation = context["portfolio_impact"]["calculation"]
    assert calculation["ticket_max_loss"] == 5_600.0
    assert calculation["combined_max_loss"] == 36_326.0
    assert calculation["price_scenarios"][0]["combined_profit_loss"] == -36_326.0
    assert context["edited_ticket"]["limit_price"] == "56.00"
    assert options_context_summary(context) == (
        "Live context: SNDK • 1 Day • Long Call • Decision Details"
    )


def _candidate(*, rank: int) -> object:
    leg = StrategyOrderLeg(
        asset_type="OPTION",
        symbol="SNDK  260828C01495000",
        instruction="BUY_TO_OPEN",
        quantity=1,
        display_name="SNDK Aug 28, 2026 $1495 Call",
        bid=55.20,
        ask=56.00,
        multiplier=100.0,
        option_type="CALL",
        strike=1495.0,
        expiration="2026-08-28",
    )
    order = StrategyOrderComponent(
        display_name="Complete strategy",
        complex_order_strategy_type="NONE",
        suggested_order_method=LIMIT_ORDER,
        suggested_limit_price=56.0,
        duration=DAY_ONLY,
        legs=(leg,),
    )
    draft = StrategyOrderDraft(
        candidate_id=f"SNDK-long-call-{rank}",
        symbol="SNDK",
        strategy_name="long_call",
        strategy_display_name="Long Call",
        legs=(leg,),
        orders=(order,),
        uses_existing_shares=False,
        shares_required_per_strategy=0.0,
        shares_available=20.0,
    )
    return SimpleNamespace(
        rank=rank,
        candidate_id=f"SNDK-long-call-{rank}",
        symbol="SNDK",
        horizon="1d",
        horizon_label="1 Day",
        strategy_display_name="Long Call",
        exact_legs="Buy 1 SNDK 2026-08-28 1495 Call",
        direction_probability_up=0.5344,
        predictive_score=0.0291,
        scenario_coverage=0.4883,
        expected_net_profit=2_432.0,
        expected_return=0.4345,
        portfolio_fit=SimpleNamespace(
            label="Adds Exposure",
            detail="Long delta increases concentrated SNDK exposure.",
            policy_version="test-fit-v1",
        ),
        score_basis="OPRA Execution + ML",
        pricing_summary="Calibrated profit model active · 1/1 quotes populated",
        quality_warning="Live Schwab validation required",
        manual_order_actionable=False,
        manual_actionability="Review current quotes at Schwab",
        model_summary="Calibrated profit model active",
        position=SchwabPositionContext(
            symbol="SNDK",
            observed_at=datetime(2026, 8, 25, 9, 15, tzinfo=timezone.utc),
            shares=20.0,
            option_contracts=0.0,
            working_option_orders=0,
            available_cash=100_705.68,
            cash_balance=41_627.24,
            buying_power=214_170.0,
            liquidation_value=100_705.68,
            account_values_status="CURRENT",
            underlying_price=1_475.0,
        ),
        order_draft=draft,
        row={
            "decision_timestamp": datetime(
                2026, 8, 25, 9, 10, tzinfo=timezone.utc
            ),
            "underlying_price": 1_475.0,
            "max_profit": None,
            "max_loss": 5_600.0,
            "capital_required": 5_600.0,
            "net_delta": 0.42,
            "net_gamma": 0.01,
            "net_theta": -2.3,
            "net_vega": 1.1,
            "internal_only": "must not be sent",
        },
    )


def _portfolio_impact(observed_at: datetime) -> StrategyPortfolioImpact:
    return StrategyPortfolioImpact(
        account_label="Schwab",
        observed_at=observed_at,
        applicable_funds_label="Available Funds",
        applicable_funds=100_705.68,
        available_funds=100_705.68,
        non_marginable_funds=95_105.03,
        cash_balance=41_627.24,
        buying_power=214_170.0,
        liquidation_value=100_705.68,
        account_values_status="CURRENT",
        estimated_funds_required=5_600.65,
        estimated_capital_at_risk=5_600.0,
        estimated_opening_cash_flow=-5_600.0,
        opening_cash_flow_basis="Configured opening debit",
        funds_after_estimate=95_105.03,
        coverage_ratio=17.98,
        requirement_basis="Published strategy risk-capital estimate",
        shares_held=20.0,
        shares_required=0.0,
        shares_after_estimate=20.0,
        option_contracts=0.0,
        working_option_orders=0,
        reference_price=1_475.0,
        reference_price_basis="Current SNDK position price",
        risk_expiration="2026-08-28",
        risk_status="EXACT",
        published_strategy_max_loss=5_600.0,
        ticket_max_loss=5_600.0,
        ticket_max_loss_unbounded=False,
        ticket_worst_case_price=0.0,
        combined_max_loss=36_326.0,
        combined_max_loss_unbounded=False,
        combined_worst_case_price=0.0,
        risk_basis="Expiration payoff including current shares and selected ticket.",
        price_scenarios=(
            StrategyPriceScenario(
                label="Down 20%",
                underlying_price=1_180.0,
                price_change=-295.0,
                price_change_percent=-0.20,
                existing_shares_profit_loss=-5_900.0,
                ticket_profit_loss=-30_426.0,
                combined_profit_loss=-36_326.0,
            ),
        ),
    )
