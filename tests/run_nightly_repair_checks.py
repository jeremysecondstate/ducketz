"""Run offline repair fixtures without the external CLI's requests.py shadow.

The pinned harness still disables networking and audits every candidate import.
Only its executable directory is removed from import lookup; the harness itself
continues to run unchanged. This lets Databento import the installed requests
package instead of the coordination helper with the same basename.
"""
from pathlib import Path
import sys
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path[:] = [entry for entry in sys.path
               if not (Path(entry).name == "cross_pc" and (Path(entry) / "test_runner.py").is_file())]

# The SDK imports Live at package import time, opening a Windows loopback pair
# for an unused event loop. Keep its real DBN readers and all hash verification,
# while prohibiting a Live client (as the existing offline SDK fixture does).
live = ModuleType("databento.live.client")


class NoLiveClient:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("Live clients are disabled in offline repair checks")


live.Live = NoLiveClient
sys.modules["databento.live.client"] = live

import pytest

if __name__ == "__main__":
    raise SystemExit(pytest.main([
        "tests/test_nightly_source_repair.py", "tests/test_gameplan_archive_features.py",
        "tests/test_gameplan_archive_seconds.py", "tests/test_nightly_recovery.py",
        "tests/test_nightly_workflow.py", "tests/test_gameplan_model_feedback.py",
        "tests/test_late_forecast_validation.py", "tests/test_gameplan_trade_planning.py",
        "tests/test_joint_capital_handoff.py", "tests/test_joint_capital_plan.py",
        "tests/test_independent_stock_signals.py", "tests/test_independent_stock_session.py",
        "-q", "-p", "no:cacheprovider",
    ]))
