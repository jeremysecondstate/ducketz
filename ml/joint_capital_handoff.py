"""Export verified saved forecasts and planning prices, without account data.

This narrow handoff validates publication and forecast contracts. It does not
reload fitted models, training cohorts, account snapshots, or trading reports,
and does not repeat the producer's model evaluation or authorize execution.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import stat
from typing import Mapping

import pandas as pd

from ml.joint_capital_plan import build_owner_package, publish_owner_package

_JSON_LIMIT = 8 * 1024 * 1024
_PARQUET_LIMIT = 32 * 1024 * 1024
MAX_NATIVE_PRICE_BYTES = 64 * 1024 * 1024
_REVISION = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_FORECAST_FIELDS = frozenset({
    "id", "symbol", "action_date", "model_group", "route", "target_role", "execution_eligible",
    "target_window_start", "target_window_end", "decision_timestamp", "information_available_at",
    "frozen_at", "action_anchor_local", "calibrated_probability", "raw_probability", "direction",
    "model_status", "model_family", "model_artifact", "target_contract_version",
    "target_price_source_contract", "target_price_dataset", "execution_authority", "broker_orders_enabled",
    "symbol_fitted_target_rows", "symbol_route_fitted_target_rows", "assumed_round_trip_cost",
    "probability_target_contract", "gameplan_variant",
})
_PRICE_FIELDS = frozenset({
    "contract_version", "observed_at", "price_source_contract", "price_dataset", "working_half_width_bps",
    "method", "working_range_semantics", "historical_range_semantics", "market_gap_policy",
    "lookback_sessions", "minimum_samples",
})
_POINT_FIELDS = frozenset({
    "symbol", "action_date", "clock_local", "timestamp", "status", "reason", "method",
    "planned_price_low", "planned_price_mid", "planned_price_high", "historical_price_low",
    "historical_price_high", "reference_price", "reference_observed_at", "reference_session",
    "reference_is_synthetic", "reference_effective_at", "reference_original_observed_at",
    "reference_synthetic_reason", "reference_staleness_minutes", "ratio_median", "sample_count",
    "reference_fill_count", "reference_gap_minutes", "reference_completion_status", "reference_completion_reason",
    "endpoint_kind", "observed_only_sample_count", "synthetic_close_sample_count",
})


def _ordinary(path: Path, root: Path, *, directory=False) -> Path:
    root, path = Path(os.path.abspath(root)), Path(os.path.abspath(path))
    if not path.is_relative_to(root):
        raise ValueError("Handoff path escapes its explicitly selected root")
    for item in [*reversed(path.parents), path]:
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Handoff paths cannot contain links or reparse points")
        expected_directory = item != path or directory
        if not (stat.S_ISDIR(info.st_mode) if expected_directory else stat.S_ISREG(info.st_mode)):
            raise ValueError("Handoff requires existing ordinary files and directories")
    return path


def _read(path: Path, root: Path, *, maximum=_JSON_LIMIT) -> bytes:
    selected = _ordinary(path, root)
    with selected.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("Saved handoff input exceeds its size bound")
    return data


def _object(data: bytes) -> dict:
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON metadata key")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("Nonfinite JSON")

    value = json.loads(data, object_pairs_hook=unique_keys, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError("Handoff metadata must be a JSON object")
    return value


def _stamp(value, label):
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError(label + " requires an explicit timezone")
    return stamp.tz_convert("UTC")


def _manifest(data: bytes) -> dict:
    value = _object(data)
    outputs = value.get("output_files")
    if not isinstance(outputs, dict) or not outputs or len(outputs) > 512:
        raise ValueError("Saved manifest output inventory is invalid")
    for name, detail in outputs.items():
        # Native manifests also inventory nested model/source files. Their
        # names must be safe even though this reader never opens those files.
        if (not isinstance(name, str) or "\\" in name or ":" in name
                or any(part in {"", ".", ".."} for part in name.split("/"))
                or not isinstance(detail, dict)):
            raise ValueError("Saved manifest contains an unsafe output path")
    if not isinstance(value.get("configuration"), dict):
        raise ValueError("Saved manifest configuration is missing")
    return value


def _output(run, root, manifest, name, *, maximum=_JSON_LIMIT):
    data = _read(run / name, root, maximum=maximum)
    detail = manifest["output_files"].get(name, {})
    if (type(detail.get("size")) is not int or detail["size"] != len(data)
            or detail.get("checksum_sha256") != sha256(data).hexdigest()):
        raise ValueError("Saved output differs from its manifest: " + name)
    return data


def _source_reference(root, config, receipt, receipt_hash, manifest_hash, *, revision, provenance, provenance_hash):
    saved = [item.get("source_revision") for item in (config, receipt) if item.get("source_revision") is not None]
    if saved:
        if (len(saved) != 2 or len(set(saved)) != 1 or not isinstance(saved[0], str)
                or not _REVISION.fullmatch(saved[0]) or (revision is not None and revision != saved[0])):
            raise ValueError("Recorded source revisions disagree")
        if provenance is not None or provenance_hash is not None:
            raise ValueError("Recorded source revision does not need a second provenance assertion")
        return saved[0], {"kind": "SAVED_ARTIFACT_PROVENANCE", "source_revision": saved[0],
            "receipt_sha256": receipt_hash, "reference_sha256": manifest_hash}
    if revision is None:
        if provenance is not None or provenance_hash is not None:
            raise ValueError("External source provenance requires its explicit revision")
        return None, None
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision) or provenance is None:
        raise ValueError("Caller source revision requires immutable receipt-bound provenance")
    path = Path(provenance)
    data = _read(path if path.is_absolute() else root / path, root)
    if not isinstance(provenance_hash, str) or sha256(data).hexdigest() != provenance_hash:
        raise ValueError("Source provenance differs from its independently selected digest")
    record = _object(data)
    if (record.get("schema_version") != "gameplan-source-provenance-v1"
            or record.get("source_revision") != revision or record.get("receipt_sha256") != receipt_hash):
        raise ValueError("Source provenance does not bind this exact Gameplan receipt")
    return revision, {"kind": "CALLER_PINNED_SOURCE_REFERENCE", "source_revision": revision,
        "receipt_sha256": receipt_hash, "reference_sha256": provenance_hash}


def export_owner_package(datastore_root: Path, *, gameplan_run: Path, trade_plan_run: Path,
                         owner_id: str, output_root: Path, created_at: str,
                         source_revision: str | None = None, source_provenance: Path | None = None,
                         source_provenance_sha256: str | None = None) -> Path:
    """Export selected immutable artifacts without following or changing pointers.

    Missing recorded generation revisions remain UNRECORDED. A supplied source
    revision requires separately selected, receipt-bound provenance; the current
    checkout revision is never used as an artifact generation claim.
    """
    root = _ordinary(Path(datastore_root), Path(datastore_root), directory=True)
    created = _stamp(created_at, "created_at")
    paths = []
    for supplied, folder in ((gameplan_run, "nightly-gameplan-runs"), (trade_plan_run, "gameplan-trade-plan-runs")):
        candidate = Path(supplied)
        run = _ordinary(candidate if candidate.is_absolute() else root / candidate, root, directory=True)
        if run.parent != root / "ml" / folder:
            raise ValueError("Selected publication is outside its immutable run directory")
        paths.append(run)
    game, trade = paths
    inputs = {}

    def saved(path, *, maximum=_JSON_LIMIT):
        data = _read(path, root, maximum=maximum)
        inputs[path] = sha256(data).hexdigest()
        return data

    manifest_bytes, receipt_bytes = saved(game / "manifest.json"), saved(game / "receipt.json")
    manifest, receipt = _manifest(manifest_bytes), _object(receipt_bytes)
    config, day = manifest["configuration"], receipt.get("action_date")
    if (receipt.get("schema_version") != "immutable-overnight-gameplan-receipt-v1"
            or receipt.get("run_path") != game.relative_to(root).as_posix()
            or receipt.get("manifest_checksum_sha256") != sha256(manifest_bytes).hexdigest()
            or receipt.get("run_timestamp") != manifest.get("run_timestamp")
            or config.get("action_date") != day or config.get("preparation_scope") != "STOCK_ONLY"
            or config.get("target_contract_version") != "independent-stock-targets-v1"
            or receipt.get("execution_authority") != "ADVISORY_PAPER_ONLY"
            or receipt.get("broker_orders_enabled") is not False or receipt.get("orders_placed") != 0):
        raise ValueError("Saved Gameplan manifest and receipt disagree")
    published = _stamp(receipt.get("published_at"), "published_at")
    if published > created or _stamp(manifest.get("run_timestamp"), "run_timestamp") > published:
        raise ValueError("Gameplan publication postdates handoff")
    symbols = config.get("symbols")
    if not isinstance(symbols, list) or not symbols or len(set(symbols)) != len(symbols):
        raise ValueError("Saved Gameplan frozen universe is invalid")
    forecast_bytes = _output(game, root, manifest, "forecasts.parquet", maximum=_PARQUET_LIMIT)
    inputs[game / "forecasts.parquet"] = sha256(forecast_bytes).hexdigest()
    from ml.stock_trader.independent_signals import _REQUIRED_COLUMNS, _validated_independent_forecasts, late_publication_time
    from ml.stock_trader.gameplan_execution import _validated_instructions
    late_published = late_publication_time(config, receipt)
    frame = _validated_independent_forecasts(pd.read_parquet(BytesIO(forecast_bytes)), action_date=day, symbols=tuple(symbols),
        late_publication_at=late_published)
    if frame[list(_REQUIRED_COLUMNS - {"action_anchor_local"})].isna().any().any():
        raise ValueError("Required forecast fields cannot be null")
    if frame.frozen_at.gt(published).any():
        raise ValueError("Frozen forecasts postdate their publication")
    _validated_instructions(frame)
    from ml.gameplan_probability_target import probability_target_contract, probability_target_metadata
    metadata = probability_target_metadata(probability_target_contract(config))
    if (probability_target_contract(frame) != metadata["probability_target_contract"]
            or probability_target_contract(receipt) != metadata["probability_target_contract"]):
        raise ValueError("Forecast probability contract differs from manifest")
    if "probability_target_contract" in config:
        if any(config.get(k) != v or receipt.get(k) != v or k not in frame or not frame[k].eq(v).all()
               for k, v in metadata.items()):
            raise ValueError("Forecast probability metadata is inconsistent")
    if not frame.target_price_source_contract.eq(config.get("target_price_source_contract")).all():
        raise ValueError("Forecast price source differs from manifest")
    trade_manifest_bytes, trade_receipt_bytes = saved(trade / "manifest.json"), saved(trade / "receipt.json")
    trade_manifest, trade_receipt = _manifest(trade_manifest_bytes), _object(trade_receipt_bytes)
    trade_config = trade_manifest["configuration"]
    binding = {"schema_version": "cash-aware-gameplan-trade-planning-v4", "action_date": day,
        "source_gameplan_run": game.relative_to(root).as_posix(), "source_receipt_sha256": sha256(receipt_bytes).hexdigest(),
        "execution_authority": "REVIEW_ONLY_REVALIDATE_AT_ENTRY", "orders_placed": 0, "broker_orders_enabled": False}
    if (any(trade_config.get(k) != v or trade_receipt.get(k) != v for k, v in binding.items())
            or trade_receipt.get("broker_orders_enabled") is not False or trade_config.get("broker_orders_enabled") is not False
            or trade_receipt.get("status") != "COMPLETE" or trade_receipt.get("forecast_rows") != len(frame)
            or trade_receipt.get("run_path") != trade.relative_to(root).as_posix()
            or trade_receipt.get("manifest_sha256") != sha256(trade_manifest_bytes).hexdigest()):
        raise ValueError("Trade-plan receipt does not bind this exact Gameplan")
    completed = _stamp(trade_receipt.get("completed_at"), "completed_at")
    if completed > created or _stamp(trade_manifest.get("run_timestamp"), "run_timestamp") > completed:
        raise ValueError("Trade-plan publication postdates handoff")
    fallback = config.get("cross_horizon_fallback_policy")
    if any(item.get("cross_horizon_fallback_policy") != fallback for item in (receipt, trade_config, trade_receipt)):
        raise ValueError("Frozen fallback policy differs between publications")
    price_bytes = _output(trade, root, trade_manifest, "planning-price-path.json", maximum=MAX_NATIVE_PRICE_BYTES)
    inputs[trade / "planning-price-path.json"] = sha256(price_bytes).hexdigest()
    prices = _object(price_bytes)
    if (prices.get("price_source_contract") != config.get("target_price_source_contract")
            or not isinstance(prices.get("points"), dict)
            or _stamp(prices.get("observed_at"), "price observed_at") > completed):
        raise ValueError("Planning prices differ from the frozen source contract")
    # Deliberate allowlists exclude account fields, unknown extensions and raw
    # historical samples while retaining disclosed planning-reference quality.
    public_prices = {key: value for key, value in prices.items() if key in _PRICE_FIELDS}
    if "reference_completion" in prices:
        completion = prices["reference_completion"]
        if not isinstance(completion, dict):
            raise ValueError("Planning reference completion is invalid")
        public_prices["reference_completion"] = {key: value for key, value in completion.items()
            if key in {"contract_version", "observed_at", "maximum_gap_minutes", "maximum_fill_bars",
                "synthetic_bar_count", "synthetic_reference_count", "unavailable_reference_count",
                "available_observed_count", "available_synthetic_count", "policy", "status"}
            and (value is None or type(value) in (str, bool, int, float))}
    public_prices["points"] = {key: {k: v for k, v in point.items() if k in _POINT_FIELDS}
        for key, point in prices["points"].items() if isinstance(point, dict)}
    revision, reference = _source_reference(root, config, receipt, sha256(receipt_bytes).hexdigest(),
        sha256(manifest_bytes).hexdigest(), revision=source_revision, provenance=source_provenance,
        provenance_hash=source_provenance_sha256)
    package = build_owner_package(owner_id=owner_id, run_id=game.name, source_revision=revision,
        action_date=day, frozen_symbols=symbols, created_at=created.isoformat(),
        source_hashes={"receipt_sha256": sha256(receipt_bytes).hexdigest(), "manifest_sha256": sha256(manifest_bytes).hexdigest(),
            "forecasts_sha256": sha256(forecast_bytes).hexdigest(), "price_path_sha256": sha256(price_bytes).hexdigest()},
        forecasts=frame[[name for name in frame if name in _FORECAST_FIELDS]].to_dict("records"),
        price_path=public_prices, cross_horizon_fallback_policy=fallback, source_reference=reference,
        late_publication_at=late_published)
    for path, expected in inputs.items():
        maximum = (MAX_NATIVE_PRICE_BYTES if path == trade / "planning-price-path.json" else
                   _PARQUET_LIMIT if path.suffix == ".parquet" else _JSON_LIMIT)
        if sha256(_read(path, root, maximum=maximum)).hexdigest() != expected:
            raise ValueError("Selected handoff input changed during validation")
    _ordinary(Path(output_root), Path(output_root), directory=True)
    return publish_owner_package(Path(output_root), package)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("datastore", "gameplan-run", "trade-plan-run", "output-root"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--created-at", required=True)
    parser.add_argument("--source-revision")
    parser.add_argument("--source-provenance", type=Path)
    parser.add_argument("--source-provenance-sha256")
    args = parser.parse_args(argv)
    path = export_owner_package(args.datastore, gameplan_run=args.gameplan_run, trade_plan_run=args.trade_plan_run,
        owner_id=args.owner_id, output_root=args.output_root, created_at=args.created_at,
        source_revision=args.source_revision, source_provenance=args.source_provenance,
        source_provenance_sha256=args.source_provenance_sha256)
    print(json.dumps({"status": "EXPORTED", "package_file": str(path), "orders_placed": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
