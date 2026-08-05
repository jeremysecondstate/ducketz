from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
import requests

from app.models.option_management import (
    OptionChainContract,
    OrderReviewCashDirection,
    OrderReviewNoticeSeverity,
    OrderReviewOperation,
    OrderReviewOutcomeStatus,
    OrderReviewPlacementCapability,
    OrderReviewPlacementState,
    OrderReviewQuoteState,
)
from app.models.portfolio import PortfolioSnapshot
from app.services.option_exit_plans import SINGLE_TARGET, TARGET_STOP, build_exit_plan_draft
from app.services.option_order_review import (
    BROKER_PREVIEW,
    LOCAL_CALCULATION,
    BrokerAuthenticationFailure,
    BrokerNetworkFailure,
    BrokerOrderPreview,
    BrokerOrderRejected,
    BrokerSubmissionResultUnknown,
    OptionOrderReviewController,
    closing_order_analysis,
    closing_order_review,
    exit_plan_review,
    mask_account_label,
    quote_state,
    roll_order_review,
)
from app.services.option_rolls import ROLL_SCOPE_ENTIRE, build_roll_order_draft
from app.services.schwab_option_management import build_closing_order_draft, option_position_book
from app.ui.option_order_review import OptionOrderReviewDialog


NOW = datetime(2026, 8, 5, 17, 0, tzinfo=timezone.utc)
ACCOUNT = "Schwab account 739204681"
LONG = "ACME  260918P00125000"
SHORT = "ACME  260918P00120000"


def test_close_review_preserves_every_exact_leg_and_masks_account() -> None:
    draft = _credit_close_draft()

    review = closing_order_review(draft, now=NOW)

    assert review.operation == OrderReviewOperation.CLOSE
    assert review.title == "Review closing order"
    assert "2 exact legs" in review.subtitle
    assert "Qty 1" in review.subtitle
    assert review.account_display_label.endswith(f"••••{ACCOUNT[-4:]}")
    assert ACCOUNT not in review.account_display_label
    assert [(leg.symbol, leg.action, leg.quantity) for leg in review.legs] == [
        (LONG, "Sell to close", 1),
        (SHORT, "Buy to close", 1),
    ]
    assert all(leg.contract_label.startswith("ACME 18 SEP 26") for leg in review.legs)
    assert review.safety_copy == "This order closes a position; it does not open a new one."
    assert review.placement_capability == OrderReviewPlacementCapability.SUPPORTED


def test_close_credit_debit_and_bid_mid_ask_rail_are_not_inferred_from_color() -> None:
    credit = closing_order_review(_credit_close_draft(), now=NOW)
    debit = closing_order_review(_debit_close_draft(), now=NOW)

    assert credit.cash_direction == OrderReviewCashDirection.CREDIT
    assert credit.estimated_cash_effect == pytest.approx(100.0)
    assert credit.price_rail is not None
    assert (credit.price_rail.bid, credit.price_rail.midpoint, credit.price_rail.ask) == pytest.approx(
        (0.80, 1.00, 1.20)
    )
    assert debit.cash_direction == OrderReviewCashDirection.DEBIT
    assert debit.estimated_cash_effect == pytest.approx(-100.0)
    assert debit.price_rail is not None
    assert debit.price_rail.bid <= debit.price_rail.midpoint <= debit.price_rail.ask


def test_close_analysis_uses_the_universal_surface_without_placement_capability() -> None:
    draft = _credit_close_draft()

    analysis = closing_order_analysis(draft, now=NOW)
    controller = OptionOrderReviewController(review=analysis, draft=draft)

    assert analysis.title == "Analyze closing order"
    assert analysis.placement_capability == OrderReviewPlacementCapability.REVIEW_ONLY
    assert analysis.primary_action_label == "Finish analysis"
    assert analysis.price_editable is False
    controller.acknowledge(True)
    assert controller.finish_review() is True
    assert controller.place().status == OrderReviewOutcomeStatus.UNSUPPORTED


