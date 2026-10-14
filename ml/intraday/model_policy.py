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


def load_active_models(root, *, pc, symbol, feature_names=None):
    path = _path(root, pc, symbol)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
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
        return specs
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise ContractError("Active supervised model policy is invalid") from exc


def install_selection(root, *, pc, symbol, report, feature_names, installed_at):
    """Require the frozen proposal's final gate; retain prior settings otherwise."""
    path = _path(root, pc, symbol)
    if report.get("status") != "promoted" or not report.get("selected_specs"):
        return {"status": "retained", "policy_path": str(path), "reason": report.get("reason", report.get("status"))}
    specs = configured_models(report["selected_specs"])
    if any(spec.probability_calibration != "sigmoid" for spec in specs):
        raise ContractError("Only calibrated qualified specifications may be installed")
    value = dict(schema=SCHEMA, pc=pc, symbol=symbol, target_version=TARGET_VERSION,
                 source_dataset="EQUS.MINI", feature_names=list(feature_names),
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
    loaded = load_active_models(root, pc=pc, symbol=symbol, feature_names=feature_names)
    if tuple(asdict(s) for s in loaded) != tuple(asdict(s) for s in specs):
        raise ContractError("Installed policy readback differs")
    return {"status": "installed", "policy_sha256": value["sha256"], "models": [s.name for s in specs]}
