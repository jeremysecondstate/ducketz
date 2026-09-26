"""Small, chronological Hyperliquid research ensemble over immutable snapshots.

This module only reads local market data and fits/predicts models. It has no
account, order, scheduler, or artifact-writing dependencies. The evaluated
bundle is the bundle returned for publication: there is no hidden final refit.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from time import perf_counter
from typing import Any, Callable
import warnings

import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesClassifier, HistGradientBoostingClassifier
from sklearn.exceptions import ConvergenceWarning
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss, roc_auc_score
from sklearn.neural_network import MLPClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from datafetching.hyperliquid_candles import CANDLE_COLUMNS, INTERVAL_MS
from technicals.hyperliquid_features import FEATURE_SCHEMA_VERSION


FEATURE_NAMES = (
    "log_return_1", "log_return_3", "log_return_6", "ema_close_6",
    "ema_close_10", "ema_close_20", "ema_close_50",
    "volatility_log_return_6", "volatility_log_return_10",
    "volatility_log_return_20", "rsi_14", "atr_14", "bb_mid_20",
    "bb_width_20", "bb_position_20", "volume_ratio_20", "stochastic_14",
    "momentum_6", "lwmf_v2", "tdindi_v2", "vaa_v2", "adi_v2",
    "dirpresh_v2", "vwtmi_v2", "pvam_v2", "apdi_v2", "ivts_v2",
    "directins_v2", "paindex_v2", "dvwa_v2", "pdpf_v2", "dypim_v2",
    "pmvf_v2", "epdii_v2", "cvei_v2", "smdi_v2", "etsa_v2", "roc_v2",
)
LABEL_MEANING = (
    "1 (not_down) if the close exactly horizon_bars candles later is greater "
    "than or equal to this completed candle's close; 0 (down) otherwise. "
    "Unknown future outcomes are excluded from training and evaluation."
)


@dataclass(frozen=True)
class ModelSettings:
    horizon_bars: int = 4
    min_train_rows: int = 1000
    calibration_rows: int = 192
    assessment_rows: int = 288
    random_state: int = 42
    model_threads: int = 2
    max_train_rows: int | None = None

    def __post_init__(self) -> None:
        for name in ("horizon_bars", "min_train_rows", "calibration_rows",
                     "assessment_rows", "model_threads"):
            value = getattr(self, name)
            minimum = 2 if name.endswith("rows") else 1
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}.")
        if self.max_train_rows is not None and (
                type(self.max_train_rows) is not int or self.max_train_rows < self.min_train_rows):
            raise ValueError("max_train_rows must be None or an integer >= min_train_rows.")
        if (isinstance(self.random_state, bool)
                or not isinstance(self.random_state, int)
                or not 0 <= self.random_state <= 2**32 - 1):
            raise ValueError("random_state must be a uint32 integer.")


@dataclass(frozen=True)
class MarketSnapshot:
    coin: str
    interval: str
    run_id: str
    feature_revision: str
    feature_names: tuple[str, ...]
    features: pd.DataFrame
    labels: pd.DataFrame
    run_dir: Path


@dataclass
class ModelBundle:
    coin: str
    interval: str
    horizon_bars: int
    feature_revision: str
    feature_names: tuple[str, ...]
    estimators: dict[str, Any]
    calibration: dict[str, Any]
    fitted_through_close_utc: str
    source_run_id: str
    calibrated_through_close_utc: str = ""
    fit_label_end_utc: str = ""


def _market(coin: str, interval: str) -> tuple[str, str]:
    if not isinstance(coin, str) or not re.fullmatch(r"[A-Z0-9]{1,20}", coin.upper()):
        raise ValueError("coin must be a simple Hyperliquid market symbol.")
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported interval: {interval!r}.")
    return coin.upper(), interval


def _validate_snapshot(snapshot: MarketSnapshot) -> None:
    coin, interval = _market(snapshot.coin, snapshot.interval)
    if coin != snapshot.coin:
        raise ValueError("Snapshot coin must be uppercase.")
    if snapshot.feature_revision != FEATURE_SCHEMA_VERSION:
        raise ValueError("Unsupported feature revision; build a compatible model explicitly.")
    if tuple(snapshot.feature_names) != FEATURE_NAMES:
        raise ValueError("Feature catalog must match the supported 38 features in order.")
    frame, labels = snapshot.features, snapshot.labels
    if frame.empty or not frame.columns.is_unique or not labels.columns.is_unique:
        raise ValueError("Snapshot is empty or has duplicate columns.")
    if list(frame.columns) != [*CANDLE_COLUMNS, *FEATURE_NAMES]:
        raise ValueError("Feature table must contain canonical raw columns then the exact feature schema.")
    if not {"timestamp", "symbol", "interval"}.issubset(labels.columns):
        raise ValueError("Label table lacks timestamp/market identity.")
    for table in (frame, labels):
        if not table["symbol"].eq(coin).all() or not table["interval"].eq(interval).all():
            raise ValueError("Snapshot market does not match the requested coin and interval.")
        if (not isinstance(table["timestamp"].dtype, pd.DatetimeTZDtype)
                or table["timestamp"].isna().any()):
            raise ValueError("Snapshot timestamps must be timezone-aware and nonmissing.")
    if not frame["timestamp"].is_monotonic_increasing or frame["timestamp"].duplicated().any():
        raise ValueError("Snapshot timestamps must be strictly increasing and unique.")
    if not frame["timestamp"].reset_index(drop=True).equals(labels["timestamp"].reset_index(drop=True)):
        raise ValueError("Feature and label timestamps must align exactly.")
    if not isinstance(frame["close_time"].dtype, pd.DatetimeTZDtype):
        raise ValueError("Candle close_time must be timezone-aware.")
    step = pd.Timedelta(milliseconds=INTERVAL_MS[interval])
    if not frame["close_time"].eq(frame["timestamp"] + step).all():
        raise ValueError("Candle close_time must be the exclusive interval boundary.")
    raw_close = frame["close"].to_numpy(dtype=float)
    if not np.isfinite(raw_close).all() or (raw_close <= 0).any():
        raise ValueError("Closing prices must be finite and positive.")
    if np.isinf(frame.loc[:, list(FEATURE_NAMES)].to_numpy(dtype=float)).any():
        raise ValueError("Feature infinities are invalid; unknown values must be NaN.")


def load_snapshot(output_root: str | Path, coin: str, interval: str) -> MarketSnapshot:
    """Resolve latest.json once; use only its run ID and fixed local filenames."""
    coin, interval = _market(coin, interval)
    dataset_dir = Path(output_root).resolve() / coin / interval
    pointer = json.loads((dataset_dir / "latest.json").read_text(encoding="utf-8"))
    run_id = pointer.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"\d{8}T\d{6}Z-[a-f0-9]{8}", run_id):
        raise ValueError("Invalid local data run identifier in latest.json.")
    runs_dir = (dataset_dir / "runs").resolve()
    run_dir = (runs_dir / run_id).resolve()
    if run_dir.parent != runs_dir:
        raise ValueError("Data snapshot must stay within the local runs directory.")
    catalog = json.loads((run_dir / "feature_catalog.json").read_text(encoding="utf-8"))
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    if (summary.get("run_id") != run_id or summary.get("coin") != coin
            or summary.get("interval") != interval):
        raise ValueError("Snapshot summary does not match the requested dataset.")
    revision = catalog.get("feature_revision")
    if summary.get("feature_revision") != revision:
        raise ValueError("Catalog and snapshot feature revisions differ.")
    specs = catalog.get("features")
    if not isinstance(specs, list) or not all(isinstance(spec, dict) for spec in specs):
        raise ValueError("Invalid feature catalog.")
    snapshot = MarketSnapshot(
        coin=coin, interval=interval, run_id=run_id,
        feature_revision=revision,
        feature_names=tuple(spec.get("name") for spec in specs),
        features=pd.read_parquet(run_dir / "features.parquet"),
        labels=pd.read_parquet(run_dir / "labels.parquet"), run_dir=run_dir,
    )
    _validate_snapshot(snapshot)
    if summary.get("rows") != len(snapshot.features):
        raise ValueError("Snapshot row count does not match its summary.")
    return snapshot


def _training_rows(snapshot: MarketSnapshot, horizon_bars: int) -> pd.DataFrame:
    """Keep exact-time, mature outcomes and derive not-down from signed returns."""
    column = f"future_return_{horizon_bars}bar"
    if column not in snapshot.labels:
        raise ValueError(f"Snapshot has no {column} labels.")
    frame = snapshot.features.copy().reset_index(drop=True)
    actual_return = snapshot.labels[column].to_numpy(dtype=float, na_value=np.nan)
    if np.isinf(actual_return).any():
        raise ValueError("Future return labels contain infinity.")
    frame["actual_return"] = actual_return
    frame["label_end_time"] = frame["close_time"] + pd.Timedelta(
        milliseconds=INTERVAL_MS[snapshot.interval] * horizon_bars
    )
    last_close = frame["close_time"].iloc[-1]
    known = np.isfinite(actual_return)
    if (known & frame["label_end_time"].gt(last_close).to_numpy()).any():
        raise ValueError("A label claims an outcome after the snapshot's last completed candle.")
    close_by_time = frame.set_index("close_time")["close"]
    future_close = close_by_time.reindex(frame["label_end_time"]).to_numpy(dtype=float)
    expected_return = future_close / frame["close"].to_numpy(dtype=float) - 1.0
    if not np.allclose(actual_return[known], expected_return[known], rtol=1e-9, atol=1e-12):
        raise ValueError("Future return labels do not match exact-time candle outcomes.")
    usable_features = frame.loc[:, list(snapshot.feature_names)].notna().any(axis=1)
    frame = frame.loc[known & usable_features].copy()
    frame["y_not_down"] = frame["actual_return"].ge(0).astype("int8")
    return frame.reset_index(drop=True)


def _split_rows(frame: pd.DataFrame, settings: ModelSettings) -> dict[str, pd.DataFrame]:
    """Purge partition boundaries, then optionally retain only the latest fit rows."""
    if len(frame) < settings.assessment_rows:
        raise ValueError("Not enough mature labels for the assessment block.")
    assessment = frame.iloc[-settings.assessment_rows:].copy()
    # Strict inequality intentionally drops boundary labels that finish exactly
    # at the next block's first decision time, as well as those finishing later.
    before_assessment = frame.loc[frame["label_end_time"] < assessment["close_time"].iloc[0]]
    if len(before_assessment) < settings.calibration_rows:
        raise ValueError("Not enough mature labels for the purged calibration block.")
    calibration = before_assessment.iloc[-settings.calibration_rows:].copy()
    fit = frame.loc[frame["label_end_time"] < calibration["close_time"].iloc[0]].copy()
    if settings.max_train_rows is not None:
        fit = fit.iloc[-settings.max_train_rows:].copy()
    if len(fit) < settings.min_train_rows:
        raise ValueError(f"Only {len(fit)} training rows after horizon purging; need {settings.min_train_rows}.")
    if fit["y_not_down"].nunique() != 2:
        raise ValueError("Training labels must contain both down and not-down outcomes.")
    return {"fit": fit, "calibration": calibration, "assessment": assessment}


def _make_estimators(settings: ModelSettings) -> dict[str, Pipeline]:
    def pipeline(model: Any, *, scale: bool) -> Pipeline:
        steps: list[tuple[str, Any]] = [
            ("imputer", SimpleImputer(strategy="median", keep_empty_features=True)),
        ]
        if scale:
            steps.append(("scaler", StandardScaler()))
        steps.append(("model", model))
        return Pipeline(steps)

    seed = settings.random_state
    return {
        "logistic": pipeline(LogisticRegression(C=1.0, max_iter=500, random_state=seed), scale=True),
        "extra_trees": pipeline(ExtraTreesClassifier(
            n_estimators=128, max_depth=10, min_samples_leaf=12,
            max_features=0.7, n_jobs=1, random_state=seed,
        ), scale=False),
        "hist_gradient_boosting": pipeline(HistGradientBoostingClassifier(
            max_iter=100, max_leaf_nodes=15, learning_rate=0.06,
            min_samples_leaf=20, l2_regularization=1.0,
            early_stopping=False, random_state=seed,
        ), scale=False),
        "mlp": pipeline(MLPClassifier(
            hidden_layer_sizes=(32, 16), alpha=0.01, batch_size=128,
            max_iter=100, learning_rate_init=0.001, early_stopping=False,
            shuffle=False, random_state=seed,
        ), scale=True),
    }


def _positive_probability(estimator: Any, values: pd.DataFrame | np.ndarray) -> np.ndarray:
    classes = np.asarray(estimator.classes_)
    if set(classes.tolist()) != {0, 1} or len(classes) != 2:
        raise ValueError("A binary model must expose exactly classes 0=down and 1=not_down.")
    probabilities = np.asarray(estimator.predict_proba(values), dtype=float)
    if probabilities.shape != (len(values), 2):
        raise ValueError("Model probability output has an unexpected shape.")
    if (not np.isfinite(probabilities).all() or (probabilities < 0).any()
            or (probabilities > 1).any() or not np.allclose(probabilities.sum(axis=1), 1.0)):
        raise ValueError("Model probabilities must be finite, bounded, and sum to one.")
    return probabilities[:, int(np.flatnonzero(classes == 1)[0])]


def _logit(probabilities: np.ndarray) -> np.ndarray:
    clipped = np.clip(probabilities, 1e-6, 1.0 - 1e-6)
    return np.log(clipped / (1.0 - clipped)).reshape(-1, 1)


def _calibrated_probability(bundle: ModelBundle, name: str, values: pd.DataFrame) -> np.ndarray:
    raw = _positive_probability(bundle.estimators[name], values)
    calibrator = bundle.calibration[name]
    return raw if calibrator is None else _positive_probability(calibrator, _logit(raw))


def _metrics(labels: np.ndarray, probabilities: np.ndarray) -> dict:
    return {
        "rows": len(labels),
        "log_loss": float(log_loss(labels, probabilities, labels=[0, 1])),
        "brier_score": float(brier_score_loss(labels, probabilities)),
        "accuracy": float(accuracy_score(labels, probabilities >= 0.5)),
        "roc_auc": float(roc_auc_score(labels, probabilities)) if len(np.unique(labels)) == 2 else None,
    }


def _block_summary(frame: pd.DataFrame) -> dict:
    return {
        "rows": len(frame),
        "first_decision_close_utc": frame["close_time"].iloc[0].isoformat(),
        "last_decision_close_utc": frame["close_time"].iloc[-1].isoformat(),
        "last_label_end_utc": frame["label_end_time"].max().isoformat(),
        "not_down_fraction": float(frame["y_not_down"].mean()),
    }


def train_candidate(
    snapshot: MarketSnapshot,
    settings: ModelSettings,
    *,
    model_factory: Callable[[ModelSettings], dict[str, Any]] | None = None,
) -> dict:
    """Fit, calibrate, and assess a fixed ensemble without looking past a split.

    The optional factory is a test seam; production uses all four small models.
    The caller bounds native BLAS/OpenMP threads for the training process.
    """
    started = perf_counter()
    _validate_snapshot(snapshot)
    frame = _training_rows(snapshot, settings.horizon_bars)
    blocks = _split_rows(frame, settings)
    fit, calibration, assessment_rows = (blocks[name] for name in ("fit", "calibration", "assessment"))
    fit_rows_before_window_cap = int((frame["label_end_time"] < calibration["close_time"].iloc[0]).sum())
    training_window_omitted_rows = fit_rows_before_window_cap - len(fit)
    names = list(snapshot.feature_names)
    x_fit, x_calibration, x_assessment = (block.loc[:, names] for block in (fit, calibration, assessment_rows))
    y_fit, y_calibration, y_assessment = (block["y_not_down"].to_numpy() for block in (fit, calibration, assessment_rows))
    estimators = (model_factory or _make_estimators)(settings)
    if not estimators or not all(isinstance(name, str) and name for name in estimators):
        raise ValueError("An ensemble needs at least one named estimator.")
    if len({id(model) for model in estimators.values()}) != len(estimators):
        raise ValueError("Each ensemble member must have its own estimator instance.")
    bundle = ModelBundle(
        coin=snapshot.coin, interval=snapshot.interval, horizon_bars=settings.horizon_bars,
        feature_revision=snapshot.feature_revision, feature_names=tuple(snapshot.feature_names),
        estimators=estimators, calibration={},
        fitted_through_close_utc=fit["close_time"].iloc[-1].isoformat(),
        source_run_id=snapshot.run_id,
        calibrated_through_close_utc=calibration["close_time"].iloc[-1].isoformat(),
        fit_label_end_utc=fit["label_end_time"].max().isoformat(),
    )
    model_timings: dict[str, dict] = {}
    calibration_methods: dict[str, str] = {}
    captured_warnings: list[str] = []
    predictions: dict[str, np.ndarray] = {}
    for name, estimator in estimators.items():
        model_started = perf_counter()
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always", ConvergenceWarning)
            estimator.fit(x_fit, y_fit)
            fit_seconds = perf_counter() - model_started
            cal_started = perf_counter()
            raw_cal = _positive_probability(estimator, x_calibration)
            if len(np.unique(y_calibration)) == 2:
                calibrator = LogisticRegression(C=1.0, max_iter=200, random_state=settings.random_state)
                calibrator.fit(_logit(raw_cal), y_calibration)
                bundle.calibration[name] = calibrator
                calibration_methods[name] = "platt_logit_on_later_calibration_block"
            else:
                bundle.calibration[name] = None
                calibration_methods[name] = "identity_single_class_calibration"
                captured_warnings.append(f"{name}: calibration contains one class; keeping uncalibrated probabilities.")
            calibration_seconds = perf_counter() - cal_started
            assessment_started = perf_counter()
            predictions[name] = _calibrated_probability(bundle, name, x_assessment)
        captured_warnings.extend(f"{name}: {warning.category.__name__}: {warning.message}" for warning in recorded)
        model_timings[name] = {
            "fit_seconds": fit_seconds, "calibration_seconds": calibration_seconds,
            "assessment_seconds": perf_counter() - assessment_started,
            "total_seconds": perf_counter() - model_started,
        }
    ensemble = np.mean(np.column_stack(list(predictions.values())), axis=1)
    # This baseline learns a constant only from observations before assessment.
    prior = float(np.concatenate([y_fit, y_calibration]).mean())
    prior_predictions = np.full(len(y_assessment), prior)
    neutral_predictions = np.full(len(y_assessment), 0.5)
    metrics = {name: _metrics(y_assessment, probability) for name, probability in predictions.items()}
    metrics.update({
        "ensemble": _metrics(y_assessment, ensemble),
        "prior_baseline": _metrics(y_assessment, prior_predictions),
        "neutral_baseline": _metrics(y_assessment, neutral_predictions),
    })
    failed_comparisons = [
        f"{metric} worse than {baseline}"
        for baseline in ("prior_baseline", "neutral_baseline")
        for metric in ("log_loss", "brier_score")
        if metrics["ensemble"][metric] > metrics[baseline][metric] + 1e-9
    ]
    assessment = assessment_rows[["timestamp", "close_time", "label_end_time", "y_not_down", "actual_return"]].copy()
    assessment["p_not_down"] = ensemble
    assessment["p_down"] = 1.0 - ensemble
    for name, probability in predictions.items():
        assessment[f"{name}_p_not_down"] = probability
    assessment["prior_baseline_p_not_down"] = prior
    assessment["neutral_baseline_p_not_down"] = 0.5
    last_close = snapshot.features["close_time"].iloc[-1]
    report = {
        "coin": snapshot.coin, "interval": snapshot.interval,
        "horizon_bars": settings.horizon_bars,
        "horizon_minutes": settings.horizon_bars * INTERVAL_MS[snapshot.interval] / 60_000,
        "data_run_id": snapshot.run_id, "feature_revision": snapshot.feature_revision,
        "feature_names": names, "feature_count": len(names),
        "label_meaning": LABEL_MEANING,
        "ensemble_method": "equal mean of separately calibrated P(not_down); P(down)=1-P(not_down)",
        "model_names": list(estimators), "metrics": metrics,
        "eligible": not failed_comparisons,
        "eligibility": not failed_comparisons,
        "eligibility_reasons": failed_comparisons or ["Ensemble log loss and Brier score meet both past-prior and 0.5 baselines."],
        "evaluation_role": "Chronological assessment used for eligibility; this is a promotion holdout, not an untouched final test.",
        "refit_after_assessment": False,
        "snapshot_last_close_utc": last_close.isoformat(),
        "model_fit_through_close_utc": bundle.fitted_through_close_utc,
        "calibration_through_close_utc": bundle.calibrated_through_close_utc,
        "model_fit_lag_hours": (last_close - fit["close_time"].iloc[-1]).total_seconds() / 3600,
        "splits": {name: _block_summary(block) for name, block in blocks.items()},
        "mature_usable_rows": len(frame),
        "unknown_or_featureless_rows": len(snapshot.features) - len(frame),
        "max_train_rows": settings.max_train_rows,
        "fit_rows_before_window_cap": fit_rows_before_window_cap,
        "training_window_omitted_rows": training_window_omitted_rows,
        "purged_rows": len(frame) - sum(len(block) for block in blocks.values()) - training_window_omitted_rows,
        "calibration_methods": calibration_methods,
        "prior_baseline_probability": prior,
        "model_timings": model_timings,
        "total_fit_seconds": sum(item["fit_seconds"] for item in model_timings.values()),
        "total_seconds": perf_counter() - started,
        "model_threads": settings.model_threads,
        "warnings": captured_warnings,
    }
    return {"bundle": bundle, "report": report, "assessment": assessment.reset_index(drop=True)}


def predict_bundle(bundle: ModelBundle, snapshot: MarketSnapshot) -> dict:
    """Predict the actual latest completed row, including its missing features."""
    started = perf_counter()
    _validate_snapshot(snapshot)
    if (bundle.coin, bundle.interval) != (snapshot.coin, snapshot.interval):
        raise ValueError("Model market does not match the prediction snapshot.")
    if (bundle.feature_revision != snapshot.feature_revision
            or tuple(bundle.feature_names) != tuple(snapshot.feature_names)):
        raise ValueError("Model feature revision/order does not match the snapshot.")
    if isinstance(bundle.horizon_bars, bool) or not isinstance(bundle.horizon_bars, int) or bundle.horizon_bars < 1:
        raise ValueError("Model horizon must be a positive integer.")
    if not bundle.estimators or set(bundle.estimators) != set(bundle.calibration):
        raise ValueError("Model estimators and calibrators must have matching names.")
    latest = snapshot.features.iloc[-1]
    if pd.Timestamp(bundle.fitted_through_close_utc) > latest["close_time"]:
        raise ValueError("Cannot predict a snapshot older than the model fit cutoff.")
    if bundle.calibrated_through_close_utc and pd.Timestamp(bundle.calibrated_through_close_utc) > latest["close_time"]:
        raise ValueError("Cannot predict a snapshot older than the calibration cutoff.")
    values = snapshot.features.iloc[[-1]].loc[:, list(bundle.feature_names)]
    if values.isna().all(axis=1).iloc[0]:
        raise ValueError("Latest completed candle has no observed model features; an older row is not substituted.")
    per_model = {}
    for name in bundle.estimators:
        p = float(_calibrated_probability(bundle, name, values)[0])
        per_model[name] = {"p_not_down": p, "p_down": 1.0 - p}
    probability = float(np.mean([values["p_not_down"] for values in per_model.values()]))
    target_close = latest["close_time"] + pd.Timedelta(milliseconds=INTERVAL_MS[bundle.interval] * bundle.horizon_bars)
    return {
        "coin": bundle.coin, "interval": bundle.interval, "horizon_bars": bundle.horizon_bars,
        "data_run_id": snapshot.run_id, "model_source_run_id": bundle.source_run_id,
        "feature_revision": bundle.feature_revision, "feature_count": len(bundle.feature_names),
        "timestamp": latest["timestamp"].isoformat(),
        "decision_timestamp_utc": latest["timestamp"].isoformat(),
        "close_time": latest["close_time"].isoformat(),
        "decision_close_utc": latest["close_time"].isoformat(),
        "label_end_time": target_close.isoformat(),
        "target_close_utc": target_close.isoformat(),
        "decision_close": float(latest["close"]),
        "decision_price": float(latest["close"]),
        "label_meaning": LABEL_MEANING,
        "p_not_down": probability, "p_down": 1.0 - probability,
        "consensus": probability, "per_model": per_model,
        "missing_features_imputed": values.columns[values.iloc[0].isna()].tolist(),
        "model_fit_through_close_utc": bundle.fitted_through_close_utc,
        "calibration_through_close_utc": bundle.calibrated_through_close_utc,
        "elapsed_seconds": perf_counter() - started,
    }
