import sys
import threading
from types import SimpleNamespace

from app.services.databento_lifecycle import close_unused_databento_loop


class Loop:
    """Socket-free loop double for the cross-PC network-disabled test harness."""
    def __init__(self, running=False):
        self.running, self.closed = running, False
    def is_running(self):
        return self.running
    def is_closed(self):
        return self.closed
    def close(self):
        assert not self.running
        self.closed = True


def test_closes_only_never_started_sdk_loop(monkeypatch):
    loop = Loop()
    live = SimpleNamespace(_loop=loop, _thread=threading.Thread(), _lock=threading.Lock())
    monkeypatch.setitem(sys.modules, "databento.live.client", SimpleNamespace(Live=live))
    assert close_unused_databento_loop()
    assert loop.is_closed()
    assert not close_unused_databento_loop()


def test_preserves_previously_started_loop(monkeypatch):
    loop = Loop()
    thread = threading.Thread(target=lambda: None)
    thread.start()
    thread.join()
    live = SimpleNamespace(_loop=loop, _thread=thread, _lock=threading.Lock())
    monkeypatch.setitem(sys.modules, "databento.live.client", SimpleNamespace(Live=live))
    try:
        assert not close_unused_databento_loop()
        assert not loop.is_closed()
    finally:
        loop.close()


def test_new_sdk_without_global_loop_is_noop(monkeypatch):
    monkeypatch.setitem(sys.modules, "databento.live.client", SimpleNamespace(Live=object))
    assert not close_unused_databento_loop()


def test_preserves_running_loop(monkeypatch):
    loop = Loop(running=True)
    live = SimpleNamespace(_loop=loop, _thread=threading.Thread(), _lock=threading.Lock())
    monkeypatch.setitem(sys.modules, "databento.live.client", SimpleNamespace(Live=live))
    assert not close_unused_databento_loop() and not loop.is_closed()
