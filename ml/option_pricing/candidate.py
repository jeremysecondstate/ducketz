from __future__ import annotations

import json
import os
import importlib.metadata
import platform
from pathlib import Path
from typing import Mapping, Sequence

import joblib
import pandas as pd

from ml.artifacts import file_checksum, semantic_metadata_fingerprint, utc_timestamp
from ml.option_pricing.eligibility import (
    EligibilityPolicyArtifact,
    read_current_eligibility_report,
)
from ml.option_pricing.lineage import verify_completed_option_pricing_lineage
from ml.option_pricing.model import PricingRouteModel
from ml.option_pricing.publication import read_current_option_pricing_publication


CANDIDATE_SCHEMA_VERSION = "option-pricing-frozen-candidate-v1"
CANDIDATE_RECEIPT_VERSION = "option-pricing-frozen-candidate-receipt-v1"
CANDIDATE_POINTER_VERSION = "option-pricing-frozen-candidate-pointer-v1"
CANDIDATE_INVALIDATION_VERSION = "option-pricing-candidate-invalidation-v1"


class CandidateError(RuntimeError):
    """A frozen-candidate identity or receipt failed closed."""


def freeze_candidate(
    datastore_root: Path,
    *,
    pricing_run: Path,
    policy_artifact: EligibilityPolicyArtifact,
    eligibility_report: Mapping[str, object],
    frozen_at: object | None = None,
) -> Mapping[str, object]:
    """Freeze a receipt-proven candidate after all offline gates pass."""

    root = Path(datastore_root).resolve()
    run = Path(pricing_run).resolve()
    current = read_current_option_pricing_publication(root)
    if current.run_directory != run:
        raise CandidateError("Only the current verified Pricing run can be frozen")
    current_eligibility = read_current_eligibility_report(root)
    if (
        current_eligibility.receipt.get("pricing_run_path")
        != run.relative_to(root).as_posix()
        or semantic_metadata_fingerprint(current_eligibility.report)
        != semantic_metadata_fingerprint(eligibility_report)
    ):
        raise CandidateError(
            "Candidate freeze requires the current immutable eligibility report"
        )
    lineage = verify_completed_option_pricing_lineage(
        root,
        run_directory=run,
        policy_artifact=policy_artifact,
    )
    if not lineage.get("verified"):
        raise CandidateError("Candidate source lineage did not verify")
    _require_offline_gates(eligibility_report)
    production_evidence_eligible = _production_offline_evidence_verified(
        eligibility_report,
        lineage=lineage,
    )
    if eligibility_report.get("automated_action_allowed") is not False:
        raise CandidateError("Candidate source report must remain shadow-only")
    report_policy = eligibility_report.get("eligibility_policy")
    report_policy = report_policy if isinstance(report_policy, Mapping) else {}
    if report_policy.get("policy_hash") != policy_artifact.policy_hash:
        raise CandidateError("Candidate report and policy hashes do not match")
    closed_lockbox = eligibility_report.get("closed_lockbox_inventory")
    if (
        not isinstance(closed_lockbox, Mapping)
        or not closed_lockbox.get("target_snapshot_fors")
    ):
        closed_lockbox = _run_lockbox_inventory(run)
    if (
        not isinstance(closed_lockbox, Mapping)
        or closed_lockbox.get("target_values_read") is not False
        or not closed_lockbox.get("target_snapshot_fors")
    ):
        raise CandidateError("Closed-lockbox inventory is unavailable or opened")
    lockbox_freshness = verify_fresh_future_lockbox_targets(
        root,
        target_snapshot_fors=closed_lockbox["target_snapshot_fors"],
    )
    if lockbox_freshness.get("status") != "PASS":
        raise CandidateError(
            "Candidate lockbox is not genuinely future to every prior attempt"
        )

    manifest = current.manifest
    basis = {
        "schema_version": CANDIDATE_SCHEMA_VERSION,
        "status": "FROZEN",
        "pricing_run_path": run.relative_to(root).as_posix(),
        "pricing_manifest_checksum_sha256": file_checksum(run / "manifest.json"),
        "pricing_receipt_checksum_sha256": file_checksum(run / "publication.json"),
        "eligibility_policy_hash": policy_artifact.policy_hash,
        "eligibility_policy_path": policy_artifact.directory.relative_to(root).as_posix(),
        "eligibility_policy_receipt_checksum_sha256": file_checksum(
            policy_artifact.directory / "receipt.json"
        ),
        "offline_gate_evidence_hash": semantic_metadata_fingerprint(
            {"gates": list(eligibility_report.get("gates", ()))[:8]}
        ),
        "source_evidence_kind": (
            "REAL_RECEIPT_PROVEN"
            if production_evidence_eligible
            else "FIXTURE_TEST_ONLY"
        ),
        "production_evidence_eligible": production_evidence_eligible,
        "model_artifacts": _file_inventory(run / "model-artifacts", relative_to=root),
        "source_evidence": list(manifest.get("input_files", ())),
        "dependency_contracts": _dependency_inventory(),
        "code_inventory": list(policy_artifact.policy.get("code_inventory", ())),
        "runtime_versions": dict(policy_artifact.policy.get("runtime", {})),
        "closed_lockbox": dict(closed_lockbox),
        "fresh_future_lockbox": lockbox_freshness,
        "retraining_allowed": False,
        "hyperparameter_changes_allowed": False,
        "lockbox_score_limit": 1,
        "automated_action_allowed": False,
    }
    if not basis["model_artifacts"]:
        raise CandidateError("Frozen candidate has no copied model artifacts")
    candidate_id = semantic_metadata_fingerprint(basis)
    candidate = {
        **basis,
        "candidate_id": candidate_id,
        "frozen_at": utc_timestamp(frozen_at).isoformat(),
        "permanently_invalidated": False,
    }
    candidates_root = root / "ml" / "option-pricing-candidates"
    destination = candidates_root / candidate_id
    if destination.exists():
        return read_candidate(destination, datastore_root=root)
    candidates_root.mkdir(parents=True, exist_ok=True)
    staging = candidates_root / f".{candidate_id}.tmp-{os.getpid()}"
    staging.mkdir()
    try:
        candidate_path = staging / "candidate.json"
        _write_json(candidate_path, candidate)
        receipt = {
            "schema_version": CANDIDATE_RECEIPT_VERSION,
            "candidate_id": candidate_id,
            "candidate_path": (
                destination.relative_to(root) / "candidate.json"
            ).as_posix(),
            "candidate_checksum_sha256": file_checksum(candidate_path),
            "frozen_at": candidate["frozen_at"],
            "automated_action_allowed": False,
        }
        _write_json(staging / "receipt.json", receipt)
        staging.replace(destination)
    except BaseException:
        raise
    _write_json_atomic(
        root / "ml" / "option-pricing-candidate-latest" / "candidate.json",
        {
            "schema_version": CANDIDATE_POINTER_VERSION,
            "current": {
                "candidate_id": candidate_id,
                "candidate_path": (
                    destination.relative_to(root) / "candidate.json"
                ).as_posix(),
                "candidate_checksum_sha256": file_checksum(
                    destination / "candidate.json"
                ),
                "receipt_checksum_sha256": file_checksum(destination / "receipt.json"),
            },
        },
    )
    return read_candidate(destination, datastore_root=root)


