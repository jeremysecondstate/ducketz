from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd
import pyarrow.parquet as pq
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from ml.artifacts import file_checksum, utc_timestamp
from ml.option_pricing.eligibility import REQUIRED_SYMBOLS, read_eligibility_policy
from ml.option_pricing.lineage import verify_completed_option_pricing_lineage
from ml.option_pricing.publication import (
    OPTION_PRICING_POINTER_VERSION,
    authoritative_option_pricing_runs,
    pricing_pointer_path,
    read_current_option_pricing_publication,
)


OPERATIONAL_REPORT_VERSION = "option-pricing-operational-readiness-v1"
OPERATIONAL_RECEIPT_VERSION = "option-pricing-operational-readiness-receipt-v1"
OPERATIONAL_POINTER_VERSION = "option-pricing-operational-readiness-pointer-v1"
RUNTIME_HEALTH_VERSION = "option-pricing-runtime-health-v1"
ROLLBACK_AUTHORIZATION_VERSION = "option-pricing-rollback-authorization-v1"
ROLLBACK_RECEIPT_VERSION = "option-pricing-pointer-rollback-v1"

EXIT_OK = 0
EXIT_RUNTIME_FAILURE = 1
EXIT_CONFIGURATION = 2
EXIT_DEPENDENCY = 3
EXIT_CAPACITY = 4
EXIT_PUBLICATION = 5
EXIT_EVIDENCE = 6

_REQUIRED_PRODUCTION_PYTHON = (3, 13)
_VERIFIED_OPTIONAL_DEPENDENCY_GROUPS = ("ml", "ml-tree", "ml-test")


class OperationalError(RuntimeError):
    """Operational readiness, health, or rollback verification failed."""


@dataclass(frozen=True)
class RuntimeLimits:
    minimum_free_disk_bytes: int = 5 * 1024**3
    maximum_cycle_seconds: float = 600.0
    maximum_peak_memory_bytes: int = 4 * 1024**3
    maximum_sample_rows: int = 2_000_000
    maximum_prediction_rows: int = 1_500_000
    maximum_evaluation_rows: int = 1_500_000
    maximum_surface_rows: int = 500_000
    maximum_model_rows_per_route: int = 500_000
    maximum_stale_pointer_minutes: int = 45
    evidence_stagnation_alert_hours: int = 48
    immutable_retention: str = "FOREVER_UNLESS_SEPARATELY_AUTHORIZED_ARCHIVE"

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if name == "immutable_retention":
                continue
            if float(value) <= 0:
                raise ValueError(f"{name} must be positive")


def dependency_contract_report(
    repository_root: Path | None = None,
) -> Mapping[str, object]:
    repository = (
        Path(repository_root).resolve()
        if repository_root is not None
        else Path(__file__).resolve().parents[2]
    )
    lock_path = repository / "requirements-ml-runtime.lock"
    pyproject_path = repository / "pyproject.toml"
    locked = _parse_exact_lock(lock_path)
    details: dict[str, object] = {}
    supported_python = (
        sys.implementation.name == "cpython"
        and sys.version_info[:2] == _REQUIRED_PRODUCTION_PYTHON
    )
    pass_all = supported_python
    for distribution, expected in sorted(locked.items()):
        try:
            installed = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            installed = None
        matched = expected is not None and installed == expected
        pass_all &= matched
        details[distribution] = {
            "locked": expected,
            "installed": installed,
            "matches": matched,
        }
    declared = _declared_dependency_contract(pyproject_path)
    direct_details: dict[str, object] = {}
    for name, requirement in sorted(declared.items()):
        expected = locked.get(name)
        aligned = expected is not None and (
            not requirement.specifier
            or requirement.specifier.contains(expected, prereleases=True)
        )
        pass_all &= aligned
        direct_details[name] = {
            "requirement": str(requirement),
            "locked": expected,
            "aligned": aligned,
        }
    direct_databento = "databento" in declared
    pass_all &= direct_databento
    return {
        "status": "PASS" if pass_all else "FAIL",
        "python": {
            "executable": sys.executable,
            "version": sys.version.split()[0],
            "implementation": sys.implementation.name,
            "required": "CPython 3.13.x",
            "supported": supported_python,
        },
        "lock_path": str(lock_path),
        "lock_checksum_sha256": file_checksum(lock_path),
        "pyproject_checksum_sha256": file_checksum(pyproject_path),
        "databento_declared_directly": direct_databento,
        "direct_dependencies": direct_details,
        "all_locked_distributions_verified": all(
            value["matches"] for value in details.values()
        ),
        "packages": details,
    }


