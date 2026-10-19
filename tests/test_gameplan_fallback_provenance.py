"""Accepted-plan fallback receipt provenance with only a synthetic broker."""
import hashlib
import json

import pytest

from ml.stock_trader import independent_runtime as runtime
from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY
from test_atlas_stats_first_execution import accepted_env
from test_account_gameplan_execution import account_env
from test_independent_stock_runtime import environment


@pytest.mark.parametrize("entries", [False, True], ids=["entries-disabled", "initial-signals-empty"])
def test_accepted_fallback_catchup_records_exact_input_provenance(accepted_env, monkeypatch, entries):
    env = accepted_env
    # A managed polling cycle can omit initial entry loading while the saved
    # accepted plan still produces catch-up decisions after broker capture.
    monkeypatch.setattr(runtime, "_has_inventory", lambda path: True)
    monkeypatch.setattr(runtime, "load_current_independent_gameplan_signals", lambda *a, **k: ({}, ()))
    result = runtime.run_independent_stock_trader_once(
        env.root, execute=True, session=env.broker, runtime_clock=lambda: env.now,
        entries=entries, session_managed=True, sizing_policy=GAMEPLAN_SIZING_POLICY,
        broker_state_retry_max_seconds=0,
    )
    assert result.status == "ORDERS_SUBMITTED", result.error
    assert result.submitted_orders == len(env.broker.submissions) > 0
    receipt = json.loads((result.run_directory / "receipt.json").read_text())
    manifest = json.loads((result.run_directory / "manifest.json").read_text())
    assert receipt["prediction_handoff"]["catchup"]["plan_sha256"] == env.plan.path.name
    expected = {str((env.plan.path / name).relative_to(env.root)):
                hashlib.sha256((env.plan.path / name).read_bytes()).hexdigest()
                for name in ("accepted-plan.json", "joint-plan.json")}
    assert {row["path"]: row["checksum_sha256"] for row in manifest["input_files"]} == expected
    assert receipt["prediction_handoff"]["source_gameplan_run"] == env.plan.path.relative_to(env.root).as_posix()
    assert all(row["status"] == "present" for row in manifest["input_files"])
