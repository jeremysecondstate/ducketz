from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

from datafetching.fred_vintage_import import (
    FRED_ALFRED_RECEIPT_NAME,
    FRED_ALFRED_SUPPORTED_SERIES,
    FredVintageImportResult,
    read_fred_alfred_vintage_import,
)
from datafetching.fred_vintages import (
    ALFRED_RELEASE_CONTEXT_NAME,
    ALFRED_VINTAGE_AVAILABILITY_BASIS,
    FRED_VINTAGE_NATURAL_KEY,
    FRED_VINTAGE_SCHEMA_VERSION,
    MACRO_CALCULATION,
    MACRO_CALCULATION_VERSION,
    MACRO_FEATURE_COLUMNS,
    MACRO_SCHEMA_VERSION,
    normalize_fred_vintage_rows,
    read_persisted_fred_vintages,
)
from ml.artifacts import file_checksum, input_inventory, utc_timestamp
from ml.current_publication import resolve_current_output
from ml.datasets.families import MACRO_LINEAGE, MACRO_VALUES, load_macro_features


FRED_ALFRED_READINESS_VERSION = "fred-alfred-readiness-v1"
FRED_ALFRED_READINESS_RECEIPT_VERSION = "fred-alfred-readiness-receipt-v1"
FRED_ALFRED_READINESS_POINTER_VERSION = "fred-alfred-readiness-pointer-v1"
FRED_ALFRED_MINIMUM_COVERAGE = 0.95
FRED_ALFRED_INCREMENTAL_OVERLAP_DAYS = 7
FRED_ALFRED_INCREMENTAL_MAX_REALTIME_DAYS = 130
FRED_ALFRED_MODEL_HORIZONS = (
    "1d",
    "1w",
    "1w-d1",
    "1w-d2",
    "1w-d3",
    "1w-d4",
    "1w-d5",
)


class FredAlfredReadinessError(RuntimeError):
    """Verified ALFRED evidence or its causal coverage failed closed."""


@dataclass(frozen=True)
class FredAlfredRequestPlan:
    mode: str
    realtime_start: date
    realtime_end: date
    observation_start: date
    observation_end: date
    earliest_eligible_decision: pd.Timestamp
    decision_source: Path

    def as_request(self) -> dict[str, str]:
        return {
            "realtime_start": self.realtime_start.isoformat(),
            "realtime_end": self.realtime_end.isoformat(),
            "observation_start": self.observation_start.isoformat(),
            "observation_end": self.observation_end.isoformat(),
        }


@dataclass(frozen=True)
class FredAlfredReadiness:
    directory: Path
    report_path: Path
    receipt_path: Path
    verified_at: pd.Timestamp
    coverage: Mapping[str, object]

    @property
    def receipt_checksum_sha256(self) -> str:
        return file_checksum(self.receipt_path)


@dataclass(frozen=True)
class VerifiedMacroEvidence:
    release_context: pd.DataFrame
    vintages: pd.DataFrame
    source_files: tuple[Path, ...]
    readiness: FredAlfredReadiness


def derive_fred_alfred_backfill_plan(
    datastore_root: Path,
    *,
    as_of: object | None = None,
) -> FredAlfredRequestPlan:
    """Derive provider bounds from the earliest eligible daily/weekly decision."""

    root = Path(datastore_root).resolve()
    observed = utc_timestamp(as_of)
    decision_source, decisions = _eligible_decisions(root)
    earliest = pd.Timestamp(decisions["decision_timestamp"].min())

    # Each observation bound covers the configured transform lag plus the
    # feature's own freshness allowance. The GDP bound is floored to the
    # containing quarter so a first-quarter observation and its four-quarter
    # lag are both included without an operator-supplied guess.
    candidates = {
        "FEDFUNDS": earliest - pd.Timedelta(days=45),
        "CPIAUCSL": earliest - pd.DateOffset(months=12) - pd.Timedelta(days=45),
        "UNRATE": earliest - pd.DateOffset(months=1) - pd.Timedelta(days=56),
        "GDP": earliest - pd.DateOffset(months=12) - pd.Timedelta(days=120),
    }
    observation_start = min(
        _series_period_start(series, pd.Timestamp(value))
        for series, value in candidates.items()
    )
    return FredAlfredRequestPlan(
        mode="BACKFILL",
        realtime_start=observation_start,
        realtime_end=observed.date(),
        observation_start=observation_start,
        observation_end=observed.date(),
        earliest_eligible_decision=earliest,
        decision_source=decision_source,
    )


