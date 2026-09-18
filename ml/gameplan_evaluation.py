"""Evaluate saved Gameplans without training a model or contacting a broker."""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.artifacts import create_timestamp_directory, file_checksum, utc_timestamp, verify_manifest, write_manifest
from ml.gameplan_probability_target import (
    observed_probability_target, probability_target_contract, probability_target_metadata,
)

FIRST_GAMEPLAN_ACTION_DATE = "2026-09-04"
EVALUATION_VERSION = "saved-gameplan-evaluations-v1"
EVALUATION_COLUMNS = (
    "id", "source_gameplan_run", "source_forecast_id", "action_date", "symbol",
    "model_group", "model_status", "route", "target_window_start", "target_window_end",
    "predicted_probability", "observed_target", "observed_return", "brier_score",
    "direction_correct", "evaluation_status", "evaluated_at",
    "probability_target_contract", "gameplan_variant", "direction", "raw_direction_correct",
    "assumed_round_trip_cost", "observed_cost_adjusted_positive", "cost_adjusted_return",
    "target_price_source_contract", "target_price_dataset",
)


@dataclass(frozen=True)
class GameplanEvaluationResult:
    run_directory: Path
    evaluations: pd.DataFrame
    summary: Mapping[str, object]


def read_evaluation_history(root: Path) -> GameplanEvaluationResult | None:
    root = Path(root).resolve()
    pointer = root / "ml/gameplan-evaluation-latest/run.json"
    if not pointer.is_file():
        return None
    payload = json.loads(pointer.read_text(encoding="utf-8"))
    if payload.get("schema_version") != EVALUATION_VERSION:
        raise RuntimeError("Gameplan evaluation pointer has an unsupported version")
    current = payload["current"]
    run = (root / current["run_path"]).resolve()
    if run.parent != root / "ml/gameplan-evaluation-runs":
        raise RuntimeError("Gameplan evaluation pointer escapes its run directory")
    manifest = verify_manifest(run)
    receipt = json.loads((run / "receipt.json").read_text(encoding="utf-8"))
    if (
        current.get("receipt_checksum_sha256") != file_checksum(run / "receipt.json")
        or receipt.get("schema_version") != EVALUATION_VERSION
        or receipt.get("run_path") != run.relative_to(root).as_posix()
        or receipt.get("manifest_checksum_sha256") != file_checksum(run / "manifest.json")
        or receipt.get("orders_placed") != 0
        or receipt.get("broker_orders_enabled") is not False
        or manifest.get("configuration", {}).get("first_action_date") != FIRST_GAMEPLAN_ACTION_DATE
    ):
        raise RuntimeError("Gameplan evaluation receipt and pointer disagree")
    evaluations = pd.read_parquet(run / "evaluations.parquet")
    for row in evaluations.to_dict("records"):
        metadata = probability_target_metadata(probability_target_contract(row))
        if pd.notna(row.get("gameplan_variant")) and row["gameplan_variant"] != metadata["gameplan_variant"]:
            raise RuntimeError("Saved evaluation variant disagrees with its probability target")
    return GameplanEvaluationResult(
        run, evaluations,
        json.loads((run / "summary.json").read_text(encoding="utf-8")),
    )


def _saved_forecasts(root: Path) -> tuple[pd.DataFrame, tuple[Path, ...]]:
    # Import lazily so the nightly publisher can use this evaluator too.
    from ml.nightly_gameplan import read_gameplan_run

    frames: list[pd.DataFrame] = []
    files: list[Path] = []
    for receipt_path in sorted((root / "ml/nightly-gameplan-runs").glob("*/receipt.json")):
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        action_date = str(receipt.get("action_date") or "")
        if not action_date:
            raise RuntimeError(f"Saved Gameplan has no action date: {receipt_path}")
        if action_date < FIRST_GAMEPLAN_ACTION_DATE:
            continue
        publication = read_gameplan_run(root, receipt_path.parent)
        frame = pd.read_parquet(publication.run_directory / "forecasts.parquet")
        required = {"id", "symbol", "route", "target_window_start", "target_window_end", "calibrated_probability"}
        if not required.issubset(frame.columns) or frame["id"].duplicated().any():
            raise RuntimeError(f"Saved Gameplan forecast identities are invalid: {receipt_path}")
        frame["source_gameplan_run"] = publication.run_directory.relative_to(root).as_posix()
        frame["action_date"] = action_date
        frames.append(frame)
        files.extend((receipt_path, receipt_path.parent / "manifest.json", receipt_path.parent / "forecasts.parquet"))
    return (pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()), tuple(files)


