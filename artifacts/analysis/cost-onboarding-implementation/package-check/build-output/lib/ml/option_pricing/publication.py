from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

import pandas as pd

from ml.artifacts import file_checksum, verify_manifest
from ml.parquet_contracts import (
    LEGACY_OPTION_PRICING_SURFACE_SCHEMA,
    LEGACY_OPTION_PRICING_EVALUATION_SCHEMA,
    LEGACY_OPTION_PRICING_PREDICTION_SCHEMA,
    LEGACY_OPTION_PRICING_SAMPLE_SCHEMA,
    OPTION_PRICING_EVALUATION_SCHEMA,
    OPTION_PRICING_MONITORING_SCHEMA,
    OPTION_PRICING_PREDICTION_SCHEMA,
    OPTION_PRICING_SAMPLE_SCHEMA,
    OPTION_PRICING_SURFACE_SCHEMA,
    V2_OPTION_PRICING_SURFACE_SCHEMA,
    verify_parquet_schema,
)


OPTION_PRICING_PUBLICATION_VERSION = "option-pricing-publication-v3"
OPTION_PRICING_POINTER_VERSION = "option-pricing-pointer-v3"
LEGACY_OPTION_PRICING_PUBLICATION_VERSION = "option-pricing-publication-v1"
V2_OPTION_PRICING_PUBLICATION_VERSION = "option-pricing-publication-v2"
_LEGACY_OPTION_PRICING_POINTER_VERSIONS = {
    "option-pricing-pointer-v1",
    "option-pricing-pointer-v2",
}
OPTION_PRICING_RECEIPT_NAME = "publication.json"
OPTION_PRICING_REQUIRED_OUTPUTS = {
    "pricing-samples.parquet": OPTION_PRICING_SAMPLE_SCHEMA,
    "pricing-predictions.parquet": OPTION_PRICING_PREDICTION_SCHEMA,
    "pricing-evaluations.parquet": OPTION_PRICING_EVALUATION_SCHEMA,
    "pricing-surfaces.parquet": OPTION_PRICING_SURFACE_SCHEMA,
    "pricing-monitoring.parquet": OPTION_PRICING_MONITORING_SCHEMA,
}
_OPTION_PRICING_REQUIRED_OUTPUTS_BY_VERSION = {
    LEGACY_OPTION_PRICING_PUBLICATION_VERSION: {
        "pricing-samples.parquet": LEGACY_OPTION_PRICING_SAMPLE_SCHEMA,
        "pricing-predictions.parquet": LEGACY_OPTION_PRICING_PREDICTION_SCHEMA,
        "pricing-evaluations.parquet": LEGACY_OPTION_PRICING_EVALUATION_SCHEMA,
        "pricing-surfaces.parquet": LEGACY_OPTION_PRICING_SURFACE_SCHEMA,
        "pricing-monitoring.parquet": OPTION_PRICING_MONITORING_SCHEMA,
    },
    V2_OPTION_PRICING_PUBLICATION_VERSION: {
        "pricing-samples.parquet": LEGACY_OPTION_PRICING_SAMPLE_SCHEMA,
        "pricing-predictions.parquet": LEGACY_OPTION_PRICING_PREDICTION_SCHEMA,
        "pricing-evaluations.parquet": LEGACY_OPTION_PRICING_EVALUATION_SCHEMA,
        "pricing-surfaces.parquet": V2_OPTION_PRICING_SURFACE_SCHEMA,
        "pricing-monitoring.parquet": OPTION_PRICING_MONITORING_SCHEMA,
    },
    OPTION_PRICING_PUBLICATION_VERSION: OPTION_PRICING_REQUIRED_OUTPUTS,
}
OPTION_PRICING_REPORT_NAME = "option-pricing-model-reports.json"
OPTION_PRICING_RECOVERY_AUTHORIZATION_VERSION = (
    "option-pricing-orphan-recovery-authorization-v1"
)
OPTION_PRICING_RECOVERY_RECEIPT_VERSION = "option-pricing-orphan-recovery-v1"


