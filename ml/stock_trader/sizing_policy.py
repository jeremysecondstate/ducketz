"""Requested execution choices and the dated permanent stock direction policy."""
from datetime import date

LEARNED_SIZING_POLICY = "qualified-enrichment"
FIXED_SIZING_POLICY = "fixed-horizon-budget-v1"
GAMEPLAN_SIZING_POLICY = "gameplan-direction-current-market-v1"
SIZING_POLICIES = (LEARNED_SIZING_POLICY, FIXED_SIZING_POLICY, GAMEPLAN_SIZING_POLICY)


def validate_sizing_policy(value: str) -> str:
    if value not in SIZING_POLICIES:
        raise ValueError("Unknown independent stock sizing policy")
    return value


PERMANENT_DIRECTION_POLICY_DATE = date(2026, 10, 19)


def effective_sizing_policy(requested: str, action_date: date | str) -> str:
    """Use the permanent direction engine while retaining historical choices.

    Callers record the requested choice separately from this effective policy;
    archived pre-policy decisions retain their original sizing behavior.
    """
    requested = validate_sizing_policy(requested)
    day = date.fromisoformat(str(action_date))
    return GAMEPLAN_SIZING_POLICY if day >= PERMANENT_DIRECTION_POLICY_DATE else requested
