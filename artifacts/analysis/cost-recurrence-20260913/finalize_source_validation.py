"""Make a readable coverage-only comparison and conservative quality sensitivity."""
import json
import hashlib
from pathlib import Path
import pandas as pd

OUT=Path(__file__).parent/"source-probe/validation-120"
run=json.loads((OUT/"run.json").read_text())
assert run["status"] == "COMPLETE"
condition=json.loads((OUT/"provider-conditions.json").read_text())
assert condition["status"] == "COMPLETE"
boundaries=pd.read_parquet(OUT/"boundary-results.parquet")
windows=pd.read_parquet(OUT/"entry-window-results.parquet")
positive_boundaries=pd.read_parquet(OUT/"positive-volume-boundaries.parquet")
positive_windows=pd.read_parquet(OUT/"positive-volume-entry-windows.parquet")
candidate_windows=windows[windows.dataset.eq("XNAS.BASIC")]
positive_windows=positive_windows.merge(candidate_windows[["symbol","route","action_date","target_start","target_end"]],on=["symbol","route","action_date"],validate="one_to_one")
bad_dates=[r["date"] for r in condition["datasets"]["XNAS.BASIC"] if r["condition"] not in ("available","holiday","weekend")]
positive_windows["provider_quality_eligible"]=True
positive_boundaries["provider_quality_eligible"]=True
for date in bad_dates:
    # Conservatively exclude the entire action date and every target touching it.
    left=pd.Timestamp(date,tz="America/Los_Angeles")+pd.Timedelta(hours=4)
    right=pd.Timestamp(date,tz="America/Los_Angeles")+pd.Timedelta(hours=17)
    positive_windows.loc[positive_windows.target_start.le(right)&positive_windows.target_end.ge(left),"provider_quality_eligible"]=False
    positive_boundaries.loc[positive_boundaries.session.eq(date),"provider_quality_eligible"]=False
positive_windows.to_parquet(OUT/"conservative-entry-windows.parquet",index=False)
positive_boundaries.to_parquet(OUT/"conservative-boundaries.parquet",index=False)
comparison=[]
for symbol in run["symbols"]:
    row={"symbol":symbol}
    for dataset,label in (("XNAS.ITCH","itch"),("XNAS.BASIC","basic")):
        b=boundaries[boundaries.symbol.eq(symbol)&boundaries.dataset.eq(dataset)]
        w=windows[windows.symbol.eq(symbol)&windows.dataset.eq(dataset)&windows.mature]
        row[label]={"boundary_total":len(b),"boundary_missing":int((~b.available).sum()),"entry_mature":len(w),"entry_available":int(w.available.sum())}
    b=positive_boundaries[positive_boundaries.symbol.eq(symbol)]
    w=positive_windows[positive_windows.symbol.eq(symbol)&positive_windows.mature]
    row["basic_positive"]={"boundary_total":len(b),"boundary_missing":int((~b.available).sum()),"entry_mature":len(w),"entry_available":int(w.available.sum())}
    b=b[b.provider_quality_eligible]
    w=w[w.provider_quality_eligible]
    row["basic_conservative"]={"boundary_total":len(b),"boundary_missing":int((~b.available).sum()),"entry_mature":len(w),"entry_available":int(w.available.sum())}
    baseline_b=boundaries[boundaries.symbol.eq(symbol)&boundaries.dataset.eq("XNAS.ITCH")&~boundaries.session.isin(bad_dates)]
    baseline_w=windows[windows.symbol.eq(symbol)&windows.dataset.eq("XNAS.ITCH")&windows.mature].copy()
    for date in bad_dates:
        left=pd.Timestamp(date,tz="America/Los_Angeles")+pd.Timedelta(hours=4)
        right=pd.Timestamp(date,tz="America/Los_Angeles")+pd.Timedelta(hours=17)
        baseline_w=baseline_w[~(baseline_w.target_start.le(right)&baseline_w.target_end.ge(left))]
    row["itch_matched_exclusions"]={"boundary_total":len(baseline_b),"boundary_missing":int((~baseline_b.available).sum()),"entry_mature":len(baseline_w),"entry_available":int(baseline_w.available.sum())}
    comparison.append(row)
