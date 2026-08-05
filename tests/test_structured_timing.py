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
        output_format="json",
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


def test_compact_stage_timing_is_readable_and_keeps_required_fields() -> None:
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
        extra={"operation_name": "GOOG minute bars"},
    ) as timing:
        timing.annotate(row_count=7, operation="reused", partitions_compared=1)

    assert len(messages) == 2
    started, ended = messages
    assert " START provider.request " in started
    assert 'name="GOOG minute bars"' in started
    assert "sym=GOOG" in started
    assert "src=databento/ohlcv-1m" in started
    assert "req=2026-08-05T10:00:00Z..2026-08-05T10:01:00Z" in started
    assert "try=2" in started
    assert " END   provider.request " in ended
    assert "elapsed=" in ended
    assert "rows=7" in ended
    assert "op=reused" in ended
    assert "status=ok" in ended
    assert "partitions_compared=1" in ended
    assert not started.startswith("{")


def test_json_timing_can_be_selected_through_environment(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DUCKETS_TIMING_FORMAT", "json")
    messages: list[str] = []
    with timed_stage("test.stage", reporter=messages.append):
        pass

    assert json.loads(messages[0])["event"] == "stage_start"
    assert json.loads(messages[1])["event"] == "stage_end"
