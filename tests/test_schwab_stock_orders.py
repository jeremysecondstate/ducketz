from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.schwab import SchwabSession
from app.services.schwab_stock_orders import (
    build_schwab_stock_order_payload,
    build_schwab_stock_replacement_payload,
    schwab_stock_order_edit,
)
from app.ui.ducket_bucket import SchwabDucketsTab
from app.ui.schwab_order_messages import (
    order_replacement_confirmation_message,
    order_replaced_message,
)


def _working_equity_order(**overrides: object) -> dict[str, object]:
    order: dict[str, object] = {
        "orderId": 1007713091518,
        "status": "WORKING",
        "editable": True,
        "filledQuantity": 0.0,
        "remainingQuantity": 40.0,
        "session": "SEAMLESS",
        "duration": "GOOD_TILL_CANCEL",
        "orderType": "LIMIT",
        "price": 55.9,
        "specialInstruction": "ALL_OR_NONE",
        "orderStrategyType": "SINGLE",
        "complexOrderStrategyType": "NONE",
        "orderLegCollection": [
            {
                "instruction": "SELL",
                "positionEffect": "CLOSING",
                "quantity": 40.0,
                "instrument": {
                    "symbol": "DRAM",
                    "assetType": "EQUITY",
                },
            }
        ],
    }
    order.update(overrides)
    return order


def test_stock_order_payload_honors_position_effect() -> None:
    payload = build_schwab_stock_order_payload(
        symbol="dram",
        instruction="sell",
        order_type="limit",
        time_in_force="gtc_ext",
        position_effect="closing",
        quantity="40",
        price="55.90",
    )

    assert payload == {
        "orderType": "LIMIT",
        "session": "SEAMLESS",
        "duration": "GOOD_TILL_CANCEL",
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [
            {
                "instruction": "SELL",
                "positionEffect": "CLOSING",
                "quantity": 40,
                "instrument": {
                    "symbol": "DRAM",
                    "assetType": "EQUITY",
                },
            }
        ],
        "price": "55.90",
    }


def test_stock_order_payload_maps_auto_position_effect_to_schwab_automatic() -> None:
    payload = build_schwab_stock_order_payload(
        symbol="AMZN",
        instruction="BUY",
        order_type="MARKET",
        time_in_force="DAY",
        position_effect="AUTO",
        quantity="2.0",
    )

    leg = payload["orderLegCollection"][0]
    assert leg["positionEffect"] == "AUTOMATIC"
    assert "price" not in payload
    assert "stopPrice" not in payload


def test_extended_hours_stock_order_rejects_non_limit_order_type() -> None:
    with pytest.raises(ValueError, match="must use the LIMIT order type"):
        build_schwab_stock_order_payload(
            symbol="DRAM",
            instruction="SELL",
            order_type="STOP_LIMIT",
            time_in_force="GTC_EXT",
            position_effect="CLOSING",
            quantity=40,
            price=55.9,
            stop_price=55.5,
        )


def test_working_equity_order_populates_editable_fields() -> None:
    edit = schwab_stock_order_edit(_working_equity_order())

    assert edit.order_id == "1007713091518"
    assert edit.symbol == "DRAM"
    assert edit.instruction == "SELL"
    assert edit.order_type == "LIMIT"
    assert edit.time_in_force == "GTC_EXT"
    assert edit.position_effect == "CLOSING"
    assert edit.quantity == 40
    assert edit.price == "55.9"
    assert edit.stop_price == ""
    assert edit.special_instruction == "ALL_OR_NONE"


