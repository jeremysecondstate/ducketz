from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterator, Mapping


Reporter = Callable[[str], None]


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
) -> Iterator[StageTiming]:
    """Emit machine-readable start/end events using a monotonic duration clock."""

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
        _emit(reporter, payload)


def _emit(reporter: Reporter | None, payload: Mapping[str, object]) -> None:
    if reporter is not None:
        reporter(json.dumps(dict(payload), sort_keys=True, default=str))


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


__all__ = ["StageTiming", "timed_stage"]
