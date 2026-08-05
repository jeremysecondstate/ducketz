from __future__ import annotations

import json
import inspect
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.models.option_management import (
    OptionChainContract,
    OptionPositionBook,
    OptionPositionLeg,
    OptionPositionSummary,
    SavedRollTemplate,
)
from app.services.option_rolls import (
    ROLL_EXECUTION_ATOMIC,
    ROLL_EXECUTION_NON_ATOMIC,
    ROLL_EXECUTION_UNSUPPORTED,
    ROLL_PRICE_MANUAL,
    ROLL_SCOPE_ENTIRE,
    ROLL_SCOPE_SELECTED,
    build_roll_order_draft,
    build_roll_order_payloads,
    eligible_roll_expirations,
    load_roll_templates,
    parse_roll_chain,
    refresh_roll_order_draft,
    roll_action_disabled_reason,
    save_roll_template,
    suggest_replacement_contracts,
)
from app.services.schwab_strategy_orders import DAY_ONLY, GOOD_UNTIL_CANCELED
from app.ui.option_rolls import RollWorkspaceController, RollWorkspaceDialog
from app.ui.options_management import OptionsManagementView


OBSERVED_AT = datetime(2026, 8, 5, 16, 0, tzinfo=timezone.utc)
SHORT = "NVDA  260821P00100000"
LONG = "NVDA  260821P00095000"


def test_roll_closing_instruction_uses_signed_quantity_and_preserves_ratio() -> None:
    book = _book(
        (
            _position(SHORT, strike=100, quantity=-4, mark=2.50, bid=2.40, ask=2.60),
            _position(LONG, strike=95, quantity=2, mark=1.00, bid=0.90, ask=1.10),
        )
    )
    chain = parse_roll_chain(_chain(), expected_underlying="NVDA", observed_at=OBSERVED_AT)
    replacements = suggest_replacement_contracts(
        book.legs,
        chain,
        expiration="2026-09-18",
        keep_strike_widths=True,
    )

    draft = build_roll_order_draft(
        book,
        (SHORT, LONG),
        (SHORT, LONG),
        replacements,
        scope_mode=ROLL_SCOPE_ENTIRE,
        keep_strike_widths=True,
        underlying_price=chain.underlying_price,
        now=OBSERVED_AT + timedelta(seconds=10),
    )

    assert [leg.instruction for leg in draft.close_legs] == ["BUY_TO_CLOSE", "SELL_TO_CLOSE"]
    assert [leg.instruction for leg in draft.replacement_legs] == ["SELL_TO_OPEN", "BUY_TO_OPEN"]
    assert draft.order_quantity == 2
    assert [leg.ratio_quantity for leg in draft.close_legs] == [2, 1]
    assert [leg.ratio_quantity for leg in draft.replacement_legs] == [2, 1]
    assert [leg.signed_quantity for leg in draft.replacement_legs] == [-4, 2]


def test_chain_mapping_preserves_exact_symbols_and_changes_with_expiration() -> None:
    book = _book(_vertical())
    chain = parse_roll_chain(_chain(), expected_underlying="NVDA", observed_at=OBSERVED_AT)

    assert eligible_roll_expirations(book.legs, chain, now=OBSERVED_AT) == (
        "2026-09-18",
        "2026-10-16",
    )
    september = suggest_replacement_contracts(
        book.legs,
        chain,
        expiration="2026-09-18",
        keep_strike_widths=True,
    )
    october = suggest_replacement_contracts(
        book.legs,
        chain,
        expiration="2026-10-16",
        keep_strike_widths=True,
    )

    assert [contract.symbol for contract in september] == [
        "NVDA  260918P00100000",
        "NVDA  260918P00095000",
    ]
    assert [contract.symbol for contract in october] == [
        "NVDA  261016P00100000",
        "NVDA  261016P00095000",
    ]


def test_keep_width_suggestion_uses_one_exact_shift_while_individual_mode_can_change_width() -> None:
    book = _book(_vertical())
    chain = parse_roll_chain(
        _chain(
            september_strikes=(
                (101.0, 3.00, 3.20),
                (96.0, 1.20, 1.40),
                (94.5, 0.80, 1.00),
            )
        ),
        expected_underlying="NVDA",
        observed_at=OBSERVED_AT,
    )

    kept = suggest_replacement_contracts(
        book.legs,
        chain,
        expiration="2026-09-18",
        keep_strike_widths=True,
    )
    independent = suggest_replacement_contracts(
        book.legs,
        chain,
        expiration="2026-09-18",
        keep_strike_widths=False,
    )

    assert [item.strike for item in kept] == [101.0, 96.0]
    assert [item.strike for item in independent] == [101.0, 94.5]
    assert all(item.symbol for item in kept + independent)


