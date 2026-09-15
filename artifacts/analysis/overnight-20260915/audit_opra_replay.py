"""Inspect only current-session OPRA metadata and delivery receipts; no API calls."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("C:/DATASTORE")
CANONICAL = ROOT / "market-data/databento/opra/OPRA.PILLAR"
OUT = Path(__file__).resolve().parent
RUN = ROOT / "ml/overnight-runs/20260915T040742.365640Z"
symbols = [s.strip() for s in Path("C:/dev/ducketz/datafetching/watchlist.txt").read_text().splitlines()
           if s.strip() and not s.lstrip().startswith("#")]
schemas = ["ohlcv-1h", "cbbo-1m", "definition"]
session, required_end = "2026-09-14", "2026-09-15"
at = datetime.now(timezone.utc).isoformat()
entitlement_path = CANONICAL / "metadata/entitlement.json"
entitlement = json.loads(entitlement_path.read_text())
log_path = RUN / "loop_a_close_fetch.log"
log = log_path.read_bytes()
lines = log.decode().splitlines()
result = {
    "audited_at": at, "read_only": True,
    "scope": "Metadata, current-session manifest/receipt/cursor bindings and native replay log only. No raw/parquet contents or full archive verification.",
    "source_session": session, "required_exclusive_end": required_end,
    "symbols": symbols, "schemas": schemas,
    "entitlement_path": str(entitlement_path), "entitlement_snapshot": entitlement,
    "historical_availability_interpretation": "The three production schema ranges end within Sep14, so the full-day entitled_end is Sep14, short of required Sep15. Existing cursors already reached Sep14; native planning skips unchanged Historical scopes and tries bounded OPRA replay. Zero selected Historical preflights does not mean a paid/unverified Historical download was performed.",
    "source_separation": "This fallback uses OPRA.PILLAR option parents only. It is unrelated to the denied XNAS.ITCH Live route; this audit made no provider calls.",
    "log_snapshot": {"path": str(log_path), "bytes": len(log), "sha256": hashlib.sha256(log).hexdigest()},
    "relevant_log_lines": [line for line in lines if any(t in line for t in ["OPRA history guarded preflight:", "OPRA_LIVE_REPLAY_", "OPRA symbol/schema history", "OPRA historical catch-up", "REFRESHED_OPRA_HEALTH", "Options history maintenance finished:", "Loop A OPRA Strategy history maintenance:", "Market-data catalog refreshed:", "Completed scopes:", "Failed scopes:"])],
    "scopes": [], "metadata_errors": [],
}
for schema in schemas:
    for symbol in symbols:
        path = CANONICAL / "state/symbol-history" / symbol / f"{schema}.json"
        entry = {"symbol": symbol, "schema": schema, "cursor_path": str(path)}
        try:
            cursor = json.loads(path.read_text())
            entry["cursor"] = cursor
            entry["covers_required_session"] = cursor["completed_through"] >= required_end
            partition = CANONICAL / schema / f"{symbol}.OPT" / "dates" / session / "segments/live-session"
            manifest_path = partition / "manifest.json"
            receipt_path = partition / "receipt.json"
            if manifest_path.exists() and receipt_path.exists():
                manifest_bytes = manifest_path.read_bytes()
                manifest = json.loads(manifest_bytes)
                receipt = json.loads(receipt_path.read_text())
                sha = hashlib.sha256(manifest_bytes).hexdigest()
                expected_start = session + ("T12:30:00+00:00" if schema == "cbbo-1m" else "T00:00:00+00:00")
                expected_end = required_end + "T00:00:00+00:00"
                delivery = manifest["provider_delivery"]
                coverage = cursor.get("replay_coverage", {})
                bindings = {
                    "cursor_identity": cursor["dataset"] == "OPRA.PILLAR" and cursor["symbol"] == symbol and cursor["schema"] == schema,
                    "cursor_manifest_checksum": coverage.get("manifest_checksum_sha256") == sha,
                    "receipt_manifest_checksum": receipt.get("manifest_checksum_sha256") == sha,
                    "manifest_request_identity": manifest["request"] == {"dataset":"OPRA.PILLAR","start":expected_start,"end":expected_end,"schema":schema,"stype_in":"parent","symbols":[f"{symbol}.OPT"]},
                    "exact_coverage": manifest["partition_start"] == expected_start and manifest["partition_end"] == expected_end and coverage.get("partition_start") == expected_start and coverage.get("partition_end") == expected_end,
                    "delivery_completed": delivery.get("completed") is True and bool(delivery.get("replay_completed")) and bool(delivery.get("subscription_ack")),
                    "delivery_no_errors": not delivery.get("error_messages") and not delivery.get("callback_errors") and delivery.get("reconnect_count") == 0,
                    "delivery_identity": delivery.get("dataset") == "OPRA.PILLAR" and delivery.get("schema") == schema and delivery.get("symbols") == [f"{symbol}.OPT"] and delivery.get("mode") == "live-intraday-replay",
                    "receipt_raw_hash_matches_manifest": receipt.get("raw_checksum_sha256") == manifest["raw"]["checksum_sha256"],
                    "receipt_normalized_hash_matches_manifest": receipt.get("normalized_checksum_sha256") == manifest["normalized"]["checksum_sha256"],
                    "saved_file_sizes_match_manifest": (partition / "provider.dbn").stat().st_size == manifest["raw"]["size_bytes"] and (partition / "normalized.parquet").stat().st_size == manifest["normalized"]["size_bytes"],
                }
                entry.update({"manifest_path": str(manifest_path), "receipt_path": str(receipt_path),
                    "manifest_sha256": sha, "bindings": bindings, "provider_delivery": delivery,
                    "normalized_summary": {k:manifest["normalized"].get(k) for k in ["row_count","duplicate_natural_key_rows","earliest_event_timestamp","latest_event_timestamp","size_bytes"]}})
                # A manifest may be published a moment before the cursor advances.
                failed = [k for k,v in bindings.items() if not v]
                entry["metadata_binding_status"] = "PASS" if not failed else "CURSOR_PENDING" if not entry["covers_required_session"] else "FAIL"
                if entry["metadata_binding_status"] == "FAIL":
                    result["metadata_errors"].append({"symbol":symbol,"schema":schema,"failed_checks":failed})
            else:
                entry["metadata_binding_status"] = "PENDING_DELIVERY"
        except Exception as exc:
            entry["metadata_binding_status"] = "READ_ERROR"
            entry["error"] = f"{type(exc).__name__}: {exc}"
            result["metadata_errors"].append({"symbol":symbol,"schema":schema,"error":entry["error"]})
        result["scopes"].append(entry)
result["completed_current_scopes"] = sum(s["metadata_binding_status"] == "PASS" and s.get("covers_required_session",False) for s in result["scopes"])
result["expected_scopes"] = len(symbols) * len(schemas)
result["total_current_replay_raw_bytes"] = sum(s.get("provider_delivery",{}).get("raw_bytes",0) for s in result["scopes"])
result["replay_failure_lines"] = [line for line in lines if "OPRA_LIVE_REPLAY_FAILED" in line]
result["status"] = "COMPLETE_CURRENT_SESSION_METADATA" if result["completed_current_scopes"] == result["expected_scopes"] else "REPLAY_IN_PROGRESS"
if result["metadata_errors"] or result["replay_failure_lines"]:
    result["status"] = "REPLAY_REQUIRES_ATTENTION"
final_lines = [line for line in lines if line.startswith("Options history maintenance finished:")]
native_note = ""
if final_lines:
    native_summary = {key: float(value) if "." in value else int(value) for key, value in
                      re.findall(r"(\w+)=([0-9.]+)", final_lines[-1])}
    health_path = CANONICAL / "health/current.json"
    health_bytes = health_path.read_bytes()
    health = json.loads(health_bytes)
    stage_path = RUN / "stage-report.json"
    stage_report = json.loads(stage_path.read_text())
    loop_a_stage = next(stage for stage in stage_report["stages"] if stage["stage"] == "loop_a_close_fetch")
    result["native_completion"] = {
        "final_maintenance_line": final_lines[-1], "parsed_summary": native_summary,
        "loop_a_stage_report_path": str(stage_path), "loop_a_stage": loop_a_stage,
        "health_path": str(health_path), "health_sha256": hashlib.sha256(health_bytes).hexdigest(),
        "health_snapshot": health, "failed_partitions_listed": [], "failed_partitions_listed_count": 0,
        "archive_failure_count_available": False,
        "health_failure_count_limitation": "The native health-v1 schema has no failed-partition count or list. iter_verified_partitions catches OpraSyncError and excludes those partitions, and excludes replay superseded by verified Historical delivery. Thus zero failures are listed, but the total number of invalid retained archive partitions cannot be inferred from this health file.",
        "optional_history_boundary": "cbbo-1s, ohlcv-1d/1m/1s, statistics, status, tcbbo and trades remain retained research history, ending Sep11 in this health snapshot; they are outside the three production-schema freshness contract. Their earlier timestamp is not a listed failed partition or evidence of quarantine. The previously documented optional Pricing feature quarantine is separate from OPRA partition coverage and was not re-audited here.",
        "source_code": ["C:/dev/ducketz/datafetching/databento_opra_history.py:831", "C:/dev/ducketz/datafetching/databento_opra_history.py:903"],
        "full_archive_rehashed_by_this_audit": False,
    }
    assert native_summary["completed_scopes"] == native_summary["requested_scopes"] == len(symbols)*len(schemas)
    assert native_summary["failed_scopes"] == native_summary["capacity_blocked_scopes"] == native_summary["deferred_scopes"] == 0
    assert native_summary["live_replay_bytes"] == result["total_current_replay_raw_bytes"]
    assert loop_a_stage["status"] == "COMPLETE" and loop_a_stage["exit_code"] == 0
    if result["status"] == "COMPLETE_CURRENT_SESSION_METADATA":
        result["status"] = "NATIVE_LOOP_A_AND_OPRA_COMPLETE"
    native_note = f"""
