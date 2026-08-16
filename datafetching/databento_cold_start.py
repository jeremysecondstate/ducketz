"""Safe, resumable Databento historical cold-start coordinator.

This command is deliberately separate from the seven production supervisors.
It writes only historical bootstrap evidence: OPRA uses the existing canonical
OPRA partition contract, while CME and US-equity data live under a distinct
``market-data/databento-cold-start`` archive.  It never writes Loop A readiness,
live snapshots, or any ML publication pointer.  After a checksum-verified OPRA
scope completes, it writes the v5 symbol/schema history cursor that hands later
overlap maintenance to the independent Options runtime; that cursor is not
snapshot or live-publication authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

from datafetching.cme_runtime import load_repository_environment
from datafetching.databento_history_policy import interval_lookback_policy
from datafetching.databento_opra_history import (
    DATASET as OPRA_DATASET,
    SyncScope,
    canonical_root as canonical_opra_root,
    synchronize as synchronize_opra,
)
from datafetching.options_runtime import publish_opra_symbol_history_cursor
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.artifacts import file_checksum


COORDINATOR_VERSION = "databento-cold-start-v2"
MANIFEST_VERSION = "databento-cold-start-manifest-v2"
PARTITION_VERSION = "databento-cold-start-partition-v1"
RECEIPT_VERSION = "databento-cold-start-receipt-v1"
PREFLIGHT_VERSION = "databento-cold-start-preflight-v2"
PROGRESS_VERSION = "databento-cold-start-progress-v1"
STORAGE_RESERVE_BYTES = 5 * 1024**3
STORAGE_EXPANSION_FACTOR = 2
DEFAULT_WATCHLIST = Path(__file__).resolve().parent / "watchlist.txt"
DEFAULT_EQUITIES_DATASET = "XNAS.ITCH"
COLD_START_EQUITIES_DATASET_ENV = "DATABENTO_COLD_START_EQUITIES_DATASET"
STANDARD_PLAN_AUTHORITY = "docs/databento-plan/databento_standard_plan_data_access.md"
PLAN_DATASET_OPRA = "OPRA"
PLAN_DATASET_CME = "CME"
PLAN_DATASET_US_EQUITIES = "US_EQUITIES"

OPRA_SCHEMAS = (
    "ohlcv-1s",
    "ohlcv-1m",
    "ohlcv-1h",
    "ohlcv-1d",
    "definition",
    "statistics",
    "status",
    "cmbp-1",
    "tcbbo",
    "cbbo-1s",
    "cbbo-1m",
    "trades",
)
CME_SCHEMAS = (
    "ohlcv-1s",
    "ohlcv-1m",
    "ohlcv-1h",
    "ohlcv-1d",
    "definition",
    "statistics",
    "status",
    "mbp-1",
    "tbbo",
    "bbo-1s",
    "bbo-1m",
    "trades",
    "mbp-10",
    "mbo",
)
US_EQUITIES_SCHEMAS = (
    "ohlcv-1s",
    "ohlcv-1m",
    "ohlcv-1h",
    "ohlcv-1d",
    "definition",
    "statistics",
    "status",
    "mbp-1",
    "tbbo",
    "bbo-1s",
    "bbo-1m",
    "trades",
    "mbp-10",
    "mbo",
    "imbalance",
)

_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9._-]*$")


class ColdStartError(RuntimeError):
    """A cold-start safety contract was not satisfied."""


@dataclass(frozen=True)
class CmeScope:
    symbol: str
    stype_in: str
    source: str


@dataclass(frozen=True)
class ColdStartRequest:
    request_id: str
    dataset: str
    standard_plan_dataset: str
    schema: str
    symbol_scope: tuple[str, ...]
    stype_in: str
    start: str
    end: str
    storage_path: str
    storage_contract: str
    window: Mapping[str, object]
    status: str = "PENDING"


def cold_start_root(datastore_root: Path) -> Path:
    return Path(datastore_root).resolve() / "market-data" / "databento-cold-start"


def coordinator_state_root(datastore_root: Path) -> Path:
    return Path(datastore_root).resolve() / "state" / "databento-cold-start"


def parse_watchlist(path: Path) -> tuple[str, ...]:
    """Read a direct-equity universe without silently accepting ambiguity."""

    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ColdStartError(f"Watchlist is unreadable: {path}") from exc
    symbols: list[str] = []
    seen: set[str] = set()
    for number, line in enumerate(lines, start=1):
        value = line.split("#", maxsplit=1)[0].strip().upper()
        if not value:
            continue
        if not _SYMBOL_RE.fullmatch(value) or value.endswith(".OPT"):
            raise ColdStartError(
                f"Watchlist line {number} is not an unambiguous direct equity symbol: {value!r}"
            )
        if value in seen:
            raise ColdStartError(f"Watchlist contains duplicate symbol {value!r}")
        seen.add(value)
        symbols.append(value)
    if not symbols:
        raise ColdStartError("Watchlist contains no direct equity symbols")
    return tuple(symbols)


def opra_parent_symbols(symbols: Iterable[str]) -> tuple[str, ...]:
    parents: list[str] = []
    seen: set[str] = set()
    for value in symbols:
        symbol = str(value).strip().upper()
        if not _SYMBOL_RE.fullmatch(symbol) or symbol.endswith(".OPT"):
            raise ColdStartError(f"Cannot construct an OPRA parent from {value!r}")
        parent = f"{symbol}.OPT"
        if parent in seen:
            raise ColdStartError(f"Ambiguous duplicate OPRA parent scope {parent!r}")
        seen.add(parent)
        parents.append(parent)
    if not parents:
        raise ColdStartError("At least one OPRA parent symbol is required")
    return tuple(parents)


def resolve_cme_scopes(
    environment: Mapping[str, str] | None = None,
    *,
    explicit_symbols: Sequence[str] = (),
    explicit_stype_in: str | None = None,
) -> tuple[CmeScope, ...]:
    """Use configured CME scopes only; never derive CME names from equities."""

    env = os.environ if environment is None else environment
    supplied = tuple(str(value).strip() for value in explicit_symbols if str(value).strip())
    if supplied:
        stype = str(explicit_stype_in or "").strip()
        if not stype or stype == "mixed":
            raise ColdStartError(
                "--cme-symbol requires one explicit non-mixed --cme-stype-in"
            )
        scopes = [CmeScope(_validate_cme_symbol(value), stype, "cli") for value in supplied]
    else:
        context_stype = str(env.get("DATABENTO_CME_CONTEXT_STYPE_IN", "")).strip() or "continuous"
        contract_stype = str(env.get("DATABENTO_CME_CONTRACT_STYPE_IN", "")).strip() or "raw_symbol"
        scopes = [
            CmeScope(_validate_cme_symbol(value), context_stype, "repository-context")
            for value in _split_configured_symbols(env.get("DATABENTO_CME_CONTEXT_SYMBOLS", ""))
        ]
        scopes.extend(
            CmeScope(_validate_cme_symbol(value), contract_stype, "repository-contract")
            for value in _split_configured_symbols(env.get("DATABENTO_CME_CONTRACT_SYMBOLS", ""))
        )
    if not scopes:
        raise ColdStartError(
            "CME cold-start requires explicit CME scope: configure "
            "DATABENTO_CME_CONTEXT_SYMBOLS or DATABENTO_CME_CONTRACT_SYMBOLS, "
            "or pass --cme-symbol with --cme-stype-in. Equity tickers are never CME symbols."
        )
    seen: dict[str, str] = {}
    for scope in scopes:
        previous = seen.get(scope.symbol)
        if previous is not None:
            detail = "different stype_in values" if previous != scope.stype_in else "duplicate entries"
            raise ColdStartError(
                f"Ambiguous CME scope for {scope.symbol!r}: {detail}"
            )
        seen[scope.symbol] = scope.stype_in
    return tuple(sorted(scopes, key=lambda item: (item.symbol, item.stype_in)))


def resolve_equities_dataset(explicit_dataset: str | None = None) -> str:
    """Resolve the cold archive without inheriting Loop A's live dataset."""

    dataset = str(
        explicit_dataset
        or os.environ.get(COLD_START_EQUITIES_DATASET_ENV, "")
        or DEFAULT_EQUITIES_DATASET
    ).strip()
    if not dataset:
        raise ColdStartError("US equities cold-start dataset is required")
    return dataset


