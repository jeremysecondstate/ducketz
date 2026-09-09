from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, Mapping, Sequence

import pandas as pd
from filelock import FileLock, Timeout

from ml.artifacts import file_checksum, file_inventory, utc_timestamp
from ml.option_pricing.candidate import (
    load_candidate_models,
    permanently_invalidate_candidate,
    read_candidate,
    verify_fresh_future_lockbox_targets,
)
from ml.option_pricing.causal import evaluate_offline_predictions
from ml.option_pricing.eligibility import (
    EligibilityPolicy,
    EligibilityPolicyArtifact,
    eligibility_policy_payload,
    evaluate_required_route_performance,
    read_current_eligibility_report,
)
from ml.option_pricing.model import compare_pricing_models
from ml.option_pricing.opra_materialization import materialize_committed_opra_history
from ml.option_pricing.policies import ContractSelectionPolicy, ProjectionPolicy
from ml.option_pricing.prediction import create_prediction_rows
from ml.universe import PRODUCTION_OPTION_SYMBOLS


LOCKBOX_AUTHORIZATION_VERSION = "option-pricing-lockbox-authorization-v1"
LOCKBOX_ATTEMPT_VERSION = "option-pricing-lockbox-attempt-v1"
LOCKBOX_RESULT_VERSION = "option-pricing-lockbox-result-v1"
LOCKBOX_RECEIPT_VERSION = "option-pricing-lockbox-result-receipt-v1"
LOCKBOX_ACTION = "OPEN_AND_SCORE_OPTION_PRICING_LOCKBOX_ONCE"


class LockboxError(RuntimeError):
    """The one-time Pricing lockbox protocol failed closed."""


def score_closed_lockbox_once(
    datastore_root: Path,
    *,
    candidate_id: str,
    policy_artifact: EligibilityPolicyArtifact,
    authorization_path: Path,
    policy: EligibilityPolicy | None = None,
    scored_at: object | None = None,
    materializer: Callable[[], pd.DataFrame] | None = None,
) -> Mapping[str, object]:
    """Serialize every one-time attempt across all candidate identities."""

    root = Path(datastore_root).resolve()
    writer_lock = FileLock(str(root / ".ducketz-option-pricing-lockbox.lock"))
    try:
        with writer_lock.acquire(timeout=0):
            return _score_closed_lockbox_once_locked(
                root,
                candidate_id=candidate_id,
                policy_artifact=policy_artifact,
                authorization_path=authorization_path,
                policy=policy,
                scored_at=scored_at,
                materializer=materializer,
            )
    except Timeout as exc:
        raise LockboxError("Another lockbox evaluator owns the one-time writer lock") from exc


