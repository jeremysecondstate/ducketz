from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

import pandas as pd

from ml.contracts import (
    ACTIVE,
    AUDIT_CONTROL,
    IMPLEMENTED_BUT_QUARANTINED,
    INSUFFICIENT_COVERAGE,
    MODEL_VALUE,
    NEEDS_NORMALIZATION,
    NEEDS_POINT_IN_TIME_HISTORY,
    USABLE_NOW,
    CalculationSpec,
    FeatureSet,
    FeatureSpec,
    MLContractError,
)


MARKET_REGIME = CalculationSpec(
    source_family="mr",
    calculation_name="market-regime",
    allowed_versions=("1.2.0",),
    mode_column="regime_mode",
    allowed_modes=("FULL",),
)
BREAKOUT_PRESSURE = CalculationSpec(
    source_family="bp",
    calculation_name="breakout-pressure",
    allowed_versions=("1.1.0",),
    mode_column="calculation_mode",
    allowed_modes=("FULL",),
)

_EXACT_FRESHNESS = (("1h", "exact-decision"), ("1d", "exact-decision"), ("1w", "exact-decision"))
_QUARANTINED = IMPLEMENTED_BUT_QUARANTINED


def _technical_feature(
    *,
    family: str,
    column: str,
    aggregate: bool = False,
    confidence: bool = False,
    transform: str = "identity-v1",
) -> FeatureSpec:
    calculation = MARKET_REGIME if family == "mr" else BREAKOUT_PRESSURE
    return FeatureSpec(
        name=f"{family}__{column}",
        source_family=family,
        source_column=column,
        provider_policy="databento-canonical-adjusted-v1",
        source_timeframe="horizon-source-timeframe",
        source_grain="completed-canonical-bar",
        required_calculation_versions=calculation.allowed_versions,
        availability_rule="bar-end-plus-configured-five-minute-delay-equals-decision",
        availability_rule_version="technical-exact-v1",
        processing_delay_seconds=300,
        freshness_by_horizon=_EXACT_FRESHNESS,
        missing_policy="no-backfill-training-median-indicator-v1",
        transform_version=transform,
        coverage_policy="full-requested-symbol-history-v1",
        readiness_policy_version="technical-readiness-v1",
        audit_classification=USABLE_NOW,
        activation_status=ACTIVE,
        is_aggregate_score=aggregate,
        is_confidence_field=confidence,
    )


def _candidate_feature(
    name: str,
    *,
    source_column: str,
    horizons: tuple[str, ...],
    provider_policy: str,
    source_timeframe: str,
    source_grain: str,
    calculation_versions: tuple[str, ...],
    schema_versions: tuple[str, ...],
    availability_rule: str,
    availability_version: str,
    freshness: tuple[tuple[str, str], ...],
    missing_policy: str,
    transform: str,
    coverage_policy: str,
    classification: str,
    readiness_policy: str,
    role: str = MODEL_VALUE,
) -> FeatureSpec:
    family = name.split("__", 1)[0]
    return FeatureSpec(
        name=name,
        source_family=family,
        source_column=source_column,
        applicable_horizons=horizons,
        provider_policy=provider_policy,
        source_timeframe=source_timeframe,
        source_grain=source_grain,
        required_calculation_versions=calculation_versions,
        required_schema_versions=schema_versions,
        availability_rule=availability_rule,
        availability_rule_version=availability_version,
        processing_delay_seconds=300 if availability_version.startswith(("technical", "weekly")) else 0,
        freshness_by_horizon=freshness,
        missing_policy=missing_policy,
        transform_version=transform,
        coverage_policy=coverage_policy,
        readiness_policy_version=readiness_policy,
        audit_classification=classification,
        activation_status=_QUARANTINED,
        value_role=role,
    )


RAW_COMPONENTS = (
    _technical_feature(family="mr", column="trend_atr"),
    _technical_feature(family="mr", column="momentum_risk_adjusted"),
    _technical_feature(family="mr", column="range_position"),
    _technical_feature(family="mr", column="volume_score"),
    _technical_feature(family="mr", column="volatility_ratio"),
    _technical_feature(family="bp", column="compression_score"),
    _technical_feature(family="bp", column="range_contraction_score"),
    _technical_feature(family="bp", column="direction_score"),
    _technical_feature(family="bp", column="upside_pressure_score"),
    _technical_feature(family="bp", column="downside_pressure_score"),
    _technical_feature(family="bp", column="breakout_magnitude_atr"),
    _technical_feature(family="bp", column="volume_participation_score"),
)

AGGREGATE_SCORES = (
    _technical_feature(family="mr", column="technical_score", aggregate=True),
    _technical_feature(family="mr", column="regime_strength", aggregate=True),
    _technical_feature(
        family="bp", column="breakout_readiness_score", aggregate=True
    ),
    _technical_feature(
        family="bp", column="breakout_strength_score", aggregate=True
    ),
    _technical_feature(family="bp", column="setup_quality", aggregate=True),
)

CONFIDENCE_SCORES = (
    _technical_feature(
        family="mr",
        column="confidence_score",
        aggregate=True,
        confidence=True,
    ),
    _technical_feature(
        family="bp",
        column="confidence_score",
        aggregate=True,
        confidence=True,
    ),
)

PHASE1_TECHNICAL_FEATURES = (
    _technical_feature(family="mr", column="atr_percent"),
    _technical_feature(family="mr", column="technical_score_change_5"),
    _technical_feature(
        family="mr",
        column="bars_since_regime_change",
        transform="log1p-nonnegative-v1",
    ),
    _technical_feature(family="mr", column="component_agreement"),
    _technical_feature(family="bp", column="readiness_change_5"),
    _technical_feature(
        family="bp",
        column="bars_since_state_change",
        transform="log1p-nonnegative-v1",
    ),
    _technical_feature(
        family="mr",
        column="realized_volatility_effective",
        transform="log1p-nonnegative-v1",
    ),
    _technical_feature(family="mr", column="return_effective"),
)

LOOP_A_TECHNICAL_COMPONENT_FEATURES = (
    _technical_feature(family="mr", column="trend_score"),
    _technical_feature(family="mr", column="momentum_score"),
    _technical_feature(family="mr", column="range_score"),
    _technical_feature(family="bp", column="boundary_proximity_score"),
)

_TECHNICAL_BY_NAME = {
    feature.name: feature
    for feature in (
        *RAW_COMPONENTS,
        *AGGREGATE_SCORES,
        *CONFIDENCE_SCORES,
        *PHASE1_TECHNICAL_FEATURES,
        *LOOP_A_TECHNICAL_COMPONENT_FEATURES,
    )
}
TECHNICAL_ALL_FEATURES = (*RAW_COMPONENTS, *AGGREGATE_SCORES, *CONFIDENCE_SCORES)

