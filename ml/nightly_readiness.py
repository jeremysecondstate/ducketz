"""Read-only verification of the intended session's local nightly completion.

No worker, review, provider, synthesis, transport or trader is launched here.
An old successful run is not readiness for a new action date.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import exchange_calendars as xcals
import pandas as pd

from ml.artifacts import utc_timestamp
from ml import nightly_workflow as workflow


def readiness(config: dict, *, now=None) -> dict:
    workflow.verify_installation(config)
    observed = utc_timestamp(now)
    local = observed.tz_convert("America/Los_Angeles")
    label = pd.Timestamp(local.date())
    calendar = xcals.get_calendar("XNYS", start=label - pd.Timedelta(days=10),
                                 end=label + pd.Timedelta(days=30))
    result = {"actor": config["actor"], "observed_at": observed.isoformat(),
              "local_ready": False, "joint_ready": False, "execution_authorized": False,
              "peer_communication_enabled": False, "orders_placed": 0}
    if local.hour < 17 and not calendar.is_session(label):
        return {**result, "status": "NOOP_NON_SESSION_DATE"}
    if local.hour >= 17:
        from ml.overnight_runtime import next_action_deadline
        day = next_action_deadline(observed).tz_convert("America/Los_Angeles").date().isoformat()
    else:
        day = local.date().isoformat()
    result["action_date"] = day
    state = workflow.status(config)
    result["worker_status"] = state["status"]
    if state.get("action_date") != day:
        return {**result, "status": "NOT_READY", "reason": "No worker result for the intended action date"}
    if state.get("actor") != config["actor"] or state.get("schema_version") != workflow.VERSION:
        raise ValueError("Saved nightly identity differs from this machine")
    workflow._verify_configuration_binding(config, state)
    workflow._verify_symbol_binding(config, state)
    if state["source_identity"] != workflow.source_identity(Path(config["repository"])):
        raise ValueError("Application source differs from the saved nightly run")
    for step in workflow.STEPS:
        entry = state.get("steps", {}).get(step, {})
        if entry.get("status") != "COMPLETE":
            return {**result, "status": "NOT_READY", "step": step,
                    "reason": entry.get("error") or state.get("error") or "Stage incomplete"}
        workflow._verify_outputs(entry["output"])
    if state["status"] != "LOCAL_COMPLETE_PEER_SETUP_PENDING":
        return {**result, "status": "NOT_READY", "reason": state.get("error") or "Worker has not completed"}
    display = workflow._display(config, state)
    if display != state["steps"]["verify_display"]["output"]:
        raise ValueError("Default UI selection differs from the saved display verification")
    handoff = state["steps"]["local_handoff"]["output"]
    if handoff.get("delivery") != "HELD_FOR_SEPARATE_PC_SETUP":
        raise ValueError("Local rollout handoff hold differs")
    return {**result, "status": "LOCAL_COMPLETE_PEER_SETUP_PENDING", "local_ready": True,
            "source_session": state["source_session"], "run_id": state["run_id"],
            "completed_at": state["completed_at"], "source_identity": state["source_identity"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = readiness(workflow.load_config(args.config))
        code = 0 if result["status"] in {"NOOP_NON_SESSION_DATE", "LOCAL_COMPLETE_PEER_SETUP_PENDING"} else 1
    except Exception as exc:
        result, code = {"status": "VERIFICATION_FAILED", "error": f"{type(exc).__name__}: {exc}",
                        "local_ready": False, "joint_ready": False, "orders_placed": 0}, 1
    print(json.dumps(result, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
