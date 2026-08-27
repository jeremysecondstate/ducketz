from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Final

import pandas as pd
import pyarrow as pa

from datafetching.parquet_store import resolve_datastore_dir
from ml.current_publication import (
    CurrentPublicationError,
    resolve_current_output,
)
from ml.horizons import (
    INTERNAL_HORIZON_ORDER,
    INTERNAL_HORIZON_SPECIFICATIONS,
    WEEKLY_HORIZON_ORDER,
)
from ml.live_evidence import minimum_live_decisions
from ml.parquet_contracts import (
    EVALUATION_SCHEMA,
    INTELLIGENCE_SCHEMA,
    verify_parquet_schema,
)

STANDARD_HORIZON_ORDER: Final = ("1h", "4h", "1d")
# ``HORIZON_ORDER`` remains the visual card order: three ordinary cards and
# one remaining-week aggregate card. Its component routes are rendered inside
# the weekly card rather than as peer cards.
HORIZON_ORDER: Final = (*STANDARD_HORIZON_ORDER, "1w")
SUPPORTED_HORIZON_ORDER: Final = INTERNAL_HORIZON_ORDER
HORIZON_LABELS: Final = {
    "1h": "1 Hour",
    "4h": "4 Hour",
    "1d": "1 Day",
    "1w": "Remaining-Week Aggregate",
    "1w-d1": "Day 1",
    "1w-d2": "Day 2",
    "1w-d3": "Day 3",
    "1w-d4": "Day 4",
    "1w-d5": "Day 5",
}
FORECAST_SUBTITLE: Final = (
    "Read-only 1h, 4h, 1d, and dynamic remaining-week probability outlooks. "
    "Probabilities are not recommendations."
)
ACTIONABLE_STATUS: Final = "ACTIONABLE"
_WEEKLY_TARGET_DEFINITION_VERSIONS: Final = {
    horizon: INTERNAL_HORIZON_SPECIFICATIONS[horizon].target_definition_version
    for horizon in WEEKLY_HORIZON_ORDER
}
_REFRESH_SENTINELS: Final = {"REFRESH_FAILED", "REFRESH_IN_PROGRESS"}
_CURRENT_OPERATIONAL_STATUSES: Final = {
    "OPERATIONAL",
    "OPERATIONALLY_CURRENT",
}
_CURRENT_INTELLIGENCE_SCHEMA_VERSION: Final = "one-id-v2"
_LEGACY_INTELLIGENCE_SCHEMA_VERSION: Final = "one-id-v1"
_SUPPORTED_INTELLIGENCE_SCHEMA_VERSIONS: Final = {
    _LEGACY_INTELLIGENCE_SCHEMA_VERSION,
    _CURRENT_INTELLIGENCE_SCHEMA_VERSION,
}
_V2_INTELLIGENCE_FIELDS: Final = (
    "target_definition_version",
    "minimum_live_decision_count",
)


def _schema_without_fields(
    schema: pa.Schema,
    fields: tuple[str, ...],
) -> pa.Schema:
    result = schema
    for field in fields:
        index = result.get_field_index(field)
        if index >= 0:
            result = result.remove(index)
    return result


LEGACY_INTELLIGENCE_SCHEMA: Final = _schema_without_fields(
    INTELLIGENCE_SCHEMA,
    _V2_INTELLIGENCE_FIELDS,
)


@dataclass(frozen=True)
class LivePerformanceView:
    evidence_count: int
    scored_count: int
    correct_count: int
    hit_rate: float | None
    down_only_rate: float | None
    lift_over_down_only: float | None
    rolling_window_size: int
    rolling_count: int
    rolling_correct_count: int
    rolling_hit_rate: float | None
    rolling_down_only_rate: float | None
    rolling_lift_over_down_only: float | None


@dataclass(frozen=True)
class ForecastRouteView:
    id: str | None
    symbol: str
    horizon: str
    horizon_label: str
    model_name: str | None
    decision_timestamp: datetime | None
    forecast_created_at: datetime | None
    information_available_at: datetime | None
    target_window_start: datetime | None
    target_window_end: datetime | None
    actionable_until: datetime | None
    target_definition_version: str | None
    probability_up: float | None
    probability_down: float | None
    actionability_status: str
    actionability_label: str
    actionability_tone: str
    model_evidence_status: str
    live_evidence_status: str
    live_evidence_label: str
    completed_decision_count: int | None
    minimum_live_decision_count: int
    live_performance: LivePerformanceView | None
    operational_status: str
    intelligence_status: str
    automated_action_allowed: bool | None
    limitation: str | None
    is_missing: bool
    warnings: tuple[str, ...]
    debug_fields: tuple[tuple[str, str], ...]

    @property
    def is_actionable(self) -> bool:
        return self.actionability_status == ACTIONABLE_STATUS

    @property
    def is_in_progress(self) -> bool:
        return (
            self.actionability_status == "TARGET_WINDOW_STARTED"
            and self.intelligence_status == "FORECAST_IN_PROGRESS"
            and self.probability_up is not None
            and self.probability_down is not None
        )

    @property
    def is_weekly(self) -> bool:
        return self.horizon in WEEKLY_HORIZON_ORDER


@dataclass(frozen=True)
class WeeklyOutlookView:
    aggregate: ForecastRouteView
    sessions: tuple[ForecastRouteView, ...]
    issued_at: datetime

    @property
    def routes(self) -> tuple[ForecastRouteView, ...]:
        return (self.aggregate, *self.sessions)


@dataclass(frozen=True)
class SymbolForecastView:
    symbol: str
    routes: tuple[ForecastRouteView, ...]
    weekly_outlook: WeeklyOutlookView | None

    @property
    def all_routes(self) -> tuple[ForecastRouteView, ...]:
        if self.weekly_outlook is None:
            return self.routes
        return (*self.routes, *self.weekly_outlook.sessions)