TECHNICAL_V2_1H_FEATURES = (
    *TECHNICAL_ALL_FEATURES,
    _TECHNICAL_BY_NAME["mr__atr_percent"],
    _TECHNICAL_BY_NAME["mr__technical_score_change_5"],
    _TECHNICAL_BY_NAME["bp__bars_since_state_change"],
)
TECHNICAL_V2_1D_FEATURES = (
    *TECHNICAL_ALL_FEATURES,
    _TECHNICAL_BY_NAME["mr__atr_percent"],
    _TECHNICAL_BY_NAME["mr__technical_score_change_5"],
    _TECHNICAL_BY_NAME["bp__readiness_change_5"],
)
TECHNICAL_V2_1W_FEATURES = (
    *TECHNICAL_ALL_FEATURES,
    _TECHNICAL_BY_NAME["mr__atr_percent"],
    _TECHNICAL_BY_NAME["mr__technical_score_change_5"],
    _TECHNICAL_BY_NAME["mr__bars_since_regime_change"],
)


BAR_SHAPE_FEATURES = (
    _candidate_feature(
        "bar__overnight_gap_atr",
        source_column="overnight_gap_atr",
        horizons=("1d", "1w"),
        provider_policy="databento-canonical-adjusted-v1",
        source_timeframe="horizon-source-timeframe",
        source_grain="completed-canonical-bar",
        calculation_versions=("1.0.0",),
        schema_versions=("bar-shape-v1",),
        availability_rule="bar-end-plus-five-minute-delay-equals-decision",
        availability_version="technical-exact-v1",
        freshness=(("1d", "exact-decision"), ("1w", "exact-decision")),
        missing_policy="missing-prior-close-no-backfill-v1",
        transform="identity-v1",
        coverage_policy="complete-safe-bars-all-requested-symbols-v1",
        classification=NEEDS_NORMALIZATION,
        readiness_policy="bar-shape-readiness-v1",
    ),
    _candidate_feature(
        "bar__intrabar_range_atr",
        source_column="intrabar_range_atr",
        horizons=("1h", "1d", "1w"),
        provider_policy="databento-canonical-adjusted-v1",
        source_timeframe="horizon-source-timeframe",
        source_grain="completed-canonical-bar",
        calculation_versions=("1.0.0",),
        schema_versions=("bar-shape-v1",),
        availability_rule="bar-end-plus-five-minute-delay-equals-decision",
        availability_version="technical-exact-v1",
        freshness=_EXACT_FRESHNESS,
        missing_policy="undefined-atr-missing-no-backfill-v1",
        transform="identity-v1",
        coverage_policy="complete-safe-bars-all-requested-symbols-v1",
        classification=NEEDS_NORMALIZATION,
        readiness_policy="bar-shape-readiness-v1",
    ),
    _candidate_feature(
        "bar__close_location",
        source_column="close_location",
        horizons=("1h", "1d", "1w"),
        provider_policy="databento-canonical-adjusted-v1",
        source_timeframe="horizon-source-timeframe",
        source_grain="completed-canonical-bar",
        calculation_versions=("1.0.0",),
        schema_versions=("bar-shape-v1",),
        availability_rule="bar-end-plus-five-minute-delay-equals-decision",
        availability_version="technical-exact-v1",
        freshness=_EXACT_FRESHNESS,
        missing_policy="zero-range-missing-no-backfill-v1",
        transform="identity-v1",
        coverage_policy="complete-safe-bars-all-requested-symbols-v1",
        classification=NEEDS_NORMALIZATION,
        readiness_policy="bar-shape-readiness-v1",
    ),
)


def _weekly(name: str, column: str) -> FeatureSpec:
    return _candidate_feature(
        name,
        source_column=column,
        horizons=("1d", "1w"),
        provider_policy="canonical-databento-daily-aggregation-v1",
        source_timeframe="exchange-week",
        source_grain="completed-exchange-week",
        calculation_versions=("1.0.0",),
        schema_versions=("weekly-context-v1",),
        availability_rule="final-eligible-exchange-session-close-plus-five-minutes",
        availability_version="weekly-final-session-v1",
        freshness=(("1d", "8-calendar-days"), ("1w", "8-calendar-days")),
        missing_policy="missing-until-first-completed-week-v1",
        transform="identity-v1",
        coverage_policy="nvda-goog-mu-calendar-and-timing-gate-v1",
        classification=NEEDS_NORMALIZATION,
        readiness_policy="weekly-context-readiness-v1",
    )


WEEKLY_FEATURES = (
    _weekly("weekly__technical_score", "technical_score"),
    _weekly("weekly__technical_score_change_5", "technical_score_change_5"),
    _weekly(
        "weekly__breakout_readiness_score",
        "breakout_readiness_score",
    ),
)


def _option(
    name: str,
    column: str,
    horizons: tuple[str, ...],
    transform: str = "identity-v1",
) -> FeatureSpec:
    return _candidate_feature(
        name,
        source_column=column,
        horizons=horizons,
        provider_policy="schwab-option-surface-receipt-v1",
        source_timeframe="scheduled-surface",
        source_grain="immutable-option-surface-receipt",
        calculation_versions=("1.2.0",),
        schema_versions=("option-surface-v2",),
        availability_rule="receipt-available-at-with-causal-quote-cutoff",
        availability_version="option-receipt-v1",
        freshness=(
            ("1h", "scheduled-intraday-surface"),
            ("1d", "one-eligible-session"),
            ("1w", "three-calendar-days"),
        ),
        missing_policy="no-surface-before-first-receipt-reject-quality-failures-v1",
        transform=transform,
        coverage_policy="option-250-daily-2y-weekly-6mo-intraday-v1",
        classification=INSUFFICIENT_COVERAGE,
        readiness_policy="option-surface-readiness-v1",
    )


OPTION_FEATURES = (
    _option("opt__iv_minus_realized", "iv_minus_realized_volatility", ("1d", "1w", "1h")),
    _option("opt__put25d_minus_call25d_iv", "put_25d_iv_minus_call_25d_iv", ("1d", "1w", "1h")),
    _option("opt__front_minus_back_iv", "front_iv_minus_back_iv", ("1d", "1w")),
    _option("opt__atm_move_richness", "atm_straddle_move_richness", ("1d", "1w")),
    _option(
        "opt__log_call_put_oi_ratio",
        "call_put_open_interest_ratio",
        ("1d", "1w"),
        "log1p-capped-training-v2",
    ),
    _option(
        "opt__log_call_put_volume_ratio",
        "call_put_volume_ratio",
        ("1h", "1d"),
        "log1p-capped-training-v2",
    ),
    _option("opt__open_interest_concentration", "open_interest_concentration", ("1d", "1w")),
    _option("opt__relative_spread", "relative_bid_ask_spread", ("1h", "1d")),
)

