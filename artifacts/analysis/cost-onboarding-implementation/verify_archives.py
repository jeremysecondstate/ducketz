"""Verify every planned COST archive's file checksums and normalized row counts."""

import hashlib
import json
from pathlib import Path


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


base = Path(__file__).resolve().parent
plan = json.loads((base / "plan.json").read_text())
results = []
for request, estimate in zip(plan["requests"], plan["estimates"], strict=True):
    directory = Path(request["storage_path"])
    manifests = (sorted(directory.glob("dates/*/segments/*/manifest.json"))
                 if request["dataset"] == "OPRA.PILLAR" else [directory / "manifest.json"])
    if not manifests:
        raise RuntimeError(f"No published manifests: {directory}")
    rows = duplicates = stored_bytes = 0
    dates = []
    warnings = []
    for path in manifests:
        manifest = json.loads(path.read_text())
        receipt = json.loads((path.parent / "receipt.json").read_text())
        assert digest(path) == receipt["manifest_checksum_sha256"], path
        for kind in ("raw", "normalized"):
            evidence = path.parent / manifest[kind]["path"]
            assert evidence.stat().st_size == manifest[kind]["size_bytes"], evidence
            assert digest(evidence) == receipt[kind + "_checksum_sha256"], evidence
            stored_bytes += evidence.stat().st_size
        rows += manifest["normalized"]["row_count"]
        duplicates += manifest["normalized"].get("provider_duplicate_rows_removed", 0)
        if manifest.get("partition_date"):
            dates.append(manifest["partition_date"])
        warnings.extend(manifest.get("provider_warnings", []))
    assert rows > 0, request["request_id"]
    results.append({"request_id": request["request_id"], "dataset": request["dataset"],
        "schema": request["schema"], "start": request["start"], "end": request["end"],
        "partitions": len(manifests), "normalized_rows": rows,
        "provider_duplicate_rows_removed": duplicates,
        "estimated_provider_records": estimate["record_count"],
        "record_difference_after_duplicates": rows + duplicates - estimate["record_count"],
        "published_raw_and_normalized_bytes": stored_bytes,
        "first_published_date": min(dates) if dates else None,
        "last_published_date": max(dates) if dates else None,
        "provider_warnings": warnings, "checksums": "VERIFIED"})
output = {"plan_id": plan["plan_id"], "status": "VERIFIED", "requests": results}
(base / "archive-verification.json").write_text(json.dumps(output, indent=2) + "\n")
print(json.dumps({"verified_requests": len(results), "normalized_rows": sum(r["normalized_rows"] for r in results),
    "record_differences": [{k: r[k] for k in ("dataset", "schema", "normalized_rows", "estimated_provider_records", "record_difference_after_duplicates")}
                           for r in results if r["record_difference_after_duplicates"]],
    "second_quote_records": next(r["normalized_rows"] for r in results if r["schema"] == "cbbo-1s")}))
