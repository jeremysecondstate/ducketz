"""Offline real-process controller relay probe; no broker or provider imports."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from datetime import datetime, timedelta, timezone

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from ml.trader_renewal import Renewal, PlannedRenewal


def main():
    mode, root, host_path = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    spec = importlib.util.spec_from_file_location("offline_relay_host", host_path)
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    config = json.loads((root / "config.json").read_text())
    runtime_tag = sys.argv[sys.argv.index("--runtime-tag") + 1] if "--runtime-tag" in sys.argv else "v1"
    base_python = sys._base_executable
    if mode == "worker":
        context = Renewal(sys.argv[4])
        role = context.request["role"]
        count = context.state.get("count", 0) + 1
        context.wait_due()
        if context.stopped():
            context.finish({"status": "STOPPED"})
            return 0
        if role == "forecast":
            until = time.monotonic() + (.35 if count == 1 else .01)
            while time.monotonic() < until and not context.stopped():
                time.sleep(.01)
            if context.stopped():
                context.finish({"status": "STOPPED"})
                return 0
            host.write_json(root / f"science-{count}.json", {"count": count, "at": time.time()})
            if count == 2:
                context.finish({"status": "COMPLETED"})
                return 0
        else:
            with host.byte_lock(root / "execution-exclusive.lock"):
                host.write_json(root / f"execution-enter-{count}.json", {"pid": os.getpid(), "controller": os.getppid()})
                if config.get("hold_after_relay") and host.FIXTURE_REVISION != "v1":
                    while not context.stopped():
                        time.sleep(.02)
                if config.get("crash_worker_after_relay") and host.FIXTURE_REVISION != "v1":
                    os._exit(27)
                if context.stopped():
                    context.finish({"status": "STOPPED"})
                    return 0
                action = {"pid": os.getpid(), "controller": os.getppid(), "count": count,
                          "session": context.session_id, "owner": context.envelope["owner_pid"],
                          "version": host.FIXTURE_REVISION, "runtime": runtime_tag, "at": time.time()}
                host.write_json(root / f"action-{count}.json", action)
                if count == 1:
                    host.write_json(root / "installed.json", {"path": str(root / (
                        "host-v1.py" if config.get("runtime_only") else "host-v2.py")),
                        "runtime": "v2" if config.get("runtime_only") else "v1"})
                if host.FIXTURE_REVISION == "v2" and config.get("second_update"):
                    host.write_json(root / "installed.json", {"path": str(root / "host-v3.py")})
            if count >= config.get("turns", 8):
                context.finish({"status": "COMPLETED"})
                return 0
        try:
            context.checkpoint({"count": count}, (datetime.now(timezone.utc) + timedelta(seconds=.1)).isoformat())
        except PlannedRenewal:
            return 75

    def phase(name, role):
        if name.startswith("controller_"):
            host.write_json(root / (name + "-" + host.FIXTURE_REVISION + ".json"), {"pid": os.getpid(), "role": role})
        if config.get("stop_phase") == name:
            host.write_json(root / "session/stop.json", {"reason": "MANUAL_STOP"})
        if name == "controller_draining" and config.get("rollback"):
            host.write_json(root / "installed.json", {"path": str(root / "host-v1.py")})

    def commands(envelope):
        def command(role, request):
            return [base_python, "-I", "-S", "-B", __file__, "worker", str(root), str(host_path), str(request),
                    "--runtime-tag", runtime_tag]

        def relay_selector():
            installed = json.loads((root / "installed.json").read_text())
            selected = Path(installed["path"])
            selected_runtime = installed.get("runtime", "v1")
            import hashlib
            digest = hashlib.sha256(selected.read_bytes()).hexdigest()
            runtime_changed = selected_runtime != runtime_tag
            if digest == host.HOST_SHA256 and not runtime_changed:
                return None
            return {"host_sha256": digest, "runtime_changed": runtime_changed,
                    "command": [base_python, "-I", "-S", "-B", __file__, "controller", str(root),
                                str(selected), "--runtime-tag", selected_runtime]}
        return command, relay_selector

    if "--continue" in sys.argv:
        if config.get("crash_successor"):
            os._exit(23)
        path = Path(sys.argv[sys.argv.index("--continue") + 1])
        return host.resume_controller(path, commands=commands, phase=phase)
    host.bind_kernel_lifetime()
    envelope = {"trader": config.get("trader", "gameplan"), "datastore": str(root),
                "session": {"session_id": "one-manually-started-session", "started_at": datetime.now(timezone.utc).isoformat()},
                "check_controls": False, "roles": ["forecast", "execution"]}
    command, selector = commands(envelope)
    with host.own_session(root, envelope["trader"]):
        return host.run_protocol(directory=root / "session", envelope=envelope, command=command,
                                 initialize=False, phase=phase, relay_selector=selector)


if __name__ == "__main__":
    raise SystemExit(main())
