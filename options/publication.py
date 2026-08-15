from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import pandas as pd

from datafetching.ids import add_readable_id
from datafetching.layout import safe_token
from datafetching.pricing_barrier import verify_pricing_barrier_metadata
from ml.artifacts import file_checksum


LEGACY_OPTION_SNAPSHOT_PUBLICATION_VERSION = "option-snapshot-publication-v1"
OPTION_SNAPSHOT_PUBLICATION_VERSION = "option-snapshot-publication-v2"
LEGACY_OPTION_SNAPSHOT_POINTER_VERSION = "option-snapshot-pointer-v1"
OPTION_SNAPSHOT_POINTER_VERSION = "option-snapshot-pointer-v2"
OPTION_SNAPSHOT_RECEIPT_NAME = "receipt.json"
OPTION_SNAPSHOT_OUTPUTS = (
    "raw.parquet",
    "contracts.parquet",
    "option-quality.parquet",
)
SUPPORTED_OPTION_PROVIDERS = ("databento-opra", "schwab")
_LEGACY_SNAPSHOT_KEY = ("symbol", "snapshot_for", "available_at")
_SNAPSHOT_KEY = ("provider", "symbol", "target_snapshot_for")


class OptionSnapshotPublicationError(RuntimeError):
    """A committed Schwab option snapshot failed strict validation."""


@dataclass(frozen=True)
class CommittedOptionSnapshot:
    symbol: str
    snapshot_for: pd.Timestamp
    available_at: pd.Timestamp
    directory: Path
    raw_path: Path
    contracts_path: Path
    features_path: Path
    receipt_path: Path
    receipt: Mapping[str, object]
    provider: str = "schwab"
    dataset: str = "schwab-option-chain"
    receipt_published_at: pd.Timestamp | None = None
    schema_version: str = OPTION_SNAPSHOT_PUBLICATION_VERSION


def option_writer_lock_path(datastore_root: Path) -> Path:
    return Path(datastore_root) / ".ducketz-options-writer.lock"


def option_snapshot_root(
    datastore_root: Path,
    *,
    symbol: str,
    provider: str = "schwab",
) -> Path:
    clean_provider = _provider(provider)
    return (
        Path(datastore_root)
        / "stocks"
        / safe_token(symbol.strip().upper())
        / "options"
        / "snapshots"
        / safe_token(clean_provider)
    )


def option_snapshot_pointer_path(
    datastore_root: Path,
    *,
    symbol: str,
    provider: str = "schwab",
) -> Path:
    clean_provider = _provider(provider)
    return (
        Path(datastore_root)
        / "stocks"
        / safe_token(symbol.strip().upper())
        / "options"
        / "latest"
        / f"{safe_token(clean_provider)}.json"
    )