def derive_fred_alfred_incremental_plan(
    datastore_root: Path,
    *,
    as_of: object | None = None,
) -> FredAlfredRequestPlan:
    """Create a bounded overlap update while retaining the full required lag span."""

    root = Path(datastore_root).resolve()
    observed = utc_timestamp(as_of)
    base = derive_fred_alfred_backfill_plan(root, as_of=observed)
    vintages, _ = read_persisted_fred_vintages(
        root,
        series_ids=FRED_ALFRED_SUPPORTED_SERIES,
    )
    present = set(vintages["series_name"].astype(str))
    if not set(FRED_ALFRED_SUPPORTED_SERIES).issubset(present):
        missing = sorted(set(FRED_ALFRED_SUPPORTED_SERIES).difference(present))
        raise FredAlfredReadinessError(
            "Incremental ALFRED ingestion requires the one-time backfill first; "
            "missing: " + ", ".join(missing)
        )
    latest_by_series = (
        vintages.groupby("series_name", observed=True)["realtime_start"]
        .max()
        .reindex(FRED_ALFRED_SUPPORTED_SERIES)
    )
    overlap_start = pd.Timestamp(latest_by_series.min()) - pd.Timedelta(
        days=FRED_ALFRED_INCREMENTAL_OVERLAP_DAYS
    )
    lower_bound = observed - pd.Timedelta(
        days=FRED_ALFRED_INCREMENTAL_MAX_REALTIME_DAYS
    )
    realtime_start = max(overlap_start, lower_bound).date()
    coverage_end = _minimum_prior_coverage_end(vintages)
    if realtime_start > coverage_end + pd.Timedelta(days=1):
        raise FredAlfredReadinessError(
            "Bounded ALFRED incremental history would contain an uncovered "
            "real-time gap; rerun the one-time backfill"
        )
    return FredAlfredRequestPlan(
        mode="INCREMENTAL",
        realtime_start=realtime_start,
        realtime_end=observed.date(),
        observation_start=base.observation_start,
        observation_end=observed.date(),
        earliest_eligible_decision=base.earliest_eligible_decision,
        decision_source=base.decision_source,
    )


