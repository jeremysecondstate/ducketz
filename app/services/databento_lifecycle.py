"""Close the old SDK's import-created, never-started loop at process exit.

Recent SDKs need no workaround. Never close a running or previously used live
client loop here: its owning transport controls that lifecycle.
"""
from __future__ import annotations

import atexit
import sys
import threading

_registered = False
_registration_lock = threading.Lock()


def close_unused_databento_loop() -> bool:
    module = sys.modules.get("databento.live.client")
    live = getattr(module, "Live", None)
    loop = getattr(live, "_loop", None)
    thread = getattr(live, "_thread", None)
    lock = getattr(live, "_lock", None)
    if loop is None or thread is None or lock is None:
        return False
    with lock:
        if thread.ident is not None or thread.is_alive() or loop.is_running() or loop.is_closed():
            return False
        loop.close()
        return True


def register_databento_cleanup() -> None:
    global _registered
    with _registration_lock:
        if not _registered:
            atexit.register(close_unused_databento_loop)
            _registered = True
