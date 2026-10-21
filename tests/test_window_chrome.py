from __future__ import annotations

import ctypes
from ctypes import wintypes
from types import SimpleNamespace
import tkinter as tk

import pytest

from app.ui import window_chrome
from app.ui.theme import BACKGROUND, BORDER, TEXT


class FakeRoot:
    def __init__(self):
        self.bindings = []
        self.idle = []

    def bind(self, event, callback, *, add):
        self.bindings.append((event, callback, add))

    def after_idle(self, callback):
        self.idle.append(callback)

    def winfo_id(self):
        return 123


def test_titlebar_applies_after_creation_and_restore_only(monkeypatch):
    monkeypatch.setattr(window_chrome.sys, "platform", "win32")
    applied = []
    monkeypatch.setattr(window_chrome, "_theme_windows_frame", applied.append)
    root = FakeRoot()
    window_chrome.theme_title_bar(root)
    event, callback, add = root.bindings[0]
    assert (event, add) == ("<Map>", "+")
    assert applied == []
    root.idle[0]()
    callback(SimpleNamespace(widget=object()))
    callback(SimpleNamespace(widget=root))
    assert applied == [123, 123]


def test_other_platforms_keep_their_native_frame(monkeypatch):
    monkeypatch.setattr(window_chrome.sys, "platform", "linux")
    root = FakeRoot()
    window_chrome.theme_title_bar(root)
    assert root.bindings == root.idle == []


@pytest.mark.parametrize("error", [OSError("unavailable"), AttributeError("unsupported"), tk.TclError("destroyed")])
def test_unavailable_native_theming_does_not_break_ui(monkeypatch, error):
    monkeypatch.setattr(window_chrome.sys, "platform", "win32")
    def unavailable(_):
        raise error
    monkeypatch.setattr(window_chrome, "_theme_windows_frame", unavailable)
    root = FakeRoot()
    window_chrome.theme_title_bar(root)
    root.idle[0]()


@pytest.mark.parametrize("legacy", [False, True])
def test_windows_wrapper_handle_colors_and_legacy_fallback(monkeypatch, legacy):
    hwnd = 0x123456789ABC
    calls = []
    class NativeFunction:
        def __init__(self, callback):
            self.callback = callback
        def __call__(self, *args):
            return self.callback(*args)
    def ancestor(widget, flag):
        assert (widget, flag) == (123, 2)
        return hwnd
    def set_attribute(handle, attribute, pointer, size):
        assert handle == hwnd
        assert size == 4
        value = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32)).contents.value
        calls.append((attribute, value))
        return -1 if legacy and attribute == 20 else 0
    user32 = SimpleNamespace(GetAncestor=NativeFunction(ancestor))
    dwmapi = SimpleNamespace(DwmSetWindowAttribute=NativeFunction(set_attribute))
    monkeypatch.setattr(window_chrome.ctypes, "WinDLL", lambda name, **_: {"user32": user32, "dwmapi": dwmapi}[name], raising=False)
    window_chrome._theme_windows_frame(123)
    assert calls == ([(20, 1), (19, 1)] if legacy else [(20, 1)]) + [
        (35, window_chrome._colorref(BACKGROUND)),
        (36, window_chrome._colorref(TEXT)),
        (34, window_chrome._colorref(BORDER)),
    ]
    assert window_chrome._colorref("#123456") == 0x563412
    assert user32.GetAncestor.restype == wintypes.HWND
    assert user32.GetAncestor.argtypes[0] == wintypes.HWND
    assert dwmapi.DwmSetWindowAttribute.argtypes[0] == wintypes.HWND