def schema_window(standard_plan_dataset: str, schema: str) -> dict[str, object]:
    interval_policy = interval_lookback_policy(schema)
    if interval_policy is not None:
        return interval_policy
    if schema == "definition":
        if standard_plan_dataset == PLAN_DATASET_OPRA:
            return {"unit": "years", "value": 13}
        if standard_plan_dataset == PLAN_DATASET_US_EQUITIES:
            return {"unit": "years", "value": 8}
        if standard_plan_dataset == PLAN_DATASET_CME:
            return {"unit": "days", "value": 5_000}
        raise ColdStartError(
            f"Unknown Standard-plan dataset role: {standard_plan_dataset}"
        )
    return {"unit": "calendar_months", "value": 1}


def standard_plan_history_policy(
    standard_plan_dataset: str, schema: str
) -> dict[str, object]:
    if standard_plan_dataset == PLAN_DATASET_OPRA:
        if schema in {"ohlcv-1s", "ohlcv-1m", "ohlcv-1h", "ohlcv-1d", "definition", "statistics", "status"}:
            return {"unit": "years", "value": 13}
        if schema in {"cmbp-1", "tcbbo", "cbbo-1s", "cbbo-1m", "trades"}:
            return {"unit": "months", "value": 12}
    elif standard_plan_dataset == PLAN_DATASET_CME:
        if schema in {"ohlcv-1s", "ohlcv-1m", "ohlcv-1h", "ohlcv-1d", "definition", "statistics", "status"}:
            return {"unit": "years", "value": 16}
        if schema in {"mbp-1", "tbbo", "bbo-1s", "bbo-1m", "trades"}:
            return {"unit": "months", "value": 12}
        if schema in {"mbp-10", "mbo"}:
            return {"unit": "calendar_months", "value": 1}
    elif standard_plan_dataset == PLAN_DATASET_US_EQUITIES:
        if schema in {"ohlcv-1s", "ohlcv-1m", "ohlcv-1h", "ohlcv-1d", "definition", "statistics", "status"}:
            return {"unit": "years", "value": 8}
        if schema in {"mbp-1", "tbbo", "bbo-1s", "bbo-1m", "trades"}:
            return {"unit": "months", "value": 12}
        if schema in {"mbp-10", "mbo", "imbalance"}:
            return {"unit": "calendar_months", "value": 1}
    raise ColdStartError(
        f"No Standard-plan history policy for {standard_plan_dataset}/{schema}"
    )


def required_free_bytes(total_download_payload_bytes: int) -> int:
    if total_download_payload_bytes < 0:
        raise ValueError("Estimated download payload bytes cannot be negative")
    return (
        STORAGE_RESERVE_BYTES
        + STORAGE_EXPANSION_FACTOR * total_download_payload_bytes
    )


