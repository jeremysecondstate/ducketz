"""Cheap Windows wake adapter for the single durable nightly coordinator.

This adapter never acquires data, runs a model, transports packets or controls a
trader itself. The coordinator selects one eligible responsibility and launches
its existing bounded worker. Native responsibility tasks own repairs and notices.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

from filelock import FileLock, Timeout

from ml import nightly_workflow as workflow
from ml.artifacts import utc_timestamp


def _bootstrap_failure(config_path: Path, error: Exception) -> dict:
    """Record a pre-dispatch failure without trusting a broken state-root binding."""
    root = config_path.resolve().parent / "watchdog"
    root.mkdir(parents=True, exist_ok=True)
    owner = "datastore"
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        selected = raw.get("responsibility_owners", {}).get("datastore")
        if isinstance(selected, str) and selected.strip():
            owner = selected
    except (OSError, ValueError, AttributeError):
        pass
    decision = {"status": "FAILED", "owner": owner, "kind": "CONFIGURATION_OR_INSTALLATION",
                "error": f"{type(error).__name__}: {error}",
                "corrective_action": "Verify the local configuration and pinned installation before resuming dispatch"}
    fingerprint = sha256(json.dumps(decision, sort_keys=True).encode()).hexdigest()
    with FileLock(str(root / "bootstrap.lock"), timeout=0):
        evidence = root / "bootstrap-failures" / (fingerprint + ".json")
        if not evidence.exists():
            workflow._write(evidence, {"at": utc_timestamp().isoformat(), **decision})
        status_path = root / "bootstrap-status.json"
        previous = workflow._json(status_path) if status_path.exists() else {}
        result = {**decision, "fingerprint": fingerprint, "evidence": str(evidence),
                  "disposition": "OPEN", "changed": previous.get("fingerprint") != fingerprint
                  or previous.get("disposition") != "OPEN"}
        workflow._write(status_path, result)
        return result


def _resolve_bootstrap_failure(config_path: Path) -> None:
    root = config_path.resolve().parent / "watchdog"
    status_path = root / "bootstrap-status.json"
    if not status_path.exists():
        return
    with FileLock(str(root / "bootstrap.lock"), timeout=0):
        previous = workflow._json(status_path)
        if previous.get("disposition") == "OPEN":
            workflow._write(status_path, {**previous, "disposition": "RESOLVED",
                "resolved_at": utc_timestamp().isoformat(),
                "resolution": "Configuration and pinned installation passed; current dispatch outcome is recorded separately"})


def run_once(config_path: Path, *, runner=None, now=None) -> dict:
    config = workflow.load_config(config_path)
    workflow.verify_installation(config)
    root = Path(config["state_root"]) / "watchdog"
    root.mkdir(parents=True, exist_ok=True)
    observed = utc_timestamp(now)
    runner = runner or subprocess.run
    with FileLock(str(root / "wake.lock"), timeout=0):
        command = [sys.executable, "-B", "-m", "ml.nightly_workflow",
                   "--config", str(config_path.resolve()), "--dispatch"]
        try:
            result = runner(command, cwd=config["repository"], capture_output=True,
                            text=True, timeout=90, check=False,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                decision = json.loads(result.stdout)
                if not isinstance(decision, dict):
                    raise ValueError("Dispatcher returned a non-object")
            except (ValueError, TypeError):
                decision = {"status": "DISPATCH_FAILED", "exit_code": result.returncode,
                            "error": (result.stderr or result.stdout)[-4000:]}
            if result.returncode and decision.get("status") != "DISPATCH_FAILED":
                decision = {**decision, "exit_code": result.returncode}
        except (OSError, subprocess.TimeoutExpired) as error:
            decision = {"status": "DISPATCH_FAILED", "error": f"{type(error).__name__}: {error}",
                        "owner": config.get("responsibility_owners", {}).get("datastore", "datastore"),
                        "corrective_action": "Inspect launcher/runtime availability and resume the same dated coordinator"}
        if decision.get("status") in {"FAILED", "DISPATCH_FAILED"}:
            # Malformed output and a failed interpreter need an owner just as a
            # timeout does; preserve a more specific owner returned by dispatch.
            responsibility = decision.get("responsibility") or "datastore"
            decision.setdefault("owner", config.get("responsibility_owners", {}).get(
                responsibility, responsibility))
            decision.setdefault("corrective_action",
                                "Inspect launcher/runtime availability and resume the same dated coordinator")
        # Volatile timestamps/PIDs/log paths are evidence, not notification identity.
        stable = {k: decision.get(k) for k in
                  ("status", "action_date", "responsibility", "reason", "error")}
        stable["failure"] = {k: decision.get("failure", {}).get(k) for k in
                             ("fingerprint", "kind", "owner", "step", "disposition")}
        fingerprint = sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()
        previous = workflow._json(root / "last.json") if (root / "last.json").exists() else {}
        changed = previous.get("fingerprint") != fingerprint
        receipt = {"at": observed.isoformat(), "fingerprint": fingerprint,
                   "changed": changed, "decision": decision}
        workflow._write(root / "last.json", receipt)
        if changed:
            with (root / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(receipt, sort_keys=True) + "\n")
        return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = run_once(args.config)
        _resolve_bootstrap_failure(args.config)
        print(json.dumps(result, indent=2))
        return int(result["decision"].get("status") in {"FAILED", "DISPATCH_FAILED"})
    except Timeout:
        print(json.dumps({"status": "BUSY", "reason": "Existing watchdog owns this wake"}))
        return 0
    except Exception as error:
        print(json.dumps(_bootstrap_failure(args.config, error), indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
