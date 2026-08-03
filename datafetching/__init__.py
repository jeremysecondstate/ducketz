"""Provider-only data ingestion for Duckets.

This package fetches external data and writes Parquet files to DATASTORE.
It intentionally excludes technical analysis, scenario generation, and UI code.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FetchResult:
    provider: str
    data_files: int
    error_files: int