def discover_dataset_catalog(
    client: object,
    *,
    dataset: str,
    required_schemas: Sequence[str],
) -> Mapping[str, Mapping[str, str]]:
    """Read provider metadata and fail before requests for unsupported schemas."""

    metadata = getattr(client, "metadata", None)
    if metadata is None:
        raise ColdStartError("Databento client has no metadata endpoint")
    advertised = tuple(_metadata_call(metadata.list_schemas, dataset=dataset))
    missing = sorted(set(required_schemas).difference(str(item) for item in advertised))
    if missing:
        raise ColdStartError(
            f"Databento dataset {dataset} does not support required schemas: {', '.join(missing)}"
        )
    payload = _metadata_call(metadata.get_dataset_range, dataset=dataset)
    if not isinstance(payload, Mapping):
        raise ColdStartError(f"Databento returned malformed dataset range for {dataset}")
    schema_ranges = payload.get("schema")
    if not isinstance(schema_ranges, Mapping):
        raise ColdStartError(f"Databento returned no schema ranges for {dataset}")
    result: dict[str, Mapping[str, str]] = {}
    for schema in required_schemas:
        item = schema_ranges.get(schema)
        if not isinstance(item, Mapping):
            raise ColdStartError(f"Databento returned no range for {dataset}/{schema}")
        start = _as_date(item.get("start"))
        end = _as_date(item.get("end"))
        if start >= end:
            raise ColdStartError(f"Databento returned invalid range for {dataset}/{schema}")
        result[str(schema)] = {"start": start.isoformat(), "end": end.isoformat()}
    return result


def latest_common_available_date(
    catalogs: Mapping[str, Mapping[str, Mapping[str, str]]],
) -> date:
    """Return the latest exclusive date bound supported by every required schema."""

    ends: list[date] = []
    for dataset, schemas in catalogs.items():
        if not schemas:
            raise ColdStartError(f"Databento returned an empty schema catalog for {dataset}")
        for schema, bounds in schemas.items():
            try:
                ends.append(_as_date(bounds["end"]))
            except (KeyError, TypeError) as exc:
                raise ColdStartError(
                    f"Databento returned no end bound for {dataset}/{schema}"
                ) from exc
    if not ends:
        raise ColdStartError("Databento returned no common historical date bound")
    return min(ends)


def build_manifest(
    *,
    datastore_root: Path,
    equities_symbols: Sequence[str],
    cme_dataset: str,
    cme_scopes: Sequence[CmeScope],
    equities_dataset: str,
    as_of: date,
    catalogs: Mapping[str, Mapping[str, Mapping[str, str]]] | None = None,
) -> dict[str, object]:
    """Build a sorted, deterministic request manifest with no provider calls."""

    entries: list[ColdStartRequest] = []
    dataset_specs = (
        (
            OPRA_DATASET,
            PLAN_DATASET_OPRA,
            OPRA_SCHEMAS,
            opra_parent_symbols(equities_symbols),
            "parent",
            "canonical-opra",
        ),
        (
            cme_dataset,
            PLAN_DATASET_CME,
            CME_SCHEMAS,
            tuple(scope.symbol for scope in cme_scopes),
            "cme",
            "isolated-cold-start",
        ),
        (
            equities_dataset,
            PLAN_DATASET_US_EQUITIES,
            US_EQUITIES_SCHEMAS,
            tuple(equities_symbols),
            "raw_symbol",
            "isolated-cold-start",
        ),
    )
    cme_stypes = {scope.symbol: scope.stype_in for scope in cme_scopes}
    for (
        dataset,
        standard_plan_dataset,
        schemas,
        symbols,
        default_stype,
        contract,
    ) in dataset_specs:
        catalog = catalogs.get(dataset) if catalogs else None
        for schema in schemas:
            for symbol in symbols:
                bounds = catalog.get(schema) if catalog else None
                end = as_of
                window = schema_window(standard_plan_dataset, schema)
                start = _window_start(end, window)
                if bounds:
                    provider_start = _as_date(bounds["start"])
                    provider_end = _as_date(bounds["end"])
                    if provider_start > start or provider_end < end:
                        raise ColdStartError(
                            f"Provider range does not cover the configured included "
                            f"bootstrap for {dataset}/{schema}: requested="
                            f"{start.isoformat()}..{end.isoformat()} provider="
                            f"{provider_start.isoformat()}..{provider_end.isoformat()}"
                        )
                if start >= end:
                    raise ColdStartError(
                        f"No historical interval remains for {dataset}/{schema}/{symbol}"
                    )
                stype = cme_stypes[symbol] if default_stype == "cme" else default_stype
                storage = _entry_storage_path(
                    Path(datastore_root),
                    dataset=dataset,
                    schema=schema,
                    symbol=symbol,
                    start=start,
                    end=end,
                    contract=contract,
                )
                identity = {
                    "dataset": dataset,
                    "standard_plan_dataset": standard_plan_dataset,
                    "schema": schema,
                    "symbol_scope": [symbol],
                    "stype_in": stype,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "storage_contract": contract,
                    "window": window,
                }
                entries.append(
                    ColdStartRequest(
                        request_id=_checksum(identity)[:24],
                        storage_path=str(storage),
                        **identity,
                    )
                )
    entries.sort(key=lambda item: (item.dataset, item.schema, item.symbol_scope, item.stype_in))
    body: dict[str, object] = {
        "schema_version": MANIFEST_VERSION,
        "coordinator_version": COORDINATOR_VERSION,
        "as_of": as_of.isoformat(),
        "datastore_root": str(Path(datastore_root).resolve()),
        "entitlement_authority": STANDARD_PLAN_AUTHORITY,
        "requests": [asdict(item) for item in entries],
        "derived_views": [],
    }
    _validate_manifest_included_scope(body)
    manifest_id = _checksum(body)[:24]
    return {"manifest_id": manifest_id, **body, "semantic_checksum_sha256": _checksum(body)}