def test_close_distinguishes_local_estimates_from_unavailable_broker_values() -> None:
    review = closing_order_review(_credit_close_draft(), now=NOW)
    costs = {cost.label: cost for cost in review.costs}
    metrics = {metric.label: metric for metric in review.metrics}

    assert costs["Estimated net proceeds"].provenance == LOCAL_CALCULATION
    assert costs["Estimated fees"].value == "Unavailable"
    assert costs["Settlement"].value == "Unavailable"
    assert metrics["Buying power"].after == "—"
    assert metrics["Delta"].after == "—"
    assert metrics["Theta / day"].after == "—"
    assert {notice.severity for notice in review.notices} >= {
        OrderReviewNoticeSeverity.INFORMATION,
        OrderReviewNoticeSeverity.WARNING,
    }


def test_normal_review_hides_redundant_notice_rails_but_keeps_actionable_checks() -> None:
    review = closing_order_review(_credit_close_draft(), now=NOW)
    dialog = OptionOrderReviewDialog.__new__(OptionOrderReviewDialog)

    routine = dialog._effective_notices(review)
    stale = dialog._effective_notices(replace(review, quote_state=OrderReviewQuoteState.STALE))

    assert routine == (next(notice for notice in review.notices if notice.title == "Atomic net-order structure"),)
    assert any(notice.blocking and notice.title == "Stale quote" for notice in stale)
    assert "may not fill" in review.price_editor_explanation


def test_roll_review_has_close_and_replacement_roles_metrics_and_local_provenance() -> None:
    draft = _roll_draft()

    review = roll_order_review(draft, now=NOW)

    assert review.operation == OrderReviewOperation.ROLL
    assert review.title == "Review roll order"
    assert review.primary_action_label == "Finish roll review"
    assert review.placement_capability == OrderReviewPlacementCapability.REVIEW_ONLY
    assert review.safety_copy.startswith("This roll closes current legs and opens replacement legs")
    assert [(leg.role, leg.action, leg.quantity, leg.symbol) for leg in review.legs] == [
        ("Close", "Sell to close", 1, LONG),
        ("Close", "Buy to close", 1, SHORT),
        ("Open replacement", "Buy to open", 1, "ACME  261016P00125000"),
        ("Open replacement", "Sell to open", 1, "ACME  261016P00120000"),
    ]
    metrics = {metric.label: metric for metric in review.metrics}
    assert metrics["Current → replacement legs"].before == "2"
    assert metrics["Current → replacement legs"].after == "2"
    assert metrics["Days extended"].after == "+28"
    assert metrics["Delta"].provenance == LOCAL_CALCULATION
    costs = {cost.label: cost for cost in review.costs}
    assert costs["Estimated fees"].value == "Unavailable"
    assert costs["Settlement"].value == "Unavailable"
    assert any("not verified" in notice.title.lower() for notice in review.notices)


def test_single_target_is_placeable_while_linked_exit_uses_the_same_review_model() -> None:
    book = option_position_book(_snapshot())
    single = build_exit_plan_draft(book, (LONG, SHORT), template_id=SINGLE_TARGET)
    linked = build_exit_plan_draft(book, (LONG, SHORT), template_id=TARGET_STOP)

    single_review = exit_plan_review(single, now=NOW)
    linked_review = exit_plan_review(linked, now=NOW)

    assert single_review.operation == OrderReviewOperation.EXIT_PLAN
    assert single_review.title == "Review exit plan"
    assert "2 exact review legs" in single_review.subtitle
    assert single_review.placement_capability == OrderReviewPlacementCapability.SUPPORTED
    assert single_review.primary_action_label == "Place exit order"
    assert single_review.price_title == "Exit limit price"
    assert single_review.price_rail is not None
    assert [(leg.role, leg.symbol, leg.action) for leg in single_review.legs] == [
        ("Target", LONG, "Sell to close"),
        ("Target", SHORT, "Buy to close"),
    ]
    assert all(leg.quantity == 1 for leg in single_review.legs)
    assert linked_review.placement_capability == OrderReviewPlacementCapability.UNAVAILABLE
    assert "4 exact review legs" in linked_review.subtitle
    assert linked_review.primary_action_label == "Placement unavailable"
    assert {leg.role for leg in linked_review.legs} == {"Target", "Stop"}
    assert all(leg.quantity == 1 for leg in linked_review.legs)
    assert any(notice.blocking for notice in linked_review.notices)
    metrics = {metric.label: metric for metric in linked_review.metrics}
    assert metrics["Protected quantity"].after == "1"
    assert metrics["Active branches"].after == "2"
    assert metrics["Trigger relationship"].after == "OCO"


