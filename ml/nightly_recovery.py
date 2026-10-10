"""Explicit late preparation, with real timestamps and a fixed recovery window."""
from __future__ import annotations

import json
from pathlib import Path

import exchange_calendars as xcals
import pandas as pd

from ml.artifacts import file_checksum, utc_timestamp

VERSION = "nightly-preparation-recovery-v1"
TIMEZONE = "America/Los_Angeles"


def session_context(observed_at):
    """Select the latest closed exchange session, including after midnight."""
    observed = utc_timestamp(observed_at)
    local = observed.tz_convert(TIMEZONE)
    label = pd.Timestamp(local.date())
    calendar = xcals.get_calendar("XNYS", start=label - pd.Timedelta(days=15),
                                 end=label + pd.Timedelta(days=15))
    completed = label if local.hour >= 17 else label - pd.Timedelta(days=1)
    session = calendar.date_to_session(completed, direction="previous")
    action = calendar.next_session(session).date().isoformat()
    opening = pd.Timestamp(action).tz_localize(TIMEZONE) + pd.Timedelta(hours=4)
    close = pd.Timestamp(action).tz_localize(TIMEZONE) + pd.Timedelta(hours=17)
    return {"source_session": session.date().isoformat(), "action_date": action,
            "original_deadline_at": opening.tz_convert("UTC").isoformat(),
            "session_close_at": close.tz_convert("UTC").isoformat()}


def make_recovery(config, observed_at, *, reason):
    observed = utc_timestamp(observed_at)
    context = session_context(observed)
    if not str(reason).strip():
        raise ValueError("Catch-up requires an explicit operator authorization reason")
    deadline = min(observed + pd.Timedelta(hours=7), utc_timestamp(context["session_close_at"]))
    if observed >= deadline:
        raise ValueError("The missed action session has closed")
    return {"schema_version": VERSION, **context, "actor": config["actor"],
            "datastore": str(Path(config["datastore"]).resolve()),
            "requested_at": observed.isoformat(), "expires_at": deadline.isoformat(),
            "operator_authorized": True, "authorization_reason": reason,
            "orders_authorized": False,
            "training_information_cutoff": (utc_timestamp(context["original_deadline_at"])
                                             - pd.Timedelta(microseconds=1)).isoformat()}


def verify_recovery(root, path, observed_at, *, action_date=None):
    """Verify the same bounded evidence at each stage; retries never renew it."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    def aware(key):
        value = pd.Timestamp(payload[key])
        if pd.isna(value) or value.tzinfo is None:
            raise ValueError("Recovery timestamps must be timezone aware")
        return value.tz_convert("UTC")
    requested, expires = aware("requested_at"), aware("expires_at")
    original, cutoff = aware("original_deadline_at"), aware("training_information_cutoff")
    context = session_context(requested)
    now = utc_timestamp(observed_at)
    if (payload.get("schema_version") != VERSION
            or payload.get("actor") not in ("Scout", "Atlas")
            or payload.get("operator_authorized") is not True
            or payload.get("orders_authorized") is not False
            or not str(payload.get("authorization_reason", "")).strip()
            or payload.get("datastore") != str(Path(root).resolve())
            or any(payload.get(key) != value for key, value in context.items())
            or (action_date is not None and payload["action_date"] != action_date)
            or cutoff != original - pd.Timedelta(microseconds=1)
            or not requested <= now < expires <= min(requested + pd.Timedelta(hours=7),
                                                     utc_timestamp(context["session_close_at"]))):
        raise ValueError("Nightly recovery evidence is invalid, expired, or belongs to another session")
    return {"path": str(Path(path).resolve()), "sha256": file_checksum(Path(path)),
            "authorization": payload}


def verify_saved_recovery(root, evidence, observed_at, *, action_date=None):
    current = verify_recovery(root, evidence["path"], observed_at, action_date=action_date)
    if current != evidence:
        raise ValueError("Nightly recovery evidence changed since this run")
    return current
