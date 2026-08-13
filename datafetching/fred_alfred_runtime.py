from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

from datafetching.cme_runtime import load_repository_environment
from datafetching.fred_alfred_readiness import (
    FredAlfredReadiness,
    FredAlfredReadinessError,
    derive_fred_alfred_incremental_plan,
    verify_and_publish_fred_alfred_readiness,
)
from datafetching.fred_vintage_import import (
    FRED_ALFRED_SUPPORTED_SERIES,
    FredAlfredClient,
    FredVintageImportError,
    import_fred_alfred_vintages,
)
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.artifacts import file_checksum, utc_timestamp


FRED_ALFRED_RUNTIME_VERSION = "fred-alfred-daily-runtime-v1"
FRED_ALFRED_RUNTIME_POINTER_VERSION = "fred-alfred-daily-pointer-v1"
FRED_ALFRED_RUNTIME_LOCK = ".ducketz-fred-alfred-import.lock"


@dataclass(frozen=True)
class FredAlfredDailyResult:
    status: str
    run_date: str
    receipt_path: Path
    readiness: FredAlfredReadiness | None


def run_fred_alfred_incremental_once(
    datastore_root: Path,
    *,
    client: FredAlfredClient,
    as_of: object | None = None,
) -> FredAlfredDailyResult:
    """Run at most one successful complete incremental import per UTC date."""

    root = Path(datastore_root).resolve()
    observed = utc_timestamp(as_of)
    run_date = observed.date().isoformat()
    prior = _read_runtime_pointer(root)
    if prior is not None:
        prior_date, prior_receipt = prior
        if prior_date == run_date:
            return FredAlfredDailyResult(
                status="ALREADY_COMPLETE_TODAY",
                run_date=run_date,
                receipt_path=prior_receipt,
                readiness=None,
            )

    plan = derive_fred_alfred_incremental_plan(root, as_of=observed)
    imported = import_fred_alfred_vintages(
        root,
        client=client,
        series_ids=FRED_ALFRED_SUPPORTED_SERIES,
        acquired_at=observed,
        **plan.as_request(),
    )
    readiness = verify_and_publish_fred_alfred_readiness(
        root,
        import_result=imported,
        verified_at=observed,
    )
    receipt = {
        "schema_version": FRED_ALFRED_RUNTIME_VERSION,
        "status": "COMPLETE",
        "run_date_utc": run_date,
        "completed_at": observed.isoformat(),
        "series": list(FRED_ALFRED_SUPPORTED_SERIES),
        "request_mode": plan.mode,
        "request": plan.as_request(),
        "import_run_path": imported.evidence_directory.relative_to(root).as_posix(),
        "import_receipt_checksum_sha256": file_checksum(
            imported.evidence_directory / "receipt.json"
        ),
        "readiness_run_path": readiness.directory.relative_to(root).as_posix(),
        "readiness_receipt_checksum_sha256": readiness.receipt_checksum_sha256,
        "loop_b_consumption_authorized": True,
        "current_revised_history_used": False,
        "automated_action_allowed": False,
    }
    client.assert_secret_free(receipt)
    receipt_path = _publish_runtime_receipt(root, observed, receipt)
    _publish_runtime_pointer(root, run_date=run_date, receipt_path=receipt_path)
    return FredAlfredDailyResult(
        status="COMPLETE",
        run_date=run_date,
        receipt_path=receipt_path,
        readiness=readiness,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Own the once-daily bounded ALFRED update and verified readiness "
            "publication. Run the one-time backfill command first."
        )
    )
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path, default=None)
    datastore.add_argument(
        "--datastore-target",
        choices=tuple(DATASTORE_TARGETS),
        default="pc",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--utc-hour",
        type=int,
        default=7,
        help="UTC hour for subsequent daily runs (default: 07).",
    )
    args = parser.parse_args(argv)
    if not 0 <= args.utc_hour <= 23:
        parser.error("--utc-hour must satisfy 0 <= hour <= 23")
    root = resolve_datastore_dir(
        root_dir=args.datastore,
        target=None if args.datastore is not None else args.datastore_target,
    )
    load_repository_environment()
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        parser.error(
            "FRED_API_KEY is required in the process environment or repository .env"
        )
    try:
        client = FredAlfredClient(api_key)
    except FredVintageImportError as exc:
        parser.error(str(exc))

    lock_path = root / FRED_ALFRED_RUNTIME_LOCK
    try:
        with exclusive_runtime_lock(
            lock_path,
            process_name="Duckets daily FRED/ALFRED owner",
        ):
            while True:
                try:
                    result = run_fred_alfred_incremental_once(
                        root,
                        client=client,
                    )
                except (FredAlfredReadinessError, FredVintageImportError, ValueError) as exc:
                    print(f"ALFRED daily update failed: {type(exc).__name__}: {exc}")
                    if args.once:
                        return 1
                else:
                    print(
                        json.dumps(
                            {
                                "status": result.status,
                                "run_date_utc": result.run_date,
                                "receipt_path": str(result.receipt_path),
                            },
                            indent=2,
                            sort_keys=True,
                        )
                    )
                    if args.once:
                        return 0
                next_run = _next_daily_boundary(
                    datetime.now(timezone.utc),
                    utc_hour=args.utc_hour,
                )
                print(f"Next ALFRED daily update: {next_run.isoformat()}")
                time.sleep(
                    max(
                        0.0,
                        (next_run - datetime.now(timezone.utc)).total_seconds(),
                    )
                )
    except RuntimeError as exc:
        parser.error(str(exc))
    except KeyboardInterrupt:
        print("ALFRED daily owner stopped.")
        return 0


