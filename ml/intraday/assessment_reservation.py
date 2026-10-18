"""Local prospective observations are excluded from automatic model development.

This reservation is a private host binding, not a shared model recipe. It has
no release-by-date behavior: inspecting results never authorizes reuse.
"""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import numpy as np

from .prediction import ContractError, digest, instant

SCHEMA = "intraday-assessment-reservation-v1"


def load_reservation(datastore, *, pc, symbol=None):
    if pc not in {"Scout", "Atlas"}:
        raise ContractError("Assessment reservation requires an explicit local PC")
    path = Path(datastore) / "intraday" / pc.lower() / "assessment-reservation.json"
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        payload = {k: v for k, v in value.items() if k != "sha256"}
        if (value.get("schema") != SCHEMA or value.get("pc") != pc
                or value.get("sha256") != digest(payload)
                or not value.get("experiment_id") or not value.get("symbols")
                or value.get("policy") != "exclude_from_automatic_development_until_explicit_new_experiment"):
            raise ValueError("Invalid reservation identity")
        instant(value["protected_from"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("Invalid prospective assessment reservation") from exc
    return value if symbol is None or symbol in value["symbols"] else None


def protect_dataset(dataset, reservation):
    """Exclude overlapping target horizons before row caps or feature selection.

    Inference may use new eligible prices. Only fitting/calibration/selection
    rows are filtered; a frozen prospective model can still make predictions.
    """
    if reservation is None:
        return dataset
    boundary = instant(reservation["protected_from"])
    keep = np.asarray([instant(value) <= boundary for value in dataset.target_close_at], dtype=bool)
    manifest = {**dataset.manifest, "assessment_reservation": {
        "sha256": reservation["sha256"], "experiment_id": reservation["experiment_id"],
        "protected_from": reservation["protected_from"], "excluded_training_rows": int((~keep).sum()),
        "scope": "training_calibration_feature_selection; inference_remains_causal"}}
    retained_references = dataset.reference_at[keep]
    retained_availability = dataset.label_available_at[keep]
    manifest.update(training_rows=int(keep.sum()),
        first_training_reference=retained_references[0].isoformat() if keep.any() else None,
        last_training_reference=retained_references[-1].isoformat() if keep.any() else None,
        label_available_at_max=max(retained_availability).isoformat() if keep.any() else None)
    return replace(dataset, X=dataset.X[keep], y=dataset.y[keep],
        label_available_at=dataset.label_available_at[keep],
        training_reference_at=dataset.reference_at[keep],
        target_close_at=dataset.target_close_at[keep], manifest=manifest)


def assert_training_unreserved(target_close_at, reservation):
    if reservation and any(instant(value) > instant(reservation["protected_from"]) for value in target_close_at):
        raise ContractError("Reserved prospective observations cannot enter automatic training or calibration")