def configuration_report(
    datastore_root: Path,
    *,
    symbols: Sequence[str],
    require_databento_secret: bool,
) -> Mapping[str, object]:
    root = Path(datastore_root).resolve()
    normalized = tuple(
        dict.fromkeys(str(value).strip().upper() for value in symbols if str(value).strip())
    )
    exact_symbols = len(normalized) == len(REQUIRED_SYMBOLS) and set(normalized) == set(
        REQUIRED_SYMBOLS
    )
    secret_present = bool(os.environ.get("DATABENTO_API_KEY", "").strip())
    secret_pass = secret_present or not require_databento_secret
    return {
        "status": "PASS" if exact_symbols and secret_pass else "FAIL",
        "datastore": str(root),
        "required_symbols": list(REQUIRED_SYMBOLS),
        "configured_symbols": list(normalized),
        "exact_required_symbols": exact_symbols,
        "databento_secret_required": require_databento_secret,
        "databento_secret_present": secret_present,
        "credentials_rendered": False,
        "automated_action_allowed": False,
    }


def capacity_report(
    datastore_root: Path,
    *,
    limits: RuntimeLimits | None = None,
) -> Mapping[str, object]:
    root = Path(datastore_root).resolve()
    effective = limits or RuntimeLimits()
    anchor = root if root.exists() else root.parent
    usage = shutil.disk_usage(anchor)
    status = "PASS" if usage.free >= effective.minimum_free_disk_bytes else "FAIL"
    return {
        "status": status,
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "minimum_free_bytes": effective.minimum_free_disk_bytes,
        "retention_policy": effective.immutable_retention,
        "automatic_evidence_deletion": False,
    }


def enforce_runtime_limits(
    *,
    samples: pd.DataFrame,
    predictions: pd.DataFrame,
    evaluations: pd.DataFrame,
    surfaces: pd.DataFrame,
    elapsed_seconds: float,
    peak_memory_bytes: int,
    limits: RuntimeLimits | None = None,
) -> None:
    effective = limits or RuntimeLimits()
    observed = {
        "sample_rows": (len(samples), effective.maximum_sample_rows),
        "prediction_rows": (len(predictions), effective.maximum_prediction_rows),
        "evaluation_rows": (len(evaluations), effective.maximum_evaluation_rows),
        "surface_rows": (len(surfaces), effective.maximum_surface_rows),
        "cycle_seconds": (elapsed_seconds, effective.maximum_cycle_seconds),
        "peak_memory_bytes": (peak_memory_bytes, effective.maximum_peak_memory_bytes),
    }
    exceeded = [
        f"{name}={value} exceeds {maximum}"
        for name, (value, maximum) in observed.items()
        if value > maximum
    ]
    if exceeded:
        raise OperationalError("Runtime resource limit exceeded: " + "; ".join(exceeded))