def test_single_target_exit_uses_close_revalidation_and_exactly_once_submission() -> None:
    snapshot = _snapshot()
    draft = build_exit_plan_draft(
        option_position_book(snapshot),
        (LONG, SHORT),
        template_id=SINGLE_TARGET,
    )
    submissions: list[dict[str, object]] = []
    controller = OptionOrderReviewController(
        review=exit_plan_review(draft, now=NOW),
        draft=draft,
        snapshot_loader=lambda: snapshot,
        session_factory=lambda: _Session(submissions),
        now_provider=lambda: NOW,
    )
    controller.acknowledge(True)

    first = controller.place()
    second = controller.place()

    assert first.status == OrderReviewOutcomeStatus.ACCEPTED
    assert second.status == OrderReviewOutcomeStatus.BLOCKED
    assert len(submissions) == 1
    assert submissions[0]["duration"] == "GOOD_TILL_CANCEL"
    assert submissions[0]["price"] == pytest.approx(draft.branches[0].limit_price)
    assert controller.state == OrderReviewPlacementState.ACCEPTED


def test_single_target_exit_revalidates_new_working_order_conflicts_before_submission() -> None:
    snapshot = _snapshot()
    draft = build_exit_plan_draft(
        option_position_book(snapshot),
        (LONG, SHORT),
        template_id=SINGLE_TARGET,
    )
    active_order = {
        "order_id": "4412",
        "order_status": "WORKING",
        "entered_time": NOW.isoformat(),
        "order_type": "LIMIT",
        "complex_order_strategy_type": "NONE",
        "duration": "GOOD_TILL_CANCEL",
        "remaining_quantity": 1,
        "limit_price": 1.25,
        "asset_type": "OPTION",
        "legs": [
            {
                "symbol": LONG,
                "instruction": "SELL_TO_CLOSE",
                "remaining_quantity": 1,
            }
        ],
    }
    latest_facts = dict(snapshot.account_facts)
    latest_facts["working_orders"] = {
        "status": "CURRENT",
        "items": [active_order],
        "active_option_orders": [active_order],
    }
    latest = replace(snapshot, account_facts=latest_facts)
    submissions: list[dict[str, object]] = []
    controller = OptionOrderReviewController(
        review=exit_plan_review(draft, now=NOW),
        draft=draft,
        snapshot_loader=lambda: latest,
        session_factory=lambda: _Session(submissions),
        now_provider=lambda: NOW,
    )
    controller.acknowledge(True)

    outcome = controller.place()

    assert outcome.status == OrderReviewOutcomeStatus.INVALIDATED
    assert controller.review.placement_capability == OrderReviewPlacementCapability.UNAVAILABLE
    assert any("conflicting" in notice.title.casefold() for notice in controller.review.notices)
    assert submissions == []


@pytest.mark.parametrize(
    ("age_seconds", "expected"),
    [
        (2, OrderReviewQuoteState.LIVE),
        (80, OrderReviewQuoteState.AGING),
        (121, OrderReviewQuoteState.STALE),
    ],
)
def test_quote_freshness_transitions_are_deterministic(
    age_seconds: int,
    expected: OrderReviewQuoteState,
) -> None:
    assert quote_state(NOW - timedelta(seconds=age_seconds), now=NOW) == expected


