"""Read-only audit of saved optional Pricing-family quarantine evidence."""
from __future__ import annotations
import hashlib
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, "C:/dev/ducketz")
import pandas as pd
from ml.artifacts import verify_manifest, file_checksum
from ml.option_pricing.consumers import read_verified_compact_pricing_features

root = Path("C:/DATASTORE")
run = root / "ml/runs/20260912T045017.509319Z"
manifest = verify_manifest(run)
publication = json.loads((run / "publication.json").read_text())
assert publication["manifest_checksum_sha256"] == file_checksum(run / "manifest.json")
cfg = manifest["configuration"]
pricing = cfg["pricing_evidence"]
assert len(pricing["failed_routes"]) == len(pricing["routes"]) == 63
assert pricing["enabled"] and not pricing["downstream_training_eligible"]
assert not cfg["route_errors"]
source, source_files = read_verified_compact_pricing_features(root, available_not_after=cfg["causal_input_cutoff"])
groups = {}
for route in pricing["route_gates"].values():
    for name, item in route["groups"].items():
        groups.setdefault(name, []).append(item)
aggregated = {name: {"route_count": len(items), "passing_routes": sum(item["pass"] for item in items),
    "maximum_complete_fraction": max(item["complete_row_fraction"] for item in items),
    "maximum_fresh_joined_fraction": max(item["fresh_joined_row_fraction"] for item in items),
    "maximum_distinct_targets": max(item["distinct_surface_targets"] for item in items),
    "total_complete_rows": sum(item["complete_rows"] for item in items)} for name, items in groups.items()}
fields = [name for name in source if name in ("symbol", "target_snapshot_for", "available_at", "surface_quality_pass",
    "source_provider", "model_generation", "causal_coverage", "median_normalized_residual", "median_predictive_standard_deviation",
    "median_model_edge_in_half_spreads", "interval_80_coverage", "interval_95_coverage", "median_relative_bid_ask_spread")]
report = {"audited_at": pd.Timestamp.now(tz="UTC").isoformat(), "read_only": True,
    "status": "EXPECTED_OPTIONAL_PRICING_EXCLUSION_DIRECTIONAL_PUBLICATION_COMPLETE",
    "loop_b_run": str(run), "publication": publication, "loop_b_manifest_sha256": file_checksum(run / "manifest.json"),
    "loop_b_outputs_verified": True, "causal_input_cutoff": cfg["causal_input_cutoff"],
    "publication_counts": cfg["publication_counts"], "route_errors": cfg["route_errors"],
    "pricing_gate": pricing, "group_summary": aggregated,
    "verified_pricing_source": {"rows": len(source), "symbols": sorted(source.symbol.unique()),
        "distinct_targets": int(source.target_snapshot_for.nunique()),
        "latest_target": source.target_snapshot_for.max(), "compact_rows": source[fields].to_dict("records"),
        "source_files": [{"path": str(path), "sha256": file_checksum(path)} for path in source_files]},
    "join_freshness_limits": {"1h": "2 hours", "4h": "4 hours", "1d": "2 days", "1w_and_daily_slices": "8 days"},
    "interpretation": "Native coverage/freshness exclusion of optional Pricing features. The reader verified the historical source; this is missing and stale feature coverage, not a checksum or new provider failure. Registered baseline feature contracts preserve exactly the requested non-Pricing features. Stock-only workflow can continue without independent optional Pricing training, subject to later mandatory stock stages and their own model gates.",
    "limitations": "Gate counts summarize the original 229571 materialized rows. Published samples omit closed lockbox rows and total 220713, so this audit does not incorrectly recompute original gate fractions from the reduced published frame. Old Pricing health checked Aug20 is contextual evidence, not a current incident.",
}
path = Path(__file__).resolve().parent / "option-pricing-quarantine.json"
path.write_text(json.dumps(report, indent=2, default=str, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps({"path": str(path), "status": report["status"], "source_rows": len(source),
                  "source_symbols": sorted(source.symbol.unique()), "groups": aggregated}, indent=2))
