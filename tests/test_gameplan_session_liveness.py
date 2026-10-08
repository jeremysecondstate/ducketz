"""Session liveness is separate from a failed trading cycle; no account calls."""
from pathlib import Path
import sys
import pytest
import pandas as pd
from tools import gameplan_execution_readiness as module

@pytest.mark.parametrize("status", ["RUNNING", "DEGRADED"])
def test_live_session_including_degraded_requires_actual_process_and_lock(tmp_path, monkeypatch, status):
    now=pd.Timestamp("2026-10-08T13:25:30Z")
    started=now-pd.Timedelta(seconds=30)
    path=tmp_path/module.SESSION
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    saved={"schema_version":"independent-stock-session-status-v1", "action_date":"2026-10-08",
        "execute":True, "sizing_policy":module.GAMEPLAN_SIZING_POLICY, "status":status,
        "started_at":started.isoformat(), "heartbeat_at":now.isoformat(), "pid":123}
    monkeypatch.setattr(module,"_json",lambda _:saved)
    lock=tmp_path/module.SESSION_LOCK
    lock.parent.mkdir(parents=True)
    lock.write_text("process=independent-stock-session\npid=123\nstarted_at="+started.isoformat()+"\ntoken="+"a"*32+"\n")
    process={"alive":True,"created_at":started-pd.Timedelta(seconds=1),
        "executable":str(Path(getattr(sys,"_base_executable",None) or sys.executable).resolve()),
        "argv":[sys.executable,"-u","-m","ml.gameplan_stock_trader","--datastore-target","pc","--execute",
            "--target-horizon","all","--sizing-policy",module.GAMEPLAN_SIZING_POLICY,"--run-session","--wait-for-open"]}
    result=module._session(tmp_path,"2026-10-08",now,lambda _:process)
    assert result["running"] is True and result["status"]==status
    stopped=module._session(tmp_path,"2026-10-08",now,lambda _:{"alive":False})
    assert stopped["running"] is False
    stale=module._session(tmp_path,"2026-10-08",now+pd.Timedelta(seconds=91),lambda _:process)
    assert stale["running"] is False and stale["status"]=="SAME_DATE_SESSION_HEARTBEAT_STALE"
