from __future__ import annotations

import math
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone

import requests

from app.models.option_management import (
    ClosingOrderSubmission,
    OptionOrderReview,
    OptionOrderReviewCost,
    OptionOrderReviewLeg,
    OptionOrderReviewMetric,
    OptionOrderReviewNotice,
    OptionOrderReviewPriceRail,
    OrderReviewCashDirection,
    OrderReviewNoticeSeverity,
    OrderReviewOperation,
    OrderReviewOutcomeStatus,
    OrderReviewPlacementCapability,
    OrderReviewPlacementOutcome,
    OrderReviewPlacementState,
    OrderReviewQuoteState,
)
from app.models.portfolio import PortfolioSnapshot
from app.services.option_order_review import (
    BrokerAuthenticationFailure,
    BrokerOrderRejected,
    BrokerSubmissionResultUnknown,
    DEFAULT_REVIEW_MAX_QUOTE_AGE_SECONDS,
    LOCAL_CALCULATION,
    PREVIEW_FALLBACK_STATUS,
    mask_account_label,
    quote_state,
)
from app.services.schwab_market_data import SchwabMarketDataProvider
from app.services.schwab_strategy_orders import (
    MARKET_ORDER,
    NET_CREDIT_LIMIT,
    NET_DEBIT_LIMIT,
    StrategyOrderComponent,
    StrategyOrderDraft,
    build_strategy_order_draft,
    build_strategy_order_payload,
    refresh_strategy_order_quotes,
    schwab_position_context,
)
from app.services.strategy_portfolio_impact import (
    StrategyPortfolioImpact,
    calculate_strategy_portfolio_impact,
)


@dataclass(frozen=True)
class StrategyEntryOrderReviewDraft:
    """One configured Discover order component awaiting broker review."""

    candidate_row: Mapping[str, object]
    order_draft: StrategyOrderDraft
    order_index: int
    strategy_quantity: int
    order_method: str
    limit_price: float | None
    duration: str
    account_label: str
    reviewed_account_at: datetime | None
    quote_observed_at: datetime | None
    available_cash: float | None
    working_option_orders: int
    research_only: bool
    research_reason: str
    portfolio_detail: str
    model_summary: str
    pricing_summary: str
    quality_warning: str
    applicable_funds: float | None = None
    applicable_funds_label: str = "Available Funds"
    estimated_funds_required: float | None = None
    funds_after_estimate: float | None = None
    requirement_basis: str = ""

    @property
    def component(self) -> StrategyOrderComponent:
        if not 0 <= self.order_index < self.order_draft.order_count:
            raise ValueError("The selected Schwab order component is unavailable.")
        return self.order_draft.orders[self.order_index]

    def payload(self) -> dict[str, object]:
        return build_strategy_order_payload(
            self.order_draft,
            order_index=self.order_index,
            strategy_quantity=self.strategy_quantity,
            order_method=self.order_method,
            limit_price=self.limit_price,
            duration=self.duration,
        )


def build_strategy_entry_review_draft(
    *,
    candidate_row: Mapping[str, object],
    order_draft: StrategyOrderDraft,
    order_index: int,
    strategy_quantity: object,
    order_method: str,
    limit_price: object | None,
    duration: str,
    account_label: str,
    reviewed_account_at: datetime | None,
    available_cash: float | None,
    working_option_orders: int,
    research_only: bool,
    research_reason: str,
    portfolio_detail: str,
    model_summary: str,
    pricing_summary: str,
    quality_warning: str,
    portfolio_impact: StrategyPortfolioImpact | None = None,
) -> StrategyEntryOrderReviewDraft:
    quantity = _positive_int(strategy_quantity, "Strategy quantity")
    clean_limit = (
        None
        if order_method == MARKET_ORDER
        else _positive_number(limit_price, "Limit price")
    )
    draft = StrategyEntryOrderReviewDraft(
        candidate_row=dict(candidate_row),
        order_draft=order_draft,
        order_index=int(order_index),
        strategy_quantity=quantity,
        order_method=str(order_method),
        limit_price=clean_limit,
        duration=str(duration),
        account_label=str(account_label or "Schwab"),
        reviewed_account_at=reviewed_account_at,
        quote_observed_at=None,
        available_cash=available_cash,
        working_option_orders=max(0, int(working_option_orders)),
        research_only=bool(research_only),
        research_reason=str(research_reason).strip(),
        portfolio_detail=str(portfolio_detail).strip(),
        model_summary=str(model_summary).strip(),
        pricing_summary=str(pricing_summary).strip(),
        quality_warning=str(quality_warning).strip(),
        applicable_funds=(
            None if portfolio_impact is None else portfolio_impact.applicable_funds
        ),
        applicable_funds_label=(
            "Available Funds"
            if portfolio_impact is None
            else portfolio_impact.applicable_funds_label
        ),
        estimated_funds_required=(
            None
            if portfolio_impact is None
            else portfolio_impact.estimated_funds_required
        ),
        funds_after_estimate=(
            None
            if portfolio_impact is None
            else portfolio_impact.funds_after_estimate
        ),
        requirement_basis=(
            ""
            if portfolio_impact is None
            else portfolio_impact.requirement_basis
        ),
    )
    draft.payload()
    return draft