_ALL_HORIZONS = ("1h", "1d", "1w")
OPTION_EVIDENCE_FEATURES = (
    _option(
        "opt__realized_volatility_20d",
        "realized_volatility_20d",
        _ALL_HORIZONS,
    ),
    _option(
        "opt__realized_expected_absolute_move_atm_horizon",
        "realized_expected_absolute_move_atm_horizon",
        _ALL_HORIZONS,
    ),
    _option(
        "opt__atm_implied_volatility",
        "atm_implied_volatility",
        _ALL_HORIZONS,
    ),
    _option(
        "opt__atm_straddle_implied_move",
        "atm_straddle_implied_move",
        _ALL_HORIZONS,
    ),
    _option(
        "opt__atm_straddle_move_excess",
        "atm_straddle_move_excess",
        _ALL_HORIZONS,
    ),
    _option(
        "opt__atm_relative_bid_ask_spread",
        "atm_relative_bid_ask_spread",
        ("1h", "1d"),
    ),
    _option(
        "opt__front_atm_implied_volatility",
        "front_atm_implied_volatility",
        ("1d", "1w"),
    ),
    _option(
        "opt__back_atm_implied_volatility",
        "back_atm_implied_volatility",
        ("1d", "1w"),
    ),
    _option(
        "opt__put_25d_implied_volatility",
        "put_25d_implied_volatility",
        _ALL_HORIZONS,
    ),
    _option(
        "opt__call_25d_implied_volatility",
        "call_25d_implied_volatility",
        _ALL_HORIZONS,
    ),
    _option("opt__smile_curvature", "smile_curvature", _ALL_HORIZONS),
    _option(
        "opt__volume_to_open_interest",
        "volume_to_open_interest",
        _ALL_HORIZONS,
        "log1p-capped-training-v2",
    ),
    _option(
        "opt__put_call_parity_residual",
        "put_call_parity_residual",
        _ALL_HORIZONS,
    ),
    _option(
        "opt__atm_put_call_parity_residual",
        "atm_put_call_parity_residual",
        _ALL_HORIZONS,
    ),
    _option("opt__quote_coverage", "quote_coverage", _ALL_HORIZONS),
    _option(
        "opt__quote_time_coverage",
        "quote_time_coverage",
        _ALL_HORIZONS,
    ),
    _option("opt__iv_coverage", "iv_coverage", _ALL_HORIZONS),
    _option("opt__greeks_coverage", "greeks_coverage", _ALL_HORIZONS),
    _option(
        "opt__open_interest_coverage",
        "open_interest_coverage",
        _ALL_HORIZONS,
    ),
    _option(
        "opt__intrinsic_value_violation_rate",
        "intrinsic_value_violation_rate",
        _ALL_HORIZONS,
    ),
    _option(
        "opt__contract_count",
        "contract_count",
        _ALL_HORIZONS,
        "log1p-nonnegative-v1",
    ),
    _option(
        "opt__expiration_count",
        "expiration_count",
        _ALL_HORIZONS,
        "log1p-nonnegative-v1",
    ),
    _option(
        "opt__atm_days_to_expiration",
        "atm_days_to_expiration",
        _ALL_HORIZONS,
        "log1p-nonnegative-v1",
    ),
    _option(
        "opt__quote_staleness_seconds",
        "quote_staleness_seconds",
        _ALL_HORIZONS,
        "log1p-nonnegative-v1",
    ),
)

QUOTE_FEATURES = (
    _candidate_feature(
        "quote__relative_bid_ask_spread",
        source_column="relative_bid_ask_spread",
        horizons=("1h", "1d"),
        provider_policy="schwab-actual-quote-event-receipt-v1",
        source_timeframe="quote-event",
        source_grain="immutable-quote-receipt",
        calculation_versions=("1.0.0",),
        schema_versions=("quote-liquidity-v1",),
        availability_rule="actual-event-and-local-receipt-backward-asof",
        availability_version="quote-receipt-v1",
        freshness=(("1h", "5-minutes"), ("1d", "one-eligible-session")),
        missing_policy="reject-crossed-locked-nonpositive-or-stale-v1",
        transform="training-cap-robust-v1",
        coverage_policy="six-months-consistent-decision-snapshots-v1",
        classification=INSUFFICIENT_COVERAGE,
        readiness_policy="quote-liquidity-readiness-v1",
    ),
)


def _cme(name: str, column: str, horizons: tuple[str, ...]) -> FeatureSpec:
    return _candidate_feature(
        name,
        source_column=column,
        horizons=horizons,
        provider_policy="databento-continuous-cme-roll-policy-v1",
        source_timeframe="synchronized-context-window",
        source_grain="complete-common-window",
        calculation_versions=("1.0.0",),
        schema_versions=("cme-cross-asset-v1",),
        availability_rule="max-event-receive-receipt-calculation-across-constituents",
        availability_version="cme-common-window-v1",
        freshness=(("1h", "15-minutes"), ("1d", "one-session"), ("1w", "three-calendar-days")),
        missing_policy="missing-if-any-leg-incomplete-stale-or-limit-saturated-v1",
        transform="signed-log-return-robust-v1" if "return" in name else "identity-v1",
        coverage_policy="two-years-reproducible-complete-windows-v1",
        classification=INSUFFICIENT_COVERAGE,
        readiness_policy="cme-context-readiness-v1",
    )


CME_FEATURES = (
    _cme("cme__nq_return_1h", "nq_return", ("1h", "1d")),
    _cme("cme__es_return_1h", "es_return", ("1h", "1d")),
    _cme(
        "cme__small_cap_breadth",
        "rty_minus_es_return",
        ("1h", "1d", "1w"),
    ),
    _cme(
        "cme__tech_breadth",
        "nq_minus_es_return",
        ("1h", "1d", "1w"),
    ),
    _cme("cme__gold_return", "gold_return", ("1h", "1d", "1w")),
    _cme("cme__crude_return", "crude_return", ("1h", "1d", "1w")),
    _cme("cme__relative_spread", "relative_spread", ("1h",)),
    _cme("cme__book_imbalance", "book_imbalance", ("1h",)),
)


def _lifecycle(name: str, classification: str) -> FeatureSpec:
    return _candidate_feature(
        name,
        source_column=name.split("__", 1)[1],
        horizons=("1h", "1d", "1w"),
        provider_policy="canonical-technical-provider-v1",
        source_timeframe="daily-with-canonical-weekly-context",
        source_grain="immutable-technical-lifecycle-calculation",
        calculation_versions=("1.0.0",),
        schema_versions=("technical-lifecycle-v1",),
        availability_rule="max-constituent-availability-plus-calculation-completion",
        availability_version="lifecycle-calculation-v1",
        freshness=(
            ("1h", "2-calendar-days"),
            ("1d", "2-calendar-days"),
            ("1w", "8-calendar-days"),
        ),
        missing_policy="missing-before-first-calculation-no-stale-carry-v1",
        transform="identity-v1",
        coverage_policy=(
            "minimum-80-percent-usable-history-v1"
            if name.endswith("technical_term_spread")
            else "complete-canonical-technical-history-v1"
        ),
        classification=classification,
        readiness_policy="technical-lifecycle-readiness-v1",
    )


