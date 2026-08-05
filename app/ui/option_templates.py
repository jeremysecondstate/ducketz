from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from tkinter import ttk

from app.models.option_management import SavedExitPlanTemplate, SavedRollTemplate
from app.services.option_exit_plans import (
    OCO_CAPABILITY_REASON,
    TRAILING_STOP_CAPABILITY_REASON,
    TWO_TARGET_CAPABILITY_REASON,
    load_exit_plan_templates,
)
from app.services.option_rolls import load_roll_templates
from app.ui.theme import BACKGROUND, BORDER, MUTED_TEXT, SURFACE, TEXT


@dataclass(frozen=True)
class OptionTemplateWorkspaceRow:
    category: str
    name: str
    configuration: str
    availability: str


def option_template_workspace_rows(
    *,
    roll_templates: Sequence[SavedRollTemplate] = (),
    exit_templates: Sequence[SavedExitPlanTemplate] = (),
) -> tuple[OptionTemplateWorkspaceRow, ...]:
    rows = [
        OptionTemplateWorkspaceRow(
            "Built-in exit",
            "Target + stop",
            "Profit target plus stop-limit branch",
            f"Review only — {OCO_CAPABILITY_REASON}",
        ),
        OptionTemplateWorkspaceRow(
            "Built-in exit",
            "Single target",
            "One GTC exact-leg limit close",
            "Placeable through universal review",
        ),
        OptionTemplateWorkspaceRow(
            "Built-in exit",
            "2 targets",
            "Two-step scale out",
            f"Unavailable — {TWO_TARGET_CAPABILITY_REASON}",
        ),
        OptionTemplateWorkspaceRow(
            "Built-in exit",
            "Trailing stop",
            "Follow the position mark",
            f"Unavailable — {TRAILING_STOP_CAPABILITY_REASON}",
        ),
    ]
    rows.extend(
        OptionTemplateWorkspaceRow(
            "Saved roll",
            template.name,
            (
                f"+{template.days_forward} days • "
                f"{'keep widths' if template.keep_strike_widths else 'map strikes independently'} • "
                f"{template.duration} • {template.price_policy}"
            ),
            "Apply from a selected position's Roll workspace",
        )
        for template in roll_templates
    )
    rows.extend(
        OptionTemplateWorkspaceRow(
            "Saved exit",
            template.name,
            (
                f"{template.base_template_id.replace('_', ' ').title()} • "
                f"target {template.target_percent:g}% • stop {template.stop_percent:g}% • "
                f"offset ${template.limit_offset:.2f} • {template.duration}"
                + (
                    f" • timed {template.time_exit.sessions_before_expiration} session"
                    f"{'s' if template.time_exit.sessions_before_expiration != 1 else ''} "
                    f"before expiration, {template.time_exit.minutes_before_session_close} min before close"
                    if template.time_exit is not None
                    else ""
                )
            ),
            "Apply from a selected position's Exit Plan workspace",
        )
        for template in exit_templates
    )
    return tuple(rows)


class OptionsTemplatesView:
    def __init__(
        self,
        *,
        root: tk.Misc,
        parent: ttk.Frame,
        roll_loader: Callable[[], tuple[SavedRollTemplate, ...]] = load_roll_templates,
        exit_loader: Callable[[], tuple[SavedExitPlanTemplate, ...]] = load_exit_plan_templates,
    ) -> None:
        self.root = root
        self.roll_loader = roll_loader
        self.exit_loader = exit_loader
        self.status = tk.StringVar(master=root, value="Loading reusable option-management templates")
        self._build(parent)
        self.refresh()

    def _build(self, parent: ttk.Frame) -> None:
        outer = ttk.Frame(parent, padding=(10, 9), style="ManagementPage.TFrame")
        outer.pack(fill=tk.BOTH, expand=True)
        header = ttk.Frame(outer, padding=(12, 10), style="ManagementCard.TFrame")
        header.pack(fill=tk.X, pady=(0, 8))
        copy = ttk.Frame(header, style="ManagementCard.TFrame")
        copy.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(copy, text="Templates", style="ManagementSection.TLabel").pack(anchor=tk.W)
        ttk.Label(
            copy,
            text=(
                "Reusable policy defaults only. Templates never store account IDs, quantities, quotes, "
                "or executable OCC assumptions."
            ),
            style="ManagementMuted.TLabel",
            wraplength=780,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(2, 0))
        ttk.Button(header, text="Reload", command=self.refresh).pack(side=tk.RIGHT)

        surface = ttk.Frame(outer, padding=(10, 9), style="ManagementCard.TFrame")
        surface.pack(fill=tk.BOTH, expand=True)
        table = ttk.Treeview(
            surface,
            columns=("category", "name", "configuration", "availability"),
            show="headings",
            selectmode="browse",
        )
        for name, label, width, anchor in (
            ("category", "Type", 115, tk.W),
            ("name", "Template", 170, tk.W),
            ("configuration", "Stored configuration", 360, tk.W),
            ("availability", "Capability / how to apply", 460, tk.W),
        ):
            table.heading(name, text=label)
            table.column(name, width=width, anchor=anchor, stretch=name in {"configuration", "availability"})
        scroll_y = ttk.Scrollbar(surface, orient=tk.VERTICAL, command=table.yview)
        scroll_x = ttk.Scrollbar(surface, orient=tk.HORIZONTAL, command=table.xview)
        table.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        table.grid(row=0, column=0, sticky=tk.NSEW)
        scroll_y.grid(row=0, column=1, sticky=tk.NS)
        scroll_x.grid(row=1, column=0, sticky=tk.EW)
        surface.grid_rowconfigure(0, weight=1)
        surface.grid_columnconfigure(0, weight=1)
        self.table = table
        ttk.Label(outer, textvariable=self.status, style="ManagementMuted.TLabel").pack(
            anchor=tk.W, pady=(6, 0)
        )

    def refresh(self) -> None:
        try:
            rows = option_template_workspace_rows(
                roll_templates=self.roll_loader(),
                exit_templates=self.exit_loader(),
            )
        except Exception as exc:
            rows = option_template_workspace_rows()
            self.status.set(
                f"Saved templates unavailable ({type(exc).__name__}); built-in capabilities remain visible."
            )
        else:
            saved_count = sum(row.category.startswith("Saved") for row in rows)
            self.status.set(
                f"{len(rows)} template definitions • {saved_count} saved configuration"
                f"{'s' if saved_count != 1 else ''}"
            )
        for item in self.table.get_children():
            self.table.delete(item)
        for row in rows:
            self.table.insert(
                "",
                tk.END,
                values=(row.category, row.name, row.configuration, row.availability),
            )


__all__ = [
    "OptionTemplateWorkspaceRow",
    "OptionsTemplatesView",
    "option_template_workspace_rows",
]