def refresh_strategy_entry_review_draft(
    draft: StrategyEntryOrderReviewDraft,
    *,
    snapshot: PortfolioSnapshot,
    session: object,
) -> StrategyEntryOrderReviewDraft:
    """Re-read account state and every exact-leg quote before entry review."""
    position = schwab_position_context(
        snapshot.account_facts,
        symbol=draft.order_draft.symbol,
        observed_at=snapshot.synced_at,
    )
    rebuilt = build_strategy_order_draft(
        draft.candidate_row,
        position=position,
    )
    provider = SchwabMarketDataProvider(session=session)  # type: ignore[arg-type]
    fetched = provider.fetch_quotes(leg.symbol for leg in rebuilt.legs)
    quotes = {symbol: result[0] for symbol, result in fetched.items()}
    if len(quotes) != len({leg.symbol for leg in rebuilt.legs}):
        missing = sorted({leg.symbol for leg in rebuilt.legs} - set(quotes))
        raise ValueError(
            "Schwab did not return every exact-leg quote: " + ", ".join(missing)
        )
    refreshed = refresh_strategy_order_quotes(rebuilt, quotes)
    impact = calculate_strategy_portfolio_impact(
        draft.candidate_row,
        order_draft=refreshed,
        position=position,
        order_index=draft.order_index,
        strategy_quantity=draft.strategy_quantity,
        order_method=draft.order_method,
        limit_price=draft.limit_price,
        account_label=snapshot.account_label,
    )
    quote_observed_at = min(quote.fetched_at for quote in quotes.values())
    updated = replace(
        draft,
        order_draft=refreshed,
        account_label=str(snapshot.account_label or draft.account_label),
        reviewed_account_at=snapshot.synced_at,
        quote_observed_at=quote_observed_at,
        available_cash=position.available_cash,
        working_option_orders=position.working_option_orders,
        applicable_funds=impact.applicable_funds,
        applicable_funds_label=impact.applicable_funds_label,
        estimated_funds_required=impact.estimated_funds_required,
        funds_after_estimate=impact.funds_after_estimate,
        requirement_basis=impact.requirement_basis,
    )
    updated.payload()
    return updated


