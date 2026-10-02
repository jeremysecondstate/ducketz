"""Read-only source-session OPRA reconciliation; no provider/runtime writes."""
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
import hashlib
import json
import math
import re
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, "C:/dev/ducketz")
from datafetching.databento_opra_history import verify_partition
from datafetching.opra_replay_fallback import verify_replay_cursor_coverage

ROOT = Path("C:/DATASTORE")
REPO = Path("C:/dev/ducketz")
OPRA = ROOT / "market-data/databento/opra/OPRA.PILLAR"
RUN = ROOT / "ml/overnight-runs/20261002T040848.225355Z"
OUT = Path(__file__).resolve().parent
SOURCE = "2026-10-01"
END = "2026-10-02"
SCHEMAS = ("ohlcv-1h", "cbbo-1m", "definition")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path):
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def main():
    started = datetime.now(timezone.utc)
    symbols = [x.strip() for x in (REPO / "datafetching/watchlist.txt").read_text().splitlines()
               if x.strip() and not x.lstrip().startswith("#")]
    log_path = RUN / "loop_a_close_fetch.log"
    lines = log_path.read_text().splitlines()
    summaries = [x for x in lines if x.startswith("Options history maintenance finished:")]
    if len(summaries) != 1 or "completed_scopes=33;" not in summaries[0] or "failed_scopes=0;" not in summaries[0]:
        raise RuntimeError("Native reconciliation is not yet complete and passing")
    scope_rows, errors = [], []
    for schema in SCHEMAS:
        for symbol in symbols:
            cursor_path = OPRA / "state/symbol-history" / symbol / (schema + ".json")
            cursor = read(cursor_path)
            row = {"symbol": symbol, "schema": schema, "cursor_path": str(cursor_path),
                   "cursor_sha256": digest(cursor_path), "cursor": cursor}
            try:
                assert cursor["provider"] == "databento-opra" and cursor["dataset"] == "OPRA.PILLAR"
                assert cursor["symbol"] == symbol and cursor["provider_symbol"] == symbol + ".OPT"
                assert cursor["schema"] == schema and cursor["completed_through"] == END
                replay = cursor.get("replay_coverage")
                if replay:
                    verify_replay_cursor_coverage(ROOT, symbol=symbol, schema=schema, cursor=cursor)
                    directory = (ROOT / replay["manifest_path"]).parent
                    manifest = read(directory / "manifest.json")
                    receipt = read(directory / "receipt.json")
                else:
                    directory = OPRA / schema / (symbol + ".OPT") / "dates" / SOURCE / "segments/full-day"
                    verified = verify_partition(directory, datastore_root=ROOT)
                    manifest, receipt = verified["manifest"], verified["receipt"]
                request = manifest["request"]
                assert manifest["dataset"] == "OPRA.PILLAR" and manifest["schema"] == schema
                assert manifest["partition_date"] == SOURCE and manifest["symbol_scope"] == symbol + ".OPT"
                assert request["dataset"] == "OPRA.PILLAR" and request["schema"] == schema
                assert request["symbols"] == [symbol + ".OPT"] and request["stype_in"] == "parent"
                row.update(status="VERIFIED", directory=str(directory),
                           manifest_sha256=digest(directory / "manifest.json"),
                           receipt_sha256=digest(directory / "receipt.json"),
                           request=request, partition_start=manifest["partition_start"],
                           partition_end=manifest["partition_end"],
                           delivery=manifest.get("provider_delivery", {}),
                           raw=manifest["raw"], normalized=manifest["normalized"],
                           replay=bool(replay), published_at=receipt["published_at"])
            except Exception as exc:
                row.update(status="FAILED_VERIFICATION", error=type(exc).__name__ + ": " + str(exc))
                errors.append(symbol + "/" + schema + ": " + row["error"])
            scope_rows.append(row)
    entitlement_path = OPRA / "metadata/entitlement.json"
    entitlement = read(entitlement_path)
    preflights = []
    for schema in SCHEMAS:
        for symbol in symbols:
            for path in (OPRA / "metadata/preflights" / schema / (symbol + ".OPT")).glob("*_to_2026-10-02/preflight.json"):
                value = read(path)
                if value.get("generated_at", "") < "2026-10-02T04:08:48":
                    continue
                checksum = value.get("semantic_checksum_sha256")
                payload = {k: v for k, v in value.items() if k != "semantic_checksum_sha256"}
                semantic = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                estimate = value["estimates"][schema]
                checks = {"semantic_checksum": semantic == checksum,
                          "single_scope": value["scope"]["schemas"] == [schema] and value["scope"]["symbols"] == [symbol + ".OPT"],
                          "zero_cost": value["cost_estimates_complete"] is True and value["estimated_cost_usd"] == 0 and estimate["estimated_cost_usd"] == 0,
                          "finite_nonnegative_counts": all(isinstance(value[x], (int, float)) and math.isfinite(value[x]) and value[x] >= 0 for x in ("record_count", "estimated_download_size_bytes")),
                          "capacity": value["capacity_pass"] is True and value["available_free_bytes"] >= value["required_free_bytes"]}
                preflights.append({"path": str(path), "sha256": digest(path), "scope": value["scope"],
                                   "estimated_cost_usd": value["estimated_cost_usd"],
                                   "estimated_download_size_bytes": value["estimated_download_size_bytes"],
                                   "record_count": value["record_count"], "checks": checks})
                if not all(checks.values()):
                    errors.append("PREFLIGHT_CHECK_FAILED: " + symbol + "/" + schema)
    failed_lines = [x for x in lines if x.startswith("OPRA symbol/schema history preflight failed:")]
    failed_scopes = []
    for line in failed_lines:
        match = re.search(r"symbol=([^;]+); schema=([^;]+);", line)
        if match:
            symbol, schema = match.groups()
            corresponding = next(x for x in scope_rows if x["symbol"] == symbol and x["schema"] == schema)
            failed_scopes.append({"symbol": symbol, "schema": schema,
                                  "native_failure_retained": line,
                                  "final_resolution": corresponding["status"],
                                  "resolution_delivery": corresponding.get("delivery", {}).get("mode")})
    result = {"schema_version": 1, "status": "OPRA_SOURCE_SESSION_VERIFIED" if not errors else "OPRA_SOURCE_SESSION_VERIFICATION_FAILED",
              "started_at_utc": started.isoformat(), "completed_at_utc": datetime.now(timezone.utc).isoformat(),
              "native_run_id": RUN.name, "source_session": SOURCE, "required_exclusive_end": END,
              "expected_scopes": len(symbols) * len(SCHEMAS), "verified_scopes": sum(x["status"] == "VERIFIED" for x in scope_rows),
              "native_summary": summaries[0], "delivery_counts": dict(Counter(x.get("delivery", {}).get("mode", "UNKNOWN") for x in scope_rows)),
              "scope_evidence": scope_rows, "failed_preflights_reconciled": failed_scopes,
              "successful_preflight_count": len(preflights), "preflights": preflights,
              "entitlement_evidence": {"path": str(entitlement_path), "sha256": digest(entitlement_path), "observed_at": entitlement["observed_at"]},
              "source_evidence": [{"path": p, "sha256": digest(REPO / p)} for p in ("datafetching/databento_opra_history.py", "datafetching/opra_replay_fallback.py", "datafetching/options_runtime.py")],
              "errors": errors, "provider_calls": 0, "production_writes": 0, "source_changes": 0,
              "limitations": ["Historical preflight failures remain preserved; Live replay has distinct exact session coverage and does not manufacture their missing metadata replies.",
                              "Replay CBBO covers one hour before regular open through midnight, not the earlier UTC hours.",
                              "This verifies OPRA source coverage, not whole overnight completion or model admission."]}
    path = OUT / started.strftime("opra-source-session-verification-%Y%m%dT%H%M%SZ.json")
    path.write_text(json.dumps(result, indent=2, default=str) + "\n")
    print(json.dumps({"path": str(path), "status": result["status"], "verified_scopes": result["verified_scopes"],
                      "delivery_counts": result["delivery_counts"], "successful_preflight_count": len(preflights), "errors": errors}, indent=2))


if __name__ == "__main__":
    main()
