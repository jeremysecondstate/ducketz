from __future__ import annotations

import json

from datafetching.observability import timed_stage


def test_structured_stage_timing_has_correlation_and_monotonic_duration_fields() -> None:
    messages: list[str] = []
    with timed_stage(
        "provider.request",
        symbol="GOOG",
        provider="databento",
        schema="ohlcv-1m",
        request_start="2026-08-05T10:00:00Z",
        request_end="2026-08-05T10:01:00Z",
        attempt=2,
        reporter=messages.append,
    ) as timing:
        timing.annotate(row_count=7, operation="reused", partitions_compared=1)

    assert len(messages) == 2
    started, ended = (json.loads(message) for message in messages)
    assert started["event"] == "stage_start"
    assert ended["event"] == "stage_end"
    assert ended["stage"] == "provider.request"
    assert ended["symbol"] == "GOOG"
    assert ended["provider"] == "databento"
    assert ended["schema"] == "ohlcv-1m"
    assert ended["attempt"] == 2
    assert ended["row_count"] == 7
    assert ended["operation"] == "reused"
    assert ended["partitions_compared"] == 1
    assert ended["elapsed_milliseconds"] >= 0
    assert ended["utc_timestamp"] >= started["utc_timestamp"]