def _score_closed_lockbox_once_locked(
    datastore_root: Path,
    *,
    candidate_id: str,
    policy_artifact: EligibilityPolicyArtifact,
    authorization_path: Path,
    policy: EligibilityPolicy | None = None,
    scored_at: object | None = None,
    materializer: Callable[[], pd.DataFrame] | None = None,
) -> Mapping[str, object]:
    """Consume one authorization and score a frozen candidate exactly once.

    The attempt marker is committed before the target materializer is invoked.
    Any failure after that point permanently invalidates the candidate.
    """

    root = Path(datastore_root).resolve()
    candidate_dir = root / "ml" / "option-pricing-candidates" / candidate_id
    candidate = read_candidate(candidate_dir, datastore_root=root)
    if candidate.get("permanently_invalidated") is not False:
        raise LockboxError("The frozen candidate is permanently invalidated")
    fixture_materializer = materializer is not None
    if fixture_materializer and candidate.get("production_evidence_eligible") is True:
        raise LockboxError("A production candidate cannot use an injected lockbox materializer")
    if candidate.get("eligibility_policy_hash") != policy_artifact.policy_hash:
        raise LockboxError("Candidate and eligibility-policy hashes do not match")
    effective_policy = policy or EligibilityPolicy()
    if (
        eligibility_policy_payload(effective_policy).get("policy_values")
        != policy_artifact.policy.get("policy_values")
    ):
        raise LockboxError("Lockbox thresholds differ from the frozen eligibility policy")
    contract_policy, projection_policy = _frozen_scoring_policies(policy_artifact)
    timestamp = utc_timestamp(scored_at)
    authorization = read_lockbox_authorization(
        authorization_path,
        candidate_id=candidate_id,
        policy_hash=policy_artifact.policy_hash,
    )
    approved_at = pd.Timestamp(authorization["approved_at"])
    if approved_at > timestamp:
        raise LockboxError("Lockbox authorization is future-dated")
    prelockbox = read_current_eligibility_report(root)
    _require_non_lockbox_evidence(prelockbox.report, candidate_id=candidate_id)

    results_root = root / "ml" / "option-pricing-lockbox-results"
    destination = results_root / candidate_id
    if destination.exists():
        if (destination / "result.json").is_file() and (
            destination / "receipt.json"
        ).is_file():
            return read_lockbox_result(root, candidate_id=candidate_id)
        return _terminalize_interrupted_attempt(
            root,
            candidate=candidate,
            directory=destination,
            scored_at=scored_at,
        )
    freshness = verify_fresh_future_lockbox_targets(
        root,
        target_snapshot_fors=candidate["closed_lockbox"]["target_snapshot_fors"],
        exclude_candidate_id=candidate_id,
    )
    if freshness.get("status") != "PASS":
        raise LockboxError(
            "Candidate lockbox is not genuinely future to every prior attempt"
        )
    results_root.mkdir(parents=True, exist_ok=True)
    # mkdir(exist_ok=False) is the one-time claim. There is no second target read.
    destination.mkdir()
    attempt = {
        "schema_version": LOCKBOX_ATTEMPT_VERSION,
        "candidate_id": candidate_id,
        "eligibility_policy_hash": policy_artifact.policy_hash,
        "authorization_checksum_sha256": file_checksum(Path(authorization_path)),
        "authorization": authorization,
        "prelockbox_report_checksum_sha256": file_checksum(
            prelockbox.directory / "eligibility-report.json"
        ),
        "attempted_at": timestamp.isoformat(),
        "one_time_score": True,
        "target_read_started_after_attempt_receipt": True,
        "fresh_future_lockbox": freshness,
        "fitting_allowed": False,
        "calibration_allowed": False,
        "selection_allowed": False,
        "threshold_changes_allowed": False,
        "automated_action_allowed": False,
    }
    _write_json_atomic(destination / "attempt.json", attempt)
    try:
        samples = (
            materializer()
            if materializer is not None
            else _materialize_candidate_lockbox(
                root,
                candidate,
                contract_policy=contract_policy,
            )
        )
        result, predictions, evaluations = _score_samples(
            root,
            candidate=candidate,
            policy=effective_policy,
            samples=samples,
            scored_at=timestamp,
            projection_policy=projection_policy,
        )
        result = {
            **result,
            "evidence_kind": (
                "FIXTURE_TEST_ONLY"
                if fixture_materializer
                else "REAL_RECEIPT_PROVEN"
            ),
            "production_evidence_eligible": bool(
                not fixture_materializer
                and candidate.get("production_evidence_eligible") is True
            ),
        }
        _write_result_artifacts(
            destination,
            result=result,
            samples=samples,
            predictions=predictions,
            evaluations=evaluations,
            candidate=candidate,
            timestamp=timestamp,
        )
    except BaseException as exc:
        failure = {
            "schema_version": LOCKBOX_RESULT_VERSION,
            "status": "FAIL",
            "candidate_id": candidate_id,
            "eligibility_policy_hash": policy_artifact.policy_hash,
            "one_time_score": True,
            "all_required_routes_pass": False,
            "all_required_buckets_pass": False,
            "evidence_kind": (
                "FIXTURE_TEST_ONLY"
                if fixture_materializer
                else "REAL_RECEIPT_PROVEN"
            ),
            "production_evidence_eligible": False,
            "failure_reason": f"{type(exc).__name__}: {exc}",
            "permanently_invalidates_candidate": True,
            "rescore_allowed": False,
            "automated_action_allowed": False,
        }
        _write_result_artifacts(
            destination,
            result=failure,
            samples=pd.DataFrame(),
            predictions=pd.DataFrame(),
            evaluations=pd.DataFrame(),
            candidate=candidate,
            timestamp=timestamp,
        )
        permanently_invalidate_candidate(
            root,
            candidate_id=candidate_id,
            reason=failure["failure_reason"],
            lockbox_result_checksum_sha256=file_checksum(destination / "result.json"),
            invalidated_at=timestamp,
        )
        raise LockboxError(
            "Lockbox attempt failed and permanently invalidated the candidate"
        ) from exc
    final = read_lockbox_result(root, candidate_id=candidate_id)
    if final.get("status") != "PASS":
        permanently_invalidate_candidate(
            root,
            candidate_id=candidate_id,
            reason="LOCKBOX_THRESHOLDS_FAILED",
            lockbox_result_checksum_sha256=file_checksum(destination / "result.json"),
            invalidated_at=timestamp,
        )
    return final


