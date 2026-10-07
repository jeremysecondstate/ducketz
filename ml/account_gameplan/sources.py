"""Explicit portable frozen inputs for an account plan; no acquisition or execution."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import re
from typing import Mapping, Sequence

import pandas as pd

from ml.artifacts import file_checksum, verify_manifest
from ml.gameplan_actuals_review import _match_trade_rows
from ml.gameplan_probability_target import probability_target_contract
from ml.stock_trader.cross_horizon_fallback import validate_fallback_policy
from ml.stock_trader.independent_signals import _validated_independent_forecasts

VERSION = "account-gameplan-source-v1"
_FILES = {"forecasts.parquet", "planning-price-path.json", "ownership.json"}


@dataclass(frozen=True)
class SourceBundle:
    metadata: Mapping
    forecasts: pd.DataFrame
    price_path: Mapping
    ownership: Mapping | None
    manifest_sha256: str
    path: Path


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("An explicit SHA256 digest is required")
    return value


def _producer(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value) is None:
        raise ValueError("Invalid source producer identity")
    return value


def _symbols(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("Source symbols must be an explicit registry")
    symbols = tuple(values)
    if (not symbols or len(set(symbols)) != len(symbols)
            or any(not isinstance(value, str) or re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", value) is None for value in symbols)):
        raise ValueError("Source symbol registry is invalid")
    return tuple(sorted(symbols))


def _json(raw: str | bytes) -> dict:
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("Duplicate JSON key")
            value[key] = item
        return value
    def constant(_value):
        raise ValueError("Nonfinite JSON value")
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(value, dict):
        raise ValueError("Expected JSON object")
    return value


def _encoded(value) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _clock(value: object) -> pd.Timestamp:
    if value is None:
        raise ValueError("An explicit aware source timestamp is required")
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp) or timestamp.tzinfo is None:
        raise ValueError("An explicit aware source timestamp is required")
    return timestamp.tz_convert("UTC")


def _day(value: object) -> str:
    day = date.fromisoformat(str(value)).isoformat()
    if day != value:
        raise ValueError("Source action date must be exact ISO format")
    return day


def _before_open(value, day):
    timestamp = _clock(value)
    if timestamp >= pd.Timestamp(f"{day} 04:00", tz="America/Los_Angeles").tz_convert("UTC"):
        raise ValueError("Source was generated after its action opening")
    return timestamp


def combined_forecast_id(producer_id: str, source_receipt_sha256: str, original_forecast_id: str) -> str:
    """Preserve producer, frozen source and original forecast; never choose latest."""
    if not isinstance(original_forecast_id, str) or not original_forecast_id or "\x00" in original_forecast_id:
        raise ValueError("Original forecast identity is required")
    return f"{_producer(producer_id)}:{_digest(source_receipt_sha256)}:{original_forecast_id}"


def _safe_outputs(path: Path, manifest: Mapping) -> None:
    outputs = manifest.get("output_files")
    if not isinstance(outputs, dict):
        raise ValueError("Native output inventory is missing")
    for name in outputs:
        relative = PurePosixPath(name)
        if (not name or "\\" in name or ":" in name or relative.is_absolute()
                or ".." in relative.parts or (path / name).is_symlink()
                or not (path / name).resolve().is_relative_to(path.resolve())):
            raise ValueError("Native output path escapes its source")


def _output_matches(manifest, name, raw):
    evidence = manifest.get("output_files", {}).get(name, {})
    if evidence.get("checksum_sha256") != _sha(raw) or evidence.get("size") != len(raw):
        raise ValueError(f"Portable bytes differ from native output: {name}")


def _deployment(root, publication, day):
    from ml.gameplan_deployment import read_deployment, assert_execution_gameplan
    pointer = root / f"ml/gameplan-deployment-by-date/{day}/run.json"
    original = file_checksum(pointer) if pointer.exists() else None
    report = read_deployment(root, day)
    if report is None:
        return {"deployment_status": "NO_REGISTERED_HANDOFF"}, None, {pointer: original}
    assert_execution_gameplan(root, publication, action_date=day)
    pointer_raw = pointer.read_bytes()
    saved = _json(pointer_raw)
    reference = saved["current"]["run_path"]
    run = (root / reference).resolve()
    if run.parent != root / "ml/gameplan-deployment-runs":
        raise ValueError("Deployment proof escapes native source directories")
    raw = {key: (run / filename).read_bytes() for key, filename in (
        ("receipt", "receipt.json"), ("manifest", "manifest.json"), ("report", "deployment.json"))}
    raw["pointer"] = pointer_raw
    if (_sha(pointer_raw) != original or _json(raw["report"]) != report
            or read_deployment(root, day) != report):
        raise ValueError("Source deployment changed during export")
    metadata = {"deployment_status": "ACTIVE", "deployment_run": reference,
                **{f"deployment_{key}_sha256": _sha(value) for key, value in raw.items()}}
    proof = {f"{key}_json": value.decode("utf-8") for key, value in raw.items()}
    checks = {pointer: original, **{run / filename: _sha(raw[key]) for key, filename in (
        ("receipt", "receipt.json"), ("manifest", "manifest.json"), ("report", "deployment.json"))}}
    return metadata, proof, checks


def _validate_deployment(manifest, metadata):
    proof = manifest["native"].get("deployment")
    if metadata.get("deployment_status") == "NO_REGISTERED_HANDOFF":
        if proof is not None or any(key.startswith("deployment_") and key != "deployment_status" for key in metadata):
            raise ValueError("Legacy source claims an unbound deployment")
        return
    if metadata.get("deployment_status") != "ACTIVE" or not isinstance(proof, dict):
        raise ValueError("Source handoff is not verified ACTIVE")
    raw = {key: proof[f"{key}_json"].encode("utf-8") for key in ("pointer", "receipt", "manifest", "report")}
    if any(_sha(value) != _digest(metadata.get(f"deployment_{key}_sha256")) for key, value in raw.items()):
        raise ValueError("Deployment proof hash differs")
    pointer, receipt, native, report = (_json(raw[key]) for key in ("pointer", "receipt", "manifest", "report"))
    version = "gameplan-variant-deployment-v1"
    current, selected = pointer.get("current", {}), report.get("YG", {})
    if (pointer.get("schema_version") != version or receipt.get("schema_version") != version
            or report.get("schema_version") != version or report.get("status") != "ACTIVE"
            or receipt.get("status") != "ACTIVE" or report.get("action_date") != metadata["action_date"]
            or receipt.get("action_date") != metadata["action_date"]
            or current.get("run_path") != metadata.get("deployment_run")
            or receipt.get("run_path") != metadata.get("deployment_run")
            or current.get("receipt_sha256") != metadata["deployment_receipt_sha256"]
            or receipt.get("manifest_sha256") != metadata["deployment_manifest_sha256"]
            or native.get("configuration", {}).get("status") != "ACTIVE"
            or native.get("configuration", {}).get("action_date") != metadata["action_date"]
            or report.get("orders_placed") != 0 or report.get("broker_orders_enabled") is not False
            or selected.get("run_path") != metadata["source_gameplan_run"]
            or selected.get("receipt_sha256") != metadata["source_receipt_sha256"]):
        raise ValueError("Deployment does not select this exact frozen source")
    _output_matches(native, "deployment.json", raw["report"])
    if "YG_trade_plan" in report and report["YG_trade_plan"] != {
            "run_path": metadata["trade_plan_run"], "receipt_sha256": metadata["trade_plan_receipt_sha256"]}:
        raise ValueError("Deployment selected a different completed trade plan")
    _before_open(report.get("updated_at"), metadata["action_date"])


def _proof(manifest, metadata, prefix):
    proof = manifest["native"][prefix]
    receipt_raw, source_raw = proof["receipt_json"].encode("utf-8"), proof["manifest_json"].encode("utf-8")
    if (_sha(receipt_raw) != metadata[f"{prefix}_receipt_sha256"]
            or _sha(source_raw) != metadata[f"{prefix}_manifest_sha256"]):
        raise ValueError("Native receipt or manifest proof hash differs")
    receipt, source = _json(receipt_raw), _json(source_raw)
    digest_key = "manifest_checksum_sha256" if prefix == "source" else "manifest_sha256"
    if (receipt.get(digest_key) != _sha(source_raw) or receipt.get("action_date") != metadata["action_date"]
            or source.get("configuration", {}).get("action_date") != metadata["action_date"]):
        raise ValueError("Native source receipt binding differs")
    return receipt, source


def _validate_path(path, metadata):
    if (path.get("contract_version") not in {f"conditional-hourly-planning-price-path-v{i}" for i in (1, 2, 3)}
            or path.get("price_source_contract") != metadata["target_price_source_contract"]
            or path.get("price_dataset") != metadata["target_price_dataset"]
            or path.get("observed_at") != metadata["planning_observed_at"]):
        raise ValueError("Planning path source or policy differs")
    _before_open(path["observed_at"], metadata["action_date"])
    expected = {f"{symbol}|{metadata['action_date']}|{hour:02d}:00" for symbol in metadata["symbols"] for hour in range(4, 18)}
    if not isinstance(path.get("points"), dict) or set(path["points"]) != expected:
        raise ValueError("Planning price point universe or clocks differ")
    for key, point in path["points"].items():
        symbol, day, clock = key.split("|")
        close_kind = "planning_close" if path["contract_version"] == "conditional-hourly-planning-price-path-v3" else "observed_close"
        if (point.get("symbol") != symbol or point.get("action_date") != day or point.get("clock_local") != clock
                or point.get("endpoint_kind") != (close_kind if clock == "17:00" else "observed_open")
                or _clock(point.get("timestamp")) != pd.Timestamp(f"{day} {clock}", tz="America/Los_Angeles").tz_convert("UTC")):
            raise ValueError("Planning point identity differs")
        prices = [point.get(f"planned_price_{field}") for field in ("low", "mid", "high")]
        if point.get("status") == "AVAILABLE":
            if (any(type(value) not in (int, float) or not math.isfinite(value) for value in prices)
                    or not 0 < prices[0] <= prices[1] <= prices[2]):
                raise ValueError("Invalid planning price range")
        elif point.get("status") not in {"UNAVAILABLE_REFERENCE_PRICE", "UNAVAILABLE_MINIMUM_SAMPLES"} or any(value is not None for value in prices):
            raise ValueError("Unavailable planning point cannot invent a price")


def read_source_bundle(path: Path, *, expected_manifest_sha256: str, expected_producer: str,
                       expected_symbols: Sequence[str], expected_action_date: str | None = None) -> SourceBundle:
    """Admit an explicit pinned portable source; no machine-local universe lookup."""
    path = Path(path).resolve()
    expected_hash = _digest(expected_manifest_sha256)
    raw_manifest = (path / "manifest.json").read_bytes()
    if _sha(raw_manifest) != expected_hash:
        raise ValueError("Bundle manifest does not match its expected SHA256")
    manifest = _json(raw_manifest)
    if manifest.get("schema_version") != VERSION or set(manifest.get("files", {})) != _FILES:
        raise ValueError("Unsupported or incomplete source bundle")
    if set(item.name for item in path.iterdir()) != _FILES | {"manifest.json"}:
        raise ValueError("Unexpected source bundle contents")
    metadata = manifest["metadata"]
    day = _day(metadata.get("action_date"))
    if (metadata.get("schema_version") != VERSION or _producer(metadata.get("producer_id")) != _producer(expected_producer)
            or tuple(metadata.get("symbols", ())) != _symbols(expected_symbols)
            or (expected_action_date is not None and day != _day(expected_action_date))):
        raise ValueError("Source producer, date or universe differs from registry")
    if metadata.get("holding_policy") not in {"fixed_target_expiry", "accumulate_bullish_sell_on_bearish_no_scheduled_expiry_v1"}:
        raise ValueError("Unsupported source holding policy")
    source_receipt, native_source = _proof(manifest, metadata, "source")
    trade_receipt, native_trade = _proof(manifest, metadata, "trade_plan")
    _validate_deployment(manifest, metadata)
    source_config, trade_config = native_source["configuration"], native_trade["configuration"]
    if metadata.get("holding_policy_proof") == "NATIVE_MANIFEST":
        if any(item.get("holding_policy") != metadata["holding_policy"] for item in (trade_config, trade_receipt)):
            raise ValueError("Holding policy differs from native manifest-bound policy")
    elif metadata.get("holding_policy_proof") == "EXPORTER_ATTESTED_LEGACY":
        if any("holding_policy" in item for item in (trade_config, trade_receipt)):
            raise ValueError("Native holding-policy proof cannot be downgraded to a legacy attestation")
    else:
        raise ValueError("Unsupported source holding-policy proof")
    planning_mode = metadata.get("planning_mode")
    if planning_mode not in {"NIGHTLY_REVIEW", "ACCOUNT_PRODUCER_SOURCE"}:
        raise ValueError("Unsupported portable planning preparation mode")
    if trade_config.get("publication_mode", "NIGHTLY_REVIEW") != planning_mode:
        raise ValueError("Portable preparation mode differs from native proof")
    if planning_mode == "ACCOUNT_PRODUCER_SOURCE":
        binding = _digest(metadata.get("planning_config_sha256"))
        if (trade_receipt.get("publication_mode") != planning_mode
                or any(item.get("account_config_sha256") != binding or item.get("producer_id") != metadata["producer_id"]
                       for item in (trade_config, trade_receipt))
                or "account-snapshot.json" in native_trade.get("output_files", {})):
            raise ValueError("Producer-only preparation claims invalid account or snapshot evidence")
    if (source_receipt.get("run_path") != metadata["source_gameplan_run"]
            or trade_receipt.get("run_path") != metadata["trade_plan_run"]
            or _symbols(source_config.get("symbols", ())) != tuple(metadata["symbols"])
            or source_config.get("target_contract_version") != "independent-stock-targets-v1"
            or source_config.get("preparation_scope") != "STOCK_ONLY"
            or source_config.get("target_price_source_contract") != metadata["target_price_source_contract"]
            or probability_target_contract(source_config) != metadata["probability_target_contract"]
            or trade_receipt.get("status") != "COMPLETE"):
        raise ValueError("Source metadata differs from native publication")
    fallback = validate_fallback_policy(metadata.get("cross_horizon_fallback_policy"), day)
    for item in (source_config, source_receipt, trade_config, trade_receipt):
        if item.get("cross_horizon_fallback_policy") != fallback:
            raise ValueError("Frozen fallback policy bindings differ")
    for item in (trade_config, trade_receipt):
        if (item.get("source_gameplan_run") != metadata["source_gameplan_run"]
                or item.get("source_receipt_sha256") != metadata["source_receipt_sha256"]
                or item.get("orders_placed") != 0 or item.get("broker_orders_enabled") is not False):
            raise ValueError("Trade plan does not bind this non-executable source")
    if (source_receipt.get("published_at") != metadata["source_published_at"]
            or trade_receipt.get("completed_at") != metadata["trade_plan_completed_at"]
            or _before_open(metadata["source_published_at"], day) > _before_open(metadata["trade_plan_completed_at"], day)):
        raise ValueError("Source publication times differ")
    raw = {}
    for name in sorted(_FILES):
        file = path / name
        if file.is_symlink() or not file.is_file():
            raise ValueError("Portable inputs must be regular files")
        raw[name] = file.read_bytes()
        if manifest["files"][name] != {"sha256": _sha(raw[name]), "size": len(raw[name])}:
            raise ValueError("Portable output hash or size differs")
    _output_matches(native_source, "forecasts.parquet", raw["forecasts.parquet"])
    _output_matches(native_trade, "planning-price-path.json", raw["planning-price-path.json"])
    forecasts = _validated_independent_forecasts(pd.read_parquet(io.BytesIO(raw["forecasts.parquet"])), action_date=day, symbols=tuple(metadata["symbols"]))
    if (any(not isinstance(value, str) for value in forecasts.id)
            or sorted(forecasts.id.tolist()) != metadata["original_forecast_ids"]
            or probability_target_contract(forecasts) != metadata["probability_target_contract"]
            or not forecasts.target_price_source_contract.eq(metadata["target_price_source_contract"]).all()
            or not forecasts.target_price_dataset.eq(metadata["target_price_dataset"]).all()):
        raise ValueError("Frozen forecast identity or source differs")
    promoted = sorted(set(forecasts.loc[forecasts.model_status.eq("PROMOTED"), "model_group"]))
    if metadata.get("verified_promoted_groups") != promoted:
        raise ValueError("Forecast promotion lacks the exporter verification binding")
    if promoted:
        if _digest(metadata.get("model_reports_sha256")) != native_source.get("output_files", {}).get("model-reports.json", {}).get("checksum_sha256"):
            raise ValueError("Verified promotion is not bound to the native model reports")
    elif metadata.get("model_reports_sha256") is not None:
        raise ValueError("Research-only forecasts cannot claim verified promotion evidence")
    for value in forecasts.frozen_at:
        if _before_open(value, day) > _clock(metadata["source_published_at"]):
            raise ValueError("Forecast was frozen after publication")
    for key in ("direction_policy_version", "direction_up_threshold", "direction_down_threshold"):
        if key not in forecasts or not forecasts[key].eq(metadata[key]).all():
            raise ValueError("Frozen direction policy differs")
    price_path = _json(raw["planning-price-path.json"])
    _validate_path(price_path, metadata)
    if _clock(price_path["observed_at"]) > _clock(metadata["trade_plan_completed_at"]):
        raise ValueError("Planning observation postdates completion")
    ownership_payload = _json(raw["ownership.json"])
    ownership = ownership_payload.get("evidence")
    if planning_mode == "ACCOUNT_PRODUCER_SOURCE" and ownership is not None:
        raise ValueError("Producer-only source cannot provide account ownership evidence")
    if ownership is not None:
        if (ownership.get("observed_at") != metadata.get("ownership_observed_at")
                or set(ownership) != {"observed_at", "source_snapshot_sha256", "held_shares", "pending_buy_shares", "pending_sell_shares", "ownership"}
                or native_trade.get("output_files", {}).get("account-snapshot.json", {}).get("checksum_sha256") != ownership.get("source_snapshot_sha256")):
            raise ValueError("Saved ownership evidence binding differs")
        if _before_open(ownership["observed_at"], day) > _clock(metadata["trade_plan_completed_at"]):
            raise ValueError("Ownership observation postdates completion")
        for key in ("held_shares", "pending_buy_shares", "pending_sell_shares"):
            values = ownership[key]
            if (not isinstance(values, dict) or set(values) - set(metadata["symbols"])
                    or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in values.values())):
                raise ValueError("Saved ownership quantities differ from source universe")
        if not isinstance(ownership["ownership"], dict):
            raise ValueError("Saved native ownership evidence is invalid")
    elif metadata.get("ownership_observed_at") is not None:
        raise ValueError("Ownership clock exists without its evidence")
    if file_checksum(path / "manifest.json") != expected_hash:
        raise ValueError("Source manifest changed while reading")
    return SourceBundle(metadata, forecasts, price_path, ownership, expected_hash, path)


def export_source_bundle(datastore_root: Path, *, gameplan_run: Path, trade_plan_run: Path,
                         destination: Path, producer_id: str, expected_symbols: Sequence[str]) -> SourceBundle:
    """Copy verified completed saved artifacts into a new immutable directory."""
    from ml.nightly_gameplan import read_gameplan_run
    root = Path(datastore_root).resolve()
    source, trade = Path(gameplan_run).resolve(), Path(trade_plan_run).resolve()
    if source.parent != root / "ml/nightly-gameplan-runs" or trade.parent != root / "ml/gameplan-trade-plan-runs":
        raise ValueError("Explicit native source and trade-plan directories are required")
    for directory in (source, trade):
        _safe_outputs(directory, _json((directory / "manifest.json").read_bytes()))
    initial_hashes = {(str(directory), name): file_checksum(directory / f"{name}.json")
                     for directory in (source, trade) for name in ("receipt", "manifest")}
    publication = read_gameplan_run(root, source)
    trade_manifest = verify_manifest(trade)
    if not {"report.json", "direction-ledger.json", "trade-plan.parquet", "planning-price-path.json"}.issubset(trade_manifest["output_files"]):
        raise ValueError("Native trade plan omits required bound outputs")
    source_manifest = publication.manifest
    source_receipt, trade_receipt = publication.receipt, _json((trade / "receipt.json").read_bytes())
    report = _json((trade / "report.json").read_bytes())
    ledger = _json((trade / "direction-ledger.json").read_bytes())
    config = source_manifest["configuration"]
    day = _day(source_receipt["action_date"])
    deployment_metadata, deployment_proof, deployment_checks = _deployment(root, publication, day)
    policy = validate_fallback_policy(config.get("cross_horizon_fallback_policy"), day)
    if any(item.get("cross_horizon_fallback_policy") != policy for item in (report, ledger)):
        raise ValueError("Saved planning policy differs from frozen source")
    if (report.get("status") != "COMPLETE" or report.get("action_date") != day
            or report.get("source_gameplan_run") != source.relative_to(root).as_posix()
            or report.get("source_receipt_sha256") != file_checksum(source / "receipt.json")
            or report.get("orders_placed") != 0 or report.get("broker_orders_enabled") is not False
            or ledger.get("status") not in {"COMPLETE", "UNAVAILABLE_PRICE_REFERENCES", "UNAVAILABLE_SHARED_ACCOUNT_PROJECTION"}
            or report.get("direction_based_projection") != ledger):
        raise ValueError("Completed source-bound trade plan is required")
    if not (_before_open(report.get("observed_at"), day) <= _before_open(report.get("completed_at"), day)
            <= _before_open(trade_receipt.get("completed_at"), day)):
        raise ValueError("Trade plan report completion clock differs")
    raw_forecasts = (source / "forecasts.parquet").read_bytes()
    forecasts = pd.read_parquet(io.BytesIO(raw_forecasts))
    promoted = sorted(set(forecasts.loc[forecasts.model_status.eq("PROMOTED"), "model_group"]))
    if promoted:
        from ml.stock_trader.independent_signals import verified_promoted_model_groups
        if not set(promoted).issubset(verified_promoted_model_groups(publication)):
            raise ValueError("Frozen forecasts claim unverified model promotion")
    trade_rows = pd.read_parquet(trade / "trade-plan.parquet")
    _match_trade_rows(forecasts, trade_rows)
    planning_mode = trade_manifest["configuration"].get("publication_mode", "NIGHTLY_REVIEW")
    holding = ledger.get("holding_policy", "fixed_target_expiry")
    if "holding_policy" in trade_manifest["configuration"]:
        if any(item.get("holding_policy") != holding for item in (trade_manifest["configuration"], trade_receipt, report)):
            raise ValueError("Native holding policy differs from its original ledger")
        holding_proof = "NATIVE_MANIFEST"
    else:
        if "holding_policy" in trade_receipt:
            raise ValueError("Native holding-policy receipt lacks manifest binding")
        holding_proof = "EXPORTER_ATTESTED_LEGACY"
    if ledger["status"] == "UNAVAILABLE_SHARED_ACCOUNT_PROJECTION":
        financial = [name for name in trade_rows if name.startswith(("direction_based_", "projected_cash_after_"))
                     or name in {"trade_quantity", "scheduled_trade_quantity", "projected_trade_quantity", "projected_shares_after",
                                 "trade_notional_reserved", "projected_trade_budget", "projected_trade_notional"}]
        if (planning_mode != "ACCOUNT_PRODUCER_SOURCE" or report.get("publication_mode") != planning_mode
                or report.get("direction_projection_status") != ledger["status"] or report.get("snapshot") is not None
                or report.get("snapshot_status") != "NOT_CAPTURED_PRODUCER_ONLY"
                or financial and trade_rows[financial].notna().any().any()
                or any(ledger.get(key) != empty for key, empty in (("events", []), ("hourly", []), ("ending_positions", {}), ("summary", {})))
                or "account-snapshot.json" in trade_manifest["output_files"]):
            raise ValueError("Producer-only preparation cannot invent financial or ownership projections")
    elif planning_mode == "ACCOUNT_PRODUCER_SOURCE":
        raise ValueError("Producer-only preparation must explicitly defer the shared account projection")
    path_raw = (trade / "planning-price-path.json").read_bytes()
    price_path = _json(path_raw)
    ownership = None
    if "account-snapshot.json" in trade_manifest["output_files"]:
        snapshot_raw = (trade / "account-snapshot.json").read_bytes()
        snapshot = _json(snapshot_raw)
        if report.get("snapshot") != snapshot:
            raise ValueError("Saved snapshot differs from trade-plan report")
        ownership = {"observed_at": snapshot["observed_at"], "source_snapshot_sha256": _sha(snapshot_raw),
            **{key: snapshot.get(key, {}) for key in ("held_shares", "pending_buy_shares", "pending_sell_shares", "ownership")}}
    metadata = {"schema_version": VERSION, **deployment_metadata, "producer_id": _producer(producer_id), "action_date": day,
        "symbols": list(_symbols(expected_symbols)), "original_forecast_ids": sorted(forecasts.id.tolist()),
        "source_gameplan_run": source.relative_to(root).as_posix(), "trade_plan_run": trade.relative_to(root).as_posix(),
        "probability_target_contract": probability_target_contract(config),
        "target_price_source_contract": config["target_price_source_contract"],
        "target_price_dataset": forecasts.target_price_dataset.iloc[0],
        "cross_horizon_fallback_policy": policy, "holding_policy": holding, "holding_policy_proof": holding_proof,
        "planning_mode": planning_mode, "planning_config_sha256": trade_manifest["configuration"].get("account_config_sha256"),
        "source_published_at": source_receipt["published_at"], "trade_plan_completed_at": trade_receipt["completed_at"],
        "planning_observed_at": price_path["observed_at"], "ownership_observed_at": ownership["observed_at"] if ownership else None}
    metadata["verified_promoted_groups"] = promoted
    metadata["model_reports_sha256"] = file_checksum(source / "model-reports.json") if promoted else None
    for key in ("direction_policy_version", "direction_up_threshold", "direction_down_threshold"):
        values = forecasts[key].drop_duplicates().tolist()
        if len(values) != 1:
            raise ValueError("Source mixes direction policies")
        metadata[key] = values[0]
    native = {}
    if deployment_proof is not None:
        native["deployment"] = deployment_proof
    for prefix, directory in (("source", source), ("trade_plan", trade)):
        native[prefix] = {}
        for name in ("receipt", "manifest"):
            raw = (directory / f"{name}.json").read_bytes()
            metadata[f"{prefix}_{name}_sha256"] = _sha(raw)
            native[prefix][f"{name}_json"] = raw.decode("utf-8")
    payloads = {"forecasts.parquet": raw_forecasts, "planning-price-path.json": path_raw,
                "ownership.json": _encoded({"evidence": ownership})}
    manifest = {"schema_version": VERSION, "metadata": metadata, "native": native,
        "files": {name: {"sha256": _sha(raw), "size": len(raw)} for name, raw in payloads.items()}}
    # Recheck the native receipts/manifests after all reads. Never mutate them.
    for prefix, directory in (("source", source), ("trade_plan", trade)):
        for name in ("receipt", "manifest"):
            if (file_checksum(directory / f"{name}.json") != metadata[f"{prefix}_{name}_sha256"]
                    or metadata[f"{prefix}_{name}_sha256"] != initial_hashes[str(directory), name]):
                raise ValueError("Native source changed during export")
    for path, expected in deployment_checks.items():
        if (file_checksum(path) if path.exists() else None) != expected:
            raise ValueError("Native source deployment changed during export")
    destination = Path(destination).resolve()
    if destination == source or destination == trade or destination.is_relative_to(source) or destination.is_relative_to(trade):
        raise ValueError("Portable destination cannot modify a native source")
    destination.mkdir(parents=True, exist_ok=False)
    for name, raw in payloads.items():
        (destination / name).write_bytes(raw)
    raw_manifest = _encoded(manifest)
    (destination / "manifest.json").write_bytes(raw_manifest)
    return read_source_bundle(destination, expected_manifest_sha256=_sha(raw_manifest),
        expected_producer=producer_id, expected_symbols=expected_symbols, expected_action_date=day)