def verify_and_publish_fred_alfred_readiness(
    datastore_root: Path,
    *,
    import_result: FredVintageImportResult,
    verified_at: object | None = None,
    minimum_coverage: float = FRED_ALFRED_MINIMUM_COVERAGE,
) -> FredAlfredReadiness:
    """Verify coverage/lineage/lookahead and publish a distinct authorization."""

    if not 0.0 < float(minimum_coverage) <= 1.0:
        raise ValueError("minimum_coverage must satisfy 0 < value <= 1")
    root = Path(datastore_root).resolve()
    observed = utc_timestamp(verified_at)
    verified_import = read_fred_alfred_vintage_import(
        import_result.evidence_directory,
        datastore_root=root,
    )
    import_receipt = import_result.evidence_directory / FRED_ALFRED_RECEIPT_NAME
    if not import_receipt.is_file():
        raise FredAlfredReadinessError("Sealed ALFRED importer receipt is missing")

    vintages, vintage_paths = read_persisted_fred_vintages(
        root,
        series_ids=FRED_ALFRED_SUPPORTED_SERIES,
    )
    _validate_vintage_integrity(vintages)
    release_context, release_paths = _read_release_context(root)
    decision_source, decisions = _eligible_decisions(root)
    coverage = _coverage_report(
        decisions,
        release_context=release_context,
        vintages=vintages,
        minimum_coverage=float(minimum_coverage),
    )
    if coverage["status"] != "PASS":
        raise FredAlfredReadinessError(
            "ALFRED readiness verification failed: "
            + json.dumps(coverage, sort_keys=True, default=str)
        )

    source_paths = (
        *vintage_paths,
        *release_paths,
        decision_source,
        import_result.evidence_directory / "manifest.json",
        import_receipt,
    )
    source_inventory = _relative_inventory(root, source_paths)
    report = {
        "schema_version": FRED_ALFRED_READINESS_VERSION,
        "verified_at": observed.isoformat(),
        "status": "PASS",
        "authorization": "LOOP_B_MACRO_CONSUMPTION",
        "loop_b_consumption_authorized": True,
        "automated_action_allowed": False,
        "minimum_coverage": float(minimum_coverage),
        "availability_basis": ALFRED_VINTAGE_AVAILABILITY_BASIS,
        "vintage_schema_version": FRED_VINTAGE_SCHEMA_VERSION,
        "release_context_schema_version": MACRO_SCHEMA_VERSION,
        "release_context_calculation_version": MACRO_CALCULATION_VERSION,
        "current_revised_history_used": False,
        "waiting_period_required": False,
        "operator_approval_required": False,
        "import_run_path": import_result.evidence_directory.relative_to(root).as_posix(),
        "import_receipt_checksum_sha256": file_checksum(import_receipt),
        "import_row_count": int(verified_import["receipt"]["row_count"]),
        "coverage": coverage,
        "source_files": source_inventory,
    }
    parent = root / "ml" / "macro-readiness" / "fred-alfred"
    destination = _unused_timestamp_directory(parent, observed)
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.tmp-{os.getpid()}-",
            dir=parent,
        )
    )
    try:
        report_path = staging / "readiness.json"
        _write_json(report_path, report)
        receipt = {
            "schema_version": FRED_ALFRED_READINESS_RECEIPT_VERSION,
            "verified_at": observed.isoformat(),
            "run_path": destination.relative_to(root).as_posix(),
            "readiness_checksum_sha256": file_checksum(report_path),
            "status": "PASS",
            "loop_b_consumption_authorized": True,
            "automated_action_allowed": False,
            "availability_basis": ALFRED_VINTAGE_AVAILABILITY_BASIS,
            "current_revised_history_used": False,
            "minimum_coverage": float(minimum_coverage),
            "lookahead_violation_count": int(
                coverage["lookahead_violation_count"]
            ),
            "import_run_path": report["import_run_path"],
            "import_receipt_checksum_sha256": report[
                "import_receipt_checksum_sha256"
            ],
        }
        _write_json(staging / "receipt.json", receipt)
        staging.replace(destination)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    readiness = _read_readiness_directory(root, destination)
    _publish_pointer(root, readiness)
    return readiness


