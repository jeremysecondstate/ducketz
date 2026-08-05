from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterator, Mapping


Reporter = Callable[[str], None]
TIMING_FORMAT_ENV = "DUCKETS_TIMING_FORMAT"
_CORE_FIELDS = {
    "attempt",
    "elapsed_milliseconds",
    "error_type",
    "event",
    "operation",
    "operation_name",
    "provider",
    "request_end",
    "request_start",
    "row_count",
    "schema",
    "stage",
    "status",
    "symbol",
    "utc_timestamp",
}


@dataclass
class StageTiming:
    """Mutable annotations attached to one structured stage timing."""

    row_count: int | None = None
    operation: str = "skipped"
    extra: dict[str, object] = field(default_factory=dict)

    def annotate(
        self,
        *,
        row_count: int | None = None,
        operation: str | None = None,
        **extra: object,
    ) -> None:
        if row_count is not None:
            self.row_count = int(row_count)
        if operation is not None:
            self.operation = str(operation)
        self.extra.update(extra)


@contextmanager
def timed_stage(
    stage: str,
    *,
    symbol: str | None = None,
    provider: str | None = None,
    schema: str | None = None,
    request_start: object | None = None,
    request_end: object | None = None,
    attempt: int | None = None,
    reporter: Reporter | None = print,
    extra: Mapping[str, object] | None = None,
    output_format: str | None = None,
) -> Iterator[StageTiming]:
    """Emit compact or JSON start/end events using a monotonic duration clock."""

    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    common: dict[str, object] = {
        "stage": str(stage),
        "symbol": symbol,
        "provider": provider,
        "schema": schema,
        "request_start": _render_time(request_start),
        "request_end": _render_time(request_end),
        "attempt": attempt,
    }
    if extra:
        common.update(dict(extra))
    _emit(
        reporter,
        {
            "event": "stage_start",
            "utc_timestamp": started_at.isoformat(),
            **common,
        },
        output_format=output_format,
    )
    timing = StageTiming()
    status = "ok"
    error_type: str | None = None
    try:
        yield timing
    except BaseException as exc:
        status = "error"
        error_type = type(exc).__name__
        if timing.operation == "skipped":
            timing.operation = "failed"
        raise
    finally:
        finished_at = datetime.now(timezone.utc)
        payload: dict[str, object] = {
            "event": "stage_end",
            "utc_timestamp": finished_at.isoformat(),
            **common,
            "row_count": timing.row_count,
            "operation": timing.operation,
            "status": status,
            "error_type": error_type,
            "elapsed_milliseconds": round(
                (time.perf_counter() - started) * 1_000.0,
                3,
            ),
        }
        payload.update(timing.extra)
        _emit(reporter, payload, output_format=output_format)


def _emit(
    reporter: Reporter | None,
    payload: Mapping[str, object],
    *,
    output_format: str | None,
) -> None:
    if reporter is not None:
        selected = str(
            output_format or os.getenv(TIMING_FORMAT_ENV, "compact")
        ).strip().lower()
        if selected in {"json", "jsonl"}:
            reporter(json.dumps(dict(payload), sort_keys=True, default=str))
        else:
            reporter(_compact_event(payload))


def _compact_event(payload: Mapping[str, object]) -> str:
    event = "START" if payload.get("event") == "stage_start" else "END"
    timestamp = _compact_timestamp(payload.get("utc_timestamp"))
    parts = [f"[{timestamp}]", f"{event:<5}", str(payload.get("stage") or "unknown")]

    operation_name = payload.get("operation_name")
    if operation_name is not None:
        parts.append(_field("name", operation_name))
    symbol = payload.get("symbol")
    if symbol is not None:
        parts.append(_field("sym", symbol))
    provider = payload.get("provider")
    schema = payload.get("schema")
    if provider is not None or schema is not None:
        source = "/".join(
            str(value) for value in (provider, schema) if value is not None
        )
        parts.append(_field("src", source))
    request_start = payload.get("request_start")
    request_end = payload.get("request_end")
    if request_start is not None or request_end is not None:
        parts.append(
            _field(
                "req",
                f"{_compact_timestamp(request_start)}..{_compact_timestamp(request_end)}",
            )
        )
    attempt = payload.get("attempt")
    if attempt is not None:
        parts.append(_field("try", attempt))

    if event == "END":
        parts.append(_field("elapsed", f"{float(payload.get('elapsed_milliseconds') or 0):.1f}ms"))
        parts.append(_field("rows", payload.get("row_count") if payload.get("row_count") is not None else "?"))
        parts.append(_field("op", payload.get("operation") or "skipped"))
        parts.append(_field("status", payload.get("status") or "unknown"))
        if payload.get("error_type") is not None:
            parts.append(_field("error", payload["error_type"]))

    for key in sorted(set(payload) - _CORE_FIELDS):
        value = payload.get(key)
        if value is not None:
            parts.append(_field(key, value))
    return " ".join(parts)


def _field(name: str, value: object) -> str:
    if isinstance(value, bool):
        rendered = "true" if value else "false"
    elif isinstance(value, (int, float)):
        rendered = str(value)
    elif isinstance(value, (list, tuple, dict)):
        rendered = json.dumps(value, separators=(",", ":"), default=str)
    else:
        rendered = str(value)
        if not rendered or any(character.isspace() for character in rendered):
            rendered = json.dumps(rendered)
    return f"{name}={rendered}"


def _compact_timestamp(value: object | None) -> str:
    if value is None:
        return "?"
    return str(value).replace("+00:00", "Z")


def _render_time(value: object | None) -> str | None:
    if value is None:
        return None
    try:
        import pandas as pd

        timestamp = pd.to_datetime(value, utc=True, errors="coerce")
        if not pd.isna(timestamp):
            return pd.Timestamp(timestamp).isoformat()
    except Exception:
        pass
    return str(value)


__all__ = ["StageTiming", "TIMING_FORMAT_ENV", "timed_stage"]
