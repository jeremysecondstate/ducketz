from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import requests

from datafetching import FetchResult
from datafetching.fred_vintages import persist_current_fred_rate_receipt
from datafetching.parquet_store import ParquetStore

FRED_CSV_BASE_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"


@dataclass(frozen=True, init=False)
class FredSeriesSpec:
    output_symbol: str
    series_id: str
    label: str
    source_agency: str
    cadence: str
    unit: str
    fetch_liveness_limit_days: int

    def __init__(
        self,
        output_symbol: str,
        series_id: str,
        label: str,
        source_agency: str,
        cadence: str,
        unit: str,
        fetch_liveness_limit_days: int | None = None,
        *,
        freshness_limit_days: int | None = None,
    ) -> None:
        effective_limit = fetch_liveness_limit_days
        if effective_limit is None:
            effective_limit = freshness_limit_days
        elif (
            freshness_limit_days is not None
            and freshness_limit_days != effective_limit
        ):
            raise ValueError("FRED liveness limit aliases must match")
        if effective_limit is None:
            raise TypeError("fetch_liveness_limit_days is required")
        object.__setattr__(self, "output_symbol", output_symbol)
        object.__setattr__(self, "series_id", series_id)
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "source_agency", source_agency)
        object.__setattr__(self, "cadence", cadence)
        object.__setattr__(self, "unit", unit)
        object.__setattr__(self, "fetch_liveness_limit_days", effective_limit)

    @property
    def freshness_limit_days(self) -> int:
        """Backward-compatible alias for the former field name."""
        return self.fetch_liveness_limit_days


FRED_SERIES = (
    FredSeriesSpec(
        output_symbol="GDP",
        series_id="GDP",
        label="Gross Domestic Product",
        source_agency="U.S. Bureau of Economic Analysis",
        cadence="quarterly",
        unit="billions of dollars, seasonally adjusted annual rate",
        fetch_liveness_limit_days=240,
    ),
    FredSeriesSpec(
        output_symbol="CPI",
        series_id="CPIAUCSL",
        label="Consumer Price Index for All Urban Consumers: All Items",
        source_agency="U.S. Bureau of Labor Statistics",
        cadence="monthly",
        unit="index points (1982-1984=100), seasonally adjusted",
        fetch_liveness_limit_days=90,
    ),
    FredSeriesSpec(
        output_symbol="unemploymentRate",
        series_id="UNRATE",
        label="Unemployment Rate",
        source_agency="U.S. Bureau of Labor Statistics",
        cadence="monthly",
        unit="percent, seasonally adjusted",
        fetch_liveness_limit_days=90,
    ),
    FredSeriesSpec(
        output_symbol="federalFunds",
        series_id="FEDFUNDS",
        label="Federal Funds Effective Rate",
        source_agency="Board of Governors of the Federal Reserve System",
        cadence="monthly",
        unit="percent, not seasonally adjusted",
        fetch_liveness_limit_days=90,
    ),
)


class StaleFredSeriesError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        latest_observation_date: date | None = None,
        age_days: int | None = None,
        limit_days: int | None = None,
    ) -> None:
        super().__init__(message)
        self.latest_observation_date = latest_observation_date
        self.age_days = age_days
        self.limit_days = limit_days


def fetch(_symbol: str, store: ParquetStore) -> FetchResult:
    """Fetch official U.S. macro series into idempotent raw/normalized Parquets."""
    data_files = 0
    error_files = 0

    for spec in FRED_SERIES:
        metadata = _metadata(spec)
        try:
            rows, raw_csv, latest_date, age_days = fetch_series(spec)
            metadata.update(
                _liveness_metadata(
                    latest_date=latest_date,
                    age_days=age_days,
                    status="CURRENT",
                )
            )
        except Exception as exc:
            if (
                isinstance(exc, StaleFredSeriesError)
                and exc.latest_observation_date is not None
                and exc.age_days is not None
            ):
                metadata.update(
                    _liveness_metadata(
                        latest_date=exc.latest_observation_date,
                        age_days=exc.age_days,
                        status="STALE",
                    )
                )
            store.save_error(
                source="fred",
                category="macro",
                symbol=spec.output_symbol,
                request_key=spec.series_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
                metadata=metadata,
                pool="macro",
            )
            error_files += 1
            continue

        if store.save_macro_rows(
            "fred",
            spec.output_symbol,
            spec.series_id,
            rows,
            metadata=metadata,
            pool="macro",
        ) is not None:
            data_files += 1

        if store.save_raw_payload(
            source="fred",
            category="macro",
            symbol=spec.output_symbol,
            endpoint=spec.series_id,
            dataset_key=spec.series_id,
            payload=raw_csv,
            metadata=metadata,
            pool="macro",
        ) is not None:
            data_files += 1

        if spec.series_id == "FEDFUNDS":
            # This is a current-receipt bridge, not a historical-vintage claim.
            # Its local fetched_at clock makes it usable only by future decisions.
            data_files += len(
                persist_current_fred_rate_receipt(store.root_dir, rows)
            )

    return FetchResult("fred", data_files, error_files)