def read_verified_macro_evidence(
    datastore_root: Path,
    *,
    available_not_after: object | None = None,
) -> VerifiedMacroEvidence:
    """Read only checksum-bound, coverage-authorized ALFRED macro evidence."""

    root = Path(datastore_root).resolve()
    pointer_path = fred_alfred_readiness_pointer_path(root)
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        current = pointer["current"]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise FredAlfredReadinessError(
            f"Verified ALFRED readiness pointer is unavailable: {pointer_path}"
        ) from None
    if (
        not isinstance(pointer, Mapping)
        or pointer.get("schema_version") != FRED_ALFRED_READINESS_POINTER_VERSION
        or not isinstance(current, Mapping)
    ):
        raise FredAlfredReadinessError("Verified ALFRED readiness pointer is malformed")
    relative = Path(str(current.get("run_path", "")))
    if relative.is_absolute():
        raise FredAlfredReadinessError("ALFRED readiness run path must be relative")
    directory = (root / relative).resolve()
    allowed = (root / "ml" / "macro-readiness" / "fred-alfred").resolve()
    if directory.parent != allowed:
        raise FredAlfredReadinessError("ALFRED readiness path escapes its authority")
    readiness = _read_readiness_directory(root, directory)
    if (
        current.get("receipt_checksum_sha256")
        != readiness.receipt_checksum_sha256
        or _required_utc(current.get("verified_at"), label="pointer verified_at")
        != readiness.verified_at
    ):
        raise FredAlfredReadinessError("ALFRED readiness pointer does not match receipt")
    if available_not_after is not None and readiness.verified_at > utc_timestamp(
        available_not_after
    ):
        raise FredAlfredReadinessError(
            "Verified ALFRED readiness was published after the causal input cutoff"
        )

    report = json.loads(readiness.report_path.read_text(encoding="utf-8"))
    source_files = _verify_inventory(root, report.get("source_files"))
    import_receipt = root / str(report["import_run_path"]) / FRED_ALFRED_RECEIPT_NAME
    if (
        not import_receipt.is_file()
        or file_checksum(import_receipt)
        != report.get("import_receipt_checksum_sha256")
    ):
        raise FredAlfredReadinessError("ALFRED importer receipt lineage changed")
    vintage_paths = tuple(
        path
        for path in source_files
        if "macro-vintages" in path.parts and path.suffix == ".parquet"
    )
    release_paths = tuple(
        path
        for path in source_files
        if "alfred-release-context" in path.parts and path.suffix == ".parquet"
    )
    if not vintage_paths or not release_paths:
        raise FredAlfredReadinessError("ALFRED readiness inventory is incomplete")
    vintages = normalize_fred_vintage_rows(
        pd.concat(
            [
                pd.read_parquet(path).drop(columns=["id"], errors="ignore")
                for path in vintage_paths
            ],
            ignore_index=True,
            sort=False,
        )
    )
    _validate_vintage_integrity(vintages)
    release_context = _normalize_release_context(
        pd.concat(
            [
                pd.read_parquet(path).drop(columns=["id"], errors="ignore")
                for path in release_paths
            ],
            ignore_index=True,
            sort=False,
        )
    )
    return VerifiedMacroEvidence(
        release_context=release_context,
        vintages=vintages,
        source_files=tuple(
            dict.fromkeys(
                (
                    *source_files,
                    readiness.report_path,
                    readiness.receipt_path,
                    import_receipt,
                )
            )
        ),
        readiness=readiness,
    )


def fred_alfred_readiness_pointer_path(datastore_root: Path) -> Path:
    return Path(datastore_root) / "ml" / "macro-readiness-latest" / "run.json"


def _eligible_decisions(root: Path) -> tuple[Path, pd.DataFrame]:
    source = resolve_current_output(root, "samples.parquet")
    frame = pd.read_parquet(
        source,
        columns=["symbol", "horizon", "decision_timestamp"],
    )
    decisions = frame.loc[
        frame["horizon"].astype(str).isin(FRED_ALFRED_MODEL_HORIZONS)
    ].copy()
    decisions["decision_timestamp"] = pd.to_datetime(
        decisions["decision_timestamp"], utc=True, errors="coerce"
    )
    decisions = decisions.dropna(subset=["decision_timestamp"])
    decisions = decisions.drop_duplicates(
        ["symbol", "horizon", "decision_timestamp"]
    ).reset_index(drop=True)
    missing_horizons = sorted(
        set(FRED_ALFRED_MODEL_HORIZONS).difference(decisions["horizon"])
    )
    if decisions.empty or missing_horizons:
        raise FredAlfredReadinessError(
            "Authoritative Loop B samples lack eligible macro decisions: "
            + ", ".join(missing_horizons or ["all"])
        )
    return source, decisions


def _read_release_context(root: Path) -> tuple[pd.DataFrame, tuple[Path, ...]]:
    directory = (
        root
        / "pools"
        / "macro"
        / "features"
        / "alfred-release-context"
        / "fred"
    )
    paths = tuple(sorted(directory.glob("*.parquet")))
    if not paths:
        raise FredAlfredReadinessError("Derived ALFRED release context is absent")
    frame = pd.concat(
        [pd.read_parquet(path).drop(columns=["id"], errors="ignore") for path in paths],
        ignore_index=True,
        sort=False,
    )
    return _normalize_release_context(frame), paths