def strategy_entry_order_review(
    draft: StrategyEntryOrderReviewDraft,
    *,
    now: datetime | None = None,
    max_quote_age_seconds: float = DEFAULT_REVIEW_MAX_QUOTE_AGE_SECONDS,
) -> OptionOrderReview:
    current = _aware(now or datetime.now(timezone.utc))
    component = draft.component
    freshness = quote_state(
        draft.quote_observed_at,
        now=current,
        max_age_seconds=max_quote_age_seconds,
    )
    direction = _cash_direction(draft)
    estimated_cash_effect = _estimated_cash_effect(draft, direction)
    legs = tuple(
        OptionOrderReviewLeg(
            role="Open",
            action=_human_instruction(leg.instruction),
            quantity=leg.quantity * draft.strategy_quantity,
            contract_label=leg.display_name,
            symbol=leg.symbol,
            bid=leg.bid,
            ask=leg.ask,
            mark=(
                None
                if leg.bid is None or leg.ask is None
                else round((leg.bid + leg.ask) / 2.0, 4)
            ),
            before_quantity=0.0,
            after_quantity=(
                -leg.quantity * draft.strategy_quantity
                if leg.instruction.startswith("SELL")
                else leg.quantity * draft.strategy_quantity
            ),
            quote_observed_at=draft.quote_observed_at,
        )
        for leg in component.legs
    )
    notices: list[OptionOrderReviewNotice] = []
    if draft.research_only:
        notices.append(
            _warning(
                "Publication Evidence Requires Validation",
                (
                    f"{draft.research_reason} This describes the candidate "
                    "publication; current Schwab quotes and account facts remain "
                    "the execution authority."
                ).strip(),
            )
        )
    if draft.research_only and draft.quality_warning:
        notices.append(
            _warning("Candidate Publication Checks", draft.quality_warning)
        )
    if draft.pricing_summary:
        notices.append(
            _information(
                "Published Model Context",
                (
                    f"{draft.pricing_summary}. The final order review uses a fresh "
                    "Schwab quote read instead of treating the candidate snapshot "
                    "as execution authority."
                ),
            )
        )
    context_details = " ".join(
        detail
        for detail in (draft.model_summary, draft.portfolio_detail)
        if detail
    )
    if context_details:
        notices.append(_information("Candidate and Portfolio Context", context_details))
    if len(component.legs) > 1:
        notices.append(
            _information(
                "Atomic Net-Order Structure",
                f"All {len(component.legs)} exact legs are transmitted as one net order; partial fills may still be possible.",
            )
        )
    if draft.order_draft.order_count > 1:
        notices.append(
            _warning(
                "Multi-Order Strategy",
                (
                    f"This is Schwab order {draft.order_index + 1} of "
                    f"{draft.order_draft.order_count}. Each component requires its "
                    "own review and temporary exposure can exist between accepted orders."
                ),
            )
        )
    if freshness == OrderReviewQuoteState.STALE:
        notices.append(
            _blocking(
                "Stale Quote",
                "The exact-leg quote is too old for placement. Refresh and review again.",
            )
        )
    elif freshness == OrderReviewQuoteState.UNAVAILABLE:
        notices.append(
            _blocking(
                "Quote Unavailable",
                "Placement remains blocked until every exact leg is refreshed from Schwab.",
            )
        )
    estimated_funds_required = draft.estimated_funds_required
    if (
        estimated_funds_required is None
        and estimated_cash_effect is not None
        and estimated_cash_effect < 0.0
    ):
        estimated_funds_required = abs(estimated_cash_effect)
    applicable_funds = (
        draft.applicable_funds
        if draft.applicable_funds is not None
        else draft.available_cash
    )
    if estimated_funds_required is not None and applicable_funds is None:
        notices.append(
            _blocking(
                "Applicable Balance Unavailable",
                (
                    f"The local requirement estimate is "
                    f"{_money(estimated_funds_required)}, but Schwab did not report "
                    f"{draft.applicable_funds_label.lower()}."
                ),
            )
        )
    elif (
        estimated_funds_required is not None
        and applicable_funds is not None
        and estimated_funds_required > applicable_funds + 1e-9
    ):
        notices.append(
            _blocking(
                "Insufficient Available Funds",
                (
                    f"The local requirement estimate is "
                    f"{_money(estimated_funds_required)}, but refreshed Schwab "
                    f"{draft.applicable_funds_label.lower()} are "
                    f"{_money(applicable_funds)}."
                ),
            )
        )

    payload_valid = True
    try:
        draft.payload()
    except (TypeError, ValueError):
        payload_valid = False
        notices.append(
            _blocking(
                "Invalid Strategy Order",
                "The selected quantity, order method, duration, or limit is invalid.",
            )
        )
    price_rail = _price_rail(draft)
    available_funds = (
        "Unavailable"
        if applicable_funds is None
        else _money(applicable_funds)
    )
    classification = (
        "Evidence Refresh Required"
        if draft.research_only
        else "Publication Checks Passed"
    )
    execution_mode = (
        "Atomic Net Order"
        if len(component.legs) > 1
        else "Single Opening Order"
    )
    if draft.order_draft.order_count > 1:
        execution_mode += (
            f" • Component {draft.order_index + 1} of "
            f"{draft.order_draft.order_count}"
        )
    return OptionOrderReview(
        operation=OrderReviewOperation.ENTRY,
        title="Review Strategy Order",
        subtitle=(
            f"Open {draft.order_draft.strategy_display_name} • "
            f"{len(legs)} Exact Leg{'s' if len(legs) != 1 else ''} • "
            f"Qty {draft.strategy_quantity}"
        ),
        account_display_label=mask_account_label(draft.account_label),
        strategy_label=draft.order_draft.strategy_display_name,
        instruction="Open Exact Strategy Legs",
        order_type=_order_type_label(draft.order_method),
        duration=draft.duration,
        execution_mode=execution_mode,
        legs=legs,
        package_quantity=draft.strategy_quantity,
        price_title="Net Price",
        net_price=draft.limit_price,
        cash_direction=direction,
        price_rail=price_rail,
        price_editable=draft.order_method != MARKET_ORDER,
        price_editor_explanation=(
            "Changing the limit resets acknowledgment. Final placement refreshes "
            "the exact Schwab quotes again; a limit order may not fill."
        ),
        estimated_cash_effect=estimated_cash_effect,
        estimated_cash_label=(
            "Estimated Proceeds"
            if direction == OrderReviewCashDirection.CREDIT
            else "Estimated Cost"
        ),
        price_provenance="Configured limit with current exact-leg Schwab quotes",
        display_quote_at=draft.quote_observed_at,
        validation_quote_at=draft.quote_observed_at,
        max_quote_age_seconds=max_quote_age_seconds,
        quote_state=freshness,
        metrics=(
            OptionOrderReviewMetric(
                "Publication Readiness",
                classification,
                "Schwab-Validated Review",
                "Strategy publication plus current broker refresh",
                before_tone="warning" if draft.research_only else "positive",
            ),
            OptionOrderReviewMetric(
                "Strategy Units",
                "0",
                str(draft.strategy_quantity),
                LOCAL_CALCULATION,
            ),
            OptionOrderReviewMetric(
                "Exact Legs",
                "0",
                str(len(legs)),
                LOCAL_CALCULATION,
            ),
            OptionOrderReviewMetric(
                "Working Option Orders",
                str(draft.working_option_orders),
                "Broker Determined After Placement",
                "Current Schwab account read",
            ),
        ),
        costs=(
            OptionOrderReviewCost(
                "Estimated Fees",
                "Unavailable",
                "Unavailable Until Broker Review",
            ),
            OptionOrderReviewCost(
                "Estimated Net Proceeds"
                if direction == OrderReviewCashDirection.CREDIT
                else "Estimated Net Cost",
                _money(abs(estimated_cash_effect))
                if estimated_cash_effect is not None
                else "Unavailable",
                LOCAL_CALCULATION,
                tone=(
                    "positive"
                    if direction == OrderReviewCashDirection.CREDIT
                    else "negative"
                ),
                estimated=True,
            ),
            OptionOrderReviewCost(
                draft.applicable_funds_label,
                available_funds,
                "Current Schwab account read",
            ),
            OptionOrderReviewCost(
                "Estimated Funds Required",
                (
                    _money(estimated_funds_required)
                    if estimated_funds_required is not None
                    else "Unavailable"
                ),
                draft.requirement_basis or LOCAL_CALCULATION,
                estimated=True,
            ),
            OptionOrderReviewCost(
                "Estimated Funds After",
                (
                    _money(draft.funds_after_estimate)
                    if draft.funds_after_estimate is not None
                    else "Unavailable"
                ),
                LOCAL_CALCULATION,
                tone=(
                    "negative"
                    if draft.funds_after_estimate is not None
                    and draft.funds_after_estimate < 0.0
                    else "positive"
                ),
                estimated=True,
            ),
            OptionOrderReviewCost(
                "Broker Preview",
                "Not Run",
                PREVIEW_FALLBACK_STATUS,
            ),
            OptionOrderReviewCost(
                "Quote Age",
                "Refreshing" if draft.quote_observed_at is None else "Current",
                "Exact-leg Schwab quote read",
            ),
        ),
        notices=tuple(notices),
        acknowledgment_copy=(
            "I reviewed every contract, action, quantity, limit, warning, and the "
            "publication-evidence notice."
            if draft.research_only
            else "I reviewed every contract, action, quantity, limit, and warning."
        ),
        safety_copy=(
            "This order opens new market risk. Placement requires current exact-leg "
            "Schwab quotes and explicit confirmation."
        ),
        placement_capability=OrderReviewPlacementCapability.SUPPORTED,
        placement_disabled_reason=None,
        primary_action_label="Place Strategy Order",
        broker_preview_status=PREVIEW_FALLBACK_STATUS,
        internal_valid=bool(payload_valid and legs and draft.account_label),
    )


