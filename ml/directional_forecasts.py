"""Freeze onboarding forecasts independently of the options Gameplan.

Use the ordinary horizon targets, chronological fitting and promotion gates,
with options-derived features excluded. These artifacts are display/evaluation
only and cannot advance the production Gameplan or activate a symbol.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import pandas as pd

from datafetching.runtime_lock import exclusive_runtime_lock
from datafetching.symbol_onboarding import load_plan
from datafetching.symbol_universe import normalize_symbol
from ml import nightly_gameplan as nightly
from ml.artifacts import create_timestamp_directory, file_checksum, utc_timestamp, verify_manifest, write_manifest
from ml.current_publication import read_current_publication

VERSION = "read-only-directional-forecasts-v1"
AUTHORITY = "READ_ONLY_FORECAST"
RUN_ROOT = Path("ml/directional-forecast-runs")
POINTER_ROOT = Path("ml/directional-forecast-latest")
OPTION_PREFIXES = ("opt__", "opx__")


@dataclass(frozen=True)
class DirectionalPublication:
    run_directory: Path
    manifest: Mapping[str, object]
    receipt: Mapping[str, object]


def read_directional_forecast_run(root: Path, run: Path) -> DirectionalPublication:
    root, run = Path(root).resolve(), Path(run).resolve()
    if run.parent != root / RUN_ROOT:
        raise RuntimeError("Directional forecast path escapes its immutable run root")
    manifest = verify_manifest(run)
    receipt = json.loads((run / "receipt.json").read_text(encoding="utf-8"))
    config = manifest.get("configuration", {})
    features = manifest.get("feature_columns", ())
    expected_outputs = {"forecasts.parquet", "model-reports.json", "forecast.json",
                        *(f"models/{group}/model.joblib" for group in nightly.MODEL_GROUPS)}
    if (
        not isinstance(receipt, dict) or not isinstance(config, dict)
        or receipt.get("schema_version") != VERSION or config.get("schema_version") != VERSION
        or receipt.get("run_path") != run.relative_to(root).as_posix()
        or receipt.get("manifest_checksum_sha256") != file_checksum(run / "manifest.json")
        or receipt.get("run_timestamp") != manifest.get("run_timestamp")
        or any(receipt.get(key) != config.get(key) for key in
               ("symbol", "action_date", "onboarding_plan_id", "execution_authority", "broker_orders_enabled", "orders_placed"))
        or config.get("execution_authority") != AUTHORITY
        or config.get("broker_orders_enabled") is not False or config.get("orders_placed") != 0
        or config.get("feature_scope") != "NON_OPTIONS" or not features
        or any(str(feature).startswith(OPTION_PREFIXES) for feature in features)
        or set(manifest.get("output_files", {})) != expected_outputs
    ):
        raise RuntimeError("Directional forecast manifest and receipt disagree")
    payload = json.loads((run / "forecast.json").read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or any(payload.get(key) != value for key, value in config.items()):
        raise RuntimeError("Directional forecast metadata disagrees with its manifest")
    symbol = normalize_symbol(config["symbol"])
    frame = pd.read_parquet(run / "forecasts.parquet")
    required = {"id", "symbol", "route", "model_group", "calibrated_probability",
                "action_date", "execution_authority", "broker_orders_enabled", "option_feature_count"}
    if not required.issubset(frame.columns):
        raise RuntimeError("Directional forecast fields are incomplete")
    probabilities = pd.to_numeric(frame["calibrated_probability"], errors="coerce")
    if (
        len(frame) != nightly.EXPECTED_FORECASTS_PER_SYMBOL or frame["id"].duplicated().any()
        or frame["route"].duplicated().any() or set(frame["symbol"]) != {symbol}
        or frame["model_group"].value_counts().to_dict() != {"1h": 14, "4h": 4, "1d": 5, "1w": 1}
        or not probabilities.between(0, 1).all()
        or not frame["execution_authority"].eq(AUTHORITY).all()
        or frame["broker_orders_enabled"].astype("boolean").fillna(True).any()
        or not frame["option_feature_count"].eq(0).all()
        or not frame["action_date"].eq(config["action_date"]).all()
        or not frame["id"].str.startswith(f"directional:{run.name}:").all()
    ):
        raise RuntimeError("Directional forecast grid or authority is invalid")
    return DirectionalPublication(run, manifest, receipt)


def read_current_directional_forecast(root: Path, symbol: str) -> DirectionalPublication | None:
    root = Path(root).resolve()
    symbol = normalize_symbol(symbol)
    path = root / POINTER_ROOT / f"{symbol}.json"
    if not path.is_file():
        return None
    pointer = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(pointer, dict) or pointer.get("schema_version") != VERSION:
        raise RuntimeError("Directional forecast pointer is invalid")
    current = pointer.get("current", {})
    if not isinstance(current, dict) or not current.get("run_path"):
        raise RuntimeError("Directional forecast pointer has no run")
    publication = read_directional_forecast_run(root, root / current["run_path"])
    if (
        publication.receipt["symbol"] != symbol
        or current != {"run_path": publication.run_directory.relative_to(root).as_posix(),
                       "receipt_checksum_sha256": file_checksum(publication.run_directory / "receipt.json")}
    ):
        raise RuntimeError("Directional forecast pointer and receipt disagree")
    return publication


def _publish(root: Path, run: Path, *, config: dict, inputs: tuple[Path, ...],
             features: tuple[str, ...], created: pd.Timestamp) -> DirectionalPublication:
    nightly._write_json_atomic(run / "forecast.json", config)
    write_manifest(run, run_timestamp=created, input_files=inputs,
                   output_files=("forecasts.parquet", "model-reports.json", "forecast.json",
                                 *(f"models/{group}/model.joblib" for group in nightly.MODEL_GROUPS)),
                   feature_columns=features, model_name="directional-non-options-hgb-mlp",
                   target_column="target_cost_adjusted_positive", configuration=config, datastore_root=root)
    finished = utc_timestamp()
    if finished >= nightly._local_timestamp(pd.Timestamp(config["action_date"]).date(), nightly.ACTION_START_HOUR):
        raise RuntimeError("Directional forecast missed the 04:00 PT publication boundary")
    nightly._write_json_atomic(run / "receipt.json", {
        **{key: config[key] for key in ("schema_version", "symbol", "action_date", "onboarding_plan_id",
                                       "execution_authority", "broker_orders_enabled", "orders_placed")},
        "run_path": run.relative_to(root).as_posix(), "run_timestamp": created.isoformat(),
        "published_at": finished.isoformat(), "manifest_checksum_sha256": file_checksum(run / "manifest.json"),
    })
    publication = read_directional_forecast_run(root, run)
    nightly._write_json_atomic(root / POINTER_ROOT / f"{config['symbol']}.json", {
        "schema_version": VERSION,
        "current": {"run_path": run.relative_to(root).as_posix(),
                    "receipt_checksum_sha256": file_checksum(run / "receipt.json")},
    })
    return publication


def run_directional_forecasts_once(plan_path: Path, *, reporter=print) -> DirectionalPublication:
    plan_path = Path(plan_path).resolve()
    plan = load_plan(plan_path)
    root, symbol = Path(plan["datastore_root"]).resolve(), normalize_symbol(plan["symbol"])
    registry_path = root / "state/symbol-onboarding" / f"{symbol}.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    if registry.get("plan_id") != plan["plan_id"] or Path(registry["plan_path"]).resolve() != plan_path:
        raise RuntimeError("Directional forecasts require the registered onboarding plan")
    created = utc_timestamp()
    source = read_current_publication(root)
    samples_path = source.run_directory / "samples.parquet"
    samples = pd.read_parquet(samples_path)
    training_symbols = nightly._configured_symbols(source.manifest, samples)
    if symbol not in training_symbols:
        raise RuntimeError("The registered candidate is absent from the completed directional preparation")
    all_features = nightly._feature_columns(source.manifest, samples)
    features = tuple(feature for feature in all_features if not feature.startswith(OPTION_PREFIXES))
    if not features:
        raise RuntimeError("No non-options model features are available")
    sources = nightly._overnight_sources(samples, symbols=training_symbols, available_at=created)
    current_sources, action_date = nightly._current_overnight_sources(
        sources.loc[sources["symbol"].eq(symbol)], symbols=(symbol,), as_of=created)
    action_start = nightly._local_timestamp(action_date, nightly.ACTION_START_HOUR)
    if created >= action_start:
        raise RuntimeError("New directional forecasts cannot be frozen after the 04:00 PT action boundary")
    current_sources = current_sources.loc[current_sources["symbol"].eq(symbol)]
    bars, bar_files = nightly._load_equity_minute_bars(root, symbols=training_symbols)
    groups = nightly._build_training_groups(samples, sources=sources, feature_columns=features, minute_bars=bars)
    current = nightly._build_current_groups(samples, current_sources=current_sources, action_date=action_date,
        as_of=created, symbols=(symbol,), feature_columns=features)
    run = create_timestamp_directory(root / RUN_ROOT, timestamp=created)
    frames, reports = [], {}
    for group in nightly.MODEL_GROUPS:
        reporter(f"Directional forecast: fitting {symbol} {group}, non-options features={len(features)}")
        trained = nightly._fit_group_model(groups[group], current=current[group], feature_columns=features,
            group=group, model_directory=run / "models" / group, trained_at=created)
        frames.append(trained["forecasts"])
        reports[group] = trained["report"]
        reporter(f"Directional forecast: {group} status={trained['report']['promotion_gate']['status']}")
    forecasts = nightly._finalize_forecasts(pd.concat(frames, ignore_index=True), symbols=(symbol,),
        action_date=action_date, frozen_at=created, action_start=action_start,
        action_end=nightly._local_timestamp(action_date, nightly.ACTION_END_HOUR), opra_freshness=None)
    forecasts["id"] = f"directional:{run.name}:" + forecasts["id"].astype(str)
    forecasts["execution_authority"] = AUTHORITY
    forecasts = forecasts.drop(columns="opra_completed_through")
    forecasts.to_parquet(run / "forecasts.parquet", index=False)
    nightly._write_json_atomic(run / "model-reports.json", reports)
    config = {
        "schema_version": VERSION, "symbol": symbol, "symbols": [symbol],
        "action_date": action_date.isoformat(), "onboarding_plan_id": plan["plan_id"],
        "execution_authority": AUTHORITY, "broker_orders_enabled": False, "orders_placed": 0,
        "feature_scope": "NON_OPTIONS", "excluded_features": [f for f in all_features if f not in features],
        "training_symbols": list(training_symbols), "source_loop_b_run": source.run_directory.relative_to(root).as_posix(),
        "forecast_contract_version": nightly.FORECAST_CONTRACT_VERSION,
        "target_contract_version": nightly.TARGET_CONTRACT_VERSION,
        "options_status": "PENDING_OPRA_HISTORY",
        "limitations": ["Stock-direction forecasts use no options-derived features.",
                        "Options Gameplan and production activation remain pending.",
                        "Offline promotion status is reported separately for each horizon."],
    }
    publication = _publish(root, run, config=config, inputs=(plan_path, registry_path, samples_path,
        source.run_directory / "manifest.json", source.run_directory / "publication.json", *bar_files),
        features=features, created=created)
    from ml.directional_forecast_evaluation import evaluate_saved_directional_forecasts
    evaluation_run = evaluate_saved_directional_forecasts(root, observed_groups=groups,
        evaluated_at=created, input_files=(samples_path, *bar_files))
    reporter(f"Directional evaluation saved: {evaluation_run}")
    reporter(f"Directional forecasts published: {symbol}, rows={len(forecasts)}, run={run}")
    return publication


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args(argv)
    plan = load_plan(args.plan)
    root = Path(plan["datastore_root"])
    with exclusive_runtime_lock(root / ".ducketz-directional-forecasts.lock", process_name="Directional forecasts"):
        run_directional_forecasts_once(args.plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