def read_current_candidate(datastore_root: Path) -> Mapping[str, object] | None:
    root = Path(datastore_root).resolve()
    pointer_path = root / "ml" / "option-pricing-candidate-latest" / "candidate.json"
    if not pointer_path.is_file():
        return None
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CandidateError("Frozen-candidate pointer is unreadable") from exc
    if not isinstance(pointer, Mapping):
        raise CandidateError("Frozen-candidate pointer is malformed")
    current = pointer.get("current")
    if (
        pointer.get("schema_version") != CANDIDATE_POINTER_VERSION
        or not isinstance(current, Mapping)
    ):
        raise CandidateError("Frozen-candidate pointer is malformed")
    raw_path = current.get("candidate_path")
    if not isinstance(raw_path, str):
        raise CandidateError("Frozen-candidate pointer has no path")
    path = (root / raw_path).resolve()
    candidate = read_candidate(path.parent, datastore_root=root)
    if (
        current.get("candidate_id") != candidate.get("candidate_id")
        or current.get("candidate_checksum_sha256") != file_checksum(path)
        or current.get("receipt_checksum_sha256")
        != file_checksum(path.parent / "receipt.json")
    ):
        raise CandidateError("Frozen-candidate pointer verification failed")
    return candidate


def read_candidate(
    directory: Path, *, datastore_root: Path
) -> Mapping[str, object]:
    root = Path(datastore_root).resolve()
    resolved = Path(directory).resolve()
    allowed = (root / "ml" / "option-pricing-candidates").resolve()
    if resolved.parent != allowed:
        raise CandidateError("Frozen-candidate path escapes its immutable root")
    candidate_path = resolved / "candidate.json"
    receipt_path = resolved / "receipt.json"
    try:
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CandidateError("Frozen-candidate artifact is unreadable") from exc
    if not isinstance(candidate, Mapping) or not isinstance(receipt, Mapping):
        raise CandidateError("Frozen-candidate artifact is malformed")
    expected_path = (resolved.relative_to(root) / "candidate.json").as_posix()
    if (
        candidate.get("schema_version") != CANDIDATE_SCHEMA_VERSION
        or candidate.get("candidate_id") != resolved.name
        or semantic_metadata_fingerprint(
            {
                key: value
                for key, value in candidate.items()
                if key
                not in {"candidate_id", "frozen_at", "permanently_invalidated"}
            }
        )
        != resolved.name
        or candidate.get("status") != "FROZEN"
        or candidate.get("retraining_allowed") is not False
        or candidate.get("hyperparameter_changes_allowed") is not False
        or candidate.get("automated_action_allowed") is not False
        or receipt.get("schema_version") != CANDIDATE_RECEIPT_VERSION
        or receipt.get("candidate_id") != resolved.name
        or receipt.get("candidate_path") != expected_path
        or receipt.get("candidate_checksum_sha256") != file_checksum(candidate_path)
        or receipt.get("automated_action_allowed") is not False
    ):
        raise CandidateError("Frozen-candidate receipt verification failed")
    _verify_candidate_references(root, candidate)
    invalidation_path = resolved / "invalidation.json"
    output = dict(candidate)
    if invalidation_path.is_file():
        invalidation = _read_invalidation(invalidation_path, candidate_id=resolved.name)
        output["permanently_invalidated"] = True
        output["invalidation"] = invalidation
    return output