class OptionPricingPublicationError(RuntimeError):
    """The immutable Pricing publication or its authority failed closed."""


@dataclass(frozen=True)
class OptionPricingPublication:
    run_directory: Path
    manifest: Mapping[str, object]
    receipt: Mapping[str, object]
    pointer: Mapping[str, object]


def pricing_pointer_path(datastore_root: Path) -> Path:
    return Path(datastore_root) / "ml" / "option-pricing-latest" / "run.json"


def publish_option_pricing_run(
    datastore_root: Path,
    *,
    run_directory: Path,
    published_at: object | None = None,
    clock: Callable[[], object] | None = None,
) -> OptionPricingPublication:
    root = Path(datastore_root).resolve()
    run = _validate_run_location(root, run_directory)
    manifest = _verify_run(run)
    run_timestamp = _utc(manifest.get("run_timestamp"), "manifest run_timestamp")
    pointer_path = pricing_pointer_path(root)
    previous: Mapping[str, object] | None = None
    if pointer_path.is_file():
        current = read_current_option_pricing_publication(root)
        previous = current.pointer["current"]
        if current.run_directory == run:
            return current
        current_run_timestamp = _utc(
            current.receipt.get("run_timestamp"), "current run_timestamp"
        )
        current_published = _utc(
            current.receipt.get("published_at"), "current published_at"
        )
        if run_timestamp <= current_run_timestamp:
            raise OptionPricingPublicationError(
                "Ordinary Pricing publication cannot move the authority to an older run"
            )
        verified_orphans = _verified_newer_orphans(root, current=current)
        if verified_orphans:
            raise OptionPricingPublicationError(
                "A newer verified orphan Pricing publication exists; diagnose and "
                "recover it explicitly before publishing another generation"
            )
    else:
        current_published = None

    receipt_path = run / OPTION_PRICING_RECEIPT_NAME
    if receipt_path.is_file():
        raise OptionPricingPublicationError(
            "Ordinary publication cannot silently adopt an orphan Pricing receipt; "
            "use separately authorized recovery tooling"
        )
    else:
        sampled = (
            published_at
            if published_at is not None
            else (clock or (lambda: pd.Timestamp.now(tz="UTC")))()
        )
        published = _utc(sampled, "published_at")
        existing = None
    if pd.isna(published) or published < run_timestamp:
        raise OptionPricingPublicationError("Pricing publication predates its run")
    if current_published is not None and published <= current_published:
        raise OptionPricingPublicationError(
            "Ordinary Pricing publication must advance publication availability"
        )

    desired_base = {
        "schema_version": OPTION_PRICING_PUBLICATION_VERSION,
        "run_path": run.relative_to(root).as_posix(),
        "run_timestamp": run_timestamp.isoformat(),
        "published_at": published.isoformat(),
        "manifest_checksum_sha256": file_checksum(run / "manifest.json"),
        "previous_publication": dict(previous) if previous is not None else None,
    }
    _write_json_atomic(receipt_path, desired_base)
    receipt = desired_base

    record = _publication_record(root, run, receipt)
    pointer = {
        "schema_version": OPTION_PRICING_POINTER_VERSION,
        "current": record,
    }
    _write_json_atomic(pointer_path, pointer)
    return read_current_option_pricing_publication(root)