def operational_preflight_report(
    datastore_root: Path,
    *,
    symbols: Sequence[str] = REQUIRED_SYMBOLS,
    limits: RuntimeLimits | None = None,
    repository_root: Path | None = None,
) -> Mapping[str, object]:
    """Run bounded installation, CLI, chain, capacity, and benchmark checks."""

    root = Path(datastore_root).resolve()
    effective = limits or RuntimeLimits()
    dependency = dependency_contract_report(repository_root)
    configuration = configuration_report(
        root,
        symbols=symbols,
        require_databento_secret=False,
    )
    capacity = capacity_report(root, limits=effective)
    pip_check = _subprocess_check(
        (sys.executable, "-m", "pip", "check"), timeout_seconds=120
    )
    cli_checks = {
        module: _subprocess_check(
            (sys.executable, "-m", module, "--help"), timeout_seconds=60
        )
        for module in (
            "ml.option_pricing_runtime",
            "ml.option_pricing_opra",
            "ml.option_pricing_admin",
            "ml.option_pricing_lockbox",
        )
    }
    publication = _publication_preflight(root, limits=effective)
    publication_policy = publication.get("eligibility_policy")
    publication_policy = (
        publication_policy if isinstance(publication_policy, Mapping) else {}
    )
    documentation_path = (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "datafetch-ml"
        / "option-pricing-operations.md"
    )
    documentation = documentation_path.is_file()
    checks_pass = documentation and all(
        value.get("status") == "PASS"
        for value in (
            dependency,
            configuration,
            capacity,
            pip_check,
            *cli_checks.values(),
            publication,
        )
    )
    return {
        "schema_version": OPERATIONAL_REPORT_VERSION,
        "status": "PASS" if checks_pass else "NOT_PROVEN",
        "checked_at": utc_timestamp().isoformat(),
        "runtime_limits": asdict(effective),
        "dependency_contract": dependency,
        "configuration": configuration,
        "capacity_and_retention": capacity,
        "pip_check": pip_check,
        "cli_smoke": cli_checks,
        "publication_and_benchmark": publication,
        "eligibility_policy_hash": publication_policy.get("policy_hash"),
        "startup_shutdown_crash_recovery_documented": documentation,
        "automated_action_allowed": False,
    }


def publish_operational_readiness(
    datastore_root: Path,
    *,
    report: Mapping[str, object],
    published_at: object | None = None,
) -> Mapping[str, object]:
    root = Path(datastore_root).resolve()
    effective_report = dict(report)
    if effective_report.get("status") == "PASS":
        raw_limits = effective_report.get("runtime_limits")
        raw_limits = raw_limits if isinstance(raw_limits, Mapping) else {}
        try:
            limits = RuntimeLimits(**dict(raw_limits))
        except (TypeError, ValueError) as exc:
            raise OperationalError("Operational report has invalid runtime limits") from exc
        configuration = effective_report.get("configuration")
        configuration = configuration if isinstance(configuration, Mapping) else {}
        configured_symbols = configuration.get("configured_symbols", REQUIRED_SYMBOLS)
        symbols = (
            tuple(str(value) for value in configured_symbols)
            if isinstance(configured_symbols, Sequence)
            and not isinstance(configured_symbols, (str, bytes))
            else REQUIRED_SYMBOLS
        )
        effective_report = dict(
            operational_preflight_report(root, symbols=symbols, limits=limits)
        )
        effective_report["reverified_before_publication"] = True
    timestamp = utc_timestamp(published_at)
    parent = root / "ml" / "option-pricing-operational-readiness"
    parent.mkdir(parents=True, exist_ok=True)
    base = timestamp.strftime("%Y%m%dT%H%M%S.%fZ")
    directory = parent / base
    suffix = 2
    while directory.exists():
        directory = parent / f"{base}-{suffix}"
        suffix += 1
    staging = parent / f".{directory.name}.tmp-{os.getpid()}"
    staging.mkdir()
    report_path = staging / "report.json"
    _write_json(report_path, effective_report)
    receipt = {
        "schema_version": OPERATIONAL_RECEIPT_VERSION,
        "run_path": directory.relative_to(root).as_posix(),
        "published_at": timestamp.isoformat(),
        "report_checksum_sha256": file_checksum(report_path),
        "status": effective_report.get("status"),
        "automated_action_allowed": False,
    }
    _write_json(staging / "receipt.json", receipt)
    staging.replace(directory)
    _write_json_atomic(
        root / "ml" / "option-pricing-operational-latest" / "report.json",
        {
            "schema_version": OPERATIONAL_POINTER_VERSION,
            "current": {
                **receipt,
                "receipt_checksum_sha256": file_checksum(directory / "receipt.json"),
            },
        },
    )
    return read_current_operational_readiness(root)


