"""Read-only proof that an in-round qualified forecast reached Paper policy."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re

from datafetching.hyperliquid_candles import INTERVAL_MS

WIN_FORECAST_RULE = "qualified-in-round-forecast-v1"
RUN_ID = re.compile(r"\d{8}T\d{6}Z-[a-f0-9]{8}")


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError("Forecast proof requires a timestamp string")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Forecast proof requires timezone-aware timestamps")
    return result.astimezone(timezone.utc).timestamp()


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("Forecast proof requires finite numbers")
    return float(value)


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def validate_witness(witness, seed_at_utc, endpoint_at_utc, recipe=None):
    """Validate retained decision and independent model-record source evidence.

    A policy hold or skip may qualify: a fill is deliberately not required.
    The SQL join in the collector supplies committed cycle provenance, and the
    retained source strings permit validation after the run is archived.
    """
    row = witness["decision"]
    if (not isinstance(row["decision_id"], str) or not row["decision_id"]
            or not isinstance(row["cycle_id"], str) or not row["cycle_id"]
            or row["timestamp_utc"] != row["committed_at_utc"]
            or _hash(row["details_json"]) != witness["decision_sha256"]):
        raise ValueError("Decision commitment or source digest mismatch")
    detail = json.loads(row["details_json"])
    observation = detail["forecast_observation"]
    if type(observation.get("schema_version")) is not int or observation["schema_version"] != 1:
        raise ValueError("Unsupported forecast observation proof")
    prediction = observation["prediction"]
    coin, interval, horizon = prediction["coin"], prediction["interval"], prediction["horizon_bars"]
    if (not isinstance(coin, str) or not re.fullmatch(r"[A-Z0-9]{1,20}", coin)
            or interval not in INTERVAL_MS or type(horizon) is not int or horizon < 1):
        raise ValueError("Invalid forecast market recipe")
    if recipe is not None and (coin not in recipe["symbols"] or interval != recipe["interval"]
                              or horizon not in recipe["horizons_bars"]):
        raise ValueError("Forecast does not belong to the accepted recipe")
    if (prediction.get("qualified") is not True or detail.get("qualified") is not True
            or prediction.get("role") != "active"):
        raise ValueError("Forecast is not qualified and active")
    for key in ("model_id", "data_run_id"):
        if not isinstance(prediction[key], str) or not RUN_ID.fullmatch(prediction[key]):
            raise ValueError("Invalid forecast source identifier")
    if not isinstance(prediction["prediction_id"], str) or not prediction["prediction_id"].strip():
        raise ValueError("Missing prediction identity")
    for key, source in (("coin", "coin"), ("forecast_id", "prediction_id"), ("model_id", "model_id")):
        if row[key] != prediction[source] or detail[key] != prediction[source]:
            raise ValueError("Committed decision and forecast identity mismatch")
    if (detail.get("data_run_id") != prediction["data_run_id"]
            or detail.get("forecast_created_at_utc") != prediction["created_at_utc"]
            or _number(detail["p_not_down"]) != _number(prediction["p_not_down"])):
        raise ValueError("Committed decision and forecast values mismatch")
    p, q = _number(prediction["p_not_down"]), _number(prediction["p_down"])
    if not 0 <= p <= 1 or not 0 <= q <= 1 or abs(p + q - 1) > 1e-9:
        raise ValueError("Invalid complementary forecast probabilities")
    seed, endpoint = _stamp(seed_at_utc), _stamp(endpoint_at_utc)
    created, decision = _stamp(prediction["created_at_utc"]), _stamp(prediction["decision_close_utc"])
    observed, committed = _stamp(observation["observed_at_utc"]), _stamp(row["timestamp_utc"])
    target = _stamp(prediction["target_close_utc"])
    if not seed <= created <= observed <= committed <= endpoint or decision > created:
        raise ValueError("Forecast publication or decision is outside the round")
    if target != decision + INTERVAL_MS[interval] * horizon / 1000:
        raise ValueError("Forecast target does not match its horizon")
    model_source = witness["model_record_json"]
    if _hash(model_source) != witness["model_record_sha256"]:
        raise ValueError("Model record source digest mismatch")
    model = json.loads(model_source)
    if (model.get("coin"), model.get("interval"), model.get("horizon_bars"), model.get("model_id")) != (
            coin, interval, horizon, prediction["model_id"]) or model.get("eligible") is not True:
        raise ValueError("Forecast lacks a matching independently eligible model")
    trained = _stamp(model["trained_at_utc"])
    max_forecast_age = _number(observation["max_forecast_age_seconds"])
    max_model_age = _number(observation["max_model_age_seconds"])
    if max_forecast_age <= 0 or max_model_age <= 0 or trained > created or _number(observation["sigma"]) < 0:
        raise ValueError("Invalid native consumer validation bounds")
    valid_until = min(decision + max_forecast_age, target, trained + max_model_age)
    if (_number(prediction["_valid_until_epoch"]) != valid_until or not committed < valid_until
            or not 0 <= observed - decision <= max_forecast_age or not 0 <= observed - trained <= max_model_age):
        raise ValueError("Forecast was stale or expired at the committed decision")
    return coin, prediction["prediction_id"]


def collect_forecast_evidence(root, experiment, seed_at_utc, endpoint_at_utc, rows):
    """Inspect only committed rows from the caller's read-only ledger snapshot."""
    result = {"schema_version": 1, "rule": WIN_FORECAST_RULE,
              "experiment_id": experiment["experiment_id"], "seed_at_utc": seed_at_utc,
              "endpoint_at_utc": endpoint_at_utc, "committed_decision_count": len(rows),
              "decision_rows_sha256": _hash(json.dumps(rows, sort_keys=True, separators=(",", ":"))),
              "qualified_decision_count": 0, "valid_qualified_forecast_count": 0,
              "invalid_qualified_decision_count": 0, "first_valid_witness": None}
    seen, models = set(), {}
    for row in rows:
        try:
            detail = json.loads(row["details_json"])
            if detail.get("qualified") is not True:
                continue
            result["qualified_decision_count"] += 1
            prediction = detail["forecast_observation"]["prediction"]
            coin, interval, horizon, model_id = (prediction[key] for key in ("coin", "interval", "horizon_bars", "model_id"))
            if (not isinstance(coin, str) or not re.fullmatch(r"[A-Z0-9]{1,20}", coin)
                    or interval not in INTERVAL_MS or type(horizon) is not int or horizon < 1
                    or not isinstance(model_id, str) or not RUN_ID.fullmatch(model_id)):
                raise ValueError("Invalid model source path")
            key = (coin, interval, horizon, model_id)
            if key not in models:
                models[key] = (Path(root) / "_models" / coin / interval / f"h{horizon}" / "runs" / model_id / "record.json").read_bytes().decode("utf-8")
            witness = {"decision": row, "decision_sha256": _hash(row["details_json"]),
                       "model_record_json": models[key], "model_record_sha256": _hash(models[key])}
            identity = validate_witness(witness, seed_at_utc, endpoint_at_utc)
            seen.add(identity)
            if result["first_valid_witness"] is None:
                result["first_valid_witness"] = witness
        except (OSError, AttributeError, KeyError, TypeError, ValueError, OverflowError):
            result["invalid_qualified_decision_count"] += 1
    result["valid_qualified_forecast_count"] = len(seen)
    return result


def has_eligible_forecast(evidence, active, endpoint_at_utc):
    """Fail closed for a missing/malformed summary, then verify its witness."""
    try:
        if (type(evidence.get("schema_version")) is not int or evidence["schema_version"] != 1
                or evidence.get("rule") != WIN_FORECAST_RULE
                or evidence.get("experiment_id") != active["experiment_id"]
                or evidence.get("seed_at_utc") != active["seed_at_utc"]
                or evidence.get("endpoint_at_utc") != endpoint_at_utc
                or type(evidence.get("valid_qualified_forecast_count")) is not int
                or evidence["valid_qualified_forecast_count"] < 1):
            return False
        validate_witness(evidence["first_valid_witness"], active["seed_at_utc"], endpoint_at_utc, active["recipe"])
        return True
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return False