LIFECYCLE_FEATURES = (
    _lifecycle("life__technical_consensus_score", NEEDS_NORMALIZATION),
    _lifecycle("life__technical_consensus_change_5d", NEEDS_NORMALIZATION),
    _lifecycle("life__long_term_technical_score", NEEDS_NORMALIZATION),
    _lifecycle("life__technical_term_spread", INSUFFICIENT_COVERAGE),
    _lifecycle("life__timing_score", NEEDS_NORMALIZATION),
)


def _fundamental_technical_lifecycle(name: str, column: str) -> FeatureSpec:
    return _candidate_feature(
        name,
        source_column=column,
        horizons=("1d", "1w"),
        provider_policy="fundamental-technical-consensus-v1",
        source_timeframe="daily-consensus",
        source_grain="completed-fundamental-technical-lifecycle-calculation",
        calculation_versions=("1.0.0",),
        schema_versions=(),
        availability_rule=(
            "conservative-end-of-day-timestamp-after-causal-constituent-asof"
        ),
        availability_version="fundamental-technical-lifecycle-timestamp-v1",
        freshness=(("1d", "2-calendar-days"), ("1w", "8-calendar-days")),
        missing_policy="missing-before-source-availability-no-stale-carry-v1",
        transform="identity-v1",
        coverage_policy="available-completed-lifecycle-history-v1",
        classification=NEEDS_NORMALIZATION,
        readiness_policy="fundamental-technical-lifecycle-readiness-v1",
    )


_FUNDAMENTAL_TECHNICAL_LIFECYCLE_COLUMNS = (
    "technical_consensus_score",
    "technical_timeframe_coverage",
    "technical_consensus_confidence",
    "short_term_technical_score",
    "long_term_technical_score",
    "technical_term_spread",
    "technical_consensus_change_5d",
    "timing_score",
    "fundamental_score",
    "fundamental_confidence",
    "fundamental_change_1q",
    "fundamental_acceleration",
    "lifecycle_confidence",
    "fundamental_technical_spread",
    "agreement_strength",
    "divergence_strength",
    "setup_quality",
)
FUNDAMENTAL_TECHNICAL_LIFECYCLE_FEATURES = tuple(
    _fundamental_technical_lifecycle(f"ftlife__{column}", column)
    for column in _FUNDAMENTAL_TECHNICAL_LIFECYCLE_COLUMNS
)


def _sec(name: str, column: str) -> FeatureSpec:
    return _candidate_feature(
        name,
        source_column=column,
        horizons=("1d", "1w"),
        provider_policy="sec-accepted-document-receipt-v1",
        source_timeframe="filing-event",
        source_grain="immutable-versioned-extraction",
        calculation_versions=("1.0.0",),
        schema_versions=("sec-event-v1",),
        availability_rule="max-filing-acceptance-document-receipt-extraction-completion",
        availability_version="sec-event-v1",
        freshness=(
            ("1d", "first-eligible-decision-only"),
            ("1w", "first-eligible-decision-only"),
        ),
        missing_policy=(
            "missing-before-coverage-and-after-first-eligible-event-decision-v1"
        ),
        transform="identity-v1",
        coverage_policy="multi-year-all-requested-symbols-v1",
        classification=INSUFFICIENT_COVERAGE,
        readiness_policy="sec-event-readiness-v1",
    )


SEC_FEATURES = (
    _sec("sec__dilution_event", "dilution_event"),
    _sec("sec__offering_size_to_market_cap", "offering_size_to_market_cap"),
    _sec("sec__filing_event_impulse", "filing_event_impulse"),
)


def _fundamental(name: str, column: str) -> FeatureSpec:
    return _candidate_feature(
        name,
        source_column=column,
        horizons=("1d", "1w"),
        provider_policy="fmp-immutable-statement-vintage-v1",
        source_timeframe="quarterly-or-annual-filing-version",
        source_grain="immutable-statement-calculation-version",
        calculation_versions=("1.0.0",),
        schema_versions=("point-in-time-fundamentals-v1",),
        availability_rule="max-publication-acceptance-receipt-calculation",
        availability_version="fundamental-publication-v1",
        freshness=(("1d", "120d-quarterly-400d-annual"), ("1w", "120d-quarterly-400d-annual")),
        missing_policy="no-prepublication-fill-forward-only-within-freshness-v1",
        transform="training-winsor-bounded-ratio-v1",
        coverage_policy="immutable-point-in-time-history-all-requested-symbols-v1",
        classification=NEEDS_POINT_IN_TIME_HISTORY,
        readiness_policy="fundamental-vintage-readiness-v1",
    )


_FUNDAMENTAL_COLUMNS = (
    "revenue_growth_yoy",
    "operating_margin",
    "operating_margin_change_yoy",
    "free_cash_flow_margin",
    "cfo_to_net_income",
    "cash_to_debt",
    "current_ratio",
    "diluted_share_growth_yoy",
    "stock_comp_to_revenue",
    "net_issuance_to_market_cap",
    "buyback_yield",
    "roic",
    "fcf_yield",
)
FUNDAMENTAL_FEATURES = tuple(
    _fundamental(f"fund__{column}", column) for column in _FUNDAMENTAL_COLUMNS
)


def _fundamental_direction(
    name: str,
    column: str,
    *,
    transform: str,
) -> FeatureSpec:
    return _candidate_feature(
        name,
        source_column=column,
        horizons=("1d", "1w"),
        provider_policy="fmp-calculated-fundamental-direction-v1",
        source_timeframe="quarterly-or-annual-filing",
        source_grain="completed-fundamental-direction-calculation",
        calculation_versions=("1.0.0",),
        schema_versions=(),
        availability_rule="statement-effective-from-no-prepublication",
        availability_version="fundamental-direction-effective-v1",
        freshness=(
            ("1d", "120d-quarterly-400d-annual"),
            ("1w", "120d-quarterly-400d-annual"),
        ),
        missing_policy="no-prepublication-fill-forward-only-within-freshness-v1",
        transform=transform,
        coverage_policy="available-fundamental-direction-history-v1",
        classification=NEEDS_POINT_IN_TIME_HISTORY,
        readiness_policy="fundamental-direction-readiness-v1",
    )


