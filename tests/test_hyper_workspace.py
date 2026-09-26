from __future__ import annotations

import threading
import time
import traceback
import tkinter as tk
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from tkinter import ttk

import pytest

from app.ui.hyper_workspace import HyperWorkspace, MINT
from app.services.hyperliquid_paper_view import PaperViewSnapshot, SourceState
from visual_hyper_workspace_fixture import FixtureService, _snapshot


@pytest.fixture(scope="module")
def root():
    try:
        window = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk is unavailable: {exc}")
    window.withdraw()
    yield window
    try:
        window.destroy()
    except tk.TclError:
        pass


@pytest.fixture
def tk_callback_errors(root, monkeypatch):
    errors = []
    monkeypatch.setattr(root, "report_callback_exception", lambda *exc: errors.append(
        "".join(traceback.format_exception(*exc))))
    yield errors
    assert not errors, "Tk callback failed:\n" + "\n".join(errors)


@pytest.fixture
def workspace(root, monkeypatch, tk_callback_errors):
    # H.Y.P.E.R. is an observer. No view action may initialize a ledger, reach
    # the exchange or start a process, including merely switching to Powder.
    import subprocess
    import requests
    from ml.hyperliquid_paper_ledger import PaperLedger

    def forbidden(*args, **kwargs):
        pytest.fail("H.Y.P.E.R. attempted network, execution or a runtime mutation")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(PaperLedger, "__init__", forbidden)
    window = tk.Toplevel(root)
    window.geometry("1706x1000")
    parent = ttk.Frame(window)
    parent.pack(fill=tk.BOTH, expand=True)
    view = HyperWorkspace(window, parent, service=FixtureService(), auto_load=False)
    view.show_snapshot(_snapshot())
    window.update()
    yield view, window, parent
    try:
        window.destroy()
    except tk.TclError:
        pass


def _visible_rows(view):
    return [view._rows_by_id[item] for item in view.tree.get_children()]


