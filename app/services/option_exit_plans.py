from __future__ import annotations

import json
import math
import os
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from app.models.option_management import (
    ExitPlanBranch,
    ExitPlanDraft,
    ManagedOptionOrder,
    OptionPositionBook,
    SavedExitPlanTemplate,
    SavedTimeBasedExitRule,
    TimeBasedExitRule,
)
from app.models.portfolio import PortfolioSnapshot
from app.services.schwab_option_management import (
    build_closing_order_draft,
    build_closing_order_payload,
    option_orders_from_snapshot,
    option_position_book,
    validate_closing_position_drift,
)
from app.services.schwab_strategy_orders import DAY_ONLY, GOOD_UNTIL_CANCELED
from app.services.option_time_exits import (
    BEFORE_EXPIRATION,
    TIME_EXIT_CAPABILITY_REASON,
    validate_time_exit_rule,
)


TARGET_STOP = "target_stop"
SINGLE_TARGET = "single_target"
TWO_TARGETS = "two_targets"
TRAILING_STOP = "trailing_stop"
EXIT_PLAN_TEMPLATE_IDS = (TARGET_STOP, SINGLE_TARGET, TWO_TARGETS, TRAILING_STOP)
EXIT_TEMPLATE_SCHEMA_VERSION = 2

OCO_CAPABILITY_REASON = (
    "Schwab linked OCO/child option-order submission is not verified in this project. "
    "The plan can be reviewed, but it cannot be placed."
)
TWO_TARGET_CAPABILITY_REASON = (
    "Two-target scale-out linkage and partial strategy quantities are not verified for Schwab option orders."
)
TRAILING_STOP_CAPABILITY_REASON = (
    "Trailing-stop option orders are not verified for the Schwab adapter."
)

TEMPLATE_LABELS: Mapping[str, str] = {
    TARGET_STOP: "Target + stop",
    SINGLE_TARGET: "Single target",
    TWO_TARGETS: "2 targets",
    TRAILING_STOP: "Trailing stop",
}


