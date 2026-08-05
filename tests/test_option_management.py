from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models.portfolio import PortfolioSnapshot
from app.services.schwab_option_management import (
    build_closing_order_draft,
    build_closing_order_payload,
    enrich_option_position_quotes,
    filter_option_positions,
    option_orders_from_payload,
    option_orders_from_snapshot,
    option_position_book,
    submit_validated_closing_order,
    validate_closing_position_drift,
)
from app.services.schwab_policy_inputs import normalize_schwab_policy_inputs
from app.services.schwab_strategy_orders import DAY_ONLY, GOOD_UNTIL_CANCELED
from app.ui.background_tasks import run_in_background
from app.ui.options_management import (
    CLOSE_SCOPE_ENTIRE,
    CLOSE_SCOPE_SELECTED,
    _closing_price_rail,
    _closing_scope_rows,
    _initial_position_selection,
    _matching_order_iid,
    _selection_after_click,
)


OBSERVED_AT = datetime(2026, 8, 3, 20, 0, tzinfo=timezone.utc)


def test_exact_leg_selection_supports_toggle_and_range_without_inferred_groups() -> None:
    selected, anchor = _selection_after_click((), 1, None, 5, extend=False, toggle=False)
    assert selected == (1,)
    assert anchor == 1

    selected, anchor = _selection_after_click(selected, 3, anchor, 5, extend=False, toggle=True)
    assert selected == (1, 3)
    assert anchor == 3

    selected, anchor = _selection_after_click(selected, 1, anchor, 5, extend=True, toggle=False)
    assert selected == (1, 2, 3)
    assert anchor == 3

    selected, anchor = _selection_after_click(selected, 2, anchor, 5, extend=False, toggle=True)
    assert selected == (1, 3)
    assert anchor == 2


def test_exact_leg_selection_ignores_clicks_outside_visible_rows() -> None:
    assert _selection_after_click((1,), 4, 2, 4, extend=False, toggle=False) == ((1,), 2)


def test_initial_position_selection_prefers_preserved_symbol_then_first_closable_row() -> None:
    blocked = _position(
        symbol="NVDA  260918P00190000",
        quantity=1,
        mark=0.25,
        strike=190.0,
    )
    blocked["option_fields_complete"] = False
    book = option_position_book(
        _snapshot(
            [
                blocked,
                _position(
                    symbol="NVDA  260918P00200000",
                    quantity=1,
                    mark=0.50,
                    strike=200.0,
                ),
            ]
        )
    )

    assert _initial_position_selection(book.legs) == (1,)
    assert _initial_position_selection(book.legs, (book.legs[0].symbol,)) == (0,)


def test_close_scope_uses_active_position_or_every_explicit_selection() -> None:
    book = option_position_book(
        _snapshot(
            [
                _position(symbol="NVDA  260918P00210000", quantity=1, mark=1.50),
                _position(
                    symbol="NVDA  260918P00200000",
                    quantity=-1,
                    mark=0.50,
                    strike=200.0,
                ),
            ]
        )
    )

    assert _closing_scope_rows(
        book.legs,
        active_symbol=book.legs[1].symbol,
        scope=CLOSE_SCOPE_ENTIRE,
    ) == (book.legs[1],)
    assert _closing_scope_rows(
        book.legs,
        active_symbol=book.legs[1].symbol,
        scope=CLOSE_SCOPE_SELECTED,
    ) == book.legs


def test_closing_price_rail_uses_executable_composite_bid_mid_ask() -> None:
    long_leg = _position(
        symbol="NVDA  260918P00210000",
        quantity=1,
        mark=1.50,
    )
    long_leg.update({"bid": 1.40, "ask": 1.60})
    short_leg = _position(
        symbol="NVDA  260918P00200000",
        quantity=-1,
        mark=0.50,
        strike=200.0,
    )
    short_leg.update({"bid": 0.40, "ask": 0.60})
    draft = build_closing_order_draft(
        option_position_book(_snapshot([long_leg, short_leg])),
        (str(long_leg["symbol"]), str(short_leg["symbol"])),
    )

    assert draft.api_order_type == "NET_CREDIT"
    assert _closing_price_rail(draft) == pytest.approx((0.80, 1.00, 1.20))


