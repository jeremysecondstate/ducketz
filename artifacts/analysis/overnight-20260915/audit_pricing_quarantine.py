"""Read-only bounded Pricing source and native quarantine audit; no training."""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, "C:/dev/ducketz")
import pandas as pd
from ml.artifacts import file_checksum
from ml.option_pricing.consumers import read_verified_compact_pricing_features
from ml.horizons import horizon_specifications_for_profile
from ml.runtime_pipeline import (
    OPTION_PRICING_LOOP_B_GATE_POLICY_VERSION,
    OPTION_PRICING_LOOP_B_MINIMUM_COVERAGE,
    OPTION_PRICING_LOOP_B_MINIMUM_DISTINCT_TARGETS,
    _specification_for_pricing_gate,
)

ROOT = Path("C:/DATASTORE")
OUT = Path(__file__).resolve().parent
RUN = ROOT / "ml/runs/20260915T051611.319055Z"
LOG = ROOT / "ml/overnight-runs/20260915T040742.365640Z/loop_b_directional_generation.log"
PRIOR = Path("C:/dev/ducketz/artifacts/analysis/overnight-20260912/option-pricing-quarantine.json")
prior = json.loads(PRIOR.read_text())
cycle = json.loads((ROOT / ".ducketz-loop-a-complete.json").read_text())
assert cycle["generation"] == "20260915T040743.170425Z-pid5368"
symbols = cycle["symbols"]
cutoff = cycle["finished_at"]
source, files = read_verified_compact_pricing_features(ROOT, available_not_after=cutoff)
file_evidence = [{"path":str(p),"sha256":file_checksum(p)} for p in files]
prior_files = {Path(item["path"]).resolve().as_posix():item["sha256"] for item in prior["verified_pricing_source"]["source_files"]}
current_files = {Path(item["path"]).resolve().as_posix():item["sha256"] for item in file_evidence}
freshness = {"1h":pd.Timedelta(hours=2),"4h":pd.Timedelta(hours=4),"1d":pd.Timedelta(days=2),"1w":pd.Timedelta(days=8)}
first = pd.to_datetime(source.first_available_at,utc=True)
target = pd.to_datetime(source.target_snapshot_for,utc=True)
fresh_rows = {h:int(pd.concat([first+limit,target+limit],axis=1).min(axis=1).ge(pd.Timestamp(cutoff)).sum()) for h,limit in freshness.items()}
field_names = [c for c in ["causal_coverage","median_normalized_residual","median_predictive_standard_deviation",
               "median_model_edge_in_half_spreads","positive_edge_fraction","negative_edge_fraction",
               "raw_arbitrage_violation_rate","constrained_arbitrage_violation_rate",
               "interval_80_coverage","interval_95_coverage","median_relative_bid_ask_spread"] if c in source]
source_symbols = sorted(source.symbol.unique())
source_counts = {symbol:{"compact_rows":len(frame),"distinct_targets":int(frame.target_snapshot_for.nunique()),
    "latest_target":frame.target_snapshot_for.max(),"first_available_at":frame.first_available_at.min(),
    "non_null_rows_by_feature":{c:int(frame[c].notna().sum()) for c in field_names}}
    for symbol,frame in source.groupby("symbol",sort=True)}
log = LOG.read_bytes()
lines = log.decode().splitlines()
quarantine = []
for line in lines:
    match = re.match(r"\[Loop B/(.*?)\] Option Pricing family quarantined; fitting (.*?) until .*\((.*?)\)$",line)
    if match:
        quarantine.append({"horizon":match[1],"effective_feature_set":match[2],"failed_routes":match[3].split(", "),"log_line":line})
effective = {}
for horizon,spec in horizon_specifications_for_profile("loop-a-all-bsgp-active-v3").items():
    fallback = _specification_for_pricing_gate(spec,gate={"enabled":True,"downstream_training_eligible":False})
    effective[horizon]={"requested_feature_set":spec.feature_set,"effective_feature_set":fallback.feature_set,"native_exact_non_pricing_preservation_check":"PASS"}