class StrategyOrderReviewController:
    """Concept D state machine for exactly-once manual strategy entry."""

    def __init__(
        self,
        *,
        draft: StrategyEntryOrderReviewDraft,
        refresher: Callable[
            [StrategyEntryOrderReviewDraft], StrategyEntryOrderReviewDraft
        ],
        session_factory: Callable[[], object],
        on_accepted: Callable[[StrategyEntryOrderReviewDraft], None] | None = None,
        on_unknown: Callable[[], None] | None = None,
        now_provider: Callable[[], datetime] | None = None,
        state_listener: Callable[[StrategyOrderReviewController], None] | None = None,
    ) -> None:
        self.draft = draft
        self.refresher = refresher
        self.session_factory = session_factory
        self.on_accepted = on_accepted
        self.on_unknown = on_unknown
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self.state_listener = state_listener
        self.review = strategy_entry_order_review(
            draft,
            now=self.now_provider(),
        )
        self.acknowledged = False
        self.state = OrderReviewPlacementState.READY
        self._lock = threading.RLock()
        self._refreshing = False
        self._transmission_started = False

    @property
    def supports_background_refresh(self) -> bool:
        return True

    @property
    def can_place(self) -> bool:
        with self._lock:
            return self._can_place_unlocked()

    @property
    def can_finish_review(self) -> bool:
        return False

    @property
    def primary_action_enabled(self) -> bool:
        return self.can_place

    @property
    def state_text(self) -> str:
        if self.state == OrderReviewPlacementState.REVALIDATING:
            return "Revalidating Account and Quotes…"
        if self.state == OrderReviewPlacementState.FALLBACK:
            return "Using Local-Estimate Fallback…"
        if self.state == OrderReviewPlacementState.SUBMITTING:
            return "Submitting…"
        if self.state == OrderReviewPlacementState.ACCEPTED:
            return "Order Accepted"
        if self.state == OrderReviewPlacementState.REJECTED:
            return "Review Rejected"
        if self.state == OrderReviewPlacementState.UNKNOWN:
            return "Submission Result Unknown"
        if self._refreshing or self.review.quote_state == OrderReviewQuoteState.UPDATING:
            return "Refreshing Account and Quotes…"
        if self.review.quote_state == OrderReviewQuoteState.STALE:
            return "Stale Quote — Refresh Required"
        if self.review.quote_state == OrderReviewQuoteState.UNAVAILABLE:
            return "Current Schwab Quote Required"
        if self.review.has_blocking_notice or not self.review.internal_valid:
            return "Review Action Required"
        return (
            "Ready for Final Revalidation"
            if self.acknowledged
            else "Confirmation Required"
        )

    def acknowledge(self, acknowledged: bool) -> None:
        with self._lock:
            if self.state in {
                OrderReviewPlacementState.REVALIDATING,
                OrderReviewPlacementState.SUBMITTING,
                OrderReviewPlacementState.ACCEPTED,
                OrderReviewPlacementState.UNKNOWN,
            }:
                return
            self.acknowledged = bool(acknowledged)
        self._notify()

    def set_limit_price(self, value: object) -> OptionOrderReview:
        with self._lock:
            if not self.review.price_editable:
                raise ValueError("This reviewed order does not use an editable limit.")
            if self.state != OrderReviewPlacementState.READY or self._refreshing:
                raise ValueError(
                    "Wait for the current review operation to finish before changing price."
                )
            updated = replace(
                self.draft,
                limit_price=_positive_number(value, "Limit price"),
            )
            updated.payload()
            self.draft = updated
            self.review = strategy_entry_order_review(
                updated,
                now=self.now_provider(),
                max_quote_age_seconds=self.review.max_quote_age_seconds,
            )
            self.acknowledged = False
            result = self.review
        self._notify()
        return result

    def refresh_review(self) -> OptionOrderReview:
        with self._lock:
            if self.state in {
                OrderReviewPlacementState.ACCEPTED,
                OrderReviewPlacementState.UNKNOWN,
            }:
                return self.review
            prior_review = self.review
            self._refreshing = True
            self.acknowledged = False
            self.review = replace(
                self.review,
                quote_state=OrderReviewQuoteState.UPDATING,
            )
        self._notify()
        try:
            refreshed = self.refresher(self.draft)
            updated_review = strategy_entry_order_review(
                refreshed,
                now=self.now_provider(),
                max_quote_age_seconds=prior_review.max_quote_age_seconds,
            )
        except Exception:
            with self._lock:
                self._refreshing = False
                self.review = replace(
                    prior_review,
                    quote_state=quote_state(
                        prior_review.validation_quote_at,
                        now=self.now_provider(),
                        max_age_seconds=prior_review.max_quote_age_seconds,
                    ),
                )
            self._notify()
            raise
        with self._lock:
            self.draft = refreshed
            self.review = updated_review
            self._refreshing = False
            self.state = OrderReviewPlacementState.READY
        self._notify()
        return updated_review

    def age_quotes(self, *, now: datetime | None = None) -> OrderReviewQuoteState:
        changed = False
        with self._lock:
            if self._refreshing:
                return self.review.quote_state
            updated = quote_state(
                self.review.validation_quote_at,
                now=now or self.now_provider(),
                max_age_seconds=self.review.max_quote_age_seconds,
            )
            if updated != self.review.quote_state:
                self.review = replace(self.review, quote_state=updated)
                changed = True
                if updated in {
                    OrderReviewQuoteState.STALE,
                    OrderReviewQuoteState.UNAVAILABLE,
                }:
                    self.acknowledged = False
        if changed:
            self._notify()
        return updated

    def finish_review(self) -> bool:
        return False

    def abandon_review(self) -> None:
        """Closing or backing out is intentionally side-effect free."""

    def save_order(self) -> bool:
        return False

    def place(self) -> OrderReviewPlacementOutcome:
        with self._lock:
            if not self._can_place_unlocked():
                return OrderReviewPlacementOutcome(
                    OrderReviewOutcomeStatus.BLOCKED,
                    self.state_text,
                )
            reviewed = self.draft
            reviewed_semantic = _semantic_fingerprint(reviewed)
            reviewed_market = _market_fingerprint(reviewed)
            self.state = OrderReviewPlacementState.REVALIDATING
        self._notify()

        try:
            refreshed = self.refresher(reviewed)
            refreshed_review = strategy_entry_order_review(
                refreshed,
                now=self.now_provider(),
                max_quote_age_seconds=self.review.max_quote_age_seconds,
            )
        except Exception as exc:
            return self._reject_before_submission(
                OrderReviewOutcomeStatus.NETWORK_FAILED
                if isinstance(exc, requests.RequestException)
                else OrderReviewOutcomeStatus.INVALIDATED,
                f"Final account/quote refresh failed: {_safe_error(exc)}",
                retryable=isinstance(exc, requests.RequestException),
            )

        if (
            refreshed_review.quote_state
            not in {OrderReviewQuoteState.LIVE, OrderReviewQuoteState.AGING}
            or refreshed_review.has_blocking_notice
            or not refreshed_review.internal_valid
        ):
            with self._lock:
                self.draft = refreshed
                self.review = refreshed_review
                self.acknowledged = False
                self.state = OrderReviewPlacementState.READY
            self._notify()
            return OrderReviewPlacementOutcome(
                OrderReviewOutcomeStatus.INVALIDATED,
                "Current account or quote state requires another review.",
                retryable=True,
            )

        if (
            _semantic_fingerprint(refreshed) != reviewed_semantic
            or _market_fingerprint(refreshed) != reviewed_market
        ):
            with self._lock:
                self.draft = refreshed
                self.review = replace(
                    refreshed_review,
                    notices=(
                        *refreshed_review.notices,
                        _warning(
                            "Order Facts Changed",
                            "Account coverage or exact-leg quotes changed during final revalidation; review and acknowledge again.",
                        ),
                    ),
                )
                self.acknowledged = False
                self.state = OrderReviewPlacementState.READY
            self._notify()
            return OrderReviewPlacementOutcome(
                OrderReviewOutcomeStatus.INVALIDATED,
                "Current order facts changed; review the refreshed values and confirm again.",
                retryable=True,
            )

        try:
            payload = refreshed.payload()
            session = self.session_factory()
            submit = getattr(session, "submit_order", None)
            if not callable(submit):
                raise TypeError("Schwab session does not provide submit_order.")
        except Exception as exc:
            return self._reject_before_submission(
                OrderReviewOutcomeStatus.REJECTED,
                _safe_error(exc),
                retryable=False,
            )

        self._set_state(OrderReviewPlacementState.FALLBACK)
        self._set_state(OrderReviewPlacementState.SUBMITTING)
        with self._lock:
            if self._transmission_started:
                self.state = OrderReviewPlacementState.UNKNOWN
                self._notify()
                return OrderReviewPlacementOutcome(
                    OrderReviewOutcomeStatus.UNKNOWN,
                    "Submission result unknown; check Orders before taking any further action.",
                )
            self._transmission_started = True
        try:
            location = submit(payload)
        except Exception as exc:
            return self._submission_exception(exc)

        submission = ClosingOrderSubmission(payload=payload, location=location)
        with self._lock:
            self.draft = refreshed
            self.review = refreshed_review
            self.state = OrderReviewPlacementState.ACCEPTED
        self._notify()
        if self.on_accepted is not None:
            try:
                self.on_accepted(refreshed)
            except Exception:
                pass
        return OrderReviewPlacementOutcome(
            OrderReviewOutcomeStatus.ACCEPTED,
            "Schwab accepted the strategy order.",
            submission=submission,
        )

    def _can_place_unlocked(self) -> bool:
        return bool(
            self.review.placement_capability
            == OrderReviewPlacementCapability.SUPPORTED
            and self.acknowledged
            and self.review.internal_valid
            and not self.review.has_blocking_notice
            and self.review.quote_state
            in {OrderReviewQuoteState.LIVE, OrderReviewQuoteState.AGING}
            and self.state == OrderReviewPlacementState.READY
            and not self._refreshing
            and not self._transmission_started
        )

    def _set_state(self, state: OrderReviewPlacementState) -> None:
        with self._lock:
            self.state = state
        self._notify()

    def _reject_before_submission(
        self,
        status: OrderReviewOutcomeStatus,
        message: str,
        *,
        retryable: bool,
    ) -> OrderReviewPlacementOutcome:
        with self._lock:
            self.state = (
                OrderReviewPlacementState.READY
                if retryable
                else OrderReviewPlacementState.REJECTED
            )
            self.acknowledged = False
        self._notify()
        return OrderReviewPlacementOutcome(
            status,
            message,
            retryable=retryable,
        )

    def _submission_exception(
        self,
        exc: Exception,
    ) -> OrderReviewPlacementOutcome:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if isinstance(exc, (BrokerAuthenticationFailure, BrokerOrderRejected)) or (
            isinstance(status_code, int) and 400 <= status_code < 500
        ):
            with self._lock:
                self._transmission_started = False
            return self._reject_before_submission(
                OrderReviewOutcomeStatus.REJECTED,
                _safe_error(exc),
                retryable=isinstance(status_code, int)
                and status_code in {401, 403},
            )
        with self._lock:
            self.state = OrderReviewPlacementState.UNKNOWN
            self.acknowledged = False
            self.review = replace(
                self.review,
                notices=(
                    *self.review.notices,
                    _blocking(
                        "Submission Result Unknown",
                        "Do not resubmit blindly. Check Schwab Orders first.",
                    ),
                ),
            )
        self._notify()
        if self.on_unknown is not None:
            try:
                self.on_unknown()
            except Exception:
                pass
        message = (
            "Submission result unknown; check Schwab Orders before taking any "
            "further action."
        )
        if isinstance(exc, BrokerSubmissionResultUnknown):
            message += f" {_safe_error(exc)}"
        return OrderReviewPlacementOutcome(
            OrderReviewOutcomeStatus.UNKNOWN,
            message,
        )

    def _notify(self) -> None:
        listener = self.state_listener
        if listener is not None:
            listener(self)


