"""Bounded current-session price-availability triage; no source edits or fetches."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0,"C:/dev/ducketz")
import pandas as pd
from ml.gameplan_price_completion import _complete_source_coverage

ROOT=Path("C:/DATASTORE")
RUN=ROOT/"ml/gameplan-trade-plan-runs/20260915T054710.644968Z"
OUT=Path(__file__).resolve().parent
report=json.loads((RUN/"report.json").read_text())
completion=json.loads((RUN/"planning-reference-completion.json").read_text())
path_report=json.loads((RUN/"planning-price-path.json").read_text())
ledger=json.loads((RUN/"direction-ledger.json").read_text())
inventory=report["source_price_inventory"]
results={}
for symbol in ["IONQ","PATH"]:
    reference=completion["references"][f"{symbol}|2026-09-15"]
    metadata=next(p for p in inventory["partitions"] if p.get("symbol")==symbol and p.get("end")=="2026-09-15")
    manifest_path=Path(metadata["manifest_path"])
    manifest=json.loads(manifest_path.read_text())
    receipt=json.loads((manifest_path.parent/"receipt.json").read_text())
    normalized_path=manifest_path.parent/"normalized.parquet"
    # These two current partitions total only about 81 KB normalized. No full
    # symbol archive, native DBN payload, or broad file inventory is rehashed.
    frame=pd.read_parquet(normalized_path).reset_index()
    current=frame.loc[frame.ts_event.ge(pd.Timestamp("2026-09-14",tz="UTC"))]
    coverage=_complete_source_coverage(inventory,symbol=symbol,
        origin_start=pd.Timestamp(reference["origin_bar_start"]),boundary=pd.Timestamp(reference["boundary_at"]),
        now=pd.Timestamp(completion["observed_at"]))
    points={key:{k:v for k,v in value.items() if k!="samples"} for key,value in path_report["points"].items() if key.startswith(symbol+"|")}
    results[symbol]={
        "saved_current_reference":reference,"saved_missing_price_count":inventory["missing_price_rows_by_symbol"][symbol],
        "saved_missing_price_examples":[x for x in inventory["missing_price_examples"] if x["symbol"]==symbol],
        "current_partition_metadata":metadata,"current_partition_manifest":manifest,"current_partition_receipt":receipt,
        "current_partition_checks":{
            "manifest_receipt_binding":hashlib.sha256(manifest_path.read_bytes()).hexdigest()==receipt["manifest_checksum_sha256"],
            "normalized_sha_matches_manifest_and_receipt":hashlib.sha256(normalized_path.read_bytes()).hexdigest()==manifest["normalized"]["checksum_sha256"]==receipt["normalized_checksum_sha256"],
            "normalized_rows":len(frame),"row_count_matches_manifest":len(frame)==manifest["normalized"]["row_count"],
            "undefined_open_and_close_rows":int((frame.open.isna()&frame.close.isna()).sum()),
            "source_session_rows":len(current),"source_session_undefined_open_and_close_rows":int((current.open.isna()&current.close.isna()).sum()),
            "last_source_session_rows":current[["ts_event","open","close","volume"]].tail(3).to_dict("records"),
        },
        "read_only_source_coverage_helper_result":coverage,
        "coverage_result_interpretation":"The saved verified-source metadata satisfies the interval coverage helper. Native completion did not reach this check, because the symbol-wide undefined-price guard returned first.",
        "price_path_points":points,
    }
    assert coverage is not None
    assert reference["reason"]=="UNDEFINED_NATIVE_PRICE_OBSERVATIONS" and reference["fill_count"]==0
    assert results[symbol]["current_partition_checks"]["manifest_receipt_binding"]
    assert results[symbol]["current_partition_checks"]["normalized_sha_matches_manifest_and_receipt"]
    assert results[symbol]["current_partition_checks"]["undefined_open_and_close_rows"]==0
result={
    "audited_at":pd.Timestamp.now(tz="UTC").isoformat(),"read_only":True,
    "status":"INTENTIONAL_SYMBOL_WIDE_UNDEFINED_PRICE_GATE_NO_CURRENT_COVERAGE_FAILURE",
    "trade_plan_run":str(RUN),"completion_contract":completion["contract_version"],"path_contract":path_report["contract_version"],
    "maximum_gap_minutes":completion["max_gap_minutes"],"symbols":results,
    "guard_order":"After actual-observation tolerance, maximum gap and regular-session checks, extended-hours completion rejects any nonzero missing_price_rows_by_symbol count before _complete_source_coverage. It checks the loaded symbol history, not only the proposed trailing interval.",
    "classification":"The 13-minute IONQ and 28-minute PATH gaps are within policy and have covering verified current acquisitions. They remain unavailable because each symbol has preserved undefined native observations years earlier. Code and an explicit regression test require this conservative symbol-wide gate. No concrete code defect or current acquisition failure is established by this audit.",
    "price_path_effect":{"unavailable_point_count":sum(len(v["price_path_points"]) for v in results.values()),
        "status_counts":{s:sum(p["status"]==s for v in results.values() for p in v["price_path_points"].values()) for s in ["UNAVAILABLE_REFERENCE_PRICE","UNAVAILABLE_MINIMUM_SAMPLES"]},
        "ledger_status":ledger["status"],"events":len(ledger["events"]),"hourly_rows":len(ledger["hourly"]),
        "summary":ledger["summary"],"ending_positions":ledger["ending_positions"]},
    "source_evidence":[str(RUN/name) for name in ["planning-reference-completion.json","planning-price-path.json","report.json","direction-ledger.json"]],
    "implementation_evidence":["C:/dev/ducketz/ml/gameplan_price_completion.py:253","C:/dev/ducketz/ml/stock_target_prices.py:126","C:/dev/ducketz/tests/test_gameplan_price_completion.py:278","C:/dev/ducketz/docs/loops-system-analysis/NIGHTLY_GAMEPLAN.md:245"],
    "limitations":"Read-only current two-partition metadata and normalized checks; no broad source archive rehash, provider request, native data edit, synthesized bar creation, retraining or production mutation. Historical missing-price examples were read from the publication-bound loader inventory rather than loading old archives again.",
}
output=OUT/"price-availability-audit.json"
output.write_text(json.dumps(result,indent=2,default=str,sort_keys=True)+"\n")
(OUT/"price-availability-audit.md").write_text(f"""# IONQ and PATH planning references remain unavailable

