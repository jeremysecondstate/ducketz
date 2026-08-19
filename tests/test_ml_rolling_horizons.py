from __future__ import annotations

import pandas as pd

from ml.calendars import (
    ExchangeSessionCalendar,
    attach_official_daily_sessions,
    attach_official_intraday_sessions,
)
from ml.horizons import (
    HORIZON_ORDER,
    INTERNAL_HORIZON_ORDER,
    WEEKLY_HORIZON_ORDER,
    feature_contract_horizon,
    horizon_specification,
    horizon_specifications,
    horizon_specifications_for_profile,
)
from ml.rolling_samples import build_rolling_samples
from ml.timing import (
    ACTIONABLE,
    NO_ACTIONABLE_CANDIDATE,
    TARGET_TIMESTAMP_INVALID,
    TARGET_WINDOW_STARTED,
    classify_actionability,
    evaluate_actionability_rows,
)


def test_horizon_contracts_are_readable_and_share_feature_columns() -> None:
    contracts = horizon_specifications()
    assert tuple(contracts) == HORIZON_ORDER == ("1h", "4h", "1d", "1w")
    assert tuple(
        contract["horizon"] for contract in contracts.values()
    ) == HORIZON_ORDER
    assert {
        horizon: contract["feature_set"]
        for horizon, contract in contracts.items()
    } == {
        "1h": "technical-all",
        "4h": "technical-all-4h",
        "1d": "technical-all",
        "1w": "technical-all",
    }
    assert not any(
        key.endswith(("_id", "_ids"))
        for contract in contracts.values()
        for key in contract
    )
    weekly = contracts["1w"]
    assert weekly["target_definition_version"] == (
        "dynamic-remaining-week-aggregate-open-close-v2"
    )
    assert weekly["return_definition"] == (
        "first_remaining_exchange_week_session_open_to_final_remaining_"
        "exchange_week_session_close_simple_return"
    )
    expanded = horizon_specifications_for_profile(
        "loop-a-all-v1",
        horizons=HORIZON_ORDER,
    )
    assert tuple(expanded) == INTERNAL_HORIZON_ORDER
    assert tuple(
        horizon_specifications_for_profile(
            "loop-a-all-v1",
            horizons=("1w",),
        )
    ) == WEEKLY_HORIZON_ORDER
    assert all(
        specification.feature_set == "loop-a-all-v1-1w"
        for specification in expanded.values()
        if specification.horizon in WEEKLY_HORIZON_ORDER
    )
    assert all(
        feature_contract_horizon(horizon) == "1w"
        for horizon in WEEKLY_HORIZON_ORDER
    )
    four_hour = contracts["4h"]
    assert four_hour["target_definition_version"] == (
        "next-180-eligible-regular-minutes-open-close-v3"
    )
    assert four_hour["source_timeframe"] == "1h"
    assert four_hour["target_price_timeframe"] == "1m"
    assert four_hour["target_calendar_policy_version"] == (
        "session-open-break-resume-plus-full-local-clock-anchor-v1"
    )
    assert four_hour["processing_delay"] == "0 days 00:05:00"
    one_hour = contracts["1h"]
    assert one_hour["target_definition_version"] == (
        "next-60-eligible-regular-minutes-open-close-v3"
    )
    assert one_hour["target_price_timeframe"] == "1m"
    assert one_hour["target_calendar_policy_version"] == (
        "session-open-break-resume-plus-full-local-clock-anchor-v1"
    )
    for unaffected_horizon in ("1d", "1w"):
        assert "target_price_timeframe" not in contracts[unaffected_horizon]
        assert (
            "target_calendar_policy_version"
            not in contracts[unaffected_horizon]
        )


def test_shared_timing_rule_is_strict_at_target_start() -> None:
    before = classify_actionability(
        information_available_at="2026-07-27T15:05:00Z",
        forecast_created_at="2026-07-27T15:59:59.999999Z",
        target_window_start="2026-07-27T16:00:00Z",
    )
    exact = classify_actionability(
        information_available_at="2026-07-27T15:05:00Z",
        forecast_created_at="2026-07-27T16:00:00Z",
        target_window_start="2026-07-27T16:00:00Z",
    )
    invalid = classify_actionability(
        information_available_at="2026-07-27T16:05:00Z",
        forecast_created_at="2026-07-27T16:10:00Z",
        target_window_start="2026-07-27T16:00:00Z",
    )
    assert before.status == ACTIONABLE
    assert exact.status == TARGET_WINDOW_STARTED
    assert invalid.status == TARGET_TIMESTAMP_INVALID


