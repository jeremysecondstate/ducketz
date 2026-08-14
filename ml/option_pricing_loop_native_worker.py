from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

from datafetching.runtime_lock import exclusive_runtime_lock
from ml.artifacts import semantic_metadata_fingerprint, utc_timestamp
from ml.option_pricing.eligibility import eligibility_policy_payload
from ml.option_pricing.opra_materialization import (
    materialize_committed_opra_history_v2,
)
from ml.option_pricing.policies import (
    LOOP_NATIVE_SYMBOLS,
    LoopNativeModelPolicy,
    PricingPartitionConfig,
)
from ml.option_pricing.rates import load_point_in_time_rate_observations
from ml.option_pricing.schwab_materialization import (
    materialize_loop_native_schwab_history,
)
from ml.option_pricing.shadow_model import (
    LoopNativeModelError,
    train_loop_native_shadow_generation,
)


LOOP_NATIVE_WORKER_STATUS_VERSION = "loop-native-opra-first-worker-status-v2"
LEGACY_LOOP_NATIVE_WORKER_STATUS_VERSION = "loop-native-bsgp-worker-status-v1"


def run_loop_native_worker_once(
    datastore_root: Path,
    *,
    trainer_cutoff: object,
    dry_run: bool = False,
    model_policy: LoopNativeModelPolicy | None = None,
) -> Mapping[str, object]:
    """Materialize and train locally after a completed fast target publication."""

    root = Path(datastore_root).resolve()
    cutoff = utc_timestamp(trainer_cutoff)
    policy = model_policy or LoopNativeModelPolicy()
    with exclusive_runtime_lock(
        loop_native_worker_lock_path(root),
        process_name="Loop-native finite-basis residual worker",
    ):
        rates, _rate_files = load_point_in_time_rate_observations(root)
        eligibility_hash = semantic_metadata_fingerprint(
            eligibility_policy_payload()
        )
        opra = materialize_committed_opra_history_v2(
            root,
            symbols=LOOP_NATIVE_SYMBOLS,
            rate_observations=rates,
            closed_lockbox_clusters=PricingPartitionConfig().lockbox_clusters,
            eligibility_policy_hash=eligibility_hash,
        )
        materialization = materialize_loop_native_schwab_history(
            root,
            symbols=LOOP_NATIVE_SYMBOLS,
            trainer_cutoff=cutoff,
            rate_observations=rates,
            offline_emulation_delay_seconds=policy.offline_emulation_delay_seconds,
            dry_run=dry_run,
            opra_samples=opra.samples,
            opra_source_files=opra.source_files,
        )
        generation = None
        model_status = "DRY_RUN_NOT_PUBLISHED" if dry_run else "MODEL_NOT_FIT"
        model_reason = ""
        if not dry_run:
            if materialization.receipt is None:
                raise LoopNativeModelError(
                    "Published materialization is missing its verified receipt"
                )
            materialization_published = utc_timestamp(
                materialization.receipt.get("published_at")
            )
            try:
                generation = train_loop_native_shadow_generation(
                    root,
                    materialization=materialization,
                    trainer_cutoff=cutoff,
                    published_at=max(
                        utc_timestamp(),
                        cutoff + pd.Timedelta(microseconds=1),
                        materialization_published + pd.Timedelta(microseconds=1),
                    ),
                    policy=policy,
                )
                model_status = "MODEL_GENERATION_PUBLISHED"
            except LoopNativeModelError as exc:
                model_status = "BASELINE_FALLBACK_NO_MODEL"
                model_reason = str(exc)
        status = {
            "schema_version": LOOP_NATIVE_WORKER_STATUS_VERSION,
            "completed_at": utc_timestamp().isoformat(),
            "trainer_cutoff": cutoff.isoformat(),
            "dry_run": dry_run,
            "materialization": (
                materialization.directory.relative_to(root).as_posix()
                if materialization.directory is not None
                else None
            ),
            "materialization_rows": int(materialization.report.get("sample_rows", 0)),
            "materialization_available_rows": int(
                materialization.report.get("available_sample_rows", 0)
            ),
            "opra_materialization_rows": len(opra.samples),
            "opra_materialization_errors": dict(opra.errors),
            "provider_precedence": ["databento-opra", "schwab"],
            "model_status": model_status,
            "model_reason": model_reason,
            "model_generation": (
                generation.directory.relative_to(root).as_posix()
                if generation is not None
                else None
            ),
            "external_provider_requests": 0,
            "paid_opra_used": not opra.samples.empty,
            "automated_action_allowed": False,
        }
        if not dry_run:
            _write_json_atomic(loop_native_worker_status_path(root), status)
        return status