Audited {result['audited_at']}. This is an intentional native source-quality guard; no current acquisition failure or concrete code defect was established.

| Symbol | Last observed minute close | Boundary gap | Recorded undefined native rows |
|---|---|---:|---|
| IONQ | September 14, 23:47 UTC | 13 minutes | 3, on September 19, 2022 at 09:06, 09:08 and 09:32 UTC |
| PATH | September 14, 23:32 UTC | 28 minutes | 1, on October 26, 2021 at 22:20 UTC |

Both gaps fit the 240-minute after-hours policy. Their fresh September 10–15 XNAS.ITCH partitions cover the exact origin through September 15 00:00 UTC, were published before the planning cutoff, contain no provider warnings and have zero undefined price rows. Bounded current manifest/receipt and normalized checks pass; the native coverage helper accepts the saved coverage metadata for both.

The existing completion code first checks **any undefined-price count in the symbol's loaded history**. IONQ's count is 3 and PATH's is 1, so it returns `UNDEFINED_NATIVE_PRICE_OBSERVATIONS` before checking coverage. This is explicitly required by the existing regression test. Consequently `source_coverage: null` means the check was not reached; it does not show an incomplete current download.

All 28 affected hourly path points retain `UNAVAILABLE_REFERENCE_PRICE`. The shared direction ledger correctly records `UNAVAILABLE_PRICE_REFERENCES`, with no events, hourly cash projection, ending cash summary or projected ending holdings. No prices or synthetic rows were created by this audit.

Detailed [evidence]({output.as_posix()}); [saved completion]({(RUN/'planning-reference-completion.json').as_posix()}); [native guard](C:/dev/ducketz/ml/gameplan_price_completion.py:253); [regression test](C:/dev/ducketz/tests/test_gameplan_price_completion.py:278).
""")
print(json.dumps({"status":result["status"],"artifact":str(output),"unavailable_points":result["price_path_effect"],"current_checks":{s:v["current_partition_checks"] for s,v in results.items()}},indent=2,default=str))
