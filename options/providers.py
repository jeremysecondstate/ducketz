from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import pandas as pd


class OptionProviderUnavailable(RuntimeError):
    """The canonical option provider was unavailable for this causal target."""


@dataclass(frozen=True)
class ProviderOptionEvidence:
    provider: str
    dataset: str
    schema: str
    symbol: str
    target_snapshot_for: pd.Timestamp
    received_at: pd.Timestamp
    quotes: pd.DataFrame
    definitions: pd.DataFrame


class OptionMarketDataAdapter(Protocol):
    """Injected, mockable source of already-received prospective option evidence."""

    provider: str
    dataset: str
    schema: str

    def fetch_snapshot(
        self,
        *,
        symbol: str,
        target_snapshot_for: pd.Timestamp,
        requested_at: pd.Timestamp,
    ) -> ProviderOptionEvidence:
        """Return one bounded target; implementations own credentials and transport."""


def validate_canonical_opra_adapter(adapter: OptionMarketDataAdapter) -> None:
    if str(adapter.provider).strip().lower() != "databento-opra":
        raise ValueError("The canonical market adapter must identify databento-opra")
    if str(adapter.dataset).strip().upper() != "OPRA.PILLAR":
        raise ValueError("The canonical Databento adapter must use OPRA.PILLAR")
    if str(adapter.schema).strip().lower() != "cbbo-1s":
        raise ValueError("Prospective OPRA L1 capture must default to cbbo-1s")


__all__ = [
    "OptionMarketDataAdapter",
    "OptionProviderUnavailable",
    "ProviderOptionEvidence",
    "validate_canonical_opra_adapter",
]
