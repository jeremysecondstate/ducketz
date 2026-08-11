from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Mapping, Sequence

import pandas as pd
import requests

from datafetching.fred_vintages import (
    ALFRED_VINTAGE_AVAILABILITY_BASIS,
    FRED_VINTAGE_COLUMNS,
    FRED_VINTAGE_NATURAL_KEY,
    derive_alfred_rate_release_features,
    normalize_fred_vintage_rows,
    persist_fred_vintages,
    persist_macro_release_features,
)
from datafetching.ids import add_readable_id
from ml.artifacts import file_checksum, file_inventory, utc_timestamp

FRED_API_BASE_URL = "https://api.stlouisfed.org/fred"
FRED_ALFRED_IMPORT_VERSION = "fred-alfred-vintage-import-v1"
FRED_ALFRED_RECEIPT_NAME = "receipt.json"
FRED_ALFRED_MANIFEST_NAME = "manifest.json"
FRED_ALFRED_RAW_NAME = "provider-responses.json"
FRED_ALFRED_PARQUET_NAME = "vintages.parquet"
FRED_ALFRED_SUPPORTED_SERIES = ("FEDFUNDS", "CPIAUCSL", "UNRATE", "GDP")
FRED_ALFRED_OUTPUT_TYPE = 1
FRED_ALFRED_RELEASE_TIME_PRECISION = "DATE"
FRED_ALFRED_RELEASE_TIME_POLICY = (
    "provider realtime_start/vintage date; available only at the next "
    "America/Chicago midnight"
)
_API_KEY_PATTERN = re.compile(r"^[a-z0-9]{32}$")


class FredVintageImportError(RuntimeError):
    pass


@dataclass(frozen=True)
class FredVintageImportResult:
    evidence_directory: Path
    row_count: int
    series_count: int
    vintage_partition_paths: tuple[Path, ...]
    rate_feature_paths: tuple[Path, ...]


