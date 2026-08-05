from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.models.option_management import (
    ManagedOptionOrder,
    ManagedOrderLeg,
    SavedExitPlanTemplate,
    SavedTimeBasedExitRule,
    TimeBasedExitRule,
)
from app.models.portfolio import PortfolioSnapshot
from app.services.option_exit_plans import (
    OCO_CAPABILITY_REASON,
    SINGLE_TARGET,
    TARGET_STOP,
    TIME_EXIT_CAPABILITY_REASON,
    TRAILING_STOP,
    TWO_TARGETS,
    build_exit_plan_draft,
    build_exit_plan_payload,
    load_exit_plan_templates,
    save_exit_plan_template,
)
from app.services.option_time_exits import (
    BEFORE_EXPIRATION,
    resolve_before_expiration_time_exit,
    resolve_specific_time_exit,
    time_exit_presentation,
)
from app.services.schwab_option_management import option_position_book
from app.services.schwab_strategy_orders import DAY_ONLY, GOOD_UNTIL_CANCELED


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


@pytest.mark.parametrize(
    ("duration", "api_duration"),
    [
        (DAY_ONLY, "DAY"),
        (GOOD_UNTIL_CANCELED, "GOOD_TILL_CANCEL"),
    ],
)
def test_single_target_accepts_every_visible_time_in_force_value(
    duration: str,
    api_duration: str,
) -> None:
    symbol = "NVDA  260918P00210000"
    book = option_position_book(_snapshot([_position(symbol=symbol, quantity=1, mark=1.00)]))

    draft = build_exit_plan_draft(
        book,
        [symbol],
        template_id=SINGLE_TARGET,
        duration=duration,
    )

    assert draft.placeable is True
    assert build_exit_plan_payload(draft)["duration"] == api_duration


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
    assert payload["schema_version"] == 2
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