def permanently_invalidate_candidate(
    datastore_root: Path,
    *,
    candidate_id: str,
    reason: str,
    lockbox_result_checksum_sha256: str,
    invalidated_at: object | None = None,
) -> Mapping[str, object]:
    """Persist an irreversible invalidation after a lockbox failure/attempt fault."""

    root = Path(datastore_root).resolve()
    directory = root / "ml" / "option-pricing-candidates" / candidate_id
    candidate = read_candidate(directory, datastore_root=root)
    path = directory / "invalidation.json"
    if path.exists():
        return _read_invalidation(path, candidate_id=candidate_id)
    payload = {
        "schema_version": CANDIDATE_INVALIDATION_VERSION,
        "candidate_id": candidate_id,
        "invalidated_at": utc_timestamp(invalidated_at).isoformat(),
        "reason": str(reason).strip() or "LOCKBOX_ATTEMPT_FAILED",
        "lockbox_result_checksum_sha256": lockbox_result_checksum_sha256,
        "permanently_invalidated": True,
        "retraining_under_candidate_identity_allowed": False,
        "rescore_allowed": False,
        "automated_action_allowed": False,
    }
    _write_json_atomic(path, payload)
    # Re-read to ensure the candidate identity and new terminal marker cohere.
    read_candidate(directory, datastore_root=root)
    return payload