def _semantic_fingerprint(draft: StrategyEntryOrderReviewDraft) -> tuple[object, ...]:
    component = draft.component
    return (
        draft.order_draft.candidate_id,
        draft.order_index,
        draft.strategy_quantity,
        draft.order_method,
        draft.limit_price,
        draft.duration,
        draft.account_label,
        draft.available_cash,
        draft.applicable_funds,
        draft.applicable_funds_label,
        draft.estimated_funds_required,
        draft.funds_after_estimate,
        draft.working_option_orders,
        draft.order_draft.uses_existing_shares,
        draft.order_draft.shares_available,
        tuple(
            (leg.symbol, leg.instruction, leg.quantity, leg.asset_type)
            for leg in component.legs
        ),
    )


def _market_fingerprint(draft: StrategyEntryOrderReviewDraft) -> tuple[object, ...]:
    return tuple(
        (leg.symbol, leg.bid, leg.ask)
        for leg in draft.component.legs
    )


def _cash_direction(
    draft: StrategyEntryOrderReviewDraft,
) -> OrderReviewCashDirection:
    if draft.order_method == NET_CREDIT_LIMIT:
        return OrderReviewCashDirection.CREDIT
    if draft.order_method == NET_DEBIT_LIMIT:
        return OrderReviewCashDirection.DEBIT
    component = draft.component
    if len(component.legs) == 1 and component.legs[0].instruction.startswith("SELL"):
        return OrderReviewCashDirection.CREDIT
    return OrderReviewCashDirection.DEBIT


