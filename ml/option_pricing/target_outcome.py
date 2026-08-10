from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

import pandas as pd

from ml.artifacts import file_checksum, utc_timestamp
from ml.parquet_contracts import (
    OPTION_PRICING_PREDICTION_SCHEMA,
    OPTION_PRICING_SAMPLE_SCHEMA,
    empty_frame,
    frame_with_readable_id,
    verify_parquet_schema,
    write_parquet_with_schema,
)


TARGET_OUTCOME_VERSION = "option-pricing-target-outcome-v1"
TARGET_OUTCOME_RECEIPT_VERSION = "option-pricing-target-outcome-receipt-v1"
TARGET_OUTCOME_POINTER_VERSION = "option-pricing-target-outcome-pointer-v1"
TARGET_OUTCOME_MANIFEST_VERSION = "option-pricing-target-outcome-manifest-v1"
TARGET_OUTCOME_PROOF_COLUMNS = (
    "_pricing_outcome_run_path",
    "_pricing_outcome_receipt_checksum_sha256",
    "_pricing_authority_published_at",
)


class TargetOutcomeError(RuntimeError):
    """The target-scoped Pricing authority failed closed."""


@dataclass(frozen=True)
class TargetOutcomePublication:
    target_snapshot_for: pd.Timestamp
    created_at: pd.Timestamp
    published_at: pd.Timestamp
    symbols: tuple[str, ...]
    terminal_status: str
    symbol_outcomes: Mapping[str, Mapping[str, object]]
    directory: Path
    samples_path: Path
    predictions_path: Path
    outcome_path: Path
    manifest_path: Path
    receipt_path: Path
    receipt: Mapping[str, object]
    pointer_record: Mapping[str, object]

    @property
    def receipt_checksum_sha256(self) -> str:
        return file_checksum(self.receipt_path)

    def samples(self) -> pd.DataFrame:
        return pd.read_parquet(self.samples_path).drop(columns="id", errors="ignore")

    def predictions(self, *, include_proof: bool = True) -> pd.DataFrame:
        frame = pd.read_parquet(self.predictions_path).drop(columns="id", errors="ignore")
        if include_proof and not frame.empty:
            frame[TARGET_OUTCOME_PROOF_COLUMNS[0]] = str(
                self.receipt.get("run_path", "")
            )
            frame[TARGET_OUTCOME_PROOF_COLUMNS[1]] = self.receipt_checksum_sha256
            frame[TARGET_OUTCOME_PROOF_COLUMNS[2]] = self.published_at
        return frame


def target_outcome_pointer_path(datastore_root: Path) -> Path:
    return Path(datastore_root) / "ml" / "option-pricing-target-latest" / "run.json"