def build_exit_plan_draft(
    book: OptionPositionBook,
    selected_symbols: Iterable[str],
    *,
    working_orders: Sequence[ManagedOptionOrder] = (),
    coverage_mode: str = "entire",
    template_id: str = TARGET_STOP,
    target_percent: object = 25.0,
    stop_percent: object = 12.0,
    limit_offset: object = 0.05,
    duration: str = GOOD_UNTIL_CANCELED,
    time_exit_rule: TimeBasedExitRule | None = None,
    now: datetime | None = None,
) -> ExitPlanDraft:
    if template_id not in EXIT_PLAN_TEMPLATE_IDS:
        raise ValueError(f"Unknown exit-plan template: {template_id or 'missing'}")
    if coverage_mode not in {"entire", "selected"}:
        raise ValueError(f"Unknown position coverage: {coverage_mode or 'missing'}")
    target_pct = _percent(target_percent, "Profit target")
    stop_pct = _percent(stop_percent, "Stop loss")
    offset = _nonnegative_price(limit_offset, "Stop-limit offset")
    if duration not in {DAY_ONLY, GOOD_UNTIL_CANCELED}:
        raise ValueError(f"Unsupported exit-plan duration: {duration or 'missing'}")

    symbols = tuple(
        dict.fromkeys(
            str(symbol).strip().upper()
            for symbol in selected_symbols
            if str(symbol).strip()
        )
    )
    base_close = build_closing_order_draft(book, symbols, duration=duration)
    if time_exit_rule is not None:
        validate_time_exit_rule(
            time_exit_rule,
            selected_expirations=(leg.expiration for leg in base_close.legs),
            now=now or datetime.now(timezone.utc),
        )
    current_mark = base_close.limit_price
    cash_direction = 1.0 if base_close.estimated_cash_effect >= 0 else -1.0
    target_price = _resolved_price(
        current_mark * (1.0 + cash_direction * target_pct / 100.0),
        "Profit target",
    )
    stop_price = _resolved_price(
        current_mark * (1.0 - cash_direction * stop_pct / 100.0),
        "Stop trigger",
    )
    stop_limit_price = _resolved_price(
        stop_price - cash_direction * offset,
        "Stop-limit price",
    )
    target_close = build_closing_order_draft(
        book,
        symbols,
        duration=duration,
        limit_price=target_price,
    )
    stop_close = build_closing_order_draft(
        book,
        symbols,
        duration=duration,
        limit_price=stop_limit_price,
    )
    conflicts = conflicting_closing_order_ids(working_orders, symbols)
    target_branch = ExitPlanBranch(
        branch_id="target",
        label="Take profit",
        enabled=True,
        trigger_basis="Position mark",
        trigger_operator="+" if cash_direction > 0 else "−",
        trigger_percent=target_pct,
        trigger_price=target_price,
        order_type="LIMIT",
        limit_price=target_price,
        limit_offset=None,
        duration=duration,
        quantity_fraction=1.0,
        closing_order=target_close,
    )
    stop_branch = ExitPlanBranch(
        branch_id="stop",
        label="Stop loss",
        enabled=True,
        trigger_basis="Position mark",
        trigger_operator="−" if cash_direction > 0 else "+",
        trigger_percent=stop_pct,
        trigger_price=stop_price,
        order_type="STOP_LIMIT",
        limit_price=stop_limit_price,
        limit_offset=offset,
        duration=duration,
        quantity_fraction=1.0,
        closing_order=stop_close,
    )

    if template_id == SINGLE_TARGET:
        branches = (target_branch,)
        relationship = "SINGLE"
        executable = True
        capability_reason = None
    elif template_id == TARGET_STOP:
        branches = (target_branch, stop_branch)
        relationship = "OCO"
        executable = False
        capability_reason = OCO_CAPABILITY_REASON
    elif template_id == TWO_TARGETS:
        second_target_pct = min(target_pct * 2.0, 99.0)
        second_target_price = _resolved_price(
            current_mark * (1.0 + cash_direction * second_target_pct / 100.0),
            "Second profit target",
        )
        branches = (
            _without_order(target_branch, branch_id="target_1", label="First target", fraction=0.5),
            ExitPlanBranch(
                branch_id="target_2",
                label="Second target",
                enabled=True,
                trigger_basis="Position mark",
                trigger_operator=target_branch.trigger_operator,
                trigger_percent=second_target_pct,
                trigger_price=second_target_price,
                order_type="LIMIT",
                limit_price=second_target_price,
                limit_offset=None,
                duration=duration,
                quantity_fraction=0.5,
                closing_order=None,
            ),
        )
        relationship = "SCALE_OUT"
        executable = False
        capability_reason = TWO_TARGET_CAPABILITY_REASON
    else:
        branches = (
            ExitPlanBranch(
                branch_id="trailing_stop",
                label="Trailing stop",
                enabled=True,
                trigger_basis="Position mark",
                trigger_operator="TRAIL",
                trigger_percent=stop_pct,
                trigger_price=None,
                order_type="TRAILING_STOP",
                limit_price=None,
                limit_offset=None,
                duration=duration,
                quantity_fraction=1.0,
                closing_order=None,
            ),
        )
        relationship = "TRAILING"
        executable = False
        capability_reason = TRAILING_STOP_CAPABILITY_REASON

    if time_exit_rule is not None:
        executable = False
        capability_reason = " ".join(
            dict.fromkeys(
                reason
                for reason in (capability_reason, TIME_EXIT_CAPABILITY_REASON)
                if reason
            )
        )

    warnings = [
        (
            f"Trigger percentages resolve from the current net position mark of ${current_mark:,.2f}; "
            "a refresh can change the resolved prices."
        )
    ]
    if template_id == TARGET_STOP:
        warnings.append("Stop-limit orders may not fill during fast moves or price gaps.")
    if capability_reason:
        warnings.append(capability_reason)
    if conflicts:
        warnings.append(
            "Working close order conflict: resolve order "
            + ", ".join(conflicts)
            + " before activating another exit."
        )

    underlying = base_close.legs[0].underlying_symbol if base_close.legs else ""
    if coverage_mode == "selected":
        coverage_label = f"{len(symbols)} selected leg{'s' if len(symbols) != 1 else ''}"
    elif len(symbols) > 1:
        coverage_label = "Entire strategy"
    else:
        coverage_label = "Entire position"
    return ExitPlanDraft(
        template_id=template_id,
        template_name=TEMPLATE_LABELS[template_id],
        account_label=book.account_label,
        underlying_symbol=underlying,
        coverage_label=coverage_label,
        position_symbols=symbols,
        position_mark=current_mark,
        price_source=base_close.price_source,
        protected_quantity=base_close.order_quantity,
        relationship=relationship,
        branches=branches,
        executable=executable,
        capability_reason=capability_reason,
        conflicting_order_ids=conflicts,
        warnings=tuple(warnings),
        time_exit_rule=time_exit_rule,
    )