def read_current_operational_readiness(
    datastore_root: Path,
) -> Mapping[str, object] | None:
    root = Path(datastore_root).resolve()
    pointer_path = root / "ml" / "option-pricing-operational-latest" / "report.json"
    if not pointer_path.is_file():
        return None
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OperationalError("Operational pointer is unreadable") from exc
    if not isinstance(pointer, Mapping):
        raise OperationalError("Operational pointer is malformed")
    current = pointer.get("current")
    if (
        pointer.get("schema_version") != OPERATIONAL_POINTER_VERSION
        or not isinstance(current, Mapping)
    ):
        raise OperationalError("Operational pointer is malformed")
    directory = (root / str(current.get("run_path", ""))).resolve()
    allowed = (root / "ml" / "option-pricing-operational-readiness").resolve()
    if directory.parent != allowed:
        raise OperationalError("Operational pointer escapes its immutable root")
    report_path = directory / "report.json"
    receipt_path = directory / "receipt.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OperationalError("Operational artifact is unreadable") from exc
    expected = {**receipt, "receipt_checksum_sha256": file_checksum(receipt_path)}
    if (
        not isinstance(report, Mapping)
        or not isinstance(receipt, Mapping)
        or dict(current) != expected
        or report.get("schema_version") != OPERATIONAL_REPORT_VERSION
        or receipt.get("schema_version") != OPERATIONAL_RECEIPT_VERSION
        or receipt.get("report_checksum_sha256") != file_checksum(report_path)
        or receipt.get("status") != report.get("status")
        or report.get("automated_action_allowed") is not False
        or receipt.get("automated_action_allowed") is not False
    ):
        raise OperationalError("Operational artifact verification failed")
    return dict(report)


