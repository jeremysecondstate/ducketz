from __future__ import annotations

import json
import math
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from functools import reduce
from math import gcd
from pathlib import Path

from app.models.option_management import (
    OptionChainContract,
    OptionPositionBook,
    OptionPositionLeg,
    RollAnalysis,
    RollChainSnapshot,
    RollMetricSnapshot,
    RollOrderComponent,
    RollOrderDraft,
    RollOrderLeg,
    RollPayoffCurve,
    RollPriceRail,
    SavedRollTemplate,
)
from app.models.portfolio import PortfolioSnapshot
from app.services.schwab_option_management import option_position_book
from app.services.schwab_strategy_orders import DAY_ONLY, GOOD_UNTIL_CANCELED


ROLL_SCOPE_ENTIRE = "entire"
ROLL_SCOPE_SELECTED = "selected"
ROLL_PRICE_MID = "MID"
ROLL_PRICE_NATURAL = "NATURAL"
ROLL_PRICE_MANUAL = "MANUAL"
ROLL_EXECUTION_ATOMIC = "ATOMIC"
ROLL_EXECUTION_NON_ATOMIC = "NON_ATOMIC"
ROLL_EXECUTION_UNSUPPORTED = "UNSUPPORTED"
DEFAULT_MAX_QUOTE_AGE_SECONDS = 120.0
ROLL_TEMPLATE_SCHEMA_VERSION = 1
MAX_SCHWAB_COMPLEX_LEGS = 4
LOCAL_ROLL_PRICE_SOURCE = "Current Schwab leg quotes (local net estimate)"


@dataclass(frozen=True)
class _AnalyticsLeg:
    strike: float
    option_type: str
    expiration: str
    signed_quantity: int
    multiplier: float
    mark: float
    delta: float | None
    theta: float | None
    pnl_basis: float | None


def parse_roll_chain(
    payload: object,
    *,
    expected_underlying: str,
    observed_at: datetime | None = None,
) -> RollChainSnapshot:
    """Normalize a Schwab chain without ever constructing an OCC symbol."""

    if not isinstance(payload, Mapping):
        raise ValueError("The option-chain response is unavailable or malformed.")
    expected = str(expected_underlying or "").strip().upper()
    if not expected:
        raise ValueError("An underlying symbol is required to load a roll chain.")
    read_at = _aware_datetime(observed_at or datetime.now(timezone.utc))
    underlying_row = payload.get("underlying")
    underlying = underlying_row if isinstance(underlying_row, Mapping) else {}
    returned_symbol = str(
        underlying.get("symbol") or payload.get("symbol") or expected
    ).strip().upper()
    if returned_symbol and returned_symbol != expected:
        raise ValueError(
            f"Option chain {returned_symbol} does not match the position underlying {expected}."
        )
    underlying_price = _first_number(
        payload,
        ("underlyingPrice",),
    )
    if underlying_price is None:
        underlying_price = _first_number(underlying, ("mark", "last", "close", "bid"))
    root_quote_at = _timestamp_from_values(
        underlying,
        ("quoteTimeInLong", "quoteTime", "tradeTimeInLong", "tradeTime"),
    ) or _timestamp_from_values(
        payload,
        ("quoteTimeInLong", "quoteTime", "tradeTimeInLong", "tradeTime"),
    )

    contracts: list[OptionChainContract] = []
    unavailable: list[str] = []
    seen_symbols: set[str] = set()
    for option_type, map_name in (("CALL", "callExpDateMap"), ("PUT", "putExpDateMap")):
        expiration_map = payload.get(map_name)
        if expiration_map is None:
            continue
        if not isinstance(expiration_map, Mapping):
            unavailable.append(f"{option_type.title()} expiration map is malformed.")
            continue
        for expiration_key, strike_map in expiration_map.items():
            expiration = str(expiration_key or "").split(":", 1)[0][:10]
            if _parse_date(expiration) is None or not isinstance(strike_map, Mapping):
                unavailable.append(f"An {option_type.lower()} expiration row is incomplete.")
                continue
            for strike_key, rows in strike_map.items():
                if not _is_sequence(rows):
                    continue
                for row in rows:
                    if not isinstance(row, Mapping):
                        continue
                    symbol = str(row.get("symbol") or "").strip().upper()
                    strike = _first_number(row, ("strikePrice", "strike"))
                    if strike is None:
                        strike = _finite_number(strike_key)
                    row_expiration = _contract_expiration(row) or expiration
                    if not symbol or strike is None or _parse_date(row_expiration) is None:
                        unavailable.append(
                            f"A {row_expiration or expiration} {option_type.lower()} contract lacks exact identity."
                        )
                        continue
                    if symbol in seen_symbols:
                        raise ValueError(
                            f"Option chain returned duplicate exact contract {symbol}; replacement mapping is ambiguous."
                        )
                    seen_symbols.add(symbol)
                    bid = _first_number(row, ("bid", "bidPrice"))
                    ask = _first_number(row, ("ask", "askPrice"))
                    mark = _first_number(row, ("mark", "markPrice"))
                    if mark is None and bid is not None and ask is not None:
                        mark = (bid + ask) / 2.0
                    contracts.append(
                        OptionChainContract(
                            symbol=symbol,
                            underlying_symbol=expected,
                            option_type=option_type,
                            expiration=row_expiration,
                            strike=float(strike),
                            bid=bid,
                            ask=ask,
                            mark=mark,
                            delta=_finite_number(row.get("delta")),
                            theta=_finite_number(row.get("theta")),
                            contract_multiplier=_first_number(
                                row,
                                ("multiplier", "contractMultiplier"),
                            ),
                            quote_observed_at=_timestamp_from_values(
                                row,
                                (
                                    "quoteTimeInLong",
                                    "quoteTime",
                                    "tradeTimeInLong",
                                    "tradeTime",
                                ),
                            )
                            or root_quote_at,
                        )
                    )
    if not contracts:
        unavailable.append("The chain did not contain any exact option contracts.")
    contracts.sort(
        key=lambda item: (
            item.expiration,
            item.option_type,
            item.strike,
            item.symbol,
        )
    )
    return RollChainSnapshot(
        underlying_symbol=expected,
        underlying_price=underlying_price,
        observed_at=read_at,
        contracts=tuple(contracts),
        unavailable_reasons=tuple(dict.fromkeys(unavailable)),
    )