_FUNDAMENTAL_DIRECTION_SCORE_COLUMNS = (
    "fundamental_score",
    "fundamental_confidence",
    "earnings_momentum_score",
    "cash_conversion_score",
    "accrual_quality_score",
    "balance_sheet_score",
    "tax_quality_score",
    "investment_dilution_score",
    "component_agreement",
    "component_coverage",
    "metric_coverage",
)
_FUNDAMENTAL_DIRECTION_RATIO_COLUMNS = (
    "revenue_growth",
    "operating_income_growth",
    "net_income_growth",
    "cfo_growth",
    "receivables_growth_minus_revenue_growth",
    "inventory_growth_minus_cost_of_revenue_growth",
    "cfo_growth_minus_net_income_growth",
    "debt_growth_minus_cfo_growth",
    "operating_margin",
    "free_cash_flow_margin",
    "cfo_to_net_income",
    "cash_to_debt",
    "current_ratio",
    "effective_tax_rate",
)
FUNDAMENTAL_DIRECTION_FEATURES = (
    *(
        _fundamental_direction(
            f"fdir__{column}",
            column,
            transform="identity-v1",
        )
        for column in _FUNDAMENTAL_DIRECTION_SCORE_COLUMNS
    ),
    *(
        _fundamental_direction(
            f"fdir__{column}",
            column,
            transform="training-winsor-bounded-ratio-v1",
        )
        for column in _FUNDAMENTAL_DIRECTION_RATIO_COLUMNS
    ),
)


def _legacy_macro(name: str, column: str, freshness: str) -> FeatureSpec:
    return _candidate_feature(
        name,
        source_column=column,
        horizons=("1d", "1w"),
        provider_policy="fred-normalized-current-receipt-v1",
        source_timeframe="normalized-observation-history",
        source_grain="four-series-current-receipt-context",
        calculation_versions=("1.0.0",),
        schema_versions=("macro-release-context-v1",),
        availability_rule="max-normalized-source-fetched-at",
        availability_version="fred-current-receipt-v1",
        freshness=(("1d", freshness), ("1w", freshness)),
        missing_policy="no-value-before-current-source-receipt-v1",
        transform="identity-v1",
        coverage_policy="all-required-normalized-series-current-receipt-v1",
        classification=NEEDS_POINT_IN_TIME_HISTORY,
        readiness_policy="macro-current-source-readiness-v1",
    )


LEGACY_MACRO_FEATURES = (
    _legacy_macro("macro__fed_funds_level", "macro__fed_funds_level", "45-days"),
    _legacy_macro("macro__cpi_yoy", "macro__cpi_yoy", "45-days"),
    _legacy_macro(
        "macro__unemployment_change",
        "macro__unemployment_change",
        "45-days",
    ),
    _legacy_macro("macro__gdp_yoy", "macro__gdp_yoy", "120-days"),
)


def _macro(name: str, column: str, freshness: str) -> FeatureSpec:
    return _candidate_feature(
        name,
        source_column=column,
        horizons=("1d", "1w"),
        provider_policy="fred-alfred-api-v1-immutable-vintages",
        source_timeframe="provider-real-time-interval-history",
        source_grain="series-observation-revision-interval",
        calculation_versions=("2.0.0",),
        schema_versions=("macro-alfred-release-context-v2",),
        availability_rule=(
            "feature-component-provider-realtime-start-next-"
            "america-chicago-midnight"
        ),
        availability_version="fred-alfred-date-precision-v1",
        freshness=(("1d", freshness), ("1w", freshness)),
        missing_policy="no-value-before-provider-availability-or-after-freshness-v2",
        transform="identity-v1",
        coverage_policy="verified-causal-eligible-decision-coverage-95pct-v1",
        classification=USABLE_NOW,
        readiness_policy="fred-alfred-readiness-receipt-v1",
    )


MACRO_FEATURES = (
    _macro("macro__fed_funds_level", "macro__fed_funds_level", "45-days"),
    _macro("macro__cpi_yoy", "macro__cpi_yoy", "45-days"),
    _macro(
        "macro__unemployment_change",
        "macro__unemployment_change",
        "56-days",
    ),
    _macro("macro__gdp_yoy", "macro__gdp_yoy", "120-days"),
)

ENERGY_FEATURES = (
    _candidate_feature(
        "energy__wti_or_proxy_return",
        source_column="wti_or_proxy_return",
        horizons=("1h", "1d"),
        provider_policy="fmp-canonical-energy-instrument-v1",
        source_timeframe="quote-receipt",
        source_grain="canonical-instrument-return-chain",
        calculation_versions=("1.0.0",),
        schema_versions=("energy-context-v1",),
        availability_rule="normalized-unix-seconds-event-and-local-receipt",
        availability_version="energy-receipt-v1",
        freshness=(("1h", "30-minutes"), ("1d", "one-session")),
        missing_policy="break-chain-on-instrument-change-or-staleness-v1",
        transform="signed-log-return-v1",
        coverage_policy="consistent-canonical-history-v1",
        classification=INSUFFICIENT_COVERAGE,
        readiness_policy="energy-context-readiness-v1",
    ),
)

AUDIT_CONTROL_SPECS = (
    _candidate_feature(
        "opt__surface_quality_pass",
        source_column="surface_quality_pass",
        horizons=("1h", "1d", "1w"),
        provider_policy="schwab-option-surface-receipt-v1",
        source_timeframe="scheduled-surface",
        source_grain="immutable-option-surface-receipt",
        calculation_versions=("1.2.0",),
        schema_versions=("option-surface-v2",),
        availability_rule="receipt-available-at-with-causal-quote-cutoff",
        availability_version="option-receipt-v1",
        freshness=(),
        missing_policy="fail-closed-v1",
        transform="not-a-model-input",
        coverage_policy="option-quality-gate-v1",
        classification=INSUFFICIENT_COVERAGE,
        readiness_policy="option-surface-readiness-v1",
        role=AUDIT_CONTROL,
    ),
    _candidate_feature(
        "cme__constituent_complete",
        source_column="constituent_complete",
        horizons=("1h", "1d", "1w"),
        provider_policy="databento-continuous-cme-roll-policy-v1",
        source_timeframe="synchronized-context-window",
        source_grain="complete-common-window",
        calculation_versions=("1.0.0",),
        schema_versions=("cme-cross-asset-v1",),
        availability_rule="max-event-receive-receipt-calculation-across-constituents",
        availability_version="cme-common-window-v1",
        freshness=(),
        missing_policy="fail-closed-v1",
        transform="not-a-model-input",
        coverage_policy="complete-window-quality-gate-v1",
        classification=INSUFFICIENT_COVERAGE,
        readiness_policy="cme-context-readiness-v1",
        role=AUDIT_CONTROL,
    ),
)


