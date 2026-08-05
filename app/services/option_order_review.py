from __future__ import annotations

import math
import re
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import requests

from app.models.option_management import (
    ClosingOrderDraft,
    ClosingOrderSubmission,
    ExitPlanDraft,
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
    RollOrderDraft,
)
from app.models.portfolio import PortfolioSnapshot
from app.services.schwab_option_management import (
    build_closing_order_draft,
    build_closing_order_payload,
    option_position_book,
    validate_closing_position_drift,
)


DEFAULT_REVIEW_MAX_QUOTE_AGE_SECONDS = 120.0
LOCAL_CALCULATION = "Local calculation"
CURRENT_SCHWAB_QUOTE = "Current Schwab quote"
BROKER_PREVIEW = "Broker preview"
UNAVAILABLE_UNTIL_BROKER_REVIEW = "Unavailable until broker review"
PREVIEW_FALLBACK_STATUS = "Not previewed — verified schema unavailable; local-estimate fallback"


class BrokerAuthenticationFailure(RuntimeError):
    """The broker rejected authentication before an order was accepted."""


class BrokerNetworkFailure(RuntimeError):
    """A pre-transmission broker read failed and may be retried safely."""


class BrokerOrderRejected(RuntimeError):
    """The broker explicitly rejected a preview or placement request."""


class BrokerSubmissionResultUnknown(RuntimeError):
    """Transmission may have begun, so retrying blindly is unsafe."""


@dataclass(frozen=True)
class BrokerOrderPreview:
    accepted: bool
    reason: str | None = None
    estimated_fees: float | None = None
    buying_power_after: float | None = None
    settlement: str | None = None


def mask_account_label(label: str) -> str:
    """Mask long account-like tokens while preserving a useful provider/suffix label."""

    text = str(label or "").strip()
    if not text:
        return "Account unavailable"
    if re.search(r"[•*]{2,}\s*[A-Za-z0-9]{1,4}$", text):
        return text

    def mask_token(match: re.Match[str]) -> str:
        token = match.group(0)
        if len(token) <= 4 or not any(character.isdigit() for character in token):
            return token
        return "••••" + token[-4:]

    masked = re.sub(r"(?<![A-Za-z0-9])[A-Za-z0-9-]{6,}(?![A-Za-z0-9])", mask_token, text)
    return masked


def quote_age_seconds(timestamp: datetime | None, *, now: datetime) -> float | None:
    if timestamp is None:
        return None
    current = _aware(now)
    observed = _aware(timestamp)
    return max(0.0, (current - observed).total_seconds())


def quote_state(
    timestamp: datetime | None,
    *,
    now: datetime,
    max_age_seconds: float = DEFAULT_REVIEW_MAX_QUOTE_AGE_SECONDS,
) -> OrderReviewQuoteState:
    if timestamp is None or max_age_seconds <= 0:
        return OrderReviewQuoteState.UNAVAILABLE
    current = _aware(now)
    observed = _aware(timestamp)
    raw_age = (current - observed).total_seconds()
    if raw_age < -30:
        return OrderReviewQuoteState.UNAVAILABLE
    age = max(0.0, raw_age)
    if age > max_age_seconds:
        return OrderReviewQuoteState.STALE
    if age > max_age_seconds * 0.5:
        return OrderReviewQuoteState.AGING
    return OrderReviewQuoteState.LIVE


def closing_price_rail(draft: ClosingOrderDraft) -> OptionOrderReviewPriceRail | None:
    low_cash = 0.0
    high_cash = 0.0
    for leg in draft.legs:
        if leg.bid is None or leg.ask is None or leg.bid < 0 or leg.ask < leg.bid:
            return None
        if leg.instruction.startswith("SELL"):
            low_cash += leg.bid * leg.ratio_quantity
            high_cash += leg.ask * leg.ratio_quantity
        else:
            low_cash -= leg.ask * leg.ratio_quantity
            high_cash -= leg.bid * leg.ratio_quantity
    if not draft.legs:
        return None
    if draft.api_order_type == "LIMIT":
        price_sign = 1.0 if draft.legs[0].instruction.startswith("SELL") else -1.0
    else:
        price_sign = 1.0 if draft.api_order_type == "NET_CREDIT" else -1.0
    prices = sorted((price_sign * low_cash, price_sign * high_cash))
    if prices[0] <= 0:
        return None
    bid = _cent(prices[0])
    ask = _cent(prices[1])
    return OptionOrderReviewPriceRail(
        bid=bid,
        midpoint=_cent((bid + ask) / 2.0),
        ask=ask,
        selected=_cent(draft.limit_price),
    )


def reprice_closing_order_draft(
    draft: ClosingOrderDraft,
    limit_price: object,
) -> ClosingOrderDraft:
    price = _finite_number(limit_price)
    if price is None or price <= 0 or price > 99_999.99:
        raise ValueError("Limit price must be between $0.01 and $99,999.99.")
    price = _cent(price)
    if price < 0.01:
        raise ValueError("Limit price must be at least $0.01.")
    if not draft.legs:
        raise ValueError("A closing review needs at least one exact leg.")
    multipliers = {round(leg.contract_multiplier, 8) for leg in draft.legs}
    if len(multipliers) != 1:
        raise ValueError("Reviewed legs do not share one verified contract multiplier.")
    if draft.api_order_type == "NET_CREDIT":
        direction = 1.0
    elif draft.api_order_type == "NET_DEBIT":
        direction = -1.0
    elif draft.api_order_type == "LIMIT":
        direction = 1.0 if draft.legs[0].instruction.startswith("SELL") else -1.0
    else:
        raise ValueError(f"Unsupported reviewed order type: {draft.api_order_type or 'missing'}")
    estimated_cash = direction * price * next(iter(multipliers)) * draft.order_quantity
    return replace(
        draft,
        limit_price=price,
        estimated_cash_effect=round(estimated_cash, 2),
    )


