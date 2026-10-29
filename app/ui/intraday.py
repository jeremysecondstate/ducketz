"""15-Minute Duckets: a read-only view of persisted forecasts and outcomes."""
from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import ttk
from typing import Mapping

from app.ui.intraday_data import (
    PACIFIC, WINDOWS, FORECAST_MODES, DEFAULT_FORECAST_MODE, DEFAULT_METRIC_SCOPE, METRIC_SCOPES,
    IntradaySnapshot, configured_actor, default_databases, error_correlations,
    cycle_symbol_evidence, filter_forecasts, forecast_metrics, load_intraday_snapshot, symbol_coverage,
    metric_rows, model_metrics, prediction_timing, probability, probability_geometry, regime_metrics, source_dataset_label, timestamp,
    reference_input_label, reference_input_provenance, saved_forecast_context,
    window_rows,
)
from app.ui.intraday_models import load_model_research, research_choices, research_view
from app.ui.theme import BACKGROUND, BORDER, DANGER, MUTED_TEXT, SUCCESS, SURFACE, SURFACE_ALT, TEXT, WARNING

AUTO_REFRESH_MS = 15 * 60 * 1000
POLL_MS = 40


def _prepare_metrics(snapshot, selection, database_paths=None):
    """Compute saved-history statistics without touching Tk or live services."""
    filters = {key: selection[key] for key in
               ("pc", "symbol", "session", "mode", "reference_slot", "source_dataset")}
    rows = filter_forecasts(snapshot, **filters)
    cohort = metric_rows(rows, scope=selection["metric_scope"])
    current = window_rows(cohort, selection["metric_window"], snapshot.loaded_at)
    metrics = model_metrics(current)
    detailed = list(metrics.items())
    for name, scoped in (
        ("Full ensemble", [row for row in current if row.get("ensemble_status") == "full"]),
        ("Incomplete ensemble", [row for row in current if row.get("ensemble_status") == "incomplete"]),
        ("Issued before target close", [row for row in current if timestamp(row.get("issued_at")) and timestamp(row.get("target_close"))
                                       and timestamp(row["issued_at"]) < timestamp(row["target_close"])]),
        ("Issued at/after target", [row for row in current if timestamp(row.get("issued_at")) and timestamp(row.get("target_close"))
                                    and timestamp(row["issued_at"]) >= timestamp(row["target_close"])]),
    ):
        detailed.append((name, forecast_metrics(scoped)))
    for dimension in ("pc", "symbol"):
        for value in sorted({str(row.get(dimension)) for row in current}):
            detailed.append((f"{dimension}: {value}", forecast_metrics([row for row in current if row.get(dimension) == value])))
    newest = symbol_coverage(snapshot, **filters)
    newest_forecasts = [display["forecast"] for display in newest if display["forecast"] is not None]
    provenance = {(row.get("source_label", "Local"), row.get("pc"), row.get("forecast_id")): reference_input_provenance(
        row, database_path=(database_paths or {}).get(row.get("source_label", "Local"))) for row in newest_forecasts}
    return dict(selection=selection, rows=rows, newest=newest, provenance=provenance, cohort=cohort,
                windows=[(window, forecast_metrics(window_rows(cohort, window, snapshot.loaded_at))) for window in WINDOWS],
                detailed=detailed, ensemble=metrics["Ensemble"], correlations=error_correlations(current),
                regimes=regime_metrics(current))


def percent(value):
    return "—" if value is None else f"{value * 100:.1f}%"


def numeric(value, places=3):
    return "—" if value is None else f"{value:.{places}f}"


def clock(value):
    parsed = timestamp(value)
    return parsed.astimezone(PACIFIC).strftime("%b %d %H:%M:%S %Z") if parsed else "—"


def age(value, now):
    parsed = timestamp(value)
    if parsed is None:
        return "unknown"
    seconds = (now - parsed).total_seconds()
    return "future timestamp" if seconds < 0 else f"{seconds / 60:.1f}m ago"


def coverage_text(row):
    if not row.get("coverage_reason"):
        return ""
    return (f"{row['coverage_reason']} Recorded {clock(row.get('evidence_at'))}; "
            f"cycle started {clock(row.get('started_at'))}; reference {clock(row.get('reference_at'))}.")


def _label(parent, text="", *, color=TEXT, size=10, bold=False, **kwargs):
    return tk.Label(parent, text=text, bg=parent.cget("background"), fg=color,
                    font=("Segoe UI", size, "bold" if bold else "normal"), **kwargs)


