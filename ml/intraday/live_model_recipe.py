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

from .data import FEATURE_NAMES
from .models import ModelSpec, configured_models, fit_ensemble, purged_chronological_split
from .observed_data import (
    FEATURE_POLICY, FEATURE_VERSION, LABEL_PROVENANCE, build_observed_dataset,
)
from .prediction import ContractError, TARGET_VERSION, instant


SCHEMA_VERSION = "intraday-manual-observed-calibrated-v1"
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
        "models": [asdict(spec) for spec in _specs()],
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
    expected = make_manual_recipe(pc=pc, symbols=symbols,
        installed_at=value.get("installed_at"),
        authority_reference=value.get("authority_reference"))
    payload = {key: item for key, item in value.items() if key != "sha256"}
    try:
        checksum = _sha(payload)
    except (TypeError, ValueError) as exc:
        raise ContractError("Manual model recipe contains noncanonical values") from exc
    if value.get("sha256") != checksum:
        raise ContractError("Manual model recipe checksum mismatch")
    # Canonical comparison also rejects booleans in numeric fields, arbitrary
    # extra fields, changed model settings, and peer or partial symbol scopes.
    if _canonical(value) != _canonical(expected):
        raise ContractError("Manual model recipe scope, fields, or fixed candidate settings differ")
    return deepcopy(value)


def _training_support(y, reference_at, target_close_at, label_available_at, *, cutoff):
    labels = np.asarray(y)
    references = tuple(instant(v) for v in reference_at)
    targets = tuple(instant(v) for v in target_close_at)
    available = tuple(instant(v) for v in label_available_at)
    if (labels.ndim != 1 or len(labels) < MIN_ROWS or len(labels) > MAX_ROWS
            or len(references) != len(labels) or len(targets) != len(labels)
            or len(available) != len(labels) or not np.isin(labels, [0, 1]).all()):
        raise ContractError(f"Observed recipe requires {MIN_ROWS} to {MAX_ROWS} matching real training rows")
    if any(a > instant(cutoff) for a in available):
        raise ContractError("Observed recipe training labels are not yet available at cutoff")
    if any(t != r + pd.Timedelta(minutes=45) for r, t in zip(references, targets)):
        raise ContractError("Observed recipe requires exact 45-minute label windows")
    dates = {pd.Timestamp(v).tz_convert("America/New_York").date() for v in references}
    if len(dates) < MIN_DATES:
        raise ContractError(f"Observed recipe requires at least {MIN_DATES} actual training dates")
    counts = {str(v): int((labels == v).sum()) for v in (0, 1)}
    if min(counts.values()) < MIN_CLASS:
        raise ContractError(f"Observed recipe requires at least {MIN_CLASS} examples of each target class")
    train, calibration = purged_chronological_split(references, targets,
        fraction=0.25, label_available_at=available)
    partitions = {}
    for name, indices in (("base", train), ("calibration", calibration)):
        class_counts = {str(v): int((labels[indices] == v).sum()) for v in (0, 1)}
        if len(indices) < MIN_CALIBRATION_ROWS or min(class_counts.values()) < MIN_CLASS:
            raise ContractError(f"Observed recipe {name} partition requires at least "
                f"{MIN_CALIBRATION_ROWS} rows and {MIN_CLASS} examples of each class after the purge")
        partitions[name] = {"rows": len(indices), "class_counts": class_counts}
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
        super().__init__("Observed manual recipe requires both calibrated models; "
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
    specs = _specs()
    selected_config = replace(config, models=tuple(asdict(spec) for spec in specs),
                              synthetic_policy="exclude", use_supervised_models=False).validate()

    def dataset_builder(candles, *, cutoff, reference_at, lookback_days=None, synthetic_policy="exclude"):
        if synthetic_policy != "exclude":
            raise ContractError("Observed manual recipe cannot admit synthetic labels or feature candles")
        dataset = build_observed_dataset(candles, cutoff=cutoff, reference_at=reference_at,
                                         lookback_days=lookback_days)
        if dataset.inference.get("symbol") not in symbols:
            raise ContractError("Observed manual recipe cannot train a symbol outside this PC's scope")
        input_rows = len(dataset.y)
        dataset = replace(dataset, X=dataset.X[-MAX_ROWS:], y=dataset.y[-MAX_ROWS:],
            label_available_at=dataset.label_available_at[-MAX_ROWS:],
            training_reference_at=dataset.reference_at[-MAX_ROWS:],
            target_close_at=dataset.target_close_at[-MAX_ROWS:])
        support = _training_support(dataset.y, dataset.reference_at, dataset.target_close_at,
                                    dataset.label_available_at, cutoff=cutoff)
        manifest = {**dataset.manifest, "manual_model_recipe": deepcopy(recipe),
            "manual_recipe_training_support": support,
            "training_rows": len(dataset.y), "input_training_rows": input_rows,
            "first_training_reference": dataset.reference_at[0].isoformat(),
            "last_training_reference": dataset.reference_at[-1].isoformat(),
            "training_window": {"policy": "newest_causal_rows", "max_rows": MAX_ROWS,
                                "excluded_rows": input_rows - len(dataset.y)}}
        return replace(dataset, manifest=manifest)

    def trainer(X, y, inference_X, *, config=None, dataset_metadata=None, **kwargs):
        if tuple(asdict(spec) for spec in configured_models(config)) != tuple(asdict(spec) for spec in specs):
            raise ContractError("Manual recipe trainer received different model specifications")
        metadata = dataset_metadata or {}
        if (metadata.get("manual_model_recipe") != recipe
                or metadata.get("feature_version") != FEATURE_VERSION
                or metadata.get("feature_policy") != FEATURE_POLICY
                or metadata.get("label_provenance") != LABEL_PROVENANCE
                or metadata.get("synthetic_policy") != "exclude"
                or metadata.get("synthetic_labels_included") != 0
                or tuple(kwargs.get("feature_names", ())) != FEATURE_NAMES):
            raise ContractError("Manual recipe fit requires its own frozen observed-only dataset evidence")
        features, inference = np.asarray(X), np.asarray(inference_X)
        if (features.ndim != 2 or features.shape != (len(y), len(FEATURE_NAMES))
                or inference.shape != (1, len(FEATURE_NAMES))
                or not np.isfinite(features).all() or not np.isfinite(inference).all()
                or (features[:, FEATURE_NAMES.index("synthetic_input_fraction20")] != 0).any()
                or inference[0, FEATURE_NAMES.index("synthetic_input_fraction20")] != 0):
            raise ContractError("Manual recipe fit requires finite observed-only feature matrices")
        _training_support(y, kwargs.get("reference_at", ()), kwargs.get("target_close_at", ()),
                          kwargs.get("label_available_at", ()), cutoff=kwargs.get("training_cutoff"))
        result = fit_ensemble(X, y, inference_X, config=specs, dataset_metadata=metadata, **kwargs)
        outputs = result.get("outputs", [])
        if (result.get("configured_count") != 2 or result.get("successful_count") != 2
                or result.get("complete") is not True or result.get("consensus_status") != "full"
                or len(outputs) != 2 or [out.get("name") for out in outputs] != [s.name for s in specs]
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

    return selected_config, {"dataset_builder": dataset_builder, "trainer": trainer}