def publish_target_outcome(
    datastore_root: Path,
    *,
    target_snapshot_for: object,
    created_at: object,
    symbols: Sequence[str],
    symbol_outcomes: Mapping[str, Mapping[str, object]],
    terminal_status: str,
    samples: pd.DataFrame,
    predictions: pd.DataFrame,
    bar_readiness: Mapping[str, object] | None,
    clock: Callable[[], object] | None = None,
) -> TargetOutcomePublication:
    """Publish the small prediction-or-skip authority consumed by Options."""

    root = Path(datastore_root).resolve()
    target = utc_timestamp(target_snapshot_for)
    created = utc_timestamp(created_at)
    clean_symbols = tuple(
        dict.fromkeys(str(value).strip().upper() for value in symbols if str(value).strip())
    )
    if not clean_symbols:
        raise ValueError("Target outcome requires at least one symbol")
    if set(symbol_outcomes) != set(clean_symbols):
        raise ValueError("Target outcome must contain exactly one status per symbol")
    existing = _read_optional_target(root, target)
    if existing is not None:
        if existing.symbols != clean_symbols:
            raise TargetOutcomeError(
                "An authoritative outcome already owns this target with another scope"
            )
        return existing

    prepared_samples = _validate_target_frame(samples, target=target, label="samples")
    prepared_predictions = _validate_target_frame(
        predictions,
        target=target,
        label="predictions",
    )
    normalized_terminal = str(terminal_status).strip().upper()
    if normalized_terminal == "PREDICTIONS_PUBLISHED" and prepared_predictions.empty:
        raise ValueError(
            "PREDICTIONS_PUBLISHED requires at least one verified target prediction"
        )
    if not prepared_predictions.empty:
        modes = prepared_predictions["prediction_mode"].astype("string").str.upper()
        if not modes.eq("LIVE").all():
            raise ValueError("Target authority may publish only LIVE predictions")
        prediction_created = pd.to_datetime(
            prepared_predictions["prediction_created_at"], utc=True, errors="coerce"
        )
        if prediction_created.isna().any() or prediction_created.gt(created).any():
            raise ValueError("Target predictions have an invalid creation clock")

    previous: Mapping[str, object] | None = None
    pointer_path = target_outcome_pointer_path(root)
    if pointer_path.is_file():
        previous = read_current_target_outcome(root).pointer_record
    parent = root / "ml" / "option-pricing-target-outcomes"
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / f"{target.value}-{created.value}"
    if destination.exists():
        raise TargetOutcomeError(f"Target outcome destination already exists: {destination}")
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.tmp-{os.getpid()}-",
            dir=parent,
        )
    )
    samples_path = staging / "pricing-samples.parquet"
    predictions_path = staging / "pricing-predictions.parquet"
    outcome_path = staging / "outcome.json"
    manifest_path = staging / "manifest.json"
    receipt_path = staging / "receipt.json"
    outcome = {
        "schema_version": TARGET_OUTCOME_VERSION,
        "target_snapshot_for": target.isoformat(),
        "prediction_created_at": created.isoformat(),
        "terminal_status": normalized_terminal,
        "symbols": list(clean_symbols),
        "symbol_outcomes": {
            symbol: dict(symbol_outcomes[symbol]) for symbol in clean_symbols
        },
        "bar_readiness": dict(bar_readiness) if bar_readiness is not None else None,
        "sample_rows": len(prepared_samples),
        "prediction_rows": len(prepared_predictions),
        "automated_action_allowed": False,
    }
    try:
        write_parquet_with_schema(
            _output_frame(
                prepared_samples,
                schema=OPTION_PRICING_SAMPLE_SCHEMA,
                keys=("symbol", "target_snapshot_for", "contract_symbol"),
            ),
            samples_path,
            OPTION_PRICING_SAMPLE_SCHEMA,
        )
        _write_json(outcome_path, outcome)
        # Sample materialization and prior-chain verification are complete before
        # the availability clock is sampled. Options separately records the later
        # barrier-observation time, so this field cannot grant credit on its own.
        published = max(utc_timestamp((clock or utc_timestamp)()), created)
        if not prepared_predictions.empty:
            prepared_predictions = prepared_predictions.copy()
            prepared_predictions["prediction_available_at"] = published
        write_parquet_with_schema(
            _output_frame(
                prepared_predictions,
                schema=OPTION_PRICING_PREDICTION_SCHEMA,
                keys=(
                    "symbol",
                    "target_snapshot_for",
                    "contract_symbol",
                    "prediction_created_at",
                ),
            ),
            predictions_path,
            OPTION_PRICING_PREDICTION_SCHEMA,
        )
        outputs = {
            path.name: {
                "size": path.stat().st_size,
                "checksum_sha256": file_checksum(path),
            }
            for path in (samples_path, predictions_path, outcome_path)
        }
        manifest = {
            "schema_version": TARGET_OUTCOME_MANIFEST_VERSION,
            "target_snapshot_for": target.isoformat(),
            "created_at": created.isoformat(),
            "outputs": outputs,
        }
        _write_json(manifest_path, manifest)
        receipt = {
            "schema_version": TARGET_OUTCOME_RECEIPT_VERSION,
            "run_path": destination.relative_to(root).as_posix(),
            "target_snapshot_for": target.isoformat(),
            "created_at": created.isoformat(),
            "published_at": published.isoformat(),
            "terminal_status": outcome["terminal_status"],
            "manifest_checksum_sha256": file_checksum(manifest_path),
            "previous_outcome": dict(previous) if previous is not None else None,
            "automated_action_allowed": False,
        }
        _write_json(receipt_path, receipt)
        staging.replace(destination)
    except BaseException:
        # Private staging remains unreachable and is safe to inspect after restart.
        raise

    publication = _read_directory(root, destination)
    pointer = {
        "schema_version": TARGET_OUTCOME_POINTER_VERSION,
        "current": dict(publication.pointer_record),
    }
    _write_json_atomic(pointer_path, pointer)
    try:
        observed_pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TargetOutcomeError("Pricing target pointer was not durably readable") from exc
    if observed_pointer != pointer:
        raise TargetOutcomeError("Pricing target pointer disagrees after atomic publication")
    return publication


