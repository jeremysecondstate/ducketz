"""Offline audit of the September 11 source / September 14 action overnight run.

Run from the repository with its .venv Python. This script never publishes,
trains, captures an account, calls a provider, claims a lock, or submits orders.
All recomputation uses saved account evidence and the local verified archive.
Based on the 20260910T040818.113702Z operator verifier, retaining its source,
model, forecast, cash/share, evaluation and actuals checks. A failed ancestor
is allowed only when the selected terminal resume completes the original stages.
Reference completion is a separately labeled assumption, never an actual price.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--overnight-run", type=Path, required=True)
parser.add_argument("--repository", type=Path, default=Path("C:/dev/ducketz"))
parser.add_argument("--datastore", type=Path, default=Path("C:/DATASTORE"))
parser.add_argument("--expected-original-run", default="20260912T040657.173506Z")
parser.add_argument("--expected-source-date", default="2026-09-11")
parser.add_argument("--expected-action-date", default="2026-09-14")
parser.add_argument("--expected-deadline", default="2026-09-14T11:00:00Z")
args = parser.parse_args()
sys.path.insert(0, str(args.repository.resolve()))

import pandas as pd
from ml.artifacts import file_checksum, verify_manifest
from ml.nightly_gameplan import read_current_gameplan, read_gameplan_run, _verify_opra_history
from ml.stock_trader.independent_signals import (
    _validated_independent_forecasts, _validate_intent_keys, verified_promoted_model_groups,
)
from ml.gameplan_actuals_review import (
    _saved_trade_plan, _latest_saved_gameplan, _match_trade_rows, previous_action_date,
    compare_forecasts, compare_price_points,
)
from ml.gameplan_cash_ledger import project_direction_trades
from ml.stock_trader.contracts import StockTraderPolicy
from ml.gameplan_evaluation import read_evaluation_history
from ml.stock_trader.model import load_current_enrichment_model

root = args.datastore.resolve()
overnight = args.overnight_run.resolve()
summary = {"verified_at": pd.Timestamp.now(tz="UTC").isoformat(),
           "overnight_run": str(overnight), "verification_is_read_only": True,
           "errors": [], "coverage_notes": [], "checks": {}}
context = {}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def same_number(left, right):
    return math.isclose(float(left), float(right), rel_tol=1e-10, abs_tol=1e-7)


def zero_orders(payload, label):
    require(payload.get("orders_placed") == 0 and payload.get("broker_orders_enabled") is False,
            label + " must explicitly report zero orders and disabled broker orders")


def input_matches(manifest, path):
    resolved = Path(path).resolve()
    entries = [item for item in manifest.get("input_files", [])
               if (root / item["path"]).resolve() == resolved]
    require(len(entries) == 1 and entries[0].get("status") == "present",
            "Expected one recorded immutable input: " + str(resolved))
    require(entries[0]["size"] == resolved.stat().st_size
            and entries[0]["checksum_sha256"] == file_checksum(resolved),
            "Immutable input checksum differs: " + str(resolved))


def pointer_run(pointer_path, folder):
    payload = read(root / pointer_path)
    current = payload.get("current", payload)
    run = (root / current["run_path"]).resolve()
    require(run.parent == (root / folder).resolve(), "Pointer escapes its run directory")
    receipt = read(run / "receipt.json")
    expected = current.get("receipt_sha256", current.get("receipt_checksum_sha256"))
    require(expected == file_checksum(run / "receipt.json"), "Pointer receipt checksum differs")
    manifest = verify_manifest(run)
    expected_manifest = receipt.get("manifest_sha256", receipt.get("manifest_checksum_sha256"))
    require(expected_manifest == file_checksum(run / "manifest.json"), "Receipt manifest checksum differs")
    require(receipt.get("run_path") == run.relative_to(root).as_posix(), "Receipt run path differs")
    return run, receipt, manifest, current


def section(name, function):
    try:
        summary["checks"][name] = {"status": "VERIFIED", **function()}
    except Exception as exc:
        message = type(exc).__name__ + ": " + str(exc)
        summary["errors"].append({"check": name, "error": message})
        summary["checks"][name] = {"status": "FAILED", "error": message}


def local_prices(symbols, contract):
    from ml.stock_target_prices import load_stock_target_prices
    key = tuple(sorted(symbols)), contract
    cache = context.setdefault("local_archive_cache", {})
    if key not in cache:
        cache[key] = load_stock_target_prices(root, symbols=key[0], source_contract=contract)
    return cache[key]


def check_overnight():
    current, chain, completed, seen = overnight, [], {}, set()
    deadline = None
    pinned = None
    while current:
        require(current.parent == root / "ml/overnight-runs" and current not in seen,
                "Invalid overnight resume ancestry")
        seen.add(current)
        report = read(current / "stage-report.json")
        receipt = read(current / "receipt.json")
        require(receipt["run_path"] == current.relative_to(root).as_posix(), "Overnight receipt path differs")
        require(receipt["status"] == report["status"], "Overnight receipt status differs")
        require(receipt["stage_report_checksum_sha256"] == file_checksum(current / "stage-report.json")
                and receipt["stage_report_size"] == (current / "stage-report.json").stat().st_size,
                "Overnight stage report checksum/size differs")
        zero_orders(receipt, "Overnight receipt")
        zero_orders(report, "Overnight stage report")
        if deadline is None:
            deadline = report["deadline_at"]
            context["overnight_report"] = report
            require(report["status"] == "COMPLETE", "Final overnight attempt is not COMPLETE")
        require(report["deadline_at"] == deadline, "Resume changed original publication deadline")
        if pinned is None:
            pinned = report.get("enrichment_gameplan")
        if report.get("enrichment_gameplan") is not None:
            require(report["enrichment_gameplan"] == pinned, "Resume changed its pinned Gameplan")
        require(report.get("stock_price_source") == "xnas-itch-archive-v1"
                and report.get("stock_only") is True and report.get("independent_stock_horizons") is True,
                "Overnight source or stock-only independent mode differs")
        for name, record in receipt.get("logs", {}).items():
            path = (current / name).resolve()
            require(path.parent == current and path.stat().st_size == record["size"]
                    and file_checksum(path) == record["checksum_sha256"], "Overnight log evidence differs: " + name)
        for stage in report.get("stages", []):
            if stage["status"] == "COMPLETE":
                require(stage.get("exit_code") == 0, "Completed stage has nonzero exit code")
                completed.setdefault(stage["stage"], {**stage, "overnight_run": str(current)})
        chain.append({"run": str(current), "status": report["status"], "stage_order": report["stage_order"]})
        current = Path(report["resumed_from"]).resolve() if report.get("resumed_from") else None
    required = chain[-1]["stage_order"]
    expected_order = ["loop_a_close_fetch", "loop_b_directional_generation", "stock_target_history",
                      "gameplan_evaluation", "gameplan_publication", "stock_enrichment_training",
                      "gameplan_trade_planning", "gameplan_actuals_review"]
    require(required == expected_order, "Original independent overnight stage order differs")
    for attempt in chain:
        order = attempt["stage_order"]
        require(order and order == expected_order[expected_order.index(order[0]):],
                "Resume did not retain the ordered original remaining stages")
    require(all(stage in completed for stage in required), "A recorded original stage lacks successful completion")
    require(set(required).issubset(completed), "Incomplete original stage order")
    context["completed_stages"] = completed
    context["required_stages"] = required
    context["deadline"] = pd.Timestamp(deadline)
    context["action_date"] = context["deadline"].tz_convert("America/Los_Angeles").date().isoformat()
    require(context["deadline"].tz_convert("America/Los_Angeles").strftime("%H:%M") == "04:00",
            "Original deadline is not 04:00 Pacific")
    require(Path(chain[-1]["run"]).name == args.expected_original_run,
            "Terminal run does not descend from this scheduled attempt")
    require(context["action_date"] == args.expected_action_date
            and context["deadline"] == pd.Timestamp(args.expected_deadline)
            and previous_action_date(context["action_date"]) == args.expected_source_date,
            "Source/action date or original deadline differs from this scheduled run")
    return {"attempt_chain": chain, "completed_stages": list(completed), "deadline_at": deadline,
            "expected_action_date": context["action_date"], "orders_placed": 0,
            "log_checksums_verified": True}


def check_gameplan():
    pub = read_current_gameplan(root)
    cfg = pub.manifest["configuration"]
    pinned = context["overnight_report"].get("enrichment_gameplan")
    require(pinned and pinned["run_path"] == pub.run_directory.relative_to(root).as_posix()
            and pinned["receipt_sha256"] == file_checksum(pub.run_directory / "receipt.json"),
            "Current Gameplan differs from the overnight pinned source")
    require(pub.receipt["action_date"] == context["action_date"], "Gameplan next-session date differs")
    require(cfg["target_price_source_contract"] == "xnas-itch-archive-v1"
            and cfg["target_price_dataset"] == "XNAS.ITCH", "Gameplan price source differs")
    symbols = tuple(line.strip().upper() for line in (args.repository / "datafetching/watchlist.txt")
                    .read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#"))
    require(set(symbols) == set(cfg["symbols"]), "Current Gameplan universe differs from production watchlist")
    forecasts = _validated_independent_forecasts(pd.read_parquet(pub.run_directory / "forecasts.parquet"),
                    action_date=pub.receipt["action_date"], symbols=tuple(cfg["symbols"]))
    intents = pd.read_parquet(pub.run_directory / "option-strategy-intents.parquet")
    _validate_intent_keys(intents, forecasts)
    require(len(forecasts) == len(symbols) * 24 and len(intents) == len(forecasts)
            and intents.groupby("symbol").size().eq(24).all(), "Forecast/intent row counts differ")
    require(set(intents.id) == set(forecasts.id + ":OPTION") and intents.plan_status.eq("NO_TRADE_STOCK_ONLY").all(),
            "Stock-only intent identity/status differs")
    for field in ("legs_json", "candidate_key", "strategy_source_run"):
        require(intents[field].isna().all(), "Stock-only intent contains executable option data")
    require(int(forecasts.execution_eligible.sum()) == 19 * len(symbols), "Entry opportunity count differs")
    promoted = verified_promoted_model_groups(pub)
    require(set(forecasts.loc[forecasts.model_status.eq("PROMOTED"), "model_group"]).issubset(promoted),
            "Forecast promotion lacks native authority")
    for horizon in ("1h", "4h", "1d", "1w"):
        require("training-cohort-" + horizon + ".parquet" in pub.manifest["output_files"], "Native training cohort missing")
    zero_orders(pub.receipt, "Gameplan receipt")
    plan = read(pub.run_directory / "gameplan.json")
    zero_orders(plan, "Gameplan payload")
    require(pd.Timestamp(pub.receipt["published_at"]) < context["deadline"], "Gameplan publication missed deadline")
    require(len(symbols) == 7 and len(set(symbols)) == 7, "This scheduled run requires seven configured symbols")
    require(forecasts.target_price_source_contract.eq("xnas-itch-archive-v1").all()
            and forecasts.target_price_dataset.eq("XNAS.ITCH").all(), "Frozen rows changed the native target source")
    reports = read(pub.run_directory / "model-reports.json")
    model_details = {}
    for horizon, report in reports.items():
        require(report["target_price_source_contract"] == "xnas-itch-archive-v1"
                and report["target_price_dataset"] == "XNAS.ITCH", "Model report target source differs")
        model_details[horizon] = {key: report.get(key) for key in (
            "promotion_gate", "assessment", "training_base_rate_assessment", "partitions",
            "calibration_diagnostics", "calibration_method", "selected_family",
            "selected_logistic_regularization_c", "logistic_regularization_candidates",
            "target_support_by_symbol", "target_boundary_quality")}
    research = forecasts.loc[~forecasts.model_status.eq("PROMOTED"), ["symbol", "route", "model_status"]]
    if len(research):
        summary["coverage_notes"].append({"directional_research_rows": research.to_dict("records"),
                                         "evidence": str(pub.run_directory / "model-reports.json")})
    context.update(publication=pub, forecasts=forecasts, symbols=symbols)
    model_counts = forecasts.groupby(["model_group", "model_status"]).size()
    return {"run": str(pub.run_directory), "action_date": pub.receipt["action_date"], "symbols": list(symbols),
            "forecast_rows": len(forecasts), "intent_rows": len(intents),
            "rows_per_symbol": forecasts.groupby("symbol").size().to_dict(),
            "execution_rows": int(forecasts.execution_eligible.sum()), "verified_promoted_groups": sorted(promoted),
            "model_status_counts": {"/".join(key): int(value) for key, value in model_counts.items()},
            "model_assessments": model_details,
            "stock_price_source": {key: plan.get("stock_price_source", {}).get(key) for key in
                                   ("status", "source_contract", "dataset", "schema", "rows", "symbols", "source_policy")}}


def check_opra():
    cfg = context["publication"].manifest["configuration"]
    recorded = cfg["opra_history"]
    _, current = _verify_opra_history(root, symbols=context["symbols"],
        action_date=pd.Timestamp(context["action_date"]).date(),
        required_completed_through=pd.Timestamp(recorded["required_completed_through"]).date())
    require(recorded["verified_cursor_count"] == len(context["symbols"]) * 3
            and set(current["verified_cursors"]) == set(recorded["verified_cursors"]),
            "Production OPRA cursor universe differs")
    return {"recorded": recorded, "current_verified": current,
            "provider_coverage_boundary": "Native OPRA and XNAS receipts; inspect Loop A log for FMP/FRED/Schwab/SEC scope outcomes"}


def check_enrichment():
    load_current_enrichment_model(root)  # Native source/cohort/partition/qualification verification; no training.
    run, receipt, manifest, _ = pointer_run("ml/stock-trader-model-latest/run.json", "ml/stock-trader-model-runs")
    report = read(run / "training-report.json")
    require(report["source_gameplan_run"] == context["publication"].run_directory.relative_to(root).as_posix(),
            "Enrichment model is bound to a different Gameplan")
    require(receipt["training_report_sha256"] == file_checksum(run / "training-report.json"),
            "Enrichment training report checksum differs")
    zero_orders(report, "Enrichment training report")
    horizons = {group: {key: value.get(key) for key in ("status", "reason", "admitted_rows", "fitted_scope_count",
                    "qualified_scope_count", "diagnostic_scope_count", "symbols_without_targets")}
                for group, value in report["horizons"].items()}
    require(set(horizons) == {"1h", "4h", "1d", "1w"}, "Enrichment horizon groups differ")
    research = [group for group, value in horizons.items() if not value.get("qualified_scope_count")]
    if research:
        summary["coverage_notes"].append({"enrichment_research_only_groups": research, "evidence": str(run / "training-report.json")})
    return {"run": str(run), "source_gameplan_run": report["source_gameplan_run"], "horizons": horizons}


def check_stock_history():
    """Verify existing local native acquisition evidence; never call its CLI."""
    record = context["completed_stages"]["stock_target_history"]
    log_path = Path(record["overnight_run"]) / "stock_target_history.log"
    matches = re.findall(r"^Stock target history receipt: (.+)$", log_path.read_text(encoding="utf-8"), re.M)
    require(len(matches) == 1, "Stock history stage must identify its exact native receipt")
    receipt_path = Path(matches[0].strip()).resolve()
    run = receipt_path.parent
    require(run.parent == root / "ml/stock-target-history-runs", "Stock history receipt escapes native run root")
    receipt, manifest = read(receipt_path), read(run / "manifest.json")
    require(receipt["status"] in {"COMPLETE", "CURRENT"} and receipt["orders_placed"] == 0,
            "Stock history stage is incomplete or reports orders")
    through = (pd.Timestamp(args.expected_source_date) + pd.Timedelta(days=1)).date().isoformat()
    require(receipt["completed_through"] == through
            and receipt["manifest_sha256"] == file_checksum(run / "manifest.json"),
            "Stock history receipt has wrong boundary or manifest checksum")
    from datafetching.databento_cold_start import _validate_manifest_checksum, _validate_manifest_included_scope
    _validate_manifest_checksum(manifest)
    _validate_manifest_included_scope(manifest)
    requests = manifest["requests"]
    require(all(row["dataset"] == "XNAS.ITCH" and row["schema"] == "ohlcv-1m"
                and len(row["symbol_scope"]) == 1 and row["symbol_scope"][0] in context["symbols"]
                for row in requests), "Stock history included another dataset/schema/symbol")
    acquisition = {"requests": len(requests), "status": receipt["status"], "counts": receipt.get("counts", {})}
    if requests:
        cost, capacity = read(run / "cost-preflight.json"), read(run / "preflight.json")
        require(receipt["cost_preflight_sha256"] == file_checksum(run / "cost-preflight.json")
                and receipt["preflight_sha256"] == file_checksum(run / "preflight.json"),
                "Acquisition preflight checksums differ")
        request_ids = {row["request_id"] for row in requests}
        require({row["request_id"] for row in cost["requests"]} == request_ids
                and {row["request_id"] for row in capacity["estimates"]} == request_ids,
                "Cost/capacity preflights do not cover the exact acquired requests")
        require(cost["maximum_cost_usd"] == cost["total_cost_usd"] == receipt["estimated_cost_usd"] == 0
                and all(row["estimated_cost_usd"] == 0 for row in cost["requests"])
                and capacity["capacity_pass"] is True
                and receipt.get("counts", {}).get("failed", 0) == 0,
                "Native stock history violated its zero-dollar, capacity, or acquisition gate")
        acquisition.update(cost_usd=cost["total_cost_usd"], capacity_pass=capacity["capacity_pass"])
    prices, files, inventory = local_prices(context["symbols"], "xnas-itch-archive-v1")
    start = pd.Timestamp(args.expected_source_date, tz="America/Los_Angeles").replace(hour=4).tz_convert("UTC")
    end = pd.Timestamp(args.expected_source_date, tz="America/Los_Angeles").replace(hour=17).tz_convert("UTC")
    by_symbol = {}
    for symbol in context["symbols"]:
        partitions = [item for item in inventory["partitions"] if item["symbol"] == symbol]
        intervals = []
        for item in partitions:
            if item.get("delivery_mode") == "live-intraday-replay":
                session = pd.Timestamp(item["session"], tz="America/Los_Angeles")
                intervals.append((session.replace(hour=4).tz_convert("UTC"), session.replace(hour=17).tz_convert("UTC")))
            else:
                intervals.append((pd.to_datetime(item["start"], utc=True), pd.to_datetime(item["end"], utc=True)))
        cursor = start
        for left, right in sorted(intervals):
            if left <= cursor < right:
                cursor = right
        require(cursor >= end, "Native acquired intervals fail completed-session coverage: " + symbol)
        rows = prices.loc[prices.symbol.eq(symbol) & prices.timestamp.ge(start) & prices.timestamp.lt(end)]
        require(len(rows) > 0, "Native completed-session prices missing: " + symbol)
        by_symbol[symbol] = {"rows": len(rows), "first_bar_start": rows.timestamp.min().isoformat(),
            "last_bar_start": rows.timestamp.max().isoformat(),
            "last_actual_close_observed_at": (rows.timestamp.max() + pd.Timedelta(minutes=1)).isoformat(),
            "close_gap_minutes": (end - rows.timestamp.max() - pd.Timedelta(minutes=1)).total_seconds() / 60,
            "requested_interval_coverage": "VERIFIED"}
    return {"run": str(run), "receipt": str(receipt_path), "completed_through": through,
            "source_session": args.expected_source_date, "acquisition": acquisition,
            "native_archive_partitions_verified": inventory["native_archive_partitions_verified"],
            "native_source_files_verified": len(files), "by_symbol": by_symbol}


def check_fetch_log():
    record = context["completed_stages"]["loop_a_close_fetch"]
    log_path = Path(record["overnight_run"]) / "loop_a_close_fetch.log"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    opra = [line for line in lines if line.startswith("Options history maintenance finished:")]
    require(len(opra) == 1, "Fetch stage lacks one completed OPRA maintenance summary")
    stats = dict(re.findall(r"(\w+)=([^; ]+)", opra[0]))
    require(int(stats["requested_scopes"]) == int(stats["completed_scopes"]) == 21
            and all(int(stats[key]) == 0 for key in ("capacity_blocked_scopes", "failed_scopes", "bootstrap_required_scopes", "deferred_scopes"))
            and float(stats["selected_estimated_cost_usd"]) == 0,
            "Production OPRA maintenance did not complete 21 zero-dollar scopes")
    scopes = [line for line in lines if line.startswith("OPRA symbol/schema history:")]
    require(len(scopes) == 21 and all("status=COMPLETE;" in line for line in scopes),
            "OPRA per-scope completions differ")
    # Other providers' scope semantics differ; preserve their exact native
    # summaries and warnings for the supervisor's evidence-based assessment.
    native_summaries = [line for line in lines if re.search(
        r"(Loop A|finished:|summary|Summary|failed|Failed|ERROR|WARNING|NO_DATA|SKIP|UNAVAILABLE)", line)]
    providers = sorted({match.group(1) for line in lines
                        for match in [re.search(r"src=([^/ ]+)", line)] if match})
    return {"log": str(log_path), "opra_summary": stats,
            "providers_in_request_log": providers, "native_scope_summaries": native_summaries,
            "remaining_manual_review": "Assess FMP/FRED/Schwab/SEC and nonproduction Databento scope warnings from the preserved native summaries; these providers do not share the OPRA cursor contract."}


def check_trade_plan():
    pub = context["publication"]
    saved = _saved_trade_plan(root, pub)
    require(saved is not None, "Matching pre-open successor trade plan is missing")
    run, rows, price_path = saved
    pointed, receipt, manifest, _ = pointer_run("ml/gameplan-trade-plan-latest/run.json", "ml/gameplan-trade-plan-runs")
    require(run == pointed, "Trade-plan current pointer differs from pinned successor plan")
    require(receipt["status"] == "COMPLETE" and receipt["action_date"] == context["action_date"], "Trade-plan receipt differs")
    zero_orders(receipt, "Trade-plan receipt")
    report = read(run / "report.json")
    snapshot = read(run / "account-snapshot.json")
    ledger = read(run / "direction-ledger.json")
    require(report["snapshot"] == snapshot and report["direction_based_projection"] == ledger,
            "Trade report snapshot or ledger differs from saved evidence")
    require(snapshot["status"] == "OBSERVED" and snapshot["cash_status"] == "CASH_ONLY_BOUNDED"
            and snapshot["orders_placed"] == 0 and snapshot["orders_enabled"] is False
            and snapshot["broker_data_http_methods"] == ["GET"]
            and snapshot["ownership"]["safe_for_planning"] is True,
            "Planning snapshot lacks fresh read-only cash/ownership evidence")
    require(set(snapshot["held_shares"]) == set(context["symbols"])
            and set(snapshot["quotes"]) == set(context["symbols"]), "Snapshot omits a configured stock")
    zero_orders(report, "Trade-plan report")
    require(len(rows) == len(context["symbols"]) * 24 and rows.groupby("symbol").size().eq(24).all(),
            "Augmented trade row counts differ")
    require(pd.Timestamp(report["observed_at"]) <= pd.Timestamp(snapshot["observed_at"])
            <= pd.Timestamp(report["completed_at"]) < context["deadline"], "Account snapshot not captured during this planning run")
    require(pd.Timestamp(report["observed_at"]) <= pd.Timestamp(price_path["observed_at"])
            <= pd.Timestamp(report["completed_at"]), "Price observation not fixed during this planning run")
    require(price_path["price_source_contract"] == pub.manifest["configuration"]["target_price_source_contract"],
            "Planning price path source differs")
    require(len(price_path["points"]) == len(context["symbols"]) * 14, "Planning path must cover 04:00 through 17:00")
    expected_clocks = {f"{hour:02}:00" for hour in range(4, 18)}
    require({(point["symbol"], point["action_date"], point["clock_local"]) for point in price_path["points"].values()}
            == {(symbol, context["action_date"], clock) for symbol in context["symbols"] for clock in expected_clocks},
            "Planning path hourly identities differ")
    require(all(point["status"] == "AVAILABLE" for point in price_path["points"].values()),
            "Completed planning path has an unavailable hourly price point")
    rebuilt_rows, rebuilt_ledger = project_direction_trades(rows, snapshot, price_path,
                                                    policy=StockTraderPolicy(**report["sizing_policy"]))
    require(rebuilt_ledger == ledger, "Saved ledger differs from deterministic native account/price projection")
    fields = [column for column in rebuilt_rows if column.startswith("direction_based_")
              or column.startswith("projected_cash_after_") or column.startswith("projected_shares_after")
              or column.startswith("projected_available_shares_after")]
    pd.testing.assert_frame_equal(rebuilt_rows.set_index("id")[fields].sort_index(), rows.set_index("id")[fields].sort_index(),
                                  check_dtype=False, check_exact=True)
    cash = {field: ledger["summary"]["starting_cash"] for field in ("low", "base", "high")}
    shares = dict(ledger["starting_positions"])
    hourly = {row["timestamp"]: row for row in ledger["hourly"]}
    require(len(hourly) == 14, "Hourly cash ledger has missing clocks")
    for clock, hour in hourly.items():
        events = [event for event in ledger["events"] if event["timestamp"] == clock]
        require(hour["event_sequences"] == [event["sequence"] for event in events], "Hourly event identity differs")
        for event in events:
            for field in cash:
                require(same_number(event["cash_before_" + field], cash[field]), "Event starting cash fails conservation")
                cash[field] += event["cash_change_" + field]
                require(same_number(event["cash_" + field], cash[field]), "Event ending cash fails conservation")
            symbol = event["symbol"]
            require(same_number(event["shares_before"], shares[symbol]), "Event starting shares fail conservation")
            shares[symbol] += event["quantity"] * (1 if event["action"] == "BUY" else -1)
            require(same_number(event["shares_after"], shares[symbol]), "Event ending shares fail conservation")
        require(all(same_number(hour["cash_" + field], cash[field]) for field in cash), "Hourly cash fails conservation")
        require(hour["held_shares"] == shares, "Hourly shares fail conservation")
    require(all(same_number(ledger["summary"]["ending_cash_" + field], cash[field]) for field in cash), "EOD cash fails conservation")
    require(ledger["ending_positions"] == shares, "EOD shares fail conservation")
    require(ledger["no_fill_baseline"] == {"cash": snapshot["available_cash"], "held_shares": ledger["starting_positions"]},
            "No-fill baseline differs from literal available cash and holdings")
    from ml.gameplan_trade_planning import _plan_working_price_rows
    bands = read(run / "price-bands.json")
    # v4 standalone capacity uses the conditional hourly working range. The
    # separately retained historical stress band is not its purchase price.
    capacity_rows = _plan_working_price_rows(context["forecasts"], snapshot, bands, price_path,
                                           policy=StockTraderPolicy(**report["sizing_policy"]))
    capacity_fields = [field for field in capacity_rows if field.startswith("projected_trade_") or field == "projected_quantity_reason"]
    pd.testing.assert_frame_equal(capacity_rows.set_index("id")[capacity_fields].sort_index(),
                                  rows.set_index("id")[capacity_fields].sort_index(), check_dtype=False, check_exact=True)
    readable = (run / "Gameplan.md").read_text(encoding="utf-8")
    require(readable.count("| Projected Trade Quantity | Direction Based Trade Qty |") == len(context["symbols"]),
            "Readable review lost adjacent capacity/direction columns")
    input_matches(manifest, pub.run_directory / "receipt.json")
    context.update(trade_plan=run, trade_report=report, trade_manifest=manifest, trade_receipt=receipt)
    return {"run": str(run), "readable_gameplan": str(run / "Gameplan.md"), "rows": len(rows),
            "snapshot_observed_at": snapshot["observed_at"], "cash_status": snapshot["cash_status"],
            "available_cash": snapshot["available_cash"], "pending_reserved_cash": snapshot["reserved_cash"],
            "held_shares": snapshot["held_shares"], "quote_observation_times": {symbol: value.get("price_reference_time")
                for symbol, value in snapshot["quotes"].items()}, "price_observed_at": price_path["observed_at"],
            "planning_price_points": len(price_path["points"]), "hourly_rows": len(hourly), "event_count": len(ledger["events"]),
            "cash_share_conservation": "VERIFIED_BY_NATIVE_RECOMPUTATION_AND_EVENT_ROLLFORWARD",
            "standalone_capacity_recomputed": True, "summary": ledger["summary"]}


def check_evaluation():
    result = read_evaluation_history(root)
    require(result is not None, "Cumulative Gameplan evaluation is missing")
    manifest = verify_manifest(result.run_directory)
    frame = result.evaluations
    observed = pd.Timestamp(result.summary["observed_at"])
    expected, publications = set(), []
    for receipt_path in (root / "ml/nightly-gameplan-runs").glob("*/receipt.json"):
        receipt = read(receipt_path)
        if receipt.get("action_date", "") < "2026-09-04" or pd.Timestamp(receipt["published_at"]) > observed:
            continue  # Tonight's publication can legitimately postdate its cumulative evaluation.
        pub = read_gameplan_run(root, receipt_path.parent)
        forecasts = pd.read_parquet(pub.run_directory / "forecasts.parquet")
        symbols = pub.manifest["configuration"]["symbols"]
        require(set(forecasts.symbol) == set(symbols) and forecasts.groupby("symbol").size().eq(24).all(),
                "Historical Gameplan differs from its own symbol manifest")
        run_name = pub.run_directory.relative_to(root).as_posix()
        expected.update((run_name, str(identity)) for identity in forecasts.id)
        input_matches(manifest, receipt_path)
        publications.append({"run": run_name, "action_date": receipt["action_date"], "symbols": len(symbols), "rows": len(forecasts)})
    actual = set(zip(frame.source_gameplan_run.astype(str), frame.source_forecast_id.astype(str)))
    require(actual == expected and len(actual) == len(frame), "Cumulative evaluation does not cover every saved pre-evaluation forecast exactly once")
    if int(frame.evaluation_status.eq("MATURE_AWAITING_DATA").sum()):
        summary["coverage_notes"].append({"cumulative_mature_awaiting_data": int(frame.evaluation_status.eq("MATURE_AWAITING_DATA").sum()),
                                         "evidence": str(result.run_directory / "summary.json")})
    return {"run": str(result.run_directory), "observed_at": str(observed), "publications": publications,
            "coverage_counts": result.summary["all_saved_gameplans"], "by_action_date": result.summary["by_action_date"]}


def check_planning_prices():
    from ml.gameplan_price_bands import (build_entry_price_bands, build_planning_price_path,
        COMPLETED_PRICE_BAND_CONTRACT, COMPLETED_PLANNING_PRICE_PATH_CONTRACT)
    run = context["trade_plan"]
    bands = read(run / "price-bands.json")
    saved = read(run / "planning-price-path.json")
    cfg = context["trade_manifest"]["configuration"]
    enabled = cfg.get("allow_reference_forward_fill")
    require(type(enabled) is bool and enabled is True,
            "Completed-fallback verification requires the immutable explicit forward-fill configuration")
    require(cfg.get("schema_version") == context["trade_receipt"].get("schema_version")
            == context["trade_report"].get("schema_version") == "cash-aware-gameplan-trade-planning-v4",
            "Reference-completed trade planning must retain its v4 identity")
    require(bands["contract_version"] == COMPLETED_PRICE_BAND_CONTRACT
            and saved["contract_version"] == COMPLETED_PLANNING_PRICE_PATH_CONTRACT,
            "Completed planning price contracts differ")
    require(saved["lookback_sessions"] == bands["lookback_sessions"] == 120
            and saved["minimum_samples"] == bands["minimum_samples"] == 2
            and saved["working_half_width_bps"] == 20,
            "Planning history window, sample minimum, or working allowance differs")
    prices, _, _ = local_prices(context["symbols"], saved["price_source_contract"])
    original_attrs = deepcopy(prices.attrs)
    original_columns = list(prices.columns)
    original_fingerprint = hashlib.sha256(pd.util.hash_pandas_object(prices, index=True).values.tobytes()).hexdigest()
    require("is_synthetic" not in prices or prices.is_synthetic.fillna(False).eq(False).all(),
            "Verified native archive already contains synthetic prices")
    rebuilt_bands = build_entry_price_bands(prices, context["forecasts"], observed_at=saved["observed_at"],
                                           lookback_sessions=bands["lookback_sessions"], minimum_samples=bands["minimum_samples"],
                                           allow_reference_forward_fill=enabled)
    rebuilt = build_planning_price_path(prices, context["forecasts"], observed_at=saved["observed_at"],
                                       working_half_width_bps=saved["working_half_width_bps"], entry_bands=rebuilt_bands,
                                       allow_reference_forward_fill=enabled)
    require(rebuilt_bands == bands, "Saved entry bands differ from native verified local archive recomputation")
    require(rebuilt == saved, "Saved price path differs from native verified local archive observations")
    native_bands = build_entry_price_bands(prices, context["forecasts"], observed_at=saved["observed_at"],
                                          lookback_sessions=bands["lookback_sessions"], minimum_samples=bands["minimum_samples"],
                                          allow_reference_forward_fill=False)
    native_path = build_planning_price_path(prices, context["forecasts"], observed_at=saved["observed_at"],
                                           working_half_width_bps=saved["working_half_width_bps"], entry_bands=native_bands,
                                           allow_reference_forward_fill=False)
    require(set(native_bands["statistics"]) == set(bands["statistics"])
            and set(native_path["points"]) == set(saved["points"]), "Reference completion changed planning scopes")
    history_fields = ("samples", "sample_count", "candidate_sessions", "coverage", "missing_prior_close_count",
                      "missing_entry_open_count", "history_first_session", "history_last_session", "long_gap_sample_count",
                      "ratio_p05", "ratio_p95")
    for key, statistics in bands["statistics"].items():
        require(all(statistics[field] == native_bands["statistics"][key][field] for field in history_fields),
                "Reference completion changed historical entry observations or coverage: " + key)
    for key, point in saved["points"].items():
        native_point = native_path["points"][key]
        require(all(point[field] == native_point[field] for field in ("samples", "sample_count", "timestamp", "endpoint_kind")),
                "Reference completion changed historical path samples or action timestamps: " + key)
    require(prices.attrs == original_attrs and list(prices.columns) == original_columns
            and hashlib.sha256(pd.util.hash_pandas_object(prices, index=True).values.tobytes()).hexdigest() == original_fingerprint,
            "Planning builders modified native archive observations")
    context.update(planning_bands=bands, planning_path=saved, planning_prices=prices,
                   native_planning_bands=native_bands, native_planning_path=native_path)
    return {"source_contract": saved["price_source_contract"], "dataset": saved["price_dataset"],
            "observed_at": saved["observed_at"], "price_points": len(saved["points"]),
            "lookback_sessions": saved["lookback_sessions"], "minimum_samples": saved["minimum_samples"],
            "working_half_width_bps": saved["working_half_width_bps"],
            "reference_sessions": sorted({point["reference_session"] for point in saved["points"].values()}),
            "reference_observation_times": sorted({point["reference_observed_at"] for point in saved["points"].values()}),
            "local_archive_recomputation": "VERIFIED", "allow_reference_forward_fill": enabled,
            "historical_sample_frames_unchanged": True, "native_prices_unchanged": True}


def check_reference_completion():
    from ml.gameplan_price_completion import complete_planning_reference_gaps, PLANNING_REFERENCE_COMPLETION_CONTRACT
    run = context["trade_plan"]
    manifest = context["trade_manifest"]
    cfg = manifest["configuration"]
    artifacts = {}
    for name in ("planning-reference-completion.json", "synthetic-reference-bars.parquet"):
        require(name in manifest["output_files"], "Reference-completion artifact is missing from immutable manifest: " + name)
        path = run / name
        recorded = manifest["output_files"][name]
        require(recorded["size"] == path.stat().st_size and recorded["checksum_sha256"] == file_checksum(path),
                "Reference-completion artifact size/hash differs: " + name)
        artifacts[name] = {"path": str(path), "size": path.stat().st_size, "checksum_sha256": file_checksum(path)}
    completion = read(run / "planning-reference-completion.json")
    path = context["planning_path"]
    bands = context["planning_bands"]
    prices = context["planning_prices"]
    require(cfg["reference_completion_contract"] == completion["contract_version"]
            == PLANNING_REFERENCE_COMPLETION_CONTRACT, "Reference-completion version differs")
    require(completion == bands["reference_completion"] == path["reference_completion"],
            "Saved completion report differs between JSON, bands and path")
    require(context["trade_report"]["reference_completion"]
            == {key: value for key, value in completion.items() if key != "synthetic_bars"},
            "Trade report completion summary differs")
    require(completion == complete_planning_reference_gaps(prices, context["forecasts"], observed_at=path["observed_at"]),
            "Completion report differs from native deterministic local-archive recomputation")
    require(completion["max_gap_minutes"] == 15 and completion["native_boundary_tolerance_seconds"] == 300
            and completion["synthetic_reason"] == "ASSUMED_NO_TRADES"
            and completion["historical_samples_modified"] is False and completion["native_prices_modified"] is False,
            "Reference completion changed the bounded policy or native observation semantics")
    require(completion["price_source_contract"] == path["price_source_contract"] == "xnas-itch-archive-v1"
            and completion["price_dataset"] == path["price_dataset"] == "XNAS.ITCH",
            "Reference completion substituted another source")
    synthetic = pd.read_parquet(run / "synthetic-reference-bars.parquet")
    expected = pd.DataFrame(completion["synthetic_bars"])
    if expected.empty:
        expected = pd.DataFrame(columns=["symbol", "timestamp", "open", "high", "low", "close",
                                        "volume", "is_synthetic", "reason", "original_observed_at"])
    # JSON publication sorts object keys; Parquet retains creation column
    # order. Compare the exact column set and every value independently of
    # this serialization-only ordering difference.
    require(set(synthetic.columns) == set(expected.columns), "Synthetic artifact column set differs")
    columns = sorted(expected.columns)
    pd.testing.assert_frame_equal(synthetic.loc[:, columns], expected.loc[:, columns],
                                  check_dtype=False, check_exact=True)
    require(not synthetic.duplicated(["symbol", "timestamp"]).any(), "Synthetic artifact duplicates a symbol/minute")
    scopes = {str(row.symbol) + "|" + str(row.action_date) for row in context["forecasts"].itertuples()}
    require(set(completion["references"]) == scopes, "Completion reference scopes differ from original forecasts")
    native_times = pd.to_datetime(prices.timestamp, utc=True)
    synthetic_references = {}
    for key, ref in completion["references"].items():
        require(ref["status"] in {"AVAILABLE_OBSERVED", "AVAILABLE_SYNTHETIC"}, "Completed planning has an unavailable close reference")
        require(ref["source_contract"] == "xnas-itch-archive-v1" and ref["dataset"] == "XNAS.ITCH",
                "Reference origin differs from verified XNAS")
        origin_start = pd.Timestamp(ref["origin_bar_start"])
        actual_time = pd.Timestamp(ref["observed_at"])
        boundary = pd.Timestamp(ref["boundary_at"])
        require(ref["session"] == args.expected_source_date and ref["action_date"] == args.expected_action_date,
                "Closing reference crosses the exact prior XNYS session")
        require(actual_time == origin_start + pd.Timedelta(minutes=1) and actual_time <= boundary <= pd.Timestamp(completion["observed_at"]),
                "Reference original observation timestamp or candle completion changed")
        origin = prices.loc[prices.symbol.eq(ref["symbol"]) & native_times.eq(origin_start)]
        require(len(origin) == 1 and float(origin.iloc[0].close) == ref["price"],
                "Reference did not retain the exact actual native close")
        rows = [row for row in completion["synthetic_bars"]
                if row["symbol"] == ref["symbol"] and row["action_date"] == ref["action_date"]]
        if ref["status"] == "AVAILABLE_OBSERVED":
            require(not rows and ref["fill_count"] == 0 and ref["is_synthetic"] is False
                    and ref["effective_at"] == ref["observed_at"], "Native reference was relabeled or synthesized")
        else:
            gap = (boundary - actual_time).total_seconds() / 60
            require(ref["is_synthetic"] is True and ref["reason"] == "ASSUMED_NO_TRADES"
                    and 5 < gap <= 15 and ref["gap_minutes"] == gap and ref["fill_count"] == gap
                    and pd.Timestamp(ref["effective_at"]) == boundary, "Synthetic reference bounds or assumption label differ")
            expected_starts = list(pd.date_range(actual_time, boundary - pd.Timedelta(minutes=1), freq="min"))
            require([pd.Timestamp(row["timestamp"]) for row in rows] == expected_starts,
                    "Synthetic artifact does not contain exactly the trailing absent minute range")
            for row in rows:
                require(not (prices.symbol.eq(ref["symbol"]) & native_times.eq(pd.Timestamp(row["timestamp"]))).any(),
                        "Synthetic artifact replaces an actual native minute")
                require(row["is_synthetic"] is True and row["reason"] == "ASSUMED_NO_TRADES" and row["volume"] == 0
                        and all(row[field] == ref["price"] for field in ("open", "high", "low", "close"))
                        and row["original_observed_at"] == ref["observed_at"]
                        and row["origin_bar_start"] == ref["origin_bar_start"]
                        and row["origin_source_contract"] == ref["source_contract"] and row["origin_dataset"] == ref["dataset"]
                        and row["source_contract"] == PLANNING_REFERENCE_COMPLETION_CONTRACT,
                        "Synthetic row lost original-close lineage or assumption semantics")
            synthetic_references[key] = ref
        for point in path["points"].values():
            if point["symbol"] == ref["symbol"] and point["action_date"] == ref["action_date"]:
                require(point["reference_observed_at"] == ref["observed_at"]
                        and point["reference_effective_at"] == ref["effective_at"]
                        and point["reference_is_synthetic"] is ref["is_synthetic"],
                        "Planning path changed original observation time or synthetic status")
    if synthetic_references:
        from ml.gameplan_trade_review import _money, _pacific
        readable = (run / "Gameplan.md").read_text(encoding="utf-8")
        require("synthetic zero-volume intervals under an assumed no-trade policy" in readable,
                "Readable review hides the synthetic no-trade assumption")
        for ref in synthetic_references.values():
            disclosure = "| " + " | ".join((ref["symbol"], _money(ref["price"]), _pacific(ref["observed_at"]),
                                               _pacific(ref["effective_at"]), str(ref["fill_count"]))) + " |"
            require(disclosure in readable, "Readable review omits an exact synthetic anchor: " + ref["symbol"])
        summary["coverage_notes"].append({"planning_reference_assumption": "Bounded carry-forward; zero volume assumes no trades and is not an actual exchange observation",
                                         "synthetic_references": synthetic_references, "evidence": artifacts})
    return {"contract_version": completion["contract_version"], "artifacts": artifacts,
            "reference_count": len(completion["references"]), "synthetic_rows": len(synthetic),
            "synthetic_references": synthetic_references, "exact_json_and_parquet_recomputation": True,
            "original_observation_times_preserved": True, "native_minutes_replaced": 0}


def check_actuals():
    if "gameplan_actuals_review" not in context["required_stages"]:
        return {"status": "NOT_REQUIRED_RECORDED_NARROWER_BOUNDARY"}
    run, receipt, manifest, current = pointer_run("ml/gameplan-actuals-review-latest/run.json", "ml/gameplan-actuals-review-runs")
    report = read(run / "report.json")
    prior_date = previous_action_date(context["action_date"])
    require(receipt["status"] == "COMPLETE" and report["status"] == "COMPLETE"
            and receipt["action_date"] == report["action_date"] == prior_date, "Actuals previous-session date/status differs")
    zero_orders(receipt, "Actuals receipt")
    zero_orders(report, "Actuals report")
    pub = context["publication"]
    require(report["successor_action_date"] == context["action_date"]
            and report["successor_gameplan_run"] == pub.run_directory.relative_to(root).as_posix()
            and pd.Timestamp(report["deadline_at"]) == context["deadline"], "Actuals successor source/deadline differs")
    require(pd.Timestamp(read(context["trade_plan"] / "receipt.json")["completed_at"]) <= pd.Timestamp(report["reviewed_at"])
            < context["deadline"], "Actuals review did not follow successor planning within deadline")
    input_matches(manifest, pub.run_directory / "receipt.json")
    input_matches(manifest, context["trade_plan"] / "receipt.json")
    dated = root / "ml/gameplan-actuals-review-by-date" / prior_date
    require(read(dated / "run.json")["current"] == current
            and file_checksum(dated / "Gameplan-results.md") == file_checksum(run / "Gameplan-results.md"),
            "Dated actuals reader differs from immutable review")
    require(Path(context["trade_report"]["previous_session_results_path"]).resolve() == dated / "Gameplan-results.md",
            "Successor trade plan results link differs")
    original = _latest_saved_gameplan(root, prior_date)
    if original is None:
        require(report.get("coverage_status") == "NO_SAVED_INDEPENDENT_GAMEPLAN", "Missing prior plan is not explicit")
        summary["coverage_notes"].append({"actuals": report["coverage_status"], "evidence": str(run / "report.json")})
        return {"run": str(run), "report": report}
    require(report["source_gameplan_run"] == original.run_directory.relative_to(root).as_posix(),
            "Actuals did not select the last verified pre-open prior-session Gameplan")
    original_trades = _saved_trade_plan(root, original)
    forecasts = pd.read_parquet(original.run_directory / "forecasts.parquet")
    frozen_results = pd.read_parquet(run / "forecast-results.parquet")
    price_results = pd.read_parquet(run / "price-results.parquet")
    _match_trade_rows(forecasts, frozen_results)
    require(report["target_price_source_contract"] == original.manifest["configuration"]["target_price_source_contract"],
            "Actuals changed original observed price source")
    require(len(price_results) == 14 * forecasts.symbol.nunique(), "Actuals same-clock price row count differs")
    input_matches(manifest, original.run_directory / "receipt.json")
    input_matches(manifest, original.run_directory / "forecasts.parquet")
    if original_trades:
        require(Path(report["source_trade_plan_path"]).resolve() == original_trades[0], "Actuals source trade plan differs")
        input_matches(manifest, original_trades[0] / "receipt.json")
        if original_trades[2] is not None:
            input_matches(manifest, original_trades[0] / "planning-price-path.json")
    # Mandatory: actual outcomes use only the original verified native archive.
    prices, _, inventory = local_prices(tuple(sorted(forecasts.symbol.unique())), report["target_price_source_contract"])
    require("is_synthetic" not in prices or prices.is_synthetic.fillna(False).eq(False).all(),
            "Actual outcomes received a synthetic observation frame")
    expected_forecasts = compare_forecasts(forecasts, prices, observed_at=report["outcomes_through"],
                                           trade_rows=original_trades[1] if original_trades else None)
    expected_prices = compare_price_points(forecasts, prices, observed_at=report["outcomes_through"], action_date=prior_date,
                                           planning_path=original_trades[2] if original_trades else None)
    pd.testing.assert_frame_equal(expected_forecasts.sort_values("id").reset_index(drop=True),
                                  frozen_results.sort_values("id").reset_index(drop=True), check_dtype=False, check_exact=True)
    pd.testing.assert_frame_equal(expected_prices.sort_values(["symbol", "clock_local"]).reset_index(drop=True),
                                  price_results.sort_values(["symbol", "clock_local"]).reset_index(drop=True), check_dtype=False, check_exact=True)
    recomputed = True
    require(frozen_results.loc[~frozen_results.actuals_status.eq("EVALUATED")
            | ~frozen_results.direction.isin(["BULLISH", "BEARISH"]), "direction_correct"].isna().all(),
            "Neutral, future or missing forecasts entered direction accuracy")
    calls = frozen_results.loc[frozen_results.direction_correct.notna(), "direction_correct"]
    forecast_counts = frozen_results.actuals_status.value_counts().to_dict()
    price_counts = price_results.comparison_status.value_counts().to_dict()
    pending = {"forecast_outcomes": forecast_counts, "same_clock_prices": price_counts}
    if (forecast_counts.get("MATURE_AWAITING_DATA", 0) or forecast_counts.get("PENDING_MATURITY", 0)
            or any(status != "COMPARED" for status in price_counts)):
        summary["coverage_notes"].append({"actuals_coverage": pending, "evidence": str(run / "report.json")})
    return {"run": str(run), "readable_results": str(run / "Gameplan-results.md"), "prior_action_date": prior_date,
            "source_gameplan_run": report["source_gameplan_run"], "forecast_rows": len(frozen_results),
            "price_rows": len(price_results), "coverage": pending, "local_archive_outcomes_recomputed": recomputed,
            "synthetic_prices_used_for_actuals": False, "missing_and_future_actuals_preserved": True,
            "directional_calls_scored": len(calls), "directional_calls_correct": int(calls.sum()),
            "direction_accuracy": float(calls.mean()) if len(calls) else None}


for name, function in (("overnight", check_overnight), ("gameplan", check_gameplan),
        ("opra_coverage", check_opra), ("stock_history", check_stock_history), ("fetch_scope_log", check_fetch_log),
        ("enrichment", check_enrichment), ("trade_plan", check_trade_plan),
        ("planning_prices", check_planning_prices), ("reference_completion", check_reference_completion),
        ("cumulative_evaluation", check_evaluation), ("actuals_review", check_actuals)):
    section(name, function)
summary["status"] = "FAILED" if summary["errors"] else "VERIFIED_WITH_COVERAGE_NOTES" if summary["coverage_notes"] else "VERIFIED"
print(json.dumps(summary, indent=2, default=str, sort_keys=True))
raise SystemExit(1 if summary["errors"] else 0)
