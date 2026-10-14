"""Cancellation authority for the existing nightly preparation workflow.

Supervisor records and process exceptions have no cancellation authority. Only
a local record of Jeremy's explicit instruction can stop a bound nightly run.
Callers must copy the actual human instruction and its source reference; they
must never manufacture this evidence from a status or an automatic stop reason.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Mapping


CANCELLATION_VERSION = "jeremy-explicit-nightly-cancellation-v1"
CANCELLATION_AUTHORITY = "jeremy-explicit-nightly-cancellation"


def validate_cancellation(record: object, *, run_id: str | None = None) -> bool:
    """Return whether a record carries explicit human authority for its run."""
    if not isinstance(record, Mapping):
        return False
    if (record.get("schema_version") != CANCELLATION_VERSION
            or record.get("authority") != CANCELLATION_AUTHORITY
            or record.get("requested_by") != "Jeremy"
            or record.get("explicit_human_request") is not True
            or record.get("action") != "cancel"):
        return False
    for name in ("human_instruction", "human_request_ref", "run_id", "run_path", "requested_at"):
        if not isinstance(record.get(name), str) or not record[name].strip():
            return False
    try:
        timestamp = datetime.fromisoformat(record["requested_at"].replace("Z", "+00:00"))
        path = Path(record["run_path"])
        if timestamp.tzinfo is None or not path.is_absolute() or path.name != record["run_id"]:
            return False
    except (ValueError, OSError):
        return False
    return run_id is None or record["run_id"] == run_id


class ExplicitNightlyCancellation(InterruptedError):
    """A stop supported by Jeremy's actual, recorded instruction."""

    def __init__(self, request: Mapping[str, object]):
        if not validate_cancellation(request):
            raise ValueError("Nightly cancellation requires Jeremy's explicit instruction and source reference")
        self.request = dict(request)
        super().__init__("Jeremy explicitly cancelled nightly operations: " + str(request["human_instruction"]))


def read_cancellation_request(stop_request_path: Path, *, run_path: Path | None = None) -> dict[str, object] | None:
    """Read validated local human evidence; legacy or automatic files are inert."""
    request_path = Path(stop_request_path)
    bound_run = Path(run_path if run_path is not None else request_path.parent).resolve()
    try:
        record = json.loads(request_path.read_text(encoding="utf-8"))
        if (not validate_cancellation(record, run_id=bound_run.name)
                or Path(record["run_path"]).resolve() != bound_run):
            return None
    except (OSError, ValueError, TypeError):
        return None
    return dict(record)