class FredAlfredClient:
    """Small credential-redacting client for FRED API v1 real-time periods."""

    def __init__(
        self,
        api_key: str,
        *,
        session: requests.Session | None = None,
        timeout_seconds: float = 30.0,
        maximum_attempts: int = 3,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not _API_KEY_PATTERN.fullmatch(str(api_key or "")):
            raise FredVintageImportError(
                "FRED_API_KEY must be a securely configured 32-character "
                "lowercase alphanumeric credential"
            )
        if timeout_seconds <= 0 or maximum_attempts < 1:
            raise ValueError("FRED client bounds must be positive")
        self._api_key = str(api_key)
        self._session = session or requests.Session()
        self._timeout_seconds = float(timeout_seconds)
        self._maximum_attempts = int(maximum_attempts)
        self._sleeper = sleeper

    def get_json(
        self,
        endpoint: str,
        *,
        params: Mapping[str, object],
    ) -> Mapping[str, object]:
        clean_endpoint = str(endpoint).strip().strip("/")
        if clean_endpoint not in {
            "series",
            "series/vintagedates",
            "series/observations",
        }:
            raise FredVintageImportError("Unsupported FRED/ALFRED endpoint")
        url = f"{FRED_API_BASE_URL}/{clean_endpoint}"
        request_params = {**dict(params), "api_key": self._api_key}
        for attempt in range(1, self._maximum_attempts + 1):
            try:
                response = self._session.get(
                    url,
                    params=request_params,
                    timeout=self._timeout_seconds,
                )
                status_code = int(getattr(response, "status_code", 200))
            except Exception:
                if attempt < self._maximum_attempts:
                    self._sleeper(float(attempt))
                    continue
                raise FredVintageImportError(
                    f"FRED/ALFRED request failed for {clean_endpoint}"
                ) from None
            if status_code == 429 or 500 <= status_code < 600:
                if attempt < self._maximum_attempts:
                    self._sleeper(float(attempt))
                    continue
            if status_code < 200 or status_code >= 300:
                raise FredVintageImportError(
                    "FRED/ALFRED request failed for "
                    f"{clean_endpoint} with HTTP {status_code}"
                )
            try:
                payload = response.json()
            except Exception:
                raise FredVintageImportError(
                    f"FRED/ALFRED returned invalid JSON for {clean_endpoint}"
                ) from None
            if not isinstance(payload, Mapping):
                raise FredVintageImportError(
                    f"FRED/ALFRED returned malformed JSON for {clean_endpoint}"
                )
            return payload
        raise AssertionError("FRED request retry loop exited unexpectedly")

    def assert_secret_free(self, payload: object) -> None:
        _assert_secret_free(payload, secret=self._api_key)


def import_fred_alfred_vintages(
    datastore_root: Path,
    *,
    client: FredAlfredClient,
    series_ids: Sequence[str],
    realtime_start: object,
    realtime_end: object,
    observation_start: object,
    observation_end: object,
    acquired_at: object | None = None,
) -> FredVintageImportResult:
    """Fetch, receipt, verify, and materialize exact ALFRED vintage intervals."""

    root = Path(datastore_root).resolve()
    acquired = utc_timestamp(acquired_at)
    request = _validated_request(
        series_ids=series_ids,
        realtime_start=realtime_start,
        realtime_end=realtime_end,
        observation_start=observation_start,
        observation_end=observation_end,
        acquired_at=acquired,
    )
    vintages, provider_responses, provider_summary = _fetch_vintages(
        client,
        request=request,
        acquired_at=acquired,
    )
    parent = root / "ml" / "option-pricing-evidence" / "fred-alfred-vintages"
    parent.mkdir(parents=True, exist_ok=True)
    destination = _unused_timestamp_directory(parent, acquired)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.tmp-{os.getpid()}-",
            dir=parent,
        )
    )
    try:
        client.assert_secret_free(provider_responses)
        _write_json(staging / FRED_ALFRED_RAW_NAME, provider_responses)
        persisted = add_readable_id(
            vintages,
            key_columns=FRED_VINTAGE_NATURAL_KEY,
        )
        persisted.to_parquet(staging / FRED_ALFRED_PARQUET_NAME, index=False)
        outputs = file_inventory(
            staging,
            (FRED_ALFRED_RAW_NAME, FRED_ALFRED_PARQUET_NAME),
        )
        manifest = {
            "schema_version": FRED_ALFRED_IMPORT_VERSION,
            "acquired_at": acquired.isoformat(),
            "request": request,
            "provider": "Federal Reserve Bank of St. Louis FRED/ALFRED API v1",
            "provider_endpoint": "fred/series/observations",
            "provider_output_type": FRED_ALFRED_OUTPUT_TYPE,
            "provider_realtime_period_semantics": "closed-closed",
            "provider_summary": provider_summary,
            "release_time_precision": FRED_ALFRED_RELEASE_TIME_PRECISION,
            "release_time_policy": FRED_ALFRED_RELEASE_TIME_POLICY,
            "availability_basis": ALFRED_VINTAGE_AVAILABILITY_BASIS,
            "local_acquisition_column": "fetched_at",
            "historical_availability_column": "available_at",
            "current_revised_history_used": False,
            "historical_coverage_status": "NOT_EVALUATED",
            "row_count": len(vintages),
            "outputs": outputs,
            "automated_action_allowed": False,
        }
        client.assert_secret_free(manifest)
        _write_json(staging / FRED_ALFRED_MANIFEST_NAME, manifest)
        receipt = {
            "schema_version": FRED_ALFRED_IMPORT_VERSION,
            "imported_at": acquired.isoformat(),
            "run_path": destination.relative_to(root).as_posix(),
            "manifest_checksum_sha256": file_checksum(
                staging / FRED_ALFRED_MANIFEST_NAME
            ),
            "row_count": len(vintages),
            "series": list(request["series"]),
            "availability_basis": ALFRED_VINTAGE_AVAILABILITY_BASIS,
            "current_revised_history_used": False,
            "historical_coverage_status": "NOT_EVALUATED",
            "automated_action_allowed": False,
        }
        client.assert_secret_free(receipt)
        _write_json(staging / FRED_ALFRED_RECEIPT_NAME, receipt)
        staging.replace(destination)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    verified = read_fred_alfred_vintage_import(
        destination,
        datastore_root=root,
    )
    verified_vintages = verified["vintages"]
    if not isinstance(verified_vintages, pd.DataFrame):
        raise FredVintageImportError("Verified ALFRED vintage frame is invalid")
    vintage_paths = persist_fred_vintages(root, verified_vintages)
    rate_paths: tuple[Path, ...] = ()
    if "FEDFUNDS" in set(verified_vintages["series_name"].astype(str)):
        rate_paths = persist_macro_release_features(
            root,
            derive_alfred_rate_release_features(verified_vintages),
        )
    return FredVintageImportResult(
        evidence_directory=destination,
        row_count=len(verified_vintages),
        series_count=len(set(verified_vintages["series_name"].astype(str))),
        vintage_partition_paths=vintage_paths,
        rate_feature_paths=rate_paths,
    )


