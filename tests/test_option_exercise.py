from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.models.option_management import OptionPositionLeg
from app.services.option_exercise import (
    EXERCISE_CAPABILITY_REASON,
    build_option_exercise_analysis,
    exercise_action_disabled_reason,
)


NOW = datetime(2026, 8, 5, 17, 0, tzinfo=timezone.utc)


def test_long_call_exercise_analysis_shows_stock_and_cash_effect_without_submission() -> None:
    leg = _leg(option_type="CALL", strike=24.0, quantity=1, mark=1.03)

    analysis = build_option_exercise_analysis(leg, underlying_price=25.50)

    assert analysis.resulting_stock_quantity == pytest.approx(100.0)
    assert analysis.strike_cash_effect == pytest.approx(-2_400.0)
    assert analysis.intrinsic_value_per_share == pytest.approx(1.50)
    assert analysis.extrinsic_value_per_share == pytest.approx(0.0)
    assert analysis.capability_reason == EXERCISE_CAPABILITY_REASON
    assert analysis.settlement is None


def test_long_put_exercise_analysis_shows_delivery_and_proceeds() -> None:
    leg = _leg(option_type="PUT", strike=30.0, quantity=2, mark=6.50)

    analysis = build_option_exercise_analysis(leg, underlying_price=24.0)

    assert analysis.resulting_stock_quantity == pytest.approx(-200.0)
    assert analysis.strike_cash_effect == pytest.approx(6_000.0)
    assert analysis.intrinsic_value_per_share == pytest.approx(6.0)
    assert analysis.extrinsic_value_per_share == pytest.approx(0.5)
    assert any("forfeit" in warning for warning in analysis.warnings)


def test_exercise_analysis_is_exact_long_leg_only_and_never_invents_underlying_values() -> None:
    long_leg = _leg(option_type="CALL", strike=24.0, quantity=1, mark=1.03)
    short_leg = replace(long_leg, symbol="WULF  260918C00026000", strike=26.0, net_quantity=-1)

    assert "per exact long leg" in str(exercise_action_disabled_reason((long_leg, short_leg)))
    assert "Short option legs" in str(exercise_action_disabled_reason((short_leg,)))
    analysis = build_option_exercise_analysis(long_leg, related_position_legs=(long_leg, short_leg))
    assert analysis.underlying_price is None
    assert analysis.intrinsic_value_per_share is None
    assert analysis.extrinsic_value_per_share is None
    assert any("other option leg" in warning for warning in analysis.warnings)


def test_expired_or_incomplete_exercise_identity_fails_closed() -> None:
    leg = _leg(option_type="CALL", strike=24.0, quantity=1, mark=1.03)
    expired = replace(leg, expiration="2025-09-18")
    incomplete = replace(leg, symbol="", close_disabled_reason="Exact OCC symbol is unavailable.")

    assert "expired" in str(exercise_action_disabled_reason((expired,))).lower()
    with pytest.raises(ValueError, match="Exact OCC symbol"):
        build_option_exercise_analysis(incomplete)


def _leg(
    *,
    option_type: str,
    strike: float,
    quantity: float,
    mark: float,
) -> OptionPositionLeg:
    return OptionPositionLeg(
        account_label="Schwab ••••1234",
        symbol=f"WULF  260918{option_type[0]}00024000",
        underlying_symbol="WULF",
        option_type=option_type,
        expiration="2026-09-18",
        strike=strike,
        net_quantity=quantity,
        settled_quantity=quantity,
        contract_multiplier=100.0,
        bid=max(0.01, mark - 0.04),
        ask=mark + 0.04,
        mark=mark,
        market_value=mark * quantity * 100,
        unrealized_pnl=3.0,
        day_pnl=1.0,
        delta=0.4,
        theta=-0.03,
        observed_at=NOW,
        quote_observed_at=NOW,
        source_ref="fixture",
    )