def test_closing_price_rail_reverses_a_short_leg_to_buy_prices() -> None:
    short_leg = _position(
        symbol="NVDA  260918P00210000",
        quantity=-1,
        mark=0.60,
    )
    short_leg.update({"bid": 0.50, "ask": 0.70})
    draft = build_closing_order_draft(
        option_position_book(_snapshot([short_leg])),
        (str(short_leg["symbol"]),),
    )

    assert draft.legs[0].instruction == "BUY_TO_CLOSE"
    assert _closing_price_rail(draft) == pytest.approx((0.50, 0.60, 0.70))


def test_closing_price_rail_orients_a_net_debit_independent_of_first_leg_action() -> None:
    long_leg = _position(
        symbol="NVDA  260918P00200000",
        quantity=1,
        mark=0.50,
        strike=200.0,
    )
    long_leg.update({"bid": 0.40, "ask": 0.60})
    short_leg = _position(
        symbol="NVDA  260918P00210000",
        quantity=-1,
        mark=1.50,
        strike=210.0,
    )
    short_leg.update({"bid": 1.40, "ask": 1.60})
    draft = build_closing_order_draft(
        option_position_book(_snapshot([long_leg, short_leg])),
        (str(long_leg["symbol"]), str(short_leg["symbol"])),
    )

    assert draft.api_order_type == "NET_DEBIT"
    assert draft.legs[0].instruction == "SELL_TO_CLOSE"
    assert _closing_price_rail(draft) == pytest.approx((0.80, 1.00, 1.20))


def test_option_position_book_preserves_exact_contracts_and_complete_summaries() -> None:
    snapshot = _snapshot(
        [
            _position(
                symbol="NVDA  260918P00210000",
                quantity=2,
                mark=1.50,
                market_value=300.0,
                unrealized_pnl=-40.0,
                day_pnl=10.0,
                delta=-0.22,
                theta=-0.08,
                strike=210.0,
            ),
            _position(
                symbol="NVDA  261016P00200000",
                quantity=-2,
                mark=0.50,
                market_value=-100.0,
                unrealized_pnl=20.0,
                day_pnl=-4.0,
                delta=-0.10,
                theta=-0.04,
                strike=200.0,
                expiration="2026-10-16",
            ),
        ]
    )

    book = option_position_book(snapshot)

    assert book.account_label == "Schwab ••••907"
    assert [leg.symbol for leg in book.legs] == [
        "NVDA  260918P00210000",
        "NVDA  261016P00200000",
    ]
    assert book.summary.net_market_value == pytest.approx(200.0)
    assert book.summary.unrealized_pnl == pytest.approx(-20.0)
    assert book.summary.day_pnl == pytest.approx(6.0)
    assert book.summary.theta_per_day == pytest.approx(-8.0)
    assert book.summary.available_funds == pytest.approx(43_959.84)
    assert book.summary.buying_power == pytest.approx(51_234.56)
    assert filter_option_positions(book, symbol="nvda", expiration="2026-10-16") == (
        book.legs[1],
    )


def test_option_position_summary_does_not_treat_missing_values_as_zero() -> None:
    row = _position(symbol="NVDA  260918P00210000", quantity=1, mark=1.5)
    row["day_pnl"] = None

    book = option_position_book(_snapshot([row]))

    assert book.summary.day_pnl is None


def test_option_quote_enrichment_reconciles_mark_and_multiplier_without_assuming_100() -> None:
    symbol = "WULF  260918C00024000"
    row = _position(symbol=symbol, quantity=1, mark=0.0, market_value=125.5)
    row["price"] = None
    row["contract_multiplier"] = None
    snapshot = _snapshot([row])

    count = enrich_option_position_quotes(
        snapshot.account_facts,
        {
            symbol: {
                "bidPrice": 1.18,
                "askPrice": 1.33,
                "mark": 1.255,
                "delta": 0.51,
                "theta": -0.07,
                "quoteTime": 1_775_000_000_000,
            }
        },
        observed_at=OBSERVED_AT,
    )
    book = option_position_book(snapshot)

    assert count == 1
    assert book.legs[0].bid == pytest.approx(1.18)
    assert book.legs[0].ask == pytest.approx(1.33)
    assert book.legs[0].mark == pytest.approx(1.255)
    assert book.legs[0].contract_multiplier == pytest.approx(100.0)
    assert book.legs[0].theta == pytest.approx(-0.07)
    assert book.legs[0].close_disabled_reason is None