def build_exit_plan_payload(draft: ExitPlanDraft) -> dict[str, object]:
    if draft.time_exit_rule is not None:
        raise ValueError(TIME_EXIT_CAPABILITY_REASON)
    if draft.conflicting_order_ids:
        raise ValueError(
            "Resolve conflicting working close order(s) before placement: "
            + ", ".join(draft.conflicting_order_ids)
        )
    if not draft.executable:
        raise ValueError(draft.capability_reason or "This exit-plan shape is not executable.")
    if draft.template_id != SINGLE_TARGET or len(draft.branches) != 1:
        raise ValueError("Only the verified single-target plan can be converted to a Schwab payload.")
    closing_order = draft.branches[0].closing_order
    if closing_order is None:
        raise ValueError("The single-target plan does not contain a verified closing order.")
    return build_closing_order_payload(closing_order)


def refresh_exit_plan_draft(
    draft: ExitPlanDraft,
    latest_snapshot: PortfolioSnapshot,
) -> ExitPlanDraft:
    """Rebuild a verified single-target plan from current option/account facts."""

    if draft.time_exit_rule is not None:
        raise ValueError(TIME_EXIT_CAPABILITY_REASON)
    if draft.template_id != SINGLE_TARGET or len(draft.branches) != 1:
        raise ValueError(draft.capability_reason or "Only a single-target exit can be refreshed for placement.")
    branch = draft.branches[0]
    closing_order = branch.closing_order
    if closing_order is None or branch.trigger_percent is None:
        raise ValueError("The single-target exit does not contain a complete closing instruction.")
    validate_closing_position_drift(closing_order, latest_snapshot)
    latest_book = option_position_book(latest_snapshot)
    if latest_book.status != "CURRENT":
        raise ValueError("Current Schwab option positions are unavailable or incomplete; review again.")
    coverage_mode = "selected" if "selected" in draft.coverage_label.casefold() else "entire"
    return build_exit_plan_draft(
        latest_book,
        draft.position_symbols,
        working_orders=option_orders_from_snapshot(latest_snapshot),
        coverage_mode=coverage_mode,
        template_id=SINGLE_TARGET,
        target_percent=branch.trigger_percent,
        duration=branch.duration,
    )


def conflicting_closing_order_ids(
    orders: Sequence[ManagedOptionOrder],
    selected_symbols: Iterable[str],
) -> tuple[str, ...]:
    selected = {str(symbol).strip().upper() for symbol in selected_symbols if str(symbol).strip()}
    conflicts: list[str] = []
    for order in orders:
        overlapping = any(
            leg.symbol.strip().upper() in selected and leg.instruction.strip().upper().endswith("_TO_CLOSE")
            for leg in order.legs
        )
        if overlapping:
            conflicts.append(order.order_id or "unknown")
    return tuple(dict.fromkeys(conflicts))


def default_exit_template_path() -> Path:
    configured = os.getenv("DUCKETS_OPTION_EXIT_TEMPLATES_PATH", "").strip()
    return Path(configured) if configured else Path("data") / "option_exit_templates.json"


def load_exit_plan_templates(path: Path | None = None) -> tuple[SavedExitPlanTemplate, ...]:
    template_path = path or default_exit_template_path()
    if not template_path.exists():
        return ()
    try:
        payload = json.loads(template_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Exit-plan template file is unreadable: {exc}") from exc

    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, Mapping):
        version = payload.get("schema_version")
        if version not in {1, EXIT_TEMPLATE_SCHEMA_VERSION}:
            raise ValueError(f"Unsupported exit-plan template schema version: {version!r}")
        rows = payload.get("templates")
    else:
        raise ValueError("Exit-plan template file must contain an object or legacy list.")
    if not isinstance(rows, list):
        raise ValueError("Exit-plan template file has no template list.")
    return tuple(_template_from_row(row) for row in rows)


def save_exit_plan_template(
    template: SavedExitPlanTemplate,
    path: Path | None = None,
) -> Path:
    template_path = path or default_exit_template_path()
    validated = _template_from_row(asdict(template))
    existing = list(load_exit_plan_templates(template_path))
    existing = [item for item in existing if item.name.casefold() != validated.name.casefold()]
    existing.append(validated)
    payload = {
        "schema_version": EXIT_TEMPLATE_SCHEMA_VERSION,
        "templates": [asdict(item) for item in existing],
    }
    template_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = template_path.with_name(template_path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, template_path)
    return template_path


def _without_order(
    branch: ExitPlanBranch,
    *,
    branch_id: str,
    label: str,
    fraction: float,
) -> ExitPlanBranch:
    return ExitPlanBranch(
        branch_id=branch_id,
        label=label,
        enabled=branch.enabled,
        trigger_basis=branch.trigger_basis,
        trigger_operator=branch.trigger_operator,
        trigger_percent=branch.trigger_percent,
        trigger_price=branch.trigger_price,
        order_type=branch.order_type,
        limit_price=branch.limit_price,
        limit_offset=branch.limit_offset,
        duration=branch.duration,
        quantity_fraction=fraction,
        closing_order=None,
    )