def read_fred_alfred_vintage_import(
    directory: Path,
    *,
    datastore_root: Path,
) -> Mapping[str, object]:
    root = Path(datastore_root).resolve()
    run = Path(directory).resolve()
    allowed = (
        root / "ml" / "option-pricing-evidence" / "fred-alfred-vintages"
    ).resolve()
    if run.parent != allowed:
        raise FredVintageImportError(
            "FRED/ALFRED evidence path escapes the immutable import root"
        )
    try:
        manifest = json.loads(
            (run / FRED_ALFRED_MANIFEST_NAME).read_text(encoding="utf-8")
        )
        receipt = json.loads(
            (run / FRED_ALFRED_RECEIPT_NAME).read_text(encoding="utf-8")
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        raise FredVintageImportError(
            f"FRED/ALFRED evidence metadata is unreadable: {run}"
        ) from None
    if not isinstance(manifest, Mapping) or not isinstance(receipt, Mapping):
        raise FredVintageImportError("FRED/ALFRED evidence metadata is malformed")
    if (
        manifest.get("schema_version") != FRED_ALFRED_IMPORT_VERSION
        or receipt.get("schema_version") != FRED_ALFRED_IMPORT_VERSION
        or receipt.get("run_path") != run.relative_to(root).as_posix()
        or receipt.get("manifest_checksum_sha256")
        != file_checksum(run / FRED_ALFRED_MANIFEST_NAME)
        or manifest.get("availability_basis")
        != ALFRED_VINTAGE_AVAILABILITY_BASIS
        or receipt.get("availability_basis")
        != ALFRED_VINTAGE_AVAILABILITY_BASIS
        or manifest.get("current_revised_history_used") is not False
        or receipt.get("current_revised_history_used") is not False
        or manifest.get("historical_coverage_status") != "NOT_EVALUATED"
        or receipt.get("historical_coverage_status") != "NOT_EVALUATED"
        or manifest.get("automated_action_allowed") is not False
        or receipt.get("automated_action_allowed") is not False
    ):
        raise FredVintageImportError(
            "FRED/ALFRED evidence receipt does not match its manifest"
        )
    outputs = manifest.get("outputs")
    if not isinstance(outputs, Mapping):
        raise FredVintageImportError("FRED/ALFRED output inventory is invalid")
    expected_names = {FRED_ALFRED_RAW_NAME, FRED_ALFRED_PARQUET_NAME}
    if set(map(str, outputs)) != expected_names:
        raise FredVintageImportError("FRED/ALFRED output inventory is incomplete")
    for raw_name, raw_metadata in outputs.items():
        name = str(raw_name)
        relative = Path(name)
        metadata = raw_metadata if isinstance(raw_metadata, Mapping) else {}
        path = run / relative
        if (
            relative.is_absolute()
            or len(relative.parts) != 1
            or not path.is_file()
            or int(metadata.get("size", -1)) != path.stat().st_size
            or metadata.get("checksum_sha256") != file_checksum(path)
        ):
            raise FredVintageImportError(
                f"FRED/ALFRED evidence output checksum mismatch: {path}"
            )
    try:
        frame = pd.read_parquet(run / FRED_ALFRED_PARQUET_NAME)
    except Exception:
        raise FredVintageImportError(
            "FRED/ALFRED normalized evidence is unreadable"
        ) from None
    if (
        list(frame.columns).count("id") != 1
        or not frame.columns.tolist()
        or frame.columns[0] != "id"
        or len(frame) != int(manifest.get("row_count", -1))
        or len(frame) != int(receipt.get("row_count", -1))
    ):
        raise FredVintageImportError(
            "FRED/ALFRED normalized evidence identity or row count is invalid"
        )
    try:
        normalized = normalize_fred_vintage_rows(frame.drop(columns="id"))
    except Exception:
        raise FredVintageImportError(
            "FRED/ALFRED normalized evidence failed its vintage contract"
        ) from None
    if not normalized["availability_basis"].eq(
        ALFRED_VINTAGE_AVAILABILITY_BASIS
    ).all():
        raise FredVintageImportError(
            "FRED/ALFRED normalized evidence has a non-vintage availability basis"
        )
    return {"manifest": manifest, "receipt": receipt, "vintages": normalized}


def _fetch_vintages(
    client: FredAlfredClient,
    *,
    request: Mapping[str, object],
    acquired_at: pd.Timestamp,
) -> tuple[pd.DataFrame, Mapping[str, object], Mapping[str, object]]:
    rows: list[dict[str, object]] = []
    raw_series: dict[str, object] = {}
    summaries: dict[str, object] = {}
    missing_value_count = 0
    for series_id in request["series"]:
        series = str(series_id)
        metadata = client.get_json(
            "series",
            params={"series_id": series, "file_type": "json"},
        )
        series_metadata = metadata.get("seriess")
        if (
            not isinstance(series_metadata, list)
            or len(series_metadata) != 1
            or not isinstance(series_metadata[0], Mapping)
            or str(series_metadata[0].get("id", "")).upper() != series
        ):
            raise FredVintageImportError(
                f"FRED series metadata is invalid for {series}"
            )
        metadata_row = dict(series_metadata[0])
        vintage_pages = _paged_request(
            client,
            endpoint="series/vintagedates",
            collection_name="vintage_dates",
            params={
                "series_id": series,
                "file_type": "json",
                "realtime_start": request["realtime_start"],
                "realtime_end": request["realtime_end"],
                "sort_order": "asc",
            },
            page_limit=10_000,
        )
        observation_pages = _paged_request(
            client,
            endpoint="series/observations",
            collection_name="observations",
            params={
                "series_id": series,
                "file_type": "json",
                "realtime_start": request["realtime_start"],
                "realtime_end": request["realtime_end"],
                "observation_start": request["observation_start"],
                "observation_end": request["observation_end"],
                "output_type": FRED_ALFRED_OUTPUT_TYPE,
                "units": "lin",
                "sort_order": "asc",
            },
            page_limit=100_000,
        )
        vintage_dates = [
            str(value)
            for page in vintage_pages
            for value in page.get("vintage_dates", ())
        ]
        observations = [
            value
            for page in observation_pages
            for value in page.get("observations", ())
        ]
        if any(
            page.get("output_type") not in {FRED_ALFRED_OUTPUT_TYPE, str(FRED_ALFRED_OUTPUT_TYPE)}
            for page in observation_pages
        ):
            raise FredVintageImportError(
                f"FRED observations did not preserve real-time intervals for {series}"
            )
        series_rows = 0
        for raw in observations:
            if not isinstance(raw, Mapping):
                raise FredVintageImportError(
                    f"FRED observation is malformed for {series}"
                )
            required = {"date", "realtime_start", "realtime_end", "value"}
            if not required.issubset(raw):
                raise FredVintageImportError(
                    "Current-revised FRED observations cannot be imported as "
                    f"ALFRED vintages for {series}"
                )
            value = pd.to_numeric(pd.Series([raw["value"]]), errors="coerce").iloc[0]
            if pd.isna(value):
                missing_value_count += 1
                continue
            release_at = _conservative_provider_available_at(raw["realtime_start"])
            rows.append(
                {
                    "series_name": series,
                    "observation_date": raw["date"],
                    "realtime_start": raw["realtime_start"],
                    "realtime_end": raw["realtime_end"],
                    "release_at": release_at,
                    "release_time_precision": FRED_ALFRED_RELEASE_TIME_PRECISION,
                    "fetched_at": acquired_at,
                    "availability_basis": ALFRED_VINTAGE_AVAILABILITY_BASIS,
                    "value": value,
                    "unit": str(metadata_row.get("units", "")),
                    "frequency": str(metadata_row.get("frequency", "")),
                }
            )
            series_rows += 1
        if series_rows == 0:
            raise FredVintageImportError(
                f"FRED/ALFRED returned no finite vintage observations for {series}"
            )
        raw_series[series] = {
            "series_metadata": metadata,
            "vintage_date_pages": vintage_pages,
            "observation_pages": observation_pages,
        }
        summaries[series] = {
            "vintage_date_count": len(vintage_dates),
            "observation_record_count": len(observations),
            "finite_vintage_row_count": series_rows,
            "unit": str(metadata_row.get("units", "")),
            "frequency": str(metadata_row.get("frequency", "")),
            "metadata_last_updated": metadata_row.get("last_updated"),
            "metadata_acquired_at": acquired_at.isoformat(),
        }
    try:
        normalized = normalize_fred_vintage_rows(rows)
    except Exception as exc:
        raise FredVintageImportError(
            f"FRED/ALFRED vintage normalization failed: {type(exc).__name__}: {exc}"
        ) from exc
    if normalized.empty:
        raise FredVintageImportError("FRED/ALFRED vintage import is empty")
    provider_responses = {
        "schema_version": FRED_ALFRED_IMPORT_VERSION,
        "acquired_at": acquired_at.isoformat(),
        "series": raw_series,
    }
    provider_summary = {
        "series": summaries,
        "missing_value_count": missing_value_count,
        "normalized_row_count": len(normalized),
    }
    return normalized, provider_responses, provider_summary


def _paged_request(
    client: FredAlfredClient,
    *,
    endpoint: str,
    collection_name: str,
    params: Mapping[str, object],
    page_limit: int,
) -> list[Mapping[str, object]]:
    pages: list[Mapping[str, object]] = []
    offset = 0
    while True:
        page = client.get_json(
            endpoint,
            params={**dict(params), "limit": page_limit, "offset": offset},
        )
        collection = page.get(collection_name)
        if not isinstance(collection, list):
            raise FredVintageImportError(
                f"FRED/ALFRED response omitted {collection_name}"
            )
        pages.append(dict(page))
        try:
            count = int(page.get("count", len(collection)))
            returned_offset = int(page.get("offset", offset))
            returned_limit = int(page.get("limit", page_limit))
        except (TypeError, ValueError):
            raise FredVintageImportError(
                f"FRED/ALFRED pagination is invalid for {collection_name}"
            ) from None
        if returned_offset != offset or returned_limit < 1:
            raise FredVintageImportError(
                f"FRED/ALFRED pagination changed for {collection_name}"
            )
        offset += len(collection)
        if offset >= count:
            break
        if not collection:
            raise FredVintageImportError(
                f"FRED/ALFRED pagination stalled for {collection_name}"
            )
    return pages


def _validated_request(
    *,
    series_ids: Sequence[str],
    realtime_start: object,
    realtime_end: object,
    observation_start: object,
    observation_end: object,
    acquired_at: pd.Timestamp,
) -> Mapping[str, object]:
    series = tuple(dict.fromkeys(str(value).strip().upper() for value in series_ids))
    if not series or any(value not in FRED_ALFRED_SUPPORTED_SERIES for value in series):
        raise FredVintageImportError(
            "FRED/ALFRED series must be selected from: "
            + ", ".join(FRED_ALFRED_SUPPORTED_SERIES)
        )
    real_start = _iso_date(realtime_start, label="realtime_start")
    real_end = _iso_date(realtime_end, label="realtime_end")
    obs_start = _iso_date(observation_start, label="observation_start")
    obs_end = _iso_date(observation_end, label="observation_end")
    if real_start > real_end or obs_start > obs_end:
        raise FredVintageImportError("FRED/ALFRED request date ranges are reversed")
    if real_end > acquired_at.date():
        raise FredVintageImportError(
            "FRED/ALFRED realtime_end cannot be later than local acquisition"
        )
    return {
        "series": list(series),
        "realtime_start": real_start.isoformat(),
        "realtime_end": real_end.isoformat(),
        "observation_start": obs_start.isoformat(),
        "observation_end": obs_end.isoformat(),
        "output_type": FRED_ALFRED_OUTPUT_TYPE,
        "units": "lin",
    }


def _conservative_provider_available_at(value: object) -> pd.Timestamp:
    provider_date = _iso_date(value, label="realtime_start")
    next_midnight = pd.Timestamp(provider_date) + pd.Timedelta(days=1)
    return next_midnight.tz_localize("America/Chicago").tz_convert("UTC")


def _iso_date(value: object, *, label: str) -> date:
    try:
        rendered = str(value).strip()[:10]
        parsed = date.fromisoformat(rendered)
    except (TypeError, ValueError):
        raise FredVintageImportError(
            f"FRED/ALFRED {label} must be an ISO calendar date"
        ) from None
    return parsed


def _unused_timestamp_directory(parent: Path, timestamp: pd.Timestamp) -> Path:
    base = timestamp.strftime("%Y%m%dT%H%M%S.%fZ")
    destination = parent / base
    suffix = 2
    while destination.exists():
        destination = parent / f"{base}-{suffix}"
        suffix += 1
    return destination


def _assert_secret_free(payload: object, *, secret: str) -> None:
    rendered = json.dumps(payload, sort_keys=True, default=str)
    if secret and secret in rendered:
        raise FredVintageImportError(
            "FRED/ALFRED artifact preparation rejected credential material"
        )


def _write_json(path: Path, payload: object) -> None:
    Path(path).write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "FRED_ALFRED_IMPORT_VERSION",
    "FRED_ALFRED_SUPPORTED_SERIES",
    "FredAlfredClient",
    "FredVintageImportError",
    "FredVintageImportResult",
    "import_fred_alfred_vintages",
    "read_fred_alfred_vintage_import",
]