def roll_action_disabled_reason(
    book: OptionPositionBook,
    position_symbols: Iterable[str],
    *,
    now: datetime | None = None,
) -> str | None:
    try:
        legs = _position_legs(book, position_symbols, label="roll position")
        _validate_position_legs(book, legs, now=now)
    except ValueError as exc:
        return str(exc)
    return None


def eligible_roll_expirations(
    close_legs: Sequence[OptionPositionLeg],
    chain: RollChainSnapshot,
    *,
    now: datetime | None = None,
) -> tuple[str, ...]:
    if not close_legs:
        return ()
    current_date = _aware_datetime(now or chain.observed_at).date()
    current_expirations = [_required_date(leg.expiration, f"{leg.symbol} expiration") for leg in close_legs]
    latest_current = max(current_expirations)
    required_types = {leg.option_type.upper() for leg in close_legs}
    result: list[str] = []
    for expiration in sorted({contract.expiration for contract in chain.contracts}):
        parsed = _parse_date(expiration)
        if parsed is None or parsed <= latest_current or parsed <= current_date:
            continue
        available_types = {
            contract.option_type.upper()
            for contract in chain.contracts
            if contract.expiration == expiration
        }
        if required_types.issubset(available_types):
            result.append(expiration)
    return tuple(result)


def suggest_replacement_contracts(
    close_legs: Sequence[OptionPositionLeg],
    chain: RollChainSnapshot,
    *,
    expiration: str,
    keep_strike_widths: bool,
) -> tuple[OptionChainContract, ...]:
    if not close_legs:
        raise ValueError("Select at least one exact position leg to roll.")
    target_date = _required_date(expiration, "Replacement expiration")
    latest_current = max(
        _required_date(leg.expiration, f"{leg.symbol} expiration") for leg in close_legs
    )
    if target_date <= latest_current:
        raise ValueError("Replacement expiration must be later than every closing leg.")
    candidates = tuple(
        contract for contract in chain.contracts if contract.expiration == expiration
    )
    if not candidates:
        raise ValueError(f"No exact chain contracts are available for {expiration}.")
    by_key: dict[tuple[str, float], list[OptionChainContract]] = {}
    for contract in candidates:
        by_key.setdefault(
            (contract.option_type.upper(), _strike_key(contract.strike)), []
        ).append(contract)

    if keep_strike_widths:
        anchor = close_legs[0]
        offsets = sorted(
            {
                _strike_key(contract.strike - anchor.strike)
                for contract in candidates
                if contract.option_type.upper() == anchor.option_type.upper()
            },
            key=lambda value: (abs(value), value),
        )
        for offset in offsets:
            proposed: list[OptionChainContract] = []
            for leg in close_legs:
                matches = by_key.get(
                    (
                        leg.option_type.upper(),
                        _strike_key(leg.strike + offset),
                    ),
                    [],
                )
                if len(matches) != 1:
                    break
                proposed.append(matches[0])
            if len(proposed) == len(close_legs) and len({item.symbol for item in proposed}) == len(proposed):
                return tuple(proposed)
        raise ValueError(
            "The selected expiration has no exact contract set that preserves all strike widths."
        )

    proposed = []
    for leg in close_legs:
        matching_type = [
            contract
            for contract in candidates
            if contract.option_type.upper() == leg.option_type.upper()
        ]
        if not matching_type:
            raise ValueError(
                f"No exact {leg.option_type.lower()} contract is available for {expiration}."
            )
        distances = sorted(
            ((abs(contract.strike - leg.strike), contract) for contract in matching_type),
            key=lambda item: (item[0], item[1].strike, item[1].symbol),
        )
        best_distance = distances[0][0]
        tied = [contract for distance, contract in distances if math.isclose(distance, best_distance, abs_tol=1e-9)]
        if len(tied) != 1:
            raise ValueError(
                f"Nearest replacement strike for {leg.symbol} is ambiguous; choose another expiration or keep widths."
            )
        proposed.append(tied[0])
    if len({item.symbol for item in proposed}) != len(proposed):
        raise ValueError("Replacement mapping collapses multiple position legs onto one contract.")
    return tuple(proposed)