def test_single_leg_and_selected_leg_rolls_are_explicit() -> None:
    book = _book(_vertical())
    chain = parse_roll_chain(_chain(), expected_underlying="NVDA", observed_at=OBSERVED_AT)
    selected_leg = (book.legs[0],)
    replacement = suggest_replacement_contracts(
        selected_leg,
        chain,
        expiration="2026-09-18",
        keep_strike_widths=True,
    )

    selected = build_roll_order_draft(
        book,
        (SHORT, LONG),
        (SHORT,),
        replacement,
        scope_mode=ROLL_SCOPE_SELECTED,
        keep_strike_widths=True,
        underlying_price=100.0,
        now=OBSERVED_AT,
    )
    single_book = _book((book.legs[0],))
    single = build_roll_order_draft(
        single_book,
        (SHORT,),
        (SHORT,),
        replacement,
        scope_mode=ROLL_SCOPE_ENTIRE,
        keep_strike_widths=True,
        underlying_price=100.0,
        now=OBSERVED_AT,
    )

    assert selected.scope_label == "1 Selected Leg"
    assert selected.position_symbols == (SHORT, LONG)
    assert selected.close_symbols == (SHORT,)
    assert single.scope_label == "Entire Position"
    assert len(single.close_legs) == len(single.replacement_legs) == 1


def test_net_credit_debit_rail_and_manual_price_are_derived_from_leg_quotes() -> None:
    book = _book(_vertical())
    credit_chain = parse_roll_chain(_chain(), expected_underlying="NVDA", observed_at=OBSERVED_AT)
    credit_contracts = suggest_replacement_contracts(
        book.legs,
        credit_chain,
        expiration="2026-09-18",
        keep_strike_widths=True,
    )
    credit = build_roll_order_draft(
        book,
        (SHORT, LONG),
        (SHORT, LONG),
        credit_contracts,
        scope_mode=ROLL_SCOPE_ENTIRE,
        keep_strike_widths=True,
        limit_price="0.42",
        underlying_price=100.0,
        now=OBSERVED_AT,
    )
    midpoint_credit = build_roll_order_draft(
        book,
        (SHORT, LONG),
        (SHORT, LONG),
        credit_contracts,
        scope_mode=ROLL_SCOPE_ENTIRE,
        keep_strike_widths=True,
        underlying_price=100.0,
        now=OBSERVED_AT,
    )

    debit_contracts = tuple(
        replace(contract, bid=contract.bid * 0.25, ask=contract.ask * 0.25, mark=contract.mark * 0.25)
        for contract in credit_contracts
    )
    debit = build_roll_order_draft(
        book,
        (SHORT, LONG),
        (SHORT, LONG),
        debit_contracts,
        scope_mode=ROLL_SCOPE_ENTIRE,
        keep_strike_widths=True,
        underlying_price=100.0,
        now=OBSERVED_AT,
    )

    assert credit.api_order_type == "NET_CREDIT"
    assert credit.limit_price == pytest.approx(0.42)
    assert credit.price_policy == ROLL_PRICE_MANUAL
    assert credit.estimated_cash_effect == pytest.approx(42.0)
    assert credit.price_rail.bid <= credit.price_rail.midpoint <= credit.price_rail.ask
    assert credit.analysis.after_curve.profit_loss[0] - midpoint_credit.analysis.after_curve.profit_loss[0] == pytest.approx(
        credit.estimated_cash_effect - midpoint_credit.estimated_cash_effect
    )
    assert debit.api_order_type == "NET_DEBIT"
    assert debit.estimated_cash_effect < 0


