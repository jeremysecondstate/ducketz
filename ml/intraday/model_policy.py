"""Atomic host-local installation of qualified specifications, never fitted blobs."""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import tempfile

from .models import configured_models
from .prediction import ContractError, TARGET_VERSION, digest, instant

SCHEMA = "intraday-supervised-model-policy-v1"


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, sort_keys=True, indent=2, allow_nan=False).encode("utf-8") + b"\n"
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _path(root, pc, symbol):
    if pc not in {"Atlas", "Scout"} or not re.fullmatch(r"[A-Z][A-Z0-9.]{0,15}", symbol):
        raise ContractError("Invalid local model policy identity")
    return Path(root) / "active" / f"{pc}-{symbol}.json"


def _validated_scope(scope):
    if (not isinstance(scope, dict) or set(scope) != {"synthetic_policy", "lookback_days"}
            or not isinstance(scope["synthetic_policy"], str)
            or scope["synthetic_policy"] not in {"include", "exclude"}
            or (scope["lookback_days"] is not None and
                (type(scope["lookback_days"]) is not int or scope["lookback_days"] <= 0))):
        raise ContractError("Supervised dataset scope requires an explicit synthetic policy and positive integer lookback or None")
    return dict(scope)


def _manifest_scope(manifest):
    if manifest is None:
        return {"synthetic_policy": "exclude", "lookback_days": None}
    if not isinstance(manifest, dict):
        raise ContractError("Supervised dataset manifest must be an object")
    fields = {key: manifest[key] for key in ("synthetic_policy", "lookback_days") if key in manifest}
    # Historical decisions did not always record either field. Their evaluated
    # population was observed labels across all available history.
    return _validated_scope(fields) if fields else {"synthetic_policy": "exclude", "lookback_days": None}


def load_active_policy(root, *, pc, symbol, feature_names=None):
    """Read selected specs and their evaluated population without changing bytes.

    Existing v1 policies lack dataset_scope. Their original selection always
    excluded synthetic labels and used all history; derive that scope only after
    validating the original document checksum.
    """
    path = _path(root, pc, symbol)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ContractError("Active supervised policy must be an object")
        checksum = value.pop("sha256")
        if (checksum != digest(value) or value["schema"] != SCHEMA or value["pc"] != pc
                or value["symbol"] != symbol or value["target_version"] != TARGET_VERSION
                or value["source_dataset"] != "EQUS.MINI"
                or feature_names is not None and list(feature_names) != value["feature_names"]):
            raise ContractError("Active supervised policy identity or integrity changed")
        instant(value["installed_at"])
        specs = configured_models(value["models"])
        if any(spec.probability_calibration != "sigmoid" for spec in specs):
            raise ContractError("Supervised models require chronological probability calibration")
        scope = (_validated_scope(value["dataset_scope"]) if "dataset_scope" in value else
                 {"synthetic_policy": "exclude", "lookback_days": None})
        return {"models": specs, "dataset_scope": scope, "sha256": checksum,
                "feature_names": value["feature_names"]}
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise ContractError("Active supervised model policy is invalid") from exc


def load_active_models(root, *, pc, symbol, feature_names=None):
    """Compatibility API for callers that require only selected specifications."""
    policy = load_active_policy(root, pc=pc, symbol=symbol, feature_names=feature_names)
    return policy["models"] if policy is not None else None


def install_selection(root, *, pc, symbol, report, feature_names, installed_at, dataset_manifest=None):
    """Require the frozen proposal's final gate; retain prior settings otherwise."""
    path = _path(root, pc, symbol)
    if report.get("status") != "promoted" or not report.get("selected_specs"):
        return {"status": "retained", "policy_path": str(path), "reason": report.get("reason", report.get("status"))}
    specs = configured_models(report["selected_specs"])
    if any(spec.probability_calibration != "sigmoid" for spec in specs):
        raise ContractError("Only calibrated qualified specifications may be installed")
    scope = _manifest_scope(dataset_manifest)
    value = dict(schema=SCHEMA, pc=pc, symbol=symbol, target_version=TARGET_VERSION,
                 source_dataset="EQUS.MINI", feature_names=list(feature_names),
                 dataset_scope=scope,
                 models=[asdict(spec) for spec in specs], installed_at=instant(installed_at).isoformat(),
                 report_sha256=digest(report), holdout_session=report.get("holdout_session"),
                 application="fresh_fit_each_cycle_no_serialized_model_reuse")
    value["sha256"] = digest(value)
    # Every revision remains available for review/rollback; active replacement is atomic.
    archive = Path(root) / "policies" / f"{value['sha256']}.json"
    if archive.exists():
        if json.loads(archive.read_text(encoding="utf-8")) != value:
            raise ContractError("Immutable policy archive changed")
    else:
        write_json(archive, value)
    write_json(path, value)
    loaded = load_active_policy(root, pc=pc, symbol=symbol, feature_names=feature_names)
    if (tuple(asdict(s) for s in loaded["models"]) != tuple(asdict(s) for s in specs)
            or loaded["dataset_scope"] != scope or loaded["sha256"] != value["sha256"]):
        raise ContractError("Installed policy readback differs")
    return {"status": "installed", "policy_sha256": value["sha256"], "models": [s.name for s in specs]}
