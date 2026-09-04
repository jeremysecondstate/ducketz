from __future__ import annotations

import inspect
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.ui.rolling_forecast_data import (
    ForecastDataError,
    ForecastRefreshState,
    FORECAST_SUBTITLE,
    HORIZON_LABELS,
    HORIZON_ORDER,
    LEGACY_INTELLIGENCE_SCHEMA,
    STANDARD_HORIZON_ORDER,
    SUPPORTED_HORIZON_ORDER,
    WEEKLY_HORIZON_ORDER,
    _live_performance_by_route,
    dashboard_debug_text,
    dashboard_layout,
    format_session_date,
    format_timestamp_local,
    format_timestamp_utc,
    load_forecast_dashboard,
    route_accessible_status_labels,
    route_live_performance_labels,
    route_outcome_evidence_label,
    route_publication_summary,
)
from app.ui.rolling_forecasts import (
    HOURLY_AUTO_REFRESH_MS,
    RollingForecastTab,
    evidence_progress_fraction,
    forecast_symbol_header_text,
    forecast_symbol_section_summary,
    live_performance_lift_tone,
    merge_symbol_expansion_state,
    prediction_pulse_columns,
    prediction_pulse_mark_path,
    prediction_pulse_probabilities,
    prediction_pulse_probability_text,
    prediction_pulse_tone,
    probability_segment_fractions,
    rolling_performance_heading,
    weekly_session_details_header_text,
)
from ml.live_evidence import minimum_live_decisions
from ml.parquet_contracts import (
    EVALUATION_SCHEMA,
    INTELLIGENCE_SCHEMA,
    write_parquet_with_schema,
)


def test_supported_horizon_order_labels_and_subtitle_are_exact() -> None:
    assert HORIZON_ORDER == ("1h", "4h", "1d", "1w")
    assert SUPPORTED_HORIZON_ORDER == (
        "1h",
        "4h",
        "1d",
        "1w",
        "1w-d1",
        "1w-d2",
        "1w-d3",
        "1w-d4",
        "1w-d5",
    )
    assert HORIZON_LABELS["4h"] == "4 Hour"
    assert f"{HORIZON_LABELS['4h']} Forecast" == "4 Hour Forecast"
    assert HORIZON_LABELS["1w"] == "Remaining-Week Aggregate"
    assert FORECAST_SUBTITLE == (
        "Read-only 1h, 4h, 1d, and dynamic remaining-week probability outlooks. "
        "Probabilities are not recommendations."
    )


def test_collapsed_symbol_summary_and_header_copy_are_explicit() -> None:
    summary = forecast_symbol_section_summary(
        "aapl",
        forecast_count=3,
        remaining_week_available=True,
    )

    assert summary == "AAPL · 3 Forecasts · Remaining-Week Snapshot Available"
    assert forecast_symbol_header_text(
        "aapl",
        expanded=True,
        collapsed_summary=summary,
    ) == "▼ AAPL"
    assert forecast_symbol_header_text(
        "aapl",
        expanded=False,
        collapsed_summary=summary,
    ) == f"▶ {summary}"
    assert forecast_symbol_section_summary(
        "msft",
        forecast_count=1,
        remaining_week_available=False,
    ) == "MSFT · 1 Forecast · No Remaining-Week Snapshot"


@pytest.mark.parametrize(
    ("probability", "tone", "text"),
    (
        (0.5001, "up", "50.0%"),
        (0.5, "neutral", "50.0%"),
        (0.4999, "down", "50.0%"),
        (0.0, "down", "0.0%"),
        (1.0, "up", "100.0%"),
        (None, "unavailable", "N/A"),
        (float("nan"), "unavailable", "N/A"),
        (-0.01, "unavailable", "N/A"),
        (1.01, "unavailable", "N/A"),
    ),
)
def test_prediction_pulse_thresholds_do_not_invent_values(
    probability: float | None,
    tone: str,
    text: str,
) -> None:
    assert prediction_pulse_tone(probability) == tone
    assert prediction_pulse_probability_text(probability) == text


@pytest.mark.parametrize(
    ("width", "columns"),
    (
        (1900, 6),
        (1180, 6),
        (1039, 3),
        (720, 3),
        (719, 2),
        (520, 2),
        (519, 1),
    ),
)
def test_prediction_pulse_stacks_at_narrow_widths(
    width: int,
    columns: int,
) -> None:
    assert prediction_pulse_columns(width) == columns


def test_prediction_pulse_uses_standard_routes_in_horizon_order(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        [
            _row(horizon="1d", probability_up=0.48),
            _row(horizon="1h", probability_up=0.62),
            _row(horizon="4h", probability_up=0.50),
        ],
    )
    routes = load_forecast_dashboard(
        path,
        loaded_at=datetime(2026, 7, 27, 14, 45, tzinfo=timezone.utc),
    ).symbols[0].routes

    assert prediction_pulse_probabilities(routes) == (
        ("1h", pytest.approx(0.62)),
        ("4h", pytest.approx(0.50)),
        ("1d", pytest.approx(0.48)),
    )


def test_prediction_pulse_reuses_local_security_marks() -> None:
    aapl = prediction_pulse_mark_path("aapl")

    assert aapl is not None
    assert aapl.name == "aapl.png"
    assert prediction_pulse_mark_path("unknown") is None


def test_symbol_expansion_state_survives_refresh_and_new_symbols_default_open() -> None:
    prior = {"AAPL": False, "REMOVED": False}

    refreshed = merge_symbol_expansion_state(prior, ("AAPL", "MSFT"))

    assert refreshed["AAPL"] is False
    assert refreshed["MSFT"] is True
    assert refreshed["REMOVED"] is False
    second_refresh = merge_symbol_expansion_state(refreshed, ("MSFT", "NVDA"))
    assert second_refresh["MSFT"] is True
    assert second_refresh["NVDA"] is True
    assert second_refresh["AAPL"] is False


@pytest.mark.parametrize(
    ("probability_up", "probability_down", "expected"),
    (
        (0.43, 0.57, pytest.approx((0.43, 0.57))),
        (0.40, 0.40, pytest.approx((0.50, 0.50))),
        (0.40, 0.60, pytest.approx((0.40, 0.60))),
        (0.40, None, None),
        (None, 0.60, None),
        (float("nan"), 0.60, None),
        (0.0, 0.0, None),
    ),
)
def test_probability_segments_use_both_published_values_or_render_unavailable(
    probability_up: float | None,
    probability_down: float | None,
    expected: object,
) -> None:
    result = probability_segment_fractions(probability_up, probability_down)

    if expected is None:
        assert result is None
    else:
        assert result == expected


@pytest.mark.parametrize(
    ("completed", "minimum", "expected"),
    (
        (7, 60, pytest.approx(7 / 60)),
        (60, 60, 1.0),
        (83, 60, 1.0),
        (0, 60, 0.0),
        (None, 60, None),
        (7, 0, None),
    ),
)
def test_evidence_progress_clamps_geometry_without_rewriting_counts(
    completed: int | None,
    minimum: int,
    expected: float | None,
) -> None:
    assert evidence_progress_fraction(completed, minimum) == expected


@pytest.mark.parametrize(
    ("lift", "tone"),
    (
        (0.01, "success"),
        (0.0, "neutral"),
        (-0.01, "danger"),
        (None, "neutral"),
    ),
)
def test_live_performance_lift_tones_keep_zero_and_unavailable_neutral(
    lift: float | None,
    tone: str,
) -> None:
    assert live_performance_lift_tone(lift) == tone


