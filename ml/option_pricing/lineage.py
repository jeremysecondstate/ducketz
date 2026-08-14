from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

import pandas as pd

from ml.artifacts import file_checksum, verify_manifest
from ml.option_pricing.eligibility import EligibilityPolicyArtifact
from ml.option_pricing.policies import (
    OPTION_PRICING_POLICY_VERSION,
    OPTION_PRICING_SCHEMA_VERSION,
    OPTION_PRICING_TIMING_POLICY_VERSION,
)
from ml.option_pricing.publication import (
    OPTION_PRICING_REPORT_NAME,
    OPTION_PRICING_REQUIRED_OUTPUTS,
    authoritative_option_pricing_runs,
    read_current_option_pricing_publication,
    receipt_proven_prediction_rows,
)
from ml.parquet_contracts import verify_parquet_schema


LINEAGE_VERIFICATION_VERSION = "option-pricing-lineage-verification-v2"


def verify_staged_option_pricing_run(
    datastore_root: Path,
    *,
    run_directory: Path,
    policy_artifact: EligibilityPolicyArtifact,
) -> dict[str, object]:
    """Verify a completed staging/final run without asserting pointer reachability."""

    root = Path(datastore_root).resolve()
    run = Path(run_directory).resolve()
    checks: dict[str, object] = {}
    errors: list[str] = []
    expected_parent = (root / "ml" / "option-pricing-runs").resolve()
    checks["run_location"] = run.parent == expected_parent
    if not checks["run_location"]:
        errors.append("Pricing run is outside option-pricing-runs")
        return _report(checks, errors, stage="STAGED")
    try:
        manifest = verify_manifest(run)
        checks["manifest_outputs"] = True
    except Exception as exc:
        checks["manifest_outputs"] = False
        errors.append(f"manifest verification failed: {type(exc).__name__}: {exc}")
        return _report(checks, errors, stage="STAGED")
    outputs = manifest.get("output_files")
    outputs = outputs if isinstance(outputs, Mapping) else {}
    required = set(OPTION_PRICING_REQUIRED_OUTPUTS) | {OPTION_PRICING_REPORT_NAME}
    checks["required_outputs"] = required.issubset(outputs)
    if not checks["required_outputs"]:
        errors.append(
            "required outputs missing: "
            + ", ".join(sorted(required.difference(outputs)))
        )
    for name, schema in OPTION_PRICING_REQUIRED_OUTPUTS.items():
        try:
            verify_parquet_schema(run / name, schema)
            checks[f"schema:{name}"] = True
        except Exception as exc:
            checks[f"schema:{name}"] = False
            errors.append(f"schema verification failed for {name}: {exc}")
    configuration = manifest.get("configuration")
    configuration = configuration if isinstance(configuration, Mapping) else {}
    policy_reference = configuration.get("eligibility_policy")
    policy_reference = (
        policy_reference if isinstance(policy_reference, Mapping) else {}
    )
    expected_policy_path = policy_artifact.directory.relative_to(root).as_posix()
    checks["eligibility_policy_reference"] = bool(
        policy_reference.get("policy_hash") == policy_artifact.policy_hash
        and policy_reference.get("path") == expected_policy_path
        and policy_reference.get("receipt_checksum_sha256")
        == file_checksum(policy_artifact.directory / "receipt.json")
    )
    if not checks["eligibility_policy_reference"]:
        errors.append("manifest eligibility-policy reference is absent or mismatched")
    declared_contract = policy_artifact.policy.get("model_contract")
    declared_contract = (
        declared_contract if isinstance(declared_contract, Mapping) else {}
    )
    finite_basis_key = (
        "finite_basis_nystroem_rbf_bayesian_ridge"
        if "finite_basis_nystroem_rbf_bayesian_ridge" in declared_contract
        else "bsgp"
    )
    manifest_contract = {
        "partitions": configuration.get("partition_config"),
        finite_basis_key: configuration.get("model_policy"),
        "contract_selection": configuration.get("contract_policy"),
        "projection": configuration.get("projection_policy"),
    }
    checks["predeclared_model_contract"] = manifest_contract == declared_contract
    if not checks["predeclared_model_contract"]:
        errors.append("manifest model contract differs from the predeclared policy")
    input_checks = _verify_inputs(root, manifest.get("input_files"))
    checks["input_files"] = input_checks
    if not input_checks["verified"]:
        errors.extend(str(value) for value in input_checks["errors"])
    return _report(checks, errors, stage="STAGED")


