"""Bind verified portable native sources to Scout's canonical composition API."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import PurePosixPath
import re

import pandas as pd

from ml.account_gameplan.sources import read_source_bundle
from ml.joint_capital_handoff import _FORECAST_FIELDS, _PRICE_FIELDS, _POINT_FIELDS
from ml.joint_capital_plan import build_owner_package, content_sha256


def account_forecast_id(producer_id: str, native_run_name: str, original_forecast_id: str) -> str:
    """Use Scout's unchanged joint identity, independent of account provenance."""
    return "joint:" + content_sha256([producer_id, native_run_name, original_forecast_id])


def _prices(value):
    result = {key: deepcopy(item) for key, item in value.items() if key in _PRICE_FIELDS}
    if "reference_completion" in value:
        completion = value["reference_completion"]
        if not isinstance(completion, dict):
            raise ValueError("Invalid native reference completion")
        allowed = {"contract_version", "observed_at", "maximum_gap_minutes", "maximum_fill_bars",
                   "synthetic_bar_count", "synthetic_reference_count", "unavailable_reference_count",
                   "available_observed_count", "available_synthetic_count", "policy", "status"}
        result["reference_completion"] = {key: item for key, item in completion.items()
            if key in allowed and (item is None or type(item) in (str, bool, int, float))}
    result["points"] = {key: {name: deepcopy(item) for name, item in point.items() if name in _POINT_FIELDS}
                        for key, point in value["points"].items()}
    return result


def assert_same_source(source, verified):
    """Do not silently replace a caller's changed data with pinned originals."""
    try:
        pd.testing.assert_frame_equal(source.forecasts, verified.forecasts, check_exact=True)
    except AssertionError as exc:
        raise ValueError("In-memory source forecasts differ from pinned native bytes") from exc
    if (source.metadata != verified.metadata or source.price_path != verified.price_path
            or source.ownership != verified.ownership):
        raise ValueError("In-memory source evidence differs from pinned native bytes")


def owner_package(source):
    """Read explicit pinned bytes; reject mutated in-memory claims, never repair them.

    The portable reader proves these exact source hashes against the original
    native manifests. Reopening is read-only and does not discover other sources.
    """
    meta = source.metadata
    if meta.get("holding_policy_proof") != "NATIVE_MANIFEST":
        raise ValueError("Combined planning requires NATIVE_MANIFEST holding policy proof")
    verified = read_source_bundle(source.path, expected_manifest_sha256=source.manifest_sha256,
        expected_producer=meta["producer_id"], expected_symbols=meta["symbols"],
        expected_action_date=meta["action_date"])
    assert_same_source(source, verified)
    raw = (source.path / "manifest.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != source.manifest_sha256:
        raise ValueError("Source manifest changed during package binding")
    manifest = json.loads(raw)
    native = manifest["native"]["source"]
    receipt = json.loads(native["receipt_json"])
    configuration = json.loads(native["manifest_json"])["configuration"]
    revisions = [value for value in (configuration.get("source_revision"), receipt.get("source_revision"))
                 if value is not None]
    if revisions and (len(revisions) != 2 or revisions[0] != revisions[1]
            or not isinstance(revisions[0], str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revisions[0])):
        raise ValueError("Recorded source revisions disagree")
    revision = revisions[0] if revisions else None
    reference = None if revision is None else {"kind": "SAVED_ARTIFACT_PROVENANCE", "source_revision": revision,
        "receipt_sha256": meta["source_receipt_sha256"], "reference_sha256": meta["source_manifest_sha256"]}
    return build_owner_package(owner_id=meta["producer_id"],
        run_id=PurePosixPath(meta["source_gameplan_run"]).name, source_revision=revision,
        action_date=meta["action_date"], frozen_symbols=meta["symbols"],
        created_at=meta["trade_plan_completed_at"],
        source_hashes={"receipt_sha256": meta["source_receipt_sha256"],
            "manifest_sha256": meta["source_manifest_sha256"],
            "forecasts_sha256": manifest["files"]["forecasts.parquet"]["sha256"],
            "price_path_sha256": manifest["files"]["planning-price-path.json"]["sha256"]},
        forecasts=source.forecasts[[name for name in source.forecasts if name in _FORECAST_FIELDS]].to_dict("records"),
        price_path=_prices(source.price_path),
        cross_horizon_fallback_policy=meta.get("cross_horizon_fallback_policy"), source_reference=reference)


def joint_snapshot(snapshot):
    """Map the already verified same-account identity without recomputing money."""
    result = deepcopy(snapshot)
    fingerprint = result.get("account_fingerprint")
    if result.get("account_scope_sha256", fingerprint) != fingerprint:
        raise ValueError("Conflicting account snapshot identity")
    result["account_scope_sha256"] = fingerprint
    return result
