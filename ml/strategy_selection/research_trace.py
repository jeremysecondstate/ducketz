from __future__ import annotations

from copy import deepcopy
from typing import Mapping

from ml.strategy_selection.contracts import STRATEGY_RESEARCH_TRACE_VERSION


_TRACE: Mapping[str, object] = {
    "version": STRATEGY_RESEARCH_TRACE_VERSION,
    "sources": {
        "NYU-FUND-ML": {
            "path": "docs/edu/NYU-FUND-ML.md",
            "retained_insights": [
                "point-in-time feature availability",
                "chronological train-validation-test evaluation",
                "study predicted-edge thresholds as an evaluation dimension",
                "data-quality controls",
                "transaction-cost and robustness evaluation",
            ],
            "implemented_requirements": [
                "immutable receipt-only inputs",
                "decision-cluster chronological partitions",
                "probability-first scoring and complete deterministic rankings",
                "quote-quality and liquidity diagnostics published as features",
                "bid-ask and fee-aware outcome labels",
            ],
            "limitation": (
                "Annual cross-sectional earnings evidence does not establish "
                "GOOG options performance or intraday transferability."
            ),
        },
        "HU-ML-OPTIONS": {
            "path": "docs/edu/HU-ML-OPTIONS.md",
            "retained_insights": [
                "compare realized option outcome with market-implied cost",
                "retrain and evaluate chronologically",
                "evaluate performance as estimated edge changes",
            ],
            "implemented_requirements": [
                "exact Schwab-chain premium and liquidity features",
                "future causal chain receipts for quote-based pseudo-outcome labels",
                "calibrated profitable-outcome probability",
                "expected return reported separately from ranking probability",
                "full route ranking without an embedded trading verdict",
            ],
            "rejected_elements": [
                "synthetic Black-Scholes option prices as observed labels",
                "test-set reuse for policy selection",
                "deficit-recovery or martingale sizing",
            ],
            "limitation": (
                "The thesis is option-relevant methodology, not production "
                "evidence for Duckets, GOOG, or Schwab execution."
            ),
        },
        "UH-OPTIONS-OVERVIEW": {
            "path": "docs/edu/UH-OPTIONS-OVERVIEW.md",
            "retained_insights": [
                "multi-leg payoff algebra",
                "option valuation drivers and Greeks",
                "put-call parity and arbitrage relationships",
            ],
            "implemented_requirements": [
                "declarative leg algebra",
                "expiration-payoff max-loss validation",
                "aggregate candidate Greeks",
                "standard-contract and exact-leg checks",
            ],
            "correction": (
                "The usual American put upper bound is strike and the European "
                "put upper bound is discounted strike; the source's stock-price "
                "upper-bound statement is not used."
            ),
            "limitation": (
                "Educational payoff material is not empirical ML or execution evidence."
            ),
        },
    },
    "evidence_boundary": {
        "demonstrated_by_sources": (
            "evaluation discipline, option-cost-aware targets, and payoff mechanics"
        ),
        "must_be_demonstrated_by_duckets": (
            "route-level GOOG strategy ranking, calibration, executable fills, "
            "lifecycle performance, and economic value after all costs"
        ),
    },
}


def strategy_research_trace() -> dict[str, object]:
    """Return a copy suitable for readable manifests and design audits."""

    return deepcopy(dict(_TRACE))


__all__ = ["strategy_research_trace"]