def verify_completed_option_pricing_lineage(
    datastore_root: Path,
    *,
    run_directory: Path,
    policy_artifact: EligibilityPolicyArtifact,
) -> dict[str, object]:
    """Derive lineage success from final manifests, receipts, schemas, and clocks."""

    root = Path(datastore_root).resolve()
    run = Path(run_directory).resolve()
    staged = verify_staged_option_pricing_run(
        root,
        run_directory=run,
        policy_artifact=policy_artifact,
    )
    checks = dict(staged.get("checks", {}))
    errors = list(staged.get("errors", []))
    try:
        current = read_current_option_pricing_publication(root)
        reachable = authoritative_option_pricing_runs(root, current=current)
        checks["current_publication_verified"] = current.run_directory == run
        checks["publication_reachable"] = run in reachable
        checks["publication_chain_length"] = len(reachable)
        if current.run_directory != run:
            errors.append("completed run is not the current verified Pricing authority")
        if run not in reachable:
            errors.append("completed run is not reachable through its receipt chain")
        receipt = current.receipt
        manifest = current.manifest
        checks["receipt_manifest_checksum"] = (
            receipt.get("manifest_checksum_sha256")
            == file_checksum(run / "manifest.json")
        )
        if not checks["receipt_manifest_checksum"]:
            errors.append("publication receipt does not checksum the final manifest")
    except Exception as exc:
        checks["current_publication_verified"] = False
        checks["publication_reachable"] = False
        errors.append(f"final publication verification failed: {type(exc).__name__}: {exc}")
        return _report(checks, errors, stage="COMPLETED")

    samples = pd.read_parquet(run / "pricing-samples.parquet")
    predictions = pd.read_parquet(run / "pricing-predictions.parquet")
    evaluations = pd.read_parquet(run / "pricing-evaluations.parquet")
    surfaces = pd.read_parquet(run / "pricing-surfaces.parquet")
    timing = _verify_timing(samples, predictions, evaluations)
    checks["causal_timing"] = timing
    if not timing["verified"]:
        errors.extend(str(value) for value in timing["errors"])

    compatibility = _verify_versions(samples, predictions, evaluations, surfaces)
    checks["policy_schema_versions"] = compatibility
    if not compatibility["verified"]:
        errors.extend(str(value) for value in compatibility["errors"])

    provider_evidence = _verify_provider_evidence(
        root,
        manifest=manifest,
        samples=samples,
        predictions=predictions,
        evaluations=evaluations,
        eligibility_policy_hash=policy_artifact.policy_hash,
    )
    checks["provider_receipt_evidence"] = provider_evidence
    if not provider_evidence["verified"]:
        errors.extend(str(value) for value in provider_evidence["errors"])

    automation = _verify_shadow_only(predictions, surfaces, run)
    checks["shadow_only"] = automation
    if not automation["verified"]:
        errors.extend(str(value) for value in automation["errors"])

    receipt_proof = _verify_live_prediction_receipts(root, predictions)
    checks["live_prediction_receipts"] = receipt_proof
    if not receipt_proof["verified"]:
        errors.extend(str(value) for value in receipt_proof["errors"])
    return _report(checks, errors, stage="COMPLETED")


def _verify_inputs(root: Path, raw_inventory: object) -> dict[str, object]:
    errors: list[str] = []
    verified_count = 0
    inventory = raw_inventory if isinstance(raw_inventory, list) else []
    if not isinstance(raw_inventory, list):
        return {
            "verified": False,
            "verified_count": 0,
            "errors": ["manifest input inventory is not a list"],
        }
    for index, raw in enumerate(inventory):
        if not isinstance(raw, Mapping):
            errors.append(f"input inventory item {index} is not an object")
            continue
        rendered = raw.get("path")
        if not isinstance(rendered, str) or not rendered:
            errors.append(f"input inventory item {index} has no path")
            continue
        candidate = Path(rendered)
        path = candidate if candidate.is_absolute() else root / candidate
        path = path.resolve()
        if raw.get("status") != "present" or not path.is_file():
            errors.append(f"lineage input is unavailable: {rendered}")
            continue
        checksum = raw.get("checksum_sha256")
        if not isinstance(checksum, str) or checksum != file_checksum(path):
            errors.append(f"lineage input checksum is absent or mismatched: {rendered}")
            continue
        if int(raw.get("size", -1)) != path.stat().st_size:
            errors.append(f"lineage input size is mismatched: {rendered}")
            continue
        verified_count += 1
    return {
        "verified": not errors,
        "verified_count": verified_count,
        "inventory_count": len(inventory),
        "errors": errors,
    }


