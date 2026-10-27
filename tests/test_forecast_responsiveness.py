"""Offline regressions for forecast layout callback responsiveness."""

from types import SimpleNamespace

from app.ui.rolling_forecasts import RollingForecastTab


class IdleLoop:
    def __init__(self):
        self.callbacks = []

    def after_idle(self, callback):
        self.callbacks.append(callback)
        return f"idle-{len(self.callbacks)}"

    def drain(self):
        callbacks, self.callbacks = self.callbacks, []
        for callback in callbacks:
            callback()


def make_tab(loop, events):
    tab = object.__new__(RollingForecastTab)
    tab.root = loop
    tab._scroll_region_job = None
    # This reproduces Tk's nested-idle behavior without a display or providers.
    tab.content_frame = SimpleNamespace(update_idletasks=loop.drain)
    tab._update_scroll_region = lambda: events.append("scroll-bounds")
    return tab


def test_forecast_resize_does_not_run_pending_tab_initialization_inline():
    loop = IdleLoop()
    events = []
    loop.after_idle(lambda: events.append("other-tab-initialization"))
    tab = make_tab(loop, events)

    tab._refresh_scroll_region()

    assert events == [], "A resize must return before running unrelated idle work"
    loop.drain()
    assert events == ["other-tab-initialization", "scroll-bounds"]


def test_forecast_resize_burst_updates_bounds_once_and_accepts_later_resizes():
    loop = IdleLoop()
    events = []
    tab = make_tab(loop, events)

    for _ in range(50):
        tab._refresh_scroll_region()
    assert len(loop.callbacks) == 1
    loop.drain()
    assert events == ["scroll-bounds"]

    tab._refresh_scroll_region()
    loop.drain()
    assert events == ["scroll-bounds", "scroll-bounds"]
