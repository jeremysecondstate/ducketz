from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from ml.strategy_selection.contracts import LegRule, StrategyDefinition


def _option(
    side: str,
    option_type: str,
    offset: int,
    quantity: int = 1,
    expiration: str = "FRONT",
) -> LegRule:
    return LegRule(
        asset="OPTION",
        side=side,  # type: ignore[arg-type]
        quantity=quantity,
        option_type=option_type,  # type: ignore[arg-type]
        expiration_role=expiration,  # type: ignore[arg-type]
        strike_offset=offset,
    )


def _stock(side: str = "LONG", quantity: int = 100) -> LegRule:
    return LegRule(
        asset="STOCK",
        side=side,  # type: ignore[arg-type]
        quantity=quantity,
    )


def _definition(
    name: str,
    display_name: str,
    family: str,
    *legs: LegRule,
    risk_form: str = "DEFINED_RISK",
    expiration_structure: str = "SINGLE",
    stock_requirement: str = "NONE",
    cash_requirement: str = "NORMAL_BUYING_POWER",
    lifecycle: bool = False,
    research_basis: tuple[str, ...] = ("UH",),
    notes: str = "",
) -> StrategyDefinition:
    return StrategyDefinition(
        name=name,
        display_name=display_name,
        family=family,
        legs=tuple(legs),
        risk_form=risk_form,
        expiration_structure=expiration_structure,
        stock_requirement=stock_requirement,
        cash_requirement=cash_requirement,
        lifecycle=lifecycle,
        research_basis=research_basis,
        notes=notes,
    )


