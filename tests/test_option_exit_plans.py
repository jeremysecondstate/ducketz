from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.models.option_management import ManagedOptionOrder, ManagedOrderLeg, SavedExitPlanTemplate
from app.models.portfolio import PortfolioSnapshot
from app.services.option_exit_plans import (
    OCO_CAPABILITY_REASON,
    SINGLE_TARGET,
    TARGET_STOP,
    TRAILING_STOP,
    TWO_TARGETS,
    build_exit_plan_draft,
    build_exit_plan_payload,
    load_exit_plan_templates,
    save_exit_plan_template,
)
from app.services.schwab_option_management import option_position_book
from app.services.schwab_strategy_orders import GOOD_UNTIL_CANCELED


OBSERVED_AT = datetime(2026, 8, 4, 20, 0, tzinfo=timezone.utc)


def test_target_stop_resolves_fresh_mark_and_synchronizes_oco_quantities() -> None:
    symbol = "NVDA  260918P00210000"
    book = option_position_book(_snapshot([_position(symbol=symbol, quantity=1, mark=1.42)]))

    draft = build_exit_plan_draft(
        book,
        [symbol],
        template_id=TARGET_STOP,
        target_percent=25,
        stop_percent=12,
        limit_offset=0.05,
    )

    assert draft.relationship == "OCO"
    assert draft.position_mark == pytest.approx(1.42)
    assert draft.take_profit is not None
    assert draft.take_profit.trigger_price == pytest.approx(1.78)
    assert draft.stop_loss is not None
    assert draft.stop_loss.trigger_price == pytest.approx(1.25)
    assert draft.stop_loss.limit_price == pytest.approx(1.20)
    assert [branch.quantity_fraction for branch in draft.branches] == [1.0, 1.0]
    assert [branch.closing_order.legs[0].quantity for branch in draft.branches if branch.closing_order] == [1, 1]
    assert draft.executable is False
    assert draft.capability_reason == OCO_CAPABILITY_REASON
    assert any("may not fill" in warning for warning in draft.warnings)


def test_single_target_for_short_position_is_verified_limit_close() -> None:
    symbol = "NVDA  260918P00210000"
    book = option_position_book(_snapshot([_position(symbol=symbol, quantity=-2, mark=1.00)]))

    draft = build_exit_plan_draft(
        book,
        [symbol],
        template_id=SINGLE_TARGET,
        target_percent=25,
    )
    payload = build_exit_plan_payload(draft)

    assert draft.take_profit is not None
    assert draft.take_profit.trigger_operator == "−"
    assert draft.take_profit.trigger_price == pytest.approx(0.75)
    assert draft.placeable is True
    assert payload["orderType"] == "LIMIT"
    assert payload["duration"] == "GOOD_TILL_CANCEL"
    assert payload["price"] == pytest.approx(0.75)
    assert payload["orderLegCollection"][0]["instruction"] == "BUY_TO_CLOSE"
    assert payload["orderLegCollection"][0]["quantity"] == 2


def test_working_close_order_blocks_an_overlapping_exit_plan() -> None:
    symbol = "NVDA  260918P00210000"
    book = option_position_book(_snapshot([_position(symbol=symbol, quantity=1, mark=1.00)]))
    order = ManagedOptionOrder(
        order_id="4412",
        status="WORKING",
        entered_time="2026-08-04T19:00:00Z",
        order_type="LIMIT",
        complex_order_strategy_type="NONE",
        duration="GTC",
        remaining_quantity=1,
        limit_price=1.25,
        stop_price=None,
        legs=(ManagedOrderLeg(symbol=symbol, instruction="SELL_TO_CLOSE", quantity=1),),
        can_cancel=True,
        cancel_disabled_reason=None,
    )

    draft = build_exit_plan_draft(
        book,
        [symbol],
        working_orders=(order,),
        template_id=SINGLE_TARGET,
    )

    assert draft.conflicting_order_ids == ("4412",)
    assert draft.placeable is False
    with pytest.raises(ValueError, match="4412"):
        build_exit_plan_payload(draft)