def _template_from_row(row: object) -> SavedExitPlanTemplate:
    if not isinstance(row, Mapping):
        raise ValueError("Each saved exit-plan template must be an object.")
    name = str(row.get("name") or "").strip()
    if not name or len(name) > 80:
        raise ValueError("Saved exit-plan template name must contain 1 to 80 characters.")
    base_template = str(
        row.get("base_template_id") or row.get("template_id") or row.get("template") or ""
    ).strip()
    if base_template not in EXIT_PLAN_TEMPLATE_IDS:
        raise ValueError(f"Saved template {name!r} has an unknown base template.")
    duration = str(row.get("duration") or GOOD_UNTIL_CANCELED).strip()
    if duration not in {DAY_ONLY, GOOD_UNTIL_CANCELED}:
        raise ValueError(f"Saved template {name!r} has an unsupported duration.")
    return SavedExitPlanTemplate(
        name=name,
        base_template_id=base_template,
        target_percent=_percent(
            row.get("target_percent", row.get("target", 25.0)),
            "Saved profit target",
        ),
        stop_percent=_percent(
            row.get("stop_percent", row.get("stop", 12.0)),
            "Saved stop loss",
        ),
        limit_offset=_nonnegative_price(
            row.get("limit_offset", 0.05),
            "Saved stop-limit offset",
        ),
        duration=duration,
        time_exit=_saved_time_exit_from_row(row.get("time_exit"), template_name=name),
    )


def _saved_time_exit_from_row(
    value: object,
    *,
    template_name: str,
) -> SavedTimeBasedExitRule | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"Saved template {template_name!r} has a malformed time-based exit.")
    if value.get("trigger_at") is not None or value.get("specific_datetime") is not None:
        raise ValueError(
            f"Saved template {template_name!r} contains an unsafe absolute timestamp. "
            "Only relative before-expiration time exits are reusable."
        )
    rule_type = str(value.get("rule_type") or "").strip()
    if rule_type != BEFORE_EXPIRATION:
        raise ValueError(
            f"Saved template {template_name!r} has an unsupported time-based exit type. "
            "Only Before expiration may be saved."
        )
    sessions = _positive_integer(
        value.get("sessions_before_expiration"),
        "Saved trading sessions before expiration",
    )
    close_offset = _nonnegative_integer(
        value.get("minutes_before_session_close"),
        "Saved minutes before scheduled close",
    )
    calendar_name = str(value.get("calendar_name") or "").strip().upper()
    if not calendar_name or len(calendar_name) > 20:
        raise ValueError(f"Saved template {template_name!r} has an invalid exchange calendar.")
    return SavedTimeBasedExitRule(
        rule_type=BEFORE_EXPIRATION,
        sessions_before_expiration=sessions,
        minutes_before_session_close=close_offset,
        calendar_name=calendar_name,
    )


def _percent(value: object, label: str) -> float:
    number = _finite_number(value)
    if number is None or number <= 0 or number >= 100:
        raise ValueError(f"{label} must be greater than 0% and less than 100%.")
    return round(number, 4)


def _nonnegative_price(value: object, label: str) -> float:
    number = _finite_number(value)
    if number is None or number < 0:
        raise ValueError(f"{label} must be a nonnegative number.")
    return round(number, 2)


def _positive_integer(value: object, label: str) -> int:
    number = _integer(value, label)
    if number < 1:
        raise ValueError(f"{label} must be a positive whole number.")
    return number


def _nonnegative_integer(value: object, label: str) -> int:
    number = _integer(value, label)
    if number < 0:
        raise ValueError(f"{label} cannot be negative.")
    return number


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a whole number.")
    text = str("" if value is None else value).strip()
    try:
        number = int(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a whole number.") from exc
    if text not in {str(number), f"+{number}"}:
        raise ValueError(f"{label} must be a whole number.")
    return number


def _resolved_price(value: float, label: str) -> float:
    if not math.isfinite(value) or value < 0.01:
        raise ValueError(f"{label} resolves below the minimum $0.01 option price.")
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _finite_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


__all__ = [
    "EXIT_PLAN_TEMPLATE_IDS",
    "EXIT_TEMPLATE_SCHEMA_VERSION",
    "OCO_CAPABILITY_REASON",
    "SINGLE_TARGET",
    "TARGET_STOP",
    "TEMPLATE_LABELS",
    "TIME_EXIT_CAPABILITY_REASON",
    "TRAILING_STOP",
    "TWO_TARGETS",
    "build_exit_plan_draft",
    "build_exit_plan_payload",
    "conflicting_closing_order_ids",
    "default_exit_template_path",
    "load_exit_plan_templates",
    "refresh_exit_plan_draft",
    "save_exit_plan_template",
]
