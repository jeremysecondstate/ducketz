"""Match the native Windows frame to the app without replacing window controls."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import sys
import tkinter as tk

from app.ui.theme import BACKGROUND, BORDER, TEXT


def theme_title_bar(root: tk.Tk) -> None:
    """Apply after Tk creates its native wrapper, and again after restoration."""
    if sys.platform != "win32":
        return

    def apply(event: tk.Event | None = None) -> None:
        # Toplevel bindings also see descendant Map events.
        if event is not None and event.widget is not root:
            return
        try:
            _theme_windows_frame(root.winfo_id())
        except (AttributeError, OSError, tk.TclError):
            # Older Windows/Tk versions retain their normal native frame.
            pass

    root.bind("<Map>", apply, add="+")
    root.after_idle(apply)


def _colorref(color: str) -> int:
    """Windows COLORREF stores RGB as 0x00BBGGRR."""
    red, green, blue = (int(color[index:index + 2], 16) for index in (1, 3, 5))
    return red | (green << 8) | (blue << 16)


def _theme_windows_frame(widget_id: int) -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
    ancestor = user32.GetAncestor
    ancestor.argtypes = [wintypes.HWND, wintypes.UINT]
    ancestor.restype = wintypes.HWND
    # Tk's widget ID is a child of the actual native caption window. Use
    # pointer-sized HWND types so 64-bit handles are never truncated.
    hwnd = ancestor(widget_id, 2)  # GA_ROOT
    if not hwnd:
        return

    set_attribute = dwmapi.DwmSetWindowAttribute
    set_attribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    set_attribute.restype = wintypes.LONG  # HRESULT

    dark = wintypes.BOOL(1)
    if set_attribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark)) < 0:
        # Early Windows 10 builds used attribute 19 for immersive dark mode.
        set_attribute(hwnd, 19, ctypes.byref(dark), ctypes.sizeof(dark))

    # Explicit caption colors are supported on Windows 11. Unsupported
    # attributes return a failed HRESULT, leaving the dark-mode fallback intact.
    # https://learn.microsoft.com/windows/win32/api/dwmapi/ne-dwmapi-dwmwindowattribute
    for attribute, color in ((35, BACKGROUND), (36, TEXT), (34, BORDER)):
        value = wintypes.DWORD(_colorref(color))
        set_attribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