def test_independent_horizon_actionability_statuses() -> None:
    frame = pd.DataFrame(
        {
            "symbol": ["GOOG", "GOOG", "GOOG"],
            "horizon": ["1h", "1d", "1w"],
            "information_available_at": pd.to_datetime(
                [
                    "2026-07-28T04:00:00Z",
                    "2026-07-28T04:00:00Z",
                    "2026-07-24T20:05:00Z",
                ],
                utc=True,
            ),
            "target_window_start": pd.to_datetime(
                [
                    "2026-07-28T14:00:00Z",
                    "2026-07-28T13:30:00Z",
                    "2026-07-27T13:30:00Z",
                ],
                utc=True,
            ),
        }
    )
    selected, diagnostics = evaluate_actionability_rows(
        frame,
        forecast_created_at="2026-07-28T04:50:00Z",
        group_columns=("symbol", "horizon"),
    )
    assert set(selected["horizon"]) == {"1h", "1d"}
    status = diagnostics.set_index("horizon")["actionability_status"]
    assert status["1h"] == ACTIONABLE
    assert status["1d"] == ACTIONABLE
    assert status["1w"] == TARGET_WINDOW_STARTED


def test_no_actionable_candidate_before_information_is_available() -> None:
    result = classify_actionability(
        information_available_at="2026-07-28T20:05:00Z",
        forecast_created_at="2026-07-28T19:00:00Z",
        target_window_start="2026-07-29T14:00:00Z",
    )
    assert result.status == NO_ACTIONABLE_CANDIDATE
    assert result.reason == "INFORMATION_NOT_YET_AVAILABLE"


def test_hourly_calendar_excludes_partial_open_and_respects_early_close() -> None:
    calendar = ExchangeSessionCalendar(
        "XNAS",
        start="2026-11-20",
        end="2026-12-04",
    )
    intervals = calendar.eligible_hour_intervals(
        start_session="2026-11-27",
        end_session="2026-11-27",
    )
    assert [
        (item.start_timestamp.isoformat(), item.end_timestamp.isoformat())
        for item in intervals
    ] == [
        ("2026-11-27T15:00:00+00:00", "2026-11-27T16:00:00+00:00"),
        ("2026-11-27T16:00:00+00:00", "2026-11-27T17:00:00+00:00"),
        ("2026-11-27T17:00:00+00:00", "2026-11-27T18:00:00+00:00"),
    ]


def test_hourly_calendar_does_not_cross_market_breaks() -> None:
    calendar = ExchangeSessionCalendar(
        "XHKG",
        start="2026-07-20",
        end="2026-08-03",
    )
    intervals = calendar.eligible_hour_intervals(
        start_session="2026-07-27",
        end_session="2026-07-27",
    )
    assert not any(
        item.start_timestamp < pd.Timestamp("2026-07-27T05:00:00Z")
        and item.end_timestamp > pd.Timestamp("2026-07-27T04:00:00Z")
        for item in intervals
    )
    assert {item.start_timestamp.hour for item in intervals} == {2, 3, 5, 6, 7}


def test_hourly_calendar_uses_exchange_dst_schedule() -> None:
    calendar = ExchangeSessionCalendar(
        "XNAS",
        start="2026-03-01",
        end="2026-03-15",
    )
    before = calendar.eligible_hour_intervals(
        start_session="2026-03-06",
        end_session="2026-03-06",
    )
    after = calendar.eligible_hour_intervals(
        start_session="2026-03-09",
        end_session="2026-03-09",
    )
    assert before[0].start_timestamp == pd.Timestamp("2026-03-06T15:00:00Z")
    assert after[0].start_timestamp == pd.Timestamp("2026-03-09T14:00:00Z")


def test_one_hour_decisions_admit_only_bounded_us_extended_full_hours() -> None:
    intervals = pd.DataFrame(
        {
            "exchange_calendar": ["XNAS"] * 4,
            "bar_timestamp": pd.to_datetime(
                [
                    "2026-07-27T19:00:00Z",
                    "2026-07-27T20:00:00Z",
                    "2026-07-27T23:00:00Z",
                    "2026-07-28T00:00:00Z",
                ],
                utc=True,
            ),
            "operational_bar_end_timestamp": pd.to_datetime(
                [
                    "2026-07-27T20:00:00Z",
                    "2026-07-27T21:00:00Z",
                    "2026-07-28T00:00:00Z",
                    "2026-07-28T01:00:00Z",
                ],
                utc=True,
            ),
        }
    )

    regular_only = attach_official_intraday_sessions(intervals)
    with_extended = attach_official_intraday_sessions(
        intervals,
        include_extended_hours=True,
    )

    assert regular_only["intraday_interval_eligible"].tolist() == [
        True,
        False,
        False,
        False,
    ]
    assert with_extended["intraday_interval_eligible"].tolist() == [
        True,
        True,
        True,
        False,
    ]
    assert with_extended.loc[1:2, "exchange_session"].eq(
        pd.Timestamp("2026-07-27")
    ).all()