def _table(parent, columns, *, height=12):
    holder = tk.Frame(parent, bg=SURFACE)
    holder.pack(fill="both", expand=True, pady=(4, 10))
    tree = ttk.Treeview(holder, columns=[key for key, _, _ in columns], show="headings", height=height)
    for key, text, width in columns:
        tree.heading(key, text=text)
        tree.column(key, width=width, minwidth=55, anchor="w", stretch=False)
    vertical = ttk.Scrollbar(holder, orient="vertical", command=tree.yview)
    horizontal = ttk.Scrollbar(holder, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vertical.grid(row=0, column=1, sticky="ns")
    horizontal.grid(row=1, column=0, sticky="ew")
    holder.columnconfigure(0, weight=1)
    holder.rowconfigure(0, weight=1)
    for tag, color in (("not-down", SUCCESS), ("down", DANGER), ("pending", MUTED_TEXT),
                       ("synthetic", WARNING), ("missing", WARNING), ("unscorable", WARNING)):
        tree.tag_configure(tag, foreground=color)
    return tree


def _replace(tree, rows):
    tree.delete(*tree.get_children())
    for values, tags in rows:
        tree.insert("", "end", values=values, tags=tags)


class IntradayTab:
    def __init__(self, root: tk.Tk, parent: ttk.Frame, *, datastore_root: Path | None = None,
                 database_paths: Mapping[str, Path] | None = None, local_pc: str | None = None,
                 model_research_directory: Path | None = None, auto_load=True):
        self.root, self.parent = root, parent
        self.database_paths = dict(database_paths if database_paths is not None else default_databases(datastore_root))
        self.local_pc = local_pc or configured_actor()
        local_database = self.database_paths.get("Local")
        self.model_research_directory = model_research_directory
        if self.model_research_directory is None and local_database and self.local_pc in {"Atlas", "Scout"}:
            suffix = "supervision" if self.local_pc == "Scout" else "model-supervision/logs"
            self.model_research_directory = Path(local_database).parent / suffix
        self.model_research = {"status": "missing", "message": "Refresh to load local model research."}
        self.snapshot = IntradaySnapshot(datetime.now(timezone.utc))
        self.pc = tk.StringVar(master=root, value="All")
        self.symbol = tk.StringVar(master=root, value="All")
        self.session = tk.StringVar(master=root, value="All")
        self.reference_slot = tk.StringVar(master=root, value="All")
        self.source_dataset = tk.StringVar(master=root, value="All")
        self.mode = tk.StringVar(master=root, value=DEFAULT_FORECAST_MODE)
        self.metric_window = tk.StringVar(master=root, value="Cumulative")
        self.metric_scope = tk.StringVar(master=root, value=DEFAULT_METRIC_SCOPE)
        self.status = tk.StringVar(master=root, value="Read-only saved forecasts · no store loaded")
        self.consensus_choice = tk.StringVar(master=root)
        self._closed, self._auto_job = False, None
        self._auto_enabled = auto_load
        self._loading = self._render_loading = False
        self._render_again = False
        self._messages = queue.Queue()
        self._stopped = threading.Event()
        self._view_revision = 0
        self.visible_rows = ()
        self._build()
        parent.bind("<Destroy>", self._destroy, add="+")
        self._poll_job = parent.after(POLL_MS, self._poll)
        if auto_load:
            parent.after_idle(self.refresh)
            self._auto_job = parent.after(AUTO_REFRESH_MS, self._auto_refresh)

    def _build(self):
        self.page = tk.Frame(self.parent, bg=BACKGROUND)
        self.page.pack(fill="both", expand=True, padx=16, pady=12)
        _label(self.page, "15-Minute Duckets", size=21, bold=True).pack(anchor="w")
        _label(self.page, "Persisted probabilities and final target outcomes · times Pacific · classification ≥50% is not-down",
               color=MUTED_TEXT).pack(anchor="w", pady=(2, 7))
        _label(self.page, "Live identifies a manual-session forecast. Submission and fill evidence remain separate; monitoring never enables trading.",
               color=MUTED_TEXT, size=9).pack(anchor="w", pady=(0, 7))
        controls = tk.Frame(self.page, bg=BACKGROUND)
        controls.pack(fill="x")
        self.boxes = {}
        for title, key, variable, choices, width in (
            ("PC", "pc", self.pc, ("All",), 11), ("Symbol", "symbol", self.symbol, ("All",), 10),
            ("Session", "session", self.session, ("All",), 13),
            ("Mode", "mode", self.mode, FORECAST_MODES, 19),
            ("Metrics", "window", self.metric_window, WINDOWS, 13),
        ):
            _label(controls, title, color=MUTED_TEXT).pack(side="left", padx=(0, 4))
            box = ttk.Combobox(controls, textvariable=variable, values=choices, state="readonly", width=width)
            box.pack(side="left", padx=(0, 10))
            box.bind("<<ComboboxSelected>>", lambda _event: self.render())
            self.boxes[key] = box
        ttk.Button(controls, text="Refresh saved results", command=self.refresh).pack(side="right")
        time_controls = tk.Frame(self.page, bg=BACKGROUND)
        time_controls.pack(fill="x", pady=(6, 0))
        _label(time_controls, "Reference candle opening", color=MUTED_TEXT).pack(side="left", padx=(0, 8))
        reference_box = ttk.Combobox(time_controls, textvariable=self.reference_slot, values=("All",), state="readonly", width=8)
        reference_box.pack(side="left")
        reference_box.bind("<<ComboboxSelected>>", lambda _event: self.render())
        self.boxes["reference_slot"] = reference_box
        _label(time_controls, "Source dataset", color=MUTED_TEXT).pack(side="left", padx=(16, 8))
        source_box = ttk.Combobox(time_controls, textvariable=self.source_dataset, values=("All",), state="readonly", width=15)
        source_box.pack(side="left")
        source_box.bind("<<ComboboxSelected>>", lambda _event: self.render())
        self.boxes["source_dataset"] = source_box
        _label(time_controls, "Score cohort", color=MUTED_TEXT).pack(side="left", padx=(16, 8))
        scope_box = ttk.Combobox(time_controls, textvariable=self.metric_scope, values=METRIC_SCOPES,
                                state="readonly", width=34)
        scope_box.pack(side="left")
        scope_box.bind("<<ComboboxSelected>>", lambda _event: self.render())
        self.boxes["metric_scope"] = scope_box
        self.metric_status = _label(self.page, color=MUTED_TEXT, size=9, wraplength=1400, justify="left")
        self.metric_status.pack(fill="x", pady=(5, 0))
        self.status_label = _label(self.page, color=MUTED_TEXT, size=9, textvariable=self.status,
                                   wraplength=1200, justify="left")
        self.status_label.pack(fill="x", pady=(7, 8))
        notebook = ttk.Notebook(self.page)
        notebook.pack(fill="both", expand=True)
        pages = {}
        for key, text in (("consensus", "Ensemble consensus"), ("matrix", "Prediction matrix"),
                          ("metrics", "Accuracy"), ("calibration", "Calibration & disagreement"),
                          ("regimes", "Regime diagnostics"), ("research", "Model research"),
                          ("operations", "Operations & intentions")):
            frame = tk.Frame(notebook, bg=SURFACE)
            notebook.add(frame, text=text)
            pages[key] = frame
        self.consensus_page = pages["consensus"]
        chooser = tk.Frame(self.consensus_page, bg=SURFACE)
        chooser.pack(fill="x", padx=14, pady=(10, 4))
        _label(chooser, "Saved issue / cycle coverage for", color=MUTED_TEXT).pack(side="left")
        self.consensus_box = ttk.Combobox(chooser, textvariable=self.consensus_choice, state="readonly", width=62)
        self.consensus_box.pack(side="left", padx=10)
        self.consensus_box.bind("<<ComboboxSelected>>", lambda _event: self._render_consensus())
        self.consensus_context = _label(self.consensus_page, color=WARNING, bold=True,
                                        justify="left", wraplength=1200)
        self.consensus_context.pack(anchor="w", padx=14, pady=(5, 0))
        self.consensus_value = _label(self.consensus_page, "No saved predictions", size=26, bold=True)
        self.consensus_value.pack(anchor="w", padx=14, pady=5)
        self.consensus_meta = _label(self.consensus_page, color=MUTED_TEXT, justify="left", wraplength=1200)
        self.consensus_meta.pack(anchor="w", padx=14)
        viewport = tk.Frame(self.consensus_page, bg=SURFACE)
        viewport.pack(fill="both", expand=True, padx=14, pady=10)
        self.model_canvas = tk.Canvas(viewport, bg=SURFACE, highlightthickness=0)
        self.model_canvas.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(viewport, orient="vertical", command=self.model_canvas.yview)
        scroll.pack(side="right", fill="y")
        self.model_canvas.configure(yscrollcommand=scroll.set)
        self.models_frame = tk.Frame(self.model_canvas, bg=SURFACE)
        self.model_window = self.model_canvas.create_window(0, 0, window=self.models_frame, anchor="nw")
        self.models_frame.bind("<Configure>", lambda _e: self.model_canvas.configure(scrollregion=self.model_canvas.bbox("all")))
        self.model_canvas.bind("<Configure>", lambda e: self.model_canvas.itemconfigure(self.model_window, width=e.width))
        self.matrix = _table(pages["matrix"], [
            ("pc", "PC", 70), ("symbol", "Symbol", 75), ("source_dataset", "Source dataset", 110), ("probability", "Not-down", 95),
            ("direction", "Class", 90), ("count", "Models", 80), ("ensemble", "Ensemble", 95),
            ("outcome", "Outcome", 90), ("issue", "Issue Pacific", 170), ("target", "Target close Pacific", 170),
            ("age", "Issue age", 100), ("basis", "Availability evidence", 140), ("mode", "Mode", 140),
            ("coverage", "Latest cycle evidence", 500),
            ("timing", "Forecast timing", 190),
        ])
        _label(pages["matrix"], "Newest saved issue per PC/symbol/source dataset/mode. Symbols recorded in saved cycles remain visible without a forecast. Missing-issue rows use the cycle start date for the session filter and do not enter scores. A later cycle failure does not change an older saved probability or outcome.",
               color=MUTED_TEXT, wraplength=1200).pack(anchor="w", padx=10, pady=5)
        _label(pages["metrics"], "The score cohort applies before each window. By default, scores use observed outcomes from forecasts issued before target open. During-target and late catch-up results remain available in their own cohorts; All saved outcomes retains the historical combined view. Pending, missing and unscorable targets do not enter score denominators. The majority baseline describes the same scored sample and is not a prediction. Small samples cannot establish model quality or trading profitability.",
               color=MUTED_TEXT, wraplength=1200, justify="left").pack(anchor="w", padx=10, pady=8)
        metric_columns = [("name", "Window / model / scope", 190), ("issued", "Issued", 65), ("n", "Headline n", 85),
                          ("accuracy", "Accuracy", 90), ("observed", "Observed % / n", 135),
                          ("synthetic_accuracy", "Synthetic % / n", 135),
                          ("equality", "Equality % / n (obs / synth)", 220), ("balanced", "Balanced", 85), ("brier", "Brier", 80),
                          ("loss", "Log loss", 80), ("pending", "Pending", 70), ("missing", "Missing", 70),
                          ("synthetic", "Synthetic", 80), ("unscorable", "Unscorable", 85),
                          ("baseline", "Majority baseline", 135), ("prevalence", "Not-down outcomes", 135)]
        self.window_table = _table(pages["metrics"], metric_columns, height=6)
        self.model_table = _table(pages["metrics"], metric_columns, height=11)
        _label(pages["calibration"], "Calibration and correlations use the selected score cohort and metric window. Synthetic and late catch-up outcomes enter only when included in that cohort. Error correlation is pairwise (probability − target label), with its own overlap count.",
               color=MUTED_TEXT, wraplength=1200).pack(anchor="w", padx=10, pady=8)
        self.disagreement_label = _label(pages["calibration"], color=MUTED_TEXT)
        self.disagreement_label.pack(anchor="w", padx=10)
        self.calibration_table = _table(pages["calibration"], [("bin", "Ensemble probability bin", 180),
            ("n", "Headline scored n", 140), ("mean", "Mean probability", 160), ("rate", "Realized not-down rate", 180)], height=10)
        self.correlation_table = _table(pages["calibration"], [("left", "Model A", 190), ("right", "Model B", 190),
            ("n", "Pairwise n", 100), ("correlation", "Error correlation", 140)], height=7)
        _label(pages["regimes"], "Realized |return| bins are retrospective (≤0.1%, 0.1–1%, ≥1%). Persisted causal volatility20, trend efficiency (net / total absolute change) and choppiness20 use trailing observed candles. Their Low/Mid/High bins use selected-cohort retrospective tertiles (ties stay together); they are not live features or execution gates.",
               color=MUTED_TEXT, wraplength=1200, justify="left").pack(anchor="w", padx=10, pady=8)
        self.regime_table = _table(pages["regimes"], [("dimension", "Dimension", 200), ("bucket", "Category", 160),
            ("n", "Headline scored n", 140), ("observed", "Observed n", 100), ("synthetic", "Synthetic n", 100),
            ("accuracy", "Accuracy", 110), ("brier", "Brier", 100), ("loss", "Log loss", 100)])
        self.research_status = _label(pages["research"], color=MUTED_TEXT, wraplength=1400, justify="left")
        self.research_status.pack(anchor="w", padx=10, pady=8)
        self.research_table = _table(pages["research"], [
            ("family", "Model family", 180), ("name", "Configuration", 230),
            ("status", "Research result", 120), ("selected", "Selected specs", 110),
            ("reason", "Selection / exclusion reason", 340), ("brier", "Validation Brier", 130),
            ("balanced", "Validation balanced", 140), ("advantage", "Brier advantage", 130),
            ("holdout", "Saved holdout Brier", 180), ("seconds", "Research seconds", 130)])
        self.operation_status = _label(pages["operations"], color=MUTED_TEXT, wraplength=1200, justify="left")
        self.operation_status.pack(anchor="w", padx=10, pady=8)
        self.cycle_table = _table(pages["operations"], [("pc", "PC", 80), ("cycle", "Cycle", 190),
            ("start", "Start Pacific", 170), ("status", "Status", 100), ("latency", "Saved timing/resource evidence", 620)], height=7)
        _label(pages["operations"], "Intended actions are recorded independently of forecast scoring. Quantities and disposition below are saved evidence; no submitted orders, fills or P&L are inferred.",
               color=MUTED_TEXT, wraplength=1200).pack(anchor="w", padx=10, pady=5)
        self.intent_table = _table(pages["operations"], [("pc", "PC", 70), ("symbol", "Symbol", 75),
            ("forecast", "Forecast", 250), ("direction", "Intended direction", 120), ("desired", "Desired", 80),
            ("adjusted", "Adjusted", 80), ("disposition", "Disposition", 330)], height=9)

    def refresh(self):
        if self._closed or self._loading:
            return
        self._loading = True
        self.status.set("Loading saved results… You can keep using the other tabs.")
        directory, actor, paths = self.model_research_directory, self.local_pc, dict(self.database_paths)
        messages, stopped, selection = self._messages, self._stopped, self._selection()
        # Capture plain data only. Workers never keep a Tk owner alive or call
        # after()/StringVar, even when the window closes during a slow read.
        def work():
            try:
                research = load_model_research(directory, actor=actor)
                snapshot = load_intraday_snapshot(paths, local_pc=actor, stopped=stopped.is_set)
                if any(source.get("status") == "unreadable" for source in snapshot.sources) and not any(
                        source.get("status") == "read" for source in snapshot.sources):
                    raise ValueError(" | ".join(snapshot.warnings) or "Saved source could not be read")
                prepared = None if stopped.is_set() else _prepare_metrics(snapshot, selection, paths)
                if not stopped.is_set():
                    messages.put(("loaded", (research, snapshot, prepared)))
            except Exception as exc:
                if not stopped.is_set():
                    messages.put(("load_error", str(exc) or type(exc).__name__))
        threading.Thread(target=work, name="intraday-ui-read", daemon=True).start()

    def _selection(self):
        return {key: getattr(self, key).get() for key in
                ("pc", "symbol", "session", "mode", "reference_slot", "source_dataset", "metric_scope", "metric_window")}

    def _poll(self):
        self._poll_job = None
        if self._closed:
            return
        try:
            while True:
                kind, value = self._messages.get_nowait()
                if kind == "loaded":
                    self._loading = False
                    self.model_research, snapshot, prepared = value
                    self.set_snapshot(snapshot, _prepared=prepared)
                elif kind == "load_error":
                    self._loading = False
                    self.status.set(f"Refresh failed; previous saved results retained: {value}")
                elif kind == "rendered":
                    self._render_loading = False
                    revision, prepared = value
                    if revision == self._view_revision and prepared["selection"] == self._selection():
                        self.render(_prepared=prepared)
                    else:
                        self._render_again = True
                    if self._render_again:
                        self._render_again = False
                        self.render()
                elif kind == "render_error":
                    self._render_loading = False
                    self.status.set(f"Saved-result display failed; previous view retained: {value}")
        except queue.Empty:
            pass
        except Exception as exc:
            if not self._closed:
                self.status.set(f"Saved-result display failed: {str(exc) or type(exc).__name__}")
        finally:
            if not self._closed:
                self._poll_job = self.parent.after(POLL_MS, self._poll)

    def set_snapshot(self, snapshot: IntradaySnapshot, *, _prepared=None):
        if self._closed:
            return
        self._view_revision += 1
        self.snapshot = snapshot
        research = research_choices(self.model_research)
        coverage = cycle_symbol_evidence(snapshot)
        choices = {
            "pc": ["All", *sorted({str(row.get("pc")) for row in (*snapshot.forecasts, *coverage)} | set(research["pcs"]))],
            "symbol": ["All", *sorted({str(row.get("symbol")) for row in (*snapshot.forecasts, *coverage)} | set(research["symbols"]))],
            "source_dataset": ["All", *sorted({source_dataset_label(row) for row in (*snapshot.forecasts, *coverage)} | set(research["datasets"]))],
            "session": ["All", *sorted({timestamp(row.get("issued_at")).astimezone(PACIFIC).date().isoformat()
                         for row in snapshot.forecasts if timestamp(row.get("issued_at"))}
                         | {timestamp(row.get("started_at")).astimezone(PACIFIC).date().isoformat()
                            for row in coverage if timestamp(row.get("started_at"))}, reverse=True)],
            "reference_slot": ["All", *sorted({timestamp(row.get("reference_at")).astimezone(PACIFIC).strftime("%H:%M")
                               for row in (*snapshot.forecasts, *coverage) if timestamp(row.get("reference_at"))})],
        }
        for key, values in choices.items():
            self.boxes[key].configure(values=values)
            variable = getattr(self, key)
            if variable.get() not in values:
                variable.set("All")
        sources = "; ".join(f"{item['label']}: {item['status']} {item.get('telemetry_status', '')} (newest saved issue {age(item.get('latest_issue'), snapshot.loaded_at)})"
                            for item in snapshot.sources)
        self.status.set(f"Loaded {clock(snapshot.loaded_at)} · {len(snapshot.forecasts)} saved issues · {sources or 'No sources configured'}"
                        + (" · " + " | ".join(snapshot.warnings) if snapshot.warnings else ""))
        self.render(_prepared=_prepared)

    def render(self, *, _prepared=None):
        if self._closed:
            return
        selection = self._selection()
        if _prepared is not None and _prepared["selection"] != selection:
            _prepared = None
        if _prepared is None and (len(self.snapshot.forecasts) > 100 or self._auto_enabled):
            if self._render_loading:
                self._render_again = True
                return
            self._render_loading = True
            snapshot, revision = self.snapshot, self._view_revision
            paths = dict(self.database_paths)
            messages, stopped = self._messages, self._stopped
            def work():
                try:
                    prepared = _prepare_metrics(snapshot, selection, paths)
                    if not stopped.is_set():
                        messages.put(("rendered", (revision, prepared)))
                except Exception as exc:
                    if not stopped.is_set():
                        messages.put(("render_error", str(exc) or type(exc).__name__))
            threading.Thread(target=work, name="intraday-ui-metrics", daemon=True).start()
            return
        prepared = _prepared if _prepared is not None else _prepare_metrics(self.snapshot, selection, getattr(self, "database_paths", {}))
        self.visible_rows = prepared["rows"]
        self._reference_provenance = prepared["provenance"]
        newest = prepared["newest"]
        missing = [row for row in newest if row["forecast"] is None]
        coverage_status = (
            f"Coverage: {len(newest)-len(missing)} with saved forecasts · {len(missing)} without"
            + (": " + ", ".join(f"{row['pc']} {row['symbol']} ({source_dataset_label(row)})" for row in missing) if missing else "")
            + ". Scope comes from saved cycle records; absent peer scope remains unknown.")
        self.consensus_evidence = {f"{row.get('pc')} · {row.get('symbol')} · {source_dataset_label(row)} · {row.get('mode', 'shadow')}": row
                                   for row in newest}
        self.consensus_rows = {key: row["forecast"] for key, row in self.consensus_evidence.items()}
        self.consensus_box.configure(values=list(self.consensus_rows))
        if self.consensus_choice.get() not in self.consensus_rows:
            self.consensus_choice.set(next(iter(self.consensus_rows), ""))
        self._render_consensus()
        matrix = []
        for display in newest:
            row = display["forecast"]
            detail = coverage_text(display)
            if row is None:
                matrix.append(((display.get("pc"), display.get("symbol"), source_dataset_label(display), "—",
                    "unavailable", "—", "No saved forecast", "—", "—", "—", "—", "—", display.get("mode"),
                    detail, "No saved forecast"), ("missing",)))
                continue
            p = probability(row.get("probability_not_down"))
            classification = "not-down" if p is not None and p >= .5 else "down" if p is not None else "unavailable"
            issued = timestamp(row.get("issued_at"))
            age_text = age(issued, self.snapshot.loaded_at)
            if issued and (self.snapshot.loaded_at-issued).total_seconds() >= 15*60:
                age_text += " · no newer issue in 15m"
            if p is not None and abs(p-.5) <= .01:
                classification += " · ±1pp"
            matrix.append(((row.get("pc"), row.get("symbol"), source_dataset_label(row), percent(p), classification,
                            f"{row.get('successful_count', 0)}/{row.get('configured_count', 0)}", row.get("ensemble_status", "unknown"),
                            row["outcome_status"], clock(row.get("issued_at")), clock(row.get("target_close")), age_text,
                            row.get("availability_basis", "unknown"), row.get("mode", "shadow"), detail, prediction_timing(row)),
                           (classification.split(" · ")[0],)))
        _replace(self.matrix, matrix)
        cohort = prepared["cohort"]
        self.metric_status.configure(text=f"Scores: {self.metric_scope.get()} · {len(cohort)} of {len(self.visible_rows)} saved issues in this cohort"
            f" · before target open {sum(prediction_timing(row) == 'Before target open' for row in self.visible_rows)}"
            f" · during target {sum(prediction_timing(row) == 'During target' for row in self.visible_rows)}"
            f" · at/after close {sum(prediction_timing(row) == 'At or after target close' for row in self.visible_rows)}."
            " Consensus and matrix retain all saved issues.\n" + coverage_status)
        _replace(self.window_table, [(self._metric_values(window, values), ()) for window, values in prepared["windows"]])
        detailed = [(self._metric_values(name, values), ()) for name, values in prepared["detailed"]]
        _replace(self.model_table, detailed)
        ensemble = prepared["ensemble"]
        self.disagreement_label.configure(text=f"{self.metric_scope.get()} · {self.metric_window.get()} · Mean per-issue probability standard deviation {percent(ensemble['mean_disagreement'])}"
                                          f" · n={ensemble['disagreement_denominator']} issues with ≥2 successful outputs. Balanced accuracy needs both target classes.")
        _replace(self.calibration_table, [((entry["range"], entry["count"], percent(entry["mean_probability"]), percent(entry["observed_not_down"])), ()) for entry in ensemble["calibration"]])
        _replace(self.correlation_table, [((entry["left"], entry["right"], entry["count"], numeric(entry["correlation"])), ()) for entry in prepared["correlations"]])
        _replace(self.regime_table, [((entry["dimension"], entry["bucket"], entry["denominator"], entry["observed_denominator"],
                                     entry["synthetic_denominator"], percent(entry["accuracy"]), numeric(entry["brier"]), numeric(entry["log_loss"])), ()) for entry in prepared["regimes"]])
        self._render_operations()

    @staticmethod
    def _metric_values(name, values):
        counts = values["counts"]
        return (name, values["issued"], values["denominator"], percent(values["accuracy"]),
                f"{percent(values['observed_accuracy'])} / {values['observed_denominator']}",
                f"{percent(values['synthetic_accuracy'])} / {values['synthetic_denominator']}",
                f"{percent(values['equality_accuracy'])} / {values['equality_denominator']} ({values['observed_equality_denominator']} / {values['synthetic_equality_denominator']})",
                percent(values["balanced_accuracy"]),
                numeric(values["brier"]), numeric(values["log_loss"]), counts["pending"], counts["missing"], counts["synthetic"], counts["unscorable"],
                percent(values["majority_class_baseline_accuracy"]), percent(values["positive_label_fraction"]))

    def _render_consensus(self):
        for child in self.models_frame.winfo_children():
            child.destroy()
        row = getattr(self, "consensus_rows", {}).get(self.consensus_choice.get())
        display = getattr(self, "consensus_evidence", {}).get(self.consensus_choice.get()) or row
        # The research view remains usable before a manual trader has produced
        # a forecast. Explicit PC/source filters still apply in that case.
        pc = display.get("pc") if display else self.pc.get()
        source = source_dataset_label(display) if display else self.source_dataset.get()
        view = research_view(self.model_research, pc=self.local_pc if pc == "All" else pc,
                             symbol=display.get("symbol") if display else self.symbol.get(),
                             source_dataset=None if source == "All" else source)
        self.research_status.configure(text=view["summary"])
        if view.get("holdout_label"):
            self.research_table.heading("holdout", text=view["holdout_label"])
        _replace(self.research_table, [((item.get("family"), item.get("name"), item.get("status"),
            "Yes" if item.get("selected") else "No", item.get("reason"), numeric(item.get("validation_brier")),
            percent(item.get("validation_balanced_accuracy")), numeric(item.get("baseline_improvement")),
            numeric(item.get("holdout_brier")), numeric(item.get("seconds"))), ()) for item in view["rows"]])
        if row is None:
            self.consensus_context.configure(text=coverage_text(display) if display else "")
            self.consensus_value.configure(text="No saved forecast for this symbol" if display else "No saved predictions in these filters", fg=MUTED_TEXT)
            self.consensus_meta.configure(text=(
                f"{display.get('pc')} · {display.get('symbol')} · Source dataset {source_dataset_label(display)} · {display.get('mode')}\n"
                "Saved cycle evidence provides no probability or model result for this selection. "
                "A missing forecast alone does not establish training, order, fill or market-wide trading activity."
                if display else "The display will populate when a trained ensemble persists its issue. Historical replay is selectable separately."))
            return
        p = probability(row.get("probability_not_down"))
        self.consensus_context.configure(text=saved_forecast_context(row, now=self.snapshot.loaded_at)
            + ("\nLatest cycle evidence: " + coverage_text(display) if display.get("coverage_reason") else ""))
        classification = "not-down" if p is not None and p >= .5 else "down" if p is not None else "unavailable"
        color = SUCCESS if classification == "not-down" else DANGER if classification == "down" else MUTED_TEXT
        self.consensus_value.configure(text=f"{percent(p)} ensemble probability · {classification}", fg=color)
        try:
            reference = f"${float(row.get('reference_open')):,.4f}"
        except (TypeError, ValueError):
            reference = "unavailable"
        provenance = getattr(self, "_reference_provenance", {}).get((row.get("source_label", "Local"), row.get("pc"), row.get("forecast_id")))
        # Legacy provenance is resolved by the worker, never by a Tk callback.
        input_label = reference_input_label(provenance or reference_input_provenance(row))
        self.consensus_meta.configure(text=(f"{row.get('successful_count', 0)}/{row.get('configured_count', 0)} enabled models succeeded · {row.get('ensemble_status', 'unknown')} ensemble · outcome {row['outcome_status']}\n"
            f"{input_label}\n"
            f"Source dataset {source_dataset_label(row)} · source {row.get('source_id') or 'Unrecorded'}\n"
            f"Reference OPEN {reference} at {clock(row.get('reference_at'))} · target candle opens {clock(row.get('target_open'))}, closes {clock(row.get('target_close'))}\n"
            f"Issued {clock(row.get('issued_at'))} · data through {clock(row.get('data_through'))} · information available {clock(row.get('information_available_at'))}\n"
            f"Training completed {clock(row.get('training_completed_at'))} · {prediction_timing(row)}"
            f" · execution {row.get('execution_blocked_reason') or row.get('execution_eligibility') or 'not recorded'}\n"
            f"Stage seconds: preprocessing {numeric(row.get('preprocessing_seconds'))} · training {numeric(row.get('training_seconds'))} · inference {numeric(row.get('inference_seconds'))} · persistence {numeric(row.get('persistence_evidence', {}).get('persistence_seconds'))}\n"
            f"Target {row.get('target_version', 'unknown')} · {row.get('mode', 'shadow')} · receipt availability {row.get('availability_basis', 'unknown')} · forecast {row.get('forecast_id')}"))
        for output in row.get("models", []):
            frame = tk.Frame(self.models_frame, bg=SURFACE)
            frame.pack(fill="x", pady=6)
            _label(frame, str(output.get("name", "Unnamed model")), bold=True, width=23, anchor="w").pack(side="left")
            value = probability(output.get("probability_not_down"))
            bar = tk.Canvas(frame, width=390, height=34, bg=SURFACE_ALT, highlightthickness=1, highlightbackground=BORDER)
            bar.pack(side="left", fill="x", expand=True, padx=8)
            def draw(event=None, canvas=bar, model_probability=value):
                canvas.delete("all")
                width = max(1, canvas.winfo_width())
                fill, midpoint = probability_geometry(model_probability, width)
                if fill is not None:
                    canvas.create_rectangle(0, 7, fill, 27, fill=SUCCESS if model_probability >= .5 else DANGER, width=0)
                canvas.create_line(midpoint, 0, midpoint, 34, fill=TEXT, dash=(3, 2))
                canvas.create_text(midpoint + 3, 3, text="50%", anchor="nw", fill=TEXT, font=("Segoe UI", 7))
                canvas.create_text(3, 32, text="0", anchor="sw", fill=MUTED_TEXT, font=("Segoe UI", 7))
                canvas.create_text(width-3, 32, text="100%", anchor="se", fill=MUTED_TEXT, font=("Segoe UI", 7))
            bar.bind("<Configure>", draw)
            _label(frame, percent(value), width=8, bold=True).pack(side="left")
            status = str(output.get("status", "unknown"))
            version = str(output.get("version") or "no version")
            visible_version = version[:12] + "…" if len(version) > 16 else version
            detail = f"{status} · version {visible_version}"
            if output.get("error"):
                detail += f" · {output['error']}"
            timings = " / ".join(f"{key.replace('_seconds', '')} {float(output[key]):.3f}s" for key in ("training_seconds", "inference_seconds") if isinstance(output.get(key), (int, float)))
            model_detail = _label(frame, detail + (f" · {timings}" if timings else ""), color=MUTED_TEXT if status == "success" else WARNING,
                                  size=9, wraplength=280, justify="left", width=42, anchor="w", cursor="hand2")
            model_detail.pack(side="left", padx=(8, 0))
            model_detail.bind("<Button-1>", lambda _e, saved=output: self._show_model(saved))

    def _show_model(self, output):
        window = tk.Toplevel(self.root)
        window.title(f"Saved model summary · {output.get('name', 'unknown')}")
        window.geometry("900x650")
        if output.get("saved_evidence_source"):
            _label(window, "Display fields only. Full scientific metadata remains in the original database; the source locator below identifies the saved record.",
                   color=MUTED_TEXT, wraplength=860, justify="left").pack(fill="x", padx=10, pady=8)
        text = tk.Text(window, bg=SURFACE, fg=TEXT, wrap="word", font=("Consolas", 10))
        text.pack(fill="both", expand=True)
        text.insert("1.0", json.dumps(output, indent=2, ensure_ascii=False, sort_keys=True))
        text.configure(state="disabled")

    def _render_operations(self):
        lines = []
        for source in self.snapshot.sources:
            line = f"{source['label']}: saved store {source['status']}; latest saved issue {age(source.get('latest_issue'), self.snapshot.loaded_at)}."
            if source.get("produced_at"):
                line += f" Producer {source.get('producer_pc')} exported {clock(source['produced_at'])}; first received {clock(source.get('received_at'))}; telemetry {source.get('telemetry_status')} (15m freshness interval; newest observed mode {source.get('latest_forecast_mode') or 'none'}). Private remote intention/order quantities are unavailable."
            lines.append(line + " Receipt freshness is not a live PC heartbeat.")
        self.operation_status.configure(text="\n".join(lines) or "No telemetry source configured. Remote monitoring requires an explicitly supplied, received read-only store path.")
        selected_cycles = {row.get("cycle_id") for row in self.visible_rows}
        cycles = [row for row in self.snapshot.cycles if (self.pc.get() == "All" or row.get("pc") == self.pc.get())
                  and (self.mode.get() == "All" or row.get("mode", "shadow") == self.mode.get())
                  and (self.source_dataset.get() == "All" or row.get("id", row.get("cycle_id")) in selected_cycles)]
        latest = {}
        # Local stores and peer packets return opposite cycle orders. Select by
        # saved start time so receipt order cannot replace the newest evidence.
        for row in sorted(cycles, key=lambda item: (
                timestamp(item.get("started_at")) or datetime.min.replace(tzinfo=timezone.utc),
                str(item.get("id", "")))):
            latest[(row.get("source_label"), row.get("pc"))] = row
        def evidence(row):
            keys = ("mode", "timings", "latency", "resources", "resources_before", "resources_after", "acquisition",
                    "failures", "failed_symbol_names", "symbols", "finished_at", "completed_at", "workload", "successful_symbols", "failed_symbols",
                    "total_cycle_seconds", "compute_cycle_seconds", "compute_persist_seconds", "acquisition_seconds",
                    "process_cpu_seconds", "exceeds_15m", "overrun_seconds", "measurement_scope",
                    "cumulative_attempt_seconds", "wall_elapsed_seconds", "wall_exceeds_15m", "wall_overrun_seconds",
                    "attempt_count", "latest_attempt")
            values = {key: row[key] for key in keys if key in row}
            return json.dumps(values, ensure_ascii=False, sort_keys=True) if values else "No cycle timing evidence saved"
        _replace(self.cycle_table, [((row.get("pc"), row.get("id"), clock(row.get("started_at")), row.get("status", "unknown"), evidence(row)), ()) for row in latest.values()])
        intents = []
        for row in reversed(self.visible_rows):
            for intent in row.get("intents", []):
                intents.append(((row.get("pc"), row.get("symbol"), row.get("forecast_id"),
                    intent.get("intended_direction", intent.get("direction", intent.get("side", "unrecorded"))),
                    intent.get("desired_quantity", "unrecorded"), intent.get("adjusted_quantity", intent.get("capacity_adjusted_quantity", "unrecorded")),
                    intent.get("disposition", intent.get("execution_disposition", "unrecorded"))), ()))
        _replace(self.intent_table, intents)

    def _auto_refresh(self):
        if not self._closed:
            self.refresh()
            self._auto_job = self.parent.after(AUTO_REFRESH_MS, self._auto_refresh)

    def _destroy(self, event):
        if event.widget is not self.parent:
            return
        self._closed = True
        self._stopped.set()
        if self._poll_job is not None:
            self.parent.after_cancel(self._poll_job)
            self._poll_job = None
        if self._auto_job is not None:
            self.parent.after_cancel(self._auto_job)
            self._auto_job = None
