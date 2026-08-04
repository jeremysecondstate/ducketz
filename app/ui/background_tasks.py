from __future__ import annotations

import threading
import tkinter as tk
from collections.abc import Callable
from typing import TypeVar


ResultT = TypeVar("ResultT")


def run_in_background(
    root: tk.Misc,
    work: Callable[[], ResultT],
    on_success: Callable[[ResultT], None],
    on_error: Callable[[Exception], None],
) -> threading.Thread:
    def worker() -> None:
        try:
            result = work()
        except Exception as exc:
            root.after(0, lambda caught=exc: on_error(caught))
            return
        root.after(0, lambda completed=result: on_success(completed))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


__all__ = ["run_in_background"]