def test_duplicate_exact_position_rows_fail_closed() -> None:
    symbol = "NVDA  260918P00210000"
    book = option_position_book(
        _snapshot(
            [
                _position(symbol=symbol, quantity=1, mark=1.5),
                _position(symbol=symbol, quantity=1, mark=1.5),
            ]
        )
    )

    assert all(leg.close_disabled_reason for leg in book.legs)
    with pytest.raises(ValueError, match="more than one open row"):
        build_closing_order_draft(book, [symbol])


def test_option_book_fails_closed_when_option_row_set_is_explicitly_incomplete() -> None:
    snapshot = _snapshot([_position(symbol="NVDA  260918P00210000", quantity=1, mark=1.5)])
    positions = snapshot.account_facts["positions"]
    assert isinstance(positions, dict)
    positions.update(
        {
            "status": "INCOMPLETE",
            "option_row_set_complete": False,
            "option_unavailable_reasons": ["An unidentified row may be an option position."],
        }
    )

    book = option_position_book(snapshot)

    assert book.status == "INCOMPLETE"
    draft = build_closing_order_draft(book, (book.legs[0].symbol,))
    with pytest.raises(ValueError, match="option row-set is unavailable or incomplete"):
        validate_closing_position_drift(draft, snapshot)


def test_collective_investment_rows_do_not_block_exact_option_revalidation() -> None:
    option_symbol = "WULF  260918C00024000"
    account_facts = normalize_schwab_policy_inputs(
        {
            "securitiesAccount": {
                "positions": [
                    _raw_schwab_position(
                        symbol=option_symbol,
                        asset_type="OPTION",
                        price=0.93,
                        multiplier=100.0,
                    ),
                    _raw_schwab_position(
                        symbol="VXUS",
                        asset_type="COLLECTIVE_INVESTMENT",
                        price=72.0,
                    ),
                ]
            }
        },
        [],
        observed_at=OBSERVED_AT,
    )
    positions = account_facts["positions"]
    assert isinstance(positions, dict)
    assert positions["option_row_set_complete"] is True
    assert positions["option_unavailable_reasons"] == []

    snapshot = PortfolioSnapshot(
        source="schwab",
        account_label="Schwab",
        synced_at=OBSERVED_AT,
        account_facts=account_facts,
    )
    book = option_position_book(snapshot)
    draft = build_closing_order_draft(book, (option_symbol,))

    assert book.status == "CURRENT"
    validate_closing_position_drift(draft, snapshot)


def test_conflicting_asset_aliases_still_make_option_row_set_incomplete() -> None:
    ambiguous = _raw_schwab_position(
        symbol="VXUS",
        asset_type="COLLECTIVE_INVESTMENT",
        price=72.0,
    )
    ambiguous["assetType"] = "OPTION"
    account_facts = normalize_schwab_policy_inputs(
        {"securitiesAccount": {"positions": [ambiguous]}},
        [],
        observed_at=OBSERVED_AT,
    )
    positions = account_facts["positions"]
    assert isinstance(positions, dict)

    assert positions["option_row_set_complete"] is False
    assert any(
        "missing or conflicting asset identity" in str(reason)
        for reason in positions["option_unavailable_reasons"]
    )


def test_single_short_option_builds_exact_buy_to_close_limit_payload() -> None:
    symbol = "NVDA  260918P00210000"
    draft = build_closing_order_draft(
        option_position_book(_snapshot([_position(symbol=symbol, quantity=-3, mark=0.55)])),
        [symbol],
    )

    assert draft.api_order_type == "LIMIT"
    assert draft.estimated_cash_effect == pytest.approx(-165.0)
    assert draft.legs[0].underlying_symbol == "NVDA"
    assert draft.legs[0].expiration == "2026-09-18"
    assert draft.legs[0].strike == pytest.approx(210.0)
    assert draft.legs[0].option_type == "PUT"
    assert build_closing_order_payload(draft) == {
        "orderType": "LIMIT",
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [
            {
                "instruction": "BUY_TO_CLOSE",
                "quantity": 3,
                "instrument": {"symbol": symbol, "assetType": "OPTION"},
            }
        ],
        "price": 0.55,
    }


