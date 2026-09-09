from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Final, Iterable, Mapping

import pandas as pd

from ml.calendars import (
    FOUR_HOUR_CHECKPOINT_START_POLICY,
    HYBRID_TARGET_START_POLICY,
    US_EQUITY_CONTINUOUS_EXTENDED_SOURCE_POLICY,
    US_EQUITY_ACTIONABLE_TARGET_POLICY,
    US_EQUITY_EXTENDED_SOURCE_POLICY,
)


HORIZON_ORDER: Final[tuple[str, ...]] = ("1h", "4h", "1d", "1w")
WEEKLY_HORIZON_ORDER: Final[tuple[str, ...]] = (
    "1w",
    "1w-d1",
    "1w-d2",
    "1w-d3",
    "1w-d4",
    "1w-d5",
)
INTERNAL_HORIZON_ORDER: Final[tuple[str, ...]] = (
    "1h",
    "4h",
    "1d",
    *WEEKLY_HORIZON_ORDER,
)


def is_weekly_horizon(horizon: str) -> bool:
    return str(horizon or "").strip().lower() in WEEKLY_HORIZON_ORDER


def feature_contract_horizon(horizon: str) -> str:
    """Return the explicit existing feature-contract route for a target route."""

    normalized = str(horizon or "").strip().lower()
    return "1w" if is_weekly_horizon(normalized) else normalized


@dataclass(frozen=True)
class HorizonSpecification:
    """Readable timing and target settings for one forecast horizon."""

    horizon: str
    target_definition_version: str
    source_timeframe: str
    information_availability_rule: str
    decision_time_rule: str
    target_window_start_rule: str
    target_window_end_rule: str
    return_definition: str
    label_definition: str
    cost_convention: str
    exchange_calendar_rule: str
    actionability_deadline: str
    feature_set: str
    processing_delay: pd.Timedelta = pd.Timedelta(minutes=5)
    target_price_provider: str | None = None
    target_price_timeframe: str | None = None
    target_price_source_version: str | None = None
    target_constituent_rule: str | None = None
    target_calendar_policy_version: str | None = None
    intraday_source_session_policy: str | None = None
    intraday_target_session_policy: str | None = None
    intraday_target_start_policy: str | None = None

    def __post_init__(self) -> None:
        if self.horizon not in INTERNAL_HORIZON_ORDER:
            raise ValueError(f"Unsupported rolling horizon: {self.horizon!r}")
        if not self.source_timeframe:
            raise ValueError("source_timeframe is required")
        if self.processing_delay < pd.Timedelta(0):
            raise ValueError("processing_delay cannot be negative")
        for field_name in (
            "target_definition_version",
            "information_availability_rule",
            "decision_time_rule",
            "target_window_start_rule",
            "target_window_end_rule",
            "return_definition",
            "label_definition",
            "cost_convention",
            "exchange_calendar_rule",
            "actionability_deadline",
            "feature_set",
        ):
            if not str(getattr(self, field_name) or "").strip():
                raise ValueError(f"{field_name} is required")
        target_fields = (
            "target_price_provider",
            "target_price_timeframe",
            "target_price_source_version",
            "target_constituent_rule",
            "target_calendar_policy_version",
        )
        configured_target_fields = tuple(
            field_name
            for field_name in target_fields
            if getattr(self, field_name) is not None
        )
        if configured_target_fields and len(configured_target_fields) != len(
            target_fields
        ):
            missing = ", ".join(
                field_name
                for field_name in target_fields
                if getattr(self, field_name) is None
            )
            raise ValueError(
                "Intraday target-price metadata must be complete; missing "
                + missing
            )
        intraday_policy_fields = (
            "intraday_source_session_policy",
            "intraday_target_session_policy",
            "intraday_target_start_policy",
        )
        configured_intraday_policy_fields = tuple(
            field_name
            for field_name in intraday_policy_fields
            if getattr(self, field_name) is not None
        )
        if self.source_timeframe == "1h":
            if len(configured_intraday_policy_fields) != len(
                intraday_policy_fields
            ):
                missing = ", ".join(
                    field_name
                    for field_name in intraday_policy_fields
                    if getattr(self, field_name) is None
                )
                raise ValueError(
                    "Intraday source/target session policies must be complete; "
                    "missing " + missing
                )
        elif configured_intraday_policy_fields:
            raise ValueError(
                "Intraday session policies apply only to 1h source timeframes"
            )

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["processing_delay"] = str(self.processing_delay)
        return {
            key: value
            for key, value in payload.items()
            if value is not None
        }