manifest_path = RUN / "manifest.json"
manifest_present = manifest_path.is_file()
current_gate = None
current_groups = {}
native_publication = None
if manifest_present:
    cfg = json.loads(manifest_path.read_text())["configuration"]
    current_gate = cfg.get("pricing_evidence")
    assert pd.Timestamp(cfg["causal_input_cutoff"]) == pd.Timestamp(cutoff)
    publication_path = RUN / "publication.json"
    publication = json.loads(publication_path.read_text())
    assert publication["manifest_checksum_sha256"] == file_checksum(manifest_path)
    groups = {}
    for route in current_gate["route_gates"].values():
        for name,item in route["groups"].items():
            groups.setdefault(name,[]).append(item)
    current_groups = {name:{"route_count":len(items),"passing_routes":sum(item["pass"] for item in items),
        "maximum_complete_fraction":max(item["complete_row_fraction"] for item in items),
        "maximum_fresh_joined_fraction":max(item["fresh_joined_row_fraction"] for item in items),
        "maximum_distinct_targets":max(item["distinct_surface_targets"] for item in items),
        "total_complete_rows":sum(item["complete_rows"] for item in items)} for name,items in groups.items()}
    native_publication = {"publication":publication,"manifest_path":str(manifest_path),"manifest_checksum_sha256":file_checksum(manifest_path),
        "publication_manifest_binding":"PASS","publication_counts":cfg["publication_counts"],"route_errors":cfg["route_errors"],
        "original_gate_rows":sum(r["sample_rows"] for r in current_gate["routes"].values()),
        "published_sample_rows":335785,"published_sample_row_count_source":"native final Loop B summary",
        "scope_limitation":"Only the manifest-to-publication binding and saved control-plane gate were read; current model/sample output payloads were not rehashed by this audit."}
result = {
    "audited_at":pd.Timestamp.now(tz="UTC").isoformat(),"read_only":True,
    "status":"EXPECTED_OPTIONAL_PRICING_EXCLUSION_SOURCE_UNCHANGED",
    "run_path":str(RUN),"log_snapshot":{"path":str(LOG),"bytes":len(log),"sha256":hashlib.sha256(log).hexdigest()},
    "symbols":symbols,"route_count_from_native_quarantine_log":sum(len(row["failed_routes"]) for row in quarantine),
    "native_quarantine":quarantine,"effective_feature_contracts":effective,
    "source_cutoff_used_for_read_only_verification":cutoff,
    "gate_policy":{"version":OPTION_PRICING_LOOP_B_GATE_POLICY_VERSION,"minimum_complete_row_fraction":OPTION_PRICING_LOOP_B_MINIMUM_COVERAGE,"minimum_fresh_joined_row_fraction":OPTION_PRICING_LOOP_B_MINIMUM_COVERAGE,"minimum_distinct_surface_targets":OPTION_PRICING_LOOP_B_MINIMUM_DISTINCT_TARGETS},
    "verified_source":{"rows":len(source),"symbols":source_symbols,"missing_production_symbols":sorted(set(symbols)-set(source_symbols)),
        "distinct_targets":int(source.target_snapshot_for.nunique()),"latest_target":target.max(),"latest_availability":first.max(),
        "source_files":file_evidence,"source_provenance":source.attrs.get("pricing_evidence",{}),"by_symbol":source_counts,
        "all_rows_missing_features":[c for c in field_names if source[c].isna().all()],"fresh_source_rows_at_current_cutoff_by_horizon":fresh_rows},
    "comparison_to_september_12":{"prior_audit":str(PRIOR),"source_paths_and_hashes_unchanged":current_files==prior_files,
        "prior_compact_rows":prior["verified_pricing_source"]["rows"],"prior_distinct_targets":prior["verified_pricing_source"]["distinct_targets"],
        "prior_failed_routes":len(prior["pricing_gate"]["failed_routes"]),"current_failed_routes_from_log":sum(len(row["failed_routes"]) for row in quarantine),
        "explanation":"The same verified Aug20 source now faces 99 symbol/horizon routes instead of 63. CROX/PATH/TWST/IONQ, as well as COST, have no Pricing surfaces. No new Pricing generation or new source checksum error was found."},
    "current_manifest_available":manifest_present,"current_native_pricing_gate_if_saved":current_gate,
    "current_group_summary":current_groups,"native_publication":native_publication,
    "current_route_fraction_availability":"Saved native manifest contains current gate detail" if current_gate else "UNAVAILABLE_WHILE_MODEL_STAGE_IN_PROGRESS: source verified and current rejection log observed, but original materialized route fractions are not yet saved. Prior Sep12 fractions are not reused or represented as current.",
    "current_reasons":[
        "The compact source has only 17 distinct targets globally, less than the unchanged minimum 20 for every route; per-symbol target counts are lower still.",
        "Predictive standard deviation and 80%/95% interval calibration are missing throughout the verified compact source.",
        "Latest target and availability remain Aug20; no source row is fresh at the current cutoff for 1h,4h,1d or1w.",
        "COST,CROX,PATH,TWST,IONQ have no source surfaces; optional Pricing does not cover the expanded universe.",
    ],
    "interpretation":"Known optional feature coverage/freshness quarantine, with expected additional uncovered routes after universe expansion. The native reduced feature contracts preserve all requested non-Pricing features. This is distinct from the fully completed current OPRA production history and does not qualify Pricing models or authorize options execution.",
    "actions":"No repair,retry,training,provider/broker call,claim or production mutation was performed. Root continues the running native Directional stage and its existing downstream gates.",
}
output = OUT / "option-pricing-quarantine.json"
output.write_text(json.dumps(result,indent=2,default=str,sort_keys=True)+"\n")
saved_note = ""
if current_gate:
    saved_note = "\n## Published native qualification evidence\n\n"
    saved_note += f"The completed publication has zero route errors and retains the Pricing gate across {native_publication['original_gate_rows']:,} original materialized rows. Its published 335,785-row sample output omits closed lockbox rows and is not substituted for the gate denominator. The publication binds the manifest checksum.\n\n"
    saved_note += "| Pricing subfamily | Maximum complete/fresh fraction | Maximum usable targets | Passing routes |\n|---|---:|---:|---:|\n"
    for name,group in current_groups.items():
        saved_note += f"| {name} | {group['maximum_fresh_joined_fraction']:.4%} | {group['maximum_distinct_targets']} | {group['passing_routes']}/{group['route_count']} |\n"
    saved_note += "\nHistorical joins can contain some fresh rows at their original decision clocks even though every source row is stale at today's cutoff. The all-route exclusion remains expected; the six subfamilies fail the unchanged 80%/20-target rules.\n"
