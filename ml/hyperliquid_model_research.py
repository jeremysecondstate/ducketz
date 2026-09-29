"""Bounded, offline model comparisons on pinned Hyperliquid snapshots.

No runtime, publication, account, or execution module is imported. Output is
confined to a new directory beneath the datastore's _model_research namespace.
All recipes and blends are fixed before assessment. These historical periods
have been examined in earlier research; they are not a globally unseen lockbox.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
from time import perf_counter
import warnings

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from datafetching.hyperliquid_candles import INTERVAL_MS
from ml.hyperliquid_model_config import load_config
from ml.hyperliquid_models import (
    _block_summary, _ensemble_probability, _logit, _metrics, _normalized_weights, _positive_probability,
    _split_rows_with_details, _training_rows, _validate_snapshot, load_snapshot, train_candidate,
)
from ml.hyperliquid_research_classical import make_classical_estimators


CURRENT_MEMBERS = ("logistic", "extra_trees", "hist_gradient_boosting", "mlp")
SEQUENCE_MEMBERS = ("cnn", "gru", "cnn_gru")
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "configs/hyperliquid-models-research-70-15-15.json"


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _current_weights(settings) -> dict[str, float]:
    return _normalized_weights({name: getattr(settings, f"{name}_weight") for name in CURRENT_MEMBERS})


def context_positions(snapshot, rows: pd.DataFrame, sequence_length: int) -> tuple[np.ndarray, np.ndarray]:
    """Locate past-only windows; reject short history and windows across gaps."""
    if type(sequence_length) is not int or sequence_length < 1:
        raise ValueError("sequence_length must be a positive integer.")
    stamps = pd.DatetimeIndex(snapshot.features["close_time"])
    positions = stamps.get_indexer(rows["close_time"])
    if (positions < 0).any():
        raise ValueError("Every decision row must exist in the source snapshot.")
    step = pd.Timedelta(milliseconds=INTERVAL_MS[snapshot.interval])
    # The prefix count gives an O(1) continuity check for each window.
    broken = np.r_[False, np.asarray(stamps[1:] - stamps[:-1] != step)]
    prefix = np.cumsum(broken)
    starts = np.maximum(positions - sequence_length + 1, 0)
    keep = (positions >= sequence_length - 1) & (prefix[positions] == prefix[starts])
    return positions[keep], keep


def prepare_data(snapshot, settings, sequence_length=32):
    """Fit preprocessing once; share identical decision rows across families."""
    _validate_snapshot(snapshot)
    mature = _training_rows(snapshot, settings.horizon_bars)
    nominal, split_details = _split_rows_with_details(mature, settings)
    blocks, positions, context_omissions = {}, {}, {}
    for name, rows in nominal.items():
        positions[name], keep = context_positions(snapshot, rows, sequence_length)
        blocks[name] = rows.loc[keep].copy()
        context_omissions[name] = int((~keep).sum())
        if len(blocks[name]) < 2:
            raise ValueError(f"Need at least two {name} rows with complete sequence context.")
    if len(blocks["fit"]) < settings.min_train_rows:
        raise ValueError("Insufficient fitting rows after sequence context exclusions.")
    if blocks["fit"]["y_not_down"].nunique() != 2:
        raise ValueError("Fitting rows must contain both classes.")
    names = list(snapshot.feature_names)
    imputer = SimpleImputer(strategy="median", keep_empty_features=True)
    imputer.fit(blocks["fit"].loc[:, names])
    values = imputer.transform(snapshot.features.loc[:, names])
    scaler = StandardScaler().fit(values[positions["fit"]])
    scaled = np.asarray(scaler.transform(values), dtype=np.float32)
    values = np.asarray(values, dtype=np.float32)
    if not np.isfinite(scaled).all() or not np.isfinite(values).all():
        raise ValueError("Preprocessed arrays must be finite.")
    windows = np.lib.stride_tricks.sliding_window_view(
        scaled, window_shape=sequence_length, axis=0,
    ).transpose(0, 2, 1)
    arrays = {}
    for name, block in blocks.items():
        index = positions[name]
        arrays[name] = {
            "raw": np.ascontiguousarray(values[index]),
            "scaled": np.ascontiguousarray(scaled[index]),
            "sequence": np.ascontiguousarray(windows[index - sequence_length + 1]),
            "labels": block["y_not_down"].to_numpy(),
        }
    return {
        "blocks": blocks, "nominal_blocks": nominal, "arrays": arrays,
        "imputer": imputer, "scaler": scaler, "context_omissions": context_omissions,
        "mature_rows": len(mature), "unknown_rows": len(snapshot.features) - len(mature),
        "split_details": split_details,
    }


def blend_probabilities(probabilities: dict[str, np.ndarray], classical_names, *, current_weights=None) -> dict[str, np.ndarray]:
    """Predeclared weights; no label or assessment metric can enter this function."""
    mean = lambda names: np.mean(np.column_stack([probabilities[name] for name in names]), axis=1)
    current = _ensemble_probability({name: probabilities[name] for name in CURRENT_MEMBERS},
                                    current_weights or {})
    result = {"current_four": current, "expanded_classical": mean(classical_names)}
    if all(name in probabilities for name in SEQUENCE_MEMBERS):
        neural = mean(SEQUENCE_MEMBERS)
        result.update({
            "sequence_three": neural,
            "current_plus_sequence": 0.5 * current + 0.5 * neural,
            "current_plus_cnn_gru": 0.75 * current + 0.25 * probabilities["cnn_gru"],
        })
    return result


def compare_snapshot(snapshot, settings, *, sequence_length=32, epochs=8,
                     include_sequences=True, compare_fixed=True):
    """Train every candidate before using assessment labels to calculate scores."""
    started = perf_counter()
    prepared = prepare_data(snapshot, settings, sequence_length)
    arrays = prepared["arrays"]
    specifications, skipped = make_classical_estimators(seed=settings.random_state, threads=settings.model_threads)
    fitted, model_info, captured = {}, {}, []
    # Separate fit/calibration from assessment, including for neural candidates.
    for name, spec in specifications.items():
        estimator, representation = spec["estimator"], "scaled" if spec["scaled"] else "raw"
        clock = perf_counter()
        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter("always")
            estimator.fit(arrays["fit"][representation], arrays["fit"]["labels"])
        captured.extend(f"{name}: {w.category.__name__}: {w.message}" for w in records)
        fit_seconds = perf_counter() - clock
        fitted[name] = (estimator, representation)
        model_info[name] = {"fit_seconds": fit_seconds, "input": representation,
                            "estimator": type(estimator).__name__,
                            "parameters": estimator.get_params(deep=False)}
    if include_sequences:
        from ml.hyperliquid_research_sequences import fit_sequence_models
        neural = fit_sequence_models(
            arrays["fit"]["sequence"], arrays["fit"]["labels"], arrays["calibration"]["sequence"],
            seed=settings.random_state, threads=settings.model_threads, epochs=epochs,
        )
        for name, item in neural.items():
            fitted[name] = (item["model"], "sequence")
            model_info[name] = {key: value for key, value in item.items()
                                if key not in {"model", "raw_calibration_probability"}}
    calibrators = {}
    y_cal = arrays["calibration"]["labels"]
    for name, (estimator, representation) in fitted.items():
        raw = _positive_probability(estimator, arrays["calibration"][representation])
        clock = perf_counter()
        if len(np.unique(y_cal)) == 2:
            calibrator = LogisticRegression(C=settings.calibration_c, max_iter=200, random_state=settings.random_state)
            calibrator.fit(_logit(raw), y_cal)
        else:
            calibrator = None
            captured.append(f"{name}: calibration contains one class; identity calibration.")
        calibrators[name] = calibrator
        model_info[name]["calibration_seconds"] = perf_counter() - clock
    # All model parameters, calibrators, membership and weights are now fixed.
    probabilities = {}
    for name, (estimator, representation) in fitted.items():
        clock = perf_counter()
        raw = _positive_probability(estimator, arrays["assessment"][representation])
        cal = calibrators[name]
        probabilities[name] = raw if cal is None else _positive_probability(cal, _logit(raw))
        model_info[name]["assessment_seconds"] = perf_counter() - clock
    current_weights = _current_weights(settings)
    probabilities.update(blend_probabilities(probabilities, list(specifications), current_weights=current_weights))
    prior = np.concatenate([arrays["fit"]["labels"], y_cal]).mean()
    y_test = arrays["assessment"]["labels"]
    probabilities["prior_baseline"] = np.full(len(y_test), prior)
    probabilities["neutral_baseline"] = np.full(len(y_test), 0.5)
    metrics = {name: _metrics(y_test, prediction) for name, prediction in probabilities.items()}
    assessment = prepared["blocks"]["assessment"][[
        "timestamp", "close_time", "label_end_time", "y_not_down", "actual_return",
    ]].copy().reset_index(drop=True)
    for name, prediction in probabilities.items():
        assessment[f"{name}_p_not_down"] = prediction
    nominal = prepared["nominal_blocks"]
    actual = prepared["blocks"]
    report = {
        "coin": snapshot.coin, "interval": snapshot.interval, "source_run_id": snapshot.run_id,
        "horizon_bars": settings.horizon_bars,
        "horizon_minutes": settings.horizon_bars * INTERVAL_MS[snapshot.interval] / 60_000,
        "settings": asdict(settings), "feature_count": len(snapshot.feature_names),
        "current_four_weights": current_weights,
        "sequence_length": sequence_length, "sequence_epochs": epochs,
        "sequence_window_minutes": sequence_length * INTERVAL_MS[snapshot.interval] / 60_000,
        "source_rows": len(snapshot.features), "mature_usable_rows": prepared["mature_rows"],
        "unknown_or_featureless_rows": prepared["unknown_rows"],
        "split_before_context_filter": {name: _block_summary(rows) for name, rows in nominal.items()},
        "splits": {name: _block_summary(rows) for name, rows in actual.items()},
        "context_omitted_rows": prepared["context_omissions"],
        "split_details": prepared["split_details"],
        "effective_fractions_of_mature_rows": {name: len(rows) / prepared["mature_rows"] for name, rows in actual.items()},
        "preprocessing": "One train-only median imputer; one train-only scaler shared by logistic/MLP/sequence models; unscaled imputed inputs for trees; contiguous past-only sequence windows.",
        "evaluation_role": "Historical final assessment; not used to fit, calibrate, tune or select these recipes. Source history has been examined in earlier research, so not a globally untouched lockbox.",
        "selection_policy": "Fixed recipes and blends; report every result. No automatic promotion or best-test selection.",
        "model_fit_lag_hours": (snapshot.features["close_time"].iloc[-1] - actual["fit"]["close_time"].iloc[-1]).total_seconds() / 3600,
        "metrics": metrics, "models": model_info, "skipped_models": skipped, "warnings": captured,
    }
    fixed_result = None
    if compare_fixed:
        fixed_settings = replace(settings, split_mode="fixed_rows", train_fraction=None,
                                 calibration_fraction=None, assessment_fraction=None)
        fixed_result = train_candidate(snapshot, fixed_settings)
        fractional_control = train_candidate(snapshot, settings)
        common = fractional_control["assessment"][["close_time", "y_not_down", "p_not_down"]].rename(
            columns={"p_not_down": "fractional_probability"},
        ).merge(fixed_result["assessment"][["close_time", "p_not_down"]], on="close_time", validate="one_to_one")
        if len(common) < 2:
            raise ValueError("No common assessment interval for fixed-row comparison.")
        report["common_tail_split_comparison"] = {
            "rows": len(common), "first_close_utc": common.close_time.iloc[0].isoformat(),
            "last_close_utc": common.close_time.iloc[-1].isoformat(),
            "fixed_rows_current_four": _metrics(common.y_not_down, common.p_not_down),
            "fractional_current_four": _metrics(common.y_not_down, common.fractional_probability),
            "note": "Identical production estimators and preprocessing, same source snapshot and same assessment timestamps; only splitting changes. Separate from the shared-float32/context-filtered expanded-model comparison.",
        }
        fixed_result["fractional_control"] = fractional_control
    report["total_seconds"] = perf_counter() - started
    return report, assessment, fixed_result


def _json_safe(value):
    """Estimator manifests may include objects; retain their explicit repr."""
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return repr(value)


def run_experiment(config_path=DEFAULT_CONFIG, *, output=None, include_sequences=True, epochs=8, sequence_length=32):
    config = load_config(config_path)
    if len(config.horizons_bars) != 1:
        raise ValueError("This bounded comparison requires exactly one configured horizon.")
    settings = config.model_settings(config.horizons_bars[0])
    if settings.split_mode != "fractions" or settings.max_train_rows is not None:
        raise ValueError("Research requires uncapped chronological fractional splitting.")
    markets = config.load_markets()
    if (settings.train_fraction, settings.calibration_fraction,
            settings.assessment_fraction) != (.70, .15, .15):
        raise ValueError("This protocol requires the approved 70/15/15 fractions.")
    horizon_minutes = settings.horizon_bars * INTERVAL_MS[markets.interval] / 60_000
    root = (markets.output_root / "_model_research").resolve()
    destination = Path(output).resolve() if output else root / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-expanded-70-15-15")
    if not destination.is_relative_to(root) or destination == root:
        raise ValueError("Research output must be a new child of the datastore _model_research directory.")
    # Resolve every pointer once and pin the exact source before fitting anything.
    snapshots = [load_snapshot(markets.output_root, coin, markets.interval) for coin in markets.symbols]
    for snapshot in snapshots:
        # Fail before creating an output when a selected horizon has no labels,
        # or its labels do not match mature outcomes at the exact future time.
        _training_rows(snapshot, settings.horizon_bars)
    destination.mkdir(parents=True, exist_ok=False)
    manifest = []
    for snapshot in snapshots:
        source_files = {}
        copied = destination / "inputs" / snapshot.coin / snapshot.interval / "runs" / snapshot.run_id
        copied.mkdir(parents=True)
        for name in ("features.parquet", "labels.parquet", "feature_catalog.json", "summary.json"):
            target = copied / name
            shutil.copy2(snapshot.run_dir / name, target)
            source_files[name] = hashlib.sha256(target.read_bytes()).hexdigest()
        _write_json(copied.parent.parent / "latest.json", {"run_id": snapshot.run_id})
        manifest.append({"coin": snapshot.coin, "interval": snapshot.interval, "run_id": snapshot.run_id,
                         "last_close_utc": snapshot.features.close_time.iloc[-1].isoformat(), "sha256": source_files})
    source_dir = destination / "source"
    source_dir.mkdir()
    recipe_paths = [Path(__file__), Path(__file__).with_name("hyperliquid_models.py"),
                    Path(__file__).with_name("hyperliquid_model_config.py"),
                    Path(__file__).with_name("hyperliquid_research_classical.py"),
                    Path(__file__).with_name("hyperliquid_research_sequences.py"), Path(config_path)]
    source_hashes = {}
    for path in recipe_paths:
        shutil.copy2(path, source_dir / path.name)
        source_hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    specs, skipped = make_classical_estimators(seed=settings.random_state, threads=settings.model_threads)
    _write_json(destination / "protocol.json", _json_safe({
        "created_at_utc": datetime.now(timezone.utc).isoformat(), "settings": asdict(settings),
        "interval": markets.interval, "horizon_bars": settings.horizon_bars,
        "horizon_minutes": horizon_minutes,
        "input_manifest": manifest, "source_sha256": source_hashes,
        "classical_recipes": {name: spec["estimator"].get_params(deep=False) for name, spec in specs.items()},
        "skipped": skipped, "sequences": list(SEQUENCE_MEMBERS) if include_sequences else [],
        "sequence_length": sequence_length, "epochs": epochs, "sequence_batch_size": 128,
        "sequence_window_minutes": sequence_length * INTERVAL_MS[markets.interval] / 60_000,
        "current_four_weights": _current_weights(settings),
        "blends": {"current_four": list(CURRENT_MEMBERS), "expanded_classical": list(specs),
                   "sequence_three": list(SEQUENCE_MEMBERS), "current_plus_sequence": "0.5 current_four + 0.5 sequence_three",
                   "current_plus_cnn_gru": "0.75 current_four + 0.25 cnn_gru"},
        "secondary_comparison": f"Fixed {settings.calibration_rows}/{settings.assessment_rows} calibration/assessment rows vs fractional splitting with identical production models, calibration, ensemble weights and preprocessing, scored on their shared final timestamps. Only split controls differ.",
        "assessment_policy": "Report all prespecified recipes. No score-based tuning, promotion or paper reset. Previously researched history, not globally untouched.",
    }))
    reports = []
    with threadpool_limits(limits=settings.model_threads):
        for snapshot in snapshots:
            report, assessment, fixed = compare_snapshot(snapshot, settings, sequence_length=sequence_length,
                                                         epochs=epochs, include_sequences=include_sequences)
            report = _json_safe(report)
            market_dir = destination / snapshot.coin
            market_dir.mkdir()
            _write_json(market_dir / "report.json", report)
            assessment.to_parquet(market_dir / "assessment.parquet", index=False)
            _write_json(market_dir / "fixed_rows_report.json", fixed["report"])
            fixed["assessment"].to_parquet(market_dir / "fixed_rows_assessment.parquet", index=False)
            _write_json(market_dir / "fractional_control_report.json", fixed["fractional_control"]["report"])
            fixed["fractional_control"]["assessment"].to_parquet(market_dir / "fractional_control_assessment.parquet", index=False)
            reports.append(report)
            print(f"{snapshot.coin}: {report['total_seconds']:.2f}s; {len(report['models'])} models; {len(assessment)} assessment rows", flush=True)
    names = reports[0]["metrics"].keys()
    summary = {
        "output": str(destination), "markets": [report["coin"] for report in reports],
        "interval": markets.interval, "horizon_bars": settings.horizon_bars,
        "horizon_minutes": horizon_minutes,
        "mean_metrics": {name: {metric: float(np.mean([report["metrics"][name][metric] for report in reports]))
                                for metric in ("brier_score", "log_loss", "accuracy")} for name in names},
        "mean_common_tail": {name: {metric: float(np.mean([report["common_tail_split_comparison"][name][metric] for report in reports]))
                                   for metric in ("brier_score", "log_loss", "accuracy")}
                             for name in ("fixed_rows_current_four", "fractional_current_four")},
        "fit_seconds_by_model": {name: sum(report["models"][name]["fit_seconds"] for report in reports)
                                 for name in reports[0]["models"]},
        "total_seconds": sum(report["total_seconds"] for report in reports),
        "live_state_changed": False,
        "limitations": ["One retrospective assessment period per market; observations and markets are dependent.",
                        (f"Consecutive {settings.horizon_bars}-bar ({horizon_minutes:g}-minute) outcomes overlap."
                         if settings.horizon_bars > 1 else
                         "One-bar outcome windows are adjacent; this does not establish independent samples."),
                        "No trading policy, fee, funding, slippage or profit evaluation.",
                        "Earlier research examined parts of this historical data; not a globally untouched final test.",
                        "No inference of statistical significance or automatic deployment from test rankings."],
    }
    _write_json(destination / "summary.json", summary)
    lines = ["# Expanded models on chronological 70/15/15", "",
             f"Candles: {markets.interval}; forecast: {settings.horizon_bars} bar(s) / {horizon_minutes:g} minutes.", "",
             "All recipes were fixed before assessment; live model and Paper state were not changed.", "", "| Model / blend | Mean Brier | Mean log loss | Mean accuracy |", "| --- | ---: | ---: | ---: |"]
    for name, score in summary["mean_metrics"].items():
        lines.append(f"| {name} | {score['brier_score']:.6f} | {score['log_loss']:.6f} | {score['accuracy']:.2%} |")
    lines.extend(["", "## Same-timestamp split comparison", "", json.dumps(summary["mean_common_tail"], indent=2), "", "## Limits", "", *[f"- {item}" for item in summary["limitations"]]])
    (destination / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--skip-sequences", action="store_true")
    parser.add_argument("--epochs", type=int, default=8)
    parser.add_argument("--sequence-length", type=int, default=32)
    args = parser.parse_args()
    print(json.dumps(run_experiment(args.config, output=args.output, include_sequences=not args.skip_sequences,
                                   epochs=args.epochs, sequence_length=args.sequence_length), indent=2))


if __name__ == "__main__":
    main()