def _option_pricing_shadow(column: str) -> FeatureSpec:
    return _candidate_feature(
        f"opx__{column}",
        source_column=column,
        horizons=("1h", "4h", "1d", "1w"),
        provider_policy="verified-option-pricing-publication-v3-opra-first",
        source_timeframe="completed-pricing-surface-publication",
        source_grain="symbol-target-compact-surface-aggregate",
        calculation_versions=(
            "black-scholes-nystroem-rbf-bayesian-ridge-residual-v3",
        ),
        schema_versions=("option-pricing-compact-surface-v3",),
        availability_rule="verified-receipt-and-row-available-no-later-than-decision",
        availability_version="pricing-append-history-causal-first-availability-v3",
        freshness=(
            ("1h", "2-hours"),
            ("4h", "4-hours"),
            ("1d", "2-calendar-days"),
            ("1w", "8-calendar-days"),
        ),
        missing_policy="explicit-active-route-unavailable-no-current-substitution-v2",
        transform="training-median-robust-scale-v1",
        coverage_policy="pricing-family-coverage-freshness-gate-v1",
        classification=INSUFFICIENT_COVERAGE,
        readiness_policy="pricing-active-feature-readiness-v3",
    )


OPTION_PRICING_SHADOW_FEATURES = tuple(
    _option_pricing_shadow(column)
    for column in (
        "causal_coverage",
        "median_normalized_residual",
        "median_predictive_standard_deviation",
        "median_model_edge_in_half_spreads",
        "positive_edge_fraction",
        "negative_edge_fraction",
        "raw_arbitrage_violation_rate",
        "constrained_arbitrage_violation_rate",
        "interval_80_coverage",
        "interval_95_coverage",
        "median_relative_bid_ask_spread",
    )
)

ALL_FEATURE_SPECS = (
    *TECHNICAL_ALL_FEATURES,
    *PHASE1_TECHNICAL_FEATURES,
    *LOOP_A_TECHNICAL_COMPONENT_FEATURES,
    *BAR_SHAPE_FEATURES,
    *WEEKLY_FEATURES,
    *OPTION_FEATURES,
    *OPTION_EVIDENCE_FEATURES,
    *OPTION_PRICING_SHADOW_FEATURES,
    *QUOTE_FEATURES,
    *CME_FEATURES,
    *LIFECYCLE_FEATURES,
    *FUNDAMENTAL_TECHNICAL_LIFECYCLE_FEATURES,
    *SEC_FEATURES,
    *FUNDAMENTAL_FEATURES,
    *FUNDAMENTAL_DIRECTION_FEATURES,
    *MACRO_FEATURES,
    *ENERGY_FEATURES,
    *AUDIT_CONTROL_SPECS,
)

_LOOP_A_ADDITIONAL_FEATURES = (
    *BAR_SHAPE_FEATURES,
    *WEEKLY_FEATURES,
    *LIFECYCLE_FEATURES,
    *FUNDAMENTAL_DIRECTION_FEATURES,
    *FUNDAMENTAL_FEATURES,
    *FUNDAMENTAL_TECHNICAL_LIFECYCLE_FEATURES,
    *QUOTE_FEATURES,
    *OPTION_FEATURES,
    *OPTION_EVIDENCE_FEATURES,
    *ENERGY_FEATURES,
    *LEGACY_MACRO_FEATURES,
    *SEC_FEATURES,
    *CME_FEATURES,
)


def _active_features_for_horizon(
    features: tuple[FeatureSpec, ...],
    horizon: str,
) -> tuple[FeatureSpec, ...]:
    active: list[FeatureSpec] = []
    for feature in features:
        if horizon not in feature.applicable_horizons:
            continue
        overrides: dict[str, object] = {"activation_status": ACTIVE}
        if feature.source_family in {"bar", "weekly", "macro", "cme"}:
            overrides["required_schema_versions"] = ()
        if feature.source_family in {"macro", "cme"}:
            overrides["required_calculation_versions"] = ()
        active.append(replace(feature, **overrides))
    return tuple(active)


LOOP_A_ALL_1H_FEATURES = (
    *TECHNICAL_V2_1H_FEATURES,
    *LOOP_A_TECHNICAL_COMPONENT_FEATURES,
    *_active_features_for_horizon(_LOOP_A_ADDITIONAL_FEATURES, "1h"),
)
LOOP_A_ALL_1D_FEATURES = (
    *TECHNICAL_V2_1D_FEATURES,
    *LOOP_A_TECHNICAL_COMPONENT_FEATURES,
    *_active_features_for_horizon(_LOOP_A_ADDITIONAL_FEATURES, "1d"),
)
LOOP_A_ALL_1W_FEATURES = (
    *TECHNICAL_V2_1W_FEATURES,
    *LOOP_A_TECHNICAL_COMPONENT_FEATURES,
    *_active_features_for_horizon(_LOOP_A_ADDITIONAL_FEATURES, "1w"),
)

LOOP_A_ALL_BSGP_SHADOW_1H_FEATURES = (
    *LOOP_A_ALL_1H_FEATURES,
    *_active_features_for_horizon(OPTION_PRICING_SHADOW_FEATURES, "1h"),
)
LOOP_A_ALL_BSGP_SHADOW_1D_FEATURES = (
    *LOOP_A_ALL_1D_FEATURES,
    *_active_features_for_horizon(OPTION_PRICING_SHADOW_FEATURES, "1d"),
)
LOOP_A_ALL_BSGP_SHADOW_1W_FEATURES = (
    *LOOP_A_ALL_1W_FEATURES,
    *_active_features_for_horizon(OPTION_PRICING_SHADOW_FEATURES, "1w"),
)


def _with_verified_alfred_macro(
    features: tuple[FeatureSpec, ...],
) -> tuple[FeatureSpec, ...]:
    replacements = {feature.name: feature for feature in MACRO_FEATURES}
    return tuple(
        replace(replacements[feature.name], activation_status=ACTIVE)
        if feature.name in replacements
        else feature
        for feature in features
    )


LOOP_A_ALL_ACTIVE_V3_1D_FEATURES = _with_verified_alfred_macro(
    LOOP_A_ALL_1D_FEATURES
)
LOOP_A_ALL_ACTIVE_V3_1W_FEATURES = _with_verified_alfred_macro(
    LOOP_A_ALL_1W_FEATURES
)
LOOP_A_ALL_BSGP_ACTIVE_V3_1D_FEATURES = _with_verified_alfred_macro(
    LOOP_A_ALL_BSGP_SHADOW_1D_FEATURES
)
LOOP_A_ALL_BSGP_ACTIVE_V3_1W_FEATURES = _with_verified_alfred_macro(
    LOOP_A_ALL_BSGP_SHADOW_1W_FEATURES
)


_FOUR_HOUR_FRESHNESS_BY_FAMILY = {
    "mr": "exact-decision",
    "bp": "exact-decision",
    "bar": "exact-decision",
    "opt": "2-hours",
    "quote": "5-minutes",
    "cme": "15-minutes",
    "life": "2-calendar-days",
    "energy": "30-minutes",
}


