"""Explicit execution policy selection; research sizing is never relabelled."""
LEARNED_SIZING_POLICY = "qualified-enrichment"
FIXED_SIZING_POLICY = "fixed-horizon-budget-v1"
SIZING_POLICIES = (LEARNED_SIZING_POLICY, FIXED_SIZING_POLICY)


def validate_sizing_policy(value: str) -> str:
    if value not in SIZING_POLICIES:
        raise ValueError("Unknown independent stock sizing policy")
    return value