def evaluate_forecasts(
    forecasts: pd.DataFrame, *, observed_groups: Mapping[str, pd.DataFrame],
    evaluated_at: object, previous: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Keep one durable row per saved forecast; score it once actual data exists."""
    now = utc_timestamp(evaluated_at)
    previous = pd.DataFrame() if previous is None else previous
    prior = {str(row["id"]): row for row in previous.to_dict("records")}
    from ml.independent_stock_targets import STOCK_TARGET_CONTRACT_VERSION

    def target_family(row):
        # Legacy v1/v2 keep their historical endpoint-based matching. Independent
        # stock labels may never supply outcomes for those earlier contracts.
        if str(row.get("target_contract_version")) == STOCK_TARGET_CONTRACT_VERSION:
            from ml.stock_target_prices import independent_price_identity
            selection = row.get("source_selection_contract")
            selection = None if pd.isna(selection) else str(selection)
            return (STOCK_TARGET_CONTRACT_VERSION, *independent_price_identity(row), selection)
        return "legacy"
    observed_frames = [frame for frame in observed_groups.values() if not frame.empty]
    lookup: dict[tuple, Mapping[str, object]] = {}
    for frame in observed_frames:
        for row in frame.to_dict("records"):
            probability_target_contract(row)  # Unknown explicit contracts never become outcome evidence.
            start, end = utc_timestamp(row["target_window_start"]), utc_timestamp(row["target_window_end"])
            key = (target_family(row), str(row["symbol"]).upper(), str(row["route"]), start, end)
            target, change = row.get("target"), row.get("observed_return")
            if pd.notna(target) and target in (0, 1) and pd.notna(change) and np.isfinite(float(change)):
                lookup[key] = row
    rows: list[dict[str, object]] = []
    for forecast in forecasts.to_dict("records"):
        start, end = utc_timestamp(forecast["target_window_start"]), utc_timestamp(forecast["target_window_end"])
        probability = float(forecast["calibrated_probability"])
        if not np.isfinite(probability) or not 0 <= probability <= 1:
            raise RuntimeError("Saved Gameplan contains an invalid probability")
        identity = f"{forecast['source_gameplan_run']}:{forecast['id']}"
        contract = probability_target_contract(forecast)
        metadata = probability_target_metadata(contract)
        variant = forecast.get("gameplan_variant")
        if pd.notna(variant) and variant != metadata["gameplan_variant"]:
            raise RuntimeError("Saved Gameplan variant disagrees with its probability target")
        cost = forecast.get("assumed_round_trip_cost", .001)
        cost = .001 if pd.isna(cost) else float(cost)
        observed_probability_target(0., cost, contract)  # Validate costs even for pending forecasts.
        direction = forecast.get("direction")
        if pd.isna(direction):
            direction = "BULLISH" if probability > .5 else "BEARISH" if probability < .5 else "NO_EDGE"
        if direction not in {"BULLISH", "BEARISH", "NO_EDGE"}:
            raise RuntimeError("Saved Gameplan contains an invalid direction")

        def evidence(change):
            known = change is not None and pd.notna(change)
            return {**metadata, "direction": direction, "assumed_round_trip_cost": cost,
                    "raw_direction_correct": (bool(change > 0) if direction == "BULLISH" else bool(change < 0))
                    if known and direction != "NO_EDGE" else None,
                    "observed_cost_adjusted_positive": int(change > cost) if known else None,
                    "cost_adjusted_return": float(change) - cost if known else None,
                    "target_price_source_contract": forecast.get("target_price_source_contract", "legacy"),
                    "target_price_dataset": forecast.get("target_price_dataset", "legacy")}

        old = prior.get(identity)
        if old is not None and old["evaluation_status"] == "EVALUATED":
            if (float(old["predicted_probability"]) != probability or utc_timestamp(old["target_window_end"]) != end
                    or probability_target_contract(old) != contract
                    or (pd.notna(old.get("assumed_round_trip_cost")) and float(old["assumed_round_trip_cost"]) != cost)
                    or (pd.notna(old.get("direction")) and old["direction"] != direction)):
                raise RuntimeError("An evaluated immutable forecast changed")
            # Retain the original scored target and errors. New descriptive
            # columns expose the same saved observation without relabeling OG.
            rows.append({**old, **evidence(old.get("observed_return"))})
            continue
        outcome = lookup.get((target_family(forecast), str(forecast["symbol"]).upper(), str(forecast["route"]), start, end))
        matured = end <= now
        scored = matured and outcome is not None
        change = float(outcome["observed_return"]) if scored else None
        target = observed_probability_target(change, cost, contract) if scored else None
        rows.append({
            "id": identity, "source_gameplan_run": forecast["source_gameplan_run"],
            "source_forecast_id": forecast["id"], "action_date": forecast["action_date"],
            "symbol": forecast["symbol"], "model_group": forecast.get("model_group", ""),
            "model_status": forecast.get("model_status", ""), "route": forecast["route"],
            "target_window_start": start, "target_window_end": end,
            "predicted_probability": probability, "observed_target": target,
            "observed_return": change,
            "brier_score": (probability - target) ** 2 if scored else None,
            "direction_correct": bool((probability >= 0.5) == bool(target)) if scored else None,
            "evaluation_status": "EVALUATED" if scored else "MATURE_AWAITING_DATA" if matured else "PENDING_MATURITY",
            "evaluated_at": now if scored else pd.NaT,
            **evidence(change),
        })
    frame = pd.DataFrame(rows, columns=EVALUATION_COLUMNS)
    if len(frame) and frame["id"].duplicated().any():
        raise RuntimeError("Saved Gameplan evaluation identities are duplicated")
    if set(prior) - set(frame["id"]):
        raise RuntimeError("A saved Gameplan disappeared from evaluation history")
    for column in ("target_window_start", "target_window_end", "evaluated_at"):
        frame[column] = pd.to_datetime(frame[column], utc=True)
    return frame


def _counts(frame: pd.DataFrame) -> dict[str, object]:
    scored = frame.loc[frame["evaluation_status"].eq("EVALUATED")]
    return {
        "forecasts": len(frame), "evaluated": len(scored),
        "pending_maturity": int(frame["evaluation_status"].eq("PENDING_MATURITY").sum()),
        "mature_awaiting_data": int(frame["evaluation_status"].eq("MATURE_AWAITING_DATA").sum()),
        "mean_brier_score": float(scored["brier_score"].mean()) if len(scored) else None,
        "direction_accuracy": float(scored["direction_correct"].astype(float).mean()) if len(scored) else None,
        "raw_direction_accuracy": (float(scored["raw_direction_correct"].dropna().astype(float).mean())
                                   if len(scored) and scored["raw_direction_correct"].notna().any() else None),
    }


def compare_gameplan_variants(evaluations: pd.DataFrame) -> dict[str, object]:
    """Compare every frozen OG/YG pair on identical, mutually observed windows.

    Probability scores retain each edition's own target and are not ranked
    against each other. No best historical edition is selected after results.
    """
    pairs = []
    keys = ["symbol", "model_group", "route", "target_window_start", "target_window_end",
            "target_price_source_contract", "target_price_dataset"]
    for day, dated in evaluations.groupby("action_date", sort=True):
        editions = {str(run): frame for run, frame in dated.groupby("source_gameplan_run", sort=True)}
        for og_run, og in editions.items():
            if not og.gameplan_variant.eq("OG").all():
                continue
            for yg_run, yg in editions.items():
                if not yg.gameplan_variant.eq("YG").all():
                    continue
                joined = og.merge(yg, on=keys, suffixes=("_og", "_yg"), validate="one_to_one")
                scored = joined.loc[joined.evaluation_status_og.eq("EVALUATED") & joined.evaluation_status_yg.eq("EVALUATED")]
                if len(scored) and not np.allclose(scored.observed_return_og, scored.observed_return_yg, rtol=0., atol=1e-12):
                    raise RuntimeError("OG/YG comparison has conflicting observations for the same source window")
                calls = scored.loc[scored.raw_direction_correct_og.notna() & scored.raw_direction_correct_yg.notna()]
                pairs.append({
                    "action_date": str(day), "og_source_gameplan_run": og_run, "yg_source_gameplan_run": yg_run,
                    "status": "EVALUATED" if len(scored) else "AWAITING_MATCHED_OUTCOMES",
                    "og_forecasts": len(og), "yg_forecasts": len(yg), "matched_windows": len(joined),
                    "evaluated_same_windows": len(scored), "directional_calls_in_both": len(calls),
                    "og_direction_correct": int(calls.raw_direction_correct_og.sum()),
                    "yg_direction_correct": int(calls.raw_direction_correct_yg.sum()),
                    "og_direction_accuracy": float(calls.raw_direction_correct_og.astype(float).mean()) if len(calls) else None,
                    "yg_direction_accuracy": float(calls.raw_direction_correct_yg.astype(float).mean()) if len(calls) else None,
                    "og_cost_target_brier": float(scored.brier_score_og.mean()) if len(scored) else None,
                    "yg_raw_direction_brier": float(scored.brier_score_yg.mean()) if len(scored) else None,
                    "both_promoted_windows": int((scored.model_status_og.eq("PROMOTED") & scored.model_status_yg.eq("PROMOTED")).sum()),
                })
    return {"schema_version": "og-yg-gameplan-comparison-v1", "pairs": pairs,
            "scope": "All frozen OG/YG editions, paired by action date, symbol, route, exact windows and price source; no edition selected after observing outcomes.",
            "probability_score_note": "OG and YG Brier scores have different targets and are not directly ranked. Direction accuracy compares the same raw price moves.",
            "orders_placed": 0}


def evaluate_saved_gameplans(
    root: Path, *, observed_groups: Mapping[str, pd.DataFrame], evaluated_at: object,
    input_files: Sequence[Path] = (),
) -> GameplanEvaluationResult:
    root = Path(root).resolve()
    now = utc_timestamp(evaluated_at)
    from ml.directional_forecast_evaluation import evaluate_saved_directional_forecasts
    evaluate_saved_directional_forecasts(root, observed_groups=observed_groups,
                                        evaluated_at=now, input_files=input_files)
    with exclusive_runtime_lock(root / ".ducketz-gameplan-evaluation.lock", process_name="Gameplan evaluation"):
        forecasts, source_files = _saved_forecasts(root)
        prior = read_evaluation_history(root)
        frame = evaluate_forecasts(forecasts, observed_groups=observed_groups, evaluated_at=now,
                                   previous=prior.evaluations if prior else None)
        local = now.tz_convert("America/Los_Angeles")
        friday = local.date() - pd.Timedelta(days=(local.weekday() - 4) % 7)
        if local.weekday() == 4 and local.hour < 17:
            friday -= pd.Timedelta(days=7)
        monday = friday - pd.Timedelta(days=4)
        weekly = frame.loc[frame["action_date"].ge(str(monday)) & frame["action_date"].le(str(friday))]
        summary = {
            "schema_version": EVALUATION_VERSION, "observed_at": now.isoformat(),
            "metric_semantics": "Brier and historical direction_accuracy score each forecast's own frozen binary target. raw_direction_accuracy scores saved bullish/bearish calls against the price-move sign. Compare probability quality within probability_target_contract, not across OG and YG targets.",
            "first_action_date": FIRST_GAMEPLAN_ACTION_DATE, "all_saved_gameplans": _counts(frame),
            "review_week": {"start": str(monday), "end": str(friday), **_counts(weekly)},
            "by_action_date": {str(day): _counts(group) for day, group in frame.groupby("action_date", sort=True)},
            "by_probability_target": {str(contract): _counts(group) for contract, group in frame.groupby("probability_target_contract", sort=True)},
            "by_model_group_and_status": {
                f"{horizon}/{status}": _counts(group)
                for (horizon, status), group in frame.groupby(["model_group", "model_status"], sort=True)
            },
            "broker_orders_enabled": False, "orders_placed": 0,
            "scope": "Directional forecasts from saved Gameplans only; no inferred trade or options P/L",
        }
        run = create_timestamp_directory(root / "ml/gameplan-evaluation-runs", timestamp=utc_timestamp())
        frame.to_parquet(run / "evaluations.parquet", index=False)
        _write_json(run / "summary.json", summary)
        comparison = compare_gameplan_variants(frame)
        _write_json(run / "og-yg-comparison.json", comparison)
        lines = ["# Gameplan review", "", f"First Gameplan date: {FIRST_GAMEPLAN_ACTION_DATE}.", "",
                 f"Review week: {monday} through {friday}.", "", summary["scope"], "",
                 "| Gameplan date | Forecasts | Evaluated | Not mature yet | Mature, waiting for data |",
                 "|---|---:|---:|---:|---:|"]
        for day, counts in summary["by_action_date"].items():
            lines.append(f"| {day} | {counts['forecasts']} | {counts['evaluated']} | {counts['pending_maturity']} | {counts['mature_awaiting_data']} |")
        lines.extend(["", "Longer forecasts remain saved until their exact target windows mature.",
                      "Missing outcome data is reported explicitly and retried on the next evaluation.", "",
                      "## OG Gameplan vs Yung Gameplan (YG)", "", comparison["scope"], "", comparison["probability_score_note"], ""])
        for pair in comparison["pairs"]:
            count = pair["directional_calls_in_both"]
            lines.append(f"- {pair['action_date']}: {pair['evaluated_same_windows']} observed matching windows; "
                         f"OG {pair['og_direction_correct']}/{count}, YG {pair['yg_direction_correct']}/{count} correct directional calls. "
                         f"{pair['status']}. OG: `{pair['og_source_gameplan_run']}`; YG: `{pair['yg_source_gameplan_run']}`.")
        if not comparison["pairs"]:
            lines.append("No matching frozen OG/YG editions are available yet.")
        (run / "review.md").write_text("\n".join(lines), encoding="utf-8")
        previous_files = (prior.run_directory / "receipt.json", prior.run_directory / "evaluations.parquet") if prior else ()
        write_manifest(run, run_timestamp=now, input_files=(*source_files, *input_files, *previous_files),
                       output_files=("evaluations.parquet", "summary.json", "review.md", "og-yg-comparison.json"),
                       configuration={"first_action_date": FIRST_GAMEPLAN_ACTION_DATE}, datastore_root=root)
        _write_json(run / "receipt.json", {
            "schema_version": EVALUATION_VERSION, "run_path": run.relative_to(root).as_posix(),
            "completed_at": utc_timestamp().isoformat(), "manifest_checksum_sha256": file_checksum(run / "manifest.json"),
            "broker_orders_enabled": False, "orders_placed": 0,
        })
        _write_json(root / "ml/gameplan-evaluation-latest/run.json", {
            "schema_version": EVALUATION_VERSION,
            "current": {"run_path": run.relative_to(root).as_posix(), "receipt_checksum_sha256": file_checksum(run / "receipt.json")},
        })
        return read_evaluation_history(root)


def saved_independent_price_sources(root: Path) -> set[str]:
    """Discover source identities from verified immutable independent plans."""
    return {price for price, _ in saved_independent_observation_sources(root)}


def saved_independent_observation_sources(root: Path) -> set[tuple[str, str | None]]:
    """Keep old feature selection when reconstructing old saved target cohorts."""
    from ml.independent_stock_targets import STOCK_TARGET_CONTRACT_VERSION
    from ml.nightly_gameplan import read_gameplan_run
    from ml.stock_target_prices import independent_price_identity
    sources = set()
    for receipt in (Path(root) / "ml/nightly-gameplan-runs").glob("*/receipt.json"):
        publication = read_gameplan_run(root, receipt.parent)
        config = publication.manifest.get("configuration", {})
        if config.get("target_contract_version") == STOCK_TARGET_CONTRACT_VERSION:
            sources.add((independent_price_identity(config)[0], config.get("source_selection_contract")))
    return sources


def run_gameplan_evaluation_once(root: Path, *, evaluated_at: object | None = None) -> GameplanEvaluationResult:
    from ml.current_publication import read_current_publication
    from ml.nightly_gameplan import _daily_weekly_outcomes, _intraday_outcomes, _load_equity_minute_bars, _overnight_sources
    root = Path(root).resolve()
    now = utc_timestamp(evaluated_at)
    source = read_current_publication(root)
    samples_path = source.run_directory / "samples.parquet"
    samples = pd.read_parquet(samples_path)
    symbols = tuple(sorted(samples["symbol"].astype(str).unique()))
    sources = _overnight_sources(samples, symbols=symbols, available_at=now)
    bars, bar_files = _load_equity_minute_bars(root, symbols=symbols)
    # Future rows may exist in a local file; they are never outcome evidence now.
    bars = bars.loc[pd.to_datetime(bars["timestamp"], utc=True).lt(now)]
    hourly, four = _intraday_outcomes(sources=sources, feature_columns=(), minute_bars=bars)
    daily, weekly = _daily_weekly_outcomes(samples, feature_columns=())
    from ml.independent_stock_targets import build_stock_training_groups
    from ml.stock_target_prices import CANONICAL_STOCK_PRICE_SOURCE, load_stock_target_prices
    from ml.gameplan_source_selection import GAMEPLAN_SOURCE_SELECTION_VERSION, select_prior_session_sources
    groups = {"1h": hourly, "4h": four, "1d": daily, "1w": weekly}
    price_files = list(bar_files)
    independent_sources = None
    for contract, selection_contract in sorted(saved_independent_observation_sources(root), key=str):
        source_bars = bars
        if contract != CANONICAL_STOCK_PRICE_SOURCE:
            source_bars, source_files, _ = load_stock_target_prices(root, symbols=symbols, source_contract=contract)
            price_files.extend(source_files)
        selected_sources = sources
        if selection_contract == GAMEPLAN_SOURCE_SELECTION_VERSION:
            if independent_sources is None:
                # Duplicate validation includes the original feature values even
                # when this evaluator only needs observed target endpoints.
                from ml.nightly_gameplan import _feature_columns
                independent_sources = select_prior_session_sources(samples, symbols=symbols, available_at=now,
                    feature_columns=_feature_columns(source.manifest, samples))
            selected_sources = independent_sources
        independent = build_stock_training_groups(selected_sources, feature_columns=(), minute_bars=source_bars,
                                                   available_at=now, price_source_contract=contract)
        groups.update({f"{contract}/{selection_contract}/{name}": frame for name, frame in independent.items()})
    return evaluate_saved_gameplans(root, observed_groups=groups,
                                   evaluated_at=now, input_files=(samples_path, *dict.fromkeys(price_files)))


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".tmp-{os.getpid()}")
    try:
        temp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--datastore", type=Path)
    group.add_argument("--datastore-target", choices=tuple(DATASTORE_TARGETS), default="pc")
    args = parser.parse_args(argv)
    root = resolve_datastore_dir(root_dir=args.datastore, target=None if args.datastore else args.datastore_target)
    try:
        result = run_gameplan_evaluation_once(root)
    except Exception as exc:
        print(json.dumps({"status": "FAILED", "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps({"status": "COMPLETE", "run_path": str(result.run_directory), **result.summary}, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