def test_missing_quote_timestamp_is_unavailable_and_blocks_close() -> None:
    assert quote_state(None, now=NOW) == OrderReviewQuoteState.UNAVAILABLE
    draft = _credit_close_draft()
    missing = replace(
        draft,
        oldest_quote_at=None,
        legs=tuple(replace(leg, quote_observed_at=None) for leg in draft.legs),
    )

    review = closing_order_review(missing, now=NOW)

    assert review.quote_state == OrderReviewQuoteState.UNAVAILABLE
    assert review.has_blocking_notice
    assert any(notice.severity == OrderReviewNoticeSeverity.BLOCKING for notice in review.notices)


def test_limit_price_change_rebuilds_draft_and_resets_acknowledgment_and_preview() -> None:
    draft = _credit_close_draft()
    controller = _controller(draft)
    controller.acknowledge(True)
    controller.preview_result = BrokerOrderPreview(accepted=True)

    controller.set_limit_price("1.07")

    assert controller.acknowledged is False
    assert controller.preview_result is None
    assert controller.draft is not draft
    assert controller.review.net_price == pytest.approx(1.07)
    assert controller.review.estimated_cash_effect == pytest.approx(107.0)
    assert draft.limit_price == pytest.approx(1.0)


def test_refresh_resets_acknowledgment_and_keeps_exact_semantics() -> None:
    snapshot = _snapshot()
    draft = build_closing_order_draft(option_position_book(snapshot), (LONG, SHORT))
    controller = _controller(draft, snapshot_loader=lambda: snapshot)
    controller.acknowledge(True)

    controller.refresh_review()

    assert controller.acknowledged is False
    assert [(leg.symbol, leg.action, leg.quantity) for leg in controller.review.legs] == [
        (LONG, "Sell to close", 1),
        (SHORT, "Buy to close", 1),
    ]
    assert controller.review.quote_state == OrderReviewQuoteState.LIVE


def test_complete_option_row_set_is_not_blocked_by_unrelated_incomplete_positions() -> None:
    snapshot = _snapshot()
    positions = snapshot.account_facts["positions"]
    assert isinstance(positions, dict)
    positions.update(
        {
            "status": "INCOMPLETE",
            "option_row_set_complete": True,
            "option_unavailable_reasons": [],
            "unavailable_reasons": ["An unrelated equity row omitted a policy-only field."],
        }
    )
    book = option_position_book(snapshot)
    draft = build_closing_order_draft(book, (LONG, SHORT))
    controller = _controller(draft, snapshot_loader=lambda: snapshot)

    refreshed = controller.refresh_review()

    assert book.status == "CURRENT"
    assert book.unavailable_reasons == ()
    assert refreshed.quote_state == OrderReviewQuoteState.LIVE
    controller.acknowledge(True)
    assert controller.can_place is True


def test_failed_convenience_refresh_retains_fresh_review_for_final_retry() -> None:
    snapshot = _snapshot()
    draft = build_closing_order_draft(option_position_book(snapshot), (LONG, SHORT))
    calls = 0

    def flaky_snapshot_loader() -> PortfolioSnapshot:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise BrokerNetworkFailure("temporary quote refresh outage")
        return snapshot

    controller = _controller(draft, snapshot_loader=flaky_snapshot_loader)

    with pytest.raises(BrokerNetworkFailure, match="temporary quote refresh outage"):
        controller.refresh_review()

    assert controller.review.quote_state == OrderReviewQuoteState.LIVE
    assert controller.review.has_blocking_notice is False
    controller.acknowledge(True)
    assert controller.can_place is True
    assert controller.place().status == OrderReviewOutcomeStatus.ACCEPTED
    assert calls == 2