def publish_option_snapshot(
    datastore_root: Path,
    *,
    symbol: str,
    raw: pd.DataFrame,
    contracts: pd.DataFrame,
    features: pd.DataFrame,
    provider: str = "schwab",
    dataset: str | None = None,
    request_started_at: object | None = None,
    pricing_barrier: Mapping[str, object] | None = None,
    receipt_published_at: object | None = None,
) -> CommittedOptionSnapshot:
    """Publish one immutable provider-neutral natural target.

    The natural identity is ``(provider, symbol, target_snapshot_for)``.  A
    semantically identical retry returns the earliest verified receipt; a
    divergent retry fails closed and never calls a provider or changes evidence.
    """

    clean_symbol = symbol.strip().upper()
    clean_provider = _provider(provider)
    clean_dataset = str(
        dataset
        or ("OPRA.PILLAR" if clean_provider == "databento-opra" else "SCHWAB_CHAIN")
    ).strip()
    if not clean_dataset:
        raise ValueError("Option snapshot dataset is required")
    snapshot_for, available_at = _coherent_key(
        clean_symbol,
        (raw, contracts, features),
    )
    prepared_inputs = {
        name: _provider_neutral_frame(
            frame,
            provider=clean_provider,
            dataset=clean_dataset,
            symbol=clean_symbol,
            target_snapshot_for=snapshot_for,
            available_at=available_at,
        )
        for name, frame in {
            "raw.parquet": raw,
            "contracts.parquet": contracts,
            "option-quality.parquet": features,
        }.items()
    }
    existing_natural = _existing_natural_snapshot(
        datastore_root,
        provider=clean_provider,
        symbol=clean_symbol,
        target_snapshot_for=snapshot_for,
    )
    if existing_natural is not None:
        _verify_semantically_identical_retry(existing_natural, prepared_inputs)
        _publish_pointer(datastore_root, existing_natural)
        return existing_natural

    parent = option_snapshot_root(
        datastore_root,
        symbol=clean_symbol,
        provider=clean_provider,
    )
    parent.mkdir(parents=True, exist_ok=True)
    run_name = str(snapshot_for.value)
    destination = parent / run_name
    if destination.is_dir():
        committed = read_option_snapshot(destination)
        _publish_pointer(datastore_root, committed)
        return committed

    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{run_name}.tmp-{os.getpid()}-",
            dir=parent,
        )
    )
    try:
        prepared = {
            "raw.parquet": add_readable_id(
                prepared_inputs["raw.parquet"].reset_index(drop=True),
                key_columns=_SNAPSHOT_KEY,
            ),
            "contracts.parquet": add_readable_id(
                prepared_inputs["contracts.parquet"].reset_index(drop=True),
                key_columns=(*_SNAPSHOT_KEY, "contract_symbol"),
            ),
            "option-quality.parquet": add_readable_id(
                prepared_inputs["option-quality.parquet"].reset_index(drop=True),
                key_columns=_SNAPSHOT_KEY,
            ),
        }
        for name, frame in prepared.items():
            frame.to_parquet(staging / name, index=False)

        output_inventory = {
            name: {
                "rows": len(prepared[name]),
                "size": (staging / name).stat().st_size,
                "checksum_sha256": file_checksum(staging / name),
            }
            for name in OPTION_SNAPSHOT_OUTPUTS
        }
        receipt_published = (
            _utc(receipt_published_at, "receipt_published_at")
            if receipt_published_at is not None
            else pd.Timestamp.now(tz="UTC")
        )
        if receipt_published < available_at:
            raise ValueError("Option receipt publication cannot predate response availability")
        manifest = {
            "schema_version": OPTION_SNAPSHOT_PUBLICATION_VERSION,
            "normalized_schema_version": "option-market-evidence-v2",
            "provider": clean_provider,
            "dataset": clean_dataset,
            "symbol": clean_symbol,
            "target_snapshot_for": snapshot_for.isoformat(),
            "snapshot_for": snapshot_for.isoformat(),
            "first_available_at": available_at.isoformat(),
            "available_at": available_at.isoformat(),
            "receipt_published_at": receipt_published.isoformat(),
            "request_started_at": (
                _utc(request_started_at, "request_started_at").isoformat()
                if request_started_at is not None
                else None
            ),
            "pricing_barrier": (
                dict(pricing_barrier) if pricing_barrier is not None else None
            ),
            "outputs": output_inventory,
        }
        manifest_path = staging / "manifest.json"
        _write_json(manifest_path, manifest)
        receipt = {
            "schema_version": OPTION_SNAPSHOT_PUBLICATION_VERSION,
            "normalized_schema_version": "option-market-evidence-v2",
            "provider": clean_provider,
            "dataset": clean_dataset,
            "symbol": clean_symbol,
            "target_snapshot_for": snapshot_for.isoformat(),
            "snapshot_for": snapshot_for.isoformat(),
            "first_available_at": available_at.isoformat(),
            "available_at": available_at.isoformat(),
            "receipt_published_at": manifest["receipt_published_at"],
            "request_started_at": manifest["request_started_at"],
            "pricing_barrier": manifest["pricing_barrier"],
            "run_path": destination.relative_to(Path(datastore_root)).as_posix(),
            "manifest_checksum_sha256": file_checksum(manifest_path),
            "outputs": output_inventory,
        }
        _write_json(staging / OPTION_SNAPSHOT_RECEIPT_NAME, receipt)
        staging.replace(destination)
    except BaseException:
        _remove_unpublished_staging(staging)
        raise

    committed = read_option_snapshot(destination)
    _publish_pointer(datastore_root, committed)
    return committed