def _clone_1h_features_for_4h(
    features: tuple[FeatureSpec, ...],
) -> tuple[FeatureSpec, ...]:
    """Create a closed 4h contract without widening deployed feature specs."""

    cloned: list[FeatureSpec] = []
    for feature in features:
        try:
            freshness = _FOUR_HOUR_FRESHNESS_BY_FAMILY[
                feature.source_family
            ]
        except KeyError as exc:
            raise ValueError(
                "No 4h freshness policy for feature family "
                f"{feature.source_family!r}"
            ) from exc
        cloned.append(
            replace(
                feature,
                applicable_horizons=("4h",),
                freshness_by_horizon=(("4h", freshness),),
            )
        )
    return tuple(cloned)


TECHNICAL_ALL_4H_FEATURES = _clone_1h_features_for_4h(
    TECHNICAL_ALL_FEATURES
)
TECHNICAL_V2_4H_FEATURES = _clone_1h_features_for_4h(
    TECHNICAL_V2_1H_FEATURES
)
LOOP_A_ALL_4H_FEATURES = _clone_1h_features_for_4h(
    LOOP_A_ALL_1H_FEATURES
)
LOOP_A_ALL_BSGP_SHADOW_4H_FEATURES = (
    *LOOP_A_ALL_4H_FEATURES,
    *_active_features_for_horizon(OPTION_PRICING_SHADOW_FEATURES, "4h"),
)


def _quarantined_set(
    name: str,
    features: tuple[FeatureSpec, ...],
    *,
    horizons: tuple[str, ...],
    reason: str,
) -> FeatureSet:
    return FeatureSet(
        name,
        features,
        version="1.0.0",
        applicable_horizons=horizons,
        activation_status=IMPLEMENTED_BUT_QUARANTINED,
        blocking_reason=reason,
    )