def _estimated_cash_effect(
    draft: StrategyEntryOrderReviewDraft,
    direction: OrderReviewCashDirection,
) -> float | None:
    if draft.limit_price is None:
        return None
    component = draft.component
    if len(component.legs) == 1:
        multiplier = component.legs[0].multiplier
        units = component.legs[0].quantity * draft.strategy_quantity
    else:
        multiplier = 100.0
        units = draft.strategy_quantity
    amount = round(draft.limit_price * multiplier * units, 2)
    return amount if direction == OrderReviewCashDirection.CREDIT else -amount


def _price_rail(
    draft: StrategyEntryOrderReviewDraft,
) -> OptionOrderReviewPriceRail | None:
    if draft.limit_price is None:
        return None
    legs = draft.component.legs
    if any(leg.bid is None or leg.ask is None for leg in legs):
        return None
    if len(legs) == 1:
        bid = float(legs[0].bid)
        ask = float(legs[0].ask)
        midpoint = (bid + ask) / 2.0
    else:
        natural = 0.0
        opposite = 0.0
        midpoint_cash = 0.0
        for leg in legs:
            sign = 1.0 if leg.instruction.startswith("BUY") else -1.0
            natural_quote = float(leg.ask if sign > 0 else leg.bid)
            opposite_quote = float(leg.bid if sign > 0 else leg.ask)
            mid_quote = (float(leg.bid) + float(leg.ask)) / 2.0
            natural += sign * natural_quote * leg.quantity * leg.multiplier
            opposite += sign * opposite_quote * leg.quantity * leg.multiplier
            midpoint_cash += sign * mid_quote * leg.quantity * leg.multiplier
        values = sorted((abs(opposite) / 100.0, abs(natural) / 100.0))
        bid, ask = values
        midpoint = abs(midpoint_cash) / 100.0
    return OptionOrderReviewPriceRail(
        bid=round(bid, 2),
        midpoint=round(min(max(midpoint, bid), ask), 2),
        ask=round(ask, 2),
        selected=round(draft.limit_price, 2),
    )


