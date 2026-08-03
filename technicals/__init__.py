"""Parquet-in, Parquet-out custom technical calculations for Duckets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TechnicalOutput:
    calculation: str
    provider: str
    timeframe: str
    rows: int
    path: Path