def test_hour_target_skips_the_interval_already_started_during_processing() -> None:
    feature = _feature(
        horizon="1h",
        session="2026-07-27",
        bar_start="2026-07-27T14:00:00Z",
        bar_end="2026-07-27T15:00:00Z",
        decision="2026-07-27T15:05:00Z",
    )
    target_prices = pd.DataFrame(
        _minute_prices(
            "2026-07-27T16:00:00Z",
            60,
            open_value=102.0,
            close_value=103.0,
        )
    )
    samples = build_rolling_samples(
        feature,
        target_prices,
        specification=horizon_specification("1h"),
        assumed_round_trip_cost=0.001,
        materialized_at="2026-07-27T17:05:00Z",
        source_adjusted_prices=pd.DataFrame(
            [_hour_price("2026-07-27T14:00:00Z", 100.0, 101.0)]
        ),
    )
    row = samples.iloc[0]
    assert row["target_window_start"] == pd.Timestamp("2026-07-27T16:00:00Z")
    assert row["target_window_end"] == pd.Timestamp("2026-07-27T17:00:00Z")
    assert row["target_open"] == 102.0
    assert row["target_close"] == 103.0
    assert row["label_status"] == "COMPLETE"


def test_target_bar_values_never_enter_feature_columns() -> None:
    feature = _feature(
        horizon="1h",
        session="2026-07-27",
        bar_start="2026-07-27T14:00:00Z",
        bar_end="2026-07-27T15:00:00Z",
        decision="2026-07-27T15:05:00Z",
    )
    feature["mr__trend_atr"] = 7.25
    prices = pd.DataFrame(
        _minute_prices(
            "2026-07-27T16:00:00Z",
            60,
            open_value=102.0,
            close_value=103.0,
        )
    )
    first = build_rolling_samples(
        feature,
        prices,
        specification=horizon_specification("1h"),
        assumed_round_trip_cost=0.0,
        materialized_at="2026-07-27T17:05:00Z",
        source_adjusted_prices=pd.DataFrame(
            [_hour_price("2026-07-27T14:00:00Z", 100.0, 101.0)]
        ),
    )
    changed = prices.copy()
    changed.loc[
        changed["timestamp"].eq("2026-07-27T16:59:00Z"), "close"
    ] = 1.0
    second = build_rolling_samples(
        feature,
        changed,
        specification=horizon_specification("1h"),
        assumed_round_trip_cost=0.0,
        materialized_at="2026-07-27T17:05:00Z",
        source_adjusted_prices=pd.DataFrame(
            [_hour_price("2026-07-27T14:00:00Z", 100.0, 101.0)]
        ),
    )
    assert first.loc[0, "mr__trend_atr"] == second.loc[0, "mr__trend_atr"] == 7.25
    assert first.loc[0, "target_cost_adjusted_positive"] == 1
    assert second.loc[0, "target_cost_adjusted_positive"] == 0


def test_future_source_information_is_excluded_point_in_time() -> None:
    available = _feature(
        horizon="1h",
        session="2026-07-27",
        bar_start="2026-07-27T14:00:00Z",
        bar_end="2026-07-27T15:00:00Z",
        decision="2026-07-27T15:05:00Z",
    )
    future = _feature(
        horizon="1h",
        session="2026-07-27",
        bar_start="2026-07-27T15:00:00Z",
        bar_end="2026-07-27T16:00:00Z",
        decision="2026-07-27T16:05:00Z",
    )
    samples = build_rolling_samples(
        pd.concat([available, future], ignore_index=True),
        pd.DataFrame(
            _minute_prices(
                "2026-07-27T16:00:00Z",
                60,
                open_value=102.0,
                close_value=103.0,
            )
        ),
        specification=horizon_specification("1h"),
        assumed_round_trip_cost=0.001,
        materialized_at="2026-07-27T15:30:00Z",
        source_adjusted_prices=pd.DataFrame(
            [
                _hour_price("2026-07-27T14:00:00Z", 100.0, 101.0),
                _hour_price("2026-07-27T15:00:00Z", 101.0, 102.0),
            ]
        ),
    )

    assert samples["id"].tolist() == [
        "GOOG|1h|2026-07-27T15:05:00Z"
    ]
    assert samples.iloc[0]["label_status"] == "INCOMPLETE_LABEL"
    assert pd.isna(samples.iloc[0]["target_cost_adjusted_positive"])
    assert pd.isna(samples.iloc[0]["target_open"])
    assert pd.isna(samples.iloc[0]["target_close"])