def launch_loop_native_worker(
    datastore_root: Path,
    *,
    trainer_cutoff: object,
    minimum_refresh_minutes: int = 60,
) -> Mapping[str, object]:
    """Start one hidden local worker without blocking Pricing or Options."""

    root = Path(datastore_root).resolve()
    cutoff = utc_timestamp(trainer_cutoff)
    if minimum_refresh_minutes < 1:
        raise ValueError("minimum_refresh_minutes must be positive")
    lock = loop_native_worker_lock_path(root)
    if lock.is_file():
        return {
            "status": "WORKER_ALREADY_RUNNING",
            "lock_path": lock.relative_to(root).as_posix(),
            "automated_action_allowed": False,
        }
    prior = _read_optional_worker_status(root)
    if prior is not None:
        completed = pd.to_datetime(
            prior.get("completed_at"), utc=True, errors="coerce"
        )
        if pd.notna(completed) and cutoff < pd.Timestamp(completed) + pd.Timedelta(
            minutes=minimum_refresh_minutes
        ):
            return {
                "status": "REFRESH_NOT_DUE",
                "last_completed_at": pd.Timestamp(completed).isoformat(),
                "minimum_refresh_minutes": minimum_refresh_minutes,
                "automated_action_allowed": False,
            }
    log_directory = root / "ml" / "option-pricing-loop-native-worker"
    log_directory.mkdir(parents=True, exist_ok=True)
    log_path = log_directory / "worker.log"
    command = (
        sys.executable,
        "-m",
        "ml.option_pricing_loop_native_worker",
        "--datastore",
        str(root),
        "--trainer-cutoff",
        cutoff.isoformat(),
    )
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
    with log_path.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
            creationflags=flags,
            start_new_session=os.name != "nt",
        )
    return {
        "status": "WORKER_STARTED",
        "pid": process.pid,
        "trainer_cutoff": cutoff.isoformat(),
        "log_path": log_path.relative_to(root).as_posix(),
        "external_provider_requests": 0,
        "paid_opra_used": False,
        "automated_action_allowed": False,
    }


def loop_native_worker_lock_path(datastore_root: Path) -> Path:
    return Path(datastore_root) / ".ducketz-loop-native-bsgp-worker.lock"


def loop_native_worker_status_path(datastore_root: Path) -> Path:
    return (
        Path(datastore_root)
        / "ml"
        / "option-pricing-loop-native-worker"
        / "latest.json"
    )


def _read_optional_worker_status(root: Path) -> Mapping[str, object] | None:
    path = loop_native_worker_status_path(root)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema_version")
        not in {
            LEGACY_LOOP_NATIVE_WORKER_STATUS_VERSION,
            LOOP_NATIVE_WORKER_STATUS_VERSION,
        }
        or payload.get("automated_action_allowed") is not False
    ):
        return None
    return payload


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + f".tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Materialize receipt-proven OPRA-primary residual evidence with explicit "
            "Schwab fallback and optionally publish a future-only finite-basis "
            "generation. No provider request is made."
        )
    )
    parser.add_argument("--datastore", required=True, type=Path)
    parser.add_argument("--trainer-cutoff", required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    status = run_loop_native_worker_once(
        args.datastore,
        trainer_cutoff=args.trainer_cutoff,
        dry_run=args.dry_run,
    )
    print(json.dumps(status, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI boundary
    raise SystemExit(main())


__all__ = [
    "LOOP_NATIVE_WORKER_STATUS_VERSION",
    "LEGACY_LOOP_NATIVE_WORKER_STATUS_VERSION",
    "launch_loop_native_worker",
    "loop_native_worker_lock_path",
    "loop_native_worker_status_path",
    "main",
    "run_loop_native_worker_once",
]