def read_current_target_outcome(datastore_root: Path) -> TargetOutcomePublication:
    root = Path(datastore_root).resolve()
    pointer_path = target_outcome_pointer_path(root)
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TargetOutcomeError(f"Pricing target pointer is unreadable: {pointer_path}") from exc
    if (
        not isinstance(pointer, Mapping)
        or pointer.get("schema_version") != TARGET_OUTCOME_POINTER_VERSION
        or not isinstance(pointer.get("current"), Mapping)
    ):
        raise TargetOutcomeError("Pricing target pointer is malformed")
    publication = _read_record(root, pointer["current"])
    _verify_chain(root, publication.receipt.get("previous_outcome"), newer=publication)
    return publication


def read_target_outcome(
    datastore_root: Path,
    *,
    target_snapshot_for: object,
) -> TargetOutcomePublication:
    root = Path(datastore_root).resolve()
    target = utc_timestamp(target_snapshot_for)
    current = read_current_target_outcome(root)
    publication = current
    seen: set[str] = set()
    while True:
        run_path = str(publication.receipt.get("run_path", ""))
        if run_path in seen:
            raise TargetOutcomeError("Pricing target outcome chain contains a cycle")
        seen.add(run_path)
        if publication.target_snapshot_for == target:
            return publication
        previous = publication.receipt.get("previous_outcome")
        if not isinstance(previous, Mapping):
            break
        publication = _read_record(root, previous)
    raise TargetOutcomeError(
        f"No authoritative Pricing outcome exists for target {target.isoformat()}"
    )


def authoritative_target_outcomes(
    datastore_root: Path,
    *,
    published_after: object | None = None,
) -> tuple[TargetOutcomePublication, ...]:
    """Return verified outcomes, optionally only those not in an older generation.

    The current outcome and the complete receipt chain are always metadata-verified
    by ``read_current_target_outcome``.  When a cutoff is supplied, older Parquet
    payloads are not repeatedly hashed and materialized on every full Pricing run.
    """

    root = Path(datastore_root).resolve()
    cutoff = utc_timestamp(published_after) if published_after is not None else None
    try:
        current = read_current_target_outcome(root)
    except TargetOutcomeError:
        if target_outcome_pointer_path(root).exists():
            raise
        return ()
    output: list[TargetOutcomePublication] = []
    publication = current
    seen: set[str] = set()
    while True:
        run_path = str(publication.receipt.get("run_path", ""))
        if run_path in seen:
            raise TargetOutcomeError("Pricing target outcome chain contains a cycle")
        seen.add(run_path)
        if cutoff is not None and publication.published_at <= cutoff:
            break
        output.append(publication)
        previous = publication.receipt.get("previous_outcome")
        if not isinstance(previous, Mapping):
            break
        if cutoff is not None:
            _, previous_published = _read_record_metadata(root, previous)
            if previous_published <= cutoff:
                break
        publication = _read_record(root, previous)
    return tuple(reversed(output))


def _read_optional_target(root: Path, target: pd.Timestamp) -> TargetOutcomePublication | None:
    if not target_outcome_pointer_path(root).is_file():
        return None
    try:
        return read_target_outcome(root, target_snapshot_for=target)
    except TargetOutcomeError as exc:
        if "No authoritative Pricing outcome exists" in str(exc):
            return None
        raise


