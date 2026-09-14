"""Read-only provider quality metadata for the bounded candidate comparison."""
import json
import os
import sys
from pathlib import Path
import databento as db
import pandas as pd
sys.path.insert(0,str(Path.cwd()))
from datafetching.cme_runtime import load_repository_environment
from datafetching.databento_xnas_replay import _external_failure

root=Path(__file__).parent/"source-probe/validation-120"
report={"observed_at":pd.Timestamp.now(tz="UTC").isoformat(),"datasets":{},"status":"COMPLETE"}
try:
    load_repository_environment()
    client=db.Historical(os.environ["DATABENTO_API_KEY"])
    for dataset in ("XNAS.BASIC","XNAS.ITCH"):
        rows=client.metadata.get_dataset_condition(dataset=dataset,start_date="2026-03-20",end_date="2026-09-11")
        report["datasets"][dataset]=rows
        print(json.dumps({"dataset":dataset,"non_available": [r for r in rows if r.get("condition") not in ("available", "holiday", "weekend")]}),flush=True)
except Exception as exc:
    report.update(status="BLOCKED",error=_external_failure(exc))
(root/"provider-conditions.json").write_text(json.dumps(report,indent=2,default=str)+"\n")