def test_partial_rolling_window_heading_uses_numeric_counts() -> None:
    assert rolling_performance_heading(8, 30) == "ROLLING 8/30"
    assert rolling_performance_heading(30, 30) == "ROLLING 30"
    assert rolling_performance_heading(83, 60) == "ROLLING 60"


def test_three_standard_cards_and_complete_weekly_outlook_are_grouped(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        [
            *_weekly_rows(),
            _row(horizon="1h", probability_up=0.61),
            _row(horizon="4h", probability_up=0.59),
            _row(horizon="1d", probability_up=0.57),
        ],
    )

    view = load_forecast_dashboard(
        path,
        loaded_at=datetime(2026, 7, 27, 14, 45, tzinfo=timezone.utc),
    )

    assert view.actionable_route_count == 3
    assert view.published_route_count == 9
    assert view.frozen_weekly_snapshot_count == 1
    assert [symbol.symbol for symbol in view.symbols] == ["GOOG"]
    assert [
        route.horizon for route in view.symbols[0].routes
    ] == ["1h", "4h", "1d", "1w"]
    assert [
        route.probability_up for route in view.symbols[0].routes
    ] == [0.61, 0.59, 0.57, 0.54]
    assert view.symbols[0].routes[-1].horizon_label == "Remaining-Week Aggregate"
    assert view.symbols[0].weekly_outlook is not None
    assert [
        route.horizon for route in view.symbols[0].weekly_outlook.sessions
    ] == ["1w-d1", "1w-d2", "1w-d3", "1w-d4", "1w-d5"]
    assert view.freshness_label == "Data Pipeline Is Current"


def test_missing_standard_route_warns_while_frozen_weekly_outlook_remains(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        [
            *_weekly_rows(),
            _row(horizon="1h", probability_up=0.61),
            _row(horizon="4h", probability_up=0.59),
        ],
    )

    view = load_forecast_dashboard(
        path,
        loaded_at=datetime(2026, 7, 27, 14, 45, tzinfo=timezone.utc),
    )

    assert view.freshness_label == "Current Outlooks with Route Gaps"
    assert view.freshness_tone == "warning"
    assert view.operational_label == "Operational with Route Timing Gaps"
    assert view.operational_tone == "warning"
    assert view.frozen_weekly_snapshot_count == 1
    assert view.symbols[0].weekly_outlook is not None
    assert view.symbols[0].routes[2].is_missing


def test_monday_decision_renders_dynamic_tuesday_through_friday_outlook(
    tmp_path: Path,
) -> None:
    rows = _weekly_rows()[:5]
    windows = (
        ("2026-08-04T13:30:00Z", "2026-08-04T20:00:00Z"),
        ("2026-08-05T13:30:00Z", "2026-08-05T20:00:00Z"),
        ("2026-08-06T13:30:00Z", "2026-08-06T20:00:00Z"),
        ("2026-08-07T13:30:00Z", "2026-08-07T20:00:00Z"),
    )
    for index, row in enumerate(rows):
        horizon = str(row["horizon"])
        start, end = windows[0] if horizon == "1w" else windows[index - 1]
        row.update(
            {
                "id": f"GOOG|{horizon}|2026-08-03T20:05:00Z",
                "decision_timestamp": "2026-08-03T20:05:00Z",
                "forecast_created_at": "2026-08-03T20:06:00Z",
                "information_available_at": "2026-08-03T20:05:00Z",
                "target_window_start": start,
                "target_window_end": windows[-1][1] if horizon == "1w" else end,
                "actionable_until": windows[0][1] if horizon == "1w" else end,
            }
        )
    rows.append(
        _row(
            horizon="1w-d5",
            actionability_status="NO_ACTIONABLE_CANDIDATE",
        )
    )

    view = load_forecast_dashboard(_write(tmp_path, rows))
    outlook = view.symbols[0].weekly_outlook

    assert outlook is not None
    assert outlook.aggregate.target_window_start == datetime(
        2026, 8, 4, 13, 30, tzinfo=timezone.utc
    )
    assert outlook.aggregate.target_window_end == datetime(
        2026, 8, 7, 20, 0, tzinfo=timezone.utc
    )
    assert [route.horizon for route in outlook.sessions] == [
        "1w-d1",
        "1w-d2",
        "1w-d3",
        "1w-d4",
    ]


def test_legacy_short_horizon_output_has_missing_cards(
    tmp_path: Path,
) -> None:
    path = _write_legacy(
        tmp_path,
        [_row(horizon=horizon) for horizon in ("1h", "1d")],
    )

    view = load_forecast_dashboard(
        path,
        loaded_at=datetime(2026, 7, 27, 14, 45, tzinfo=timezone.utc),
    )
    routes = view.symbols[0].routes

    assert [route.horizon for route in routes] == [
        "1h",
        "4h",
        "1d",
        "1w",
    ]
    assert routes[1].is_missing
    assert routes[1].actionability_status == "MISSING_HORIZON"
    assert view.published_route_count == 2
    assert view.actionable_route_count == 2
    assert view.schema_version == "one-id-v1"
    assert routes[0].minimum_live_decision_count == 60
    assert view.symbols[0].weekly_outlook is None


def test_mixed_actionable_and_unavailable_horizons_preserve_statuses(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        [
            _row(horizon="1h", probability_up=0.61),
            _row(
                horizon="1d",
                actionability_status="MODEL_UNAVAILABLE",
                operational_status="MODEL_UNAVAILABLE",
                model_evidence_status="MODEL_UNAVAILABLE",
            ),
        ],
    )

    view = load_forecast_dashboard(
        path,
        loaded_at=datetime(2026, 7, 27, 14, 45, tzinfo=timezone.utc),
    )
    routes = {
        route.horizon: route for route in view.symbols[0].routes
    }

    assert routes["1h"].actionability_label == "Current Forecast"
    assert routes["1d"].actionability_label == "Model Unavailable"
    assert routes["1d"].probability_up is None
    assert routes["1w"].is_missing
    assert view.operational_label == "Operational with Limitations"


def test_missing_symbol_horizon_is_an_explicit_empty_route(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        [
            _row(horizon="1h"),
            _row(horizon="1d"),
        ],
    )

    view = load_forecast_dashboard(path)
    four_hour = view.symbols[0].routes[1]
    weekly = view.symbols[0].routes[3]

    assert four_hour.horizon == "4h"
    assert four_hour.is_missing
    assert four_hour.horizon_label == "4 Hour"
    assert weekly.horizon == "1w"
    assert weekly.is_missing
    assert weekly.actionability_label == "No Current Forecast"
    assert weekly.probability_up is None


def test_no_actionable_output_has_a_clear_limited_state(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        [
            _row(
                horizon=horizon,
                actionability_status="NO_ACTIONABLE_CANDIDATE",
            )
            for horizon in STANDARD_HORIZON_ORDER
        ],
    )

    view = load_forecast_dashboard(path)

    assert view.actionable_route_count == 0
    assert (
        view.empty_message
        == "The data pipeline is current, but there is no actionable forecast."
    )
    assert all(
        route.probability_up is None
        for route in view.symbols[0].routes
    )


def test_null_probability_is_never_fabricated(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        [_row(horizon="1h", probability_up=None)],
    )

    view = load_forecast_dashboard(
        path,
        loaded_at=datetime(2026, 7, 27, 14, 45, tzinfo=timezone.utc),
    )
    route = view.symbols[0].routes[0]

    assert route.is_actionable
    assert route.probability_up is None
    assert route.probability_down is None
    assert "probability is null" in route.warnings[0]