class FeatureRegistry:
    """Strict semantic registry; model columns are never inferred by dtype."""

    def __init__(self) -> None:
        self._calculations = {
            MARKET_REGIME.source_family: MARKET_REGIME,
            BREAKOUT_PRESSURE.source_family: BREAKOUT_PRESSURE,
        }
        self._features = {feature.name: feature for feature in ALL_FEATURE_SPECS}
        if len(self._features) != len(ALL_FEATURE_SPECS):
            raise ValueError("Registered feature names must be unique")
        self._feature_sets = {
            "technical-all": FeatureSet(
                "technical-all",
                TECHNICAL_ALL_FEATURES,
                version="1.0.0",
            ),
            "technical-all-4h": FeatureSet(
                "technical-all-4h",
                TECHNICAL_ALL_4H_FEATURES,
                version="1.0.0",
                applicable_horizons=("4h",),
            ),
            "technical-all-v2-1h": FeatureSet(
                "technical-all-v2-1h",
                TECHNICAL_V2_1H_FEATURES,
                version="2.0.0",
                applicable_horizons=("1h",),
            ),
            "technical-all-v2-4h": FeatureSet(
                "technical-all-v2-4h",
                TECHNICAL_V2_4H_FEATURES,
                version="2.0.0",
                applicable_horizons=("4h",),
            ),
            "technical-all-v2-1d": FeatureSet(
                "technical-all-v2-1d",
                TECHNICAL_V2_1D_FEATURES,
                version="2.0.0",
                applicable_horizons=("1d",),
            ),
            "technical-all-v2-1w": FeatureSet(
                "technical-all-v2-1w",
                TECHNICAL_V2_1W_FEATURES,
                version="2.0.0",
                applicable_horizons=("1w",),
            ),
            "loop-a-all-v1-1h": FeatureSet(
                "loop-a-all-v1-1h",
                LOOP_A_ALL_1H_FEATURES,
                version="1.2.0",
                applicable_horizons=("1h",),
            ),
            "loop-a-all-v1-4h": FeatureSet(
                "loop-a-all-v1-4h",
                LOOP_A_ALL_4H_FEATURES,
                version="1.2.0",
                applicable_horizons=("4h",),
            ),
            "loop-a-all-v1-1d": FeatureSet(
                "loop-a-all-v1-1d",
                LOOP_A_ALL_1D_FEATURES,
                version="1.2.0",
                applicable_horizons=("1d",),
            ),
            "loop-a-all-v1-1w": FeatureSet(
                "loop-a-all-v1-1w",
                LOOP_A_ALL_1W_FEATURES,
                version="1.2.0",
                applicable_horizons=("1w",),
            ),
            "loop-a-all-bsgp-shadow-v1-1h": FeatureSet(
                "loop-a-all-bsgp-shadow-v1-1h",
                LOOP_A_ALL_BSGP_SHADOW_1H_FEATURES,
                version="1.0.0",
                applicable_horizons=("1h",),
            ),
            "loop-a-all-bsgp-shadow-v1-4h": FeatureSet(
                "loop-a-all-bsgp-shadow-v1-4h",
                LOOP_A_ALL_BSGP_SHADOW_4H_FEATURES,
                version="1.0.0",
                applicable_horizons=("4h",),
            ),
            "loop-a-all-bsgp-shadow-v1-1d": FeatureSet(
                "loop-a-all-bsgp-shadow-v1-1d",
                LOOP_A_ALL_BSGP_SHADOW_1D_FEATURES,
                version="1.0.0",
                applicable_horizons=("1d",),
            ),
            "loop-a-all-bsgp-shadow-v1-1w": FeatureSet(
                "loop-a-all-bsgp-shadow-v1-1w",
                LOOP_A_ALL_BSGP_SHADOW_1W_FEATURES,
                version="1.0.0",
                applicable_horizons=("1w",),
            ),
            "loop-a-all-bsgp-active-v2-1h": FeatureSet(
                "loop-a-all-bsgp-active-v2-1h",
                LOOP_A_ALL_BSGP_SHADOW_1H_FEATURES,
                version="2.0.0",
                applicable_horizons=("1h",),
            ),
            "loop-a-all-bsgp-active-v2-4h": FeatureSet(
                "loop-a-all-bsgp-active-v2-4h",
                LOOP_A_ALL_BSGP_SHADOW_4H_FEATURES,
                version="2.0.0",
                applicable_horizons=("4h",),
            ),
            "loop-a-all-bsgp-active-v2-1d": FeatureSet(
                "loop-a-all-bsgp-active-v2-1d",
                LOOP_A_ALL_BSGP_SHADOW_1D_FEATURES,
                version="2.0.0",
                applicable_horizons=("1d",),
            ),
            "loop-a-all-bsgp-active-v2-1w": FeatureSet(
                "loop-a-all-bsgp-active-v2-1w",
                LOOP_A_ALL_BSGP_SHADOW_1W_FEATURES,
                version="2.0.0",
                applicable_horizons=("1w",),
            ),
            "loop-a-all-v3-1d": FeatureSet(
                "loop-a-all-v3-1d",
                LOOP_A_ALL_ACTIVE_V3_1D_FEATURES,
                version="3.0.0",
                applicable_horizons=("1d",),
            ),
            "loop-a-all-v3-1w": FeatureSet(
                "loop-a-all-v3-1w",
                LOOP_A_ALL_ACTIVE_V3_1W_FEATURES,
                version="3.0.0",
                applicable_horizons=("1w",),
            ),
            "loop-a-all-bsgp-active-v3-1d": FeatureSet(
                "loop-a-all-bsgp-active-v3-1d",
                LOOP_A_ALL_BSGP_ACTIVE_V3_1D_FEATURES,
                version="3.0.0",
                applicable_horizons=("1d",),
            ),
            "loop-a-all-bsgp-active-v3-1w": FeatureSet(
                "loop-a-all-bsgp-active-v3-1w",
                LOOP_A_ALL_BSGP_ACTIVE_V3_1W_FEATURES,
                version="3.0.0",
                applicable_horizons=("1w",),
            ),
            "bar-shape-candidate-v1": _quarantined_set(
                "bar-shape-candidate-v1",
                BAR_SHAPE_FEATURES,
                horizons=("1h", "1d", "1w"),
                reason="not selected by an active feature set; coverage gate pending",
            ),
            "weekly-context-candidate-v1": _quarantined_set(
                "weekly-context-candidate-v1",
                WEEKLY_FEATURES,
                horizons=("1d", "1w"),
                reason="calendar/timing and requested-symbol completeness gate pending",
            ),
            "option-candidate-v1": _quarantined_set(
                "option-candidate-v1",
                OPTION_FEATURES,
                horizons=("1h", "1d", "1w"),
                reason="minimum immutable option-surface history is not met",
            ),
            "quote-candidate-v1": _quarantined_set(
                "quote-candidate-v1",
                QUOTE_FEATURES,
                horizons=("1h", "1d"),
                reason="six months of scheduled quote receipts is not met",
            ),
            "cme-candidate-v1": _quarantined_set(
                "cme-candidate-v1",
                CME_FEATURES,
                horizons=("1h", "1d", "1w"),
                reason="two years of synchronized continuous context is not met",
            ),
            "technical-lifecycle-candidate-v1": _quarantined_set(
                "technical-lifecycle-candidate-v1",
                LIFECYCLE_FEATURES,
                horizons=("1h", "1d", "1w"),
                reason="immutable canonical-provider history and term-spread coverage are not met",
            ),
            "sec-candidate-v1": _quarantined_set(
                "sec-candidate-v1",
                SEC_FEATURES,
                horizons=("1d", "1w"),
                reason="multi-year persisted filing-event history is not met",
            ),
            "fundamental-candidate-v1": _quarantined_set(
                "fundamental-candidate-v1",
                FUNDAMENTAL_FEATURES,
                horizons=("1d", "1w"),
                reason="existing statement history is not immutable point-in-time history",
            ),
            "macro-candidate-v1": _quarantined_set(
                "macro-candidate-v1",
                MACRO_FEATURES,
                horizons=("1d", "1w"),
                reason=(
                    "standalone candidate selection is disabled; verified ALFRED "
                    "macros are active through the production v3 profile"
                ),
            ),
            "energy-candidate-v1": _quarantined_set(
                "energy-candidate-v1",
                ENERGY_FEATURES,
                horizons=("1h", "1d"),
                reason="consistent canonical-instrument return history is not met",
            ),
        }

    @property
    def feature_set_names(self) -> tuple[str, ...]:
        return tuple(self._feature_sets)

    @property
    def feature_names(self) -> tuple[str, ...]:
        return tuple(self._features)

    def feature(self, name: str) -> FeatureSpec:
        try:
            return self._features[name]
        except KeyError as exc:
            raise MLContractError(f"Unknown feature {name!r}.") from exc

    def feature_set(
        self,
        name: str,
        *,
        require_active: bool = False,
        horizon: str | None = None,
    ) -> FeatureSet:
        try:
            feature_set = self._feature_sets[name]
        except KeyError as exc:
            raise MLContractError(
                f"Unknown feature set {name!r}; expected one of "
                f"{', '.join(self.feature_set_names)}."
            ) from exc
        if require_active:
            feature_set.ensure_model_eligible(horizon=horizon)
        return feature_set

    def calculation(self, source_family: str) -> CalculationSpec:
        try:
            return self._calculations[source_family]
        except KeyError as exc:
            raise MLContractError(f"Unknown source family {source_family!r}.") from exc

    def validate_source(
        self,
        frame: pd.DataFrame,
        *,
        source_family: str,
        feature_set: FeatureSet,
    ) -> None:
        if frame.columns.has_duplicates:
            duplicates = frame.columns[frame.columns.duplicated()].tolist()
            raise MLContractError(f"Duplicate source columns are prohibited: {duplicates}")

        calculation = self.calculation(source_family)
        family_features = feature_set.for_family(source_family)
        required = {
            "symbol",
            "provider",
            "timeframe",
            "timestamp",
            "bar_end_timestamp",
            "bar_complete",
            "calculation",
            "calculation_version",
            "price_adjustment_status",
            "split_event_count",
            *(feature.source_column for feature in family_features),
        }
        if calculation.mode_column:
            required.add(calculation.mode_column)
        missing = sorted(required.difference(frame.columns))
        if missing:
            if "calculation_version" in missing and "calculation_version_x" in frame.columns:
                raise MLContractError(
                    f"{calculation.calculation_name} contains legacy "
                    "calculation_version_x source column. Rerun technicals after the "
                    "fundamental join namespace fix."
                )
            raise MLContractError(
                f"{calculation.calculation_name} source is missing required columns: "
                + ", ".join(missing)
            )

        self._validate_allowed_values(
            frame["calculation"],
            allowed=(calculation.calculation_name,),
            field="calculation",
            source_family=source_family,
        )
        self._validate_allowed_values(
            frame["calculation_version"],
            allowed=calculation.allowed_versions,
            field="calculation_version",
            source_family=source_family,
        )
        if calculation.mode_column:
            self._validate_allowed_values(
                frame[calculation.mode_column],
                allowed=calculation.allowed_modes,
                field=calculation.mode_column,
                source_family=source_family,
            )

        complete = frame["bar_complete"].fillna(False).astype(bool)
        if not complete.all():
            raise MLContractError(
                f"{source_family} contains {int((~complete).sum())} incomplete bars."
            )

    @staticmethod
    def _validate_allowed_values(
        values: pd.Series,
        *,
        allowed: Iterable[str],
        field: str,
        source_family: str,
    ) -> None:
        allowed_set = {str(value) for value in allowed}
        observed = {str(value) for value in values.dropna().unique()}
        invalid = sorted(observed.difference(allowed_set))
        if values.isna().any() or invalid:
            rendered = invalid or ["<missing>"]
            raise MLContractError(
                f"{source_family} has unsupported {field}: {', '.join(rendered)}; "
                f"allowed: {', '.join(sorted(allowed_set))}."
            )


DEFAULT_FEATURE_REGISTRY = FeatureRegistry()
