from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.models.option_management import (
    OrderReviewOperation,
    OrderReviewOutcomeStatus,
    OrderReviewPlacementCapability,
    OrderReviewQuoteState,
)
from app.models.portfolio import PortfolioSnapshot
from app.services.schwab_strategy_orders import (
    DAY_ONLY,
    NET_DEBIT_LIMIT,
    build_strategy_order_draft,
    schwab_position_context,
)
from app.services.strategy_order_review import (
    StrategyEntryOrderReviewDraft,
    StrategyOrderReviewController,
    build_strategy_entry_review_draft,
    refresh_strategy_entry_review_draft,
    strategy_entry_order_review,
)


NOW = datetime(2026, 8, 24, 15, 0, tzinfo=timezone.utc)
LONG = "AAPL  260918P00310000"
SHORT = "AAPL  260918P00305000"


def test_research_only_candidate_retains_manual_concept_d_review() -> None:
    draft = _entry_draft(research_only=True)

    review = strategy_entry_order_review(draft, now=NOW)

    assert review.operation == OrderReviewOperation.ENTRY
    assert review.title == "Review Strategy Order"
    assert review.placement_capability == OrderReviewPlacementCapability.SUPPORTED
    assert review.primary_action_label == "Place Strategy Order"
    assert review.quote_state == OrderReviewQuoteState.UNAVAILABLE
    assert review.has_blocking_notice
    assert [(leg.symbol, leg.action) for leg in review.legs] == [
        (LONG, "Buy to Open"),
        (SHORT, "Sell to Open"),
    ]
    assert any(
        notice.title == "Research-Only Candidate"
        for notice in review.notices
    )
    assert "research-only classification" in review.acknowledgment_copy
    assert review.safety_copy.startswith("This order opens new market risk")


def test_exact_schwab_quote_refresh_makes_research_only_review_confirmable() -> None:
    initial = _entry_draft(research_only=True)
    session = _Session()

    refreshed = refresh_strategy_entry_review_draft(
        initial,
        snapshot=_snapshot(),
        session=session,
    )
    review = strategy_entry_order_review(
        refreshed,
        now=refreshed.quote_observed_at,
    )

    assert review.quote_state == OrderReviewQuoteState.LIVE
    assert not review.has_blocking_notice
    assert review.account_display_label.endswith("••••5678")
    assert [(leg.bid, leg.ask) for leg in refreshed.component.legs] == [
        (2.45, 2.55),
        (1.15, 1.25),
    ]
    assert session.quote_requests == [(LONG, SHORT)]


def test_strategy_review_requires_acknowledgment_and_submits_exactly_once() -> None:
    session = _Session()
    controller = _controller(session)

    controller.refresh_review()
    assert not controller.can_place
    controller.acknowledge(True)
    assert controller.can_place

    first = controller.place()
    second = controller.place()

    assert first.status == OrderReviewOutcomeStatus.ACCEPTED
    assert first.submission is not None
    assert first.submission.payload["orderType"] == "NET_DEBIT"
    assert first.submission.payload["complexOrderStrategyType"] == "VERTICAL"
    assert second.status == OrderReviewOutcomeStatus.BLOCKED
    assert len(session.submissions) == 1


def test_changed_exact_leg_quotes_force_review_again_before_submission() -> None:
    session = _Session()
    controller = _controller(session)
    controller.refresh_review()
    controller.acknowledge(True)
    session.quotes[LONG] = {"bidPrice": 2.50, "askPrice": 2.60}

    outcome = controller.place()

    assert outcome.status == OrderReviewOutcomeStatus.INVALIDATED
    assert outcome.retryable
    assert not controller.acknowledged
    assert session.submissions == []
    assert any(
        notice.title == "Order Facts Changed"
        for notice in controller.review.notices
    )


def test_changed_account_facts_force_review_again_before_submission() -> None:
    session = _Session()
    available_cash = [10_000.0]
    controller = StrategyOrderReviewController(
        draft=_entry_draft(research_only=True),
        refresher=lambda draft: refresh_strategy_entry_review_draft(
            draft,
            snapshot=_snapshot(available_cash=available_cash[0]),
            session=session,
        ),
        session_factory=lambda: session,
        now_provider=lambda: datetime.now(timezone.utc),
    )
    controller.refresh_review()
    controller.acknowledge(True)
    available_cash[0] = 9_000.0

    outcome = controller.place()

    assert outcome.status == OrderReviewOutcomeStatus.INVALIDATED
    assert outcome.retryable
    assert not controller.acknowledged
    assert session.submissions == []


