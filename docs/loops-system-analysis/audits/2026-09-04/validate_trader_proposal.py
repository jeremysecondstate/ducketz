"""Validate the proposed session fix in memory; no trading or source-file writes."""
from pathlib import Path
import json
import re
import sys
from types import ModuleType
from unittest.mock import patch

import pandas as pd

REPOSITORY = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPOSITORY))
from ml.nightly_gameplan import read_gameplan_run
from ml.stock_trader import gameplan
from ml.stock_trader.runtime import _signal_time_in_force


def main():
    folder = Path(__file__).resolve().parent
    source_path = REPOSITORY / "ml/stock_trader/session.py"
    original = source_path.read_text(encoding="utf-8").splitlines(keepends=True)
    proposal = (folder / "gameplan-session-fix.patch").read_text(encoding="utf-8").splitlines(keepends=True)
    updated, index = [], 0
    for line in proposal[2:]:
        if line.startswith("@@"):
            start = int(re.match(r"@@ -(\d+)", line).group(1)) - 1
            updated.extend(original[index:start])
            index = start
        elif line.startswith((" ", "-")):
            assert original[index] == line[1:], "Proposal no longer matches current source"
            if line[0] == " ":
                updated.append(original[index])
            index += 1
        elif line.startswith("+"):
            updated.append(line[1:])
    updated.extend(original[index:])
    module = ModuleType("proposed_stock_session")
    sys.modules[module.__name__] = module
    exec(compile("".join(updated), str(source_path), "exec"), module.__dict__)
    root = Path(r"C:\DATASTORE")
    publication = read_gameplan_run(root, root / "ml/nightly-gameplan-runs/20260904T105944.876700Z")
    results = []
    with patch.object(gameplan, "checkpoint_session_for_target", module.checkpoint_session_for_target), patch.object(gameplan, "read_current_gameplan", return_value=publication):
        for hour in range(4, 17):
            minute = 6 if hour == 13 else 1
            observed = pd.Timestamp(f"2026-09-04 {hour:02d}:{minute:02d}", tz="America/Los_Angeles")
            signals, _ = gameplan.load_current_gameplan_prediction_signals(root, as_of=observed, target_horizon="1h")
            window = module.stock_execution_window(observed)
            assert window.executable
            for signal in signals.values():
                assert module.decision_targets_open(signal.target_window_start, window)
            tif = _signal_time_in_force(signals, as_of=observed, allow_open_queue=False, allow_premarket_queue=False) if signals else None
            results.append({"hour": hour, "signals": len(signals), "session": window.checkpoint_session, "time_in_force": tif})
    assert not module.stock_execution_window("2026-09-04T20:04:00Z").executable
    assert module.stock_execution_window("2026-09-04T11:00:00Z").executable
    assert not module.stock_execution_window("2026-09-05T00:00:00Z").executable
    assert not module.stock_execution_window("2026-09-05T16:00:00Z").executable
    assert not module.stock_execution_window("2026-09-08T00:00:00Z").executable
    report = {"status": "DRAFT_VALIDATED_NOT_APPLIED", "broker_contact": False, "orders_placed": 0,
              "hours": results, "actual_broker_pause_and_non_session_gates_retained": True}
    (folder / "gameplan-session-fix-validation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
