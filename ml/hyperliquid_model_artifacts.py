"""Local, immutable model releases and an append-once forward prediction journal.

The model runtime is the single writer. Only bundles created in this module's
own market directory are loaded; joblib files must never come from downloads.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any
from uuid import uuid4

import joblib
import numpy as np
import pandas as pd

from datafetching.hyperliquid_candles import INTERVAL_MS
from ml.hyperliquid_data_loop import _atomic_json
from ml.hyperliquid_data_pipeline import _jsonable


MODEL_ARTIFACT_VERSION = 1
_RUN_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}")


def _utc(now: float) -> str:
    if not math.isfinite(now):
        raise ValueError("The current time must be finite.")
    return datetime.fromtimestamp(now, timezone.utc).isoformat()


def _timestamp(value: Any, name: str) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware timestamp.")
    return stamp.tz_convert("UTC")


def model_dir(data_root, coin: str, interval: str, horizon: int) -> Path:
    """Return the isolated model directory, without allowing path components."""
    if not isinstance(coin, str) or not re.fullmatch(r"[A-Z0-9]{1,20}", coin):
        raise ValueError("Use an uppercase perpetual coin symbol, such as BTC.")
    if interval not in INTERVAL_MS or type(horizon) is not int or horizon < 1:
        raise ValueError("Use a supported interval and a positive integer horizon.")
    return Path(data_root).resolve() / "_models" / coin / interval / f"h{horizon}"


def _atomic_parquet(path: Path, frame: pd.DataFrame) -> None:
    temporary = path.with_name(f".{path.name}-{uuid4().hex}.tmp")
    try:
        frame.to_parquet(temporary, index=False)
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.01 * 2 ** attempt)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def publish_candidate(data_root, snapshot, settings, result: dict, *, now: float) -> dict:
    """Publish the exact evaluated bundle; ineligible models stay research-only."""
    trained_at = _utc(now)
    horizon = settings.horizon_bars
    directory = model_dir(data_root, snapshot.coin, snapshot.interval, horizon)
    directory.mkdir(parents=True, exist_ok=True)
    model_id = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8]
    run_dir = directory / "runs" / model_id
    run_dir.mkdir(parents=True, exist_ok=False)
    report = result["report"]
    if type(report.get("eligible")) is not bool:
        raise ValueError("A candidate report must state its boolean eligibility.")
    recipe = asdict(settings) if is_dataclass(settings) else vars(settings).copy()
    record = _jsonable({
        "model_id": model_id,
        "source_run_id": snapshot.run_id,
        "source_last_close_utc": (
            snapshot.features["close_time"].iloc[-1].isoformat()
            if hasattr(snapshot, "features") and len(snapshot.features) else None
        ),
        "trained_at_utc": trained_at,
        "eligible": report["eligible"],
        "settings": recipe,
        "horizon_bars": horizon,
        "coin": snapshot.coin,
        "interval": snapshot.interval,
        "codeversion": MODEL_ARTIFACT_VERSION,
        "feature_revision": snapshot.feature_revision,
        "feature_names": list(snapshot.feature_names),
        "bundle_path": str(run_dir / "bundle.joblib"),
        "report_path": str(run_dir / "report.json"),
        "assessment_path": str(run_dir / "assessment.parquet"),
    })
    # The pointers are changed only after every immutable artifact is complete.
    # A failed write can leave an unreferenced run, never a partially live model.
    joblib.dump(result["bundle"], run_dir / "bundle.joblib")
    result["assessment"].to_parquet(run_dir / "assessment.parquet", index=False)
    _atomic_json(run_dir / "report.json", _jsonable(report))
    _atomic_json(run_dir / "record.json", record)
    _atomic_json(directory / "candidate.json", record)
    if record["eligible"]:
        _atomic_json(directory / "active.json", record)
    return record


def _load_record(directory: Path, name: str, *, now: float, max_age_seconds: float) -> tuple[dict, Path] | None:
    pointer = directory / name
    if not pointer.exists():
        return None
    try:
        pointed = json.loads(pointer.read_text(encoding="utf-8"))
        model_id = pointed["model_id"]
        if not isinstance(model_id, str) or _RUN_ID.fullmatch(model_id) is None:
            return None
        run = directory / "runs" / model_id
        # Never trust the display paths in a pointer. Reject redirected run dirs.
        if run.resolve() != run or (run / "bundle.joblib").resolve() != run / "bundle.joblib":
            return None
        record = json.loads((run / "record.json").read_text(encoding="utf-8"))
        if record["model_id"] != model_id or record["codeversion"] != MODEL_ARTIFACT_VERSION:
            return None
        if model_dir(directory.parents[3], record["coin"], record["interval"], record["horizon_bars"]) != directory:
            return None
        age = now - _timestamp(record["trained_at_utc"], "trained_at_utc").timestamp()
        if not 0 <= age <= max_age_seconds:
            return None
        if name == "active.json" and record.get("eligible") is not True:
            return None
        return record, run / "bundle.joblib"
    except (OSError, ValueError, TypeError, KeyError):
        return None


def load_predictor(
    data_root, coin, interval, horizon, *, now: float, max_age_seconds: float,
    expected_feature_revision=None,
) -> dict | None:
    """Prefer a fresh eligible release, otherwise return the fresh research model."""
    _utc(now)
    if not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
        raise ValueError("Model maximum age must be finite and positive.")
    directory = model_dir(data_root, coin, interval, horizon)
    for pointer, role in (("active.json", "active"), ("candidate.json", "research_candidate")):
        loaded = _load_record(directory, pointer, now=now, max_age_seconds=max_age_seconds)
        if loaded is not None:
            record, bundle_path = loaded
            if expected_feature_revision is not None and record.get("feature_revision") != expected_feature_revision:
                continue
            return {"bundle": joblib.load(bundle_path), "record": record, "role": role}
    return None


def _prediction_record(row: dict) -> dict:
    result = dict(row)
    encoded = result.pop("per_model_json", None)
    if isinstance(encoded, str):
        result["per_model"] = json.loads(encoded)
    for key in ("decision_close_utc", "decision_timestamp_utc", "target_close_utc", "created_at_utc"):
        if key in result:
            result[key] = _timestamp(result[key], key).isoformat()
    return _jsonable(result)


def record_prediction(data_root, coin, interval, horizon, prediction: dict, model_record: dict, *, now: float) -> dict:
    """Keep the first forecast for a decision candle, including its model version."""
    directory = model_dir(data_root, coin, interval, horizon)
    directory.mkdir(parents=True, exist_ok=True)
    decision_time = _timestamp(prediction["decision_close_utc"], "decision_close_utc")
    path = directory / "predictions.parquet"
    existing = pd.read_parquet(path) if path.exists() else pd.DataFrame()
    if not existing.empty:
        decision_times = pd.to_datetime(existing["decision_close_utc"], utc=True)
        duplicate = decision_times.eq(decision_time)
        if duplicate.any():
            # The journal commits first. Repair its JSON projection if a prior
            # write stopped between the Parquet and JSON replacements, using
            # the newest forecast even when this retry concerns an older row.
            latest = _prediction_record(existing.loc[decision_times.idxmax()].to_dict())
            _atomic_json(directory / "latest_prediction.json", latest)
            return _prediction_record(existing.loc[duplicate].iloc[0].to_dict())
    current = _timestamp(_utc(now), "now")
    target = _timestamp(prediction["target_close_utc"], "target_close_utc")
    timestamp = _timestamp(prediction["decision_timestamp_utc"], "decision_timestamp_utc")
    step = pd.Timedelta(milliseconds=INTERVAL_MS[interval])
    if decision_time > current or target <= current:
        raise ValueError("A forecast requires a completed decision candle and a still-future outcome.")
    if decision_time - timestamp != step or target - decision_time != step * horizon:
        raise ValueError("Prediction timestamps must match the configured candle interval and horizon.")
    price = float(prediction["decision_price"])
    p = float(prediction["p_not_down"])
    q = float(prediction["p_down"])
    if not math.isfinite(price) or price <= 0:
        raise ValueError("The decision price must be finite and positive.")
    if not all(math.isfinite(value) and 0 <= value <= 1 for value in (p, q)) or not math.isclose(p + q, 1.0, abs_tol=1e-9):
        raise ValueError("Direction probabilities must be finite, bounded, and complementary.")
    metadata = model_record.get("record", model_record)
    role = model_record.get("role", "active" if metadata.get("eligible") else "research_candidate")
    if role not in ("active", "research_candidate"):
        raise ValueError("Unsupported model role.")
    if (metadata.get("coin"), metadata.get("interval"), metadata.get("horizon_bars")) != (coin, interval, horizon):
        raise ValueError("The model belongs to a different market or horizon.")
    record = _jsonable({
        "prediction_id": uuid4().hex,
        "model_id": metadata["model_id"],
        "data_run_id": prediction["data_run_id"],
        "coin": coin,
        "interval": interval,
        "horizon_bars": horizon,
        "role": role,
        "qualified": role == "active" and metadata.get("eligible") is True,
        "created_at_utc": current.isoformat(),
        "decision_timestamp_utc": timestamp.isoformat(),
        "decision_close_utc": decision_time.isoformat(),
        "decision_price": price,
        "target_close_utc": target.isoformat(),
        "p_not_down": p,
        "p_down": q,
        "per_model": prediction.get("per_model", {}),
    })
    stored = {key: value for key, value in record.items() if key != "per_model"}
    stored["per_model_json"] = json.dumps(record["per_model"], sort_keys=True, allow_nan=False)
    frame = pd.concat([existing, pd.DataFrame([stored])], ignore_index=True)
    frame = frame.sort_values("decision_close_utc").reset_index(drop=True)
    _atomic_parquet(path, frame)
    latest = _prediction_record(frame.iloc[-1].to_dict())
    _atomic_json(directory / "latest_prediction.json", latest)
    return record


def _metrics(frame: pd.DataFrame) -> dict:
    if frame.empty:
        return {"n": 0, "brier": None, "logloss": None, "accuracy": None}
    probability = frame["p_not_down"].to_numpy(dtype=float)
    actual = frame["target_not_down"].to_numpy(dtype=float)
    clipped = np.clip(probability, 1e-15, 1 - 1e-15)
    return {
        "n": len(frame),
        "brier": float(np.mean((probability - actual) ** 2)),
        "logloss": float(-np.mean(actual * np.log(clipped) + (1 - actual) * np.log(1 - clipped))),
        "accuracy": float(np.mean((probability >= 0.5) == actual)),
    }


def score_matured(data_root, snapshot, horizon: int, *, now: float) -> dict:
    """Score previously recorded forecasts at exact observed target timestamps."""
    directory = model_dir(data_root, snapshot.coin, snapshot.interval, horizon)
    directory.mkdir(parents=True, exist_ok=True)
    current = _timestamp(_utc(now), "now")
    outcome_path = directory / "outcomes.parquet"
    existing = pd.read_parquet(outcome_path) if outcome_path.exists() else pd.DataFrame()
    scored = set(existing["prediction_id"]) if not existing.empty else set()
    forecasts_path = directory / "predictions.parquet"
    forecasts = pd.read_parquet(forecasts_path) if forecasts_path.exists() else pd.DataFrame()
    data = snapshot.features[["close_time", "close"]].copy()
    data["close_time"] = pd.to_datetime(data["close_time"], utc=True)
    prices = data.drop_duplicates("close_time", keep="last").set_index("close_time")["close"]
    last_close = data["close_time"].max()
    rows = []
    for forecast in forecasts.to_dict("records"):
        if forecast["prediction_id"] in scored:
            continue
        target = _timestamp(forecast["target_close_utc"], "target_close_utc")
        created = _timestamp(forecast["created_at_utc"], "created_at_utc")
        if target > current or pd.isna(last_close) or target > last_close or created >= target or target not in prices.index:
            continue
        actual_price = float(prices.loc[target])
        if not math.isfinite(actual_price) or actual_price <= 0:
            continue
        actual_return = actual_price / float(forecast["decision_price"]) - 1.0
        row = {key: value for key, value in forecast.items() if key != "per_model_json"}
        row.update({
            "target_price": actual_price,
            "actual_return": actual_return,
            "target_not_down": int(actual_return >= 0),
            "scored_at_utc": current.isoformat(),
            "outcome_data_run_id": snapshot.run_id,
        })
        rows.append(row)
    outcomes = pd.concat([existing, pd.DataFrame(rows)], ignore_index=True) if rows else existing
    if rows:
        _atomic_parquet(outcome_path, outcomes)
    metrics = {
        **_metrics(outcomes),
        "new_outcomes": len(rows),
        "updated_at_utc": current.isoformat(),
        "by_role": {
            role: _metrics(outcomes.loc[outcomes["role"].eq(role)]) if not outcomes.empty else _metrics(outcomes)
            for role in ("active", "research_candidate")
        },
    }
    _atomic_json(directory / "forward_metrics.json", metrics)
    return metrics
