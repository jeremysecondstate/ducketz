"""Explicit, portable read-only monitoring packets for Atlas and Scout.

Packets contain projected public forecast evidence, never raw inputs, fitted
models, accounts, quantities, orders or local filesystem paths. Integrity hashes
detect changed bytes; they are not authentication. The caller owns transport and
explicit peer admission. This module starts no network service or worker.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from .prediction import ContractError, STEP, TARGET_VERSION
from ml.stock_direction_policy import (
    BEARISH_PROBABILITY, BULLISH_PROBABILITY, CURRENT_STOCK_DIRECTION_POLICY_VERSIONS,
    stock_direction_for_policy,
)

PACKET_SCHEMA = "intraday-monitoring-v1"
RECEIPT_SCHEMA = "intraday-monitoring-receipt-v1"
MAX_PACKET_BYTES = 4 * 1024 * 1024
MAX_RECEIPT_BYTES = MAX_PACKET_BYTES + 4096
PCS = {"Atlas", "Scout"}
FORECAST_MODES = {"live", "shadow", "historical_replay"}
OBSERVED_MODES = {"live", "shadow"}
_HEX = re.compile(r"[a-f0-9]{64}")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")

_FORECAST_REQUIRED = {
    "forecast_id", "pc", "symbol", "cycle_id", "reference_at", "reference_open",
    "data_through", "target_open", "target_close", "information_available_at",
    "training_cutoff", "issued_at", "recorded_at", "target_version", "mode",
    "availability_basis", "probability_not_down", "configured_count", "successful_count",
    "ensemble_status", "classification", "models", "outcome",
}
_FORECAST_OPTIONAL = {
    "preprocessing_seconds", "training_seconds", "inference_seconds", "symbol_seconds_before_persistence",
    "training_started_at", "training_completed_at", "synthetic_policy", "issue_after_target",
    "source_dataset", "source_id",
    "direction", "direction_policy_version", "direction_up_threshold", "direction_down_threshold",
}
_DIRECTION_POLICY_FIELDS = {"direction", "direction_policy_version", "direction_up_threshold", "direction_down_threshold"}
_SOURCE_IDENTITIES = {dataset: f"databento:{dataset}:ohlcv-1m" for dataset in ("EQUS.MINI", "XNAS.ITCH")}
_SOURCE_IDENTITIES["MULTI_PROVIDER"] = "multi:databento-schwab-fmp:15m"
_MODEL_REQUIRED = {"name", "status", "probability_not_down", "version"}
_MODEL_OPTIONAL = {"family", "training_seconds", "inference_seconds", "artifact_persistence_seconds",
                   "training_started_at", "training_completed_at", "inference_started_at", "inference_completed_at",
                   "artifact_persisted_at", "failure_type", "failure_phase"}
_OUTCOME_REQUIRED = {"forecast_id", "revision_id", "status", "target_not_down", "target_close_price",
                     "observed_at", "availability_basis"}
_CYCLE_REQUIRED = {"cycle_id", "pc", "reference_at", "mode", "started_at", "status"}
_CYCLE_OPTIONAL = {"completed_at", "candle_close", "scheduled_acquisition", "training_cutoff", "full_retrain",
                   "compute_persist_seconds", "acquisition_seconds", "total_cycle_seconds", "successful_symbols",
                   "failed_symbols", "process_cpu_seconds", "exceeds_15m", "overrun_seconds", "failed_symbol_names",
                   "measurement_scope", "cumulative_attempt_seconds", "wall_elapsed_seconds",
                   "wall_exceeds_15m", "wall_overrun_seconds"}
_CYCLE_MEASUREMENT_SCOPES = {"cumulative_attempt_work", "wall_elapsed_with_cumulative_attempt_work_v1"}


def _canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError) as exc:
        raise ContractError("Monitoring payload must contain finite JSON values") from exc


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _pc(value):
    if not isinstance(value, str) or value not in PCS:
        raise ContractError("Unknown monitoring PC; expected Atlas or Scout")
    return value


def _time(value):
    if not isinstance(value, str) or len(value) > 48:
        raise ContractError("Monitoring timestamps must be UTC ISO strings")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("Invalid monitoring timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ContractError("Monitoring timestamps must carry UTC timezone information")
    return parsed.astimezone(timezone.utc)


def _as_of(value):
    """Caller clocks may be aware datetime objects; persisted fields stay strict."""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ContractError("Monitoring caller clock must be timezone aware")
        return value.astimezone(timezone.utc)
    return _time(value)


def _token(value, label):
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ContractError(f"Invalid monitoring {label}")
    return value


def _hash(value, label):
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise ContractError(f"Invalid monitoring {label} hash")
    return value


def _numeric(value, label, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (float, int, str)):
        raise ContractError(f"Invalid monitoring {label}")
    if isinstance(value, str) and not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", value):
        raise ContractError(f"Invalid monitoring numeric {label}")
    try:
        number = float(value)
    except (ValueError, OverflowError) as exc:
        raise ContractError(f"Invalid monitoring {label}") from exc
    if not math.isfinite(number) or number < 0 or (positive and number == 0):
        raise ContractError(f"Invalid monitoring {label}")
    return number


def _count(value, label, *, positive=False, maximum=10000):
    if type(value) is not int or not (1 if positive else 0) <= value <= maximum:
        raise ContractError(f"Invalid monitoring {label} count")
    return value


def _keys(value, required, optional=()):
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - set(optional):
        raise ContractError("Unknown, private or missing monitoring fields")


def _operational_fields(value, produced):
    for key, val in value.items():
        if key.endswith("_seconds"):
            _numeric(val, key)
        elif key.endswith("_at") and val is not None:
            if _time(val) > produced:
                raise ContractError("Operational monitoring timestamp is after packet production")


def _validate_forecast(forecast, *, pc, produced):
    _keys(forecast, _FORECAST_REQUIRED, _FORECAST_OPTIONAL)
    if "source_dataset" in forecast or "source_id" in forecast:
        dataset = forecast.get("source_dataset")
        if (not isinstance(dataset, str) or dataset not in _SOURCE_IDENTITIES
                or forecast.get("source_id") != _SOURCE_IDENTITIES[dataset]):
            raise ContractError("Monitoring source identity must name a supported dataset and its matching portable source")
    _hash(forecast["forecast_id"], "forecast identity")
    _token(forecast["cycle_id"], "cycle identity")
    if forecast["pc"] != pc:
        raise ContractError("Monitoring forecast belongs to a different producer PC")
    if not isinstance(forecast["symbol"], str) or not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,15}", forecast["symbol"]):
        raise ContractError("Invalid monitoring symbol")
    reference = _time(forecast["reference_at"])
    issue = _time(forecast["issued_at"])
    if reference.minute % 15 or reference.second or reference.microsecond:
        raise ContractError("Monitoring reference must be a quarter-hour candle opening")
    if (_time(forecast["data_through"]) != reference + STEP or _time(forecast["target_open"]) != reference + 2 * STEP
            or _time(forecast["target_close"]) != reference + 3 * STEP or issue < reference + STEP):
        raise ContractError("Monitoring forecast changed its fixed intraday target")
    if (_time(forecast["information_available_at"]) > issue or _time(forecast["training_cutoff"]) > issue
            or _time(forecast["recorded_at"]) > produced or issue > produced):
        raise ContractError("Monitoring forecast contains future information")
    if forecast["target_version"] != TARGET_VERSION:
        raise ContractError("Monitoring target version is incompatible")
    _numeric(forecast["reference_open"], "reference price", positive=True)
    if forecast["mode"] not in FORECAST_MODES:
        raise ContractError("Unknown monitoring forecast mode")
    if forecast["availability_basis"] not in {"observed", "assumed_historical"}:
        raise ContractError("Unknown monitoring information availability")
    if forecast["mode"] in OBSERVED_MODES and forecast["availability_basis"] != "observed":
        raise ContractError("Historical assumptions cannot masquerade as shadow or live availability")
    _count(forecast["configured_count"], "configured models", positive=True, maximum=128)
    _count(forecast["successful_count"], "successful models", positive=True, maximum=128)
    models = forecast["models"]
    if not isinstance(models, list) or len(models) != forecast["configured_count"]:
        raise ContractError("Monitoring requires every configured individual model output")
    successes, exact_probabilities, names = [], [], set()
    for model in models:
        _keys(model, _MODEL_REQUIRED, _MODEL_OPTIONAL)
        name = _token(model["name"], "model name")
        if name in names:
            raise ContractError("Duplicate monitoring model identity")
        names.add(name)
        if model["status"] not in {"success", "failed"}:
            raise ContractError("Unknown monitoring model status")
        if model["version"] is not None:
            _hash(model["version"], "model version")
        if model["status"] == "success":
            if model["version"] is None:
                raise ContractError("Successful monitoring model needs a fitted version")
            probability = _numeric(model["probability_not_down"], "model probability")
            if probability > 1:
                raise ContractError("Model probability is outside zero to one")
            successes.append(probability)
            exact_probabilities.append(Decimal(str(model["probability_not_down"])))
        elif model["probability_not_down"] is not None:
            raise ContractError("Failed model cannot contain a fake probability")
        for key in ("family", "failure_type", "failure_phase"):
            if key in model:
                _token(model[key], key)
        _operational_fields(model, produced)
    if len(successes) != forecast["successful_count"]:
        raise ContractError("Monitoring successful model count is inconsistent")
    mean = sum(successes) / len(successes)
    if abs(_numeric(forecast["probability_not_down"], "ensemble probability") - mean) > 1e-10:
        raise ContractError("Monitoring consensus differs from equal arithmetic mean")
    status = "full" if len(successes) == len(models) else "incomplete"
    exact_mean = sum(exact_probabilities) / len(exact_probabilities)
    if forecast["ensemble_status"] != status or forecast["classification"] != ("not-down" if exact_mean >= Decimal("0.50") else "down"):
        raise ContractError("Monitoring consensus classification or completeness is inconsistent")
    if _DIRECTION_POLICY_FIELDS & forecast.keys():
        if not _DIRECTION_POLICY_FIELDS <= forecast.keys():
            raise ContractError("Monitoring direction policy metadata is incomplete")
        version = forecast["direction_policy_version"]
        cutoffs = {"stock-direction-50-v2": (.5, .5),
                   **{identity: (BULLISH_PROBABILITY, BEARISH_PROBABILITY)
                      for identity in CURRENT_STOCK_DIRECTION_POLICY_VERSIONS}}
        if (not isinstance(version, str) or version not in cutoffs
                or any(type(forecast[field]) not in (int, float) or forecast[field] != expected
                       for field, expected in zip(("direction_up_threshold", "direction_down_threshold"), cutoffs[version]))
                or forecast["direction"] != stock_direction_for_policy(forecast["probability_not_down"], version)):
            raise ContractError("Monitoring direction policy differs from its recorded probability and cutoffs")
    _operational_fields({k: forecast[k] for k in _FORECAST_OPTIONAL if k in forecast}, produced)
    if "synthetic_policy" in forecast and forecast["synthetic_policy"] not in {"include", "exclude"}:
        raise ContractError("Unknown monitoring synthetic policy")
    if "issue_after_target" in forecast and forecast["issue_after_target"] is not (issue >= reference + 3 * STEP):
        raise ContractError("Monitoring late-issue marker differs from fixed target")
    outcome = forecast["outcome"]
    if outcome is not None:
        _keys(outcome, _OUTCOME_REQUIRED)
        if outcome["forecast_id"] != forecast["forecast_id"]:
            raise ContractError("Monitoring outcome belongs to a different immutable forecast")
        _hash(outcome["revision_id"], "outcome revision")
        if outcome["status"] not in {"pending", "missing", "unscorable", "scored", "synthetic"}:
            raise ContractError("Unknown monitoring outcome status")
        observed = _time(outcome["observed_at"])
        if observed > produced:
            raise ContractError("Monitoring outcome observation is in the future")
        if outcome["availability_basis"] not in {None, "actual", "assumed_historical_close"}:
            raise ContractError("Unknown monitoring outcome availability")
        if outcome["status"] in {"scored", "synthetic"}:
            if type(outcome["target_not_down"]) is not bool or observed < reference + 3 * STEP:
                raise ContractError("Monitoring score needs a completed exact target")
            _numeric(outcome["target_close_price"], "target outcome price", positive=True)
            not_down = Decimal(str(outcome["target_close_price"])) >= Decimal(str(forecast["reference_open"]))
            if outcome["target_not_down"] is not not_down:
                raise ContractError("Monitoring outcome equality must classify as not-down")
            if forecast["mode"] in OBSERVED_MODES and outcome["availability_basis"] != "actual":
                raise ContractError("Monitoring shadow or live outcome needs actual availability")
        elif outcome["target_not_down"] is not None or outcome["target_close_price"] is not None:
            raise ContractError("Pending or missing monitoring target cannot have a fabricated score")


def validate_packet(packet, *, expected_peer=None, as_of=None):
    _keys(packet, {"schema_version", "packet_id", "payload"})
    if packet["schema_version"] != PACKET_SCHEMA:
        raise ContractError("Unsupported monitoring packet schema")
    _hash(packet["packet_id"], "packet identity")
    payload = packet["payload"]
    _keys(payload, {"producer_pc", "produced_at", "source_version", "forecasts", "cycles"})
    pc = _pc(payload["producer_pc"])
    if expected_peer is not None and pc != _pc(expected_peer):
        raise ContractError("Monitoring packet was not admitted for the expected peer")
    _token(payload["source_version"], "source version")
    produced = _time(payload["produced_at"])
    if as_of is not None and produced > _as_of(as_of):
        raise ContractError("Monitoring packet production is in the future")
    if packet["packet_id"] != _digest(payload):
        raise ContractError("Monitoring packet hash does not match its canonical payload")
    if len(_canonical(packet)) > MAX_PACKET_BYTES:
        raise ContractError("Monitoring packet exceeds the bounded size")
    forecasts, cycles = payload["forecasts"], payload["cycles"]
    if not isinstance(forecasts, list) or len(forecasts) > 256 or not isinstance(cycles, list) or len(cycles) > 32:
        raise ContractError("Monitoring snapshot exceeds forecast/cycle bounds")
    names = set()
    for forecast in forecasts:
        _validate_forecast(forecast, pc=pc, produced=produced)
        key = (forecast["symbol"], forecast["mode"], forecast.get("source_dataset"))
        if key in names:
            raise ContractError("Monitoring snapshot repeats a latest symbol/mode/source dataset")
        names.add(key)
    cycle_ids = set()
    for cycle in cycles:
        _keys(cycle, _CYCLE_REQUIRED, _CYCLE_OPTIONAL)
        _token(cycle["cycle_id"], "cycle identity")
        if cycle["cycle_id"] in cycle_ids or cycle["pc"] != pc:
            raise ContractError("Monitoring cycle producer or identity is inconsistent")
        cycle_ids.add(cycle["cycle_id"])
        if cycle["mode"] not in FORECAST_MODES or cycle["status"] not in {"running", "completed", "partial_failure"}:
            raise ContractError("Unknown monitoring cycle mode/status")
        reference = _time(cycle["reference_at"])
        if reference.minute % 15 or reference.second or reference.microsecond:
            raise ContractError("Monitoring cycle reference must be a quarter-hour")
        _operational_fields({k: v for k, v in cycle.items() if k != "reference_at"}, produced)
        if "candle_close" in cycle and _time(cycle["candle_close"]) != reference + STEP:
            raise ContractError("Monitoring cycle candle close changed its reference interval")
        if "training_cutoff" in cycle and not reference + STEP <= _time(cycle["training_cutoff"]) <= produced:
            raise ContractError("Monitoring cycle training cutoff is unavailable")
        if "scheduled_acquisition" in cycle and not reference + STEP <= _time(cycle["scheduled_acquisition"]) < reference + 2 * STEP:
            raise ContractError("Monitoring cycle acquisition must use its anchored boundary offset")
        if "completed_at" in cycle and _time(cycle["completed_at"]) < _time(cycle["started_at"]):
            raise ContractError("Monitoring cycle cannot complete before it started")
        for key in ("successful_symbols", "failed_symbols"):
            if key in cycle:
                _count(cycle[key], key, maximum=256)
        if "failed_symbol_names" in cycle:
            values = cycle["failed_symbol_names"]
            if (not isinstance(values, list) or len(values) > 256
                    or any(not isinstance(v, str) or not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,15}", v) for v in values)):
                raise ContractError("Invalid monitoring failure symbol list")
        if "measurement_scope" in cycle and (not isinstance(cycle["measurement_scope"], str)
                or cycle["measurement_scope"] not in _CYCLE_MEASUREMENT_SCOPES):
            raise ContractError("Unknown monitoring cycle measurement scope")
        for key in ("full_retrain", "exceeds_15m", "wall_exceeds_15m"):
            if key in cycle and type(cycle[key]) is not bool:
                raise ContractError("Invalid monitoring boolean marker")
    return packet


def _path(value, *, suffix=None, must_exist=False):
    raw = str(value)
    if not raw or "\x00" in raw or raw.startswith(("\\\\", "//")) or "://" in raw:
        raise ContractError("Monitoring paths must be explicit local absolute paths")
    path = Path(value)
    if not path.is_absolute() or (suffix and path.suffix.lower() != suffix):
        raise ContractError("Monitoring path must be absolute with the expected file extension")
    for component in (path, *path.parents):
        if component.is_symlink() or (hasattr(component, "is_junction") and component.is_junction()):
            raise ContractError("Monitoring paths cannot traverse symbolic links or junctions")
    if must_exist and not path.is_file():
        raise ContractError("Monitoring input file is missing")
    return path.resolve()


def _read(path, *, maximum):
    path = _path(path, suffix=".json", must_exist=True)
    if path.stat().st_size > maximum:
        raise ContractError("Monitoring input exceeds the bounded size")
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ContractError("Monitoring input exceeds the bounded size")
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ContractError("Duplicate monitoring JSON key")
            value[key] = item
        return value
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=unique_object,
                          parse_constant=lambda value: (_ for _ in ()).throw(ContractError("Non-finite monitoring JSON")))
    except ContractError:
        raise
    except (ValueError, UnicodeError) as exc:
        raise ContractError("Invalid monitoring JSON") from exc


def _write_exclusive(path, value, *, maximum):
    path = _path(path, suffix=".json")
    data = _canonical(value)
    if len(data) > maximum:
        raise ContractError("Monitoring output exceeds the bounded size")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".monitoring-", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def export_packet(database_path, *, producer_pc, source_version, produced_at=None):
    """Read the existing DB without opening writable connections or private tables."""
    pc = _pc(producer_pc)
    produced_at = produced_at or _now()
    _time(produced_at)
    database = _path(database_path, must_exist=True)
    forecasts, cycles, keys = [], [], set()
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        # The WHERE and ordered source identities isolate this PC and keep replay,
        # live, shadow and source datasets separate. Private inputs/intents/orders are never queried.
        for (payload,) in db.execute("SELECT payload FROM forecasts WHERE pc=? ORDER BY issued_at DESC, rowid DESC", (pc,)):
            value = json.loads(payload)
            key = (value["symbol"], value["mode"], value.get("source_dataset"))
            if key in keys:
                continue
            keys.add(key)
            if len(keys) > 256:
                raise ContractError("Local monitoring symbol/mode scope exceeds its bounded snapshot")
            forecast = {k: value[k] for k in _FORECAST_REQUIRED | _FORECAST_OPTIONAL if k in value and k not in {"models", "outcome"}}
            forecast["models"] = []
            for model in value["models"]:
                projected = {k: model[k] for k in _MODEL_REQUIRED | _MODEL_OPTIONAL if k in model}
                projected.setdefault("version", None)
                error = model.get("error")
                if isinstance(error, dict):
                    for source_key, destination in (("type", "failure_type"), ("phase", "failure_phase")):
                        candidate = error.get(source_key)
                        if isinstance(candidate, str) and _TOKEN.fullmatch(candidate):
                            projected[destination] = candidate
                forecast["models"].append(projected)
            outcome = db.execute("SELECT payload FROM outcomes WHERE forecast_id=? ORDER BY rowid DESC LIMIT 1", (value["forecast_id"],)).fetchone()
            observation = json.loads(outcome[0]) if outcome else None
            forecast["outcome"] = {k: observation[k] for k in _OUTCOME_REQUIRED} if observation else None
            forecasts.append(forecast)
        for identity, payload in db.execute("SELECT id,payload FROM cycles WHERE pc=? ORDER BY started_at DESC,rowid DESC LIMIT 32", (pc,)):
            value = json.loads(payload)
            cycle = {k: value[k] for k in _CYCLE_REQUIRED | _CYCLE_OPTIONAL if k in value}
            cycle["cycle_id"] = identity
            if value.get("failures"):
                cycle["failed_symbol_names"] = sorted(value["failures"])
            cycles.append(cycle)
    payload = {"producer_pc": pc, "produced_at": produced_at, "source_version": source_version,
               "forecasts": forecasts, "cycles": cycles}
    packet = {"schema_version": PACKET_SCHEMA, "packet_id": _digest(payload), "payload": payload}
    return validate_packet(packet, as_of=produced_at)


def write_packet(packet, output_path):
    validate_packet(packet)
    return _write_exclusive(output_path, packet, maximum=MAX_PACKET_BYTES)


def validate_receipt(receipt, *, expected_peer, local_pc, as_of=None):
    _keys(receipt, {"schema_version", "packet_id", "producer_pc", "local_pc", "received_at", "packet"})
    if receipt["schema_version"] != RECEIPT_SCHEMA:
        raise ContractError("Unsupported monitoring receipt schema")
    if _pc(local_pc) == _pc(expected_peer) or receipt["local_pc"] != local_pc or receipt["producer_pc"] != expected_peer:
        raise ContractError("Monitoring receipt local/peer admission is inconsistent")
    received = _time(receipt["received_at"])
    if as_of is not None and received > _as_of(as_of):
        raise ContractError("Monitoring receipt claims a future reception")
    validate_packet(receipt["packet"], expected_peer=expected_peer, as_of=receipt["received_at"])
    if receipt["packet_id"] != receipt["packet"]["packet_id"]:
        raise ContractError("Monitoring receipt changed the original packet identity")
    return receipt


def _check_conflicts(receipt, directory, *, expected_peer, local_pc):
    new_forecasts = {f["forecast_id"]: f for f in receipt["packet"]["payload"]["forecasts"]}
    new_cycles = {c["cycle_id"]: c for c in receipt["packet"]["payload"]["cycles"]}
    for path in directory.glob("*.json"):
        if not _HEX.fullmatch(path.stem):
            raise ContractError("Unknown file in immutable monitoring receipt namespace")
        previous = validate_receipt(_read(path, maximum=MAX_RECEIPT_BYTES), expected_peer=expected_peer, local_pc=local_pc)
        if previous["packet_id"] != path.stem:
            raise ContractError("Immutable monitoring receipt filename differs from its packet identity")
        for old in previous["packet"]["payload"]["forecasts"]:
            new = new_forecasts.get(old["forecast_id"])
            if new is None:
                continue
            if {k: v for k, v in old.items() if k != "outcome"} != {k: v for k, v in new.items() if k != "outcome"}:
                raise ContractError("Conflicting bytes for an immutable peer forecast identity")
            old_outcome, new_outcome = old["outcome"], new["outcome"]
            if (old_outcome and new_outcome and old_outcome["revision_id"] == new_outcome["revision_id"]
                    and old_outcome != new_outcome):
                raise ContractError("Conflicting bytes for an immutable peer outcome revision")
        for old in previous["packet"]["payload"]["cycles"]:
            new = new_cycles.get(old["cycle_id"])
            if new and any(old[key] != new[key] for key in ("pc", "reference_at", "mode", "started_at")):
                raise ContractError("Conflicting immutable peer cycle identity; only progress may advance")


def import_packet(packet_path, receipts_root, *, expected_peer, local_pc, received_at=None):
    """Explicit local file admission; duplicates retain the first reception time."""
    received_at = received_at or _now()
    if _pc(expected_peer) == _pc(local_pc):
        raise ContractError("A remote monitoring peer must differ from this PC")
    packet = validate_packet(_read(packet_path, maximum=MAX_PACKET_BYTES), expected_peer=expected_peer, as_of=received_at)
    receipt = {"schema_version": RECEIPT_SCHEMA, "packet_id": packet["packet_id"], "producer_pc": expected_peer,
               "local_pc": local_pc, "received_at": received_at, "packet": packet}
    validate_receipt(receipt, expected_peer=expected_peer, local_pc=local_pc)
    root = _path(receipts_root)
    root.mkdir(parents=True, exist_ok=True)
    # This is a host-local transport receipt lock, not account coordination.
    # Native FileLock ownership is released by the OS if an importer crashes.
    # Import lazily so read-only UI validation needs only the standard library.
    from filelock import FileLock
    with FileLock(str(_path(root / "monitoring-import.lock"))):
        return _accept_receipt(receipt, root / expected_peer.lower(), expected_peer=expected_peer, local_pc=local_pc)


def _accept_receipt(receipt, directory, *, expected_peer, local_pc):
    """Conflict scan and immutable publication occur under the same local lock."""
    packet = receipt["packet"]
    path = directory / f"{packet['packet_id']}.json"
    if path.exists():
        old = validate_receipt(_read(path, maximum=MAX_RECEIPT_BYTES), expected_peer=expected_peer, local_pc=local_pc)
        if old["packet"] != packet:
            raise ContractError("Immutable monitoring packet identity has conflicting received bytes")
        return {"receipt": old, "receipt_path": str(path), "duplicate": True}
    _check_conflicts(receipt, directory, expected_peer=expected_peer, local_pc=local_pc)
    try:
        _write_exclusive(path, receipt, maximum=MAX_RECEIPT_BYTES)
    except FileExistsError:
        old = validate_receipt(_read(path, maximum=MAX_RECEIPT_BYTES), expected_peer=expected_peer, local_pc=local_pc)
        if old["packet"] != packet:
            raise ContractError("Concurrent monitoring reception changed immutable packet bytes")
        return {"receipt": old, "receipt_path": str(path), "duplicate": True}
    return {"receipt": receipt, "receipt_path": str(path), "duplicate": False}


def load_receipt(receipt_path, *, expected_peer, local_pc, as_of=None, stale_after_seconds=900):
    """Read one explicit validated receipt; never infer freshness from file mtime."""
    as_of = as_of or _now()
    now = _as_of(as_of)
    threshold = _numeric(stale_after_seconds, "freshness threshold", positive=True)
    receipt = validate_receipt(_read(receipt_path, maximum=MAX_RECEIPT_BYTES), expected_peer=expected_peer,
                               local_pc=local_pc, as_of=as_of)
    payload = receipt["packet"]["payload"]
    producer_age = (now - _time(payload["produced_at"])).total_seconds()
    receipt_age = (now - _time(receipt["received_at"])).total_seconds()
    observed = [f for f in payload["forecasts"] if f["mode"] in OBSERVED_MODES]
    latest = max(observed, key=lambda f: _time(f["issued_at"])) if observed else None
    forecast_age = (now - _time(latest["issued_at"])).total_seconds() if latest else None
    status = ("fresh" if producer_age <= threshold and forecast_age <= threshold else "stale") if latest else (
        "historical_only" if payload["forecasts"] else "empty")
    return {"receipt": receipt, "status": status, "producer_age_seconds": producer_age,
            "receipt_age_seconds": receipt_age, "forecast_age_seconds": forecast_age,
            "latest_forecast_issue": latest["issued_at"] if latest else None,
            "latest_forecast_mode": latest["mode"] if latest else None,
            "latest_recorded_at": max((f["recorded_at"] for f in payload["forecasts"]), key=_time, default=None)}


def latest_received(receipts_root, *, expected_peer, local_pc, as_of=None, stale_after_seconds=900):
    """Order receipts by original production, so delayed old packets cannot regress."""
    directory = _path(receipts_root) / _pc(expected_peer).lower()
    candidates = []
    for path in directory.glob("*.json"):
        receipt = validate_receipt(_read(path, maximum=MAX_RECEIPT_BYTES), expected_peer=expected_peer, local_pc=local_pc, as_of=as_of)
        if not _HEX.fullmatch(path.stem) or receipt["packet_id"] != path.stem:
            raise ContractError("Immutable monitoring receipt filename differs from its packet identity")
        candidates.append((_time(receipt["packet"]["payload"]["produced_at"]), receipt["packet_id"], path))
    if not candidates:
        return None
    return load_receipt(max(candidates)[2], expected_peer=expected_peer, local_pc=local_pc, as_of=as_of,
                        stale_after_seconds=stale_after_seconds)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--database", required=True, type=Path)
    export.add_argument("--pc", required=True, choices=sorted(PCS))
    export.add_argument("--source-version", required=True)
    export.add_argument("--output", required=True, type=Path)
    receive = commands.add_parser("import")
    receive.add_argument("--packet", required=True, type=Path)
    receive.add_argument("--receipts-root", required=True, type=Path)
    receive.add_argument("--expected-peer", required=True, choices=sorted(PCS))
    receive.add_argument("--local-pc", required=True, choices=sorted(PCS))
    args = parser.parse_args(argv)
    if args.command == "export":
        packet = export_packet(args.database, producer_pc=args.pc, source_version=args.source_version)
        output = write_packet(packet, args.output)
        print(json.dumps({"packet_id": packet["packet_id"], "output": str(output), "forecasts": len(packet["payload"]["forecasts"])}))
    else:
        result = import_packet(args.packet, args.receipts_root, expected_peer=args.expected_peer, local_pc=args.local_pc)
        print(json.dumps({k: v for k, v in result.items() if k != "receipt"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
