"""Latch an explicitly installed local experiment for the next manual session.

Loading is read-only and independent of model-supervision policies. The private
recipe selects the observed-only candidate Jeremy authorized for live use; it
does not claim a research promotion or alter an already running session.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

import numpy as np
import pandas as pd

from .data import FEATURE_NAMES, FEATURE_VERSION as BASE_FEATURE_VERSION
from .models import (
    ModelSpec, configured_models, fit_ensemble, freeze_model_spec, purged_chronological_split,
)
from .observed_data import (
    FEATURE_POLICY, FEATURE_VERSION, LABEL_PROVENANCE, build_observed_dataset,
)
from .prediction import ContractError, TARGET_VERSION, instant
from .training_timing import (LEGACY, HISTORICAL_RESEARCH, OPERATIONAL_REPLAY,
                              timing_contract, timing_mode_from_metadata)


SCHEMA_VERSION = "intraday-manual-observed-calibrated-v1"
FROZEN_SCHEMA_VERSION = "intraday-manual-observed-calibrated-v2"
NATIVE_SCHEMA_VERSION = "intraday-manual-observed-calibrated-v3"
MARKET_SCHEMA_VERSION = "intraday-manual-observed-market-context-v4"
SELECTION = "authorized_human_live_experiment"
MAX_ROWS = 4096
MIN_ROWS = 160
MIN_DATES = 5
MIN_CLASS = 5
MIN_CALIBRATION_ROWS = 32


def _specs():
    return (
        ModelSpec("logistic_strong", family="logistic", parameters={"C": 0.05},
                  probability_calibration="sigmoid", calibration_fraction=0.25,
                  training_max_rows=MAX_ROWS),
        ModelSpec("forest_shallow", family="random_forest", parameters={
            "n_estimators": 64, "max_depth": 3, "min_samples_leaf": 16},
            probability_calibration="sigmoid", calibration_fraction=0.25,
            training_max_rows=MAX_ROWS),
    )


def _legacy_spec_payload(spec):
    # Preserve the v1 private checksum and ResearchConfig/cycle identity exactly.
    return {key: value for key, value in asdict(spec).items() if key != "runtime_contract"}


def _feature_contract():
    return {"version": "intraday-observed-feature-contract-v1",
            "base_feature_version": BASE_FEATURE_VERSION,
            "feature_version": FEATURE_VERSION, "feature_policy": FEATURE_POLICY,
            "feature_names": list(FEATURE_NAMES), "label_provenance": LABEL_PROVENANCE,
            "synthetic_policy": "exclude", "target_version": TARGET_VERSION}


def _native_feature_contract():
    from .native_interval import CONTRACT, FEATURE_VERSION, FEATURE_POLICY, LABEL_PROVENANCE
    return {**_feature_contract(), "version": "intraday-covered-native-feature-contract-v1",
            "feature_version": FEATURE_VERSION, "feature_policy": FEATURE_POLICY,
            "label_provenance": LABEL_PROVENANCE, "native_interval_contract": CONTRACT}


def _dataset_scope(value):
    if (not isinstance(value, dict) or set(value) != {"source_dataset", "lookback_days"}
            or value["source_dataset"] != "EQUS.MINI"
            or value["lookback_days"] is not None and
            (type(value["lookback_days"]) is not int or value["lookback_days"] <= 0)):
        raise ContractError("Frozen manual recipe requires an explicit supported source/history scope")
    return deepcopy(value)


def _validate_support(value):
    required = {"newest_training_rows", "minimum_rows", "minimum_training_dates",
                "minimum_each_class", "minimum_calibration_rows", "calibration"}
    if not isinstance(value, dict) or set(value) != required:
        raise ContractError("Manual recipe training support contract is invalid")
    floors = {"minimum_rows": 160, "minimum_training_dates": 5,
              "minimum_each_class": 5, "minimum_calibration_rows": 32}
    if (any(type(value[key]) is not int or value[key] < floor for key, floor in floors.items())
            or type(value["newest_training_rows"]) is not int
            or not value["minimum_rows"] <= value["newest_training_rows"] <= 4096
            or value["calibration"] != "purged_chronological_sigmoid"):
        raise ContractError("Manual recipe cannot weaken the observed training/calibration evidence gates")
    return deepcopy(value)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _scope(pc, symbols):
    values = tuple(symbols)
    if (pc not in {"Scout", "Atlas"} or not values or len(values) != len(set(values))
            or any(not isinstance(s, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,19}", s)
                   for s in values)):
        raise ContractError("Manual model recipe requires this PC's exact unique symbol scope")
    return values


def make_manual_recipe(*, pc, symbols, installed_at, authority_reference):
    """Build the private installation payload; this helper writes no files."""
    symbols = _scope(pc, symbols)
    timestamp = instant(installed_at).isoformat()
    if not isinstance(authority_reference, str) or not authority_reference.strip():
        raise ContractError("Manual model recipe requires the actual human authority reference")
    value = {
        "schema_version": SCHEMA_VERSION,
        "selection": SELECTION,
        "pc": pc,
        "symbols": list(symbols),
        "installed_at": timestamp,
        "authority_reference": authority_reference,
        "target_version": TARGET_VERSION,
        "feature_version": FEATURE_VERSION,
        "feature_policy": FEATURE_POLICY,
        "label_provenance": LABEL_PROVENANCE,
        "synthetic_policy": "exclude",
        "models": [_legacy_spec_payload(spec) for spec in _specs()],
        "training_support": {
            "newest_training_rows": MAX_ROWS,
            "minimum_rows": MIN_ROWS,
            "minimum_training_dates": MIN_DATES,
            "minimum_each_class": MIN_CLASS,
            "minimum_calibration_rows": MIN_CALIBRATION_ROWS,
            "calibration": "purged_chronological_sigmoid",
        },
    }
    return {**value, "sha256": _sha(value)}


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ContractError("Manual model recipe contains duplicate JSON fields")
        value[key] = item
    return value


def _validate_recipe(value, *, pc, symbols):
    if not isinstance(value, dict):
        raise ContractError("Manual model recipe must be a JSON object")
    payload = {key: item for key, item in value.items() if key != "sha256"}
    try:
        checksum = _sha(payload)
    except (TypeError, ValueError) as exc:
        raise ContractError("Manual model recipe contains noncanonical values") from exc
    if value.get("sha256") != checksum:
        raise ContractError("Manual model recipe checksum mismatch")
    if value.get("schema_version") in {FROZEN_SCHEMA_VERSION, NATIVE_SCHEMA_VERSION, MARKET_SCHEMA_VERSION}:
        symbols = _scope(pc, symbols)
        required = {"schema_version", "selection", "pc", "symbols", "installed_at", "authority_reference",
                    "target_version", "feature_version", "feature_policy", "label_provenance",
                    "synthetic_policy", "models", "training_support", "feature_contract",
                    "dataset_scope", "migrated_from_sha256", "sha256"}
        if "training_timing_contract" in value:
            required.add("training_timing_contract")
            if timing_mode_from_metadata(value) not in {HISTORICAL_RESEARCH, OPERATIONAL_REPLAY}:
                raise ContractError("A migrated recipe must explicitly select research or exact replay")
        if value["schema_version"] in {NATIVE_SCHEMA_VERSION, MARKET_SCHEMA_VERSION}:
            required.add("symbol_feature_contracts")
            selected = value.get("symbol_feature_contracts")
            if (not isinstance(selected, dict) or not selected or not set(selected).issubset(symbols)
                    or any(contract != _native_feature_contract() for contract in selected.values())):
                raise ContractError("Native manual recipe requires explicit local symbol feature contracts")
        if value["schema_version"] == MARKET_SCHEMA_VERSION:
            from .market_context import validate_market_context_contract
            required.add("market_context_contract")
            required.add("market_collection_configuration_sha256")
            context = value.get("market_context_contract")
            if (not isinstance(context, dict)
                    or context != validate_market_context_contract(context)
                    or set(selected) != set(symbols)
                    or not isinstance(value.get("market_collection_configuration_sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", value["market_collection_configuration_sha256"])):
                raise ContractError("Market context requires a frozen source contract and native observations for every local symbol")
            if "historical_recovery_contract" in value:
                from .native_interval import RECOVERY_CONTRACT
                from .market_context import validate_market_context_contract
                required.add("historical_recovery_contract")
                if (value["historical_recovery_contract"] != RECOVERY_CONTRACT
                        or context != validate_market_context_contract(context)):
                    raise ContractError("Historical recovery requires an explicit supported local context contract")
        contract = _feature_contract()
        if (set(value) != required or value["selection"] != SELECTION
                or value["pc"] != pc or value["symbols"] != list(symbols)
                or value["feature_contract"] != contract
                or any(value[key] != contract[key] for key in
                       ("target_version", "feature_version", "feature_policy", "label_provenance", "synthetic_policy"))
                or not isinstance(value["authority_reference"], str) or not value["authority_reference"].strip()
                or not isinstance(value["migrated_from_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", value["migrated_from_sha256"])):
            raise ContractError("Frozen manual recipe scope or feature contract is incompatible")
        instant(value["installed_at"])
        _dataset_scope(value["dataset_scope"])
        support = _validate_support(value["training_support"])
        specs = configured_models(value["models"])
        if (len(specs) > 16 or any(spec.runtime_contract is None
                or spec.probability_calibration != "sigmoid"
                or spec.training_max_rows != support["newest_training_rows"] for spec in specs)):
            raise ContractError("Frozen manual recipe requires complete calibrated model contracts")
        if (len({spec.calibration_fraction for spec in specs}) != 1
                or "training_timing_contract" not in value and any(spec.calibration_fraction != .25 for spec in specs)):
            raise ContractError("A different common calibration fraction requires explicit local timing migration")
        for spec in specs:
            freeze_model_spec(spec)  # Validate compatibility without fitting or changing the saved specification.
        return deepcopy(value)
    expected = make_manual_recipe(pc=pc, symbols=symbols,
        installed_at=value.get("installed_at"),
        authority_reference=value.get("authority_reference"))
    # Canonical comparison also rejects booleans in numeric fields, arbitrary
    # extra fields, changed model settings, and peer or partial symbol scopes.
    if _canonical(value) != _canonical(expected):
        raise ContractError("Manual model recipe scope, fields, or fixed candidate settings differ")
    return deepcopy(value)


def migrate_manual_recipe(value, *, pc, symbols, config, installed_at=None, authority_reference=None):
    """Return a reviewed-installation candidate; never write, fit or activate it.

    v1 retains its checksum and interpretation while being read. Migration
    snapshots this machine's effective parameters, preprocessing and calibrator,
    rather than importing a peer's settings or selecting another candidate.
    """
    original = _validate_recipe(value, pc=pc, symbols=symbols)
    if original["schema_version"] in {NATIVE_SCHEMA_VERSION, MARKET_SCHEMA_VERSION}:
        raise ContractError("A native manual recipe requires its explicit symbol-scoped migration")
    frozen = deepcopy(original)
    frozen.update(schema_version=FROZEN_SCHEMA_VERSION,
                  models=[asdict(freeze_model_spec(spec)) for spec in configured_models(original["models"])],
                  feature_contract=_feature_contract(),
                  dataset_scope=_dataset_scope(original.get("dataset_scope", {
                      "source_dataset": config.source_dataset, "lookback_days": config.lookback_days})),
                  migrated_from_sha256=original["sha256"])
    if installed_at is not None:
        frozen["installed_at"] = instant(installed_at).isoformat()
    if authority_reference is not None:
        frozen["authority_reference"] = authority_reference
    frozen.pop("sha256", None)
    frozen["sha256"] = _sha(frozen)
    return _validate_recipe(frozen, pc=pc, symbols=symbols)


def migrate_native_interval_recipe(value, *, pc, symbols, native_symbols, installed_at, authority_reference):
    """Select a reviewed local population correction without changing model settings."""
    original = _validate_recipe(value, pc=pc, symbols=symbols)
    if original["schema_version"] not in {FROZEN_SCHEMA_VERSION, NATIVE_SCHEMA_VERSION}:
        raise ContractError("Freeze the local model parameters before changing its population contract")
    selected = tuple(native_symbols)
    if not selected or len(set(selected)) != len(selected) or not set(selected).issubset(original["symbols"]):
        raise ContractError("Native population selection must be an explicit subset of this PC's symbols")
    revised = {**deepcopy(original), "schema_version": NATIVE_SCHEMA_VERSION,
        "installed_at": instant(installed_at).isoformat(), "authority_reference": authority_reference,
        "migrated_from_sha256": original["sha256"],
        "symbol_feature_contracts": {symbol: _native_feature_contract() for symbol in selected}}
    revised.pop("sha256", None)
    revised["sha256"] = _sha(revised)
    return _validate_recipe(revised, pc=pc, symbols=symbols)


def native_interval_symbols_for_runtime(model_runtime):
    """Read only the validated startup adapter, never mutable disk selection."""
    return tuple(getattr(model_runtime.get("dataset_builder"), "native_interval_symbols", ()))


def migrate_market_context_recipe(value, *, pc, symbols, source_ids, collection_configuration_sha256,
                                  installed_at, authority_reference, historical_recovery_contract=None):
    """Explicit local population/feature migration; retain fitted model settings and gates.

    No current context is backdated into historical training. Missing context has
    explicit indicator features until actual point-in-time observations accrue.
    This helper never writes a recipe, starts a trader, or promotes a model.
    """
    from .market_context import market_context_contract
    original = _validate_recipe(value, pc=pc, symbols=symbols)
    if original["schema_version"] not in {FROZEN_SCHEMA_VERSION, NATIVE_SCHEMA_VERSION, MARKET_SCHEMA_VERSION}:
        raise ContractError("Freeze this PC's effective models before changing data inputs")
    revised = {**deepcopy(original), "schema_version": MARKET_SCHEMA_VERSION,
        "installed_at": instant(installed_at).isoformat(), "authority_reference": authority_reference,
        "migrated_from_sha256": original["sha256"],
        "symbol_feature_contracts": {symbol: _native_feature_contract() for symbol in symbols},
        "market_collection_configuration_sha256": collection_configuration_sha256,
        "market_context_contract": market_context_contract(source_ids)}
    if historical_recovery_contract is not None:
        revised["historical_recovery_contract"] = historical_recovery_contract
    revised.pop("sha256", None)
    revised["sha256"] = _sha(revised)
    return _validate_recipe(revised, pc=pc, symbols=symbols)


def manual_recipe_migration_evidence(original, migrated, *, pc, symbols, config):
    """Compare the exact effective specifications without fitting either recipe."""
    previous = _validate_recipe(original, pc=pc, symbols=symbols)
    current = _validate_recipe(migrated, pc=pc, symbols=symbols)
    resolved = [asdict(freeze_model_spec(spec)) for spec in configured_models(previous["models"])]
    return {"original_sha256": previous["sha256"], "migrated_sha256": current["sha256"],
            "effective_models_identical": _canonical(resolved) == _canonical(current["models"]),
            "training_support_identical": previous["training_support"] == current["training_support"],
            "feature_contract_identical": (_feature_contract() == current.get("feature_contract")
                and previous.get("symbol_feature_contracts", {}) == current.get("symbol_feature_contracts", {})),
            "symbol_feature_contracts_identical": previous.get("symbol_feature_contracts", {}) == current.get("symbol_feature_contracts", {}),
            "dataset_scope_identical": previous.get("dataset_scope", {
                "source_dataset": config.source_dataset, "lookback_days": config.lookback_days}) == current.get("dataset_scope"),
            "provider_calls": 0, "training_runs": 0, "files_written": 0}


def migrate_training_timing_recipe(value, *, pc, symbols, installed_at, authority_reference,
                                    mode=HISTORICAL_RESEARCH):
    """Select a local timing contract while retaining every model and gate."""
    original = _validate_recipe(value, pc=pc, symbols=symbols)
    if original["schema_version"] == SCHEMA_VERSION or mode not in {HISTORICAL_RESEARCH, OPERATIONAL_REPLAY}:
        raise ContractError("Explicit training timing requires a frozen recipe and supported mode")
    revised = {**deepcopy(original), "installed_at": instant(installed_at).isoformat(),
        "authority_reference": authority_reference, "migrated_from_sha256": original["sha256"],
        "training_timing_contract": timing_contract(mode)}
    revised.pop("sha256", None)
    revised["sha256"] = _sha(revised)
    return _validate_recipe(revised, pc=pc, symbols=symbols)


class ManualRecipeSupportError(ContractError):
    """Bounded rejected-input evidence survives the runtime's string-only error log."""
    def __init__(self, message, diagnostics):
        self.diagnostics = deepcopy(diagnostics)
        super().__init__(message + "; support_diagnostics=" + _canonical(self.diagnostics))


