"""Synthetic Scout aggregate-report display; no operating sources are opened."""
from __future__ import annotations

import copy
import gc
import tkinter as tk
from tkinter import ttk
import weakref

import pytest

from app.ui.scout_stats_report import ScoutStatsReport
from app.ui.scout_stats_report_widgets import ScoutStatsReportPanel


def saved_report(session="2026-10-29"):
    """A small two-company aggregate, without forecast or individual order rows."""
    symbols = []
    for symbol, forecasts, scored, correct, missing, synthetic, brier, filled, buy, sell in (
            ("TEST", 7, 5, 3, 2, 0, .2, 2, 8, 4),
            ("DEMO", 5, 3, 2, 1, 1, .22666666666666666, 2, 5, 3)):
        symbols.append(dict(session_id=session, pc="Scout", symbol=symbol, forecasts=forecasts,
            scored=scored, correct=correct, missing_outcomes=missing, synthetic_outcomes=synthetic,
            other_unscored_outcomes=0, unscored_outcomes=missing + synthetic, late_forecasts=0,
            accuracy=correct / scored, brier=brier, scored_coverage=scored / forecasts,
            observed_not_down_fraction=.4, majority_baseline_accuracy=.6,
            extreme_probability_count=0, wrong_extreme_probability_count=0,
            submitted_quantity=12 if symbol == "TEST" else 9, filled_quantity=buy + sell,
            submission_unknown=1 if symbol == "TEST" else 0, realized_pnl=None,
            status="reviewed", trade_result_basis="saved_execution_projections_only",
            filled_orders=filled,
            execution_status_counts=({"FILLED": 2, "not_submitted": 4, "UNKNOWN": 1}
                if symbol == "TEST" else {"FILLED": 2, "not_submitted": 2, "CANCELLED": 1}),
            fill_details=dict(buy_quantity=buy, sell_quantity=sell, buy_notional=str(buy * 10),
                              sell_notional=str(sell * 10), fill_events=3 if symbol == "TEST" else 2),
            limitations=["Synthetic fixture; no raw execution rows are supplied."]))
    aggregate = dict(forecasts=12, scored=8, correct=5, missing_outcomes=3, synthetic_outcomes=1,
        other_unscored_outcomes=0, unscored_outcomes=4, late_forecasts=0,
        accuracy=.625, brier=.21, scored_coverage=8 / 12, observed_not_down_fraction=3 / 8,
        majority_baseline_accuracy=5 / 8, extreme_probability_count=0,
        wrong_extreme_probability_count=0, submitted_quantity=21, filled_quantity=20,
        submission_unknown=1, realized_pnl=None, unique_fill_events=5,
        buy_quantity=13, sell_quantity=7, buy_notional="130", sell_notional="70",
        execution_status_counts={"FILLED": 4, "CANCELLED": 1, "UNKNOWN": 1, "not_submitted": 6},
        execution_dispositions={"order_status_reconciled": 5, "submission_recovery_required": 1,
                                "no_purchasing_capacity": 4, "no_eligible_uncommitted_shares": 2},
        journal_request_states={"ACCEPTED": 5, "UNKNOWN": 1},
        confusion_matrix={"true_not_down": 2, "false_down": 1, "true_down": 3, "false_not_down": 2})
    return ScoutStatsReport(session=session, status="available",
        message="Synthetic Scout aggregate report; raw source rows are unavailable locally.",
        completion_id="synthetic-scout-report", generated_at=f"{session}T23:00:00+00:00",
        report_sha256="ab" * 32, aggregate=aggregate, symbols=tuple(symbols),
        target_definition="Third target close compared with reference open; equality is not-down.",
        limitations=("Raw source rows unavailable locally; this is a saved aggregate report.",
                     "Realized P&L is unknown; buy and sell notional measures activity only."))


@pytest.fixture(scope="module")
def tk_root():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk unavailable: {exc}")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def panel(tk_root):
    frame = ttk.Frame(tk_root)
    frame.pack(fill="both", expand=True)
    widget = ScoutStatsReportPanel(frame, tk_root)
    widget.frame.pack(fill="x")
    reference = weakref.ref(widget)
    try:
        yield weakref.proxy(widget)
    finally:
        frame.destroy()
        tk_root.withdraw()
        del widget, frame
        gc.collect()
        assert reference() is None, "Scout aggregate panel retained its destroyed widgets"


def table_rows(tree):
    return [tuple(str(value) for value in tree.item(item, "values")) for item in tree.get_children()]


