from __future__ import annotations

import math
import threading
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
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
HOURLY_REFRESH_BOUNDARY_GRACE_SECONDS = 5
ANALYTIC_BLUE = "#5aaeff"
UP_COLOR = "#58c76b"
DOWN_COLOR = "#ff684a"
PERFORMANCE_SURFACE = "#0d1929"
TRACK_COLOR = "#26384e"
TRACK_REMAINDER = "#34485f"
SUCCESS_BADGE = "#173a2a"
WARNING_BADGE = "#3b3018"
DANGER_BADGE = "#3d2426"
NEUTRAL_BADGE = "#263344"
PREDICTION_PULSE_MARK_ASSET_DIR = (
    Path(__file__).with_name("assets") / "security_marks"
)
PREDICTION_PULSE_UP_BACKGROUND = "#123721"
PREDICTION_PULSE_UP_BORDER = "#43d65c"
PREDICTION_PULSE_NEUTRAL_BACKGROUND = "#343331"
PREDICTION_PULSE_NEUTRAL_BORDER = "#e6b83f"
PREDICTION_PULSE_DOWN_BACKGROUND = "#412326"
PREDICTION_PULSE_DOWN_BORDER = "#ff514f"
PREDICTION_PULSE_UNAVAILABLE_BACKGROUND = "#172334"


def probability_segment_fractions(
    probability_up: float | None,
    probability_down: float | None,
) -> tuple[float, float] | None:
    """Return safe proportional geometry without inventing either probability."""

    if not _is_finite_number(probability_up) or not _is_finite_number(
        probability_down
    ):
        return None
    up = max(0.0, float(probability_up))
    down = max(0.0, float(probability_down))
    total = up + down
    if total <= 0.0:
        return None
    return up / total, down / total


def evidence_progress_fraction(
    completed_count: int | None,
    minimum_count: int,
) -> float | None:
    """Return clamped drawing progress while leaving displayed counts untouched."""

    if completed_count is None or minimum_count <= 0:
        return None
    return max(0.0, min(1.0, float(completed_count) / float(minimum_count)))


def live_performance_lift_tone(lift: float | None) -> str:
    if not _is_finite_number(lift):
        return "neutral"
    if float(lift) > 0.0:
        return "success"
    if float(lift) < 0.0:
        return "danger"
    return "neutral"


def rolling_performance_heading(
    rolling_count: int,
    rolling_window_size: int,
) -> str:
    count = max(0, int(rolling_count))
    window = max(0, int(rolling_window_size))
    if window == 0:
        return "ROLLING"
    if count >= window:
        return f"ROLLING {window}"
    return f"ROLLING {count}/{window}"


def milliseconds_until_next_hour(now: datetime | None = None) -> int:
    """Align forecast rotation to the next wall-clock hour plus a short grace."""

    current = now or datetime.now().astimezone()
    boundary = (current + timedelta(hours=1)).replace(
        minute=0,
        second=HOURLY_REFRESH_BOUNDARY_GRACE_SECONDS,
        microsecond=0,
    )
    return max(1_000, math.ceil((boundary - current).total_seconds() * 1_000))


def weekly_session_details_header_text(
    session_count: int,
    *,
    expanded: bool,
) -> str:
    count = max(0, int(session_count))
    noun = "Session" if count == 1 else "Sessions"
    state = "Expanded" if expanded else "Collapsed"
    marker = "▼" if expanded else "▶"
    return f"{marker} Weekly Session Details · {count} Published {noun} · {state}"


def _is_finite_number(value: object) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _percentage_text(value: float | None) -> str:
    return "Unavailable" if not _is_finite_number(value) else f"{float(value):.1%}"


def _lift_text(value: float | None) -> str:
    return (
        "Unavailable"
        if not _is_finite_number(value)
        else f"{float(value) * 100:+.1f} pp"
    )


def _stacked_utc_timestamp(value: datetime | None) -> str:
    text = format_timestamp_utc(value)
    if text == "Unavailable":
        return text
    date, clock, zone = text.rsplit(" ", maxsplit=2)
    return f"{date}\n{clock} {zone}"


def _compact_local_timestamp(
    value: datetime | None,
    *,
    local_timezone: tzinfo | None,
) -> str:
    text = format_timestamp_local(value, local_timezone=local_timezone)
    if text == "Unavailable":
        return text
    parts = text.split()
    if len(parts) <= 5:
        return text
    zone = "".join(word[0].upper() for word in parts[4:] if word)
    return " ".join((*parts[:4], zone))


def prediction_pulse_tone(probability_up: float | None) -> str:
    """Classify a published up probability without inventing a fallback."""

    if not _is_finite_number(probability_up):
        return "unavailable"
    probability = float(probability_up)
    if probability < 0.0 or probability > 1.0:
        return "unavailable"
    if math.isclose(probability, 0.5, rel_tol=0.0, abs_tol=1e-12):
        return "neutral"
    return "up" if probability > 0.5 else "down"


def prediction_pulse_probability_text(probability_up: float | None) -> str:
    if prediction_pulse_tone(probability_up) == "unavailable":
        return "N/A"
    return f"{float(probability_up):.1%}"


def prediction_pulse_probabilities(
    routes: Iterable[ForecastRouteView],
) -> tuple[tuple[str, float | None], ...]:
    """Return displayed values, using saved raw scores when calibration is flat."""

    by_horizon = {
        route.horizon: route.display_probability_up
        for route in routes
        if route.horizon in STANDARD_HORIZON_ORDER
    }
    return tuple(
        (horizon, by_horizon.get(horizon))
        for horizon in STANDARD_HORIZON_ORDER
    )


def prediction_pulse_columns(width: int) -> int:
    """Keep the six-symbol concept intact while stacking it on narrow screens."""

    if width >= 1040:
        return 6
    if width >= 720:
        return 3
    if width >= 520:
        return 2
    return 1


def prediction_pulse_mark_path(
    symbol: object,
    *,
    asset_root: Path = PREDICTION_PULSE_MARK_ASSET_DIR,
) -> Path | None:
    name = str(symbol or "").strip().upper()
    if not name:
        return None
    asset_name = "GOOG" if name == "GOOGL" else name
    candidate = asset_root / f"{asset_name.casefold()}.png"
    return candidate if candidate.is_file() else None


def _prediction_pulse_monogram(symbol: object) -> str:
    cleaned = "".join(
        character
        for character in str(symbol or "").strip().upper()
        if character.isalnum()
    )
    return cleaned[:2] or "--"


def _prediction_pulse_symbol_color(symbol: object) -> str:
    palette = (
        "#2563eb",
        "#0f766e",
        "#7c3aed",
        "#b45309",
        "#be123c",
        "#0369a1",
    )
    cleaned = str(symbol or "").strip().upper()
    if not cleaned:
        return BORDER
    return palette[sum(ord(character) for character in cleaned) % len(palette)]


def _prediction_pulse_palette(tone: str) -> tuple[str, str, str]:
    return {
        "up": (
            PREDICTION_PULSE_UP_BACKGROUND,
            PREDICTION_PULSE_UP_BORDER,
            "#eefbf0",
        ),
        "neutral": (
            PREDICTION_PULSE_NEUTRAL_BACKGROUND,
            PREDICTION_PULSE_NEUTRAL_BORDER,
            "#fff8e5",
        ),
        "down": (
            PREDICTION_PULSE_DOWN_BACKGROUND,
            PREDICTION_PULSE_DOWN_BORDER,
            "#fff0ef",
        ),
        "unavailable": (
            PREDICTION_PULSE_UNAVAILABLE_BACKGROUND,
            BORDER,
            MUTED_TEXT,
        ),
    }.get(
        tone,
        (PREDICTION_PULSE_UNAVAILABLE_BACKGROUND, BORDER, MUTED_TEXT),
    )


