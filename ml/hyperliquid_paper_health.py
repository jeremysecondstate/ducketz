"""Bounded read-only workspace/process health; write only a new labeled report."""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from app.services.hyperliquid_powder_view import HyperliquidWorkspaceViewService
import psutil


def configured_slot_health(root, project, snapshot, now):
    """Bind a health claim to configured candle/horizon slots and usable forecasts.

    The UI's freshness projection alone cannot certify a frequency transition:
    old 15m sources can still be fresh just after switching the config to 5m.
    These reads use the same strict forecast reader as Paper, including target
    maturity, model identity, qualification provenance and pinned feature rows.
    """
    from datafetching.hyperliquid_candles import INTERVAL_MS
    from ml.hyperliquid_model_config import load_config as load_models
    from ml.hyperliquid_paper_policy import load_config as load_paper
    from ml.hyperliquid_forecast_reader import read_forecast

    report = {"result": "failed", "errors": [], "slots": {}}
    try:
        paper = load_paper(project / "configs/hyperliquid-paper.json")
        models = load_models(paper.model_config)
        markets = models.load_markets()
        if paper.data_root != root or markets.output_root != root:
            raise ValueError("Configured Paper/model datastore differs from the health target")
        interval, primary = markets.interval, models.horizons_bars[0]
        expected_data = {f"{coin}/{interval}" for coin in markets.symbols}
        expected_models = {f"{coin}/{interval}/h{horizon}" for coin in markets.symbols
                           for horizon in models.horizons_bars}
        report["configured_recipe"] = {
            "interval": interval, "horizons_bars": list(models.horizons_bars),
            "paper_horizon_bars": primary,
            "paper_horizon_minutes": INTERVAL_MS[interval] * primary / 60000,
            "symbols": list(markets.symbols), "model_config": str(paper.model_config),
            "markets_config": str(models.markets_config),
        }
        coordinator = snapshot.runtime.get("coordinator", {})
        model_runtime = snapshot.runtime.get("models", {})
        if coordinator.get("interval") != interval or set(coordinator.get("symbols", [])) != set(markets.symbols):
            report["errors"].append("Coordinator interval/symbols differ from configured recipe")
        if set(coordinator.get("markets", {})) != expected_data:
            report["errors"].append("Coordinator market slots differ from configured recipe")
        if model_runtime.get("horizons_bars") != list(models.horizons_bars):
            report["errors"].append("Model runtime horizons differ from configured recipe")
        if set(model_runtime.get("markets", {})) != expected_models:
            report["errors"].append("Model runtime market/horizon slots differ from configured recipe")
        if snapshot.runtime.get("horizon_bars") != primary:
            report["errors"].append("Paper runtime horizon differs from configured recipe")
        view_recipe = getattr(snapshot, "market_recipe", {})
        if (view_recipe.get("interval") != interval or view_recipe.get("horizon_bars") != primary
                or set(view_recipe.get("symbols", [])) != set(markets.symbols)):
            report["errors"].append("Workspace market recipe differs from configured recipe")
        observed_view = [(row.get("coin"), row.get("interval"), row.get("horizon_bars"))
                         for row in snapshot.forecasts]
        expected_view = {(coin, interval, primary) for coin in markets.symbols}
        if len(observed_view) != len(expected_view) or set(observed_view) != expected_view:
            report["errors"].append("Workspace forecast coverage differs from configured Paper slots")
        for coin in markets.symbols:
            for horizon in models.horizons_bars:
                key = f"{coin}/{interval}/h{horizon}"
                try:
                    prediction, sigma = read_forecast(coin, now, paper, interval, horizon)
                    run = root / "_models" / coin / interval / f"h{horizon}" / "runs" / prediction["model_id"]
                    record = json.loads((run / "record.json").read_text(encoding="utf-8"))
                    if record.get("settings") != asdict(models.model_settings(horizon)):
                        raise ValueError("Forecast model recipe differs from configured settings")
                    report["slots"][key] = {"result": "passed", **{
                        field: prediction[field] for field in ("prediction_id", "model_id", "data_run_id",
                            "decision_close_utc", "target_close_utc", "created_at_utc", "qualified")},
                        "valid_until_epoch": prediction["_valid_until_epoch"], "horizon_sigma": sigma}
                except (OSError, ValueError, TypeError, KeyError) as error:
                    detail = type(error).__name__ + ": " + str(error)
                    report["slots"][key] = {"result": "failed", "error": detail}
                    report["errors"].append(key + ": " + detail)
    except (OSError, ValueError, TypeError, KeyError, IndexError) as error:
        report["errors"].append(type(error).__name__ + ": " + str(error))
    report["result"] = "failed" if report["errors"] else "passed"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--opening-report", type=Path)
    parser.add_argument("--previous-health", type=Path)
    parser.add_argument("--expect", choices=("prepared", "trading", "observational"), default="observational")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path("C:/DATASTORE/hyperliquid"))
    args = parser.parse_args()
    assert re.fullmatch(r"[a-zA-Z0-9_-]+", args.label)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir.resolve() / (args.label + ".json")
    assert not output.exists(), f"Evidence already exists: {output}"
    root = args.root.resolve(strict=True)
    snapshot = HyperliquidWorkspaceViewService(root, history_limit=8, journal_limit=5).load_snapshot()
    def local_report(path):
        return path if path.is_absolute() else args.output_dir.resolve() / path
    def optional_json(path):
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    runtime_keys = ("status", "state", "reported_status", "pid", "config_path", "process_alive",
                    "updated_at_utc", "reason", "last_error", "config_error", "errors", "quote_errors",
                    "funding_errors", "training_market", "pending_intents", "connected", "lifecycle_phase",
                    "prepare_only", "stop_reason")
    def runtime(row):
        value = {key: row[key] for key in runtime_keys if key in row}
        value["slot_errors"] = {name: {key: slot[key] for key in ("last_error", "last_prediction_error") if slot.get(key)}
                                for name, slot in row.get("markets", {}).items() if isinstance(slot, dict)
                                and (slot.get("last_error") or slot.get("last_prediction_error"))}
        return value
    runtimes = {"paper": runtime(snapshot.runtime), "models": runtime(snapshot.runtime.get("models", {})),
                "coordinator": runtime(snapshot.runtime.get("coordinator", {})), "powder": runtime(snapshot.powder.runtime)}
    modules = {"paper": "ml.hyperliquid_paper_runtime", "models": "ml.hyperliquid_model_runtime",
               "coordinator": "ml.hyperliquid_coordinator", "powder": "ml.hyperliquid_powder_runtime"}
    identities = {}
    for component, row in runtimes.items():
        pid = row.get("pid")
        if not pid:
            identities[component] = {"pid": None, "identity_verified": False}
            continue
        try:
            process = psutil.Process(pid)
            argv = process.cmdline()
            index = argv.index("-m") if "-m" in argv else -1
            valid = index >= 0 and argv[index + 1] == modules[component]
            if not valid:
                raise ValueError("Saved PID belongs to a different module; unrelated arguments omitted")
            config_path = str(PROJECT / "configs" / {"paper": "hyperliquid-paper.json", "models": "hyperliquid-models.json",
                                                   "coordinator": "hyperliquid-markets.json", "powder": "hyperliquid-powder.json"}[component])
            config_index = argv.index("--config") if "--config" in argv else -1
            valid &= config_index >= 0 and Path(argv[config_index + 1]).resolve() == Path(config_path).resolve()
            valid &= Path(process.cwd()).resolve() == PROJECT.resolve()
            parents = []
            for parent in process.parents()[:2]:
                parents.append({"pid": parent.pid, "name": parent.name(), "command": parent.cmdline(),
                                "created_at_utc": datetime.fromtimestamp(parent.create_time(), timezone.utc).isoformat()})
            identities[component] = {"pid": pid, "identity_verified": bool(valid), "command": argv, "cwd": process.cwd(),
                                     "created_at_utc": datetime.fromtimestamp(process.create_time(), timezone.utc).isoformat(), "parents": parents}
        except (psutil.Error, OSError, ValueError, IndexError) as error:
            identities[component] = {"pid": pid, "identity_verified": False, "error": type(error).__name__ + ": " + str(error)}
    controls = {"coordinator": "_coordinator", "models": "_models/_runtime", "paper": "_paper/_runtime", "powder": "_powder/_runtime"}
    record = {"read_at_utc": snapshot.observed_at_utc, "expectation": args.expect,
              "config_sha256": {name: hashlib.sha256((PROJECT / "configs" / name).read_bytes()).hexdigest()
                                for name in ("hyperliquid-markets.json", "hyperliquid-models.json", "hyperliquid-paper.json", "hyperliquid-powder.json")},
              "portfolio_at_utc": snapshot.portfolio_observed_at_utc, "paper_seed_at_utc": snapshot.seed.get("timestamp_utc"),
              "paper_baseline_equity": snapshot.seed.get("baseline_equity"), "paper_pooled": snapshot.pooled,
              "experiment": optional_json(root / "_paper/experiment.json"),
              "paper_policy": optional_json(root / "_paper/policy.json"),
              "maintenance": optional_json(root / "_operations/paper-maintenance.json"),
              "runtime": runtimes, "process_identity": identities,
              "stop_requests": {name: (root / path / "stop.request").exists() for name, path in controls.items()},
              "sources": {name: asdict(source) for name, source in snapshot.sources.items()},
              "powder_sources": {name: asdict(source) for name, source in snapshot.powder.sources.items() if name.startswith("powder")},
              "warnings": list(snapshot.warnings) + list(snapshot.powder.warnings)}
    record["configured_slots"] = configured_slot_health(
        root, PROJECT, snapshot, datetime.now(timezone.utc).timestamp())
    if args.opening_report:
        prior = json.loads(local_report(args.opening_report).read_text())
        assert prior["stage"] == "opening" and prior["result"] == "passed"
        assert record["paper_seed_at_utc"] == prior["seed_at_utc"]
        assert abs(sum(record["paper_baseline_equity"].values()) - prior["opening_equity"]) < 1e-7
    if args.expect in {"prepared", "trading"}:
        assert record["configured_slots"]["result"] == "passed", record["configured_slots"]
        assert not record["warnings"], record["warnings"]
        assert not any(record["stop_requests"].values()), "Pending stop intent"
        for name in ("coordinator", "models"):
            assert identities[name]["identity_verified"]
            assert not any(runtimes[name].get(key) for key in ("last_error", "config_error", "slot_errors")), runtimes[name]
        required_sources = {key: value for key, value in record["sources"].items()
                            if args.expect == "trading" or key not in {"ledger", "paper", "performance"}}
        assert all(value["state"] == "fresh" for value in required_sources.values()), {
            key: value for key, value in required_sources.items() if value["state"] != "fresh"}
        assert not snapshot.powder.runtime.get("connected") and snapshot.powder.runtime.get("pending_intents", 0) == 0
        if args.expect == "prepared":
            assert snapshot.runtime.get("prepare_only") is True
            assert snapshot.runtime.get("lifecycle_phase") == "opening_prepared"
            assert snapshot.pooled["fees"] == snapshot.pooled["total_pnl"] == 0
        else:
            assert identities["paper"]["identity_verified"]
            assert snapshot.runtime.get("lifecycle_phase") == "trading"
            assert snapshot.runtime.get("process_alive") is True
            assert not any(snapshot.runtime.get(key) for key in ("last_error", "errors", "quote_errors", "funding_errors"))
            if args.previous_health:
                prior = json.loads(local_report(args.previous_health).read_text())
                assert record["portfolio_at_utc"] > prior["portfolio_at_utc"], "Committed observation did not advance"
                assert record["paper_seed_at_utc"] == prior["paper_seed_at_utc"]
    record["result"] = "observed" if args.expect == "observational" else "passed"
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(record, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"result": record["result"], "report": str(output), "read_at_utc": record["read_at_utc"],
                      "portfolio_at_utc": record["portfolio_at_utc"], "seed_at_utc": record["paper_seed_at_utc"],
                      "process_identity": {key: value["identity_verified"] for key, value in identities.items()},
                      "warnings": record["warnings"]}, indent=2))


if __name__ == "__main__":
    main()