def test_distinct_completed_source_bars_remain_distinct_decisions() -> None:
    earlier = _feature(
        horizon="1h",
        session="2026-07-27",
        bar_start="2026-07-27T18:00:00Z",
        bar_end="2026-07-27T19:00:00Z",
        decision="2026-07-27T19:05:00Z",
    )
    later = _feature(
        horizon="1h",
        session="2026-07-27",
        bar_start="2026-07-27T19:00:00Z",
        bar_end="2026-07-27T20:00:00Z",
        decision="2026-07-27T20:05:00Z",
    )
    prices = pd.DataFrame(
        _minute_prices(
            "2026-07-28T13:30:00Z",
            60,
            open_value=103.0,
            close_value=104.0,
        )
    )
    samples = build_rolling_samples(
        pd.concat([earlier, later], ignore_index=True),
        prices,
        specification=horizon_specification("1h"),
        assumed_round_trip_cost=0.001,
        materialized_at="2026-07-28T14:35:00Z",
        source_adjusted_prices=pd.DataFrame(
            [
                _hour_price("2026-07-27T18:00:00Z", 100.0, 101.0),
                _hour_price("2026-07-27T19:00:00Z", 101.0, 102.0),
            ]
        ),
    )

    assert len(samples) == 2
    assert samples["id"].nunique() == 2
    assert samples["id"].tolist() == [
        "GOOG|1h|2026-07-27T19:05:00Z",
        "GOOG|1h|2026-07-27T20:05:00Z",
    ]
    assert not any(
        column.endswith(("_id", "_ids")) for column in samples.columns
    )
    assert samples["target_window_start"].nunique() == 1


def test_daily_calendar_uses_the_actual_early_close_for_availability() -> None:
    attached = attach_official_daily_sessions(
        pd.DataFrame(
            {
                "exchange_calendar": ["XNAS"],
                "bar_timestamp": ["2026-11-27T00:00:00Z"],
            }
        ),
        calendar_column="exchange_calendar",
        processing_delay=pd.Timedelta(minutes=5),
    )
    row = attached.iloc[0]
    assert row["bar_end_timestamp"] == pd.Timestamp(
        "2026-11-27T18:00:00Z"
    )
    assert row["decision_timestamp"] == pd.Timestamp(
        "2026-11-27T18:05:00Z"
    )


def test_frozen_weekly_routes_resolve_the_requested_july_31_outlook() -> None:
    prices = pd.DataFrame(
        [
            _daily_price("2026-07-31T00:00:00Z", 99.0, 100.0),
            _daily_price("2026-08-03T00:00:00Z", 101.0, 102.0),
            _daily_price("2026-08-04T00:00:00Z", 102.0, 101.0),
            _daily_price("2026-08-05T00:00:00Z", 103.0, 105.0),
            _daily_price("2026-08-06T00:00:00Z", 105.0, 104.0),
            _daily_price("2026-08-07T00:00:00Z", 106.0, 108.0),
        ]
    )
    observed: dict[str, pd.Series] = {}
    for route in WEEKLY_HORIZON_ORDER:
        observed[route] = build_rolling_samples(
            _feature(
                horizon=route,
                session="2026-07-31",
                bar_start="2026-07-31T00:00:00Z",
                bar_end="2026-07-31T20:00:00Z",
                decision="2026-07-31T20:05:00Z",
            ),
            prices,
            specification=horizon_specification(route),
            assumed_round_trip_cost=0.001,
            materialized_at="2026-08-07T20:05:00Z",
        ).iloc[0]

    starts = pd.to_datetime(
        [
            "2026-08-03T13:30:00Z",
            "2026-08-04T13:30:00Z",
            "2026-08-05T13:30:00Z",
            "2026-08-06T13:30:00Z",
            "2026-08-07T13:30:00Z",
        ],
        utc=True,
    )
    ends = pd.to_datetime(
        [
            "2026-08-03T20:00:00Z",
            "2026-08-04T20:00:00Z",
            "2026-08-05T20:00:00Z",
            "2026-08-06T20:00:00Z",
            "2026-08-07T20:00:00Z",
        ],
        utc=True,
    )
    aggregate = observed["1w"]
    assert aggregate["target_window_start"] == starts[0]
    assert aggregate["target_window_end"] == ends[-1]
    assert aggregate["target_open"] == 101.0
    assert aggregate["target_close"] == 108.0
    assert aggregate["forward_raw_return"] == 108.0 / 101.0 - 1.0
    for lead in range(1, 6):
        component = observed[f"1w-d{lead}"]
        assert component["target_window_start"] == starts[lead - 1]
        assert component["target_window_end"] == ends[lead - 1]
        assert component["actionable_until"] == ends[lead - 1]
        assert component["label_available_at"] == (
            ends[lead - 1] + pd.Timedelta(minutes=5)
        )
    assert aggregate["actionable_until"] == ends[0]