def build_runtime_health(
    *,
    pricing_run: Path,
    eligibility_report: Mapping[str, object],
    lineage_report: Mapping[str, object],
    route_errors: Mapping[str, str],
    live_routes: Mapping[str, object],
    live_symbols: Sequence[str] | None = None,
    elapsed_seconds: float,
    peak_memory_bytes: int,
    capacity: Mapping[str, object],
    checked_at: object,
    previous_prospective_count: int | None = None,
    previous_prospective_checked_at: object | None = None,
    limits: RuntimeLimits | None = None,
) -> Mapping[str, object]:
    effective = limits or RuntimeLimits()
    now = utc_timestamp(checked_at)
    alerts: list[dict[str, object]] = []
    if not lineage_report.get("verified"):
        alerts.append(_alert("BROKEN_CHAIN_OR_LINEAGE", "TERMINAL", lineage_report.get("errors")))
    for route, error in sorted(route_errors.items()):
        kind = "MISSED_PHASE" if route.endswith("/live") else "ROUTE_LOSS"
        alerts.append(_alert(kind, "ACTION_REQUIRED", {"route": route, "error": error}))
    expected_live_symbols = tuple(
        dict.fromkeys(
            str(value).strip().upper()
            for value in (live_symbols or REQUIRED_SYMBOLS)
            if str(value).strip()
        )
    )
    for symbol in expected_live_symbols:
        status = live_routes.get(symbol)
        if not isinstance(status, Mapping) or status.get("status") not in {
            "READY",
            "AVAILABLE",
            "TARGET_BAR_NOT_READY",
        }:
            alerts.append(_alert("MISSED_PHASE", "ACTION_REQUIRED", {"symbol": symbol, "status": status}))
    generated = pd.to_datetime(
        eligibility_report.get("generated_at"), utc=True, errors="coerce"
    )
    if pd.isna(generated) or now - pd.Timestamp(generated) > pd.Timedelta(
        minutes=effective.maximum_stale_pointer_minutes
    ):
        alerts.append(
            _alert(
                "STALE_POINTER",
                "TERMINAL",
                {"eligibility_generated_at": eligibility_report.get("generated_at")},
            )
        )
    routes = eligibility_report.get("routes")
    routes = routes if isinstance(routes, Mapping) else {}
    for symbol in REQUIRED_SYMBOLS:
        for call_put in ("call", "put"):
            route = f"{symbol}/{call_put}"
            if route not in routes:
                alerts.append(_alert("ROUTE_LOSS", "TERMINAL", {"route": route}))
                continue
            route_evidence = routes.get(route)
            partition = (
                route_evidence.get("partition")
                if isinstance(route_evidence, Mapping)
                else None
            )
            if not isinstance(partition, Mapping) or partition.get("status") != "PASS":
                alerts.append(
                    _alert(
                        "ROUTE_LOSS",
                        "ACTION_REQUIRED",
                        {"route": route, "partition": partition},
                    )
                )
    if eligibility_report.get("drift_status") == "FAIL":
        alerts.append(
            _alert(
                "DRIFT_FAILURE",
                "ACTION_REQUIRED",
                eligibility_report.get("drift_evidence"),
            )
        )
    gates = {
        int(gate.get("number")): gate
        for gate in eligibility_report.get("gates", ())
        if isinstance(gate, Mapping) and str(gate.get("number", "")).isdigit()
    }
    if gates.get(6, {}).get("status") == "FAIL":
        alerts.append(_alert("INTERVAL_FAILURE", "TERMINAL", gates[6].get("evidence")))
    if gates.get(7, {}).get("status") == "FAIL":
        alerts.append(_alert("CONSTRAINT_FAILURE", "TERMINAL", gates[7].get("evidence")))
    if capacity.get("status") != "PASS":
        alerts.append(_alert("DISK_EXHAUSTION", "TERMINAL", capacity))
    if elapsed_seconds > effective.maximum_cycle_seconds:
        alerts.append(_alert("LATENCY_LIMIT", "TERMINAL", elapsed_seconds))
    if peak_memory_bytes > effective.maximum_peak_memory_bytes:
        alerts.append(_alert("MEMORY_LIMIT", "TERMINAL", peak_memory_bytes))
    current_prospective = _prospective_count(eligibility_report)
    last_increase_at = now
    if (
        previous_prospective_count is not None
        and current_prospective <= previous_prospective_count
        and previous_prospective_checked_at is not None
    ):
        prior = pd.to_datetime(
            previous_prospective_checked_at, utc=True, errors="coerce"
        )
        if not pd.isna(prior):
            last_increase_at = pd.Timestamp(prior)
            if now - last_increase_at >= pd.Timedelta(
                hours=effective.evidence_stagnation_alert_hours
            ):
                alerts.append(
                    _alert(
                        "EVIDENCE_COUNT_STAGNATION",
                        "ACTION_REQUIRED",
                        {
                            "previous_count": previous_prospective_count,
                            "current_count": current_prospective,
                            "since": last_increase_at.isoformat(),
                        },
                    )
                )
    terminal = any(alert["severity"] == "TERMINAL" for alert in alerts)
    return {
        "schema_version": RUNTIME_HEALTH_VERSION,
        "status": "FAIL" if terminal else "DEGRADED" if alerts else "HEALTHY",
        "checked_at": now.isoformat(),
        "pricing_run": str(Path(pricing_run).resolve()),
        "gate_status": eligibility_report.get("gate_status"),
        "alerts": alerts,
        "elapsed_seconds": elapsed_seconds,
        "peak_memory_bytes": peak_memory_bytes,
        "runtime_limits": asdict(effective),
        "prospective_completed_count": current_prospective,
        "prospective_last_increase_at": last_increase_at.isoformat(),
        "actionable_exit_code": EXIT_EVIDENCE if alerts else EXIT_OK,
        "automated_action_allowed": False,
    }


def publish_runtime_health(
    datastore_root: Path, *, health: Mapping[str, object]
) -> Path:
    path = Path(datastore_root) / "ml" / "option-pricing-health" / "latest.json"
    _write_json_atomic(path, health)
    return path


def read_current_runtime_health(datastore_root: Path) -> Mapping[str, object] | None:
    path = Path(datastore_root) / "ml" / "option-pricing-health" / "latest.json"
    if not path.is_file():
        return None
    try:
        health = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OperationalError("Runtime health record is unreadable") from exc
    if (
        not isinstance(health, Mapping)
        or health.get("schema_version") != RUNTIME_HEALTH_VERSION
        or health.get("automated_action_allowed") is not False
        or pd.isna(pd.to_datetime(health.get("checked_at"), utc=True, errors="coerce"))
    ):
        raise OperationalError("Runtime health record is malformed")
    return dict(health)