def _read_runtime_pointer(root: Path) -> tuple[str, Path] | None:
    pointer_path = _runtime_pointer_path(root)
    if not pointer_path.is_file():
        return None
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        relative = Path(str(pointer["receipt_path"]))
        run_date = str(pointer["run_date_utc"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise FredAlfredReadinessError("ALFRED daily pointer is malformed") from None
    receipt_path = (root / relative).resolve()
    authority = (root / "ml" / "fred-alfred-runtime").resolve()
    if (
        pointer.get("schema_version") != FRED_ALFRED_RUNTIME_POINTER_VERSION
        or relative.is_absolute()
        or authority not in receipt_path.parents
        or not receipt_path.is_file()
        or pointer.get("receipt_checksum_sha256") != file_checksum(receipt_path)
    ):
        raise FredAlfredReadinessError("ALFRED daily pointer verification failed")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        raise FredAlfredReadinessError("ALFRED daily receipt is unreadable") from None
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("schema_version") != FRED_ALFRED_RUNTIME_VERSION
        or receipt.get("status") != "COMPLETE"
        or receipt.get("run_date_utc") != run_date
        or receipt.get("loop_b_consumption_authorized") is not True
        or receipt.get("current_revised_history_used") is not False
        or receipt.get("automated_action_allowed") is not False
    ):
        raise FredAlfredReadinessError("ALFRED daily receipt verification failed")
    return run_date, receipt_path


def _publish_runtime_receipt(
    root: Path,
    observed: pd.Timestamp,
    receipt: Mapping[str, object],
) -> Path:
    parent = root / "ml" / "fred-alfred-runtime"
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / observed.strftime("%Y%m%dT%H%M%S.%fZ")
    suffix = 2
    while destination.exists():
        destination = parent / f"{observed.strftime('%Y%m%dT%H%M%S.%fZ')}-{suffix}"
        suffix += 1
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.tmp-{os.getpid()}-",
            dir=parent,
        )
    )
    try:
        receipt_path = staging / "receipt.json"
        _write_json(receipt_path, receipt)
        staging.replace(destination)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return destination / "receipt.json"


def _publish_runtime_pointer(
    root: Path,
    *,
    run_date: str,
    receipt_path: Path,
) -> None:
    pointer_path = _runtime_pointer_path(root)
    _write_json_atomic(
        pointer_path,
        {
            "schema_version": FRED_ALFRED_RUNTIME_POINTER_VERSION,
            "run_date_utc": run_date,
            "receipt_path": receipt_path.relative_to(root).as_posix(),
            "receipt_checksum_sha256": file_checksum(receipt_path),
        },
    )


def _runtime_pointer_path(root: Path) -> Path:
    return root / "ml" / "fred-alfred-runtime-latest" / "run.json"


def _next_daily_boundary(now: datetime, *, utc_hour: int) -> datetime:
    current = now.astimezone(timezone.utc)
    candidate = current.replace(
        hour=utc_hour,
        minute=0,
        second=0,
        microsecond=0,
    )
    if candidate <= current:
        candidate += timedelta(days=1)
    return candidate


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    try:
        _write_json(temporary, payload)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FRED_ALFRED_RUNTIME_LOCK",
    "FRED_ALFRED_RUNTIME_VERSION",
    "FredAlfredDailyResult",
    "run_fred_alfred_incremental_once",
]