def _verify_timing(
    samples: pd.DataFrame,
    predictions: pd.DataFrame,
    evaluations: pd.DataFrame,
) -> dict[str, object]:
    errors: list[str] = []
    if not samples.empty:
        source = pd.to_datetime(samples["source_snapshot_for"], utc=True, errors="coerce")
        target = pd.to_datetime(samples["target_snapshot_for"], utc=True, errors="coerce")
        source_available = pd.to_datetime(
            samples["source_available_at"], utc=True, errors="coerce"
        )
        if source.isna().any() or target.isna().any() or source_available.isna().any():
            errors.append("samples contain invalid source/target clocks")
        if not source.lt(target).all():
            errors.append("samples contain non-lagged source surfaces")
        offline = samples["prediction_mode"].astype("string").str.upper().eq("OFFLINE")
        completed = samples["sample_status"].eq("AVAILABLE") & offline
        if completed.any():
            quote = pd.to_datetime(
                samples.loc[completed, "observed_quote_timestamp"],
                utc=True,
                errors="coerce",
            )
            observed_available = pd.to_datetime(
                samples.loc[completed, "observed_available_at"],
                utc=True,
                errors="coerce",
            )
            completed_target = target.loc[completed]
            if quote.isna().any() or observed_available.isna().any():
                errors.append("available offline samples lack observed target clocks")
            elif not quote.gt(completed_target).all() or not observed_available.gt(
                completed_target
            ).all():
                errors.append("offline targets are not strictly later than prediction boundary")
    if not predictions.empty:
        created = pd.to_datetime(
            predictions["prediction_created_at"], utc=True, errors="coerce"
        )
        available = pd.to_datetime(
            predictions["prediction_available_at"], utc=True, errors="coerce"
        )
        target = pd.to_datetime(
            predictions["target_snapshot_for"], utc=True, errors="coerce"
        )
        source_available = pd.to_datetime(
            predictions["source_available_at"], utc=True, errors="coerce"
        )
        if pd.concat([created, available, target, source_available], axis=1).isna().any().any():
            errors.append("predictions contain invalid causal clocks")
        if not available.ge(created).all() or not source_available.lt(created).all():
            errors.append("prediction creation/publication chronology is invalid")
        live = predictions["prediction_mode"].astype("string").str.upper().eq("LIVE")
        if live.any() and not target.loc[live].lt(created.loc[live]).all():
            errors.append("live predictions were not created after completed target bars")
    if not evaluations.empty:
        complete = evaluations["evaluation_status"].eq("COMPLETE")
        if complete.any():
            quote = pd.to_datetime(
                evaluations.loc[complete, "observed_quote_timestamp"],
                utc=True,
                errors="coerce",
            )
            observed_available = pd.to_datetime(
                evaluations.loc[complete, "observed_available_at"],
                utc=True,
                errors="coerce",
            )
            created = pd.to_datetime(
                evaluations.loc[complete, "prediction_created_at"],
                utc=True,
                errors="coerce",
            )
            available = pd.to_datetime(
                evaluations.loc[complete, "prediction_available_at"],
                utc=True,
                errors="coerce",
            )
            if quote.isna().any() or observed_available.isna().any():
                errors.append("complete evaluations lack observed clocks")
            elif not quote.gt(created).all() or not quote.gt(available).all():
                errors.append("complete outcomes do not strictly follow prediction commitment")
            elif not observed_available.gt(available).all():
                errors.append("complete outcome receipts do not follow prediction commitment")
        prospective = evaluations.get(
            "prospective_eligible", pd.Series(False, index=evaluations.index)
        ).fillna(False).astype(bool)
        if prospective.any():
            modes = evaluations.loc[prospective, "prediction_mode"].astype("string").str.upper()
            providers = evaluations.loc[prospective, "source_provider"].astype("string").str.lower()
            statuses = evaluations.loc[prospective, "evaluation_status"]
            if not (
                modes.eq("LIVE").all()
                and providers.eq("schwab").all()
                and statuses.eq("COMPLETE").all()
            ):
                errors.append("prospective flags include non-live, non-Schwab, or incomplete rows")
    return {
        "verified": not errors,
        "sample_rows": len(samples),
        "prediction_rows": len(predictions),
        "evaluation_rows": len(evaluations),
        "errors": errors,
    }