def test_non_actionable_probability_is_suppressed_as_stale(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        [
            _row(
                horizon="1h",
                actionability_status="TARGET_WINDOW_STARTED",
                probability_up=0.91,
                retain_probability=True,
            )
        ],
    )

    view = load_forecast_dashboard(path)
    route = view.symbols[0].routes[0]

    assert route.probability_up is None
    assert route.probability_down is None
    assert "dashboard suppressed it" in route.warnings[0]


def test_trusted_active_window_forecasts_remain_visible_but_not_actionable(
    tmp_path: Path,
) -> None:
    one_hour = _row(
        horizon="1h",
        probability_up=0.61,
        actionability_status="TARGET_WINDOW_STARTED",
        intelligence_status="FORECAST_IN_PROGRESS",
        completed_count=0,
        retain_probability=True,
    )
    one_hour.update(
        {
            "forecast_created_at": "2026-08-05T15:42:00Z",
            "information_available_at": "2026-08-05T15:05:00Z",
            "target_window_start": "2026-08-05T16:00:00Z",
            "target_window_end": "2026-08-05T17:00:00Z",
            "actionable_until": "2026-08-05T16:00:00Z",
        }
    )
    four_hour = _row(
        horizon="4h",
        probability_up=0.58,
        actionability_status="TARGET_WINDOW_STARTED",
        intelligence_status="FORECAST_IN_PROGRESS",
        completed_count=0,
        retain_probability=True,
    )
    four_hour.update(
        {
            "forecast_created_at": "2026-08-05T15:42:00Z",
            "information_available_at": "2026-08-05T15:05:00Z",
            "target_window_start": "2026-08-05T16:00:00Z",
            "target_window_end": "2026-08-05T20:00:00Z",
            "actionable_until": "2026-08-05T16:00:00Z",
        }
    )
    one_day = _row(
        horizon="1d",
        probability_up=0.55,
        actionability_status="TARGET_WINDOW_STARTED",
        intelligence_status="FORECAST_IN_PROGRESS",
        completed_count=0,
        retain_probability=True,
    )
    one_day.update(
        {
            "forecast_created_at": "2026-08-04T20:10:00Z",
            "information_available_at": "2026-08-04T20:05:00Z",
            "target_window_start": "2026-08-05T13:30:00Z",
            "target_window_end": "2026-08-05T20:00:00Z",
            "actionable_until": "2026-08-05T13:30:00Z",
        }
    )
    path = _write(tmp_path, [one_hour, four_hour, one_day])

    view = load_forecast_dashboard(
        path,
        loaded_at=datetime(2026, 8, 5, 16, 10, tzinfo=timezone.utc),
    )
    routes = {route.horizon: route for route in view.symbols[0].routes}

    assert view.actionable_route_count == 0
    assert [routes[horizon].probability_up for horizon in ("1h", "4h", "1d")] == [
        0.61,
        0.58,
        0.55,
    ]
    assert all(
        routes[horizon].is_in_progress
        and not routes[horizon].is_actionable
        and routes[horizon].automated_action_allowed is False
        for horizon in ("1h", "4h", "1d")
    )
    assert route_accessible_status_labels(routes["1h"])[0] == (
        "Actionability: Forecast in Progress — Entry Window Passed; "
        "Not Actionable"
    )
    assert routes["1h"].live_evidence_label == (
        "Awaiting First Completed Forecast (0 of 60)"
    )
    assert routes["1d"].live_evidence_label == (
        "Awaiting First Completed Forecast (0 of 30)"
    )
    assert routes["1h"].target_window_start == datetime(
        2026, 8, 5, 16, 0, tzinfo=timezone.utc
    )
    assert routes["1h"].target_window_end == datetime(
        2026, 8, 5, 17, 0, tzinfo=timezone.utc
    )


def test_in_progress_probability_is_suppressed_at_target_window_end(
    tmp_path: Path,
) -> None:
    row = _row(
        horizon="1h",
        probability_up=0.61,
        actionability_status="TARGET_WINDOW_STARTED",
        intelligence_status="FORECAST_IN_PROGRESS",
        retain_probability=True,
    )
    row.update(
        {
            "forecast_created_at": "2026-08-05T15:42:00Z",
            "target_window_start": "2026-08-05T16:00:00Z",
            "target_window_end": "2026-08-05T17:00:00Z",
            "actionable_until": "2026-08-05T16:00:00Z",
        }
    )
    route = load_forecast_dashboard(
        _write(tmp_path, [row]),
        loaded_at=datetime(2026, 8, 5, 17, 0, tzinfo=timezone.utc),
    ).symbols[0].routes[0]

    assert route.published_actionability_status == "TARGET_WINDOW_STARTED"
    assert route.actionability_status == "TARGET_WINDOW_PASSED"
    assert route.published_intelligence_status == "FORECAST_IN_PROGRESS"
    assert route.probability_up is None
    assert route.probability_down is None
    assert route.automated_action_allowed is False
    assert not route.is_in_progress
    assert "dashboard suppressed it" in route.warnings[0]


def test_actionable_route_remains_actionable_before_deadline(
    tmp_path: Path,
) -> None:
    row = _row(horizon="4h", probability_up=0.61)
    row.update(
        {
            "forecast_created_at": "2026-08-05T15:42:00Z",
            "target_window_start": "2026-08-05T16:00:00Z",
            "target_window_end": "2026-08-05T20:00:00Z",
            "actionable_until": "2026-08-05T16:00:00Z",
            "automated_action_allowed": True,
        }
    )

    view = load_forecast_dashboard(
        _write(tmp_path, [row]),
        loaded_at=datetime(2026, 8, 5, 15, 59, tzinfo=timezone.utc),
    )
    route = view.symbols[0].routes[1]

    assert route.published_actionability_status == "ACTIONABLE"
    assert route.actionability_status == "ACTIONABLE"
    assert route.published_intelligence_status == "RISK_ANALYSIS_SUPPORT"
    assert route.intelligence_status == "RISK_ANALYSIS_SUPPORT"
    assert route.probability_up == pytest.approx(0.61)
    assert route.published_automated_action_allowed is True
    assert route.automated_action_allowed is True
    assert view.automated_action_allowed is True
    assert view.actionable_route_count == 1


def test_actionable_route_becomes_read_only_in_progress_at_deadline(
    tmp_path: Path,
) -> None:
    row = _row(horizon="4h", probability_up=0.61)
    row.update(
        {
            "forecast_created_at": "2026-08-05T15:42:00Z",
            "target_window_start": "2026-08-05T16:05:00Z",
            "target_window_end": "2026-08-05T20:00:00Z",
            "actionable_until": "2026-08-05T16:00:00Z",
            "automated_action_allowed": True,
        }
    )

    view = load_forecast_dashboard(
        _write(tmp_path, [row]),
        loaded_at=datetime(2026, 8, 5, 16, 0, tzinfo=timezone.utc),
    )
    route = view.symbols[0].routes[1]

    assert route.published_actionability_status == "ACTIONABLE"
    assert route.actionability_status == "TARGET_WINDOW_STARTED"
    assert route.published_intelligence_status == "RISK_ANALYSIS_SUPPORT"
    assert route.intelligence_status == "FORECAST_IN_PROGRESS"
    assert route.probability_up == pytest.approx(0.61)
    assert route.probability_down == pytest.approx(0.39)
    assert route.published_automated_action_allowed is True
    assert route.automated_action_allowed is False
    assert view.automated_action_allowed is False
    assert view.automation_label == "Automated action is off"
    assert route.is_in_progress
    assert not route.is_actionable
    assert view.actionable_route_count == 0