def _normalize_release_context(frame: pd.DataFrame) -> pd.DataFrame:
    missing = sorted(set(MACRO_FEATURE_COLUMNS).difference(frame.columns))
    if missing:
        raise FredAlfredReadinessError(
            "Derived ALFRED context is missing columns: " + ", ".join(missing)
        )
    values = frame.loc[
        frame["context_name"].astype(str).eq(ALFRED_RELEASE_CONTEXT_NAME)
        & frame["availability_basis"].astype(str).eq(
            ALFRED_VINTAGE_AVAILABILITY_BASIS
        )
        & frame["calculation"].astype(str).eq(MACRO_CALCULATION)
        & frame["calculation_version"].astype(str).eq(
            MACRO_CALCULATION_VERSION
        )
        & frame["schema_version"].astype(str).eq(MACRO_SCHEMA_VERSION)
        & frame["vintage_schema_version"].astype(str).eq(
            FRED_VINTAGE_SCHEMA_VERSION
        )
    ].copy()
    if values.empty:
        raise FredAlfredReadinessError(
            "No verified ALFRED-vintage release context is available"
        )
    for column in (
        "available_at",
        "fed_funds_available_at",
        "cpi_available_at",
        "unemployment_available_at",
        "gdp_available_at",
    ):
        values[column] = pd.to_datetime(values[column], utc=True, errors="coerce")
    if values["available_at"].isna().any():
        raise FredAlfredReadinessError("ALFRED release context has invalid availability")
    if values.duplicated(
        ["context_name", "available_at", "calculation_version"]
    ).any():
        raise FredAlfredReadinessError("ALFRED release contexts are duplicated")
    return values.reindex(columns=MACRO_FEATURE_COLUMNS).sort_values(
        "available_at", kind="stable"
    ).reset_index(drop=True)


def _validate_vintage_integrity(vintages: pd.DataFrame) -> None:
    if vintages.empty:
        raise FredAlfredReadinessError("Canonical ALFRED vintage history is empty")
    if not vintages["availability_basis"].eq(
        ALFRED_VINTAGE_AVAILABILITY_BASIS
    ).all():
        raise FredAlfredReadinessError(
            "Current-revised or local-receipt rows are not historical evidence"
        )
    present = set(vintages["series_name"].astype(str))
    missing = sorted(set(FRED_ALFRED_SUPPORTED_SERIES).difference(present))
    if missing:
        raise FredAlfredReadinessError(
            "Canonical ALFRED history is missing series: " + ", ".join(missing)
        )
    if vintages.duplicated(list(FRED_VINTAGE_NATURAL_KEY)).any():
        raise FredAlfredReadinessError("Canonical ALFRED revisions are duplicated")
    if vintages["revision_identity"].duplicated().any():
        raise FredAlfredReadinessError("Canonical ALFRED revision identities repeat")
    if vintages["available_at"].ne(vintages["release_at"]).any():
        raise FredAlfredReadinessError(
            "ALFRED historical availability must equal provider release policy"
        )
    if vintages["fetched_at"].lt(vintages["release_at"]).any():
        raise FredAlfredReadinessError(
            "ALFRED retrieval precedes its provider release clock"
        )