def diagnose_option_pricing_publications(datastore_root: Path) -> Mapping[str, object]:
    """Read and verify pointer divergence without changing any authority."""

    root = Path(datastore_root).resolve()
    pointer = pricing_pointer_path(root)
    current: OptionPricingPublication | None = None
    current_error: str | None = None
    reachable: set[Path] = set()
    if pointer.is_file():
        try:
            current = read_current_option_pricing_publication(root)
            reachable = set(authoritative_option_pricing_runs(root, current=current))
        except Exception as exc:
            current_error = f"{type(exc).__name__}: {exc}"
    records: list[dict[str, object]] = []
    runs_root = root / "ml" / "option-pricing-runs"
    for receipt_path in sorted(runs_root.glob(f"*/{OPTION_PRICING_RECEIPT_NAME}")):
        run = receipt_path.parent.resolve()
        record: dict[str, object] = {
            "run_path": run.relative_to(root).as_posix(),
            "reachable": run in reachable,
            "verified": False,
            "orphan": run not in reachable,
        }
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if not isinstance(receipt, Mapping):
                raise OptionPricingPublicationError("receipt is not an object")
            immutable_record = _publication_record(root, run, receipt)
            _verify_record(root, immutable_record)
            record.update(
                {
                    "verified": True,
                    "published_at": immutable_record["published_at"],
                    "run_timestamp": immutable_record["run_timestamp"],
                    "attaches_to_current": bool(
                        current is not None
                        and receipt.get("previous_publication")
                        == current.pointer.get("current")
                    ),
                }
            )
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
        records.append(record)
    verified_orphans = [
        record for record in records if record["orphan"] and record["verified"]
    ]
    newer_orphans = []
    if current is not None:
        current_published = _utc(current.receipt.get("published_at"), "published_at")
        newer_orphans = [
            record
            for record in verified_orphans
            if _utc(record.get("published_at"), "orphan published_at")
            > current_published
        ]
    return {
        "schema_version": "option-pricing-publication-diagnosis-v1",
        "pointer_path": pointer.relative_to(root).as_posix(),
        "pointer_status": (
            "INVALID" if current_error else "VERIFIED" if current is not None else "MISSING"
        ),
        "pointer_error": current_error,
        "current_run_path": (
            current.run_directory.relative_to(root).as_posix()
            if current is not None
            else None
        ),
        "reachable_publication_count": len(reachable),
        "verified_orphan_count": len(verified_orphans),
        "newer_verified_orphan_count": len(newer_orphans),
        "newer_verified_orphans": newer_orphans,
        "runs": records,
        "mutation_performed": False,
        "automated_action_allowed": False,
    }