def test_position_coverage_copy_distinguishes_whole_strategy_from_selected_legs() -> None:
    symbols = (
        "NVDA  260918P00210000",
        "NVDA  260918P00205000",
        "NVDA  260918P00200000",
    )
    book = option_position_book(
        _snapshot(
            [
                _position(symbol=symbol, quantity=1, mark=mark)
                for symbol, mark in zip(symbols, (1.42, 0.92, 0.55), strict=True)
            ]
        )
    )

    whole = build_exit_plan_draft(book, symbols, coverage_mode="entire")
    selected = build_exit_plan_draft(book, symbols[:2], coverage_mode="selected")
    single = build_exit_plan_draft(book, symbols[:1], coverage_mode="entire")

    assert whole.coverage_label == "Entire strategy"
    assert selected.coverage_label == "2 selected legs"
    assert single.coverage_label == "Entire position"
    with pytest.raises(ValueError, match="position coverage"):
        build_exit_plan_draft(book, symbols, coverage_mode="unknown")


@pytest.mark.parametrize(
    ("template_id", "relationship", "reason_fragment"),
    [
        (TWO_TARGETS, "SCALE_OUT", "scale-out"),
        (TRAILING_STOP, "TRAILING", "Trailing-stop"),
    ],
)
def test_advanced_templates_remain_representable_but_not_executable(
    template_id: str,
    relationship: str,
    reason_fragment: str,
) -> None:
    symbol = "NVDA  260918P00210000"
    book = option_position_book(_snapshot([_position(symbol=symbol, quantity=2, mark=1.00)]))

    draft = build_exit_plan_draft(book, [symbol], template_id=template_id)

    assert draft.relationship == relationship
    assert draft.executable is False
    assert reason_fragment in str(draft.capability_reason)
    with pytest.raises(ValueError, match="not verified"):
        build_exit_plan_payload(draft)


def test_template_persistence_is_versioned_atomic_and_contains_no_position_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "exit-plans.json"
    template = SavedExitPlanTemplate(
        name="Conservative bracket",
        base_template_id=TARGET_STOP,
        target_percent=20,
        stop_percent=8,
        limit_offset=0.05,
        duration=GOOD_UNTIL_CANCELED,
    )

    assert save_exit_plan_template(template, path) == path
    assert load_exit_plan_templates(path) == (template,)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert "account" not in path.read_text(encoding="utf-8").lower()
    assert "symbol" not in path.read_text(encoding="utf-8").lower()
    assert not path.with_name(path.name + ".tmp").exists()


def test_legacy_template_list_migrates_and_malformed_files_fail_closed(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.json"
    legacy.write_text(
        json.dumps(
            [
                {
                    "name": "Legacy target",
                    "template": SINGLE_TARGET,
                    "target": 15,
                    "stop": 10,
                    "limit_offset": 0.10,
                }
            ]
        ),
        encoding="utf-8",
    )

    loaded = load_exit_plan_templates(legacy)
    assert loaded[0].name == "Legacy target"
    assert loaded[0].base_template_id == SINGLE_TARGET
    assert loaded[0].duration == GOOD_UNTIL_CANCELED

    malformed = tmp_path / "malformed.json"
    malformed.write_text('{"schema_version": 99, "templates": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="schema version"):
        load_exit_plan_templates(malformed)


def test_resolved_short_profit_target_cannot_fall_below_one_cent() -> None:
    symbol = "NVDA  260918P00210000"
    book = option_position_book(_snapshot([_position(symbol=symbol, quantity=-1, mark=0.01)]))

    with pytest.raises(ValueError, match=r"minimum \$0.01"):
        build_exit_plan_draft(book, [symbol], template_id=SINGLE_TARGET, target_percent=25)


def _snapshot(rows: list[dict[str, object]]) -> PortfolioSnapshot:
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
                "items": [],
                "active_option_orders": [],
            },
        },
    )


def _position(*, symbol: str, quantity: float, mark: float) -> dict[str, object]:
    return {
        "status": "CURRENT",
        "symbol": symbol,
        "asset_type": "OPTION",
        "contract_multiplier": 100.0,
        "underlying_symbol": "NVDA",
        "option_type": "PUT",
        "strike": 210.0,
        "expiration": "2026-09-18",
        "delta": -0.2,
        "theta": -0.05,
        "net_quantity": quantity,
        "settled_quantity": quantity,
        "price": mark,
        "bid": max(0.0, mark - 0.05),
        "ask": mark + 0.05,
        "market_value": mark * quantity * 100,
        "unrealized_pnl": 0.0,
        "day_pnl": 0.0,
        "source_ref": f"fixture:{symbol}",
        "option_fields_complete": True,
        "option_unavailable_reasons": [],
        "unavailable_reasons": [],
    }