def _pump_until(window, condition, *, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        window.update()
        if condition():
            return
        time.sleep(.01)
    assert condition(), "Tk did not receive the background read before timeout"


def test_headline_uses_opening_baseline_pnl_and_preserves_inherited_position(workspace):
    view, _, _ = workspace
    assert "42,112.86" in view.metric_values["equity"].cget("text")
    assert "34.58" in view.metric_values["pnl"].cget("text")
    assert "795.93" not in view.metric_values["pnl"].cget("text")
    assert not any(row.get("passive") for row in _visible_rows(view))
    view.include_passive.set(True)
    rows = _visible_rows(view)
    inherited = next(row for row in rows if row.get("passive"))
    assert inherited["account"] == "alex"
    assert inherited["kind"] == "spot"


def test_immutable_opening_stays_visible_beside_postfill_portfolio_and_inventory(workspace):
    view, window, _ = workspace
    snapshot = _snapshot()
    seed = {"timestamp_utc": "2026-09-26T11:30:32+00:00",
            "baseline_equity": {"alex": 5879.21, "jeremy": 6151.79, "clearpond": 30068.53},
            "cash": {"alex": 6200, "jeremy": 5500, "clearpond": 30000},
            "positions": [{"account": "alex", "coin": "HYPE", "kind": "perp", "quantity": -25,
                           "avg_entry": 45, "mark_price": 47, "risk_reference_price": 47,
                           "risk_reference_source": "opening_mark"}],
            "metadata": {"snapshot_not_atomic_across_accounts": True}}
    view.show_snapshot(replace(snapshot, seed=seed))
    text = view.opening_label.cget("text")
    assert "04:30:32 PT" in text and "$42,099.53" in text
    assert "Experiment P/L $0.00" in text and "Seed fees $0.00" in text
    assert "1 inherited position" in text
    assert "42,112.86" in view.metric_values["equity"].cget("text")
    assert "34.58" in view.metric_values["pnl"].cget("text")
    assert "export" in view.metric_titles["drawdown"].cget("text")
    assert view.metric_captions["drawdown"].cget("text").startswith("As of ")
    before = set(view.root.winfo_children())
    view.opening_button.invoke()
    window.update()
    dialog = next(child for child in view.root.winfo_children() if child not in before and isinstance(child, tk.Toplevel))
    try:
        def texts(widget):
            return ([widget.get("1.0", "end")] if isinstance(widget, tk.Text) else []) + [
                value for child in widget.winfo_children() for value in texts(child)]
        evidence = "\n".join(texts(dialog))
        assert "perp HYPE · -25 · 45 · 47 · 47 (opening_mark)" in evidence
        assert "Account reads were sequential" in evidence
        assert "IMMUTABLE SEED RECORD" in evidence
    finally:
        dialog.destroy()
    view.mode.set("Powder")
    assert not view.opening_panel.winfo_manager()
    view.mode.set("Paper")
    assert view.opening_panel.winfo_manager() == "pack"
    assert view.opening_label.cget("text") == text


def test_opening_does_not_invent_baseline_from_current_equity(workspace):
    view, _, _ = workspace
    view.show_snapshot(replace(_snapshot(), seed={}))
    assert view.opening_label.cget("text") == "Opening baseline unavailable"
    assert str(view.opening_button.cget("state")) == "disabled"


def test_zero_cost_opening_renders_before_strategy_fills(workspace):
    view, _, _ = workspace
    start = "2026-09-26T11:30:32+00:00"
    accounts = {key: {"equity": 1000, "initial_equity": 1000, "total_pnl": 0,
                      "free_cash": 1000, "gross_exposure": 0, "fees": 0} for key in ("alex", "jeremy", "clearpond")}
    pooled = {"equity": 3000, "initial_equity": 3000, "total_pnl": 0, "gross_exposure": 0, "fees": 0}
    view.show_snapshot(PaperViewSnapshot(observed_at_utc=start, portfolio_observed_at_utc=start,
        accounts=accounts, pooled=pooled,
        seed={"timestamp_utc": start, "baseline_equity": {key: 1000 for key in accounts}, "positions": []},
        equity_history=[{"account": "pooled", "timestamp_utc": start, "equity": 3000, "total_pnl": 0}],
        performance={"as_of_utc": start, "max_drawdown_fraction": 0},
        runtime={"status": "stopped", "stop_reason": "prepare_only", "lifecycle_phase": "opening_prepared"},
        sources={"paper": SourceState("Paper", "stopped")}))
    assert view.metric_values["equity"].cget("text") == "$3,000.00"
    assert view.metric_values["pnl"].cget("text") == "$0.00"
    assert view.metric_values["fees"].cget("text") == "$0.00"
    assert len(view._chart_series()) == 1
    assert not view.tree.get_children()
    assert "Opening prepared" in view.source_labels["paper"][0].cget("text")
    assert "Awaiting strategy start" in view.source_labels["paper"][1].cget("text")


def test_refresh_retains_filters_and_stable_row_selection(workspace):
    view, window, _ = workspace
    view.account_filter.set("Alex")
    view.asset_filter.set("HYPE")
    view.qualification_filter.set("All models")
    view.include_passive.set(True)
    view.chart_metric.set("Drawdown")
    view.chart_range.set("24H")
    view._render()
    rows = view.tree.get_children()
    assert len(rows) == 2
    selected = next(item for item in rows if view._rows_by_id[item]["kind"] == "perp")
    view.tree.selection_set(selected)
    view.tree.focus(selected)
    before = view._rows_by_id[selected]

    snapshot = _snapshot()
    updated = [{**row, "mark_price": row["mark_price"] + .01} for row in snapshot.positions]
    view.show_snapshot(replace(snapshot, positions=updated))
    window.update()

    assert view.account_filter.get() == "Alex"
    assert view.asset_filter.get() == "HYPE"
    assert view.qualification_filter.get() == "All models"
    assert view.include_passive.get()
    assert view.chart_metric.get() == "Drawdown"
    assert view.chart_range.get() == "24H"
    assert view.tree.selection() == (selected,)
    assert view._rows_by_id[selected]["account"] == before["account"]
    assert view._rows_by_id[selected]["mark_price"] == pytest.approx(before["mark_price"] + .01)


@pytest.mark.parametrize("journal", ["Decisions", "Fills"])
def test_account_asset_and_qualification_filters_apply_to_each_journal(workspace, journal):
    view, _, _ = workspace
    view.view.set(journal)
    view.account_filter.set("Clear Pond")
    view.asset_filter.set("ETH")
    view.qualification_filter.set("Research")
    view._render()
    rows = _visible_rows(view)
    assert len(rows) == 1
    assert rows[0]["account"] == "clearpond"
    assert rows[0]["coin"] == "ETH"
    assert rows[0]["qualified"] is False


def test_position_filters_do_not_invent_qualification_from_current_forecasts(workspace):
    view, _, _ = workspace
    view.account_filter.set("Jeremy")
    view.asset_filter.set("BTC")
    view._render()
    assert len(_visible_rows(view)) == 1
    assert _visible_rows(view)[0]["account"] == "jeremy"
    assert _visible_rows(view)[0]["coin"] == "BTC"
    view.qualification_filter.set("Qualified")
    view._render()
    # A current qualified BTC forecast does not establish the provenance of
    # inventory accumulated across earlier fills or inherited opening positions.
    assert not _visible_rows(view)


@pytest.mark.parametrize("journal", ["Decisions", "Fills"])
def test_partial_fill_inspector_distinguishes_requested_executed_and_unfilled(workspace, journal):
    view, window, _ = workspace
    view.view.set(journal)
    view.account_filter.set("Alex")
    view.asset_filter.set("HYPE")
    view._render()
    item = view.tree.get_children()[0]
    view.tree.selection_set(item)
    view.tree.event_generate("<<TreeviewSelect>>")
    window.update()
    # Inspect the user-facing explanation, rather than the raw JSON record.
    description = view.detail.get("1.0", "end").split("SAVED RECORD")[0]
    assert "SIMULATED EXECUTION" in description
    assert "Requested -4" in description
    assert "Executed -3.407" in description
    assert "Unfilled remainder -0.593" in description
    assert "Partial" in description


def test_hold_reason_is_explained_without_an_execution_claim(workspace):
    view, window, _ = workspace
    view.view.set("Decisions")
    view.account_filter.set("Clear Pond")
    view.asset_filter.set("ETH")
    view._render()
    item = view.tree.get_children()[0]
    view.tree.selection_set(item)
    view.tree.event_generate("<<TreeviewSelect>>")
    window.update()
    description = view.detail.get("1.0", "end").split("SAVED RECORD")[0]
    assert "Below entry threshold" in description
    assert "SIMULATED EXECUTION" not in description


def test_historical_nonfill_reasons_use_saved_facts_without_inventing_cooldown(workspace):
    view, window, _ = workspace
    snapshot = _snapshot()
    base = {"timestamp_utc": "2026-09-26T08:30:39+00:00", "account": "alex", "coin": "ZEC",
            "kind": "perp", "qualified": True, "qualification": "qualified", "model_id": "old-model",
            "forecast_id": "old-forecast", "reason": "signal_rebalance", "action": "hold",
            "current_notional": 0, "target_notional": 0}
    rows = [
        {**base, "decision_id": "deadband", "p_not_down": .503, "policy": {"reason": "entry_deadband"}},
        {**base, "decision_id": "precision", "account": "clearpond", "kind": "spot", "action": "skip",
         "p_not_down": .40, "current_notional": .08, "policy": {"reason": "entry_threshold_met"},
         "execution": {"reason": "below_size_precision", "status": "unfilled", "quantity": 0,
                       "requested_quantity": -.00005, "unfilled_quantity": -.00005}},
        {**base, "decision_id": "unrecorded", "p_not_down": .40,
         "policy": {"reason": "entry_threshold_met", "target_gross": 5157.51}},
    ]
    view.show_snapshot(replace(snapshot, decisions=rows, policy={"entry_band": .04, "exit_band": .02}))
    view.view.set("Decisions")
    view._render()
    assert view.tree.set("deadband", "reason") == "Below entry threshold"
    assert view.tree.set("precision", "reason") == "Below size precision"
    assert view.tree.set("unrecorded", "reason") == "Recorded target unchanged"
    view.tree.selection_set("precision")
    view.tree.event_generate("<<TreeviewSelect>>")
    window.update()
    assert "Complete exits bypass the ordinary rebalance threshold" in view.detail.get("1.0", "end")
    view.tree.selection_set("unrecorded")
    view.tree.event_generate("<<TreeviewSelect>>")
    window.update()
    description = view.detail.get("1.0", "end").split("SAVED RECORD")[0]
    assert "Detailed checks were not recorded" in description
    assert "Saved sizing reason: Entry threshold met" in description
    assert "Stop cooldown" not in description and "Cooldown until" not in description
    assert "Complete exits bypass" not in description
    assert all(value not in description for value in ("55.00%", "54.00%", "46.00%", "52.00%"))
    # A skipped zero fill stays a decision; the actual fill journal is unchanged.
    view.view.set("Fills")
    view._render()
    assert len(view.tree.get_children()) == len(snapshot.fills)
    assert {row["fill_id"] for row in _visible_rows(view)} == {row["fill_id"] for row in snapshot.fills}


@pytest.mark.parametrize("cooldown", [False, True])
@pytest.mark.parametrize("long_entry,short_entry,entry_text", [
    (.55, .45, "long ≥ 55.00%; short ≤ 45.00%"),
    (.54, .46, "long ≥ 54.00%; short ≤ 46.00%"),
])
def test_saved_decision_checks_explain_thresholds_roles_and_cooldown_at_decision(workspace, cooldown, long_entry, short_entry, entry_text):
    view, window, _ = workspace
    checks = {"policy_reason": "entry_threshold_met", "entry_probability_long": long_entry,
              "entry_probability_short": short_entry, "exit_probability_long": .52, "exit_probability_short": .48,
              "minimum_trade_notional": 25, "rebalance_min_delta_fraction": .10,
              "rebalance_threshold_notional": 25, "delta_notional": 0 if cooldown else -15,
              "venue_minimum_fill_notional": 10, "proposed_target_notional": -100,
              "account_capacity_notional": 800, "available_cash": 900, "account_role": "short_perp",
              "strategy_direction": "short", "cooldown_until_utc": "2026-09-26T09:00:00+00:00" if cooldown else None,
              "cooldown_remaining_seconds": 1800 if cooldown else 0, "cooldown_blocks_target": cooldown,
              "rebalance_required": False, "rebalance_forced": False}
    row = {"decision_id": "checked-hold", "timestamp_utc": "2026-09-26T08:30:00+00:00",
           "account": "alex", "coin": "ZEC", "kind": "perp", "action": "hold",
           "reason": "stop_cooldown" if cooldown else "below_rebalance_threshold",
           "current_notional": 0 if cooldown else -85, "target_notional": 0 if cooldown else -100,
           "p_not_down": .40, "qualified": True, "qualification": "qualified",
           "model_id": "recorded-model", "forecast_id": "recorded-forecast", "decision_checks": checks}
    # Today's policy must never replace the policy recorded on an older row.
    view.show_snapshot(replace(_snapshot(), decisions=[row], policy={"entry_band": .08, "exit_band": .03}))
    view.view.set("Decisions")
    view._render()
    window.update()
    assert view.tree.set("checked-hold", "qualification") == "Qualified"
    assert view.tree.set("checked-hold", "action") == "Hold"
    assert view.tree.set("checked-hold", "reason") == ("Stop cooldown" if cooldown else "Below rebalance threshold")
    description = view.detail.get("1.0", "end").split("SAVED RECORD")[0]
    assert "Qualified describes model validation" in description
    assert "Account role: Short perps · signed target ≤ 0" in description
    assert "Eligible signal entry P(not-down): " + entry_text in description
    assert "Eligible signal retention: long > 52.00%; short < 48.00%" in description
    assert "58.00%" not in description and "42.00%" not in description and "53.00%" not in description
    assert "max($25.00 trade minimum, 10.00% of absolute target)" in description
    assert "Venue minimum applies to the actual fill: $10.00" in description
    assert "SIMULATED EXECUTION" not in description
    if cooldown:
        assert "1,800 seconds remaining at decision" in description
        assert "Cooldown blocks the proposed target" in description
    else:
        assert "Signed target delta −$15.00" in description
        assert "No active cooldown at decision" in description


@pytest.mark.parametrize("journal", ["Decisions", "Fills"])
@pytest.mark.parametrize("reason,model_label,reason_label", [
    ("qualified_forecast_unavailable", "No forecast", "Qualified forecast unavailable"),
    ("stop_loss", "Risk exit", "Stop loss"),
])
def test_paper_journals_explain_missing_forecast_attribution(workspace, journal, reason, model_label, reason_label):
    view, window, _ = workspace
    snapshot = _snapshot()
    source = getattr(snapshot, journal.lower())
    row = {**source[0], "model_id": None, "forecast_id": None, "prediction_id": None,
           "qualified": None, "qualification": "unavailable", "reason": reason,
           "p_not_down": None, "details": {}}
    view.show_snapshot(replace(snapshot, **{journal.lower(): [row]}))
    view.view.set(journal)
    view._render()
    item = view.tree.get_children()[0]
    assert view.tree.set(item, "qualification") == model_label
    assert view.tree.heading("reason", "text") == "Reason"
    assert view.tree.set(item, "reason") == reason_label
    view.tree.selection_set(item)
    view.tree.event_generate("<<TreeviewSelect>>")
    window.update()
    description = view.detail.get("1.0", "end").split("SAVED RECORD")[0]
    assert "No forecast attributed to this record" in description
    assert "Unavailable model" not in description
    assert view._rows_by_id[item]["qualification"] == "unavailable"
    view.qualification_filter.set("Qualified")
    assert not view.tree.get_children()


@pytest.mark.parametrize("recipe,holds", [("direction-volatility-v1", False),
                                         ("direction-volatility-v2-qualified-hold", True)])
def test_qualified_policy_hold_text_matches_recorded_recipe(workspace, recipe, holds):
    view, _, _ = workspace
    snapshot = _snapshot()
    view.show_snapshot(replace(snapshot, policy={**snapshot.policy, "require_qualified_forecasts": True,
                                                 "recipe_version": recipe}))
    text = view.policy_label.cget("text")
    assert ("Hold without an eligible signal; risk checks continue." in text) is holds
    assert ("Only qualified signals." if holds else "Only qualified forecasts participate.") in text


def test_policy_footer_uses_saved_entry_band_and_complete_exit_check_remains_visible(workspace):
    view, window, _ = workspace
    row = {"decision_id": "guarded-exit", "timestamp_utc": "2026-09-26T08:30:00+00:00",
           "account": "jeremy", "coin": "HYPE", "kind": "perp", "action": "skip",
           "reason": "below_min_notional", "current_notional": 5, "target_notional": 0,
           "decision_checks": {"rebalance_forced": False, "rebalance_required": True,
                               "minimum_trade_notional": 25, "rebalance_threshold_notional": 25,
                               "rebalance_min_delta_fraction": .1, "venue_minimum_fill_notional": 10}}
    blocked = {**row, "decision_id": "blocked-increase", "account": "alex", "action": "hold",
               "current_notional": 0, "target_notional": 0, "reason": "opposing_reduction_unavailable",
               "decision_checks": {}}
    snapshot = replace(_snapshot(), decisions=[row, blocked], policy={"entry_band": .04})
    view.show_snapshot(snapshot)
    assert "long ≥ 54.00% · short ≤ 46.00%" in view.policy_label.cget("text")
    view.view.set("Decisions")
    view._render()
    window.update()
    assert view.tree.set("blocked-increase", "reason") == "Opposing exit has no executable book"
    assert view.tree.set("guarded-exit", "reason") == "Below venue minimum"
    description = view.detail.get("1.0", "end").split("SAVED RECORD")[0]
    assert "Complete exits bypass the ordinary rebalance threshold" in description
    assert "venue size and minimum still apply" in description
    assert "Risk reduction bypasses" not in description
    assert "54.00%" not in description and "46.00%" not in description
    view.show_snapshot(replace(snapshot, policy={"entry_band": .05}))
    assert "long ≥ 55.00% · short ≤ 45.00%" in view.policy_label.cget("text")


@pytest.mark.parametrize("account,key,expected_count", [("Alex", "alex", 2), ("Jeremy", "jeremy", 1)])
def test_transfer_account_filter_includes_both_sides(workspace, account, key, expected_count):
    view, _, _ = workspace
    view.view.set("Transfers")
    view.account_filter.set(account)
    view._render()
    rows = _visible_rows(view)
    assert len(rows) == expected_count
    assert all(key in (row["from_account"], row["to_account"]) for row in rows)


def test_powder_has_no_paper_balances_or_journal_and_labels_forecast_preview(workspace):
    view, window, _ = workspace
    view.include_passive.set(True)
    selected = view.tree.get_children()[0]
    view.tree.selection_set(selected)
    view.tree.event_generate("<<TreeviewSelect>>")
    window.update()
    assert "Alex" in view.detail.get("1.0", "end")
    view.mode.set("Powder")
    view._choose_mode()
    window.update()
    for journal in ("Positions", "Decisions", "Fills", "Transfers"):
        view.view.set(journal)
        view._render()
        assert not view.tree.get_children()
    for label in view.metric_values.values():
        assert not any(character.isdigit() for character in label.cget("text"))
    for widgets in view.account_values.values():
        for key in ("equity", "pnl", "free_cash", "gross_exposure"):
            assert not any(character.isdigit() for character in widgets[key].cget("text"))
    assert view.badge.cget("text") == "REAL MONEY"
    assert view.mode_status.cget("text") == "Not connected"
    assert not view._chart_points
    assert "Not connected" in view.detail.get("1.0", "end")
    assert "Alex" not in view.detail.get("1.0", "end")
    caption = view.forecast_caption.cget("text").casefold()
    assert "shared" in caption
    assert "preview" in caption
    view.mode.set("Paper")
    view._choose_mode()
    view.view.set("Positions")
    view._render()
    assert len(view.tree.get_children()) == len(_snapshot().positions)


def test_powder_observations_and_fills_are_separate_from_paper(workspace):
    from app.services.hyperliquid_powder_view import project_powder
    view, window, _ = workspace
    paper = _snapshot()
    paper.powder = project_powder({"latest_observation": {"observed_at": 1000,
        "accounts": {"clearpond": {"equity": 800, "available_cash": 600, "gross": 200,
            "positions": {"HYPE": {"quantity": 5, "mark": 40, "kind": "spot"}}}}},
        "fills": [{"account": "clearpond", "symbol": "HYPE", "tid": 7, "quantity": 1,
                   "price": 40, "fee": .001, "fee_token": "HYPE", "time": 1000000}]},
        paper=paper, now=1001)
    view.show_snapshot(paper)
    view.mode.set("Powder")
    window.update()
    assert view.metric_values["equity"].cget("text") == "$800.00"
    assert view.metric_values["pnl"].cget("text") == "—"
    assert len(view.tree.get_children()) == 1
    view.view.set("Fills")
    view._render()
    assert "0.001 HYPE" in view.tree.item(view.tree.get_children()[0], "values")
    assert "reason" not in view.tree["columns"]
    assert view.tree.set(view.tree.get_children()[0], "qualification") == "Unavailable"
    assert "Not connected" not in view.detail.get("1.0", "end")
    view.mode.set("Paper")
    assert "42,112.86" in view.metric_values["equity"].cget("text")


def test_missing_and_degraded_sources_remain_visible_without_fabricated_balances(workspace):
    view, _, _ = workspace
    view.show_snapshot(PaperViewSnapshot(
        observed_at_utc="2026-09-26T03:30:12+00:00",
        sources={"ledger": SourceState("Ledger", "missing"),
                 "paper": SourceState("Paper", "stale", "2026-09-26T00:00:00+00:00", 12612, 30),
                 "models": SourceState("Models", "stopped", "2026-09-26T02:00:00+00:00", 5412, 5),
                 "forecast:ETH": SourceState("ETH forecast", "partial")},
        warnings=("Paper ledger is missing; no balances have been inferred",),
    ))
    assert "missing" in view.alert.cget("text").lower()
    assert "stale" in view.source_labels["paper"][0].cget("text").lower()
    assert "stopped" in view.source_labels["models"][0].cget("text").lower()
    assert "partial" in view.source_labels["forecasts"][0].cget("text").lower()
    assert all(label.cget("text") == "—" for label in view.metric_values.values())
    assert not view.tree.get_children()


def test_compact_layout_keeps_panels_reachable_without_horizontal_clipping(workspace):
    view, window, _ = workspace
    assert view._layout_mode == "wide"
    window.geometry("1180x760")
    window.update()
    assert view._layout_mode == "compact"
    assert view.body.winfo_height() > view.canvas.winfo_height()
    for panel in (view.accounts_rail, view.center, view.right):
        assert panel.winfo_x() >= 0
        assert panel.winfo_x() + panel.winfo_width() <= view.body.winfo_width() + 2
    view.canvas.yview_moveto(1)
    window.update_idletasks()
    assert view.canvas.yview()[1] == pytest.approx(1)


def test_resizing_repeatedly_between_layouts_keeps_right_panels_visible(workspace):
    view, window, _ = workspace
    for width, mode in [(1180, "compact"), (1706, "wide"), (1180, "compact"),
                        (850, "narrow"), (1180, "compact"), (1706, "wide")] * 2:
        window.geometry(f"{width}x760")
        window.update()
        assert view._layout_mode == mode
        for panel in (view.forecast_card, view.detail_card, view.policy_label):
            assert panel.winfo_ismapped()
            assert panel.winfo_x() >= 0
            assert panel.winfo_x() + panel.winfo_width() <= view.right.winfo_width() + 2
        forecast, detail = view.forecast_card, view.detail_card
        if mode == "compact":
            assert detail.winfo_x() >= forecast.winfo_x() + forecast.winfo_width()
        else:
            assert detail.winfo_y() >= forecast.winfo_y() + forecast.winfo_height()


def test_resize_tolerates_view_destruction_during_idle_layout(workspace):
    view, window, parent = workspace
    event = tk.Event()
    event.width, event.height = 1180, 760
    window.after_idle(parent.destroy)
    view._resize(event)
    assert view._closed
    assert not view._jobs
    # A previously queued resize must also leave the destroyed widgets alone.
    view._resize(event)
    window.update()


def test_drawdown_chart_uses_transfer_adjusted_presampling_drawdown(workspace):
    view, _, _ = workspace
    snapshot = _snapshot()
    # The outgoing transfer reduces marked account equity by $100. A prior
    # performance peak omitted by downsampling still makes the true drawdown
    # 2.5%; neither raw equity nor the two visible P/L points recover that.
    history = [{"account": "alex", "timestamp_utc": "2026-09-26T02:30:00+00:00",
                "equity": 1000, "total_pnl": 0, "drawdown_fraction": 0},
               {"account": "alex", "timestamp_utc": "2026-09-26T03:30:00+00:00",
                "equity": 900, "total_pnl": 0, "drawdown_fraction": -.025}]
    view.show_snapshot(replace(snapshot, equity_history=history, history_sampled=True))
    view.account_filter.set("Alex")
    view.chart_metric.set("Drawdown")
    assert [value for _, value, _ in view._chart_series()] == [0, -2.5]
    view.chart_metric.set("P/L")
    assert [value for _, value, _ in view._chart_series()] == [0, 0]


def test_chart_sampling_keeps_recorded_opening_before_immediate_fills(workspace):
    view, _, _ = workspace
    start = datetime(2026, 9, 26, 3, 0, tzinfo=timezone.utc)
    history = [{"account": "pooled", "timestamp_utc": (start + timedelta(milliseconds=i)).isoformat(),
                "equity": 3000-i, "total_pnl": -i} for i in range(400)]
    view.show_snapshot(replace(_snapshot(), equity_history=history,
                              portfolio_observed_at_utc=history[-1]["timestamp_utc"]))
    view.chart_range.set("All")
    view.chart_metric.set("P/L")
    series = view._chart_series()
    assert len(series) <= 350
    assert series[0][2] == history[0]
    assert series[0][1] == 0
    assert series[-1][2] == history[-1]


class BlockingService:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.thread_ids = []

    def load_snapshot(self):
        self.calls += 1
        self.thread_ids.append(threading.get_ident())
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("Test did not release the read worker")
        return _snapshot()


def test_refresh_is_bounded_and_applies_results_on_tk_thread(root):
    window = tk.Toplevel(root)
    parent = ttk.Frame(window)
    parent.pack(fill=tk.BOTH, expand=True)
    service = BlockingService()
    view = HyperWorkspace(window, parent, service=service, auto_load=False, refresh_ms=60_000)
    rendered_on = []
    original_show = view.show_snapshot

    def record_show(snapshot):
        rendered_on.append(threading.get_ident())
        original_show(snapshot)

    view.show_snapshot = record_show
    try:
        view.refresh()
        assert service.started.wait(timeout=1)
        assert view._loading
        for _ in range(8):
            view.refresh()
        window.update()
        assert service.calls == 1
        assert not rendered_on
        service.release.set()
        _pump_until(window, lambda: not view._loading)
        assert rendered_on == [threading.get_ident()]
        assert service.thread_ids[0] != threading.get_ident()
        view.refresh()
        _pump_until(window, lambda: service.calls == 2 and not view._loading)
        assert len(rendered_on) == 2
    finally:
        service.release.set()
        window.destroy()


def test_failed_refresh_preserves_last_observation_and_clears_loading(workspace):
    view, window, _ = workspace

    class UnavailableService:
        def load_snapshot(self):
            raise OSError("Sample ledger unavailable during refresh")

    # Use a dedicated view rather than replacing its private service field.
    frame = ttk.Frame(window)
    frame.pack(fill=tk.BOTH, expand=True)
    failing = HyperWorkspace(window, frame, service=UnavailableService(), auto_load=False)
    failing.show_snapshot(_snapshot())
    previous_rows = failing.tree.get_children()
    failing.refresh()
    _pump_until(window, lambda: not failing._loading)
    assert "42,112.86" in failing.metric_values["equity"].cget("text")
    assert failing.tree.get_children() == previous_rows
    assert "displaying previous snapshot" in failing.alert.cget("text")
    for title, _, _, _ in failing.source_labels.values():
        assert "Read failed" in title.cget("text")
        assert title.cget("fg") != MINT
    frame.destroy()


def test_destroy_during_read_discards_results_and_prevents_later_refresh(root):
    window = tk.Toplevel(root)
    parent = ttk.Frame(window)
    parent.pack(fill=tk.BOTH, expand=True)
    service = BlockingService()
    view = HyperWorkspace(window, parent, service=service, auto_load=False, refresh_ms=10)
    applied = []
    view.show_snapshot = applied.append
    try:
        view.refresh()
        assert service.started.wait(timeout=1)
        parent.destroy()
        assert view._closed
        assert not view._jobs
        service.release.set()
        view.refresh()
        # Process several would-be refresh intervals after destruction.
        deadline = time.monotonic() + .15
        while time.monotonic() < deadline:
            window.update()
            time.sleep(.01)
        assert service.calls == 1
        assert not applied
    finally:
        service.release.set()
        window.destroy()