def _order_type_label(method: str) -> str:
    return {
        "Day only": "Day Only",
        "Limit": "Limit",
        "Market": "Market",
        "Net debit limit": "Net Debit Limit",
        "Net credit limit": "Net Credit Limit",
    }.get(method, method)


def _human_instruction(value: str) -> str:
    return {
        "BUY": "Buy",
        "SELL": "Sell",
        "BUY_TO_OPEN": "Buy to Open",
        "SELL_TO_OPEN": "Sell to Open",
    }.get(value, value.replace("_", " ").title())


def _information(title: str, detail: str) -> OptionOrderReviewNotice:
    return OptionOrderReviewNotice(
        OrderReviewNoticeSeverity.INFORMATION,
        title,
        detail,
    )


def _warning(title: str, detail: str) -> OptionOrderReviewNotice:
    return OptionOrderReviewNotice(
        OrderReviewNoticeSeverity.WARNING,
        title,
        detail,
    )


def _blocking(title: str, detail: str) -> OptionOrderReviewNotice:
    return OptionOrderReviewNotice(
        OrderReviewNoticeSeverity.BLOCKING,
        title,
        detail,
        blocking=True,
    )


def _positive_number(value: object, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive number.") from exc
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{label} must be a positive number.")
    return round(number, 2)


def _positive_int(value: object, label: str) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive whole number.") from exc
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{label} must be a positive whole number.")
    integer = int(number)
    if not math.isclose(number, integer, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError(f"{label} must be a whole number.")
    return integer


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _safe_error(exc: Exception) -> str:
    return str(exc).strip() or type(exc).__name__


__all__ = [
    "StrategyEntryOrderReviewDraft",
    "StrategyOrderReviewController",
    "build_strategy_entry_review_draft",
    "refresh_strategy_entry_review_draft",
    "strategy_entry_order_review",
]
