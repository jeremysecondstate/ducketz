"""Offline subprocess probe. Has no broker/provider imports or production roots."""
import json
import os
from pathlib import Path
import sys
import time
from datetime import datetime, timedelta, timezone

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from tools.trader_session_host import bind_kernel_lifetime, own_session, run_protocol, write_json
from ml.trader_renewal import Renewal, PlannedRenewal


def main():
    mode, directory = sys.argv[1], Path(sys.argv[2])
    config = json.loads((directory / "config.json").read_text())
    if mode == "worker":
        renewal = Renewal(sys.argv[3])
        renewal.claim()
        count = renewal.state.get("count", 0) + 1
        if config.get("worker_crash"):
            os._exit(21)
        write_json(directory / f"worker-start-{count}.json", {"pid":os.getpid()})
        if config.get("hold_start"):
            while not (directory / "release").exists() and not renewal.stopped():
                time.sleep(.02)
        renewal.wait_due()
        if renewal.stopped():
            renewal.finish({"status":"STOPPED"})
            return 0
        value = {"pid":os.getpid(),"session_id":renewal.session_id,"count":count,
                 "at":datetime.now(timezone.utc).isoformat(),"version":sys.argv[4]}
        write_json(directory / f"action-{count}.json", value)
        if count == config.get("turns", 3):
            renewal.finish({"status":"COMPLETED"})
            return 0
        try:
            renewal.checkpoint({"count":count}, (datetime.now(timezone.utc) + timedelta(seconds=.15)).isoformat())
        except PlannedRenewal:
            return 75
    bind_kernel_lifetime()
    for relative in ("controls/stock-trader/operator-intent.txt", "controls/gameplan-stock-trader/operator-intent.txt"):
        path = directory / relative
        path.parent.mkdir(parents=True,exist_ok=True)
        key = "CONFIRM_GAMEPLAN_STOCK_TRADING" if "gameplan-stock" in relative else "CONFIRM_ACTIVE_TRADING"
        path.write_text(key + "=TRUE\n")
    write_json(directory / "intraday/manual-session.json", {"session_id":"one-manual-session","status":"RUNNING"})
    envelope = {"trader":config["trader"],"datastore":str(directory),
                "session":{"session_id":"one-manual-session"},"roles":["execution"]}
    calls = {}
    def phase(name, role):
        calls[name] = calls.get(name,0)+1
        write_json(directory / f"phase-{name}-{calls[name]}.json", {"role":role})
        if config.get("stop_phase") == name and calls[name] == config.get("stop_occurrence",1):
            write_json(directory / "session/stop.json", {"reason":"MANUAL_STOP"})
        if config.get("install_phase") == name and calls[name] == 1:
            write_json(directory / "installed.json", {"version":"v2"})
    def command(role, request):
        installed = json.loads((directory / "installed.json").read_text())
        if installed.get("invalid"):
            raise ValueError("Incomplete/invalid installation")
        return [sys.executable,"-I","-S","-B",__file__,"worker",str(directory),str(request),installed["version"]]
    with own_session(directory, config["trader"]):
        return run_protocol(directory=directory/"session",envelope=envelope,command=command,initialize=False,phase=phase)


if __name__ == "__main__":
    raise SystemExit(main())
