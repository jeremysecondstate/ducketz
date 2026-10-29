"""Offline integration: current prices have a separate, fenced refresh lane."""
from datetime import datetime, timedelta, timezone
import gc
import threading
import time
import tkinter as tk
from tkinter import ttk

import pytest

from app.ui import gameplan_stats as stats
from app.services.rolling_price_change import RollingPriceSnapshot, SymbolPriceChange


def snapshot(as_of, value=1.25):
    return RollingPriceSnapshot(as_of, tuple(SymbolPriceChange(symbol, value, "available", "Fixture")
        for symbol in ("AAA", "BBB")), "offline fixture", "Completed minute closes")


def pump(tab, predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        tab.root.update()
        time.sleep(.005)
    assert predicate()


@pytest.fixture(scope="session")
def tk_root():
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def view(tmp_path, tk_root):
    root = tk_root
    frame = ttk.Frame(root)
    frame.pack(fill="both", expand=True)
    tab = stats.GameplanStatsTab(root, frame, datastore_root=tmp_path, auto_load=False,
        price_loader=lambda *, as_of: snapshot(as_of))
    yield tab
    frame.destroy()
    del tab, frame, root
    gc.collect()


def test_current_price_population_and_value_ignore_all_saved_filters(view):
    view.refresh()
    pump(view, lambda: not view._loading and not view._price_loading)
    saved = view.rolling_prices.snapshot
    assert view.review is None and saved.symbols == ("AAA", "BBB")
    assert saved.as_of.tzinfo is timezone.utc
    assert saved.average_pct == 1.25
    view.horizon.set("Weekly")
    view.execution.company.set("UNRELATED")
    view.execution.status_filter.set("Cancelled")
    view.execution.scope_filter.set("Carryover orders")
    view.render()
    view.execution.render()
    assert view.rolling_prices.snapshot is saved
    assert not view.execution.observation_label.winfo_manager()
    assert not view.execution.coverage_label.winfo_manager()
    assert view.rolling_prices.frame.master is view.execution.frame
    assert "last recorded broker observation" in view.execution.evidence_tooltip.text()


def test_slow_saved_reader_does_not_block_prices_and_auto_refresh(view, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def slow(*_):
        entered.set()
        release.wait(3)
        return ()
    monkeypatch.setattr(stats, "review_sessions", slow)
    try:
        view.refresh()
        assert entered.wait(1)
        pump(view, lambda: not view._price_loading)
        assert view._loading and view.rolling_prices.snapshot.average_pct == 1.25
        previous_request = view._price_request_id
        view._auto_enabled = True
        # Simulate a visible tab without mapping a live application.
        monkeypatch.setattr(view.parent, "winfo_ismapped", lambda: True)
        view._auto_refresh()
        pump(view, lambda: not view._price_loading)
        assert view._price_request_id == previous_request + 1
    finally:
        release.set()
        pump(view, lambda: not view._loading)


def test_slow_prices_do_not_block_saved_results_or_refresh_button(view):
    entered, release = threading.Event(), threading.Event()
    def slow(*, as_of):
        entered.set()
        release.wait(3)
        return snapshot(as_of)
    view._price_loader = slow
    try:
        view.refresh_button.invoke()
        assert entered.wait(1)
        pump(view, lambda: not view._loading)
        assert view._price_loading
        assert "disabled" not in view.refresh_button.state()
    finally:
        release.set()
        pump(view, lambda: not view._price_loading)


def test_each_request_has_new_utc_cutoff_and_obsolete_replies_are_rejected(view, monkeypatch):
    # Control reply delivery; no provider or hidden thread is involved.
    monkeypatch.setattr(stats.threading, "Thread", lambda **_: type("Thread", (), {"start": lambda _: None})())
    view._refresh_prices()
    first = view._price_request_id
    view._refresh_prices()
    second = view._price_request_id
    now = datetime.now(timezone.utc)
    newer, older = snapshot(now, 2.5), snapshot(now - timedelta(minutes=5), -9)
    view._messages.put(("prices", second, newer))
    view._messages.put(("prices", first, older))
    view._poll()
    assert view.rolling_prices.snapshot is newer
    assert not view._price_loading
    view.parent.destroy()
    view._messages.put(("prices", second, older))
    view._poll()
    assert view._closed and view.rolling_prices.snapshot is newer


def test_initial_load_and_historical_selection_still_request_current_prices(tmp_path, tk_root):
    root = tk_root
    root.withdraw()
    frame = ttk.Frame(root)
    frame.pack()
    cutoffs = []
    def loader(*, as_of):
        cutoffs.append(as_of)
        return snapshot(as_of)
    try:
        tab = stats.GameplanStatsTab(root, frame, datastore_root=tmp_path, price_loader=loader)
        pump(tab, lambda: bool(cutoffs) and not tab._loading and not tab._price_loading)
        tab.session.set("2020-01-02")
        tab._date_changed()
        pump(tab, lambda: len(cutoffs) == 2 and not tab._loading and not tab._price_loading)
        assert cutoffs[1] >= cutoffs[0]
        assert abs((datetime.now(timezone.utc) - cutoffs[1]).total_seconds()) < 5
        assert tab.rolling_prices.snapshot.as_of == cutoffs[1]
    finally:
        frame.destroy()

