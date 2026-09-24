from pathlib import Path
import json
r=Path("C:/DATASTORE/ml/overnight-runs/20260924T044352.904210Z");out=Path(__file__).parent;s=json.loads((r/"stage-report.json").read_text());p=r/"loop_a_close_fetch.log";o=out/"log-offset.json";old=json.loads(o.read_text()) if o.exists() else {"offset":0}
with p.open("rb") as f:f.seek(old["offset"]);b=f.read();end=f.tell()
lines=b.decode("utf-8",errors="replace").splitlines();o.write_text(json.dumps({"offset":end,"heartbeat":s["heartbeat_at"]}))
interest=[l for l in lines if any(k in l.lower() for k in ("error","exception","warning","failed","cme","did not resolve","opra history","opra symbol/schema","schwab] fetching","schwab/options]","loop-a.databento-watchlist","changed parquet files:"))]
print(json.dumps({"heartbeat":s["heartbeat_at"],"status":s["status"],"current_stage":s["current_stage"],"loop_a_stage":next((x for x in s.get("stages",[]) if x["stage"]=="loop_a_close_fetch"),None),"recent_issues":s["recent_issues"],"new_interesting":interest[-25:],"tail":lines[-3:]},indent=2))