def _coverage_report(
    decisions: pd.DataFrame,
    *,
    release_context: pd.DataFrame,
    vintages: pd.DataFrame,
    minimum_coverage: float,
) -> dict[str, object]:
    rows: dict[str, object] = {}
    total_lookahead = 0
    all_pass = True
    for horizon in FRED_ALFRED_MODEL_HORIZONS:
        horizon_decisions = decisions.loc[
            decisions["horizon"].astype(str).eq(horizon)
        ].copy()
        joined = load_macro_features(
            horizon_decisions,
            release_context,
            value_columns=MACRO_VALUES,
            freshness=None,
            vintage_source=vintages,
        )
        feature_rows: dict[str, object] = {}
        decision_time = pd.to_datetime(
            joined["decision_timestamp"], utc=True, errors="coerce"
        )
        for feature, (lineage_column, _series, freshness) in MACRO_LINEAGE.items():
            first_derivable = pd.to_datetime(
                release_context.loc[
                    pd.to_numeric(
                        release_context[MACRO_VALUES[feature]], errors="coerce"
                    ).notna()
                    & release_context[lineage_column].notna(),
                    "available_at",
                ],
                utc=True,
                errors="coerce",
            ).min()
            if pd.isna(first_derivable):
                eligible = pd.Series(False, index=joined.index)
            else:
                eligible = decision_time.ge(first_derivable)
            populated = pd.to_numeric(joined[feature], errors="coerce").notna()
            eligible_count = int(eligible.sum())
            covered_count = int((eligible & populated).sum())
            coverage = (
                float(covered_count / eligible_count) if eligible_count else 0.0
            )
            feature_available = pd.to_datetime(
                joined[f"{feature}__available_at"], utc=True, errors="coerce"
            )
            lookahead = populated & (
                feature_available.isna() | feature_available.gt(decision_time)
            )
            stale_join = populated & decision_time.sub(feature_available).gt(freshness)
            pre_release = populated & decision_time.lt(first_derivable)
            violations = int((lookahead | stale_join | pre_release).sum())
            total_lookahead += violations
            passed = eligible_count > 0 and coverage >= minimum_coverage and violations == 0
            all_pass &= passed
            feature_rows[feature] = {
                "status": "PASS" if passed else "FAIL",
                "first_derivable_release": (
                    pd.Timestamp(first_derivable).isoformat()
                    if not pd.isna(first_derivable)
                    else None
                ),
                "eligible_decision_count": eligible_count,
                "covered_decision_count": covered_count,
                "coverage": coverage,
                "freshness_seconds": float(freshness.total_seconds()),
                "lookahead_or_freshness_violation_count": violations,
            }
        rows[horizon] = {
            "decision_count": int(len(horizon_decisions)),
            "features": feature_rows,
        }
    return {
        "status": "PASS" if all_pass and total_lookahead == 0 else "FAIL",
        "minimum_coverage": minimum_coverage,
        "lookahead_violation_count": total_lookahead,
        "horizons": rows,
    }


def _read_readiness_directory(root: Path, directory: Path) -> FredAlfredReadiness:
    report_path = directory / "readiness.json"
    receipt_path = directory / "receipt.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        raise FredAlfredReadinessError(
            f"ALFRED readiness evidence is unreadable: {directory}"
        ) from None
    if not isinstance(report, Mapping) or not isinstance(receipt, Mapping):
        raise FredAlfredReadinessError("ALFRED readiness evidence is malformed")
    expected_path = directory.relative_to(root).as_posix()
    verified_at = _required_utc(report.get("verified_at"), label="verified_at")
    if (
        report.get("schema_version") != FRED_ALFRED_READINESS_VERSION
        or receipt.get("schema_version")
        != FRED_ALFRED_READINESS_RECEIPT_VERSION
        or receipt.get("run_path") != expected_path
        or receipt.get("readiness_checksum_sha256") != file_checksum(report_path)
        or _required_utc(
            receipt.get("verified_at"), label="receipt verified_at"
        )
        != verified_at
        or report.get("status") != "PASS"
        or receipt.get("status") != "PASS"
        or report.get("loop_b_consumption_authorized") is not True
        or receipt.get("loop_b_consumption_authorized") is not True
        or report.get("automated_action_allowed") is not False
        or receipt.get("automated_action_allowed") is not False
        or report.get("current_revised_history_used") is not False
        or receipt.get("current_revised_history_used") is not False
        or report.get("availability_basis")
        != ALFRED_VINTAGE_AVAILABILITY_BASIS
        or receipt.get("availability_basis")
        != ALFRED_VINTAGE_AVAILABILITY_BASIS
        or int(receipt.get("lookahead_violation_count", -1)) != 0
    ):
        raise FredAlfredReadinessError("ALFRED readiness receipt verification failed")
    coverage = report.get("coverage")
    if not isinstance(coverage, Mapping) or coverage.get("status") != "PASS":
        raise FredAlfredReadinessError("ALFRED readiness coverage is not passing")
    return FredAlfredReadiness(
        directory=directory,
        report_path=report_path,
        receipt_path=receipt_path,
        verified_at=verified_at,
        coverage=coverage,
    )


def _publish_pointer(root: Path, readiness: FredAlfredReadiness) -> None:
    _write_json_atomic(
        fred_alfred_readiness_pointer_path(root),
        {
            "schema_version": FRED_ALFRED_READINESS_POINTER_VERSION,
            "current": {
                "run_path": readiness.directory.relative_to(root).as_posix(),
                "verified_at": readiness.verified_at.isoformat(),
                "receipt_checksum_sha256": readiness.receipt_checksum_sha256,
            },
        },
    )