def _training_support(y, reference_at, target_close_at, label_available_at, *, cutoff, support=None,
                      diagnostic_context=None, timing_mode=LEGACY, calibration_fraction=.25):
    support = _validate_support(support or {
        "newest_training_rows": MAX_ROWS, "minimum_rows": MIN_ROWS, "minimum_training_dates": MIN_DATES,
        "minimum_each_class": MIN_CLASS, "minimum_calibration_rows": MIN_CALIBRATION_ROWS,
        "calibration": "purged_chronological_sigmoid"})
    minimum, maximum = support["minimum_rows"], support["newest_training_rows"]
    min_dates, min_class = support["minimum_training_dates"], support["minimum_each_class"]
    min_calibration = support["minimum_calibration_rows"]
    diagnostics = {"schema_version": "intraday-manual-support-rejection-v1",
                   "requirements": support, "context": deepcopy(diagnostic_context or {})}
    reasons = []

    def reject(message, *failed):
        diagnostics["reasons"] = list(failed)
        raise ManualRecipeSupportError(message, diagnostics)

    try:
        labels = np.asarray(y)
    except (TypeError, ValueError):
        diagnostics["label_shape"] = None
        reject("Observed recipe requires a regular label array", "invalid_label_array")
    diagnostics["label_shape"] = list(labels.shape)
    rows = len(labels) if labels.ndim else 0
    diagnostics["training_rows"] = rows
    if labels.ndim != 1:
        reasons.append("label_dimension_mismatch")
    if rows < minimum:
        reasons.append("training_rows_below_minimum")
    if rows > maximum:
        reasons.append("training_rows_above_maximum")
    binary = np.isin(labels, [0, 1])
    diagnostics["nonbinary_labels"] = int((~binary).sum())
    if not binary.all():
        reasons.append("nonbinary_labels")
    counts = {str(v): int((labels == v).sum()) for v in (0, 1)}
    diagnostics["class_counts"] = counts
    timestamps = {}
    for name, values in (("reference_at", reference_at), ("target_close_at", target_close_at),
                         ("label_available_at", label_available_at)):
        try:
            values = tuple(values)
        except TypeError:
            diagnostics[name + "_length"] = None
            reasons.append(name + "_not_array")
            continue
        diagnostics[name + "_length"] = len(values)
        if len(values) != rows:
            reasons.append(name + "_length_mismatch")
        try:
            timestamps[name] = tuple(instant(v) for v in values)
        except (TypeError, ValueError, OverflowError):
            reasons.append(name + "_invalid_timestamp")
    try:
        selected_cutoff = instant(cutoff)
        diagnostics["cutoff"] = selected_cutoff.isoformat()
    except (TypeError, ValueError, OverflowError):
        selected_cutoff = None
        diagnostics["cutoff"] = None
        reasons.append("invalid_cutoff")
    references = timestamps.get("reference_at", ())
    targets = timestamps.get("target_close_at", ())
    available = timestamps.get("label_available_at", ())
    dates = {pd.Timestamp(v).tz_convert("America/New_York").date() for v in references}
    diagnostics["training_dates"] = len(dates)
    diagnostics["latest_label_available_at"] = max(available).isoformat() if available else None
    diagnostics["invalid_target_windows"] = sum(
        t != r + pd.Timedelta(minutes=45) for r, t in zip(references, targets))
    diagnostics["labels_after_cutoff"] = (sum(a > selected_cutoff for a in available)
                                           if selected_cutoff is not None else None)
    train, calibration, split_error = None, None, None
    # A deficient overall population can still expose its real purged support.
    # Malformed inputs never enter the splitter or index another array.
    if (labels.ndim == 1 and len(references) == len(targets) == len(available) == rows
            and rows >= 4 and len(timestamps) == 3):
        try:
            train, calibration = purged_chronological_split(references, targets,
                fraction=calibration_fraction, label_available_at=available, timing_mode=timing_mode)
        except ContractError as exc:
            split_error = str(exc)
    diagnostics["split_error"] = split_error
    partitions = {}
    if train is not None:
        for name, indices in (("base", train), ("calibration", calibration)):
            partitions[name] = {"rows": len(indices),
                "class_counts": {str(v): int((labels[indices] == v).sum()) for v in (0, 1)}}
    diagnostics["partitions"] = partitions
    diagnostics["purged_rows"] = rows - len(train) - len(calibration) if train is not None else None
    if reasons:
        reject(f"Observed recipe requires {minimum} to {maximum} matching real training rows", *reasons)
    if diagnostics["labels_after_cutoff"]:
        reject("Observed recipe training labels are not yet available at cutoff", "labels_after_cutoff")
    if diagnostics["invalid_target_windows"]:
        reject("Observed recipe requires exact 45-minute label windows", "invalid_target_windows")
    if len(dates) < min_dates:
        reject(f"Observed recipe requires at least {min_dates} actual training dates", "training_dates_below_minimum")
    if min(counts.values()) < min_class:
        reject(f"Observed recipe requires at least {min_class} examples of each target class", "class_count_below_minimum")
    if split_error is not None:
        reject(split_error, "invalid_chronological_split")
    for name, partition in partitions.items():
        failed = []
        if partition["rows"] < min_calibration:
            failed.append(name + "_rows_below_minimum")
        if min(partition["class_counts"].values()) < min_class:
            failed.append(name + "_class_count_below_minimum")
        if failed:
            reject(f"Observed recipe {name} partition requires at least "
                f"{min_calibration} rows and {min_class} examples of each class after the purge", *failed)
    return {"training_rows": len(labels), "training_dates": len(dates),
            "class_counts": counts, "purged_rows": len(labels) - len(train) - len(calibration),
            **partitions}