def _read_record(root: Path, record: Mapping[str, object]) -> TargetOutcomePublication:
    expected = {
        "run_path",
        "target_snapshot_for",
        "created_at",
        "published_at",
        "terminal_status",
        "manifest_checksum_sha256",
        "receipt_checksum_sha256",
    }
    if set(record) != expected:
        raise TargetOutcomeError("Pricing target pointer record has invalid fields")
    relative = Path(str(record.get("run_path", "")))
    directory = (root / relative).resolve()
    allowed = (root / "ml" / "option-pricing-target-outcomes").resolve()
    if relative.is_absolute() or directory.parent != allowed:
        raise TargetOutcomeError("Pricing target pointer escapes its immutable root")
    publication = _read_directory(root, directory)
    if dict(record) != dict(publication.pointer_record):
        raise TargetOutcomeError("Pricing target pointer disagrees with its receipt")
    return publication


def _read_directory(root: Path, directory: Path) -> TargetOutcomePublication:
    receipt_path = directory / "receipt.json"
    manifest_path = directory / "manifest.json"
    outcome_path = directory / "outcome.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TargetOutcomeError(f"Pricing target outcome is unreadable: {directory}") from exc
    if not all(isinstance(value, Mapping) for value in (receipt, manifest, outcome)):
        raise TargetOutcomeError("Pricing target outcome metadata is malformed")
    target = utc_timestamp(receipt.get("target_snapshot_for"))
    created = utc_timestamp(receipt.get("created_at"))
    published = utc_timestamp(receipt.get("published_at"))
    outputs = manifest.get("outputs")
    if not isinstance(outputs, Mapping):
        raise TargetOutcomeError("Pricing target manifest output inventory is malformed")
    expected_names = {"pricing-samples.parquet", "pricing-predictions.parquet", "outcome.json"}
    if set(outputs) != expected_names:
        raise TargetOutcomeError("Pricing target manifest output inventory is incomplete")
    for name in expected_names:
        path = directory / name
        metadata = outputs.get(name)
        if (
            not path.is_file()
            or not isinstance(metadata, Mapping)
            or int(metadata.get("size", -1)) != path.stat().st_size
            or metadata.get("checksum_sha256") != file_checksum(path)
        ):
            raise TargetOutcomeError(f"Pricing target output verification failed: {path}")
    verify_parquet_schema(directory / "pricing-samples.parquet", OPTION_PRICING_SAMPLE_SCHEMA)
    verify_parquet_schema(
        directory / "pricing-predictions.parquet", OPTION_PRICING_PREDICTION_SCHEMA
    )
    symbols = tuple(str(value).strip().upper() for value in outcome.get("symbols", ()))
    symbol_outcomes = outcome.get("symbol_outcomes")
    if (
        receipt.get("schema_version") != TARGET_OUTCOME_RECEIPT_VERSION
        or manifest.get("schema_version") != TARGET_OUTCOME_MANIFEST_VERSION
        or outcome.get("schema_version") != TARGET_OUTCOME_VERSION
        or receipt.get("run_path") != directory.relative_to(root).as_posix()
        or receipt.get("manifest_checksum_sha256") != file_checksum(manifest_path)
        or utc_timestamp(manifest.get("target_snapshot_for")) != target
        or utc_timestamp(outcome.get("target_snapshot_for")) != target
        or utc_timestamp(manifest.get("created_at")) != created
        or utc_timestamp(outcome.get("prediction_created_at")) != created
        or published < created
        or receipt.get("terminal_status") != outcome.get("terminal_status")
        or receipt.get("automated_action_allowed") is not False
        or outcome.get("automated_action_allowed") is not False
        or not symbols
        or not isinstance(symbol_outcomes, Mapping)
        or set(symbol_outcomes) != set(symbols)
    ):
        raise TargetOutcomeError("Pricing target receipt verification failed")
    pointer_record = {
        "run_path": receipt["run_path"],
        "target_snapshot_for": target.isoformat(),
        "created_at": created.isoformat(),
        "published_at": published.isoformat(),
        "terminal_status": receipt["terminal_status"],
        "manifest_checksum_sha256": file_checksum(manifest_path),
        "receipt_checksum_sha256": file_checksum(receipt_path),
    }
    return TargetOutcomePublication(
        target_snapshot_for=target,
        created_at=created,
        published_at=published,
        symbols=symbols,
        terminal_status=str(receipt["terminal_status"]),
        symbol_outcomes={str(key): dict(value) for key, value in symbol_outcomes.items()},
        directory=directory,
        samples_path=directory / "pricing-samples.parquet",
        predictions_path=directory / "pricing-predictions.parquet",
        outcome_path=outcome_path,
        manifest_path=manifest_path,
        receipt_path=receipt_path,
        receipt=receipt,
        pointer_record=pointer_record,
    )


