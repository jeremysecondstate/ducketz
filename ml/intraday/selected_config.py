"""Read the local supervisor's selected specifications; never load fitted models."""
from __future__ import annotations

import json
from pathlib import Path

from .models import configured_models
from .prediction import ContractError, TARGET_VERSION, instant

VERSION = "intraday-model-supervision-v1"


def load_selected_config(path, *, pc, symbols, source_dataset, as_of, on_error=None):
    """Validate this actor/source and freeze portable specs for one live cycle.

    Missing files preserve the established default configuration. A malformed
    present file is an actual error for the caller to record. Other symbols in
    the same local manifest are never added to this runner's membership.
    """
    path = Path(path)
    if not path.exists():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(document, dict) or document.get("schema_version") != VERSION or document.get("actor") != pc
            or document.get("source_dataset") != source_dataset
            or document.get("target_version") != TARGET_VERSION
            or not isinstance(document.get("symbols"), dict)):
        raise ContractError("Selected models differ from local actor, source or target")
    result = {}
    for symbol in symbols:
        entry = document["symbols"].get(symbol)
        if entry is None:
            continue
        try:
            result[symbol] = _validated_entry(entry, as_of)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if on_error is None:
                raise
            on_error(symbol, exc)
    return result


def _validated_entry(entry, as_of):
    if not isinstance(entry, dict) or not entry.get("selection_id"):
        raise ContractError("Selected models require their recorded selection identity")
    if instant(entry["selected_at"]) > instant(as_of):
        raise ContractError("Selected models cannot come from a future supervision run")
    if entry.get("training_cutoff") and instant(entry["training_cutoff"]) > instant(entry["selected_at"]):
        raise ContractError("Selected model training cutoff cannot follow its selection")
    specs = configured_models(entry["models"])
    if any(spec.probability_calibration != "sigmoid" for spec in specs):
        raise ContractError("Supervised models require chronological probability calibration")
    names = entry.get("feature_names")
    if not isinstance(names, list) or not names or len(set(names)) != len(names):
        raise ContractError("Selected models require their exact feature names")
    scope = {"models": entry["models"], "selection_id": entry["selection_id"],
                      "selected_at": entry["selected_at"], "feature_names": names,
                      "synthetic_policy": entry.get("synthetic_policy", "include"),
                      "lookback_days": entry.get("lookback_days"),
                      "max_training_rows": entry.get("max_training_rows", 3000)}
    from .coverage import check_population_contract
    check_population_contract(entry.get("population_contract"))
    scope.update({key:entry[key] for key in ("population_contract", "feature_version", "feature_contract", "market_input_version")
                  if key in entry})
    if (scope["synthetic_policy"] not in {"include", "exclude"}
            or type(scope["max_training_rows"]) is not int or scope["max_training_rows"] < 1
            or (scope["lookback_days"] is not None and
                (type(scope["lookback_days"]) is not int or scope["lookback_days"] < 1))):
        raise ContractError("Selected model training scope is invalid")
    return scope
