"""Pure Pacific submission-time classification for independent callers.

This helper checks only the supplied session date and strict 16:45 cutoff.
It does not establish market eligibility, broker capacity or manual authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal

from .prediction import ContractError, PACIFIC, instant


@dataclass(frozen=True)
class SubmissionGate:
    state: Literal["before_submission_cutoff", "submission_cutoff", "wrong_session_date"]
    checked_at: str
    eligible_session: str
    cutoff_at: str
    reason: str


def submission_gate(*, as_of: str | datetime, eligible_session: date) -> SubmissionGate:
    """Reject submission at/after 16:45 Pacific, with DST from the zone database.

    Before-cutoff does not mean the market is open, the session is eligible,
    orders are authorized or an input/model/quote is usable. The caller supplies
    an independently established eligible session date; this helper does not
    invent calendars, an overnight target or a first eligible submission time.
    """
    if type(eligible_session) is not date:
        raise ContractError("An explicit eligible session date is required")
    checked = instant(as_of)
    local = checked.astimezone(PACIFIC)
    cutoff = datetime.combine(eligible_session, time(16, 45), tzinfo=PACIFIC)
    if local.date() != eligible_session:
        state, reason = "wrong_session_date", "Submission date differs from the supplied eligible session"
    elif checked >= instant(cutoff):
        state, reason = "submission_cutoff", "New submissions are forbidden at or after 16:45 Pacific"
    else:
        state, reason = "before_submission_cutoff", "Time check only; market, model, sizing, quote and account checks remain"
    return SubmissionGate(state, checked.isoformat(), eligible_session.isoformat(), instant(cutoff).isoformat(), reason)