def build_roll_order_draft(
    book: OptionPositionBook,
    position_symbols: Iterable[str],
    close_symbols: Iterable[str],
    replacement_contracts: Sequence[OptionChainContract],
    *,
    scope_mode: str,
    keep_strike_widths: bool,
    duration: str = DAY_ONLY,
    limit_price: object | None = None,
    price_policy: str = ROLL_PRICE_MID,
    atomic_order_supported: bool = False,
    underlying_price: float | None = None,
    fee_per_contract: float | None = None,
    now: datetime | None = None,
    max_quote_age_seconds: float = DEFAULT_MAX_QUOTE_AGE_SECONDS,
) -> RollOrderDraft:
    current_time = _aware_datetime(now or datetime.now(timezone.utc))
    if scope_mode not in {ROLL_SCOPE_ENTIRE, ROLL_SCOPE_SELECTED}:
        raise ValueError(f"Unknown roll scope: {scope_mode or 'missing'}")
    if duration not in {DAY_ONLY, GOOD_UNTIL_CANCELED}:
        raise ValueError(f"Unsupported roll duration: {duration or 'missing'}")
    if price_policy not in {ROLL_PRICE_MID, ROLL_PRICE_NATURAL, ROLL_PRICE_MANUAL}:
        raise ValueError(f"Unsupported roll price policy: {price_policy or 'missing'}")
    position = _position_legs(book, position_symbols, label="roll position")
    _validate_position_legs(book, position, now=current_time)
    close = _position_legs(book, close_symbols, label="closing scope")
    position_set = {leg.symbol for leg in position}
    if any(leg.symbol not in position_set for leg in close):
        raise ValueError("Every closing leg must belong to the selected position.")
    if scope_mode == ROLL_SCOPE_ENTIRE and {leg.symbol for leg in close} != position_set:
        raise ValueError("Entire-strategy scope must include every selected position leg.")
    if scope_mode == ROLL_SCOPE_SELECTED and len(position) < 2:
        raise ValueError("Selected-leg scope requires a multi-leg position.")
    _validate_position_legs(book, close, now=current_time)
    if len(replacement_contracts) != len(close):
        raise ValueError("Every closing leg requires one exact replacement contract.")

    quantities = tuple(_whole_quantity(abs(leg.net_quantity), f"{leg.symbol} quantity") for leg in close)
    order_quantity = reduce(gcd, quantities)
    close_order_legs: list[RollOrderLeg] = []
    replacement_order_legs: list[RollOrderLeg] = []
    replacement_expirations: set[str] = set()
    multipliers: set[float] = set()
    for position_leg, replacement, quantity in zip(close, replacement_contracts, quantities, strict=True):
        _validate_current_quote(
            position_leg,
            now=current_time,
            max_quote_age_seconds=max_quote_age_seconds,
        )
        _validate_replacement_contract(
            replacement,
            position_leg=position_leg,
            now=current_time,
            max_quote_age_seconds=max_quote_age_seconds,
        )
        replacement_expirations.add(replacement.expiration)
        current_expiration = _required_date(position_leg.expiration, f"{position_leg.symbol} expiration")
        replacement_expiration = _required_date(
            replacement.expiration,
            f"{replacement.symbol} expiration",
        )
        if replacement_expiration <= current_expiration:
            raise ValueError(
                f"Replacement {replacement.symbol} must expire after {position_leg.symbol}."
            )
        current_multiplier = float(position_leg.contract_multiplier)
        replacement_multiplier = float(replacement.contract_multiplier or 0.0)
        if not math.isclose(current_multiplier, replacement_multiplier, rel_tol=0.0, abs_tol=1e-8):
            raise ValueError(
                f"Replacement {replacement.symbol} multiplier does not match {position_leg.symbol}."
            )
        multipliers.update((round(current_multiplier, 8), round(replacement_multiplier, 8)))
        ratio = quantity // order_quantity
        signed_quantity = quantity if position_leg.net_quantity > 0 else -quantity
        close_order_legs.append(
            RollOrderLeg(
                role="CLOSE",
                source_position_symbol=position_leg.symbol,
                symbol=position_leg.symbol,
                underlying_symbol=position_leg.underlying_symbol,
                option_type=position_leg.option_type.upper(),
                expiration=position_leg.expiration,
                strike=position_leg.strike,
                instruction=position_leg.close_instruction,
                signed_quantity=signed_quantity,
                quantity=quantity,
                ratio_quantity=ratio,
                before_quantity=position_leg.net_quantity,
                after_quantity=0.0,
                bid=float(position_leg.bid),
                ask=float(position_leg.ask),
                mark=float(position_leg.mark),
                delta=position_leg.delta,
                theta=position_leg.theta,
                contract_multiplier=current_multiplier,
                quote_observed_at=_aware_datetime(position_leg.quote_observed_at),
            )
        )
        replacement_order_legs.append(
            RollOrderLeg(
                role="OPEN",
                source_position_symbol=position_leg.symbol,
                symbol=replacement.symbol,
                underlying_symbol=replacement.underlying_symbol,
                option_type=replacement.option_type.upper(),
                expiration=replacement.expiration,
                strike=replacement.strike,
                instruction="BUY_TO_OPEN" if signed_quantity > 0 else "SELL_TO_OPEN",
                signed_quantity=signed_quantity,
                quantity=quantity,
                ratio_quantity=ratio,
                before_quantity=0.0,
                after_quantity=float(signed_quantity),
                bid=float(replacement.bid),
                ask=float(replacement.ask),
                mark=float(replacement.mark),
                delta=replacement.delta,
                theta=replacement.theta,
                contract_multiplier=replacement_multiplier,
                quote_observed_at=_aware_datetime(replacement.quote_observed_at),
            )
        )
    if len(replacement_expirations) != 1:
        raise ValueError("All replacement legs must use one exact expiration.")
    if len({leg.symbol for leg in replacement_order_legs}) != len(replacement_order_legs):
        raise ValueError("Replacement contracts must be unique exact OCC symbols.")
    if len(multipliers) != 1:
        raise ValueError("All roll legs must have one contract multiplier.")
    multiplier = next(iter(multipliers))

    all_order_legs = tuple(close_order_legs + replacement_order_legs)
    api_order_type, bid, midpoint, ask = _net_order_terms(all_order_legs)
    if limit_price not in (None, ""):
        selected_price = _positive_price(limit_price, "Roll limit price")
        resolved_policy = ROLL_PRICE_MANUAL
    elif price_policy == ROLL_PRICE_NATURAL:
        selected_price = bid
        resolved_policy = price_policy
    else:
        selected_price = midpoint
        resolved_policy = ROLL_PRICE_MID
    selected_price = _cent_price(selected_price)
    direction = 1.0 if api_order_type == "NET_CREDIT" else -1.0
    estimated_cash = round(direction * selected_price * multiplier * order_quantity, 2)
    rail = RollPriceRail(
        bid=bid,
        midpoint=midpoint,
        ask=ask,
        selected=selected_price,
    )

    blockers: list[str] = []
    warnings: list[str] = [
        "Net price, payoff, Greeks, and realized P/L are local estimates from current quotes; no broker preview is wired.",
        "Exact current quantities and replacement OCC symbols must be refreshed again before any placement flow.",
    ]
    if selected_price < bid or selected_price > ask:
        warnings.append("Selected net price is outside the current executable bid/ask estimate.")
    close_component = _component("Close current legs", tuple(close_order_legs))
    open_component = _component("Open replacement legs", tuple(replacement_order_legs))
    if atomic_order_supported and len(all_order_legs) <= MAX_SCHWAB_COMPLEX_LEGS:
        execution_mode = ROLL_EXECUTION_ATOMIC
        execution_detail = "One atomic custom net order"
        components = (
            RollOrderComponent(
                label="Atomic roll",
                legs=all_order_legs,
                api_order_type=api_order_type,
                complex_order_strategy_type="CUSTOM",
                order_quantity=order_quantity,
                limit_price=selected_price,
                estimated_cash_effect=estimated_cash,
            ),
        )
    elif len(close_order_legs) <= MAX_SCHWAB_COMPLEX_LEGS and len(replacement_order_legs) <= MAX_SCHWAB_COMPLEX_LEGS:
        execution_mode = ROLL_EXECUTION_NON_ATOMIC
        execution_detail = "Two separate orders; close/open exposure risk"
        components = (close_component, open_component)
        warnings.append(
            "Atomic mixed close/open roll support is not verified for this adapter. Review shows two components; fills can leave temporary directional or assignment exposure."
        )
    else:
        execution_mode = ROLL_EXECUTION_UNSUPPORTED
        execution_detail = "Order shape exceeds supported component leg limits"
        components = ()
        blockers.append(
            f"This roll requires more than {MAX_SCHWAB_COMPLEX_LEGS} legs in a broker component."
        )
    if atomic_order_supported and len(all_order_legs) > MAX_SCHWAB_COMPLEX_LEGS:
        warnings.append(
            "The complete roll exceeds the atomic custom-order leg limit and was split into explicit components."
        )

    replacement_expiration = next(iter(replacement_expirations))
    analysis = _roll_analysis(
        book,
        position,
        close,
        tuple(replacement_order_legs),
        replacement_expiration=replacement_expiration,
        underlying_price=underlying_price,
        estimated_roll_cash=estimated_cash,
        fee_per_contract=fee_per_contract,
    )
    if not analysis.before_curve.available or not analysis.after_curve.available:
        warnings.append(
            "Expiration payoff is unavailable because all required basis, expiration, or underlying facts were not present."
        )
    if analysis.after_metrics.buying_power is None:
        warnings.append("After-roll buying power is unavailable until a broker preview is supported.")
    if analysis.estimated_fees is None:
        warnings.append("Estimated contract fees are unavailable because no configured fee schedule was supplied.")

    oldest_quote = min(leg.quote_observed_at for leg in all_order_legs)
    scope_label = (
        "Entire strategy"
        if scope_mode == ROLL_SCOPE_ENTIRE and len(position) > 1
        else "Entire position"
        if scope_mode == ROLL_SCOPE_ENTIRE
        else f"{len(close)} selected leg{'s' if len(close) != 1 else ''}"
    )
    return RollOrderDraft(
        account_label=book.account_label,
        underlying_symbol=position[0].underlying_symbol,
        reviewed_position_at=book.observed_at,
        oldest_quote_at=oldest_quote,
        position_symbols=tuple(leg.symbol for leg in position),
        reviewed_position_quantities=tuple(
            (leg.symbol, leg.net_quantity) for leg in position
        ),
        close_symbols=tuple(leg.symbol for leg in close),
        scope_mode=scope_mode,
        scope_label=scope_label,
        replacement_expiration=replacement_expiration,
        keep_strike_widths=bool(keep_strike_widths),
        close_legs=tuple(close_order_legs),
        replacement_legs=tuple(replacement_order_legs),
        api_order_type=api_order_type,
        complex_order_strategy_type="CUSTOM",
        order_quantity=order_quantity,
        limit_price=selected_price,
        duration=duration,
        price_policy=resolved_policy,
        price_rail=rail,
        estimated_cash_effect=estimated_cash,
        execution_mode=execution_mode,
        execution_detail=execution_detail,
        atomic_order_supported=atomic_order_supported,
        components=components,
        analysis=analysis,
        price_source=LOCAL_ROLL_PRICE_SOURCE,
        warnings=tuple(dict.fromkeys(warnings)),
        review_blockers=tuple(blockers),
    )