def write_manifest(datastore_root: Path, manifest: Mapping[str, object]) -> Path:
    manifest_id = str(manifest["manifest_id"])
    path = coordinator_state_root(datastore_root) / "manifests" / manifest_id / "manifest.json"
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ColdStartError(f"Existing cold-start manifest is unreadable: {path}") from exc
        if existing.get("semantic_checksum_sha256") != manifest.get("semantic_checksum_sha256"):
            raise ColdStartError(f"Manifest identity collision at {path}")
        return path
    _write_json_exclusive(path, manifest)
    return path


def _validate_manifest_included_scope(manifest: Mapping[str, object]) -> None:
    """Reject any cold-start request outside the documented included scope."""

    try:
        as_of = _as_date(manifest["as_of"])
        requests = manifest["requests"]
    except KeyError as exc:
        raise ColdStartError("Cold-start manifest lacks included-scope metadata") from exc
    if manifest.get("entitlement_authority") != STANDARD_PLAN_AUTHORITY:
        raise ColdStartError("Cold-start manifest has the wrong entitlement authority")
    if not isinstance(requests, list):
        raise ColdStartError("Cold-start manifest has no request list")
    for raw in requests:
        if not isinstance(raw, Mapping):
            raise ColdStartError("Cold-start manifest request is malformed")
        role = str(raw.get("standard_plan_dataset", ""))
        schema = str(raw.get("schema", ""))
        configured_window = schema_window(role, schema)
        if raw.get("window") != configured_window:
            raise ColdStartError(
                f"Cold-start request changed the configured bootstrap window for "
                f"{role}/{schema}"
            )
        start = _as_date(raw.get("start"))
        end = _as_date(raw.get("end"))
        expected_start = _window_start(as_of, configured_window)
        if start != expected_start or end != as_of:
            raise ColdStartError(
                f"Cold-start request does not match the configured bootstrap scope "
                f"for {role}/{schema}: requested={start.isoformat()}.."
                f"{end.isoformat()} expected={expected_start.isoformat()}.."
                f"{as_of.isoformat()}"
            )
        included_start = _window_start(
            as_of, standard_plan_history_policy(role, schema)
        )
        if start < included_start or end > as_of:
            raise ColdStartError(
                f"Cold-start request is outside the included Standard-plan scope "
                f"for {role}/{schema}: requested={start.isoformat()}.."
                f"{end.isoformat()} included={included_start.isoformat()}.."
                f"{as_of.isoformat()} authority={STANDARD_PLAN_AUTHORITY}"
            )


def preflight_manifest(
    client: object,
    *,
    datastore_root: Path,
    manifest: Mapping[str, object],
    disk_usage: Callable[[str | Path], Any] = shutil.disk_usage,
) -> dict[str, object]:
    """Use exact Databento metadata and calculate the required free capacity."""

    _validate_manifest_included_scope(manifest)
    metadata = getattr(client, "metadata", None)
    if metadata is None:
        raise ColdStartError("Databento client has no metadata endpoint")
    requests = manifest.get("requests")
    if not isinstance(requests, list):
        raise ColdStartError("Cold-start manifest has no request list")
    estimates: list[dict[str, object]] = []
    for raw in requests:
        if not isinstance(raw, Mapping):
            raise ColdStartError("Cold-start manifest request is malformed")
        kwargs = _request_kwargs(raw)
        try:
            estimated_download_size = int(
                _metadata_call(metadata.get_billable_size, **kwargs)
            )
            records = int(_metadata_call(metadata.get_record_count, **kwargs))
        except AttributeError as exc:
            raise ColdStartError(
                "Databento metadata does not support the required download-size "
                "and record-count capacity preflight"
            ) from exc
        if estimated_download_size < 0 or records < 0:
            raise ColdStartError(f"Databento returned negative preflight values for {raw['request_id']}")
        estimates.append(
            {
                "request_id": raw["request_id"],
                "dataset": raw["dataset"],
                "schema": raw["schema"],
                "symbol_scope": list(raw["symbol_scope"]),
                "start": raw["start"],
                "end": raw["end"],
                "record_count": records,
                "estimated_download_size_bytes": estimated_download_size,
            }
        )
    estimates.sort(key=lambda item: (str(item["dataset"]), str(item["schema"]), tuple(item["symbol_scope"])))
    total_download_size = sum(
        int(item["estimated_download_size_bytes"]) for item in estimates
    )
    total_records = sum(int(item["record_count"]) for item in estimates)
    usage = disk_usage(Path(datastore_root).resolve().anchor)
    required = required_free_bytes(total_download_size)
    return {
        "schema_version": PREFLIGHT_VERSION,
        "manifest_id": manifest["manifest_id"],
        "generated_at": _utc_now().isoformat(),
        "estimates": estimates,
        "total_estimated_download_size_bytes": total_download_size,
        "total_record_count": total_records,
        "storage_reserve_bytes": STORAGE_RESERVE_BYTES,
        "storage_expansion_factor": STORAGE_EXPANSION_FACTOR,
        "required_free_bytes": required,
        "available_free_bytes": int(usage.free),
        "capacity_pass": int(usage.free) >= required,
        "shortfall_bytes": max(required - int(usage.free), 0),
    }


def write_preflight(datastore_root: Path, preflight: Mapping[str, object]) -> Path:
    manifest_id = str(preflight["manifest_id"])
    timestamp = _utc_now().strftime("%Y%m%dT%H%M%S%fZ")
    path = coordinator_state_root(datastore_root) / "manifests" / manifest_id / f"preflight-{timestamp}.json"
    payload = dict(preflight)
    payload["semantic_checksum_sha256"] = _checksum(payload)
    _write_json_exclusive(path, payload)
    return path


