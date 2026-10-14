"""Dated preparation context; publication targets never authorize trading."""
from __future__ import annotations

import os

import pandas as pd

ENVIRONMENT_KEY = "DUCKETZ_NIGHTLY_ACTION_DATE"
POLICY = "complete-dated-nightly-preparation-v1"


def action_date(value=None):
    value = os.environ.get(ENVIRONMENT_KEY) if value is None else value
    if value is None:
        return None
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is not None or stamp.date().isoformat() != value:
        raise ValueError("Nightly continuation requires an exact action date")
    return value


def continuing(day=None):
    selected = action_date()
    return selected is not None and (day is None or selected == str(day))


def deadline_expired(observed, deadline):
    deadline = pd.Timestamp(deadline)
    day = deadline.tz_convert("America/Los_Angeles").date().isoformat()
    return pd.Timestamp(observed) >= deadline and not continuing(day)
