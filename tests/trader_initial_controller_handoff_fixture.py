"""Real offline initial-admission relay fixture; no application adapters."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from ml.trader_renewal import Renewal


def main():
    mode, root, host_path, runtime = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
    spec = importlib.util.spec_from_file_location("initial_relay_host", host_path)
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    config = json.loads((root / "config.json").read_text())
    if mode == "worker":
        context = Renewal(sys.argv[5])
        context.claim()
        role = context.request["role"]
        host.write_json(root / ("entered-" + runtime + "-" + role + ".json"), {
            "role": role, "runtime": runtime, "pid": os.getpid(), "controller": os.getppid(),
            "token": context.envelope["token"], "owner_pid": context.envelope["owner_pid"],
            "owner_birth": context.envelope["owner_birth"], "session": context.session,
            "generation": context.request["generation"], "at": time.time()})
        if role == "initialize":
            context.finish({"session": {"session_id": "offline-initial-admission",
                "started_at": "2026-10-28T10:55:22+00:00", "close_at": "2026-10-29T00:00:00+00:00",
                "symbols": ["FIXTURE"], "original_order_id": "unchanged-order"},
                "roles": ["execution", "forecast-0", "scoring"]})
            return 0
        if role == "execution":
            while not (root / "release").exists() and not context.stopped():
                time.sleep(.01)
        context.finish({"status": "STOPPED" if context.stopped() else "COMPLETED"})
        return 0

    def phase(name, role):
        if name.startswith("controller_"):
            host.write_json(root / (name + "-" + runtime + ".json"),
                            {"runtime": runtime, "role": role, "at": time.time()})
        if runtime == "v1" and config.get("stop_phase") == name:
            host.write_json(root / "session/stop.json", {"reason": "MANUAL_STOP"})
        if name == "controller_draining" and config.get("rollback"):
            (root / "rollback").write_text("selected original controller")

    def commands(envelope):
        def command(role, request):
            return [sys._base_executable, "-I", "-S", "-B", __file__, "worker", str(root),
                    str(host_path), runtime, str(request)]
        def selector():
            if runtime == "v2" or (root / "rollback").exists():
                return None
            return {"host_sha256": host.HOST_SHA256, "runtime_changed": True,
                    "command": [sys._base_executable, "-I", "-S", "-B", __file__, "controller",
                                str(root), str(host_path), "v2"]}
        return command, selector

    if "--continue" in sys.argv:
        return host.resume_controller(Path(sys.argv[sys.argv.index("--continue") + 1]),
                                      commands=commands, phase=phase)
    host.bind_kernel_lifetime()
    envelope = {"trader": "intraday", "datastore": str(root), "check_controls": True,
                "session": {"session_id": "uninitialized-offline-admission"}, "roles": []}
    command, selector = commands(envelope)
    with host.own_session(root, "intraday"):
        return host.run_protocol(directory=root / "session", envelope=envelope, command=command,
                                 initialize=True, phase=phase, relay_selector=selector)


if __name__ == "__main__":
    raise SystemExit(main())