def test_payoff_greeks_realized_pnl_days_and_optional_fees_compare_before_after() -> None:
    book = _book(_vertical())
    chain = parse_roll_chain(_chain(), expected_underlying="NVDA", observed_at=OBSERVED_AT)
    replacements = suggest_replacement_contracts(
        book.legs,
        chain,
        expiration="2026-09-18",
        keep_strike_widths=True,
    )
    draft = build_roll_order_draft(
        book,
        (SHORT, LONG),
        (SHORT, LONG),
        replacements,
        scope_mode=ROLL_SCOPE_ENTIRE,
        keep_strike_widths=True,
        underlying_price=chain.underlying_price,
        fee_per_contract=0.65,
        now=OBSERVED_AT,
    )

    assert draft.analysis.before_curve.available
    assert draft.analysis.after_curve.available
    assert len(draft.analysis.before_curve.prices) == 81
    assert draft.analysis.before_metrics.max_loss is not None
    assert draft.analysis.after_metrics.max_profit is not None
    assert draft.analysis.before_metrics.delta is not None
    assert draft.analysis.after_metrics.theta_per_day is not None
    assert draft.analysis.estimated_realized_pnl == pytest.approx(-30.0)
    assert draft.analysis.days_extended == 28
    assert draft.analysis.estimated_fees == pytest.approx(2.60)
    assert draft.analysis.before_metrics.buying_power == pytest.approx(50_000.0)
    assert draft.analysis.after_metrics.buying_power is None


def test_unavailable_basis_or_multiple_expirations_never_fabricates_payoff() -> None:
    first, second = _vertical()
    first = replace(first, unrealized_pnl=None)
    book = _book((first, second))
    chain = parse_roll_chain(_chain(), expected_underlying="NVDA", observed_at=OBSERVED_AT)
    replacements = suggest_replacement_contracts(
        book.legs,
        chain,
        expiration="2026-09-18",
        keep_strike_widths=True,
    )

    draft = build_roll_order_draft(
        book,
        (SHORT, LONG),
        (SHORT, LONG),
        replacements,
        scope_mode=ROLL_SCOPE_ENTIRE,
        keep_strike_widths=True,
        underlying_price=100.0,
        now=OBSERVED_AT,
    )

    assert not draft.analysis.before_curve.available
    assert draft.analysis.before_metrics.max_profit is None
    assert draft.analysis.estimated_realized_pnl is None
    assert draft.review_eligible


@pytest.mark.parametrize(
    ("mutator", "message"),
    (
        (lambda leg: replace(leg, bid=None), "missing a complete bid"),
        (lambda leg: replace(leg, quote_observed_at=OBSERVED_AT - timedelta(minutes=5)), "quote is stale"),
    ),
)
def test_missing_or_stale_position_quotes_fail_closed(mutator: object, message: str) -> None:
    first, second = _vertical()
    changed = mutator(first)  # type: ignore[operator]
    book = _book((changed, second))
    chain = parse_roll_chain(_chain(), expected_underlying="NVDA", observed_at=OBSERVED_AT)
    replacements = suggest_replacement_contracts(
        book.legs,
        chain,
        expiration="2026-09-18",
        keep_strike_widths=True,
    )

    with pytest.raises(ValueError, match=message):
        build_roll_order_draft(
            book,
            (SHORT, LONG),
            (SHORT, LONG),
            replacements,
            scope_mode=ROLL_SCOPE_ENTIRE,
            keep_strike_widths=True,
            underlying_price=100.0,
            now=OBSERVED_AT,
        )


def test_position_drift_stops_review_refresh() -> None:
    book = _book(_vertical())
    chain = parse_roll_chain(_chain(), expected_underlying="NVDA", observed_at=OBSERVED_AT)
    draft = build_roll_order_draft(
        book,
        (SHORT, LONG),
        (SHORT, LONG),
        suggest_replacement_contracts(
            book.legs,
            chain,
            expiration="2026-09-18",
            keep_strike_widths=True,
        ),
        scope_mode=ROLL_SCOPE_ENTIRE,
        keep_strike_widths=True,
        underlying_price=100.0,
        now=OBSERVED_AT,
    )
    drifted = _book((replace(book.legs[0], net_quantity=-2), book.legs[1]))

    with pytest.raises(ValueError, match="Position drift"):
        refresh_roll_order_draft(
            draft,
            latest=drifted,
            chain=chain,
            now=OBSERVED_AT,
        )


