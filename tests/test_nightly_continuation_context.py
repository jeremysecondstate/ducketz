"""Offline dated-context checks without the nightly runtime or provider stack."""
from datetime import date
import os

import pandas as pd
import pytest

from ml.nightly_continuation import (
    ENVIRONMENT_KEY, action_date, continuing, deadline_expired,
)


@pytest.fixture(autouse=True)
def isolated_continuation_environment(monkeypatch):
    monkeypatch.delenv(ENVIRONMENT_KEY, raising=False)


def test_absent_context_keeps_ordinary_deadlines():
    assert action_date() is None
    assert not continuing()
    assert not continuing("2026-10-13")


@pytest.mark.parametrize("value", ["2026-10-13", "2028-02-29"])
def test_explicit_exact_iso_action_date(value):
    assert action_date(value) == value


@pytest.mark.parametrize("value", [
    "", "NaT", "2026-10-3", "2026/10/13", "2026-02-29",
    "2026-10-13 00:00", "2026-10-13T00:00:00Z",
    "2026-10-13T00:00:00-07:00", " 2026-10-13 ",
    date(2026, 10, 13), pd.Timestamp("2026-10-13"), pd.NaT,
    20261013, 1.0, True,
])
def test_action_date_rejects_nonexact_invalid_or_nonstring_values(value):
    with pytest.raises((TypeError, ValueError)):
        action_date(value)


@pytest.mark.parametrize("saved", ["2026-10-12", "invalid saved context"])
def test_explicit_action_date_overrides_environment_without_mutating_it(monkeypatch, saved):
    monkeypatch.setenv(ENVIRONMENT_KEY, saved)
    assert action_date("2026-10-13") == "2026-10-13"
    assert os.environ[ENVIRONMENT_KEY] == saved


def test_environment_context_matches_only_the_selected_night(monkeypatch):
    monkeypatch.setenv(ENVIRONMENT_KEY, "2026-10-13")
    assert action_date() == "2026-10-13"
    assert continuing()
    assert continuing("2026-10-13")
    assert not continuing("2026-10-12")
    assert not continuing("2026-10-14")


def test_invalid_environment_cannot_supply_continuation(monkeypatch):
    monkeypatch.setenv(ENVIRONMENT_KEY, "NaT")
    with pytest.raises(ValueError, match="exact action date"):
        continuing()
    with pytest.raises(ValueError, match="exact action date"):
        deadline_expired("2026-10-13T11:00Z", "2026-10-13T11:00Z")


@pytest.mark.parametrize("offset,expired", [
    (pd.Timedelta(nanoseconds=-1), False),
    (pd.Timedelta(0), True),
    (pd.Timedelta(nanoseconds=1), True),
])
def test_no_context_expires_at_the_exact_deadline(offset, expired):
    deadline = pd.Timestamp("2026-10-13T11:00Z")
    assert deadline_expired(deadline + offset, deadline) is expired


@pytest.mark.parametrize("offset", [
    pd.Timedelta(nanoseconds=-1), pd.Timedelta(0), pd.Timedelta(days=3),
])
def test_selected_night_continues_through_a_missed_deadline(monkeypatch, offset):
    monkeypatch.setenv(ENVIRONMENT_KEY, "2026-10-13")
    deadline = pd.Timestamp("2026-10-13T11:00Z")
    assert not deadline_expired(deadline + offset, deadline)


@pytest.mark.parametrize("selected", ["2026-10-12", "2026-10-14"])
def test_continuation_of_another_night_does_not_bypass_expiry(monkeypatch, selected):
    monkeypatch.setenv(ENVIRONMENT_KEY, selected)
    assert deadline_expired("2026-10-13T11:00Z", "2026-10-13T11:00Z")


@pytest.mark.parametrize("selected,expired", [
    ("2026-10-13", False), ("2026-10-14", True),
])
def test_deadline_uses_its_pacific_action_date_across_utc_midnight(monkeypatch, selected, expired):
    monkeypatch.setenv(ENVIRONMENT_KEY, selected)
    deadline = pd.Timestamp("2026-10-14T00:30Z")  # October 13, 17:30 Pacific.
    assert deadline_expired(deadline + pd.Timedelta(days=1), deadline) is expired


@pytest.mark.parametrize("day,expected_utc", [
    ("2026-03-07", "2026-03-07T12:00Z"),
    ("2026-03-08", "2026-03-08T11:00Z"),
    ("2026-10-31", "2026-10-31T11:00Z"),
    ("2026-11-01", "2026-11-01T12:00Z"),
])
def test_pacific_deadline_keeps_its_action_day_across_dst(monkeypatch, day, expected_utc):
    deadline = pd.Timestamp(f"{day}T04:00", tz="America/Los_Angeles")
    assert deadline.tz_convert("UTC") == pd.Timestamp(expected_utc)
    monkeypatch.setenv(ENVIRONMENT_KEY, day)
    assert not deadline_expired(deadline, deadline)
    monkeypatch.setenv(ENVIRONMENT_KEY, "2026-01-01")
    assert deadline_expired(deadline, deadline)


def test_temporary_context_restores_parent_process_environment(monkeypatch):
    monkeypatch.setenv(ENVIRONMENT_KEY, "2026-10-12")
    with monkeypatch.context() as child_context:
        child_context.setenv(ENVIRONMENT_KEY, "2026-10-13")
        assert continuing("2026-10-13")
        assert not deadline_expired("2026-10-14T11:00Z", "2026-10-13T11:00Z")
    assert os.environ[ENVIRONMENT_KEY] == "2026-10-12"
    assert continuing("2026-10-12")
    assert not continuing("2026-10-13")
    assert deadline_expired("2026-10-14T11:00Z", "2026-10-13T11:00Z")


def test_temporary_context_leaves_no_environment_when_parent_had_none(monkeypatch):
    with monkeypatch.context() as child_context:
        child_context.setenv(ENVIRONMENT_KEY, "2026-10-13")
        assert continuing("2026-10-13")
    assert ENVIRONMENT_KEY not in os.environ
    assert not continuing()
