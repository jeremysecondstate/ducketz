from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

import pandas as pd


@dataclass(frozen=True)
class MarketEvent:
    event_timestamp: pd.Timestamp
    available_at: pd.Timestamp
    source_sequence: int
    event_type: str
    payload: Mapping[str, object]

    @classmethod
    def create(
        cls,
        *,
        event_timestamp: object,
        available_at: object,
        source_sequence: int,
        event_type: str,
        payload: Mapping[str, object],
    ) -> "MarketEvent":
        event = _utc(event_timestamp, "event_timestamp")
        available = _utc(available_at, "available_at")
        if available < event:
            raise ValueError("Event availability cannot precede event time")
        if source_sequence < 0:
            raise ValueError("source_sequence cannot be negative")
        if not str(event_type).strip():
            raise ValueError("event_type is required")
        return cls(event, available, int(source_sequence), str(event_type), dict(payload))


def deterministic_event_order(events: Iterable[MarketEvent]) -> tuple[MarketEvent, ...]:
    return tuple(
        sorted(
            events,
            key=lambda value: (
                value.available_at,
                value.event_timestamp,
                value.source_sequence,
                value.event_type,
            ),
        )
    )


def replay_available_events(
    events: Iterable[MarketEvent],
    *,
    decision_time: object,
) -> tuple[MarketEvent, ...]:
    """Select events by event clocks; replay wall speed is intentionally absent."""

    cutoff = _utc(decision_time, "decision_time")
    return tuple(
        event
        for event in deterministic_event_order(events)
        if event.available_at <= cutoff
    )


def _utc(value: object, label: str) -> pd.Timestamp:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"{label} must be a valid timestamp")
    return pd.Timestamp(parsed)


__all__ = ["MarketEvent", "deterministic_event_order", "replay_available_events"]