def test_close_cash_estimate_uses_the_displayed_cent_rounded_limit() -> None:
    symbol = "NVDA  260918P00210000"
    draft = build_closing_order_draft(
        option_position_book(_snapshot([_position(symbol=symbol, quantity=1, mark=1.205)])),
        [symbol],
    )

    assert draft.estimated_cash_effect == pytest.approx(draft.limit_price * 100)


@pytest.mark.parametrize(
    ("duration", "api_duration"),
    [
        (DAY_ONLY, "DAY"),
        (GOOD_UNTIL_CANCELED, "GOOD_TILL_CANCEL"),
    ],
)
def test_close_accepts_every_visible_time_in_force_value(
    duration: str,
    api_duration: str,
) -> None:
    symbol = "NVDA  260918P00210000"
    draft = build_closing_order_draft(
        option_position_book(_snapshot([_position(symbol=symbol, quantity=1, mark=1.5)])),
        [symbol],
        duration=duration,
    )

    assert build_closing_order_payload(draft)["duration"] == api_duration


def test_selected_long_and_short_legs_preserve_ratio_as_one_net_credit() -> None:
    long_symbol = "NVDA  260918P00210000"
    short_symbol = "NVDA  260918P00200000"
    book = option_position_book(
        _snapshot(
            [
                _position(symbol=long_symbol, quantity=2, mark=1.50, strike=210.0),
                _position(symbol=short_symbol, quantity=-4, mark=0.25, strike=200.0),
            ]
        )
    )

    draft = build_closing_order_draft(book, [long_symbol, short_symbol])
    payload = build_closing_order_payload(draft)

    assert draft.api_order_type == "NET_CREDIT"
    assert draft.order_quantity == 2
    assert [leg.ratio_quantity for leg in draft.legs] == [1, 2]
    assert draft.limit_price == pytest.approx(1.0)
    assert draft.estimated_cash_effect == pytest.approx(200.0)
    assert payload["complexOrderStrategyType"] == "CUSTOM"
    assert payload["quantity"] == 2
    assert [leg["quantity"] for leg in payload["orderLegCollection"]] == [2, 4]
    assert [leg["instruction"] for leg in payload["orderLegCollection"]] == [
        "SELL_TO_CLOSE",
        "BUY_TO_CLOSE",
    ]


def test_close_draft_rejects_custom_order_across_underlyings() -> None:
    nvda_symbol = "NVDA  260918P00210000"
    wulf_symbol = "WULF  260918P00024000"
    wulf = _position(symbol=wulf_symbol, quantity=-1, mark=0.25, strike=24.0)
    wulf["underlying_symbol"] = "WULF"
    book = option_position_book(
        _snapshot(
            [
                _position(symbol=nvda_symbol, quantity=1, mark=1.50),
                wulf,
            ]
        )
    )

    with pytest.raises(ValueError, match="share one underlying"):
        build_closing_order_draft(book, [nvda_symbol, wulf_symbol])


def test_close_draft_rejects_incomplete_identity_and_zero_net_mark() -> None:
    incomplete_symbol = "NVDA  260918P00210000"
    incomplete = _position(symbol=incomplete_symbol, quantity=1, mark=1.0)
    incomplete["option_fields_complete"] = False
    with pytest.raises(ValueError, match="identity is incomplete"):
        build_closing_order_draft(
            option_position_book(_snapshot([incomplete])),
            [incomplete_symbol],
        )

    first = "NVDA  260918P00210000"
    second = "NVDA  260918P00200000"
    book = option_position_book(
        _snapshot(
            [
                _position(symbol=first, quantity=1, mark=1.0),
                _position(symbol=second, quantity=-1, mark=1.0, strike=200.0),
            ]
        )
    )
    with pytest.raises(ValueError, match="zero net position mark"):
        build_closing_order_draft(book, [first, second])


