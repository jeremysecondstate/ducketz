from __future__ import annotations

import threading
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from tkinter import ttk
from typing import Iterable, Mapping

from app.ui.rolling_forecast_data import (
    ForecastDashboardView,
    ForecastDataError,
    ForecastRefreshState,
    ForecastRouteView,
    WeeklyOutlookView,
    FORECAST_SUBTITLE,
    STANDARD_HORIZON_ORDER,
    dashboard_debug_text,
    dashboard_layout,
    default_rolling_predictions_path,
    format_probability,
    format_session_date,
    format_timestamp_local,
    format_timestamp_utc,
    load_forecast_dashboard,
    route_accessible_status_labels,
    route_live_performance_labels,
    route_outcome_evidence_label,
    route_publication_summary,
)
from app.ui.theme import (
    ACCENT,
    BACKGROUND,
    BORDER,
    DANGER,
    MUTED_TEXT,
    SUCCESS,
    SURFACE,
    SURFACE_ALT,
    TEXT,
    WARNING,
)

HOURLY_AUTO_REFRESH_MS = 60 * 60 * 1000


@dataclass
class _SymbolSectionWidgets:
    symbol: str
    section: ttk.Frame
    header: tk.Button
    body: ttk.Frame
    cards: tuple[ttk.Frame, ...]
    weekly_card: ttk.Frame
    collapsed_summary: str


def forecast_symbol_section_summary(
    symbol: object,
    *,
    forecast_count: int,
    remaining_week_available: bool,
) -> str:
    name = str(symbol or "").strip().upper()
    if not name:
        raise ValueError("A forecast symbol is required for its section summary.")
    if forecast_count < 0:
        raise ValueError("Forecast count cannot be negative.")
    weekly = (
        "Remaining-Week Snapshot Available"
        if remaining_week_available
        else "No Remaining-Week Snapshot"
    )
    return (
        f"{name} · {forecast_count} Forecast{'s' if forecast_count != 1 else ''} · {weekly}"
    )


def merge_symbol_expansion_state(
    prior: Mapping[str, bool],
    symbols: Iterable[object],
) -> dict[str, bool]:
    """Retain session state by symbol while defaulting newly encountered names open."""

    merged = {str(symbol).strip().upper(): bool(expanded) for symbol, expanded in prior.items()}
    for value in symbols:
        symbol = str(value or "").strip().upper()
        if symbol:
            merged.setdefault(symbol, True)
    return merged


def forecast_symbol_header_text(
    symbol: object,
    *,
    expanded: bool,
    collapsed_summary: str,
) -> str:
    name = str(symbol or "").strip().upper()
    if not name:
        raise ValueError("A forecast symbol is required for its section header.")
    return f"▼ {name}" if expanded else f"▶ {collapsed_summary}"