def test_actionable_route_requires_ordered_timestamps_for_ui_transition(
    tmp_path: Path,
) -> None:
    row = _row(horizon="4h", probability_up=0.61)
    row.update(
        {
            "forecast_created_at": "2026-08-05T15:42:00Z",
            "target_window_start": "2026-08-05T16:00:00Z",
            "target_window_end": "2026-08-05T20:00:00Z",
            "actionable_until": "2026-08-05T16:05:00Z",
            "automated_action_allowed": True,
        }
    )

    view = load_forecast_dashboard(
        _write(tmp_path, [row]),
        loaded_at=datetime(2026, 8, 5, 16, 10, tzinfo=timezone.utc),
    )
    route = view.symbols[0].routes[1]

    assert route.published_actionability_status == "ACTIONABLE"
    assert route.actionability_status == "TARGET_TIMESTAMP_INVALID"
    assert route.probability_up is None
    assert route.published_automated_action_allowed is True
    assert route.automated_action_allowed is False
    assert view.actionable_route_count == 0
    assert view.automated_action_allowed is False


def test_actionable_route_is_suppressed_at_target_window_end(
    tmp_path: Path,
) -> None:
    row = _row(horizon="4h", probability_up=0.61)
    row.update(
        {
            "forecast_created_at": "2026-08-05T15:42:00Z",
            "target_window_start": "2026-08-05T16:00:00Z",
            "target_window_end": "2026-08-05T20:00:00Z",
            "actionable_until": "2026-08-05T16:00:00Z",
            "automated_action_allowed": True,
        }
    )

    view = load_forecast_dashboard(
        _write(tmp_path, [row]),
        loaded_at=datetime(2026, 8, 5, 20, 0, tzinfo=timezone.utc),
    )
    route = view.symbols[0].routes[1]

    assert route.published_actionability_status == "ACTIONABLE"
    assert route.actionability_status == "TARGET_WINDOW_PASSED"
    assert route.published_intelligence_status == "RISK_ANALYSIS_SUPPORT"
    assert route.probability_up is None
    assert route.probability_down is None
    assert route.published_automated_action_allowed is True
    assert route.automated_action_allowed is False
    assert view.automated_action_allowed is False
    assert not route.is_in_progress
    assert not route.is_actionable
    assert view.actionable_route_count == 0
    assert "dashboard suppressed it" in route.warnings[0]


def test_expired_standard_probability_downgrades_current_dashboard(
    tmp_path: Path,
) -> None:
    expired = _row(horizon="4h", probability_up=0.61)
    expired["target_window_end"] = "2026-07-27T15:30:00Z"
    path = _write(
        tmp_path,
        [
            _row(horizon="1h", probability_up=0.63),
            expired,
            _row(horizon="1d", probability_up=0.57),
        ],
    )

    view = load_forecast_dashboard(
        path,
        loaded_at=datetime(2026, 7, 27, 15, 30, tzinfo=timezone.utc),
    )
    routes = {route.horizon: route for route in view.symbols[0].routes}

    assert routes["4h"].actionability_status == "TARGET_WINDOW_PASSED"
    assert routes["4h"].probability_up is None
    assert view.freshness_label == "Current Outlooks with Route Gaps"
    assert view.freshness_tone == "warning"
    assert view.operational_label == "Operational with Route Timing Gaps"
    assert view.operational_tone == "warning"


def test_stale_data_uses_backend_operational_status(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        [
            _row(
                horizon="1h",
                actionability_status="NO_ACTIONABLE_CANDIDATE",
                operational_status="OPERATIONALLY_STALE",
            )
        ],
    )

    view = load_forecast_dashboard(path)

    assert view.freshness_label == "Data Is Stale"
    assert view.freshness_tone == "danger"
    assert view.operational_label == "Operational Data Is Stale"


def test_current_weekly_outlook_with_stale_live_routes_is_not_global_failure(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        [
            *_weekly_rows(),
            *(
                _row(
                    horizon=horizon,
                    actionability_status="NOT_ACTIONABLE",
                    operational_status="OPERATIONALLY_STALE",
                )
                for horizon in STANDARD_HORIZON_ORDER
            ),
        ],
    )

    view = load_forecast_dashboard(path)

    assert view.freshness_label == "Current Outlooks with Route Gaps"
    assert view.freshness_tone == "warning"
    assert view.operational_label == "Operational with Route Timing Gaps"
    assert view.operational_tone == "warning"
    assert route_publication_summary(view) == (
        "0 live routes; 1 current remaining-week outlook; 9 published rows"
    )


def test_unsupported_schema_version_is_structured(
    tmp_path: Path,
) -> None:
    row = _row()
    row["schema_version"] = "one-id-v999"
    path = _write(tmp_path, [row])

    with pytest.raises(ForecastDataError) as caught:
        load_forecast_dashboard(path)

    assert caught.value.code == "UNSUPPORTED_SCHEMA_VERSION"
    assert "Newer App Version" in caught.value.title


def test_missing_file_is_structured(tmp_path: Path) -> None:
    path = tmp_path / "missing.parquet"

    with pytest.raises(ForecastDataError) as caught:
        load_forecast_dashboard(path)

    assert caught.value.code == "MISSING_FILE"
    assert caught.value.path == path


def test_corrupt_file_is_structured(tmp_path: Path) -> None:
    path = tmp_path / "rolling-predictions.parquet"
    path.write_bytes(b"not a parquet file")

    with pytest.raises(ForecastDataError) as caught:
        load_forecast_dashboard(path)

    assert caught.value.code == "UNREADABLE_FILE"
    assert "corrupt" in caught.value.message


def test_operationally_current_can_still_be_non_actionable(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        [
            _row(
                horizon=horizon,
                actionability_status="TARGET_WINDOW_STARTED",
                operational_status="OPERATIONALLY_CURRENT",
                intelligence_status="FORECAST_IN_PROGRESS",
                retain_probability=True,
            )
            for horizon in STANDARD_HORIZON_ORDER
        ],
    )

    view = load_forecast_dashboard(
        path,
        loaded_at=datetime(2026, 7, 27, 15, 30, tzinfo=timezone.utc),
    )

    assert view.operational_label == "Operationally Current"
    assert view.operational_tone == "success"
    assert view.actionable_route_count == 0
    assert "no actionable forecast" in view.empty_message.lower()


def test_model_identity_and_evidence_remain_in_debug_details(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        [
            _row(
                model_evidence_status="MODEL_SELECTION_PENDING",
            )
        ],
    )

    view = load_forecast_dashboard(path)
    route = view.symbols[0].routes[0]
    debug = dashboard_debug_text(view)

    assert route.model_name == "logistic-regression-1h"
    assert route.model_evidence_status == "MODEL_SELECTION_PENDING"
    assert "model_name: logistic-regression-1h" in debug
    assert "model_evidence_status: MODEL_SELECTION_PENDING" in debug


def test_insufficient_live_evidence_keeps_completed_count(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        [
            _row(
                live_evidence_status="INSUFFICIENT_LIVE_EVIDENCE",
                completed_count=7,
            )
        ],
    )

    route = load_forecast_dashboard(path).symbols[0].routes[0]

    assert route.live_evidence_label == "7 of 60 Completed Forecasts"
    assert route.completed_decision_count == 7
    assert route.minimum_live_decision_count == 60