def test_relative_time_exit_skips_weekend_and_holiday_sessions() -> None:
    rule = resolve_before_expiration_time_exit(
        ("2026-09-08",),
        sessions_before_expiration=1,
        minutes_before_session_close=0,
        now=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    assert rule.expiration_basis == "2026-09-08"
    assert rule.trigger_at == datetime(2026, 9, 4, 20, 0, tzinfo=timezone.utc)
    assert rule.timezone_name == "America/New_York"


def test_relative_time_exit_uses_the_actual_early_close() -> None:
    rule = resolve_before_expiration_time_exit(
        ("2026-11-30",),
        sessions_before_expiration=1,
        minutes_before_session_close=30,
        now=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    assert rule.trigger_at == datetime(2026, 11, 27, 17, 30, tzinfo=timezone.utc)
    presentation = time_exit_presentation(
        rule,
        local_timezone=ZoneInfo("America/Los_Angeles"),
    )
    assert "12:30 PM EST" in presentation.resolved_time
    assert presentation.local_equivalent is not None
    assert "9:30 AM PST" in presentation.local_equivalent


def test_time_exit_uses_earliest_selected_expiration_and_fails_payload_closed() -> None:
    symbols = (
        "NVDA  261016P00210000",
        "NVDA  260918P00205000",
    )
    book = option_position_book(
        _snapshot(
            [
                _position(symbol=symbols[0], quantity=1, mark=1.42, expiration="2026-10-16"),
                _position(symbol=symbols[1], quantity=-1, mark=0.92, expiration="2026-09-18"),
            ]
        )
    )
    rule = resolve_before_expiration_time_exit(
        ("2026-10-16", "2026-09-18"),
        sessions_before_expiration=1,
        minutes_before_session_close=30,
        now=OBSERVED_AT,
    )

    draft = build_exit_plan_draft(
        book,
        symbols,
        template_id=SINGLE_TARGET,
        time_exit_rule=rule,
        now=OBSERVED_AT,
    )

    assert rule.expiration_basis == "2026-09-18"
    assert rule.selected_expirations == ("2026-09-18", "2026-10-16")
    assert draft.time_exit_rule == rule
    assert len(draft.branches) == 1
    assert draft.placeable is False
    assert TIME_EXIT_CAPABILITY_REASON in str(draft.capability_reason)
    with pytest.raises(ValueError, match="No order is scheduled or sent"):
        build_exit_plan_payload(draft)


def test_specific_time_exit_requires_future_valid_explicit_timezone_values() -> None:
    rule = resolve_specific_time_exit(
        ("2026-09-18",),
        specific_date="2026-09-17",
        specific_time="15:30",
        timezone_name="America/New_York",
        now=OBSERVED_AT,
    )
    presentation = time_exit_presentation(
        rule,
        local_timezone=ZoneInfo("America/Los_Angeles"),
    )

    assert rule.trigger_at == datetime(2026, 9, 17, 19, 30, tzinfo=timezone.utc)
    assert "America/New_York" in presentation.resolved_time
    assert presentation.local_equivalent is not None
    assert "12:30 PM PDT" in presentation.local_equivalent

    with pytest.raises(ValueError, match="in the past"):
        resolve_specific_time_exit(
            ("2026-09-18",),
            specific_date="2026-08-03",
            specific_time="15:30",
            timezone_name="America/New_York",
            now=OBSERVED_AT,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        TimeBasedExitRule(
            rule_type="SPECIFIC_DATE_TIME",
            trigger_at=datetime(2026, 9, 17, 15, 30),
            timezone_name="America/New_York",
            calendar_name=None,
            sessions_before_expiration=None,
            minutes_before_session_close=None,
            expiration_basis=None,
            selected_expirations=("2026-09-18",),
        )


@pytest.mark.parametrize(
    ("sessions", "message"),
    [
        ("", "whole number"),
        ("1.5", "whole number"),
        (0, "positive whole number"),
        (-1, "positive whole number"),
    ],
)
def test_relative_time_exit_rejects_malformed_session_values(
    sessions: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        resolve_before_expiration_time_exit(
            ("2026-09-18",),
            sessions_before_expiration=sessions,
            now=OBSERVED_AT,
        )


@pytest.mark.parametrize(
    ("specific_date", "specific_time", "timezone_name", "message"),
    [
        ("09/17/2026", "15:30", "America/New_York", "YYYY-MM-DD"),
        ("2026-09-17", "3:30 PM", "America/New_York", "HH:MM"),
        ("2026-09-17", "15:30", "", "timezone is required"),
        ("2026-09-17", "15:30", "Mars/Olympus", "not recognized"),
    ],
)
def test_specific_time_exit_rejects_malformed_values(
    specific_date: str,
    specific_time: str,
    timezone_name: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        resolve_specific_time_exit(
            ("2026-09-18",),
            specific_date=specific_date,
            specific_time=specific_time,
            timezone_name=timezone_name,
            now=OBSERVED_AT,
        )


def test_relative_time_exit_template_round_trip_and_v1_migration(tmp_path: Path) -> None:
    path = tmp_path / "timed-exit-templates.json"
    template = SavedExitPlanTemplate(
        name="Exit before expiry",
        base_template_id=SINGLE_TARGET,
        target_percent=20,
        stop_percent=10,
        limit_offset=0.05,
        duration=GOOD_UNTIL_CANCELED,
        time_exit=SavedTimeBasedExitRule(
            rule_type=BEFORE_EXPIRATION,
            sessions_before_expiration=2,
            minutes_before_session_close=30,
        ),
    )

    save_exit_plan_template(template, path)
    assert load_exit_plan_templates(path) == (template,)
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["schema_version"] == 2
    assert "trigger_at" not in path.read_text(encoding="utf-8")

    v1_path = tmp_path / "v1.json"
    v1_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "templates": [
                    {
                        "name": "Version one",
                        "base_template_id": SINGLE_TARGET,
                        "target_percent": 15,
                        "stop_percent": 8,
                        "limit_offset": 0.05,
                        "duration": GOOD_UNTIL_CANCELED,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    migrated = load_exit_plan_templates(v1_path)
    assert migrated[0].time_exit is None
    save_exit_plan_template(migrated[0], v1_path)
    assert json.loads(v1_path.read_text(encoding="utf-8"))["schema_version"] == 2


def test_absolute_time_is_rejected_from_reusable_template_storage(tmp_path: Path) -> None:
    path = tmp_path / "unsafe-absolute.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "templates": [
                    {
                        "name": "Unsafe absolute",
                        "base_template_id": SINGLE_TARGET,
                        "target_percent": 20,
                        "stop_percent": 10,
                        "limit_offset": 0.05,
                        "duration": GOOD_UNTIL_CANCELED,
                        "time_exit": {
                            "rule_type": "SPECIFIC_DATE_TIME",
                            "trigger_at": "2026-09-17T19:30:00Z",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsafe absolute timestamp"):
        load_exit_plan_templates(path)


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


def _position(
    *,
    symbol: str,
    quantity: float,
    mark: float,
    expiration: str = "2026-09-18",
) -> dict[str, object]:
    return {
        "status": "CURRENT",
        "symbol": symbol,
        "asset_type": "OPTION",
        "contract_multiplier": 100.0,
        "underlying_symbol": "NVDA",
        "option_type": "PUT",
        "strike": 210.0,
        "expiration": expiration,
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