class RollingForecastTab:
    def __init__(
        self,
        *,
        root: tk.Tk,
        parent: ttk.Frame,
        predictions_path: Path | None = None,
        local_timezone: tzinfo | None = None,
    ) -> None:
        self.root = root
        self.predictions_path = Path(
            predictions_path or default_rolling_predictions_path()
        )
        self.local_timezone = (
            local_timezone or datetime.now().astimezone().tzinfo
        )
        self.state = ForecastRefreshState()
        self.refresh_button: ttk.Button | None = None
        self.debug_button: ttk.Button | None = None
        self.summary_frame: ttk.Frame | None = None
        self.message_frame: ttk.Frame | None = None
        self.canvas: tk.Canvas | None = None
        self.canvas_window: int | None = None
        self.content_frame: ttk.Frame | None = None
        self.source_label: ttk.Label | None = None
        self._summary_cards: list[ttk.Frame] = []
        self._symbol_sections: list[_SymbolSectionWidgets] = []
        self._symbol_expanded: dict[str, bool] = {}
        self._layout_columns: int | None = None
        self._width = 1180
        self._hourly_refresh_job: str | None = None

        self._apply_styles()
        self._build(parent)
        self.root.after_idle(self.refresh)
        self._schedule_hourly_refresh()

    def _apply_styles(self) -> None:
        style = ttk.Style(self.root)
        style.configure(
            "Forecast.TFrame",
            background=BACKGROUND,
        )
        style.configure(
            "ForecastSurface.TFrame",
            background=SURFACE,
            bordercolor=BORDER,
            borderwidth=1,
            relief=tk.SOLID,
        )
        style.configure(
            "ForecastCard.TFrame",
            background=SURFACE,
            bordercolor=BORDER,
            borderwidth=1,
            relief=tk.SOLID,
        )
        style.configure(
            "ForecastCardBody.TFrame",
            background=SURFACE,
        )
        style.configure(
            "ForecastTitle.TLabel",
            background=BACKGROUND,
            foreground=TEXT,
            font=("Segoe UI", 22, "bold"),
        )
        style.configure(
            "ForecastSubtitle.TLabel",
            background=BACKGROUND,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 10),
        )
        style.configure(
            "ForecastSection.TLabel",
            background=SURFACE,
            foreground=TEXT,
            font=("Segoe UI", 15, "bold"),
        )
        style.configure(
            "ForecastCardTitle.TLabel",
            background=SURFACE,
            foreground=TEXT,
            font=("Segoe UI", 12, "bold"),
        )
        style.configure(
            "ForecastCardValue.TLabel",
            background=SURFACE,
            foreground=TEXT,
            font=("Segoe UI", 11, "bold"),
        )
        style.configure(
            "ForecastBody.TLabel",
            background=SURFACE,
            foreground=TEXT,
            font=("Segoe UI", 9),
        )
        style.configure(
            "ForecastMuted.TLabel",
            background=SURFACE,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 9),
        )
        for tone, color in (
            ("Success", SUCCESS),
            ("Warning", WARNING),
            ("Danger", DANGER),
            ("Neutral", MUTED_TEXT),
        ):
            style.configure(
                f"Forecast{tone}.TLabel",
                background=SURFACE,
                foreground=color,
                font=("Segoe UI", 9, "bold"),
            )
        style.configure(
            "ForecastBanner.TLabel",
            background=SURFACE_ALT,
            foreground=TEXT,
            font=("Segoe UI", 9),
            padding=(10, 8),
        )

    def _build(self, parent: ttk.Frame) -> None:
        outer = ttk.Frame(
            parent,
            padding=(18, 14, 18, 12),
            style="Forecast.TFrame",
        )
        outer.pack(fill=tk.BOTH, expand=True)
        outer.bind("<Configure>", self._on_resize, add="+")

        header = ttk.Frame(outer, style="Forecast.TFrame")
        header.pack(fill=tk.X)
        title_area = ttk.Frame(header, style="Forecast.TFrame")
        title_area.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(
            title_area,
            text="Rolling Forecasts",
            style="ForecastTitle.TLabel",
        ).pack(anchor=tk.W)
        ttk.Label(
            title_area,
            text=FORECAST_SUBTITLE,
            style="ForecastSubtitle.TLabel",
        ).pack(anchor=tk.W, pady=(2, 0))

        actions = ttk.Frame(header, style="Forecast.TFrame")
        actions.pack(side=tk.RIGHT, anchor=tk.N)
        self.debug_button = ttk.Button(
            actions,
            text="Debug Details",
            command=self._show_debug,
            state=tk.DISABLED,
        )
        self.debug_button.pack(side=tk.LEFT, padx=(0, 8))
        self.refresh_button = ttk.Button(
            actions,
            text="Refresh",
            command=self.refresh,
        )
        self.refresh_button.pack(side=tk.LEFT)

        self.summary_frame = ttk.Frame(outer, style="Forecast.TFrame")
        self.summary_frame.pack(fill=tk.X, pady=(14, 8))

        self.message_frame = ttk.Frame(outer, style="Forecast.TFrame")
        self.message_frame.pack(fill=tk.X)

        canvas_frame = ttk.Frame(outer, style="Forecast.TFrame")
        canvas_frame.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        self.canvas = tk.Canvas(
            canvas_frame,
            background=BACKGROUND,
            borderwidth=0,
            highlightthickness=0,
        )
        scrollbar = ttk.Scrollbar(
            canvas_frame,
            orient=tk.VERTICAL,
            command=self.canvas.yview,
        )
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.content_frame = ttk.Frame(
            self.canvas,
            style="Forecast.TFrame",
        )
        self.canvas_window = self.canvas.create_window(
            (0, 0),
            window=self.content_frame,
            anchor=tk.NW,
        )
        self.content_frame.bind(
            "<Configure>",
            lambda _event: self._update_scroll_region(),
            add="+",
        )
        self.canvas.bind(
            "<Configure>",
            self._on_canvas_resize,
            add="+",
        )
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel, add="+")

        self.source_label = ttk.Label(
            outer,
            text=f"Current-Output Source: {self.predictions_path}",
            style="ForecastSubtitle.TLabel",
        )
        self.source_label.pack(fill=tk.X, pady=(7, 0))

    def refresh(self) -> None:
        if self.state.loading:
            return
        generation = self.state.begin()
        if self.refresh_button is not None:
            self.refresh_button.configure(state=tk.DISABLED)
        if self.debug_button is not None:
            self.debug_button.configure(state=tk.DISABLED)
        self._render_loading()
        thread = threading.Thread(
            target=self._load_in_background,
            args=(generation,),
            daemon=True,
        )
        thread.start()

    def _schedule_hourly_refresh(self) -> None:
        if self._hourly_refresh_job is not None:
            return
        self._hourly_refresh_job = self.root.after(
            HOURLY_AUTO_REFRESH_MS,
            self._run_hourly_refresh,
        )

    def _run_hourly_refresh(self) -> None:
        self._hourly_refresh_job = None
        self.refresh()
        self._schedule_hourly_refresh()

    def _load_in_background(self, generation: int) -> None:
        try:
            view = load_forecast_dashboard(self.predictions_path)
        except ForecastDataError as exc:
            self.root.after(
                0,
                lambda error=exc: self._finish_error(
                    generation,
                    error,
                ),
            )
        except Exception as exc:
            error = ForecastDataError(
                code="UNEXPECTED_ERROR",
                title="Forecast Dashboard Could Not Be Loaded",
                message=(
                    "An unexpected error occurred while preparing the "
                    "forecast dashboard."
                ),
                path=self.predictions_path,
                technical_detail=f"{type(exc).__name__}: {exc}",
            )
            self.root.after(
                0,
                lambda caught=error: self._finish_error(
                    generation,
                    caught,
                ),
            )
        else:
            self.root.after(
                0,
                lambda loaded=view: self._finish_success(
                    generation,
                    loaded,
                ),
            )

    def _finish_success(
        self,
        generation: int,
        view: ForecastDashboardView,
    ) -> None:
        if not self.state.succeed(generation, view):
            return
        self._set_action_states()
        self._render_view(view)

    def _finish_error(
        self,
        generation: int,
        error: ForecastDataError,
    ) -> None:
        if not self.state.fail(generation, error):
            return
        self._set_action_states()
        self._render_error(error)

    def _set_action_states(self) -> None:
        if self.refresh_button is not None:
            self.refresh_button.configure(state=tk.NORMAL)
        if self.debug_button is not None:
            self.debug_button.configure(
                state=(
                    tk.NORMAL
                    if self.state.view is not None
                    or self.state.error is not None
                    else tk.DISABLED
                )
            )

    def _render_loading(self) -> None:
        self._clear_dashboard()
        if self.content_frame is None:
            return
        panel = ttk.Frame(
            self.content_frame,
            padding=24,
            style="ForecastSurface.TFrame",
        )
        panel.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(
            panel,
            text="Loading Current Forecasts...",
            style="ForecastCardValue.TLabel",
        ).pack(anchor=tk.W)
        ttk.Label(
            panel,
            text=(
                "Reading the consolidated current-output file. "
                "Previous values have been cleared."
            ),
            style="ForecastMuted.TLabel",
        ).pack(anchor=tk.W, pady=(5, 0))

    def _render_error(self, error: ForecastDataError) -> None:
        self._clear_dashboard()
        if self.content_frame is None:
            return
        panel = ttk.Frame(
            self.content_frame,
            padding=24,
            style="ForecastSurface.TFrame",
        )
        panel.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(
            panel,
            text=error.title,
            style="ForecastCardTitle.TLabel",
        ).pack(anchor=tk.W)
        ttk.Label(
            panel,
            text=error.message,
            style="ForecastBody.TLabel",
            wraplength=760,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(8, 0))
        ttk.Label(
            panel,
            text=f"Configured Source: {error.path}",
            style="ForecastMuted.TLabel",
            wraplength=760,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(12, 0))
        ttk.Label(
            panel,
            text="Select Refresh to try the current-output file again.",
            style="ForecastMuted.TLabel",
        ).pack(anchor=tk.W, pady=(4, 0))

    def _render_view(self, view: ForecastDashboardView) -> None:
        self._clear_dashboard()
        self._render_summary(view)
        self._render_messages(view)
        if self.content_frame is None:
            return
        if not view.symbols:
            panel = ttk.Frame(
                self.content_frame,
                padding=20,
                style="ForecastSurface.TFrame",
            )
            panel.pack(fill=tk.X, pady=(4, 0))
            ttk.Label(
                panel,
                text="No Forecast Rows",
                style="ForecastCardTitle.TLabel",
            ).pack(anchor=tk.W)
            ttk.Label(
                panel,
                text=(
                    "The consolidated file is readable and schema-compatible, "
                    "but it does not contain a symbol forecast."
                ),
                style="ForecastBody.TLabel",
                wraplength=780,
                justify=tk.LEFT,
            ).pack(anchor=tk.W, pady=(6, 0))
        else:
            self._symbol_expanded = merge_symbol_expansion_state(
                self._symbol_expanded,
                (symbol.symbol for symbol in view.symbols),
            )
            controls = ttk.Frame(self.content_frame, style="Forecast.TFrame")
            controls.pack(fill=tk.X, pady=(0, 7))
            ttk.Button(
                controls,
                text="Expand All",
                command=lambda: self._set_all_symbols_expanded(True),
                width=12,
            ).pack(side=tk.RIGHT)
            ttk.Button(
                controls,
                text="Collapse All",
                command=lambda: self._set_all_symbols_expanded(False),
                width=12,
            ).pack(side=tk.RIGHT, padx=(0, 6))
            for symbol in view.symbols:
                symbol_name = str(symbol.symbol).strip().upper()
                section = ttk.Frame(
                    self.content_frame,
                    style="ForecastSurface.TFrame",
                )
                section.pack(fill=tk.X, pady=(0, 10))
                body = ttk.Frame(
                    section,
                    padding=(12, 0, 12, 12),
                    style="ForecastCardBody.TFrame",
                )
                collapsed_summary = forecast_symbol_section_summary(
                    symbol_name,
                    forecast_count=sum(
                        route.horizon in STANDARD_HORIZON_ORDER
                        for route in symbol.routes
                    ),
                    remaining_week_available=symbol.weekly_outlook is not None,
                )
                header = tk.Button(
                    section,
                    command=lambda name=symbol_name: self._toggle_symbol(name),
                    background=SURFACE,
                    foreground=TEXT,
                    activebackground=SURFACE_ALT,
                    activeforeground=TEXT,
                    font=("Segoe UI", 11, "bold"),
                    anchor=tk.W,
                    justify=tk.LEFT,
                    relief=tk.FLAT,
                    borderwidth=0,
                    highlightthickness=1,
                    highlightbackground=BORDER,
                    highlightcolor=ACCENT,
                    takefocus=True,
                    cursor="hand2",
                    padx=11,
                    pady=8,
                )
                header.pack(fill=tk.X)
                header.bind(
                    "<Return>",
                    lambda _event, name=symbol_name: self._toggle_symbol_from_key(name),
                )
                header.bind(
                    "<KP_Enter>",
                    lambda _event, name=symbol_name: self._toggle_symbol_from_key(name),
                )
                cards = tuple(
                    self._build_route_card(body, route)
                    for route in symbol.routes
                    if route.horizon in STANDARD_HORIZON_ORDER
                )
                weekly_card = self._build_weekly_outlook_card(
                    body,
                    symbol.weekly_outlook,
                )
                widgets = _SymbolSectionWidgets(
                    symbol=symbol_name,
                    section=section,
                    header=header,
                    body=body,
                    cards=cards,
                    weekly_card=weekly_card,
                    collapsed_summary=collapsed_summary,
                )
                self._symbol_sections.append(widgets)
                if self._symbol_expanded[symbol_name]:
                    body.pack(fill=tk.X)
                self._update_symbol_header(widgets)
        self._apply_responsive_layout(force=True)
        self._update_scroll_region()

    def _render_summary(self, view: ForecastDashboardView) -> None:
        if self.summary_frame is None:
            return
        refresh_detail = (
            "Local: "
            + format_timestamp_local(
                view.last_successful_refresh,
                local_timezone=self.local_timezone,
            )
        )
        route_detail = route_publication_summary(view)
        cards = (
            (
                "Data Freshness",
                view.freshness_label,
                route_detail,
                view.freshness_tone,
            ),
            (
                "Last Successful Refresh",
                format_timestamp_utc(view.last_successful_refresh),
                refresh_detail,
                "neutral",
            ),
            (
                "Operational Status",
                view.operational_label,
                "Independent from forecast actionability",
                view.operational_tone,
            ),
            (
                "Automated Action",
                "Off" if not view.automated_action_allowed else "Flag on",
                view.automation_label,
                view.automation_tone,
            ),
        )
        for heading, value, detail, tone in cards:
            card = ttk.Frame(
                self.summary_frame,
                padding=11,
                style="ForecastCard.TFrame",
            )
            ttk.Label(
                card,
                text=heading,
                style="ForecastMuted.TLabel",
            ).pack(anchor=tk.W)
            ttk.Label(
                card,
                text=value,
                style=_tone_style(tone, value=True),
                wraplength=260,
                justify=tk.LEFT,
            ).pack(anchor=tk.W, pady=(4, 0))
            ttk.Label(
                card,
                text=detail,
                style="ForecastMuted.TLabel",
                wraplength=260,
                justify=tk.LEFT,
            ).pack(anchor=tk.W, pady=(4, 0))
            self._summary_cards.append(card)

    def _render_messages(self, view: ForecastDashboardView) -> None:
        if self.message_frame is None:
            return
        if view.empty_message:
            ttk.Label(
                self.message_frame,
                text=view.empty_message,
                style="ForecastBanner.TLabel",
                wraplength=1050,
                justify=tk.LEFT,
            ).pack(fill=tk.X, pady=(0, 6))

    def _build_route_card(
        self,
        parent: ttk.Frame,
        route: ForecastRouteView,
    ) -> ttk.Frame:
        card = ttk.Frame(
            parent,
            padding=12,
            style="ForecastCard.TFrame",
        )
        ttk.Label(
            card,
            text=f"{route.horizon_label} Forecast",
            style="ForecastCardTitle.TLabel",
        ).pack(anchor=tk.W)

        actionability, live_evidence = (
            route_accessible_status_labels(route)
        )
        ttk.Label(
            card,
            text=actionability,
            style=_tone_style(route.actionability_tone),
            wraplength=330,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(7, 0))

        if route.is_actionable or route.is_in_progress:
            probability_text = (
                "Probability Up: "
                f"{format_probability(route.probability_up)}\n"
                "Probability Down: "
                f"{format_probability(route.probability_down)}"
            )
        else:
            probability_text = "No Current Probability"
        ttk.Label(
            card,
            text=probability_text,
            style="ForecastCardValue.TLabel",
            wraplength=330,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(8, 0))

        window_text = (
            "Forecast Window\n"
            f"UTC Start: {format_timestamp_utc(route.target_window_start)}\n"
            f"UTC End: {format_timestamp_utc(route.target_window_end)}\n"
            "Local Start: "
            + format_timestamp_local(
                route.target_window_start,
                local_timezone=self.local_timezone,
            )
            + "\nLocal End: "
            + format_timestamp_local(
                route.target_window_end,
                local_timezone=self.local_timezone,
            )
        )
        ttk.Label(
            card,
            text=window_text,
            style="ForecastBody.TLabel",
            wraplength=330,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(10, 0))

        ttk.Label(
            card,
            text=live_evidence,
            style=_tone_style(_live_tone(route)),
            wraplength=330,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(10, 0))
        cumulative_performance, rolling_performance = (
            route_live_performance_labels(route)
        )
        ttk.Label(
            card,
            text=f"{cumulative_performance}\n{rolling_performance}",
            style="ForecastBody.TLabel",
            wraplength=330,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(5, 0))
        if route.is_missing:
            ttk.Label(
                card,
                text="No current-output row was published for this horizon.",
                style="ForecastMuted.TLabel",
                wraplength=330,
                justify=tk.LEFT,
            ).pack(anchor=tk.W, pady=(8, 0))
        return card

    def _build_weekly_outlook_card(
        self,
        parent: ttk.Frame,
        outlook: WeeklyOutlookView | None,
    ) -> ttk.Frame:
        card = ttk.Frame(
            parent,
            padding=12,
            style="ForecastCard.TFrame",
        )
        header = ttk.Frame(card, style="ForecastCardBody.TFrame")
        header.pack(fill=tk.X)
        ttk.Label(
            header,
            text="Remaining-Week Outlook",
            style="ForecastCardTitle.TLabel",
        ).pack(side=tk.LEFT, anchor=tk.W)

        if outlook is None:
            ttk.Label(
                header,
                text="No Current Snapshot",
                style="ForecastNeutral.TLabel",
            ).pack(side=tk.RIGHT, anchor=tk.E)
            ttk.Label(
                card,
                text=(
                    "No current remaining-week aggregate and session snapshot "
                    "was published for this symbol."
                ),
                style="ForecastMuted.TLabel",
                wraplength=980,
                justify=tk.LEFT,
            ).pack(anchor=tk.W, pady=(10, 0))
            return card

        ttk.Label(
            header,
            text="Current Remaining-Week Snapshot",
            style="ForecastSuccess.TLabel",
        ).pack(side=tk.RIGHT, anchor=tk.E)
        ttk.Label(
            card,
            text=(
                    "Snapshot Issued: "
                f"{format_timestamp_utc(outlook.issued_at)}\n"
                    "Local Issuance: "
                + format_timestamp_local(
                    outlook.issued_at,
                    local_timezone=self.local_timezone,
                )
            ),
            style="ForecastMuted.TLabel",
            wraplength=980,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(7, 0))

        aggregate = outlook.aggregate
        aggregate_panel = ttk.Frame(
            card,
            padding=10,
            style="ForecastCard.TFrame",
        )
        aggregate_panel.pack(fill=tk.X, pady=(10, 4))
        ttk.Label(
            aggregate_panel,
            text="Aggregate (Remaining Week)",
            style="ForecastCardTitle.TLabel",
        ).pack(anchor=tk.W)
        ttk.Label(
            aggregate_panel,
            text=(
                f"Probability Up: {format_probability(aggregate.probability_up)}   "
                f"Probability Down: {format_probability(aggregate.probability_down)}"
            ),
            style="ForecastCardValue.TLabel",
            wraplength=960,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(5, 0))
        ttk.Label(
            aggregate_panel,
            text=(
                "UTC Start: "
                f"{format_timestamp_utc(aggregate.target_window_start)}\n"
                "UTC End: "
                f"{format_timestamp_utc(aggregate.target_window_end)}\n"
                "Local Start: "
                + format_timestamp_local(
                    aggregate.target_window_start,
                    local_timezone=self.local_timezone,
                )
                + "\nLocal End: "
                + format_timestamp_local(
                    aggregate.target_window_end,
                    local_timezone=self.local_timezone,
                )
            ),
            style="ForecastBody.TLabel",
            wraplength=960,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(6, 0))
        ttk.Label(
            aggregate_panel,
            text=(
                "Outcome/evidence: "
                f"{route_outcome_evidence_label(aggregate)} · "
                f"{aggregate.live_evidence_label}"
            ),
            style=_tone_style(_outcome_tone(aggregate)),
            wraplength=960,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(7, 0))
        aggregate_cumulative, aggregate_rolling = (
            route_live_performance_labels(aggregate)
        )
        ttk.Label(
            aggregate_panel,
            text=f"{aggregate_cumulative}\n{aggregate_rolling}",
            style="ForecastBody.TLabel",
            wraplength=960,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(5, 0))

        for route in outlook.sessions:
            session_panel = ttk.Frame(
                card,
                padding=10,
                style="ForecastCard.TFrame",
            )
            session_panel.pack(fill=tk.X, pady=(4, 0))
            ttk.Label(
                session_panel,
                text=(
                    f"{route.horizon_label} · "
                    + format_session_date(
                        route.target_window_start,
                        local_timezone=self.local_timezone,
                    )
                ),
                style="ForecastCardTitle.TLabel",
            ).pack(anchor=tk.W)
            ttk.Label(
                session_panel,
                text=(
                    f"Probability Up: {format_probability(route.probability_up)}   "
                    f"Probability Down: {format_probability(route.probability_down)}"
                ),
                style="ForecastCardValue.TLabel",
                wraplength=960,
                justify=tk.LEFT,
            ).pack(anchor=tk.W, pady=(5, 0))
            ttk.Label(
                session_panel,
                text=(
                    "UTC Open: "
                    f"{format_timestamp_utc(route.target_window_start)}\n"
                    "UTC Close: "
                    f"{format_timestamp_utc(route.target_window_end)}\n"
                    "Local Open: "
                    + format_timestamp_local(
                        route.target_window_start,
                        local_timezone=self.local_timezone,
                    )
                    + "\nLocal Close: "
                    + format_timestamp_local(
                        route.target_window_end,
                        local_timezone=self.local_timezone,
                    )
                ),
                style="ForecastBody.TLabel",
                wraplength=960,
                justify=tk.LEFT,
            ).pack(anchor=tk.W, pady=(6, 0))
            ttk.Label(
                session_panel,
                text=(
                    "Outcome/Evidence: "
                    f"{route_outcome_evidence_label(route)} · "
                    f"{route.live_evidence_label}"
                ),
                style=_tone_style(_outcome_tone(route)),
                wraplength=960,
                justify=tk.LEFT,
            ).pack(anchor=tk.W, pady=(7, 0))
            session_cumulative, session_rolling = (
                route_live_performance_labels(route)
            )
            ttk.Label(
                session_panel,
                text=f"{session_cumulative}\n{session_rolling}",
                style="ForecastBody.TLabel",
                wraplength=960,
                justify=tk.LEFT,
            ).pack(anchor=tk.W, pady=(5, 0))
        return card

    def _clear_dashboard(self) -> None:
        for container in (
            self.summary_frame,
            self.message_frame,
            self.content_frame,
        ):
            if container is None:
                continue
            for child in container.winfo_children():
                child.destroy()
        self._summary_cards.clear()
        self._symbol_sections.clear()
        self._layout_columns = None

    def _toggle_symbol(self, symbol: str) -> None:
        name = str(symbol or "").strip().upper()
        self._set_symbol_expanded(
            name,
            not self._symbol_expanded.get(name, True),
        )

    def _toggle_symbol_from_key(self, symbol: str) -> str:
        self._toggle_symbol(symbol)
        return "break"

    def _set_symbol_expanded(
        self,
        symbol: str,
        expanded: bool,
        *,
        refresh_layout: bool = True,
    ) -> None:
        name = str(symbol or "").strip().upper()
        if not name:
            return
        self._symbol_expanded[name] = bool(expanded)
        widgets = next(
            (item for item in self._symbol_sections if item.symbol == name),
            None,
        )
        if widgets is None:
            return
        if expanded:
            widgets.body.pack(fill=tk.X)
        else:
            widgets.body.pack_forget()
        self._update_symbol_header(widgets)
        if refresh_layout:
            self._apply_responsive_layout(force=True)
        else:
            self._refresh_scroll_region()

    def _set_all_symbols_expanded(self, expanded: bool) -> None:
        for widgets in self._symbol_sections:
            self._set_symbol_expanded(
                widgets.symbol,
                expanded,
                refresh_layout=False,
            )
        self._apply_responsive_layout(force=True)

    def _update_symbol_header(self, widgets: _SymbolSectionWidgets) -> None:
        expanded = self._symbol_expanded.get(widgets.symbol, True)
        widgets.header.configure(
            text=forecast_symbol_header_text(
                widgets.symbol,
                expanded=expanded,
                collapsed_summary=widgets.collapsed_summary,
            )
        )

    def _refresh_scroll_region(self) -> None:
        if self.content_frame is not None:
            self.content_frame.update_idletasks()
        self._update_scroll_region()
        self.root.after_idle(self._update_scroll_region)

    def _on_resize(self, event: tk.Event[tk.Misc]) -> None:
        self._width = max(1, int(event.width))
        self._apply_responsive_layout()

    def _on_canvas_resize(self, event: tk.Event[tk.Canvas]) -> None:
        if self.canvas is not None and self.canvas_window is not None:
            self.canvas.itemconfigure(
                self.canvas_window,
                width=max(1, int(event.width)),
            )
        self._refresh_scroll_region()

    def _apply_responsive_layout(self, *, force: bool = False) -> None:
        columns = dashboard_layout(self._width)
        if columns == self._layout_columns and not force:
            return
        self._layout_columns = columns
        self._layout_summary()
        for widgets in self._symbol_sections:
            if not self._symbol_expanded.get(widgets.symbol, True):
                continue
            section = widgets.body
            cards = widgets.cards
            weekly_card = widgets.weekly_card
            for card in cards:
                card.grid_forget()
            weekly_card.grid_forget()
            for column in range(len(STANDARD_HORIZON_ORDER)):
                section.grid_columnconfigure(
                    column,
                    weight=1 if column < columns else 0,
                    uniform=(
                        "forecast-route"
                        if column < columns
                        else ""
                    ),
                )
            for index, card in enumerate(cards):
                row, column = divmod(index, columns)
                card.grid(
                    row=row,
                    column=column,
                    sticky=tk.NSEW,
                    padx=(0 if column == 0 else 5, 0),
                    pady=(0, 7),
                )
            ordinary_rows = (len(cards) + columns - 1) // columns
            weekly_card.grid(
                row=ordinary_rows,
                column=0,
                columnspan=columns,
                sticky=tk.NSEW,
                pady=(1, 7),
            )
        self._refresh_scroll_region()

    def _layout_summary(self) -> None:
        if self.summary_frame is None:
            return
        if self._width >= 1080:
            columns = 4
        elif self._width >= 650:
            columns = 2
        else:
            columns = 1
        for card in self._summary_cards:
            card.grid_forget()
        for column in range(4):
            self.summary_frame.grid_columnconfigure(
                column,
                weight=1 if column < columns else 0,
            )
        for index, card in enumerate(self._summary_cards):
            row, column = divmod(index, columns)
            card.grid(
                row=row,
                column=column,
                sticky=tk.NSEW,
                padx=(0 if column == 0 else 5, 0),
                pady=(0, 5),
            )

    def _update_scroll_region(self) -> None:
        if self.canvas is not None:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_mousewheel(self, event: tk.Event[tk.Misc]) -> None:
        if self.canvas is None:
            return
        widget = self.canvas.winfo_containing(
            event.x_root,
            event.y_root,
        )
        while widget is not None:
            if widget == self.canvas:
                self.canvas.yview_scroll(
                    int(-event.delta / 120),
                    "units",
                )
                return
            widget = widget.master

    def _show_debug(self) -> None:
        if self.state.view is not None:
            content = dashboard_debug_text(self.state.view)
        elif self.state.error is not None:
            error = self.state.error
            content = "\n".join(
                (
                    "ROLLING FORECAST LOAD ERROR",
                    "",
                    f"Code: {error.code}",
                    f"Source: {error.path}",
                    f"Message: {error.message}",
                    f"Technical detail: {error.technical_detail}",
                )
            )
        else:
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("Rolling Forecast Debug Details")
        dialog.geometry("820x620")
        dialog.configure(background=BACKGROUND)
        dialog.transient(self.root)

        ttk.Label(
            dialog,
            text="Technical Fields and Raw Statuses",
            style="ForecastTitle.TLabel",
        ).pack(anchor=tk.W, padx=16, pady=(14, 8))
        text = tk.Text(
            dialog,
            background=SURFACE,
            foreground=TEXT,
            insertbackground=TEXT,
            selectbackground=ACCENT,
            borderwidth=1,
            relief=tk.SOLID,
            wrap=tk.NONE,
            font=("Cascadia Mono", 9),
        )
        text.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 10))
        text.insert("1.0", content)
        text.configure(state=tk.DISABLED)
        ttk.Button(
            dialog,
            text="Close",
            command=dialog.destroy,
        ).pack(anchor=tk.E, padx=16, pady=(0, 14))
        dialog.bind("<Escape>", lambda _event: dialog.destroy())


def _tone_style(tone: str, *, value: bool = False) -> str:
    styles = {
        "success": "ForecastSuccess.TLabel",
        "warning": "ForecastWarning.TLabel",
        "danger": "ForecastDanger.TLabel",
        "neutral": "ForecastNeutral.TLabel",
    }
    if value and tone == "neutral":
        return "ForecastCardValue.TLabel"
    return styles.get(tone, "ForecastNeutral.TLabel")


def _live_tone(route: ForecastRouteView) -> str:
    if route.live_evidence_status == "LIVE_EVIDENCE_AVAILABLE":
        return "success"
    if route.live_evidence_status == "INSUFFICIENT_LIVE_EVIDENCE":
        return "warning"
    return "neutral"


def _outcome_tone(route: ForecastRouteView) -> str:
    if route.intelligence_status == "COMPLETED_EVIDENCE":
        return "success"
    if route.intelligence_status == "PENDING_EVIDENCE":
        return "warning"
    return "neutral"