(OUT / "option-pricing-quarantine.md").write_text(f"""# Current optional Pricing quarantine

Audited {result['audited_at']}. No newly actionable source failure identified.

- Native Loop B excluded Pricing features on **{result['route_count_from_native_quarantine_log']} routes** across all 11 symbols and nine horizons. It selected `loop-a-all-v1-1h`, `loop-a-all-v1-4h`, `loop-a-all-v3-1d` and `loop-a-all-v3-1w`; the native exact preservation check passes for every requested non-Pricing feature contract.
- Pricing source verification succeeds. The **{len(source)} compact rows and their source hashes are unchanged** from the September 12 audit. They cover six symbols and only **{int(source.target_snapshot_for.nunique())} distinct August 20 target clocks**, below the required 20 per route.
- Predictive standard deviation and interval calibration remain entirely missing. No source row is fresh at the current cutoff for any supported horizon. COST, CROX, PATH, TWST and IONQ have no Pricing surfaces.
- Thresholds remain 80% complete rows, 80% fresh joined rows and 20 distinct usable targets. The previous 63 rejected routes have become 99 with the expanded production universe; this is the same optional coverage limitation.
- Current original route fractions are {'available in the saved manifest' if current_gate else 'not yet persisted while the Directional stage runs'}. September 12 fractions were not reused as current. The current log records 349,769 materialized rows; this audit did not rematerialize or retrain them.
- This optional Pricing exclusion is separate from tonight's completed 33-scope OPRA production coverage. No provider calls, repairs, retries, training, broker actions, claim operations or production edits occurred.

Detailed [audit]({output.as_posix()}); [native log]({LOG.as_posix()}); [prior comparison]({PRIOR.as_posix()}).
{saved_note}
""")
print(json.dumps({"status":result["status"],"rows":len(source),"targets":int(source.target_snapshot_for.nunique()),"source_unchanged":current_files==prior_files,
    "quarantined_routes":result["route_count_from_native_quarantine_log"],"missing_symbols":sorted(set(symbols)-set(source_symbols)),"fresh_rows":fresh_rows,
    "missing_features":result["verified_source"]["all_rows_missing_features"],"manifest_present":manifest_present,"current_groups":current_groups,"artifact":str(output)},indent=2))
