from __future__ import annotations

from collections.abc import Callable

import app.ui.ducket_bucket as ducket_bucket_ui


def test_desktop_app_mounts_workspaces_once_when_selected(monkeypatch) -> None:
    mounted_tabs: list[str] = []
    mounted_views: list[str] = []
    tab_frames: dict[str, object] = {}
    view_frames: dict[str, object] = {}
    class FakeRoot:
        def after_idle(self, callback):
            self.pending = callback

    root = FakeRoot()

    class FakeNotebook:
        def __init__(self, _root: object) -> None:
            self.selected = ""

        def pack(self, **_kwargs: object) -> None:
            pass

        def add(self, _frame: object, *, text: str) -> None:
            mounted_tabs.append(text)
            tab_frames[text] = _frame
            if not self.selected:
                self.selected = str(_frame)

        def bind(self, event, callback, **kwargs):
            assert event == "<<NotebookTabChanged>>"
            self.callback = callback

        def select(self):
            return self.selected

    class FakeFrame:
        def __init__(self, _parent: object) -> None:
            pass

    def view_factory(name: str) -> Callable[..., None]:
        def mount(**_kwargs: object) -> None:
            assert _kwargs["root"] is root
            mounted_views.append(name)
            view_frames[name] = _kwargs["parent"]

        return mount

    monkeypatch.setattr(ducket_bucket_ui.ttk, "Notebook", FakeNotebook)
    monkeypatch.setattr(ducket_bucket_ui.ttk, "Frame", FakeFrame)
    monkeypatch.setattr(
        ducket_bucket_ui,
        "RollingForecastTab",
        view_factory("Rolling Forecasts"),
    )
    monkeypatch.setattr(
        ducket_bucket_ui,
        "OptionsStrategiesTab",
        view_factory("Options Strategies"),
    )
    monkeypatch.setattr(
        ducket_bucket_ui,
        "SchwabDucketsTab",
        view_factory("Schwab Duckets"),
    )
    monkeypatch.setattr(
        ducket_bucket_ui,
        "HyperliquidDucketsTab",
        view_factory("Hyperliquid Duckets"),
    )
    monkeypatch.setattr(ducket_bucket_ui, "HyperWorkspace", view_factory("H.Y.P.E.R."))
    monkeypatch.setattr(ducket_bucket_ui, "GameplanStatsTab", view_factory("Stats"))
    monkeypatch.setattr(ducket_bucket_ui, "GameplanTab", view_factory("Gameplan"))
    monkeypatch.setattr(ducket_bucket_ui, "IntradayTab", view_factory("15-Minute Duckets"))

    app = ducket_bucket_ui.DucketBucketApp.__new__(
        ducket_bucket_ui.DucketBucketApp
    )
    app.root = root
    app._build_layout()

    expected = [
        "Rolling Forecasts",
        "Options Strategies",
        "Schwab Duckets",
        "Hyperliquid Duckets",
        "H.Y.P.E.R.",
        "Stats",
        "Gameplan",
        "15-Minute Duckets",
    ]
    assert mounted_tabs == expected
    assert mounted_views == []
    assert app._account_history is None
    root.pending()
    assert mounted_views == ["Rolling Forecasts"]
    for name in expected:
        app.notebook.selected = str(tab_frames[name])
        app.notebook.callback(None)
        app.notebook.callback(None)
    assert mounted_views == expected
    assert view_frames == tab_frames
    assert len({id(frame) for frame in tab_frames.values()}) == len(expected)


def test_workspace_constructor_reentrancy_and_failure_retry():
    app = ducket_bucket_ui.DucketBucketApp.__new__(ducket_bucket_ui.DucketBucketApp)
    calls = []

    class Parent:
        def winfo_children(self):
            return []

    class Notebook:
        def select(self):
            return "frame"

    def factory(**kwargs):
        calls.append(kwargs)
        app._show_selected_workspace()
        if len(calls) == 1:
            raise ValueError("fixture failure")
        return "mounted"

    app.root = object()
    app.notebook = Notebook()
    app._workspaces = {}
    app._workspace_factories = {"frame": (Parent(), factory, False)}
    import pytest
    with pytest.raises(ValueError, match="fixture failure"):
        app._show_selected_workspace()
    assert not app._workspaces
    app._show_selected_workspace()
    assert len(calls) == 2
    assert app._workspaces == {"frame": "mounted"}
