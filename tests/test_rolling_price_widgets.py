"""Offline rendering checks; fixtures contain no provider connections."""
from __future__ import annotations

import tkinter as tk
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.ui.rolling_price_widgets import RollingPricePanel, change_text
from app.ui.theme import DANGER, MUTED_TEXT, SUCCESS, WARNING


def fixture_snapshot(*, missing=False, closed=False):
    symbols = ("DOCU", "DBX", "SDGR", "QBTS", "PYPL", "GLOB", "OUST", "ABCL", "MRNA", "RR", "PDYN")
    values = (2.12514, -1.237, 0., .123, -2.458, 3.484, -.48, 1.119, -3.04, 4.0001, -.987)
    rows = []
    for index, (symbol, value) in enumerate(zip(symbols, values)):
        stamp = datetime(2026, 10, 28, 22, 0, tzinfo=timezone.utc)
        current = SimpleNamespace(price=102.125, observed_at=stamp, source="Schwab fixture", basis="same-response candle closes",
                                  interval_start=stamp.replace(minute=59, hour=21))
        previous = SimpleNamespace(**{**vars(current), "price": 100., "observed_at": stamp.replace(day=27),
                                     "interval_start": stamp.replace(day=27, minute=59, hour=21)})
        rows.append(SimpleNamespace(symbol=symbol, change_pct=None if missing and index == 10 else value,
                                    status="stale" if missing and index == 10 else "closed" if closed else "available",
                                    reason="Fixture evidence is stale" if missing and index == 10 else "Eligible completed candle closes",
                                    current=current, previous=previous))
    average = None if missing else sum(values) / len(values)
    return SimpleNamespace(as_of=datetime(2026, 10, 28, 22, 0, tzinfo=timezone.utc), rows=tuple(rows),
                           total_count=11, available_count=10 if missing else 11, average_pct=average,
                           source="Schwab fixture", policy="Completed one-minute candle ends at or before each cutoff.", error=None)


@pytest.fixture(scope="session")
def tk_root():
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def view(tk_root):
    tk_root.withdraw()
    frame = tk.Frame(tk_root)
    frame.pack(fill="both", expand=True)
    widget = RollingPricePanel(frame)
    widget.frame.pack(fill="both", expand=True)
    try:
        yield widget
    finally:
        frame.destroy()


def test_panel_displays_all_members_and_signed_values_with_prominent_full_average(view):
    view.set_snapshot(fixture_snapshot())
    assert len(view.tiles) == 11
    assert view.average_title.cget("text") == "11-symbol average"
    assert view.average_value.cget("text") == change_text(view.snapshot.average_pct)
    assert view.coverage_label.cget("text") == "11/11 available · Equal weighting"
    assert view.value_labels["DOCU"].cget("text") == "+2.13%"
    assert view.value_labels["DOCU"].cget("foreground") == SUCCESS
    assert view.value_labels["DBX"].cget("text") == "-1.24%"
    assert view.value_labels["DBX"].cget("foreground") == DANGER
    assert view.value_labels["SDGR"].cget("text") == "+0.00%"
    assert view.value_labels["SDGR"].cget("foreground") == MUTED_TEXT


def test_stale_row_never_becomes_zero_or_complete_average(view):
    view.set_snapshot(fixture_snapshot(missing=True))
    assert view.value_labels["PDYN"].cget("text") == "Unavailable"
    assert view.state_labels["PDYN"].cget("text") == "Stale evidence"
    assert view.state_labels["PDYN"].cget("foreground") == WARNING
    assert view.average_value.cget("text") == "Unavailable"
    assert "10/11 available" in view.coverage_label.cget("text")
    assert "1 stale" in view.status_label.cget("text")


def test_closure_is_explicit_and_keeps_actual_prices_and_source_in_details(view):
    view.set_snapshot(fixture_snapshot(closed=True))
    assert "Market closed" in view.status_label.cget("text")
    assert "Closed" in view.state_labels["DOCU"].cget("text")
    detail = view._symbol_detail("DOCU")
    assert "2026-10-28T22:00:00+00:00" in detail
    assert "Candle start (UTC): 2026-10-28T21:59:00+00:00" in detail
    assert "$102.125" in detail and "Schwab fixture" in detail
    assert "24h earlier cutoff" in detail and "$100" in detail
    assert "Earlier cutoff (UTC): 2026-10-27T22:00:00+00:00" in view._policy_detail()


def test_historical_session_label_never_changes_current_market_snapshot(view):
    snapshot = fixture_snapshot()
    view.set_snapshot(snapshot)
    view.set_session_context("2026-09-11")
    assert view.snapshot is snapshot
    assert "Current prices · Stats session 2026-09-11" in view.context_label.cget("text")
    assert "Independent of session and order filters" in view.context_label.cget("text")
    assert "Oct 28, 15:00 PDT" in view.status_label.cget("text")
    view.set_loading()
    assert view.snapshot is snapshot and view.loading
    assert "Refreshing…" in view.status_label.cget("text")
    view.set_snapshot(snapshot)
    assert not view.loading


@pytest.mark.parametrize("width,columns", [(860, 4), (560, 3), (430, 2), (280, 1)])
def test_responsive_symbol_grid_keeps_every_symbol_in_bounds(view, width, columns):
    view.set_snapshot(fixture_snapshot())
    root = view.frame.winfo_toplevel()
    root.geometry(f"{width}x900")
    root.deiconify()
    root.update()
    assert view._columns == columns
    assert len({tile.grid_info()["row"] for tile in view.tiles}) == (11 + columns - 1) // columns
    for symbol, tile in zip(view.symbol_labels, view.tiles):
        assert tile.winfo_x() >= 0
        assert tile.winfo_x() + tile.winfo_width() <= view.grid_frame.winfo_width()
        value = view.value_labels[symbol]
        ticker = view.symbol_labels[symbol]
        assert ticker.winfo_x() + ticker.winfo_width() <= value.winfo_x()
        assert value.winfo_x() + value.winfo_width() <= tile.winfo_width()


def test_member_replacement_removes_old_tiles_and_loading_never_fabricates_values(view):
    view.set_loading()
    assert view.snapshot is None and view.value_labels == {}
    view.set_snapshot(fixture_snapshot())
    smaller = fixture_snapshot()
    smaller.rows = smaller.rows[:2]
    smaller.total_count = smaller.available_count = 2
    view.set_snapshot(smaller)
    assert tuple(view.value_labels) == ("DOCU", "DBX")
    assert len(view.tiles) == 2
    assert view.average_title.cget("text") == "2-symbol average"
