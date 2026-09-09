from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class OptionSnapshotOutput:
    contracts_path: Path
    features_path: Path
    raw_path: Path
    contract_rows: int
    receipt_path: Path | None = field(default=None, compare=False)
    snapshot_directory: Path | None = field(default=None, compare=False)


__all__ = ["OptionSnapshotOutput"]
