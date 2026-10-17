"""Resolve an explicitly dated Gameplan source policy without rewriting old runs."""
from datetime import date

POLICY_VERSION = "gameplan-price-source-effective-date-v1"
PRICE_SOURCES = frozenset({
    "canonical-equity-minute-v1", "xnas-itch-archive-v1",
    "observed-multi-source-stock-v1", "observed-multi-source-stock-v2",
})


def _date(value):
    if not isinstance(value, str):
        raise ValueError("Gameplan source policy needs an ISO action date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Gameplan source policy needs an ISO action date") from exc
    if parsed.isoformat() != value:
        raise ValueError("Gameplan source policy needs an ISO action date")
    return parsed


def resolve_gameplan_price_source(config, action_date=None):
    """Bind one source per dated run; an absent policy preserves the old default.

    Existing completed runs before the effective date keep their original source
    and configuration binding. New runs at/after it use the explicit new source.
    This does not alter models, acquired observations, saved forecasts or gates.
    """
    base = config.get("stock_price_source", "xnas-itch-archive-v1")
    if not isinstance(base, str) or base not in PRICE_SOURCES:
        raise ValueError("Unknown base Gameplan stock price source")
    policy = config.get("stock_price_source_policy")
    selected_date = _date(action_date) if action_date is not None else None
    if policy is None:
        return base
    if (not isinstance(policy, dict)
            or set(policy) != {"version", "effective_from_action_date", "source_contract"}
            or policy["version"] != POLICY_VERSION
            or not isinstance(policy["source_contract"], str)
            or policy["source_contract"] not in PRICE_SOURCES):
        raise ValueError("Invalid versioned Gameplan price source policy")
    effective = _date(policy["effective_from_action_date"])
    return policy["source_contract"] if selected_date is not None and selected_date >= effective else base