def validate_roll_position_drift(
    draft: RollOrderDraft,
    latest: OptionPositionBook | PortfolioSnapshot,
) -> OptionPositionBook:
    book = option_position_book(latest) if isinstance(latest, PortfolioSnapshot) else latest
    if book.account_label != draft.account_label:
        raise ValueError(
            f"Reviewed account {draft.account_label} changed to {book.account_label}; review the roll again."
        )
    if book.status != "CURRENT":
        raise ValueError("Current option positions are unavailable or stale; the roll review was stopped.")
    by_symbol = {leg.symbol: leg for leg in book.legs}
    for symbol, reviewed_quantity in draft.reviewed_position_quantities:
        current = by_symbol.get(symbol)
        if current is None:
            raise ValueError(
                f"Position drift: {symbol} is no longer held. Review the roll again."
            )
        if current.close_disabled_reason:
            raise ValueError(f"Position drift: {current.close_disabled_reason}")
        if not math.isclose(
            current.net_quantity,
            reviewed_quantity,
            rel_tol=0.0,
            abs_tol=1e-8,
        ):
            raise ValueError(
                f"Position drift: {symbol} changed from {reviewed_quantity:g} "
                f"to {current.net_quantity:g}. Review the roll again."
            )
    return book


def refresh_roll_order_draft(
    draft: RollOrderDraft,
    *,
    latest: OptionPositionBook | PortfolioSnapshot,
    chain: RollChainSnapshot,
    now: datetime | None = None,
    max_quote_age_seconds: float = DEFAULT_MAX_QUOTE_AGE_SECONDS,
) -> RollOrderDraft:
    book = validate_roll_position_drift(draft, latest)
    by_symbol = {contract.symbol: contract for contract in chain.contracts}
    missing = [leg.symbol for leg in draft.replacement_legs if leg.symbol not in by_symbol]
    if missing:
        raise ValueError(
            "Replacement contract disappeared from the refreshed chain: " + ", ".join(missing)
        )
    fee_per_contract: float | None = None
    total_contracts = sum(leg.quantity for leg in draft.all_legs)
    if draft.analysis.estimated_fees is not None and total_contracts:
        fee_per_contract = draft.analysis.estimated_fees / total_contracts
    return build_roll_order_draft(
        book,
        draft.position_symbols,
        draft.close_symbols,
        tuple(by_symbol[leg.symbol] for leg in draft.replacement_legs),
        scope_mode=draft.scope_mode,
        keep_strike_widths=draft.keep_strike_widths,
        duration=draft.duration,
        limit_price=(draft.limit_price if draft.price_policy == ROLL_PRICE_MANUAL else None),
        price_policy=draft.price_policy,
        atomic_order_supported=draft.atomic_order_supported,
        underlying_price=chain.underlying_price,
        fee_per_contract=fee_per_contract,
        now=now,
        max_quote_age_seconds=max_quote_age_seconds,
    )