def refresh_closing_order_draft(
    draft: ClosingOrderDraft,
    latest_snapshot: PortfolioSnapshot,
) -> ClosingOrderDraft:
    validate_closing_position_drift(draft, latest_snapshot)
    latest_book = option_position_book(latest_snapshot)
    if latest_book.status != "CURRENT":
        raise ValueError("Current Schwab option positions are unavailable or stale; review again.")
    refreshed = build_closing_order_draft(
        latest_book,
        (leg.symbol for leg in draft.legs),
        duration=draft.duration,
        limit_price=draft.limit_price,
    )
    if _closing_semantic_fingerprint(refreshed) != _closing_semantic_fingerprint(draft):
        raise ValueError("Position drift changed the reviewed account, exact legs, quantities, ratios, or order shape.")
    return refreshed


def closing_order_review(
    draft: ClosingOrderDraft,
    *,
    now: datetime | None = None,
    max_quote_age_seconds: float = DEFAULT_REVIEW_MAX_QUOTE_AGE_SECONDS,
) -> OptionOrderReview:
    current = _aware(now or datetime.now(timezone.utc))
    legs = tuple(
        OptionOrderReviewLeg(
            role="Close",
            action=_human_instruction(leg.instruction),
            quantity=leg.quantity,
            contract_label=_contract_label(
                leg.underlying_symbol,
                leg.expiration,
                leg.strike,
                leg.option_type,
            ),
            symbol=leg.symbol,
            bid=leg.bid,
            ask=leg.ask,
            mark=leg.mark,
            before_quantity=leg.before_quantity,
            after_quantity=leg.after_quantity,
            quote_observed_at=leg.quote_observed_at,
        )
        for leg in draft.legs
    )
    timestamps = tuple(leg.quote_observed_at for leg in legs)
    display_quote_at, validation_quote_at = _quote_bounds(timestamps)
    freshness = quote_state(
        validation_quote_at,
        now=current,
        max_age_seconds=max_quote_age_seconds,
    )
    rail = closing_price_rail(draft)
    notices: list[OptionOrderReviewNotice] = []
    notices.extend(_quote_notices(freshness, validation_quote_at, current, max_quote_age_seconds))
    if rail is None:
        notices.append(
            _blocking(
                "Executable quote unavailable",
                "Every reviewed leg needs a valid bid and ask before placement.",
            )
        )
    else:
        distance = abs(draft.limit_price - rail.midpoint)
        relation = "at" if distance < 0.005 else "away from"
        notices.append(
            _warning(
                f"Limit price is {relation} the current midpoint",
                "The market may move before this order fills.",
            )
        )
    if len(legs) > 1:
        notices.append(
            _information(
                "Atomic net-order structure",
                f"All {len(legs)} exact legs will be transmitted as one custom net order; partial fills may still be possible.",
            )
        )
    provenance_details: list[str] = []
    for warning in draft.warnings:
        normalized_warning = warning.lower()
        if "re-read" in normalized_warning or "local estimate" in normalized_warning:
            provenance_details.append(warning)
        elif len(legs) > 1 and "custom net order" in normalized_warning and "partial fills" in normalized_warning:
            # The atomic-structure notice immediately above already carries this risk.
            continue
        else:
            notices.append(_warning("Execution warning", warning))
    if provenance_details:
        notices.append(
            _information(
                "Data provenance and revalidation",
                " ".join(provenance_details),
            )
        )
    internal_valid = bool(
        legs
        and draft.account_label
        and draft.limit_price > 0
        and draft.order_quantity > 0
        and all(leg.symbol and leg.quantity and leg.quantity > 0 for leg in legs)
    )
    if not internal_valid:
        notices.append(_blocking("Invalid reviewed draft", "Required account, leg, quantity, or price data is missing."))
    direction = (
        OrderReviewCashDirection.CREDIT
        if draft.estimated_cash_effect >= 0
        else OrderReviewCashDirection.DEBIT
    )
    strategy = "Exact option position" if len(legs) == 1 else "Selected exact legs"
    subtitle = (
        f"{strategy} • {len(legs)} exact leg{'s' if len(legs) != 1 else ''} "
        f"• Qty {draft.order_quantity}"
    )
    return OptionOrderReview(
        operation=OrderReviewOperation.CLOSE,
        title="Review closing order",
        subtitle=subtitle,
        account_display_label=mask_account_label(draft.account_label),
        strategy_label=strategy,
        instruction="Close entire position" if len(legs) == 1 else "Close selected exact legs",
        order_type=_order_type_label(draft.api_order_type),
        duration=draft.duration,
        execution_mode="Atomic net order" if len(legs) > 1 else "Single closing order",
        legs=legs,
        package_quantity=draft.order_quantity,
        price_title="Net price",
        net_price=draft.limit_price,
        cash_direction=direction,
        price_rail=rail,
        price_editable=True,
        price_editor_explanation="Changing the limit rebuilds this immutable closing draft and requires confirmation again.",
        estimated_cash_effect=draft.estimated_cash_effect,
        estimated_cash_label=(
            "Estimated proceeds" if draft.estimated_cash_effect >= 0 else "Estimated cost"
        ),
        price_provenance=f"{LOCAL_CALCULATION} from {CURRENT_SCHWAB_QUOTE.lower()} marks",
        display_quote_at=display_quote_at,
        validation_quote_at=validation_quote_at,
        max_quote_age_seconds=max_quote_age_seconds,
        quote_state=freshness,
        metrics=(
            OptionOrderReviewMetric(
                "Position quantity",
                _quantity(draft.order_quantity),
                "0",
                LOCAL_CALCULATION,
            ),
            OptionOrderReviewMetric("Open P/L", "—", "Realized / remaining unavailable", UNAVAILABLE_UNTIL_BROKER_REVIEW),
            OptionOrderReviewMetric("Buying power", "—", "—", UNAVAILABLE_UNTIL_BROKER_REVIEW),
            OptionOrderReviewMetric("Delta", "—", "—", "Position Greeks unavailable in closing draft"),
            OptionOrderReviewMetric("Theta / day", "—", "—", "Position Greeks unavailable in closing draft"),
        ),
        costs=(
            OptionOrderReviewCost("Estimated fees", "Unavailable", UNAVAILABLE_UNTIL_BROKER_REVIEW),
            OptionOrderReviewCost(
                "Estimated net proceeds" if draft.estimated_cash_effect >= 0 else "Estimated net cost",
                _money(abs(draft.estimated_cash_effect)),
                LOCAL_CALCULATION,
                tone="positive" if draft.estimated_cash_effect >= 0 else "negative",
                estimated=True,
            ),
            OptionOrderReviewCost("Settlement", "Unavailable", UNAVAILABLE_UNTIL_BROKER_REVIEW),
            OptionOrderReviewCost("Broker preview", "Not run", "Verified preview schema unavailable"),
            OptionOrderReviewCost("Quote age", _age_label(validation_quote_at, current), CURRENT_SCHWAB_QUOTE),
        ),
        notices=_dedupe_notices(notices),
        acknowledgment_copy="I reviewed the contracts, actions, quantities, price, and warnings.",
        safety_copy="This order closes a position; it does not open a new one.",
        placement_capability=OrderReviewPlacementCapability.SUPPORTED,
        placement_disabled_reason=None,
        primary_action_label="Place closing order",
        broker_preview_status=PREVIEW_FALLBACK_STATUS,
        internal_valid=internal_valid,
    )


