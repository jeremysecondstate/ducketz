from __future__ import annotations

from collections.abc import Callable

import app.ui.ducket_bucket as ducket_bucket_ui


def test_desktop_app_mounts_the_seven_active_workspaces(monkeypatch) -> None:
    mounted_tabs: list[str] = []
    mounted_views: list[str] = []
    tab_frames: dict[str, object] = {}
    view_frames: dict[str, object] = {}
    root = object()

    class FakeNotebook:
        def __init__(self, _root: object) -> None:
            pass

        def pack(self, **_kwargs: object) -> None:
            pass

        def add(self, _frame: object, *, text: str) -> None:
            mounted_tabs.append(text)
            tab_frames[text] = _frame

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
    monkeypatch.setattr(ducket_bucket_ui, "GameplanStatsTab", view_factory("Gameplan Stats"))
    monkeypatch.setattr(ducket_bucket_ui, "GameplanTab", view_factory("Gameplan"))

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
        "Gameplan Stats",
        "Gameplan",
    ]
    assert mounted_tabs == expected
    assert mounted_views == expected
    assert view_frames == tab_frames
    assert len({id(frame) for frame in tab_frames.values()}) == len(expected)
