"""Field-level current-state evidence, separate from market event timestamps.

Confirmations describe what a current endpoint actually returned. They never
change a trade/event clock, fabricate a trade, or confer historical availability.
Stream state is scoped to one connection and is discarded on reconnect.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache
import math

VERSION = "intraday-field-market-state-v1"
QUOTE_FIELDS = ("bid", "ask", "bid_size", "ask_size", "bid_depth_volume",
                "ask_depth_volume", "bid_depth_levels", "ask_depth_levels")
OPTION_FIELDS = ("implied_volatility", "delta", "gamma", "theta", "vega", "rho",
                 "open_interest", "day_volume", "volume")
STATE_FIELDS = (*QUOTE_FIELDS, *OPTION_FIELDS, "last", "price")


def _valid(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def confirm_snapshot(row, *, confirmed_at=None, basis="authoritative_current_snapshot", fields=None):
    """Attach actual response confirmation only for numeric fields present."""
    row = dict(row)
    when = confirmed_at or datetime.now(timezone.utc).isoformat()
    present = [field for field in (fields or STATE_FIELDS) if _valid(row.get(field))]
    row["market_state"] = {"version": VERSION, "basis": basis,
        "confirmed_at": when, "field_confirmed_at": {field: when for field in present},
        "state_scope": "returned_instrument_fields_only", "connection_health": "response_received"}
    return row


@lru_cache(maxsize=256)
def _regular_bounds(day):
    import exchange_calendars as xcals
    import pandas as pd
    stamp = pd.Timestamp(day)
    calendar = xcals.get_calendar("XNAS", start=stamp-pd.Timedelta(days=7), end=stamp+pd.Timedelta(days=7))
    selected = calendar.schedule.loc[str(day):str(day)]
    if selected.empty:
        return None
    return selected.iloc[0]["open"], selected.iloc[0]["close"]


def session_status(cutoff, *, kind, schema):
    """Explicit regular/extended/closed equity state; unknown foreign sessions."""
    import pandas as pd
    stamp = pd.Timestamp(cutoff).tz_convert("America/New_York")
    if kind == "cross_asset":
        return {"status": "UNKNOWN", "basis": "no_instrument_calendar_bound"}
    bounds = _regular_bounds(stamp.date().isoformat())
    if bounds is None:
        status = "CLOSED"
    elif bounds[0] <= stamp < bounds[1]:
        status = "REGULAR"
    elif kind not in {"options", "opra_options"} and (4 <= stamp.hour < 20) and (
            bounds[1].tz_convert("America/New_York").hour == 16):
        status = "EXTENDED"
    else:
        status = "CLOSED"
    return {"status": status, "basis": "XNAS_calendar_equity_regular_and_standard_extended; options_regular_only"}


class StreamState:
    """Merge actual level-one deltas only after a two-sided native baseline.

No exchange-sequence claim is made. An incomplete first message cannot inherit
values from an older connection. Book messages replace their supplied sides;
an explicitly empty side clears that side, while an omitted side stays unknown.
"""
    def __init__(self, connection_id):
        self.connection_id, self.states = connection_id, {}

    def observe(self, service, row):
        if service.startswith("CHART_"):
            return
        key = (service, row["provider_symbol"])
        previous = self.states.get(key)
        native = row.get("raw", {})
        is_book = service.endswith("_BOOK")
        # A real snapshot with both sides establishes the baseline. Two unrelated
        # one-sided observations before any baseline are insufficient.
        baseline = (isinstance(native.get("2"), list) and isinstance(native.get("3"), list) if is_book else
                    _valid(row.get("bid")) and _valid(row.get("ask")))
        if previous is None and not baseline:
            return
        current = deepcopy(previous or row)
        confirmation = dict((current.get("market_state") or {}).get("field_confirmed_at", {}))
        when = row["stream_receipt_at"]
        if is_book:
            for side, field in (("bid", "2"), ("ask", "3")):
                if field in native:
                    for name in QUOTE_FIELDS:
                        if name == side or name.startswith(side+"_"):
                            current.pop(name, None)
                            confirmation.pop(name, None)
        else:
            option = service == "LEVELONE_OPTIONS"
            native_fields = ({"bid": "2", "ask": "3", "last": "4", "bid_size": "16", "ask_size": "17"}
                if option else {"bid": "1", "ask": "2", "last": "3", "bid_size": "4", "ask_size": "5"})
            native_fields["day_volume"] = "8"
            if option:
                native_fields.update(implied_volatility="10", open_interest="9",
                    delta="28", gamma="29", theta="30", vega="31", rho="32")
            for field, native_field in native_fields.items():
                if native_field in native and not _valid(row.get(field)):
                    current.pop(field, None)
                    confirmation.pop(field, None)
        for field, value in row.items():
            if value is not None and field not in {"raw", "market_state"}:
                current[field] = deepcopy(value)
        for field in STATE_FIELDS:
            if _valid(row.get(field)):
                confirmation[field] = when
        current["raw"] = deepcopy(native)
        current["market_state"] = {"version": VERSION, "basis": "connection_baseline_and_updates",
            "connection_id": self.connection_id,
            "baseline_at": (previous or {}).get("market_state", {}).get("baseline_at", when),
            "confirmed_at": when, "field_confirmed_at": confirmation,
            "connection_health": "receiving", "continuity_basis": "same_websocket_received_messages_only",
            "exchange_sequence_verified": False, "state_scope": "one_connection_no_gap_replay"}
        self.states[key] = current

    def checkpoint(self, accepted, *, ended_at, healthy, transport_confirmed_at=None):
        output = {}
        for (service, key), original in self.states.items():
            if key not in accepted.get(service, ()):
                continue
            row = deepcopy(original)
            state = row["market_state"]
            state.update(connection_health="closed_after_capture" if healthy else "interrupted",
                continuity_through=(transport_confirmed_at or state["confirmed_at"]) if healthy else state["confirmed_at"],
                last_validated_transport_at=(transport_confirmed_at or state["confirmed_at"]) if healthy else state["confirmed_at"],
                disconnected_at=ended_at)
            # Closing the socket does not renew any field confirmation. Evidence
            # supports current state as of the actual last instrument message.
            output.setdefault(service, []).append(row)
        return output