def roll_order_review(
    draft: RollOrderDraft,
    *,
    now: datetime | None = None,
    max_quote_age_seconds: float = DEFAULT_REVIEW_MAX_QUOTE_AGE_SECONDS,
) -> OptionOrderReview:
    current = _aware(now or datetime.now(timezone.utc))
    legs = tuple(
        OptionOrderReviewLeg(
            role="Close" if leg.role.upper() == "CLOSE" else "Open replacement",
            action=_human_instruction(leg.instruction),
            quantity=leg.quantity,
            contract_label=_contract_label(
                leg.underlying_symbol,
                leg.expiration,
                leg.strike,
                leg.option_type,
            ),
            symbol=leg.symbol,
            bid=leg.bid,
            ask=leg.ask,
            mark=leg.mark,
            before_quantity=leg.before_quantity,
            after_quantity=leg.after_quantity,
            quote_observed_at=leg.quote_observed_at,
        )
        for leg in draft.all_legs
    )
    display_quote_at, validation_quote_at = _quote_bounds(tuple(leg.quote_observed_at for leg in legs))
    freshness = quote_state(
        validation_quote_at,
        now=current,
        max_age_seconds=max_quote_age_seconds,
    )
    notices: list[OptionOrderReviewNotice] = list(
        _quote_notices(freshness, validation_quote_at, current, max_quote_age_seconds)
    )
    notices.append(
        _information(
            "Execution structure",
            f"{draft.execution_detail}. This route is review only and cannot transmit a broker order.",
        )
    )
    if draft.execution_mode != "ATOMIC":
        notices.append(
            _warning(
                "Atomic roll placement is not verified",
                "Separate close/open components can create temporary exposure and are not submitted here.",
            )
        )
    notices.extend(_warning("Roll warning", warning) for warning in draft.warnings)
    notices.extend(_blocking("Roll review blocked", blocker) for blocker in draft.review_blockers)
    rail = OptionOrderReviewPriceRail(
        bid=draft.price_rail.bid,
        midpoint=draft.price_rail.midpoint,
        ask=draft.price_rail.ask,
        selected=draft.limit_price,
    )
    strategy = "Exact option position" if len(draft.close_legs) == 1 else "Custom option strategy"
    direction = (
        OrderReviewCashDirection.CREDIT
        if draft.estimated_cash_effect >= 0
        else OrderReviewCashDirection.DEBIT
    )
    before_metrics = draft.analysis.before_metrics
    after_metrics = draft.analysis.after_metrics
    internal_valid = bool(draft.review_eligible and legs and draft.account_label and draft.order_quantity > 0)
    return OptionOrderReview(
        operation=OrderReviewOperation.ROLL,
        title="Review roll order",
        subtitle=(
            f"Roll {draft.underlying_symbol} {strategy} • {len(legs)} exact legs "
            f"• Qty {draft.order_quantity}"
        ),
        account_display_label=mask_account_label(draft.account_label),
        strategy_label=strategy,
        instruction="Close current legs and open exact replacement legs",
        order_type=_order_type_label(draft.api_order_type),
        duration=draft.duration,
        execution_mode=f"{draft.execution_detail} • Review only",
        legs=legs,
        package_quantity=draft.order_quantity,
        price_title="Net roll price",
        net_price=draft.limit_price,
        cash_direction=direction,
        price_rail=rail,
        price_editable=False,
        price_editor_explanation="Return to the roll workspace to change the net limit and rebuild its analysis.",
        estimated_cash_effect=draft.estimated_cash_effect,
        estimated_cash_label="Estimated net credit" if draft.estimated_cash_effect >= 0 else "Estimated net debit",
        price_provenance=f"{LOCAL_CALCULATION} from current leg quotes",
        display_quote_at=display_quote_at,
        validation_quote_at=validation_quote_at,
        max_quote_age_seconds=max_quote_age_seconds,
        quote_state=freshness,
        metrics=(
            OptionOrderReviewMetric("Current → replacement legs", str(len(draft.close_legs)), str(len(draft.replacement_legs)), LOCAL_CALCULATION),
            OptionOrderReviewMetric("Days extended", "0", f"+{draft.analysis.days_extended}", LOCAL_CALCULATION),
            OptionOrderReviewMetric(
                "Realized P/L estimate",
                "Open",
                _money(draft.analysis.estimated_realized_pnl),
                LOCAL_CALCULATION if draft.analysis.estimated_realized_pnl is not None else "Unavailable from current position facts",
                after_tone=_tone(draft.analysis.estimated_realized_pnl),
            ),
            OptionOrderReviewMetric("Buying power", _money(before_metrics.buying_power), _money(after_metrics.buying_power), UNAVAILABLE_UNTIL_BROKER_REVIEW),
            OptionOrderReviewMetric("Delta", _signed(before_metrics.delta), _signed(after_metrics.delta), LOCAL_CALCULATION),
            OptionOrderReviewMetric("Theta / day", _money(before_metrics.theta_per_day), _money(after_metrics.theta_per_day), LOCAL_CALCULATION, before_tone=_tone(before_metrics.theta_per_day), after_tone=_tone(after_metrics.theta_per_day)),
        ),
        costs=(
            OptionOrderReviewCost("Estimated fees", _money_or_unavailable(draft.analysis.estimated_fees), LOCAL_CALCULATION if draft.analysis.estimated_fees is not None else "No configured fee schedule", estimated=draft.analysis.estimated_fees is not None),
            OptionOrderReviewCost("Estimated net credit" if draft.estimated_cash_effect >= 0 else "Estimated net cost", _money(abs(draft.estimated_cash_effect)), LOCAL_CALCULATION, tone="positive" if draft.estimated_cash_effect >= 0 else "negative", estimated=True),
            OptionOrderReviewCost("Settlement", "Unavailable", UNAVAILABLE_UNTIL_BROKER_REVIEW),
            OptionOrderReviewCost("Broker preview", "Not run", "Roll placement is not enabled"),
            OptionOrderReviewCost("Quote age", _age_label(validation_quote_at, current), CURRENT_SCHWAB_QUOTE),
        ),
        notices=_dedupe_notices(notices),
        acknowledgment_copy="I reviewed the current and replacement contracts, actions, quantities, price, and warnings.",
        safety_copy="This roll closes current legs and opens replacement legs; no order is sent from this review.",
        placement_capability=OrderReviewPlacementCapability.REVIEW_ONLY,
        placement_disabled_reason="Live roll placement is not enabled because atomic broker semantics are unverified.",
        primary_action_label="Finish roll review",
        broker_preview_status="Not available — roll placement is disabled",
        internal_valid=internal_valid,
    )