def test_aggregate_ends_with_the_first_target_exchange_week() -> None:
    prices = pd.DataFrame(
        [
            _daily_price("2026-05-22T00:00:00Z", 100.0, 101.0),
            _daily_price("2026-05-26T00:00:00Z", 102.0, 103.0),
            _daily_price("2026-05-27T00:00:00Z", 103.0, 104.0),
            _daily_price("2026-05-28T00:00:00Z", 104.0, 105.0),
            _daily_price("2026-05-29T00:00:00Z", 105.0, 106.0),
            _daily_price("2026-06-01T00:00:00Z", 106.0, 107.0),
        ]
    )
    feature = _feature(
        horizon="1w",
        session="2026-05-22",
        bar_start="2026-05-22T00:00:00Z",
        bar_end="2026-05-22T20:00:00Z",
        decision="2026-05-22T20:05:00Z",
    )
    aggregate = build_rolling_samples(
        feature,
        prices,
        specification=horizon_specification("1w"),
        assumed_round_trip_cost=0.001,
        materialized_at="2026-06-01T20:05:00Z",
    ).iloc[0]
    assert aggregate["target_window_start"] == pd.Timestamp(
        "2026-05-26T13:30:00Z"
    )
    assert aggregate["target_window_end"] == pd.Timestamp(
        "2026-05-29T20:00:00Z"
    )


def test_component_windows_use_early_close_and_dst_exchange_schedule() -> None:
    early = build_rolling_samples(
        _feature(
            horizon="1w-d1",
            session="2026-11-25",
            bar_start="2026-11-25T00:00:00Z",
            bar_end="2026-11-25T21:00:00Z",
            decision="2026-11-25T21:05:00Z",
        ),
        pd.DataFrame(
            [
                _daily_price("2026-11-25T00:00:00Z", 100.0, 101.0),
                _daily_price("2026-11-27T00:00:00Z", 102.0, 104.0),
            ]
        ),
        specification=horizon_specification("1w-d1"),
        assumed_round_trip_cost=0.001,
        materialized_at="2026-11-27T18:05:00Z",
    ).iloc[0]
    assert early["target_window_start"] == pd.Timestamp(
        "2026-11-27T14:30:00Z"
    )
    assert early["target_window_end"] == pd.Timestamp(
        "2026-11-27T18:00:00Z"
    )

    dst = build_rolling_samples(
        _feature(
            horizon="1w-d1",
            session="2026-10-30",
            bar_start="2026-10-30T00:00:00Z",
            bar_end="2026-10-30T20:00:00Z",
            decision="2026-10-30T20:05:00Z",
        ),
        pd.DataFrame(
            [
                _daily_price("2026-10-30T00:00:00Z", 100.0, 101.0),
                _daily_price("2026-11-02T00:00:00Z", 102.0, 103.0),
            ]
        ),
        specification=horizon_specification("1w-d1"),
        assumed_round_trip_cost=0.001,
        materialized_at="2026-11-02T21:05:00Z",
    ).iloc[0]
    assert dst["target_window_start"] == pd.Timestamp(
        "2026-11-02T14:30:00Z"
    )
    assert dst["target_window_end"] == pd.Timestamp(
        "2026-11-02T21:00:00Z"
    )