def test_unchanged_leg_drift_also_stops_selected_leg_review() -> None:
    book = _book(_vertical())
    chain = parse_roll_chain(_chain(), expected_underlying="NVDA", observed_at=OBSERVED_AT)
    draft = build_roll_order_draft(
        book,
        (SHORT, LONG),
        (SHORT,),
        suggest_replacement_contracts(
            (book.legs[0],),
            chain,
            expiration="2026-09-18",
            keep_strike_widths=True,
        ),
        scope_mode=ROLL_SCOPE_SELECTED,
        keep_strike_widths=True,
        underlying_price=100.0,
        now=OBSERVED_AT,
    )
    drifted = _book((book.legs[0], replace(book.legs[1], net_quantity=2)))

    with pytest.raises(ValueError, match="Position drift"):
        refresh_roll_order_draft(
            draft,
            latest=drifted,
            chain=chain,
            now=OBSERVED_AT,
        )


def test_non_atomic_roll_is_explicit_and_atomic_capability_builds_one_exact_payload() -> None:
    book = _book(_vertical())
    chain = parse_roll_chain(_chain(), expected_underlying="NVDA", observed_at=OBSERVED_AT)
    replacements = suggest_replacement_contracts(
        book.legs,
        chain,
        expiration="2026-09-18",
        keep_strike_widths=True,
    )
    non_atomic = build_roll_order_draft(
        book,
        (SHORT, LONG),
        (SHORT, LONG),
        replacements,
        scope_mode=ROLL_SCOPE_ENTIRE,
        keep_strike_widths=True,
        underlying_price=100.0,
        now=OBSERVED_AT,
    )
    atomic = build_roll_order_draft(
        book,
        (SHORT, LONG),
        (SHORT, LONG),
        replacements,
        scope_mode=ROLL_SCOPE_ENTIRE,
        keep_strike_widths=True,
        atomic_order_supported=True,
        underlying_price=100.0,
        now=OBSERVED_AT,
    )

    assert non_atomic.execution_mode == ROLL_EXECUTION_NON_ATOMIC
    assert len(non_atomic.components) == 2
    assert "exposure risk" in non_atomic.execution_detail
    assert atomic.execution_mode == ROLL_EXECUTION_ATOMIC
    payloads = build_roll_order_payloads(atomic)
    assert len(payloads) == 1
    assert payloads[0]["orderType"] == atomic.api_order_type
    assert payloads[0]["complexOrderStrategyType"] == "CUSTOM"
    assert [row["instruction"] for row in payloads[0]["orderLegCollection"]] == [
        "BUY_TO_CLOSE",
        "SELL_TO_CLOSE",
        "SELL_TO_OPEN",
        "BUY_TO_OPEN",
    ]


@pytest.mark.parametrize(
    ("duration", "api_duration"),
    [
        (DAY_ONLY, "DAY"),
        (GOOD_UNTIL_CANCELED, "GOOD_TILL_CANCEL"),
    ],
)
def test_roll_accepts_every_visible_time_in_force_value(
    duration: str,
    api_duration: str,
) -> None:
    book = _book(_vertical())
    chain = parse_roll_chain(_chain(), expected_underlying="NVDA", observed_at=OBSERVED_AT)
    replacements = suggest_replacement_contracts(
        book.legs,
        chain,
        expiration="2026-09-18",
        keep_strike_widths=True,
    )
    draft = build_roll_order_draft(
        book,
        (SHORT, LONG),
        (SHORT, LONG),
        replacements,
        scope_mode=ROLL_SCOPE_ENTIRE,
        keep_strike_widths=True,
        duration=duration,
        atomic_order_supported=True,
        underlying_price=chain.underlying_price,
        now=OBSERVED_AT,
    )

    payloads = build_roll_order_payloads(draft)

    assert len(payloads) == 1
    assert payloads[0]["duration"] == api_duration


def test_unsupported_component_shape_blocks_review() -> None:
    positions = tuple(
        _position(
            f"NVDA  260821P{int((90 + index) * 1000):08d}",
            strike=90 + index,
            quantity=-1,
            mark=1.0 + index * 0.15,
            bid=0.95 + index * 0.15,
            ask=1.05 + index * 0.15,
        )
        for index in range(5)
    )
    book = _book(positions)
    replacements = tuple(
        OptionChainContract(
            symbol=f"NVDA  260918P{int(leg.strike * 1000):08d}",
            underlying_symbol="NVDA",
            option_type="PUT",
            expiration="2026-09-18",
            strike=leg.strike,
            bid=leg.bid + 1.0,
            ask=leg.ask + 1.0,
            mark=leg.mark + 1.0,
            delta=-0.2,
            theta=-0.04,
            contract_multiplier=100.0,
            quote_observed_at=OBSERVED_AT,
        )
        for leg in positions
    )

    draft = build_roll_order_draft(
        book,
        tuple(leg.symbol for leg in positions),
        tuple(leg.symbol for leg in positions),
        replacements,
        scope_mode=ROLL_SCOPE_ENTIRE,
        keep_strike_widths=True,
        underlying_price=100.0,
        now=OBSERVED_AT,
    )

    assert draft.execution_mode == ROLL_EXECUTION_UNSUPPORTED
    assert not draft.review_eligible
    with pytest.raises(ValueError, match="not review eligible"):
        build_roll_order_payloads(draft)