def execute_manifest(
    client: object,
    *,
    datastore_root: Path,
    manifest: Mapping[str, object],
    preflight: Mapping[str, object],
    reporter: Callable[[str], None] | None = print,
) -> dict[str, int]:
    """Fetch only a preflighted manifest and retain durable progress receipts."""

    _validate_manifest_included_scope(manifest)
    if not bool(preflight.get("capacity_pass")):
        raise ColdStartError("Cold-start capacity preflight failed; no downloads were started")
    if preflight.get("manifest_id") != manifest.get("manifest_id"):
        raise ColdStartError("Preflight does not belong to this cold-start manifest")
    estimates = {
        str(item["request_id"]): item
        for item in preflight.get("estimates", [])
        if isinstance(item, Mapping)
    }
    requests = manifest.get("requests")
    if not isinstance(requests, list):
        raise ColdStartError("Cold-start manifest has no request list")
    progress_path = _progress_path(datastore_root, str(manifest["manifest_id"]))
    progress = _read_progress(progress_path, manifest_id=str(manifest["manifest_id"]))
    counts = {"verified": 0, "downloaded": 0, "no_data": 0, "failed": 0}
    for raw in requests:
        if not isinstance(raw, Mapping):
            raise ColdStartError("Cold-start manifest request is malformed")
        request_id = str(raw["request_id"])
        estimate = estimates.get(request_id)
        if estimate is None:
            raise ColdStartError(f"Preflight is incomplete for request {request_id}")
        if int(estimate["record_count"]) == 0:
            _update_progress(progress, raw, status="NO_DATA_VERIFIED", details={"preflight": estimate})
            _write_progress(progress_path, progress)
            _write_request_cursor(
                datastore_root,
                manifest_id=str(manifest["manifest_id"]),
                request=raw,
                status="NO_DATA_VERIFIED",
            )
            counts["no_data"] += 1
            if reporter:
                reporter(f"NO_DATA_VERIFIED {raw['dataset']}/{raw['schema']}/{raw['symbol_scope'][0]}")
            continue
        try:
            if _entry_is_verified(datastore_root, raw):
                status = "VERIFIED_EXISTING"
                counts["verified"] += 1
            elif raw["storage_contract"] == "canonical-opra":
                _execute_opra_entry(client, datastore_root=datastore_root, request=raw, manifest_id=str(manifest["manifest_id"]), reporter=reporter)
                status = "PUBLISHED"
                counts["downloaded"] += 1
            else:
                _download_generic_entry(client, datastore_root=datastore_root, request=raw)
                status = "PUBLISHED"
                counts["downloaded"] += 1
            _update_progress(progress, raw, status=status, details={"preflight": estimate})
            _write_progress(progress_path, progress)
            _write_request_cursor(
                datastore_root,
                manifest_id=str(manifest["manifest_id"]),
                request=raw,
                status=status,
            )
            if reporter:
                reporter(f"{status} {raw['dataset']}/{raw['schema']}/{raw['symbol_scope'][0]}")
        except Exception as exc:
            _update_progress(
                progress,
                raw,
                status="FAILED",
                details={"error": f"{type(exc).__name__}: {exc}", "preflight": estimate},
            )
            _write_progress(progress_path, progress)
            counts["failed"] += 1
            if reporter:
                reporter(f"FAILED {raw['dataset']}/{raw['schema']}/{raw['symbol_scope'][0]}: {exc}")
    if counts["failed"]:
        raise ColdStartError(
            f"Cold-start incomplete: {counts['failed']} request(s) failed; rerun the same manifest to resume"
        )
    return counts


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Plan, preflight, and explicitly execute the isolated Databento historical cold-start. "
            "No downloads occur unless both --execute and --confirm-download are supplied."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Print the deterministic request plan without credentials or network access.")
    mode.add_argument("--preflight", action="store_true", help="Fetch only provider metadata, write the manifest/preflight receipt, and check disk capacity.")
    mode.add_argument("--execute", action="store_true", help="Re-preflight, then download and publish verified historical partitions.")
    parser.add_argument(
        "--confirm-download",
        action="store_true",
        help="Required with --execute to confirm the included historical transfer.",
    )
    parser.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST)
    parser.add_argument("--as-of", type=_parse_date, default=None, help="Exclusive UTC date bound (YYYY-MM-DD); defaults to today.")
    parser.add_argument(
        "--equities-dataset",
        default=None,
        help=(
            "Cold-archive dataset; defaults to "
            "DATABENTO_COLD_START_EQUITIES_DATASET or XNAS.ITCH."
        ),
    )
    parser.add_argument("--cme-dataset", default=None, help="Defaults to required DATABENTO_CME_DATASET.")
    parser.add_argument("--cme-symbol", action="append", default=[], help="Explicit CME symbol/root. Repeat for multiple symbols.")
    parser.add_argument("--cme-stype-in", default=None, help="Required with --cme-symbol; for example continuous or raw_symbol.")
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path, default=None)
    datastore.add_argument("--datastore-target", choices=tuple(DATASTORE_TARGETS), default=None)
    args = parser.parse_args(argv)
    if args.confirm_download and not args.execute:
        parser.error("--confirm-download is only valid with --execute")
    if args.execute and not args.confirm_download:
        parser.error("--execute requires --confirm-download; no downloads were started")

    try:
        load_repository_environment()
        root = resolve_datastore_dir(root_dir=args.datastore, target=args.datastore_target)
        equities = parse_watchlist(args.watchlist)
        cme_scopes = resolve_cme_scopes(
            explicit_symbols=args.cme_symbol,
            explicit_stype_in=args.cme_stype_in,
        )
        cme_dataset = str(args.cme_dataset or os.environ.get("DATABENTO_CME_DATASET", "")).strip()
        if not cme_dataset:
            raise ColdStartError(
                "CME cold-start requires DATABENTO_CME_DATASET or --cme-dataset before any request"
            )
        equities_dataset = resolve_equities_dataset(args.equities_dataset)
        if args.dry_run:
            as_of = args.as_of or _utc_now().date()
            manifest = build_manifest(
                datastore_root=root,
                equities_symbols=equities,
                cme_dataset=cme_dataset,
                cme_scopes=cme_scopes,
                equities_dataset=equities_dataset,
                as_of=as_of,
            )
            _print_dry_run(manifest)
            return 0

        api_key = os.environ.get("DATABENTO_API_KEY", "").strip()
        if not api_key:
            raise ColdStartError("DATABENTO_API_KEY is required; no Databento request was started")
        import databento as db

        client = db.Historical(api_key)
        catalogs = {
            OPRA_DATASET: discover_dataset_catalog(client, dataset=OPRA_DATASET, required_schemas=OPRA_SCHEMAS),
            cme_dataset: discover_dataset_catalog(client, dataset=cme_dataset, required_schemas=CME_SCHEMAS),
            equities_dataset: discover_dataset_catalog(client, dataset=equities_dataset, required_schemas=US_EQUITIES_SCHEMAS),
        }
        as_of = args.as_of or latest_common_available_date(catalogs)
        manifest = build_manifest(
            datastore_root=root,
            equities_symbols=equities,
            cme_dataset=cme_dataset,
            cme_scopes=cme_scopes,
            equities_dataset=equities_dataset,
            as_of=as_of,
            catalogs=catalogs,
        )
        with exclusive_runtime_lock(
            Path(root).resolve() / ".ducketz-databento-cold-start.lock",
            process_name="Duckets Databento cold-start coordinator",
        ):
            manifest_path = write_manifest(root, manifest)
            preflight = preflight_manifest(client, datastore_root=root, manifest=manifest)
            preflight_path = write_preflight(root, preflight)
            _print_preflight(manifest, preflight, manifest_path=manifest_path, preflight_path=preflight_path)
            if not bool(preflight["capacity_pass"]):
                return 2
            if args.preflight:
                return 0
            counts = execute_manifest(client, datastore_root=root, manifest=manifest, preflight=preflight)
            print("Cold-start completed: " + "; ".join(f"{key}={value}" for key, value in counts.items()))
            return 0
    except ColdStartError as exc:
        print(f"Databento cold-start blocked: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        print(f"Databento cold-start failed closed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def _execute_opra_entry(
    client: object,
    *,
    datastore_root: Path,
    request: Mapping[str, object],
    manifest_id: str,
    reporter: Callable[[str], None] | None,
) -> None:
    schema = str(request["schema"])
    symbol = str(request["symbol_scope"][0])
    catalog = {
        "entitlements": {
            schema: {
                "level": "historical",
                "dataset_start": str(request["start"]),
                "entitled_start": str(request["start"]),
                "entitled_end": str(request["end"]),
            }
        }
    }
    # The existing OPRA writer owns the canonical path and its history sync
    # lock. This preserves Options Capture's forward-maintenance authority.
    with exclusive_runtime_lock(
        canonical_opra_root(datastore_root) / "state" / "sync.lock",
        process_name="Duckets cold-start OPRA history synchronizer",
    ):
        result = synchronize_opra(
            client,
            datastore_root=datastore_root,
            entitlement=catalog,
            scope=SyncScope(
                schemas=(schema,),
                start=str(request["start"]),
                end=str(request["end"]),
                symbols=(symbol,),
            ),
            reporter=reporter,
        )
        if result.errors or result.completed_rows < 1:
            raise ColdStartError(
                f"Canonical OPRA synchronization was not completely verified for {schema}/{symbol}; "
                f"errors={len(result.errors)} rows={result.completed_rows}"
            )
        underlying = symbol.removesuffix(".OPT")
        cursor_policy = dict(request["window"])
        if cursor_policy.get("unit") == "calendar_months":
            cursor_policy["unit"] = "months"
        publish_opra_symbol_history_cursor(
            datastore_root,
            symbol=underlying,
            schema=schema,
            requested_start=str(request["start"]),
            completed_through=str(request["end"]),
            lookback_policy=cursor_policy,
            bootstrap_manifest_id=manifest_id,
        )


def _download_generic_entry(client: object, *, datastore_root: Path, request: Mapping[str, object]) -> None:
    destination = Path(str(request["storage_path"]))
    if destination.exists():
        _verify_generic_partition(destination, request)
        return
    staging = cold_start_root(datastore_root) / ".staging" / f"{request['request_id']}-{uuid.uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=False)
    raw_path = staging / "provider.dbn.zst"
    parquet_path = staging / "normalized.parquet"
    kwargs = _request_kwargs(request)
    kwargs["path"] = raw_path
    try:
        store = getattr(client, "timeseries").get_range(**kwargs)
        if not raw_path.is_file() or raw_path.stat().st_size == 0:
            raise ColdStartError("Databento did not produce a provider-native DBN file")
        if store is None:
            import databento as db

            store = db.DBNStore.from_file(raw_path)
        store.to_parquet(parquet_path, map_symbols=True)
        if not parquet_path.is_file():
            raise ColdStartError("Databento DBN normalization produced no Parquet")
        validation = _validate_generic_parquet(parquet_path, request)
        manifest = {
            "schema_version": PARTITION_VERSION,
            "coordinator_version": COORDINATOR_VERSION,
            "request": dict(request),
            "published_at": _utc_now().isoformat(),
            "raw": {
                "path": raw_path.name,
                "size_bytes": raw_path.stat().st_size,
                "checksum_sha256": file_checksum(raw_path),
            },
            "normalized": {
                "path": parquet_path.name,
                "size_bytes": parquet_path.stat().st_size,
                "checksum_sha256": file_checksum(parquet_path),
                **validation,
            },
        }
        manifest_path = staging / "manifest.json"
        _write_json_exclusive(manifest_path, manifest)
        receipt = {
            "schema_version": RECEIPT_VERSION,
            "request_id": request["request_id"],
            "manifest_checksum_sha256": file_checksum(manifest_path),
            "raw_checksum_sha256": manifest["raw"]["checksum_sha256"],
            "normalized_checksum_sha256": manifest["normalized"]["checksum_sha256"],
            "published_at": manifest["published_at"],
        }
        _write_json_exclusive(staging / "receipt.json", receipt)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            _verify_generic_partition(destination, request)
            return
        staging.replace(destination)
        _verify_generic_partition(destination, request)
    except Exception:
        # Failed staging is retained for operator inspection and never becomes
        # consumer authority.
        raise


def _entry_is_verified(datastore_root: Path, request: Mapping[str, object]) -> bool:
    if request["storage_contract"] == "canonical-opra":
        # Re-run the canonical synchronizer on resume.  It verifies each stored
        # partition before skipping it and catches any damaged receipt.
        return False
    destination = Path(str(request["storage_path"]))
    if not destination.is_dir():
        return False
    _verify_generic_partition(destination, request)
    return True


def _verify_generic_partition(directory: Path, request: Mapping[str, object]) -> None:
    try:
        manifest_path = directory / "manifest.json"
        receipt_path = directory / "receipt.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ColdStartError(f"Cold-start partition metadata is unreadable: {directory}") from exc
    if (
        manifest.get("schema_version") != PARTITION_VERSION
        or receipt.get("schema_version") != RECEIPT_VERSION
        or receipt.get("request_id") != request.get("request_id")
        or receipt.get("manifest_checksum_sha256") != file_checksum(manifest_path)
        or manifest.get("request") != dict(request)
    ):
        raise ColdStartError(f"Cold-start partition receipt does not match request: {directory}")
    for name in ("raw", "normalized"):
        info = manifest.get(name)
        if not isinstance(info, Mapping):
            raise ColdStartError(f"Cold-start partition lacks {name} evidence: {directory}")
        path = directory / str(info.get("path"))
        if (
            not path.is_file()
            or path.stat().st_size != int(info.get("size_bytes", -1))
            or file_checksum(path) != info.get("checksum_sha256")
        ):
            raise ColdStartError(f"Cold-start {name} checksum verification failed: {directory}")
    validation = _validate_generic_parquet(directory / str(manifest["normalized"]["path"]), request)
    for key in ("row_count", "earliest_timestamp", "latest_timestamp", "timestamp_column"):
        if validation.get(key) != manifest["normalized"].get(key):
            raise ColdStartError(f"Cold-start normalized verification changed {key}: {directory}")


def _validate_generic_parquet(path: Path, request: Mapping[str, object]) -> dict[str, object]:
    parquet = pq.ParquetFile(path)
    columns = tuple(parquet.schema_arrow.names)
    timestamp_column = next((name for name in ("ts_recv", "ts_event") if name in columns), None)
    if timestamp_column is None:
        raise ColdStartError("Normalized Databento Parquet has no event/receive timestamp")
    row_count = int(parquet.metadata.num_rows)
    if row_count < 1:
        raise ColdStartError("Normalized Databento Parquet is empty despite nonzero preflight count")
    earliest: pd.Timestamp | None = None
    latest: pd.Timestamp | None = None
    for batch in parquet.iter_batches(batch_size=250_000, columns=[timestamp_column]):
        column = batch.column(0)
        if len(column) == column.null_count:
            continue
        minimum = pd.to_datetime(pc.min(column).as_py(), utc=True, errors="coerce")
        maximum = pd.to_datetime(pc.max(column).as_py(), utc=True, errors="coerce")
        if not pd.isna(minimum):
            earliest = minimum if earliest is None else min(earliest, minimum)
        if not pd.isna(maximum):
            latest = maximum if latest is None else max(latest, maximum)
    if earliest is None or latest is None:
        raise ColdStartError("Normalized Databento Parquet has no valid timestamps")
    start = pd.Timestamp(str(request["start"]), tz="UTC")
    end = pd.Timestamp(str(request["end"]), tz="UTC")
    if earliest < start or latest >= end:
        raise ColdStartError("Normalized Databento timestamps escape the requested interval")
    return {
        "row_count": row_count,
        "columns": list(columns),
        "timestamp_column": timestamp_column,
        "earliest_timestamp": earliest.isoformat(),
        "latest_timestamp": latest.isoformat(),
    }


def _entry_storage_path(
    datastore_root: Path,
    *,
    dataset: str,
    schema: str,
    symbol: str,
    start: date,
    end: date,
    contract: str,
) -> Path:
    if contract == "canonical-opra":
        return canonical_opra_root(datastore_root) / f"schema={schema}"
    scope = _checksum({"symbol": symbol})[:16]
    return (
        cold_start_root(datastore_root)
        / "archive-v1"
        / f"dataset={_safe_token(dataset)}"
        / f"schema={_safe_token(schema)}"
        / f"scope={scope}"
        / f"window={start.isoformat()}--{end.isoformat()}"
    )


def _request_kwargs(request: Mapping[str, object]) -> dict[str, object]:
    return {
        "dataset": request["dataset"],
        "schema": request["schema"],
        "symbols": list(request["symbol_scope"]),
        "stype_in": request["stype_in"],
        "start": request["start"],
        "end": request["end"],
    }


def _progress_path(datastore_root: Path, manifest_id: str) -> Path:
    return coordinator_state_root(datastore_root) / "manifests" / manifest_id / "progress.json"


def _write_request_cursor(
    datastore_root: Path,
    *,
    manifest_id: str,
    request: Mapping[str, object],
    status: str,
) -> Path:
    """Record completed request identity without becoming a live-loop cursor."""

    path = coordinator_state_root(datastore_root) / "cursors" / f"{request['request_id']}.json"
    payload = {
        "schema_version": "databento-cold-start-request-cursor-v1",
        "manifest_id": manifest_id,
        "request_id": request["request_id"],
        "dataset": request["dataset"],
        "schema": request["schema"],
        "symbol_scope": list(request["symbol_scope"]),
        "start": request["start"],
        "end": request["end"],
        "storage_path": request["storage_path"],
        "status": status,
        "completed_at": _utc_now().isoformat(),
    }
    _write_json_atomic(path, payload)
    return path


def _read_progress(path: Path, *, manifest_id: str) -> dict[str, object]:
    if not path.is_file():
        return {"schema_version": PROGRESS_VERSION, "manifest_id": manifest_id, "entries": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ColdStartError(f"Cold-start progress is unreadable: {path}") from exc
    if payload.get("schema_version") != PROGRESS_VERSION or payload.get("manifest_id") != manifest_id:
        raise ColdStartError(f"Cold-start progress identity is invalid: {path}")
    if not isinstance(payload.get("entries"), dict):
        raise ColdStartError(f"Cold-start progress entries are invalid: {path}")
    return payload


def _update_progress(
    progress: dict[str, object], request: Mapping[str, object], *, status: str, details: Mapping[str, object]
) -> None:
    entries = progress["entries"]
    assert isinstance(entries, dict)
    entries[str(request["request_id"])] = {
        "dataset": request["dataset"],
        "schema": request["schema"],
        "symbol_scope": list(request["symbol_scope"]),
        "status": status,
        "updated_at": _utc_now().isoformat(),
        "details": dict(details),
    }


def _write_progress(path: Path, progress: Mapping[str, object]) -> None:
    _write_json_atomic(path, progress)


def _metadata_call(function: Callable[..., object], **kwargs: object) -> object:
    try:
        return function(**kwargs)
    except Exception as exc:
        raise ColdStartError(
            f"Databento metadata request failed: {type(exc).__name__}: {exc}"
        ) from exc


def _window_start(end: date, policy: Mapping[str, object]) -> date:
    amount = int(policy["value"])
    if policy["unit"] == "days":
        return (pd.Timestamp(end) - pd.Timedelta(days=amount)).date()
    if policy["unit"] == "years":
        return (pd.Timestamp(end) - pd.DateOffset(years=amount)).date()
    if policy["unit"] in {"months", "calendar_months"}:
        return (pd.Timestamp(end) - pd.DateOffset(months=amount)).date()
    raise ColdStartError(f"Unsupported cold-start window policy: {policy}")


def _split_configured_symbols(value: str) -> tuple[str, ...]:
    text = str(value).strip()
    if not text:
        return ()
    if text.startswith("["):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ColdStartError("CME symbol configuration looks like malformed JSON") from exc
        if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
            raise ColdStartError("CME JSON symbol configuration must be an array of strings")
        return tuple(item.strip() for item in decoded if item.strip())
    return tuple(part.strip() for part in re.split(r"[\s,;]+", text) if part.strip())


def _validate_cme_symbol(value: str) -> str:
    symbol = str(value).strip()
    if not symbol or not re.fullmatch(r"[A-Za-z0-9._+\-/]+", symbol):
        raise ColdStartError(f"Invalid explicit CME symbol/root: {value!r}")
    return symbol


def _as_date(value: object) -> date:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        raise ColdStartError(f"Invalid Databento date bound: {value!r}")
    return parsed.date()


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use YYYY-MM-DD for --as-of") from exc


def _safe_token(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-") or "unknown"


def _checksum(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_json_exclusive(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(dict(payload), handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}-{uuid.uuid4().hex}")
    _write_json_exclusive(temporary, payload)
    temporary.replace(path)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _print_dry_run(manifest: Mapping[str, object]) -> None:
    print("DATABENTO COLD-START DRY RUN")
    print(f"Manifest: {manifest['manifest_id']}; requests={len(manifest['requests'])}")
    for item in manifest["requests"]:
        print(
            f"PENDING {item['dataset']} {item['schema']} {item['symbol_scope'][0]} "
            f"{item['start']}..{item['end']} -> {item['storage_path']}"
        )


def _print_preflight(
    manifest: Mapping[str, object],
    preflight: Mapping[str, object],
    *,
    manifest_path: Path,
    preflight_path: Path,
) -> None:
    print("DATABENTO COLD-START PREFLIGHT")
    print(f"Manifest: {manifest_path}")
    print(f"Preflight: {preflight_path}")
    for item in preflight["estimates"]:
        print(
            f"{item['dataset']} {item['schema']} {item['symbol_scope'][0]} "
            f"records={item['record_count']} "
            f"estimated_download_bytes={item['estimated_download_size_bytes']}"
        )
    print(
        "Totals: "
        f"records={preflight['total_record_count']} "
        f"estimated_download_bytes="
        f"{preflight['total_estimated_download_size_bytes']} "
        f"required_free_bytes={preflight['required_free_bytes']} "
        f"available_free_bytes={preflight['available_free_bytes']} "
        f"capacity_pass={preflight['capacity_pass']}"
    )


__all__ = [
    "CME_SCHEMAS",
    "OPRA_SCHEMAS",
    "US_EQUITIES_SCHEMAS",
    "ColdStartError",
    "CmeScope",
    "build_manifest",
    "discover_dataset_catalog",
    "execute_manifest",
    "latest_common_available_date",
    "main",
    "opra_parent_symbols",
    "parse_watchlist",
    "preflight_manifest",
    "required_free_bytes",
    "resolve_cme_scopes",
    "resolve_equities_dataset",
    "schema_window",
    "standard_plan_history_policy",
    "write_manifest",
]


if __name__ == "__main__":
    raise SystemExit(main())
