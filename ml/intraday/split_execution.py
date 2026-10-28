"""Immutable small execution derivatives, prepared outside the broker process.

Full model diagnostics and full indicator observations remain retrievable by
content hash. This is a new immutable derivative, never a mutable journal cache.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .prediction import ContractError, digest, encoded, instant, number

VERSION = "intraday-split-execution-v1"
_SHA = re.compile(r"[a-f0-9]{64}")
_MODEL_FIELDS = frozenset({
    "name", "family", "probability_not_down", "status", "version", "error",
    "artifact_path", "artifact_sha256", "artifact_persisted_at", "artifact_persistence_seconds",
    "training_started_at", "training_completed_at", "training_seconds", "failed_at",
    "inference_started_at", "inference_completed_at", "inference_seconds", "warnings",
})


def _file_hash(path):
    checksum = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def seal_evidence(root, value):
    """Atomically publish canonical JSON; existing objects always revalidate."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".partial-", suffix=".json", dir=root)
    temporary = Path(name)
    checksum, count = hashlib.sha256(), 0
    try:
        with os.fdopen(descriptor, "wb") as stream:
            encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"), allow_nan=False)
            for part in encoder.iterencode(value):
                raw = part.encode("utf-8")
                stream.write(raw)
                checksum.update(raw)
                count += len(raw)
            stream.flush()
            os.fsync(stream.fileno())
        sha = checksum.hexdigest()
        target = root / (sha + ".json")
        try:
            os.link(temporary, target)
        except FileExistsError:
            pass
        if _file_hash(target) != sha or target.stat().st_size != count:
            raise ContractError("Immutable split execution evidence changed")
        return {"sha256": sha, "file": target.name, "bytes": count}
    finally:
        temporary.unlink(missing_ok=True)


def read_evidence(root, reference):
    """Explicit full-evidence inspection/reuse always checks original bytes."""
    _validate_reference(reference)
    path = Path(root) / reference["file"]
    raw = path.read_bytes()
    if len(raw) != reference["bytes"] or hashlib.sha256(raw).hexdigest() != reference["sha256"]:
        raise ContractError("Immutable split execution evidence changed")
    return json.loads(raw)


def _validate_reference(value):
    if (not isinstance(value, dict) or set(value) != {"sha256", "file", "bytes"}
            or not isinstance(value["sha256"], str) or not _SHA.fullmatch(value["sha256"])
            or value["file"] != value["sha256"] + ".json"
            or type(value["bytes"]) is not int or value["bytes"] < 1):
        raise ContractError("Exact immutable split evidence reference required")


def prepare_forecast(payload, snapshot, fitted, *, evidence_root):
    """Keep original probabilities/recipes and precompute the exact TA inputs."""
    from .starter_sizing import starter_policy
    from .technical_sizing import indicator_snapshot
    from ml.market_provenance import capture_runtime

    if digest(snapshot) != payload["snapshot_hash"]:
        raise ContractError("Prepared execution snapshot differs from its forecast")
    policy = starter_policy(payload["symbol"])
    indicators = indicator_snapshot(snapshot["candles"], symbol=policy.symbol,
        reference_at=payload["reference_at"], data_cutoff=payload["training_cutoff"],
        indicator_specs=policy.indicators, window_policy=policy.window,
        forecast_snapshot=snapshot, forecast_snapshot_hash=payload["snapshot_hash"])
    full = indicators.payload()
    indicator_evidence = seal_evidence(evidence_root, full)
    derivation_provenance = capture_runtime([
        __name__, "ml.intraday.starter_sizing", "ml.intraday.technical_sizing",
        "technicals.hyperliquid_features"], object_root=Path(evidence_root) / "source-objects")
    model_evidence = seal_evidence(evidence_root, {
        "schema": VERSION, "snapshot_hash": payload["snapshot_hash"], "symbol": payload["symbol"],
        "cycle_id": payload["cycle_id"], "reference_at": payload["reference_at"],
        "training_cutoff": payload["training_cutoff"], "fitted": fitted,
        "execution_preparation_provenance": derivation_provenance})
    compact = {key: value for key, value in full.items() if key != "candles"}
    compact.update(candles=[], full_indicator_evidence=indicator_evidence,
                   candles_storage="immutable_full_indicator_evidence")
    result = dict(payload)
    result["models"] = [{key: value for key, value in item.items() if key in _MODEL_FIELDS}
                        for item in payload["models"]]
    result["split_execution"] = {
        "schema": VERSION, "policy_hash": digest(asdict(policy)),
        "indicator_hash": digest(compact), "indicator": compact,
        "full_indicator": indicator_evidence, "full_models": model_evidence}
    return result


def execution_indicators(forecast):
    """Validate the small immutable original derivative on every consumption."""
    from .starter_sizing import starter_policy
    from .technical_sizing import IndicatorSnapshot, VERSION as INDICATOR_VERSION, _indicator_config

    original = dict(forecast)
    forecast_id = original.pop("forecast_id", None)
    if digest(original) != forecast_id:
        raise ContractError("Compact execution needs an original immutable forecast")
    record = forecast.get("split_execution")
    if (not isinstance(record, dict)
            or set(record) != {"schema", "policy_hash", "indicator_hash", "indicator", "full_indicator", "full_models"}
            or record["schema"] != VERSION):
        raise ContractError("Exact compact execution contract required")
    _validate_reference(record["full_indicator"])
    _validate_reference(record["full_models"])
    value = record["indicator"]
    policy = starter_policy(forecast["symbol"])
    if (not isinstance(value, dict) or digest(value) != record["indicator_hash"]
            or record["policy_hash"] != digest(asdict(policy))
            or value.get("version") != INDICATOR_VERSION
            or value.get("candles") != []
            or value.get("candles_storage") != "immutable_full_indicator_evidence"
            or value.get("full_indicator_evidence") != record["full_indicator"]
            or value.get("forecast_snapshot_hash") != forecast["snapshot_hash"]
            or value.get("symbol") != forecast["symbol"]
            or instant(value.get("reference_at")) != instant(forecast["reference_at"])
            or instant(value.get("data_cutoff")) != instant(forecast["training_cutoff"])
            or value.get("indicator_config") != _indicator_config(policy.indicators, policy.window)
            or value.get("indicator_config_hash") != digest(value["indicator_config"])):
        raise ContractError("Compact execution indicators changed or belong to another forecast")
    if value.get("status") == "ready":
        if (value.get("source_id") != forecast.get("source_id")
                or value.get("source_dataset") != forecast.get("source_dataset")
                or number(value.get("reference_open")) != number(forecast["reference_open"])):
            raise ContractError("Compact execution source or original reference differs")
    return IndicatorSnapshot(record["indicator_hash"], encoded(value))