def recover_option_pricing_orphan(
    datastore_root: Path,
    *,
    run_directory: Path,
    authorization_record: Path,
    recovered_at: object | None = None,
) -> OptionPricingPublication:
    """Promote one verified child orphan using a separately recorded approval."""

    root = Path(datastore_root).resolve()
    current = read_current_option_pricing_publication(root)
    run = _validate_run_location(root, run_directory)
    if run == current.run_directory:
        return current
    receipt_path = run / OPTION_PRICING_RECEIPT_NAME
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise OptionPricingPublicationError(
            f"Orphan Pricing receipt is unreadable: {receipt_path}"
        ) from exc
    if not isinstance(receipt, Mapping):
        raise OptionPricingPublicationError("Orphan Pricing receipt is malformed")
    record = _publication_record(root, run, receipt)
    _verify_record(root, record)
    _verify_chain(root, receipt.get("previous_publication"), newer=receipt)
    if receipt.get("previous_publication") != current.pointer.get("current"):
        raise OptionPricingPublicationError(
            "Recovery only permits a verified orphan that directly extends current"
        )
    if _utc(receipt.get("published_at"), "orphan published_at") <= _utc(
        current.receipt.get("published_at"), "current published_at"
    ):
        raise OptionPricingPublicationError("Recovery cannot roll the pointer backward")
    authorization_path = Path(authorization_record).resolve()
    authorization = _read_recovery_authorization(authorization_path)
    expected_run = run.relative_to(root).as_posix()
    expected_current = current.run_directory.relative_to(root).as_posix()
    if (
        authorization.get("action") != "PROMOTE_VERIFIED_OPTION_PRICING_ORPHAN"
        or authorization.get("approved") is not True
        or authorization.get("run_path") != expected_run
        or authorization.get("current_run_path") != expected_current
        or authorization.get("orphan_receipt_checksum_sha256")
        != file_checksum(receipt_path)
        or authorization.get("automated_action_allowed") is not False
        or not str(authorization.get("approved_by", "")).strip()
    ):
        raise OptionPricingPublicationError(
            "Recovery authorization does not match the exact pointer transition"
        )
    authorized_at = _utc(authorization.get("authorized_at"), "authorized_at")
    recovered = _utc(
        recovered_at if recovered_at is not None else pd.Timestamp.now(tz="UTC"),
        "recovered_at",
    )
    if recovered < authorized_at:
        raise OptionPricingPublicationError("Recovery predates its authorization")
    recovery_root = root / "ml" / "option-pricing-recoveries"
    recovery_directory = recovery_root / recovered.strftime("%Y%m%dT%H%M%S.%fZ")
    if recovery_directory.exists():
        raise OptionPricingPublicationError("Recovery receipt identity already exists")
    recovery_directory.mkdir(parents=True)
    authorization_copy = recovery_directory / "authorization.json"
    authorization_copy.write_text(
        json.dumps(dict(authorization), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    recovery_receipt = {
        "schema_version": OPTION_PRICING_RECOVERY_RECEIPT_VERSION,
        "recovered_at": recovered.isoformat(),
        "from_run_path": expected_current,
        "to_run_path": expected_run,
        "authorization_checksum_sha256": file_checksum(authorization_copy),
        "orphan_receipt_checksum_sha256": file_checksum(receipt_path),
        "automated_action_allowed": False,
    }
    _write_json_atomic(recovery_directory / "recovery.json", recovery_receipt)
    _write_json_atomic(
        pricing_pointer_path(root),
        {"schema_version": OPTION_PRICING_POINTER_VERSION, "current": record},
    )
    publication = read_current_option_pricing_publication(root)
    if publication.run_directory != run:
        raise OptionPricingPublicationError("Recovered pointer failed verification")
    return publication


def _verified_newer_orphans(
    root: Path, *, current: OptionPricingPublication
) -> list[Path]:
    reachable = set(authoritative_option_pricing_runs(root, current=current))
    current_published = _utc(current.receipt.get("published_at"), "published_at")
    output: list[Path] = []
    for receipt_path in sorted(
        (root / "ml" / "option-pricing-runs").glob(
            f"*/{OPTION_PRICING_RECEIPT_NAME}"
        )
    ):
        run = receipt_path.parent.resolve()
        if run in reachable:
            continue
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if not isinstance(receipt, Mapping):
                continue
            record = _publication_record(root, run, receipt)
            if _utc(record["published_at"], "published_at") <= current_published:
                continue
            _verify_record(root, record)
        except Exception:
            continue
        output.append(run)
    return output


def _read_recovery_authorization(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise OptionPricingPublicationError(
            f"Recovery authorization is unreadable: {path}"
        ) from exc
    if (
        not isinstance(value, Mapping)
        or value.get("schema_version")
        != OPTION_PRICING_RECOVERY_AUTHORIZATION_VERSION
    ):
        raise OptionPricingPublicationError("Recovery authorization is invalid")
    return value


def read_current_option_pricing_publication(
    datastore_root: Path,
) -> OptionPricingPublication:
    root = Path(datastore_root).resolve()
    pointer_path = pricing_pointer_path(root)
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise OptionPricingPublicationError(
            f"Pricing pointer is unreadable: {pointer_path}"
        ) from exc
    if (
        not isinstance(pointer, Mapping)
        or pointer.get("schema_version")
        not in {
            OPTION_PRICING_POINTER_VERSION,
            *_LEGACY_OPTION_PRICING_POINTER_VERSIONS,
        }
    ):
        raise OptionPricingPublicationError(f"Pricing pointer is invalid: {pointer_path}")
    current = pointer.get("current")
    _validate_record(current, label="current Pricing record")
    run = _run_from_record(root, current)
    manifest, receipt = _verify_record(root, current)
    _verify_chain(root, receipt.get("previous_publication"), newer=receipt)
    return OptionPricingPublication(run, manifest, receipt, pointer)


def authoritative_option_pricing_runs(
    datastore_root: Path,
    *,
    current: OptionPricingPublication | None = None,
) -> dict[Path, pd.Timestamp]:
    root = Path(datastore_root).resolve()
    publication = current or read_current_option_pricing_publication(root)
    output: dict[Path, pd.Timestamp] = {}
    record: object = publication.pointer["current"]
    seen: set[str] = set()
    while record is not None:
        _validate_record(record, label="Pricing chain record")
        raw_path = str(record["run_path"])
        if raw_path in seen:
            raise OptionPricingPublicationError("Pricing publication chain contains a cycle")
        seen.add(raw_path)
        _manifest, receipt = (
            (publication.manifest, publication.receipt)
            if str(record["run_path"])
            == str(publication.pointer["current"]["run_path"])
            else _verify_record_metadata(root, record)
        )
        run = _run_from_record(root, record)
        output[run] = _utc(receipt.get("published_at"), "published_at")
        record = receipt.get("previous_publication")
    return output


def verified_option_pricing_history(
    datastore_root: Path,
    *,
    available_not_after: object | None = None,
) -> tuple[OptionPricingPublication, ...]:
    """Return every reachable generation, fully verified, oldest to newest."""

    root = Path(datastore_root).resolve()
    cutoff = (
        _utc(available_not_after, "available_not_after")
        if available_not_after is not None
        else None
    )
    current = read_current_option_pricing_publication(root)
    history: list[OptionPricingPublication] = []
    record: object = current.pointer["current"]
    seen: set[str] = set()
    while record is not None:
        _validate_record(record, label="Pricing history record")
        raw_path = str(record["run_path"])
        if raw_path in seen:
            raise OptionPricingPublicationError("Pricing history contains a cycle")
        seen.add(raw_path)
        published = _utc(record.get("published_at"), "published_at")
        manifest, receipt = _verify_record(root, record)
        if cutoff is None or published <= cutoff:
            history.append(
                OptionPricingPublication(
                    _run_from_record(root, record),
                    manifest,
                    receipt,
                    current.pointer,
                )
            )
        record = receipt.get("previous_publication")
    history.reverse()
    return tuple(history)


def read_option_pricing_publication_at(
    datastore_root: Path,
    *,
    available_not_after: object,
) -> OptionPricingPublication:
    """Return the newest fully verified publication causal by ``available_not_after``.

    The current authority and its complete receipt chain are verified before a
    cutoff is applied.  The selected historical run is then verified with its
    full manifest, output checksums, and physical Parquet contracts.  This is
    deliberately not a recovery search: corruption in the current authority or
    selected run is fatal and never causes an older run to be substituted.
    """

    root = Path(datastore_root).resolve()
    cutoff = _utc(available_not_after, "available_not_after")
    current = read_current_option_pricing_publication(root)
    record: object = current.pointer["current"]
    while record is not None:
        _validate_record(record, label="Pricing chain record")
        published = _utc(record.get("published_at"), "published_at")
        if published <= cutoff:
            if str(record["run_path"]) == str(
                current.pointer["current"]["run_path"]
            ):
                return current
            manifest, receipt = _verify_record(root, record)
            return OptionPricingPublication(
                _run_from_record(root, record),
                manifest,
                receipt,
                current.pointer,
            )
        _manifest, receipt = _verify_record_metadata(root, record)
        record = receipt.get("previous_publication")
    raise FileNotFoundError(
        "No verified reachable Pricing publication was available by the causal cutoff"
    )


def resolve_current_option_pricing_output(
    datastore_root: Path,
    name: str,
) -> Path:
    publication = read_current_option_pricing_publication(datastore_root)
    outputs = publication.manifest.get("output_files")
    if not isinstance(outputs, Mapping) or name not in outputs:
        raise OptionPricingPublicationError(
            f"Current Pricing run did not publish required output: {name}"
        )
    path = publication.run_directory / name
    if not path.is_file():
        raise OptionPricingPublicationError(f"Current Pricing output is missing: {path}")
    return path


def receipt_proven_prediction_rows(datastore_root: Path) -> pd.DataFrame:
    """Return earliest LIVE predictions whose first availability has a receipt."""

    root = Path(datastore_root).resolve()
    from ml.option_pricing.target_outcome import authoritative_target_outcomes

    target_frames = [
        publication.predictions()
        for publication in authoritative_target_outcomes(root)
    ]
    if pricing_pointer_path(root).is_file():
        current = read_current_option_pricing_publication(root)
        reachable = authoritative_option_pricing_runs(root, current=current)
    else:
        reachable = {}
    frames: list[pd.DataFrame] = []
    for run, published in sorted(reachable.items(), key=lambda item: item[1]):
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        _verify_manifest_output(run, manifest, "pricing-predictions.parquet")
        frame = pd.read_parquet(run / "pricing-predictions.parquet")
        if frame.empty:
            continue
        # Legacy generations assigned the field before files were complete.  The
        # immutable receipt's filesystem availability is a conservative lower
        # bound that cannot be made earlier by the embedded field.
        receipt_available = max(
            published,
            pd.Timestamp((run / OPTION_PRICING_RECEIPT_NAME).stat().st_mtime_ns, tz="UTC"),
        )
        available = pd.to_datetime(frame["prediction_available_at"], utc=True, errors="coerce")
        created = pd.to_datetime(
            frame["prediction_created_at"], utc=True, errors="coerce"
        )
        live = frame["prediction_mode"].astype("string").str.upper().eq("LIVE")
        first_committed_here = live & available.eq(published) & created.le(receipt_available)
        proven = frame.loc[first_committed_here].drop(columns="id", errors="ignore").copy()
        if not proven.empty:
            proven["_pricing_outcome_run_path"] = run.relative_to(root).as_posix()
            proven["_pricing_outcome_receipt_checksum_sha256"] = file_checksum(
                run / OPTION_PRICING_RECEIPT_NAME
            )
            proven["_pricing_authority_published_at"] = receipt_available
            frames.append(proven)
    frames = [*target_frames, *frames]
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True, sort=False)
    combined["prediction_available_at"] = pd.to_datetime(
        combined["prediction_available_at"], utc=True
    )
    combined["prediction_created_at"] = pd.to_datetime(
        combined["prediction_created_at"], utc=True
    )
    return (
        combined.sort_values(
            ["prediction_available_at", "prediction_created_at"], kind="stable"
        )
        .drop_duplicates(
            ["symbol", "target_snapshot_for", "contract_symbol"], keep="first"
        )
        .reset_index(drop=True)
    )


def _verify_chain(
    root: Path,
    record: object,
    *,
    newer: Mapping[str, object],
) -> None:
    seen: set[str] = set()
    newer_published = _utc(newer.get("published_at"), "published_at")
    while record is not None:
        _validate_record(record, label="previous Pricing publication")
        path = str(record["run_path"])
        if path in seen:
            raise OptionPricingPublicationError("Pricing publication chain contains a cycle")
        seen.add(path)
        _, receipt = _verify_record_metadata(root, record)
        published = _utc(receipt.get("published_at"), "published_at")
        if published > newer_published:
            raise OptionPricingPublicationError("Pricing publication chronology moves backwards")
        newer_published = published
        record = receipt.get("previous_publication")


def _verify_record(
    root: Path,
    record: Mapping[str, object],
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    run = _run_from_record(root, record)
    manifest = _verify_run(run)
    receipt_path = run / OPTION_PRICING_RECEIPT_NAME
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise OptionPricingPublicationError(
            f"Pricing receipt is unreadable: {receipt_path}"
        ) from exc
    if not isinstance(receipt, Mapping):
        raise OptionPricingPublicationError(f"Pricing receipt is malformed: {receipt_path}")
    expected_record = _publication_record(root, run, receipt)
    if dict(record) != expected_record:
        raise OptionPricingPublicationError(
            f"Pricing pointer/chain record does not match receipt: {run}"
        )
    manifest_version = _publication_contract_version(manifest)
    manifest_timestamp = _utc(manifest.get("run_timestamp"), "manifest run timestamp")
    receipt_timestamp = _utc(receipt.get("run_timestamp"), "receipt run timestamp")
    published = _utc(receipt.get("published_at"), "receipt published_at")
    previous = receipt.get("previous_publication")
    if previous is not None:
        _validate_record(previous, label="previous Pricing publication")
    if (
        receipt.get("schema_version") != manifest_version
        or receipt.get("run_path") != run.relative_to(root).as_posix()
        or receipt.get("manifest_checksum_sha256") != file_checksum(run / "manifest.json")
        or receipt_timestamp != manifest_timestamp
        or published < manifest_timestamp
    ):
        raise OptionPricingPublicationError(
            f"Pricing receipt does not match its run manifest: {run}"
        )
    return manifest, receipt


def _verify_record_metadata(
    root: Path,
    record: Mapping[str, object],
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    """Verify immutable chain metadata without rehashing every historical output."""

    _validate_record(record, label="Pricing chain record")
    run = _run_from_record(root, record)
    receipt_path = run / OPTION_PRICING_RECEIPT_NAME
    manifest_path = run / "manifest.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise OptionPricingPublicationError(
            f"Pricing chain metadata is unreadable: {run}"
        ) from exc
    if not isinstance(receipt, Mapping) or not isinstance(manifest, Mapping):
        raise OptionPricingPublicationError(f"Pricing chain metadata is malformed: {run}")
    manifest_checksum = file_checksum(manifest_path)
    receipt_checksum = file_checksum(receipt_path)
    manifest_timestamp = _utc(manifest.get("run_timestamp"), "manifest run timestamp")
    receipt_timestamp = _utc(receipt.get("run_timestamp"), "receipt run timestamp")
    published = _utc(receipt.get("published_at"), "receipt published_at")
    if (
        record.get("manifest_checksum_sha256") != manifest_checksum
        or record.get("receipt_checksum_sha256") != receipt_checksum
        or receipt.get("schema_version")
        not in _OPTION_PRICING_REQUIRED_OUTPUTS_BY_VERSION
        or receipt.get("run_path") != run.relative_to(root).as_posix()
        or receipt.get("manifest_checksum_sha256") != manifest_checksum
        or receipt_timestamp != manifest_timestamp
        or published < manifest_timestamp
        or _publication_record(root, run, receipt) != dict(record)
    ):
        raise OptionPricingPublicationError(
            f"Pricing chain receipt does not match its immutable metadata: {run}"
        )
    return manifest, receipt


def _verify_manifest_output(
    run: Path,
    manifest: Mapping[str, object],
    name: str,
) -> None:
    outputs = manifest.get("output_files")
    metadata = outputs.get(name) if isinstance(outputs, Mapping) else None
    path = run / name
    if (
        not isinstance(metadata, Mapping)
        or not path.is_file()
        or int(metadata.get("size", -1)) != path.stat().st_size
        or metadata.get("checksum_sha256") != file_checksum(path)
    ):
        raise OptionPricingPublicationError(
            f"Pricing manifest output checksum mismatch: {path}"
        )


def _verify_run(run: Path) -> Mapping[str, object]:
    try:
        manifest = verify_manifest(run)
    except Exception as exc:
        raise OptionPricingPublicationError(f"Pricing manifest is invalid: {run}") from exc
    outputs = manifest.get("output_files")
    if not isinstance(outputs, Mapping):
        raise OptionPricingPublicationError("Pricing manifest output inventory is invalid")
    resolved_run = run.resolve()
    for raw_name in outputs:
        relative = Path(str(raw_name))
        output = (resolved_run / relative).resolve()
        if relative.is_absolute() or resolved_run not in output.parents:
            raise OptionPricingPublicationError(
                f"Pricing manifest output path escapes its run: {raw_name}"
            )
    expected = set(OPTION_PRICING_REQUIRED_OUTPUTS) | {OPTION_PRICING_REPORT_NAME}
    if not expected.issubset(outputs):
        missing = sorted(expected.difference(outputs))
        raise OptionPricingPublicationError(
            "Pricing run lacks required outputs: " + ", ".join(missing)
        )
    publication_version = _publication_contract_version(manifest)
    required_outputs = _OPTION_PRICING_REQUIRED_OUTPUTS_BY_VERSION[publication_version]
    for name, schema in required_outputs.items():
        try:
            verify_parquet_schema(run / name, schema)
        except Exception as exc:
            raise OptionPricingPublicationError(
                f"Pricing Parquet contract is invalid: {run / name}"
            ) from exc
    return manifest


def _publication_contract_version(manifest: Mapping[str, object]) -> str:
    configuration = manifest.get("configuration")
    contract = (
        configuration.get("publication_contract")
        if isinstance(configuration, Mapping)
        else None
    )
    version = contract.get("version") if isinstance(contract, Mapping) else None
    if not isinstance(contract, Mapping) or (
        version not in _OPTION_PRICING_REQUIRED_OUTPUTS_BY_VERSION
        or contract.get("authority") != "ml/option-pricing-latest/run.json"
        or contract.get("schema_validation") is not True
        or contract.get("automated_action_allowed") is not False
    ):
        raise OptionPricingPublicationError(
            "Pricing manifest publication contract is incompatible"
        )
    return str(version)


def _publication_record(
    root: Path,
    run: Path,
    receipt: Mapping[str, object],
) -> dict[str, object]:
    return {
        "run_path": run.relative_to(root).as_posix(),
        "run_timestamp": _utc(receipt.get("run_timestamp"), "run_timestamp").isoformat(),
        "published_at": _utc(receipt.get("published_at"), "published_at").isoformat(),
        "manifest_checksum_sha256": file_checksum(run / "manifest.json"),
        "receipt_checksum_sha256": file_checksum(run / OPTION_PRICING_RECEIPT_NAME),
    }


def _validate_record(value: object, *, label: str) -> None:
    if not isinstance(value, Mapping):
        raise OptionPricingPublicationError(f"{label} is not an object")
    expected = {
        "run_path",
        "run_timestamp",
        "published_at",
        "manifest_checksum_sha256",
        "receipt_checksum_sha256",
    }
    if set(value) != expected:
        raise OptionPricingPublicationError(f"{label} has invalid fields")
    for key in ("run_path", "manifest_checksum_sha256", "receipt_checksum_sha256"):
        if not isinstance(value.get(key), str) or not value.get(key):
            raise OptionPricingPublicationError(f"{label} has invalid {key}")
    _utc(value.get("run_timestamp"), f"{label} run_timestamp")
    _utc(value.get("published_at"), f"{label} published_at")


def _run_from_record(root: Path, record: Mapping[str, object]) -> Path:
    raw = record.get("run_path")
    if not isinstance(raw, str) or not raw:
        raise OptionPricingPublicationError("Pricing record run_path is invalid")
    relative = Path(raw)
    if relative.is_absolute():
        raise OptionPricingPublicationError("Pricing record run_path must be relative")
    run = (root / relative).resolve()
    return _validate_run_location(root, run)


def _validate_run_location(root: Path, run_directory: Path) -> Path:
    run = Path(run_directory).resolve()
    allowed = (Path(root) / "ml" / "option-pricing-runs").resolve()
    if run.parent != allowed:
        raise OptionPricingPublicationError(
            f"Pricing run escapes immutable option-pricing-runs: {run}"
        )
    return run


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


def _utc(value: object, label: str) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise OptionPricingPublicationError(f"Invalid {label}")
    return pd.Timestamp(timestamp)


__all__ = [
    "LEGACY_OPTION_PRICING_PUBLICATION_VERSION",
    "OPTION_PRICING_PUBLICATION_VERSION",
    "OPTION_PRICING_RECEIPT_NAME",
    "OPTION_PRICING_RECOVERY_AUTHORIZATION_VERSION",
    "OPTION_PRICING_RECOVERY_RECEIPT_VERSION",
    "OPTION_PRICING_REPORT_NAME",
    "OptionPricingPublication",
    "OptionPricingPublicationError",
    "authoritative_option_pricing_runs",
    "diagnose_option_pricing_publications",
    "pricing_pointer_path",
    "publish_option_pricing_run",
    "read_current_option_pricing_publication",
    "read_option_pricing_publication_at",
    "recover_option_pricing_orphan",
    "receipt_proven_prediction_rows",
    "resolve_current_option_pricing_output",
    "verified_option_pricing_history",
]
