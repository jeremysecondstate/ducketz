from __future__ import annotations

from pathlib import Path

import pandas as pd

from datafetching.ids import add_readable_id, without_internal_identity_columns
from signals.calculation import CALCULATION_NAME
from signals.consensus import TIMEFRAME_WEIGHTS


def load_market_regime_outputs(
    technicals_root: Path,
    *,
    calculation: str = "market-regime",
) -> dict[tuple[str, str], pd.DataFrame]:
    """Load supported market-regime outputs keyed by provider and timeframe."""
    root = technicals_root / calculation
    if not root.is_dir():
        return {}

    outputs: dict[tuple[str, str], pd.DataFrame] = {}
    for provider_folder in sorted(path for path in root.iterdir() if path.is_dir()):
        provider = provider_folder.name.strip().lower()
        for path in sorted(provider_folder.glob("*.parquet")):
            timeframe = path.stem.strip().lower()
            if timeframe not in TIMEFRAME_WEIGHTS:
                continue
            try:
                frame = pd.read_parquet(path)
            except Exception as exc:
                raise RuntimeError(f"Could not read technical parquet {path}: {exc}") from exc
            if frame.empty:
                continue
            outputs[(provider, timeframe)] = frame
    return outputs


def write_signal_parquet(
    output_root: Path,
    *,
    frame: pd.DataFrame,
    calculation: str = CALCULATION_NAME,
    provider: str = "consensus",
    frequency: str = "daily",
) -> Path:
    """Atomically replace one current lifecycle signal output."""
    folder = output_root / calculation / provider
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{frequency}.parquet"
    temporary = path.with_suffix(".tmp.parquet")
    output = without_internal_identity_columns(frame)
    output = add_readable_id(output, key_columns=("timestamp",))
    output.to_parquet(temporary, index=False)
    temporary.replace(path)
    return path