def exit_plan_review(
    draft: ExitPlanDraft,
    *,
    now: datetime | None = None,
    max_quote_age_seconds: float = DEFAULT_REVIEW_MAX_QUOTE_AGE_SECONDS,
) -> OptionOrderReview:
    current = _aware(now or datetime.now(timezone.utc))
    review_legs: list[OptionOrderReviewLeg] = []
    for branch in draft.branches:
        role = _exit_role(branch.branch_id, branch.label)
        if branch.closing_order is not None:
            for leg in branch.closing_order.legs:
                review_legs.append(
                    OptionOrderReviewLeg(
                        role=role,
                        action=_human_instruction(leg.instruction),
                        quantity=leg.quantity,
                        contract_label=_contract_label(leg.underlying_symbol, leg.expiration, leg.strike, leg.option_type),
                        symbol=leg.symbol,
                        bid=leg.bid,
                        ask=leg.ask,
                        mark=leg.mark,
                        before_quantity=leg.before_quantity,
                        after_quantity=leg.after_quantity,
                        quote_observed_at=leg.quote_observed_at,
                    )
                )
        else:
            quantity = _fractional_quantity(draft.protected_quantity, branch.quantity_fraction)
            for symbol in draft.position_symbols:
                review_legs.append(
                    OptionOrderReviewLeg(
                        role=role,
                        action="Closing action unavailable",
                        quantity=quantity,
                        contract_label="Exact position contract",
                        symbol=symbol,
                        bid=None,
                        ask=None,
                        mark=None,
                        before_quantity=None,
                        after_quantity=None,
                        quote_observed_at=None,
                    )
                )
    legs = tuple(review_legs)
    timestamps = tuple(leg.quote_observed_at for leg in legs if leg.quote_observed_at is not None)
    display_quote_at, validation_quote_at = _quote_bounds(timestamps)
    freshness = quote_state(validation_quote_at, now=current, max_age_seconds=max_quote_age_seconds)
    notices: list[OptionOrderReviewNotice] = []
    if timestamps:
        notices.extend(_quote_notices(freshness, validation_quote_at, current, max_quote_age_seconds))
    else:
        notices.append(_information("Quote data unavailable", "This unsupported plan shape does not contain executable closing-order quotes."))
    notices.extend(
        _warning("Exit-plan warning", warning)
        for warning in draft.warnings
        if not draft.capability_reason or warning != draft.capability_reason
    )
    if draft.conflicting_order_ids:
        notices.append(
            _blocking(
                "Conflicting closing order",
                "Resolve the overlapping working closing order before activating another exit.",
            )
        )
    if draft.capability_reason:
        notices.append(_blocking("Broker placement unavailable", draft.capability_reason))
    can_review = draft.executable and not draft.conflicting_order_ids
    capability = (
        OrderReviewPlacementCapability.REVIEW_ONLY
        if can_review
        else OrderReviewPlacementCapability.UNAVAILABLE
    )
    active_branches = sum(1 for branch in draft.branches if branch.enabled)
    strategy = "Exact option position" if len(draft.position_symbols) == 1 else "Custom option strategy"
    durations = {branch.duration for branch in draft.branches if branch.enabled}
    duration = next(iter(durations)) if len(durations) == 1 else "Mixed"
    internal_valid = bool(draft.position_symbols and draft.protected_quantity > 0 and draft.branches)
    if not internal_valid:
        notices.append(_blocking("Invalid exit-plan review", "Required position, quantity, or branch data is missing."))
    return OptionOrderReview(
        operation=OrderReviewOperation.EXIT_PLAN,
        title="Review exit plan",
        subtitle=(
            f"{draft.template_name} for {strategy} • {len(legs)} exact review leg"
            f"{'s' if len(legs) != 1 else ''} • Qty {draft.protected_quantity}"
        ),
        account_display_label=mask_account_label(draft.account_label),
        strategy_label=f"{draft.template_name} • {draft.coverage_label}",
        instruction="Create linked closing instructions" if active_branches > 1 else "Create one planned closing instruction",
        order_type=(draft.branches[0].order_type.replace("_", " ").title() if len(draft.branches) == 1 else f"{draft.relationship} linked exits"),
        duration=duration,
        execution_mode="Review only" if can_review else "Placement unavailable",
        legs=legs,
        package_quantity=draft.protected_quantity,
        price_title="Current position mark",
        net_price=draft.position_mark,
        cash_direction=OrderReviewCashDirection.REFERENCE,
        price_rail=None,
        price_editable=False,
        price_editor_explanation="Edit target, stop, and linkage terms in the exit-plan builder.",
        estimated_cash_effect=None,
        estimated_cash_label="Estimated proceeds or cost",
        price_provenance=f"{LOCAL_CALCULATION} from current position marks",
        display_quote_at=display_quote_at,
        validation_quote_at=validation_quote_at,
        max_quote_age_seconds=max_quote_age_seconds,
        quote_state=freshness,
        metrics=(
            OptionOrderReviewMetric("Protected quantity", "0", str(draft.protected_quantity), LOCAL_CALCULATION),
            OptionOrderReviewMetric("Active branches", "0", str(active_branches), LOCAL_CALCULATION),
            OptionOrderReviewMetric(
                "Trigger relationship",
                "None",
                draft.relationship if draft.relationship.isupper() else draft.relationship.replace("_", " ").title(),
                "Exit-plan configuration",
            ),
            OptionOrderReviewMetric("Resulting coverage", "Unprotected", draft.coverage_label, "Exit-plan configuration"),
        ),
        costs=(
            OptionOrderReviewCost("Estimated fees", "Unavailable", UNAVAILABLE_UNTIL_BROKER_REVIEW),
            OptionOrderReviewCost("Estimated proceeds or cost", "Unavailable", UNAVAILABLE_UNTIL_BROKER_REVIEW),
            OptionOrderReviewCost("Settlement", "Unavailable", UNAVAILABLE_UNTIL_BROKER_REVIEW),
            OptionOrderReviewCost("Broker preview", "Not run", "Exit-plan placement is disabled"),
            OptionOrderReviewCost("Quote age", _age_label(validation_quote_at, current), CURRENT_SCHWAB_QUOTE if validation_quote_at else "Unavailable"),
        ),
        notices=_dedupe_notices(notices),
        acknowledgment_copy="I reviewed the protected contracts, branch actions, quantities, triggers, and warnings.",
        safety_copy="This exit plan creates closing instructions; no order is sent from this review.",
        placement_capability=capability,
        placement_disabled_reason=(
            "Exit-plan live placement is not enabled in the universal review."
            if can_review
            else draft.capability_reason or "Resolve blocking exit-plan conditions before placement."
        ),
        primary_action_label="Finish exit-plan review" if can_review else "Placement unavailable",
        broker_preview_status="Not available — exit-plan placement is disabled",
        internal_valid=internal_valid,
    )