def read_lockbox_authorization(
    path: Path,
    *,
    candidate_id: str,
    policy_hash: str,
) -> Mapping[str, object]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LockboxError("Lockbox authorization record is unreadable") from exc
    if not isinstance(payload, Mapping):
        raise LockboxError("Lockbox authorization record is malformed")
    approved = pd.to_datetime(payload.get("approved_at"), utc=True, errors="coerce")
    if (
        payload.get("schema_version") != LOCKBOX_AUTHORIZATION_VERSION
        or payload.get("action") != LOCKBOX_ACTION
        or payload.get("candidate_id") != candidate_id
        or payload.get("eligibility_policy_hash") != policy_hash
        or payload.get("maximum_score_attempts") != 1
        or not str(payload.get("authorization_id", "")).strip()
        or not str(payload.get("operator_id", "")).strip()
        or pd.isna(approved)
        or payload.get("automated_action_allowed") is not False
    ):
        raise LockboxError("Lockbox authorization record failed validation")
    return dict(payload)


def read_lockbox_result(
    datastore_root: Path, *, candidate_id: str
) -> Mapping[str, object]:
    root = Path(datastore_root).resolve()
    directory = (root / "ml" / "option-pricing-lockbox-results" / candidate_id).resolve()
    allowed = (root / "ml" / "option-pricing-lockbox-results").resolve()
    if directory.parent != allowed:
        raise LockboxError("Lockbox result path escapes its immutable root")
    try:
        result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
        receipt = json.loads((directory / "receipt.json").read_text(encoding="utf-8"))
        attempt = json.loads((directory / "attempt.json").read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LockboxError("Lockbox result artifact is unreadable") from exc
    outputs = receipt.get("outputs") if isinstance(receipt, Mapping) else None
    if (
        not isinstance(result, Mapping)
        or not isinstance(receipt, Mapping)
        or not isinstance(attempt, Mapping)
        or not isinstance(outputs, Mapping)
        or result.get("schema_version") != LOCKBOX_RESULT_VERSION
        or result.get("candidate_id") != candidate_id
        or result.get("one_time_score") is not True
        or result.get("rescore_allowed") is not False
        or result.get("automated_action_allowed") is not False
        or receipt.get("schema_version") != LOCKBOX_RECEIPT_VERSION
        or receipt.get("candidate_id") != candidate_id
        or receipt.get("eligibility_policy_hash")
        != result.get("eligibility_policy_hash")
        or attempt.get("schema_version") != LOCKBOX_ATTEMPT_VERSION
        or attempt.get("candidate_id") != candidate_id
        or attempt.get("eligibility_policy_hash")
        != result.get("eligibility_policy_hash")
        or attempt.get("one_time_score") is not True
        or attempt.get("fitting_allowed") is not False
        or attempt.get("calibration_allowed") is not False
        or attempt.get("selection_allowed") is not False
        or attempt.get("threshold_changes_allowed") is not False
        or not isinstance(attempt.get("fresh_future_lockbox"), Mapping)
        or attempt["fresh_future_lockbox"].get("status") != "PASS"
        or attempt.get("automated_action_allowed") is not False
        or receipt.get("automated_action_allowed") is not False
    ):
        raise LockboxError("Lockbox result metadata failed validation")
    for name, raw in outputs.items():
        metadata = raw if isinstance(raw, Mapping) else {}
        path = directory / str(name)
        if (
            not path.is_file()
            or int(metadata.get("size", -1)) != path.stat().st_size
            or metadata.get("checksum_sha256") != file_checksum(path)
        ):
            raise LockboxError(f"Lockbox result output changed: {path}")
    return dict(result)


def _require_non_lockbox_evidence(
    report: Mapping[str, object], *, candidate_id: str
) -> None:
    gates = report.get("gates")
    if not isinstance(gates, Sequence) or any(
        not isinstance(gate, Mapping) or gate.get("status") != "PASS"
        for gate in gates
    ):
        raise LockboxError("All ten non-lockbox evidence gates must pass first")
    frozen = report.get("frozen_candidate")
    frozen = frozen if isinstance(frozen, Mapping) else {}
    candidate_evidence = frozen.get("evidence")
    candidate_evidence = (
        candidate_evidence if isinstance(candidate_evidence, Mapping) else {}
    )
    operational = report.get("operational_promotion")
    lockbox = report.get("closed_lockbox")
    if (
        frozen.get("status") != "PASS"
        or candidate_evidence.get("candidate_id") != candidate_id
        or not isinstance(operational, Mapping)
        or operational.get("status") != "PASS"
        or not isinstance(lockbox, Mapping)
        or lockbox.get("status") != "NOT_PROVEN"
    ):
        raise LockboxError("Pre-lockbox promotion evidence is incomplete")


def _materialize_candidate_lockbox(
    root: Path,
    candidate: Mapping[str, object],
    *,
    contract_policy: ContractSelectionPolicy,
) -> pd.DataFrame:
    closed = candidate.get("closed_lockbox")
    closed = closed if isinstance(closed, Mapping) else {}
    targets = closed.get("target_snapshot_fors")
    outputs = closed.get("outputs")
    if not isinstance(targets, Sequence) or not isinstance(outputs, Sequence):
        raise LockboxError("Frozen lockbox inventory is incomplete")
    cbbo_paths: list[Path] = []
    for raw in outputs:
        if not isinstance(raw, Mapping):
            raise LockboxError("Frozen lockbox output inventory is malformed")
        path = (root / str(raw.get("path", ""))).resolve()
        if (
            not path.is_file()
            or int(raw.get("size", -1)) != path.stat().st_size
            or raw.get("checksum_sha256") != file_checksum(path)
        ):
            raise LockboxError(f"Frozen lockbox output changed: {path}")
        cbbo_paths.append(path)
    definition_paths: list[Path] = []
    for raw in candidate.get("source_evidence", ()):
        if not isinstance(raw, Mapping):
            continue
        path = (root / str(raw.get("path", ""))).resolve()
        if path.name.startswith("definitions-") and path.name.endswith(".dbn.zst"):
            definition_paths.append(path)
    samples, _, errors = materialize_committed_opra_history(
        root,
        symbols=PRODUCTION_OPTION_SYMBOLS,
        rate_observations=_load_frozen_rate_observations(root, candidate),
        contract_policy=contract_policy,
        target_snapshot_fors=targets,
        allowed_cbbo_paths=cbbo_paths,
        allowed_definition_paths=definition_paths,
    )
    if errors:
        raise LockboxError("Lockbox materialization errors: " + json.dumps(errors))
    if samples.empty:
        raise LockboxError("Lockbox materialization produced no eligible samples")
    observed = set(
        pd.to_datetime(samples["target_snapshot_for"], utc=True, errors="coerce")
    )
    expected = set(pd.to_datetime(list(targets), utc=True, errors="coerce"))
    if not observed.issubset(expected):
        raise LockboxError("Lockbox materializer returned a non-frozen target")
    return samples


def _load_frozen_rate_observations(
    root: Path, candidate: Mapping[str, object]
) -> pd.DataFrame | None:
    paths: list[Path] = []
    for raw in candidate.get("source_evidence", ()):
        if not isinstance(raw, Mapping):
            continue
        rendered = str(raw.get("path", ""))
        if "release-context/fred" in rendered.replace("\\", "/") and rendered.endswith(
            ".parquet"
        ):
            paths.append((root / rendered).resolve())
    if not paths:
        return None
    combined = pd.concat((pd.read_parquet(path) for path in paths), ignore_index=True)
    if not {"fed_funds_available_at", "macro__fed_funds_level"}.issubset(combined):
        return None
    output = pd.DataFrame(
        {
            "available_at": pd.to_datetime(
                combined["fed_funds_available_at"], utc=True, errors="coerce"
            ),
            "risk_free_rate": pd.to_numeric(
                combined["macro__fed_funds_level"], errors="coerce"
            )
            / 100.0,
        }
    ).dropna()
    return output.loc[output["risk_free_rate"].between(-0.20, 1.0)].sort_values(
        "available_at"
    ).drop_duplicates("available_at", keep="last").reset_index(drop=True)


def _score_samples(
    root: Path,
    *,
    candidate: Mapping[str, object],
    policy: EligibilityPolicy,
    samples: pd.DataFrame,
    scored_at: pd.Timestamp,
    projection_policy: ProjectionPolicy,
) -> tuple[Mapping[str, object], pd.DataFrame, pd.DataFrame]:
    models = load_candidate_models(
        root,
        candidate=candidate,
        required_routes=policy.required_routes,
    )
    predictions = pd.concat(
        [
            create_prediction_rows(
                cluster,
                prediction_created_at=target,
                prediction_available_at=target,
                models=models,
                projection_policy=projection_policy,
            )
            for target, cluster in samples.groupby("target_snapshot_for", sort=True)
        ],
        ignore_index=True,
        sort=False,
    )
    evaluations = evaluate_offline_predictions(
        predictions,
        samples,
        evaluated_at=scored_at,
    )
    model_reports: dict[str, object] = {}
    for symbol, call_put in policy.required_routes:
        route = samples.loc[
            samples["symbol"].astype("string").str.upper().eq(symbol)
            & samples["call_put"].astype("string").str.upper().eq(call_put)
        ].copy()
        name = f"{symbol}/{call_put.lower()}"
        model = models.get((symbol, call_put))
        if model is None or route.empty:
            model_reports[name] = {
                "status": "MODEL_NOT_AVAILABLE",
                "source_provider": "databento-opra",
            }
            continue
        model_reports[name] = {
            "status": "MODEL_FIT",
            "source_provider": "databento-opra",
            "assessment_metrics": compare_pricing_models(
                route,
                bsgp=model.bsgp,
                standard_gp=model.standard_gp,
                constant_residual=model.constant_residual,
                interval_calibration=model.interval_calibration,
            ),
        }
    route_evidence = evaluate_required_route_performance(
        policy=policy,
        evaluations=evaluations,
        predictions=predictions,
        model_reports={"model_reports": model_reports},
        include_partitions=False,
        include_prospective=False,
    )
    required_keys = (
        "black_scholes",
        "constant_residual",
        "standard_gp",
        "intervals",
        "constraints",
        "economic_edge",
    )
    for symbol, call_put in policy.required_routes:
        name = f"{symbol}/{call_put.lower()}"
        route = samples.loc[
            samples["symbol"].astype("string").str.upper().eq(symbol)
            & samples["call_put"].astype("string").str.upper().eq(call_put)
        ]
        observed_clusters = int(route["target_snapshot_for"].nunique())
        route_evidence[name]["lockbox_partition"] = {
            "status": (
                "PASS"
                if observed_clusters >= policy.lockbox_clusters
                else "NOT_PROVEN"
            ),
            "observed_clusters": observed_clusters,
            "required_clusters": policy.lockbox_clusters,
            "row_count": len(route),
        }
    route_pass = {
        route: evidence["lockbox_partition"].get("status") == "PASS"
        and all(evidence[key].get("status") == "PASS" for key in required_keys)
        for route, evidence in route_evidence.items()
    }
    all_routes = bool(route_pass) and all(route_pass.values())
    all_buckets = all(
        evidence["economic_edge"].get("bucket_minima_pass") is True
        and evidence["economic_edge"].get("bucket_monotonic") is True
        for evidence in route_evidence.values()
    )
    passed = all_routes and all_buckets
    result = {
        "schema_version": LOCKBOX_RESULT_VERSION,
        "status": "PASS" if passed else "FAIL",
        "candidate_id": candidate["candidate_id"],
        "eligibility_policy_hash": candidate["eligibility_policy_hash"],
        "scored_at": scored_at.isoformat(),
        "one_time_score": True,
        "all_required_routes_pass": all_routes,
        "all_required_buckets_pass": all_buckets,
        "route_pass": route_pass,
        "routes": route_evidence,
        "sample_rows": len(samples),
        "target_snapshot_clusters": int(samples["target_snapshot_for"].nunique()),
        "fitting_performed": False,
        "calibration_performed": False,
        "selection_performed": False,
        "threshold_changes_performed": False,
        "permanently_invalidates_candidate": not passed,
        "rescore_allowed": False,
        "automated_action_allowed": False,
    }
    return result, predictions, evaluations


def _frozen_scoring_policies(
    policy_artifact: EligibilityPolicyArtifact,
) -> tuple[ContractSelectionPolicy, ProjectionPolicy]:
    model_contract = policy_artifact.policy.get("model_contract")
    model_contract = model_contract if isinstance(model_contract, Mapping) else {}
    contract = model_contract.get("contract_selection")
    projection = model_contract.get("projection")
    if not isinstance(contract, Mapping) or not isinstance(projection, Mapping):
        raise LockboxError("Frozen policy has no exact materialization/projection contract")
    try:
        return ContractSelectionPolicy(**dict(contract)), ProjectionPolicy(
            **dict(projection)
        )
    except (TypeError, ValueError) as exc:
        raise LockboxError("Frozen scoring policies are invalid") from exc


def _write_result_artifacts(
    directory: Path,
    *,
    result: Mapping[str, object],
    samples: pd.DataFrame,
    predictions: pd.DataFrame,
    evaluations: pd.DataFrame,
    candidate: Mapping[str, object],
    timestamp: pd.Timestamp,
) -> None:
    result_path = directory / "result.json"
    _write_json_atomic(result_path, result)
    outputs = ["attempt.json", "result.json"]
    for name, frame in (
        ("lockbox-samples.parquet", samples),
        ("lockbox-predictions.parquet", predictions),
        ("lockbox-evaluations.parquet", evaluations),
    ):
        if not frame.empty:
            frame.to_parquet(directory / name, index=False)
            outputs.append(name)
    receipt = {
        "schema_version": LOCKBOX_RECEIPT_VERSION,
        "candidate_id": candidate["candidate_id"],
        "eligibility_policy_hash": candidate["eligibility_policy_hash"],
        "published_at": timestamp.isoformat(),
        "outputs": file_inventory(directory, outputs),
        "automated_action_allowed": False,
    }
    _write_json_atomic(directory / "receipt.json", receipt)


def _terminalize_interrupted_attempt(
    root: Path,
    *,
    candidate: Mapping[str, object],
    directory: Path,
    scored_at: object | None,
) -> Mapping[str, object]:
    timestamp = utc_timestamp(scored_at)
    failure = {
        "schema_version": LOCKBOX_RESULT_VERSION,
        "status": "FAIL",
        "candidate_id": candidate["candidate_id"],
        "eligibility_policy_hash": candidate["eligibility_policy_hash"],
        "scored_at": timestamp.isoformat(),
        "one_time_score": True,
        "all_required_routes_pass": False,
        "all_required_buckets_pass": False,
        "evidence_kind": "UNVERIFIED_INTERRUPTED_ATTEMPT",
        "production_evidence_eligible": False,
        "failure_reason": "INTERRUPTED_PRIOR_ATTEMPT",
        "permanently_invalidates_candidate": True,
        "rescore_allowed": False,
        "automated_action_allowed": False,
    }
    _write_result_artifacts(
        directory,
        result=failure,
        samples=pd.DataFrame(),
        predictions=pd.DataFrame(),
        evaluations=pd.DataFrame(),
        candidate=candidate,
        timestamp=timestamp,
    )
    permanently_invalidate_candidate(
        root,
        candidate_id=str(candidate["candidate_id"]),
        reason="INTERRUPTED_PRIOR_ATTEMPT",
        lockbox_result_checksum_sha256=file_checksum(directory / "result.json"),
        invalidated_at=timestamp,
    )
    return read_lockbox_result(root, candidate_id=str(candidate["candidate_id"]))


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


__all__ = [
    "LOCKBOX_ACTION",
    "LOCKBOX_AUTHORIZATION_VERSION",
    "LockboxError",
    "read_lockbox_authorization",
    "read_lockbox_result",
    "score_closed_lockbox_once",
]
