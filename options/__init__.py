from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OptionSnapshotOutput:
    contracts_path: Path
    features_path: Path
    raw_path: Path
    contract_rows: int


__all__ = ["OptionSnapshotOutput"]