def texts(widget):
    result = []
    try:
        result.append(str(widget.cget("text")))
    except tk.TclError:
        pass
    for child in widget.winfo_children():
        result.extend(texts(child))
    return result


def test_aggregate_scores_and_company_counts_display_without_constructing_forecasts(panel):
    snapshot = saved_report()
    before = copy.deepcopy(snapshot)
    panel.set_snapshot(snapshot)
    assert panel.snapshot is snapshot
    assert panel.accuracy_label.cget("text") == "62.5%"
    assert panel.brier_label.cget("text") == "0.210"
    assert "5 / 8" in panel.accuracy_caption.cget("text")
    coverage = panel.coverage_label.cget("text")
    for count in ("12", "8", "3", "1"):
        assert count in coverage
    rows = table_rows(panel.company_tree)
    assert len(rows) == 2
    assert {row[0] for row in rows} == {"TEST", "DEMO"}
    assert panel.snapshot == before
    assert not hasattr(snapshot, "forecasts") and not hasattr(snapshot, "orders")


def test_execution_shows_aggregate_counts_and_preserves_unknown_submissions(panel):
    panel.set_snapshot(saved_report())
    assert "6 saved order requests" in panel.order_total.cget("text")
    assert "5 accepted" in panel.order_total.cget("text")
    counts = panel.order_counts.cget("text").lower()
    assert "4 filled" in counts
    assert "1" in counts and "unknown" in counts
    assert "6" in counts
    coverage = panel.fill_coverage.cget("text")
    assert "20" in coverage and "21" in coverage
    rows = table_rows(panel.order_tree)
    assert len(rows) == 2
    assert {row[0] for row in rows} == {"TEST", "DEMO"}
    headings = [panel.order_tree.heading(column, "text").lower()
                for column in panel.order_tree.cget("columns")]
    assert not {"saved time", "side", "order id"}.intersection(headings)


def test_report_provenance_and_scope_do_not_claim_raw_order_or_profit_validation(panel):
    panel.set_snapshot(saved_report())
    content = "\n".join(texts(panel.frame)).lower()
    assert "report" in panel.source_label.cget("text").lower()
    assert "raw source rows" in content
    assert "profit" in content or "p&l" in content
    assert "unknown" in content or "not available" in content
    assert "synthetic-scout-report" in content
    assert "select an order" not in content


@pytest.mark.parametrize("snapshot", (None, ScoutStatsReport(),
    ScoutStatsReport(session="2026-10-29", status="unreadable", message="Report could not be verified.")))
def test_unavailable_report_clears_previous_values_and_does_not_show_zeros(panel, snapshot):
    panel.set_snapshot(saved_report())
    panel.set_snapshot(snapshot)
    assert panel.accuracy_label.cget("text") == "—"
    assert panel.brier_label.cget("text") == "—"
    assert panel.company_tree.get_children() == ()
    assert panel.order_tree.get_children() == ()
    assert "0 filled" not in panel.order_counts.cget("text").lower()
    if snapshot is not None and snapshot.status == "unreadable":
        assert "could not be verified" in panel.state_label.cget("text").lower()


def test_repeated_snapshot_replaces_company_aggregates_instead_of_adding(panel):
    snapshot = saved_report()
    panel.set_snapshot(snapshot)
    predictions = table_rows(panel.company_tree)
    orders = table_rows(panel.order_tree)
    panel.set_snapshot(snapshot)
    assert table_rows(panel.company_tree) == predictions
    assert table_rows(panel.order_tree) == orders
    assert len(panel.order_tree.get_children()) == 2


@pytest.mark.parametrize("width", (1280, 700))
def test_report_tables_and_responsive_columns_fit_available_width(panel, width):
    panel.root.geometry(f"{width}x900")
    panel.root.deiconify()
    panel.set_snapshot(saved_report())
    panel.root.update()
    right = panel.frame.winfo_rootx() + panel.frame.winfo_width()
    for widget in (panel.prediction_panel, panel.order_panel, panel.company_tree, panel.order_tree):
        assert widget.winfo_rootx() + widget.winfo_width() <= right + 2
    for tree in (panel.company_tree, panel.order_tree):
        bars = [child for child in tree.master.winfo_children() if isinstance(child, ttk.Scrollbar)]
        assert any(str(bar.cget("orient")) == "horizontal" and bar.winfo_ismapped() for bar in bars)
    assert panel.order_panel.grid_info()["row"] == (1 if width < 900 else 0)