def read_option_snapshot(directory: Path) -> CommittedOptionSnapshot:
    run = Path(directory)
    receipt_path = run / OPTION_SNAPSHOT_RECEIPT_NAME
    manifest_path = run / "manifest.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OptionSnapshotPublicationError(
            f"Option snapshot receipt is unreadable: {run}"
        ) from exc
    if not isinstance(receipt, Mapping) or not isinstance(manifest, Mapping):
        raise OptionSnapshotPublicationError(
            f"Option snapshot metadata is malformed: {run}"
        )
    version = str(receipt.get("schema_version") or "")
    if (
        version
        not in {
            OPTION_SNAPSHOT_PUBLICATION_VERSION,
            LEGACY_OPTION_SNAPSHOT_PUBLICATION_VERSION,
        }
        or manifest.get("schema_version") != version
        or receipt.get("manifest_checksum_sha256") != file_checksum(manifest_path)
    ):
        raise OptionSnapshotPublicationError(
            f"Option snapshot metadata does not validate: {run}"
        )
    provider = (
        _provider(receipt.get("provider"))
        if version == OPTION_SNAPSHOT_PUBLICATION_VERSION
        else "schwab"
    )
    dataset = str(
        receipt.get("dataset")
        or ("OPRA.PILLAR" if provider == "databento-opra" else "SCHWAB_CHAIN")
    ).strip()
    symbol = str(receipt.get("symbol") or "").strip().upper()
    snapshot_for = _utc(
        receipt.get("target_snapshot_for", receipt.get("snapshot_for")),
        "target_snapshot_for",
    )
    available_at = _utc(
        receipt.get("first_available_at", receipt.get("available_at")),
        "first_available_at",
    )
    receipt_published_at = (
        _utc(receipt.get("receipt_published_at"), "receipt_published_at")
        if receipt.get("receipt_published_at") is not None
        else available_at
    )
    request_started_at = (
        _utc(receipt.get("request_started_at"), "request_started_at")
        if receipt.get("request_started_at") is not None
        else None
    )
    if (
        manifest.get("symbol") != symbol
        or _utc(
            manifest.get("target_snapshot_for", manifest.get("snapshot_for")),
            "manifest target_snapshot_for",
        )
        != snapshot_for
        or _utc(
            manifest.get("first_available_at", manifest.get("available_at")),
            "manifest first_available_at",
        )
        != available_at
        or manifest.get("request_started_at") != receipt.get("request_started_at")
        or manifest.get("pricing_barrier") != receipt.get("pricing_barrier")
        or manifest.get("receipt_published_at")
        != receipt.get("receipt_published_at")
        or receipt_published_at < available_at
    ):
        raise OptionSnapshotPublicationError(
            f"Option snapshot receipt disagrees with its manifest: {run}"
        )
    if request_started_at is not None:
        if request_started_at > available_at:
            raise OptionSnapshotPublicationError(
                f"Option snapshot request follows its availability: {run}"
            )
        try:
            verify_pricing_barrier_metadata(
                receipt.get("pricing_barrier"),
                target_snapshot_for=snapshot_for,
                request_started_at=request_started_at,
            )
        except Exception as exc:
            raise OptionSnapshotPublicationError(
                f"Option snapshot Pricing barrier proof is invalid: {run}"
            ) from exc
    outputs = receipt.get("outputs")
    manifest_outputs = manifest.get("outputs")
    if (
        not isinstance(outputs, Mapping)
        or not isinstance(manifest_outputs, Mapping)
        or set(outputs) != set(OPTION_SNAPSHOT_OUTPUTS)
        or dict(outputs) != dict(manifest_outputs)
    ):
        raise OptionSnapshotPublicationError(
            f"Option snapshot output inventory is invalid: {run}"
        )
    for name in OPTION_SNAPSHOT_OUTPUTS:
        path = run / name
        metadata = outputs.get(name)
        if not path.is_file() or not isinstance(metadata, Mapping):
            raise OptionSnapshotPublicationError(
                f"Option snapshot output is missing: {path}"
            )
        if (
            int(metadata.get("size", -1)) != path.stat().st_size
            or metadata.get("checksum_sha256") != file_checksum(path)
        ):
            raise OptionSnapshotPublicationError(
                f"Option snapshot output checksum mismatch: {path}"
            )
    if version == OPTION_SNAPSHOT_PUBLICATION_VERSION and (
        receipt.get("provider") != provider
        or manifest.get("provider") != provider
        or receipt.get("dataset") != dataset
        or manifest.get("dataset") != dataset
        or receipt.get("normalized_schema_version") != "option-market-evidence-v2"
        or manifest.get("normalized_schema_version") != "option-market-evidence-v2"
        or receipt.get("target_snapshot_for") != snapshot_for.isoformat()
        or manifest.get("target_snapshot_for") != snapshot_for.isoformat()
        or receipt.get("first_available_at") != available_at.isoformat()
        or manifest.get("first_available_at") != available_at.isoformat()
    ):
        raise OptionSnapshotPublicationError(
            f"Provider-neutral option snapshot metadata is invalid: {run}"
        )
    return CommittedOptionSnapshot(
        provider=provider,
        dataset=dataset,
        symbol=symbol,
        snapshot_for=snapshot_for,
        available_at=available_at,
        directory=run,
        raw_path=run / "raw.parquet",
        contracts_path=run / "contracts.parquet",
        features_path=run / "option-quality.parquet",
        receipt_path=receipt_path,
        receipt=receipt,
        receipt_published_at=receipt_published_at,
        schema_version=version,
    )