def _relative_inventory(root: Path, paths: Sequence[Path]) -> list[dict[str, object]]:
    inventory = input_inventory(paths, relative_to=root)
    output: list[dict[str, object]] = []
    for record in inventory:
        item = dict(record)
        raw = Path(str(item["path"]))
        if raw.is_absolute() or item.get("status") != "present":
            raise FredAlfredReadinessError("ALFRED readiness source inventory is invalid")
        item["path"] = raw.as_posix()
        output.append(item)
    return output


def _verify_inventory(root: Path, raw_inventory: object) -> tuple[Path, ...]:
    if isinstance(raw_inventory, (str, bytes)) or not isinstance(
        raw_inventory, Sequence
    ):
        raise FredAlfredReadinessError("ALFRED readiness inventory is malformed")
    paths: list[Path] = []
    for raw_record in raw_inventory:
        if not isinstance(raw_record, Mapping):
            raise FredAlfredReadinessError("ALFRED readiness inventory row is malformed")
        relative = Path(str(raw_record.get("path", "")))
        path = (root / relative).resolve()
        if relative.is_absolute() or root not in path.parents:
            raise FredAlfredReadinessError("ALFRED readiness source escapes datastore")
        if (
            raw_record.get("status") != "present"
            or not path.is_file()
            or int(raw_record.get("size", -1)) != path.stat().st_size
            or raw_record.get("checksum_sha256") != file_checksum(path)
        ):
            raise FredAlfredReadinessError(
                f"ALFRED readiness source checksum mismatch: {path}"
            )
        paths.append(path)
    return tuple(paths)


def _series_period_start(series: str, value: pd.Timestamp) -> date:
    timestamp = pd.Timestamp(value)
    if series == "GDP":
        month = ((timestamp.month - 1) // 3) * 3 + 1
        return date(timestamp.year, month, 1)
    return date(timestamp.year, timestamp.month, 1)


def _minimum_prior_coverage_end(vintages: pd.DataFrame) -> date:
    """Return the last provider date known for every required series."""

    values = vintages.copy()
    fetched_dates = pd.to_datetime(
        values["fetched_at"], utc=True, errors="coerce"
    ).dt.date
    realtime_end_dates = values["realtime_end"].map(date.fromisoformat)
    values["_covered_through"] = [
        min(end, fetched)
        for end, fetched in zip(
            realtime_end_dates,
            fetched_dates,
            strict=True,
        )
    ]
    by_series = (
        values.groupby("series_name", observed=True)["_covered_through"]
        .max()
        .reindex(FRED_ALFRED_SUPPORTED_SERIES)
    )
    if by_series.isna().any():
        raise FredAlfredReadinessError(
            "Canonical ALFRED history lacks a complete provider coverage clock"
        )
    return min(by_series.tolist())


def _required_utc(value: object, *, label: str) -> pd.Timestamp:
    if value is None or not str(value).strip():
        raise FredAlfredReadinessError(f"ALFRED readiness lacks {label}")
    converted = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(converted):
        raise FredAlfredReadinessError(
            f"ALFRED readiness contains invalid {label}"
        )
    return pd.Timestamp(converted)


def _unused_timestamp_directory(parent: Path, timestamp: pd.Timestamp) -> Path:
    base = timestamp.strftime("%Y%m%dT%H%M%S.%fZ")
    destination = parent / base
    suffix = 2
    while destination.exists():
        destination = parent / f"{base}-{suffix}"
        suffix += 1
    return destination


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


__all__ = [
    "FRED_ALFRED_MINIMUM_COVERAGE",
    "FRED_ALFRED_MODEL_HORIZONS",
    "FredAlfredReadiness",
    "FredAlfredReadinessError",
    "FredAlfredRequestPlan",
    "VerifiedMacroEvidence",
    "derive_fred_alfred_backfill_plan",
    "derive_fred_alfred_incremental_plan",
    "fred_alfred_readiness_pointer_path",
    "read_verified_macro_evidence",
    "verify_and_publish_fred_alfred_readiness",
]
