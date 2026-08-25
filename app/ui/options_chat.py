from __future__ import annotations

import math
import tkinter as tk
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from typing import Any
from tkinter import ttk

from app.services.options_chat import OptionsChatMessage, OptionsChatService
from app.ui.background_tasks import run_in_background
from app.ui.theme import (
    ACCENT,
    BACKGROUND,
    BORDER,
    DANGER,
    FIELD_BACKGROUND,
    MUTED_TEXT,
    SUCCESS,
    SURFACE,
    SURFACE_ALT,
    TEXT,
)


MAX_DISCOVER_CANDIDATES_IN_CONTEXT = 30

OPTIONS_FIELD_GUIDE = {
    "Direction Up (ML)": (
        "Loop B's calibrated probability that the underlying price finishes up "
        "over the selected forecast horizon."
    ),
    "ML Profit Probability": (
        "The fitted strategy-outcome model's calibrated probability that this "
        "specific option structure produces a positive modeled outcome."
    ),
    "Scenario Coverage": (
        "The share of deterministic market scenarios in which the strategy is "
        "profitable; it is a coverage measure, not a probability forecast."
    ),
    "Expected Return": (
        "Modeled expected net profit divided by modeled capital at risk for the "
        "published candidate snapshot."
    ),
    "Portfolio Fit": (
        "A current-Schwab-position and funds fit label. It does not change the "
        "candidate's predictive model score or rank."
    ),
    "Score Basis": (
        "The model/evidence family used to rank the candidate, such as OPRA "
        "execution plus the strategy ML model."
    ),
    "Pricing / Quality": (
        "Snapshot pricing source, model availability, quote coverage, surface "
        "quality, liquidity checks, and any publication limitations."
    ),
    "Portfolio Impact": (
        "A local calculation from normalized Schwab balances, current shares, "
        "the edited ticket, and expiration payoff scenarios."
    ),
}

_ACCOUNT_VALUE_FIELDS = (
    "status",
    "liquidation_value",
    "cash_balance",
    "settled_cash",
    "cash_available_for_trading",
    "cash_available_for_withdrawal",
    "available_funds",
    "available_funds_non_marginable_trade",
    "buying_power",
    "buying_power_non_marginable_trade",
    "day_trading_buying_power",
    "margin_balance",
    "maintenance_requirement",
    "maintenance_excess",
    "maintenance_call",
    "unsettled_cash",
)

_POSITION_FIELDS = (
    "status",
    "symbol",
    "asset_type",
    "underlying_symbol",
    "option_type",
    "strike",
    "expiration",
    "delta",
    "underlying_price",
    "contract_multiplier",
    "long_quantity",
    "short_quantity",
    "net_quantity",
    "settled_quantity",
    "price",
    "market_value",
    "cost_basis",
    "unrealized_pnl",
    "day_pnl",
    "option_fields_complete",
)

_WORKING_ORDER_FIELDS = (
    "status",
    "order_status",
    "order_type",
    "complex_order_strategy_type",
    "symbol",
    "underlying_symbol",
    "asset_type",
    "instruction",
    "quantity",
    "filled_quantity",
    "remaining_quantity",
    "limit_price",
    "contract_multiplier",
    "reserved_cash",
    "remaining_net_debit",
    "remaining_net_credit",
    "legs",
    "option_fields_complete",
)

_CANDIDATE_ROW_FIELDS = (
    "decision_timestamp",
    "information_available_at",
    "target_window_start",
    "target_window_end",
    "entry_available_at",
    "strategy_family",
    "risk_form",
    "expiration_structure",
    "stock_requirement",
    "cash_requirement",
    "front_expiration",
    "front_days_to_expiration",
    "underlying_price",
    "entry_cash_flow",
    "entry_fees",
    "entry_net_credit",
    "entry_net_debit",
    "max_profit",
    "max_loss",
    "capital_required",
    "risk_calculation_status",
    "net_delta",
    "net_gamma",
    "net_theta",
    "net_vega",
    "mean_relative_spread",
    "max_relative_spread",
    "minimum_open_interest",
    "total_volume",
    "maximum_quote_staleness_seconds",
    "surface_quality_pass",
    "all_option_quotes_valid",
    "liquidity_policy_pass",
    "pricing_mode",
    "pricing_status",
    "pricing_source",
    "pricing_leg_coverage",
    "pricing_missing_reason",
    "pricing_candidate_edge",
    "pricing_conservative_edge",
    "pricing_edge_to_friction",
    "pricing_uncertainty",
    "pricing_probability_favorable",
    "pricing_relative_edge",
    "pricing_model_age_seconds",
    "model_version",
    "model_status",
)


