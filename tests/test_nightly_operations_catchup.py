"""Normal human late starts use the same worker, never a second executor."""
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.stock_trader import independent_session as worker


@pytest.mark.parametrize("started", [
    "2026-09-08T12:30:00Z",  # 05:30 Pacific
    "2026-09-08T21:30:00Z",  # 14:30 Pacific
    "2026-09-08T23:02:00Z",  # 16:02 Pacific
])
def test_normal_manual_start_later_in_session_checks_due_intentions_without_special_flag(
    tmp_path, monkeypatch, started,
):
    current = [pd.Timestamp(started)]
    calls = []
    monkeypatch.setattr(worker, "_has_inventory", lambda _: False)
    monkeypatch.setattr(worker, "read_gameplan_stock_activation_intent", lambda _: SimpleNamespace(active=True))
    monkeypatch.setattr("ml.stock_trader.gameplan_execution.execution_preflight",
                        lambda *a, **kw: {"status": "READY"})
    def run(root, **kwargs):
        calls.append((current[0], kwargs))
        return SimpleNamespace(submitted_orders=0, to_dict=lambda: {"status": "SYNTHETIC_NO_ORDERS"})
    def sleep(seconds):
        current[0] += pd.Timedelta(seconds=seconds)
    result = worker.run_independent_stock_session(
        tmp_path, clock=lambda: current[0], sleep=sleep, runner=run, reporter=lambda _: None,
        sizing_policy="gameplan-direction-current-market-v1",
    )
    assert result["orders_submitted"] == 0
    assert calls and calls[0][0] == pd.Timestamp(started)
    assert all(options.get("late_opening_date") is None for _, options in calls)
    assert all(options["entries"] and options["session_managed"] for _, options in calls)
    assert all(at < pd.Timestamp("2026-09-09T00:00:00Z") for at, _ in calls)