def test_roll_action_requires_current_unambiguous_same_underlying_position() -> None:
    book = _book(_vertical())
    assert roll_action_disabled_reason(book, (SHORT, LONG), now=OBSERVED_AT) is None
    mixed = _book((book.legs[0], replace(book.legs[1], underlying_symbol="AAPL")))
    assert "one underlying" in str(
        roll_action_disabled_reason(mixed, (SHORT, LONG), now=OBSERVED_AT)
    )


def test_management_roll_action_enablement_follows_domain_validation() -> None:
    class Button:
        state = ""

        def configure(self, *, state: str) -> None:
            self.state = state

    view = OptionsManagementView.__new__(OptionsManagementView)
    view.book = _book(_vertical())
    view.roll_button = Button()

    view._update_roll_control(view.book.legs)
    assert view.roll_button.state == "normal"

    view.book = replace(view.book, status="UNAVAILABLE")
    view._update_roll_control(view.book.legs)
    assert view.roll_button.state == "disabled"


def test_roll_templates_store_defaults_without_account_quote_quantity_or_symbols(tmp_path: Path) -> None:
    path = tmp_path / "roll-templates.json"
    template = SavedRollTemplate(
        name="Monthly same width",
        days_forward=28,
        keep_strike_widths=True,
        duration="Day only",
        price_policy="MID",
    )

    save_roll_template(template, path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert load_roll_templates(path) == (template,)
    text = json.dumps(payload)
    for forbidden in ("account", "symbol", "quantity", "quote", "balance", "limit_price"):
        assert forbidden not in text.lower()


def test_workspace_controller_rebuilds_scope_expiration_width_and_prices() -> None:
    book = _book(_vertical())
    controller = RollWorkspaceController(
        book=book,
        position_symbols=(SHORT, LONG),
        now_provider=lambda: OBSERVED_AT,
    )
    controller.load_chain(_chain())

    assert controller.can_review
    assert controller.expiration == "2026-09-18"
    first_symbols = tuple(leg.symbol for leg in controller.draft.replacement_legs)  # type: ignore[union-attr]
    controller.set_expiration("2026-10-16")
    assert controller.can_review
    assert tuple(leg.symbol for leg in controller.draft.replacement_legs) != first_symbols  # type: ignore[union-attr]

    controller.set_scope(ROLL_SCOPE_SELECTED)
    controller.set_leg_enabled(LONG, False)
    assert controller.draft is not None
    assert controller.draft.close_symbols == (SHORT,)
    assert len(controller.draft.replacement_legs) == 1

    midpoint = controller.draft.price_rail.midpoint
    controller.adjust_price(0.01)
    assert controller.draft is not None
    assert controller.draft.limit_price == pytest.approx(midpoint + 0.01)
    assert controller.draft.price_policy == ROLL_PRICE_MANUAL
    controller.select_midpoint()
    assert controller.draft is not None
    assert controller.draft.limit_price == pytest.approx(controller.draft.price_rail.midpoint)


def test_controller_review_gating_and_routing_never_submits() -> None:
    book = _book(_vertical())
    controller = RollWorkspaceController(
        book=book,
        position_symbols=(SHORT, LONG),
        now_provider=lambda: OBSERVED_AT,
    )
    routed: list[object] = []
    with pytest.raises(ValueError, match="Loading"):
        controller.route_review(routed.append)
    controller.load_chain(_chain())
    controller.route_review(routed.append)

    assert routed == [controller.draft]
    assert "submit_order" not in inspect.getsource(RollWorkspaceDialog)


def test_roll_action_routes_current_exact_scope_and_defers_chain_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    book = _book(_vertical())
    captured: dict[str, object] = {}
    chain_calls: list[tuple[str, int]] = []

    class Session:
        def get_option_chain(self, symbol: str, strikes: int) -> object:
            chain_calls.append((symbol, strikes))
            return _chain()

    view = OptionsManagementView.__new__(OptionsManagementView)
    view.book = book
    view.root = object()
    view.snapshot_loader = lambda: object()
    view.session_factory = Session
    view._scoped_position_rows = lambda: book.legs
    view._review_roll = lambda draft: None
    monkeypatch.setattr(
        "app.ui.options_management.RollWorkspaceDialog",
        lambda **kwargs: captured.update(kwargs),
    )

    view._open_roll()

    assert captured["book"] is book
    assert captured["position_symbols"] == (SHORT, LONG)
    assert chain_calls == []
    assert captured["chain_loader"]() == _chain()  # type: ignore[operator]
    assert chain_calls == [("NVDA", 100)]


def _vertical() -> tuple[OptionPositionLeg, OptionPositionLeg]:
    return (
        _position(SHORT, strike=100, quantity=-1, mark=2.50, bid=2.40, ask=2.60, unrealized=-50),
        _position(LONG, strike=95, quantity=1, mark=1.00, bid=0.90, ask=1.10, unrealized=20),
    )


def _position(
    symbol: str,
    *,
    strike: float,
    quantity: int,
    mark: float,
    bid: float | None,
    ask: float | None,
    expiration: str = "2026-08-21",
    unrealized: float | None = 0.0,
) -> OptionPositionLeg:
    return OptionPositionLeg(
        account_label="Schwab test",
        symbol=symbol,
        underlying_symbol="NVDA",
        option_type="PUT",
        expiration=expiration,
        strike=strike,
        net_quantity=float(quantity),
        settled_quantity=float(quantity),
        contract_multiplier=100.0,
        bid=bid,
        ask=ask,
        mark=mark,
        market_value=mark * quantity * 100,
        unrealized_pnl=unrealized,
        day_pnl=0.0,
        delta=-0.45 if strike >= 100 else -0.25,
        theta=-0.08 if strike >= 100 else -0.04,
        observed_at=OBSERVED_AT,
        quote_observed_at=OBSERVED_AT,
        source_ref=f"fixture:{symbol}",
    )


def _book(legs: tuple[OptionPositionLeg, ...]) -> OptionPositionBook:
    return OptionPositionBook(
        account_label="Schwab test",
        observed_at=OBSERVED_AT,
        status="CURRENT",
        legs=legs,
        summary=OptionPositionSummary(
            net_market_value=sum(float(leg.market_value or 0.0) for leg in legs),
            unrealized_pnl=(
                None
                if any(leg.unrealized_pnl is None for leg in legs)
                else sum(float(leg.unrealized_pnl) for leg in legs if leg.unrealized_pnl is not None)
            ),
            day_pnl=0.0,
            theta_per_day=None,
            available_funds=40_000.0,
            buying_power=50_000.0,
        ),
    )


def _chain(
    *,
    september_strikes: tuple[tuple[float, float, float], ...] = (
        (100.0, 3.60, 3.80),
        (95.0, 1.00, 1.20),
    ),
) -> dict[str, object]:
    quote_time = int(OBSERVED_AT.timestamp() * 1000)

    def expiration_rows(
        expiration: str,
        expiration_code: str,
        strikes: tuple[tuple[float, float, float], ...],
    ) -> dict[str, list[dict[str, object]]]:
        return {
            f"{strike:g}": [
                {
                    "symbol": f"NVDA  {expiration_code}P{int(round(strike * 1000)):08d}",
                    "expirationDate": expiration,
                    "strikePrice": strike,
                    "bid": bid,
                    "ask": ask,
                    "mark": (bid + ask) / 2,
                    "delta": -0.50 if strike >= 100 else -0.28,
                    "theta": -0.07 if strike >= 100 else -0.035,
                    "multiplier": 100,
                    "quoteTimeInLong": quote_time,
                }
            ]
            for strike, bid, ask in strikes
        }

    return {
        "symbol": "NVDA",
        "underlyingPrice": 100.0,
        "underlying": {"symbol": "NVDA", "mark": 100.0, "quoteTime": quote_time},
        "callExpDateMap": {},
        "putExpDateMap": {
            "2026-09-18:44": expiration_rows(
                "2026-09-18",
                "260918",
                september_strikes,
            ),
            "2026-10-16:72": expiration_rows(
                "2026-10-16",
                "261016",
                ((100.0, 3.80, 4.00), (95.0, 1.70, 1.90)),
            ),
        },
    }
