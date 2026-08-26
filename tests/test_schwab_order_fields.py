from __future__ import annotations

import pytest

from app.services.schwab_order_fields import (
    SCHWAB_EQUITY_TIME_IN_FORCE_CHOICES,
    schwab_equity_session_duration,
    schwab_equity_tif_from_api,
    schwab_equity_tif_requires_limit_order,
)


def test_equity_time_in_force_choices_include_thinkorswim_overnight_values() -> None:
    assert SCHWAB_EQUITY_TIME_IN_FORCE_CHOICES == (
        "DAY",
        "GTC",
        "EXT",
        "GTC_EXT",
        "EXTO",
        "GTC_EXTO",
        "AM",
        "PM",
    )


@pytest.mark.parametrize(
    ("time_in_force", "expected"),
    (
        ("DAY", ("NORMAL", "DAY")),
        ("GTC", ("NORMAL", "GOOD_TILL_CANCEL")),
        ("EXT", ("SEAMLESS", "DAY")),
        ("GTC_EXT", ("SEAMLESS", "GOOD_TILL_CANCEL")),
        ("AM", ("AM", "DAY")),
        ("PM", ("PM", "DAY")),
    ),
)
def test_equity_time_in_force_maps_to_supported_trader_api_values(
    time_in_force: str,
    expected: tuple[str, str],
) -> None:
    assert schwab_equity_session_duration(time_in_force) == expected


@pytest.mark.parametrize(
    ("session", "duration", "expected"),
    (
        ("NORMAL", "DAY", "DAY"),
        ("NORMAL", "GOOD_TILL_CANCEL", "GTC"),
        ("SEAMLESS", "DAY", "EXT"),
        ("SEAMLESS", "GOOD_TILL_CANCEL", "GTC_EXT"),
        ("AM", "DAY", "AM"),
        ("PM", "DAY", "PM"),
    ),
)
def test_supported_trader_api_values_map_back_to_time_in_force(
    session: str,
    duration: str,
    expected: str,
) -> None:
    assert schwab_equity_tif_from_api(session, duration) == expected


def test_unknown_trader_api_session_duration_cannot_be_edited_as_another_tif() -> None:
    with pytest.raises(ValueError, match="unsupported session/duration"):
        schwab_equity_tif_from_api("OVERNIGHT", "DAY")


@pytest.mark.parametrize("time_in_force", ("EXTO", "GTC_EXTO"))
def test_overnight_time_in_force_fails_closed_for_trader_api(time_in_force: str) -> None:
    with pytest.raises(ValueError, match="thinkorswim-only overnight TIF"):
        schwab_equity_session_duration(time_in_force)


@pytest.mark.parametrize(
    "time_in_force",
    ("EXT", "GTC_EXT", "EXTO", "GTC_EXTO", "AM", "PM"),
)
def test_extended_hours_time_in_force_requires_limit_order(time_in_force: str) -> None:
    assert schwab_equity_tif_requires_limit_order(time_in_force) is True


@pytest.mark.parametrize("time_in_force", ("DAY", "GTC"))
def test_regular_time_in_force_does_not_require_limit_order(time_in_force: str) -> None:
    assert schwab_equity_tif_requires_limit_order(time_in_force) is False