_DEFINITIONS = (
    _definition("long_call", "Long Call", "LONG_PREMIUM", _option("LONG", "CALL", 0)),
    _definition("long_put", "Long Put", "LONG_PREMIUM", _option("LONG", "PUT", 0)),
    _definition(
        "long_straddle",
        "Long Straddle",
        "LONG_VOLATILITY",
        _option("LONG", "CALL", 0),
        _option("LONG", "PUT", 0),
        research_basis=("HU", "UH"),
    ),
    _definition(
        "long_strangle",
        "Long Strangle",
        "LONG_VOLATILITY",
        _option("LONG", "PUT", -1),
        _option("LONG", "CALL", 1),
        research_basis=("HU", "UH"),
    ),
    _definition(
        "covered_call",
        "Covered Call",
        "COVERED",
        _stock(),
        _option("SHORT", "CALL", 1),
        risk_form="COVERED_STOCK",
        stock_requirement="EXISTING_100_SHARES",
    ),
    _definition(
        "buy_write",
        "Buy-Write",
        "COVERED",
        _stock(),
        _option("SHORT", "CALL", 1),
        risk_form="COVERED_STOCK",
        stock_requirement="BUY_100_SHARES_ATOMICALLY",
    ),
    _definition(
        "protective_put",
        "Protective / Married Put",
        "COVERED",
        _stock(),
        _option("LONG", "PUT", -1),
        stock_requirement="EXISTING_OR_ATOMIC_100_SHARES",
    ),
    _definition(
        "collar",
        "Collar",
        "COVERED",
        _stock(),
        _option("LONG", "PUT", -1),
        _option("SHORT", "CALL", 1),
        stock_requirement="EXISTING_100_SHARES",
    ),
    _definition(
        "cash_secured_put",
        "Cash-Secured Put",
        "CASH_SECURED",
        _option("SHORT", "PUT", -1),
        risk_form="CASH_SECURED",
        cash_requirement="STRIKE_TIMES_MULTIPLIER",
    ),
    _definition(
        "covered_strangle",
        "Covered Strangle / Combination",
        "COVERED",
        _stock(),
        _option("SHORT", "PUT", -1),
        _option("SHORT", "CALL", 1),
        risk_form="COVERED_AND_CASH_SECURED",
        stock_requirement="EXISTING_100_SHARES",
        cash_requirement="PUT_STRIKE_TIMES_MULTIPLIER",
    ),
    _definition(
        "wheel",
        "Wheel",
        "LIFECYCLE",
        _option("SHORT", "PUT", -1),
        risk_form="CASH_SECURED_LIFECYCLE",
        cash_requirement="STRIKE_TIMES_MULTIPLIER",
        lifecycle=True,
        notes="Stateful CSP-to-assignment-to-covered-call lifecycle.",
    ),
    _definition(
        "bull_call_spread",
        "Bull Call Spread",
        "VERTICAL",
        _option("LONG", "CALL", 0),
        _option("SHORT", "CALL", 1),
    ),
    _definition(
        "bear_put_spread",
        "Bear Put Spread",
        "VERTICAL",
        _option("LONG", "PUT", 0),
        _option("SHORT", "PUT", -1),
    ),
    _definition(
        "bull_put_spread",
        "Bull Put Spread",
        "VERTICAL",
        _option("LONG", "PUT", -1),
        _option("SHORT", "PUT", 0),
    ),
    _definition(
        "bear_call_spread",
        "Bear Call Spread",
        "VERTICAL",
        _option("SHORT", "CALL", 0),
        _option("LONG", "CALL", 1),
    ),
    _definition(
        "long_call_butterfly",
        "Long Call Butterfly",
        "BUTTERFLY",
        _option("LONG", "CALL", -1),
        _option("SHORT", "CALL", 0, 2),
        _option("LONG", "CALL", 1),
    ),
    _definition(
        "long_put_butterfly",
        "Long Put Butterfly",
        "BUTTERFLY",
        _option("LONG", "PUT", -1),
        _option("SHORT", "PUT", 0, 2),
        _option("LONG", "PUT", 1),
    ),
    _definition(
        "short_call_butterfly",
        "Short / Reverse Call Butterfly",
        "BUTTERFLY",
        _option("SHORT", "CALL", -1),
        _option("LONG", "CALL", 0, 2),
        _option("SHORT", "CALL", 1),
    ),
    _definition(
        "short_put_butterfly",
        "Short / Reverse Put Butterfly",
        "BUTTERFLY",
        _option("SHORT", "PUT", -1),
        _option("LONG", "PUT", 0, 2),
        _option("SHORT", "PUT", 1),
    ),
    _definition(
        "iron_butterfly",
        "Iron Butterfly",
        "BUTTERFLY",
        _option("LONG", "PUT", -1),
        _option("SHORT", "PUT", 0),
        _option("SHORT", "CALL", 0),
        _option("LONG", "CALL", 1),
    ),
    _definition(
        "reverse_iron_butterfly",
        "Reverse Iron Butterfly",
        "BUTTERFLY",
        _option("SHORT", "PUT", -1),
        _option("LONG", "PUT", 0),
        _option("LONG", "CALL", 0),
        _option("SHORT", "CALL", 1),
    ),
    _definition(
        "long_call_condor",
        "Long Call Condor",
        "CONDOR",
        _option("LONG", "CALL", -2),
        _option("SHORT", "CALL", -1),
        _option("SHORT", "CALL", 1),
        _option("LONG", "CALL", 2),
    ),
    _definition(
        "long_put_condor",
        "Long Put Condor",
        "CONDOR",
        _option("LONG", "PUT", -2),
        _option("SHORT", "PUT", -1),
        _option("SHORT", "PUT", 1),
        _option("LONG", "PUT", 2),
    ),
    _definition(
        "iron_condor",
        "Iron Condor",
        "CONDOR",
        _option("LONG", "PUT", -2),
        _option("SHORT", "PUT", -1),
        _option("SHORT", "CALL", 1),
        _option("LONG", "CALL", 2),
    ),
    _definition(
        "reverse_iron_condor",
        "Reverse Iron Condor",
        "CONDOR",
        _option("SHORT", "PUT", -2),
        _option("LONG", "PUT", -1),
        _option("LONG", "CALL", 1),
        _option("SHORT", "CALL", 2),
    ),
    _definition(
        "long_call_calendar",
        "Long Call Calendar",
        "CALENDAR",
        _option("SHORT", "CALL", 0, expiration="FRONT"),
        _option("LONG", "CALL", 0, expiration="BACK"),
        expiration_structure="MULTI",
        risk_form="PATH_DEPENDENT_DEFINED_STRUCTURE",
    ),
    _definition(
        "long_put_calendar",
        "Long Put Calendar",
        "CALENDAR",
        _option("SHORT", "PUT", 0, expiration="FRONT"),
        _option("LONG", "PUT", 0, expiration="BACK"),
        expiration_structure="MULTI",
        risk_form="PATH_DEPENDENT_DEFINED_STRUCTURE",
    ),
    _definition(
        "bull_call_diagonal",
        "Bull Call Diagonal",
        "DIAGONAL",
        _option("SHORT", "CALL", 1, expiration="FRONT"),
        _option("LONG", "CALL", 0, expiration="BACK"),
        expiration_structure="MULTI",
        risk_form="PATH_DEPENDENT_DEFINED_STRUCTURE",
    ),
    _definition(
        "poor_mans_covered_call",
        "Poor Man's Covered Call",
        "DIAGONAL",
        _option("SHORT", "CALL", 1, expiration="FRONT"),
        _option("LONG", "CALL", -2, expiration="BACK"),
        expiration_structure="MULTI",
        risk_form="PATH_DEPENDENT_DEFINED_STRUCTURE",
    ),
    _definition(
        "bear_put_diagonal",
        "Bear Put Diagonal",
        "DIAGONAL",
        _option("SHORT", "PUT", -1, expiration="FRONT"),
        _option("LONG", "PUT", 0, expiration="BACK"),
        expiration_structure="MULTI",
        risk_form="PATH_DEPENDENT_DEFINED_STRUCTURE",
    ),
    _definition(
        "double_diagonal",
        "Double Diagonal",
        "DIAGONAL",
        _option("LONG", "PUT", -2, expiration="BACK"),
        _option("SHORT", "PUT", -1, expiration="FRONT"),
        _option("SHORT", "CALL", 1, expiration="FRONT"),
        _option("LONG", "CALL", 2, expiration="BACK"),
        expiration_structure="MULTI",
        risk_form="PATH_DEPENDENT_DEFINED_STRUCTURE",
    ),
    _definition(
        "call_ratio_backspread",
        "Call Ratio Backspread",
        "RATIO_BACKSPREAD",
        _option("SHORT", "CALL", 0),
        _option("LONG", "CALL", 1, 2),
    ),
    _definition(
        "put_ratio_backspread",
        "Put Ratio Backspread",
        "RATIO_BACKSPREAD",
        _option("SHORT", "PUT", 0),
        _option("LONG", "PUT", -1, 2),
    ),
    _definition(
        "stock_repair_covered_ratio",
        "Stock Repair / Covered Ratio",
        "COVERED_RATIO",
        _stock(),
        _option("LONG", "CALL", 0),
        _option("SHORT", "CALL", 1, 2),
        risk_form="COVERED_STOCK",
        stock_requirement="EXISTING_100_SHARES",
    ),
    _definition(
        "box_spread",
        "Box Spread",
        "DEFINED_RISK",
        _option("LONG", "CALL", -1),
        _option("SHORT", "CALL", 1),
        _option("LONG", "PUT", 1),
        _option("SHORT", "PUT", -1),
    ),
    _definition(
        "reaccelerating_bull",
        "Reaccelerating Bull",
        "CUSTOM",
        _option("LONG", "CALL", -1),
        _option("SHORT", "CALL", 0),
        _option("LONG", "CALL", 1),
    ),
    _definition(
        "phoenix_collar",
        "Phoenix Collar",
        "CUSTOM",
        _stock(),
        _option("LONG", "PUT", -1),
        _option("SHORT", "CALL", 1),
        _option("LONG", "CALL", 2),
        stock_requirement="EXISTING_100_SHARES",
    ),
    _definition(
        "twin_peak_fly",
        "Twin-Peak Fly",
        "CUSTOM",
        _option("LONG", "CALL", -2),
        _option("SHORT", "CALL", -1, 2),
        _option("LONG", "CALL", 0, 2),
        _option("SHORT", "CALL", 1, 2),
        _option("LONG", "CALL", 2),
    ),
    _definition(
        "crash_and_squeeze_barbell",
        "Crash-and-Squeeze Barbell",
        "CUSTOM",
        _option("LONG", "PUT", 0),
        _option("SHORT", "PUT", -1),
        _option("SHORT", "CALL", 0),
        _option("LONG", "CALL", 1, 2),
    ),
    _definition(
        "range_to_trend_relay",
        "Range-to-Trend Relay",
        "LIFECYCLE",
        _option("LONG", "PUT", -2, expiration="FRONT"),
        _option("SHORT", "PUT", -1, expiration="FRONT"),
        _option("SHORT", "CALL", 1, expiration="FRONT"),
        _option("LONG", "CALL", 2, expiration="FRONT"),
        _option("LONG", "PUT", -1, expiration="BACK"),
        _option("LONG", "CALL", 1, expiration="BACK"),
        expiration_structure="MULTI",
        risk_form="CONTINGENT_LIFECYCLE",
        lifecycle=True,
        notes="Near iron-condor phase followed by a governed later strangle phase.",
    ),
)

STRATEGY_REGISTRY: Mapping[str, StrategyDefinition] = MappingProxyType(
    {definition.name: definition for definition in _DEFINITIONS}
)


def validate_strategy_registry() -> None:
    if len(STRATEGY_REGISTRY) != len(_DEFINITIONS):
        raise ValueError("Strategy registry contains duplicate names")
    for definition in STRATEGY_REGISTRY.values():
        if definition.risk_form == "UNLIMITED_UNCOVERED":
            raise ValueError(
                f"Strategy {definition.name} would require uncovered approval"
            )
        if definition.has_short_option and definition.risk_form not in {
            "DEFINED_RISK",
            "COVERED_STOCK",
            "CASH_SECURED",
            "COVERED_AND_CASH_SECURED",
            "CASH_SECURED_LIFECYCLE",
            "PATH_DEPENDENT_DEFINED_STRUCTURE",
            "CONTINGENT_LIFECYCLE",
        }:
            raise ValueError(
                f"Short-option strategy {definition.name} lacks an approved coverage form"
            )


validate_strategy_registry()


__all__ = ["STRATEGY_REGISTRY", "validate_strategy_registry"]