def test_acknowledgment_and_all_safety_gates_control_placement() -> None:
    draft = _credit_close_draft()
    controller = _controller(draft)

    assert controller.can_place is False
    assert controller.state_text == "Confirmation required"
    controller.acknowledge(True)
    assert controller.can_place is True
    assert controller.state_text == "Ready for final revalidation"

    stale = _controller(draft, now=NOW + timedelta(minutes=3))
    stale.acknowledge(True)
    assert stale.can_place is False
    assert stale.state_text == "Stale quote — refresh required"


def test_close_back_and_save_are_non_submitting_actions() -> None:
    submissions: list[dict[str, object]] = []
    controller = _controller(_credit_close_draft(), submissions=submissions)
    controller.acknowledge(True)

    controller.abandon_review()
    controller.abandon_review()
    assert controller.save_order() is False

    assert submissions == []
    assert controller.state == OrderReviewPlacementState.READY


def test_unchecked_stale_unsupported_and_review_only_paths_never_submit() -> None:
    submissions: list[dict[str, object]] = []
    draft = _credit_close_draft()
    unchecked = _controller(draft, submissions=submissions)
    assert unchecked.place().status == OrderReviewOutcomeStatus.BLOCKED

    stale = _controller(draft, submissions=submissions, now=NOW + timedelta(minutes=3))
    stale.acknowledge(True)
    assert stale.place().status == OrderReviewOutcomeStatus.BLOCKED

    roll = _roll_draft()
    roll_controller = OptionOrderReviewController(
        review=roll_order_review(roll, now=NOW),
        draft=roll,
        session_factory=lambda: _Session(submissions),
    )
    roll_controller.acknowledge(True)
    assert roll_controller.place().status == OrderReviewOutcomeStatus.UNSUPPORTED

    linked = build_exit_plan_draft(option_position_book(_snapshot()), (LONG, SHORT), template_id=TARGET_STOP)
    exit_controller = OptionOrderReviewController(
        review=exit_plan_review(linked, now=NOW),
        draft=linked,
        session_factory=lambda: _Session(submissions),
    )
    exit_controller.acknowledge(True)
    assert exit_controller.place().status == OrderReviewOutcomeStatus.UNSUPPORTED
    assert submissions == []


def test_position_drift_invalidates_review_before_any_submission() -> None:
    submissions: list[dict[str, object]] = []
    draft = _credit_close_draft()
    drifted = _snapshot(long_quantity=2)
    controller = _controller(
        draft,
        submissions=submissions,
        snapshot_loader=lambda: drifted,
    )
    controller.acknowledge(True)

    outcome = controller.place()

    assert outcome.status == OrderReviewOutcomeStatus.INVALIDATED
    assert controller.state == OrderReviewPlacementState.REJECTED
    assert controller.acknowledged is False
    assert submissions == []


def test_account_drift_blocks_without_exposing_either_complete_account_label() -> None:
    submissions: list[dict[str, object]] = []
    other_account = "Schwab account 135790246"
    controller = _controller(
        _credit_close_draft(),
        submissions=submissions,
        snapshot_loader=lambda: replace(_snapshot(), account_label=other_account),
    )
    controller.acknowledge(True)

    outcome = controller.place()

    assert outcome.status == OrderReviewOutcomeStatus.INVALIDATED
    assert ACCOUNT not in outcome.message
    assert other_account not in outcome.message
    assert submissions == []


def test_quote_change_during_revalidation_requires_another_acknowledgment() -> None:
    submissions: list[dict[str, object]] = []
    draft = _credit_close_draft()
    moved = _snapshot(long_mark=1.55, long_bid=1.45, long_ask=1.65)
    controller = _controller(draft, submissions=submissions, snapshot_loader=lambda: moved)
    controller.acknowledge(True)

    outcome = controller.place()

    assert outcome.status == OrderReviewOutcomeStatus.INVALIDATED
    assert "Quote values changed" in outcome.message
    assert controller.acknowledged is False
    assert controller.state == OrderReviewPlacementState.READY
    assert submissions == []