@pytest.mark.parametrize(
    (
        "horizon",
        "completed_count",
        "live_evidence_status",
        "expected",
    ),
    (
        (
            "1h",
            0,
            "NO_COMPLETED_DECISIONS",
            "Awaiting First Completed Forecast (0 of 60)",
        ),
        (
            "1h",
            1,
            "INSUFFICIENT_LIVE_EVIDENCE",
            "1 of 60 Completed Forecasts",
        ),
        (
            "1h",
            59,
            "INSUFFICIENT_LIVE_EVIDENCE",
            "59 of 60 Completed Forecasts",
        ),
        (
            "1h",
            60,
            "LIVE_EVIDENCE_AVAILABLE",
            "60 of 60 Completed Forecasts",
        ),
        (
            "4h",
            60,
            "LIVE_EVIDENCE_AVAILABLE",
            "60 of 60 Completed Forecasts",
        ),
        (
            "1d",
            29,
            "INSUFFICIENT_LIVE_EVIDENCE",
            "29 of 30 Completed Forecasts",
        ),
        (
            "1d",
            30,
            "LIVE_EVIDENCE_AVAILABLE",
            "30 of 30 Completed Forecasts",
        ),
    ),
)
def test_live_evidence_wording_and_threshold_boundaries(
    tmp_path: Path,
    horizon: str,
    completed_count: int,
    live_evidence_status: str,
    expected: str,
) -> None:
    path = _write(
        tmp_path,
        [
            _row(
                horizon=horizon,
                completed_count=completed_count,
                live_evidence_status=live_evidence_status,
            )
        ],
    )

    route = next(
        item
        for item in load_forecast_dashboard(path).symbols[0].routes
        if item.horizon == horizon
    )

    assert route.minimum_live_decision_count == (
        minimum_live_decisions(horizon)
    )
    assert route.live_evidence_label == expected
    assert route_accessible_status_labels(route)[1] == (
        f"Live Evidence: {expected}"
    )


def test_live_performance_shows_cumulative_and_rolling_percentages(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        [
            _row(
                horizon="1d",
                live_evidence_status="LIVE_EVIDENCE_AVAILABLE",
                completed_count=32,
                minimum_count=30,
            )
        ],
    )
    evaluations = [
        _evaluation_row(
            horizon="1d",
            decision_index=index,
            observed_target=index % 2,
            correct=index >= 2,
        )
        for index in range(32)
    ]
    evaluations.append(
        _evaluation_row(
            horizon="1d",
            decision_index=0,
            observed_target=0,
            correct=True,
            publication_offset_minutes=30,
        )
    )
    write_parquet_with_schema(
        pd.DataFrame(evaluations),
        tmp_path / "evaluations.parquet",
        EVALUATION_SCHEMA,
    )

    route = next(
        item
        for item in load_forecast_dashboard(path).symbols[0].routes
        if item.horizon == "1d"
    )

    assert route.live_performance is not None
    assert route.live_performance.evidence_count == 32
    assert route.live_performance.scored_count == 32
    assert route.live_performance.correct_count == 30
    assert route.live_performance.rolling_window_size == 30
    assert route.live_performance.rolling_count == 30
    assert route.live_performance.rolling_correct_count == 30
    assert route_live_performance_labels(route) == (
        "Cumulative Live: Hit 93.8% (30/32) · Down-Only 50.0% · "
        "Lift +43.8 pp",
        "Rolling 30: Hit 100.0% (30/30) · Down-Only 50.0% · "
        "Lift +50.0 pp",
    )


def test_live_performance_counts_one_hour_sibling_targets_only() -> None:
    first_one_hour = _evaluation_row(
        horizon="1h",
        decision_index=0,
        observed_target=1,
        correct=True,
    )
    second_one_hour = {
        **first_one_hour,
        "id": f"{first_one_hour['id']}|later-target",
        "target_window_start": first_one_hour["target_window_start"]
        + pd.Timedelta(minutes=30),
        "target_window_end": first_one_hour["target_window_end"]
        + pd.Timedelta(minutes=30),
        "prediction_created_at": first_one_hour["prediction_created_at"]
        + pd.Timedelta(minutes=1),
    }
    first_four_hour = _evaluation_row(
        horizon="4h",
        decision_index=1,
        observed_target=0,
        correct=True,
    )
    second_four_hour = {
        **first_four_hour,
        "id": f"{first_four_hour['id']}|later-target",
        "target_window_start": first_four_hour["target_window_start"]
        + pd.Timedelta(hours=1),
        "target_window_end": first_four_hour["target_window_end"]
        + pd.Timedelta(hours=1),
        "prediction_created_at": first_four_hour["prediction_created_at"]
        + pd.Timedelta(minutes=1),
    }

    performance = _live_performance_by_route(
        pd.DataFrame(
            [
                first_one_hour,
                second_one_hour,
                first_four_hour,
                second_four_hour,
            ]
        )
    )

    assert performance[("GOOG", "1h")].evidence_count == 2
    assert performance[("GOOG", "4h")].evidence_count == 1


def test_live_performance_is_available_for_every_displayed_horizon(
    tmp_path: Path,
) -> None:
    standard_rows = [
        _row(
            horizon=horizon,
            live_evidence_status="INSUFFICIENT_LIVE_EVIDENCE",
            completed_count=1,
        )
        for horizon in STANDARD_HORIZON_ORDER
    ]
    weekly_rows = _weekly_rows()
    for row in weekly_rows:
        row["live_evidence_status"] = "INSUFFICIENT_LIVE_EVIDENCE"
        row["completed_decision_count"] = 1
    path = _write(tmp_path, [*standard_rows, *weekly_rows])
    write_parquet_with_schema(
        pd.DataFrame(
            [
                _evaluation_row(
                    horizon=horizon,
                    decision_index=index,
                    observed_target=index % 2,
                    correct=True,
                )
                for index, horizon in enumerate(SUPPORTED_HORIZON_ORDER)
            ]
        ),
        tmp_path / "evaluations.parquet",
        EVALUATION_SCHEMA,
    )

    symbol = load_forecast_dashboard(path).symbols[0]
    routes = {route.horizon: route for route in symbol.all_routes}

    assert set(routes) == set(SUPPORTED_HORIZON_ORDER)
    for horizon, route in routes.items():
        assert route.live_performance is not None
        assert route.live_performance.rolling_window_size == (
            minimum_live_decisions(horizon)
        )
        cumulative, rolling = route_live_performance_labels(route)
        assert cumulative.startswith("Cumulative Live: Hit ")
        assert rolling.startswith(
            f"Rolling 1/{minimum_live_decisions(horizon)}: Hit "
        )


def test_utc_and_local_timestamp_rendering() -> None:
    timestamp = datetime(
        2026,
        7,
        27,
        15,
        0,
        tzinfo=timezone.utc,
    )

    assert format_timestamp_utc(timestamp) == "Jul 27, 2026 15:00 UTC"
    assert format_timestamp_local(
        timestamp,
        local_timezone=ZoneInfo("America/Los_Angeles"),
    ) == "Jul 27, 2026 08:00 PDT"


