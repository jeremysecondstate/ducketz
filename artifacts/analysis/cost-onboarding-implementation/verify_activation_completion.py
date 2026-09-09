"""Read-only final audit for the registered COST onboarding; run after activation.

Use the repository .venv Python. Only an explicit --output writes an audit file,
and that destination must remain outside DATASTORE. No provider or broker calls.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
EVIDENCE = Path(__file__).resolve().parent
REPOSITORY = EVIDENCE.parents[2]
sys.path.insert(0, str(REPOSITORY))
ROOT = Path(r"C:\DATASTORE").resolve()
PLAN_ID = "987dfa290e0f74a6cbb2962165af8d0cc3f1134edd0a4eef2a9434f7ee2ca221"
ACTION_DATE = "2026-09-08"
DEADLINE = "2026-09-08T11:00:00+00:00"
REQUIRED_THROUGH = "2026-09-05"


def require(condition: object, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"Expected JSON object: {path}")
    return value


def verify() -> dict:
    # Verify production membership even if the caller inherited candidate scope.
    os.environ["DUCKETS_PRODUCTION_WATCHLIST"] = str(REPOSITORY / "datafetching/watchlist.txt")
    import pandas as pd
    from datafetching.symbol_onboarding import load_plan
    from datafetching.symbol_universe import REPOSITORY_WATCHLIST, read_symbols
    from datafetching.options_runtime import _read_opra_symbol_history_cursor
    from ml.artifacts import file_checksum, verify_manifest
    from ml.nightly_gameplan import read_current_gameplan, read_gameplan_run, _verify_opra_history
    from ml.stock_trader.model import load_current_enrichment_model
    from ml.gameplan_evaluation import FIRST_GAMEPLAN_ACTION_DATE, read_evaluation_history
    from ml.directional_forecasts import read_directional_forecast_run, read_current_directional_forecast
    from ml.directional_forecast_evaluation import VERSION as DIRECTIONAL_EVALUATION_VERSION
    from ml.overnight_runtime import OVERNIGHT_RUNTIME_VERSION

    plan_path = EVIDENCE / "plan.json"
    plan = load_plan(plan_path)
    symbols = tuple(plan["candidate_symbols"])
    require(plan["plan_id"] == PLAN_ID and plan["symbol"] == "COST", "Unexpected onboarding plan")
    require(Path(plan["datastore_root"]).resolve() == ROOT, "Unexpected datastore")
    require(len(symbols) == 7 and len(set(symbols)) == 7, "Candidate universe must have seven unique symbols")
    require(read_symbols(REPOSITORY_WATCHLIST) == symbols, "Production watchlist differs from candidate")
    require(read_symbols(EVIDENCE / "candidate-watchlist.txt") == symbols, "Candidate watchlist differs from plan")
    registry = read_json(ROOT / "state/symbol-onboarding/COST.json")
    require(registry.get("plan_id") == PLAN_ID and Path(registry["plan_path"]).resolve() == plan_path,
            "Registered plan does not match")
    require(Path(registry["activation_path"]).resolve() == EVIDENCE / "activation.json",
            "Registered activation path does not match")
    activation = read_json(EVIDENCE / "activation.json")
    require(activation.get("status") == "ACTIVE", "COST onboarding is not ACTIVE")
    for filename, status in (("progress.json", "HISTORY_FETCHED"),
                             ("operational-history.json", "COMPLETE"),
                             ("secondary-history-quality.json", "COMPLETE")):
        evidence = read_json(EVIDENCE / filename)
        require(evidence.get("plan_id") == PLAN_ID and evidence.get("status") == status,
                f"Incomplete prerequisite: {filename}")

    publication = read_current_gameplan(ROOT)
    require(tuple(publication.manifest["configuration"]["symbols"]) == symbols,
            "Current Gameplan universe differs from activated candidate")
    require(publication.receipt["action_date"] == ACTION_DATE, "Original September 8 action date changed")
    require(pd.Timestamp(publication.receipt["published_at"]) <= pd.Timestamp(DEADLINE),
            "Gameplan publication missed the original deadline")
    model = load_current_enrichment_model(ROOT)
    require(all(f"symbol_{symbol}" in model.feature_names for symbol in symbols),
            "Stock model does not cover the activated universe")
    stock_pointer = read_json(ROOT / "ml/stock-trader-model-latest/run.json")
    stock_run = (ROOT / stock_pointer["run_path"]).resolve()
    require(stock_run.name != "20260906T090205.483129Z", "Staged stock preparation cannot satisfy final publication")
    stock_receipt = read_json(stock_run / "receipt.json")
    require(stock_receipt.get("training_report_sha256") == file_checksum(stock_run / "training-report.json"),
            "Stock training report checksum differs")
    stock_report = read_json(stock_run / "training-report.json")
    require(set(stock_report["symbol_row_counts"]) == set(symbols), "Stock report omits candidate training coverage")
    expected_activation = {"plan_id": PLAN_ID, "symbols": list(symbols),
        "gameplan_run": str(publication.run_directory), "action_date": ACTION_DATE,
        "forecasts": 168, "option_intents": 168,
        "stock_model_fingerprint": model.model_fingerprint, "orders_submitted": 0}
    for name, record in (("activation", activation), ("validation", read_json(EVIDENCE / "validation.json"))):
        require(all(record.get(key) == value for key, value in expected_activation.items()),
                f"{name} evidence disagrees with current publications")

    def grid(run: Path, own_symbols: tuple[str, ...]) -> pd.DataFrame:
        expected_counts = {symbol: 24 for symbol in own_symbols}
        frames = []
        for name in ("forecasts.parquet", "option-strategy-intents.parquet"):
            frame = pd.read_parquet(run / name)
            require(frame.groupby("symbol").size().to_dict() == expected_counts,
                    f"Saved universe counts differ: {run.name}/{name}")
            require(frame["id"].notna().all() and frame["id"].is_unique
                    and not frame.duplicated(["symbol", "route"]).any(),
                    f"Duplicate or missing saved identities: {run.name}/{name}")
            require(not frame["broker_orders_enabled"].astype("boolean").fillna(True).any(),
                    f"Broker authority unexpectedly enabled: {run.name}/{name}")
            frames.append(frame)
        require(set(zip(frames[0]["symbol"], frames[0]["route"]))
                == set(zip(frames[1]["symbol"], frames[1]["route"])), "Forecast/intent route sets differ")
        return frames[0]

    current_frame = grid(publication.run_directory, symbols)
    require(len(current_frame) == 168 and current_frame["action_date"].eq(ACTION_DATE).all(),
            "Current forecast grid does not have 168 September 8 rows")
    cursor_files, freshness = _verify_opra_history(ROOT, symbols=symbols,
        action_date=date.fromisoformat(ACTION_DATE), required_completed_through=date.fromisoformat(REQUIRED_THROUGH))
    require(len(cursor_files) == freshness["verified_cursor_count"] == 21, "Expected 21 production OPRA cursors")
    for symbol in symbols:
        for schema in freshness["production_schemas"]:
            cursor = _read_opra_symbol_history_cursor(ROOT, symbol=symbol, schema=schema)
            require(cursor is not None and pd.Timestamp(cursor["completed_through"]) >= pd.Timestamp(REQUIRED_THROUGH),
                    f"Native OPRA cursor invalid or stale: {symbol}/{schema}")
    saved_freshness = publication.manifest["configuration"]["opra_history"]
    require(saved_freshness["verified_cursor_count"] == 21
            and set(saved_freshness["verified_cursors"]) == set(freshness["verified_cursors"])
            and saved_freshness["required_completed_through"] == REQUIRED_THROUGH
            and pd.Timestamp(saved_freshness["completed_through"]) >= pd.Timestamp(REQUIRED_THROUGH),
            "Published OPRA freshness does not attest all 21 required cursors")

    overnight_pointer = read_json(ROOT / "ml/overnight-latest/run.json")
    overnight = (ROOT / overnight_pointer["run_path"]).resolve()
    require(overnight.parent == ROOT / "ml/overnight-runs", "Overnight pointer escapes immutable run root")
    report = read_json(overnight / "stage-report.json")
    receipt = read_json(overnight / "receipt.json")
    require(receipt.get("schema_version") == report.get("schema_version") == OVERNIGHT_RUNTIME_VERSION
            and receipt.get("status") == report.get("status") == "COMPLETE"
            and receipt.get("run_path") == overnight.relative_to(ROOT).as_posix(), "Overnight receipt is not COMPLETE")
    require(receipt["stage_report_checksum_sha256"] == file_checksum(overnight / "stage-report.json")
            and receipt["stage_report_size"] == (overnight / "stage-report.json").stat().st_size,
            "Overnight stage report receipt binding failed")
    require(pd.Timestamp(report["deadline_at"]) == pd.Timestamp(DEADLINE)
            and pd.Timestamp(report["completed_at"]) <= pd.Timestamp(DEADLINE), "Original overnight deadline was not preserved")
    for record in (receipt, report):
        require(record.get("orders_placed") == 0 and record.get("broker_orders_enabled") is False,
                "Overnight order/authority receipt failed")
    completed = set(report.get("completed_stages_from_previous_attempt", []))
    for stage in report["stages"]:
        require(stage["status"] == "COMPLETE" and stage["exit_code"] == 0 and not stage.get("error"),
                f"Overnight stage did not complete: {stage['stage']}")
        completed.add(stage["stage"])
    require({"strategy_profit_training", "strategy_generation", "gameplan_publication"} <= completed,
            "Candidate downstream stages are incomplete")
    for name, binding in receipt["logs"].items():
        log = (overnight / name).resolve()
        require(log.parent == overnight and log.stat().st_size == binding["size"]
                and file_checksum(log) == binding["checksum_sha256"], f"Overnight log binding failed: {name}")
    publication_log = overnight / "gameplan_publication.log"
    require(publication_log.name in receipt["logs"]
            and f"run={publication.run_directory}" in publication_log.read_text(encoding="utf-8"),
            "Overnight receipt does not bind the current Gameplan publication")

    def coverage(frame: pd.DataFrame, expected: dict, observed_at: object, label: str) -> dict:
        require(frame["id"].is_unique and set(frame["id"]) == set(expected), f"{label} evaluation coverage differs")
        require(set(frame["evaluation_status"]) <= {"EVALUATED", "PENDING_MATURITY", "MATURE_AWAITING_DATA"},
                f"{label} has unknown evaluation status")
        now = pd.Timestamp(observed_at)
        for row in frame.to_dict("records"):
            source = expected[row["id"]]
            require(row["source_forecast_id"] == source["id"] and row["symbol"] == source["symbol"]
                    and row["route"] == source["route"] and row["source_gameplan_run"] == source["source_gameplan_run"]
                    and pd.Timestamp(row["target_window_end"]) == pd.Timestamp(source["target_window_end"])
                    and abs(float(row["predicted_probability"]) - float(source["calibrated_probability"])) < 1e-12,
                    f"{label} evaluation identity binding differs: {row['id']}")
            if pd.Timestamp(row["target_window_end"]) > now:
                require(row["evaluation_status"] == "PENDING_MATURITY"
                        and pd.isna(row["observed_target"]) and pd.isna(row["observed_return"]),
                        f"{label} evaluated an unmatured window: {row['id']}")
        return {"forecasts": len(frame), "statuses": frame["evaluation_status"].value_counts().to_dict()}

    expected, saved_runs = {}, []
    for path in sorted((ROOT / "ml/nightly-gameplan-runs").glob("*/receipt.json")):
        saved = read_gameplan_run(ROOT, path.parent)
        own_symbols = tuple(saved.manifest["configuration"]["symbols"])
        forecasts = grid(saved.run_directory, own_symbols)
        saved_runs.append({"run": saved.run_directory.name, "symbols": list(own_symbols), "forecasts": len(forecasts)})
        if saved.receipt["action_date"] < FIRST_GAMEPLAN_ACTION_DATE:
            continue
        relative = saved.run_directory.relative_to(ROOT).as_posix()
        for row in forecasts.to_dict("records"):
            expected[f"{relative}:{row['id']}"] = {**row, "source_gameplan_run": relative}
    evaluation = read_evaluation_history(ROOT)
    require(evaluation is not None, "Gameplan evaluation is missing")
    require(pd.Timestamp(evaluation.summary["observed_at"]) >= pd.Timestamp(publication.receipt["published_at"]),
            "Latest evaluation predates the candidate Gameplan")
    evaluation_result = coverage(evaluation.evaluations, expected, evaluation.summary["observed_at"], "Gameplan")

    supplemental = read_current_directional_forecast(ROOT, "COST")
    require(supplemental is not None and supplemental.receipt["onboarding_plan_id"] == PLAN_ID,
            "Original supplemental COST forecast is missing or belongs to another plan")
    supplemental_expected = {}
    for path in sorted((ROOT / "ml/directional-forecast-runs").glob("*/receipt.json")):
        saved = read_directional_forecast_run(ROOT, path.parent)
        relative = saved.run_directory.relative_to(ROOT).as_posix()
        for row in pd.read_parquet(saved.run_directory / "forecasts.parquet").to_dict("records"):
            supplemental_expected[f"{relative}:{row['id']}"] = {**row, "source_gameplan_run": relative}
    pointer = read_json(ROOT / "ml/directional-forecast-evaluation-latest/run.json")
    directional_run = (ROOT / pointer["current"]["run_path"]).resolve()
    require(pointer.get("schema_version") == DIRECTIONAL_EVALUATION_VERSION
            and directional_run.parent == ROOT / "ml/directional-forecast-evaluation-runs", "Supplemental evaluation pointer invalid")
    manifest = verify_manifest(directional_run)
    directional_receipt = read_json(directional_run / "receipt.json")
    require(pointer["current"]["receipt_checksum_sha256"] == file_checksum(directional_run / "receipt.json")
            and directional_receipt.get("schema_version") == DIRECTIONAL_EVALUATION_VERSION
            and directional_receipt["manifest_checksum_sha256"] == file_checksum(directional_run / "manifest.json")
            and manifest["configuration"]["schema_version"] == DIRECTIONAL_EVALUATION_VERSION,
            "Supplemental evaluation receipt/manifest binding failed")
    summary = read_json(directional_run / "summary.json")
    require(summary.get("orders_placed") == 0 and summary.get("broker_orders_enabled") is False,
            "Supplemental evaluation authority changed")
    directional_frame = pd.read_parquet(directional_run / "evaluations.parquet")
    directional_result = coverage(directional_frame, supplemental_expected, summary["evaluated_at"], "Supplemental")
    cost_pending = directional_frame.loc[directional_frame["symbol"].eq("COST")]
    require(len(cost_pending) == 24 and cost_pending["evaluation_status"].eq("PENDING_MATURITY").all(),
            "Original 24 supplemental COST forecasts must remain pending before September 8")

    return {"status": "VERIFIED_ACTIVE", "verified_at": datetime.now(timezone.utc).isoformat(),
        "plan_id": PLAN_ID, "symbols": list(symbols), "action_date": ACTION_DATE, "deadline_at": DEADLINE,
        "gameplan_run": str(publication.run_directory), "forecasts": 168, "option_intents": 168,
        "stock_model_run": str(stock_run), "stock_model_fingerprint": model.model_fingerprint,
        "stock_training_rows": stock_report["row_count"], "opra_cursors": 21,
        "opra_completed_through": freshness["completed_through"], "overnight_run": str(overnight),
        "overnight_log_bindings": len(receipt["logs"]), "orders_placed": 0,
        "saved_gameplans": saved_runs, "evaluation_run": str(evaluation.run_directory),
        "evaluation": evaluation_result, "supplemental_evaluation_run": str(directional_run),
        "supplemental_evaluation": directional_result}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional JSON audit evidence outside DATASTORE")
    args = parser.parse_args()
    if args.output and (args.output.resolve() == ROOT or ROOT in args.output.resolve().parents):
        parser.error("--output must be outside DATASTORE")
    try:
        result, code = verify(), 0
    except Exception as exc:
        result, code = {"status": "VERIFICATION_FAILED", "verified_at": datetime.now(timezone.utc).isoformat(),
                        "error": f"{type(exc).__name__}: {exc}"}, 1
    text = json.dumps(result, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