@dataclass(frozen=True)
class ForecastDashboardView:
    source_path: Path
    loaded_at: datetime
    schema_version: str
    source_row_count: int
    forecast_created_at: datetime | None
    last_successful_refresh: datetime | None
    freshness_label: str
    freshness_tone: str
    operational_label: str
    operational_tone: str
    operational_statuses: tuple[str, ...]
    automated_action_allowed: bool
    automation_label: str
    automation_tone: str
    actionable_route_count: int
    published_route_count: int
    frozen_weekly_snapshot_count: int
    symbols: tuple[SymbolForecastView, ...]
    limitations: tuple[str, ...]
    warnings: tuple[str, ...]
    empty_message: str | None


class ForecastDataError(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        title: str,
        message: str,
        path: Path,
        technical_detail: str,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.title = title
        self.message = message
        self.path = Path(path)
        self.technical_detail = technical_detail


@dataclass
class ForecastRefreshState:
    view: ForecastDashboardView | None = None
    error: ForecastDataError | None = None
    loading: bool = False
    generation: int = 0

    def begin(self) -> int:
        self.generation += 1
        self.view = None
        self.error = None
        self.loading = True
        return self.generation

    def succeed(
        self,
        generation: int,
        view: ForecastDashboardView,
    ) -> bool:
        if generation != self.generation:
            return False
        self.view = view
        self.error = None
        self.loading = False
        return True

    def fail(
        self,
        generation: int,
        error: ForecastDataError,
    ) -> bool:
        if generation != self.generation:
            return False
        self.view = None
        self.error = error
        self.loading = False
        return True


def default_rolling_predictions_path() -> Path:
    configured = os.getenv("DUCKETS_ROLLING_PREDICTIONS_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    return (
        resolve_datastore_dir()
        / "ml-intelligence"
        / "latest"
        / "rolling-predictions.parquet"
    )


def load_forecast_dashboard(
    path: Path | None = None,
    *,
    loaded_at: datetime | None = None,
) -> ForecastDashboardView:
    requested_source = Path(path or default_rolling_predictions_path())
    try:
        source = _resolve_authoritative_current_source(requested_source)
    except CurrentPublicationError as exc:
        raise _structured_read_error(requested_source, exc) from exc
    if not source.is_file():
        raise ForecastDataError(
            code="MISSING_FILE",
            title="Forecast Data Is Not Available",
            message=(
                "The consolidated rolling forecast file has not been "
                "published at the configured location."
            ),
            path=source,
            technical_detail=f"File not found: {source}",
        )

    try:
        frame = pd.read_parquet(source)
        schema_version = _intelligence_schema_version(frame)
        expected_schema = (
            LEGACY_INTELLIGENCE_SCHEMA
            if schema_version == _LEGACY_INTELLIGENCE_SCHEMA_VERSION
            else INTELLIGENCE_SCHEMA
        )
        verify_parquet_schema(source, expected_schema)
        evaluations = _load_evaluations_for_source(source)
    except Exception as exc:
        raise _structured_read_error(source, exc) from exc

    return adapt_forecast_frame(
        frame,
        evaluations=evaluations,
        source_path=source,
        loaded_at=loaded_at,
    )


def adapt_forecast_frame(
    frame: pd.DataFrame,
    *,
    evaluations: pd.DataFrame | None = None,
    source_path: Path = Path("rolling-predictions.parquet"),
    loaded_at: datetime | None = None,
) -> ForecastDashboardView:
    loaded = _utc_datetime(loaded_at or datetime.now(timezone.utc))
    rows = _meaningful_rows(frame)
    route_rows: dict[tuple[str, str], dict[str, object]] = {}
    live_performance = _live_performance_by_route(evaluations)

    for row in rows.to_dict("records"):
        raw_symbol = _text(row.get("symbol"))
        raw_horizon = _text(row.get("horizon"))
        if raw_symbol is None or raw_horizon is None:
            raise _contract_error(
                source_path,
                "Every published row must identify both a symbol and a horizon.",
            )
        symbol = raw_symbol.upper()
        horizon = raw_horizon.lower()
        if horizon not in SUPPORTED_HORIZON_ORDER:
            raise _contract_error(
                source_path,
                f"Unsupported rolling horizon in current output: {raw_horizon}",
            )
        key = (symbol, horizon)
        if key in route_rows:
            raise _contract_error(
                source_path,
                f"Duplicate current-output route: {symbol} {horizon}",
            )
        route_rows[key] = row

    symbols: list[SymbolForecastView] = []
    all_route_warnings: list[str] = []
    for symbol in sorted(
        {key[0] for key in route_rows},
        key=lambda value: (value.casefold(), value),
    ):
        routes: list[ForecastRouteView] = []
        for horizon in STANDARD_HORIZON_ORDER:
            row = route_rows.get((symbol, horizon))
            route = (
                _route_view(
                    symbol,
                    horizon,
                    row,
                    as_of=loaded,
                    live_performance=live_performance.get((symbol, horizon)),
                )
                if row is not None
                else _missing_route(symbol, horizon)
            )
            routes.append(route)
            all_route_warnings.extend(route.warnings)
        weekly_outlook = _weekly_outlook_view(
            symbol,
            route_rows,
            live_performance=live_performance,
            source_path=source_path,
            as_of=loaded,
        )
        if weekly_outlook is None:
            weekly_aggregate = _missing_route(symbol, "1w")
        else:
            weekly_aggregate = weekly_outlook.aggregate
            all_route_warnings.extend(
                warning
                for route in weekly_outlook.routes
                for warning in route.warnings
            )
        routes.append(weekly_aggregate)
        symbols.append(
            SymbolForecastView(
                symbol=symbol,
                routes=tuple(routes),
                weekly_outlook=weekly_outlook,
            )
        )

    operational_statuses = tuple(
        sorted(
            {
                status
                for status in (
                    _text(row.get("operational_status"))
                    for row in route_rows.values()
                )
                if status is not None
            }
        )
    )
    forecast_times = tuple(
        timestamp
        for timestamp in (
            _timestamp(row.get("forecast_created_at"))
            for row in route_rows.values()
        )
        if timestamp is not None
    )
    forecast_created_at = max(forecast_times, default=None)
    refresh_is_incomplete = any(
        status in _REFRESH_SENTINELS for status in operational_statuses
    )
    last_successful_refresh = (
        None if refresh_is_incomplete else forecast_created_at
    )
    freshness_label, freshness_tone = _freshness(operational_statuses)
    operational_label, operational_tone = _operational_summary(
        operational_statuses
    )
    automated_action_allowed = any(
        _boolean(row.get("automated_action_allowed")) is True
        for row in route_rows.values()
    )
    automation_label = (
        "Automation flag reported on; this dashboard remains read-only"
        if automated_action_allowed
        else "Automated action is off"
    )
    automation_tone = "danger" if automated_action_allowed else "neutral"
    actionable_count = sum(
        _text(row.get("actionability_status")) == ACTIONABLE_STATUS
        for row in route_rows.values()
    )
    frozen_weekly_count = sum(
        symbol.weekly_outlook is not None for symbol in symbols
    )
    limitations = tuple(
        sorted(
            {
                limitation
                for limitation in (
                    _text(row.get("limitations"))
                    for row in route_rows.values()
                )
                if limitation is not None
            },
            key=str.casefold,
        )
    )
    if not route_rows:
        limitations = ("No current forecast rows were published.",)

    empty_message = _empty_message(
        published_routes=len(route_rows),
        actionable_routes=actionable_count,
        frozen_weekly_snapshots=frozen_weekly_count,
        operational_statuses=operational_statuses,
    )
    return ForecastDashboardView(
        source_path=Path(source_path),
        loaded_at=loaded,
        schema_version=_intelligence_schema_version(frame),
        source_row_count=len(frame),
        forecast_created_at=forecast_created_at,
        last_successful_refresh=last_successful_refresh,
        freshness_label=freshness_label,
        freshness_tone=freshness_tone,
        operational_label=operational_label,
        operational_tone=operational_tone,
        operational_statuses=operational_statuses,
        automated_action_allowed=automated_action_allowed,
        automation_label=automation_label,
        automation_tone=automation_tone,
        actionable_route_count=actionable_count,
        published_route_count=len(route_rows),
        frozen_weekly_snapshot_count=frozen_weekly_count,
        symbols=tuple(symbols),
        limitations=limitations,
        warnings=tuple(sorted(set(all_route_warnings), key=str.casefold)),
        empty_message=empty_message,
    )


def format_probability(value: float | None) -> str:
    return "Unavailable" if value is None else f"{value:.1%}"


def format_timestamp_utc(value: datetime | None) -> str:
    if value is None:
        return "Unavailable"
    converted = _utc_datetime(value)
    return converted.strftime("%b %d, %Y %H:%M UTC")


def format_timestamp_local(
    value: datetime | None,
    *,
    local_timezone: tzinfo | None = None,
) -> str:
    if value is None:
        return "Unavailable"
    zone = local_timezone or datetime.now().astimezone().tzinfo or timezone.utc
    converted = _utc_datetime(value).astimezone(zone)
    zone_name = converted.tzname() or "local"
    return converted.strftime(f"%b %d, %Y %H:%M {zone_name}")


def format_session_date(
    value: datetime | None,
    *,
    local_timezone: tzinfo | None = None,
) -> str:
    if value is None:
        return "Date Unavailable"
    zone = local_timezone or datetime.now().astimezone().tzinfo or timezone.utc
    converted = _utc_datetime(value).astimezone(zone)
    return (
        f"{converted:%A, %B} {converted.day}, {converted.year}"
    )


def route_publication_summary(view: ForecastDashboardView) -> str:
    """Describe live routes and the frozen outlook without conflating them."""

    live = view.actionable_route_count
    frozen = view.frozen_weekly_snapshot_count
    return (
        f"{live} live route" + ("s" if live != 1 else "")
        + f"; {frozen} current remaining-week outlook"
        + ("s" if frozen != 1 else "")
        + f"; {view.published_route_count} published rows"
    )


def route_outcome_evidence_label(route: ForecastRouteView) -> str:
    labels = {
        "PENDING_EVIDENCE": "Pending Evidence",
        "COMPLETED_EVIDENCE": "Completed Evidence",
        "FORECAST_IN_PROGRESS": "Forecast in Progress",
    }
    return labels.get(
        route.intelligence_status,
        _humanize(route.intelligence_status),
    )


def route_accessible_status_labels(
    route: ForecastRouteView,
) -> tuple[str, str]:
    return (
        f"Actionability: {route.actionability_label}",
        f"Live Evidence: {route.live_evidence_label}",
    )


def route_live_performance_labels(
    route: ForecastRouteView,
) -> tuple[str, str]:
    performance = route.live_performance
    window_size = route.minimum_live_decision_count
    if performance is None or performance.scored_count == 0:
        state = (
            "Awaiting First Scored Forecast"
            if route.completed_decision_count == 0
            else "Unavailable"
        )
        return (
            f"Cumulative Live Performance: {state}",
            f"Rolling {window_size} Performance: {state}",
        )

    cumulative = _performance_label(
        "Cumulative Live",
        correct_count=performance.correct_count,
        count=performance.scored_count,
        hit_rate=performance.hit_rate,
        down_only_rate=performance.down_only_rate,
        lift=performance.lift_over_down_only,
    )
    rolling_prefix = (
        f"Rolling {performance.rolling_window_size}"
        if performance.rolling_count >= performance.rolling_window_size
        else (
            f"Rolling {performance.rolling_count}/"
            f"{performance.rolling_window_size}"
        )
    )
    rolling = _performance_label(
        rolling_prefix,
        correct_count=performance.rolling_correct_count,
        count=performance.rolling_count,
        hit_rate=performance.rolling_hit_rate,
        down_only_rate=performance.rolling_down_only_rate,
        lift=performance.rolling_lift_over_down_only,
    )
    return cumulative, rolling


def _resolve_authoritative_current_source(source: Path) -> Path:
    """Route the canonical compatibility path through the atomic pointer."""

    candidate = Path(source)
    if (
        candidate.name == "rolling-predictions.parquet"
        and candidate.parent.name == "latest"
        and candidate.parent.parent.name == "ml-intelligence"
    ):
        datastore_root = candidate.parents[2]
        pointer = datastore_root / "ml" / "latest" / "run.json"
        if pointer.is_file():
            return resolve_current_output(
                datastore_root,
                "intelligence.parquet",
            )
    return candidate


def _load_evaluations_for_source(source: Path) -> pd.DataFrame | None:
    evaluations_path = Path(source).with_name("evaluations.parquet")
    if not evaluations_path.is_file():
        return None
    evaluations = pd.read_parquet(evaluations_path)
    verify_parquet_schema(evaluations_path, EVALUATION_SCHEMA)
    return evaluations


def _live_performance_by_route(
    evaluations: pd.DataFrame | None,
) -> dict[tuple[str, str], LivePerformanceView]:
    if evaluations is None or evaluations.empty:
        return {}
    required = {
        "symbol",
        "horizon",
        "decision_timestamp",
        "prediction_created_at",
        "prediction_mode",
        "evaluation_status",
        "observed_target",
        "prediction_correct_0_5",
    }
    missing = sorted(required - set(evaluations.columns))
    if missing:
        raise ValueError(
            "Evaluation data is missing live-performance columns: "
            + ", ".join(missing)
        )

    live = evaluations.loc[
        evaluations["prediction_mode"].astype("string").eq("LIVE")
        & evaluations["evaluation_status"].astype("string").eq("EVALUATED")
    ].copy()
    if live.empty:
        return {}
    live["symbol"] = live["symbol"].astype("string").str.strip().str.upper()
    live["horizon"] = live["horizon"].astype("string").str.strip().str.lower()
    live["prediction_created_at"] = pd.to_datetime(
        live["prediction_created_at"],
        utc=True,
        errors="coerce",
    )
    live["decision_timestamp"] = pd.to_datetime(
        live["decision_timestamp"],
        utc=True,
        errors="coerce",
    )
    live = live.loc[
        live["symbol"].ne("")
        & live["horizon"].isin(SUPPORTED_HORIZON_ORDER)
        & live["prediction_created_at"].notna()
        & live["decision_timestamp"].notna()
    ]
    canonical = (
        live.sort_values("prediction_created_at", kind="mergesort")
        .drop_duplicates(
            ["symbol", "horizon", "decision_timestamp"],
            keep="first",
        )
        .reset_index(drop=True)
    )

    output: dict[tuple[str, str], LivePerformanceView] = {}
    for (symbol, horizon), route in canonical.groupby(
        ["symbol", "horizon"],
        sort=False,
    ):
        scored = route.copy()
        scored["_observed_target"] = pd.to_numeric(
            scored["observed_target"],
            errors="coerce",
        )
        scored["_prediction_correct"] = scored[
            "prediction_correct_0_5"
        ].astype("boolean")
        scored = scored.loc[
            scored["_observed_target"].isin((0, 1))
            & scored["_prediction_correct"].notna()
        ].sort_values("decision_timestamp", kind="mergesort")
        window_size = minimum_live_decisions(str(horizon))
        rolling = scored.tail(window_size)
        (
            scored_count,
            correct_count,
            hit_rate,
            down_only_rate,
            lift,
        ) = _performance_values(scored)
        (
            rolling_count,
            rolling_correct_count,
            rolling_hit_rate,
            rolling_down_only_rate,
            rolling_lift,
        ) = _performance_values(rolling)
        output[(str(symbol), str(horizon))] = LivePerformanceView(
            evidence_count=len(route),
            scored_count=scored_count,
            correct_count=correct_count,
            hit_rate=hit_rate,
            down_only_rate=down_only_rate,
            lift_over_down_only=lift,
            rolling_window_size=window_size,
            rolling_count=rolling_count,
            rolling_correct_count=rolling_correct_count,
            rolling_hit_rate=rolling_hit_rate,
            rolling_down_only_rate=rolling_down_only_rate,
            rolling_lift_over_down_only=rolling_lift,
        )
    return output


def _performance_values(
    evaluated: pd.DataFrame,
) -> tuple[int, int, float | None, float | None, float | None]:
    count = len(evaluated)
    if count == 0:
        return 0, 0, None, None, None
    correct_count = int(evaluated["_prediction_correct"].sum())
    down_count = int(evaluated["_observed_target"].eq(0).sum())
    hit_rate = correct_count / count
    down_only_rate = down_count / count
    return (
        count,
        correct_count,
        hit_rate,
        down_only_rate,
        hit_rate - down_only_rate,
    )


def _performance_label(
    prefix: str,
    *,
    correct_count: int,
    count: int,
    hit_rate: float | None,
    down_only_rate: float | None,
    lift: float | None,
) -> str:
    if (
        count <= 0
        or hit_rate is None
        or down_only_rate is None
        or lift is None
    ):
        return f"{prefix}: Unavailable"
    return (
        f"{prefix}: Hit {_format_ratio_percentage(hit_rate)} "
        f"({correct_count}/{count}) · Down-Only "
        f"{_format_ratio_percentage(down_only_rate)} · "
        f"Lift {lift * 100:+.1f} pp"
    )


def _format_ratio_percentage(value: float) -> str:
    return f"{value * 100:.1f}%"


def _live_performance_debug_fields(
    performance: LivePerformanceView | None,
) -> tuple[tuple[str, str], ...]:
    if performance is None:
        return (("live_performance", "Unavailable"),)
    return (
        ("live_performance_evidence_count", str(performance.evidence_count)),
        ("live_performance_scored_count", str(performance.scored_count)),
        ("live_performance_correct_count", str(performance.correct_count)),
        ("live_hit_rate", _debug_value(performance.hit_rate)),
        ("live_down_only_rate", _debug_value(performance.down_only_rate)),
        (
            "live_lift_over_down_only",
            _debug_value(performance.lift_over_down_only),
        ),
        ("rolling_window_size", str(performance.rolling_window_size)),
        ("rolling_count", str(performance.rolling_count)),
        ("rolling_correct_count", str(performance.rolling_correct_count)),
        ("rolling_hit_rate", _debug_value(performance.rolling_hit_rate)),
        (
            "rolling_down_only_rate",
            _debug_value(performance.rolling_down_only_rate),
        ),
        (
            "rolling_lift_over_down_only",
            _debug_value(performance.rolling_lift_over_down_only),
        ),
    )


def dashboard_layout(width: int) -> int:
    """Return the ordinary route-card column count for *width*."""
    if width >= 1500:
        requested_columns = len(STANDARD_HORIZON_ORDER)
    elif width >= 760:
        requested_columns = 2
    else:
        requested_columns = 1
    return min(len(STANDARD_HORIZON_ORDER), requested_columns)


def dashboard_debug_text(view: ForecastDashboardView) -> str:
    lines = [
        "ROLLING FORECAST DEBUG DETAILS",
        "",
        f"Source: {view.source_path}",
        f"Supported Schema: {view.schema_version}",
        f"Source Rows: {view.source_row_count}",
        f"Loaded At: {view.loaded_at.isoformat()}",
        "Operational Statuses: "
        + (", ".join(view.operational_statuses) or "(none)"),
        f"Remaining-Week Snapshots: {view.frozen_weekly_snapshot_count}",
        f"Automated Action Allowed: {view.automated_action_allowed}",
    ]
    for symbol in view.symbols:
        lines.extend(("", symbol.symbol))
        for route in symbol.all_routes:
            lines.append(f"  {route.horizon}")
            lines.extend(
                f"    {name}: {value}" for name, value in route.debug_fields
            )
    return "\n".join(lines)


def _weekly_outlook_view(
    symbol: str,
    route_rows: dict[tuple[str, str], dict[str, object]],
    *,
    live_performance: dict[tuple[str, str], LivePerformanceView],
    source_path: Path,
    as_of: datetime,
) -> WeeklyOutlookView | None:
    present = {
        horizon: route_rows[(symbol, horizon)]
        for horizon in WEEKLY_HORIZON_ORDER
        if (
            (symbol, horizon) in route_rows
            and _text(
                route_rows[(symbol, horizon)].get("actionability_status")
            )
            == "FROZEN_WEEKLY_SNAPSHOT"
        )
    }
    if not present:
        return None
    if "1w" not in present:
        raise _contract_error(
            source_path,
            f"Incomplete remaining-week snapshot for {symbol}; the aggregate "
            "route is missing.",
        )
    component_order = WEEKLY_HORIZON_ORDER[1:]
    component_horizons = tuple(
        horizon for horizon in component_order if horizon in present
    )
    if not component_horizons:
        raise _contract_error(
            source_path,
            f"Incomplete remaining-week snapshot for {symbol}; Day 1 is missing.",
        )
    final_position = component_order.index(component_horizons[-1])
    expected_components = component_order[: final_position + 1]
    if component_horizons != expected_components:
        raise _contract_error(
            source_path,
            f"Incomplete remaining-week snapshot for {symbol}; component "
            "routes must be a contiguous Day 1 prefix.",
        )
    horizons = ("1w", *component_horizons)

    routes = tuple(
        _route_view(
            symbol,
            horizon,
            present[horizon],
            as_of=as_of,
            live_performance=live_performance.get((symbol, horizon)),
        )
        for horizon in horizons
    )
    for route in routes:
        expected_version = _WEEKLY_TARGET_DEFINITION_VERSIONS[route.horizon]
        if route.target_definition_version != expected_version:
            raise _contract_error(
                source_path,
                f"{symbol} {route.horizon} does not use the dynamic "
                "remaining-week target definition; expected "
                f"{expected_version!r}.",
            )
        if route.actionability_status != "FROZEN_WEEKLY_SNAPSHOT":
            raise _contract_error(
                source_path,
                f"{symbol} {route.horizon} is not marked as a frozen "
                "weekly snapshot.",
            )
        if (
            route.decision_timestamp is None
            or route.forecast_created_at is None
            or route.information_available_at is None
            or route.target_window_start is None
            or route.target_window_end is None
            or route.actionable_until is None
            or route.probability_up is None
            or route.probability_down is None
        ):
            raise _contract_error(
                source_path,
                f"{symbol} {route.horizon} is incomplete; a remaining-week "
                "route requires its issuance, target, and probability values.",
            )
        if route.target_window_start >= route.target_window_end:
            raise _contract_error(
                source_path,
                f"{symbol} {route.horizon} has an invalid target window.",
            )
        if (
            not 0.0 <= route.probability_up <= 1.0
            or not 0.0 <= route.probability_down <= 1.0
            or not math.isclose(
                route.probability_up + route.probability_down,
                1.0,
                rel_tol=0.0,
                abs_tol=1e-9,
            )
        ):
            raise _contract_error(
                source_path,
                f"{symbol} {route.horizon} probabilities are not complementary.",
            )

    aggregate = routes[0]
    sessions = routes[1:]
    issued_at = aggregate.forecast_created_at
    information_available_at = aggregate.information_available_at
    decision_timestamp = aggregate.decision_timestamp
    d1_start = sessions[0].target_window_start
    final_end = sessions[-1].target_window_end
    if any(route.forecast_created_at != issued_at for route in routes):
        raise _contract_error(
            source_path,
            f"{symbol} remaining-week routes do not share one issuance timestamp.",
        )
    if any(route.decision_timestamp != decision_timestamp for route in routes):
        raise _contract_error(
            source_path,
            f"{symbol} remaining-week routes do not share one decision timestamp.",
        )
    if any(
        route.information_available_at != information_available_at
        for route in routes
    ):
        raise _contract_error(
            source_path,
            f"{symbol} remaining-week routes do not share one information time.",
        )
    if aggregate.actionable_until != sessions[0].target_window_end:
        raise _contract_error(
            source_path,
            f"{symbol} remaining-week aggregate does not expire at the first "
            "remaining session close.",
        )
    if any(
        route.actionable_until != route.target_window_end
        for route in sessions
    ):
        raise _contract_error(
            source_path,
            f"{symbol} remaining-week components do not expire at their own "
            "session closes.",
        )
    if issued_at < information_available_at:
        raise _contract_error(
            source_path,
            f"{symbol} remaining-week snapshot predates its available information.",
        )
    if any(issued_at >= route.actionable_until for route in routes):
        raise _contract_error(
            source_path,
            f"{symbol} remaining-week snapshot was not issued before every "
            "published route deadline.",
        )
    if (
        aggregate.target_window_start != d1_start
        or aggregate.target_window_end != final_end
    ):
        raise _contract_error(
            source_path,
            f"{symbol} aggregate window does not span all remaining sessions.",
        )
    for previous, current in zip(sessions, sessions[1:]):
        if previous.target_window_end >= current.target_window_start:
            raise _contract_error(
                source_path,
                f"{symbol} remaining-week session windows are not ordered.",
            )
    return WeeklyOutlookView(
        aggregate=aggregate,
        sessions=sessions,
        issued_at=issued_at,
    )


def _route_view(
    symbol: str,
    horizon: str,
    row: dict[str, object],
    *,
    as_of: datetime,
    live_performance: LivePerformanceView | None,
) -> ForecastRouteView:
    actionability_status = (
        _text(row.get("actionability_status")) or "STATUS_UNAVAILABLE"
    )
    target_definition_version = _text(row.get("target_definition_version"))
    frozen_weekly_probability = (
        horizon in WEEKLY_HORIZON_ORDER
        and actionability_status == "FROZEN_WEEKLY_SNAPSHOT"
        and target_definition_version
        == _WEEKLY_TARGET_DEFINITION_VERSIONS[horizon]
    )
    intelligence_status = (
        _text(row.get("intelligence_status"))
        or "INTELLIGENCE_STATUS_UNAVAILABLE"
    )
    target_window_start = _timestamp(row.get("target_window_start"))
    target_window_end = _timestamp(row.get("target_window_end"))
    actionable_until = _timestamp(row.get("actionable_until"))
    forecast_created_at = _timestamp(row.get("forecast_created_at"))
    automated_action_allowed = _boolean(
        row.get("automated_action_allowed")
    )
    trusted_in_progress_probability = (
        horizon in STANDARD_HORIZON_ORDER
        and actionability_status == "TARGET_WINDOW_STARTED"
        and intelligence_status == "FORECAST_IN_PROGRESS"
        and automated_action_allowed is False
        and forecast_created_at is not None
        and actionable_until is not None
        and target_window_start is not None
        and target_window_end is not None
        and forecast_created_at < actionable_until
        and actionable_until <= target_window_start
        and target_window_start <= as_of < target_window_end
    )
    raw_probability_up = _number(row.get("probability_up"))
    raw_probability_down = _number(row.get("probability_down"))
    probability_up = (
        raw_probability_up
        if (
            actionability_status == ACTIONABLE_STATUS
            or frozen_weekly_probability
            or trusted_in_progress_probability
        )
        else None
    )
    probability_down = (
        raw_probability_down
        if (
            actionability_status == ACTIONABLE_STATUS
            or frozen_weekly_probability
            or trusted_in_progress_probability
        )
        else None
    )
    warnings: list[str] = []
    if (
        actionability_status != ACTIONABLE_STATUS
        and not frozen_weekly_probability
        and not trusted_in_progress_probability
        and (
            raw_probability_up is not None
            or raw_probability_down is not None
        )
    ):
        warnings.append(
            f"{symbol} {horizon} supplied a non-current probability; "
            "the dashboard suppressed it."
        )
    if (
        (
            actionability_status == ACTIONABLE_STATUS
            or frozen_weekly_probability
            or trusted_in_progress_probability
        )
        and (probability_up is None or probability_down is None)
    ):
        warnings.append(
            f"{symbol} {horizon} is marked current but its probability is null."
        )

    model_evidence_status = (
        _text(row.get("model_evidence_status"))
        or "MODEL_EVIDENCE_UNAVAILABLE"
    )
    live_evidence_status = (
        _text(row.get("live_evidence_status"))
        or "LIVE_EVIDENCE_UNAVAILABLE"
    )
    completed_count = _integer(row.get("completed_decision_count"))
    persisted_minimum = _integer(row.get("minimum_live_decision_count"))
    minimum_count = (
        persisted_minimum
        if persisted_minimum is not None and persisted_minimum > 0
        else minimum_live_decisions(horizon)
    )
    if (
        live_performance is not None
        and completed_count is not None
        and live_performance.evidence_count != completed_count
    ):
        warnings.append(
            f"{symbol} {horizon} live-performance evidence count "
            f"({live_performance.evidence_count}) does not match the published "
            f"Live Evidence count ({completed_count}); performance was suppressed."
        )
        live_performance = None
    debug_names = (
        "id",
        "symbol",
        "horizon",
        "model_name",
        "decision_timestamp",
        "forecast_created_at",
        "information_available_at",
        "target_window_start",
        "target_window_end",
        "actionable_until",
        "target_definition_version",
        "probability_up",
        "probability_down",
        "actionability_status",
        "operational_status",
        "model_evidence_status",
        "live_evidence_status",
        "intelligence_status",
        "completed_decision_count",
        "minimum_live_decision_count",
        "automated_action_allowed",
        "limitations",
        "schema_version",
    )
    return ForecastRouteView(
        id=_text(row.get("id")),
        symbol=symbol,
        horizon=horizon,
        horizon_label=HORIZON_LABELS[horizon],
        model_name=_text(row.get("model_name")),
        decision_timestamp=_timestamp(row.get("decision_timestamp")),
        forecast_created_at=forecast_created_at,
        information_available_at=_timestamp(
            row.get("information_available_at")
        ),
        target_window_start=target_window_start,
        target_window_end=target_window_end,
        actionable_until=actionable_until,
        target_definition_version=target_definition_version,
        probability_up=probability_up,
        probability_down=probability_down,
        actionability_status=actionability_status,
        actionability_label=_actionability_label(actionability_status),
        actionability_tone=_actionability_tone(actionability_status),
        model_evidence_status=model_evidence_status,
        live_evidence_status=live_evidence_status,
        live_evidence_label=_live_evidence_label(
            live_evidence_status,
            completed_count=completed_count,
            minimum_count=minimum_count,
        ),
        completed_decision_count=completed_count,
        minimum_live_decision_count=minimum_count,
        live_performance=live_performance,
        operational_status=(
            _text(row.get("operational_status"))
            or "OPERATIONAL_STATUS_UNAVAILABLE"
        ),
        intelligence_status=intelligence_status,
        automated_action_allowed=automated_action_allowed,
        limitation=_text(row.get("limitations")),
        is_missing=False,
        warnings=tuple(warnings),
        debug_fields=tuple(
            (name, _debug_value(row.get(name))) for name in debug_names
        )
        + _live_performance_debug_fields(live_performance),
    )


def _missing_route(
    symbol: str,
    horizon: str,
) -> ForecastRouteView:
    return ForecastRouteView(
        id=None,
        symbol=symbol,
        horizon=horizon,
        horizon_label=HORIZON_LABELS[horizon],
        model_name=None,
        decision_timestamp=None,
        forecast_created_at=None,
        information_available_at=None,
        target_window_start=None,
        target_window_end=None,
        actionable_until=None,
        target_definition_version=None,
        probability_up=None,
        probability_down=None,
        actionability_status="MISSING_HORIZON",
        actionability_label="No Current Forecast",
        actionability_tone="neutral",
        model_evidence_status="MODEL_EVIDENCE_UNAVAILABLE",
        live_evidence_status="LIVE_EVIDENCE_UNAVAILABLE",
        live_evidence_label="Unavailable",
        completed_decision_count=None,
        minimum_live_decision_count=minimum_live_decisions(horizon),
        live_performance=None,
        operational_status="ROUTE_UNAVAILABLE",
        intelligence_status="NO_CURRENT_FORECAST",
        automated_action_allowed=None,
        limitation="No current-output row was published for this horizon.",
        is_missing=True,
        warnings=(),
        debug_fields=(
            ("symbol", symbol),
            ("horizon", horizon),
            ("published", "False"),
            (
                "minimum_live_decision_count",
                str(minimum_live_decisions(horizon)),
            ),
        ),
    )


def _meaningful_rows(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    symbol = frame["symbol"].notna()
    horizon = frame["horizon"].notna()
    return frame.loc[symbol | horizon].copy()


def _intelligence_schema_version(frame: pd.DataFrame) -> str:
    if "schema_version" not in frame.columns:
        raise RuntimeError(
            "Unsupported intelligence schema version: "
            "schema_version column is missing"
        )
    versions = {
        value.strip()
        for value in frame["schema_version"].dropna().astype(str)
        if value.strip()
    }
    unknown_versions = versions.difference(
        _SUPPORTED_INTELLIGENCE_SCHEMA_VERSIONS
    )
    if unknown_versions:
        raise RuntimeError(
            "Unsupported intelligence schema version: "
            + ", ".join(sorted(unknown_versions))
        )
    if len(versions) > 1:
        raise RuntimeError(
            "Unsupported intelligence schema version: mixed versions "
            + ", ".join(sorted(versions))
        )
    if versions:
        return next(iter(versions))
    if all(field in frame.columns for field in _V2_INTELLIGENCE_FIELDS):
        return _CURRENT_INTELLIGENCE_SCHEMA_VERSION
    return _LEGACY_INTELLIGENCE_SCHEMA_VERSION


def _structured_read_error(
    path: Path,
    exc: Exception,
) -> ForecastDataError:
    detail = f"{type(exc).__name__}: {exc}"
    message = str(exc)
    if isinstance(exc, FileNotFoundError):
        return ForecastDataError(
            code="MISSING_FILE",
            title="Forecast Data Is Not Available",
            message="The consolidated rolling forecast file is missing.",
            path=path,
            technical_detail=detail,
        )
    if "Unsupported intelligence schema version" in message:
        return ForecastDataError(
            code="UNSUPPORTED_SCHEMA_VERSION",
            title="Forecast Data Needs a Newer App Version",
            message=(
                "The file uses a rolling forecast schema version this app "
                "does not support."
            ),
            path=path,
            technical_detail=detail,
        )
    if "Parquet physical schema mismatch" in message:
        return ForecastDataError(
            code="SCHEMA_INCOMPATIBLE",
            title="Forecast Data Has an Incompatible Schema",
            message=(
                "The consolidated file does not match the supported "
                "one-id-v1 or one-id-v2 intelligence contracts."
            ),
            path=path,
            technical_detail=detail,
        )
    return ForecastDataError(
        code="UNREADABLE_FILE",
        title="Forecast Data Could Not Be Read",
        message=(
            "The consolidated rolling forecast file is corrupt, incomplete, "
            "or temporarily unavailable."
        ),
        path=path,
        technical_detail=detail,
    )


def _contract_error(path: Path, message: str) -> ForecastDataError:
    return ForecastDataError(
        code="SCHEMA_INCOMPATIBLE",
        title="Forecast Data Has an Incompatible Schema",
        message=message,
        path=path,
        technical_detail=message,
    )


def _freshness(statuses: tuple[str, ...]) -> tuple[str, str]:
    if not statuses:
        return "No Forecast Data", "neutral"
    if "REFRESH_FAILED" in statuses:
        return "Latest Refresh Failed", "danger"
    if "REFRESH_IN_PROGRESS" in statuses:
        return "Refresh in Progress", "warning"
    has_stale = any("STALE" in status for status in statuses)
    has_current = any(
        status in _CURRENT_OPERATIONAL_STATUSES for status in statuses
    )
    if has_stale and has_current:
        return "Current Outlooks with Route Gaps", "warning"
    if has_stale:
        return "Data Is Stale", "danger"
    if all(
        status in _CURRENT_OPERATIONAL_STATUSES for status in statuses
    ):
        return "Data Pipeline Is Current", "success"
    return "Data Pipeline Has Limitations", "warning"


def _operational_summary(
    statuses: tuple[str, ...],
) -> tuple[str, str]:
    if not statuses:
        return "Operational Status Unavailable", "neutral"
    if "REFRESH_FAILED" in statuses:
        return "Refresh Failed", "danger"
    if "REFRESH_IN_PROGRESS" in statuses:
        return "Refreshing Current Output", "warning"
    has_stale = any("STALE" in status for status in statuses)
    has_current = any(
        status in _CURRENT_OPERATIONAL_STATUSES for status in statuses
    )
    if has_stale and has_current:
        return "Operational with Route Timing Gaps", "warning"
    if has_stale:
        return "Operational Data Is Stale", "danger"
    if all(
        status in _CURRENT_OPERATIONAL_STATUSES for status in statuses
    ):
        return "Operationally Current", "success"
    return "Operational with Limitations", "warning"


def _empty_message(
    *,
    published_routes: int,
    actionable_routes: int,
    frozen_weekly_snapshots: int,
    operational_statuses: tuple[str, ...],
) -> str | None:
    if published_routes == 0:
        return "No current forecast rows are available."
    if actionable_routes or frozen_weekly_snapshots:
        return None
    if operational_statuses and all(
        status in _CURRENT_OPERATIONAL_STATUSES
        for status in operational_statuses
    ):
        return (
            "The data pipeline is current, but there is no actionable "
            "forecast."
        )
    return "No published forecast is currently actionable."


def _actionability_label(status: str) -> str:
    labels = {
        "ACTIONABLE": "Current Forecast",
        "TARGET_WINDOW_STARTED": (
            "Forecast in Progress — Entry Window Passed; Not Actionable"
        ),
        "TARGET_WINDOW_PASSED": "Forecast Window Has Passed",
        "ENTRY_WINDOW_PASSED": "Forecast Window Has Passed",
        "NO_ACTIONABLE_CANDIDATE": "No Current Forecast",
        "NO_ELIGIBLE_SOURCE_DATA": "No Eligible Source Data",
        "MODEL_UNAVAILABLE": "Model Unavailable",
        "TARGET_TIMESTAMP_INVALID": "Forecast Timing Unavailable",
        "REFRESH_IN_PROGRESS": "Refresh in Progress",
        "REFRESH_FAILED": "Refresh Failed",
    }
    return labels.get(status, _humanize(status))


def _actionability_tone(status: str) -> str:
    if status == ACTIONABLE_STATUS:
        return "success"
    if "FAILED" in status or "INVALID" in status:
        return "danger"
    if status in {
        "TARGET_WINDOW_STARTED",
        "TARGET_WINDOW_PASSED",
        "ENTRY_WINDOW_PASSED",
    }:
        return "warning"
    return "neutral"


def _live_evidence_label(
    status: str,
    *,
    completed_count: int | None,
    minimum_count: int,
) -> str:
    if completed_count == 0:
        return (
            "Awaiting First Completed Forecast "
            f"(0 of {minimum_count})"
        )
    if completed_count is not None and completed_count > 0:
        return (
            f"{completed_count} of {minimum_count} Completed Forecasts"
        )
    labels = {
        "NO_MATCHING_MODEL_ROUTE": "No Matching Live Evidence",
        "LIVE_EVIDENCE_PENDING": "Pending",
        "LIVE_EVIDENCE_UNAVAILABLE": "Unavailable",
    }
    return labels.get(status, _humanize(status))


def _humanize(value: str) -> str:
    return value.replace("_", " ").strip().title() or "Unavailable"


def _timestamp(value: object) -> datetime | None:
    if _is_null(value):
        return None
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.to_pydatetime()


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _text(value: object) -> str | None:
    if _is_null(value):
        return None
    clean = str(value).strip()
    return clean or None


def _number(value: object) -> float | None:
    if _is_null(value):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _integer(value: object) -> int | None:
    number = _number(value)
    return None if number is None else int(number)


def _boolean(value: object) -> bool | None:
    if _is_null(value):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().casefold()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _is_null(value: object) -> bool:
    if value is None:
        return True
    try:
        result = pd.isna(value)
    except (TypeError, ValueError):
        return False
    if getattr(result, "shape", ()) != ():
        return False
    try:
        return bool(result)
    except (TypeError, ValueError):
        return False


def _debug_value(value: object) -> str:
    return "(null)" if _is_null(value) else str(value)
