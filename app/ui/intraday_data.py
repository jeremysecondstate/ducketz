"""Read-only projections of the auditable intraday forecast store.

This module deliberately has no training, acquisition, broker or write path.
Metrics use the latest saved outcome revision and never the live market price.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc
OUTCOME_STATES = ("scored", "pending", "missing", "unscorable", "synthetic")
WINDOWS = ("Today", "Last 20", "Last 50", "Last 100", "7 days", "Cumulative")
FORECAST_MODES = ("live", "shadow", "historical_replay", "All")
DEFAULT_FORECAST_MODE = "live"
DEFAULT_METRIC_SCOPE = "Before target open · observed"
METRIC_SCOPES = (
    DEFAULT_METRIC_SCOPE,
    "Before target open · all outcomes",
    "During target · observed",
    "During target · all outcomes",
    "After target close · observed",
    "After target close · all outcomes",
    "Timing unknown · all outcomes",
    "All saved outcomes",
)


# Forecast originals can contain hundreds of megabytes of training metadata.
# Project only fields consumed by this read-only display inside SQLite, before
# Python sees a row. All issues and original score inputs remain represented;
# this is not a history limit or a replacement of the scientific evidence.
DISPLAY_FORECAST_FIELDS = (
    "availability_basis", "classification", "configured_count", "cycle_id", "data_through",
    "direction", "direction_down_threshold", "direction_policy_version", "direction_up_threshold",
    "ensemble_status", "execution_blocked_reason", "execution_eligibility", "forecast_id",
    "inference_seconds", "information_available_at", "issued_at", "mode", "pc", "population_contract",
    "preprocessing_seconds", "probability_not_down", "reference_at", "reference_open", "reference_provenance",
    "regime", "regime_measurements", "snapshot_hash", "source_dataset", "source_id", "successful_count", "symbol",
    "target_close", "target_open", "target_version", "training_completed_at", "training_seconds", "synthetic_policy",
)
DISPLAY_MODEL_FIELDS = (
    "name", "family", "probability_not_down", "status", "version", "error", "warnings",
    "artifact_path", "artifact_sha256", "artifact_persisted_at", "artifact_persistence_seconds",
    "training_started_at", "training_completed_at", "training_seconds", "failed_at",
    "inference_started_at", "inference_completed_at", "inference_seconds",
)
DISPLAY_INTENT_FIELDS = (
    "intended_direction", "direction", "side", "desired_quantity", "adjusted_quantity",
    "capacity_adjusted_quantity", "disposition", "execution_disposition",
)
DISPLAY_CYCLE_FIELDS = (
    "mode", "status", "timings", "latency", "resources", "resources_before", "resources_after", "acquisition",
    "failures", "symbols", "failed_symbol_names", "reference_at", "finished_at", "completed_at", "workload", "successful_symbols", "failed_symbols",
    "total_cycle_seconds", "compute_cycle_seconds", "compute_persist_seconds", "acquisition_seconds",
    "process_cpu_seconds", "exceeds_15m", "overrun_seconds", "measurement_scope",
    "cumulative_attempt_seconds", "wall_elapsed_seconds", "wall_exceeds_15m", "wall_overrun_seconds",
    "attempt_count", "latest_attempt",
)


def _json_projection(column, fields, *, path_expression=None):
    keys = ",".join("'" + field + "'" for field in fields)
    # json_each retains presence, unlike a multi-path json_extract array that
    # conflates missing fields with explicitly saved JSON null. Read each raw
    # JSON value with -> instead of reserializing SQLite REAL: its decimal
    # rounding can move a saved probability across the classification boundary.
    path = "j.key" if path_expression is None else "(" + path_expression + " || '.' || j.key)"
    arguments = column if path_expression is None else column + "," + path_expression
    return ("(SELECT json_group_object(j.key, json(" + column + " -> " + path + ")) "
            "FROM json_each(" + arguments + ") AS j WHERE j.key IN (" + keys + "))")


# Keep exact saved specifications/settings without pulling their surrounding
# scientific trees. Oversized selected fields stay explicitly unavailable.
DISPLAY_MODEL_METADATA_FIELDS = (
    "spec", "effective_parameters", "calibration", "target_version", "feature_names",
    "training_rows", "training_cutoff", "training_window", "history_limit", "synthetic_policy",
)
DISPLAY_MODEL_SELECTION_FIELDS = ("selection", "selection_id", "active_policy_sha256", "policy_sha256", "models")
MAX_SAVED_SETTING_CHARS = 64 * 1024


def _settings_projection(column, *, fields=DISPLAY_MODEL_METADATA_FIELDS, path="$.metadata", path_expression=None):
    keys = ",".join("'" + field + "'" for field in fields)
    selected_path = path_expression if path_expression is not None else "'" + path + "'"
    return ("(SELECT json_group_object(j.key, CASE "
            "WHEN length(CAST(j.value AS TEXT)) > " + str(MAX_SAVED_SETTING_CHARS)
            + " THEN json('{\"display_status\":\"unavailable\",\"reason\":\"saved setting exceeds display limit; original retained in source database\"}') "
            "ELSE json(" + column + " -> (" + selected_path + " || '.' || j.key)) END) "
            "FROM json_each(" + column + "," + selected_path + ") AS j WHERE j.key IN (" + keys + "))")


def _display_payload(raw, fields):
    values = json.loads(raw)
    if not isinstance(values, dict) or set(values) - set(fields):
        raise ValueError("Saved display projection is malformed")
    return values


def _has_jsonb(connection):
    try:
        connection.execute("SELECT jsonb('{}')").fetchone()
        return True
    except sqlite3.OperationalError as exc:
        if "no such function" not in str(exc).lower():
            raise
        return False


def _forecast_display_rows(connection, query, *, use_jsonb):
    # Older SQLite versions keep the original text reader. The feature probe
    # changes only this connection and never creates an operating dependency.
    if not use_jsonb:
        yield from connection.execute(query)
        return
    # Parse one exact saved issue once. Materializing all history would retain
    # its full scientific payloads in memory; the identity inventory contains
    # only small keys, and the CTE reads each payload by its existing index.
    bounded_query = ("WITH display_source AS MATERIALIZED (SELECT id,pc,symbol,cycle_id,reference_at,issued_at,"
                     "jsonb(payload) AS payload FROM forecasts WHERE id=?) "
                     + query.replace("FROM forecasts AS f ORDER BY f.issued_at,f.id", "FROM display_source AS f"))
    for identity in connection.execute("SELECT id FROM forecasts ORDER BY issued_at,id"):
        yield from connection.execute(bounded_query, (identity[0],))


def _display_forecasts(connection, *, use_jsonb=None):
    use_jsonb = _has_jsonb(connection) if use_jsonb is None else use_jsonb
    scalars = _json_projection("f.payload", DISPLAY_FORECAST_FIELDS)
    # Address each model inside the parsed issue by key. Referencing m.value
    # would serialize and reparse its potentially huge scientific metadata.
    model_path = "'$.models[' || m.key || ']'"
    models = _json_projection("f.payload", DISPLAY_MODEL_FIELDS, path_expression=model_path)
    settings = _settings_projection("f.payload", path_expression="'$.models[' || m.key || '].metadata'")
    selection = _settings_projection("f.payload", fields=DISPLAY_MODEL_SELECTION_FIELDS, path="$.model_selection")
    # Read historical setup from this forecast, never today's recipe. These
    # scalar paths exclude full dataset manifests and selection/science trees.
    context = ("json_extract(f.payload,'$.dataset_manifest.synthetic_policy',"
               "'$.dataset_manifest.manual_model_recipe.schema_version')")
    # The scalar projection and each model summary exclude feature matrices,
    # selection trees and all unrelated nested model metadata.
    query = ("SELECT f.id,f.pc,f.symbol,f.cycle_id,f.reference_at,f.issued_at,json_type(f.payload) AS payload_type," + scalars + " AS display," + context + " AS saved_context," + selection + " AS selection,"
             "(SELECT json_group_array(json(" + models + ")) FROM json_each(f.payload,'$.models') AS m) AS models,"
             "(SELECT json_group_array(json(" + settings + ")) FROM json_each(f.payload,'$.models') AS m) AS settings "
             "FROM forecasts AS f ORDER BY f.issued_at,f.id")
    for row in _forecast_display_rows(connection, query, use_jsonb=use_jsonb):
        if row["payload_type"] != "object":
            raise ValueError("Saved payload must be a JSON object")
        value = _display_payload(row["display"], DISPLAY_FORECAST_FIELDS)
        value["models"] = [dict(_display_payload(json.dumps(model), DISPLAY_MODEL_FIELDS),
                                saved_evidence_note="Display summary; full original model evidence is retained in the source database.")
                           for model in json.loads(row["models"])]
        for output, metadata in zip(value["models"], json.loads(row["settings"])):
            if metadata:
                output["metadata"] = metadata
        value["model_selection"] = json.loads(row["selection"])
        specs = value["model_selection"].get("models", [])
        specs = {spec["name"]: spec for spec in specs if isinstance(spec, dict) and isinstance(spec.get("name"), str)} if isinstance(specs, list) else {}
        for output in value["models"]:
            if "spec" not in output.get("metadata", {}) and output.get("name") in specs:
                output.setdefault("metadata", {})["spec"] = specs[output["name"]]
                output["saved_setting_source"] = "forecast.model_selection.models"
        policy, recipe_schema = json.loads(row["saved_context"])
        value["dataset_manifest"] = {"synthetic_policy": policy,
            "manual_model_recipe": {"schema_version": recipe_schema}}
        yield row, value


@dataclass(frozen=True)
class IntradaySnapshot:
    loaded_at: datetime
    forecasts: tuple[dict[str, Any], ...] = ()
    cycles: tuple[dict[str, Any], ...] = ()
    sources: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()


def timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    else:
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    return result.astimezone(UTC) if result.tzinfo is not None else None


def probability(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) and 0 <= result <= 1 else None


def _unknown_reference_provenance(reason: str) -> dict[str, Any]:
    return {"status": "unknown", "reason": reason, "observed_constituents": None,
            "synthetic_constituents": None}


def _minute_count(value: Any) -> int | None:
    # Concatenated historical frames can serialize an integer count as 15.0.
    # Accept that exact value, without truncating fractions or accepting bools.
    if type(value) is int and 0 <= value <= 15:
        return value
    if type(value) is float and math.isfinite(value) and value.is_integer() and 0 <= value <= 15:
        return int(value)
    return None


def _validated_reference_provenance(value: Any, forecast: Mapping[str, Any], *, basis: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return _unknown_reference_provenance("malformed saved input provenance")
    observed, synthetic = (_minute_count(value.get(key)) for key in ("observed_constituents", "synthetic_constituents"))
    if observed is None or synthetic is None or observed + synthetic != 15:
        return _unknown_reference_provenance("minute counts were not recorded or are invalid")
    if (type(value.get("synthetic")) is not bool or type(value.get("no_trade")) is not bool
            or value["synthetic"] != (synthetic > 0) or value["no_trade"] != (synthetic == 15)):
        return _unknown_reference_provenance("saved no-trade flags differ from the minute counts")
    try:
        expected, reference = timestamp(forecast.get("reference_at")), timestamp(value.get("reference_at"))
    except (OverflowError, ValueError):
        return _unknown_reference_provenance("saved input reference time is invalid")
    if expected is None or reference is None or expected != reference:
        return _unknown_reference_provenance("saved input provenance differs from the reference candle")
    source = value.get("source_id")
    if source is not None and forecast.get("source_id") is not None and source != forecast["source_id"]:
        return _unknown_reference_provenance("saved input provenance differs from the source")
    return {"status": "known", "reference_at": reference.isoformat(),
            "observed_constituents": observed, "synthetic_constituents": synthetic,
            "synthetic": synthetic > 0, "no_trade": synthetic == 15,
            "availability_kind": value.get("availability_kind"), "basis": basis,
            "source_dataset": forecast.get("source_dataset"),
            "source_id": source or forecast.get("source_id")}


def reference_input_provenance(forecast: Mapping[str, Any], *, database_path: Path | None = None) -> dict[str, Any]:
    """Read one chosen forecast's reference metadata without loading fitted inputs.

    New forecasts carry their own immutable summary. Older local forecasts can
    use their linked immutable snapshot. SQL extracts only the reference candle's
    small metadata fields; training matrices and candle/evidence arrays never
    leave SQLite. This is invoked lazily for the selected consensus forecast,
    rather than joining every historical snapshot on each display refresh.
    """
    if "reference_provenance" in forecast:
        return _validated_reference_provenance(forecast["reference_provenance"], forecast, basis="saved_forecast")
    unknown = _unknown_reference_provenance("reference minute counts were not recorded")
    if database_path is None:
        return unknown
    try:
        path = Path(database_path).expanduser().resolve()
        exists = path.is_file()
    except (OSError, TypeError, ValueError):
        return unknown
    if path.suffix.casefold() == ".json" or not exists:
        return unknown
    fields = ("opened_at", "observed_constituents", "synthetic_constituents", "synthetic", "no_trade",
              "source_id", "availability_kind")
    projections = []
    for prefix in ("$.inference.snapshot.candles[#-1]", "$.candles[#-1]"):
        arguments = ", ".join("'" + prefix + "." + field + "'" for field in fields)
        projections.append(f"json_extract(s.payload, {arguments})")
    # Both snapshot layouts end at the fixed reference. A different final
    # candle is rejected below; no neighboring candle is substituted.
    query = ("SELECT s.id, " + ", ".join(projections)
             + " FROM symbol_inputs i JOIN snapshots s ON s.id=i.snapshot_id"
               " WHERE i.cycle_id=? AND i.symbol=? AND json_valid(s.payload) LIMIT 2")
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as connection:
            connection.execute("PRAGMA query_only=ON")
            rows = connection.execute(query, (forecast.get("cycle_id"), forecast.get("symbol"))).fetchall()
        if len(rows) != 1:
            return unknown
        snapshot_id, *metadata = rows[0]
        if forecast.get("snapshot_hash") is not None and forecast["snapshot_hash"] != snapshot_id:
            return _unknown_reference_provenance("saved input snapshot identity differs from the forecast")
        for raw in metadata:
            values = json.loads(raw)
            if not isinstance(values, list) or len(values) != len(fields) or all(value is None for value in values):
                continue
            value = dict(zip(fields, values))
            value["reference_at"] = value.pop("opened_at")
            if (value["observed_constituents"] is None and value["synthetic_constituents"] is None
                    and timestamp(value["reference_at"]) == timestamp(forecast.get("reference_at"))):
                # Older compact inference snapshots may omit counts that their
                # same linked snapshot's reference candle still retains.
                continue
            return _validated_reference_provenance(value, forecast, basis="immutable_snapshot")
        return unknown
    except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError):
        return _unknown_reference_provenance("saved input provenance is unavailable or malformed")


def reference_input_label(provenance: Mapping[str, Any]) -> str:
    if provenance.get("status") != "known":
        return "Input candle: minute provenance unknown"
    observed, synthetic = provenance["observed_constituents"], provenance["synthetic_constituents"]
    source = provenance.get("source_dataset") or provenance.get("source_id") or "source unrecorded"
    prefix = f"Input candle ({source}): {observed} observed minutes · {synthetic} synthetic minutes"
    if synthetic and provenance.get("availability_kind") != "actual":
        return prefix + " (source no-trade confirmation not recorded)"
    if synthetic:
        if not (provenance.get("source_dataset") or provenance.get("source_id")):
            return prefix + " (no-trade source scope unknown)"
        return prefix + " (no trades recorded by this source; market-wide inactivity is not established)"
    return prefix


def saved_forecast_context(row: Mapping[str, Any], *, now: datetime) -> str:
    """Describe the selected saved issue, never the currently installed setup."""
    issued = timestamp(row.get("issued_at"))
    if issued is None:
        timing = "Saved forecast · issue time unknown"
    else:
        local = issued.astimezone(PACIFIC)
        seconds = (now - issued).total_seconds()
        heading = "Historical saved forecast" if local.date() < now.astimezone(PACIFIC).date() else "Saved forecast"
        age = f"{seconds / 60:.1f}m ago" if seconds >= 0 else "future issue timestamp"
        timing = f"{heading} · {local:%b %d, %Y %H:%M:%S %Z} · {age}"
    manifest = row.get("dataset_manifest")
    manifest = manifest if isinstance(manifest, Mapping) else {}
    recipe = manifest.get("manual_model_recipe")
    recipe = recipe if isinstance(recipe, Mapping) else {}
    selection = row.get("model_selection")
    selection = selection if isinstance(selection, Mapping) else {}
    setup = ("manual observed-only recipe" if recipe.get("schema_version") in {
                 "intraday-manual-observed-calibrated-v1", "intraday-manual-observed-calibrated-v2"}
             else str(selection.get("selection") or "identity unrecorded").replace("_", " "))
    calibrations = set()
    for output in row.get("models", []):
        metadata = output.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        spec = metadata.get("spec")
        spec = spec if isinstance(spec, Mapping) else {}
        calibration = metadata.get("calibration")
        calibration = calibration if isinstance(calibration, Mapping) else {}
        calibrations.add(str(calibration.get("method") or spec.get("probability_calibration") or "unrecorded").replace("_", " "))
    scope = manifest.get("synthetic_policy") or row.get("synthetic_policy") or "unrecorded"
    return (f"{timing}\nSaved setup: {row.get('configured_count', 'unknown')} models · {setup}"
            f" · calibration {', '.join(sorted(calibrations)) or 'unrecorded'} · synthetic labels {scope}"
            "\nSaved forecast settings; refreshing does not refit models.")


def default_databases(datastore_root: Path | None = None) -> dict[str, Path]:
    """Only the local store is discovered; remote receipt paths are explicit."""
    configured = os.environ.get("DUCKETS_DATASTORE_DIR", "").strip() or os.environ.get("DUCKETS_OHLCV_PARQUET_DIR", "").strip()
    root = datastore_root or Path(configured or "C:/DATASTORE")
    result = {"Local": Path(root) / "intraday" / "shadow.sqlite3"}
    receipt = os.environ.get("DUCKETS_INTRADAY_PEER_RECEIPT", "").strip()
    if receipt:
        result["Peer receipt"] = Path(receipt)
    return result


def configured_actor() -> str | None:
    """Identity only; reading this never confers operating authority."""
    try:
        profile = Path(__file__).resolve().parents[2] / "scratch" / "cross-pc" / "local-profile.json"
        actor = json.loads(profile.read_text(encoding="utf-8"))["actor"]
        return actor if actor in ("Atlas", "Scout") else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _payload(row: sqlite3.Row) -> dict[str, Any]:
    value = json.loads(row["payload"])
    if not isinstance(value, dict):
        raise ValueError("Saved payload must be a JSON object")
    return value


def outcome_status(forecast: Mapping[str, Any], now: datetime) -> str:
    outcome = forecast.get("outcome") or {}
    target = timestamp(forecast.get("target_close"))
    observed = timestamp(outcome.get("observed_at"))
    available = timestamp(outcome.get("available_at", outcome.get("observed_at")))
    status = str(outcome.get("status", ""))
    if target is None:
        return "unscorable"
    if target > now or (observed is not None and observed > now) or (available is not None and available > now):
        return "pending"
    if status in ("scored", "synthetic"):
        if observed is None or outcome.get("target_not_down") not in (True, False, 0, 1):
            return "unscorable"
        return status
    # A saved pending observation describes an earlier scoring cutoff. Once
    # the fixed target is past, absence of a mature saved outcome is missing.
    if status == "pending":
        return "missing"
    return status if status in OUTCOME_STATES else "missing"


def load_intraday_snapshot(database_paths: Mapping[str, Path] | None = None, *,
                           now: datetime | None = None, local_pc: str | None = None,
                           stopped=lambda: False) -> IntradaySnapshot:
    now = timestamp(now or datetime.now(UTC))
    if now is None:
        raise ValueError("A timezone-aware now is required")
    local_pc = local_pc or configured_actor()
    forecasts, cycles, sources, warnings = [], [], [], []
    for label, raw_path in (database_paths if database_paths is not None else default_databases()).items():
        if stopped():
            break
        requested_path = Path(raw_path).expanduser()
        path = requested_path.resolve()
        source = {"label": label, "path": str(path), "status": "missing", "latest_issue": None}
        sources.append(source)
        if not path.is_file():
            continue
        try:
            if path.suffix.casefold() == ".json":
                # This validator reads sanitized immutable monitoring receipts,
                # and imports no provider, model, store or execution service.
                from ml.intraday.telemetry import load_receipt
                if local_pc not in ("Atlas", "Scout"):
                    raise ValueError("Local PC identity required for a peer receipt")
                expected_peer = "Scout" if local_pc == "Atlas" else "Atlas"
                received = load_receipt(requested_path, expected_peer=expected_peer, local_pc=local_pc, as_of=now.isoformat())
                receipt = received["receipt"]
                payload = receipt["packet"]["payload"]
                saved = []
                for raw in payload["forecasts"]:
                    value = dict(raw)
                    value["outcome"] = value.get("outcome") or {}
                    value["intents"] = []  # Private account intent quantities are excluded from this channel.
                    value["source_label"] = label
                    value["outcome_status"] = outcome_status(value, now)
                    saved.append(value)
                forecasts.extend(saved)
                for raw in payload["cycles"]:
                    value = dict(raw)
                    value.setdefault("id", value.get("cycle_id", "unknown"))
                    value["source_label"] = label
                    cycles.append(value)
                latest_issue = timestamp(received.get("latest_forecast_issue")) or max(
                    (timestamp(row.get("issued_at")) for row in saved if timestamp(row.get("issued_at"))), default=None)
                source.update(status="read", latest_issue=latest_issue, latest_recorded_at=received.get("latest_recorded_at"),
                              producer_pc=receipt["producer_pc"], produced_at=payload["produced_at"],
                              received_at=receipt["received_at"], producer_age_seconds=received["producer_age_seconds"],
                              receipt_age_seconds=received["receipt_age_seconds"], telemetry_status=received["status"],
                              latest_forecast_mode=received.get("latest_forecast_mode"))
                continue
            # mode=ro forbids schema creation or accidental commits. Do not use
            # immutable=1: a running local store may legitimately have WAL data.
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                # The one-issue JSONB CTE is transient connection-local work;
                # keep it in memory instead of spilling scientific bytes.
                use_jsonb = _has_jsonb(connection)
                if use_jsonb:
                    connection.execute("PRAGMA temp_store=MEMORY")
                connection.set_progress_handler(lambda: int(stopped()), 1000)
                connection.execute("BEGIN")
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "forecasts" not in tables:
                    raise ValueError("Store has no forecasts table")
                outcomes: dict[str, dict[str, Any]] = {}
                if "outcomes" in tables:
                    for row in connection.execute("SELECT * FROM outcomes ORDER BY rowid"):
                        value = _payload(row)
                        value["observed_at"] = row["observed_at"]
                        value["revision_id"] = row["id"]
                        outcomes[str(row["forecast_id"])] = value
                intents: dict[str, list[dict[str, Any]]] = defaultdict(list)
                if "intents" in tables:
                    query = ("SELECT forecast_id,recorded_at," + _json_projection("payload", DISPLAY_INTENT_FIELDS)
                             + " AS display FROM intents ORDER BY rowid")
                    for row in connection.execute(query):
                        value = _display_payload(row["display"], DISPLAY_INTENT_FIELDS)
                        value["recorded_at"] = row["recorded_at"]
                        intents[str(row["forecast_id"])].append(value)
                persistence = {}
                if "events" in tables:
                    fields = ("forecast_id", "persistence_seconds")
                    query = ("SELECT " + _json_projection("payload", fields)
                             + " AS display FROM events WHERE kind='forecast_persisted' ORDER BY rowid")
                    for row in connection.execute(query):
                        value = _display_payload(row["display"], fields)
                        if value.get("forecast_id"):
                            persistence[str(value["forecast_id"])] = value
                saved = []
                for row, value in _display_forecasts(connection, use_jsonb=use_jsonb):
                    if stopped():
                        raise sqlite3.OperationalError("interrupted")
                    for key in ("pc", "symbol", "cycle_id", "reference_at", "issued_at"):
                        value[key] = row[key]
                    value["forecast_id"] = str(row["id"])
                    value["outcome"] = outcomes.get(value["forecast_id"], {})
                    value["intents"] = intents.get(value["forecast_id"], [])
                    value["persistence_evidence"] = persistence.get(value["forecast_id"], {})
                    value["source_label"] = label
                    for index, model in enumerate(value["models"]):
                        model["saved_evidence_source"] = dict(database=str(path), forecast_id=value["forecast_id"], model_index=index)
                    value["outcome_status"] = outcome_status(value, now)
                    saved.append(value)
                if "cycles" in tables:
                    query = ("SELECT id,pc,started_at," + _json_projection("payload", DISPLAY_CYCLE_FIELDS)
                             + " AS display,json_extract(payload,'$.configuration.source_dataset') AS configured_source FROM cycles ORDER BY started_at,rowid")
                    for row in connection.execute(query):
                        value = _display_payload(row["display"], DISPLAY_CYCLE_FIELDS)
                        value.update(id=row["id"], pc=row["pc"], started_at=row["started_at"], source_label=label)
                        if row["configured_source"] is not None:
                            value["configuration"] = {"source_dataset": row["configured_source"]}
                        cycles.append(value)
                connection.rollback()
            forecasts.extend(saved)
            latest = max((timestamp(row.get("issued_at")) for row in saved if timestamp(row.get("issued_at"))), default=None)
            source.update(status="read", latest_issue=latest)
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, IndexError) as exc:
            source["status"] = "unreadable"
            warnings.append(f"{label}: {type(exc).__name__}: {exc}")
    forecasts.sort(key=lambda row: (timestamp(row.get("issued_at")) or datetime.min.replace(tzinfo=UTC), row["forecast_id"]))
    return IntradaySnapshot(now, tuple(forecasts), tuple(cycles), tuple(sources), tuple(warnings))


def source_dataset_label(row: Mapping[str, Any]) -> str:
    return str(row.get("source_dataset") or "Unrecorded")


def filter_forecasts(snapshot: IntradaySnapshot, *, pc: str = "All", symbol: str = "All",
                     session: str = "All", mode: str = "shadow", reference_slot: str = "All",
                     source_dataset: str = "All") -> tuple[dict[str, Any], ...]:
    def accepts(row):
        issued = timestamp(row.get("issued_at"))
        reference = timestamp(row.get("reference_at"))
        return ((pc == "All" or row.get("pc") == pc)
                and (symbol == "All" or row.get("symbol") == symbol)
                and (mode == "All" or row.get("mode", "shadow") == mode)
                and (source_dataset == "All" or source_dataset_label(row) == source_dataset)
                and (reference_slot == "All" or (reference is not None and reference.astimezone(PACIFIC).strftime("%H:%M") == reference_slot))
                and (session == "All" or (issued is not None and issued.astimezone(PACIFIC).date().isoformat() == session)))
    return tuple(row for row in snapshot.forecasts if accepts(row))


def window_rows(rows: Sequence[Mapping[str, Any]], window: str, now: datetime) -> tuple[Mapping[str, Any], ...]:
    ordered = sorted(rows, key=lambda row: timestamp(row.get("issued_at")) or datetime.min.replace(tzinfo=UTC))
    if window.startswith("Last "):
        return tuple(ordered[-int(window.split()[1]):])
    if window == "Today":
        return tuple(row for row in ordered if timestamp(row.get("issued_at")) is not None
                     and timestamp(row["issued_at"]).astimezone(PACIFIC).date() == now.astimezone(PACIFIC).date())
    if window == "7 days":
        return tuple(row for row in ordered if timestamp(row.get("issued_at")) is not None
                     and now - timedelta(days=7) <= timestamp(row["issued_at"]) <= now)
    if window != "Cumulative":
        raise ValueError(f"Unknown metric window: {window}")
    return tuple(ordered)


def prediction_timing(row: Mapping[str, Any]) -> str:
    """Describe when the saved forecast existed relative to its fixed target.

    Saved timestamps, rather than a historical issue_after_target flag or
    execution disposition, define the cohort. The opening instant belongs to
    During target; the closing instant belongs to At or after target close.
    """
    try:
        issued, opened, closed = (timestamp(row.get(key)) for key in
                                  ("issued_at", "target_open", "target_close"))
    except (OverflowError, ValueError):
        return "Timing unknown"
    if issued is None or opened is None or closed is None or opened >= closed:
        return "Timing unknown"
    if issued < opened:
        return "Before target open"
    return "During target" if issued < closed else "At or after target close"


def metric_rows(rows: Sequence[Mapping[str, Any]], *,
                scope: str = DEFAULT_METRIC_SCOPE) -> tuple[Mapping[str, Any], ...]:
    """Select a descriptive score cohort without changing persisted evidence.

    Observed scopes exclude synthetic outcomes. Pending, missing and unscorable
    rows remain visible in issued/status counts, but cannot enter a score's
    denominator. Timing-unknown records are retained in the explicit audit and
    all-saved scopes only. Apply the requested metric window after this selection
    so Last N refers to this cohort's issued forecasts.
    """
    if scope not in METRIC_SCOPES:
        raise ValueError(f"Unknown metric scope: {scope}")
    if scope == "All saved outcomes":
        return tuple(rows)
    label, outcome_policy = scope.split(" · ", 1)
    timing = "At or after target close" if label == "After target close" else label
    return tuple(row for row in rows if prediction_timing(row) == timing
                 and (outcome_policy != "observed" or row.get("outcome_status") != "synthetic"))


def _scored_pairs(rows, model: str | None = None):
    result = []
    for row in rows:
        if row.get("outcome_status") not in ("scored", "synthetic"):
            continue
        value = row.get("probability_not_down")
        if model is not None:
            output = next((item for item in row.get("models", []) if item.get("name") == model and item.get("status") == "success"), None)
            value = output.get("probability_not_down") if output is not None else None
        p = probability(value)
        target = row.get("outcome", {}).get("target_not_down")
        if p is not None and target in (True, False, 0, 1):
            result.append((p, int(target), row))
    return result


def forecast_metrics(rows: Sequence[Mapping[str, Any]], *, model: str | None = None) -> dict[str, Any]:
    pairs = _scored_pairs(rows, model)
    counts = Counter(row.get("outcome_status", "unscorable") for row in rows)
    n = len(pairs)
    accuracy = mean(int(p >= .5) == y for p, y, _ in pairs) if n else None
    prevalence = mean(y for _, y, _ in pairs) if n else None
    recalls = [mean(int(p >= .5) == y for p, y, _ in pairs if y == label) for label in (0, 1) if any(y == label for _, y, _ in pairs)]
    calibration = []
    for lower in range(0, 100, 10):
        samples = [(p, y) for p, y, _ in pairs if lower / 100 <= p < (lower + 10) / 100 or (lower == 90 and p == 1)]
        calibration.append({"range": f"{lower}–{lower + 10}%", "count": len(samples),
                            "mean_probability": mean(p for p, _ in samples) if samples else None,
                            "observed_not_down": mean(y for _, y in samples) if samples else None})
    disagreements = []
    for row in rows:
        values = [probability(item.get("probability_not_down")) for item in row.get("models", []) if item.get("status") == "success"]
        values = [p for p in values if p is not None]
        if len(values) > 1:
            disagreements.append(pstdev(values))
    observed_pairs = [item for item in pairs if item[2].get("outcome_status") == "scored"]
    synthetic_pairs = [item for item in pairs if item[2].get("outcome_status") == "synthetic"]
    def is_equality(row):
        try:
            reference = Decimal(str(row.get("reference_open")))
            target = Decimal(str(row.get("outcome", {}).get("target_close_price")))
            return reference.is_finite() and target.is_finite() and reference == target
        except InvalidOperation:
            return False
    equality_pairs = [item for item in pairs if is_equality(item[2])]
    return {"issued": len(rows), "denominator": n, "observed_denominator": len(observed_pairs),
            "synthetic_denominator": len(synthetic_pairs),
            "observed_accuracy": mean(int(p >= .5) == y for p, y, _ in observed_pairs) if observed_pairs else None,
            "synthetic_accuracy": mean(int(p >= .5) == y for p, y, _ in synthetic_pairs) if synthetic_pairs else None,
            "equality_denominator": len(equality_pairs),
            "equality_accuracy": mean(int(p >= .5) == y for p, y, _ in equality_pairs) if equality_pairs else None,
            "observed_equality_denominator": sum(item[2].get("outcome_status") == "scored" for item in equality_pairs),
            "synthetic_equality_denominator": sum(item[2].get("outcome_status") == "synthetic" for item in equality_pairs),
            "counts": {key: counts[key] for key in OUTCOME_STATES},
            "accuracy": accuracy, "balanced_accuracy": mean(recalls) if len(recalls) == 2 else None,
            "positive_label_fraction": prevalence,
            "majority_class_baseline_accuracy": max(prevalence, 1-prevalence) if n else None,
            "baseline_basis": "descriptive_same_scored_cohort_majority_not_a_forecast_or_trade_return",
            "brier": mean((p-y)**2 for p, y, _ in pairs) if n else None,
            "log_loss": -mean(y*math.log(max(1e-15, p)) + (1-y)*math.log(max(1e-15, 1-p)) for p, y, _ in pairs) if n else None,
            "calibration": calibration, "mean_disagreement": mean(disagreements) if disagreements else None,
            "disagreement_denominator": len(disagreements)}


def model_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    names = sorted({item["name"] for row in rows for item in row.get("models", []) if "name" in item})
    return {"Ensemble": forecast_metrics(rows), **{name: forecast_metrics(rows, model=name) for name in names}}


def error_correlations(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    names = sorted({item["name"] for row in rows for item in row.get("models", []) if "name" in item})
    residuals = {name: {row["forecast_id"]: p-y for p, y, row in _scored_pairs(rows, name)} for name in names}
    result = []
    for index, left in enumerate(names):
        for right in names[index + 1:]:
            shared = sorted(residuals[left].keys() & residuals[right].keys())
            x, y = [residuals[left][key] for key in shared], [residuals[right][key] for key in shared]
            coefficient = None
            if len(shared) >= 2:
                xm, ym = mean(x), mean(y)
                denom = math.sqrt(sum((v-xm)**2 for v in x) * sum((v-ym)**2 for v in y))
                if denom > 0:
                    coefficient = sum((a-xm)*(b-ym) for a, b in zip(x, y)) / denom
            result.append({"left": left, "right": right, "count": len(shared), "correlation": coefficient})
    return tuple(result)


def regime_metrics(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Explicit descriptive bins; realized-return bins are retrospective only."""
    groups = defaultdict(list)
    measured = defaultdict(list)
    for row in rows:
        if row.get("outcome_status") not in ("scored", "synthetic"):
            continue
        reference = row.get("reference_open")
        close = row.get("outcome", {}).get("target_close_price")
        try:
            reference, close = float(reference), float(close)
            if not math.isfinite(reference) or not math.isfinite(close) or reference <= 0 or close <= 0:
                continue
            movement = abs(close / reference - 1)
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        bucket = "Flat ≤0.1%" if movement <= .001 else "Small 0.1–1%" if movement < .01 else "Large ≥1%"
        groups[("Realized |return|", bucket)].append(row)
        issued = timestamp(row.get("issued_at"))
        if issued:
            groups[("Issue hour Pacific", issued.astimezone(PACIFIC).strftime("%H:00"))].append(row)
        for key in ("volatility", "trend_strength", "choppiness"):
            regime = row.get("regime", {}).get(key)
            if isinstance(regime, str):
                groups[(key, regime)].append(row)
        for keys, title in ((("volatility20_observed", "volatility"), "Causal volatility20"),
                            (("trend_efficiency", "trend_strength"), "Causal trend efficiency"),
                            (("choppiness20", "choppiness"), "Causal choppiness20")):
            measurements = row.get("regime_measurements", {})
            value = next((measurements[key] for key in keys if key in measurements), None)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                measured[title].append((value, row))
    for title, samples in measured.items():
        values = sorted(value for value, _ in samples)
        def quantile(fraction):
            position = (len(values)-1) * fraction
            lower, upper = math.floor(position), math.ceil(position)
            return values[lower] + (values[upper]-values[lower]) * (position-lower)
        low, high = quantile(1/3), quantile(2/3)
        for value, row in samples:
            bucket = f"Low ≤{low:.5g}" if value <= low else f"Mid ≤{high:.5g}" if value <= high else f"High >{high:.5g}"
            groups[(title, bucket)].append(row)
    return tuple({"dimension": dimension, "bucket": bucket, **forecast_metrics(values)}
                 for (dimension, bucket), values in sorted(groups.items()))


def latest_matrix(rows: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    latest = {}
    for row in sorted(rows, key=lambda row: timestamp(row.get("issued_at")) or datetime.min.replace(tzinfo=UTC)):
        latest[(row.get("pc"), row.get("symbol"), source_dataset_label(row), row.get("mode", "shadow"))] = row
    return tuple(latest[key] for key in sorted(latest))


def probability_geometry(value: Any, width: float) -> tuple[float | None, float]:
    """Canvas geometry retains the visible midpoint even for a failed model."""
    p = probability(value)
    return (p * width if p is not None else None, width / 2)