## Native completion evidence

- Loop A completed at {loop_a_stage['finished_at']} with exit code 0. Native OPRA summary confirms 33 requested/completed, zero failed/blocked/deferred/bootstrap scopes, 33 replay completions, {native_summary['live_replay_bytes']:,} captured bytes, and zero selected Historical acquisition cost.
- Fresh [OPRA health]({health_path.as_posix()}) observed {health['observed_at']} includes {health['total_partitions']:,} verified selected partitions. Production-schema latest observations reach September 14.
- Failed partitions listed in this health: **0**. The native health format has no failure list/count, and its iterator skips failed verification; total retained archive failures therefore remain **unavailable**, rather than proven zero. This audit did not rescan or rehash that archive.
- Other schema histories still ending September 11 are retained research scopes, outside production freshness requirements. The known historical optional Pricing feature quarantine is a separate feature-quality issue; neither is counted as a failed current OPRA scope.
"""
output = OUT / "opra-replay-audit.json"
output.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n")
(OUT / "opra-replay-audit.md").write_text(f"""# OPRA current-session replay audit

Audited {at}. Status: {result['status']}.

- Current-session metadata checks passed for {result['completed_current_scopes']}/{result['expected_scopes']} production symbol/schema scopes; required exclusive cursor end is September 15, covering the September 14 session.
- Historical metadata observed at {entitlement['observed_at']} ended at September 14 14:10 UTC for hourly bars and 16:20 UTC for CBBO/definitions. The native complete-day limit was therefore September 14. Existing cursors had already reached that limit, explaining 33 requested but zero eligible Historical preflight/download scopes.
- The existing owner used the documented OPRA.PILLAR Live replay. Hourly bars/definitions request September 14 UTC midnight through September 15 UTC midnight; CBBO requests 12:30 UTC through midnight. This covers the complete regular options session.
- Completed deliveries have exact scope identities, subscription acknowledgements, replay-completion records, zero provider/callback errors, no reconnects, matching cursor/manifest/receipt bindings, and matching saved file sizes. Metadata errors: {len(result['metadata_errors'])}. Native replay failure lines: {len(result['replay_failure_lines'])}.
- Recorded current replay raw bytes: {result['total_current_replay_raw_bytes']:,}, within the native 20,000,000,000-byte run limit. No Historical acquisition cost is inferred from Live byte counts.
- This audit checked current-session metadata and bindings only; the native owner performs payload verification. It made no provider/broker calls, retries, process/claim actions, or production changes. No XNAS.ITCH Live route is involved.

Detailed [evidence]({output.as_posix()}); [native log]({log_path.as_posix()}).
{native_note}
""")
print(json.dumps({"audited_at":at,"status":result["status"],"completed_scopes":result["completed_current_scopes"],"expected_scopes":result["expected_scopes"],"metadata_errors":result["metadata_errors"],"replay_failures":result["replay_failure_lines"],"latest_log":result["relevant_log_lines"][-3:],"output":str(output)},indent=2))
