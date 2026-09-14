"""Offline sensitivity: exclude native candidate zero-volume bars without editing them."""
from pathlib import Path
import json
import sys
import pandas as pd
import numpy as np
import exchange_calendars as xcals
sys.path.insert(0, str(Path.cwd()))
from ml.independent_stock_targets import stock_target_windows, _target_observations, STOCK_TARGET_BOUNDARY_TOLERANCE

OUT = Path(__file__).parent / "source-probe/validation-120"
run = json.loads((OUT/"run.json").read_text())
assert run["status"] == "COMPLETE"
cal = xcals.get_calendar("XNYS", start="2026-03-01", end="2026-10-01")
end = pd.Timestamp(run["request_end"])
data = pd.concat([pd.read_parquet(OUT/s/"observations.parquet").rename(columns={"ts_event":"timestamp"}) for s in run["symbols"]], ignore_index=True)
data = data.loc[data.volume.gt(0)].copy()
by_symbol = {s:f.sort_values("timestamp").reset_index(drop=True) for s,f in data.groupby("symbol")}
windows = pd.DataFrame([{**w,"symbol":s,"action_date":d} for d in run["sessions"] for s in run["symbols"] for w in stock_target_windows(pd.Timestamp(d).date(),calendar=cal) if w["execution_eligible"]])
observed = _target_observations(windows, by_symbol=by_symbol)
valid = (np.isfinite(observed.entry_price)&np.isfinite(observed.exit_price)&observed.entry_price.gt(0)&observed.exit_price.gt(0)
    &(observed.observed_open_timestamp-windows.target_window_start).abs().le(STOCK_TARGET_BOUNDARY_TOLERANCE)
    &(observed.observed_close_timestamp-windows.target_window_end).abs().le(STOCK_TARGET_BOUNDARY_TOLERANCE)
    &observed.observed_open_timestamp.lt(observed.observed_close_timestamp))
route_rows = windows[["symbol","route","action_date"]].copy()
route_rows["mature"] = windows.target_window_end.le(end)
route_rows["available"] = valid & route_rows.mature
routes = route_rows[route_rows.mature].groupby(["symbol","route"]).available.agg(["size","sum"]).reset_index().rename(columns={"size":"mature_windows","sum":"available"})
routes["missing"] = routes.mature_windows-routes.available
routes["missing_rate"] = routes.missing/routes.mature_windows
rows=[]
for s,bars in by_symbol.items():
    times=pd.DatetimeIndex(bars.timestamp)
    for d in run["sessions"]:
        session=pd.Timestamp(d)
        local=pd.Timestamp(d,tz="America/Los_Angeles")
        op,cl=cal.session_open(session),cal.session_close(session)
        for close in (False,True):
            for h in (range(5,18) if close else range(4,17)):
                clock=(local+pd.Timedelta(hours=h)).tz_convert("UTC")
                p=times.searchsorted(clock-pd.Timedelta(minutes=1),side="right")-1 if close else times.searchsorted(clock,side="left")
                actual=times[p]+(pd.Timedelta(minutes=1) if close else pd.Timedelta(0)) if 0<=p<len(times) else pd.NaT
                distance=((clock-actual) if close else (actual-clock)).total_seconds()/60 if pd.notna(actual) else None
                regular=op<clock<=cl if close else op<=clock<cl
                period="regular" if regular else "premarket" if clock<op else "afterhours"
                rows.append({"symbol":s,"session":d,"clock_pacific":f"{h:02}:00","kind":"close" if close else "open","period":period,"observed_at":actual,"distance_minutes":distance,"available":distance is not None and 0<=distance<=5})
boundaries=pd.DataFrame(rows)
rates=boundaries.groupby(["symbol","period"]).available.agg(["size","sum"]).reset_index().rename(columns={"size":"boundaries","sum":"available"})
rates["missing"]=rates.boundaries-rates.available
rates["missing_rate"]=rates.missing/rates.boundaries
boundaries.to_parquet(OUT/"positive-volume-boundaries.parquet",index=False)
route_rows.to_parquet(OUT/"positive-volume-entry-windows.parquet",index=False)
summary={"generated_at":pd.Timestamp.now(tz="UTC").isoformat(),"dataset":"XNAS.BASIC","policy":"Sensitivity only: native volume > 0, same five-minute boundary selectors, no provider data changed", "boundary_rates":rates.to_dict("records"),"entry_routes":routes.to_dict("records")}
(OUT/"positive-volume-sensitivity.json").write_text(json.dumps(summary,indent=2)+"\n")
print(json.dumps({"status":"COMPLETE","boundary_rates":summary["boundary_rates"]},indent=2))
