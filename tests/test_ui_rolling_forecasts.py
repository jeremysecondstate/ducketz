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
    dashboard_debug_text,
    dashboard_layout,
    format_session_date,
    format_timestamp_local,
    format_timestamp_utc,
    load_forecast_dashboard,
    route_accessible_status_labels,
    route_outcome_evidence_label,
    route_publication_summary,
)
from app.ui.rolling_forecasts import RollingForecastTab
from ml.live_evidence import minimum_live_decisions
from ml.parquet_contracts import (
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
    assert HORIZON_LABELS["4h"] == "4 hour"
    assert f"{HORIZON_LABELS['4h']} forecast" == "4 hour forecast"
    assert HORIZON_LABELS["1w"] == "5-session aggregate"
    assert FORECAST_SUBTITLE == (
        "Read-only 1h, 4h, 1d, and frozen 5-session probability outlooks. "
        "Probabilities are not recommendations."
    )


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

    view = load_forecast_dashboard(path)

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
    assert view.symbols[0].routes[-1].horizon_label == "5-session aggregate"
    assert view.symbols[0].weekly_outlook is not None
    assert [
        route.horizon for route in view.symbols[0].weekly_outlook.sessions
    ] == ["1w-d1", "1w-d2", "1w-d3", "1w-d4", "1w-d5"]
    assert view.freshness_label == "Data pipeline is current"


def test_legacy_short_horizon_output_has_missing_cards(
    tmp_path: Path,
) -> None:
    path = _write_legacy(
        tmp_path,
        [_row(horizon=horizon) for horizon in ("1h", "1d")],
    )

    view = load_forecast_dashboard(path)
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

    view = load_forecast_dashboard(path)
    routes = {
        route.horizon: route for route in view.symbols[0].routes
    }

    assert routes["1h"].actionability_label == "Current forecast"
    assert routes["1d"].actionability_label == "Model unavailable"
    assert routes["1d"].probability_up is None
    assert routes["1w"].is_missing
    assert view.operational_label == "Operational with limitations"


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
    assert four_hour.horizon_label == "4 hour"
    assert weekly.horizon == "1w"
    assert weekly.is_missing
    assert weekly.actionability_label == "No current forecast"
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

    view = load_forecast_dashboard(path)
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

    assert view.freshness_label == "Data is stale"
    assert view.freshness_tone == "danger"
    assert view.operational_label == "Operational data is stale"


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

    assert view.freshness_label == "Current outlooks with route gaps"
    assert view.freshness_tone == "warning"
    assert view.operational_label == "Operational with route timing gaps"
    assert view.operational_tone == "warning"
    assert route_publication_summary(view) == (
        "0 live routes; 1 current frozen weekly outlook; 9 published rows"
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
    assert "newer app version" in caught.value.title


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
                actionability_status="TARGET_WINDOW_STARTED",
                operational_status="OPERATIONALLY_CURRENT",
            )
        ],
    )

    view = load_forecast_dashboard(path)

    assert view.operational_label == "Operationally current"
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

    assert route.live_evidence_label == "7 of 60 completed forecasts"
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
            "Awaiting first completed forecast (0 of 60)",
        ),
        (
            "1h",
            1,
            "INSUFFICIENT_LIVE_EVIDENCE",
            "1 of 60 completed forecasts",
        ),
        (
            "1h",
            59,
            "INSUFFICIENT_LIVE_EVIDENCE",
            "59 of 60 completed forecasts",
        ),
        (
            "1h",
            60,
            "LIVE_EVIDENCE_AVAILABLE",
            "60 of 60 completed forecasts",
        ),
        (
            "4h",
            60,
            "LIVE_EVIDENCE_AVAILABLE",
            "60 of 60 completed forecasts",
        ),
        (
            "1d",
            29,
            "INSUFFICIENT_LIVE_EVIDENCE",
            "29 of 30 completed forecasts",
        ),
        (
            "1d",
            30,
            "LIVE_EVIDENCE_AVAILABLE",
            "30 of 30 completed forecasts",
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
        f"Live evidence: {expected}"
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
        "Completed evidence"
    )
    assert route_outcome_evidence_label(outlook.sessions[-1]) == (
        "Pending evidence"
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
    assert route_outcome_evidence_label(first.sessions[0]) == "Pending evidence"
    assert route_outcome_evidence_label(second.sessions[0]) == (
        "Completed evidence"
    )


def test_incomplete_frozen_weekly_bundle_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, _weekly_rows()[:-1])

    with pytest.raises(ForecastDataError) as caught:
        load_forecast_dashboard(path)

    assert "Incomplete frozen weekly snapshot" in caught.value.message
    assert "1w-d5" in caught.value.message


def test_retired_next_session_weekly_target_is_rejected(tmp_path: Path) -> None:
    rows = _weekly_rows()
    rows[0]["target_definition_version"] = (
        "weekly-context-next-session-open-close-v2"
    )
    path = _write(tmp_path, rows)

    with pytest.raises(ForecastDataError) as caught:
        load_forecast_dashboard(path)

    assert "does not use the frozen five-session target definition" in (
        caught.value.message
    )


def test_frozen_weekly_bundle_requires_one_pre_entry_issuance(
    tmp_path: Path,
) -> None:
    rows = _weekly_rows()
    for row in rows:
        row["forecast_created_at"] = "2026-08-03T13:30:00Z"
    path = _write(tmp_path, rows)

    with pytest.raises(ForecastDataError) as caught:
        load_forecast_dashboard(path)

    assert "was not issued before Day 1 opened" in caught.value.message


def test_weekly_bundle_requires_explicit_frozen_runtime_status(
    tmp_path: Path,
) -> None:
    rows = _weekly_rows()
    rows[2]["actionability_status"] = "ACTIONABLE"
    path = _write(tmp_path, rows)

    with pytest.raises(ForecastDataError) as caught:
        load_forecast_dashboard(path)

    assert "is not marked as a frozen weekly snapshot" in caught.value.message


def test_weekly_card_source_contains_required_frozen_outlook_content() -> None:
    source = inspect.getsource(RollingForecastTab._build_weekly_outlook_card)

    for required_text in (
        "5-session outlook",
        "Frozen weekly snapshot",
        "Snapshot issued",
        "Aggregate (Full Week)",
        "UTC open",
        "Local open",
        "Outcome/evidence",
    ):
        assert required_text in source


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
    route = load_forecast_dashboard(path).symbols[0].routes[0]

    labels = route_accessible_status_labels(route)

    assert labels == (
        "Actionability: Current forecast",
        "Live evidence: 11 of 60 completed forecasts",
    )


def test_route_card_has_no_model_status_row_or_unused_separator() -> None:
    source = inspect.getsource(RollingForecastTab._build_route_card)

    assert "model_evidence" not in source
    assert "ttk.Separator" not in source
    assert "text=live_evidence" in source


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
    assert view.freshness_label == "No forecast data"
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
        "1w": "frozen-five-session-aggregate-open-close-v1",
        "1w-d1": "frozen-five-session-d1-open-close-v1",
        "1w-d2": "frozen-five-session-d2-open-close-v1",
        "1w-d3": "frozen-five-session-d3-open-close-v1",
        "1w-d4": "frozen-five-session-d4-open-close-v1",
        "1w-d5": "frozen-five-session-d5-open-close-v1",
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
                "actionable_until": "2026-08-03T13:30:00Z",
                "target_definition_version": versions[horizon],
            }
        )
        rows.append(row)
    return rows