def test_weekly_routes_materialize_every_eligible_daily_decision() -> None:
    sessions = ("2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30", "2026-07-31")
    features = pd.concat(
        [
            _feature(
                horizon="1w",
                session=session,
                bar_start=f"{session}T00:00:00Z",
                bar_end=f"{session}T20:00:00Z",
                decision=f"{session}T20:05:00Z",
            )
            for session in sessions
        ],
        ignore_index=True,
    )
    prices = pd.DataFrame(
        [
            _daily_price(f"{session}T00:00:00Z", 100.0 + offset, 101.0 + offset)
            for offset, session in enumerate((*sessions, "2026-08-03"))
        ]
    )

    for route in WEEKLY_HORIZON_ORDER:
        route_features = features.copy()
        route_features["horizon"] = route
        samples = build_rolling_samples(
            route_features,
            prices,
            specification=horizon_specification(route),
            assumed_round_trip_cost=0.001,
            materialized_at="2026-08-03T20:05:00Z",
        )
        assert len(samples) == len(sessions)
        assert samples["decision_timestamp"].nunique() == len(sessions)
        assert samples["id"].str.startswith(f"GOOG|{route}|").all()


def test_weekly_routes_mature_independently() -> None:
    prices = pd.DataFrame(
        [
            _daily_price("2026-07-31T00:00:00Z", 100.0, 101.0),
            _daily_price("2026-08-03T00:00:00Z", 102.0, 103.0),
            _daily_price("2026-08-04T00:00:00Z", 103.0, 102.0),
            _daily_price("2026-08-05T00:00:00Z", 104.0, 105.0),
        ]
    )
    statuses: dict[str, str] = {}
    for route in WEEKLY_HORIZON_ORDER:
        sample = build_rolling_samples(
            _feature(
                horizon=route,
                session="2026-07-31",
                bar_start="2026-07-31T00:00:00Z",
                bar_end="2026-07-31T20:00:00Z",
                decision="2026-07-31T20:05:00Z",
            ),
            prices,
            specification=horizon_specification(route),
            assumed_round_trip_cost=0.0,
            materialized_at="2026-08-05T20:05:00Z",
        ).iloc[0]
        statuses[route] = str(sample["label_status"])
    assert statuses == {
        "1w": "INCOMPLETE_LABEL",
        "1w-d1": "COMPLETE",
        "1w-d2": "COMPLETE",
        "1w-d3": "COMPLETE",
        "1w-d4": "INCOMPLETE_LABEL",
        "1w-d5": "INCOMPLETE_LABEL",
    }


def _feature(
    *,
    horizon: str,
    session: str,
    bar_start: str,
    bar_end: str,
    decision: str,
) -> pd.DataFrame:
    decision_timestamp = pd.Timestamp(decision)
    return pd.DataFrame(
        {
            "id": [f"GOOG|{decision_timestamp.isoformat()}"],
            "symbol": ["GOOG"],
            "venue": ["NASDAQ"],
            "currency": ["USD"],
            "provider": ["databento"],
            "exchange_calendar": ["XNAS"],
            "exchange_session": [pd.Timestamp(session)],
            "bar_timestamp": [pd.Timestamp(bar_start)],
            "bar_end_timestamp": [pd.Timestamp(bar_end)],
            "decision_timestamp": [decision_timestamp],
            "feature_available_at": [decision_timestamp],
            "feature_set": [horizon_specification(horizon).feature_set],
        }
    )


def _hour_price(timestamp: str, open_value: float, close_value: float) -> dict[str, object]:
    start = pd.Timestamp(timestamp)
    return {
        "symbol": "GOOG",
        "provider": "databento",
        "timestamp": timestamp,
        "bar_end_timestamp": start + pd.Timedelta(hours=1),
        "open": open_value,
        "close": close_value,
    }


def _minute_prices(
    start: str,
    count: int,
    *,
    open_value: float,
    close_value: float,
) -> list[dict[str, object]]:
    timestamps = pd.date_range(start, periods=count, freq="min")
    return [
        {
            "symbol": "GOOG",
            "provider": "databento",
            "timeframe": "1m",
            "timestamp": timestamp,
            "bar_end_timestamp": timestamp + pd.Timedelta(minutes=1),
            "open": open_value,
            "close": close_value if index == count - 1 else open_value,
        }
        for index, timestamp in enumerate(timestamps)
    ]


def _daily_price(timestamp: str, open_value: float, close_value: float) -> dict[str, object]:
    return {
        "symbol": "GOOG",
        "provider": "databento",
        "timestamp": timestamp,
        "open": open_value,
        "close": close_value,
    }