DEFAULT_HORIZON_SPECIFICATIONS: Final[
    Mapping[str, HorizonSpecification]
] = {
    "1h": HorizonSpecification(
        horizon="1h",
        target_definition_version=(
            "next-60-eligible-equity-minutes-open-close-v6"
        ),
        source_timeframe="1h",
        information_availability_rule=(
            "completed_native_regular_or_available_us_extended_hour_bar_end_"
            "plus_processing_delay"
        ),
        decision_time_rule=(
            "after_latest_completed_regular_or_available_us_extended_hour_"
            "and_required_processing_delay"
        ),
        target_window_start_rule=(
            "first_session_open_break_resume_or_eligible_local_clock_hour_start_"
            "strictly_after_information_availability_plus_later_starts_not_"
            "after_two_hours_from_information_availability"
        ),
        target_window_end_rule=(
            "end_after_60_calendar_selected_eligible_equity_actionable_minutes"
        ),
        return_definition=(
            "first_target_minute_open_to_final_target_minute_close_simple_"
            "return_including_intervening_price_gaps"
        ),
        label_definition=(
            "calendar_selected_1m_marks_use_native_trade_bars_or_strictly_"
            "causal_prior_close_for_no_trade_minutes_never_future_fill_and_"
            "require_collection_coverage_through_window_end_label_available_"
            "at_end_plus_processing_delay_and_one_time_cost_adjusted_return_"
            "strictly_positive"
        ),
        cost_convention=(
            "explicit_round_trip_rate_subtracted_once_from_simple_return"
        ),
        exchange_calendar_rule=(
            "us_equity_calendar_selects_actionable_segment_or_full_hour_start_"
            "before_price_lookup_and_accumulates_eligible_premarket_regular_"
            "and_postmarket_minutes_pausing_transition_gaps_and_closures"
        ),
        actionability_deadline="strictly_before_target_window_start",
        feature_set="technical-all",
        target_price_provider="databento",
        target_price_timeframe="1m",
        target_price_source_version=(
            "canonical-adjusted-native-1m-causal-no-trade-marks-v2"
        ),
        target_constituent_rule=(
            "each_calendar_predetermined_eligible_minute_uses_its_native_open_"
            "and_close_when_present_otherwise_latest_strictly_prior_native_"
            "close_with_no_future_fill_and_coverage_through_window_end_required"
        ),
        target_calendar_policy_version=(
            "us-equity-actionable-segments-plus-versioned-start-v1"
        ),
        intraday_source_session_policy=(
            US_EQUITY_CONTINUOUS_EXTENDED_SOURCE_POLICY
        ),
        intraday_target_session_policy=US_EQUITY_ACTIONABLE_TARGET_POLICY,
        intraday_target_start_policy=HYBRID_TARGET_START_POLICY,
    ),
    "4h": HorizonSpecification(
        horizon="4h",
        target_definition_version=(
            "next-180-eligible-equity-minutes-four-checkpoints-v4"
        ),
        source_timeframe="1h",
        information_availability_rule=(
            "completed_native_regular_or_available_us_extended_hour_bar_end_"
            "plus_processing_delay"
        ),
        decision_time_rule=(
            "after_each_completed_regular_or_available_us_extended_1h_source_"
            "bar_and_required_processing_delay"
        ),
        target_window_start_rule=(
            "first_0730_1130_1530_or_1930_eastern_equity_checkpoint_strictly_"
            "after_information_availability"
        ),
        target_window_end_rule=(
            "end_after_180_calendar_selected_eligible_equity_actionable_minutes"
        ),
        return_definition=(
            "first_target_minute_open_to_final_target_minute_close_simple_"
            "return_including_intervening_price_gaps"
        ),
        label_definition=(
            "calendar_selected_1m_marks_use_native_trade_bars_or_strictly_"
            "causal_prior_close_for_no_trade_minutes_never_future_fill_and_"
            "require_collection_coverage_through_window_end_label_available_"
            "at_end_plus_processing_delay_and_one_time_cost_adjusted_return_"
            "strictly_positive"
        ),
        cost_convention=(
            "explicit_round_trip_rate_subtracted_once_from_simple_return"
        ),
        exchange_calendar_rule=(
            "us_equity_calendar_selects_four_daily_checkpoint_starts_before_"
            "price_lookup_and_accumulates_eligible_premarket_regular_and_"
            "postmarket_minutes_pausing_transition_gaps_and_closures"
        ),
        actionability_deadline="strictly_before_target_window_start",
        feature_set="technical-all-4h",
        target_price_provider="databento",
        target_price_timeframe="1m",
        target_price_source_version=(
            "canonical-adjusted-native-1m-causal-no-trade-marks-v2"
        ),
        target_constituent_rule=(
            "each_calendar_predetermined_eligible_minute_uses_its_native_open_"
            "and_close_when_present_otherwise_latest_strictly_prior_native_"
            "close_with_no_future_fill_and_coverage_through_window_end_required"
        ),
        target_calendar_policy_version=(
            "us-equity-actionable-segments-plus-versioned-start-v1"
        ),
        intraday_source_session_policy=US_EQUITY_EXTENDED_SOURCE_POLICY,
        intraday_target_session_policy=US_EQUITY_ACTIONABLE_TARGET_POLICY,
        intraday_target_start_policy=FOUR_HOUR_CHECKPOINT_START_POLICY,
    ),
    "1d": HorizonSpecification(
        horizon="1d",
        target_definition_version="next-session-open-close-v1",
        source_timeframe="1d",
        information_availability_rule=(
            "official_session_close_plus_processing_delay"
        ),
        decision_time_rule=(
            "after_current_eligible_session_close_and_complete_daily_data"
        ),
        target_window_start_rule="next_eligible_session_open",
        target_window_end_rule="next_eligible_session_close",
        return_definition="next_session_open_to_close_simple_return",
        label_definition="cost_adjusted_next_session_return_strictly_positive",
        cost_convention="explicit_round_trip_rate_subtracted_from_simple_return",
        exchange_calendar_rule="regular_sessions",
        actionability_deadline="strictly_before_next_eligible_session_open",
        feature_set="technical-all",
    ),
    "1w": HorizonSpecification(
        horizon="1w",
        target_definition_version=(
            "dynamic-remaining-week-aggregate-open-close-v2"
        ),
        source_timeframe="1d",
        information_availability_rule=(
            "official_session_close_plus_processing_delay"
        ),
        decision_time_rule=(
            "historical_candidates_after_each_completed_eligible_session_close_"
            "live_issuance_from_latest_completed_session_for_targets_remaining_"
            "in_one_exchange_week"
        ),
        target_window_start_rule="next_eligible_session_open",
        target_window_end_rule=(
            "final_eligible_session_close_of_next_targets_exchange_week"
        ),
        return_definition=(
            "first_remaining_exchange_week_session_open_to_final_remaining_"
            "exchange_week_session_close_simple_return"
        ),
        label_definition=(
            "cost_adjusted_remaining_exchange_week_endpoint_return_strictly_"
            "positive_available_after_final_session_close_plus_processing_delay"
        ),
        cost_convention=(
            "explicit_round_trip_rate_subtracted_once_from_simple_return"
        ),
        exchange_calendar_rule=(
            "eligible_regular_sessions_remaining_in_the_next_targets_exchange_"
            "week_with_holidays_early_closes_weekends_and_dst_from_calendar"
        ),
        actionability_deadline=(
            "strictly_before_first_remaining_component_session_close"
        ),
        feature_set="technical-all",
    ),
}