def build_options_chat_context(
    *,
    snapshot: object | None,
    view: object | None,
    visible_candidates: Sequence[object],
    selected_candidate: object | None,
    selected_order_index: int,
    portfolio_impact: object | None,
    screen: Mapping[str, object],
    ticket: Mapping[str, object],
    impact_display: Mapping[str, object],
) -> dict[str, object]:
    """Build a compact, JSON-safe snapshot of the user's current Options UI."""

    candidates = list(visible_candidates)
    visible_slice = candidates[:MAX_DISCOVER_CANDIDATES_IN_CONTEXT]
    return {
        "schema": "ducketz-options-chat-context/v1",
        "screen": _json_safe(dict(screen)),
        "field_guide": OPTIONS_FIELD_GUIDE,
        "portfolio": _portfolio_context(snapshot),
        "discover": {
            "publication_loaded_at": _json_safe(getattr(view, "loaded_at", None)),
            "visible_candidate_count": len(candidates),
            "included_candidate_count": len(visible_slice),
            "candidate_list_truncated": len(visible_slice) < len(candidates),
            "candidates_in_current_display_order": [
                _candidate_summary(candidate) for candidate in visible_slice
            ],
        },
        "selected_strategy": _selected_candidate_context(
            selected_candidate,
            order_index=selected_order_index,
        ),
        "edited_ticket": _json_safe(dict(ticket)),
        "portfolio_impact": _impact_context(
            portfolio_impact,
            display=impact_display,
        ),
    }


def options_context_summary(context: Mapping[str, object]) -> str:
    screen = context.get("screen")
    screen_values = screen if isinstance(screen, Mapping) else {}
    selected = context.get("selected_strategy")
    selected_values = selected if isinstance(selected, Mapping) else {}
    symbol = str(screen_values.get("symbol") or "No symbol")
    horizon = str(screen_values.get("horizon") or "No horizon")
    strategy = str(selected_values.get("strategy") or "No strategy selected")
    entry_point = str(screen_values.get("chat_entry_point") or "Options Strategies")
    return f"Live context: {symbol} • {horizon} • {strategy} • {entry_point}"