class ManualRecipeFitError(ContractError):
    """A rejected fit retains all real member results, without a survivor mean."""
    def __init__(self, result, *, evidence_path=None, persistence_error=None):
        self.result = result
        self.evidence_path = evidence_path
        evidence = {"configured_count": result.get("configured_count"),
            "successful_count": result.get("successful_count"),
            "complete": result.get("complete"), "consensus_status": result.get("consensus_status"),
            "members": [{key: out.get(key) for key in
                ("name", "status", "error", "artifact_path", "artifact_sha256")}
                for out in result.get("outputs", [])],
            "evidence_path": evidence_path, "persistence_error": persistence_error}
        super().__init__("Observed manual recipe requires every configured calibrated model; "
            "no partial forecast or fallback: " + _canonical(evidence))


def _persist_rejected(result, *, artifact_dir, run_id):
    if artifact_dir is None:
        return None
    if not run_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", str(run_id)):
        raise ContractError("Rejected fit evidence requires a safe immutable run identity")
    directory = Path(artifact_dir) / str(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / "manual-recipe-rejected-ensemble.json"
    descriptor, name = tempfile.mkstemp(prefix=".rejected-fit-", suffix=".json", dir=directory)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(_canonical(result) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return str(destination.resolve())


def prepare_manual_models(config, *, datastore, pc, symbols):
    """Read a local recipe once and return startup-only runner adapters.

    Absence preserves the caller's original configuration and creates nothing.
    A malformed installed recipe fails explicitly instead of silently selecting
    old or partially configured models. Editing the file affects a later manual
    startup only; the returned adapters retain their own validated copy.
    """
    symbols = _scope(pc, symbols)
    path = Path(datastore) / "intraday" / pc.lower() / "manual-model-recipe.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return config, {}
    try:
        recipe = _validate_recipe(json.loads(raw, object_pairs_hook=_unique_object), pc=pc, symbols=symbols)
    except (ValueError, TypeError, KeyError) as exc:
        raise ContractError("Invalid manual model recipe: " + str(exc)) from exc
    specs = configured_models(recipe["models"])
    support_contract = deepcopy(recipe["training_support"])
    timing_mode = timing_mode_from_metadata(recipe)
    calibration_fraction = specs[0].calibration_fraction
    max_rows = support_contract["newest_training_rows"]
    feature_contract = deepcopy(recipe.get("feature_contract", _feature_contract()))
    symbol_contracts = deepcopy(recipe.get("symbol_feature_contracts", {}))
    context_contract = deepcopy(recipe.get("market_context_contract"))
    recovery_contract = recipe.get("historical_recovery_contract")
    expected_features = (tuple(FEATURE_NAMES) + tuple(context_contract["feature_names"])
                         if context_contract is not None else tuple(FEATURE_NAMES))
    frozen_scope = deepcopy(recipe.get("dataset_scope"))
    selected_config = replace(config, models=tuple(deepcopy(recipe["models"])),
                              synthetic_policy="exclude", use_supervised_models=False).validate()
    if frozen_scope is not None:
        selected_config = replace(selected_config, **frozen_scope).validate()

    def dataset_builder(candles, *, cutoff, reference_at, lookback_days=None, synthetic_policy="exclude",
                        _assessment=False, _preparation=False):
        if _assessment and _preparation:
            raise ContractError("Assessment history and non-trading preparation are distinct modes")
        if synthetic_policy != "exclude":
            raise ContractError("Observed manual recipe cannot admit synthetic labels or feature candles")
        if feature_contract != _feature_contract():
            raise ContractError("Manual recipe feature contract changed after session startup")
        if frozen_scope is not None and lookback_days != frozen_scope["lookback_days"]:
            raise ContractError("Manual recipe dataset history scope differs from its frozen local selection")
        symbol = None
        if symbol_contracts:
            populations = set(candles["symbol"].unique())
            if len(populations) != 1 or not populations.issubset(symbols):
                raise ContractError("Manual recipe cannot train a symbol outside this PC's exact scope")
            symbol = next(iter(populations))
        selected_contract = symbol_contracts.get(symbol, feature_contract)
        if symbol in symbol_contracts:
            from .native_interval import build_native_interval_dataset
            dataset = build_native_interval_dataset(candles, cutoff=cutoff, reference_at=reference_at,
                lookback_days=lookback_days, recovery_contract=recovery_contract, preparation_only=_preparation,
                timing_mode=timing_mode)
        else:
            if candles is not None and "native_interval_contract" in candles and candles.native_interval_contract.notna().any():
                raise ContractError("A default symbol cannot consume the opt-in native population")
            dataset = build_observed_dataset(candles, cutoff=cutoff, reference_at=reference_at,
                lookback_days=lookback_days, preparation_only=_preparation, timing_mode=timing_mode)
        if any(dataset.manifest.get(k) != selected_contract[k] for k in
               ("feature_version", "feature_policy", "label_provenance")):
            raise ContractError("Manual dataset differs from its frozen per-symbol feature contract")
        if dataset.inference.get("symbol") not in symbols:
            raise ContractError("Observed manual recipe cannot train a symbol outside this PC's scope")
        symbol = dataset.inference["symbol"]
        from .assessment_reservation import load_reservation, protect_dataset
        dataset = protect_dataset(dataset, load_reservation(datastore, pc=pc, symbol=symbol))
        if (frozen_scope is not None
                and dataset.inference.get("source_dataset") != frozen_scope["source_dataset"]):
            raise ContractError("Manual recipe source dataset differs from its frozen local selection")
        input_rows_before_decision_filter = len(dataset.y)
        decisions = None
        if context_contract is not None:
            # A recovered historical observation is acquired now and can only
            # train after that receipt. Its reconstructed feature cutoff names
            # the historical reference close, never today's receipt. Native
            # contract validation proves the tag, cutoff and retained receipts.
            decisions = {instant(row["opened_at"]): instant(
                instant(row["opened_at"]) + pd.Timedelta(minutes=15) if timing_mode == HISTORICAL_RESEARCH
                else row["feature_cutoff_at"] if recovery_contract is not None
                    and row.get("availability_kind") == "actual_historical_recovery"
                else row["available_at"]) for row in candles.to_dict("records")}
            causal = np.asarray([instant(reference) + pd.Timedelta(minutes=15)
                                 <= decisions[instant(reference)]
                                 < instant(reference) + pd.Timedelta(minutes=30)
                                 for reference in dataset.reference_at], dtype=bool)
            # A delayed reference that arrived during/after its target cannot
            # supply that decision's context. Exclude that row, not every later
            # fit, then apply the unchanged support gates to eligible rows.
            dataset = replace(dataset, X=dataset.X[causal], y=dataset.y[causal],
                label_available_at=dataset.label_available_at[causal],
                training_reference_at=dataset.reference_at[causal],
                target_close_at=dataset.target_close_at[causal])
        input_rows = len(dataset.y)
        runtime_dataset = replace(dataset, X=dataset.X[-max_rows:], y=dataset.y[-max_rows:],
            label_available_at=dataset.label_available_at[-max_rows:],
            training_reference_at=dataset.reference_at[-max_rows:],
            target_close_at=dataset.target_close_at[-max_rows:])
        # Chronological assessment needs earlier rows for each fold's own
        # 4096-row fit window. Capping once at the final decision silently
        # shortens earlier fits. Keep a bounded assessment history separately;
        # the runtime trainer below explicitly rejects it.
        history_limit = max(8192, max_rows) if _assessment else max_rows
        dataset = replace(dataset, X=dataset.X[-history_limit:], y=dataset.y[-history_limit:],
            label_available_at=dataset.label_available_at[-history_limit:],
            training_reference_at=dataset.reference_at[-history_limit:],
            target_close_at=dataset.target_close_at[-history_limit:]) if _assessment else runtime_dataset
        support = _training_support(runtime_dataset.y, runtime_dataset.reference_at, runtime_dataset.target_close_at,
            runtime_dataset.label_available_at, cutoff=cutoff, support=support_contract, timing_mode=timing_mode,
            calibration_fraction=calibration_fraction,
            diagnostic_context={"symbol": dataset.inference.get("symbol"),
                "reference_at": instant(reference_at).isoformat(), "feature_shape": list(runtime_dataset.X.shape),
                "inference_shape": list(dataset.inference_X.shape), "input_training_rows": input_rows,
                "recipe_sha256": recipe["sha256"], "feature_version": dataset.manifest.get("feature_version"),
                "source_dataset": dataset.inference.get("source_dataset"),
                "source_history_sha256": _sha(dataset.manifest.get("source_history", {})),
                "lookback_days": lookback_days, "synthetic_policy": synthetic_policy})
        if context_contract is not None:
            from .market_collection import load_snapshots
            from .market_context import augment_dataset
            # A label becomes available after its decision. It must never be
            # used as the as-of join clock for that decision's features.
            earliest = min(decisions[instant(reference)] for reference in dataset.reference_at)
            snapshots = load_snapshots(datastore, symbol=symbol, cutoff=cutoff, pc=pc,
                since=earliest - pd.Timedelta(seconds=max(context_contract["max_receipt_age_seconds"].values())),
                decision_cutoffs=[*(decisions[instant(reference)] for reference in dataset.reference_at), cutoff],
                source_ids=context_contract["source_ids"], lazy=True)
            dataset = augment_dataset(dataset, snapshots,
                training_decision_at={reference: decisions[instant(reference)] for reference in dataset.reference_at},
                inference_cutoff=cutoff, source_ids=context_contract["source_ids"],
                contract=context_contract,
                cache_path=Path(datastore) / "intraday" / pc.lower() / "market-data" / f"context-cache-{symbol}.sqlite3")
            if tuple(dataset.feature_names) != expected_features:
                raise ContractError("Market context feature order differs from the frozen local recipe")
        manifest = {**dataset.manifest, "manual_model_recipe": deepcopy(recipe),
            "manual_recipe_symbol": symbol,
            "manual_recipe_training_support": support,
            "training_rows": len(dataset.y), "input_training_rows": input_rows,
            "context_decision_rows_excluded": input_rows_before_decision_filter - input_rows,
            "first_training_reference": dataset.reference_at[0].isoformat(),
            "last_training_reference": dataset.reference_at[-1].isoformat(),
            "training_window": {"policy": "assessment_history_per_fold_fit_cap" if _assessment else "newest_causal_rows",
                                "max_rows": history_limit,
                                "excluded_rows": input_rows - len(dataset.y)}}
        if _assessment:
            manifest.update(assessment_only=True, assessment_fit_max_rows=max_rows,
                assessment_support_scope="latest_runtime_suffix; every assessment fold must independently recheck support")
        return replace(dataset, manifest=manifest)

    def assessment_dataset_builder(candles, **kwargs):
        return dataset_builder(candles, **kwargs, _assessment=True)

    dataset_builder.for_assessment = assessment_dataset_builder

    def preparation_dataset_builder(candles, **kwargs):
        return dataset_builder(candles, **kwargs, _preparation=True)

    dataset_builder.for_preparation = preparation_dataset_builder

    def trainer(X, y, inference_X, *, config=None, dataset_metadata=None, preparation_only=False, **kwargs):
        if tuple(asdict(spec) for spec in configured_models(config)) != tuple(asdict(spec) for spec in specs):
            raise ContractError("Manual recipe trainer received different model specifications")
        metadata = dataset_metadata or {}
        from .assessment_reservation import load_reservation, assert_training_unreserved
        assert_training_unreserved(kwargs.get("target_close_at", ()),
            load_reservation(datastore, pc=pc, symbol=metadata.get("manual_recipe_symbol")))
        from .data import PREPARATION_CONTRACT
        if (type(preparation_only) is not bool
                or bool(metadata.get("preparation_only")) != preparation_only
                or preparation_only and (metadata.get("preparation_contract") != PREPARATION_CONTRACT
                                         or metadata.get("executable_forecast") is not False)):
            raise ContractError("Non-trading preparation requires explicit fit mode; it cannot train a runtime forecast")
        if metadata.get("assessment_only"):
            raise ContractError("Assessment history requires per-fold fits; it cannot train a runtime forecast")
        selected_contract = symbol_contracts.get(metadata.get("manual_recipe_symbol"), feature_contract)
        if symbol_contracts and metadata.get("manual_recipe_symbol") not in symbols:
            raise ContractError("Scoped manual recipe requires exact dataset symbol evidence")
        if (metadata.get("manual_model_recipe") != recipe
                or timing_mode_from_metadata(metadata) != timing_mode
                or metadata.get("feature_version") != selected_contract["feature_version"]
                or metadata.get("feature_policy") != selected_contract["feature_policy"]
                or metadata.get("label_provenance") != selected_contract["label_provenance"]
                or metadata.get("native_interval_contract") != selected_contract.get("native_interval_contract")
                or metadata.get("synthetic_policy") != "exclude"
                or metadata.get("synthetic_labels_included") != 0
                or tuple(kwargs.get("feature_names", ())) != expected_features
                or (context_contract is not None and
                    metadata.get("market_context", {}).get("contract") != context_contract)
                or (context_contract is None and metadata.get("market_context") is not None)):
            raise ContractError("Manual recipe fit requires its own frozen observed-only dataset evidence")
        features, inference = np.asarray(X), np.asarray(inference_X)
        if (features.ndim != 2 or features.shape != (len(y), len(expected_features))
                or inference.shape != (1, len(expected_features))
                or not np.isfinite(features).all() or not np.isfinite(inference).all()
                or (features[:, FEATURE_NAMES.index("synthetic_input_fraction20")] != 0).any()
                or inference[0, FEATURE_NAMES.index("synthetic_input_fraction20")] != 0):
            raise ContractError("Manual recipe fit requires finite observed-only feature matrices")
        _training_support(y, kwargs.get("reference_at", ()), kwargs.get("target_close_at", ()),
                          kwargs.get("label_available_at", ()), cutoff=kwargs.get("training_cutoff"),
                          support=support_contract, timing_mode=timing_mode, calibration_fraction=calibration_fraction)
        result = fit_ensemble(X, y, inference_X, config=specs, dataset_metadata=metadata, **kwargs)
        if preparation_only:
            result.update(preparation_only=True, executable_forecast=False)
        outputs = result.get("outputs", [])
        if (result.get("configured_count") != len(specs) or result.get("successful_count") != len(specs)
                or result.get("complete") is not True or result.get("consensus_status") != "full"
                or len(outputs) != len(specs) or [out.get("name") for out in outputs] != [s.name for s in specs]
                or any(out.get("status") != "success" for out in outputs)
                or result.get("probability_not_down") is None):
            evidence_path, persistence_error = None, None
            try:
                evidence_path = _persist_rejected(result, artifact_dir=kwargs.get("artifact_dir"),
                                                  run_id=kwargs.get("run_id"))
            except Exception as exc:
                persistence_error = f"{type(exc).__name__}: {exc}"
            raise ManualRecipeFitError(result, evidence_path=evidence_path,
                                       persistence_error=persistence_error)
        return result

    dataset_builder.native_interval_symbols = tuple(symbol for symbol in symbols if symbol in symbol_contracts)
    dataset_builder.market_context_contract = deepcopy(context_contract)
    dataset_builder.historical_recovery_contract = recovery_contract
    dataset_builder.market_collection_configuration_sha256 = recipe.get("market_collection_configuration_sha256")
    return selected_config, {"dataset_builder": dataset_builder, "trainer": trainer}