def test_replacement_payload_changes_editable_terms_without_changing_instrument_or_side() -> None:
    edit = schwab_stock_order_edit(_working_equity_order())

    payload = build_schwab_stock_replacement_payload(
        edit,
        order_type="STOP_LIMIT",
        time_in_force="GTC",
        position_effect="AUTO",
        quantity="25",
        price="56.12",
        stop_price="55.75",
        special_instruction="NONE",
    )

    assert payload["orderType"] == "STOP_LIMIT"
    assert payload["session"] == "NORMAL"
    assert payload["duration"] == "GOOD_TILL_CANCEL"
    assert payload["price"] == "56.12"
    assert payload["stopPrice"] == "55.75"
    assert "specialInstruction" not in payload
    assert payload["orderLegCollection"] == [
        {
            "instruction": "SELL",
            "positionEffect": "AUTOMATIC",
            "quantity": 25,
            "instrument": {"symbol": "DRAM", "assetType": "EQUITY"},
        }
    ]


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"editable": False}, "not editable"),
        ({"filledQuantity": 1.0}, "partially filled"),
        ({"status": "FILLED"}, "already filled"),
        ({"childOrderStrategies": [{"orderId": 2}]}, "non-conditional"),
    ),
)
def test_unsafe_open_orders_are_not_offered_for_replacement(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        schwab_stock_order_edit(_working_equity_order(**overrides))


def test_option_order_is_not_offered_in_stock_editor() -> None:
    order = _working_equity_order(
        orderLegCollection=[
            {
                "instruction": "SELL_TO_CLOSE",
                "positionEffect": "CLOSING",
                "quantity": 1,
                "instrument": {"symbol": "DRAM  260918C00060000", "assetType": "OPTION"},
            }
        ]
    )

    with pytest.raises(ValueError, match="Select a Stock/ETF order"):
        schwab_stock_order_edit(order)


def test_schwab_session_replace_order_uses_put_and_returns_new_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Response:
        headers = {"Location": "/trader/v1/accounts/hash/orders/2002"}

        def raise_for_status(self) -> None:
            captured["raised"] = True

    def put(url: str, **kwargs: object) -> Response:
        captured.update(url=url, **kwargs)
        return Response()

    session = object.__new__(SchwabSession)
    monkeypatch.setattr(session, "_get_account_hash", lambda: "hash")
    monkeypatch.setattr(session, "_headers", lambda: {"Authorization": "Bearer test"})
    monkeypatch.setattr("app.services.schwab.requests.put", put)
    payload = {"orderType": "LIMIT"}

    location = session.replace_order(" 1001 ", payload)

    assert location == "/trader/v1/accounts/hash/orders/2002"
    assert captured == {
        "url": "https://api.schwabapi.com/trader/v1/accounts/hash/orders/1001",
        "headers": {
            "Authorization": "Bearer test",
            "Content-Type": "application/json",
        },
        "json": payload,
        "timeout": 10,
        "raised": True,
    }


class _Value:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value: object) -> None:
        self.value = str(value)


class _Table:
    def __init__(self) -> None:
        self.rows: dict[str, tuple[object, ...]] = {}
        self.selected: tuple[str, ...] = ()

    def insert(self, _parent: str, _where: str, *, values: tuple[object, ...]) -> str:
        item_id = f"row-{len(self.rows) + 1}"
        self.rows[item_id] = values
        return item_id

    def selection(self) -> tuple[str, ...]:
        return self.selected

    def selection_remove(self, *item_ids: str) -> None:
        self.selected = tuple(item_id for item_id in self.selected if item_id not in item_ids)

    def item(self, item_id: str, field: str) -> tuple[object, ...]:
        assert field == "values"
        return self.rows[item_id]


def test_selecting_rendered_order_copies_id_and_retains_open_order_for_edit() -> None:
    tab = object.__new__(SchwabDucketsTab)
    open_table = _Table()
    recent_table = _Table()
    tab.open_orders_table = open_table
    tab.recent_orders_table = recent_table
    tab.schwab_open_order_by_item_id = {}
    tab.order_id = _Value()
    tab._clear_table = lambda _table: None
    order = _working_equity_order()

    tab._show_orders(open_table, [order])
    open_table.selected = ("row-1",)
    tab._use_selected_schwab_order(SimpleNamespace(widget=open_table))

    assert tab.order_id.value == "1007713091518"
    assert tab._selected_schwab_open_order() is order


def test_replacement_messages_name_original_and_new_order_ids() -> None:
    edit = schwab_stock_order_edit(_working_equity_order())
    payload = build_schwab_stock_replacement_payload(
        edit,
        order_type="LIMIT",
        time_in_force="DAY",
        position_effect="CLOSING",
        quantity=20,
        price=56,
        stop_price="",
        special_instruction="ALL_OR_NONE",
    )

    confirmation = order_replacement_confirmation_message(edit.order_id, payload)
    succeeded = order_replaced_message(
        edit.order_id,
        payload,
        "/trader/v1/accounts/hash/orders/2002",
    )

    assert "Original order ID: 1007713091518" in confirmation
    assert "Position Effect" not in confirmation
    assert "Closing" in confirmation
    assert "Replacement order ID: 2002" in succeeded