class OptionOrderReviewController:
    """UI-independent review state and exactly-once Close placement guard."""

    def __init__(
        self,
        *,
        review: OptionOrderReview,
        draft: ClosingOrderDraft | RollOrderDraft | ExitPlanDraft,
        snapshot_loader: Callable[[], PortfolioSnapshot] | None = None,
        session_factory: Callable[[], object] | None = None,
        previewer: Callable[[dict[str, object]], BrokerOrderPreview] | None = None,
        on_accepted: Callable[[], None] | None = None,
        on_unknown: Callable[[], None] | None = None,
        now_provider: Callable[[], datetime] | None = None,
        state_listener: Callable[[OptionOrderReviewController], None] | None = None,
    ) -> None:
        self.review = review
        self.draft = draft
        self.snapshot_loader = snapshot_loader
        self.session_factory = session_factory
        self.previewer = previewer
        self.on_accepted = on_accepted
        self.on_unknown = on_unknown
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self.state_listener = state_listener
        self.acknowledged = False
        self.state = OrderReviewPlacementState.READY
        self.preview_result: BrokerOrderPreview | None = None
        self._lock = threading.RLock()
        self._refreshing = False
        self._transmission_started = False

    @property
    def supports_background_refresh(self) -> bool:
        return self.review.operation == OrderReviewOperation.CLOSE and self.snapshot_loader is not None

    @property
    def can_place(self) -> bool:
        with self._lock:
            return self._can_place_unlocked()

    @property
    def can_finish_review(self) -> bool:
        with self._lock:
            return bool(
                self.review.placement_capability == OrderReviewPlacementCapability.REVIEW_ONLY
                and self.acknowledged
                and self.review.internal_valid
                and self.state == OrderReviewPlacementState.READY
                and not self._refreshing
            )

    @property
    def primary_action_enabled(self) -> bool:
        return self.can_place or self.can_finish_review

    @property
    def state_text(self) -> str:
        if self.state == OrderReviewPlacementState.REVALIDATING:
            return "Revalidating position…"
        if self.state == OrderReviewPlacementState.PREVIEWING:
            return "Previewing order…"
        if self.state == OrderReviewPlacementState.FALLBACK:
            return "Using local-estimate fallback…"
        if self.state == OrderReviewPlacementState.SUBMITTING:
            return "Submitting…"
        if self.state == OrderReviewPlacementState.ACCEPTED:
            return "Order accepted"
        if self.state == OrderReviewPlacementState.REJECTED:
            return "Review rejected"
        if self.state == OrderReviewPlacementState.UNKNOWN:
            return "Submission result unknown"
        if self._refreshing or self.review.quote_state == OrderReviewQuoteState.UPDATING:
            return "Refreshing quote…"
        if self.review.placement_capability == OrderReviewPlacementCapability.UNAVAILABLE:
            return "Placement unavailable"
        if self.review.placement_capability == OrderReviewPlacementCapability.REVIEW_ONLY:
            return "Confirmation required" if not self.acknowledged else "Review complete"
        if self.review.quote_state == OrderReviewQuoteState.STALE:
            return "Stale quote — refresh required"
        if self.review.quote_state == OrderReviewQuoteState.UNAVAILABLE:
            return "Quote unavailable"
        if self.review.has_blocking_notice or not self.review.internal_valid:
            return "Resolve blocking notices"
        return "Ready for final revalidation" if self.acknowledged else "Confirmation required"

    def acknowledge(self, acknowledged: bool) -> None:
        with self._lock:
            if self.state in {
                OrderReviewPlacementState.REVALIDATING,
                OrderReviewPlacementState.PREVIEWING,
                OrderReviewPlacementState.FALLBACK,
                OrderReviewPlacementState.SUBMITTING,
                OrderReviewPlacementState.ACCEPTED,
                OrderReviewPlacementState.UNKNOWN,
            }:
                return
            self.acknowledged = bool(acknowledged)
        self._notify()

    def set_limit_price(self, value: object) -> OptionOrderReview:
        with self._lock:
            if not isinstance(self.draft, ClosingOrderDraft) or not self.review.price_editable:
                raise ValueError(self.review.price_editor_explanation or "This reviewed price is read only.")
            if self.state != OrderReviewPlacementState.READY or self._refreshing:
                raise ValueError("Wait for the current review operation to finish before changing price.")
            self.draft = reprice_closing_order_draft(self.draft, value)
            self.review = closing_order_review(
                self.draft,
                now=self.now_provider(),
                max_quote_age_seconds=self.review.max_quote_age_seconds,
            )
            self.acknowledged = False
            self.preview_result = None
            result = self.review
        self._notify()
        return result

    def refresh_review(self) -> OptionOrderReview:
        if not self.supports_background_refresh or self.snapshot_loader is None:
            return self.review
        with self._lock:
            if self.state in {OrderReviewPlacementState.ACCEPTED, OrderReviewPlacementState.UNKNOWN}:
                return self.review
            self._refreshing = True
            self.acknowledged = False
            self.preview_result = None
            self.review = replace(self.review, quote_state=OrderReviewQuoteState.UPDATING)
        self._notify()
        try:
            snapshot = self.snapshot_loader()
            assert isinstance(self.draft, ClosingOrderDraft)
            refreshed = refresh_closing_order_draft(self.draft, snapshot)
            updated = closing_order_review(
                refreshed,
                now=self.now_provider(),
                max_quote_age_seconds=self.review.max_quote_age_seconds,
            )
        except Exception as exc:
            with self._lock:
                self._refreshing = False
                self.review = replace(
                    self.review,
                    quote_state=OrderReviewQuoteState.UNAVAILABLE,
                    notices=_dedupe_notices(
                        (
                            *self.review.notices,
                            _blocking(
                                "Refresh failed",
                                f"Current positions and quotes could not be revalidated: {_safe_error(exc)}",
                            ),
                        )
                    ),
                )
            self._notify()
            raise
        with self._lock:
            self.draft = refreshed
            self.review = updated
            self._refreshing = False
            self.acknowledged = False
            self.state = OrderReviewPlacementState.READY
        self._notify()
        return updated

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
                changed = True
                self.review = replace(self.review, quote_state=updated)
                if updated in {OrderReviewQuoteState.STALE, OrderReviewQuoteState.UNAVAILABLE}:
                    self.acknowledged = False
        if changed:
            self._notify()
        return updated

    def finish_review(self) -> bool:
        return self.can_finish_review

    def abandon_review(self) -> None:
        """Close/Back is intentionally side-effect free and never places an order."""

    def save_order(self) -> bool:
        """There is no saved executable draft workflow in the current application."""
        return False

    def place(self) -> OrderReviewPlacementOutcome:
        with self._lock:
            if self.review.placement_capability != OrderReviewPlacementCapability.SUPPORTED:
                return OrderReviewPlacementOutcome(
                    OrderReviewOutcomeStatus.UNSUPPORTED,
                    self.review.placement_disabled_reason or "Placement is unavailable for this review.",
                )
            if not self._can_place_unlocked():
                return OrderReviewPlacementOutcome(
                    OrderReviewOutcomeStatus.BLOCKED,
                    self.state_text,
                )
            if not isinstance(self.draft, ClosingOrderDraft):
                return OrderReviewPlacementOutcome(
                    OrderReviewOutcomeStatus.UNSUPPORTED,
                    "Only validated closing drafts can be submitted.",
                )
            reviewed_draft = self.draft
            self.state = OrderReviewPlacementState.REVALIDATING
        self._notify()

        if self.snapshot_loader is None or self.session_factory is None:
            return self._reject_before_submission(
                OrderReviewOutcomeStatus.BLOCKED,
                "Required Close placement dependencies are unavailable.",
                retryable=False,
            )
        try:
            snapshot = self.snapshot_loader()
            refreshed = refresh_closing_order_draft(reviewed_draft, snapshot)
            refreshed_review = closing_order_review(
                refreshed,
                now=self.now_provider(),
                max_quote_age_seconds=self.review.max_quote_age_seconds,
            )
        except ValueError as exc:
            with self._lock:
                self.acknowledged = False
                self.state = OrderReviewPlacementState.REJECTED
                self.review = replace(
                    self.review,
                    notices=_dedupe_notices((*self.review.notices, _blocking("Position revalidation failed", _safe_error(exc)))),
                )
            self._notify()
            return OrderReviewPlacementOutcome(OrderReviewOutcomeStatus.INVALIDATED, _safe_error(exc))
        except Exception as exc:
            return self._pre_submission_exception(exc)

        if refreshed_review.quote_state not in {OrderReviewQuoteState.LIVE, OrderReviewQuoteState.AGING}:
            with self._lock:
                self.draft = refreshed
                self.review = refreshed_review
                self.acknowledged = False
                self.state = OrderReviewPlacementState.READY
            self._notify()
            return OrderReviewPlacementOutcome(
                OrderReviewOutcomeStatus.INVALIDATED,
                "The current quote is stale or unavailable; refresh and review again.",
            )
        if _closing_market_fingerprint(refreshed) != _closing_market_fingerprint(reviewed_draft):
            with self._lock:
                self.draft = refreshed
                self.review = replace(
                    refreshed_review,
                    notices=_dedupe_notices(
                        (*refreshed_review.notices, _warning("Quote changed", "Bid, ask, or mark changed during final revalidation; confirm the refreshed review again."))
                    ),
                )
                self.acknowledged = False
                self.state = OrderReviewPlacementState.READY
            self._notify()
            return OrderReviewPlacementOutcome(
                OrderReviewOutcomeStatus.INVALIDATED,
                "Quote values changed during final revalidation; review and acknowledge again.",
            )

        payload = build_closing_order_payload(refreshed)
        if self.previewer is not None:
            self._set_state(OrderReviewPlacementState.PREVIEWING)
            try:
                preview = self.previewer(payload)
            except Exception as exc:
                return self._pre_submission_exception(exc)
            if not isinstance(preview, BrokerOrderPreview):
                return self._reject_before_submission(
                    OrderReviewOutcomeStatus.PREVIEW_REJECTED,
                    "Broker preview returned an unsupported result type.",
                    retryable=False,
                )
            self.preview_result = preview
            if not preview.accepted:
                reason = preview.reason or "The broker rejected this order preview."
                with self._lock:
                    self.acknowledged = False
                    self.state = OrderReviewPlacementState.REJECTED
                    self.review = replace(
                        self.review,
                        broker_preview_status=f"Rejected — {reason}",
                        notices=_dedupe_notices((*self.review.notices, _blocking("Broker preview rejected", reason))),
                    )
                self._notify()
                return OrderReviewPlacementOutcome(OrderReviewOutcomeStatus.PREVIEW_REJECTED, reason)
            with self._lock:
                self.review = _apply_preview_values(self.review, preview)
        else:
            self._set_state(OrderReviewPlacementState.FALLBACK)

        try:
            session = self.session_factory()
            submit = getattr(session, "submit_order", None)
            if not callable(submit):
                raise TypeError("Schwab session does not provide submit_order.")
        except Exception as exc:
            return self._pre_submission_exception(exc)

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
            self.state = OrderReviewPlacementState.ACCEPTED
        self._notify()
        refresh_error: Exception | None = None
        if self.on_accepted is not None:
            try:
                self.on_accepted()
            except Exception as exc:  # acceptance is still final even if the refresh fails
                refresh_error = exc
        message = "Schwab accepted the closing order."
        if refresh_error is not None:
            message += " The follow-up position/order refresh failed; refresh the workspace manually."
        return OrderReviewPlacementOutcome(
            OrderReviewOutcomeStatus.ACCEPTED,
            message,
            submission=submission,
        )

    def _can_place_unlocked(self) -> bool:
        return bool(
            self.review.placement_capability == OrderReviewPlacementCapability.SUPPORTED
            and self.acknowledged
            and self.review.internal_valid
            and not self.review.has_blocking_notice
            and self.review.quote_state in {OrderReviewQuoteState.LIVE, OrderReviewQuoteState.AGING}
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
            self.state = OrderReviewPlacementState.READY if retryable else OrderReviewPlacementState.REJECTED
            self.acknowledged = False
        self._notify()
        return OrderReviewPlacementOutcome(status, message, retryable=retryable)

    def _pre_submission_exception(self, exc: Exception) -> OrderReviewPlacementOutcome:
        if _is_authentication_error(exc):
            return self._reject_before_submission(
                OrderReviewOutcomeStatus.AUTHENTICATION_FAILED,
                f"Authentication failed before submission: {_safe_error(exc)}",
                retryable=True,
            )
        if _is_network_error(exc):
            return self._reject_before_submission(
                OrderReviewOutcomeStatus.NETWORK_FAILED,
                f"Network failure before submission: {_safe_error(exc)}",
                retryable=True,
            )
        return self._reject_before_submission(
            OrderReviewOutcomeStatus.REJECTED,
            _safe_error(exc),
            retryable=False,
        )

    def _submission_exception(self, exc: Exception) -> OrderReviewPlacementOutcome:
        status_code = _http_status(exc)
        if _is_authentication_error(exc) or (status_code is not None and status_code in {401, 403}):
            with self._lock:
                self._transmission_started = False
            return self._reject_before_submission(
                OrderReviewOutcomeStatus.AUTHENTICATION_FAILED,
                f"Broker authentication rejected the request: {_safe_error(exc)}",
                retryable=True,
            )
        if isinstance(exc, BrokerOrderRejected) or (status_code is not None and 400 <= status_code < 500):
            with self._lock:
                self.state = OrderReviewPlacementState.REJECTED
                self.acknowledged = False
            self._notify()
            return OrderReviewPlacementOutcome(OrderReviewOutcomeStatus.REJECTED, _safe_error(exc))
        with self._lock:
            self.state = OrderReviewPlacementState.UNKNOWN
            self.acknowledged = False
            self.review = replace(
                self.review,
                notices=_dedupe_notices(
                    (*self.review.notices, _blocking("Submission result unknown", "Do not resubmit blindly. Check Schwab Orders and refresh order state first."))
                ),
            )
        self._notify()
        refresh_error: Exception | None = None
        if self.on_unknown is not None:
            try:
                self.on_unknown()
            except Exception as callback_error:
                refresh_error = callback_error
        message = "Submission result unknown; check Schwab Orders before taking any further action."
        if refresh_error is not None:
            message += " The follow-up order-state refresh could not be started."
        return OrderReviewPlacementOutcome(
            OrderReviewOutcomeStatus.UNKNOWN,
            message,
        )

    def _notify(self) -> None:
        listener = self.state_listener
        if listener is not None:
            listener(self)


def _apply_preview_values(review: OptionOrderReview, preview: BrokerOrderPreview) -> OptionOrderReview:
    costs: list[OptionOrderReviewCost] = []
    for cost in review.costs:
        if cost.label == "Broker preview":
            costs.append(replace(cost, value="Accepted", provenance=BROKER_PREVIEW))
        elif cost.label == "Estimated fees":
            costs.append(
                replace(
                    cost,
                    value=_money(preview.estimated_fees) if preview.estimated_fees is not None else "Unavailable",
                    provenance=BROKER_PREVIEW if preview.estimated_fees is not None else "Broker preview did not return fees",
                    estimated=preview.estimated_fees is not None,
                )
            )
        elif cost.label == "Settlement":
            costs.append(
                replace(
                    cost,
                    value=preview.settlement or "Unavailable",
                    provenance=BROKER_PREVIEW if preview.settlement else "Broker preview did not return settlement timing",
                )
            )
        else:
            costs.append(cost)
    metrics = tuple(
        replace(
            metric,
            after=_money(preview.buying_power_after) if preview.buying_power_after is not None else "—",
            provenance=BROKER_PREVIEW if preview.buying_power_after is not None else "Broker preview did not return buying-power effects",
        )
        if metric.label == "Buying power"
        else metric
        for metric in review.metrics
    )
    return replace(
        review,
        costs=tuple(costs),
        metrics=metrics,
        broker_preview_status="Broker preview accepted",
    )


def _closing_semantic_fingerprint(draft: ClosingOrderDraft) -> tuple[object, ...]:
    return (
        draft.account_label,
        draft.api_order_type,
        draft.complex_order_strategy_type,
        draft.order_quantity,
        draft.duration,
        _cent(draft.limit_price),
        tuple(
            (
                leg.symbol,
                leg.instruction,
                leg.quantity,
                leg.ratio_quantity,
                round(leg.before_quantity, 8),
                round(leg.after_quantity, 8),
                round(leg.contract_multiplier, 8),
            )
            for leg in draft.legs
        ),
    )


def _closing_market_fingerprint(draft: ClosingOrderDraft) -> tuple[object, ...]:
    return tuple(
        (
            leg.symbol,
            _rounded_or_none(leg.bid),
            _rounded_or_none(leg.ask),
            _rounded_or_none(leg.mark),
        )
        for leg in draft.legs
    )


def _quote_bounds(timestamps: Sequence[datetime | None]) -> tuple[datetime | None, datetime | None]:
    available = tuple(_aware(value) for value in timestamps if value is not None)
    if not available:
        return None, None
    return max(available), min(available)


def _quote_notices(
    state: OrderReviewQuoteState,
    timestamp: datetime | None,
    now: datetime,
    max_age_seconds: float,
) -> tuple[OptionOrderReviewNotice, ...]:
    if state == OrderReviewQuoteState.STALE:
        return (
            _blocking(
                "Stale quote",
                f"The oldest reviewed quote is {_age_label(timestamp, now)}; refresh before placement (maximum {max_age_seconds:.0f} seconds).",
            ),
        )
    if state == OrderReviewQuoteState.UNAVAILABLE:
        return (_blocking("Quote unavailable", "A required quote timestamp is missing or invalid."),)
    if state == OrderReviewQuoteState.AGING:
        return (
            _warning(
                "Quote is aging",
                f"The oldest reviewed quote is {_age_label(timestamp, now)} and will require refresh at {max_age_seconds:.0f} seconds.",
            ),
        )
    return ()


def _information(title: str, detail: str) -> OptionOrderReviewNotice:
    return OptionOrderReviewNotice(OrderReviewNoticeSeverity.INFORMATION, title, detail)


def _warning(title: str, detail: str) -> OptionOrderReviewNotice:
    return OptionOrderReviewNotice(OrderReviewNoticeSeverity.WARNING, title, detail)


def _blocking(title: str, detail: str) -> OptionOrderReviewNotice:
    return OptionOrderReviewNotice(OrderReviewNoticeSeverity.BLOCKING, title, detail, blocking=True)


def _dedupe_notices(notices: Sequence[OptionOrderReviewNotice]) -> tuple[OptionOrderReviewNotice, ...]:
    result: list[OptionOrderReviewNotice] = []
    seen: set[tuple[object, ...]] = set()
    for notice in notices:
        key = (notice.severity, notice.title, notice.detail, notice.blocking)
        if key not in seen:
            seen.add(key)
            result.append(notice)
    return tuple(result)


def _contract_label(
    underlying: str,
    expiration: str,
    strike: float,
    option_type: str,
) -> str:
    try:
        expiration_label = date.fromisoformat(expiration[:10]).strftime("%d %b %y").upper()
    except (TypeError, ValueError):
        expiration_label = expiration or "DATE UNAVAILABLE"
    return f"{underlying or 'UNKNOWN'} {expiration_label} {strike:g} {option_type.title()}"


def _order_type_label(value: str) -> str:
    return {
        "LIMIT": "Limit",
        "NET_CREDIT": "Net credit limit",
        "NET_DEBIT": "Net debit limit",
        "MARKET": "Market",
    }.get(value.upper(), value.replace("_", " ").title())


def _human_instruction(value: str) -> str:
    return {
        "BUY_TO_CLOSE": "Buy to close",
        "SELL_TO_CLOSE": "Sell to close",
        "BUY_TO_OPEN": "Buy to open",
        "SELL_TO_OPEN": "Sell to open",
    }.get(value.upper(), value.replace("_", " ").title())


def _exit_role(branch_id: str, label: str) -> str:
    if branch_id.startswith("target"):
        return "Target"
    if branch_id in {"stop", "trailing_stop"}:
        return "Stop"
    return label or "Exit"


def _fractional_quantity(quantity: int, fraction: float) -> int | None:
    value = quantity * fraction
    return int(round(value)) if value > 0 and math.isclose(value, round(value), abs_tol=1e-8) else None


def _money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"


def _money_or_unavailable(value: float | None) -> str:
    return "Unavailable" if value is None else _money(value)


def _signed(value: float | None) -> str:
    return "—" if value is None else f"{value:+,.2f}"


def _quantity(value: int | float | None) -> str:
    return "—" if value is None else f"{value:g}"


def _tone(value: float | None) -> str:
    if value is None or math.isclose(value, 0.0, abs_tol=1e-12):
        return "neutral"
    return "positive" if value > 0 else "negative"


def _age_label(timestamp: datetime | None, now: datetime) -> str:
    age = quote_age_seconds(timestamp, now=now)
    if age is None:
        return "Unavailable"
    seconds = int(age)
    if seconds < 60:
        return f"{seconds} sec"
    minutes, remainder = divmod(seconds, 60)
    return f"{minutes}m {remainder:02d}s"


def _safe_error(exc: Exception) -> str:
    text = str(exc).strip()
    return text or type(exc).__name__


def _is_authentication_error(exc: Exception) -> bool:
    return isinstance(exc, BrokerAuthenticationFailure) or type(exc).__name__.lower() in {
        "authenticationerror",
        "permissionerror",
    } or _http_status(exc) in {401, 403}


def _is_network_error(exc: Exception) -> bool:
    return isinstance(exc, (BrokerNetworkFailure, requests.Timeout, requests.ConnectionError))


def _http_status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _finite_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _cent(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _rounded_or_none(value: float | None) -> float | None:
    return None if value is None else round(float(value), 8)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


__all__ = [
    "BROKER_PREVIEW",
    "BrokerAuthenticationFailure",
    "BrokerNetworkFailure",
    "BrokerOrderPreview",
    "BrokerOrderRejected",
    "BrokerSubmissionResultUnknown",
    "CURRENT_SCHWAB_QUOTE",
    "DEFAULT_REVIEW_MAX_QUOTE_AGE_SECONDS",
    "LOCAL_CALCULATION",
    "OptionOrderReviewController",
    "PREVIEW_FALLBACK_STATUS",
    "UNAVAILABLE_UNTIL_BROKER_REVIEW",
    "closing_order_review",
    "closing_price_rail",
    "exit_plan_review",
    "mask_account_label",
    "quote_age_seconds",
    "quote_state",
    "refresh_closing_order_draft",
    "reprice_closing_order_draft",
    "roll_order_review",
]