def test_position_drift_invalidates_reviewed_close() -> None:
    symbol = "NVDA  260918P00210000"
    draft = build_closing_order_draft(
        option_position_book(_snapshot([_position(symbol=symbol, quantity=2, mark=1.5)])),
        [symbol],
    )

    with pytest.raises(ValueError, match="changed from 2 to 1"):
        validate_closing_position_drift(
            draft,
            _snapshot([_position(symbol=symbol, quantity=1, mark=1.6)]),
        )


def test_validated_submission_calls_schwab_exactly_once() -> None:
    symbol = "NVDA  260918P00210000"
    snapshot = _snapshot([_position(symbol=symbol, quantity=1, mark=1.5)])
    draft = build_closing_order_draft(option_position_book(snapshot), [symbol], duration=DAY_ONLY)
    submitted: list[dict[str, object]] = []

    class Session:
        def submit_order(self, payload: dict[str, object]) -> str:
            submitted.append(payload)
            return "/accounts/test/orders/42"

    result = submit_validated_closing_order(
        draft,
        snapshot_loader=lambda: snapshot,
        session_factory=Session,
    )

    assert len(submitted) == 1
    assert result.location == "/accounts/test/orders/42"


def test_drift_prevents_any_submission_call() -> None:
    symbol = "NVDA  260918P00210000"
    draft = build_closing_order_draft(
        option_position_book(_snapshot([_position(symbol=symbol, quantity=2, mark=1.5)])),
        [symbol],
    )
    called = False

    class Session:
        def submit_order(self, _payload: dict[str, object]) -> None:
            nonlocal called
            called = True

    with pytest.raises(ValueError, match="Position drift"):
        submit_validated_closing_order(
            draft,
            snapshot_loader=lambda: _snapshot([_position(symbol=symbol, quantity=1, mark=1.5)]),
            session_factory=Session,
        )
    assert called is False


def test_working_and_recent_order_views_preserve_every_exact_leg() -> None:
    snapshot = _snapshot(
        [],
        working=[
            {
                "order_id": "1001",
                "order_status": "WORKING",
                "order_type": "NET_CREDIT",
                "complex_order_strategy_type": "VERTICAL",
                "remaining_quantity": 1,
                "limit_price": 1.25,
                "asset_type": "OPTION",
                "legs": [
                    {
                        "symbol": "NVDA  260918P00210000",
                        "instruction": "SELL_TO_CLOSE",
                        "remaining_quantity": 1,
                    },
                    {
                        "symbol": "NVDA  260918P00200000",
                        "instruction": "BUY_TO_CLOSE",
                        "remaining_quantity": 1,
                    },
                ],
            }
        ],
    )
    working = option_orders_from_snapshot(snapshot)
    recent = option_orders_from_payload(
        [
            {
                "orderId": 2002,
                "status": "FILLED",
                "enteredTime": "2026-08-03T19:00:00Z",
                "orderType": "LIMIT",
                "duration": "DAY",
                "price": 0.55,
                "orderLegCollection": [
                    {
                        "instruction": "BUY_TO_CLOSE",
                        "quantity": 2,
                        "instrument": {
                            "symbol": "NVDA  260918P00210000",
                            "assetType": "OPTION",
                        },
                    }
                ],
            }
        ]
    )

    assert working[0].order_id == "1001"
    assert [leg.symbol for leg in working[0].legs] == [
        "NVDA  260918P00210000",
        "NVDA  260918P00200000",
    ]
    assert working[0].can_cancel is True
    assert recent[0].order_id == "2002"
    assert recent[0].can_cancel is False
    assert "cannot be canceled" in str(recent[0].cancel_disabled_reason)