def build_roll_order_payloads(draft: RollOrderDraft) -> tuple[dict[str, object], ...]:
    """Build exact payloads for review/integration; this function never submits them."""

    if not draft.review_eligible:
        raise ValueError("Roll draft is not review eligible: " + "; ".join(draft.review_blockers))
    if draft.execution_mode == ROLL_EXECUTION_UNSUPPORTED or not draft.components:
        raise ValueError("This roll has no supported broker order components.")
    return tuple(_component_payload(component, duration=draft.duration) for component in draft.components)


def default_roll_template_path() -> Path:
    configured = os.getenv("DUCKETS_OPTION_ROLL_TEMPLATES_PATH", "").strip()
    return Path(configured) if configured else Path("data") / "option_roll_templates.json"


def load_roll_templates(path: Path | None = None) -> tuple[SavedRollTemplate, ...]:
    template_path = path or default_roll_template_path()
    if not template_path.exists():
        return ()
    try:
        payload = json.loads(template_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Roll template file is unreadable: {exc}") from exc
    if not isinstance(payload, Mapping) or payload.get("schema_version") != ROLL_TEMPLATE_SCHEMA_VERSION:
        version = payload.get("schema_version") if isinstance(payload, Mapping) else None
        raise ValueError(f"Unsupported roll template schema version: {version!r}")
    rows = payload.get("templates")
    if not isinstance(rows, list):
        raise ValueError("Roll template file has no template list.")
    return tuple(_roll_template_from_row(row) for row in rows)


def save_roll_template(
    template: SavedRollTemplate,
    path: Path | None = None,
) -> Path:
    template_path = path or default_roll_template_path()
    validated = _roll_template_from_row(asdict(template))
    existing = [
        item
        for item in load_roll_templates(template_path)
        if item.name.casefold() != validated.name.casefold()
    ]
    existing.append(validated)
    payload = {
        "schema_version": ROLL_TEMPLATE_SCHEMA_VERSION,
        "templates": [asdict(item) for item in existing],
    }
    template_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = template_path.with_name(template_path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, template_path)
    return template_path


def _validate_position_legs(
    book: OptionPositionBook,
    legs: Sequence[OptionPositionLeg],
    *,
    now: datetime | None,
) -> None:
    if book.status != "CURRENT":
        raise ValueError("Current Schwab option positions are unavailable or stale.")
    if not book.account_label:
        raise ValueError("The selected position has no account identity.")
    accounts = {leg.account_label for leg in legs}
    if accounts != {book.account_label}:
        raise ValueError("All roll legs must use the same current account.")
    underlyings = {leg.underlying_symbol for leg in legs if leg.underlying_symbol}
    if len(underlyings) != 1:
        raise ValueError("All roll legs must use one underlying symbol.")
    current_date = _aware_datetime(now or book.observed_at or datetime.now(timezone.utc)).date()
    for leg in legs:
        if leg.close_disabled_reason:
            raise ValueError(leg.close_disabled_reason)
        expiration = _required_date(leg.expiration, f"{leg.symbol} expiration")
        if expiration <= current_date:
            raise ValueError(f"{leg.symbol} is expired or expires today and cannot start a roll.")
        _whole_quantity(abs(leg.net_quantity), f"{leg.symbol} quantity")


def _position_legs(
    book: OptionPositionBook,
    symbols: Iterable[str],
    *,
    label: str,
) -> tuple[OptionPositionLeg, ...]:
    exact = tuple(
        dict.fromkeys(
            str(symbol).strip().upper()
            for symbol in symbols
            if str(symbol).strip()
        )
    )
    if not exact:
        raise ValueError(f"Select at least one exact option leg for the {label}.")
    by_symbol = {leg.symbol: leg for leg in book.legs}
    missing = [symbol for symbol in exact if symbol not in by_symbol]
    if missing:
        raise ValueError("Selected option position is no longer available: " + ", ".join(missing))
    return tuple(by_symbol[symbol] for symbol in exact)


def _validate_current_quote(
    leg: OptionPositionLeg,
    *,
    now: datetime,
    max_quote_age_seconds: float,
) -> None:
    _validate_market(
        leg.symbol,
        leg.bid,
        leg.ask,
        leg.mark,
        leg.contract_multiplier,
        leg.quote_observed_at,
        now=now,
        max_quote_age_seconds=max_quote_age_seconds,
    )


def _validate_replacement_contract(
    contract: OptionChainContract,
    *,
    position_leg: OptionPositionLeg,
    now: datetime,
    max_quote_age_seconds: float,
) -> None:
    if contract.underlying_symbol != position_leg.underlying_symbol:
        raise ValueError(
            f"Replacement {contract.symbol} does not match underlying {position_leg.underlying_symbol}."
        )
    if contract.option_type.upper() != position_leg.option_type.upper():
        raise ValueError(
            f"Replacement {contract.symbol} changes {position_leg.option_type} to {contract.option_type}."
        )
    _validate_market(
        contract.symbol,
        contract.bid,
        contract.ask,
        contract.mark,
        contract.contract_multiplier,
        contract.quote_observed_at,
        now=now,
        max_quote_age_seconds=max_quote_age_seconds,
    )


def _validate_market(
    symbol: str,
    bid: float | None,
    ask: float | None,
    mark: float | None,
    multiplier: float | None,
    quote_at: datetime | None,
    *,
    now: datetime,
    max_quote_age_seconds: float,
) -> None:
    if bid is None or ask is None or mark is None:
        raise ValueError(f"{symbol} is missing a complete bid, ask, and mark quote.")
    if bid < 0 or ask < bid or mark < 0:
        raise ValueError(f"{symbol} has an invalid or crossed option market.")
    if multiplier is None or multiplier <= 0:
        raise ValueError(f"{symbol} has no verified contract multiplier.")
    if quote_at is None:
        raise ValueError(f"{symbol} quote timestamp is unavailable.")
    quote_time = _aware_datetime(quote_at)
    age = (now - quote_time).total_seconds()
    if age < -30:
        raise ValueError(f"{symbol} quote timestamp is unexpectedly in the future.")
    if age > max_quote_age_seconds:
        raise ValueError(
            f"{symbol} quote is stale ({age:.0f}s old; maximum {max_quote_age_seconds:.0f}s)."
        )


def _net_order_terms(legs: Sequence[RollOrderLeg]) -> tuple[str, float, float, float]:
    low_cash = 0.0
    high_cash = 0.0
    midpoint_cash = 0.0
    for leg in legs:
        ratio = leg.ratio_quantity
        if leg.instruction.startswith("SELL"):
            low_cash += leg.bid * ratio
            high_cash += leg.ask * ratio
            midpoint_cash += leg.mark * ratio
        else:
            low_cash -= leg.ask * ratio
            high_cash -= leg.bid * ratio
            midpoint_cash -= leg.mark * ratio
    if math.isclose(midpoint_cash, 0.0, abs_tol=0.005):
        raise ValueError("The roll has a zero net mark, so debit versus credit is ambiguous.")
    order_type = "NET_CREDIT" if midpoint_cash > 0 else "NET_DEBIT"
    sign = 1.0 if order_type == "NET_CREDIT" else -1.0
    if (low_cash < 0 < high_cash) or math.isclose(low_cash, 0.0, abs_tol=0.005) or math.isclose(high_cash, 0.0, abs_tol=0.005):
        raise ValueError("The current roll market crosses between a debit and a credit.")
    prices = sorted((sign * low_cash, sign * high_cash))
    if prices[0] <= 0:
        raise ValueError("The roll bid/ask estimate is not a positive executable price.")
    bid = _cent_price(prices[0])
    ask = _cent_price(prices[1])
    midpoint = _cent_price((bid + ask) / 2.0)
    return order_type, bid, midpoint, ask


def _component(label: str, legs: tuple[RollOrderLeg, ...]) -> RollOrderComponent:
    quantity = reduce(gcd, (leg.quantity for leg in legs))
    if len(legs) == 1:
        leg = legs[0]
        price = _cent_price(max(leg.mark, 0.01))
        cash_sign = 1.0 if leg.instruction.startswith("SELL") else -1.0
        return RollOrderComponent(
            label=label,
            legs=legs,
            api_order_type="LIMIT",
            complex_order_strategy_type=None,
            order_quantity=quantity,
            limit_price=price,
            estimated_cash_effect=round(
                cash_sign * price * leg.contract_multiplier * quantity,
                2,
            ),
        )
    order_type, _bid, midpoint, _ask = _net_order_terms(legs)
    direction = 1.0 if order_type == "NET_CREDIT" else -1.0
    return RollOrderComponent(
        label=label,
        legs=legs,
        api_order_type=order_type,
        complex_order_strategy_type="CUSTOM",
        order_quantity=quantity,
        limit_price=midpoint,
        estimated_cash_effect=round(
            direction * midpoint * legs[0].contract_multiplier * quantity,
            2,
        ),
    )


def _component_payload(
    component: RollOrderComponent,
    *,
    duration: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "orderType": component.api_order_type,
        "session": "NORMAL",
        "duration": "DAY" if duration == DAY_ONLY else "GOOD_TILL_CANCEL",
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [
            {
                "instruction": leg.instruction,
                "quantity": leg.quantity,
                "instrument": {"symbol": leg.symbol, "assetType": "OPTION"},
            }
            for leg in component.legs
        ],
        "price": _cent_price(component.limit_price),
    }
    if len(component.legs) > 1:
        payload["complexOrderStrategyType"] = component.complex_order_strategy_type or "CUSTOM"
        payload["quantity"] = component.order_quantity
    return payload


def _roll_analysis(
    book: OptionPositionBook,
    position: Sequence[OptionPositionLeg],
    close: Sequence[OptionPositionLeg],
    replacements: Sequence[RollOrderLeg],
    *,
    replacement_expiration: str,
    underlying_price: float | None,
    estimated_roll_cash: float,
    fee_per_contract: float | None,
) -> RollAnalysis:
    close_symbols = {leg.symbol for leg in close}
    before_legs = tuple(_analytics_from_position(leg) for leg in position)
    unchanged = tuple(
        _analytics_from_position(leg) for leg in position if leg.symbol not in close_symbols
    )
    replacement_analytics = tuple(_analytics_from_roll_leg(leg) for leg in replacements)
    realized_values = tuple(leg.unrealized_pnl for leg in close)
    realized = (
        None
        if any(value is None for value in realized_values)
        else round(sum(float(value) for value in realized_values if value is not None), 2)
    )
    before_constant = _current_position_constant(before_legs)
    after_constant = (
        None
        if before_constant is None
        else before_constant + estimated_roll_cash
    )
    before_curve, before_payoff_metrics = _payoff_analysis(
        before_legs,
        constant=before_constant,
        underlying_price=underlying_price,
        unavailable_prefix="Current-position payoff",
    )
    after_legs = unchanged + replacement_analytics
    after_curve, after_payoff_metrics = _payoff_analysis(
        after_legs,
        constant=after_constant,
        underlying_price=underlying_price,
        unavailable_prefix="After-roll payoff",
    )
    before_delta, before_theta = _greeks(before_legs)
    after_delta, after_theta = _greeks(after_legs)
    before_metrics = RollMetricSnapshot(
        max_profit=before_payoff_metrics.max_profit,
        max_profit_unbounded=before_payoff_metrics.max_profit_unbounded,
        max_loss=before_payoff_metrics.max_loss,
        max_loss_unbounded=before_payoff_metrics.max_loss_unbounded,
        breakevens=before_payoff_metrics.breakevens,
        delta=before_delta,
        theta_per_day=before_theta,
        buying_power=book.summary.buying_power,
    )
    after_metrics = RollMetricSnapshot(
        max_profit=after_payoff_metrics.max_profit,
        max_profit_unbounded=after_payoff_metrics.max_profit_unbounded,
        max_loss=after_payoff_metrics.max_loss,
        max_loss_unbounded=after_payoff_metrics.max_loss_unbounded,
        breakevens=after_payoff_metrics.breakevens,
        delta=after_delta,
        theta_per_day=after_theta,
        buying_power=None,
    )
    latest_current_expiration = max(
        _required_date(leg.expiration, f"{leg.symbol} expiration") for leg in close
    )
    days_extended = (
        _required_date(replacement_expiration, "Replacement expiration")
        - latest_current_expiration
    ).days
    fees = None
    if fee_per_contract is not None:
        if not math.isfinite(fee_per_contract) or fee_per_contract < 0:
            raise ValueError("Configured per-contract fee estimate must be nonnegative.")
        fees = round(fee_per_contract * sum(leg.quantity for leg in replacements) * 2, 2)
    return RollAnalysis(
        underlying_price=underlying_price,
        before_curve=before_curve,
        after_curve=after_curve,
        before_metrics=before_metrics,
        after_metrics=after_metrics,
        estimated_realized_pnl=realized,
        days_extended=days_extended,
        estimated_fees=fees,
    )


def _analytics_from_position(leg: OptionPositionLeg) -> _AnalyticsLeg:
    quantity = _whole_signed_quantity(leg.net_quantity, f"{leg.symbol} quantity")
    market_value = leg.market_value
    if market_value is None and leg.mark is not None:
        market_value = leg.mark * quantity * leg.contract_multiplier
    basis = (
        None
        if market_value is None or leg.unrealized_pnl is None
        else float(market_value) - float(leg.unrealized_pnl)
    )
    return _AnalyticsLeg(
        strike=leg.strike,
        option_type=leg.option_type.upper(),
        expiration=leg.expiration,
        signed_quantity=quantity,
        multiplier=leg.contract_multiplier,
        mark=float(leg.mark or 0.0),
        delta=leg.delta,
        theta=leg.theta,
        pnl_basis=basis,
    )


def _analytics_from_roll_leg(leg: RollOrderLeg) -> _AnalyticsLeg:
    return _AnalyticsLeg(
        strike=leg.strike,
        option_type=leg.option_type.upper(),
        expiration=leg.expiration,
        signed_quantity=leg.signed_quantity,
        multiplier=leg.contract_multiplier,
        mark=leg.mark,
        delta=leg.delta,
        theta=leg.theta,
        pnl_basis=None,
    )


def _current_position_constant(legs: Sequence[_AnalyticsLeg]) -> float | None:
    if any(leg.pnl_basis is None for leg in legs):
        return None
    return -sum(float(leg.pnl_basis) for leg in legs if leg.pnl_basis is not None)


def _payoff_analysis(
    legs: Sequence[_AnalyticsLeg],
    *,
    constant: float | None,
    underlying_price: float | None,
    unavailable_prefix: str,
) -> tuple[RollPayoffCurve, RollMetricSnapshot]:
    empty_metrics = RollMetricSnapshot(
        max_profit=None,
        max_profit_unbounded=False,
        max_loss=None,
        max_loss_unbounded=False,
        breakevens=None,
        delta=None,
        theta_per_day=None,
        buying_power=None,
    )
    if not legs:
        return RollPayoffCurve((), (), f"{unavailable_prefix} has no legs."), empty_metrics
    expirations = {leg.expiration for leg in legs}
    if len(expirations) != 1:
        return (
            RollPayoffCurve(
                (),
                (),
                f"{unavailable_prefix} spans multiple expirations; one expiration curve would be misleading.",
            ),
            empty_metrics,
        )
    if constant is None:
        return (
            RollPayoffCurve(
                (),
                (),
                f"{unavailable_prefix} needs complete position cost-basis/P&L facts.",
            ),
            empty_metrics,
        )
    if underlying_price is None or not math.isfinite(underlying_price) or underlying_price <= 0:
        return (
            RollPayoffCurve(
                (),
                (),
                f"{unavailable_prefix} needs the current underlying price.",
            ),
            empty_metrics,
        )
    strikes = sorted({leg.strike for leg in legs})
    low_reference = min(strikes + [underlying_price])
    high_reference = max(strikes + [underlying_price])
    lower = max(0.0, low_reference * 0.75)
    upper = max(high_reference * 1.25, lower + max(10.0, underlying_price * 0.25))
    count = 81
    prices = tuple(lower + (upper - lower) * index / (count - 1) for index in range(count))
    values = tuple(_payoff(legs, price, constant) for price in prices)
    metrics = _payoff_metrics(legs, constant)
    return (
        RollPayoffCurve(
            prices=tuple(round(price, 6) for price in prices),
            profit_loss=tuple(round(value, 2) for value in values),
        ),
        metrics,
    )


def _payoff_metrics(
    legs: Sequence[_AnalyticsLeg],
    constant: float,
) -> RollMetricSnapshot:
    knots = sorted({0.0, *(leg.strike for leg in legs)})
    values = [_payoff(legs, price, constant) for price in knots]
    right_slope = sum(
        leg.signed_quantity * leg.multiplier
        for leg in legs
        if leg.option_type == "CALL"
    )
    max_unbounded = right_slope > 0
    loss_unbounded = right_slope < 0
    max_profit = None if max_unbounded else round(max(values), 2)
    max_loss = None if loss_unbounded else round(min(values), 2)
    roots: list[float] = []
    for index in range(len(knots) - 1):
        left_x, right_x = knots[index], knots[index + 1]
        left_y, right_y = values[index], values[index + 1]
        if math.isclose(left_y, 0.0, abs_tol=1e-8):
            roots.append(left_x)
        if left_y * right_y < 0:
            roots.append(left_x - left_y * (right_x - left_x) / (right_y - left_y))
    if math.isclose(values[-1], 0.0, abs_tol=1e-8):
        roots.append(knots[-1])
    elif not math.isclose(right_slope, 0.0, abs_tol=1e-12):
        tail_root = knots[-1] - values[-1] / right_slope
        if tail_root > knots[-1]:
            roots.append(tail_root)
    deduped: list[float] = []
    for root in sorted(roots):
        if root >= 0 and not any(math.isclose(root, prior, abs_tol=1e-6) for prior in deduped):
            deduped.append(round(root, 4))
    return RollMetricSnapshot(
        max_profit=max_profit,
        max_profit_unbounded=max_unbounded,
        max_loss=max_loss,
        max_loss_unbounded=loss_unbounded,
        breakevens=tuple(deduped),
        delta=None,
        theta_per_day=None,
        buying_power=None,
    )


def _payoff(legs: Sequence[_AnalyticsLeg], underlying_price: float, constant: float) -> float:
    total = constant
    for leg in legs:
        intrinsic = (
            max(underlying_price - leg.strike, 0.0)
            if leg.option_type == "CALL"
            else max(leg.strike - underlying_price, 0.0)
        )
        total += intrinsic * leg.signed_quantity * leg.multiplier
    return total


def _greeks(legs: Sequence[_AnalyticsLeg]) -> tuple[float | None, float | None]:
    delta = (
        None
        if any(leg.delta is None for leg in legs)
        else round(
            sum(float(leg.delta) * leg.signed_quantity * leg.multiplier for leg in legs),
            4,
        )
    )
    theta = (
        None
        if any(leg.theta is None for leg in legs)
        else round(
            sum(float(leg.theta) * leg.signed_quantity * leg.multiplier for leg in legs),
            4,
        )
    )
    return delta, theta


def _roll_template_from_row(row: object) -> SavedRollTemplate:
    if not isinstance(row, Mapping):
        raise ValueError("Each saved roll template must be an object.")
    allowed = {"name", "days_forward", "keep_strike_widths", "duration", "price_policy"}
    unexpected = set(row) - allowed
    if unexpected:
        raise ValueError(
            "Roll templates may contain configuration defaults only; unexpected field(s): "
            + ", ".join(sorted(str(value) for value in unexpected))
        )
    name = str(row.get("name") or "").strip()
    if not name or len(name) > 80:
        raise ValueError("Saved roll template name must contain 1 to 80 characters.")
    days_forward = _whole_quantity(row.get("days_forward"), "Saved days forward")
    if days_forward > 730:
        raise ValueError("Saved days forward must not exceed 730 days.")
    duration = str(row.get("duration") or DAY_ONLY).strip()
    if duration not in {DAY_ONLY, GOOD_UNTIL_CANCELED}:
        raise ValueError(f"Saved template {name!r} has an unsupported duration.")
    price_policy = str(row.get("price_policy") or ROLL_PRICE_MID).strip().upper()
    if price_policy not in {ROLL_PRICE_MID, ROLL_PRICE_NATURAL}:
        raise ValueError(f"Saved template {name!r} has an unsafe price policy.")
    keep = row.get("keep_strike_widths")
    if not isinstance(keep, bool):
        raise ValueError(f"Saved template {name!r} must specify keep_strike_widths as true or false.")
    return SavedRollTemplate(
        name=name,
        days_forward=days_forward,
        keep_strike_widths=keep,
        duration=duration,
        price_policy=price_policy,
    )


def _contract_expiration(row: Mapping[str, object]) -> str | None:
    text = str(row.get("expirationDate") or row.get("expiration") or "").strip()
    if text:
        return text[:10]
    milliseconds = _finite_number(row.get("expirationDateInLong"))
    if milliseconds is None:
        return None
    try:
        return datetime.fromtimestamp(milliseconds / 1000.0, tz=timezone.utc).date().isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _timestamp_from_values(row: Mapping[str, object], keys: Sequence[str]) -> datetime | None:
    for key in keys:
        raw = row.get(key)
        if raw in (None, ""):
            continue
        numeric = _finite_number(raw)
        if numeric is not None:
            seconds = numeric / 1000.0 if numeric > 10_000_000_000 else numeric
            try:
                return datetime.fromtimestamp(seconds, tz=timezone.utc)
            except (OSError, OverflowError, ValueError):
                continue
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        return _aware_datetime(parsed)
    return None


def _first_number(row: Mapping[str, object], keys: Sequence[str]) -> float | None:
    for key in keys:
        number = _finite_number(row.get(key))
        if number is not None:
            return number
    return None


def _finite_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive_price(value: object, label: str) -> float:
    number = _finite_number(value)
    if number is None or number <= 0:
        raise ValueError(f"{label} must be a positive number.")
    return number


def _cent_price(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _whole_quantity(value: object, label: str) -> int:
    number = _finite_number(value)
    if number is None or number <= 0 or not math.isclose(number, round(number), abs_tol=1e-8):
        raise ValueError(f"{label} must be a positive whole number.")
    return int(round(number))


def _whole_signed_quantity(value: object, label: str) -> int:
    number = _finite_number(value)
    if number is None or math.isclose(number, 0.0, abs_tol=1e-9) or not math.isclose(number, round(number), abs_tol=1e-8):
        raise ValueError(f"{label} must be a nonzero whole number.")
    return int(round(number))


def _parse_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _required_date(value: object, label: str) -> date:
    parsed = _parse_date(value)
    if parsed is None:
        raise ValueError(f"{label} is unavailable or invalid.")
    return parsed


def _aware_datetime(value: datetime | None) -> datetime:
    if value is None:
        raise ValueError("Required quote timestamp is unavailable.")
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _strike_key(value: float) -> float:
    return round(float(value), 8)


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


__all__ = [
    "DEFAULT_MAX_QUOTE_AGE_SECONDS",
    "LOCAL_ROLL_PRICE_SOURCE",
    "MAX_SCHWAB_COMPLEX_LEGS",
    "ROLL_EXECUTION_ATOMIC",
    "ROLL_EXECUTION_NON_ATOMIC",
    "ROLL_EXECUTION_UNSUPPORTED",
    "ROLL_PRICE_MANUAL",
    "ROLL_PRICE_MID",
    "ROLL_PRICE_NATURAL",
    "ROLL_SCOPE_ENTIRE",
    "ROLL_SCOPE_SELECTED",
    "ROLL_TEMPLATE_SCHEMA_VERSION",
    "build_roll_order_draft",
    "build_roll_order_payloads",
    "default_roll_template_path",
    "eligible_roll_expirations",
    "load_roll_templates",
    "parse_roll_chain",
    "refresh_roll_order_draft",
    "roll_action_disabled_reason",
    "save_roll_template",
    "suggest_replacement_contracts",
    "validate_roll_position_drift",
]