def test_frozen_weekly_outlook_exposes_aggregate_and_five_dated_sessions(
    tmp_path: Path,
) -> None:
    statuses = (
        "PENDING_EVIDENCE",
        "COMPLETED_EVIDENCE",
        "COMPLETED_EVIDENCE",
        "PENDING_EVIDENCE",
        "PENDING_EVIDENCE",
        "PENDING_EVIDENCE",
    )
    path = _write(
        tmp_path,
        _weekly_rows(
            actionability_status="FROZEN_WEEKLY_SNAPSHOT",
            intelligence_statuses=statuses,
        ),
    )

    view = load_forecast_dashboard(path)
    symbol = view.symbols[0]
    outlook = symbol.weekly_outlook

    assert outlook is not None
    assert view.frozen_weekly_snapshot_count == 1
    assert view.empty_message is None
    assert outlook.issued_at == datetime(
        2026,
        7,
        31,
        20,
        6,
        tzinfo=timezone.utc,
    )
    assert outlook.aggregate.target_window_start == datetime(
        2026,
        8,
        3,
        13,
        30,
        tzinfo=timezone.utc,
    )
    assert outlook.aggregate.target_window_end == datetime(
        2026,
        8,
        7,
        20,
        0,
        tzinfo=timezone.utc,
    )
    assert [route.probability_up for route in outlook.routes] == [
        0.54,
        0.51,
        0.52,
        0.53,
        0.54,
        0.55,
    ]
    assert not outlook.aggregate.is_actionable
    assert [
        format_session_date(
            route.target_window_start,
            local_timezone=ZoneInfo("America/New_York"),
        )
        for route in outlook.sessions
    ] == [
        "Monday, August 3, 2026",
        "Tuesday, August 4, 2026",
        "Wednesday, August 5, 2026",
        "Thursday, August 6, 2026",
        "Friday, August 7, 2026",
    ]
    assert route_outcome_evidence_label(outlook.sessions[0]) == (
        "Completed Evidence"
    )
    assert route_outcome_evidence_label(outlook.sessions[-1]) == (
        "Pending Evidence"
    )


def test_frozen_weekly_probabilities_survive_evidence_only_refreshes(
    tmp_path: Path,
) -> None:
    first_directory = tmp_path / "first"
    second_directory = tmp_path / "second"
    first_directory.mkdir()
    second_directory.mkdir()
    first = load_forecast_dashboard(
        _write(
            first_directory,
            _weekly_rows(actionability_status="FROZEN_WEEKLY_SNAPSHOT"),
        )
    ).symbols[0].weekly_outlook
    second = load_forecast_dashboard(
        _write(
            second_directory,
            _weekly_rows(
                actionability_status="FROZEN_WEEKLY_SNAPSHOT",
                intelligence_statuses=(
                    "PENDING_EVIDENCE",
                    "COMPLETED_EVIDENCE",
                    "PENDING_EVIDENCE",
                    "PENDING_EVIDENCE",
                    "PENDING_EVIDENCE",
                    "PENDING_EVIDENCE",
                ),
            ),
        )
    ).symbols[0].weekly_outlook

    assert first is not None
    assert second is not None
    assert first.issued_at == second.issued_at
    assert [route.probability_up for route in first.routes] == [
        route.probability_up for route in second.routes
    ]
    assert route_outcome_evidence_label(first.sessions[0]) == "Pending Evidence"
    assert route_outcome_evidence_label(second.sessions[0]) == (
        "Completed Evidence"
    )


def test_incomplete_frozen_weekly_bundle_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        [row for row in _weekly_rows() if row["horizon"] != "1w-d3"],
    )

    with pytest.raises(ForecastDataError) as caught:
        load_forecast_dashboard(path)

    assert "Incomplete remaining-week snapshot" in caught.value.message
    assert "contiguous Day 1 prefix" in caught.value.message


def test_retired_next_session_weekly_target_is_rejected(tmp_path: Path) -> None:
    rows = _weekly_rows()
    rows[0]["target_definition_version"] = (
        "weekly-context-next-session-open-close-v2"
    )
    path = _write(tmp_path, rows)

    with pytest.raises(ForecastDataError) as caught:
        load_forecast_dashboard(path)

    assert "does not use the dynamic remaining-week target definition" in (
        caught.value.message
    )


def test_remaining_week_bundle_requires_issuance_before_its_deadline(
    tmp_path: Path,
) -> None:
    rows = _weekly_rows()
    for row in rows:
        row["forecast_created_at"] = "2026-08-03T20:00:00Z"
    path = _write(tmp_path, rows)

    with pytest.raises(ForecastDataError) as caught:
        load_forecast_dashboard(path)

    assert "was not issued before every published route deadline" in (
        caught.value.message
    )


def test_weekly_bundle_requires_explicit_frozen_runtime_status(
    tmp_path: Path,
) -> None:
    rows = _weekly_rows()
    rows[2]["actionability_status"] = "ACTIONABLE"
    path = _write(tmp_path, rows)

    with pytest.raises(ForecastDataError) as caught:
        load_forecast_dashboard(path)

    assert "contiguous Day 1 prefix" in caught.value.message


def test_weekly_card_source_contains_required_frozen_outlook_content() -> None:
    source = inspect.getsource(RollingForecastTab._build_weekly_outlook_card)
    session_source = inspect.getsource(
        RollingForecastTab._build_weekly_session_card
    )

    for required_text in (
        "Remaining-Week Outlook",
        "Current Remaining-Week Snapshot",
        "Snapshot Issued",
        "Aggregate (Remaining Week)",
        "Outcome/evidence",
        "Remaining Week Timeline",
        "Snapshot",
    ):
        assert required_text in source
    assert "open_close=True" in session_source
    assert "format_session_date(" in session_source
    assert "Outcome/evidence" in session_source
    assert source.count("self._build_live_performance_panel(") == 1
    assert session_source.count("self._build_live_performance_panel(") == 1


def test_weekly_session_details_disclosure_is_textual_and_stateful() -> None:
    assert weekly_session_details_header_text(
        5,
        expanded=False,
    ) == "▶ Weekly Session Details · 5 Published Sessions · Collapsed"
    assert weekly_session_details_header_text(
        1,
        expanded=True,
    ) == "▼ Weekly Session Details · 1 Published Session · Expanded"
    source = inspect.getsource(RollingForecastTab._build_weekly_outlook_card)
    assert "details_button.bind(" in source
    assert '"<Return>"' in source
    assert '"<KP_Enter>"' in source


def test_forecast_action_labels_use_title_capitalization() -> None:
    source = inspect.getsource(RollingForecastTab)

    for expected_text in (
        'text="Debug Details"',
        'text="Collapse All"',
        'text="Expand All"',
    ):
        assert expected_text in source

    for old_text in (
        'text="Debug details"',
        'text="Collapse all"',
        'text="Expand all"',
    ):
        assert old_text not in source


def test_session_date_and_local_window_are_dst_aware() -> None:
    winter_open = datetime(
        2026,
        11,
        2,
        14,
        30,
        tzinfo=timezone.utc,
    )

    assert format_session_date(
        winter_open,
        local_timezone=ZoneInfo("America/New_York"),
    ) == "Monday, November 2, 2026"
    assert format_timestamp_local(
        winter_open,
        local_timezone=ZoneInfo("America/New_York"),
    ) == "Nov 02, 2026 09:30 EST"


def test_symbols_and_horizons_are_deterministically_ordered(
    tmp_path: Path,
) -> None:
    rows = [
        _row(symbol="GOOG", horizon="1d"),
        _row(symbol="MU", horizon="1h"),
        _row(symbol="GOOG", horizon="4h"),
        _row(symbol="MU", horizon="4h"),
        _row(symbol="MU", horizon="1d"),
        _row(symbol="GOOG", horizon="1h"),
        *_weekly_rows(symbol="MU"),
        *_weekly_rows(symbol="GOOG"),
    ]
    path = _write(tmp_path, rows)

    view = load_forecast_dashboard(path)

    assert [symbol.symbol for symbol in view.symbols] == ["GOOG", "MU"]
    assert all(
        [route.horizon for route in symbol.routes]
        == ["1h", "4h", "1d", "1w"]
        for symbol in view.symbols
    )