def committed_option_snapshots(
    datastore_root: Path,
    *,
    symbol: str,
    provider: str = "schwab",
    available_not_after: object | None = None,
) -> tuple[CommittedOptionSnapshot, ...]:
    cutoff = (
        _utc(available_not_after, "available_not_after")
        if available_not_after is not None
        else None
    )
    clean_provider = _provider(provider)
    parent = option_snapshot_root(
        datastore_root,
        symbol=symbol,
        provider=clean_provider,
    )
    if not parent.is_dir():
        return ()
    committed: list[CommittedOptionSnapshot] = []
    for receipt_path in sorted(parent.glob(f"*/{OPTION_SNAPSHOT_RECEIPT_NAME}")):
        snapshot = read_option_snapshot(receipt_path.parent)
        if snapshot.provider != clean_provider:
            raise OptionSnapshotPublicationError(
                f"Option snapshot provider/path mismatch: {snapshot.directory}"
            )
        if cutoff is None or snapshot.available_at <= cutoff:
            committed.append(snapshot)
    return tuple(
        sorted(
            committed,
            key=lambda value: (value.available_at, value.snapshot_for),
        )
    )


def read_committed_option_surfaces(
    datastore_root: Path,
    *,
    symbols: Sequence[str],
    available_not_after: object,
    providers: Sequence[str] = ("databento-opra", "schwab"),
) -> tuple[pd.DataFrame, tuple[Path, ...]]:
    frames: list[pd.DataFrame] = []
    sources: list[Path] = []
    for symbol in dict.fromkeys(str(value).strip().upper() for value in symbols):
        selected_provider: str | None = None
        for raw_provider in providers:
            provider = _provider(raw_provider)
            snapshots = canonical_option_snapshots(
                datastore_root,
                symbol=symbol,
                provider=provider,
                available_not_after=available_not_after,
            )[0]
            if not snapshots:
                continue
            selected_provider = provider
            for snapshot in snapshots:
                frame = pd.read_parquet(snapshot.features_path)
                frame["provider"] = provider
                frame["evidence_lane"] = (
                    "PROSPECTIVE_OPRA"
                    if provider == "databento-opra"
                    else "PROSPECTIVE_SCHWAB"
                )
                frame["fallback_used"] = provider != "databento-opra"
                frames.append(frame)
                sources.extend((snapshot.features_path, snapshot.receipt_path))
            break
        _ = selected_provider
    return (
        pd.concat(frames, ignore_index=True, sort=False)
        if frames
        else pd.DataFrame(),
        tuple(dict.fromkeys(sources)),
    )


def canonical_option_snapshots(
    datastore_root: Path,
    *,
    symbol: str,
    provider: str = "schwab",
    available_not_after: object | None = None,
) -> tuple[tuple[CommittedOptionSnapshot, ...], Mapping[str, object]]:
    """Collapse publications to the earliest verified receipt per target.

    V1 used ``available_at`` as part of its natural key, so repeated captures
    of one target were legal and can contain different market observations.
    Those later captures remain immutable and readable but are diagnostic-only
    after the v2 target-key migration.  Divergent v2 publications, and
    divergent v1 publications with the same legacy key, still fail closed.
    """

    snapshots = committed_option_snapshots(
        datastore_root,
        symbol=symbol,
        provider=provider,
        available_not_after=available_not_after,
    )
    groups: dict[pd.Timestamp, list[CommittedOptionSnapshot]] = {}
    for snapshot in snapshots:
        groups.setdefault(snapshot.snapshot_for, []).append(snapshot)
    selected: list[CommittedOptionSnapshot] = []
    duplicate_count = 0
    legacy_divergent_count = 0
    for target, group in sorted(groups.items()):
        chosen, legacy_divergent = _canonical_snapshot_group(
            group,
            conflict_message=(
                "Conflicting duplicate option evidence for "
                f"{provider}/{symbol}/{target.isoformat()}"
            ),
        )
        selected.append(chosen)
        duplicate_count += max(0, len(group) - 1)
        legacy_divergent_count += legacy_divergent
    return tuple(selected), {
        "natural_key": ["provider", "symbol", "target_snapshot_for"],
        "provider": _provider(provider),
        "symbol": str(symbol).strip().upper(),
        "verified_publication_count": len(snapshots),
        "selected_natural_target_count": len(selected),
        "duplicate_publication_count": duplicate_count,
        "conflicting_publication_count": 0,
        "legacy_divergent_publication_count": legacy_divergent_count,
        "selection_policy": "earliest-verified-receipt-version-aware-v3",
    }