def rollback_option_pricing_pointer(
    datastore_root: Path,
    *,
    authorization_path: Path,
    restored_at: object | None = None,
) -> Mapping[str, object]:
    """Restore the immediately prior verified pointer without deleting evidence."""

    root = Path(datastore_root).resolve()
    current = read_current_option_pricing_publication(root)
    previous = current.receipt.get("previous_publication")
    if not isinstance(previous, Mapping):
        raise OperationalError("Current Pricing publication has no prior pointer")
    authorization = _read_rollback_authorization(
        authorization_path,
        current_run=current.run_directory.relative_to(root).as_posix(),
        target_run=str(previous.get("run_path", "")),
    )
    timestamp = utc_timestamp(restored_at)
    if pd.Timestamp(authorization["approved_at"]) > timestamp:
        raise OperationalError("Rollback authorization is future-dated")
    reachable = authoritative_option_pricing_runs(root)
    target = (root / str(previous["run_path"])).resolve()
    if target not in reachable:
        raise OperationalError("Rollback target is not in the verified receipt chain")
    # Verify the target in full before the only mutable write.
    original_pointer = current.pointer
    _write_json_atomic(
        pricing_pointer_path(root),
        {"schema_version": OPTION_PRICING_POINTER_VERSION, "current": dict(previous)},
    )
    try:
        restored = read_current_option_pricing_publication(root)
        if restored.run_directory != target:
            raise OperationalError("Rollback pointer did not select its target")
    except BaseException:
        _write_json_atomic(pricing_pointer_path(root), original_pointer)
        raise
    rollback_root = root / "ml" / "option-pricing-rollbacks"
    rollback_root.mkdir(parents=True, exist_ok=True)
    path = rollback_root / f"{timestamp.strftime('%Y%m%dT%H%M%S.%fZ')}.json"
    receipt = {
        "schema_version": ROLLBACK_RECEIPT_VERSION,
        "restored_at": timestamp.isoformat(),
        "from_run": current.run_directory.relative_to(root).as_posix(),
        "to_run": target.relative_to(root).as_posix(),
        "authorization_checksum_sha256": file_checksum(Path(authorization_path)),
        "authorization_id": authorization["authorization_id"],
        "evidence_deleted": False,
        "automated_action_allowed": False,
    }
    _write_json_atomic(path, receipt)
    return receipt


def _publication_preflight(root: Path, *, limits: RuntimeLimits) -> Mapping[str, object]:
    if not pricing_pointer_path(root).is_file():
        return {
            "status": "NOT_PROVEN",
            "reason": "NO_VERIFIED_PRICING_PUBLICATION",
        }
    try:
        publication = read_current_option_pricing_publication(root)
        reachable = authoritative_option_pricing_runs(root)
        metadata = {
            name: {
                "rows": pq.ParquetFile(publication.run_directory / name).metadata.num_rows,
                "size": (publication.run_directory / name).stat().st_size,
            }
            for name in (
                "pricing-samples.parquet",
                "pricing-predictions.parquet",
                "pricing-evaluations.parquet",
                "pricing-surfaces.parquet",
            )
        }
        manifest_config = publication.manifest.get("configuration")
        manifest_config = manifest_config if isinstance(manifest_config, Mapping) else {}
        policy_reference = manifest_config.get("eligibility_policy")
        policy_reference = (
            policy_reference if isinstance(policy_reference, Mapping) else {}
        )
        policy = read_eligibility_policy(
            root / str(policy_reference.get("path", "")),
            datastore_root=root,
        )
        lineage = verify_completed_option_pricing_lineage(
            root,
            run_directory=publication.run_directory,
            policy_artifact=policy,
        )
        benchmark = manifest_config.get("runtime_benchmark")
        benchmark = benchmark if isinstance(benchmark, Mapping) else {}
        cycle = float(benchmark.get("elapsed_seconds", float("inf")))
        peak = int(benchmark.get("peak_memory_bytes", limits.maximum_peak_memory_bytes + 1))
        passed = (
            cycle <= limits.maximum_cycle_seconds
            and peak <= limits.maximum_peak_memory_bytes
            and lineage.get("verified") is True
        )
        return {
            "status": "PASS" if passed else "FAIL",
            "current_run": str(publication.run_directory),
            "reachable_run_count": len(reachable),
            "parquet_metadata": metadata,
            "runtime_benchmark": benchmark,
            "eligibility_policy": {
                "policy_hash": policy.policy_hash,
                "path": policy.directory.relative_to(root).as_posix(),
                "receipt_checksum_sha256": file_checksum(
                    policy.directory / "receipt.json"
                ),
            },
            "lineage": lineage,
        }
    except Exception as exc:
        return {
            "status": "FAIL",
            "reason": f"{type(exc).__name__}: {exc}",
        }