def _weekly_component_specification(lead: int) -> HorizonSpecification:
    horizon = f"1w-d{lead}"
    return HorizonSpecification(
        horizon=horizon,
        target_definition_version=(
            f"dynamic-remaining-week-d{lead}-open-close-v2"
        ),
        source_timeframe="1d",
        information_availability_rule=(
            "official_session_close_plus_processing_delay"
        ),
        decision_time_rule=(
            "historical_candidates_after_each_completed_eligible_session_close_"
            "live_issuance_from_latest_completed_session_for_targets_remaining_"
            "in_one_exchange_week"
        ),
        target_window_start_rule=f"d_plus_{lead}_eligible_session_open",
        target_window_end_rule=f"d_plus_{lead}_eligible_session_close",
        return_definition=(
            f"d_plus_{lead}_eligible_session_open_to_close_simple_return"
        ),
        label_definition=(
            f"cost_adjusted_d_plus_{lead}_session_return_strictly_positive_"
            "available_after_session_close_plus_processing_delay"
        ),
        cost_convention=(
            "explicit_round_trip_rate_subtracted_once_from_simple_return"
        ),
        exchange_calendar_rule=(
            "next_five_eligible_regular_sessions_with_holidays_early_closes_"
            "weekends_and_dst_from_exchange_calendar"
        ),
        actionability_deadline="strictly_before_component_session_close",
        feature_set="technical-all",
    )