def _existing_natural_snapshot(
    datastore_root: Path,
    *,
    provider: str,
    symbol: str,
    target_snapshot_for: pd.Timestamp,
) -> CommittedOptionSnapshot | None:
    matches = [
        snapshot
        for snapshot in committed_option_snapshots(
            datastore_root,
            symbol=symbol,
            provider=provider,
        )
        if snapshot.snapshot_for == target_snapshot_for
    ]
    if not matches:
        return None
    selected, _legacy_divergent = _canonical_snapshot_group(
        matches,
        conflict_message=(
            "Existing option natural target contains divergent immutable evidence"
        ),
    )
    return selected


def _canonical_snapshot_group(
    snapshots: Sequence[CommittedOptionSnapshot],
    *,
    conflict_message: str,
) -> tuple[CommittedOptionSnapshot, int]:
    """Select one target while preserving the distinct v1 natural-key rule."""

    ordered = sorted(
        snapshots,
        key=lambda item: (_receipt_availability(item), item.directory.as_posix()),
    )
    selected = ordered[0]
    selected_hashes = _semantic_output_hashes(selected)
    legacy_hashes_by_availability: dict[
        pd.Timestamp, tuple[str, str, str]
    ] = {}
    if selected.schema_version == LEGACY_OPTION_SNAPSHOT_PUBLICATION_VERSION:
        legacy_hashes_by_availability[selected.available_at] = selected_hashes
    legacy_divergent_count = 0
    for candidate in ordered[1:]:
        candidate_hashes = _semantic_output_hashes(candidate)
        if candidate.schema_version == LEGACY_OPTION_SNAPSHOT_PUBLICATION_VERSION:
            legacy_reference = legacy_hashes_by_availability.setdefault(
                candidate.available_at,
                candidate_hashes,
            )
            if candidate_hashes != legacy_reference:
                raise OptionSnapshotPublicationError(conflict_message)
            if candidate_hashes != selected_hashes:
                legacy_divergent_count += 1
            continue
        if candidate_hashes != selected_hashes:
            raise OptionSnapshotPublicationError(conflict_message)
    return selected, legacy_divergent_count


def _verify_semantically_identical_retry(
    existing: CommittedOptionSnapshot,
    incoming: Mapping[str, pd.DataFrame],
) -> None:
    paths = {
        "raw.parquet": existing.raw_path,
        "contracts.parquet": existing.contracts_path,
        "option-quality.parquet": existing.features_path,
    }
    for name, path in paths.items():
        observed = pd.read_parquet(path)
        if _semantic_frame_fingerprint(observed) != _semantic_frame_fingerprint(
            incoming[name]
        ):
            raise OptionSnapshotPublicationError(
                "Divergent duplicate option evidence is forbidden for "
                f"{existing.provider}/{existing.symbol}/"
                f"{existing.snapshot_for.isoformat()} ({name})"
            )


def _semantic_output_hashes(
    snapshot: CommittedOptionSnapshot,
) -> tuple[str, str, str]:
    return tuple(
        _semantic_frame_fingerprint(pd.read_parquet(path))
        for path in (
            snapshot.raw_path,
            snapshot.contracts_path,
            snapshot.features_path,
        )
    )  # type: ignore[return-value]