def test_cancel_route_targets_the_matching_current_order_object() -> None:
    def raw_order(order_id: str) -> dict[str, object]:
        return {
            "orderId": order_id,
            "status": "WORKING",
            "enteredTime": "2026-08-03T19:00:00Z",
            "orderType": "LIMIT",
            "duration": "DAY",
            "price": 0.55,
            "orderLegCollection": [
                {
                    "instruction": "BUY_TO_CLOSE",
                    "quantity": 1,
                    "instrument": {
                        "symbol": "NVDA  260918P00210000",
                        "assetType": "OPTION",
                    },
                }
            ],
        }

    orders = option_orders_from_payload(
        [
            raw_order("100"),
            raw_order("200"),
        ]
    )
    visible = {f"order-{index}": order for index, order in enumerate(orders)}

    matched = _matching_order_iid(visible, ("200",))
    assert matched is not None
    assert visible[matched].order_id == "200"
    assert _matching_order_iid(visible, ("missing",)) is None


def test_background_runner_marshals_success_and_failure_back_through_after() -> None:
    class Root:
        def __init__(self) -> None:
            self.callbacks: list[object] = []

        def after(self, _delay: int, callback: object) -> None:
            self.callbacks.append(callback)

    root = Root()
    values: list[object] = []
    thread = run_in_background(
        root,  # type: ignore[arg-type]
        lambda: 42,
        values.append,
        lambda exc: values.append(exc),
    )
    thread.join(timeout=2)
    assert values == []
    assert len(root.callbacks) == 1
    root.callbacks[0]()  # type: ignore[operator]
    assert values == [42]

    root.callbacks.clear()
    thread = run_in_background(
        root,  # type: ignore[arg-type]
        lambda: (_ for _ in ()).throw(RuntimeError("offline")),
        values.append,
        lambda exc: values.append(str(exc)),
    )
    thread.join(timeout=2)
    root.callbacks[0]()  # type: ignore[operator]
    assert values[-1] == "offline"


def _snapshot(
    rows: list[dict[str, object]],
    *,
    working: list[dict[str, object]] | None = None,
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        source="schwab",
        account_label="Schwab ••••907",
        synced_at=OBSERVED_AT,
        account_facts={
            "observed_at": OBSERVED_AT.isoformat(),
            "account_values": {
                "status": "CURRENT",
                "available_funds": 43_959.84,
                "buying_power": 51_234.56,
            },
            "positions": {
                "status": "CURRENT",
                "items": rows,
                "option_unavailable_reasons": [],
            },
            "working_orders": {
                "status": "CURRENT",
                "items": working or [],
                "active_option_orders": working or [],
            },
        },
    )


def _position(
    *,
    symbol: str,
    quantity: float,
    mark: float,
    strike: float = 210.0,
    expiration: str = "2026-09-18",
    market_value: float | None = None,
    unrealized_pnl: float | None = 0.0,
    day_pnl: float | None = 0.0,
    delta: float | None = -0.2,
    theta: float | None = -0.05,
) -> dict[str, object]:
    return {
        "status": "CURRENT",
        "symbol": symbol,
        "asset_type": "OPTION",
        "contract_multiplier": 100.0,
        "underlying_symbol": "NVDA",
        "option_type": "PUT",
        "strike": strike,
        "expiration": expiration,
        "delta": delta,
        "theta": theta,
        "net_quantity": quantity,
        "settled_quantity": quantity,
        "price": mark,
        "market_value": market_value if market_value is not None else mark * quantity * 100,
        "unrealized_pnl": unrealized_pnl,
        "day_pnl": day_pnl,
        "source_ref": f"fixture:{symbol}",
        "option_fields_complete": True,
        "option_unavailable_reasons": [],
        "unavailable_reasons": [],
    }


def _raw_schwab_position(
    *,
    symbol: str,
    asset_type: str,
    price: float,
    multiplier: float | None = None,
) -> dict[str, object]:
    instrument: dict[str, object] = {
        "symbol": symbol,
        "assetType": asset_type,
    }
    if multiplier is not None:
        instrument["multiplier"] = multiplier
    return {
        "instrument": instrument,
        "longQuantity": 1.0,
        "shortQuantity": 0.0,
        "settledLongQuantity": 1.0,
        "settledShortQuantity": 0.0,
        "marketPrice": price,
        "marketValue": price * (multiplier or 1.0),
        "costBasis": price * (multiplier or 1.0),
        "openProfitLoss": 0.0,
        "currentDayProfitLoss": 0.0,
    }
