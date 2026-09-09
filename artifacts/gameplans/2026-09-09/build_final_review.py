"""Verify the pinned September 9 publication and build its local review copy.

Reads immutable pipeline artifacts; writes only this review artifact directory.
Run from the repository with its .venv Python. No provider or broker calls.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

REPO = Path(r"C:\dev\ducketz")
ROOT = Path(r"C:\DATASTORE")
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from ml.artifacts import file_checksum, verify_manifest
from ml.nightly_gameplan import read_current_gameplan, _verify_opra_history
from ml.stock_trader.independent_training import verify_independent_model_sources

GROUPS = ["1h", "4h", "1d", "1w"]
SYMBOLS = ["AAPL", "AMZN", "GOOG", "MU", "NVDA", "SNDK", "COST"]
RUN = ROOT / "ml/nightly-gameplan-runs/20260909T040455.436642Z"
OVERNIGHT = ROOT / "ml/overnight-runs/20260909T040352.061858Z"
HISTORY = ROOT / "ml/stock-target-history-runs/20260909T040353.083862Z"
ENRICHMENT = ROOT / "ml/stock-trader-model-runs/20260909T040540.539128Z"


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def require(condition: bool, detail: str):
    if not condition:
        raise RuntimeError(detail)


def verify_file(path: Path, expected_hash: str, expected_size=None):
    require(path.is_file(), f"Missing evidence: {path}")
    if expected_size is not None:
        require(path.stat().st_size == expected_size, f"Size differs: {path}")
    require(file_checksum(path) == expected_hash, f"Checksum differs: {path}")


def pac(value, short=False):
    t = pd.Timestamp(value).tz_convert("America/Los_Angeles")
    return t.strftime("%m/%d %H:%M" if short else "%Y-%m-%d %H:%M:%S %Z")


def link(label, path):
    return f"[{label}]({Path(path).as_posix()})"


publication = read_current_gameplan(ROOT)
require(publication.run_directory == RUN, "Current pointer no longer selects the pinned September 9 run")
require(publication.receipt["action_date"] == "2026-09-09", "Wrong action date")
plan = read(RUN / "gameplan.json")
reports = read(RUN / "model-reports.json")
forecasts = pd.read_parquet(RUN / "forecasts.parquet")
intents = pd.read_parquet(RUN / "option-strategy-intents.parquet")
require(set(plan["symbols"]) == set(SYMBOLS), "Universe differs")
require(set(reports) == set(GROUPS), "Missing final model group")
require(len(forecasts) == len(intents) == 168, "Forecast or placeholder count differs")
require(forecasts.groupby("symbol").size().to_dict() == {s: 24 for s in SYMBOLS}, "Per-symbol forecast count differs")
require(intents.groupby("symbol").size().to_dict() == {s: 24 for s in SYMBOLS}, "Per-symbol placeholder count differs")
require(not forecasts["id"].duplicated().any(), "Duplicate forecast IDs")
require(set(intents["plan_status"]) == {"NO_TRADE_STOCK_ONLY"}, "Unexpected option intent")
require(set(forecasts["action_date"]) == {"2026-09-09"}, "Forecast date differs")
require(set(forecasts["target_price_source_contract"]) == {"xnas-itch-archive-v1"}, "Mixed price source")
require(set(forecasts["target_price_dataset"]) == {"XNAS.ITCH"}, "Mixed dataset")
require(set(forecasts["target_contract_version"]) == {"independent-stock-targets-v1"}, "Target contract differs")
require(not forecasts["broker_orders_enabled"].any() and not intents["broker_orders_enabled"].any(), "Unexpected order authority")
require(forecasts[["raw_probability", "calibrated_probability"]].notna().all().all(), "Missing probabilities")
require(forecasts[["raw_probability", "calibrated_probability"]].ge(0).all().all() and forecasts[["raw_probability", "calibrated_probability"]].le(1).all().all(), "Invalid probability")
entry = forecasts[forecasts["execution_eligible"]]
qualified_entry = entry[entry["model_status"].eq("PROMOTED")]
qualified_bullish = qualified_entry[qualified_entry["direction"].eq("BULLISH")]
require(len(entry) == 133 and len(qualified_entry) == 126, "Entry/model eligibility count differs")
require(len(qualified_bullish) == 0, "Bullish summary needs review")

# Verify every receipt in the resume chain, including the preserved upstream logs.
chain = []
current = OVERNIGHT
while current:
    receipt = read(current / "receipt.json")
    verify_file(current / "stage-report.json", receipt["stage_report_checksum_sha256"], receipt["stage_report_size"])
    report = read(current / "stage-report.json")
    for name, meta in receipt["logs"].items():
        verify_file(current / name, meta["checksum_sha256"], meta["size"])
    require(receipt["orders_placed"] == 0 and receipt["broker_orders_enabled"] is False, "Overnight order evidence differs")
    require(report["deadline_at"] == "2026-09-09T11:00:00+00:00", "Resume deadline differs")
    chain.append({"run": str(current), "status": receipt["status"], "report_and_logs_verified": True, "stages": report["stages"]})
    if current == OVERNIGHT:
        overnight_report = report
        require(receipt["status"] == "COMPLETE", "Overnight run not complete")
        require(all(s["status"] == "COMPLETE" and s["exit_code"] == 0 for s in report["stages"]), "Remaining stage failed")
        require(set(report["completed_stages_from_previous_attempt"]) == {"loop_a_close_fetch", "loop_b_directional_generation"}, "Upstream stages were not retained")
    previous = report.get("resumed_from")
    current = Path(previous) if previous else None
require(len(chain) == 3, "Unexpected resume chain")
first_stages = {s["stage"]: s for s in chain[-1]["stages"]}
for name in ["loop_a_close_fetch", "loop_b_directional_generation"]:
    require(first_stages[name]["status"] == "COMPLETE" and first_stages[name]["exit_code"] == 0, f"Upstream stage incomplete: {name}")
require(pd.Timestamp(overnight_report["completed_at"]) < pd.Timestamp(overnight_report["deadline_at"]), "Completed after deadline")

# The original Loop B snapshot remains evidence for the retained, immutable source.
initial = read(OUT / "verification.json")
loop_b = ROOT / publication.manifest["configuration"]["source_loop_b_run"]
require(loop_b == Path(initial["loop_b_run"]), "Final plan has a different Loop B source")
verify_file(loop_b / "manifest.json", initial["loop_b_manifest_sha256"])
verify_manifest(loop_b)

# Check all archive files bound into this exact final publication.
source_files = publication.manifest["configuration"]["stock_price_source"]["files"]
for item in source_files:
    verify_file(Path(item["path"]), item["sha256"], item["bytes"])
history_receipt = read(HISTORY / "receipt.json")
for field, name in [("manifest_sha256", "manifest.json"), ("preflight_sha256", "preflight.json"), ("cost_preflight_sha256", "cost-preflight.json")]:
    verify_file(HISTORY / name, history_receipt[field])
history_manifest = read(HISTORY / "manifest.json")
cost = read(HISTORY / "cost-preflight.json")
require(history_receipt["status"] == "COMPLETE" and history_receipt["counts"]["downloaded"] == 7, "History download incomplete")
require(history_receipt["counts"]["failed"] == 0 and history_receipt["completed_through"] == "2026-09-09", "History coverage incomplete")
require(history_receipt["estimated_cost_usd"] == cost["total_cost_usd"] == 0, "Cost preflight differs")
require(read(HISTORY / "preflight.json")["capacity_pass"] is True, "Capacity preflight failed")
history_rows = []
for request in history_manifest["requests"]:
    part = Path(request["storage_path"])
    manifest = read(part / "manifest.json")
    receipt = read(part / "receipt.json")
    verify_file(part / "manifest.json", receipt["manifest_checksum_sha256"])
    require(manifest["provider_delivery"]["mode"] == "timeseries-stream", "Unexpected Live or other delivery")
    require(request["end"] == "2026-09-09", "Wrong source request end")
    for section in ["raw", "normalized"]:
        metadata = manifest[section]
        verify_file(part / metadata["path"], metadata["checksum_sha256"], metadata["size_bytes"])
        require(receipt[f"{section}_checksum_sha256"] == metadata["checksum_sha256"], "Partition receipt differs")
    bars = pd.read_parquet(part / manifest["normalized"]["path"])
    timestamp_name = manifest["normalized"]["timestamp_column"]
    if timestamp_name in bars.columns:
        timestamps = pd.to_datetime(bars[timestamp_name], utc=True)
    else:
        require(bars.index.name == timestamp_name, "Archive timestamp field missing")
        timestamps = pd.Series(pd.to_datetime(bars.index, utc=True))
    session_bars = timestamps[timestamps.dt.tz_convert("America/Los_Angeles").dt.date == date(2026, 9, 8)]
    require(len(session_bars) > 0, "Missing September 8 bars")
    require(session_bars.max() >= pd.Timestamp("2026-09-08T23:55:00Z"), "Missing close observations")
    history_rows.append({"symbol": request["symbol_scope"][0], "rows_in_overlap_download": len(bars), "september_8_rows": len(session_bars), "last_bar": session_bars.max().isoformat(), "delivery": "Historical.timeseries.get_range", "partition": str(part)})
require({r["symbol"] for r in history_rows} == set(SYMBOLS), "History universe differs")
_, opra = _verify_opra_history(ROOT, symbols=SYMBOLS, action_date=date(2026, 9, 9), required_completed_through=date(2026, 9, 9))

cohort_counts = {}
for group in GROUPS:
    report = reports[group]
    require(report["both_hist_gradient_and_mlp_trained"] and report["regularized_logistic_trained"], "Missing trained candidates")
    model_file = report["model_file"]
    verify_file(RUN / model_file["path"], model_file["checksum_sha256"], model_file["size"])
    require(set(forecasts.loc[forecasts.model_group.eq(group), "model_status"]) == {report["promotion_gate"]["status"]}, "Forecast/model status differs")
    cohort = pd.read_parquet(RUN / f"training-cohort-{group}.parquet")
    cohort_counts[group] = len(cohort)
    require(len(cohort) == report["target_boundary_quality"]["aligned_rows"], "Cohort row count differs")

enrichment_manifest = verify_manifest(ENRICHMENT)
enrichment_model = read(ENRICHMENT / "model.json")
enrichment_receipt = read(ENRICHMENT / "receipt.json")
enrichment = read(ENRICHMENT / "training-report.json")
for key, name in [("manifest_sha256", "manifest.json"), ("model_sha256", "model.json"), ("training_report_sha256", "training-report.json")]:
    verify_file(ENRICHMENT / name, enrichment_receipt[key])
require(enrichment["source_gameplan_run"] == RUN.relative_to(ROOT).as_posix(), "Enrichment source differs")
verify_independent_model_sources(ROOT, enrichment_model, enrichment_manifest)
require(all(enrichment_model["horizons"][g]["fitted"] for g in GROUPS), "An enrichment horizon was not fitted")
require(sum(enrichment["horizons"][g]["qualified_scope_count"] for g in GROUPS) == 0, "Enrichment qualification summary needs revision")
require(enrichment["orders_placed"] == 0 and enrichment["broker_orders_enabled"] is False, "Enrichment order evidence differs")

evaluation_run = ROOT / plan["evaluation_scope"]["saved_evaluation_run"]
verify_manifest(evaluation_run)
evaluation = read(evaluation_run / "summary.json")
evaluation_rows = pd.read_parquet(evaluation_run / "evaluations.parquet")
stats = evaluation["all_saved_gameplans"]
require(len(evaluation_rows) == stats["forecasts"] == 1296, "Evaluation inventory differs")
require(sum(stats[k] for k in ["evaluated", "mature_awaiting_data", "pending_maturity"]) == 1296, "Evaluation coverage totals differ")
require(plan["prior_gameplan_evaluated_rows"] == stats["evaluated"] == 823, "Evaluation publication differs")

verified_at = datetime.now(timezone.utc).isoformat()
verification = {
    "verified_at": verified_at, "status": "COMPLETE_WITH_RESEARCH_LIMITATIONS", "action_date": "2026-09-09",
    "publication_run": str(RUN), "pointer_manifest_receipt_outputs": "VERIFIED", "receipt_sha256": file_checksum(RUN / "receipt.json"),
    "overnight_run": str(OVERNIGHT), "completed_at": overnight_report["completed_at"], "original_deadline": overnight_report["deadline_at"],
    "resume_chain": chain, "loop_b_source": str(loop_b), "loop_b_manifest_and_outputs": "VERIFIED",
    "xnas_history_receipt": str(HISTORY / "receipt.json"), "xnas_source_files_verified": len(source_files), "xnas_downloads": history_rows,
    "opra": opra, "symbols": SYMBOLS, "forecasts": 168, "stock_only_placeholders": 168,
    "entry_windows": len(entry), "non_entry_outlooks": len(forecasts) - len(entry), "promoted_entry_forecasts": len(qualified_entry), "promoted_bullish_entry_forecasts": len(qualified_bullish),
    "directional_status": {g: reports[g]["promotion_gate"]["status"] for g in GROUPS}, "cohort_rows": cohort_counts,
    "enrichment_run": str(ENRICHMENT), "enrichment_source_cohorts": "NATIVELY_VERIFIED", "enrichment_fitted_horizons": 4,
    "enrichment_fitted_scopes": sum(enrichment["horizons"][g]["fitted_scope_count"] for g in GROUPS), "enrichment_qualified_scopes": 0,
    "evaluation_run": str(evaluation_run), "evaluation": stats, "orders_placed": 0, "broker_orders_enabled": False,
    "production_code_or_immutable_artifacts_changed_by_review": False,
}

lines = []
def add(value=""):
    lines.append(value)

def table(headers, rows):
    add("| " + " | ".join(headers) + " |")
    add("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        add("| " + " | ".join(str(v).replace("|", "/") for v in row) + " |")
    add()

add("# September 9, 2026 Gameplan")
add()
add("**Ready for review — pipeline complete, with model research limitations.**")
add()
add(f"Frozen {pac(plan['frozen_at'])}; published {pac(publication.receipt['published_at'])}. All overnight stages completed {pac(overnight_report['completed_at'])}, before the September 9 04:00 Pacific deadline. Verified {pac(verified_at)}.")
add()
add("September 8 data was fetched across the full Loop A workflow. XNAS.ITCH Historical then supplied all seven stocks, and the preserved run completed final training, predictions, cumulative evaluation, publication, and independent enrichment. The final plan contains **168 forecasts and 168 `NO_TRADE_STOCK_ONLY` option placeholders** for AAPL, AMZN, GOOG, MU, NVDA, SNDK, and COST.")
add()
add("**Review finding:** there are **zero promoted bullish entry forecasts**. The hourly, four-hour, and weekly models are promoted; the daily model remains research-only. AAPL's daily bullish forecast is therefore research, and the three bullish opening-gap forecasts are non-entry research. Learned sizing has four fitted models and zero qualified scopes. These statuses are preserved in the saved plan.")
add()
add("## Preparation evidence")
add()
table(["Step", "Verified result", "Finished Pacific"], [
    ["Loop A: full provider fetch + OPRA", "Complete; original successful stage retained", pac(first_stages['loop_a_close_fetch']['finished_at'])],
    ["Loop B: directional data/models", "Complete; nine newly trained models; original stage retained", pac(first_stages['loop_b_directional_generation']['finished_at'])],
    *[[s['stage'], "Complete; exit 0", pac(s['finished_at'])] for s in overnight_report['stages']],
])
add("Loop A covers configured Databento, FMP, FRED, Schwab, and SEC work, including inline CME and production OPRA history. All 21 OPRA cursors cover September 8 (September 9 exclusive). Retained Loop B evidence contains 219,625 samples, 4,446 backtest predictions and 49 fresh LIVE-mode rows. The four final Gameplan models and 168 frozen forecasts below are the downstream publication.")
add()
add("All seven XNAS downloads used the Historical time-series API under verified $0 cost and capacity preflights. Their overlap interval is September 3 through September 9 exclusive. The table isolates September 8 rows. Actual last observations remain within the enforced five-minute tolerance of the 17:00 close; prices were not substituted from another dataset.")
add()
table(["Stock", "September 8 minute rows", "Overlap download rows", "Last September 8 observation (PDT)"], [[s, next(r['september_8_rows'] for r in history_rows if r['symbol']==s), next(r['rows_in_overlap_download'] for r in history_rows if r['symbol']==s), pac(next(r['last_bar'] for r in history_rows if r['symbol']==s), True)] for s in SYMBOLS])
add(f"Integrity checks passed for the current pointer, manifest, receipt, all output files, all {len(source_files)} bound XNAS source files, the three-attempt resume chain, and the four enrichment source cohorts. All overnight receipts report zero orders and disabled broker authority.")
add()
add("## Model assessment")
add()
add("Promotion requires passing the held-out quality checks. Lower Brier score and log loss are better; the comparator is the training base-rate forecast evaluated on the same assessment rows.")
add()
table(["Horizon", "Status", "Assessment rows", "Brier: model / baseline", "Log loss: model / baseline", "Selected family; calibration"], [[g, reports[g]['promotion_gate']['status'], reports[g]['assessment']['rows'], f"{reports[g]['assessment']['brier_score']:.5f} / {reports[g]['training_base_rate_assessment']['brier_score']:.5f}", f"{reports[g]['assessment']['log_loss']:.5f} / {reports[g]['training_base_rate_assessment']['log_loss']:.5f}", f"{reports[g]['selected_family']}; {reports[g]['calibration_method']}"] for g in GROUPS])
add("The daily model was trained successfully but failed both baseline comparisons: Brier 0.25348 versus 0.24981 and log loss 0.70075 versus 0.69277. Its 35 rows remain `RESEARCH_NOT_PROMOTED`. All groups trained histogram-gradient, MLP, and regularized-logistic candidates; selection and calibration used their development partitions. `none` calibration preserves the raw probability. Published promotion is an offline assessment result, not a guarantee of future returns.")
add()
table(["Horizon", "Aligned cohort", "Train", "Selection", "Calibration", "Assessment", "Boundary rows excluded"], [[g, cohort_counts[g], *[reports[g]['partitions'][k+'_rows'] for k in ['train','selection','calibration','assessment']], reports[g]['target_boundary_quality']['excluded_rows']] for g in GROUPS])
add("Aligned cohorts are saved separately from their purged chronological partitions. A target needs actual observations within five minutes of both boundaries. Excluded rows remain excluded.")
add()
add("## Independent sizing enrichment")
add()
table(["Horizon", "Fitted", "Fitted scopes", "Qualified scopes"], [[g, enrichment['horizons'][g]['status'], enrichment['horizons'][g]['fitted_scope_count'], enrichment['horizons'][g]['qualified_scope_count']] for g in GROUPS])
add("All 178 fitted scopes remain research under their saved qualification checks. Fitting completed, but learned sizing is not qualified. The separately selected fixed-horizon-budget strategy does not require learned sizing promotion; the forecast/model gates remain separate. This review does not start or change the daytime trader.")
add()
add("## Cumulative saved-forecast evaluation")
add()
table(["Source action date", "Saved forecasts", "Evaluated", "Mature awaiting data", "Pending maturity"], [[d, *[r[k] for k in ['forecasts','evaluated','mature_awaiting_data','pending_maturity']]] for d,r in evaluation['by_action_date'].items()] + [["Total", *[stats[k] for k in ['forecasts','evaluated','mature_awaiting_data','pending_maturity']]]])
add(f"The {stats['evaluated']} scored directional forecasts have {stats['direction_accuracy']:.2%} direction accuracy and mean Brier score {stats['mean_brier_score']:.5f}. These aggregate retained publications and their own source contracts, including earlier revisions. The 174 mature forecasts with unavailable target evidence remain unscored; 299 have not matured. This is directional forecast evaluation; realized trade or option P/L is not inferred.")
add()
add("## Exact target windows")
add()
add("All times below are **Pacific daylight time (UTC−07:00)**. There are 19 nominal entry windows per stock (133 total), plus the opening-gap research row and four daily outlooks per stock (35 total). Of the 133 nominal entry rows, 126 have promoted models; the seven daily entry rows remain research-only. A nominal entry window does not by itself establish model eligibility.")
add()
window_cols = ['route','target_window_start','target_window_end','target_role','execution_eligible','trading_hours']
windows = forecasts[window_cols].drop_duplicates()
require(len(windows) == 24, "Forecast windows differ by symbol")
table(["Route", "Start PDT", "End PDT", "Trading hours", "Role"], [[r.route, pac(r.target_window_start, True), pac(r.target_window_end, True), f"{r.trading_hours:g}", r.target_role] for r in windows.itertuples(index=False)])
add("The 16:00 four-hour window contains September 9 16:00–17:00 and September 10 04:00–07:00, with the closed interval between them. The weekly window spans five exchange sessions: September 9, 10, 11, 14, and 15. The opening-gap row starts at September 8 17:00 and is explicitly non-entry research.")
add()
add("## Frozen forecasts by stock")
add()
add("Raw and calibrated columns are the saved model probabilities for a cost-adjusted positive return over the exact route window above, rounded to two percentage decimals. Direction and status are copied from the immutable forecasts, not recomputed from rounded values. `NO_EDGE` is the saved neutral label. The source parquet retains full precision.")
add()
for symbol in SYMBOLS:
    add(f"### {symbol}")
    add()
    rows=[]
    for r in forecasts[forecasts.symbol.eq(symbol)].itertuples(index=False):
        model_state = "Promoted" if r.model_status == 'PROMOTED' else "Research model"
        role = "entry window" if r.execution_eligible else "gap research" if r.target_role == 'OPENING_GAP_RESEARCH' else "outlook"
        rows.append([r.route, f"{r.raw_probability:.2%}", f"{r.calibrated_probability:.2%}", r.direction, f"{model_state}; {role}"])
    table(["Route", "Raw", "Calibrated", "Direction", "Status / role"],rows)
add("## Daily operation and evidence links")
add()
add("Loops Overnight Gameplan is scheduled daily at 21:05 America/Los_Angeles for the full fetch and downstream workflow, with native non-session no-ops. Operations Watch checks at :00 and :30 and can recover unfinished work under the single-owner claim. New missing daily work is eligible only after 21:15. The separate September 9 review follow-up is paused after this completed review; the daily schedules continue.")
add()
sources = [
    ("Final review verification", OUT/'final-verification.json'), ("Original Loop B review verification", OUT/'verification.json'),
    ("Verified daily schedule", OUT/'daily-schedule-verification.json'), ("Current Gameplan pointer", ROOT/'ml/nightly-gameplan-latest/run.json'),
    ("Immutable Gameplan receipt", RUN/'receipt.json'), ("Immutable Gameplan manifest", RUN/'manifest.json'),
    ("Frozen forecasts, full precision", RUN/'forecasts.parquet'), ("Stock-only option placeholders", RUN/'option-strategy-intents.parquet'),
    ("Four final model reports", RUN/'model-reports.json'), ("Complete overnight receipt", OVERNIGHT/'receipt.json'),
    ("Overnight stage report", OVERNIGHT/'stage-report.json'), ("XNAS Historical receipt", HISTORY/'receipt.json'),
    ("XNAS exact cost preflight", HISTORY/'cost-preflight.json'), ("Sizing enrichment report", ENRICHMENT/'training-report.json'),
    ("Cumulative evaluation summary", evaluation_run/'summary.json'),
]
for label, path in sources:
    add("- "+link(label,path))
add()

# Write only review copies after every native/receipt/source check has passed.
require(read_current_gameplan(ROOT).run_directory == RUN, "Pointer changed during review")
verification_path = OUT/'final-verification.json'
verification_path.write_text(json.dumps(verification, indent=2, default=str)+'\n', encoding='utf-8')
document = OUT/'Gameplan-2026-09-09.md'
document.write_text('\n'.join(lines), encoding='utf-8')
text = document.read_text(encoding='utf-8')
links = re.findall(r'\]\((C:/[^)]+)\)',text)
require(all(Path(p).is_file() for p in links), "A document evidence link is missing")
probability_lines = [r for r in text.splitlines() if r.startswith('| ') and '%' in r and r.count('|')==6]
require(len(probability_lines)==168, "Rendered probability table count differs")
document_verification = {"observed_at": verified_at, "status":"FINAL_REVIEW_VERIFIED", "document": str(document),
    "document_sha256": hashlib.sha256(document.read_bytes()).hexdigest(), "existing_local_evidence_links":len(links),
    "probability_rows":168,"probability_columns":2,"exact_route_windows":24,"action_date":"2026-09-09",
    "publication_receipt_sha256":file_checksum(RUN/'receipt.json'),"research_limits_disclosed":True,
    "production_code_changed_by_review":False,"followup_id":"september-9-gameplan-review",
    "followup_pause_pending":True}
(OUT/'document-verification.json').write_text(json.dumps(document_verification,indent=2)+'\n',encoding='utf-8')
print(json.dumps({"status":"VERIFIED", "document":str(document),"forecast_rows":len(forecasts),"xnas_source_files_verified":len(source_files),"directional_status":verification['directional_status'],"enrichment_qualified_scopes":0,"evaluation":stats,"document_sha256":document_verification['document_sha256']},indent=2))