def _semantic_frame_fingerprint(frame: pd.DataFrame) -> str:
    ignored = {
        "id",
        "available_at",
        "first_available_at",
        "fetched_at",
        "response_received_at",
        "provider_ingested_at",
        "import_receipt_at",
        "request_started_at",
        "receipt_at",
        "receipt_published_at",
        "decision_lag_seconds",
        "capture_provenance_json",
        "schema_version",
        "normalized_schema_version",
        "provider",
        "dataset",
        "target_snapshot_for",
    }
    normalized = frame.drop(
        columns=[column for column in ignored if column in frame.columns],
        errors="ignore",
    ).copy()
    # snapshot_for is the legacy spelling of the natural target and is also
    # ignored after the caller has matched that target.
    normalized = normalized.drop(columns=["snapshot_for"], errors="ignore")
    normalized = normalized.reindex(sorted(normalized.columns), axis=1)
    records = [
        {
            str(key): _semantic_json_value(value)
            for key, value in row.items()
        }
        for row in normalized.to_dict("records")
    ]
    records.sort(key=lambda row: json.dumps(row, sort_keys=True, default=str))
    from ml.artifacts import semantic_metadata_fingerprint

    return semantic_metadata_fingerprint({"rows": records})


def _semantic_json_value(value: object) -> object:
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, pd.Timestamp):
        return _utc(value, "semantic timestamp").isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    if hasattr(value, "item"):
        try:
            return _semantic_json_value(value.item())
        except (TypeError, ValueError):
            pass
    if isinstance(value, Mapping):
        return {
            str(key): _semantic_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_semantic_json_value(item) for item in value]
    return value


def _provider_neutral_frame(
    frame: pd.DataFrame,
    *,
    provider: str,
    dataset: str,
    symbol: str,
    target_snapshot_for: pd.Timestamp,
    available_at: pd.Timestamp,
) -> pd.DataFrame:
    output = frame.drop(columns=["id"], errors="ignore").copy()
    output["provider"] = provider
    output["dataset"] = dataset
    output["symbol"] = symbol
    output["underlying_symbol"] = symbol
    output["target_snapshot_for"] = target_snapshot_for
    output["snapshot_for"] = target_snapshot_for
    output["first_available_at"] = available_at
    output["available_at"] = available_at
    if "fetched_at" not in output:
        output["fetched_at"] = available_at
    if "provider_ingested_at" not in output:
        output["provider_ingested_at"] = output.get("fetched_at", available_at)
    output["normalized_schema_version"] = "option-market-evidence-v2"
    if "evidence_lane" not in output:
        output["evidence_lane"] = (
            "PROSPECTIVE_OPRA"
            if provider == "databento-opra"
            else "PROSPECTIVE_SCHWAB"
        )
    if "fallback_used" not in output:
        output["fallback_used"] = provider != "databento-opra"
    if "bid" in output and "ask" in output and "midpoint" not in output:
        bid = pd.to_numeric(output["bid"], errors="coerce")
        ask = pd.to_numeric(output["ask"], errors="coerce")
        output["midpoint"] = (bid + ask) / 2.0
    if "quote_timestamp" in output and "event_timestamp" not in output:
        output["event_timestamp"] = output["quote_timestamp"]
    if "last" in output and "trade_price" not in output:
        output["trade_price"] = output["last"]
    if "standard_contract" not in output and {
        "mini",
        "non_standard",
    }.issubset(output.columns):
        output["standard_contract"] = ~(
            output["mini"].fillna(False).astype(bool)
            | output["non_standard"].fillna(False).astype(bool)
        )
    if "adjusted" not in output and "non_standard" in output:
        output["adjusted"] = output["non_standard"]
    aliases = {
        "definition_as_of": pd.NaT,
        "exercise_style": None,
        "settlement_type": None,
        "settlement_reference": None,
        "publisher_id": None,
        "venue": None,
        "quote_quality_status": None,
        "trade_size": None,
        "source_file": None,
        "source_checksum_sha256": None,
        "policy_version": None,
    }
    for column, default in aliases.items():
        if column not in output:
            output[column] = default
    return output