def test_refresh_state_clears_stale_values_before_reload(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, [_row()])
    first_view = load_forecast_dashboard(path)
    state = ForecastRefreshState()
    first_generation = state.begin()
    assert state.succeed(first_generation, first_view)
    assert state.view is first_view

    second_generation = state.begin()

    assert state.loading
    assert state.view is None
    error = ForecastDataError(
        code="MISSING_FILE",
        title="Missing",
        message="Missing",
        path=path,
        technical_detail="Missing",
    )
    assert state.fail(second_generation, error)
    assert state.view is None
    assert state.error is error


def test_status_labels_are_textual_and_accessible(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, [_row(completed_count=11)])
    route = load_forecast_dashboard(
        path,
        loaded_at=datetime(2026, 7, 27, 14, 45, tzinfo=timezone.utc),
    ).symbols[0].routes[0]

    labels = route_accessible_status_labels(route)

    assert labels == (
        "Actionability: Current Forecast",
        "Live Evidence: 11 of 60 Completed Forecasts",
    )


def test_route_card_has_no_model_status_row_or_unused_separator() -> None:
    source = inspect.getsource(RollingForecastTab._build_route_card)

    assert "model_evidence" not in source
    assert "ttk.Separator" not in source
    assert "self._build_evidence_block(card, route, live_evidence)" in source
    assert "self._build_live_performance_panel(" in source
    assert "route.is_actionable or route.is_in_progress" in source


def test_live_performance_panel_is_high_contrast_large_and_bold() -> None:
    style_source = inspect.getsource(RollingForecastTab._apply_styles)
    panel_source = inspect.getsource(
        RollingForecastTab._build_live_performance_panel
    )

    assert '"ForecastPerformance.TFrame"' in style_source
    assert '"ForecastPerformanceHeading.TLabel"' in style_source
    assert '"ForecastPerformanceCumulative.TLabel"' in style_source
    assert '"ForecastPerformanceRolling.TLabel"' in style_source
    assert style_source.count('font=("Segoe UI", 11, "bold")') >= 2
    assert 'text="LIVE PERFORMANCE"' in panel_source
    assert "route.live_performance" in panel_source
    assert "performance.hit_rate" in panel_source
    assert "performance.rolling_hit_rate" in panel_source
    assert "rolling_performance_heading(" in panel_source
    assert "route_live_performance_labels" not in panel_source
    assert "panel.pack(fill=tk.X" in panel_source


def test_rolling_forecasts_refresh_live_performance_hourly() -> None:
    source = inspect.getsource(RollingForecastTab)

    assert HOURLY_AUTO_REFRESH_MS == 3_600_000
    assert "self._schedule_hourly_refresh()" in source
    assert "self.root.after(" in inspect.getsource(
        RollingForecastTab._schedule_hourly_refresh
    )


def test_view_and_debug_output_use_the_readable_intelligence_fields(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, [_row()])

    view = load_forecast_dashboard(path)
    route = view.symbols[0].routes[0]
    debug = dashboard_debug_text(view)

    assert route.id == "GOOG|1h|2026-07-27T14:00:00Z"
    assert route.model_name == "logistic-regression-1h"
    assert route.decision_timestamp == datetime(
        2026,
        7,
        27,
        14,
        0,
        tzinfo=timezone.utc,
    )
    assert "id: GOOG|1h|2026-07-27T14:00:00Z" in debug
    assert "horizon: 1h" in debug
    assert "model_name: logistic-regression-1h" in debug
    assert (
        "model_evidence_status: OFFLINE_EVALUATED_CANDIDATE"
        in debug
    )
    assert "minimum_live_decision_count: 60" in debug
    assert "target_definition_version: session-segment-v2" in debug
    assert "_id:" not in debug


def test_four_hour_debug_output_includes_route_identity_and_status(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, [_row(horizon="4h")])

    view = load_forecast_dashboard(path)
    route = view.symbols[0].routes[1]
    debug = dashboard_debug_text(view)

    assert route.id == "GOOG|4h|2026-07-27T14:00:00Z"
    assert route.model_name == "logistic-regression-4h"
    assert route.decision_timestamp == datetime(
        2026,
        7,
        27,
        14,
        0,
        tzinfo=timezone.utc,
    )
    assert "id: GOOG|4h|2026-07-27T14:00:00Z" in debug
    assert "model_name: logistic-regression-4h" in debug
    assert "decision_timestamp: 2026-07-27 14:00:00+00:00" in debug
    assert "forecast_created_at: 2026-07-27 14:30:00+00:00" in debug
    assert "target_window_start: 2026-07-27 15:00:00+00:00" in debug
    assert "target_window_end: 2026-07-27 16:00:00+00:00" in debug
    assert "actionability_status: ACTIONABLE" in debug
    assert "operational_status: OPERATIONAL" in debug


@pytest.mark.parametrize(
    ("width", "columns"),
    (
        (1906, 3),
        (1500, 3),
        (1499, 2),
        (1180, 2),
        (760, 2),
        (759, 1),
        (640, 1),
    ),
)
def test_dashboard_responsive_layout_boundaries(
    width: int,
    columns: int,
) -> None:
    assert dashboard_layout(width) == columns


def test_one_hour_sibling_routes_display_nearest_future_target(
    tmp_path: Path,
) -> None:
    started = _row(horizon="1h")
    started["id"] = "GOOG|1h|2026-07-27T14:00:00Z|2026-07-27T15:00:00Z"
    future = {
        **started,
        "id": "GOOG|1h|2026-07-27T14:00:00Z|2026-07-27T16:00:00Z",
        "target_window_start": "2026-07-27T16:00:00Z",
        "target_window_end": "2026-07-27T17:00:00Z",
        "actionable_until": "2026-07-27T16:00:00Z",
    }
    path = _write(tmp_path, [started, future])

    route = next(
        item
        for item in load_forecast_dashboard(
            path,
            loaded_at=datetime(2026, 7, 27, 15, 30, tzinfo=timezone.utc),
        ).symbols[0].routes
        if item.horizon == "1h"
    )

    assert route.target_window_start == datetime(
        2026, 7, 27, 16, 0, tzinfo=timezone.utc
    )
    assert route.actionability_status == "ACTIONABLE"


def test_duplicate_current_route_is_rejected(tmp_path: Path) -> None:
    later_row = _row(horizon="4h")
    later_row["id"] = "GOOG|4h|2026-07-27T15:00:00Z"
    later_row["decision_timestamp"] = "2026-07-27T15:00:00Z"
    path = _write(
        tmp_path,
        [_row(horizon="4h"), later_row],
    )

    with pytest.raises(ForecastDataError) as caught:
        load_forecast_dashboard(path)

    assert caught.value.code == "SCHEMA_INCOMPATIBLE"
    assert "Duplicate current-output route: GOOG 4h" in caught.value.message


def test_unknown_horizon_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, [_row(horizon="8h")])

    with pytest.raises(ForecastDataError) as caught:
        load_forecast_dashboard(path)

    assert caught.value.code == "SCHEMA_INCOMPATIBLE"
    assert "Unsupported rolling horizon" in caught.value.message


def test_zero_row_parquet_is_a_useful_empty_state(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, [])

    view = load_forecast_dashboard(path)

    assert not view.symbols
    assert view.empty_message == "No current forecast rows are available."
    assert view.freshness_label == "No Forecast Data"
    assert not view.automated_action_allowed


def _write(
    tmp_path: Path,
    rows: list[dict[str, object]],
) -> Path:
    path = tmp_path / "rolling-predictions.parquet"
    write_parquet_with_schema(
        pd.DataFrame(rows),
        path,
        INTELLIGENCE_SCHEMA,
    )
    return path