def test_preview_rejection_prevents_submission_and_records_blocker() -> None:
    submissions: list[dict[str, object]] = []
    controller = _controller(
        _credit_close_draft(),
        submissions=submissions,
        previewer=lambda _payload: BrokerOrderPreview(False, "Order would exceed a broker limit."),
    )
    controller.acknowledge(True)

    outcome = controller.place()

    assert outcome.status == OrderReviewOutcomeStatus.PREVIEW_REJECTED
    assert controller.state == OrderReviewPlacementState.REJECTED
    assert controller.review.broker_preview_status.startswith("Rejected")
    assert submissions == []


def test_valid_confirmed_close_submits_exactly_once_and_refreshes_only_after_acceptance() -> None:
    submissions: list[dict[str, object]] = []
    refresh_states: list[OrderReviewPlacementState] = []

    def record_refresh() -> None:
        refresh_states.append(controller.state)

    controller = _controller(
        _credit_close_draft(),
        submissions=submissions,
        on_accepted=record_refresh,
    )
    controller.acknowledge(True)

    first = controller.place()
    second = controller.place()

    assert first.status == OrderReviewOutcomeStatus.ACCEPTED
    assert first.submission is not None
    assert second.status == OrderReviewOutcomeStatus.BLOCKED
    assert len(submissions) == 1
    assert refresh_states == [OrderReviewPlacementState.ACCEPTED]
    assert controller.state == OrderReviewPlacementState.ACCEPTED


def test_verified_preview_values_are_labeled_as_broker_values() -> None:
    submissions: list[dict[str, object]] = []
    controller = _controller(
        _credit_close_draft(),
        submissions=submissions,
        previewer=lambda _payload: BrokerOrderPreview(
            True,
            estimated_fees=1.23,
            buying_power_after=45_000.0,
            settlement="T+1",
        ),
    )
    controller.acknowledge(True)

    assert controller.place().status == OrderReviewOutcomeStatus.ACCEPTED

    costs = {cost.label: cost for cost in controller.review.costs}
    assert costs["Estimated fees"].value == "$1.23"
    assert costs["Estimated fees"].provenance == BROKER_PREVIEW
    assert costs["Settlement"].value == "T+1"
    assert controller.review.broker_preview_status == "Broker preview accepted"


@pytest.mark.parametrize(
    ("previewer", "middle_state"),
    [
        (lambda _payload: BrokerOrderPreview(True), OrderReviewPlacementState.PREVIEWING),
        (None, OrderReviewPlacementState.FALLBACK),
    ],
)
def test_valid_close_follows_the_explicit_placement_state_machine(
    previewer: object | None,
    middle_state: OrderReviewPlacementState,
) -> None:
    controller = _controller(_credit_close_draft(), previewer=previewer)
    observed: list[OrderReviewPlacementState] = []
    controller.state_listener = lambda current: observed.append(current.state)
    controller.acknowledge(True)
    observed.clear()

    assert controller.place().status == OrderReviewOutcomeStatus.ACCEPTED

    assert observed == [
        OrderReviewPlacementState.REVALIDATING,
        middle_state,
        OrderReviewPlacementState.SUBMITTING,
        OrderReviewPlacementState.ACCEPTED,
    ]


@pytest.mark.parametrize(
    ("failure", "expected", "retryable"),
    [
        (BrokerAuthenticationFailure("token expired"), OrderReviewOutcomeStatus.AUTHENTICATION_FAILED, True),
        (BrokerNetworkFailure("offline"), OrderReviewOutcomeStatus.NETWORK_FAILED, True),
    ],
)
def test_safe_pre_submission_failures_allow_retry_without_refresh(
    failure: Exception,
    expected: OrderReviewOutcomeStatus,
    retryable: bool,
) -> None:
    submissions: list[dict[str, object]] = []

    def fail_read() -> PortfolioSnapshot:
        raise failure

    controller = _controller(
        _credit_close_draft(),
        submissions=submissions,
        snapshot_loader=fail_read,
    )
    controller.acknowledge(True)

    outcome = controller.place()

    assert outcome.status == expected
    assert outcome.retryable is retryable
    assert controller.state == OrderReviewPlacementState.READY
    assert controller.acknowledged is False
    assert submissions == []