def _verify_chain(
    root: Path,
    record: object,
    *,
    newer: TargetOutcomePublication,
) -> None:
    newer_time = newer.published_at
    seen: set[str] = set()
    while record is not None:
        if not isinstance(record, Mapping):
            raise TargetOutcomeError("Previous Pricing target record is malformed")
        path = str(record.get("run_path", ""))
        if path in seen:
            raise TargetOutcomeError("Pricing target outcome chain contains a cycle")
        seen.add(path)
        receipt, published_at = _read_record_metadata(root, record)
        if published_at > newer_time:
            raise TargetOutcomeError("Pricing target outcome chronology moves backwards")
        newer_time = published_at
        record = receipt.get("previous_outcome")


def _read_record_metadata(
    root: Path,
    record: Mapping[str, object],
) -> tuple[Mapping[str, object], pd.Timestamp]:
    expected = {
        "run_path",
        "target_snapshot_for",
        "created_at",
        "published_at",
        "terminal_status",
        "manifest_checksum_sha256",
        "receipt_checksum_sha256",
    }
    if set(record) != expected:
        raise TargetOutcomeError("Previous Pricing target record has invalid fields")
    relative = Path(str(record.get("run_path", "")))
    directory = (root / relative).resolve()
    allowed = (root / "ml" / "option-pricing-target-outcomes").resolve()
    if relative.is_absolute() or directory.parent != allowed:
        raise TargetOutcomeError("Previous Pricing target record escapes its root")
    receipt_path = directory / "receipt.json"
    manifest_path = directory / "manifest.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TargetOutcomeError("Previous Pricing target receipt is unreadable") from exc
    if not isinstance(receipt, Mapping):
        raise TargetOutcomeError("Previous Pricing target receipt is malformed")
    published = utc_timestamp(receipt.get("published_at"))
    if (
        receipt.get("schema_version") != TARGET_OUTCOME_RECEIPT_VERSION
        or receipt.get("run_path") != relative.as_posix()
        or record.get("manifest_checksum_sha256") != file_checksum(manifest_path)
        or record.get("receipt_checksum_sha256") != file_checksum(receipt_path)
        or receipt.get("manifest_checksum_sha256") != file_checksum(manifest_path)
        or utc_timestamp(record.get("published_at")) != published
        or utc_timestamp(record.get("target_snapshot_for"))
        != utc_timestamp(receipt.get("target_snapshot_for"))
    ):
        raise TargetOutcomeError("Previous Pricing target receipt verification failed")
    return receipt, published


def _validate_target_frame(
    frame: pd.DataFrame,
    *,
    target: pd.Timestamp,
    label: str,
) -> pd.DataFrame:
    output = frame.drop(columns="id", errors="ignore").copy()
    if output.empty:
        return output
    if "target_snapshot_for" not in output:
        raise ValueError(f"Target {label} lack target_snapshot_for")
    observed = pd.to_datetime(output["target_snapshot_for"], utc=True, errors="coerce")
    if observed.isna().any() or not observed.eq(target).all():
        raise ValueError(f"Target {label} mix cycle identities")
    return output


def _output_frame(
    frame: pd.DataFrame,
    *,
    schema: object,
    keys: Sequence[str],
) -> pd.DataFrame:
    if frame.empty:
        return empty_frame(schema)  # type: ignore[arg-type]
    return frame_with_readable_id(frame.drop(columns="id", errors="ignore"), key_columns=keys)


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
    "TARGET_OUTCOME_PROOF_COLUMNS",
    "TARGET_OUTCOME_VERSION",
    "TargetOutcomeError",
    "TargetOutcomePublication",
    "authoritative_target_outcomes",
    "publish_target_outcome",
    "read_current_target_outcome",
    "read_target_outcome",
    "target_outcome_pointer_path",
]
