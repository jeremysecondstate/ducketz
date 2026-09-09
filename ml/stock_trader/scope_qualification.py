"""Observed-scope coverage for a separately qualified pooled horizon model.

Only newly fitted records declaring this policy may use this helper. Legacy
records retain their original exact-subgroup qualification policy. The caller
owns the unchanged horizon-level assessment, verified cohort/source binding,
and exact exchange-calendar target validation; this helper never recomputes or
weakens the horizon quality gate.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from numbers import Integral
import re

from ml.stock_target_prices import stock_price_dataset
from ml.stock_trader.contracts import STOCK_TRADER_SYMBOLS


POOLED_SCOPE_QUALIFICATION_VERSION = "pooled-horizon-with-observed-target-coverage-v1"
MINIMUM_POOLED_ROUTE_FIT_CLUSTERS = 20
_SCOPE = re.compile(r"([A-Z][A-Z0-9.]*)\|([^|]+)\|([1-9][0-9]*)m")
_ROUTES = {
    **{f"1h@{hour:02d}:00": "1h" for hour in range(4, 17)},
    **{f"4h@{hour:02d}:00": "4h" for hour in (4, 8, 12, 16)},
    "1d@D+1": "1d",
    "1w@D+5": "1w",
}


def _scope_identity(scope: str) -> tuple[str, str, str]:
    match = _SCOPE.fullmatch(scope) if isinstance(scope, str) else None
    if match is None or match[1] not in STOCK_TRADER_SYMBOLS or match[2] not in _ROUTES:
        raise ValueError("Pooled qualification requires canonical exact execution scope keys")
    return match[1], match[2], _ROUTES[match[2]]


def qualify_pooled_scope_coverage(
    *,
    global_ready: bool,
    fit_scope_decision_clusters: Mapping[str, int],
    scope_diagnostics: Mapping[str, Mapping[str, object]],
    target_price_source_contract: str,
) -> dict[str, dict[str, object]]:
    """Combine pooled model quality with fitted symbol/route/duration coverage.

    Each exact scope must itself have at least one fitted decision cluster.
    The same symbol and route must have at least twenty fitted clusters across
    its observed durations. These counts are disjoint by construction: the
    verified cohort contains one exact duration per symbol/route/decision.
    The caller must supply counts reconstructed from that immutable cohort.

    Exact-subgroup assessment metrics remain diagnostics. No minimum count,
    local score superiority, or within-subgroup probability variation is
    required beyond the caller's unchanged pooled horizon assessment gate.
    Diagnostics-only keys are retained as research, never newly extrapolated.
    """
    if not isinstance(global_ready, bool):
        raise ValueError("Pooled qualification needs a verified boolean horizon gate")
    if not isinstance(fit_scope_decision_clusters, Mapping) or not isinstance(scope_diagnostics, Mapping):
        raise ValueError("Pooled qualification needs fitted counts and diagnostic mappings")
    dataset = stock_price_dataset(target_price_source_contract)
    identities, counts, route_counts = {}, {}, {}
    for scope, count in fit_scope_decision_clusters.items():
        identity = _scope_identity(scope)
        if isinstance(count, bool) or not isinstance(count, Integral) or count < 0:
            raise ValueError("Pooled qualification fitted cluster counts must be nonnegative integers")
        identities[scope], counts[scope] = identity, int(count)
        route = identity[:2]
        route_counts[route] = route_counts.get(route, 0) + int(count)
    for scope, diagnostics in scope_diagnostics.items():
        identities.setdefault(scope, _scope_identity(scope))
        if not isinstance(diagnostics, Mapping):
            raise ValueError("Pooled qualification scope diagnostics must be mappings")
        declared_source = diagnostics.get("target_price_source_contract")
        if declared_source is not None and declared_source != target_price_source_contract:
            raise ValueError("Pooled qualification cannot mix target price sources")
    if len({identity[2] for identity in identities.values()}) > 1:
        raise ValueError("Pooled qualification must evaluate one horizon gate at a time")

    support = {}
    for scope in sorted(identities):
        exact_count = counts.get(scope, 0)
        pooled_count = route_counts.get(identities[scope][:2], 0)
        if exact_count < 1:
            reason = "UNSEEN_EXACT_TARGET_SCOPE"
        elif pooled_count < MINIMUM_POOLED_ROUTE_FIT_CLUSTERS:
            reason = "INSUFFICIENT_POOLED_SYMBOL_ROUTE_EVIDENCE"
        elif not global_ready:
            reason = "HELD_OUT_HORIZON_QUALITY_NOT_PROMOTED"
        else:
            reason = "HELD_OUT_POOLED_HORIZON_QUALIFIED_WITH_OBSERVED_SCOPE"
        support[scope] = {
            **deepcopy(dict(scope_diagnostics.get(scope, {}))),
            "status": "READY" if exact_count >= 1 and pooled_count >= MINIMUM_POOLED_ROUTE_FIT_CLUSTERS and global_ready else "RESEARCH",
            "reason": reason,
            "qualification_policy_version": POOLED_SCOPE_QUALIFICATION_VERSION,
            "target_price_source_contract": target_price_source_contract,
            "target_price_dataset": dataset,
            "fit_decision_clusters": exact_count,
            "pooled_symbol_route_fit_decision_clusters": pooled_count,
            "exact_scope_observed_in_fit": exact_count >= 1,
            "assessment_decision_clusters": scope_diagnostics.get(scope, {}).get("assessment_decision_clusters", 0),
        }
    return support