def _coherent_key(
    symbol: str,
    frames: Iterable[pd.DataFrame],
) -> tuple[pd.Timestamp, pd.Timestamp]:
    keys: set[tuple[str, pd.Timestamp, pd.Timestamp]] = set()
    for frame in frames:
        target_column = (
            "target_snapshot_for"
            if "target_snapshot_for" in frame.columns
            else "snapshot_for"
        )
        available_column = (
            "first_available_at"
            if "first_available_at" in frame.columns
            else "available_at"
        )
        missing = [
            column
            for column in ("symbol", target_column, available_column)
            if column not in frame.columns
        ]
        if frame.empty or missing:
            raise ValueError(
                "Committed option snapshot requires non-empty coherent frames; "
                + ", ".join(missing)
            )
        symbols = frame["symbol"].astype("string").str.strip().str.upper().unique()
        snapshot_values = pd.to_datetime(
            frame[target_column], utc=True, errors="coerce"
        ).drop_duplicates()
        available_values = pd.to_datetime(
            frame[available_column], utc=True, errors="coerce"
        ).drop_duplicates()
        if len(symbols) != 1 or len(snapshot_values) != 1 or len(available_values) != 1:
            raise ValueError("Option snapshot frames do not share one receipt key")
        keys.add(
            (
                str(symbols[0]),
                pd.Timestamp(snapshot_values.iloc[0]),
                pd.Timestamp(available_values.iloc[0]),
            )
        )
    if len(keys) != 1:
        raise ValueError("Raw, normalized, and surface option files are incoherent")
    observed_symbol, snapshot_for, available_at = keys.pop()
    if observed_symbol != symbol:
        raise ValueError("Option snapshot symbol does not match publication target")
    return snapshot_for, available_at


def _publish_pointer(
    datastore_root: Path,
    snapshot: CommittedOptionSnapshot,
) -> None:
    pointer = option_snapshot_pointer_path(
        datastore_root,
        symbol=snapshot.symbol,
        provider=snapshot.provider,
    )
    if pointer.is_file():
        try:
            current = json.loads(pointer.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise OptionSnapshotPublicationError(
                f"Option snapshot pointer is unreadable: {pointer}"
            ) from exc
        if not isinstance(current, Mapping):
            raise OptionSnapshotPublicationError(
                f"Option snapshot pointer is malformed: {pointer}"
            )
        current_target = _utc(
            current.get("target_snapshot_for", current.get("snapshot_for")),
            "pointer target_snapshot_for",
        )
        current_available = _utc(
            current.get("first_available_at", current.get("available_at")),
            "pointer first_available_at",
        )
        if current_target > snapshot.snapshot_for or (
            current_target == snapshot.snapshot_for
            and current_available <= _receipt_availability(snapshot)
        ):
            return
    payload = {
        "schema_version": OPTION_SNAPSHOT_POINTER_VERSION,
        "provider": snapshot.provider,
        "dataset": snapshot.dataset,
        "symbol": snapshot.symbol,
        "target_snapshot_for": snapshot.snapshot_for.isoformat(),
        "snapshot_for": snapshot.snapshot_for.isoformat(),
        "first_available_at": snapshot.available_at.isoformat(),
        "available_at": snapshot.available_at.isoformat(),
        "receipt_published_at": (
            snapshot.receipt_published_at.isoformat()
            if snapshot.receipt_published_at is not None
            else snapshot.available_at.isoformat()
        ),
        "run_path": snapshot.directory.relative_to(Path(datastore_root)).as_posix(),
        "receipt_checksum_sha256": file_checksum(snapshot.receipt_path),
    }
    _write_json_atomic(pointer, payload)


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


def _remove_unpublished_staging(path: Path) -> None:
    if not path.is_dir() or ".tmp-" not in path.name:
        return
    for child in path.iterdir():
        if child.is_file():
            child.unlink(missing_ok=True)
    path.rmdir()


def _utc(value: object, label: str) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise OptionSnapshotPublicationError(f"Invalid option snapshot {label}")
    return pd.Timestamp(timestamp)


def _provider(value: object) -> str:
    provider = str(value or "").strip().lower()
    if provider not in SUPPORTED_OPTION_PROVIDERS:
        raise ValueError(
            "Option provider must be one of: "
            + ", ".join(SUPPORTED_OPTION_PROVIDERS)
        )
    return provider


def _receipt_availability(snapshot: CommittedOptionSnapshot) -> pd.Timestamp:
    return snapshot.receipt_published_at or snapshot.available_at


__all__ = [
    "CommittedOptionSnapshot",
    "LEGACY_OPTION_SNAPSHOT_PUBLICATION_VERSION",
    "OPTION_SNAPSHOT_PUBLICATION_VERSION",
    "OptionSnapshotPublicationError",
    "SUPPORTED_OPTION_PROVIDERS",
    "canonical_option_snapshots",
    "committed_option_snapshots",
    "option_snapshot_pointer_path",
    "option_writer_lock_path",
    "publish_option_snapshot",
    "read_committed_option_surfaces",
    "read_option_snapshot",
]
