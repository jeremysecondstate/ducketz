"""Validate saved forecasts for consumers without constructing a trading runtime."""
from __future__ import annotations

import json
import math
from pathlib import Path
import re

import pandas as pd

from datafetching.hyperliquid_candles import INTERVAL_MS


RUN_ID = re.compile(r"\d{8}T\d{6}Z-[a-f0-9]{8}")


def _stamp(value):
    result = pd.Timestamp(value)
    if pd.isna(result) or result.tzinfo is None:
        raise ValueError("A timestamp must be timezone-aware.")
    return result.timestamp()


def read_forecast(coin, now, policy, interval, horizon, *, feature_loader=None):
    """Return ``(prediction, horizon_sigma)`` from local immutable artifacts.

    ``policy`` provides data_root, max_forecast_age_seconds and
    max_model_age_seconds. The optional loader preserves Paper's existing
    per-runtime feature cache; the default reads the pinned Parquet directly.
    Qualification permission and allocation remain consumer policy decisions.
    The returned copy includes ``_valid_until_epoch`` for a consumer's final
    pre-submit freshness check; this value is computed from validated sources.
    """
    if not isinstance(coin, str) or re.fullmatch(r"[A-Z0-9]{1,20}", coin) is None:
        raise ValueError("Use a simple uppercase coin symbol.")
    if interval not in INTERVAL_MS or type(horizon) is not int or horizon <= 0:
        raise ValueError("A supported interval and positive horizon are required.")
    root = Path(policy.data_root)
    directory = root / "_models" / coin / interval / f"h{horizon}"
    prediction = json.loads((directory / "latest_prediction.json").read_text())
    if not isinstance(prediction.get("prediction_id"), str) or not prediction["prediction_id"].strip():
        raise ValueError("Forecast requires a nonempty prediction_id.")
    if (prediction["coin"], prediction["interval"], prediction["horizon_bars"]) != (coin, interval, horizon):
        raise ValueError("Forecast belongs to a different market or horizon.")
    p = float(prediction["p_not_down"])
    q = float(prediction["p_down"])
    if not math.isfinite(p) or not 0 <= p <= 1 or not math.isfinite(q) or abs(p + q - 1) > 1e-9:
        raise ValueError("Invalid complementary forecast probabilities.")
    created, decision = _stamp(prediction["created_at_utc"]), _stamp(prediction["decision_close_utc"])
    if not decision <= created <= now or not 0 <= now - decision <= policy.max_forecast_age_seconds:
        raise ValueError("Forecast is stale or has a future timestamp.")
    target = _stamp(prediction["target_close_utc"])
    if target <= now:
        raise ValueError("Forecast outcome has already matured.")
    if target != decision + INTERVAL_MS[interval] * horizon / 1000:
        raise ValueError("Forecast target does not match the configured horizon.")
    if type(prediction.get("qualified")) is not bool:
        raise ValueError("Forecast qualification must be explicit.")
    model_id = prediction["model_id"]
    if not isinstance(model_id, str) or not RUN_ID.fullmatch(model_id):
        raise ValueError("Invalid local model identifier.")
    record = json.loads((directory / "runs" / model_id / "record.json").read_text())
    if (record.get("coin"), record.get("interval"), record.get("horizon_bars"), record.get("model_id")) != (coin, interval, horizon, model_id):
        raise ValueError("Model record identity does not match the forecast.")
    if prediction["qualified"] and (prediction.get("role") != "active" or record.get("eligible") is not True):
        raise ValueError("Qualified forecast requires an active role and an eligible model record.")
    trained = _stamp(record["trained_at_utc"])
    if trained > created:
        raise ValueError("Model was published after the recorded forecast.")
    if not 0 <= now - trained <= policy.max_model_age_seconds:
        raise ValueError("Forecast model exceeds its configured age.")
    run_id = prediction["data_run_id"]
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ValueError("Invalid local data run identifier.")
    frame = (feature_loader(coin, run_id) if feature_loader else pd.read_parquet(
        root / coin / interval / "runs" / run_id / "features.parquet",
        columns=["close_time", "close", "volatility_log_return_20"]))
    row = frame.loc[frame.close_time.eq(pd.Timestamp(prediction["decision_close_utc"]))]
    if len(row) != 1:
        raise ValueError("The forecast's volatility feature is unavailable.")
    sigma = float(row.iloc[0]["volatility_log_return_20"]) * math.sqrt(horizon)
    if not math.isfinite(sigma) or sigma < 0:
        raise ValueError("The forecast's volatility feature is invalid.")
    return {**prediction, "_valid_until_epoch": min(
        decision + policy.max_forecast_age_seconds,
        target,
        trained + policy.max_model_age_seconds,
    )}, sigma