class OptionsChatController:
    """Own one conversation window shared by Options Strategies pop-outs."""

    def __init__(
        self,
        *,
        root: tk.Misc,
        context_provider: Callable[[str], Mapping[str, object]],
        service_factory: Callable[[], OptionsChatService] = OptionsChatService,
    ) -> None:
        self.root = root
        self._context_provider = context_provider
        self._service_factory = service_factory
        self._service: OptionsChatService | None = None
        self._messages: list[OptionsChatMessage] = []
        self._entry_point = "Options Strategies"
        self._window: tk.Toplevel | None = None
        self._transcript: tk.Text | None = None
        self._input: tk.Text | None = None
        self._send_button: ttk.Button | None = None
        self._context_label: tk.Label | None = None
        self._status = tk.StringVar(master=root, value="Ready")
        self._busy = False
        self._apply_styles()

    @property
    def messages(self) -> tuple[OptionsChatMessage, ...]:
        return tuple(self._messages)

    def open(self, *, entry_point: str, owner: tk.Misc | None = None) -> None:
        self._entry_point = str(entry_point or "Options Strategies")
        owner_widget = owner or self.root
        if self._window is not None:
            try:
                self._window.transient(owner_widget.winfo_toplevel())
                self._window.deiconify()
                self._window.lift()
                self._window.focus_set()
                self.refresh_context_label()
                return
            except tk.TclError:
                self._reset_widgets()

        window = tk.Toplevel(self.root)
        self._window = window
        window.title("ChatGPT — Duckets Options Desk")
        window.configure(background=BACKGROUND)
        window.minsize(520, 560)
        window.protocol("WM_DELETE_WINDOW", self._close)
        try:
            window.transient(owner_widget.winfo_toplevel())
        except tk.TclError:
            pass
        self._place_window(window, owner=owner_widget)

        outer = ttk.Frame(window, padding=(14, 12), style="OptionsChatPage.TFrame")
        outer.grid(row=0, column=0, sticky=tk.NSEW)
        window.grid_rowconfigure(0, weight=1)
        window.grid_columnconfigure(0, weight=1)
        outer.grid_rowconfigure(2, weight=1)
        outer.grid_columnconfigure(0, weight=1)

        header = ttk.Frame(outer, style="OptionsChatPage.TFrame")
        header.grid(row=0, column=0, sticky=tk.EW, pady=(0, 8))
        header.grid_columnconfigure(0, weight=1)
        ttk.Label(
            header,
            text="ChatGPT · Options Desk",
            style="OptionsChatTitle.TLabel",
        ).grid(row=0, column=0, sticky=tk.W)
        ttk.Label(
            header,
            text="Current screen + Schwab portfolio + selected ticket",
            style="OptionsChatMuted.TLabel",
        ).grid(row=1, column=0, sticky=tk.W, pady=(1, 0))
        ttk.Button(
            header,
            text="New chat",
            style="OptionsChatSecondary.TButton",
            command=self._new_chat,
        ).grid(row=0, column=1, rowspan=2, sticky=tk.E)

        context_label = tk.Label(
            outer,
            background=SURFACE_ALT,
            foreground=MUTED_TEXT,
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=620,
            padx=9,
            pady=7,
            font=("Segoe UI", 9),
        )
        context_label.grid(row=1, column=0, sticky=tk.EW, pady=(0, 8))
        self._context_label = context_label

        transcript_frame = ttk.Frame(outer, style="OptionsChatSurface.TFrame")
        transcript_frame.grid(row=2, column=0, sticky=tk.NSEW)
        transcript_frame.grid_rowconfigure(0, weight=1)
        transcript_frame.grid_columnconfigure(0, weight=1)
        transcript = tk.Text(
            transcript_frame,
            background=SURFACE,
            foreground=TEXT,
            selectbackground=ACCENT,
            selectforeground="#ffffff",
            insertbackground=TEXT,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            padx=12,
            pady=10,
            wrap=tk.WORD,
            font=("Segoe UI", 10),
            state=tk.DISABLED,
        )
        scrollbar = ttk.Scrollbar(
            transcript_frame,
            orient=tk.VERTICAL,
            command=transcript.yview,
        )
        transcript.configure(yscrollcommand=scrollbar.set)
        transcript.grid(row=0, column=0, sticky=tk.NSEW)
        scrollbar.grid(row=0, column=1, sticky=tk.NS)
        self._transcript = transcript
        self._configure_transcript_tags(transcript)

        composer = ttk.Frame(outer, style="OptionsChatPage.TFrame")
        composer.grid(row=3, column=0, sticky=tk.EW, pady=(9, 0))
        composer.grid_columnconfigure(0, weight=1)
        message_input = tk.Text(
            composer,
            height=4,
            background=FIELD_BACKGROUND,
            foreground=TEXT,
            selectbackground=ACCENT,
            selectforeground="#ffffff",
            insertbackground=TEXT,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            padx=9,
            pady=7,
            wrap=tk.WORD,
            font=("Segoe UI", 10),
        )
        message_input.grid(row=0, column=0, rowspan=2, sticky=tk.EW)
        message_input.bind("<Control-Return>", self._send_from_event)
        self._input = message_input
        send_button = ttk.Button(
            composer,
            text="Send",
            style="OptionsChatSend.TButton",
            command=self._send,
        )
        send_button.grid(row=0, column=1, sticky=tk.NSEW, padx=(8, 0))
        self._send_button = send_button
        ttk.Label(
            composer,
            text="Ctrl+Enter",
            style="OptionsChatMuted.TLabel",
        ).grid(row=1, column=1, sticky=tk.N, padx=(8, 0), pady=(4, 0))
        ttk.Label(
            outer,
            textvariable=self._status,
            style="OptionsChatMuted.TLabel",
        ).grid(row=4, column=0, sticky=tk.W, pady=(6, 0))

        self._render_history()
        self.refresh_context_label()
        self._set_busy(self._busy)
        window.lift()
        message_input.focus_set()

    def refresh_context_label(self) -> None:
        label = self._context_label
        if label is None:
            return
        try:
            context = self._context_provider(self._entry_point)
            summary = options_context_summary(context)
        except Exception:
            summary = "Live context will be read when you send your next message."
        try:
            label.configure(text=summary)
        except tk.TclError:
            self._context_label = None

    def _apply_styles(self) -> None:
        style = ttk.Style(self.root)
        style.configure("OptionsChatPage.TFrame", background=BACKGROUND)
        style.configure("OptionsChatSurface.TFrame", background=SURFACE)
        style.configure(
            "OptionsChatTitle.TLabel",
            background=BACKGROUND,
            foreground=TEXT,
            font=("Segoe UI", 15, "bold"),
        )
        style.configure(
            "OptionsChatMuted.TLabel",
            background=BACKGROUND,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 9),
        )
        style.configure(
            "OptionsChatSecondary.TButton",
            background=SURFACE_ALT,
            foreground=TEXT,
            bordercolor=BORDER,
            padding=(10, 6),
        )
        style.configure(
            "OptionsChatSend.TButton",
            background=ACCENT,
            foreground="#ffffff",
            bordercolor=ACCENT,
            font=("Segoe UI", 10, "bold"),
            padding=(14, 9),
        )
        style.map(
            "OptionsChatSend.TButton",
            background=[("active", "#42a5f5"), ("disabled", SURFACE_ALT)],
            foreground=[("disabled", MUTED_TEXT)],
        )

    def _place_window(self, window: tk.Toplevel, *, owner: tk.Misc) -> None:
        try:
            owner.update_idletasks()
            screen_width = window.winfo_screenwidth()
            screen_height = window.winfo_screenheight()
            width = max(560, min(720, screen_width - 80))
            height = max(620, min(820, screen_height - 80))
            owner_x = owner.winfo_rootx()
            owner_y = owner.winfo_rooty()
            owner_width = owner.winfo_width()
            right_of_owner = owner_x + owner_width + 12
            left_of_owner = owner_x - width - 12
            if right_of_owner + width <= screen_width - 20:
                x = right_of_owner
            elif left_of_owner >= 20:
                x = left_of_owner
            else:
                x = max(20, (screen_width - width) // 2)
            y = max(20, min(screen_height - height - 20, owner_y + 18))
            window.geometry(f"{width}x{height}+{x}+{y}")
        except tk.TclError:
            window.geometry("640x720")

    @staticmethod
    def _configure_transcript_tags(transcript: tk.Text) -> None:
        transcript.tag_configure(
            "intro",
            foreground=MUTED_TEXT,
            font=("Segoe UI", 10),
            spacing3=14,
        )
        transcript.tag_configure(
            "user_header",
            foreground=ACCENT,
            font=("Segoe UI", 9, "bold"),
            spacing1=8,
            spacing3=3,
        )
        transcript.tag_configure(
            "assistant_header",
            foreground=SUCCESS,
            font=("Segoe UI", 9, "bold"),
            spacing1=8,
            spacing3=3,
        )
        transcript.tag_configure(
            "message",
            foreground=TEXT,
            font=("Segoe UI", 10),
            spacing3=12,
        )
        transcript.tag_configure(
            "error",
            foreground=DANGER,
            font=("Segoe UI", 9, "bold"),
            spacing1=6,
            spacing3=10,
        )

    def _render_history(self) -> None:
        transcript = self._transcript
        if transcript is None:
            return
        try:
            transcript.configure(state=tk.NORMAL)
            transcript.delete("1.0", tk.END)
            transcript.insert(
                tk.END,
                (
                    "Ask about the selected strategy, a screen term, sizing, payoff, "
                    "or risk. I re-read the live Duckets context every time you send.\n"
                ),
                "intro",
            )
            for message in self._messages:
                self._insert_message(transcript, message)
            transcript.configure(state=tk.DISABLED)
            transcript.see(tk.END)
        except tk.TclError:
            self._transcript = None

    @staticmethod
    def _insert_message(transcript: tk.Text, message: OptionsChatMessage) -> None:
        if message.role == "user":
            transcript.insert(tk.END, "YOU\n", "user_header")
        else:
            transcript.insert(tk.END, "OPTIONS DESK\n", "assistant_header")
        transcript.insert(tk.END, f"{message.content}\n", "message")

    def _append_message(self, message: OptionsChatMessage) -> None:
        transcript = self._transcript
        if transcript is None:
            return
        try:
            transcript.configure(state=tk.NORMAL)
            self._insert_message(transcript, message)
            transcript.configure(state=tk.DISABLED)
            transcript.see(tk.END)
        except tk.TclError:
            self._transcript = None

    def _insert_error(self, message: str) -> None:
        transcript = self._transcript
        if transcript is None:
            return
        try:
            transcript.configure(state=tk.NORMAL)
            transcript.insert(tk.END, f"REQUEST FAILED\n{message}\n", "error")
            transcript.configure(state=tk.DISABLED)
            transcript.see(tk.END)
        except tk.TclError:
            self._transcript = None

    def _send_from_event(self, _event: object) -> str:
        self._send()
        return "break"

    def _send(self) -> None:
        if self._busy or self._input is None:
            return
        try:
            message = self._input.get("1.0", tk.END).strip()
        except tk.TclError:
            return
        if not message:
            self._status.set("Type a message first.")
            return
        try:
            context = dict(self._context_provider(self._entry_point))
        except Exception as exc:
            self._status.set("Current Duckets context could not be assembled.")
            self._insert_error(str(exc) or type(exc).__name__)
            return
        if self._context_label is not None:
            try:
                self._context_label.configure(text=options_context_summary(context))
            except tk.TclError:
                self._context_label = None

        history = tuple(self._messages)
        user_message = OptionsChatMessage(role="user", content=message)
        self._messages.append(user_message)
        self._append_message(user_message)
        try:
            self._input.delete("1.0", tk.END)
        except tk.TclError:
            pass
        self._set_busy(True)
        self._status.set("Analyzing current Duckets context…")

        def request_reply() -> str:
            if self._service is None:
                self._service = self._service_factory()
            return self._service.reply(
                message,
                history=history,
                context=context,
            )

        run_in_background(
            self.root,
            request_reply,
            self._reply_succeeded,
            self._reply_failed,
        )

    def _reply_succeeded(self, answer: str) -> None:
        assistant_message = OptionsChatMessage(role="assistant", content=answer)
        self._messages.append(assistant_message)
        self._append_message(assistant_message)
        self._set_busy(False)
        self._status.set("Ready — context refreshes on every message.")

    def _reply_failed(self, exc: Exception) -> None:
        self._set_busy(False)
        message = _chat_error_message(exc)
        self._status.set(message)
        self._insert_error(message)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        if self._send_button is not None:
            try:
                self._send_button.configure(state=state)
            except tk.TclError:
                self._send_button = None
        if self._input is not None:
            try:
                self._input.configure(state=state)
            except tk.TclError:
                self._input = None

    def _new_chat(self) -> None:
        if self._busy:
            self._status.set("Wait for the current response before starting a new chat.")
            return
        self._messages.clear()
        self._render_history()
        self._status.set("New conversation — live screen context is still attached.")
        if self._input is not None:
            try:
                self._input.focus_set()
            except tk.TclError:
                pass

    def _close(self) -> None:
        window = self._window
        self._reset_widgets()
        if window is not None:
            try:
                window.destroy()
            except tk.TclError:
                pass

    def _reset_widgets(self) -> None:
        self._window = None
        self._transcript = None
        self._input = None
        self._send_button = None
        self._context_label = None


def _portfolio_context(snapshot: object | None) -> dict[str, object] | None:
    if snapshot is None:
        return None
    facts = getattr(snapshot, "account_facts", {})
    facts = facts if isinstance(facts, Mapping) else {}
    account_values = facts.get("account_values")
    account_values = account_values if isinstance(account_values, Mapping) else {}
    positions = facts.get("positions")
    positions = positions if isinstance(positions, Mapping) else {}
    working = facts.get("working_orders")
    working = working if isinstance(working, Mapping) else {}

    cash = getattr(snapshot, "cash", ())
    holdings = getattr(snapshot, "holdings", ())
    return {
        "account_label": str(getattr(snapshot, "account_label", "Schwab")),
        "status": str(getattr(snapshot, "status", "")),
        "synced_at": _json_safe(getattr(snapshot, "synced_at", None)),
        "cash_value": _json_safe(getattr(snapshot, "cash_value", None)),
        "holdings_value": _json_safe(getattr(snapshot, "holdings_value", None)),
        "total_value": _json_safe(getattr(snapshot, "total_value", None)),
        "unrealized_pnl": _json_safe(getattr(snapshot, "unrealized_pnl", None)),
        "day_pnl": _json_safe(getattr(snapshot, "day_pnl", None)),
        "account_values": _select_mapping(account_values, _ACCOUNT_VALUE_FIELDS),
        "cash_buckets": [_object_fields(item) for item in _sequence(cash)],
        "holdings": [_object_fields(item) for item in _sequence(holdings)],
        "normalized_positions_status": positions.get("status"),
        "normalized_positions_complete": positions.get("option_row_set_complete"),
        "normalized_positions": [
            _select_mapping(item, _POSITION_FIELDS)
            for item in _mapping_sequence(positions.get("items"))
        ],
        "working_orders_status": working.get("status"),
        "working_orders_complete": working.get("option_row_set_complete"),
        "working_orders": [
            _select_mapping(item, _WORKING_ORDER_FIELDS)
            for item in _mapping_sequence(working.get("items"))
        ],
    }


def _candidate_summary(candidate: object) -> dict[str, object]:
    return {
        "rank": _json_safe(getattr(candidate, "rank", None)),
        "strategy": str(getattr(candidate, "strategy_display_name", "")),
        "exact_legs": str(getattr(candidate, "exact_legs", "")),
        "direction_probability_up": _json_safe(
            getattr(candidate, "direction_probability_up", None)
        ),
        "ml_profit_probability": _json_safe(
            getattr(candidate, "predictive_score", None)
        ),
        "scenario_coverage": _json_safe(
            getattr(candidate, "scenario_coverage", None)
        ),
        "expected_net_profit": _json_safe(
            getattr(candidate, "expected_net_profit", None)
        ),
        "expected_return": _json_safe(getattr(candidate, "expected_return", None)),
        "portfolio_fit": _portfolio_fit_context(
            getattr(candidate, "portfolio_fit", None)
        ),
        "score_basis": str(getattr(candidate, "score_basis", "")),
        "pricing_summary": str(getattr(candidate, "pricing_summary", "")),
        "quality_warning": str(getattr(candidate, "quality_warning", "")),
        "manual_order_actionable": bool(
            getattr(candidate, "manual_order_actionable", False)
        ),
    }


def _selected_candidate_context(
    candidate: object | None,
    *,
    order_index: int,
) -> dict[str, object] | None:
    if candidate is None:
        return None
    result = _candidate_summary(candidate)
    result.update(
        {
            "candidate_id": str(getattr(candidate, "candidate_id", "")),
            "symbol": str(getattr(candidate, "symbol", "")),
            "horizon": str(getattr(candidate, "horizon", "")),
            "horizon_label": str(getattr(candidate, "horizon_label", "")),
            "model_summary": str(getattr(candidate, "model_summary", "")),
            "manual_actionability": str(
                getattr(candidate, "manual_actionability", "")
            ),
            "position_context": _object_fields(
                getattr(candidate, "position", None)
            ),
            "published_snapshot": _select_mapping(
                getattr(candidate, "row", {}),
                _CANDIDATE_ROW_FIELDS,
            ),
            "order_draft": _order_draft_context(
                getattr(candidate, "order_draft", None),
                order_index=order_index,
            ),
        }
    )
    return result


def _order_draft_context(
    draft: object | None,
    *,
    order_index: int,
) -> dict[str, object] | None:
    if draft is None:
        return None
    orders = _sequence(getattr(draft, "orders", ()))
    selected = orders[order_index] if 0 <= order_index < len(orders) else None
    return {
        "uses_existing_shares": bool(getattr(draft, "uses_existing_shares", False)),
        "shares_required_per_strategy": _json_safe(
            getattr(draft, "shares_required_per_strategy", None)
        ),
        "shares_available": _json_safe(getattr(draft, "shares_available", None)),
        "strategy_legs": [
            _object_fields(leg) for leg in _sequence(getattr(draft, "legs", ()))
        ],
        "orders": [_object_fields(order) for order in orders],
        "selected_order_index": order_index,
        "selected_order": _object_fields(selected) if selected is not None else None,
    }


def _impact_context(
    impact: object | None,
    *,
    display: Mapping[str, object],
) -> dict[str, object]:
    if impact is None:
        calculated: object = None
    elif is_dataclass(impact):
        calculated = _json_safe(asdict(impact))
    else:
        calculated = _object_fields(impact)
    if isinstance(calculated, dict) and impact is not None:
        calculated["has_funds_shortfall"] = bool(
            getattr(impact, "has_funds_shortfall", False)
        )
        calculated["has_share_shortfall"] = bool(
            getattr(impact, "has_share_shortfall", False)
        )
    return {
        "display": _json_safe(dict(display)),
        "calculation": calculated,
    }


def _portfolio_fit_context(value: object | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "label": str(getattr(value, "label", "")),
        "detail": str(getattr(value, "detail", "")),
        "policy_version": str(getattr(value, "policy_version", "")),
    }


def _object_fields(value: object | None) -> dict[str, object]:
    if value is None:
        return {}
    if is_dataclass(value):
        converted = asdict(value)
        return _json_safe(converted)  # type: ignore[return-value]
    if isinstance(value, Mapping):
        return _json_safe(dict(value))  # type: ignore[return-value]
    attributes = getattr(value, "__dict__", {})
    if isinstance(attributes, Mapping):
        return _json_safe(
            {key: item for key, item in attributes.items() if not str(key).startswith("_")}
        )  # type: ignore[return-value]
    return {"value": _json_safe(value)}


def _select_mapping(
    value: object,
    fields: Sequence[str],
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {
        field: _json_safe(value.get(field))
        for field in fields
        if field in value
    }


def _mapping_sequence(value: object) -> list[Mapping[str, object]]:
    return [item for item in _sequence(value) if isinstance(item, Mapping)]


def _sequence(value: object) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _json_safe(value: object) -> Any:
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_json_safe(item) for item in value]
    converter = getattr(value, "to_pydatetime", None)
    if callable(converter):
        try:
            return _json_safe(converter())
        except (TypeError, ValueError):
            pass
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return isoformat()
        except (TypeError, ValueError):
            pass
    scalar = getattr(value, "item", None)
    if callable(scalar):
        try:
            return _json_safe(scalar())
        except (TypeError, ValueError):
            pass
    return str(value)


def _chat_error_message(exc: Exception) -> str:
    error_name = type(exc).__name__
    if error_name == "AuthenticationError":
        return "OpenAI rejected the configured OPENAI_API_KEY."
    if error_name == "RateLimitError":
        return "OpenAI rate or quota capacity is currently unavailable."
    if error_name in {"APIConnectionError", "APITimeoutError"}:
        return "The Options Desk could not reach OpenAI. Try again in a moment."
    if error_name in {"NotFoundError", "PermissionDeniedError"}:
        return "The configured OpenAI project cannot access the Options Desk model."
    detail = " ".join(str(exc).split())
    if not detail:
        detail = error_name
    return detail[:320]


__all__ = [
    "OptionsChatController",
    "build_options_chat_context",
    "options_context_summary",
]