def _verify_versions(*frames: pd.DataFrame) -> dict[str, object]:
    errors: list[str] = []
    for frame in frames:
        if frame.empty:
            continue
        if "pricing_policy_version" in frame and not frame[
            "pricing_policy_version"
        ].eq(OPTION_PRICING_POLICY_VERSION).all():
            errors.append("pricing policy version mismatch")
        if "timing_policy_version" in frame and not frame[
            "timing_policy_version"
        ].eq(OPTION_PRICING_TIMING_POLICY_VERSION).all():
            errors.append("timing policy version mismatch")
        if "schema_version" in frame and frame["schema_version"].isna().any():
            errors.append("schema version is missing")
    if frames and not frames[0].empty and not frames[0]["schema_version"].eq(
        OPTION_PRICING_SCHEMA_VERSION
    ).all():
        errors.append("sample schema version mismatch")
    return {"verified": not errors, "errors": sorted(set(errors))}


def _verify_shadow_only(
    predictions: pd.DataFrame,
    surfaces: pd.DataFrame,
    run: Path,
) -> dict[str, object]:
    errors: list[str] = []
    for label, frame in (("predictions", predictions), ("surfaces", surfaces)):
        if not frame.empty and (
            "automated_action_allowed" not in frame
            or frame["automated_action_allowed"].fillna(True).astype(bool).any()
        ):
            errors.append(f"{label} authorize automated action")
    try:
        report = json.loads(
            (run / OPTION_PRICING_REPORT_NAME).read_text(encoding="utf-8")
        )
        if report.get("automated_action_allowed") is not False:
            errors.append("Pricing report does not explicitly forbid automated action")
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        errors.append("Pricing report is unreadable")
    return {"verified": not errors, "errors": errors}


def _verify_live_prediction_receipts(
    root: Path, predictions: pd.DataFrame
) -> dict[str, object]:
    errors: list[str] = []
    live = predictions.loc[
        predictions.get(
            "prediction_mode", pd.Series(index=predictions.index, dtype="string")
        )
        .astype("string")
        .str.upper()
        .eq("LIVE")
    ].copy()
    if live.empty:
        return {"verified": True, "live_rows": 0, "receipt_proven_rows": 0, "errors": []}
    proven = receipt_proven_prediction_rows(root)
    key_columns = ["symbol", "target_snapshot_for", "contract_symbol"]
    for frame in (live, proven):
        frame["target_snapshot_for"] = pd.to_datetime(
            frame["target_snapshot_for"], utc=True, errors="coerce"
        )
    live_keys = set(map(tuple, live[key_columns].itertuples(index=False, name=None)))
    proven_keys = set(map(tuple, proven[key_columns].itertuples(index=False, name=None))) if not proven.empty else set()
    missing = live_keys.difference(proven_keys)
    if missing:
        errors.append(f"{len(missing)} live natural targets lack a reachable first receipt")
    return {
        "verified": not errors,
        "live_rows": len(live),
        "receipt_proven_rows": len(proven),
        "missing_natural_targets": len(missing),
        "errors": errors,
    }