def fetch_series(spec: FredSeriesSpec) -> tuple[list[dict[str, Any]], str, date, int]:
    response = requests.get(
        FRED_CSV_BASE_URL,
        params={"id": spec.series_id},
        timeout=30,
    )
    response.raise_for_status()
    raw_csv = response.text
    rows = _rows_from_csv(spec, raw_csv)
    if not rows:
        raise RuntimeError(f"FRED {spec.series_id} returned no numeric observations.")

    latest_date = max(date.fromisoformat(str(row["date"])) for row in rows)
    age_days = (datetime.now(timezone.utc).date() - latest_date).days
    if age_days > spec.fetch_liveness_limit_days:
        raise StaleFredSeriesError(
            f"FRED {spec.series_id} latest observation is {latest_date.isoformat()} "
            f"({age_days} days old; limit {spec.fetch_liveness_limit_days}).",
            latest_observation_date=latest_date,
            age_days=age_days,
            limit_days=spec.fetch_liveness_limit_days,
        )
    return rows, raw_csv, latest_date, age_days


def _rows_from_csv(spec: FredSeriesSpec, raw_csv: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(raw_csv))
    if not reader.fieldnames or len(reader.fieldnames) < 2:
        raise RuntimeError(f"FRED {spec.series_id} CSV had an unexpected header.")

    date_column = reader.fieldnames[0]
    value_column = spec.series_id if spec.series_id in reader.fieldnames else reader.fieldnames[1]
    fetched_at = datetime.now(timezone.utc).isoformat()
    rows: list[dict[str, Any]] = []
    for source_row in reader:
        observation_date = str(source_row.get(date_column) or "").strip()
        raw_value = str(source_row.get(value_column) or "").strip()
        if not observation_date or raw_value in {"", ".", "NaN", "nan"}:
            continue
        try:
            parsed_date = date.fromisoformat(observation_date)
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        rows.append(
            {
                "symbol": spec.output_symbol,
                "source": "fred",
                "endpoint": spec.series_id,
                "request_key": spec.series_id,
                "macro_context_kind": "economic_indicator",
                "provider_symbol": spec.series_id,
                "series": spec.series_id,
                "series_label": spec.label,
                "source_agency": spec.source_agency,
                "cadence": spec.cadence,
                "unit": spec.unit,
                "fetch_liveness_limit_days": spec.fetch_liveness_limit_days,
                "freshness_limit_days": spec.fetch_liveness_limit_days,
                "row_index": len(rows),
                "fetched_at": fetched_at,
                "date": parsed_date.isoformat(),
                "value": value,
            }
        )
    return rows


def _metadata(spec: FredSeriesSpec) -> dict[str, object]:
    return {
        "provider_base_url": FRED_CSV_BASE_URL,
        "endpoint": spec.series_id,
        "macro_context_kind": "economic_indicator",
        "series": spec.series_id,
        "series_label": spec.label,
        "source_agency": spec.source_agency,
        "cadence": spec.cadence,
        "unit": spec.unit,
        "fetch_liveness_limit_days": spec.fetch_liveness_limit_days,
        "freshness_limit_days": spec.fetch_liveness_limit_days,
    }


def _liveness_metadata(
    *,
    latest_date: date,
    age_days: int,
    status: str,
) -> dict[str, object]:
    return {
        "latest_observation_date": latest_date.isoformat(),
        "fetch_liveness_age_days": age_days,
        "fetch_liveness_status": status,
        # Preserve the existing persisted metadata names for compatibility.
        "freshness_age_days": age_days,
        "freshness_status": status,
    }