def _subprocess_check(
    command: Sequence[str], *, timeout_seconds: int
) -> Mapping[str, object]:
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        return {
            "status": "PASS" if completed.returncode == 0 else "FAIL",
            "command": list(command),
            "return_code": completed.returncode,
            "stdout_tail": completed.stdout[-2_000:],
            "stderr_tail": completed.stderr[-2_000:],
            "timeout_seconds": timeout_seconds,
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "FAIL",
            "command": list(command),
            "reason": "TIMEOUT",
            "timeout_seconds": timeout_seconds,
        }


def _parse_exact_lock(path: Path) -> dict[str, str]:
    locked: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        token = line.strip()
        if not token or token.startswith("#") or "==" not in token:
            continue
        name, version = token.split("==", 1)
        locked[canonicalize_name(name.strip())] = version.strip()
    return locked


def _declared_dependency_contract(path: Path) -> dict[str, Requirement]:
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    project = document.get("project", {})
    requirements = list(project.get("dependencies", ()))
    optional = project.get("optional-dependencies", {})
    for group in _VERIFIED_OPTIONAL_DEPENDENCY_GROUPS:
        requirements.extend(optional.get(group, ()))
    requirements.extend(document.get("build-system", {}).get("requires", ()))
    declared: dict[str, Requirement] = {}
    for raw in requirements:
        requirement = Requirement(str(raw))
        declared[canonicalize_name(requirement.name)] = requirement
    return declared


def _read_rollback_authorization(
    path: Path, *, current_run: str, target_run: str
) -> Mapping[str, object]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OperationalError("Rollback authorization is unreadable") from exc
    if not isinstance(payload, Mapping):
        raise OperationalError("Rollback authorization is malformed")
    if (
        payload.get("schema_version") != ROLLBACK_AUTHORIZATION_VERSION
        or payload.get("action") != "RESTORE_PREVIOUS_VERIFIED_OPTION_PRICING_POINTER"
        or payload.get("current_run") != current_run
        or payload.get("target_run") != target_run
        or not str(payload.get("authorization_id", "")).strip()
        or not str(payload.get("operator_id", "")).strip()
        or pd.isna(
            pd.to_datetime(payload.get("approved_at"), utc=True, errors="coerce")
        )
    ):
        raise OperationalError("Rollback authorization failed validation")
    return payload


def _alert(kind: str, severity: str, details: object) -> dict[str, object]:
    return {"kind": kind, "severity": severity, "details": details}


def _prospective_count(report: Mapping[str, object]) -> int:
    routes = report.get("routes")
    if not isinstance(routes, Mapping):
        return 0
    return sum(
        int(prospective.get("completed_predictions", 0))
        for route in routes.values()
        if isinstance(route, Mapping)
        for prospective in (route.get("prospective"),)
        if isinstance(prospective, Mapping)
    )


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
    "EXIT_CAPACITY",
    "EXIT_CONFIGURATION",
    "EXIT_DEPENDENCY",
    "EXIT_EVIDENCE",
    "EXIT_OK",
    "EXIT_PUBLICATION",
    "EXIT_RUNTIME_FAILURE",
    "OperationalError",
    "RuntimeLimits",
    "build_runtime_health",
    "capacity_report",
    "configuration_report",
    "dependency_contract_report",
    "enforce_runtime_limits",
    "operational_preflight_report",
    "publish_operational_readiness",
    "publish_runtime_health",
    "read_current_operational_readiness",
    "read_current_runtime_health",
    "rollback_option_pricing_pointer",
]
