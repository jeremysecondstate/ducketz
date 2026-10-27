"""Real Tk event-loop checks with offline loaders and no live application."""

from datetime import datetime, timezone
import socket
import sqlite3
import threading
import time
import tkinter as tk
from tkinter import ttk

import pytest

import app.ui.ducket_bucket as desktop
import app.ui.intraday as intraday
from app.ui.intraday_data import IntradaySnapshot


@pytest.fixture(scope="module")
def tk_interpreter():
    try:
        interpreter = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Real Tk display unavailable: {exc}")
    interpreter.withdraw()
    yield interpreter
    interpreter.destroy()


@pytest.fixture
def desktop_root(monkeypatch, tk_interpreter):
    def forbidden(*args, **kwargs):
        raise AssertionError("Desktop responsiveness checks must not access external data")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    # Reuse one interpreter; each test still owns a separate desktop window.
    root = tk.Toplevel(tk_interpreter)
    root.withdraw()
    errors = []
    monkeypatch.setattr(tk_interpreter, "report_callback_exception",
                        lambda *error: errors.append(error))
    try:
        yield root
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass  # A close-in-flight test has already destroyed this root.
    assert not errors, f"Tk callback errors: {errors}"


def pump_until(root, condition, timeout=2):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        root.update()
        time.sleep(.005)
    assert condition(), "The Tk event loop did not complete the expected callback"


def test_real_desktop_launch_never_initializes_hidden_workspaces(desktop_root, monkeypatch):
    root = desktop_root
    constructed, refreshed = [], []
    names = (
        "RollingForecastTab", "OptionsStrategiesTab", "SchwabDucketsTab",
        "HyperliquidDucketsTab", "HyperWorkspace", "GameplanStatsTab",
        "GameplanTab", "IntradayTab",
    )

    def factory(name):
        def construct(*, root, parent, **kwargs):
            constructed.append(name)
            ttk.Label(parent, text=name).pack()
            root.after_idle(lambda: refreshed.append(name))
            return object()
        return construct

    for name in names:
        monkeypatch.setattr(desktop, name, factory(name))
    monkeypatch.setattr(desktop, "HyperliquidAccountHistoryService",
                        lambda: pytest.fail("Hidden account history must not initialize"))
    app = desktop.DucketBucketApp.__new__(desktop.DucketBucketApp)
    app.root = root
    app._build_layout()
    pump_until(root, lambda: bool(refreshed))

    assert len(app.notebook.tabs()) == 8
    assert constructed == refreshed == ["RollingForecastTab"]
    app.notebook.select(app.notebook.tabs()[-1])
    pump_until(root, lambda: "IntradayTab" in refreshed)
    app.notebook.select(app.notebook.tabs()[0])
    root.update()
    assert constructed == refreshed == ["RollingForecastTab", "IntradayTab"]


def blocked_intraday(root, monkeypatch):
    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True)
    page, other = ttk.Frame(notebook), ttk.Frame(notebook)
    notebook.add(page, text="Saved results")
    notebook.add(other, text="Other workspace")
    started, release = threading.Event(), threading.Event()
    workers = []
    snapshot = IntradaySnapshot(datetime(2026, 10, 26, tzinfo=timezone.utc))

    def load_snapshot(*args, **kwargs):
        workers.append(threading.current_thread())
        started.set()
        # A finite wait makes the old synchronous implementation fail without
        # leaving a hung test process or holding a production data connection.
        release.wait(2)
        return snapshot

    monkeypatch.setattr(intraday, "load_intraday_snapshot", load_snapshot)
    monkeypatch.setattr(intraday, "load_model_research",
                        lambda *a, **k: {"status": "missing", "message": "Offline fixture"})
    tab = intraday.IntradayTab(root, page, database_paths={}, local_pc="Atlas", auto_load=False)
    return tab, notebook, other, started, release, workers, snapshot


def test_slow_intraday_read_allows_heartbeat_and_tab_switch(desktop_root, monkeypatch):
    root = desktop_root
    tab, notebook, other, started, release, workers, snapshot = blocked_intraday(root, monkeypatch)
    applied_threads = []
    original_set_snapshot = tab.set_snapshot

    def set_snapshot(value, **kwargs):
        applied_threads.append(threading.get_ident())
        original_set_snapshot(value, **kwargs)

    monkeypatch.setattr(tab, "set_snapshot", set_snapshot)
    heartbeat = []
    root.after(0, lambda: (heartbeat.append(True), notebook.select(other)))
    start = time.perf_counter()
    try:
        tab.refresh()
        refresh_seconds = time.perf_counter() - start
        assert refresh_seconds < .5, f"Refresh blocked Tk for {refresh_seconds:.3f}s"
        pump_until(root, lambda: bool(heartbeat) and started.is_set())
        assert notebook.select() == str(other)
        assert not applied_threads
        assert workers[0].ident != threading.get_ident()
        release.set()
        pump_until(root, lambda: tab.snapshot is snapshot)
        assert applied_threads == [threading.get_ident()]
    finally:
        release.set()
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(timeout=3)


def test_closing_desktop_during_intraday_read_never_calls_tk_from_worker(desktop_root, monkeypatch):
    root = desktop_root
    tab, _notebook, _other, started, release, workers, _snapshot = blocked_intraday(root, monkeypatch)
    ui_thread = threading.get_ident()
    unsafe_calls = []
    def guard_method(owner, name):
        original = getattr(owner, name)

        def guarded(*args, **kwargs):
            if threading.get_ident() != ui_thread:
                unsafe_calls.append(name)
                return None
            return original(*args, **kwargs)

        monkeypatch.setattr(owner, name, guarded)

    guard_method(tk.Misc, "after")
    guard_method(tk.Variable, "set")
    try:
        tab.refresh()
        assert started.wait(1)
        root.destroy()
        assert tab._closed
        release.set()
        for worker in workers:
            assert worker is not threading.current_thread(), "Store read ran on the Tk thread"
            worker.join(timeout=3)
            assert not worker.is_alive(), "Closed-tab worker attempted to wait on Tk"
        assert not unsafe_calls
    finally:
        release.set()
