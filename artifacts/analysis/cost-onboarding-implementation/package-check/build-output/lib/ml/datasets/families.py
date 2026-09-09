from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd

from datafetching.fred_vintages import (
    ALFRED_RELEASE_CONTEXT_NAME,
    ALFRED_VINTAGE_AVAILABILITY_BASIS,
    FRED_VINTAGE_SCHEMA_VERSION,
    MACRO_CALCULATION,
    MACRO_CALCULATION_VERSION,
    MACRO_SCHEMA_VERSION,
)
from ml.contracts import MLContractError
from ml.datasets.point_in_time import (
    backward_asof_by_symbol,
    backward_asof_shared,
    conservative_date_only_availability,
    exact_feature_join,
    pivot_shared_context,
)


BAR_SHAPE_VALUES = {
    "bar__overnight_gap_atr": "overnight_gap_atr",
    "bar__intrabar_range_atr": "intrabar_range_atr",
    "bar__close_location": "close_location",
}
WEEKLY_CONTEXT_VALUES = {
    "weekly__technical_score": "technical_score",
    "weekly__technical_score_change_5": "technical_score_change_5",
    "weekly__breakout_readiness_score": "breakout_readiness_score",
}
OPTION_VALUES = {
    "opt__iv_minus_realized": "iv_minus_realized_volatility",
    "opt__put25d_minus_call25d_iv": "put_25d_iv_minus_call_25d_iv",
    "opt__front_minus_back_iv": "front_iv_minus_back_iv",
    "opt__atm_move_richness": "atm_straddle_move_richness",
    "opt__log_call_put_oi_ratio": "call_put_open_interest_ratio",
    "opt__log_call_put_volume_ratio": "call_put_volume_ratio",
    "opt__open_interest_concentration": "open_interest_concentration",
    "opt__relative_spread": "relative_bid_ask_spread",
}
QUOTE_LIQUIDITY_VALUES = {
    "quote__relative_bid_ask_spread": "relative_bid_ask_spread",
}
CME_CONTEXT_VALUES = {
    "cme__nq_return_1h": "nq_return",
    "cme__es_return_1h": "es_return",
    "cme__small_cap_breadth": "rty_minus_es_return",
    "cme__tech_breadth": "nq_minus_es_return",
    "cme__gold_return": "gold_return",
    "cme__crude_return": "crude_return",
    "cme__relative_spread": "relative_spread",
    "cme__book_imbalance": "book_imbalance",
}
FUNDAMENTAL_VALUES = {
    f"fund__{column}": column
    for column in (
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
}
MACRO_VALUES = {
    "macro__fed_funds_level": "macro__fed_funds_level",
    "macro__cpi_yoy": "macro__cpi_yoy",
    "macro__unemployment_change": "macro__unemployment_change",
    "macro__gdp_yoy": "macro__gdp_yoy",
}
MACRO_LINEAGE = {
    "macro__fed_funds_level": (
        "fed_funds_available_at",
        "FEDFUNDS",
        pd.Timedelta(days=45),
    ),
    "macro__cpi_yoy": (
        "cpi_available_at",
        "CPIAUCSL",
        pd.Timedelta(days=45),
    ),
    "macro__unemployment_change": (
        "unemployment_available_at",
        "UNRATE",
        pd.Timedelta(days=56),
    ),
    "macro__gdp_yoy": (
        "gdp_available_at",
        "GDP",
        pd.Timedelta(days=120),
    ),
}
SEC_EVENT_VALUES = {
    "sec__dilution_event": "dilution_event",
    "sec__offering_size_to_market_cap": "offering_size_to_market_cap",
    "sec__filing_event_impulse": "filing_event_impulse",
}
LIFECYCLE_VALUES = {
    "life__technical_consensus_change_5d": "technical_consensus_change_5d",
    "life__long_term_technical_score": "long_term_technical_score",
    "life__technical_term_spread": "technical_term_spread",
    "life__timing_score": "timing_score",
}
ENERGY_CONTEXT_VALUES = {
    "energy__wti_or_proxy_return": "wti_or_proxy_return",
}

OPTION_FRESHNESS = {
    "1h": pd.Timedelta(hours=2),
    "4h": pd.Timedelta(hours=2),
    "1d": pd.Timedelta(days=1),
    "1w": pd.Timedelta(days=3),
}
QUOTE_FRESHNESS = {
    "1h": pd.Timedelta(minutes=5),
    "4h": pd.Timedelta(minutes=5),
    "1d": pd.Timedelta(days=1),
}
CME_FRESHNESS = {
    "1h": pd.Timedelta(minutes=15),
    "4h": pd.Timedelta(minutes=15),
    "1d": pd.Timedelta(days=1),
    "1w": pd.Timedelta(days=3),
}
LIFECYCLE_FRESHNESS = {
    "1h": pd.Timedelta(days=2),
    "4h": pd.Timedelta(days=2),
    "1d": pd.Timedelta(days=2),
    "1w": pd.Timedelta(days=8),
}
ENERGY_FRESHNESS = {
    "1h": pd.Timedelta(minutes=30),
    "4h": pd.Timedelta(minutes=30),
    "1d": pd.Timedelta(days=1),
}


def load_bar_shape_features(
    decisions: pd.DataFrame,
    source: pd.DataFrame,
    *,
    value_columns: Mapping[str, str] = BAR_SHAPE_VALUES,
) -> pd.DataFrame:
    """Load completed bar-shape values at their exact decision timestamp."""

    _validate_calculation(
        source,
        family="bar-shape",
        calculation="bar-shape",
        allowed_versions=("1.0.0",),
    )
    natural_key = ("symbol", "provider", "timeframe", "bar_timestamp")
    _require(source, set(natural_key), label="bar-shape")
    if source.duplicated(list(natural_key)).any():
        raise MLContractError("Bar-shape source natural keys must be unique")
    _validate_available_at_covers_components(
        source,
        ("bar_end_timestamp",),
        family="bar-shape",
    )
    audit_columns = _present_audit_columns(
        source,
        "bar",
        (
            "provider",
            "timeframe",
            "bar_timestamp",
            "bar_end_timestamp",
            "calculation_version",
            "price_adjustment_status",
            "split_event_count",
        ),
    )
    full_left_key = set(natural_key).issubset(decisions.columns)
    if not full_left_key:
        _require(
            decisions,
            {"symbol", "horizon", "decision_timestamp"},
            label="bar-shape decision frame",
        )
    return exact_feature_join(
        decisions,
        source,
        family="bar",
        value_columns=value_columns,
        left_keys=(
            natural_key
            if full_left_key
            else ("symbol", "horizon", "decision_timestamp")
        ),
        right_keys=(
            natural_key
            if full_left_key
            else ("symbol", "timeframe", "available_at")
        ),
        freshness=pd.Timedelta(0),
        quality_columns=("bar_complete",),
        audit_columns=audit_columns,
    )


def load_weekly_context_features(
    decisions: pd.DataFrame,
    source: pd.DataFrame,
    *,
    value_columns: Mapping[str, str] = WEEKLY_CONTEXT_VALUES,
    freshness: pd.Timedelta | str = pd.Timedelta(days=8),
) -> pd.DataFrame:
    """Join completed exchange-week context by actual weekly availability."""

    _validate_calculation(
        source,
        family="weekly-context",
        calculation="weekly-context",
        allowed_versions=("1.0.0",),
    )
    _validate_available_at_covers_components(
        source,
        ("bar_end_timestamp",),
        family="weekly-context",
    )
    natural_key = tuple(
        column
        for column in ("symbol", "provider", "timeframe", "bar_timestamp")
        if column in source.columns
    )
    return backward_asof_by_symbol(
        decisions,
        source,
        family="weekly",
        value_columns=value_columns,
        freshness=freshness,
        natural_key_columns=natural_key or None,
        quality_columns=("bar_complete", "constituent_complete"),
        audit_columns=_present_audit_columns(
            source,
            "weekly",
            (
                "week_start_session",
                "week_end_session",
                "bar_end_timestamp",
                "calculation_version",
                "availability_rule_version",
                "constituent_session_count",
                "constituent_complete",
            ),
        ),
    )


def load_option_features(
    decisions: pd.DataFrame,
    source: pd.DataFrame,
    *,
    horizon: str,
    value_columns: Mapping[str, str] = OPTION_VALUES,
    freshness: pd.Timedelta | str | None = None,
) -> pd.DataFrame:
    """Join immutable option surfaces by receipt ``available_at`` only."""

    _validate_decision_horizon(decisions, horizon)
    if "available_at" not in source.columns:
        raise MLContractError(
            "Option surfaces require receipt available_at; aligned "
            "decision_timestamp is not a valid join clock"
        )
    required = {"symbol", "snapshot_for", "available_at", "surface_quality_pass"}
    _require(source, required, label="option surface")
    selected_values = _require_value_mapping(
        source,
        value_columns,
        label="option surface",
    )
    prepared = source.copy()
    quality_columns = ["surface_quality_pass"]
    quality_columns.extend(
        column
        for column in (
            "quote_cutoff_pass",
            "iv_coverage_pass",
            "quote_time_coverage_pass",
            "greeks_coverage_pass",
            "oi_coverage_pass",
            "quote_staleness_pass",
            "no_crossed_locked_quotes",
            "no_intrinsic_value_violations",
            "tenor_delta_selection_deterministic",
            "realized_volatility_causal",
        )
        if column in source.columns
    )
    prepared["_option_timing_pass"] = False
    cutoff_column = next(
        (
            column
            for column in ("quote_cutoff_at", "surface_cutoff")
            if column in prepared.columns
        ),
        None,
    )
    if cutoff_column is not None:
        cutoff = pd.to_datetime(
            prepared[cutoff_column],
            utc=True,
            errors="coerce",
        )
        receipt = pd.to_datetime(
            prepared["available_at"],
            utc=True,
            errors="coerce",
        )
        prepared["_option_timing_pass"] = (
            cutoff.notna() & receipt.notna() & cutoff.le(receipt)
        )
        if "underlying_quote_timestamp" in prepared.columns:
            underlying = pd.to_datetime(
                prepared["underlying_quote_timestamp"],
                utc=True,
                errors="coerce",
            )
            prepared["_option_timing_pass"] &= (
                underlying.notna() & underlying.le(cutoff)
            )
        else:
            prepared["_option_timing_pass"] = False
    quality_columns.append("_option_timing_pass")
    limit = (
        pd.Timedelta(freshness)
        if freshness is not None
        else _freshness_for(horizon, OPTION_FRESHNESS, family="option")
    )
    return backward_asof_by_symbol(
        decisions,
        prepared,
        family="opt",
        value_columns=selected_values,
        freshness=limit,
        natural_key_columns=("symbol", "snapshot_for", "available_at"),
        quality_columns=quality_columns,
        audit_columns=_present_audit_columns(
            prepared,
            "opt",
            (
                "snapshot_for",
                "decision_timestamp",
                "surface_cutoff",
                "quote_cutoff_at",
                "underlying_quote_timestamp",
                "quote_coverage",
                "quote_time_coverage",
                "iv_coverage",
                "greeks_coverage",
                "open_interest_coverage",
                "quote_staleness_seconds",
                "max_quote_staleness_seconds",
                "crossed_quote_count",
                "locked_quote_count",
                "intrinsic_value_violation",
                "underlying_quote_after_cutoff",
                "selection_policy_version",
                "surface_quality_policy_version",
                "calculation_version",
                "realized_volatility_source_available_at",
                "realized_volatility_calculation_version",
                *quality_columns,
            ),
        ),
    )


def load_quote_liquidity_features(
    decisions: pd.DataFrame,
    source: pd.DataFrame,
    *,
    horizon: str,
    freshness: pd.Timedelta | str | None = None,
) -> pd.DataFrame:
    """Join receipt-timed valid quotes; crossed and locked quotes fail quality."""

    _validate_decision_horizon(decisions, horizon)
    _require(
        source,
        {"symbol", "available_at", "relative_bid_ask_spread"},
        label="quote-liquidity source",
    )
    prepared = source.copy()
    has_declared_quality = "quote_quality_pass" in prepared.columns
    has_physical_quote = bool(
        {"bid", "ask", "mid"}.intersection(prepared.columns)
    )
    if not has_declared_quality and not has_physical_quote:
        raise MLContractError(
            "Quote-liquidity source requires physical bid/ask/mid evidence "
            "or an explicit quality result"
        )
    if has_declared_quality:
        prepared["quote_quality_pass"] = _explicit_true(
            prepared["quote_quality_pass"]
        )
    else:
        prepared["quote_quality_pass"] = True
    relative = pd.to_numeric(
        prepared["relative_bid_ask_spread"],
        errors="coerce",
    )
    prepared["quote_quality_pass"] &= (
        relative.ge(0) & np.isfinite(relative)
    )
    physical_quote_columns = {"bid", "ask", "mid"}
    if has_physical_quote:
        _require(
            prepared,
            physical_quote_columns,
            label="quote-liquidity source",
        )
        bid = pd.to_numeric(prepared["bid"], errors="coerce")
        ask = pd.to_numeric(prepared["ask"], errors="coerce")
        mid = pd.to_numeric(prepared["mid"], errors="coerce")
        calculated_mid = (bid + ask) / 2.0
        calculated_relative = (ask - bid) / calculated_mid
        prepared["quote_quality_pass"] &= (
            bid.notna()
            & ask.notna()
            & bid.gt(0)
            & ask.gt(0)
            & mid.gt(0)
            & ask.gt(bid)
            & relative.ge(0)
            & np.isfinite(relative)
            & np.isclose(mid, calculated_mid, equal_nan=False)
            & np.isclose(
                relative,
                calculated_relative,
                rtol=1e-9,
                atol=1e-12,
                equal_nan=False,
            )
        )
    event_column = next(
        (
            column
            for column in ("quote_event_at", "observed_at")
            if column in prepared.columns
        ),
        None,
    )
    if event_column:
        event = pd.to_datetime(prepared[event_column], utc=True, errors="coerce")
        receipt = pd.to_datetime(
            prepared["available_at"],
            utc=True,
            errors="coerce",
        )
        prepared["quote_quality_pass"] &= (
            event.notna() & receipt.notna() & event.le(receipt)
        )
        if "quote_staleness_seconds" in prepared.columns:
            declared_staleness = pd.to_numeric(
                prepared["quote_staleness_seconds"],
                errors="coerce",
            )
            actual_staleness = (receipt - event).dt.total_seconds()
            prepared["quote_quality_pass"] &= (
                declared_staleness.ge(0)
                & np.isclose(
                    declared_staleness,
                    actual_staleness,
                    rtol=0,
                    atol=1e-6,
                    equal_nan=False,
                )
            )
    limit = (
        pd.Timedelta(freshness)
        if freshness is not None
        else _freshness_for(horizon, QUOTE_FRESHNESS, family="quote")
    )
    return backward_asof_by_symbol(
        decisions,
        prepared,
        family="quote",
        value_columns=QUOTE_LIQUIDITY_VALUES,
        freshness=limit,
        natural_key_columns=("symbol", "available_at"),
        quality_columns=("quote_quality_pass",),
        audit_columns=_present_audit_columns(
            prepared,
            "quote",
            (
                *((event_column,) if event_column else ()),
                "source",
                "fetched_at",
                "calculation_version",
                "quality_policy_version",
                "bid",
                "ask",
                "mid",
                "quote_staleness_seconds",
                "quote_quality_pass",
            ),
        ),
    )


def load_cme_context_features(
    decisions: pd.DataFrame,
    source: pd.DataFrame,
    *,
    horizon: str,
    value_columns: Mapping[str, str] = CME_CONTEXT_VALUES,
    freshness: pd.Timedelta | str | None = None,
) -> pd.DataFrame:
    """Load synchronized CME context using maximum constituent availability."""

    _validate_decision_horizon(decisions, horizon)
    source_constituent_availability = tuple(
        column
        for column in source.columns
        if column.endswith("_available_at") and column != "available_at"
    )
    if source_constituent_availability:
        _validate_available_at_covers_components(
            source,
            source_constituent_availability,
            family="CME constituent context",
        )
    selected_values = {
        model_name: source_name
        for model_name, source_name in value_columns.items()
        if source_name in source.columns
    }
    prepared = source.copy()
    if not selected_values and {"context_name", "value"}.issubset(prepared.columns):
        context_to_model = {
            source_name: model_name
            for model_name, source_name in value_columns.items()
        }
        quality_columns = tuple(
            column
            for column in (
                "constituent_complete",
                "quality_pass",
            )
            if column in prepared.columns
        )
        prepared = pivot_shared_context(
            prepared,
            context_to_model=context_to_model,
            quality_columns=quality_columns,
        )
        selected_values = {
            model_name: model_name for model_name in value_columns
        }
    if not selected_values:
        raise MLContractError("CME context contains no registered model values")
    missing_cme = set(value_columns).difference(selected_values)
    if missing_cme:
        raise MLContractError(
            "CME context is missing registered model values: "
            + ", ".join(sorted(missing_cme))
        )
    _require(
        prepared,
        {"window_start", "window_end", "available_at"},
        label="CME context",
    )
    constituent_availability_columns = tuple(
        column
        for column in prepared.columns
        if column.endswith("_available_at") and column != "available_at"
    )
    _validate_available_at_covers_components(
        prepared,
        (
            "window_end",
            "observed_at",
            "fetched_at",
            "calculated_at",
            "calculation_completed_at",
            *constituent_availability_columns,
        ),
        family="CME",
    )
    _validate_constituent_windows(prepared)
    if "constituent_complete" not in prepared.columns:
        raise MLContractError("CME context requires constituent_complete")
    prepared["_cme_quality_pass"] = (
        _explicit_true(prepared["constituent_complete"])
    )
    if "quality_pass" in prepared.columns:
        prepared["_cme_quality_pass"] &= (
            _explicit_true(prepared["quality_pass"])
        )
    if "source_stale" in prepared.columns:
        prepared["_cme_quality_pass"] &= _explicit_false(
            prepared["source_stale"]
        )
    if "limit_saturated" in prepared.columns:
        prepared["_cme_quality_pass"] &= _explicit_false(
            prepared["limit_saturated"]
        )
    limit = (
        pd.Timedelta(freshness)
        if freshness is not None
        else _freshness_for(horizon, CME_FRESHNESS, family="CME")
    )
    natural_key = tuple(
        column
        for column in ("context_name", "window_end", "calculation_version")
        if column in prepared.columns
    )
    if not natural_key:
        natural_key = ("window_start", "window_end")
    return backward_asof_shared(
        decisions,
        prepared,
        family="cme",
        value_columns=selected_values,
        freshness=limit,
        natural_key_columns=natural_key,
        quality_columns=("_cme_quality_pass",),
        audit_columns=_present_audit_columns(
            prepared,
            "cme",
            (
                "window_start",
                "window_end",
                "observed_at",
                "fetched_at",
                "calculation_version",
                "roll_policy_version",
                "constituent_complete",
                "source_stale",
                "limit_saturated",
                *constituent_availability_columns,
            ),
        ),
    )


def load_fundamental_features(
    decisions: pd.DataFrame,
    source: pd.DataFrame,
    *,
    value_columns: Mapping[str, str] = FUNDAMENTAL_VALUES,
) -> pd.DataFrame:
    """Load immutable statement versions with row-specific freshness."""

    _require(
        source,
        {
            "symbol",
            "period_type",
            "period_end_date",
            "constituent_complete",
        },
        label="point-in-time fundamentals",
    )
    prepared = source.copy()
    if "available_at" not in prepared.columns or pd.to_datetime(
        prepared["available_at"],
        utc=True,
        errors="coerce",
    ).isna().any():
        publication_column = next(
            (
                column
                for column in ("publication_date", "published_date", "accepted_date")
                if column in prepared.columns
            ),
            None,
        )
        if publication_column is None:
            raise MLContractError(
                "Fundamentals require available_at or a date-only publication field"
            )
        prepared = conservative_date_only_availability(
            prepared,
            decisions,
            date_column=publication_column,
        )
    prepared = prepared.loc[
        pd.to_datetime(
            prepared["available_at"],
            utc=True,
            errors="coerce",
        ).notna()
    ].copy()
    _validate_available_at_covers_components(
        prepared,
        (
            "published_at",
            "accepted_at",
            "fetched_at",
            "calculated_at",
            "calculation_completed_at",
            "market_cap_available_at",
            "lagged_comparison_available_at",
        ),
        family="fundamentals",
    )
    if "effective_date_estimated" not in prepared.columns:
        raise MLContractError(
            "Fundamentals require effective_date_estimated quarantine evidence"
        )
    prepared["_fund_quality_pass"] = (
        _explicit_false(prepared["effective_date_estimated"])
        & _explicit_true(prepared["constituent_complete"])
    )
    period = prepared["period_type"].astype(str).str.strip().str.lower()
    annual_periods = {"annual", "fy", "year"}
    quarterly_periods = {"quarterly", "q", "quarter"}
    invalid_periods = sorted(
        set(period).difference(annual_periods | quarterly_periods)
    )
    if invalid_periods:
        raise MLContractError(
            "Fundamentals contain unsupported period_type values: "
            + ", ".join(invalid_periods)
        )
    available = pd.to_datetime(prepared["available_at"], utc=True, errors="coerce")
    prepared["_fund_valid_until"] = available + pd.to_timedelta(
        np.where(
            period.isin(annual_periods),
            400,
            120,
        ),
        unit="D",
    )
    return backward_asof_by_symbol(
        decisions,
        prepared,
        family="fund",
        value_columns=_require_value_mapping(
            prepared,
            value_columns,
            label="point-in-time fundamentals",
        ),
        freshness=None,
        natural_key_columns=(
            "symbol",
            "period_type",
            "period_end_date",
            "available_at",
        ),
        valid_until_column="_fund_valid_until",
        quality_columns=("_fund_quality_pass",),
        audit_columns=_present_audit_columns(
            prepared,
            "fund",
            (
                "period_type",
                "period_end_date",
                "accepted_at",
                "published_at",
                "fetched_at",
                "calculated_at",
                "calculation_completed_at",
                "market_cap_available_at",
                "lagged_comparison_available_at",
                "calculation_version",
                "schema_version",
                "effective_date_estimated",
                "constituent_complete",
                "missing_statement_families",
                "statement_version_kind",
                "source",
            ),
        ),
    )


def load_macro_features(
    decisions: pd.DataFrame,
    source: pd.DataFrame,
    *,
    value_columns: Mapping[str, str] = MACRO_VALUES,
    freshness: pd.Timedelta | str | None = None,
    vintage_source: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Load FRED-derived values with feature-level vintage lineage.

    A context row may contain values last updated by different releases. Each
    model value therefore carries its own source availability clock; freshness
    is never measured only from the shared context-row availability.
    """

    vintage_evidence = vintage_source if vintage_source is not None else source
    required_vintage = {
        "series_name",
        "observation_date",
        "realtime_start",
        "realtime_end",
        "release_at",
        "release_time_precision",
        "fetched_at",
        "available_at",
        "availability_basis",
        "revision_identity",
        "schema_version",
    }
    _require(vintage_evidence, required_vintage, label="macro vintage")
    vintage = vintage_evidence.copy()
    vintage["series_name"] = (
        vintage["series_name"].astype("string").str.strip().str.upper()
    )
    if vintage["series_name"].isna().any() or vintage["series_name"].eq("").any():
        raise MLContractError("Macro vintage contains missing series identity")
    for column in (
        "observation_date",
        "realtime_start",
        "release_at",
        "fetched_at",
        "available_at",
    ):
        converted = pd.to_datetime(
            vintage[column],
            utc=True,
            errors="coerce",
        ).astype("datetime64[ns, UTC]")
        if converted.isna().any():
            raise MLContractError(
                f"Macro vintage contains invalid {column}"
            )
        vintage[column] = converted
    realtime_end = vintage["realtime_end"].astype("string").str.strip()
    if realtime_end.isna().any() or realtime_end.eq("").any():
        raise MLContractError(
            "Macro vintage contains missing realtime_end identity"
        )
    realtime = pd.to_datetime(
        vintage_evidence["realtime_start"],
        utc=True,
        errors="coerce",
    )
    if realtime.isna().any():
        raise MLContractError(
            "Current revised FRED history without realtime vintage identity "
            "is not eligible"
        )
    if not vintage["availability_basis"].astype(str).eq(
        ALFRED_VINTAGE_AVAILABILITY_BASIS
    ).all():
        raise MLContractError(
            "Current-revised or local-receipt FRED rows cannot be used as "
            "historical macro evidence"
        )
    if not vintage["release_time_precision"].astype(str).eq("DATE").all():
        raise MLContractError("ALFRED macro vintages require DATE release precision")
    if not vintage["schema_version"].astype(str).eq(
        FRED_VINTAGE_SCHEMA_VERSION
    ).all():
        raise MLContractError("Macro vintage schema is not the verified ALFRED contract")
    if vintage["available_at"].ne(vintage["release_at"]).any():
        raise MLContractError(
            "ALFRED macro available_at must equal its conservative provider clock"
        )
    if vintage["fetched_at"].lt(vintage["release_at"]).any():
        raise MLContractError("Macro vintage local receipt precedes provider release")
    natural = [
        "series_name",
        "observation_date",
        "realtime_start",
        "realtime_end",
    ]
    if vintage.duplicated(natural).any():
        raise MLContractError("Macro vintage natural keys must be unique")
    if vintage["revision_identity"].astype(str).duplicated().any():
        raise MLContractError("Macro vintage revision identities must be unique")
    selected_values = _require_value_mapping(
        source,
        value_columns,
        label="macro vintage",
    )
    uncontracted = sorted(set(selected_values).difference(MACRO_LINEAGE))
    if uncontracted:
        raise MLContractError(
            "Macro model values lack a feature-level lineage contract: "
            + ", ".join(uncontracted)
        )
    lineage_columns = {
        model_name: MACRO_LINEAGE[model_name][0]
        for model_name in selected_values
    }
    _require(
        source,
        {
            "context_name",
            "available_at",
            "availability_basis",
            "calculation",
            "calculation_version",
            "schema_version",
            "vintage_schema_version",
            *lineage_columns.values(),
        },
        label="macro derived context",
    )
    prepared = source.copy()
    valid_context = (
        prepared["context_name"].astype(str).eq(ALFRED_RELEASE_CONTEXT_NAME)
        & prepared["availability_basis"].astype(str).eq(
            ALFRED_VINTAGE_AVAILABILITY_BASIS
        )
        & prepared["calculation"].astype(str).eq(MACRO_CALCULATION)
        & prepared["calculation_version"].astype(str).eq(
            MACRO_CALCULATION_VERSION
        )
        & prepared["schema_version"].astype(str).eq(MACRO_SCHEMA_VERSION)
        & prepared["vintage_schema_version"].astype(str).eq(
            FRED_VINTAGE_SCHEMA_VERSION
        )
    )
    if not valid_context.all():
        raise MLContractError(
            "Macro derived context is not verified ALFRED-vintage evidence"
        )
    context_available = pd.to_datetime(
        prepared["available_at"],
        utc=True,
        errors="coerce",
    ).astype("datetime64[ns, UTC]")
    if context_available.isna().any():
        raise MLContractError(
            "Macro derived context contains invalid available_at"
        )
    vintage_availability_by_series = {
        series_name: set(
            vintage.loc[
                vintage["series_name"].eq(series_name),
                "available_at",
            ].tolist()
        )
        for _, series_name, _ in MACRO_LINEAGE.values()
    }
    for model_name, source_name in selected_values.items():
        lineage_name, series_name, _ = MACRO_LINEAGE[model_name]
        lineage = pd.to_datetime(
            prepared[lineage_name],
            utc=True,
            errors="coerce",
        ).astype("datetime64[ns, UTC]")
        has_value = pd.to_numeric(
            prepared[source_name],
            errors="coerce",
        ).notna()
        if (has_value & lineage.isna()).any():
            raise MLContractError(
                f"{model_name} has a value without {lineage_name}"
            )
        if (lineage.notna() & lineage.gt(context_available)).any():
            raise MLContractError(
                f"{lineage_name} exceeds macro context available_at"
            )
        known_availability = vintage_availability_by_series[series_name]
        if (
            lineage.notna()
            & ~lineage.isin(known_availability)
        ).any():
            raise MLContractError(
                f"{lineage_name} does not correspond to persisted "
                f"{series_name} vintage availability"
            )
        prepared[lineage_name] = lineage
    _validate_available_at_covers_components(
        prepared,
        (
            "fetched_at",
            "calculated_at",
            "calculation_completed_at",
        ),
        family="macro derived context",
    )

    lineage_audit_columns = {
        f"macro__audit_{lineage_name}": lineage_name
        for lineage_name in lineage_columns.values()
    }
    result = backward_asof_shared(
        decisions,
        prepared,
        family="macro",
        value_columns=selected_values,
        freshness=freshness,
        natural_key_columns=(
            ("context_name", "available_at")
            if "context_name" in prepared.columns
            else natural
        ),
        audit_columns={
            **_present_audit_columns(
                prepared,
                "macro",
                (
                    "context_name",
                    "availability_basis",
                    "calculation",
                    "fetched_at",
                    "calculated_at",
                    "calculation_completed_at",
                    "calculation_version",
                    "schema_version",
                    "vintage_schema_version",
                ),
            ),
            **lineage_audit_columns,
        },
    )
    decision_time = pd.to_datetime(
        result["decision_timestamp"],
        utc=True,
        errors="coerce",
    )
    family_stale = _explicit_true(result["macro__is_stale"])
    for model_name, lineage_name in lineage_columns.items():
        _, _, feature_freshness = MACRO_LINEAGE[model_name]
        feature_available = pd.to_datetime(
            result[f"macro__audit_{lineage_name}"],
            utc=True,
            errors="coerce",
        )
        feature_age = decision_time - feature_available
        feature_stale = (
            family_stale
            | (
                feature_available.notna()
                & feature_age.gt(feature_freshness)
            )
        )
        result[f"{model_name}__available_at"] = feature_available
        result[f"{model_name}__age_seconds"] = (
            feature_age.dt.total_seconds()
        )
        result[f"{model_name}__is_stale"] = feature_stale.astype("boolean")
        result[model_name] = result[model_name].where(~feature_stale)
    return result


def load_sec_event_features(
    decisions: pd.DataFrame,
    source: pd.DataFrame,
    *,
    value_columns: Mapping[str, str] = SEC_EVENT_VALUES,
    freshness: pd.Timedelta | str | None = None,
) -> pd.DataFrame:
    """Load SEC event calculations for their first eligible decision only."""

    component_columns = (
        "filing_accepted_at",
        "document_received_at",
        "extraction_completed_at",
    )
    _require(
        source,
        {"symbol", *component_columns},
        label="SEC event source",
    )
    prepared = source.copy()
    component_times = pd.DataFrame(index=prepared.index)
    for column in component_columns:
        component_times[column] = pd.to_datetime(
            prepared[column],
            utc=True,
            errors="coerce",
        ).astype("datetime64[ns, UTC]")
    if component_times.isna().any().any():
        raise MLContractError(
            "SEC events require actual acceptance, receipt, and extraction "
            "timestamps"
        )
    if (
        component_times["document_received_at"].lt(
            component_times["filing_accepted_at"]
        )
        | component_times["extraction_completed_at"].lt(
            component_times["document_received_at"]
        )
    ).any():
        raise MLContractError(
            "SEC acceptance/receipt/extraction timestamps are out of order"
        )
    for column in component_columns:
        prepared[column] = component_times[column]
    safe_available = component_times.max(axis=1)
    if "available_at" in prepared.columns:
        declared = pd.to_datetime(
            prepared["available_at"],
            utc=True,
            errors="coerce",
        ).astype("datetime64[ns, UTC]")
        supplied = prepared["available_at"].notna()
        if (supplied & declared.isna()).any():
            raise MLContractError("SEC events contain invalid available_at")
        if (declared.notna() & declared.lt(safe_available)).any():
            raise MLContractError(
                "SEC available_at precedes acceptance/receipt/extraction"
            )
        prepared["available_at"] = declared.combine_first(safe_available)
    else:
        prepared["available_at"] = safe_available
    _validate_available_at_covers_components(
        prepared,
        component_columns,
        family="SEC",
    )
    selected_values = _require_value_mapping(
        prepared,
        value_columns,
        label="SEC event source",
    )
    quality_columns: tuple[str, ...] = (
        ("extraction_quality_pass",)
        if "extraction_quality_pass" in prepared.columns
        else ()
    )
    event_natural_key = tuple(
        column
        for column in (
            "symbol",
            "filing_accepted_at",
            "event_type",
            "available_at",
        )
        if column in prepared.columns
    )
    if event_natural_key and prepared.duplicated(
        list(event_natural_key)
    ).any():
        raise MLContractError("SEC event natural keys must be unique")
    prepared = _aggregate_sec_event_snapshots(
        prepared,
        decisions=decisions,
        value_columns=tuple(selected_values.values()),
        component_columns=component_columns,
        quality_columns=quality_columns,
    )
    return backward_asof_by_symbol(
        decisions,
        prepared,
        family="sec",
        value_columns=selected_values,
        freshness=freshness,
        natural_key_columns=("symbol", "available_at"),
        valid_until_column="_sec_valid_until",
        quality_columns=quality_columns,
        audit_columns=_present_audit_columns(
            prepared,
            "sec",
            (
                *component_columns,
                "calculation_version",
                "event_type",
                "_sec_event_count",
                "_sec_event_types",
                *quality_columns,
            ),
        ),
    )


def load_lifecycle_features(
    decisions: pd.DataFrame,
    source: pd.DataFrame,
    *,
    horizon: str,
    value_columns: Mapping[str, str] = LIFECYCLE_VALUES,
    freshness: pd.Timedelta | str | None = None,
) -> pd.DataFrame:
    """Load only the registered technical lifecycle model values."""

    _validate_decision_horizon(decisions, horizon)
    _require(
        source,
        {"symbol", "available_at", *value_columns.values()},
        label="technical lifecycle",
    )
    prepared = source.copy()
    _validate_available_at_covers_components(
        prepared,
        (
            "constituent_available_at",
            "calculated_at",
            "calculation_completed_at",
        ),
        family="technical lifecycle",
    )
    quality_columns: tuple[str, ...] = (
        ("constituent_complete",)
        if "constituent_complete" in prepared.columns
        else ()
    )
    limit = (
        pd.Timedelta(freshness)
        if freshness is not None
        else _freshness_for(
            horizon,
            LIFECYCLE_FRESHNESS,
            family="technical lifecycle",
        )
    )
    natural_key = tuple(
        column
        for column in (
            "symbol",
            "timestamp",
            "available_at",
            "calculation_version",
            "provider_policy_version",
        )
        if column in prepared.columns
    )
    return backward_asof_by_symbol(
        decisions,
        prepared,
        family="life",
        value_columns=value_columns,
        freshness=limit,
        natural_key_columns=natural_key or ("symbol", "available_at"),
        quality_columns=quality_columns,
        audit_columns=_present_audit_columns(
            prepared,
            "life",
            (
                "timestamp",
                "constituent_available_at",
                "calculation_version",
                "provider_policy_version",
                "calculated_at",
                "calculation_completed_at",
                *quality_columns,
            ),
        ),
    )


def load_energy_context_features(
    decisions: pd.DataFrame,
    source: pd.DataFrame,
    *,
    horizon: str,
    value_columns: Mapping[str, str] = ENERGY_CONTEXT_VALUES,
    freshness: pd.Timedelta | str | None = None,
) -> pd.DataFrame:
    """Load canonical WTI/proxy returns by actual persisted availability.

    A source may be shared market context or symbol-scoped. Instrument changes
    and explicitly broken return chains make the latest observation ineligible;
    the loader does not substitute an older instrument observation.
    """

    _validate_decision_horizon(decisions, horizon)
    _require(
        source,
        {"available_at", *value_columns.values()},
        label="energy context",
    )
    change_column = next(
        (
            column
            for column in ("instrument_changed", "instrument_change")
            if column in source.columns
        ),
        None,
    )
    if change_column is None:
        raise MLContractError(
            "Energy context requires explicit instrument-change state"
        )
    prepared = source.copy()
    prepared["_energy_quality_pass"] = _explicit_false(
        prepared[change_column]
    )
    chain_column = next(
        (
            column
            for column in ("chain_complete", "return_chain_complete")
            if column in prepared.columns
        ),
        None,
    )
    if chain_column is None:
        raise MLContractError(
            "Energy context requires explicit return-chain completeness"
        )
    prepared["_energy_quality_pass"] &= _explicit_true(
        prepared[chain_column]
    )
    if "source_stale" in prepared.columns:
        prepared["_energy_quality_pass"] &= _explicit_false(
            prepared["source_stale"]
        )
    limit = (
        pd.Timedelta(freshness)
        if freshness is not None
        else _freshness_for(
            horizon,
            ENERGY_FRESHNESS,
            family="energy context",
        )
    )
    natural_key = tuple(
        column
        for column in (
            "symbol",
            "context_name",
            "canonical_instrument",
            "available_at",
        )
        if column in prepared.columns
    )
    audit = _present_audit_columns(
        prepared,
        "energy",
        (
            "context_name",
            "canonical_instrument",
            "canonical_symbol",
            "provider_instrument",
            "instrument_kind",
            "instrument_identity",
            "instrument_policy_version",
            "return_transform_version",
            change_column,
            chain_column,
            "source_stale",
            "observed_at",
            "event_at",
            "fetched_at",
            "calculation",
            "calculation_version",
        ),
    )
    if "symbol" in prepared.columns:
        return backward_asof_by_symbol(
            decisions,
            prepared,
            family="energy",
            value_columns=value_columns,
            freshness=limit,
            natural_key_columns=natural_key or ("symbol", "available_at"),
            quality_columns=("_energy_quality_pass",),
            audit_columns=audit,
        )
    return backward_asof_shared(
        decisions,
        prepared,
        family="energy",
        value_columns=value_columns,
        freshness=limit,
        natural_key_columns=natural_key or ("available_at",),
        quality_columns=("_energy_quality_pass",),
        audit_columns=audit,
    )


def _aggregate_sec_event_snapshots(
    frame: pd.DataFrame,
    *,
    decisions: pd.DataFrame,
    value_columns: Sequence[str],
    component_columns: Sequence[str],
    quality_columns: Sequence[str],
) -> pd.DataFrame:
    """Aggregate filing events without creating a persistent event state."""

    _require(
        decisions,
        {"symbol", "decision_timestamp"},
        label="SEC decision frame",
    )
    prepared = frame.copy()
    prepared["symbol"] = (
        prepared["symbol"].astype(str).str.strip().str.upper()
    )
    prepared["available_at"] = pd.to_datetime(
        prepared["available_at"],
        utc=True,
        errors="coerce",
    ).astype("datetime64[ns, UTC]")
    if prepared["available_at"].isna().any():
        raise MLContractError("SEC events contain invalid available_at")
    first = _aggregate_sec_groups(
        prepared,
        group_columns=("symbol", "available_at"),
        value_columns=value_columns,
        component_columns=component_columns,
        quality_columns=quality_columns,
    )

    decision_symbols = (
        decisions["symbol"].astype(str).str.strip().str.upper()
    )
    decision_times = pd.to_datetime(
        decisions["decision_timestamp"],
        utc=True,
        errors="coerce",
    ).astype("datetime64[ns, UTC]")
    if decision_times.isna().any():
        raise MLContractError("SEC decisions contain invalid timestamps")
    valid_until: list[pd.Timestamp | pd.NaT] = []
    for row in first.itertuples(index=False):
        symbol = str(getattr(row, "symbol"))
        available = pd.Timestamp(getattr(row, "available_at"))
        symbol_decisions = (
            decision_times.loc[decision_symbols.eq(symbol)]
            .drop_duplicates()
            .sort_values()
        )
        candidates = symbol_decisions.loc[symbol_decisions.ge(available)]
        if candidates.empty:
            valid_until.append(pd.NaT)
            continue
        candidate = pd.Timestamp(candidates.iloc[0])
        prior = symbol_decisions.loc[symbol_decisions.lt(candidate)]
        first_decision_is_proven = (
            candidate == available
            or (
                not prior.empty
                and pd.Timestamp(prior.iloc[-1]) < available
            )
        )
        valid_until.append(
            candidate if first_decision_is_proven else pd.NaT
        )
    first["_sec_valid_until"] = pd.to_datetime(
        valid_until,
        utc=True,
        errors="coerce",
    ).astype("datetime64[ns, UTC]")
    first = first.loc[first["_sec_valid_until"].notna()].copy()
    if first.empty:
        return first

    # Several filings/events can become eligible at the same model decision.
    # Collapse them into one snapshot and use the latest actual availability.
    second = _aggregate_sec_groups(
        first,
        group_columns=("symbol", "_sec_valid_until"),
        value_columns=value_columns,
        component_columns=component_columns,
        quality_columns=quality_columns,
    )
    if second.duplicated(["symbol", "available_at"]).any():
        raise MLContractError(
            "SEC aggregated availability remains ambiguous per symbol"
        )
    return second


def _aggregate_sec_groups(
    frame: pd.DataFrame,
    *,
    group_columns: Sequence[str],
    value_columns: Sequence[str],
    component_columns: Sequence[str],
    quality_columns: Sequence[str],
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for key, group in frame.groupby(
        list(group_columns),
        sort=True,
        dropna=False,
    ):
        key_values = key if isinstance(key, tuple) else (key,)
        row = {
            column: value
            for column, value in zip(
                group_columns,
                key_values,
                strict=True,
            )
        }
        row["available_at"] = pd.to_datetime(
            group["available_at"],
            utc=True,
            errors="coerce",
        ).max()
        for column in value_columns:
            values = pd.to_numeric(group[column], errors="coerce")
            row[column] = values.max() if values.notna().any() else np.nan
        for column in component_columns:
            if column in group.columns:
                row[column] = pd.to_datetime(
                    group[column],
                    utc=True,
                    errors="coerce",
                ).max()
        for column in quality_columns:
            row[column] = bool(
                _explicit_true(group[column]).all()
            )
        if "calculation_version" in group.columns:
            versions = tuple(
                sorted(
                    set(
                        group["calculation_version"]
                        .dropna()
                        .astype(str)
                    )
                )
            )
            if len(versions) != 1:
                raise MLContractError(
                    "SEC events at one availability disagree on "
                    "calculation_version"
                )
            row["calculation_version"] = versions[0]
        event_types = (
            tuple(
                sorted(
                    set(
                        group["event_type"]
                        .dropna()
                        .astype(str)
                    )
                )
            )
            if "event_type" in group.columns
            else ()
        )
        row["event_type"] = "|".join(event_types)
        row["_sec_event_types"] = "|".join(event_types)
        row["_sec_event_count"] = int(
            pd.to_numeric(
                group.get(
                    "_sec_event_count",
                    pd.Series(1, index=group.index),
                ),
                errors="coerce",
            ).fillna(0).sum()
        )
        if "_sec_valid_until" in group.columns:
            row["_sec_valid_until"] = pd.to_datetime(
                group["_sec_valid_until"],
                utc=True,
                errors="coerce",
            ).max()
        rows.append(row)
    return pd.DataFrame(rows)


def _validate_calculation(
    frame: pd.DataFrame,
    *,
    family: str,
    calculation: str,
    allowed_versions: Sequence[str],
) -> None:
    _require(
        frame,
        {"calculation", "calculation_version"},
        label=family,
    )
    if not frame["calculation"].astype(str).eq(calculation).all():
        raise MLContractError(f"{family} has unexpected calculation metadata")
    observed = set(frame["calculation_version"].dropna().astype(str))
    invalid = observed.difference(allowed_versions)
    if frame["calculation_version"].isna().any() or invalid:
        raise MLContractError(
            f"{family} has unsupported calculation versions: "
            + ", ".join(sorted(invalid or {"<missing>"}))
        )


def _validate_available_at_covers_components(
    frame: pd.DataFrame,
    components: Sequence[str],
    *,
    family: str,
) -> None:
    if "available_at" not in frame.columns:
        raise MLContractError(f"{family} source requires available_at")
    available = pd.to_datetime(frame["available_at"], utc=True, errors="coerce")
    if available.isna().any():
        raise MLContractError(f"{family} source contains invalid available_at")
    for column in components:
        if column not in frame.columns:
            continue
        component = pd.to_datetime(frame[column], utc=True, errors="coerce")
        invalid_timestamp = frame[column].notna() & component.isna()
        if invalid_timestamp.any():
            raise MLContractError(
                f"{family} source contains invalid {column}"
            )
        invalid = component.notna() & available.lt(component)
        if invalid.any():
            raise MLContractError(
                f"{family} available_at precedes {column}"
            )


def _validate_constituent_windows(frame: pd.DataFrame) -> None:
    starts = [
        column
        for column in frame.columns
        if column.endswith("_window_start") and column != "window_start"
    ]
    ends = [
        column
        for column in frame.columns
        if column.endswith("_window_end") and column != "window_end"
    ]
    expected_start = pd.to_datetime(
        frame["window_start"],
        utc=True,
        errors="coerce",
    )
    expected_end = pd.to_datetime(
        frame["window_end"],
        utc=True,
        errors="coerce",
    )
    if (
        expected_start.isna().any()
        or expected_end.isna().any()
        or expected_start.ge(expected_end).any()
    ):
        raise MLContractError(
            "CME context requires valid forward-moving windows"
        )
    for column in starts:
        observed = pd.to_datetime(frame[column], utc=True, errors="coerce")
        if not observed.eq(expected_start).all():
            raise MLContractError("CME constituent windows are not synchronized")
    for column in ends:
        observed = pd.to_datetime(frame[column], utc=True, errors="coerce")
        if not observed.eq(expected_end).all():
            raise MLContractError("CME constituent windows are not synchronized")


def _require_value_mapping(
    frame: pd.DataFrame,
    values: Mapping[str, str],
    *,
    label: str,
) -> dict[str, str]:
    missing = [
        source_name
        for source_name in values.values()
        if source_name not in frame.columns
    ]
    if missing:
        raise MLContractError(
            f"{label} is missing registered value columns: "
            + ", ".join(sorted(missing))
        )
    return dict(values)


def _present_audit_columns(
    frame: pd.DataFrame,
    family: str,
    source_columns: Sequence[str],
) -> dict[str, str]:
    return {
        f"{family}__audit_{source_name}": source_name
        for source_name in source_columns
        if source_name in frame.columns
    }


def _freshness_for(
    horizon: str,
    values: Mapping[str, pd.Timedelta],
    *,
    family: str,
) -> pd.Timedelta:
    normalized = str(horizon).strip().lower()
    try:
        return values[normalized]
    except KeyError as exc:
        raise MLContractError(
            f"{family} is not applicable to horizon {horizon!r}"
        ) from exc


def _validate_decision_horizon(
    decisions: pd.DataFrame,
    horizon: str,
) -> None:
    if "horizon" not in decisions.columns:
        return
    expected = str(horizon).strip().lower()
    observed = (
        decisions["horizon"].astype("string").str.strip().str.lower()
    )
    invalid = observed.isna() | observed.ne(expected)
    if invalid.any():
        values = sorted(set(observed.dropna().astype(str)))
        raise MLContractError(
            f"Decision frame horizons {values} do not match {expected!r}"
        )


def _require(
    frame: pd.DataFrame,
    columns: set[str] | Sequence[str],
    *,
    label: str,
) -> None:
    missing = sorted(set(columns).difference(frame.columns))
    if missing:
        raise MLContractError(
            f"{label} is missing required columns: " + ", ".join(missing)
        )


def _explicit_true(values: pd.Series) -> pd.Series:
    normalized = values.astype("string").str.strip().str.lower()
    return normalized.isin({"true", "1", "1.0", "yes", "y"}).fillna(False)


def _explicit_false(values: pd.Series) -> pd.Series:
    normalized = values.astype("string").str.strip().str.lower()
    return normalized.isin({"false", "0", "0.0", "no", "n"}).fillna(False)
