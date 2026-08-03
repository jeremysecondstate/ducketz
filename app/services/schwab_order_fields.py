from __future__ import annotations

from enum import Enum


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class TimeInForce(str, Enum):
    DAY = "DAY"
    GTC = "GTC"
    EXT = "EXT"
    GTC_EXT = "GTC_EXT"
    AM = "AM"
    PM = "PM"


SCHWAB_EQUITY_SIDE_CHOICES = tuple(side.value for side in OrderSide)
SCHWAB_EQUITY_ORDER_TYPE_CHOICES = tuple(order_type.value for order_type in OrderType)
SCHWAB_EQUITY_TIME_IN_FORCE_CHOICES = tuple(tif.value for tif in TimeInForce)

SCHWAB_EQUITY_TIF_API_MAP = {
    TimeInForce.DAY: ("NORMAL", "DAY"),
    TimeInForce.GTC: ("NORMAL", "GOOD_TILL_CANCEL"),
    TimeInForce.EXT: ("SEAMLESS", "DAY"),
    TimeInForce.GTC_EXT: ("SEAMLESS", "GOOD_TILL_CANCEL"),
    TimeInForce.AM: ("AM", "DAY"),
    TimeInForce.PM: ("PM", "DAY"),
}

SCHWAB_EQUITY_LIMIT_TIFS = {
    TimeInForce.EXT,
    TimeInForce.GTC_EXT,
    TimeInForce.AM,
    TimeInForce.PM,
}

SCHWAB_OPTION_STRATEGY_CHOICES = (
    "SINGLE",
    "COVERED_STOCK",
    "VERTICAL",
    "BUTTERFLY",
    "CALENDAR",
    "DIAGONAL",
    "IRON_CONDOR",
)

SCHWAB_SUPPORTED_OPTION_STRATEGIES = {
    "SINGLE",
}


def schwab_option_strategy_is_supported(strategy: str) -> bool:
    return str(strategy).strip().upper() in SCHWAB_SUPPORTED_OPTION_STRATEGIES


def schwab_equity_session_duration(time_in_force: str) -> tuple[str, str]:
    tif = TimeInForce(str(time_in_force).strip().upper())
    return SCHWAB_EQUITY_TIF_API_MAP[tif]


def schwab_equity_tif_requires_limit_order(time_in_force: str) -> bool:
    tif = TimeInForce(str(time_in_force).strip().upper())
    return tif in SCHWAB_EQUITY_LIMIT_TIFS