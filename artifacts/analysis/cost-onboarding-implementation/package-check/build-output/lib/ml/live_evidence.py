from __future__ import annotations

from types import MappingProxyType
from typing import Final, Mapping


MINIMUM_LIVE_DECISIONS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "1h": 60,
        "4h": 60,
        "1d": 30,
        "1w": 30,
        "1w-d1": 30,
        "1w-d2": 30,
        "1w-d3": 30,
        "1w-d4": 30,
        "1w-d5": 30,
    }
)


def minimum_live_decisions(horizon: str) -> int:
    normalized = str(horizon or "").strip().lower()
    try:
        return int(MINIMUM_LIVE_DECISIONS[normalized])
    except KeyError as exc:
        raise ValueError(f"No live-evidence threshold exists for {horizon!r}") from exc


def live_evidence_status(*, horizon: str, completed_decisions: int) -> str:
    count = int(completed_decisions)
    if count < 0:
        raise ValueError("completed_decisions cannot be negative")
    if count >= minimum_live_decisions(horizon):
        return "LIVE_EVIDENCE_AVAILABLE"
    return (
        "NO_COMPLETED_DECISIONS"
        if count == 0
        else "INSUFFICIENT_LIVE_EVIDENCE"
    )