def test_explicit_broker_rejection_does_not_claim_acceptance_or_refresh() -> None:
    refreshes: list[str] = []

    class RejectingSession:
        def submit_order(self, _payload: dict[str, object]) -> None:
            raise BrokerOrderRejected("Broker rejected price increment.")

    controller = _controller(
        _credit_close_draft(),
        session_factory=RejectingSession,
        on_accepted=lambda: refreshes.append("accepted"),
    )
    controller.acknowledge(True)

    outcome = controller.place()

    assert outcome.status == OrderReviewOutcomeStatus.REJECTED
    assert controller.state == OrderReviewPlacementState.REJECTED
    assert outcome.submission is None
    assert refreshes == []


@pytest.mark.parametrize(
    "failure",
    [
        requests.Timeout("timed out"),
        requests.ConnectionError("connection dropped"),
        BrokerSubmissionResultUnknown("ambiguous response"),
    ],
)
def test_timeout_network_or_unknown_after_transmission_blocks_blind_retry(
    failure: Exception,
) -> None:
    calls = 0
    refreshes: list[str] = []
    order_refreshes: list[str] = []

    class UnknownSession:
        def submit_order(self, _payload: dict[str, object]) -> None:
            nonlocal calls
            calls += 1
            raise failure

    controller = _controller(
        _credit_close_draft(),
        session_factory=UnknownSession,
        on_accepted=lambda: refreshes.append("accepted"),
        on_unknown=lambda: order_refreshes.append("orders"),
    )
    controller.acknowledge(True)

    first = controller.place()
    second = controller.place()

    assert first.status == OrderReviewOutcomeStatus.UNKNOWN
    assert second.status == OrderReviewOutcomeStatus.BLOCKED
    assert controller.state == OrderReviewPlacementState.UNKNOWN
    assert calls == 1
    assert refreshes == []
    assert order_refreshes == ["orders"]


def test_account_masking_preserves_safe_labels_and_masks_long_tokens() -> None:
    assert mask_account_label("Schwab ••••2048") == "Schwab ••••2048"
    assert mask_account_label("Broker 9988776655") == "Broker ••••6655"
    assert mask_account_label("Schwab test") == "Schwab test"
    assert mask_account_label("") == "Account unavailable"


def _controller(
    draft: object,
    *,
    submissions: list[dict[str, object]] | None = None,
    snapshot_loader: object | None = None,
    session_factory: object | None = None,
    previewer: object | None = None,
    on_accepted: object | None = None,
    on_unknown: object | None = None,
    now: datetime = NOW,
) -> OptionOrderReviewController:
    assert hasattr(draft, "legs")
    captured = submissions if submissions is not None else []
    return OptionOrderReviewController(
        review=closing_order_review(draft, now=now),  # type: ignore[arg-type]
        draft=draft,  # type: ignore[arg-type]
        snapshot_loader=(snapshot_loader if snapshot_loader is not None else lambda: _snapshot()),  # type: ignore[arg-type]
        session_factory=(session_factory if session_factory is not None else lambda: _Session(captured)),  # type: ignore[arg-type]
        previewer=previewer,  # type: ignore[arg-type]
        on_accepted=on_accepted,  # type: ignore[arg-type]
        on_unknown=on_unknown,  # type: ignore[arg-type]
        now_provider=lambda: now,
    )


class _Session:
    def __init__(self, submissions: list[dict[str, object]]) -> None:
        self.submissions = submissions

    def submit_order(self, payload: dict[str, object]) -> str:
        self.submissions.append(payload)
        return "/accounts/masked/orders/9001"


def _credit_close_draft() -> object:
    return build_closing_order_draft(option_position_book(_snapshot()), (LONG, SHORT))


