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
    OPTION_PRICING_EVALUATION_SCHEMA,
    OPTION_PRICING_MONITORING_SCHEMA,
    OPTION_PRICING_PREDICTION_SCHEMA,
    OPTION_PRICING_SAMPLE_SCHEMA,
    OPTION_PRICING_SURFACE_SCHEMA,
    verify_parquet_schema,
)


OPTION_PRICING_PUBLICATION_VERSION = "option-pricing-publication-v2"
OPTION_PRICING_POINTER_VERSION = "option-pricing-pointer-v2"
_LEGACY_OPTION_PRICING_PUBLICATION_VERSION = "option-pricing-publication-v1"
_LEGACY_OPTION_PRICING_POINTER_VERSIONS = {"option-pricing-pointer-v1"}
OPTION_PRICING_RECEIPT_NAME = "publication.json"
OPTION_PRICING_REQUIRED_OUTPUTS = {
    "pricing-samples.parquet": OPTION_PRICING_SAMPLE_SCHEMA,
    "pricing-predictions.parquet": OPTION_PRICING_PREDICTION_SCHEMA,
    "pricing-evaluations.parquet": OPTION_PRICING_EVALUATION_SCHEMA,
    "pricing-surfaces.parquet": OPTION_PRICING_SURFACE_SCHEMA,
    "pricing-monitoring.parquet": OPTION_PRICING_MONITORING_SCHEMA,
}
_OPTION_PRICING_REQUIRED_OUTPUTS_BY_VERSION = {
    _LEGACY_OPTION_PRICING_PUBLICATION_VERSION: {
        **OPTION_PRICING_REQUIRED_OUTPUTS,
        "pricing-surfaces.parquet": LEGACY_OPTION_PRICING_SURFACE_SCHEMA,
    },
    OPTION_PRICING_PUBLICATION_VERSION: OPTION_PRICING_REQUIRED_OUTPUTS,
}
OPTION_PRICING_REPORT_NAME = "option-pricing-model-reports.json"


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

    receipt_path = run / OPTION_PRICING_RECEIPT_NAME
    if receipt_path.is_file():
        try:
            existing = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise OptionPricingPublicationError(
                f"Existing Pricing receipt is unreadable: {receipt_path}"
            ) from exc
        published = (
            _utc(existing.get("published_at"), "orphan receipt published_at")
            if isinstance(existing, Mapping)
            else pd.NaT
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

    desired_base = {
        "schema_version": OPTION_PRICING_PUBLICATION_VERSION,
        "run_path": run.relative_to(root).as_posix(),
        "run_timestamp": run_timestamp.isoformat(),
        "published_at": published.isoformat(),
        "manifest_checksum_sha256": file_checksum(run / "manifest.json"),
        "previous_publication": dict(previous) if previous is not None else None,
    }
    if existing is not None:
        expected_without_publication_time = {
            key: value
            for key, value in desired_base.items()
            if key != "published_at"
        }
        observed_without_publication_time = (
            {
                key: value
                for key, value in existing.items()
                if key != "published_at"
            }
            if isinstance(existing, Mapping)
            else {}
        )
        if (
            not isinstance(existing, Mapping)
            or set(existing) != set(desired_base)
            or observed_without_publication_time != expected_without_publication_time
            or published < run_timestamp
        ):
            raise OptionPricingPublicationError(
                "Existing orphan Pricing receipt is incompatible with the current chain"
            )
        receipt = existing
    else:
        _write_json_atomic(receipt_path, desired_base)
        receipt = desired_base

    record = _publication_record(root, run, receipt)
    pointer = {
        "schema_version": OPTION_PRICING_POINTER_VERSION,
        "current": record,
    }
    _write_json_atomic(pointer_path, pointer)
    return read_current_option_pricing_publication(root)


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
    manifest_timestamp = _utc(manifest.get("run_timestamp"), "manifest run timestamp")
    receipt_timestamp = _utc(receipt.get("run_timestamp"), "receipt run timestamp")
    published = _utc(receipt.get("published_at"), "receipt published_at")
    previous = receipt.get("previous_publication")
    if previous is not None:
        _validate_record(previous, label="previous Pricing publication")
    if (
        receipt.get("schema_version")
        not in _OPTION_PRICING_REQUIRED_OUTPUTS_BY_VERSION
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
    "OPTION_PRICING_PUBLICATION_VERSION",
    "OPTION_PRICING_RECEIPT_NAME",
    "OPTION_PRICING_REPORT_NAME",
    "OptionPricingPublication",
    "OptionPricingPublicationError",
    "authoritative_option_pricing_runs",
    "pricing_pointer_path",
    "publish_option_pricing_run",
    "read_current_option_pricing_publication",
    "receipt_proven_prediction_rows",
    "resolve_current_option_pricing_output",
]