INTERNAL_HORIZON_SPECIFICATIONS: Final[
    Mapping[str, HorizonSpecification]
] = {
    **DEFAULT_HORIZON_SPECIFICATIONS,
    **{
        f"1w-d{lead}": _weekly_component_specification(lead)
        for lead in range(1, 6)
    },
}

DEFAULT_FEATURE_PROFILE: Final = "loop-a-all-v1"
PRODUCTION_FEATURE_PROFILE: Final = "production-v1"
PHASE1_V2_FEATURE_PROFILE: Final = "technical-all-v2"
OPTION_PRICING_SHADOW_FEATURE_PROFILE: Final = "loop-a-all-bsgp-shadow-v1"
OPTION_PRICING_ACTIVE_FEATURE_PROFILE: Final = "loop-a-all-bsgp-active-v3"
FEATURE_PROFILES: Final[Mapping[str, Mapping[str, str]]] = {
    DEFAULT_FEATURE_PROFILE: {
        "1h": "loop-a-all-v1-1h",
        "4h": "loop-a-all-v1-4h",
        "1d": "loop-a-all-v1-1d",
        "1w": "loop-a-all-v1-1w",
    },
    PRODUCTION_FEATURE_PROFILE: {
        "1h": "technical-all",
        "4h": "technical-all-4h",
        "1d": "technical-all",
        "1w": "technical-all",
    },
    PHASE1_V2_FEATURE_PROFILE: {
        "1h": "technical-all-v2-1h",
        "4h": "technical-all-v2-4h",
        "1d": "technical-all-v2-1d",
        "1w": "technical-all-v2-1w",
    },
    OPTION_PRICING_SHADOW_FEATURE_PROFILE: {
        "1h": "loop-a-all-bsgp-shadow-v1-1h",
        "4h": "loop-a-all-bsgp-shadow-v1-4h",
        "1d": "loop-a-all-bsgp-shadow-v1-1d",
        "1w": "loop-a-all-bsgp-shadow-v1-1w",
    },
    OPTION_PRICING_ACTIVE_FEATURE_PROFILE: {
        "1h": "loop-a-all-bsgp-active-v2-1h",
        "4h": "loop-a-all-bsgp-active-v2-4h",
        "1d": "loop-a-all-bsgp-active-v3-1d",
        "1w": "loop-a-all-bsgp-active-v3-1w",
    },
}


def horizon_specification(horizon: str) -> HorizonSpecification:
    normalized = str(horizon or "").strip().lower()
    try:
        return INTERNAL_HORIZON_SPECIFICATIONS[normalized]
    except KeyError as exc:
        raise ValueError(
            f"Unknown horizon {horizon!r}; expected "
            f"{', '.join(INTERNAL_HORIZON_ORDER)}."
        ) from exc


def horizon_specifications_for_profile(
    profile: str = DEFAULT_FEATURE_PROFILE,
    *,
    horizons: Iterable[str] | None = None,
) -> dict[str, HorizonSpecification]:
    """Return a closed, horizon-specific feature profile.

    The public runtime intentionally exposes no arbitrary feature-set override.
    Only complete, explicitly registered profiles can select model features.
    """

    normalized = str(profile or "").strip().lower()
    try:
        feature_sets = FEATURE_PROFILES[normalized]
    except KeyError as exc:
        raise ValueError(
            f"Unknown feature profile {profile!r}; expected one of "
            f"{', '.join(FEATURE_PROFILES)}."
        ) from exc
    requested = tuple(
        dict.fromkeys(
            str(horizon).strip().lower()
            for horizon in (horizons if horizons is not None else HORIZON_ORDER)
            if str(horizon).strip()
        )
    )
    unknown = sorted(set(requested).difference(INTERNAL_HORIZON_SPECIFICATIONS))
    if unknown:
        raise ValueError(f"Unknown horizons: {', '.join(unknown)}")
    selected = tuple(
        dict.fromkeys(
            route
            for horizon in requested
            for route in (
                WEEKLY_HORIZON_ORDER if horizon == "1w" else (horizon,)
            )
        )
    )
    return {
        horizon: replace(
            INTERNAL_HORIZON_SPECIFICATIONS[horizon],
            feature_set=feature_sets[feature_contract_horizon(horizon)],
        )
        for horizon in selected
    }


def horizon_specifications() -> dict[str, dict[str, object]]:
    return {
        horizon: spec.as_dict()
        for horizon, spec in DEFAULT_HORIZON_SPECIFICATIONS.items()
    }