def test_obvious_debit_above_available_funds_blocks_placement() -> None:
    refreshed = replace(
        _entry_draft(research_only=False),
        quote_observed_at=NOW,
        available_cash=25.0,
    )

    review = strategy_entry_order_review(refreshed, now=NOW)

    assert review.has_blocking_notice
    assert any(
        notice.title == "Insufficient Available Funds"
        for notice in review.notices
    )


def test_nonintegral_strategy_quantity_is_rejected() -> None:
    base = _strategy_order_draft()

    with pytest.raises(ValueError, match="whole number"):
        build_strategy_entry_review_draft(
            candidate_row=_candidate_row(),
            order_draft=base,
            order_index=0,
            strategy_quantity="1.25",
            order_method=NET_DEBIT_LIMIT,
            limit_price="1.30",
            duration=DAY_ONLY,
            account_label="Schwab account 12345678",
            reviewed_account_at=NOW,
            available_cash=10_000.0,
            working_option_orders=0,
            research_only=False,
            research_reason="",
            portfolio_detail="Fits current account.",
            model_summary="Calibrated profit model active",
            pricing_summary="Active pricing",
            quality_warning="Quality policies passed",
        )


def _controller(session: _Session) -> StrategyOrderReviewController:
    return StrategyOrderReviewController(
        draft=_entry_draft(research_only=True),
        refresher=lambda draft: refresh_strategy_entry_review_draft(
            draft,
            snapshot=_snapshot(),
            session=session,
        ),
        session_factory=lambda: session,
        now_provider=lambda: datetime.now(timezone.utc),
    )


def _entry_draft(*, research_only: bool) -> StrategyEntryOrderReviewDraft:
    base = _strategy_order_draft()
    return build_strategy_entry_review_draft(
        candidate_row=_candidate_row(),
        order_draft=base,
        order_index=0,
        strategy_quantity=1,
        order_method=NET_DEBIT_LIMIT,
        limit_price=1.30,
        duration=DAY_ONLY,
        account_label="Schwab account 12345678",
        reviewed_account_at=NOW,
        available_cash=10_000.0,
        working_option_orders=0,
        research_only=research_only,
        research_reason="Calibrated theoretical pricing is unavailable.",
        portfolio_detail="Defined-risk position fits current account limits.",
        model_summary="Calibrated profit model active",
        pricing_summary="Unavailable research pricing",
        quality_warning="Surface policy failed",
    )


def _strategy_order_draft():
    return build_strategy_order_draft(
        _candidate_row(),
        position=schwab_position_context(
            _snapshot().account_facts,
            symbol="AAPL",
            observed_at=NOW,
        ),
    )


def _candidate_row() -> dict[str, object]:
    return {
        "id": "AAPL|1d|bear_put_spread",
        "symbol": "AAPL",
        "strategy_name": "bear_put_spread",
        "strategy_display_name": "Bear Put Spread",
        "stock_requirement": "NONE",
        "legs_json": json.dumps(
            [
                {
                    "asset": "OPTION",
                    "side": "LONG",
                    "quantity": 1,
                    "contract_symbol": LONG,
                    "multiplier": 100,
                    "expiration_date": "2026-09-18",
                    "strike": 310,
                    "option_type": "PUT",
                    "bid": 2.40,
                    "ask": 2.50,
                },
                {
                    "asset": "OPTION",
                    "side": "SHORT",
                    "quantity": 1,
                    "contract_symbol": SHORT,
                    "multiplier": 100,
                    "expiration_date": "2026-09-18",
                    "strike": 305,
                    "option_type": "PUT",
                    "bid": 1.20,
                    "ask": 1.30,
                },
            ]
        ),
    }


def _snapshot(*, available_cash: float = 10_000.0) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        source="schwab",
        account_label="Schwab account 12345678",
        synced_at=NOW,
        account_facts={
            "positions": {"items": []},
            "working_orders": {"items": []},
            "account_values": {"available_funds": available_cash},
        },
    )


class _Session:
    def __init__(self) -> None:
        self.quotes = {
            LONG: {"bidPrice": 2.45, "askPrice": 2.55},
            SHORT: {"bidPrice": 1.15, "askPrice": 1.25},
        }
        self.quote_requests: list[tuple[str, ...]] = []
        self.submissions: list[dict[str, object]] = []

    def get_equity_quotes(
        self,
        symbols: tuple[str, ...],
    ) -> dict[str, dict[str, float]]:
        requested = tuple(symbols)
        self.quote_requests.append(requested)
        return {symbol: dict(self.quotes[symbol]) for symbol in requested}

    def submit_order(self, payload: dict[str, object]) -> str:
        self.submissions.append(payload)
        return "/accounts/test/orders/entry-123"