def _debit_close_draft() -> object:
    snapshot = _snapshot(
        long_mark=0.50,
        long_bid=0.40,
        long_ask=0.60,
        short_mark=1.50,
        short_bid=1.40,
        short_ask=1.60,
    )
    return build_closing_order_draft(option_position_book(snapshot), (LONG, SHORT))


def _roll_draft() -> object:
    book = option_position_book(_snapshot())
    replacements = (
        OptionChainContract(
            symbol="ACME  261016P00125000",
            underlying_symbol="ACME",
            option_type="PUT",
            expiration="2026-10-16",
            strike=125.0,
            bid=2.80,
            ask=3.00,
            mark=2.90,
            delta=-0.32,
            theta=-0.05,
            contract_multiplier=100.0,
            quote_observed_at=NOW,
        ),
        OptionChainContract(
            symbol="ACME  261016P00120000",
            underlying_symbol="ACME",
            option_type="PUT",
            expiration="2026-10-16",
            strike=120.0,
            bid=0.40,
            ask=0.60,
            mark=0.50,
            delta=-0.18,
            theta=-0.03,
            contract_multiplier=100.0,
            quote_observed_at=NOW,
        ),
    )
    return build_roll_order_draft(
        book,
        (LONG, SHORT),
        (LONG, SHORT),
        replacements,
        scope_mode=ROLL_SCOPE_ENTIRE,
        keep_strike_widths=True,
        underlying_price=123.0,
        now=NOW,
    )


def _snapshot(
    *,
    long_quantity: float = 1,
    long_mark: float = 1.50,
    long_bid: float = 1.40,
    long_ask: float = 1.60,
    short_mark: float = 0.50,
    short_bid: float = 0.40,
    short_ask: float = 0.60,
    quote_at: datetime = NOW,
) -> PortfolioSnapshot:
    rows = [
        _position(
            LONG,
            quantity=long_quantity,
            strike=125.0,
            mark=long_mark,
            bid=long_bid,
            ask=long_ask,
            delta=-0.42,
            theta=-0.07,
            quote_at=quote_at,
        ),
        _position(
            SHORT,
            quantity=-1,
            strike=120.0,
            mark=short_mark,
            bid=short_bid,
            ask=short_ask,
            delta=-0.24,
            theta=-0.04,
            quote_at=quote_at,
        ),
    ]
    return PortfolioSnapshot(
        source="schwab",
        account_label=ACCOUNT,
        synced_at=quote_at,
        account_facts={
            "observed_at": quote_at.isoformat(),
            "account_values": {
                "status": "CURRENT",
                "available_funds": 40_000.0,
                "buying_power": 50_000.0,
            },
            "positions": {
                "status": "CURRENT",
                "items": rows,
                "option_unavailable_reasons": [],
            },
            "working_orders": {
                "status": "CURRENT",
                "items": [],
                "active_option_orders": [],
            },
        },
    )


def _position(
    symbol: str,
    *,
    quantity: float,
    strike: float,
    mark: float,
    bid: float,
    ask: float,
    delta: float,
    theta: float,
    quote_at: datetime,
) -> dict[str, object]:
    return {
        "status": "CURRENT",
        "symbol": symbol,
        "asset_type": "OPTION",
        "contract_multiplier": 100.0,
        "underlying_symbol": "ACME",
        "option_type": "PUT",
        "strike": strike,
        "expiration": "2026-09-18",
        "delta": delta,
        "theta": theta,
        "net_quantity": quantity,
        "settled_quantity": quantity,
        "price": mark,
        "bid": bid,
        "ask": ask,
        "market_value": mark * quantity * 100,
        "unrealized_pnl": -25.0 if quantity > 0 else 10.0,
        "day_pnl": 0.0,
        "quote_observed_at": quote_at.isoformat(),
        "source_ref": f"fixture:{symbol}",
        "option_fields_complete": True,
        "option_unavailable_reasons": [],
        "unavailable_reasons": [],
    }