def _write_legacy(
    tmp_path: Path,
    rows: list[dict[str, object]],
) -> Path:
    legacy_rows: list[dict[str, object]] = []
    for row in rows:
        legacy_row = dict(row)
        legacy_row.pop("target_definition_version", None)
        legacy_row.pop("minimum_live_decision_count", None)
        legacy_row["schema_version"] = "one-id-v1"
        legacy_rows.append(legacy_row)
    path = tmp_path / "rolling-predictions-v1.parquet"
    write_parquet_with_schema(
        pd.DataFrame(legacy_rows),
        path,
        LEGACY_INTELLIGENCE_SCHEMA,
    )
    return path


def _row(
    *,
    symbol: str = "GOOG",
    horizon: str = "1h",
    probability_up: float | None = 0.60,
    actionability_status: str = "ACTIONABLE",
    operational_status: str = "OPERATIONAL",
    model_evidence_status: str = "OFFLINE_EVALUATED_CANDIDATE",
    live_evidence_status: str = "INSUFFICIENT_LIVE_EVIDENCE",
    completed_count: int = 3,
    minimum_count: int | None = None,
    retain_probability: bool = False,
    intelligence_status: str | None = None,
) -> dict[str, object]:
    probability = (
        probability_up
        if actionability_status == "ACTIONABLE" or retain_probability
        else None
    )
    decision_timestamp = "2026-07-27T14:00:00Z"
    return {
        "id": f"{symbol}|{horizon}|{decision_timestamp}",
        "symbol": symbol,
        "horizon": horizon,
        "decision_timestamp": decision_timestamp,
        "forecast_created_at": "2026-07-27T14:30:00Z",
        "information_available_at": "2026-07-27T14:05:00Z",
        "target_window_start": "2026-07-27T15:00:00Z",
        "target_window_end": "2026-07-27T16:00:00Z",
        "actionable_until": "2026-07-27T15:00:00Z",
        "target_definition_version": "session-segment-v2",
        "probability_up": probability,
        "probability_down": (
            1.0 - probability if probability is not None else None
        ),
        "actionability_status": actionability_status,
        "operational_status": operational_status,
        "model_evidence_status": model_evidence_status,
        "live_evidence_status": live_evidence_status,
        "intelligence_status": (
            intelligence_status
            if intelligence_status is not None
            else (
                "RISK_ANALYSIS_SUPPORT"
                if actionability_status == "ACTIONABLE"
                else "NO_CURRENT_FORECAST"
            )
        ),
        "model_name": f"logistic-regression-{horizon}",
        "completed_decision_count": completed_count,
        "minimum_live_decision_count": (
            minimum_count
            if minimum_count is not None
            else (
                minimum_live_decisions(horizon)
                if horizon in SUPPORTED_HORIZON_ORDER
                else 60
            )
        ),
        "automated_action_allowed": False,
        "limitations": (
            "Research support only; automated action and promotion are "
            "disabled."
        ),
        "schema_version": "one-id-v2",
    }


def _evaluation_row(
    *,
    horizon: str,
    decision_index: int,
    observed_target: int,
    correct: bool,
    symbol: str = "GOOG",
    publication_offset_minutes: int = 0,
) -> dict[str, object]:
    decision_timestamp = pd.Timestamp("2026-05-01T14:00:00Z") + pd.Timedelta(
        hours=decision_index
    )
    prediction_created_at = decision_timestamp + pd.Timedelta(
        minutes=5 + publication_offset_minutes
    )
    predicted_up = bool(observed_target) if correct else not bool(observed_target)
    probability = 0.75 if predicted_up else 0.25
    brier_score = (probability - observed_target) ** 2
    return {
        "id": (
            f"{symbol}|{horizon}|{decision_timestamp.isoformat()}|"
            f"{prediction_created_at.isoformat()}"
        ),
        "symbol": symbol,
        "provider": "databento",
        "horizon": horizon,
        "decision_timestamp": decision_timestamp,
        "target_window_start": decision_timestamp + pd.Timedelta(hours=1),
        "target_window_end": decision_timestamp + pd.Timedelta(hours=2),
        "prediction_created_at": prediction_created_at,
        "evaluated_at": decision_timestamp + pd.Timedelta(hours=3),
        "model_name": f"logistic-{horizon}",
        "model_version": "test-v1",
        "prediction_mode": "LIVE",
        "evaluation_status": "EVALUATED",
        "target_definition_version": "test-target-v1",
        "target_specification": "test-target-specification",
        "assumed_round_trip_cost": 0.001,
        "observed_target": observed_target,
        "observed_forward_raw_return": 0.01 if observed_target else -0.01,
        "observed_forward_cost_adjusted_return": (
            0.009 if observed_target else -0.011
        ),
        "raw_probability": probability,
        "calibrated_probability": probability,
        "raw_log_loss": 0.25 if correct else 1.25,
        "log_loss": 0.25 if correct else 1.25,
        "raw_brier_score": brier_score,
        "brier_score": brier_score,
        "prediction_correct_0_5": correct,
    }


def _weekly_rows(
    *,
    symbol: str = "GOOG",
    actionability_status: str = "FROZEN_WEEKLY_SNAPSHOT",
    intelligence_statuses: tuple[str, ...] | None = None,
) -> list[dict[str, object]]:
    horizons_and_windows = (
        ("1w", "2026-08-03T13:30:00Z", "2026-08-07T20:00:00Z"),
        ("1w-d1", "2026-08-03T13:30:00Z", "2026-08-03T20:00:00Z"),
        ("1w-d2", "2026-08-04T13:30:00Z", "2026-08-04T20:00:00Z"),
        ("1w-d3", "2026-08-05T13:30:00Z", "2026-08-05T20:00:00Z"),
        ("1w-d4", "2026-08-06T13:30:00Z", "2026-08-06T20:00:00Z"),
        ("1w-d5", "2026-08-07T13:30:00Z", "2026-08-07T20:00:00Z"),
    )
    versions = {
        "1w": "dynamic-remaining-week-aggregate-open-close-v2",
        "1w-d1": "dynamic-remaining-week-d1-open-close-v2",
        "1w-d2": "dynamic-remaining-week-d2-open-close-v2",
        "1w-d3": "dynamic-remaining-week-d3-open-close-v2",
        "1w-d4": "dynamic-remaining-week-d4-open-close-v2",
        "1w-d5": "dynamic-remaining-week-d5-open-close-v2",
    }
    probabilities = (0.54, 0.51, 0.52, 0.53, 0.54, 0.55)
    statuses = intelligence_statuses or ("PENDING_EVIDENCE",) * 6
    rows: list[dict[str, object]] = []
    for (
        horizon,
        target_start,
        target_end,
    ), probability, intelligence_status in zip(
        horizons_and_windows,
        probabilities,
        statuses,
        strict=True,
    ):
        row = _row(
            symbol=symbol,
            horizon=horizon,
            probability_up=probability,
            actionability_status=actionability_status,
            operational_status="OPERATIONALLY_CURRENT",
            completed_count=0,
            minimum_count=30,
            retain_probability=True,
            intelligence_status=intelligence_status,
        )
        decision_timestamp = "2026-07-31T20:05:00Z"
        row.update(
            {
                "id": f"{symbol}|{horizon}|{decision_timestamp}",
                "decision_timestamp": decision_timestamp,
                "forecast_created_at": "2026-07-31T20:06:00Z",
                "information_available_at": "2026-07-31T20:05:00Z",
                "target_window_start": target_start,
                "target_window_end": target_end,
                "actionable_until": (
                    "2026-08-03T20:00:00Z"
                    if horizon == "1w"
                    else target_end
                ),
                "target_definition_version": versions[horizon],
            }
        )
        rows.append(row)
    return rows
