from __future__ import annotations

import importlib
from pathlib import Path
import sys
from types import ModuleType

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--cross-pc-shared-watchlist",
        action="store_true",
        default=False,
        help="Bind this candidate's fingerprinted shared watchlist before test collection.",
    )


def pytest_configure(config):
    if not config.getoption("cross_pc_shared_watchlist"):
        return
    watchlist = Path(__file__).resolve().parents[1] / "datafetching" / "watchlist.txt"
    if not watchlist.is_file() or watchlist.is_symlink():
        raise pytest.UsageError("Cross-PC shared checks require a regular candidate watchlist.txt")
    patch = pytest.MonkeyPatch()
    patch.setenv("DUCKETS_PRODUCTION_WATCHLIST", str(watchlist))
    config._cross_pc_shared_watchlist_patch = patch


def pytest_unconfigure(config):
    patch = getattr(config, "_cross_pc_shared_watchlist_patch", None)
    if patch is not None:
        patch.undo()
        del config._cross_pc_shared_watchlist_patch


@pytest.fixture(scope="module")
def offline_databento_sdk():
    # Databento imports Live even for Historical/DBNStore. Its unused Windows
    # event loop opens a loopback socket pair, which strict offline checks deny.
    # These opt-in suites inject fake Live clients; keep the real data readers.
    def is_sdk_module(name: str) -> bool:
        return name == "databento" or name.startswith("databento.")

    previous = {name: module for name, module in sys.modules.items() if is_sdk_module(name)}
    live_module = ModuleType("databento.live.client")

    class NoLiveClient:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("Real Databento Live clients are disabled in these offline tests")

    live_module.Live = NoLiveClient
    patch = pytest.MonkeyPatch()
    try:
        patch.setitem(sys.modules, "databento.live.client", live_module)
        sdk = importlib.import_module("databento")
        patch.setattr(sdk, "Live", NoLiveClient)
        if "databento.live" in sys.modules:
            patch.setattr(sys.modules["databento.live"], "client", live_module, raising=False)
        yield sdk
    finally:
        patch.undo()
        # Do not leave a cached package exporting the test-only Live class
        # after this module, so later suites retain their original SDK behavior.
        for name in list(sys.modules):
            if is_sdk_module(name) and name not in previous:
                del sys.modules[name]
        sys.modules.update(previous)