quality={"generated_at":pd.Timestamp.now(tz="UTC").isoformat(),"bad_dates":bad_dates,"policy":"Conservative sensitivity only: original native data preserved; use positive-volume observations and exclude degraded action sessions plus any entry window touching them", "summary":comparison}
(OUT/"conservative-quality-summary.json").write_text(json.dumps(quality,indent=2)+"\n")
lines=["# XNAS.BASIC prospective coverage validation", "", f"Verified {len(run['sessions'])} XNYS sessions, {run['sessions'][0]} through {run['sessions'][-1]}, plus prior-session close ({run['prior_session']}). All seven exact requests passed $0 cost preflight before acquisition; total estimated billable size {run['total_preflight']['estimated_billable_bytes']:,} bytes. Each native DBN was decoded again and reproduced its saved parquet exactly. All per-symbol metadata, request scopes, row counts and checksums passed.", "", "This is coverage evidence only. It does not switch a production source, revise frozen forecasts, train models, change controls or submit orders.", "", "The comparison uses native five-minute selectors: opens at/after each opening clock and completed closes at/before each closing clock. There are 26 unique open/close boundary checks per session and 19 entry windows. Future weekly and overnight endpoints after September 11 are excluded from matured sample counts.", "", "| Symbol | XNAS.ITCH missing boundaries | XNAS.BASIC missing boundaries | BASIC positive-volume missing | ITCH available entry windows | BASIC positive-volume available |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
for r in comparison:
    lines.append(f"| {r['symbol']} | {r['itch']['boundary_missing']}/{r['itch']['boundary_total']} | {r['basic']['boundary_missing']}/{r['basic']['boundary_total']} | {r['basic_positive']['boundary_missing']}/{r['basic_positive']['boundary_total']} | {r['itch']['entry_available']}/{r['itch']['entry_mature']} | {r['basic_positive']['entry_available']}/{r['basic_positive']['entry_mature']} |")
lines += ["", "Provider quality metadata marks August 31 XNAS.BASIC degraded; XNAS.ITCH has no degraded dates in this comparison. The degraded date is retained transparently in raw files. A separate conservative sensitivity excludes that action date and any entry target touching it, and excludes all native zero-volume bars. The same date/window exclusions are applied to the ITCH baseline for a matched comparison. This affects eligibility; complete raw delivery does not establish good provider quality on that date.", "", "| Symbol | ITCH missing boundaries, matched dates | Conservative BASIC missing boundaries | ITCH available entry windows, matched dates | Conservative BASIC available entry windows | Native BASIC zero-volume rows (whole requested range) |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
for r in comparison:
    q=r["basic_conservative"]
    t=r["itch_matched_exclusions"]
    z=next(x["zero_volume_rows"] for x in run["acquisitions"] if x["symbol"]==r["symbol"])
    lines.append(f"| {r['symbol']} | {t['boundary_missing']}/{t['boundary_total']} | {q['boundary_missing']}/{q['boundary_total']} | {t['entry_available']}/{t['entry_mature']} | {q['entry_available']}/{q['entry_mature']} | {z:,} |")
lines += ["", "Zero-volume native records are not relabeled as fabricated or no-trade observations. Their provider semantics remain unresolved. The positive-volume comparison proves which gains survive without them. Existing XNAS.ITCH rows have zero such records across all seven symbols; see the independent baseline audit.", "", "Per-symbol regular/premarket/afterhours missing rates and all 19 individual route counts are retained in comparison.json and positive-volume-sensitivity.json; row-level evidence is in boundary-results.parquet and entry-window-results.parquet. Native data, preflight, metadata and manifests reside in each symbol directory. provider-conditions.json retains provider quality evidence.", "", "Before any prospective adoption: approve an explicit XNAS.BASIC source identity; enforce data-quality admission rules; determine native zero-volume semantics; validate the same-source historical labels/features, chronological cohort minimums and model assessments. Frozen XNAS.ITCH reports keep their source, and no cross-dataset gap substitution is authorized by this audit."]
lines += ["", "## Missing boundary rates by market period", "", "Each cell shows XNAS.ITCH missing/total (percent) → positive-volume XNAS.BASIC missing/total (percent), before excluding the degraded date.", "", "| Symbol | Premarket | Regular session | Afterhours |", "| --- | --- | --- | --- |"]
for symbol in run["symbols"]:
    cells=[]
    for period in ("premarket","regular","afterhours"):
        b=boundaries[boundaries.dataset.eq("XNAS.ITCH")&boundaries.symbol.eq(symbol)&boundaries.period.eq(period)]
        p=positive_boundaries[positive_boundaries.symbol.eq(symbol)&positive_boundaries.period.eq(period)]
        cells.append(f"{int((~b.available).sum())}/{len(b)} ({100*(~b.available).mean():.2f}%) → {int((~p.available).sum())}/{len(p)} ({100*(~p.available).mean():.2f}%)")
    lines.append("| "+symbol+" | "+" | ".join(cells)+" |")
lines += ["", "## All 19 entry-window routes", "", "Each cell shows XNAS.ITCH available/mature → positive-volume XNAS.BASIC available/mature, before excluding the degraded date. This measures price endpoint availability, not fitted-model qualification or feature-cohort admission.", "", "| Route | "+" | ".join(run["symbols"])+" |", "| --- | "+" | ".join(["---"]*len(run["symbols"]))+" |"]
for route in candidate_windows.route.drop_duplicates():
    cells=[]
    for symbol in run["symbols"]:
        b=windows[windows.dataset.eq("XNAS.ITCH")&windows.symbol.eq(symbol)&windows.route.eq(route)&windows.mature]
        p=positive_windows[positive_windows.symbol.eq(symbol)&positive_windows.route.eq(route)&positive_windows.mature]
        cells.append(f"{int(b.available.sum())}/{len(b)} → {int(p.available.sum())}/{len(p)}")
    lines.append("| "+route+" | "+" | ".join(cells)+" |")
(OUT/"coverage-review.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
manifest={"generated_at":pd.Timestamp.now(tz="UTC").isoformat(),"status":"VERIFIED_COVERAGE_ONLY_WITH_PROVIDER_QUALITY_LIMITATION", "files":[{"path":str(p.relative_to(OUT)),"bytes":p.stat().st_size,"sha256":hashlib.sha256(p.read_bytes()).hexdigest()}for p in sorted(OUT.rglob('*')) if p.is_file() and p.name!='validation-manifest.json']}
(OUT/"validation-manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
print(json.dumps(quality,indent=2))