def _verify_provider_evidence(
    root: Path,
    *,
    manifest: Mapping[str, object],
    samples: pd.DataFrame,
    predictions: pd.DataFrame,
    evaluations: pd.DataFrame,
    eligibility_policy_hash: str,
) -> dict[str, object]:
    errors: list[str] = []
    offline_rows = 0
    for frame in (samples, predictions, evaluations):
        if frame.empty:
            continue
        modes = frame.get(
            "prediction_mode", pd.Series(index=frame.index, dtype="string")
        ).astype("string").str.upper()
        offline = modes.eq("OFFLINE")
        offline_rows += int(offline.sum())
        if offline.any():
            providers = set(
                frame.loc[offline, "source_provider"]
                .astype("string")
                .str.strip()
                .str.lower()
                .dropna()
            ) if "source_provider" in frame else set()
            if providers != {"databento-opra"}:
                errors.append("offline rows are not exclusively verified Databento OPRA")

    inventory = manifest.get("input_files")
    inventory = inventory if isinstance(inventory, list) else []
    inventory_paths: set[Path] = set()
    for raw in inventory:
        if not isinstance(raw, Mapping) or raw.get("status") != "present":
            continue
        rendered = str(raw.get("path", ""))
        candidate = Path(rendered)
        inventory_paths.add(
            (candidate if candidate.is_absolute() else root / candidate).resolve()
        )

    verified_outputs: set[Path] = set()
    definition_imports = cbbo_imports = 0
    matching_policy_cbbo_imports = 0
    opra_root = (root / "ml" / "option-pricing-evidence" / "opra").resolve()
    receipt_paths = sorted(
        path
        for path in inventory_paths
        if path.name == "receipt.json" and opra_root in path.parents
    )
    if offline_rows:
        from ml.option_pricing.opra import OPRA_IMPORT_VERSION, read_opra_import

        for receipt_path in receipt_paths:
            try:
                verified = read_opra_import(receipt_path.parent, datastore_root=root)
                opra_manifest = verified["manifest"]
            except Exception as exc:
                errors.append(
                    "OPRA input receipt failed verification: "
                    f"{receipt_path}: {type(exc).__name__}: {exc}"
                )
                continue
            if opra_manifest.get("schema_version") != OPRA_IMPORT_VERSION:
                errors.append(f"OPRA input is readable legacy evidence only: {receipt_path}")
                continue
            if (
                opra_manifest.get("evidence_kind") != "REAL_RECEIPT_PROVEN"
                or opra_manifest.get("eligibility_scope_verified") is not True
            ):
                errors.append(
                    f"OPRA input is fixture or incomplete-scope evidence only: {receipt_path}"
                )
                continue
            phase = opra_manifest.get("phase")
            if phase == "definitions":
                definition_imports += 1
            elif phase == "cbbo":
                cbbo_imports += 1
                reference = opra_manifest.get("eligibility_policy")
                reference = reference if isinstance(reference, Mapping) else {}
                if reference.get("policy_hash") == eligibility_policy_hash:
                    matching_policy_cbbo_imports += 1
                else:
                    errors.append(
                        f"OPRA CBBO policy hash does not match Pricing: {receipt_path}"
                    )
            for name in opra_manifest.get("outputs", {}):
                verified_outputs.add((receipt_path.parent / str(name)).resolve())
        used_opra_outputs = {
            path
            for path in inventory_paths
            if opra_root in path.parents and path.name.endswith(".dbn.zst")
        }
        unverified_outputs = used_opra_outputs.difference(verified_outputs)
        if unverified_outputs:
            errors.append(
                f"{len(unverified_outputs)} OPRA inputs lack a verified import receipt"
            )
        if definition_imports < 1 or cbbo_imports < 1:
            errors.append("offline evidence lacks both verified definition and CBBO imports")
        if matching_policy_cbbo_imports != cbbo_imports:
            errors.append("not every CBBO import used the predeclared eligibility policy")
    return {
        "verified": not errors,
        "offline_rows": offline_rows,
        "verified_definition_imports": definition_imports,
        "verified_cbbo_imports": cbbo_imports,
        "matching_policy_cbbo_imports": matching_policy_cbbo_imports,
        "verified_opra_output_count": len(verified_outputs),
        "errors": sorted(set(errors)),
    }


def _report(
    checks: Mapping[str, object], errors: list[str], *, stage: str
) -> dict[str, object]:
    return {
        "schema_version": LINEAGE_VERIFICATION_VERSION,
        "stage": stage,
        "verified": not errors,
        "evidence_kind": "REAL_RECEIPT_PROVEN",
        "fixture_test_evidence": False,
        "checks": dict(checks),
        "errors": errors,
    }


__all__ = [
    "LINEAGE_VERIFICATION_VERSION",
    "verify_completed_option_pricing_lineage",
    "verify_staged_option_pricing_run",
]
