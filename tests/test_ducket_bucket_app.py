from __future__ import annotations

from collections.abc import Callable

import app.ui.ducket_bucket as ducket_bucket_ui


def test_desktop_app_mounts_only_the_four_active_workspaces(monkeypatch) -> None:
    mounted_tabs: list[str] = []
    mounted_views: list[str] = []

    class FakeNotebook:
        def __init__(self, _root: object) -> None:
            pass

        def pack(self, **_kwargs: object) -> None:
            pass

        def add(self, _frame: object, *, text: str) -> None:
            mounted_tabs.append(text)

    class FakeFrame:
        def __init__(self, _parent: object) -> None:
            pass

    def view_factory(name: str) -> Callable[..., None]:
        def mount(**_kwargs: object) -> None:
            mounted_views.append(name)

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

    app = ducket_bucket_ui.DucketBucketApp.__new__(
        ducket_bucket_ui.DucketBucketApp
    )
    app.root = object()
    app._build_layout()

    expected = [
        "Rolling Forecasts",
        "Options Strategies",
        "Schwab Duckets",
        "Hyperliquid Duckets",
    ]
    assert mounted_tabs == expected
    assert mounted_views == expected
