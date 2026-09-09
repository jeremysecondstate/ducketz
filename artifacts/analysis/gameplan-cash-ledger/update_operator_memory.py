from datetime import datetime, timezone
from pathlib import Path
import json

root = Path("C:/dev/ducketz")
evidence = root / "artifacts/analysis/gameplan-cash-ledger"
claim = json.loads(Path("C:/DATASTORE/ml/overnight-supervision.json").read_text())
assert claim.get("owner_token") == "6da6298b-36af-4716-b142-d5b31c3bce75"
assert datetime.fromisoformat(claim["expires_at"]) <= datetime.fromisoformat(claim["updated_at"])
claim["active"] = False
now = datetime.now(timezone.utc).isoformat()
entry = f"""## Direction quantities and hourly cash/share projection completed — {now}

The user's latest clarification explicitly confirms bearish=sell available held shares, bullish=buy with available cash, neutral=hold. They requested an adjacent Direction Based Trade Qty column, shared chronological cash accounting across all symbols, symbol-specific holdings by hour, tight price ranges, and permanent automation in the existing nighttime loops. This implementation completes those requests as a conditional planning scenario. It does not authorize broker writes or changing live ownership. Earlier capacity-only counts below are historical and superseded by this entry.

Native overnight run C:/DATASTORE/ml/overnight-runs/20260909T071230.488635Z completed the sole gameplan_trade_planning stage at07:13:11UTC. Source Gameplan20260909T060404.450224Z is unchanged (receipt bb4f21dc093d1195964e323d0321aa3a10c87e3d1e00cf63bc688df32c3e134b); action September9 and original11:00UTC deadline preserved. New COMPLETE trade plan C:/DATASTORE/ml/gameplan-trade-plan-runs/20260909T071232.777557Z, receipt c90e1577c4272216029d50cb172ad06d36eafb907c3c19177c8f87e6ba73cd0c. No upstream rerun or model changes. Do not rerun already-completed September9 work.

Fresh broker snapshot07:12:40UTC: literal available cash111171.11, account equity129696.42, one share of each of seven stocks includingCOST, no working orders. Snapshot now separates stock market value from attributable non-stock exposure, preserving GOOG options when stock is sold. New ml/gameplan_cash_ledger.py simulates one shared cash low/base/high range, independent horizon lots and symbol holdings. At every hour: bearish sales, remaining due exits, bullish buys. Competing unallocated shares go to shorter horizon then symbol; buys rank highest published probability then shorter horizon/symbol. Pending reservations and other horizons' inventory are respected. Buy sizes use full horizon capacity, whole shares, conservative remaining cash and exposure plus existing initial5%cash buffer; new planning quantities do not apply the old live preview's confidence multiplier or six-order cap. Row balances are after the entire hour; events show exact transaction before/change/after.

Added planning-price-path.json with all98symbol/04:00–17:00 points available, minimum27observed pairs. Center=observed historical median prior-close-to-clock ratio times observed prior close; configurable default+/-20bps conditional fill allowance, outward cents. This is not a prediction confidence interval. Broader historical ranges remain saved. All133entry rows now have positive independent projected capacity; all133retain verified PROMOTED direction models under the earlier user-approved policy. The actual direction plan has7SELL rows,1BUY row,125HOLD rows and1separate expiry sale. Opening sales consume all7initial stocks once; SNDK buys1at07:00Pacific and exits08:00. Conditional ending cash115963.37–115996.69, base115980.02, before fees/taxes; this cash increase mainly converts held stock, not profit. Seven configured stocks end0; other account assets remain. Neutral rows always have0direction quantity; earlier lots can separately expire. Unfilled trades leave balances unchanged and require later quantities to be recalculated; no-fill baseline is saved. Overnight/weekly lots with future expiry remain held.

Current derived document C:/dev/ducketz/artifacts/gameplans/2026-09-09/Gameplan-2026-09-09-trade-review.md has adjacent quantity columns, working price, post-hour cash, shares remaining, hourly/EODtables and expandable transactions. Native scheduled-entry preview is separate in details. Derived SHA2a781fbd8b3c0e060b5382ce2c5b1c2983ed51432b37043312f2c5a9e36d60b2; it uses immutable v3 data with final clarity wording, preserving native Markdown and capacity-v2 backup. Exact source rows/columns,168matching option placeholders,98pricepoints, all event/hour/ending cash and inventory, manifests and pointers verified.114final focused tests passed; earlier198integration tests also passed. Evidence under artifacts/analysis/gameplan-cash-ledger: publication-verification-v3.json, automation-verification-v3.json and operator-notes-v3.md.

After successful native publication updated existing three Scheduled prompts through automation_update, then exact readback: only prompt/updated_at changed. Loops Overnight Gameplan remains21:05dailyPacific, Operations Watch hourly:00, Daytime Supervision weekdays03:55. All model/reasoning/notifications/project/cwds/status/cadence preserved. Seven docs/loops-system-analysis Markdown files describe the new automatic tail. Windows Ducketz Independent Stock Session remainsReady, LastTaskResult0, nextSep9 03:55Pacific. The existing live worker still follows its own confidence-weighted long-only sizing and horizon-owned exits; it will not automatically execute the new document's modeled bearish manual-share sales. No worker start/restart, real horizon reassignment, trading-control change or broker order occurred. Orders placed0.

Root claim6da6298b-36af-4716-b142-d5b31c3bce75 acquired06:55:43UTC, maintained by root and same-task bounded renewal agent. Agent stopped with no in-flight/future call; root renewed07:17:50UTC then released natively. Saved claim confirms inactive at{claim.get('updated_at')}. No detached helper remains. App file-open returnedqueued; final provides direct review link.

"""
for name in ("loops-hourly-operations", "loops-stock-trader-daily-adaptation", "loops-stock-trader-live-shadow"):
    path = Path("C:/Users/7980X/.codex/automations") / name / "memory.md"
    old = path.read_text(encoding="utf-8")
    if old.startswith("# ") and "\n" in old:
        title, rest = old.split("\n", 1)
        revised = title + "\n\n" + entry + rest.lstrip("\n")
    else:
        revised = entry + old
    path.write_text(revised, encoding="utf-8")
(evidence / "supervision-release-v3.json").write_text(json.dumps(claim, indent=2) + "\n", encoding="utf-8")
with (evidence / "operator-notes-v3.md").open("a", encoding="utf-8") as handle:
    handle.write(f"\nRoot supervision UUID6da6298b-36af-4716-b142-d5b31c3bce75 released natively at{claim.get('updated_at')}; bounded renewal agent stopped with no future/in-flight calls. All three automation memories updated at{now}.\n")
print(json.dumps({"memory_files_updated": 3, "supervision_active": claim["active"], "updated_at": now}))