def load_candidate_models(
    datastore_root: Path,
    *,
    candidate: Mapping[str, object],
    required_routes: Sequence[tuple[str, str]],
) -> dict[tuple[str, str], PricingRouteModel]:
    """Load only checksum-verified models copied into a frozen Pricing run."""

    root = Path(datastore_root).resolve()
    if candidate.get("permanently_invalidated") is not False:
        raise CandidateError("Permanently invalidated candidate models cannot be loaded")
    run = (root / str(candidate.get("pricing_run_path", ""))).resolve()
    models: dict[tuple[str, str], PricingRouteModel] = {}
    for raw_symbol, raw_call_put in required_routes:
        symbol = str(raw_symbol).strip().upper()
        call_put = str(raw_call_put).strip().upper()
        parent = run / "model-artifacts" / symbol / call_put.lower()
        directories = sorted(path for path in parent.glob("*") if path.is_dir())
        if len(directories) != 1:
            raise CandidateError(
                f"Frozen route has no unique model: {symbol}/{call_put.lower()}"
            )
        directory = directories[0]
        try:
            manifest = json.loads(
                (directory / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CandidateError(
                f"Frozen route manifest is unreadable: {symbol}/{call_put.lower()}"
            ) from exc
        metadata = manifest.get("model_file")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        model_path = directory / str(metadata.get("path", "model.joblib"))
        if (
            not model_path.is_file()
            or int(metadata.get("size", -1)) != model_path.stat().st_size
            or metadata.get("checksum_sha256") != file_checksum(model_path)
        ):
            raise CandidateError(
                f"Frozen route model checksum failed: {symbol}/{call_put.lower()}"
            )
        payload = joblib.load(model_path)
        required = {
            "bsgp",
            "standard_gp",
            "interval_calibration",
            "constant_residual",
        }
        if not isinstance(payload, Mapping) or not required.issubset(payload):
            raise CandidateError(
                f"Frozen route model payload is invalid: {symbol}/{call_put.lower()}"
            )
        models[(symbol, call_put)] = PricingRouteModel(
            symbol=symbol,
            call_put=call_put,
            bsgp=payload["bsgp"],
            standard_gp=payload["standard_gp"],
            interval_calibration=payload["interval_calibration"],
            constant_residual=float(payload["constant_residual"]),
            artifact_directory=directory,
            offline_evaluation=manifest.get("offline_evaluation", {}),
            reused=True,
        )
    return models


def _require_offline_gates(report: Mapping[str, object]) -> None:
    raw_gates = report.get("gates")
    gates = raw_gates if isinstance(raw_gates, Sequence) else ()
    by_number = {
        int(gate.get("number")): gate
        for gate in gates
        if isinstance(gate, Mapping) and str(gate.get("number", "")).isdigit()
    }
    failures = [
        number
        for number in range(1, 9)
        if by_number.get(number, {}).get("status") != "PASS"
    ]
    if failures:
        raise CandidateError(
            "Offline gates must pass before candidate freeze: "
            + ", ".join(map(str, failures))
        )


def _production_offline_evidence_verified(
    report: Mapping[str, object], *, lineage: Mapping[str, object]
) -> bool:
    raw_gates = report.get("gates")
    gates = raw_gates if isinstance(raw_gates, Sequence) else ()
    by_number = {
        int(gate.get("number")): gate
        for gate in gates
        if isinstance(gate, Mapping) and str(gate.get("number", "")).isdigit()
    }
    gate_one_evidence = by_number.get(1, {}).get("evidence")
    gate_one_evidence = (
        gate_one_evidence if isinstance(gate_one_evidence, Mapping) else {}
    )
    lane_guard = gate_one_evidence.get("evidence_lane_guard")
    lane_guard = lane_guard if isinstance(lane_guard, Mapping) else {}
    provider = lineage.get("checks")
    provider = provider if isinstance(provider, Mapping) else {}
    provider = provider.get("provider_receipt_evidence")
    provider = provider if isinstance(provider, Mapping) else {}
    routes = report.get("routes")
    routes = routes if isinstance(routes, Mapping) else {}
    required_routes = {
        f"{symbol}/{call_put}"
        for symbol in ("NVDA", "GOOG", "MU")
        for call_put in ("call", "put")
    }
    required_components = {
        "partition",
        "black_scholes",
        "constant_residual",
        "standard_gp",
        "intervals",
        "constraints",
        "economic_edge",
    }
    routes_verified = set(routes) == required_routes and all(
        isinstance(route, Mapping)
        and required_components.issubset(route)
        and all(
            isinstance(route[name], Mapping) and route[name].get("status") == "PASS"
            for name in required_components
        )
        for route in routes.values()
    )
    return bool(
        all(by_number.get(number, {}).get("status") == "PASS" for number in range(1, 9))
        and lineage.get("evidence_kind") == "REAL_RECEIPT_PROVEN"
        and lineage.get("fixture_test_evidence") is False
        and lane_guard.get("pass") is True
        and not lane_guard.get("fixture_providers")
        and int(provider.get("offline_rows", 0)) > 0
        and int(provider.get("verified_definition_imports", 0)) >= 1
        and int(provider.get("verified_cbbo_imports", 0)) >= 1
        and int(provider.get("matching_policy_cbbo_imports", 0))
        == int(provider.get("verified_cbbo_imports", 0))
        and not provider.get("errors")
        and routes_verified
    )


def _run_lockbox_inventory(run: Path) -> Mapping[str, object] | None:
    try:
        inventory = json.loads(
            (run / "closed-lockbox-inventory.json").read_text(encoding="utf-8")
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return inventory if isinstance(inventory, Mapping) else None


def verify_fresh_future_lockbox_targets(
    datastore_root: Path,
    *,
    target_snapshot_fors: Sequence[object],
    exclude_candidate_id: str | None = None,
) -> Mapping[str, object]:
    """Require a new candidate's entire lockbox to follow every prior attempt."""

    root = Path(datastore_root).resolve()
    parsed = pd.to_datetime(
        list(target_snapshot_fors), utc=True, errors="coerce"
    )
    if len(parsed) < 1 or pd.isna(parsed).any() or len(set(parsed)) != len(parsed):
        raise CandidateError("Candidate lockbox targets are invalid or duplicated")
    prior_targets: list[pd.Timestamp] = []
    prior_candidates: list[str] = []
    attempts_root = root / "ml" / "option-pricing-lockbox-results"
    for attempt_path in sorted(attempts_root.glob("*/attempt.json")):
        try:
            attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CandidateError("A prior lockbox attempt is unreadable") from exc
        prior_id = str(attempt.get("candidate_id", "")) if isinstance(attempt, Mapping) else ""
        if (
            not isinstance(attempt, Mapping)
            or attempt.get("schema_version") != "option-pricing-lockbox-attempt-v1"
            or not prior_id
        ):
            raise CandidateError("A prior lockbox attempt is malformed")
        if prior_id == exclude_candidate_id:
            continue
        candidate_dir = root / "ml" / "option-pricing-candidates" / prior_id
        try:
            prior = json.loads(
                (candidate_dir / "candidate.json").read_text(encoding="utf-8")
            )
            receipt = json.loads(
                (candidate_dir / "receipt.json").read_text(encoding="utf-8")
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CandidateError("A prior attempted candidate is unreadable") from exc
        if (
            not isinstance(prior, Mapping)
            or not isinstance(receipt, Mapping)
            or prior.get("candidate_id") != prior_id
            or receipt.get("candidate_id") != prior_id
            or receipt.get("candidate_checksum_sha256")
            != file_checksum(candidate_dir / "candidate.json")
        ):
            raise CandidateError("A prior attempted candidate receipt failed verification")
        prior_closed = prior.get("closed_lockbox")
        prior_closed = prior_closed if isinstance(prior_closed, Mapping) else {}
        prior_parsed = pd.to_datetime(
            list(prior_closed.get("target_snapshot_fors", ())),
            utc=True,
            errors="coerce",
        )
        if len(prior_parsed) < 1 or pd.isna(prior_parsed).any():
            raise CandidateError("A prior attempted lockbox target inventory is invalid")
        prior_targets.extend(pd.Timestamp(value) for value in prior_parsed)
        prior_candidates.append(prior_id)
    first_new = min(pd.Timestamp(value) for value in parsed)
    latest_prior = max(prior_targets) if prior_targets else None
    passed = latest_prior is None or first_new > latest_prior
    return {
        "status": "PASS" if passed else "NOT_PROVEN",
        "first_new_target": first_new.isoformat(),
        "last_new_target": max(pd.Timestamp(value) for value in parsed).isoformat(),
        "prior_attempted_candidate_ids": prior_candidates,
        "prior_attempted_latest_target": (
            latest_prior.isoformat() if latest_prior is not None else None
        ),
        "all_new_targets_strictly_future": passed,
    }


def _verify_candidate_references(root: Path, candidate: Mapping[str, object]) -> None:
    run = (root / str(candidate.get("pricing_run_path", ""))).resolve()
    allowed = (root / "ml" / "option-pricing-runs").resolve()
    if run.parent != allowed:
        raise CandidateError("Frozen candidate references an invalid Pricing run")
    if (
        candidate.get("pricing_manifest_checksum_sha256")
        != file_checksum(run / "manifest.json")
        or candidate.get("pricing_receipt_checksum_sha256")
        != file_checksum(run / "publication.json")
    ):
        raise CandidateError("Frozen candidate Pricing evidence changed")
    policy = (root / str(candidate.get("eligibility_policy_path", ""))).resolve()
    policy_root = (root / "ml" / "option-pricing-eligibility-policies").resolve()
    if (
        policy.parent != policy_root
        or policy.name != candidate.get("eligibility_policy_hash")
        or candidate.get("eligibility_policy_receipt_checksum_sha256")
        != file_checksum(policy / "receipt.json")
    ):
        raise CandidateError("Frozen candidate eligibility policy changed")
    for raw in candidate.get("model_artifacts", ()):
        if not isinstance(raw, Mapping):
            raise CandidateError("Frozen candidate model inventory is malformed")
        path = (root / str(raw.get("path", ""))).resolve()
        if (
            not path.is_file()
            or int(raw.get("size", -1)) != path.stat().st_size
            or raw.get("checksum_sha256") != file_checksum(path)
        ):
            raise CandidateError(f"Frozen candidate model artifact changed: {path}")
    for raw in candidate.get("source_evidence", ()):
        if not isinstance(raw, Mapping):
            raise CandidateError("Frozen candidate source inventory is malformed")
        path = (root / str(raw.get("path", ""))).resolve()
        if (
            raw.get("status") != "present"
            or not path.is_file()
            or int(raw.get("size", -1)) != path.stat().st_size
            or raw.get("checksum_sha256") != file_checksum(path)
        ):
            raise CandidateError(f"Frozen candidate source evidence changed: {path}")
    closed = candidate.get("closed_lockbox")
    closed = closed if isinstance(closed, Mapping) else {}
    if (
        closed.get("target_values_read") is not False
        or not closed.get("target_snapshot_fors")
        or not isinstance(closed.get("outputs"), list)
    ):
        raise CandidateError("Frozen candidate lockbox inventory is malformed")
    opra_root = (root / "ml" / "option-pricing-evidence" / "opra").resolve()
    for raw in closed["outputs"]:
        if not isinstance(raw, Mapping):
            raise CandidateError("Frozen candidate lockbox output is malformed")
        path = (root / str(raw.get("path", ""))).resolve()
        if (
            opra_root not in path.parents
            or not path.is_file()
            or int(raw.get("size", -1)) != path.stat().st_size
            or raw.get("checksum_sha256") != file_checksum(path)
        ):
            raise CandidateError(f"Frozen candidate lockbox output changed: {path}")
    repository = Path(__file__).resolve().parents[2]
    for inventory_name in ("dependency_contracts", "code_inventory"):
        for raw in candidate.get(inventory_name, ()):
            if not isinstance(raw, Mapping):
                raise CandidateError(
                    f"Frozen candidate {inventory_name} inventory is malformed"
                )
            path = (repository / str(raw.get("path", ""))).resolve()
            if (
                not path.is_file()
                or int(raw.get("size", -1)) != path.stat().st_size
                or raw.get("checksum_sha256") != file_checksum(path)
            ):
                raise CandidateError(
                    f"Frozen candidate {inventory_name} changed: {path}"
                )
    runtime = candidate.get("runtime_versions")
    runtime = runtime if isinstance(runtime, Mapping) else {}
    python = runtime.get("python")
    python = python if isinstance(python, Mapping) else {}
    if (
        python.get("implementation") != platform.python_implementation()
        or python.get("version") != platform.python_version()
    ):
        raise CandidateError("Frozen candidate Python environment changed")
    dependencies = runtime.get("dependencies")
    if not isinstance(dependencies, Mapping):
        raise CandidateError("Frozen candidate dependency environment is malformed")
    for package, expected in dependencies.items():
        try:
            observed: str | None = importlib.metadata.version(str(package))
        except importlib.metadata.PackageNotFoundError:
            observed = None
        if observed != expected:
            raise CandidateError(
                f"Frozen candidate dependency changed: {package}={observed!r}, "
                f"expected {expected!r}"
            )


def _dependency_inventory() -> list[dict[str, object]]:
    repository = Path(__file__).resolve().parents[2]
    return _file_inventory(
        repository,
        relative_to=repository,
        names=("pyproject.toml", "requirements-ml-runtime.lock"),
    )


def _file_inventory(
    directory: Path,
    *,
    relative_to: Path,
    names: Sequence[str] | None = None,
) -> list[dict[str, object]]:
    base = Path(directory)
    if names is None:
        paths = sorted(path for path in base.rglob("*") if path.is_file())
    else:
        paths = [base / name for name in names]
    records: list[dict[str, object]] = []
    for path in paths:
        if not path.is_file():
            raise CandidateError(f"Candidate dependency/artifact is missing: {path}")
        records.append(
            {
                "path": path.resolve().relative_to(relative_to.resolve()).as_posix(),
                "size": path.stat().st_size,
                "checksum_sha256": file_checksum(path),
            }
        )
    return records


def _read_invalidation(path: Path, *, candidate_id: str) -> Mapping[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CandidateError("Candidate invalidation marker is unreadable") from exc
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema_version") != CANDIDATE_INVALIDATION_VERSION
        or payload.get("candidate_id") != candidate_id
        or payload.get("permanently_invalidated") is not True
        or payload.get("rescore_allowed") is not False
        or payload.get("automated_action_allowed") is not False
        or pd.isna(pd.to_datetime(payload.get("invalidated_at"), utc=True, errors="coerce"))
    ):
        raise CandidateError("Candidate invalidation marker failed verification")
    return payload


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    _write_json(temporary, payload)
    temporary.replace(path)


__all__ = [
    "CANDIDATE_SCHEMA_VERSION",
    "CandidateError",
    "freeze_candidate",
    "load_candidate_models",
    "permanently_invalidate_candidate",
    "read_candidate",
    "read_current_candidate",
    "verify_fresh_future_lockbox_targets",
]