class _PredictionPulseMark(tk.Canvas):
    def __init__(self, parent: tk.Misc, symbol: str, *, size: int = 64) -> None:
        super().__init__(
            parent,
            width=size,
            height=size,
            background=SURFACE,
            borderwidth=0,
            highlightthickness=0,
            takefocus=False,
        )
        self._size = size
        self._photo: tk.PhotoImage | None = None
        self._show(symbol)

    def _show(self, symbol: str) -> None:
        path = prediction_pulse_mark_path(symbol)
        if path is not None:
            try:
                source = tk.PhotoImage(master=self, file=str(path))
            except tk.TclError:
                source = None
            if source is not None:
                factor = max(
                    1,
                    math.ceil(max(source.width(), source.height()) / self._size),
                )
                self._photo = source.subsample(factor, factor)
                self.create_image(
                    self._size / 2,
                    self._size / 2,
                    image=self._photo,
                )
                return
        color = _prediction_pulse_symbol_color(symbol)
        self.create_oval(
            3,
            3,
            self._size - 3,
            self._size - 3,
            fill=color,
            outline=BORDER,
            width=2,
        )
        self.create_text(
            self._size / 2,
            self._size / 2,
            text=_prediction_pulse_monogram(symbol),
            fill=TEXT,
            font=("Segoe UI", max(10, self._size // 4), "bold"),
        )


class _StatusMark(tk.Canvas):
    def __init__(self, parent: tk.Misc, tone: str) -> None:
        super().__init__(
            parent,
            width=30,
            height=30,
            background=SURFACE,
            borderwidth=0,
            highlightthickness=0,
            takefocus=False,
        )
        color = _tone_color(tone)
        self.create_oval(4, 4, 26, 26, outline=color, width=2)
        self.create_oval(12, 12, 18, 18, fill=color, outline=color)


class _ProbabilityBar(tk.Canvas):
    def __init__(
        self,
        parent: tk.Misc,
        probability_up: float | None,
        probability_down: float | None,
    ) -> None:
        super().__init__(
            parent,
            height=20,
            background=SURFACE,
            borderwidth=0,
            highlightthickness=0,
            takefocus=False,
        )
        self._probability_up = probability_up
        self._probability_down = probability_down
        self.bind("<Configure>", self._redraw, add="+")

    def _redraw(self, _event: object | None = None) -> None:
        self.delete("all")
        width = max(1, self.winfo_width())
        height = max(1, self.winfo_height())
        inset = 1
        self.create_rectangle(
            inset,
            inset,
            width - inset,
            height - inset,
            fill=TRACK_COLOR,
            outline=BORDER,
            width=1,
        )
        fractions = probability_segment_fractions(
            self._probability_up,
            self._probability_down,
        )
        if fractions is None:
            self.create_text(
                width / 2,
                height / 2,
                text="Probability unavailable",
                fill=MUTED_TEXT,
                font=("Segoe UI", 8),
            )
            return
        up_fraction, _down_fraction = fractions
        usable_width = max(0.0, width - (2 * inset))
        split = inset + (usable_width * up_fraction)
        self.create_rectangle(
            inset,
            inset,
            split,
            height - inset,
            fill=UP_COLOR,
            outline="",
        )
        self.create_rectangle(
            split,
            inset,
            width - inset,
            height - inset,
            fill=DOWN_COLOR,
            outline="",
        )
        if split - inset >= 50:
            self.create_text(
                (inset + split) / 2,
                height / 2,
                text=format_probability(self._probability_up),
                fill="#f7fbff",
                font=("Segoe UI", 8, "bold"),
            )
        if width - inset - split >= 50:
            self.create_text(
                (split + width - inset) / 2,
                height / 2,
                text=format_probability(self._probability_down),
                fill="#f7fbff",
                font=("Segoe UI", 8, "bold"),
            )


class _EvidenceBar(tk.Canvas):
    def __init__(
        self,
        parent: tk.Misc,
        completed_count: int | None,
        minimum_count: int,
        tone: str,
    ) -> None:
        super().__init__(
            parent,
            height=7,
            background=SURFACE,
            borderwidth=0,
            highlightthickness=0,
            takefocus=False,
        )
        self._fraction = evidence_progress_fraction(completed_count, minimum_count)
        self._tone = tone
        self.bind("<Configure>", self._redraw, add="+")

    def _redraw(self, _event: object | None = None) -> None:
        self.delete("all")
        width = max(1, self.winfo_width())
        height = max(1, self.winfo_height())
        center = height / 2
        self.create_line(2, center, width - 2, center, fill=TRACK_COLOR, width=4)
        if self._fraction is None:
            return
        fill_end = 2 + (max(0, width - 4) * self._fraction)
        self.create_line(
            2,
            center,
            fill_end,
            center,
            fill=_tone_color(self._tone),
            width=4,
        )
        self.create_oval(
            0,
            center - 3,
            6,
            center + 3,
            fill=_tone_color(self._tone),
            outline="",
        )


class _AccuracyGauge(tk.Canvas):
    def __init__(self, parent: tk.Misc, hit_rate: float) -> None:
        super().__init__(
            parent,
            width=82,
            height=52,
            background=PERFORMANCE_SURFACE,
            borderwidth=0,
            highlightthickness=0,
            takefocus=False,
        )
        self._hit_rate = float(hit_rate)
        self.bind("<Configure>", self._redraw, add="+")

    def _redraw(self, _event: object | None = None) -> None:
        self.delete("all")
        width = max(1, self.winfo_width())
        height = max(1, self.winfo_height())
        pad = 8
        box = (pad, 5, width - pad, (height * 1.6) - 3)
        self.create_arc(
            *box,
            start=0,
            extent=180,
            style=tk.ARC,
            outline=TRACK_REMAINDER,
            width=9,
        )
        fraction = max(0.0, min(1.0, self._hit_rate))
        if fraction > 0.0:
            self.create_arc(
                *box,
                start=180,
                extent=-(180 * fraction),
                style=tk.ARC,
                outline=ANALYTIC_BLUE,
                width=9,
            )


class _TimelineMark(tk.Canvas):
    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(
            parent,
            height=30,
            background=SURFACE,
            borderwidth=0,
            highlightthickness=0,
            takefocus=False,
        )
        self.bind("<Configure>", self._redraw, add="+")

    def _redraw(self, _event: object | None = None) -> None:
        self.delete("all")
        width = max(1, self.winfo_width())
        y = 15
        points = (16, width / 2, width - 16)
        self.create_line(points[0], y, points[2], y, fill=MUTED_TEXT, width=2)
        for index, x in enumerate(points):
            color = ANALYTIC_BLUE if index == 1 else MUTED_TEXT
            radius = 6 if index == 1 else 4
            self.create_oval(
                x - radius,
                y - radius,
                x + radius,
                y + radius,
                fill=color,
                outline=("#dceeff" if index == 1 else color),
                width=1,
            )


@dataclass
class _SymbolSectionWidgets:
    symbol: str
    section: ttk.Frame
    header: tk.Button
    body: ttk.Frame
    cards: tuple[ttk.Frame, ...]
    weekly_card: ttk.Frame
    collapsed_summary: str


@dataclass
class _WeeklyLayoutWidgets:
    container: ttk.Frame
    summary: ttk.Frame
    timeline: ttk.Frame
    performance: ttk.Frame


@dataclass
class _WeeklyDetailsWidgets:
    symbol: str
    button: tk.Button
    body: ttk.Frame
    cards: tuple[ttk.Frame, ...]


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
        self.header_frame: ttk.Frame | None = None
        self.header_title_area: ttk.Frame | None = None
        self.header_actions: ttk.Frame | None = None
        self.subtitle_label: ttk.Label | None = None
        self.summary_frame: ttk.Frame | None = None
        self.message_frame: ttk.Frame | None = None
        self.canvas: tk.Canvas | None = None
        self.canvas_window: int | None = None
        self.content_frame: ttk.Frame | None = None
        self.source_label: ttk.Label | None = None
        self._prediction_pulse_frame: ttk.Frame | None = None
        self._prediction_pulse_symbols: tuple[
            tuple[str, tuple[tuple[str, float | None], ...]], ...
        ] = ()
        self._summary_cards: list[ttk.Frame] = []
        self._summary_labels: list[tuple[ttk.Label, ttk.Label]] = []
        self._symbol_sections: list[_SymbolSectionWidgets] = []
        self._symbol_expanded: dict[str, bool] = {}
        self._weekly_layouts: list[_WeeklyLayoutWidgets] = []
        self._weekly_details: list[_WeeklyDetailsWidgets] = []
        self._weekly_details_expanded: dict[str, bool] = {}
        self._layout_columns: int | None = None
        self._layout_signature: tuple[int, int, int, int, int] | None = None
        self._width = 1180
        self._hourly_refresh_job: str | None = None

        self._apply_styles()
        self._build(parent)
        self.root.after_idle(self.refresh)
        self._schedule_hourly_refresh()

    def _apply_styles(self) -> None:
        style = ttk.Style(self.root)
        style.configure("Forecast.TFrame", background=BACKGROUND)
        style.configure("ForecastToolbar.TFrame", background=BACKGROUND)
        style.configure(
            "ForecastSurface.TFrame",
            background=SURFACE,
            bordercolor=BORDER,
            borderwidth=1,
            relief=tk.SOLID,
        )
        style.configure(
            "ForecastSymbolSection.TFrame",
            background=SURFACE_ALT,
            bordercolor=BORDER,
            borderwidth=1,
            relief=tk.SOLID,
        )
        style.configure("ForecastSectionBody.TFrame", background=SURFACE_ALT)
        style.configure("ForecastPulse.TFrame", background=BACKGROUND)
        style.configure(
            "ForecastCard.TFrame",
            background=SURFACE,
            bordercolor=BORDER,
            borderwidth=1,
            relief=tk.SOLID,
        )
        style.configure("ForecastCardBody.TFrame", background=SURFACE)
        style.configure("ForecastProbability.TFrame", background=SURFACE)
        style.configure("ForecastWindow.TFrame", background=SURFACE)
        style.configure("ForecastEvidence.TFrame", background=SURFACE)
        style.configure("ForecastPerformance.TFrame", background=SURFACE)
        style.configure(
            "ForecastMetric.TFrame",
            background=PERFORMANCE_SURFACE,
            bordercolor=BORDER,
            borderwidth=1,
            relief=tk.SOLID,
        )
        style.configure("ForecastMetricBody.TFrame", background=PERFORMANCE_SURFACE)
        style.configure("ForecastHealthBorder.TFrame", background=BORDER)
        style.configure("ForecastHealthCell.TFrame", background=SURFACE)
        style.configure("ForecastDivider.TFrame", background=BORDER)
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
            background=SURFACE_ALT,
            foreground=TEXT,
            font=("Segoe UI", 13, "bold"),
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
            "ForecastPerformanceHeading.TLabel",
            background=SURFACE,
            foreground=ANALYTIC_BLUE,
            font=("Segoe UI", 9, "bold"),
        )
        style.configure(
            "ForecastPerformanceCumulative.TLabel",
            background=PERFORMANCE_SURFACE,
            foreground=TEXT,
            font=("Segoe UI", 11, "bold"),
        )
        style.configure(
            "ForecastPerformanceRolling.TLabel",
            background=PERFORMANCE_SURFACE,
            foreground=ANALYTIC_BLUE,
            font=("Segoe UI", 11, "bold"),
        )
        style.configure(
            "ForecastMetricHeading.TLabel",
            background=PERFORMANCE_SURFACE,
            foreground="#a9c8ed",
            font=("Segoe UI", 9),
        )
        style.configure(
            "ForecastMetricValue.TLabel",
            background=PERFORMANCE_SURFACE,
            foreground=TEXT,
            font=("Segoe UI", 13, "bold"),
        )
        style.configure(
            "ForecastMetricText.TLabel",
            background=PERFORMANCE_SURFACE,
            foreground=TEXT,
            font=("Segoe UI", 8),
        )
        style.configure(
            "ForecastMetricMuted.TLabel",
            background=PERFORMANCE_SURFACE,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 9),
        )
        style.configure(
            "ForecastMuted.TLabel",
            background=SURFACE,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 9),
        )
        style.configure(
            "ForecastHealthHeading.TLabel",
            background=SURFACE,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 9),
        )
        style.configure(
            "ForecastHealthValue.TLabel",
            background=SURFACE,
            foreground=TEXT,
            font=("Segoe UI", 10, "bold"),
        )
        style.configure(
            "ForecastHealthDetail.TLabel",
            background=SURFACE,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 8),
        )
        style.configure(
            "ForecastProbabilityLabel.TLabel",
            background=SURFACE,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 9),
        )
        style.configure(
            "ForecastUpValue.TLabel",
            background=SURFACE,
            foreground=UP_COLOR,
            font=("Segoe UI", 16, "bold"),
        )
        style.configure(
            "ForecastDownValue.TLabel",
            background=SURFACE,
            foreground=DOWN_COLOR,
            font=("Segoe UI", 16, "bold"),
        )
        style.configure(
            "ForecastUnavailableValue.TLabel",
            background=SURFACE,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 12, "bold"),
        )
        style.configure(
            "ForecastCompactHeading.TLabel",
            background=SURFACE,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 8, "bold"),
        )
        style.configure(
            "ForecastWeeklyTitle.TLabel",
            background=SURFACE,
            foreground=TEXT,
            font=("Segoe UI", 13, "bold"),
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
        for tone, foreground, background in (
            ("Success", "#aaf0b8", SUCCESS_BADGE),
            ("Warning", "#ffd27a", WARNING_BADGE),
            ("Danger", "#ffb2aa", DANGER_BADGE),
            ("Neutral", "#c2cbd7", NEUTRAL_BADGE),
        ):
            style.configure(
                f"ForecastBadge{tone}.TLabel",
                background=background,
                foreground=foreground,
                font=("Segoe UI", 8, "bold"),
                padding=(8, 3),
            )
        for tone, foreground, background in (
            ("Success", "#b6f3c1", SUCCESS_BADGE),
            ("Danger", "#ffb8b0", DANGER_BADGE),
            ("Neutral", "#d1d7df", NEUTRAL_BADGE),
        ):
            style.configure(
                f"ForecastLift{tone}.TLabel",
                background=background,
                foreground=foreground,
                font=("Segoe UI", 8, "bold"),
                padding=(5, 2),
            )
        style.configure(
            "ForecastBanner.TLabel",
            background=SURFACE_ALT,
            foreground=TEXT,
            font=("Segoe UI", 9),
            padding=(10, 8),
        )
        style.configure(
            "ForecastPrimary.TButton",
            background=SURFACE_ALT,
            foreground=TEXT,
            bordercolor="#53667f",
            padding=(14, 7),
        )
        style.map(
            "ForecastPrimary.TButton",
            background=[("active", ACCENT), ("disabled", SURFACE)],
            foreground=[("disabled", MUTED_TEXT)],
        )
        style.configure(
            "ForecastSecondary.TButton",
            background=BACKGROUND,
            foreground=MUTED_TEXT,
            bordercolor=BORDER,
            padding=(10, 7),
        )
        style.map(
            "ForecastSecondary.TButton",
            background=[("active", SURFACE_ALT), ("disabled", BACKGROUND)],
            foreground=[("active", TEXT), ("disabled", MUTED_TEXT)],
        )

    def _build(self, parent: ttk.Frame) -> None:
        outer = ttk.Frame(
            parent,
            padding=(18, 14, 18, 12),
            style="Forecast.TFrame",
        )
        outer.pack(fill=tk.BOTH, expand=True)
        outer.bind("<Configure>", self._on_resize, add="+")

        self.header_frame = ttk.Frame(outer, style="Forecast.TFrame")
        self.header_frame.pack(fill=tk.X)
        self.header_frame.grid_columnconfigure(0, weight=1)
        self.header_title_area = ttk.Frame(
            self.header_frame,
            style="Forecast.TFrame",
        )
        self.header_title_area.grid(row=0, column=0, sticky=tk.EW)
        ttk.Label(
            self.header_title_area,
            text="Rolling Forecasts",
            style="ForecastTitle.TLabel",
        ).pack(anchor=tk.W)
        self.subtitle_label = ttk.Label(
            self.header_title_area,
            text=FORECAST_SUBTITLE,
            style="ForecastSubtitle.TLabel",
            justify=tk.LEFT,
        )
        self.subtitle_label.pack(anchor=tk.W, pady=(2, 0))

        self.header_actions = ttk.Frame(
            self.header_frame,
            style="Forecast.TFrame",
        )
        self.header_actions.grid(row=0, column=1, sticky=tk.NE)
        self.debug_button = ttk.Button(
            self.header_actions,
            text="Debug Details",
            command=self._show_debug,
            state=tk.DISABLED,
            style="ForecastSecondary.TButton",
        )
        self.debug_button.pack(side=tk.LEFT, padx=(0, 8))
        self.refresh_button = ttk.Button(
            self.header_actions,
            text="Refresh",
            command=self.refresh,
            style="ForecastPrimary.TButton",
        )
        self.refresh_button.pack(side=tk.LEFT)

        self.summary_frame = ttk.Frame(outer, style="Forecast.TFrame")
        self.summary_frame.pack(fill=tk.X, pady=(12, 8))

        self.message_frame = ttk.Frame(outer, style="Forecast.TFrame")
        self.message_frame.pack(fill=tk.X)

        self.source_label = ttk.Label(
            outer,
            text=f"Current-Output Source: {self.predictions_path}",
            style="ForecastSubtitle.TLabel",
            justify=tk.LEFT,
        )
        self.source_label.pack(side=tk.BOTTOM, fill=tk.X, pady=(7, 0))

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
        delay = min(
            HOURLY_AUTO_REFRESH_MS,
            milliseconds_until_next_hour(),
        )
        self._hourly_refresh_job = self.root.after(
            delay,
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
        if self.source_label is not None:
            self.source_label.configure(
                text=f"Current-Output Source: {self.predictions_path}"
            )
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
        if self.source_label is not None:
            self.source_label.configure(
                text=f"Current-Output Source: {error.path}"
            )
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
        if self.source_label is not None:
            self.source_label.configure(
                text=f"Current-Output Source: {view.source_path}"
            )
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
            for symbol_index, symbol in enumerate(view.symbols):
                symbol_name = str(symbol.symbol).strip().upper()
                section = ttk.Frame(
                    self.content_frame,
                    style="ForecastSymbolSection.TFrame",
                )
                section.pack(fill=tk.X, pady=(0, 10))
                header_row = ttk.Frame(
                    section,
                    style="ForecastSectionBody.TFrame",
                )
                header_row.pack(fill=tk.X)
                body = ttk.Frame(
                    section,
                    padding=(10, 0, 10, 10),
                    style="ForecastSectionBody.TFrame",
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
                    header_row,
                    command=lambda name=symbol_name: self._toggle_symbol(name),
                    background=SURFACE_ALT,
                    foreground=TEXT,
                    activebackground="#20334b",
                    activeforeground=TEXT,
                    font=("Segoe UI", 12, "bold"),
                    anchor=tk.W,
                    justify=tk.LEFT,
                    relief=tk.FLAT,
                    borderwidth=0,
                    highlightthickness=1,
                    highlightbackground=BORDER,
                    highlightcolor=ACCENT,
                    takefocus=True,
                    cursor="hand2",
                    padx=12,
                    pady=9,
                    wraplength=max(180, self._width - 300),
                )
                header.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
                header.bind(
                    "<Return>",
                    lambda _event, name=symbol_name: self._toggle_symbol_from_key(name),
                )
                header.bind(
                    "<KP_Enter>",
                    lambda _event, name=symbol_name: self._toggle_symbol_from_key(name),
                )
                if symbol_index == 0:
                    controls = ttk.Frame(
                        header_row,
                        padding=(0, 5, 6, 5),
                        style="ForecastSectionBody.TFrame",
                    )
                    controls.pack(side=tk.RIGHT)
                    ttk.Button(
                        controls,
                        text="Collapse All",
                        command=lambda: self._set_all_symbols_expanded(False),
                        width=12,
                        style="ForecastSecondary.TButton",
                    ).pack(side=tk.LEFT, padx=(0, 6))
                    ttk.Button(
                        controls,
                        text="Expand All",
                        command=lambda: self._set_all_symbols_expanded(True),
                        width=12,
                        style="ForecastSecondary.TButton",
                    ).pack(side=tk.LEFT)
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
        self._render_prediction_pulse(view)
        self._apply_responsive_layout(force=True)
        self._update_scroll_region()

    def _render_prediction_pulse(self, view: ForecastDashboardView) -> None:
        if self.content_frame is None or not view.symbols:
            return
        self._prediction_pulse_symbols = tuple(
            (
                str(symbol.symbol).strip().upper(),
                prediction_pulse_probabilities(symbol.routes),
            )
            for symbol in view.symbols
        )
        self._prediction_pulse_frame = ttk.Frame(
            self.content_frame,
            style="ForecastPulse.TFrame",
        )
        self._prediction_pulse_frame.pack(fill=tk.X, pady=(0, 4))

    def _layout_prediction_pulse(self, max_columns: int) -> None:
        frame = self._prediction_pulse_frame
        if frame is None or not self._prediction_pulse_symbols:
            return
        for child in frame.winfo_children():
            child.destroy()

        group_size = max(1, int(max_columns))
        mark_size = 80 if group_size >= 6 else 64
        label_width = (
            210 if group_size >= 6 else (136 if group_size >= 3 else 112)
        )
        title = "Prediction Pulse" if group_size >= 6 else "Prediction\nPulse"
        symbols = self._prediction_pulse_symbols
        for group_index, start in enumerate(range(0, len(symbols), group_size)):
            group = symbols[start : start + group_size]
            matrix = tk.Frame(frame, background=BACKGROUND, borderwidth=0)
            matrix.pack(
                fill=tk.X,
                pady=(0, 8 if start + group_size < len(symbols) else 0),
            )
            matrix.grid_columnconfigure(0, weight=0, minsize=label_width)
            for column in range(1, group_size + 1):
                matrix.grid_columnconfigure(
                    column,
                    weight=1,
                    uniform=f"prediction-pulse-{group_index}",
                    minsize=70,
                )

            corner = tk.Frame(
                matrix,
                background=SURFACE,
                highlightbackground=BORDER,
                highlightcolor=BORDER,
                highlightthickness=1,
            )
            corner.grid(row=0, column=0, sticky=tk.NSEW)
            tk.Label(
                corner,
                text=title,
                background=SURFACE,
                foreground=TEXT,
                font=("Segoe UI", 15, "bold"),
                anchor=tk.NW,
                justify=tk.LEFT,
                wraplength=label_width - 20,
            ).pack(fill=tk.BOTH, expand=True, padx=12, pady=10)

            probabilities_by_symbol: dict[
                str,
                dict[str, float | None],
            ] = {}
            for column, (symbol, probabilities) in enumerate(group, start=1):
                probabilities_by_symbol[symbol] = dict(probabilities)
                header = tk.Frame(
                    matrix,
                    background=SURFACE,
                    highlightbackground=BORDER,
                    highlightcolor=BORDER,
                    highlightthickness=1,
                )
                header.grid(row=0, column=column, sticky=tk.NSEW)
                _PredictionPulseMark(
                    header,
                    symbol,
                    size=mark_size,
                ).pack(pady=(8, 1))
                tk.Label(
                    header,
                    text=symbol,
                    background=SURFACE,
                    foreground=TEXT,
                    font=("Segoe UI", 12, "bold"),
                ).pack(pady=(0, 7))

            for row, horizon in enumerate(STANDARD_HORIZON_ORDER, start=1):
                row_heading = tk.Label(
                    matrix,
                    text=horizon.upper(),
                    background=BACKGROUND,
                    foreground=TEXT,
                    font=("Segoe UI", 13, "bold"),
                    anchor=tk.CENTER,
                    highlightbackground=BORDER,
                    highlightcolor=BORDER,
                    highlightthickness=1,
                )
                row_heading.grid(row=row, column=0, sticky=tk.NSEW)
                for column, (symbol, _probabilities) in enumerate(
                    group,
                    start=1,
                ):
                    probability = probabilities_by_symbol[symbol].get(horizon)
                    tone = prediction_pulse_tone(probability)
                    background, outline, foreground = (
                        _prediction_pulse_palette(tone)
                    )
                    cell = tk.Frame(
                        matrix,
                        background=background,
                        highlightbackground=outline,
                        highlightcolor=outline,
                        highlightthickness=2,
                    )
                    cell.grid(row=row, column=column, sticky=tk.NSEW)
                    tk.Label(
                        cell,
                        text=prediction_pulse_probability_text(probability),
                        background=background,
                        foreground=foreground,
                        font=("Segoe UI", 16, "bold"),
                        anchor=tk.CENTER,
                    ).pack(fill=tk.BOTH, expand=True, padx=8, pady=10)

        legend = tk.Frame(frame, background=BACKGROUND, borderwidth=0)
        legend.pack(pady=(10, 4))
        for label, tone in (
            ("UP > 50%", "up"),
            ("NEUTRAL = 50%", "neutral"),
            ("DOWN < 50%", "down"),
        ):
            item = tk.Frame(legend, background=BACKGROUND, borderwidth=0)
            item.pack(side=tk.LEFT, padx=12)
            background, outline, _foreground = _prediction_pulse_palette(tone)
            swatch = tk.Canvas(
                item,
                width=28,
                height=20,
                background=BACKGROUND,
                borderwidth=0,
                highlightthickness=0,
                takefocus=False,
            )
            swatch.create_rectangle(
                2,
                2,
                26,
                18,
                fill=background,
                outline=outline,
                width=2,
            )
            swatch.pack(side=tk.LEFT, padx=(0, 6))
            tk.Label(
                item,
                text=label,
                background=BACKGROUND,
                foreground=TEXT,
                font=("Segoe UI", 10),
            ).pack(side=tk.LEFT)

    def _render_summary(self, view: ForecastDashboardView) -> None:
        if self.summary_frame is None:
            return
        self.summary_frame.configure(style="ForecastHealthBorder.TFrame")
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
                "On" if view.automated_action_allowed else "Off",
                view.automation_label,
                view.automation_tone,
            ),
        )
        for heading, value, detail, tone in cards:
            card = ttk.Frame(
                self.summary_frame,
                padding=(12, 9),
                style="ForecastHealthCell.TFrame",
            )
            _StatusMark(card, tone).pack(side=tk.LEFT, padx=(0, 10))
            copy = ttk.Frame(card, style="ForecastHealthCell.TFrame")
            copy.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            ttk.Label(
                copy,
                text=heading,
                style="ForecastHealthHeading.TLabel",
            ).pack(anchor=tk.W)
            value_label = ttk.Label(
                copy,
                text=value,
                style=_tone_style(tone, value=True),
                wraplength=250,
                justify=tk.LEFT,
            )
            value_label.pack(anchor=tk.W, pady=(2, 0))
            detail_label = ttk.Label(
                copy,
                text=detail,
                style="ForecastHealthDetail.TLabel",
                wraplength=265,
                justify=tk.LEFT,
            )
            detail_label.pack(anchor=tk.W, pady=(2, 0))
            self._summary_cards.append(card)
            self._summary_labels.append((value_label, detail_label))

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
        header = ttk.Frame(card, style="ForecastCardBody.TFrame")
        header.pack(fill=tk.X)
        header.grid_columnconfigure(0, weight=1)
        ttk.Label(
            header,
            text=f"{route.horizon_label} Forecast",
            style="ForecastCardTitle.TLabel",
        ).grid(row=0, column=0, sticky=tk.W, padx=(0, 8))

        _actionability, live_evidence = (
            route_accessible_status_labels(route)
        )
        ttk.Label(
            header,
            text=route.actionability_label,
            style=_badge_style(route.actionability_tone),
            wraplength=260,
            justify=tk.RIGHT,
        ).grid(row=0, column=1, sticky=tk.E)

        if route.display_probability_up is not None and route.display_probability_down is not None:
            probability_up = route.display_probability_up
            probability_down = route.display_probability_down
        else:
            probability_up = None
            probability_down = None
        self._build_probability_block(
            card,
            probability_up,
            probability_down,
        )
        self._add_divider(card)
        self._build_window_block(
            card,
            route.target_window_start,
            route.target_window_end,
            title="Forecast Window",
        )
        if route.option_plan_status is not None:
            self._add_divider(card)
            self._build_option_gameplan_block(card, route)
        self._add_divider(card)
        self._build_evidence_block(card, route, live_evidence)
        self._build_live_performance_panel(
            card,
            route,
            wraplength=280,
        )
        if route.is_missing:
            ttk.Label(
                card,
                text="No current-output row was published for this horizon.",
                style="ForecastMuted.TLabel",
                wraplength=330,
                justify=tk.LEFT,
            ).pack(anchor=tk.W, pady=(8, 0))
        return card

    def _build_option_gameplan_block(
        self,
        parent: ttk.Frame,
        route: ForecastRouteView,
    ) -> ttk.Frame:
        block = ttk.Frame(parent, style="ForecastEvidence.TFrame")
        block.pack(fill=tk.X)
        heading = ttk.Frame(block, style="ForecastEvidence.TFrame")
        heading.pack(fill=tk.X)
        ttk.Label(
            heading,
            text="OPTIONS GAMEPLAN",
            style="ForecastCompactHeading.TLabel",
        ).pack(side=tk.LEFT)
        ttk.Label(
            heading,
            text=route.option_plan_label or "Status Unavailable",
            style=_badge_style(route.option_plan_tone),
            wraplength=280,
            justify=tk.RIGHT,
        ).pack(side=tk.RIGHT)
        details: list[str] = []
        if route.option_strategy_name:
            details.append(route.option_strategy_name)
        else:
            details.append("No exact frozen candidate")
        if route.option_profit_probability is not None:
            details.append(
                "Modeled profit probability "
                + format_probability(route.option_profit_probability)
            )
        if route.option_pricing_source:
            details.append(route.option_pricing_source.replace("_", " ").title())
        ttk.Label(
            block,
            text=" · ".join(details),
            style="ForecastBody.TLabel",
            wraplength=520,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(5, 0))
        if route.option_reason:
            ttk.Label(
                block,
                text=route.option_reason,
                style="ForecastMuted.TLabel",
                wraplength=520,
                justify=tk.LEFT,
            ).pack(anchor=tk.W, pady=(3, 0))
        return block

    def _build_probability_block(
        self,
        parent: ttk.Frame,
        probability_up: float | None,
        probability_down: float | None,
    ) -> ttk.Frame:
        block = ttk.Frame(parent, style="ForecastProbability.TFrame")
        block.pack(fill=tk.X, pady=(10, 0))
        block.grid_columnconfigure(0, weight=1)
        block.grid_columnconfigure(1, weight=1)
        ttk.Label(
            block,
            text="Probability Up",
            style="ForecastProbabilityLabel.TLabel",
        ).grid(row=0, column=0, sticky=tk.W)
        ttk.Label(
            block,
            text="Probability Down",
            style="ForecastProbabilityLabel.TLabel",
        ).grid(row=0, column=1, sticky=tk.E)
        ttk.Label(
            block,
            text=f"UP {format_probability(probability_up)}",
            style=(
                "ForecastUpValue.TLabel"
                if probability_up is not None
                else "ForecastUnavailableValue.TLabel"
            ),
        ).grid(row=1, column=0, sticky=tk.W, pady=(2, 0))
        ttk.Label(
            block,
            text=f"DOWN {format_probability(probability_down)}",
            style=(
                "ForecastDownValue.TLabel"
                if probability_down is not None
                else "ForecastUnavailableValue.TLabel"
            ),
        ).grid(row=1, column=1, sticky=tk.E, pady=(2, 0))
        bar = _ProbabilityBar(block, probability_up, probability_down)
        bar.grid(row=2, column=0, columnspan=2, sticky=tk.EW, pady=(6, 0))
        return block

    def _build_window_block(
        self,
        parent: ttk.Frame,
        start: datetime | None,
        end: datetime | None,
        *,
        title: str,
        open_close: bool = False,
    ) -> ttk.Frame:
        block = ttk.Frame(parent, style="ForecastWindow.TFrame")
        block.pack(fill=tk.X)
        ttk.Label(
            block,
            text=title,
            style="ForecastCompactHeading.TLabel",
        ).pack(anchor=tk.W)
        start_word = "Open" if open_close else "Start"
        end_word = "Close" if open_close else "End"
        text = (
            f"UTC {start_word}: {format_timestamp_utc(start)}  —  "
            f"{end_word}: {format_timestamp_utc(end)}\n"
            f"Local {start_word}: "
            + _compact_local_timestamp(
                start,
                local_timezone=self.local_timezone,
            )
            + f"  —  {end_word}: "
            + _compact_local_timestamp(
                end,
                local_timezone=self.local_timezone,
            )
        )
        ttk.Label(
            block,
            text=text,
            style="ForecastBody.TLabel",
            wraplength=525,
            justify=tk.LEFT,
        ).pack(fill=tk.X, pady=(3, 0))
        return block

    def _build_evidence_block(
        self,
        parent: ttk.Frame,
        route: ForecastRouteView,
        live_evidence: str,
        *,
        outcome_prefix: str | None = None,
        tone: str | None = None,
    ) -> ttk.Frame:
        block = ttk.Frame(parent, style="ForecastEvidence.TFrame")
        block.pack(fill=tk.X)
        block.grid_columnconfigure(1, weight=1)
        ttk.Label(
            block,
            text="Evidence",
            style="ForecastCompactHeading.TLabel",
        ).grid(row=0, column=0, sticky=tk.W, padx=(0, 12))
        evidence_text = live_evidence
        if outcome_prefix:
            evidence_text = f"{outcome_prefix} · {live_evidence}"
        display_tone = tone or _live_tone(route)
        ttk.Label(
            block,
            text=evidence_text,
            style=_tone_style(display_tone),
            wraplength=430,
            justify=tk.LEFT,
        ).grid(row=0, column=1, sticky=tk.W)
        bar = _EvidenceBar(
            block,
            route.completed_decision_count,
            route.minimum_live_decision_count,
            display_tone,
        )
        bar.grid(row=1, column=0, columnspan=2, sticky=tk.EW, pady=(5, 0))
        return block

    def _add_divider(self, parent: ttk.Frame) -> None:
        divider = ttk.Frame(
            parent,
            height=1,
            style="ForecastDivider.TFrame",
        )
        divider.pack(fill=tk.X, pady=(8, 7))
        divider.pack_propagate(False)

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
        header.grid_columnconfigure(0, weight=1)
        ttk.Label(
            header,
            text="Remaining-Week Outlook",
            style="ForecastWeeklyTitle.TLabel",
        ).grid(row=0, column=0, sticky=tk.W, padx=(0, 12))

        if outlook is None:
            ttk.Label(
                header,
                text="No Current Snapshot",
                style="ForecastBadgeNeutral.TLabel",
            ).grid(row=0, column=1, sticky=tk.E)
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

        snapshot_label = "Current Remaining-Week Snapshot"
        snapshot_tone = "success"
        if outlook.aggregate.live_evidence_status == "FROZEN_OVERNIGHT_GAMEPLAN":
            snapshot_label = outlook.aggregate.actionability_label
            snapshot_tone = outlook.aggregate.actionability_tone
        ttk.Label(
            header,
            text=snapshot_label,
            style=_badge_style(snapshot_tone),
        ).grid(row=0, column=1, sticky=tk.E)
        ttk.Label(
            header,
            text=(
                "Snapshot Issued: "
                f"{format_timestamp_utc(outlook.issued_at)}  ·  Local: "
                + _compact_local_timestamp(
                    outlook.issued_at,
                    local_timezone=self.local_timezone,
                )
            ),
            style="ForecastMuted.TLabel",
            wraplength=980,
            justify=tk.LEFT,
        ).grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=(5, 0))

        aggregate = outlook.aggregate
        regions = ttk.Frame(card, style="ForecastCardBody.TFrame")
        regions.pack(fill=tk.X, pady=(10, 0))

        summary = ttk.Frame(
            regions,
            padding=(10, 8),
            style="ForecastCard.TFrame",
        )
        ttk.Label(
            summary,
            text="Aggregate (Remaining Week)",
            style="ForecastCardTitle.TLabel",
        ).pack(anchor=tk.W)
        self._build_probability_block(
            summary,
            aggregate.display_probability_up,
            aggregate.display_probability_down,
        )
        if aggregate.option_plan_status is not None:
            self._add_divider(summary)
            self._build_option_gameplan_block(summary, aggregate)
        self._add_divider(summary)
        _aggregate_actionability, aggregate_live_evidence = (
            route_accessible_status_labels(aggregate)
        )
        self._build_evidence_block(
            summary,
            aggregate,
            aggregate_live_evidence,
            outcome_prefix=(
                "Outcome/evidence: "
                f"{route_outcome_evidence_label(aggregate)}"
            ),
            tone=_outcome_tone(aggregate),
        )

        timeline = ttk.Frame(
            regions,
            padding=(10, 8),
            style="ForecastCard.TFrame",
        )
        ttk.Label(
            timeline,
            text="Remaining Week Timeline",
            style="ForecastCompactHeading.TLabel",
        ).pack(anchor=tk.CENTER)
        _TimelineMark(timeline).pack(fill=tk.X, pady=(5, 0))
        milestones = ttk.Frame(timeline, style="ForecastCardBody.TFrame")
        milestones.pack(fill=tk.X)
        for column in range(3):
            milestones.grid_columnconfigure(column, weight=1, uniform="timeline")
        for column, (heading, value, anchor) in enumerate(
            (
                ("Start", aggregate.target_window_start, tk.W),
                ("Snapshot", outlook.issued_at, ""),
                ("End", aggregate.target_window_end, tk.E),
            )
        ):
            ttk.Label(
                milestones,
                text=f"{heading}\n{_stacked_utc_timestamp(value)}",
                style="ForecastBody.TLabel",
                justify=tk.CENTER,
            ).grid(row=0, column=column, sticky=anchor)
        ttk.Label(
            timeline,
            text=(
                "Local Window: "
                + _compact_local_timestamp(
                    aggregate.target_window_start,
                    local_timezone=self.local_timezone,
                )
                + "  —  "
                + _compact_local_timestamp(
                    aggregate.target_window_end,
                    local_timezone=self.local_timezone,
                )
            ),
            style="ForecastMuted.TLabel",
            wraplength=470,
            justify=tk.CENTER,
        ).pack(fill=tk.X, pady=(8, 0))

        performance = ttk.Frame(
            regions,
            padding=(10, 8),
            style="ForecastCard.TFrame",
        )
        self._build_live_performance_panel(
            performance,
            aggregate,
            wraplength=300,
        )
        self._weekly_layouts.append(
            _WeeklyLayoutWidgets(
                container=regions,
                summary=summary,
                timeline=timeline,
                performance=performance,
            )
        )

        self._add_divider(card)
        symbol = str(aggregate.symbol).strip().upper()
        expanded = self._weekly_details_expanded.setdefault(symbol, False)
        details_button = tk.Button(
            card,
            command=lambda name=symbol: self._toggle_weekly_details(name),
            text=weekly_session_details_header_text(
                len(outlook.sessions),
                expanded=expanded,
            ),
            background=SURFACE,
            foreground=TEXT,
            activebackground=SURFACE_ALT,
            activeforeground=TEXT,
            font=("Segoe UI", 10, "bold"),
            anchor=tk.W,
            justify=tk.LEFT,
            relief=tk.FLAT,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            takefocus=True,
            cursor="hand2",
            padx=8,
            pady=7,
        )
        details_button.pack(fill=tk.X)
        details_button.bind(
            "<Return>",
            lambda _event, name=symbol: self._toggle_weekly_details_from_key(name),
        )
        details_button.bind(
            "<KP_Enter>",
            lambda _event, name=symbol: self._toggle_weekly_details_from_key(name),
        )
        details_body = ttk.Frame(
            card,
            style="ForecastCardBody.TFrame",
        )
        session_cards = tuple(
            self._build_weekly_session_card(details_body, route)
            for route in outlook.sessions
        )
        details = _WeeklyDetailsWidgets(
            symbol=symbol,
            button=details_button,
            body=details_body,
            cards=session_cards,
        )
        self._weekly_details.append(details)
        if expanded:
            details_body.pack(fill=tk.X, pady=(8, 0))
        return card

    def _build_weekly_session_card(
        self,
        parent: ttk.Frame,
        route: ForecastRouteView,
    ) -> ttk.Frame:
        card = ttk.Frame(
            parent,
            padding=10,
            style="ForecastCard.TFrame",
        )
        header = ttk.Frame(card, style="ForecastCardBody.TFrame")
        header.pack(fill=tk.X)
        header.grid_columnconfigure(0, weight=1)
        ttk.Label(
            header,
            text=route.horizon_label,
            style="ForecastCardTitle.TLabel",
        ).grid(row=0, column=0, sticky=tk.W)
        ttk.Label(
            header,
            text=format_session_date(
                route.target_window_start,
                local_timezone=self.local_timezone,
            ),
            style="ForecastMuted.TLabel",
        ).grid(row=0, column=1, sticky=tk.E, padx=(8, 0))

        self._build_probability_block(
            card,
            route.display_probability_up,
            route.display_probability_down,
        )
        self._add_divider(card)
        self._build_window_block(
            card,
            route.target_window_start,
            route.target_window_end,
            title="Session Window",
            open_close=True,
        )
        if route.option_plan_status is not None:
            self._add_divider(card)
            self._build_option_gameplan_block(card, route)
        self._add_divider(card)
        _actionability, live_evidence = route_accessible_status_labels(route)
        self._build_evidence_block(
            card,
            route,
            live_evidence,
            outcome_prefix=(
                "Outcome/evidence: "
                f"{route_outcome_evidence_label(route)}"
            ),
            tone=_outcome_tone(route),
        )
        self._build_live_performance_panel(
            card,
            route,
            wraplength=280,
        )
        return card

    def _toggle_weekly_details(self, symbol: str) -> None:
        name = str(symbol or "").strip().upper()
        self._set_weekly_details_expanded(
            name,
            not self._weekly_details_expanded.get(name, False),
        )

    def _toggle_weekly_details_from_key(self, symbol: str) -> str:
        self._toggle_weekly_details(symbol)
        return "break"

    def _set_weekly_details_expanded(
        self,
        symbol: str,
        expanded: bool,
        *,
        refresh_layout: bool = True,
    ) -> None:
        name = str(symbol or "").strip().upper()
        if not name:
            return
        self._weekly_details_expanded[name] = bool(expanded)
        details = next(
            (item for item in self._weekly_details if item.symbol == name),
            None,
        )
        if details is None:
            return
        details.button.configure(
            text=weekly_session_details_header_text(
                len(details.cards),
                expanded=bool(expanded),
            )
        )
        if expanded:
            details.body.pack(fill=tk.X, pady=(8, 0))
        else:
            details.body.pack_forget()
        if refresh_layout:
            self._apply_responsive_layout(force=True)
        else:
            self._refresh_scroll_region()

    def _build_live_performance_panel(
        self,
        parent: ttk.Frame,
        route: ForecastRouteView,
        *,
        wraplength: int,
    ) -> ttk.Frame:
        panel = ttk.Frame(
            parent,
            style="ForecastPerformance.TFrame",
        )
        panel.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(
            panel,
            text="LIVE PERFORMANCE",
            style="ForecastPerformanceHeading.TLabel",
        ).pack(anchor=tk.W, pady=(0, 3))

        metrics = ttk.Frame(panel, style="ForecastPerformance.TFrame")
        metrics.pack(fill=tk.X)
        metrics.grid_columnconfigure(0, weight=1, uniform="live-performance")
        metrics.grid_columnconfigure(1, weight=1, uniform="live-performance")

        performance = route.live_performance
        state = (
            "Awaiting First Scored Forecast"
            if route.completed_decision_count == 0
            else "Unavailable"
        )
        if performance is None or performance.scored_count <= 0:
            cumulative = self._build_performance_metric(
                metrics,
                heading="CUMULATIVE",
                hit_rate=None,
                correct_count=0,
                scored_count=0,
                down_only_rate=None,
                lift=None,
                unavailable_text=state,
                wraplength=wraplength,
            )
            rolling = self._build_performance_metric(
                metrics,
                heading=f"ROLLING {route.minimum_live_decision_count}",
                hit_rate=None,
                correct_count=0,
                scored_count=0,
                down_only_rate=None,
                lift=None,
                unavailable_text=state,
                wraplength=wraplength,
            )
        else:
            cumulative = self._build_performance_metric(
                metrics,
                heading="CUMULATIVE",
                hit_rate=performance.hit_rate,
                correct_count=performance.correct_count,
                scored_count=performance.scored_count,
                down_only_rate=performance.down_only_rate,
                lift=performance.lift_over_down_only,
                unavailable_text="Unavailable",
                wraplength=wraplength,
            )
            rolling = self._build_performance_metric(
                metrics,
                heading=rolling_performance_heading(
                    performance.rolling_count,
                    performance.rolling_window_size,
                ),
                hit_rate=performance.rolling_hit_rate,
                correct_count=performance.rolling_correct_count,
                scored_count=performance.rolling_count,
                down_only_rate=performance.rolling_down_only_rate,
                lift=performance.rolling_lift_over_down_only,
                unavailable_text="Unavailable",
                wraplength=wraplength,
            )
        cumulative.grid(row=0, column=0, sticky=tk.NSEW, padx=(0, 3))
        rolling.grid(row=0, column=1, sticky=tk.NSEW, padx=(3, 0))
        return panel

    def _build_performance_metric(
        self,
        parent: ttk.Frame,
        *,
        heading: str,
        hit_rate: float | None,
        correct_count: int,
        scored_count: int,
        down_only_rate: float | None,
        lift: float | None,
        unavailable_text: str,
        wraplength: int,
    ) -> ttk.Frame:
        metric = ttk.Frame(
            parent,
            padding=(7, 6),
            style="ForecastMetric.TFrame",
        )
        ttk.Label(
            metric,
            text=heading,
            style="ForecastMetricHeading.TLabel",
        ).pack(anchor=tk.CENTER)

        if (
            scored_count <= 0
            or not _is_finite_number(hit_rate)
            or not _is_finite_number(down_only_rate)
        ):
            ttk.Label(
                metric,
                text=unavailable_text,
                style="ForecastMetricMuted.TLabel",
                wraplength=max(110, wraplength // 2),
                justify=tk.CENTER,
            ).pack(fill=tk.X, pady=(10, 9))
            return metric

        body = ttk.Frame(metric, style="ForecastMetricBody.TFrame")
        body.pack(fill=tk.X, pady=(3, 0))
        _AccuracyGauge(body, float(hit_rate)).pack(
            side=tk.LEFT,
            padx=(0, 7),
            anchor=tk.N,
        )
        copy = ttk.Frame(body, style="ForecastMetricBody.TFrame")
        copy.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ttk.Label(
            copy,
            text=_percentage_text(hit_rate),
            style="ForecastMetricValue.TLabel",
        ).pack(anchor=tk.W)
        ttk.Label(
            copy,
            text=f"({int(correct_count)}/{int(scored_count)})",
            style="ForecastMetricText.TLabel",
        ).pack(anchor=tk.W)
        footer = ttk.Frame(metric, style="ForecastMetricBody.TFrame")
        footer.pack(fill=tk.X, pady=(3, 0))
        ttk.Label(
            footer,
            text=f"Down-only {_percentage_text(down_only_rate)}",
            style="ForecastMetricText.TLabel",
        ).pack(side=tk.LEFT, anchor=tk.W)
        ttk.Label(
            footer,
            text=f"Lift {_lift_text(lift)}",
            style=_lift_style(live_performance_lift_tone(lift)),
        ).pack(side=tk.RIGHT, anchor=tk.E)
        return metric

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
        self._summary_labels.clear()
        self._symbol_sections.clear()
        self._weekly_layouts.clear()
        self._weekly_details.clear()
        self._prediction_pulse_frame = None
        self._prediction_pulse_symbols = ()
        self._layout_columns = None
        self._layout_signature = None
        if self.summary_frame is not None:
            self.summary_frame.configure(style="Forecast.TFrame")

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
        health_columns = 4 if self._width >= 1080 else (2 if self._width >= 520 else 1)
        weekly_mode = 3 if self._width >= 1220 else (2 if self._width >= 760 else 1)
        pulse_columns = prediction_pulse_columns(self._width)
        signature = (
            columns,
            health_columns,
            weekly_mode,
            1 if self._width < 760 else 0,
            pulse_columns,
        )
        self._layout_header()
        if signature == self._layout_signature and not force:
            return
        self._layout_signature = signature
        self._layout_columns = columns
        self._layout_summary()
        self._layout_prediction_pulse(pulse_columns)
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
        self._layout_weekly_regions(weekly_mode)
        self._layout_weekly_details(columns)
        self._refresh_scroll_region()

    def _layout_header(self) -> None:
        if (
            self.header_frame is None
            or self.header_actions is None
            or self.subtitle_label is None
        ):
            return
        self.header_actions.grid_forget()
        if self._width < 760:
            self.header_actions.grid(
                row=1,
                column=0,
                sticky=tk.E,
                pady=(8, 0),
            )
            subtitle_width = max(280, self._width - 45)
        else:
            self.header_actions.grid(
                row=0,
                column=1,
                sticky=tk.NE,
                padx=(12, 0),
            )
            subtitle_width = max(360, self._width - 320)
        self.subtitle_label.configure(wraplength=subtitle_width)
        symbol_wrap = max(180, self._width - 300)
        for widgets in self._symbol_sections:
            widgets.header.configure(wraplength=symbol_wrap)
        if self.source_label is not None:
            self.source_label.configure(wraplength=max(280, self._width - 45))

    def _layout_weekly_regions(self, mode: int) -> None:
        for widgets in self._weekly_layouts:
            for region in (
                widgets.summary,
                widgets.timeline,
                widgets.performance,
            ):
                region.grid_forget()
            for column in range(3):
                widgets.container.grid_columnconfigure(
                    column,
                    weight=0,
                    uniform="",
                )
            if mode == 3:
                for column, weight in enumerate((4, 5, 6)):
                    widgets.container.grid_columnconfigure(
                        column,
                        weight=weight,
                        uniform="weekly-region",
                    )
                widgets.summary.grid(
                    row=0,
                    column=0,
                    sticky=tk.NSEW,
                    padx=(0, 4),
                )
                widgets.timeline.grid(
                    row=0,
                    column=1,
                    sticky=tk.NSEW,
                    padx=4,
                )
                widgets.performance.grid(
                    row=0,
                    column=2,
                    sticky=tk.NSEW,
                    padx=(4, 0),
                )
            elif mode == 2:
                for column in range(2):
                    widgets.container.grid_columnconfigure(
                        column,
                        weight=1,
                        uniform="weekly-region",
                    )
                widgets.summary.grid(
                    row=0,
                    column=0,
                    sticky=tk.NSEW,
                    padx=(0, 4),
                )
                widgets.timeline.grid(
                    row=0,
                    column=1,
                    sticky=tk.NSEW,
                    padx=(4, 0),
                )
                widgets.performance.grid(
                    row=1,
                    column=0,
                    columnspan=2,
                    sticky=tk.NSEW,
                    pady=(8, 0),
                )
            else:
                widgets.container.grid_columnconfigure(0, weight=1)
                widgets.summary.grid(row=0, column=0, sticky=tk.NSEW)
                widgets.timeline.grid(
                    row=1,
                    column=0,
                    sticky=tk.NSEW,
                    pady=(8, 0),
                )
                widgets.performance.grid(
                    row=2,
                    column=0,
                    sticky=tk.NSEW,
                    pady=(8, 0),
                )

    def _layout_weekly_details(self, columns: int) -> None:
        for details in self._weekly_details:
            for card in details.cards:
                card.grid_forget()
            for column in range(len(STANDARD_HORIZON_ORDER)):
                details.body.grid_columnconfigure(
                    column,
                    weight=1 if column < columns else 0,
                    uniform=(
                        "weekly-session"
                        if column < columns
                        else ""
                    ),
                )
            for index, card in enumerate(details.cards):
                row, column = divmod(index, columns)
                card.grid(
                    row=row,
                    column=column,
                    sticky=tk.NSEW,
                    padx=(0 if column == 0 else 5, 0),
                    pady=(0, 7),
                )

    def _layout_summary(self) -> None:
        if self.summary_frame is None:
            return
        if self._width >= 1080:
            columns = 4
        elif self._width >= 520:
            columns = 2
        else:
            columns = 1
        for card in self._summary_cards:
            card.grid_forget()
        for column in range(4):
            self.summary_frame.grid_columnconfigure(
                column,
                weight=1 if column < columns else 0,
                uniform="forecast-health" if column < columns else "",
            )
        rows = (
            (len(self._summary_cards) + columns - 1) // columns
            if self._summary_cards
            else 0
        )
        health_wrap = max(150, min(265, int(self._width / columns) - 90))
        for value_label, detail_label in self._summary_labels:
            value_label.configure(wraplength=health_wrap)
            detail_label.configure(wraplength=health_wrap)
        for index, card in enumerate(self._summary_cards):
            row, column = divmod(index, columns)
            card.grid(
                row=row,
                column=column,
                sticky=tk.NSEW,
                padx=(0, 1) if column < columns - 1 else 0,
                pady=(0, 1) if row < rows - 1 else 0,
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


def _badge_style(tone: str) -> str:
    styles = {
        "success": "ForecastBadgeSuccess.TLabel",
        "warning": "ForecastBadgeWarning.TLabel",
        "danger": "ForecastBadgeDanger.TLabel",
        "neutral": "ForecastBadgeNeutral.TLabel",
    }
    return styles.get(tone, "ForecastBadgeNeutral.TLabel")


def _lift_style(tone: str) -> str:
    styles = {
        "success": "ForecastLiftSuccess.TLabel",
        "danger": "ForecastLiftDanger.TLabel",
        "neutral": "ForecastLiftNeutral.TLabel",
    }
    return styles.get(tone, "ForecastLiftNeutral.TLabel")


def _tone_color(tone: str) -> str:
    return {
        "success": UP_COLOR,
        "warning": WARNING,
        "danger": DOWN_COLOR,
        "neutral": MUTED_TEXT,
    }.get(tone, MUTED_TEXT)


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
